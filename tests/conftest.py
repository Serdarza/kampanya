from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.config import Source  # noqa: E402
from collector.dates import TR_TZ  # noqa: E402
from collector.fetch import FetchResult  # noqa: E402

NOW = datetime(2026, 9, 27, 7, 0, tzinfo=TR_TZ)
LIST_URL = "https://kampanya.example.gov.tr/kampanyalar/"


class FakeFetcher:
    """URL → (status, html). Kayıtlı olmayan URL ağ hatası döner."""

    def __init__(self, pages: dict[str, tuple[int, str] | str]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, allowed_hosts: set[str]) -> FetchResult:
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            return FetchResult(url, False, error="ConnectionError", error_kind="network")
        status, html = page if isinstance(page, tuple) else (200, page)
        body = html.encode("utf-8")
        if status >= 400:
            return FetchResult(url, False, status, body, "text/html", f"HTTP {status}", "http")
        return FetchResult(url, True, status, body, "text/html; charset=utf-8")


def list_page(*slugs: str) -> str:
    items = "".join(f'<li><a href="/kampanya/{s}/">{s}</a></li>' for s in slugs)
    return f"<html><body><main><ul class='liste'>{items}</ul></main></body></html>"


def detail_page(title: str, *paragraphs: str) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        "<html><head><title>" + title + " | Kurum</title></head><body>"
        "<nav>Anasayfa Hakkımızda İletişim</nav>"
        f"<article><h1>{title}</h1>{body}</article>"
        "<footer>Tüm hakları saklıdır.</footer></body></html>"
    )


def make_source(**kw) -> Source:
    base = dict(
        id="test-kaynak",
        name="Test Kurumu",
        organization="Test Kurumu",
        url=LIST_URL,
        type="html_list",
        category="other",
        audiences=[],
        link_pattern=r"^https://kampanya\.example\.gov\.tr/kampanya/[a-z0-9-]+/?$",
    )
    base.update(kw)
    return Source(**base)


@pytest.fixture
def now() -> datetime:
    return NOW
