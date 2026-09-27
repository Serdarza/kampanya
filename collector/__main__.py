"""Kullanım: python -m collector [--dry-run] [--only id1,id2]"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_sources
from .dates import now_tr
from .engine import RunResult, SourceReport, merge, scan_all
from .fetch import Fetcher
from .store import dump_json, load_campaigns, load_state, record_title, save_campaigns, save_state

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("collector")


def _gh(kind: str, msg: str) -> None:
    """GitHub Actions açıklaması (yerelde düz log)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{msg}", flush=True)
    else:
        log.warning("%s: %s", kind.upper(), msg)


def _log_sources(reports: list[SourceReport]) -> None:
    for rep in reports:
        s = rep.source
        if rep.status == "ok":
            log.info(
                "OK      %-28s candidates=%d rejected=%d item_errors=%d",
                s.id,
                len(rep.candidates),
                len(rep.rejected),
                len(rep.item_errors),
            )
        elif rep.status == "parse_failed":
            _gh("warning", f"SOURCE PARSING FAILED [{s.id}] {s.url} :: {rep.error}")
        else:
            _gh("warning", f"SOURCE FAILED [{s.id}] {s.url} :: {rep.error}")
        for e in rep.item_errors[:10]:
            log.info("        item error: %s", e)
        for link, reason in rep.rejected[:10]:
            log.info("        rejected: %s (%s)", link, reason)


def _summary(reports: list[SourceReport], total: int, res: RunResult, dry: bool) -> str:
    ok = sum(r.status == "ok" for r in reports)
    parse_failed = sum(r.status == "parse_failed" for r in reports)
    failed = sum(r.status == "failed" for r in reports)
    found = sum(len(r.candidates) for r in reports)
    lines = [
        "CAMPAIGN SCAN COMPLETED" + (" (DRY RUN — no files written)" if dry else ""),
        "",
        f"Sources (total):        {total}",
        f"Checked:                {len(reports)}",
        f"Successful:             {ok}",
        f"Failed:                 {failed}",
        f"Parsing failed:         {parse_failed}",
        f"Campaigns found:        {found}",
        f"New campaigns:          {len(res.added)}",
        f"Updated:                {len(res.updated)}",
        f"Duplicates:             {len(res.duplicates)}",
        f"Expired removed:        {len(res.expired_removed)}",
        f"Expired (not added):    {len(res.expired_skipped)}",
        f"Duplicate records rm:   {len(res.duplicate_records_removed)}",
        f"Excluded category rm:   {len(res.excluded_removed)}",
        f"Stale removed:          {len(res.stale_removed)}",
        f"Stale (not added):      {len(res.stale_skipped)}",
        f"Without end date:       {len(res.no_date)}",
        f"Upcoming (not started): {len(res.upcoming)}",
        f"Total campaigns now:    {len(res.records)}",
    ]
    return "\n".join(lines)


def _details(res: RunResult) -> str:
    out = []
    for r in res.added:
        out.append(f"+ NEW      {r['baslik']}  <{r['link']}>")
    for old, new in res.updated:
        fields = [k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]
        out.append(f"~ UPDATED  {record_title(new)}  fields={fields}")
    for r in res.expired_removed:
        out.append(f"- EXPIRED  {record_title(r)}")
    for r in res.duplicate_records_removed:
        out.append(f"- DUPLICATE RECORD  {record_title(r)}")
    for r in res.excluded_removed:
        out.append(f"- EXCLUDED  {record_title(r)}")
    for r in res.stale_removed:
        out.append(f"- STALE     {record_title(r)}  ({r.get('link', '')})")
    for label in res.stale_skipped:
        out.append(f"  stale, not added: {label}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector")
    ap.add_argument("--sources", type=Path, default=ROOT / "campaign-sources.json")
    ap.add_argument("--data", type=Path, default=ROOT / "kampanya.json")
    ap.add_argument("--state", type=Path, default=ROOT / ".collector" / "state.json")
    ap.add_argument("--report", type=Path, default=ROOT / "reports" / "last-run.json")
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "").lower() == "true")
    ap.add_argument("--only", default="", help="virgülle ayrılmış kaynak id'leri")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        sources = load_sources(args.sources)
    except (ConfigError, json.JSONDecodeError, OSError) as e:
        _gh("error", f"campaign-sources.json invalid: {e}")
        return 2
    total = len(sources)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        sources = [s for s in sources if s.id in wanted]

    data, records = load_campaigns(args.data)
    state = load_state(args.state)
    now = now_tr()
    log.info("Scanning %d active sources (of %d) at %s", sum(s.active for s in sources), total, now.isoformat())

    reports = scan_all(sources, Fetcher(), workers=args.workers)
    _log_sources(reports)
    res = merge(records, state, reports, now)

    summary = _summary(reports, total, res, args.dry_run)
    details = _details(res)
    print("\n" + "=" * 60 + "\n" + summary + "\n" + "=" * 60)
    if details:
        print(details)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("```\n" + summary + "\n```\n")
            if details:
                f.write("\n```diff\n" + details + "\n```\n")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        dump_json(
            {
                "dryRun": args.dry_run,
                "ranAt": now.isoformat(),
                "sources": [
                    {
                        "id": r.source.id,
                        "status": r.status,
                        "error": r.error,
                        "candidates": len(r.candidates),
                        "itemErrors": r.item_errors,
                        "rejected": [{"link": link, "reason": why} for link, why in r.rejected],
                    }
                    for r in reports
                ],
                "added": res.added,
                "updated": [{"before": o, "after": n} for o, n in res.updated],
                "expiredRemoved": res.expired_removed,
                "duplicateRecordsRemoved": res.duplicate_records_removed,
                "excludedRemoved": res.excluded_removed,
                "staleRemoved": res.stale_removed,
                "staleSkipped": res.stale_skipped,
                "duplicates": res.duplicates,
                "noEndDate": res.no_date,
            }
        ),
        encoding="utf-8",
    )

    if args.dry_run:
        return 0
    if res.changed:
        save_campaigns(args.data, data, res.records)
    if res.state != state or res.changed:
        save_state(args.state, res.state)
    ok = sum(r.status == "ok" for r in reports)
    if reports and ok == 0:
        _gh("warning", "All sources failed — existing campaigns were kept (only date-based expiry applied).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
