#!/usr/bin/env python3
"""Recompute every customer's rfm_segment in BigQuery from suffai_ops.orders (rule rfm-v1).

Dry run by default: prints how many customers would change and the segment counts.
--apply writes the changed segments. Run after the migration, a CSV load or bulk order edits;
the Command Center also runs it after each Admin-created order.

  python3 command_center/scripts/recompute_rfm.py --project P [--dataset suffai_ops] [--apply]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ops_records  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "")
    ap.add_argument("--dataset", default=os.getenv("BQ_DATASET", "suffai_ops"))
    ap.add_argument("--apply", action="store_true", help="Write the changed segments (default: dry run)")
    args = ap.parse_args(argv)
    if not args.project:
        raise SystemExit("--project (or GOOGLE_CLOUD_PROJECT) is required")
    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["BQ_DATASET"] = args.dataset

    res = ops_records.recompute_rfm(apply=args.apply, admin="recompute_rfm.py")
    print(f"{res['rule_version']}: {res['customers']} customers, {res['changed']} with a different segment")
    for seg, n in sorted(res["by_segment"].items(), key=lambda kv: -kv[1]):
        print(f"  {seg}: {n}")
    if not args.apply:
        print("Dry run. Re-run with --apply to write the changed segments." if res["changed"] else "Nothing to change.")
    else:
        print("Segments written." if res["applied"] else "Nothing to change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
