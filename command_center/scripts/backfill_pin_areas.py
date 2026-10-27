#!/usr/bin/env python3
"""Look up every customer PIN that has no area profile in BigQuery pin_areas (Gemini + Google Search).

Dry run by default: lists the PINs it would look up. --apply runs the lookups and saves them;
--refresh also redoes PINs that already have a profile.

  python3 command_center/scripts/backfill_pin_areas.py --project P [--dataset suffai_ops] [--apply] [--refresh]

Needs Vertex AI (ENABLE_VERTEX, GEO_GROUNDING_*) and BigQuery access as the Command Center has.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pin_areas  # noqa: E402


def customer_pins() -> list[str]:
    table = pin_areas._table().replace(".pin_areas`", ".customers`")
    rows = pin_areas._run(
        f"SELECT DISTINCT TRIM(CAST(pin_code AS STRING)) AS pin FROM {table} WHERE pin_code IS NOT NULL", []
    )
    return sorted({str(r["pin"]) for r in rows if r.get("pin")})


def plan(pins: list[str], stored: dict[str, dict], *, refresh: bool) -> tuple[list[str], list[str], list[str]]:
    """(to look up, already ok, malformed). not_found / out_of_city rows are retried only with --refresh."""
    todo, done, bad = [], [], []
    for p in pins:
        if not pin_areas.valid_pin(p):
            bad.append(p)
        elif p in stored and not refresh:
            done.append(p)
        else:
            todo.append(p)
    return todo, done, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "")
    ap.add_argument("--dataset", default=os.getenv("BQ_DATASET", "suffai_ops"))
    ap.add_argument("--apply", action="store_true", help="Run the lookups (default: dry run)")
    ap.add_argument("--refresh", action="store_true", help="Also redo PINs that already have a profile")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between lookups")
    args = ap.parse_args(argv)
    if not args.project:
        raise SystemExit("--project (or GOOGLE_CLOUD_PROJECT) is required")
    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["BQ_DATASET"] = args.dataset

    stored = pin_areas.load_all(args.project, args.dataset)
    todo, done, bad = plan(customer_pins(), stored, refresh=args.refresh)
    print(f"customer PINs: {len(todo) + len(done) + len(bad)}; with a profile: {len(done)}; "
          f"to look up: {len(todo)}; malformed: {len(bad)}")
    for p in bad:
        print(f"  skip malformed PIN {p!r} (fix the customer record)")
    if not args.apply:
        for p in todo:
            print(f"  would look up {p}")
        print("Dry run. Re-run with --apply to look these up.")
        return 0
    failed = 0
    for i, p in enumerate(todo):
        res = pin_areas.lookup(p, refresh=True, request_id=f"backfill-{p}")
        status = res.get("status")
        failed += status != "ok"
        label = res.get("area_label") or "-"
        print(f"  {p}: {status} {label} population={res.get('population_pin')} saved={res.get('saved', False)}")
        if i < len(todo) - 1:
            time.sleep(max(0.0, args.sleep))
    print(f"Done: {len(todo) - failed} ok, {failed} not ok.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
