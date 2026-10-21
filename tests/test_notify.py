"""Yeni kampanya bildirimi: tarama defteri, tekrar önleme, 10:00 zamanlaması ve FCM yükü."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from collector import notify
from collector.dates import TR_TZ, now_tr
from collector.engine import RunResult
from collector.notify import (
    FcmError,
    FcmV1Sender,
    build_message,
    build_text,
    load_ledger,
    new_ledger,
    record_scan,
    run_notify,
    save_ledger,
    seconds_until_send_time,
)

SCAN_TIME = datetime(2026, 9, 27, 7, 0, tzinfo=TR_TZ)
NINE_40 = datetime(2026, 9, 27, 9, 40, tzinfo=TR_TZ)
TEN = datetime(2026, 9, 27, 10, 0, tzinfo=TR_TZ)


class Recorder:
    def __init__(self, fail_times: int = 0, retryable: bool = True):
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self.retryable = retryable

    def __call__(self, payload: dict) -> str:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise FcmError("HTTP 503", retryable=self.retryable)
        return "projects/p/messages/1"


def _pending_ledger(ids=("auto-a", "auto-b", "auto-c"), at=SCAN_TIME):
    ledger = new_ledger()
    record_scan(ledger, now=at, added_ids=list(ids), scan_ok=True)
    return ledger


# --------------------------------------------------------------------------- metin


def test_texts_without_count():
    title, body = build_text(None)
    assert title == "Rotalink – Yeni Kampanyalar Eklendi! 🎉"
    assert body == "Rotalink'e yeni kampanyalar eklendi. Fırsatları kaçırmamak için hemen keşfet!"


def test_texts_with_count_always_say_rotalink():
    title, body = build_text(5)
    assert title == "Rotalink – 5 Yeni Kampanya Eklendi! 🎉"
    assert body == "Yeni fırsatları keşfetmek için hemen Rotalink'i aç!"


def test_message_payload_is_topic_based_with_kampanyalar_route():
    msg = build_message(5, "n-1", ["20260927-abc"])["message"]
    assert msg["topic"] == "kampanya_yeni"
    assert msg["notification"]["title"] == "Rotalink – 5 Yeni Kampanya Eklendi! 🎉"
    assert msg["data"]["route"] == "kampanyalar"
    assert msg["data"]["type"] == "new_campaigns"
    assert msg["data"]["notificationId"] == "n-1"
    assert msg["android"]["notification"]["channel_id"] == "rotalink_campaigns"
    assert msg["apns"]["headers"]["apns-collapse-id"] == "n-1"
    # Hiçbir gizli bilgi yükte olmamalı.
    assert "private_key" not in json.dumps(msg)


# --------------------------------------------------------------------------- tarama kaydı


def test_new_campaigns_create_pending_scan_with_unique_id():
    ledger = new_ledger()
    a = record_scan(ledger, now=SCAN_TIME, added_ids=["auto-1", "auto-2"], scan_ok=True)
    b = record_scan(ledger, now=SCAN_TIME + timedelta(days=1), added_ids=["auto-3"], scan_ok=True)
    assert a["status"] == "pending" and a["newCount"] == 2
    assert a["scanDate"] == "2026-09-27"
    assert a["scanId"] != b["scanId"]
    assert a["sentAt"] is None and a["failureReason"] is None


def test_no_new_campaigns_is_not_pending():
    ledger = new_ledger()
    entry = record_scan(ledger, now=SCAN_TIME, added_ids=[], scan_ok=True)
    assert entry["status"] == "no_new" and entry["newCount"] == 0


def test_failed_scan_never_notifies_even_with_added_ids():
    ledger = new_ledger()
    entry = record_scan(ledger, now=SCAN_TIME, added_ids=["auto-1"], scan_ok=False)
    assert entry["status"] == "scan_failed" and entry["newCount"] == 0
    assert entry["failureReason"]
    assert ledger["notifiedCampaignIds"] == []


def test_already_claimed_campaign_is_not_counted_again():
    ledger = new_ledger()
    record_scan(ledger, now=SCAN_TIME, added_ids=["auto-1", "auto-2"], scan_ok=True)
    again = record_scan(ledger, now=SCAN_TIME + timedelta(days=1), added_ids=["auto-2", "auto-3"], scan_ok=True)
    assert again["campaignIds"] == ["auto-3"] and again["newCount"] == 1


def test_duplicate_ids_inside_one_scan_count_once():
    ledger = new_ledger()
    entry = record_scan(ledger, now=SCAN_TIME, added_ids=["auto-1", "auto-1"], scan_ok=True)
    assert entry["newCount"] == 1


# --------------------------------------------------------------------------- gönderim


def test_pending_campaigns_send_exactly_one_notification():
    ledger = _pending_ledger()
    sender = Recorder()
    result = run_notify(ledger, now=TEN, sender=sender)
    assert result.action == "sent" and result.count == 3
    assert len(sender.calls) == 1
    msg = sender.calls[0]["message"]
    assert msg["notification"]["title"] == "Rotalink – 3 Yeni Kampanya Eklendi! 🎉"
    entry = ledger["scans"][0]
    assert entry["status"] == "sent" and entry["sentAt"] and entry["notificationId"]


def test_no_pending_means_no_send():
    ledger = new_ledger()
    record_scan(ledger, now=SCAN_TIME, added_ids=[], scan_ok=True)
    sender = Recorder()
    result = run_notify(ledger, now=TEN, sender=sender)
    assert result.action == "nothing_pending" and sender.calls == []


def test_failed_scan_means_no_send():
    ledger = new_ledger()
    record_scan(ledger, now=SCAN_TIME, added_ids=["auto-1"], scan_ok=False)
    sender = Recorder()
    assert run_notify(ledger, now=TEN, sender=sender).action == "nothing_pending"
    assert sender.calls == []


def test_same_scan_is_never_sent_twice():
    ledger = _pending_ledger()
    sender = Recorder()
    run_notify(ledger, now=TEN, sender=sender)
    second = run_notify(ledger, now=TEN + timedelta(minutes=5), sender=sender)
    assert second.action == "nothing_pending"
    assert len(sender.calls) == 1


def test_same_campaign_in_later_scan_is_not_renotified():
    ledger = _pending_ledger(("auto-a",))
    sender = Recorder()
    run_notify(ledger, now=TEN, sender=sender)
    record_scan(ledger, now=SCAN_TIME + timedelta(days=1), added_ids=["auto-a"], scan_ok=True)
    nxt = run_notify(ledger, now=TEN + timedelta(days=1), sender=sender)
    assert nxt.action == "nothing_pending"
    assert len(sender.calls) == 1


def test_dry_run_sends_nothing_and_changes_nothing():
    ledger = _pending_ledger()
    sender = Recorder()
    result = run_notify(ledger, now=TEN, sender=sender, dry_run=True)
    assert result.action == "dry_run" and sender.calls == []
    assert ledger["scans"][0]["status"] == "pending"


def test_missing_credentials_keeps_pending_for_later():
    ledger = _pending_ledger()
    result = run_notify(ledger, now=TEN, sender=None)
    assert result.action == "failed"
    assert ledger["scans"][0]["status"] == "pending"


def test_transient_failure_is_retried_then_succeeds():
    ledger = _pending_ledger()
    sender = Recorder(fail_times=2, retryable=True)
    result = run_notify(ledger, now=TEN, sender=sender, sleep=lambda _s: None)
    assert result.action == "sent" and len(sender.calls) == 3


def test_permanent_failure_is_recorded_and_retried_next_day_without_duplicates():
    ledger = _pending_ledger()
    bad = Recorder(fail_times=99, retryable=False)
    result = run_notify(ledger, now=TEN, sender=bad, sleep=lambda _s: None)
    assert result.action == "failed" and len(bad.calls) == 1
    entry = ledger["scans"][0]
    assert entry["status"] == "failed" and "503" in entry["failureReason"] and entry["attempts"] == 1

    good = Recorder()
    nxt = run_notify(ledger, now=TEN + timedelta(days=1), sender=good)
    assert nxt.action == "sent" and nxt.count == 3 and len(good.calls) == 1
    assert ledger["scans"][0]["status"] == "sent"


def test_stale_pending_is_expired_not_sent():
    ledger = _pending_ledger()
    sender = Recorder()
    result = run_notify(ledger, now=SCAN_TIME + timedelta(hours=49), sender=sender)
    assert result.action == "nothing_pending" and sender.calls == []
    assert ledger["scans"][0]["status"] == "expired"


# --------------------------------------------------------------------------- 10:00 zamanlaması


def test_seconds_until_ten_turkey_time():
    assert seconds_until_send_time(NINE_40) == 20 * 60
    assert seconds_until_send_time(TEN) == 0
    assert seconds_until_send_time(TEN + timedelta(minutes=30)) == 0


def test_utc_clock_is_converted_to_turkey_time():
    utc_0640 = datetime(2026, 9, 27, 6, 40, tzinfo=UTC)  # = 09:40 TR
    assert seconds_until_send_time(utc_0640) == 20 * 60


def test_waits_until_ten_before_sending():
    ledger = _pending_ledger()
    slept: list[float] = []
    sender = Recorder()
    result = run_notify(
        ledger,
        now=NINE_40,
        sender=sender,
        sleep=slept.append,
        wait_until_send_time=True,
        max_wait_seconds=45 * 60,
    )
    assert result.action == "sent"
    assert slept == [20 * 60]  # 10:00'dan önce gönderilmez


def test_does_not_wait_when_already_past_ten():
    ledger = _pending_ledger()
    slept: list[float] = []
    result = run_notify(
        ledger, now=TEN + timedelta(minutes=12), sender=Recorder(), sleep=slept.append,
        wait_until_send_time=True, max_wait_seconds=45 * 60,
    )
    assert result.action == "sent" and slept == []


def test_refuses_to_wait_unreasonably_long():
    ledger = _pending_ledger()
    sender = Recorder()
    early = datetime(2026, 9, 27, 6, 0, tzinfo=TR_TZ)
    result = run_notify(
        ledger, now=early, sender=sender, sleep=lambda _s: None,
        wait_until_send_time=True, max_wait_seconds=45 * 60,
    )
    assert result.action == "too_early" and sender.calls == []
    assert ledger["scans"][0]["status"] == "pending"


def test_nothing_pending_does_not_wait():
    ledger = new_ledger()
    slept: list[float] = []
    run_notify(ledger, now=NINE_40, sender=Recorder(), sleep=slept.append,
               wait_until_send_time=True, max_wait_seconds=3600)
    assert slept == []


# --------------------------------------------------------------------------- defter dosyası + __main__ kancası


def test_ledger_roundtrip_and_cap(tmp_path):
    path = tmp_path / "n.json"
    ledger = new_ledger()
    for i in range(80):
        record_scan(ledger, now=SCAN_TIME + timedelta(days=i), added_ids=[f"auto-{i}"], scan_ok=True)
    save_ledger(ledger, path)
    loaded = load_ledger(path)
    assert len(loaded["scans"]) == notify.MAX_SCANS_KEPT
    assert "auto-79" in loaded["notifiedCampaignIds"]


def test_load_missing_ledger_returns_empty(tmp_path):
    assert load_ledger(tmp_path / "none.json") == new_ledger()


def test_main_hook_counts_only_final_added_campaigns(tmp_path):
    import argparse

    from collector.__main__ import _record_notification_scan

    args = argparse.Namespace(notifications=tmp_path / "n.json")
    res = RunResult(
        records=[{"id": "auto-keep"}, {"id": "old-1"}],
        state={},
        added=[{"id": "auto-keep"}, {"id": "auto-removed-by-dedupe"}],
        updated=[({"id": "old-1"}, {"id": "old-1"})],  # güncellenen kampanya yeni sayılmaz
    )
    _record_notification_scan(args, res, scan_ok=True, now=SCAN_TIME)
    entry = load_ledger(args.notifications)["scans"][0]
    assert entry["campaignIds"] == ["auto-keep"] and entry["newCount"] == 1


def test_main_hook_never_raises(tmp_path):
    import argparse

    from collector.__main__ import _record_notification_scan

    bad = tmp_path / "n.json"
    bad.write_text("[not json", encoding="utf-8")
    args = argparse.Namespace(notifications=bad)
    _record_notification_scan(args, RunResult(records=[], state={}), scan_ok=True, now=SCAN_TIME)


# --------------------------------------------------------------------------- FCM HTTP v1 istemcisi


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests: list[dict] = []

    def post(self, url, headers, data, timeout):
        self.requests.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return self.response


def _sender(session) -> FcmV1Sender:
    s = FcmV1Sender({"project_id": "rotalink-d738c", "private_key": "x"}, session=session)
    s._token = lambda: "tok"  # OAuth ağ çağrısı yok
    return s


def test_fcm_v1_request_shape():
    session = FakeSession(FakeResponse(200, {"name": "projects/rotalink-d738c/messages/9"}))
    name = _sender(session)(build_message(2, "n-1", ["s1"]))
    req = session.requests[0]
    assert name.endswith("/9")
    assert req["url"] == "https://fcm.googleapis.com/v1/projects/rotalink-d738c/messages:send"
    assert req["headers"]["Authorization"] == "Bearer tok"
    assert "Yeni Kampanya" in req["data"].decode("utf-8")


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (404, False), (429, True), (503, True)])
def test_fcm_v1_error_classification(status, retryable):
    session = FakeSession(FakeResponse(status, text="boom"))
    with pytest.raises(FcmError) as ei:
        _sender(session)({"message": {}})
    assert ei.value.retryable is retryable


def test_sender_from_env_requires_valid_key():
    assert notify.sender_from_env({}) is None
    with pytest.raises(ValueError):
        notify.sender_from_env({"FCM_SERVICE_ACCOUNT_JSON": json.dumps({"project_id": "p"})})
    ok = notify.sender_from_env(
        {"FCM_SERVICE_ACCOUNT_JSON": json.dumps({"project_id": "p", "private_key": "k", "client_email": "e"})}
    )
    assert ok is not None and ok.project_id == "p"


def test_cli_dry_run_does_not_touch_ledger(tmp_path, capsys):
    path = tmp_path / "n.json"
    save_ledger(_pending_ledger(at=now_tr()), path)
    before = path.read_text(encoding="utf-8")
    assert notify.main(["--ledger", str(path), "--dry-run"]) == 0
    assert path.read_text(encoding="utf-8") == before


def test_cli_without_secret_keeps_ledger_pending(tmp_path, monkeypatch):
    monkeypatch.delenv("FCM_SERVICE_ACCOUNT_JSON", raising=False)
    path = tmp_path / "n.json"
    save_ledger(_pending_ledger(at=now_tr()), path)
    assert notify.main(["--ledger", str(path)]) == 1
    assert load_ledger(path)["scans"][0]["status"] == "pending"


def test_cli_without_secret_is_ok_when_nothing_pending(tmp_path, monkeypatch):
    monkeypatch.delenv("FCM_SERVICE_ACCOUNT_JSON", raising=False)
    path = tmp_path / "n.json"
    save_ledger(new_ledger(), path)
    assert notify.main(["--ledger", str(path)]) == 0
