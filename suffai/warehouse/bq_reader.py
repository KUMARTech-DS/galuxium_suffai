"""Read privacy-safe marts from BigQuery when USE_BIGQUERY_MARTS=true."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config import settings

log = logging.getLogger("suffai.bq_reader")


def _client():
    from google.cloud import bigquery

    project = settings.BQ_PROJECT or settings.GCP_PROJECT
    if not project:
        raise RuntimeError("BQ_PROJECT / GOOGLE_CLOUD_PROJECT required")
    return bigquery.Client(project=project, location=settings.BQ_LOCATION), project


def load_geo_tiles_from_marts() -> Optional[List[Dict[str, Any]]]:
    if not settings.USE_BIGQUERY_MARTS:
        return None
    try:
        client, project = _client()
        table = f"`{project}.{settings.BQ_MARTS_DATASET}.mart_geo_trends`"
        sql = f"""
        SELECT
          geo_key,
          metric_value AS active_customers,
          cohort_size AS denominator,
          suppressed,
          geo_key AS pin_code,
          geo_key AS area
        FROM {table}
        WHERE metric_key = 'active_customers'
        ORDER BY metric_value DESC NULLS LAST
        LIMIT 5000
        """
        rows = [dict(r) for r in client.query(sql).result()]
        for r in rows:
            r["geo_key"] = r.get("geo_key") or "UNKNOWN"
            r["suppressed"] = bool(r.get("suppressed"))
            if r["suppressed"]:
                r["active_customers"] = None
        log.info("loaded %s geo tiles from BigQuery marts", len(rows))
        return rows
    except Exception as exc:
        log.warning("BQ marts geo read failed, falling back to local: %s", exc)
        return None


def load_rfm_distribution_from_marts() -> Optional[List[Dict[str, Any]]]:
    if not settings.USE_BIGQUERY_MARTS:
        return None
    try:
        client, project = _client()
        table = f"`{project}.{settings.BQ_MARTS_DATASET}.mart_rfm_distribution`"
        sql = f"SELECT * FROM {table} LIMIT 500"
        return [dict(r) for r in client.query(sql).result()]
    except Exception as exc:
        log.warning("BQ marts RFM read failed: %s", exc)
        return None
