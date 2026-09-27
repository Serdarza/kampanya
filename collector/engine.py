"""Tarama → sınıflandırma → eşleştirme → güncelleme → süre dolumu."""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .classify import AUDIENCES, CATEGORY_LABELS, audience_labels, classify, detect_audiences
from .config import Source
from .dates import DateInfo, extract_dates, format_tr, from_iso, is_expired, to_iso_tr
from .extract import (
    Page,
    discounts,
    list_links,
    parse_html_page,
    parse_json_api,
    parse_rss,
    pdf_text,
    summarize,
)
from .fetch import Fetcher, FetchResult
from .store import record_key, record_link, record_title
from .text import distinctive_tokens, fold, jaccard, normalize_url, site_of, tr_title_case

log = logging.getLogger("collector")

_DATIVE = {
    "general_public_employee": "Kamu Personeline",
    "civil_servant": "Memurlara",
    "teacher": "Öğretmenlere",
    "university": "Akademisyenlere",
    "healthcare": "Sağlık Çalışanlarına",
    "doctor": "Hekimlere",
    "nurse": "Hemşirelere",
    "police": "Emniyet Mensuplarına",
    "military": "TSK Personeline",
    "gendarmerie": "Jandarma Personeline",
    "coast_guard": "Sahil Güvenlik Personeline",
    "municipal": "Belediye Personeline",
    "public_worker": "Kamu İşçilerine",
}
_SHORT = {
    "general_public_employee": "Kamu",
    "civil_servant": "Memur",
    "teacher": "Öğretmen",
    "university": "Akademisyen",
    "healthcare": "Sağlık",
    "doctor": "Hekim",
    "nurse": "Hemşire",
    "police": "Emniyet",
    "military": "TSK",
    "gendarmerie": "Jandarma",
    "coast_guard": "Sahil Güvenlik",
    "municipal": "Belediye",
    "public_worker": "Kamu İşçisi",
}
_TITLE_HAS_OFFER = re.compile(r"(indirim|kampanya|%|avantaj|tarife|firsat|ucretsiz|(?:e|a|ne|na) ozel\b)")
_POSSESSIVE = re.compile(r"^(.{2,60}?)['’](?:n?[iıuü]n)\b")


# --------------------------------------------------------------------------- scan


@dataclass
class Candidate:
    source: Source
    title: str
    text: str
    link: str
    dates: DateInfo
    audiences: list[str]
    discounts: list[str]


@dataclass
class SourceReport:
    source: Source
    status: str = "ok"  # ok | failed | parse_failed | skipped
    error: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    item_errors: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def _fail(rep: SourceReport, res: FetchResult) -> SourceReport:
    rep.status = "failed"
    rep.error = f"{res.error_kind}: {res.error}"
    return rep


def _make_candidate(
    src: Source, title: str, text: str, link: str, rep: SourceReport, extra_dates: DateInfo | None = None
) -> None:
    if not title or not text:
        rep.item_errors.append(f"{link}: empty title/content (parse)")
        return
    c = classify(title, text, src.audiences, src.audience_from_source)
    if not (c.is_campaign and c.is_public_employee):
        rep.rejected.append((link, c.reason))
        return
    dates = extract_dates(f"{title}\n{text}")
    if extra_dates:
        dates.end = extra_dates.end or dates.end
        dates.start = extra_dates.start or dates.start
        dates.published = extra_dates.published or dates.published
    rep.candidates.append(Candidate(src, title, text, link, dates, c.audiences, discounts(f"{title}\n{text}")))


def _page_candidate(src: Source, fetcher: Fetcher, url: str, rep: SourceReport, hint_title: str = "") -> None:
    res = fetcher.get(url, src.hosts)
    if not res.ok:
        rep.item_errors.append(f"{url}: {res.error_kind}: {res.error}")
        return
    try:
        if res.is_pdf:
            text = pdf_text(res.content)
            page = Page(url, hint_title, text)
        else:
            page = parse_html_page(res.content, url, src.title_selector, src.content_selector)
    except Exception as e:  # bozuk HTML/PDF tek kaydı düşürür, kaynağı değil
        rep.item_errors.append(f"{url}: parse error {type(e).__name__}: {e}")
        return
    text = page.text
    if src.follow_pdfs:
        for pdf in page.pdf_links[:2]:
            pres = fetcher.get(pdf, src.hosts)
            if pres.ok:
                try:
                    text += "\n" + pdf_text(pres.content)
                except Exception as e:
                    rep.item_errors.append(f"{pdf}: pdf parse error {e}")
    extra = DateInfo(start=page.jsonld_start, end=page.jsonld_end, published=page.published)
    title = src.title if src.title and url == src.url else (page.title or hint_title)
    _make_candidate(src, title, text, url, rep, extra)


def scan_source(src: Source, fetcher: Fetcher) -> SourceReport:
    rep = SourceReport(src)
    try:
        if src.type in ("html_page", "pdf"):
            before = len(rep.item_errors)
            _page_candidate(src, fetcher, src.url, rep)
            if len(rep.item_errors) > before and not rep.candidates:
                err = rep.item_errors[-1]
                rep.status = "parse_failed" if "parse" in err else "failed"
                rep.error = err
            return rep

        res = fetcher.get(src.url, src.hosts)
        if not res.ok:
            return _fail(rep, res)

        if src.type == "html_list":
            links = list_links(res.content, src.url, src.link_pattern or "")
            if not links:
                rep.status = "parse_failed"
                rep.error = "no links matched linkPattern (page structure may have changed)"
                return rep
            for url, anchor in links[: src.max_items]:
                _page_candidate(src, fetcher, url, rep, hint_title=anchor)
            if links and not rep.candidates and len(rep.item_errors) >= len(links[: src.max_items]):
                rep.status = "parse_failed"
                rep.error = "all detail pages failed"
            return rep

        if src.type == "rss":
            items = parse_rss(res.content, src.url)
            if not items:
                rep.status = "parse_failed"
                rep.error = "feed has no items"
                return rep
            for it in items[: src.max_items]:
                _make_candidate(src, it.title, it.text or it.title, it.link, rep, DateInfo(published=it.published))
            return rep

        if src.type == "json_api":
            rows = parse_json_api(res.content, src.url, src.json_items_path, src.json_fields)
            if not rows:
                rep.status = "parse_failed"
                rep.error = "json api returned no items"
                return rep
            for row in rows[: src.max_items]:
                extra = DateInfo(
                    start=from_iso(str(row["start"])) if row.get("start") else None,
                    end=from_iso(str(row["end"])) if row.get("end") else None,
                    published=from_iso(str(row["published"])) if row.get("published") else None,
                )
                _make_candidate(
                    src, str(row["title"]), str(row.get("description") or row["title"]), row["link"], rep, extra
                )
            return rep
    except Exception as e:
        rep.status = "parse_failed"
        rep.error = f"{type(e).__name__}: {e}"
    return rep


def scan_all(sources: list[Source], fetcher: Fetcher, workers: int = 6) -> list[SourceReport]:
    active = [s for s in sources if s.active]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda s: scan_source(s, fetcher), active))


# --------------------------------------------------------------------------- build


def _primary_audiences(c: Candidate) -> list[str]:
    keys = [a for a in c.audiences if a in _DATIVE]
    if c.source.audience_from_source:
        keys = [a for a in c.source.audiences if a in _DATIVE] + [a for a in keys if a not in c.source.audiences]
    specific = [k for k in keys if k != "general_public_employee"]
    return (specific or keys)[:2]


def build_title(c: Candidate) -> str:
    t = c.title.strip()
    if t.isupper() or (sum(ch.isupper() for ch in t) > 0.7 * max(1, sum(ch.isalpha() for ch in t))):
        t = tr_title_case(t)
    if (
        c.source.title_suffix
        and not re.search(r"uye(?:ler|lerimiz)|mensup|personel|calisan", fold(t))
        and not detect_audiences(t)
    ):
        t = f"{t} – {c.source.title_suffix}"
    if _TITLE_HAS_OFFER.search(fold(t)):
        return t
    aud = _primary_audiences(c)
    if not aud:
        return t
    target = _DATIVE[aud[0]] if len(aud) == 1 else f"{_SHORT[aud[0]]} ve {_DATIVE[aud[1]]}"
    pcts = sorted({int(d[1:]) for d in c.discounts if d.startswith("%")})
    if not pcts:
        return f"{t}: {target} Özel Kampanya"
    if len(pcts) == 1:
        return f"{t}: {target} Özel %{pcts[0]} İndirim"
    return f"{t}: {target} Özel %{pcts[-1]}'{_dative_suffix(pcts[-1])} Varan İndirim"


def _dative_suffix(n: int) -> str:
    """%25'e, %20'ye, %30'a, %6'ya — sayının okunuşuna göre yönelme eki."""
    if n % 100 == 0:
        return "e"
    if n % 10 == 0:
        return {10: "a", 20: "ye", 30: "a", 40: "a", 50: "ye", 60: "a", 70: "e", 80: "e", 90: "a"}[n % 100]
    return {1: "e", 2: "ye", 3: "e", 4: "e", 5: "e", 6: "ya", 7: "ye", 8: "e", 9: "a"}[n % 10]


def build_organization(c: Candidate) -> str:
    if c.source.organization_from_title:
        t = c.title.strip()
        m = _POSSESSIVE.match(t)
        brand = m.group(1) if m else (t if len(t.split()) <= 5 else "")
        if brand:
            return tr_title_case(brand) if brand.isupper() else brand
    return c.source.organization


def build_tags(c: Candidate) -> list[str]:
    tags = audience_labels([a for a in dict.fromkeys(c.audiences)])
    label = CATEGORY_LABELS.get(c.source.category)
    if label:
        tags.append(label)
    return list(dict.fromkeys(tags))


def build_description(c: Candidate) -> str:
    desc = summarize(c.text, c.title)
    if not desc:
        # Kaynakta başlık dışında metin yok (ör. yalnızca görsel); içerik uydurulmaz.
        desc = "Kampanya ayrıntıları resmi kaynak sayfasında yer almaktadır."
    if c.dates.end and format_tr(c.dates.end) not in desc and fold(format_tr(c.dates.end)) not in fold(desc):
        # Bitiş tarihi kaynakta var ama seçilen cümlelerde yoksa ekle.
        desc = f"{desc} Kampanya {format_tr(c.dates.end)} tarihine kadar geçerlidir.".strip()
    return desc


def fingerprint(c: Candidate) -> str:
    parts = [
        fold(c.title),
        normalize_url(c.link),
        to_iso_tr(c.dates.start) or "",
        to_iso_tr(c.dates.end) or "",
        ",".join(sorted(c.discounts)),
        ",".join(sorted(c.audiences)),
        hashlib.sha1(fold(summarize(c.text, c.title)).encode(), usedforsecurity=False).hexdigest()[:10],
    ]
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:16]


def _date_only_z(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT00:00:00Z")


def new_record(c: Candidate, now: datetime) -> dict:
    rid = "auto-" + hashlib.sha1(normalize_url(c.link).encode(), usedforsecurity=False).hexdigest()[:12]
    rec = {
        "id": rid,
        "baslik": build_title(c),
        "kurum": build_organization(c),
        "aciklama": build_description(c),
        "link": c.link,
        "tarih": _date_only_z(c.dates.published or now),
    }
    tags = build_tags(c)
    if tags:
        rec["etiketler"] = tags
    return rec


# --------------------------------------------------------------------------- match


def _record_audiences(rec: dict) -> set[str]:
    return set(detect_audiences(f"{record_title(rec)}\n{rec.get('aciklama', '')}"))


def find_exact(c: Candidate, records: list[dict]) -> int | None:
    key = normalize_url(c.link)
    for i, r in enumerate(records):
        if record_link(r) and normalize_url(record_link(r)) == key:
            return i
    return None


def find_fuzzy(c: Candidate, records: list[dict], run_links: set[str]) -> tuple[int, bool] | None:
    """(index, same_site). URL'si değişmiş aynı kampanya veya başka resmi sitedeki aynı kampanya.

    Bağlantısı bu taramada ayrı bir kampanya olarak hâlâ görülen kayıtlar eşleştirilmez
    (ör. "Damat Tween" ile "D'S Damat" farklı kampanyalardır).
    """
    c_tokens = distinctive_tokens(c.title)
    if not c_tokens:
        return None
    c_site = site_of(c.link)
    c_pcts = {d for d in c.discounts if d.startswith("%")}
    best: tuple[float, int, bool] | None = None
    for i, r in enumerate(records):
        link = record_link(r)
        if not link or link.startswith("mailto:"):
            continue
        r_tokens = distinctive_tokens(record_title(r))
        if not r_tokens:
            continue
        score = jaccard(c_tokens, r_tokens)
        subset = c_tokens <= r_tokens or r_tokens <= c_tokens
        same_site = site_of(link) == c_site
        if same_site:
            ok = (score >= 0.5 or subset) and normalize_url(link) not in run_links
        else:
            r_text = f"{record_title(r)}\n{r.get('aciklama', '')}"
            shared_audience = bool(_record_audiences(r) & set(c.audiences))
            r_pcts = {d for d in discounts(r_text) if d.startswith("%")}
            same_offer = bool(c_pcts & r_pcts)
            ok = shared_audience and (score >= 0.75 or (subset and same_offer))
        if ok:
            cand = (score + (0.5 if same_site else 0), i, same_site)
            best = cand if best is None or cand[0] > best[0] else best
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------- merge


@dataclass
class RunResult:
    records: list[dict]
    state: dict
    added: list[dict] = field(default_factory=list)
    updated: list[tuple[dict, dict]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    expired_removed: list[dict] = field(default_factory=list)
    expired_skipped: list[str] = field(default_factory=list)
    duplicate_records_removed: list[dict] = field(default_factory=list)
    no_date: list[str] = field(default_factory=list)
    upcoming: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.expired_removed or self.duplicate_records_removed)


def _facts_differ_from_record(c: Candidate, rec: dict) -> bool:
    """Manuel kayıtta ilk karşılaşma: kaynak ile metin arasında somut çelişki var mı?"""
    rec_text = f"{record_title(rec)}\n{rec.get('aciklama', '')}"
    rec_end = extract_dates(rec_text).end
    if c.dates.end and rec_end and c.dates.end.date() != rec_end.date():
        return True
    rec_pcts = {d for d in discounts(rec_text) if d.startswith("%")}
    src_pcts = {d for d in c.discounts if d.startswith("%")}
    return bool(rec_pcts and src_pcts and not (rec_pcts & src_pcts))


def _apply_update(rec: dict, c: Candidate) -> dict:
    new = copy.deepcopy(rec)
    auto = str(rec.get("id", "")).startswith("auto-")
    new["aciklama"] = build_description(c)
    if auto:
        new["baslik"] = build_title(c)
        new["kurum"] = build_organization(c)
        tags = build_tags(c)
        if tags:
            new["etiketler"] = tags
    else:
        rec_pcts = {d for d in discounts(record_title(rec)) if d.startswith("%")}
        if rec_pcts and not (rec_pcts & set(c.discounts)):
            new["baslik"] = build_title(c)
    if normalize_url(record_link(rec)) != normalize_url(c.link):
        new["link"] = c.link
    return new


def merge(records: list[dict], state: dict, reports: list[SourceReport], now: datetime) -> RunResult:
    records = copy.deepcopy(records)
    state = copy.deepcopy(state)
    st = state.setdefault("records", {})
    res = RunResult(records, state)
    touched: set[int] = set()
    first_new = len(records)

    candidates = [c for rep in reports for c in rep.candidates]
    run_links = {normalize_url(c.link) for c in candidates}
    # 1. aşama: birebir URL eşleşmeleri kayıtlarını sahiplenir; 2. aşama: kalanlar.
    exact = {id(c): find_exact(c, records) for c in candidates}
    ordered = [c for c in candidates if exact[id(c)] is not None] + [c for c in candidates if exact[id(c)] is None]

    for c in ordered:
        label = f"[{c.source.id}] {c.title}"
        if c.dates.end is None:
            res.no_date.append(label)
        if c.dates.start and c.dates.start > now:
            res.upcoming.append(label)
        if exact[id(c)] is not None:
            m: tuple[int, bool] | None = (exact[id(c)], True)
        else:
            m = find_fuzzy(c, records, run_links)
        if m is None:
            if is_expired(c.dates.end, now):
                res.expired_skipped.append(label)
                continue
            rec = new_record(c, now)
            records.append(rec)
            touched.add(len(records) - 1)
            res.added.append(rec)
            st[record_key(rec)] = _state_entry(c, now, fingerprint(c), None)
            continue

        idx, same_site = m
        if idx in touched or not same_site:
            res.duplicates.append(label)
            continue
        touched.add(idx)
        rec = records[idx]
        key = record_key(rec)
        prev = st.get(key)
        fp = fingerprint(c)
        if is_expired(c.dates.end, now):
            # Kaynak bitiş tarihini geçmiş gösteriyor → güncelleme yok, süre dolumu kaldırır.
            st[key] = _state_entry(c, now, fp, prev)
            continue
        changed = (prev.get("fingerprint") != fp) if prev else _facts_differ_from_record(c, rec)
        if changed:
            new = _apply_update(rec, c)
            if new != rec:
                records[idx] = new
                res.updated.append((rec, new))
                if record_key(new) != key:
                    st.pop(key, None)
                    key = record_key(new)
        else:
            res.duplicates.append(label)
        st[key] = _state_entry(c, now, fp, prev)

    # Yeni kayıtlar listenin başına (uygulama zaten tarihe göre sıralar).
    records[:] = records[first_new:][::-1] + records[:first_new]
    _dedupe_existing(res)
    _expire(res, now)
    return res


def _state_entry(c: Candidate, now: datetime, fp: str, prev: dict | None) -> dict:
    return {
        "source": c.source.id,
        "fingerprint": fp,
        "start": to_iso_tr(c.dates.start),
        "end": to_iso_tr(c.dates.end),
        "firstSeen": (prev or {}).get("firstSeen") or now.date().isoformat(),
    }


def _dedupe_existing(res: RunResult) -> None:
    seen: dict[str, int] = {}
    keep: list[dict] = []
    for rec in res.records:
        link = record_link(rec)
        if not link or link.startswith("mailto:"):
            keep.append(rec)
            continue
        k = normalize_url(link)
        if k in seen:
            res.duplicate_records_removed.append(rec)
            continue
        seen[k] = len(keep)
        keep.append(rec)
    res.records[:] = keep


def record_end(rec: dict, state: dict) -> datetime | None:
    entry = state.get("records", {}).get(record_key(rec))
    if entry and "end" in entry:
        return from_iso(entry["end"]) if entry["end"] else None
    return extract_dates(f"{record_title(rec)}\n{rec.get('aciklama', '')}").end


def _expire(res: RunResult, now: datetime) -> None:
    keep = []
    for rec in res.records:
        end = record_end(rec, res.state)
        if is_expired(end, now):
            res.expired_removed.append(rec)
            res.state["records"].pop(record_key(rec), None)
        else:
            keep.append(rec)
    res.records[:] = keep
    live = {record_key(r) for r in keep}
    for k in list(res.state["records"]):
        if k not in live:
            res.state["records"].pop(k)


__all__ = ["AUDIENCES", "scan_all", "scan_source", "merge", "RunResult", "SourceReport", "Candidate"]
