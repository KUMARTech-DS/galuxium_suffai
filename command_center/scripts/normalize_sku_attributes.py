#!/usr/bin/env python3
"""Rewrite brand SKU attributes stored before the shared mapping (t_shirt -> T-Shirt, navy -> Navy Blue).

Each brand_skus row is re-mapped from its stored extraction result with worker/bq_items.item_attributes,
the mapping behind customer garments. Dry run by default; --apply writes. Ranking already compares both
spellings as one value; re-rank a campaign in Ads Lab to refresh its stored scores.

  DATABASE_URL=... GOOGLE_CLOUD_PROJECT=P python3 command_center/scripts/normalize_sku_attributes.py [--apply]

GOOGLE_CLOUD_PROJECT lets the mapping read suffai_ops.ref_values. Prints counts and SKU ids only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from worker import bq_items  # noqa: E402

# brand_skus column -> item_attributes key
COLUMNS = {"color": "color", "pattern": "pattern", "material": "material", "size": "size",
           "commodity": "name", "sleeve_type": "sleeve_type", "collar_type": "collar_type"}
_UNKNOWN = {"", "unknown", "none", "null", "n/a", "other"}
SHOWN = 10

# Test hook: () -> DB-API connection instead of psycopg2.
_pg_connect: Optional[Callable[[], Any]] = None


def _known(v: Any) -> Optional[str]:
    s = str(v).strip() if v is not None else ""
    return None if s.lower() in _UNKNOWN else s


def mapped_columns(raw_result: Any) -> Optional[dict[str, Optional[str]]]:
    """brand_skus attribute columns for a stored extraction result; None when there is no result."""
    raw = json.loads(raw_result) if isinstance(raw_result, str) and raw_result.strip() else raw_result
    if not isinstance(raw, dict) or not raw:
        return None
    attrs = bq_items.item_attributes(raw)
    return {col: _known(attrs.get(key)) for col, key in COLUMNS.items()}


def plan(rows: list[tuple]) -> list[tuple[str, dict[str, Optional[str]]]]:
    """rows: (sku_uuid, raw_result, *current columns in COLUMNS order) -> [(sku_uuid, new columns)]."""
    out = []
    for sku_uuid, raw, *current in rows:
        new = mapped_columns(raw)
        if new is not None and list(new.values()) != list(current):
            out.append((str(sku_uuid), new))
    return out


def _connect() -> Any:
    if _pg_connect is not None:
        return _pg_connect()
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    import psycopg2  # type: ignore

    return psycopg2.connect(dsn)


def run(*, apply: bool) -> list[tuple[str, dict[str, Optional[str]]]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT sku_uuid::text, raw_result, {', '.join(COLUMNS)} FROM brand_skus "
                        "WHERE extract_ok AND raw_result IS NOT NULL")
            changes = plan(cur.fetchall())
            if apply:
                sets = ", ".join(f"{c} = %s" for c in COLUMNS)
                for sku_uuid, new in changes:
                    cur.execute(f"UPDATE brand_skus SET {sets} WHERE sku_uuid = %s", (*new.values(), sku_uuid))
        if apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    return changes


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = p.parse_args(argv)
    changes = run(apply=args.apply)
    verb = "updated" if args.apply else "would be updated"
    print(f"brand_skus rows {verb}: {len(changes)}")
    for sku_uuid, _ in changes[:SHOWN]:
        print(f"  {sku_uuid}")
    if len(changes) > SHOWN:
        print(f"  … and {len(changes) - SHOWN} more")
    if changes and not args.apply:
        print("Dry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
