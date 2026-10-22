"""Yeni kampanya bildirimi: tarama kaydı, tekrar kontrolü ve FCM HTTP v1 gönderimi.

Akış
----
1. Günlük tarama (``python -m collector``) bitince :func:`record_scan` çağrılır.
   Yalnızca bu taramada ``kampanya.json`` içine başarıyla EKLENEN kayıtlar sayılır;
   güncellenen / zaten bilinen kampanyalar yeni sayılmaz. Her tarama için benzersiz bir
   ``scanId`` ile ``.collector/notifications.json`` defterine kayıt yazılır.
2. Türkiye saatiyle 10:00'da ``python -m collector.notify`` çalışır: bekleyen kayıtları
   toplar ve tek bir bildirim gönderir. Yeni kampanya yoksa, tarama başarısızsa veya
   kayıt zaten gönderildiyse hiçbir şey gönderilmez.

Bildirim ``kampanya_yeni`` FCM konusuna (topic) gider; uygulamada kampanya bildirimini
kapatan kullanıcı bu konudan çıkarıldığı için bildirim almaz. Cihaz tokenları sunucuda
tutulmaz; token yenileme ve geçersiz token temizliği FCM tarafından yönetilir.
Hizmet hesabı anahtarı yalnızca GitHub Secret (``FCM_SERVICE_ACCOUNT_JSON``) olarak okunur.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .dates import TR_TZ, now_tr
from .store import dump_json

log = logging.getLogger("collector.notify")

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / ".collector" / "notifications.json"

TOPIC = "kampanya_yeni"
ANDROID_CHANNEL_ID = "rotalink_campaigns"
SEND_HOUR = 10
SEND_MINUTE = 0
# 10:00'dan sonra bulunan bekleyen kayıtlar bir sonraki 10:00'a kadar bekler; bundan eskisi gönderilmez.
MAX_PENDING_AGE = timedelta(hours=48)
MAX_SCANS_KEPT = 60
MAX_CLAIMED_IDS = 3000
MAX_SEND_ATTEMPTS = 3

STATUS_PENDING = "pending"
STATUS_NO_NEW = "no_new"
STATUS_SCAN_FAILED = "scan_failed"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
_SENDABLE = {STATUS_PENDING, STATUS_FAILED}


# --------------------------------------------------------------------------- metin


def build_text(count: int | None) -> tuple[str, str]:
    """Bildirim başlığı ve açıklaması. Uygulama adı her yerde Rotalink."""
    if count and count > 0:
        return (
            f"Rotalink – {count} Yeni Kampanya Eklendi! 🎉",
            "Yeni fırsatları keşfetmek için hemen Rotalink'i aç!",
        )
    return (
        "Rotalink – Yeni Kampanyalar Eklendi! 🎉",
        "Rotalink'e yeni kampanyalar eklendi. Fırsatları kaçırmamak için hemen keşfet!",
    )


def build_message(count: int, notification_id: str, scan_ids: list[str], topic: str = TOPIC) -> dict:
    """FCM HTTP v1 ``messages:send`` gövdesi (Android + iOS aynı veri, aynı yönlendirme)."""
    title, body = build_text(count)
    return {
        "message": {
            "topic": topic,
            "notification": {"title": title, "body": body},
            "data": {
                "type": "new_campaigns",
                "route": "kampanyalar",
                "count": str(count),
                "notificationId": notification_id,
                "scanIds": ",".join(scan_ids)[:900],
            },
            "android": {
                "priority": "HIGH",
                "notification": {"channel_id": ANDROID_CHANNEL_ID, "tag": notification_id},
            },
            "apns": {
                "headers": {"apns-priority": "10", "apns-collapse-id": notification_id[:64]},
                "payload": {"aps": {"sound": "default"}},
            },
        }
    }


# --------------------------------------------------------------------------- defter


def new_ledger() -> dict:
    return {"version": 1, "notifiedCampaignIds": [], "scans": []}


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    if not path.exists():
        return new_ledger()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object")
    data.setdefault("version", 1)
    data.setdefault("notifiedCampaignIds", [])
    data.setdefault("scans", [])
    return data


def save_ledger(ledger: dict, path: Path = LEDGER_PATH) -> None:
    ledger["scans"] = ledger["scans"][-MAX_SCANS_KEPT:]
    ledger["notifiedCampaignIds"] = ledger["notifiedCampaignIds"][-MAX_CLAIMED_IDS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dump_json(ledger), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def new_scan_id(now: datetime) -> str:
    return f"{now.astimezone(TR_TZ):%Y%m%d}-{uuid.uuid4().hex[:8]}"


def record_scan(
    ledger: dict,
    *,
    now: datetime,
    added_ids: list[str],
    scan_ok: bool,
    scan_id: str | None = None,
) -> dict:
    """Günlük tarama sonucunu deftere işler ve kaydı döndürür.

    ``added_ids``: bu taramada başarıyla eklenen ve hâlâ ``kampanya.json``'da bulunan kampanya
    kimlikleri. Daha önce bildirim kuyruğuna alınmış kimlikler tekrar sayılmaz.
    """
    claimed = set(ledger["notifiedCampaignIds"])
    fresh: list[str] = []
    for cid in added_ids:
        if cid and cid not in claimed and cid not in fresh:
            fresh.append(cid)

    if not scan_ok:
        status, fresh = STATUS_SCAN_FAILED, []
    elif fresh:
        status = STATUS_PENDING
    else:
        status = STATUS_NO_NEW

    entry = {
        "scanId": scan_id or new_scan_id(now),
        "scanDate": now.astimezone(TR_TZ).date().isoformat(),
        "scannedAt": now.astimezone(TR_TZ).isoformat(),
        "newCount": len(fresh),
        "campaignIds": fresh,
        "status": status,
        "notificationId": None,
        "sentAt": None,
        "failureReason": "all sources failed" if status == STATUS_SCAN_FAILED else None,
        "attempts": 0,
    }
    ledger["scans"].append(entry)
    ledger["notifiedCampaignIds"].extend(fresh)
    return entry


# --------------------------------------------------------------------------- gönderim


class FcmError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


Sender = Callable[[dict], str]


class FcmV1Sender:
    """FCM HTTP v1 istemcisi. Anahtar yalnızca ortam değişkeninden okunur."""

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, service_account_info: dict, session=None):
        self.info = service_account_info
        self.project_id = service_account_info["project_id"]
        self._session = session
        self._credentials = None

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._credentials is None:
            self._credentials = service_account.Credentials.from_service_account_info(
                self.info, scopes=[self.SCOPE]
            )
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials.token

    def __call__(self, payload: dict) -> str:
        import requests

        session = self._session or requests
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        try:
            res = session.post(
                url,
                headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=30,
            )
        except Exception as e:  # ağ / kimlik doğrulama
            raise FcmError(f"{type(e).__name__}: {e}", retryable=True) from e
        if res.status_code == 200:
            return str(res.json().get("name", ""))
        text = res.text[:300].replace("\n", " ")
        raise FcmError(f"HTTP {res.status_code}: {text}", retryable=res.status_code >= 500 or res.status_code == 429)


def sender_from_env(env: dict | None = None) -> FcmV1Sender | None:
    raw = (env if env is not None else os.environ).get("FCM_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    info = json.loads(raw)
    if not isinstance(info, dict) or not info.get("project_id") or not info.get("private_key"):
        raise ValueError("FCM_SERVICE_ACCOUNT_JSON geçerli bir hizmet hesabı anahtarı değil")
    return FcmV1Sender(info)


# --------------------------------------------------------------------------- zamanlama


def seconds_until_send_time(now: datetime) -> float:
    """Türkiye saatiyle 10:00'a kalan saniye (geçtiyse 0)."""
    local = now.astimezone(TR_TZ)
    target = local.replace(hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0)
    return max(0.0, (target - local).total_seconds())


# --------------------------------------------------------------------------- bildirim turu


@dataclass
class NotifyResult:
    action: str  # sent | nothing_pending | dry_run | too_early | failed
    count: int = 0
    notification_id: str | None = None
    detail: str = ""


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def collect_pending(ledger: dict, now: datetime) -> list[dict]:
    """Gönderilebilir kayıtlar; 48 saatten eskiler ``expired`` olur."""
    out = []
    for e in ledger["scans"]:
        if e.get("status") not in _SENDABLE:
            continue
        if now - _parse(e["scannedAt"]) > MAX_PENDING_AGE:
            e["status"] = STATUS_EXPIRED
            e["failureReason"] = "pending notification older than 48h"
            continue
        if e.get("newCount", 0) > 0:
            out.append(e)
    return out


def notification_id_for(entries: list[dict]) -> str:
    key = ",".join(sorted(e["scanId"] for e in entries))
    return "n-" + hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]


def run_notify(
    ledger: dict,
    *,
    now: datetime,
    sender: Sender | None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    wait_until_send_time: bool = False,
    max_wait_seconds: float = 0,
) -> NotifyResult:
    """Bekleyen kayıtlar için en fazla BİR bildirim gönderir ve defteri günceller."""
    pending = collect_pending(ledger, now)
    if not pending:
        return NotifyResult("nothing_pending", detail="no pending new campaigns")

    count = sum(e["newCount"] for e in pending)
    nid = notification_id_for(pending)

    if dry_run:
        title, body = build_text(count)
        return NotifyResult("dry_run", count, nid, f"{title} | {body}")

    if wait_until_send_time:
        wait = seconds_until_send_time(now)
        if wait > max_wait_seconds:
            return NotifyResult("too_early", count, nid, f"send time is {wait:.0f}s away")
        if wait > 0:
            log.info("10:00'a %.0f sn bekleniyor", wait)
            sleep(wait)

    if sender is None:
        return NotifyResult("failed", count, nid, "FCM_SERVICE_ACCOUNT_JSON not configured")

    payload = build_message(count, nid, [e["scanId"] for e in pending])
    reason = ""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            sender(payload)
            reason = ""
            break
        except FcmError as e:
            reason = str(e)
            if not e.retryable or attempt == MAX_SEND_ATTEMPTS:
                break
            sleep(2**attempt)

    sent_at = datetime.now(TR_TZ).isoformat()
    for e in pending:
        e["attempts"] = e.get("attempts", 0) + 1
        e["notificationId"] = nid
        if reason:
            e["status"] = STATUS_FAILED
            e["failureReason"] = reason
        else:
            e["status"] = STATUS_SENT
            e["sentAt"] = sent_at
            e["failureReason"] = None
    if reason:
        return NotifyResult("failed", count, nid, reason)
    return NotifyResult("sent", count, nid)


# --------------------------------------------------------------------------- CLI


def _gh(kind: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{msg}", flush=True)
    else:
        log.warning("%s: %s", kind.upper(), msg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector.notify")
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "").lower() == "true")
    ap.add_argument("--wait-until-send-time", action="store_true", help="10:00 TR'ye kadar bekle")
    ap.add_argument("--max-wait-minutes", type=float, default=45)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ledger = load_ledger(args.ledger)
    now = now_tr()

    try:
        sender = None if args.dry_run else sender_from_env()
    except (ValueError, json.JSONDecodeError) as e:
        _gh("error", f"FCM kimlik bilgisi okunamadı: {e}")
        return 1

    if sender is None and not args.dry_run and collect_pending(ledger, now):
        if args.wait_until_send_time:
            wait = seconds_until_send_time(now)
            if wait > args.max_wait_minutes * 60:
                log.info("notify: too_early; FCM anahtarı bu turda gerekmedi")
                return 0
        if any(e.get("status") == STATUS_EXPIRED for e in ledger["scans"]):
            save_ledger(ledger, args.ledger)
        _gh(
            "error",
            "FCM_SERVICE_ACCOUNT_JSON tanımlı değil; bekleyen kampanya bildirimi gönderilmedi. "
            "kampanya deposuna (Settings → Secrets) Firebase hizmet hesabı JSON'unu ekleyin.",
        )
        return 1

    result = run_notify(
        ledger,
        now=now,
        sender=sender,
        dry_run=args.dry_run,
        wait_until_send_time=args.wait_until_send_time,
        max_wait_seconds=args.max_wait_minutes * 60,
    )
    log.info("notify: %s count=%d id=%s %s", result.action, result.count, result.notification_id, result.detail)
    if result.action in {"sent", "failed"} or any(e.get("status") == STATUS_EXPIRED for e in ledger["scans"]):
        save_ledger(ledger, args.ledger)
    if result.action == "failed":
        _gh("error", f"Kampanya bildirimi gönderilemedi: {result.detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
