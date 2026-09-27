"""Türkçe metin normalizasyonu ve URL anahtarları."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")

_TRACKING_PARAMS = re.compile(r"^(utm_.*|hl|fbclid|gclid|yclid|mc_.*|ref|_ga)$", re.I)


def tr_lower(s: str) -> str:
    return s.translate(_TR_LOWER).lower()


def tr_upper(s: str) -> str:
    return s.translate(_TR_UPPER).upper()


def fold(s: str) -> str:
    """Küçük harf + Türkçe karakterleri ASCII'ye indirger (karşılaştırma için)."""
    s = unicodedata.normalize("NFC", tr_lower(s))
    s = s.replace("’", "'").replace("‘", "'")
    return s.translate(_FOLD)


def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


_FRONT = set("eiöüEİÖÜ")
_VOWELS = set("aeıioöuüAEIİOÖUÜ")
_LOWER_WORDS = {"ve", "ile", "veya", "ya", "de", "da"}


def _lower_upper_word(w: str) -> str:
    """Büyük harfli kelimeyi küçültür; noktasız 'I' için ünlü uyumuna bakar (MEDICANA → medicana)."""
    out = []
    for i, ch in enumerate(w):
        if ch == "I":
            prev = next((c for c in reversed(w[:i]) if c in _VOWELS), None)
            ref = prev or next((c for c in w[i + 1 :] if c in _VOWELS and c != "I"), None)
            out.append("i" if ref in _FRONT else "ı")
        else:
            out.append(tr_lower(ch))
    return "".join(out)


def tr_title_case(s: str) -> str:
    """ "KOÇAK OPTİK & LENS" → "Koçak Optik & Lens"."""
    words = []
    for n, w in enumerate(clean_ws(s).split(" ")):
        if len(w) <= 1 or not any(c.isalpha() for c in w):
            words.append(w)
            continue
        if w.isupper() and w.strip(".,") in _KEEP_UPPER:
            words.append(w)
            continue
        low = _lower_upper_word(w) if w.isupper() else w[0] + tr_lower(w[1:])
        if n > 0 and low in _LOWER_WORDS:
            words.append(low)
            continue
        words.append(tr_upper(low[0]) + low[1:])
    return " ".join(words)


_KEEP_UPPER = {"MEB", "MSB", "TSK", "PTT", "THY", "TCDD", "DSİ", "SGK", "OYAK", "EGM", "AŞ", "A.Ş", "KDV"}

_STOP = {
    "ve",
    "ile",
    "icin",
    "da",
    "de",
    "ta",
    "te",
    "den",
    "dan",
    "bir",
    "bu",
    "su",
    "ozel",
    "indirim",
    "indirimi",
    "indirimli",
    "kampanya",
    "kampanyasi",
    "kampanyalari",
    "firsat",
    "firsati",
    "avantaj",
    "avantaji",
    "ek",
    "personel",
    "personeli",
    "personeline",
    "personeller",
    "calisan",
    "calisanlari",
    "calisanlarina",
    "mensup",
    "mensuplari",
    "mensuplarina",
    "ogretmen",
    "ogretmenler",
    "ogretmenlere",
    "meb",
    "tsk",
    "msb",
    "kamu",
    "memur",
    "memurlara",
    "emekli",
    "emeklileri",
    "emeklilerine",
    "aile",
    "ailelerine",
    "yakinlarina",
    "tum",
    "yeni",
    "sezon",
    "tl",
    "as",
    "a.s",
    "anlasmasi",
    "anlasma",
    "detaylari",
    "hakkinda",
    "sagligi",
    "saglik",
    "emniyet",
    "jandarma",
    "polis",
    "turk",
    "silahli",
    "kuvvetleri",
    "milli",
    "savunma",
    "bakanligi",
    "egitim",
}


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9%]+", fold(s)) if t]


def distinctive_tokens(s: str) -> set[str]:
    """Başlıktaki marka/konu kelimeleri (genel kampanya kelimeleri hariç)."""
    out = set()
    for t in tokens(s):
        t = re.sub(r"(nin|nın|nun|in|un|ın|de|da|te|ta|den|dan|ten|tan)$", "", t) if len(t) > 5 else t
        if t in _STOP or len(t) < 3 or t.startswith("%") or t.isdigit():
            continue
        out.add(t)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalize_url(url: str) -> str:
    """Karşılaştırma anahtarı: şema/host küçük, fragment ve izleme parametreleri yok."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if p.scheme not in ("http", "https"):
        return url.strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path) or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)])
    return urlunsplit(("https", host, fold(path), query, ""))


def site_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
