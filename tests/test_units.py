from __future__ import annotations

import json

import pytest
from conftest import NOW

from collector.classify import classify, detect_audiences
from collector.config import ConfigError, load_sources, validate_public_url
from collector.dates import extract_dates, is_expired
from collector.engine import merge
from collector.extract import discounts, summarize
from collector.text import normalize_url, tr_title_case


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("Kampanya 31.12.2026 tarihine kadar geçerlidir.", "2026-12-31 23:59:59"),
        ("Son başvuru: 15 Ekim 2026 saat 17:00'ye kadar", "2026-10-15 17:00:59"),
        ("01.10.2026 - 30.11.2026 tarihleri arasında", "2026-11-30 23:59:59"),
        ("Bitiş tarihi: 2027-01-31", "2027-01-31 23:59:59"),
    ],
)
def test_end_dates_use_turkey_end_of_day(text, end):
    d = extract_dates(text).end
    assert d is not None and d.strftime("%Y-%m-%d %H:%M:%S") == end
    assert d.utcoffset().total_seconds() == 3 * 3600


def test_start_date_and_no_date():
    info = extract_dates("1 Kasım 2026 tarihinden itibaren geçerlidir.")
    assert info.start is not None and info.start.month == 11 and info.end is None
    assert extract_dates("Öğretmenlere özel indirim").end is None
    assert not is_expired(None, NOW)


def test_audience_detection():
    assert detect_audiences("Sağlık çalışanlarına ve hemşirelere özel") == ["healthcare", "nurse"]
    assert detect_audiences("TSK personeli ve jandarma mensupları") == ["military", "gendarmerie"]
    assert detect_audiences("Emekli müşterilerimize") == []


def test_classify_rules():
    assert not classify("Duyuru", "Toplantı yapılacaktır.", [], False).is_campaign
    assert classify("X", "Devlet memurlarına özel %10 indirim.", [], False).is_public_employee
    assert not classify("X", "Tüm müşterilere %10 indirim, memurlar dahil.", [], False).is_public_employee
    src = classify("Diş Kliniği", "Diş hekimi muayenesinde öğretmenlere %20 indirim.", ["teacher"], True)
    assert src.audiences == ["teacher"]
    student = classify("THY", "Bakanlık bursiyer öğrencileri ve ailelerine %25 indirim.", ["teacher"], True)
    assert not student.is_public_employee


def test_discounts_ignore_url_escapes():
    assert discounts("https://x.gov.tr/a%3Ab%2Fc sayfasında %25 indirim") == ["%25"]
    assert "%50" in discounts("%50'ye varan indirim")
    assert "%3" in discounts("kampanya fiyatı üzerinden %3 indirim")


def test_summarize_is_extractive():
    text = "İndirim oranı\nTüm çerçevelerde %30 indirim uygulanacaktır.\nDetaylar için tıklayınız."
    out = summarize(text, "Başlık")
    assert out == "Tüm çerçevelerde %30 indirim uygulanacaktır."


def test_title_case_turkish():
    assert tr_title_case("MEDICANA SAĞLIK GRUBU") == "Medicana Sağlık Grubu"
    assert tr_title_case("KOÇAK OPTİK VE LENS") == "Koçak Optik ve Lens"


def test_normalize_url():
    a = normalize_url("http://www.Site.gov.tr/Kampanya/?utm_source=x#top")
    assert a == normalize_url("https://site.gov.tr/kampanya")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost/",
        "https://user:pw@site.gov.tr/",
        "http://10.0.0.5/x",
        "javascript:alert(1)",
        "https://intranet.local/",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(ConfigError):
        validate_public_url(url)


def test_sources_file_is_valid():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "campaign-sources.json"
    sources = load_sources(path)
    assert sources and len({s.id for s in sources}) == len(sources)
    assert all(s.url.startswith("https://") for s in sources)


def test_bad_source_config_rejected(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"sources": [{"id": "x1", "url": "https://a.gov.tr", "type": "shell"}]}), "utf-8")
    with pytest.raises(ConfigError):
        load_sources(p)
    p.write_text(json.dumps({"sources": [{"id": "x1", "url": "https://a.gov.tr", "type": "html_list"}]}), "utf-8")
    with pytest.raises(ConfigError):
        load_sources(p)


def test_page_parsing_ignores_related_links_and_keeps_header_title():
    from collector.extract import parse_html_page

    html = """<html><body><header><h1>Örnek Klinik İndirim Anlaşması</h1></header>
    <div class="icerik"><p>Sendika üyelerimize özel anlaşma imzalandı.</p>
    <p>Nakit ödemelerde</p><p>%15</p></div>
    <ul><li><a href="/a">Başka Kampanya %40 İndirim</a></li><li><a href="/b">Diğer Haber</a></li>
    <li><a href="/c">Üçüncü Haber</a></li></ul></body></html>"""
    page = parse_html_page(html.encode(), "https://x.gov.tr/k")
    assert page.title == "Örnek Klinik İndirim Anlaşması"
    assert "Nakit ödemelerde %15" in page.text
    assert "%40" not in page.text


def test_title_suffix_only_when_audience_missing():
    from conftest import make_source

    from collector.dates import DateInfo
    from collector.engine import Candidate, build_title

    src = make_source(title_suffix="HTKSEN Üyelerine Özel", audiences=["civil_servant"], audience_from_source=True)
    c = Candidate(src, "Allianz Sigorta İndirim Anlaşması", "", "https://x/a", DateInfo(), ["civil_servant"], [])
    assert build_title(c) == "Allianz Sigorta İndirim Anlaşması – HTKSEN Üyelerine Özel"
    c.title = "Sendika Üyelerine Özel Otel İndirimi"
    assert build_title(c) == "Sendika Üyelerine Özel Otel İndirimi"
    c.title = "KAPADOKUS THERMAL HOTEL"
    c.discounts = ["%25"]
    assert build_title(c) == "Kapadokus Thermal Hotel: %25 İndirim – HTKSEN Üyelerine Özel"
    c.discounts = []
    assert build_title(c) == "Kapadokus Thermal Hotel: Kurumsal İndirim – HTKSEN Üyelerine Özel"


def test_manual_and_mailto_records_are_preserved():
    records = [
        {
            "baslik": "Reklam",
            "aciklama": "Bize yazın",
            "link": "mailto:info@rotalink.tr",
            "tarih": "2026-01-01T00:00:00Z",
        },
        {
            "baslik": "Süresiz Öğretmen İndirimi",
            "aciklama": "Öğretmenlere %10",
            "link": "https://a.gov.tr/x",
            "tarih": "2026-01-01T00:00:00Z",
        },
    ]
    res = merge(records, {"version": 1, "records": {}}, [], NOW)
    assert res.records == records and not res.changed
