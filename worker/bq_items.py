"""BigQuery suffai_ops writes for customer garment photos.

The order must already exist (created on the Admin tab or loaded). Each photo is one garment item in
suffai_ops.commodities keyed by (order_id, item_id); this worker is the only writer of those rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from worker import vocab
from worker.photo_key import PhotoKey
from worker.result_utils import min_confidence
from worker.settings import settings

log = logging.getLogger("suffai-extraction.bq")

# (name, BigQuery type, value)
Param = Tuple[str, str, Any]

# Test hook: replaces the BigQuery round trip with (sql, params) -> rows.
_runner: Optional[Callable[[str, List[Param]], List[Dict[str, Any]]]] = None

ITEM_COLUMNS: List[Tuple[str, str]] = [
    ("customer_id", "STRING"),
    ("order_id", "STRING"),
    ("item_id", "STRING"),
    ("commodity_id", "STRING"),
    ("name", "STRING"),
    ("color", "STRING"),
    ("brand", "STRING"),
    ("pattern", "STRING"),
    ("size", "STRING"),
    ("material", "STRING"),
    ("sleeve_type", "STRING"),
    ("collar_type", "STRING"),
    ("gender", "STRING"),
    ("confidence_score", "FLOAT64"),
    ("reason", "STRING"),
    ("gcs_object_key", "STRING"),
    ("image_status", "STRING"),
    ("source", "STRING"),
    ("model_version", "STRING"),
    ("extracted_at", "TIMESTAMP"),
    ("brand_sku", "STRING"),
    ("brand_style_code", "STRING"),
    ("catalog_match", "STRING"),
    ("garment_category", "STRING"),
    ("garment_subcategory", "STRING"),
    ("sleeve_length", "STRING"),
    ("pairs_with", "STRING"),
]
KEY_COLUMNS = ("order_id", "item_id")

IMAGE_STATUS = {"COMPLETED": "extracted", "NEEDS_REVIEW": "needs_review", "FAILED": "failed"}
DEPARTMENT_TO_GENDER = {"men": "Male", "women": "Female", "unisex": "Unisex", "kids": "Kids"}
GARMENT_GENDERS = frozenset(DEPARTMENT_TO_GENDER.values())
_UNKNOWN = {"", "unknown", "none", "null", "n/a", "not_applicable"}


class BigQueryNotConfigured(RuntimeError):
    pass


def enabled() -> bool:
    return bool(settings.USE_BQ and settings.PROJECT_ID)


def table(name: str) -> str:
    if not settings.PROJECT_ID:
        raise BigQueryNotConfigured("GOOGLE_CLOUD_PROJECT is required for BigQuery writes")
    return f"`{settings.PROJECT_ID}.{settings.BQ_DATASET}.{name}`"


def _run(sql: str, params: List[Param]) -> List[Dict[str, Any]]:
    if _runner is not None:
        return _runner(sql, params)
    from google.cloud import bigquery  # type: ignore

    client = bigquery.Client(project=settings.PROJECT_ID)
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(n, t, v) for n, t, v in params]
    )
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


def lookup_order(order_id: str, customer_id: str) -> Optional[Dict[str, str]]:
    """The order when it exists, belongs to customer_id and that customer exists; None means unlinked.

    Same check as suffai-similarity (similarity/bq_sync.lookup_photo_owner).
    """
    sql = (
        "SELECT CAST(o._id AS STRING) AS order_id, CAST(o.customer_id AS STRING) AS customer_id "
        f"FROM {table('orders')} o JOIN {table('customers')} c ON CAST(c._id AS STRING) = CAST(o.customer_id AS STRING) "
        "WHERE CAST(o._id AS STRING) = @order_id AND CAST(o.customer_id AS STRING) = @customer_id LIMIT 1"
    )
    rows = _run(sql, [("order_id", "STRING", order_id), ("customer_id", "STRING", customer_id)])
    return rows[0] if rows else None


def supersede_legacy(order_id: str) -> None:
    """Hide the order's CSV-loaded items once photos arrive, so the order counts its photos instead.

    The garment_items view leaves out image_status 'superseded'. Matches nothing after the first photo.
    """
    _run(
        f"UPDATE {table('commodities')} SET image_status = 'superseded' "
        "WHERE order_id = @order_id AND IFNULL(source, 'legacy') = 'legacy' "
        "AND IFNULL(image_status, '') != 'superseded'",
        [("order_id", "STRING", order_id)],
    )


def _clean(v: Any) -> Optional[str]:
    s = str(v if v is not None else "").strip()
    return None if s.lower() in _UNKNOWN else s


def _label(v: Any) -> str:
    """Dashboard label: 'long_sleeve' -> 'Long Sleeve'; unknown stays 'unknown' so dashboards skip it."""
    s = _clean(v)
    return s.replace("_", " ").title() if s else "unknown"


def _size(result: Dict[str, Any]) -> str:
    s = _clean(result.get("size_normalized")) or _clean(result.get("size_raw"))
    return s.upper() if s and len(s) <= 4 else (s or "unknown")


def _garment_gender(dept: str) -> str:
    g = vocab.canonical("garment_gender", DEPARTMENT_TO_GENDER.get(dept, dept))
    return g if g in GARMENT_GENDERS else "unknown"


def item_attributes(result: Dict[str, Any]) -> Dict[str, str]:
    """Garment attributes as the dashboards spell them, for customer photos and brand SKUs alike.

    Brand, colour, garment name and garment gender use the canonical spelling from ref_values, so a
    photo's "Levis" lands on the same dashboard row as the CSV's "Levi's"; the rest read like
    'Long Sleeve'. Unknown values are 'unknown'.
    """
    name = _clean(result.get("display_name")) or _label(result.get("commodity_type"))
    dept = (_clean(result.get("garment_department")) or "").lower()
    return {
        "name": vocab.canonical("garment_name", name),
        "color": vocab.canonical("color", _label(result.get("primary_color"))),
        "brand": vocab.canonical("brand", _clean(result.get("brand_name"))),
        "pattern": _label(result.get("pattern")),
        "size": _size(result),
        "material": _label(result.get("material_type")),
        "sleeve_type": _label(result.get("sleeve_type")),
        "collar_type": _label(result.get("collar_type")),
        "gender": _garment_gender(dept),
    }


def item_row(
    photo: PhotoKey, *, object_key: str, result: Dict[str, Any], status: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Map an extraction result onto a suffai_ops.commodities row (same columns the dashboards read)."""
    scored = result.get("model_version") not in (None, "stub-v1")
    reason = _clean(result.get("notes")) or "Extracted from the garment photo"
    return {
        "customer_id": photo.customer_id,
        "order_id": photo.order_id,
        "item_id": photo.item_id,
        "commodity_id": photo.item_id,
        **item_attributes(result),
        "confidence_score": round(min_confidence(result), 3) if scored else None,
        "reason": reason[:300],
        "gcs_object_key": object_key,
        "image_status": IMAGE_STATUS.get(status, "needs_review"),
        "source": "pipeline",
        "model_version": result.get("model_version"),
        "extracted_at": now or datetime.now(timezone.utc),
        "brand_sku": None,
        "brand_style_code": None,
        "catalog_match": None,
        **outfit_columns(result),
    }


def outfit_columns(result: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Outfit tags for Complete your outfit; pairs_with is stored comma-separated."""
    pairs = [str(p) for p in result.get("pairs_with") or [] if _clean(p)]
    return {
        "garment_category": _clean(result.get("garment_category")),
        "garment_subcategory": _clean(result.get("garment_subcategory")),
        "sleeve_length": _clean(result.get("sleeve_length")),
        "pairs_with": ",".join(pairs) or None,
    }


def merge_sql() -> str:
    cols = [c for c, _ in ITEM_COLUMNS]
    updates = ", ".join(f"{c} = @{c}" for c in cols if c not in KEY_COLUMNS)
    return (
        f"MERGE {table('commodities')} AS t "
        "USING (SELECT @order_id AS order_id, @item_id AS item_id) AS s "
        "ON t.order_id = s.order_id AND t.item_id = s.item_id "
        f"WHEN MATCHED THEN UPDATE SET {updates} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join('@' + c for c in cols)})"
    )


def upsert_item(row: Dict[str, Any]) -> None:
    _run(merge_sql(), [(c, t, row.get(c)) for c, t in ITEM_COLUMNS])
