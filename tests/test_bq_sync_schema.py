"""jobs/bq_sync.py must write exactly the columns declared in analytics/bigquery/002_star_schema.sql."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import jobs.bq_sync as bq_sync

DDL = Path(__file__).resolve().parents[1] / "analytics" / "bigquery" / "002_star_schema.sql"
DDL_TYPES = {"STRING": "STRING", "DATE": "DATE", "NUMERIC": "NUMERIC", "INT64": "INT64",
             "FLOAT64": "FLOAT64", "BOOL": "BOOL", "TIMESTAMP": "TIMESTAMP"}


def _parse_ddl() -> dict[str, dict]:
    tables = {}
    pattern = re.compile(
        r"CREATE TABLE IF NOT EXISTS\s+suffai_(\w+)\.(\w+)\s*\((.*?)\)\s*(?:PARTITION BY\s+(\w+))?\s*;",
        re.S,
    )
    for dataset, name, body, partition in pattern.findall(DDL.read_text(encoding="utf-8")):
        cols = []
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            parts = line.split()
            cols.append((parts[0], DDL_TYPES[parts[1]], "NOT NULL" in line))
        tables[name] = {"dataset": dataset, "partition": partition or None, "columns": cols}
    return tables


def test_schemas_match_ddl_exactly():
    ddl = _parse_ddl()
    for table, spec in bq_sync.SCHEMAS.items():
        assert table in ddl, f"{table} has no DDL in 002_star_schema.sql"
        assert spec["dataset"] == ddl[table]["dataset"], table
        assert spec["partition"] == ddl[table]["partition"], table
        assert spec["columns"] == ddl[table]["columns"], table


WM = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)
ORDER = {"order_id": "o1", "customer_id": "c1", "store_id": "s1", "order_pin": "560001",
         "ordered_at": datetime(2026, 9, 1, 10, 0), "total_amount": 420.5,
         "order_status": "delivered", "payment_status": "paid", "bill_no": "B1", "refund_flag": False}
LINE = {"line_id": "l1", "order_id": "o1", "order_item_id": "i1", "customer_id": "c1", "store_id": "s1",
        "service_id": "sv1", "service_name": "Wash", "catalog_commodity_id": "cm1", "commodity_name": "Shirt",
        "menu": None, "quantity": 3, "line_price": 90.0, "order_pin": "560001",
        "ordered_at": datetime(2026, 9, 1, 10, 0)}
SNAPSHOT = {"customer_id": "c1", "recency_days": 12, "frequency": 4, "monetary": 1500.0,
            "premium_store_preference": 0.0, "insufficient_history": False, "last_order_at": None,
            "r_score": 4, "f_score": 4, "m_score": 4, "rfm_segment": "Champions",
            "as_of_date": "2026-10-01", "rule_version": "rfm-v1", "window_days": 180}
TILE = {"geo_key": "560001", "pin_code": "560001", "area": "x", "active_customers": 25,
        "suppressed": False, "denominator": 25, "metric_key": "active_customers", "metric_version": "v1"}
COMMODITY = {"commodity_name": "Shirt", "quantity": 30, "line_count": 10, "distinct_customers": 8,
             "suppressed": False, "metric_key": "commodity_popularity", "metric_version": "v1", "note": "n"}
RFM_SHARE = {"rfm_segment": "Champions", "customers": 10, "share_pct": 25.0}


def _keys(table: str) -> list[str]:
    return [c[0] for c in bq_sync.SCHEMAS[table]["columns"]]


def test_mappers_emit_exactly_the_ddl_columns():
    cases = {
        "fact_order": bq_sync.map_order(ORDER, eligible_ids={"o1"}, watermark=WM),
        "fact_order_commodity": bq_sync.map_order_line(LINE),
        "fact_customer_segment_snapshot": bq_sync.map_segment_snapshot(SNAPSHOT),
        "mart_geo_trends": bq_sync.map_geo_tile(TILE, watermark=WM),
        "mart_commodity_popularity": bq_sync.map_commodity(COMMODITY, watermark=WM),
        "mart_rfm_distribution": bq_sync.map_rfm_share(RFM_SHARE, rule_version="rfm-v1", watermark=WM),
    }
    for table, row in cases.items():
        assert sorted(row) == sorted(_keys(table)), table
        assert bq_sync.conform(table, [row]) == [{k: row[k] for k in _keys(table)}], table


def test_mapped_values():
    o = bq_sync.map_order(ORDER, eligible_ids={"o1"}, watermark=WM)
    assert o["customer_key"] == "c1" and o["net_paid"] == 420.5 and o["eligible_default"] is True
    assert o["order_date"].isoformat() == "2026-09-01"
    assert bq_sync.map_order({**ORDER, "order_pin": None}, eligible_ids=set(), watermark=WM)["geo_key"] == "UNKNOWN"
    s = bq_sync.map_segment_snapshot(SNAPSHOT)
    assert s["snapshot_key"] == "c1-2026-10-01"
    g = bq_sync.map_geo_tile({**TILE, "active_customers": None, "suppressed": True}, watermark=WM)
    assert g["metric_value"] is None and g["period_start"].isoformat() == "2026-10-18"


def test_conform_drops_rows_missing_required_columns():
    rows = [bq_sync.map_order_line(LINE), bq_sync.map_order_line({**LINE, "line_id": None})]
    assert len(bq_sync.conform("fact_order_commodity", rows)) == 1


def test_run_loads_every_table_with_conformant_rows(monkeypatch):
    monkeypatch.setattr(bq_sync, "load_source_collections", lambda _d: {})
    monkeypatch.setattr(bq_sync, "normalize_all", lambda _r: {
        "orders": [ORDER], "order_commodity_lines": [LINE], "stores": []})
    monkeypatch.setattr(bq_sync, "eligible_orders", lambda orders: orders)
    monkeypatch.setattr(bq_sync, "compute_rfm_snapshots", lambda o, s: [SNAPSHOT])
    monkeypatch.setattr(bq_sync, "geo_active_customers", lambda o: [TILE])
    monkeypatch.setattr(bq_sync, "commodity_popularity", lambda l, ids: [COMMODITY])
    monkeypatch.setattr(bq_sync, "rfm_distribution", lambda r: [RFM_SHARE])
    loaded: dict[str, list] = {}

    def loader(table, rows, **_kw):
        loaded[table] = bq_sync.conform(table, rows)
        return len(loaded[table])

    out = bq_sync.run(loader=loader)
    assert set(loaded) == set(bq_sync.SCHEMAS)
    for table, rows in loaded.items():
        assert rows and all(sorted(r) == sorted(_keys(table)) for r in rows), table
    assert out["orders"] == 1 and loaded["ingest_runs"][0]["orders"] == 1
