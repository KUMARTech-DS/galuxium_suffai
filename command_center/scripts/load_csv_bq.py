#!/usr/bin/env python3
"""Load a CSV into a suffai_ops table using the column types declared in create_bq_tables.sql.

BigQuery matches CSV columns by position, so the load schema is built in the CSV's header order.
A CSV column the DDL doesn't declare fails the load; DDL columns the CSV lacks are added afterwards
as NULL (e.g. commodities.confidence_score) instead of being silently absent.

  python load_csv_bq.py --project P --dataset suffai_ops --table commodities \
      --header path/to/commodities.csv --uri gs://bucket/tables/commodities.csv

--append keeps the table and inserts only CSV rows whose key (customers / orders: _id; commodities:
order_id + commodity_id) is not there yet, e.g. the sample data next to live customers.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

DDL_PATH = Path(__file__).resolve().parent / "create_bq_tables.sql"


def ddl_columns(table: str, ddl_text: str) -> list[tuple[str, str, bool]]:
    """(name, type, required) for `table`, in DDL order."""
    m = re.search(rf"CREATE TABLE IF NOT EXISTS\s+\w+\.{re.escape(table)}\s*\((.*?)\)\s*;", ddl_text, re.S)
    if not m:
        raise SystemExit(f"{table}: no CREATE TABLE in {DDL_PATH.name}")
    cols = []
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        name, typ = line.split()[:2]
        cols.append((name, typ.upper(), "NOT NULL" in line.upper()))
    return cols


def csv_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [h.strip() for h in next(csv.reader(fh))]


def plan_load(table: str, header: list[str], ddl_text: str) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, bool]]]:
    """Return (load schema in header order, DDL columns missing from the CSV)."""
    cols = ddl_columns(table, ddl_text)
    by_name = {c[0]: c for c in cols}
    unknown = [h for h in header if h not in by_name]
    if unknown:
        raise SystemExit(f"{table}: CSV columns not declared in {DDL_PATH.name}: {', '.join(unknown)}")
    return [by_name[h] for h in header], [c for c in cols if c[0] not in header]


LIVE_SOURCES = ("admin", "pipeline")


def live_row_count(client, table_id: str) -> int:
    """Rows created on the Admin tab or by the photo pipeline (0 if the table or its source column is missing)."""
    try:
        table = client.get_table(table_id)
    except Exception:
        return 0
    if not any(f.name == "source" for f in table.schema):
        return 0
    sources = ", ".join(f"'{s}'" for s in LIVE_SOURCES)
    rows = list(client.query(f"SELECT COUNT(*) AS n FROM `{table_id}` WHERE source IN ({sources})").result())
    return int(rows[0]["n"]) if rows else 0


def guard_truncate(client, table_id: str, *, force: bool) -> None:
    """WRITE_TRUNCATE replaces the whole table, so never silently drop Admin / pipeline rows."""
    n = live_row_count(client, table_id)
    if n and not force:
        raise SystemExit(
            f"{table_id}: {n} row(s) were created by Admin or the photo pipeline and a CSV load would delete them. "
            "Re-run with --force only if you really want to replace the table."
        )


APPEND_KEYS = {"customers": ("_id",), "orders": ("_id",), "commodities": ("order_id", "commodity_id")}


def append_sql(table_id: str, staging_id: str, table: str, header: list[str]) -> str:
    """INSERT the staging rows whose key is not in the table yet (first CSV row per key)."""
    key = APPEND_KEYS.get(table)
    if not key:
        raise SystemExit(f"--append supports {', '.join(APPEND_KEYS)}, not {table}")
    absent = [k for k in key if k not in header]
    if absent:
        raise SystemExit(f"{table}: --append needs CSV column(s) {', '.join(absent)}")
    cols = ", ".join(f"`{h}`" for h in header)
    on = " AND ".join(f"CAST(t.`{k}` AS STRING) = CAST(s.`{k}` AS STRING)" for k in key)
    part = ", ".join(f"CAST(s.`{k}` AS STRING)" for k in key)
    return (
        f"INSERT INTO `{table_id}` ({cols}) "
        f"SELECT {', '.join(f's.`{h}`' for h in header)} FROM `{staging_id}` s "
        f"WHERE NOT EXISTS (SELECT 1 FROM `{table_id}` t WHERE {on}) "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {part}) = 1"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="suffai_ops")
    ap.add_argument("--table", required=True)
    ap.add_argument("--header", required=True, type=Path, help="Local copy of the CSV (header row is read)")
    ap.add_argument("--uri", help="gs:// URI to load from (defaults to loading the local file)")
    ap.add_argument("--location", default=None)
    ap.add_argument("--force", action="store_true", help="Replace the table even if it holds Admin / pipeline rows")
    ap.add_argument("--append", action="store_true", help="Keep the table; insert only rows whose key is new")
    args = ap.parse_args(argv)

    from google.cloud import bigquery  # type: ignore

    header = csv_header(args.header)
    schema, missing = plan_load(args.table, header, DDL_PATH.read_text(encoding="utf-8"))
    client = bigquery.Client(project=args.project, location=args.location)
    table_id = f"{args.project}.{args.dataset}.{args.table}"
    target_id = table_id
    if args.append:
        table_id = f"{target_id}__csv_append"
        insert = append_sql(target_id, table_id, args.table, header)
        client.get_table(target_id)  # appending needs the table; a first load is a normal (truncate) load
    else:
        guard_truncate(client, table_id, force=args.force)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        autodetect=False,
        allow_quoted_newlines=True,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=[bigquery.SchemaField(n, t, mode="REQUIRED" if req else "NULLABLE") for n, t, req in schema],
    )
    if args.uri:
        job = client.load_table_from_uri(args.uri, table_id, job_config=job_config)
    else:
        with args.header.open("rb") as fh:
            job = client.load_table_from_file(fh, table_id, job_config=job_config)
    job.result()
    if args.append:
        try:
            job = client.query(insert)
            job.result()
        finally:
            client.delete_table(table_id, not_found_ok=True)
        print(f"Appended {job.num_dml_affected_rows or 0} new row(s) to {target_id}; rows with a known key were skipped")
        return 0
    for name, typ, _req in missing:
        client.query(f"ALTER TABLE `{table_id}` ADD COLUMN IF NOT EXISTS `{name}` {typ}").result()
    rows = client.get_table(table_id).num_rows
    added = f"; added NULL column(s): {', '.join(c[0] for c in missing)}" if missing else ""
    print(f"Loaded {table_id} rows={rows}{added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
