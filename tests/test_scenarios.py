"""İstenen 10 senaryo: tarama + birleştirme uçtan uca (ağsız)."""

from __future__ import annotations

from datetime import timedelta

from conftest import LIST_URL, FakeFetcher, detail_page, list_page, make_source

from collector.engine import merge, scan_source

BASE = "https://kampanya.example.gov.tr/kampanya/"

TEACHER_OFFER = detail_page(
    "Örnek Optik Öğretmenlere Özel İndirim",
    "Kampanya, Millî Eğitim Bakanlığına bağlı okullarda görev yapan öğretmenleri kapsamaktadır.",
    "Tüm gözlük çerçevelerinde %30 indirim uygulanacaktır.",
    "Kampanya 31.12.2026 tarihine kadar geçerlidir.",
)


def run(pages, records, state=None, source=None, now=None):
    from conftest import NOW

    src = source or make_source()
    rep = scan_source(src, FakeFetcher(pages))
    return rep, merge(records, state or {"version": 1, "records": {}}, [rep], now or NOW)


# 1. Yeni kampanya
def test_new_campaign_is_added():
    rep, res = run({LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER}, [])
    assert rep.status == "ok"
    assert len(res.added) == 1
    rec = res.records[0]
    assert rec["link"] == BASE + "ornek-optik/"
    assert rec["baslik"] == "Örnek Optik Öğretmenlere Özel İndirim"
    assert "%30" in rec["aciklama"]
    assert "Öğretmen" in rec["etiketler"]
    assert rec["id"].startswith("auto-")
    assert rec["tarih"].endswith("T00:00:00Z")
    assert set(rec) <= {"id", "baslik", "kurum", "aciklama", "link", "tarih", "etiketler"}
    entry = res.state["records"][f"id:{rec['id']}"]
    assert entry["end"].startswith("2026-12-31T23:59:59")


# 2. Aynı kampanya iki kez bulunursa
def test_same_campaign_twice_is_not_duplicated():
    pages = {LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER}
    _, first = run(pages, [])
    _, second = run(pages, first.records, first.state)
    assert not second.changed
    assert len(second.records) == 1
    assert second.duplicates

    # URL'si değişmiş (yeni slug) aynı kampanya da eklenmez; bağlantı güncellenir.
    moved = {LIST_URL: list_page("ornek-optik-2026"), BASE + "ornek-optik-2026/": TEACHER_OFFER}
    _, third = run(moved, second.records, second.state)
    assert not third.added
    assert len(third.records) == 1
    assert third.records[0]["link"] == BASE + "ornek-optik-2026/"


# 3. Kampanya güncellenirse
def test_updated_campaign_updates_record():
    pages = {LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER}
    _, first = run(pages, [])
    changed = TEACHER_OFFER.replace("%30", "%40").replace("31.12.2026", "31.03.2027")
    pages[BASE + "ornek-optik/"] = changed
    _, second = run(pages, first.records, first.state)
    assert len(second.updated) == 1
    assert not second.added
    rec = second.records[0]
    assert "%40" in rec["aciklama"] and "31.03.2027" in rec["aciklama"]
    assert second.state["records"][f"id:{rec['id']}"]["end"].startswith("2027-03-31")


def test_manual_record_with_wrong_end_date_is_corrected_not_deleted():
    manual = {
        "baslik": "Örnek Optik Öğretmenlere %30 İndirim",
        "aciklama": "Kampanya 31.05.2026 tarihine kadar geçerlidir.",
        "link": BASE + "ornek-optik/",
        "tarih": "2025-06-01T00:00:00Z",
    }
    _, res = run({LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER}, [manual])
    assert not res.expired_removed
    assert len(res.updated) == 1
    assert "31.12.2026" in res.records[0]["aciklama"]
    assert res.records[0]["baslik"] == manual["baslik"]


# 4. Kampanya süresi dolarsa
def test_expired_campaign_is_removed(now):
    expired = {
        "baslik": "Eski Kampanya Öğretmenlere %20 İndirim",
        "aciklama": "Kampanya 31.08.2026 tarihine kadar geçerlidir.",
        "link": "https://baska.example.gov.tr/eski",
        "tarih": "2026-01-01T00:00:00Z",
    }
    _, res = run({LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER}, [expired])
    assert [r["baslik"] for r in res.expired_removed] == [expired["baslik"]]
    assert all(r["baslik"] != expired["baslik"] for r in res.records)

    # Bitiş günü içinde (Türkiye saatiyle 23:59:59'a kadar) silinmez.
    same_day = dict(expired, aciklama="Kampanya 27.09.2026 tarihine kadar geçerlidir.")
    _, res2 = run({LIST_URL: list_page()}, [same_day], now=now)
    assert same_day in res2.records
    _, res3 = run({LIST_URL: list_page()}, [same_day], now=now + timedelta(days=1))
    assert same_day not in res3.records


def test_new_but_already_expired_candidate_is_not_added():
    old = TEACHER_OFFER.replace("31.12.2026", "01.09.2026")
    _, res = run({LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": old}, [])
    assert not res.added and res.expired_skipped


# 5. Kaynak site açılmazsa
def test_source_down_keeps_existing_records():
    existing = {
        "baslik": "Örnek Optik Öğretmenlere %30 İndirim",
        "aciklama": "Öğretmenlere özel indirim.",
        "link": BASE + "ornek-optik/",
        "tarih": "2026-01-01T00:00:00Z",
    }
    rep, res = run({LIST_URL: (503, "down")}, [existing])
    assert rep.status == "failed"
    assert res.records == [existing]
    assert not res.changed

    rep2, res2 = run({}, [existing])  # bağlantı hatası
    assert rep2.status == "failed" and res2.records == [existing]


# 6. HTML yapısı değişirse
def test_changed_html_structure_is_parse_failure_and_deletes_nothing():
    existing = {
        "baslik": "X Öğretmenlere İndirim",
        "aciklama": "a",
        "link": BASE + "x/",
        "tarih": "2026-01-01T00:00:00Z",
    }
    new_layout = "<html><body><div class='yeni'><span onclick='go(1)'>Kampanya</span></div></body></html>"
    rep, res = run({LIST_URL: new_layout}, [existing])
    assert rep.status == "parse_failed"
    assert "linkPattern" in rep.error
    assert res.records == [existing] and not res.changed


# 7. Tarih bulunamazsa
def test_campaign_without_date_is_added_and_never_expired(now):
    no_date = detail_page(
        "Örnek Kitabevi",
        "Öğretmenlerimize özel tüm kitaplarda %15 indirim sunulmaktadır.",
    )
    pages = {LIST_URL: list_page("kitabevi"), BASE + "kitabevi/": no_date}
    src = make_source(audiences=["teacher"], audience_from_source=True, organization_from_title=True)
    _, res = run(pages, [], source=src)
    assert len(res.added) == 1 and res.no_date
    later = now + timedelta(days=3650)
    _, res2 = run({LIST_URL: (500, "")}, res.records, res.state, source=src, now=later)
    assert res2.records == res.records and not res2.expired_removed


# 8. Kamu personeline özel kampanya
def test_public_employee_campaign_is_accepted():
    page = detail_page(
        "Kamu Personeline Özel Kasko Kampanyası",
        "Kamu personeline özel kasko poliçelerinde %20 indirim uygulanmaktadır.",
        "Kampanya 30.11.2026 tarihine kadar geçerlidir.",
    )
    rep, res = run({LIST_URL: list_page("kasko"), BASE + "kasko/": page}, [])
    assert len(rep.candidates) == 1
    assert rep.candidates[0].audiences == ["general_public_employee"]
    assert "Kamu Personeli" in res.records[0]["etiketler"]


# 9. Genel halka açık kampanya
def test_general_public_campaign_is_rejected():
    page = detail_page(
        "Yaz İndirimi",
        "Tüm müşterilerimize tüm ürünlerde %20 indirim fırsatı. Öğretmenler günü kutlu olsun.",
        "Kampanya 30.11.2026 tarihine kadar geçerlidir.",
    )
    plain = detail_page("Kış Fırsatları", "Tüm ürünlerde %10 indirim sizleri bekliyor.")
    rep, res = run({LIST_URL: list_page("yaz", "kis"), BASE + "yaz/": page, BASE + "kis/": plain}, [])
    assert not rep.candidates and len(rep.rejected) == 2
    assert not res.added


# 10. Birden fazla kamu personeli grubunu kapsayan kampanya
def test_multiple_public_employee_groups():
    page = detail_page(
        "Örnek Otel Kampanyası",
        "Öğretmenler, sağlık çalışanları ve emniyet mensuplarına özel konaklamada %25 indirim uygulanmaktadır.",
        "Kampanya 15.12.2026 tarihine kadar geçerlidir.",
    )
    rep, res = run(
        {LIST_URL: list_page("otel"), BASE + "otel/": page}, [], source=make_source(category="accommodation")
    )
    c = rep.candidates[0]
    assert {"teacher", "healthcare", "police"} <= set(c.audiences)
    tags = res.records[0]["etiketler"]
    assert {"Öğretmen", "Sağlık Çalışanı", "Emniyet", "Konaklama"} <= set(tags)


def test_one_failing_source_does_not_affect_others():
    from conftest import NOW

    ok_src = make_source()
    bad_src = make_source(id="bozuk", url="https://bozuk.example.gov.tr/")
    fetcher = FakeFetcher({LIST_URL: list_page("ornek-optik"), BASE + "ornek-optik/": TEACHER_OFFER})
    reps = [scan_source(s, fetcher) for s in (ok_src, bad_src)]
    assert [r.status for r in reps] == ["ok", "failed"]
    res = merge([], {"version": 1, "records": {}}, reps, NOW)
    assert len(res.added) == 1
