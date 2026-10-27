"""BigQuery helpers: photo owner check (same rule as suffai-extraction) + observation rows."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from similarity.photo_key import short
from similarity.settings import settings

log = logging.getLogger("similarity.bq")

Param = tuple[str, str, Any]

# Test hook: (sql, params) -> rows instead of BigQuery.
_runner: Optional[Callable[[str, list[Param]], list[dict[str, Any]]]] = None


class OwnerLookupUnavailable(RuntimeError):
    """BigQuery could not be asked; the photo is retried, not dropped."""


def _table(name: str) -> str:
    if not settings.PROJECT_ID:
        raise OwnerLookupUnavailable("GOOGLE_CLOUD_PROJECT unset; cannot check the photo's order")
    return f"`{settings.PROJECT_ID}.{settings.BQ_DATASET}.{name}`"


def _run(sql: str, params: list[Param]) -> list[dict[str, Any]]:
    if _runner is not None:
        return _runner(sql, params)
    from google.cloud import bigquery  # type: ignore

    client = bigquery.Client(project=settings.PROJECT_ID)
    cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter(n, t, v) for n, t, v in params])
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


def lookup_photo_owner(customer_id: str, order_id: str) -> Optional[dict[str, str]]:
    """{"pin_code"} of the customer when order_id exists, belongs to customer_id and the customer exists.

    None means the photo is unlinked (same outcome as extraction). The PIN is the customer's current one.
    """
    sql = (
        f"SELECT CAST(c.pin_code AS STRING) AS pin_code FROM {_table('orders')} o "
        f"JOIN {_table('customers')} c ON CAST(c._id AS STRING) = CAST(o.customer_id AS STRING) "
        "WHERE CAST(o._id AS STRING) = @order_id AND CAST(o.customer_id AS STRING) = @customer_id LIMIT 1"
    )
    try:
        rows = _run(sql, [("order_id", "STRING", order_id), ("customer_id", "STRING", customer_id)])
    except OwnerLookupUnavailable:
        raise
    except Exception as exc:
        raise OwnerLookupUnavailable(f"order lookup failed: {type(exc).__name__}") from exc
    if not rows:
        return None
    pin = str(rows[0].get("pin_code") or "").strip()
    log.info("photo owner customer=%s order=%s pin=%s", short(customer_id), short(order_id), pin or "-")
    return {"pin_code": pin}


class ObservationNotWritten(RuntimeError):
    """The BigQuery observation insert failed; the photo is retried (Pub/Sub) instead of dropped."""


def enabled() -> bool:
    return bool(settings.USE_BQ and settings.PROJECT_ID)


def observation_row(
    *,
    garment_id: str,
    user_uuid: str,
    pin_code: str,
    brand_name: Optional[str],
    outcome: str,
    gcs_object_key: str,
    similarity_score: Optional[float],
    order_id: Optional[str] = None,
    item_id: Optional[str] = None,
    event_ts: Optional[datetime] = None,
) -> dict[str, Any]:
    return {
        "event_ts": event_ts or datetime.now(timezone.utc),
        "garment_id": garment_id,
        "user_uuid": user_uuid,
        "pin_code": pin_code,
        "brand_name": brand_name,
        "outcome": outcome,
        "gcs_object_key": gcs_object_key,
        "similarity_score": similarity_score,
        "customer_id": user_uuid,
        "order_id": order_id,
        "item_id": item_id,
    }


_TYPES = {"similarity_score": "FLOAT64", "event_ts": "TIMESTAMP"}


def sync_observation(
    *,
    garment_id: str,
    user_uuid: str,
    pin_code: str,
    brand_name: Optional[str],
    outcome: str,
    gcs_object_key: str,
    similarity_score: Optional[float],
    event_ts: datetime,
    order_id: Optional[str] = None,
    item_id: Optional[str] = None,
) -> bool:
    """Insert one observation row with DML; True once written, False when BigQuery is off (USE_BQ).

    event_ts is the Cloud SQL event's created_at, so a resent row is identical to the first one and the
    garment_items view (latest row per order and item) is unchanged by duplicates. Raises
    ObservationNotWritten when the insert fails. DML (not the streaming API) so reconcile_data.py can
    delete or fix rows right away. customer_id / order_id / item_id come from the photo name and link the
    observation to the suffai_ops.commodities item written by suffai-extraction.
    """
    if not settings.USE_BQ:
        return False
    if not settings.PROJECT_ID:
        log.warning("USE_BQ=true but GOOGLE_CLOUD_PROJECT unset; skip BQ sync")
        return False
    row = observation_row(
        garment_id=garment_id,
        user_uuid=user_uuid,
        pin_code=pin_code,
        brand_name=brand_name,
        outcome=outcome,
        gcs_object_key=gcs_object_key,
        similarity_score=similarity_score,
        order_id=order_id,
        item_id=item_id,
        event_ts=event_ts,
    )
    cols = list(row)
    sql = (
        f"INSERT INTO {_table('garment_similarity_observations')} ({', '.join(cols)}) "
        f"VALUES ({', '.join('@' + c for c in cols)})"
    )
    params = [(c, _TYPES.get(c, "STRING"), row[c]) for c in cols]
    try:
        _run(sql, params)
    except Exception as exc:
        log.error("BQ observation insert failed garment=%s err=%s", garment_id, type(exc).__name__)
        raise ObservationNotWritten(f"BigQuery observation insert failed: {type(exc).__name__}") from exc
    log.info("BQ sync ok garment=%s pin=%s outcome=%s", garment_id, pin_code, outcome)
    return True
