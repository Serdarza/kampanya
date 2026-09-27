"""campaign-sources.json yükleme ve doğrulama."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .classify import AUDIENCES, CATEGORY_LABELS

SOURCE_TYPES = {"html_list", "html_page", "rss", "json_api", "pdf"}
CATEGORIES = set(AUDIENCES) | set(CATEGORY_LABELS) | {"other", "doctor", "nurse", "police"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class ConfigError(ValueError):
    pass


@dataclass
class Source:
    id: str
    name: str
    organization: str
    url: str
    type: str
    active: bool = True
    category: str = "other"
    audiences: list[str] = field(default_factory=list)
    audience_from_source: bool = False
    link_pattern: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    max_items: int = 60
    follow_pdfs: bool = False
    title_selector: str | None = None
    content_selector: str | None = None
    organization_from_title: bool = False
    json_items_path: str | None = None
    json_fields: dict[str, str] = field(default_factory=dict)
    # Yalnızca html_page: sayfa başlığı kampanyayı anlatmıyorsa editörün belirlediği sabit başlık.
    title: str | None = None
    # Başlıkta kitle geçmiyorsa eklenecek sabit ek (ör. "HTKSEN Üyelerine Özel").
    title_suffix: str | None = None

    @property
    def hosts(self) -> set[str]:
        return {_host(self.url), *(h.lower() for h in self.allowed_hosts)}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def validate_public_url(url: str) -> None:
    """Yalnızca http(s), kimlik bilgisi içermeyen, özel ağ olmayan adresler."""
    try:
        p = urlsplit(url)
    except ValueError as e:
        raise ConfigError(f"invalid url: {url!r}") from e
    if p.scheme not in ("http", "https"):
        raise ConfigError(f"unsupported scheme: {url!r}")
    if p.username or p.password:
        raise ConfigError(f"credentials in url: {url!r}")
    host = (p.hostname or "").lower()
    if not host or "." not in host or host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise ConfigError(f"non-public host: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ConfigError(f"private address: {url!r}")


def _source_from_dict(raw: dict, defaults: dict) -> Source:
    d = {**defaults, **raw}
    sid = str(d.get("id", "")).strip()
    if not _ID_RE.match(sid):
        raise ConfigError(f"invalid source id: {sid!r}")
    stype = d.get("type")
    if stype not in SOURCE_TYPES:
        raise ConfigError(f"{sid}: invalid type {stype!r}")
    url = str(d.get("url", "")).strip()
    validate_public_url(url)
    audiences = list(d.get("audiences") or [])
    bad = [a for a in audiences if a not in AUDIENCES]
    if bad:
        raise ConfigError(f"{sid}: unknown audiences {bad}")
    category = d.get("category", "other")
    if category not in CATEGORIES:
        raise ConfigError(f"{sid}: unknown category {category!r}")
    link_pattern = d.get("linkPattern")
    if stype == "html_list":
        if not link_pattern:
            raise ConfigError(f"{sid}: html_list requires linkPattern")
        try:
            re.compile(link_pattern)
        except re.error as e:
            raise ConfigError(f"{sid}: bad linkPattern: {e}") from e
    if d.get("audienceFromSource") and not audiences:
        raise ConfigError(f"{sid}: audienceFromSource requires audiences")
    allowed = [str(h).lower() for h in d.get("allowedHosts") or []]
    for h in allowed:
        validate_public_url(f"https://{h}/")
    return Source(
        id=sid,
        name=str(d.get("name") or sid),
        organization=str(d.get("organization") or d.get("name") or sid),
        url=url,
        type=stype,
        active=bool(d.get("active", True)),
        category=category,
        audiences=audiences,
        audience_from_source=bool(d.get("audienceFromSource", False)),
        link_pattern=link_pattern,
        allowed_hosts=allowed,
        max_items=max(1, min(int(d.get("maxItems", 60)), 200)),
        follow_pdfs=bool(d.get("followPdfs", False)),
        title_selector=d.get("titleSelector"),
        content_selector=d.get("contentSelector"),
        organization_from_title=bool(d.get("organizationFromTitle", False)),
        json_items_path=d.get("itemsPath"),
        json_fields=dict(d.get("fields") or {}),
        title=str(d["title"]).strip() if d.get("title") and stype == "html_page" else None,
        title_suffix=str(d["titleSuffix"]).strip() if d.get("titleSuffix") else None,
    )


def load_sources(path: Path) -> list[Source]:
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    sources = [_source_from_dict(s, defaults) for s in data.get("sources", [])]
    ids = [s.id for s in sources]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate source ids: {sorted(dupes)}")
    return sources
