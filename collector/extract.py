"""HTML / PDF / RSS / JSON içeriklerinden kampanya verisi çıkarma."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .dates import TR_TZ, from_iso
from .text import clean_ws, fold, normalize_url

_NOISE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg", "button"]
_NOISE_CLASS = re.compile(r"(a11y|breadcrumb|cookie|share|social|menu|navbar|sidebar|footer|header|modal)", re.I)
_NOISE_LINE = re.compile(
    r"^(paylas|yazdir|eposta|e-posta|anasayfa|ana sayfa|geri|tiklayiniz\.?|devamini oku|detayli bilgi"
    r"|tum kampanyalar|kampanyalar|iletisim|hakkimizda)\s*:?$"
)
_SITE_SUFFIX = re.compile(
    r"\s+(?:[-|–]\s+|\bMSB\s*\|\s*)(?:T\.C\.|.*Bakanl|.*Müdürl|.*Başkanl|.*Üniversite|.*Belediye).*$"
)
_OFFER_SENT = re.compile(
    r"(%\s?\d|\d\s?%|indirim|kampanya|avantaj|ucretsiz|kapsa|gecerli|tarih|kadar|kosul|ibraz|kimlik"
    r"|yararlan|faydalan|personel|ogretmen|calisan|mensup|ozel|tarife|fiyat|belge|basvuru|uygulan)"
)
_DISCOUNT = re.compile(r"(?:(?<![\w%])%\s?(\d{1,3})(?![\d.,]\d|[0-9a-f]\b|[g-z])|(?<![\d.,])(\d{1,3})\s?%(?![\w]))")
_INLINE_TAGS = ["a", "strong", "b", "em", "i", "u", "span", "small", "sup", "sub", "font", "mark", "abbr"]
_SKIP_SENT = re.compile(r"(tiklayiniz|tiklayin|guncelleme tarihi|yayin tarihi|bilgi icin\s*:|^\W*$)")
_TL_DISCOUNT = re.compile(r"(\d{1,3}(?:[.]\d{3})*)\s?tl\s?(?:indirim|iade)", re.I)


@dataclass
class Page:
    url: str
    title: str
    text: str
    published: datetime | None = None
    jsonld_end: datetime | None = None
    jsonld_start: datetime | None = None
    pdf_links: list[str] = field(default_factory=list)


def soup_of(content: bytes) -> BeautifulSoup:
    return BeautifulSoup(content, "lxml")


def strip_fragment(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def list_links(content: bytes, base_url: str, pattern: str) -> list[tuple[str, str]]:
    """Liste sayfasındaki, desene uyan detay bağlantıları (sıra korunur, tekrarsız)."""
    rx = re.compile(pattern)
    soup = soup_of(content)
    out: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = strip_fragment(urljoin(base_url, href))
        if not rx.search(url):
            continue
        text = clean_ws(a.get_text(" "))
        key = normalize_url(url)
        if key in seen:
            i = seen[key]
            if not out[i][1] and text:
                out[i] = (out[i][0], text)
            continue
        seen[key] = len(out)
        out.append((url, text))
    return out


def _clean_title(t: str) -> str:
    t = clean_ws(t)
    t = _SITE_SUFFIX.sub("", t)
    return t.strip(" -|–")


def _jsonld(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    end = start = None
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            for k in ("validThrough", "endDate", "priceValidUntil"):
                d = from_iso(node.get(k)) if isinstance(node.get(k), str) else None
                if d and (end is None or d > end):
                    end = d
            for k in ("validFrom", "startDate"):
                d = from_iso(node.get(k)) if isinstance(node.get(k), str) else None
                if d and (start is None or d < start):
                    start = d
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return start, end


_URL_DATE = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/")


def _published(soup: BeautifulSoup, url: str) -> datetime | None:
    """Sayfanın yayın tarihi: meta etiketleri, JSON-LD, <time> veya /YYYY/MM/DD/ URL'si."""
    for attrs in (
        {"property": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "pubdate"},
        {"name": "publish-date"},
        {"name": "date"},
    ):
        el = soup.find("meta", attrs=attrs)
        d = from_iso(el.get("content")) if el and isinstance(el.get("content"), str) else None
        if d:
            return published_to_tr(d)
    for s in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', s.string or "")
        d = from_iso(m.group(1)) if m else None
        if d:
            return published_to_tr(d)
    el = soup.find("time", attrs={"datetime": True})
    d = from_iso(el["datetime"]) if el and isinstance(el.get("datetime"), str) else None
    if d:
        return published_to_tr(d)
    m = _URL_DATE.search(url)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TR_TZ)
    return None


def _content_root(soup: BeautifulSoup, title_el: Tag | None, selector: str | None) -> Tag:
    if selector:
        el = soup.select_one(selector)
        if el:
            return el
    if title_el is not None:
        node: Tag | None = title_el
        while node is not None and node.name != "body":
            if len(clean_ws(node.get_text(" "))) >= 250:
                return node
            node = node.parent
    for sel in ("article", "main", "[role=main]", "#content", ".content", "#mainContent"):
        el = soup.select_one(sel)
        if el and len(clean_ws(el.get_text(" "))) >= 150:
            return el
    return soup.body or soup


def parse_html_page(
    content: bytes, url: str, title_selector: str | None = None, content_selector: str | None = None
) -> Page:
    soup = soup_of(content)
    start, end = _jsonld(soup)
    published = _published(soup, url)
    og = soup.find("meta", property="og:title")
    pdfs = [
        strip_fragment(urljoin(url, a["href"]))
        for a in soup.find_all("a", href=True)
        if a["href"].lower().split("?")[0].endswith(".pdf")
    ]
    # Başlık, gürültü temizliğinden önce bulunur: çoğu sitede <h1> bir <header> içindedir.
    title_el = soup.select_one(title_selector) if title_selector else None
    if title_el is None:
        title_el = next((h for h in soup.find_all("h1") if _usable_heading(h)), None)
    keep = {id(title_el), *(id(p) for p in title_el.parents)} if title_el is not None else set()
    for t in soup(_NOISE_TAGS):
        if id(t) not in keep:
            t.decompose()
    for t in soup.find_all(class_=_NOISE_CLASS):
        if t.name not in ("body", "html", "main", "article") and id(t) not in keep:
            t.decompose()

    if title_el is None:
        title_el = next((h for h in soup.find_all(["h1", "h2"]) if _usable_heading(h)), None)
    title = _clean_title(title_el.get_text(" ")) if title_el else ""
    if not title and og and og.get("content"):
        title = _clean_title(og["content"])
    if not title and soup.title and soup.title.string:
        title = _clean_title(soup.title.string)

    root = _content_root(soup, title_el, content_selector)
    for lst in root.find_all(["ul", "ol"]):
        if _is_link_list(lst):
            lst.decompose()
    for t in root.find_all(_INLINE_TAGS):
        t.unwrap()
    for br in root.find_all("br"):
        br.replace_with("\n")
    lines = []
    for raw in root.get_text("\n").splitlines():
        line = clean_ws(raw)
        if not line or _NOISE_LINE.match(fold(line)):
            continue
        if lines and lines[-1] == line:
            continue
        if lines and len(line) <= 20 and re.match(r"^%\s?\d{1,3}\b|^\d{1,3}\s?%", line):
            lines[-1] = f"{lines[-1]} {line}"  # "Nakit ödemelerde" + "%15"
            continue
        lines.append(line)
    return Page(
        url,
        title,
        "\n".join(lines),
        published=published,
        jsonld_start=start,
        jsonld_end=end,
        pdf_links=list(dict.fromkeys(pdfs)),
    )


def _usable_heading(h) -> bool:
    return "sr-only" not in (h.get("class") or []) and len(clean_ws(h.get_text())) >= 3


def _is_link_list(lst) -> bool:
    """ "Benzer kampanyalar" gibi yalnızca bağlantılardan oluşan listeler içerik değildir."""
    if getattr(lst, "decomposed", False) or len(lst.find_all("li")) < 3:
        return False
    total = len(clean_ws(lst.get_text(" ")))
    linked = sum(len(clean_ws(a.get_text(" "))) for a in lst.find_all("a"))
    return total > 0 and linked >= 0.8 * total


def pdf_text(content: bytes, max_pages: int = 15) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts = []
    for page in reader.pages[:max_pages]:
        parts.append(page.extract_text() or "")
    return "\n".join(clean_ws(line) for p in parts for line in p.splitlines() if clean_ws(line))


@dataclass
class FeedItem:
    title: str
    link: str
    text: str
    published: datetime | None = None


def parse_rss(content: bytes, base_url: str) -> list[FeedItem]:
    soup = BeautifulSoup(content, "xml")
    items = []
    for it in soup.find_all(["item", "entry"]):
        title = clean_ws(it.find("title").get_text()) if it.find("title") else ""
        link_el = it.find("link")
        link = ""
        if link_el is not None:
            link = link_el.get("href") or clean_ws(link_el.get_text())
        desc_el = it.find(["description", "summary", "content", "content:encoded"])
        desc = clean_ws(BeautifulSoup(desc_el.get_text(), "lxml").get_text(" ")) if desc_el else ""
        pub = None
        date_el = it.find(["pubDate", "published", "updated"])
        if date_el is not None:
            raw = clean_ws(date_el.get_text())
            try:
                pub = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                pub = from_iso(raw)
        if title and link:
            items.append(FeedItem(title, urljoin(base_url, link), desc, pub))
    return items


def _dig(obj, path: str | None):
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def parse_json_api(content: bytes, base_url: str, items_path: str | None, fields: dict[str, str]) -> list[dict]:
    data = json.loads(content.decode("utf-8-sig"))
    items = _dig(data, items_path)
    if not isinstance(items, list):
        raise ValueError(f"itemsPath {items_path!r} is not a list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {k: _dig(it, fields.get(k, k)) for k in ("title", "link", "description", "start", "end", "published")}
        if row["title"] and row["link"]:
            row["link"] = urljoin(base_url, str(row["link"]))
            out.append(row)
    return out


def summarize(text: str, title: str, max_chars: int = 700) -> str:
    """Kaynak metinden kampanya ile ilgili cümleleri sırayla seçer (özetleyici değil, alıntılayıcı)."""
    title_f = fold(title)
    sentences: list[str] = []
    for line in text.splitlines():
        line = clean_ws(line)
        if not line or fold(line) == title_f or len(line) < 12 or _is_heading(line):
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ0-9%])", line):
            s = re.sub(r"^[^\w%(\"'“]+", "", clean_ws(s))  # madde imleri, emojiler
            fs = fold(s)
            if len(s) < 12 or s[0].islower() or _SKIP_SENT.search(fs) or not _OFFER_SENT.search(fs) or s in sentences:
                continue
            sentences.append(s)
    out: list[str] = []
    size = 0
    for s in sentences:
        s = re.sub(r"[;,]$", ":", s)
        if not re.search(r"[.!?:)]$", s):
            s += "."
        if size + len(s) + 1 > max_chars:
            break
        out.append(s)
        size += len(s) + 1
    if not out:
        body = clean_ws(" ".join(ln for ln in text.splitlines() if fold(clean_ws(ln)) != title_f))
        return body[:max_chars].rsplit(" ", 1)[0] if len(body) > max_chars else body
    return " ".join(out)


def _is_heading(line: str) -> bool:
    """ "İndirim oranı", "Kampanya koşulları" gibi kısa, cümle olmayan başlık satırları."""
    words = len(line.split())
    if words <= 5 and line.rstrip().endswith(":"):
        return True
    return words <= 5 and not re.search(r"[.!?;]$|%|\d", line)


def discounts(text: str) -> list[str]:
    t = fold(text)
    out = []
    for m in _DISCOUNT.finditer(t):
        v = m.group(1) or m.group(2)
        if v and 0 < int(v) <= 100 and f"%{v}" not in out:
            out.append(f"%{v}")
    for m in _TL_DISCOUNT.finditer(t):
        v = f"{m.group(1)} TL"
        if v not in out:
            out.append(v)
    return out


def published_to_tr(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TR_TZ)
