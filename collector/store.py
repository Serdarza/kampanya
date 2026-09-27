"""kampanya.json ve yan durum dosyası (.collector/state.json) okuma/yazma.

kampanya.json yalnızca uygulamanın zaten okuduğu alanları taşır:
baslik, aciklama, link, tarih (+ isteğe bağlı id, kurum, etiketler).
Bitiş tarihi, kaynak ve parmak izi gibi toplayıcı verileri state.json'dadır.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .text import fold, normalize_url

ROOT_KEY = "kampanyalar"


def load_campaigns(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get(ROOT_KEY), list):
        raise ValueError(f"{path}: expected an object with a '{ROOT_KEY}' array")
    return data, data[ROOT_KEY]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "records": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", 1)
    data.setdefault("records", {})
    return data


def record_key(rec: dict) -> str:
    if rec.get("id"):
        return f"id:{rec['id']}"
    link = str(rec.get("link") or rec.get("detayLink") or rec.get("detaylink") or "").strip()
    if link:
        return f"url:{normalize_url(link)}"
    return f"title:{fold(str(rec.get('baslik') or rec.get('kampanyaBaslik') or ''))}"


def record_link(rec: dict) -> str:
    return str(rec.get("link") or rec.get("detayLink") or rec.get("detaylink") or "").strip()


def record_title(rec: dict) -> str:
    return str(rec.get("baslik") or rec.get("kampanyaBaslik") or "").strip()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dump_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def save_campaigns(path: Path, data: dict, records: list[dict]) -> None:
    out = dict(data)
    out[ROOT_KEY] = records
    text = dump_json(out)
    json.loads(text)
    _atomic_write(path, text)


def save_state(path: Path, state: dict) -> None:
    state["records"] = dict(sorted(state["records"].items()))
    _atomic_write(path, dump_json(state))
