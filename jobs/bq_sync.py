"""
Cloud Run Job: sync normalized facts + marts into BigQuery (GCP-native warehouse).

  python -m jobs.bq_sync
Env: GOOGLE_CLOUD_PROJECT, BQ_* datasets, DATA_DIR

Tables and columns are defined by analytics/bigquery/002_star_schema.sql. SCHEMAS below must
match it exactly (tests/test_bq_sync_schema.py enforces this); loads never autodetect.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from suffai import METRIC_VERSION, TAXONOMY_VERSION
from suffai.ingest import load_source_collections, normalize_all
from suffai.metrics.eligibility import eligible_orders
from suffai.metrics.geo import geo_active_customers, commodity_popularity
from suffai.metrics.rfm import compute_rfm_snapshots, rfm_distribution

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("suffai.bq_sync")

# (column, BigQuery type, required)
Column = tuple[str, str, bool]

SCHEMAS: dict[str, dict[str, Any]] = {
    "fact_order": {
        "dataset": "staging",
        "partition": "order_date",
        "columns": [
            ("order_id", "STRING", True),
            ("customer_key", "STRING", False),
            ("store_key", "STRING", False),
            ("geo_key", "STRING", False),
            ("order_date", "DATE", False),
            ("net_paid", "NUMERIC", False),
            ("order_status", "STRING", False),
            ("payment_status", "STRING", False),
            ("eligible_default", "BOOL", False),
            ("source_watermark", "TIMESTAMP", False),
        ],
    },
    "fact_order_commodity": {
        "dataset": "staging",
        "partition": "order_date",
        "columns": [
            ("line_id", "STRING", True),
            ("order_id", "STRING", True),
            ("service_key", "STRING", False),
            ("commodity_key", "STRING", False),
            ("quantity", "INT64", False),
            ("line_price", "NUMERIC", False),
            ("order_date", "DATE", False),
        ],
    },
    "fact_customer_segment_snapshot": {
        "dataset": "staging",
        "partition": "as_of_date",
        "columns": [
            ("snapshot_key", "STRING", True),
            ("customer_key", "STRING", True),
            ("as_of_date", "DATE", True),
            ("recency_days", "INT64", False),
            ("frequency", "INT64", False),
            ("monetary", "NUMERIC", False),
            ("rfm_segment", "STRING", False),
            ("rule_version", "STRING", False),
        ],
    },
    "mart_geo_trends": {
        "dataset": "marts",
        "partition": "period_start",
        "columns": [
            ("aggregate_key", "STRING", True),
            ("geo_key", "STRING", False),
            ("period_start", "DATE", False),
            ("metric_key", "STRING", False),
            ("metric_value", "FLOAT64", False),
            ("cohort_size", "INT64", False),
            ("suppressed", "BOOL", False),
            ("metric_version", "STRING", False),
            ("source_watermark", "TIMESTAMP", False),
            ("taxonomy_version", "STRING", False),
        ],
    },
    "mart_commodity_popularity": {
        "dataset": "marts",
        "partition": "period_start",
        "columns": [
            ("commodity_name", "STRING", True),
            ("quantity", "INT64", False),
            ("line_count", "INT64", False),
            ("distinct_customers", "INT64", False),
            ("suppressed", "BOOL", False),
            ("metric_key", "STRING", False),
            ("metric_version", "STRING", False),
            ("period_start", "DATE", False),
            ("source_watermark", "TIMESTAMP", False),
        ],
    },
    "mart_rfm_distribution": {
        "dataset": "marts",
        "partition": "period_start",
        "columns": [
            ("rfm_segment", "STRING", True),
            ("customers", "INT64", False),
            ("share_pct", "FLOAT64", False),
            ("rule_version", "STRING", False),
            ("period_start", "DATE", False),
            ("source_watermark", "TIMESTAMP", False),
        ],
    },
    "ingest_runs": {
        "dataset": "raw",
        "partition": None,
        "columns": [
            ("run_at", "TIMESTAMP", True),
            ("orders", "INT64", False),
            ("lines", "INT64", False),
            ("rfm", "INT64", False),
        ],
    },
}


def _dataset(kind: str) -> str:
    return {
        "raw": settings.BQ_RAW_DATASET,
        "staging": settings.BQ_STAGING_DATASET,
        "marts": settings.BQ_MARTS_DATASET,
    }[kind]


def _day(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Normalized rows -> DDL rows
# ---------------------------------------------------------------------------


def map_order(o: dict[str, Any], *, eligible_ids: set[str], watermark: datetime) -> dict[str, Any]:
    return {
        "order_id": o.get("order_id"),
        "customer_key": o.get("customer_id"),
        "store_key": o.get("store_id"),
        "geo_key": o.get("order_pin") or "UNKNOWN",
        "order_date": _day(o.get("ordered_at")),
        "net_paid": o.get("total_amount"),
        "order_status": o.get("order_status"),
        "payment_status": o.get("payment_status"),
        "eligible_default": o.get("order_id") in eligible_ids,
        "source_watermark": watermark,
    }


def map_order_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_id": line.get("line_id"),
        "order_id": line.get("order_id"),
        "service_key": line.get("service_id"),
        "commodity_key": line.get("catalog_commodity_id"),
        "quantity": line.get("quantity"),
        "line_price": line.get("line_price"),
        "order_date": _day(line.get("ordered_at")),
    }


def map_segment_snapshot(s: dict[str, Any]) -> dict[str, Any]:
    cid, as_of = s.get("customer_id"), _day(s.get("as_of_date"))
    return {
        "snapshot_key": f"{cid}-{as_of.isoformat()}" if cid and as_of else None,
        "customer_key": cid,
        "as_of_date": as_of,
        "recency_days": s.get("recency_days"),
        "frequency": s.get("frequency"),
        "monetary": s.get("monetary"),
        "rfm_segment": s.get("rfm_segment"),
        "rule_version": s.get("rule_version"),
    }


def map_geo_tile(t: dict[str, Any], *, watermark: datetime) -> dict[str, Any]:
    value = t.get("active_customers")
    return {
        "aggregate_key": f"{t.get('geo_key')}-active_customers",
        "geo_key": t.get("geo_key"),
        "period_start": watermark.date(),
        "metric_key": "active_customers",
        "metric_value": float(value) if value is not None else None,
        "cohort_size": t.get("denominator"),
        "suppressed": bool(t.get("suppressed")),
        "metric_version": t.get("metric_version") or METRIC_VERSION,
        "source_watermark": watermark,
        "taxonomy_version": TAXONOMY_VERSION,
    }


def map_commodity(c: dict[str, Any], *, watermark: datetime) -> dict[str, Any]:
    return {
        "commodity_name": c.get("commodity_name"),
        "quantity": c.get("quantity"),
        "line_count": c.get("line_count"),
        "distinct_customers": c.get("distinct_customers"),
        "suppressed": bool(c.get("suppressed")),
        "metric_key": c.get("metric_key"),
        "metric_version": c.get("metric_version") or METRIC_VERSION,
        "period_start": watermark.date(),
        "source_watermark": watermark,
    }


def map_rfm_share(r: dict[str, Any], *, rule_version: Optional[str], watermark: datetime) -> dict[str, Any]:
    return {
        "rfm_segment": r.get("rfm_segment"),
        "customers": r.get("customers"),
        "share_pct": r.get("share_pct"),
        "rule_version": rule_version,
        "period_start": watermark.date(),
        "source_watermark": watermark,
    }


def conform(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only DDL columns and drop rows missing a required (NOT NULL) column."""
    cols = SCHEMAS[table]["columns"]
    names = [c[0] for c in cols]
    required = [c[0] for c in cols if c[2]]
    out, dropped = [], 0
    for r in rows:
        if any(r.get(k) is None for k in required):
            dropped += 1
            continue
        out.append({k: r.get(k) for k in names})
    if dropped:
        log.warning("%s: skipped %s row(s) missing a required column (%s)", table, dropped, ", ".join(required))
    return out


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


def _client():
    from google.cloud import bigquery

    project = settings.BQ_PROJECT or settings.GCP_PROJECT
    if not project:
        raise RuntimeError("BQ_PROJECT / GOOGLE_CLOUD_PROJECT required")
    return bigquery.Client(project=project, location=settings.BQ_LOCATION), project


def _ensure_datasets(client, project: str) -> None:
    from google.cloud import bigquery

    for ds in (settings.BQ_RAW_DATASET, settings.BQ_STAGING_DATASET, settings.BQ_MARTS_DATASET):
        ref = bigquery.Dataset(f"{project}.{ds}")
        ref.location = settings.BQ_LOCATION
        client.create_dataset(ref, exists_ok=True)


def _json_value(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _load_rows(client, project: str, table: str, rows: list[dict], write: str = "WRITE_TRUNCATE") -> int:
    from google.cloud import bigquery

    spec = SCHEMAS[table]
    table_id = f"{project}.{_dataset(spec['dataset'])}.{table}"
    rows = conform(table, rows)
    if not rows:
        log.info("skip empty %s", table_id)
        return 0
    job_config = bigquery.LoadJobConfig(
        write_disposition=getattr(bigquery.WriteDisposition, write),
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        autodetect=False,
        schema=[
            bigquery.SchemaField(name, typ, mode="REQUIRED" if req else "NULLABLE")
            for name, typ, req in spec["columns"]
        ],
    )
    if spec["partition"]:
        job_config.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=spec["partition"]
        )
    payload = [{k: _json_value(v) for k, v in r.items()} for r in rows]
    job = client.load_table_from_json(payload, table_id, job_config=job_config)
    job.result()
    log.info("loaded %s rows=%s", table_id, len(payload))
    return len(payload)


def run(loader: Optional[Callable[..., int]] = None) -> dict:
    if loader is None:
        client, project = _client()
        _ensure_datasets(client, project)
        loader = lambda table, rows, **kw: _load_rows(client, project, table, rows, **kw)  # noqa: E731

    raw = load_source_collections(settings.DATA_DIR)
    norm = normalize_all(raw)
    watermark = datetime.now(timezone.utc)

    eligible_ids = {o["order_id"] for o in eligible_orders(norm["orders"])}
    rfm = compute_rfm_snapshots(norm["orders"], norm["stores"])
    tiles = geo_active_customers(norm["orders"])
    commodities = commodity_popularity(norm["order_commodity_lines"], eligible_ids)
    rule_version = rfm[0].get("rule_version") if rfm else None

    n_orders = loader("fact_order", [map_order(o, eligible_ids=eligible_ids, watermark=watermark) for o in norm["orders"]])
    n_lines = loader("fact_order_commodity", [map_order_line(l) for l in norm["order_commodity_lines"]])
    n_rfm = loader("fact_customer_segment_snapshot", [map_segment_snapshot(s) for s in rfm])
    loader("mart_geo_trends", [map_geo_tile(t, watermark=watermark) for t in tiles])
    loader("mart_commodity_popularity", [map_commodity(c, watermark=watermark) for c in commodities[:500]])
    loader(
        "mart_rfm_distribution",
        [map_rfm_share(r, rule_version=rule_version, watermark=watermark) for r in rfm_distribution(rfm)],
    )
    loader(
        "ingest_runs",
        [{"run_at": watermark, "orders": n_orders, "lines": n_lines, "rfm": n_rfm}],
        write="WRITE_APPEND",
    )
    return {"watermark": watermark.isoformat(), "orders": n_orders, "lines": n_lines, "rfm": n_rfm}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
