"""Kibar HTTP istemcisi: robots.txt, host başına sıra + gecikme, sınırlı retry."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import validate_public_url

USER_AGENT = "RotaLinkCampaignBot/1.0 (+https://rotalink.tr)"
MAX_BYTES = 8 * 1024 * 1024


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    content: bytes = b""
    content_type: str = ""
    error: str = ""
    # network | http | robots | blocked | too_large | invalid
    error_kind: str = ""

    @property
    def is_pdf(self) -> bool:
        return "pdf" in self.content_type.lower() or self.url.lower().split("?")[0].endswith(".pdf")


class Fetcher:
    def __init__(
        self,
        timeout: float = 25.0,
        retries: int = 2,
        per_host_delay: float = 1.5,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.per_host_delay = per_host_delay
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/json,application/pdf;q=0.9,*/*;q=0.5",
                "Accept-Language": "tr-TR,tr;q=0.9",
            }
        )
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _raw_get(self, url: str) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock_for(host):
            wait = self._last_hit.get(host, 0) + self.per_host_delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
                chunks, size = [], 0
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        r.close()
                        return FetchResult(
                            url, False, r.status_code, error="response too large", error_kind="too_large"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
            except requests.RequestException as e:
                return FetchResult(url, False, error=f"{type(e).__name__}: {e}", error_kind="network")
            finally:
                self._last_hit[host] = time.monotonic()
        final_host = (urlsplit(r.url).hostname or "").lower()
        if final_host != host and not final_host.endswith("." + host) and not host.endswith("." + final_host):
            return FetchResult(
                url, False, r.status_code, error=f"redirected off-site to {final_host}", error_kind="blocked"
            )
        ctype = r.headers.get("content-type", "")
        if r.status_code in (401, 403, 429) or (r.status_code == 503 and b"cloudflare" in body[:4000].lower()):
            return FetchResult(url, False, r.status_code, body, ctype, f"HTTP {r.status_code}", "blocked")
        if r.status_code >= 400:
            return FetchResult(url, False, r.status_code, body, ctype, f"HTTP {r.status_code}", "http")
        return FetchResult(url, True, r.status_code, body, ctype)

    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._guard:
            if base in self._robots:
                return self._robots[base]
        res = self._raw_get(base + "/robots.txt")
        rp: RobotFileParser | None = RobotFileParser()
        if res.ok:
            rp.parse(res.content.decode("utf-8", errors="replace").splitlines())
        elif res.status in (401, 403):
            rp.disallow_all = True
        elif res.error_kind == "network" or (res.status or 0) >= 500:
            rp = None  # bilinmiyor → istek atılmaz, kaynak başarısız sayılır
        else:
            rp.allow_all = True  # 404 vb. → robots yok
        with self._guard:
            self._robots[base] = rp
        return rp

    def get(self, url: str, allowed_hosts: set[str]) -> FetchResult:
        try:
            validate_public_url(url)
        except ValueError as e:
            return FetchResult(url, False, error=str(e), error_kind="invalid")
        host = (urlsplit(url).hostname or "").lower()
        if host not in allowed_hosts:
            return FetchResult(url, False, error=f"host not in trusted list: {host}", error_kind="blocked")
        rp = self._robots_for(url)
        if rp is None:
            return FetchResult(url, False, error="robots.txt unreachable", error_kind="network")
        if not rp.can_fetch(USER_AGENT, url):
            return FetchResult(url, False, error="disallowed by robots.txt", error_kind="robots")
        last = FetchResult(url, False, error="not attempted", error_kind="network")
        for attempt in range(self.retries + 1):
            last = self._raw_get(url)
            transient = last.error_kind == "network" or (last.error_kind == "http" and (last.status or 0) >= 500)
            if last.ok or not transient:
                return last
            if attempt < self.retries:
                time.sleep(2 * (attempt + 1))
        return last
