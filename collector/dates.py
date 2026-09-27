"""Türkçe tarih ifadelerinden başlangıç / bitiş tarihi çıkarımı.

Yalnızca metinde açıkça yazan tarihler kullanılır; tahmin yapılmaz.
Türkiye 2016'dan beri sabit UTC+3 kullanır.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone

from .text import fold

TR_TZ = timezone(timedelta(hours=3), "Europe/Istanbul")

_MONTHS = {
    "ocak": 1,
    "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "agustos": 8,
    "eylul": 9,
    "ekim": 10,
    "kasim": 11,
    "aralik": 12,
}

_NUM_DATE = r"(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4})"
_ISO_DATE = r"(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})"
_TXT_DATE = r"(?P<td>\d{1,2})\s+(?P<tm>" + "|".join(_MONTHS) + r")\s+(?P<ty>\d{4})"
DATE_RE = re.compile(rf"(?:{_NUM_DATE}|{_ISO_DATE}|{_TXT_DATE})")

_TIME_AFTER = re.compile(
    r"^\s*(?:'?\s*[a-z]{1,3}\b)?\s*(?:tarih[a-z]*\s*)?(?:saat\s*)?(?P<h>[01]?\d|2[0-3])[:.](?P<mi>[0-5]\d)"
)

# Tarihten SONRA gelirse bitiş anlamı taşıyan ifadeler.
_SUFFIX = r"(?:'?\s*[a-z]{1,3}\b)?"
_END_AFTER = re.compile(
    rf"^\s*{_SUFFIX}\s*(?:tarih[a-z]*\s*|gunu\s*)?"
    rf"(?:saat\s*\d{{1,2}}[:.]\d{{2}}\s*{_SUFFIX}\s*)?"
    r"(?:kadar|son\s*gun|sona\s*er|bitecek)"
)
# Tarihten ÖNCE gelirse bitiş anlamı taşıyan ifadeler.
_END_BEFORE = re.compile(
    r"(?:bitis\s*tarihi|son\s*gecerlilik\s*tarihi|son\s*kullanma\s*tarihi|gecerlilik\s*tarihi"
    r"|son\s*gun|kampanya\s*bitis|bitis)\s*[:\-–]?\s*$"
)
_START_AFTER = re.compile(rf"^\s*{_SUFFIX}\s*(?:tarih[a-z]*\s*)?(?:itibaren|baslayacak|baslar)")
_START_BEFORE = re.compile(r"(?:baslangic\s*tarihi|baslama\s*tarihi)\s*[:\-–]?\s*$")
_RANGE_SEP = re.compile(r"^\s*(?:[-–—]|ile|ve)\s*$")
_PUBLISHED_BEFORE = re.compile(
    r"(?:guncelleme\s*tarihi|yayin\s*tarihi|yayinlanma\s*tarihi|olusturulma\s*tarihi|eklenme\s*tarihi)\s*[:\-–]?\s*$"
)


@dataclass
class DateInfo:
    start: datetime | None = None
    end: datetime | None = None
    published: datetime | None = None


def _match_to_date(m: re.Match) -> datetime | None:
    try:
        if m.group("d"):
            return datetime(int(m.group("y")), int(m.group("m")), int(m.group("d")), tzinfo=TR_TZ)
        if m.group("iy"):
            return datetime(int(m.group("iy")), int(m.group("im")), int(m.group("id")), tzinfo=TR_TZ)
        return datetime(int(m.group("ty")), _MONTHS[m.group("tm")], int(m.group("td")), tzinfo=TR_TZ)
    except (ValueError, KeyError, TypeError):
        return None


def _end_of(day: datetime, after: str) -> datetime:
    t = _TIME_AFTER.match(after)
    if t:
        return day.replace(hour=int(t.group("h")), minute=int(t.group("mi")), second=59)
    return datetime.combine(day.date(), time(23, 59, 59), tzinfo=TR_TZ)


def extract_dates(text: str) -> DateInfo:
    """Metinden kampanya başlangıç/bitiş/yayın tarihi çıkarır."""
    t = fold(text)
    info = DateInfo()
    matches = [(m, _match_to_date(m)) for m in DATE_RE.finditer(t)]
    matches = [(m, d) for m, d in matches if d is not None and 2000 <= d.year <= 2100]
    ends: list[datetime] = []
    starts: list[datetime] = []
    for i, (m, d) in enumerate(matches):
        before = t[max(0, m.start() - 40) : m.start()]
        after = t[m.end() : m.end() + 60]
        if _PUBLISHED_BEFORE.search(before):
            info.published = info.published or d
            continue
        nxt = matches[i + 1] if i + 1 < len(matches) else None
        if nxt and _RANGE_SEP.match(t[m.end() : nxt[0].start()]):
            starts.append(d)
            continue
        prev = matches[i - 1] if i > 0 else None
        if prev and _RANGE_SEP.match(t[prev[0].end() : m.start()]):
            ends.append(_end_of(d, after))
            continue
        if _END_AFTER.match(after) or _END_BEFORE.search(before):
            ends.append(_end_of(d, after))
        elif _START_AFTER.match(after) or _START_BEFORE.search(before):
            starts.append(d)
    if ends:
        info.end = max(ends)
    if starts:
        info.start = min(starts)
    return info


def is_expired(end: datetime | None, now: datetime) -> bool:
    return end is not None and end < now


def now_tr() -> datetime:
    return datetime.now(TR_TZ)


def to_iso_z(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_tr(d: datetime | None) -> str | None:
    return d.astimezone(TR_TZ).isoformat() if d else None


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TR_TZ)


def format_tr(d: datetime) -> str:
    return d.astimezone(TR_TZ).strftime("%d.%m.%Y")
