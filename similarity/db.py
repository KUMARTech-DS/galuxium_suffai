"""Cloud SQL persistence for unique garments, wardrobes and similarity events.

Garment counts per PIN live in BigQuery (garment_items); this service writes one observation row per
photo there (bq_sync) and tracks in garment_similarity_events.bq_synced_at that the row was written.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from similarity.settings import settings

log = logging.getLogger("similarity.db")

# Outcomes that write a BigQuery observation row; other events have nothing to send.
SYNC_OUTCOMES = ("user_match", "global_match_new_user_link", "created_new", "user_sku_match", "global_sku_match")
EVENT_COLUMNS = ("id", "gcs_bucket", "gcs_object_key", "user_uuid", "pin_code", "brand_name", "outcome",
                 "matched_garment_id", "similarity_score", "order_id", "item_id", "idempotency_key",
                 "created_at", "bq_synced_at")
_EVENT_SELECT = ("SELECT id::text, gcs_bucket, gcs_object_key, user_uuid, pin_code, brand_name, outcome, "
                 "matched_garment_id::text, similarity_score, order_id, item_id, idempotency_key, "
                 "created_at, bq_synced_at FROM garment_similarity_events")


@contextmanager
def connect() -> Iterator[Any]:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    import psycopg2

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _event(row: Sequence[Any]) -> Dict[str, Any]:
    return dict(zip(EVENT_COLUMNS, row))


def event_for_key(idempotency_key: str) -> Optional[Dict[str, Any]]:
    """The event already recorded for this upload (Pub/Sub message), or None."""
    if not idempotency_key:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"{_EVENT_SELECT} WHERE idempotency_key = %s LIMIT 1", (idempotency_key,))
            row = cur.fetchone()
    return _event(row) if row else None


def record_event(
    *,
    gcs_bucket: Optional[str],
    gcs_object_key: str,
    user_uuid: Optional[str],
    pin_code: Optional[str],
    brand_name: Optional[str],
    outcome: str,
    matched_garment_id: Optional[str] = None,
    similarity_score: Optional[float] = None,
    error_message: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    order_id: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert the event; returns {id, created_at}. Events with no observation row count as synced."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO garment_similarity_events (
                  gcs_bucket, gcs_object_key, user_uuid, pin_code, brand_name,
                  outcome, matched_garment_id, similarity_score, error_message, idempotency_key,
                  order_id, item_id, bq_synced_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN NULL ELSE now() END)
                RETURNING id::text, created_at
                """,
                (
                    gcs_bucket,
                    gcs_object_key,
                    user_uuid,
                    pin_code,
                    brand_name,
                    outcome,
                    matched_garment_id,
                    similarity_score,
                    error_message,
                    idempotency_key,
                    order_id,
                    item_id,
                    outcome in SYNC_OUTCOMES,
                ),
            )
            event_id, created_at = cur.fetchone()
    return {"id": event_id, "created_at": created_at}


def mark_bq_synced(event_id: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE garment_similarity_events SET bq_synced_at = now() WHERE id = %s AND bq_synced_at IS NULL",
                (event_id,),
            )


def pending_bq_events(*, limit: int, older_than_sec: int) -> List[Dict[str, Any]]:
    """Events whose BigQuery observation row has not been written, oldest first."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"{_EVENT_SELECT} WHERE bq_synced_at IS NULL AND outcome = ANY(%s) "
                "AND matched_garment_id IS NOT NULL AND created_at < now() - make_interval(secs => %s) "
                "ORDER BY created_at LIMIT %s",
                (list(SYNC_OUTCOMES), older_than_sec, limit),
            )
            return [_event(r) for r in cur.fetchall()]


def load_user_embeddings(user_uuid: str) -> List[Tuple[str, List[float], Optional[str]]]:
    """Return list of (garment_id, embedding, brand_name) for the user."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ug.garment_id::text, ug.embedding, ug.brand_name
                FROM user_wardrobe uw
                JOIN unique_garments ug ON ug.garment_id = uw.garment_id
                WHERE uw.user_uuid = %s
                """,
                (user_uuid,),
            )
            rows = cur.fetchall()
    out: List[Tuple[str, List[float], Optional[str]]] = []
    for gid, emb, brand in rows:
        vec = emb if isinstance(emb, list) else json.loads(emb)
        out.append((gid, [float(x) for x in vec], brand))
    return out


def load_global_embeddings(
    *, brand_name: Optional[str], limit: int
) -> List[Tuple[str, List[float], Optional[str]]]:
    with connect() as conn:
        with conn.cursor() as cur:
            if brand_name:
                cur.execute(
                    """
                    SELECT garment_id::text, embedding, brand_name
                    FROM unique_garments
                    WHERE brand_name IS NOT NULL AND lower(brand_name) = lower(%s)
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (brand_name, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT garment_id::text, embedding, brand_name
                    FROM unique_garments
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
    out: List[Tuple[str, List[float], Optional[str]]] = []
    for gid, emb, brand in rows:
        vec = emb if isinstance(emb, list) else json.loads(emb)
        out.append((gid, [float(x) for x in vec], brand))
    return out


def load_embeddings(garment_ids: Sequence[str]) -> Dict[str, Tuple[List[float], Optional[str]]]:
    """garment_id -> (embedding, embedding_model) for the ids that exist in unique_garments."""
    ids = [str(g) for g in garment_ids]
    if not ids:
        return {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT garment_id::text, embedding, embedding_model FROM unique_garments "
                "WHERE garment_id = ANY(%s::uuid[])",
                (ids,),
            )
            rows = cur.fetchall()
    out: Dict[str, Tuple[List[float], Optional[str]]] = {}
    for gid, emb, model in rows:
        vec = emb if isinstance(emb, list) else json.loads(emb)
        out[str(gid).lower()] = ([float(x) for x in vec], model)
    return out


def create_unique_garment(
    *,
    bucket: str,
    object_key: str,
    embedding: Sequence[float],
    model: str,
    user_uuid: str,
    pin_code: str,
    brand_name: Optional[str],
    brand_sku: Optional[str] = None,
) -> str:
    garment_id = str(uuid.uuid4())
    sku_col, sku_val = (", brand_sku", ",%s") if brand_sku else ("", "")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO unique_garments (
                  garment_id, representative_gcs_bucket, representative_gcs_object_key,
                  brand_name, embedding, embedding_model, embedding_dim,
                  first_user_uuid, first_pin_code, observation_count_total{sku_col}
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,1{sku_val})
                """,
                (
                    garment_id,
                    bucket,
                    object_key,
                    brand_name,
                    json.dumps(list(embedding)),
                    model,
                    len(embedding),
                    user_uuid,
                    pin_code,
                    *((brand_sku,) if brand_sku else ()),
                ),
            )
            cur.execute(
                """
                INSERT INTO user_wardrobe (
                  user_uuid, garment_id, first_gcs_object_key, pin_code_at_first_sight
                ) VALUES (%s,%s,%s,%s)
                """,
                (user_uuid, garment_id, object_key, pin_code),
            )
    return garment_id


def find_user_garment_by_sku(user_uuid: str, brand_sku: str) -> Optional[str]:
    """This customer's unique garment already identified as this brand SKU."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ug.garment_id::text
                FROM user_wardrobe uw
                JOIN unique_garments ug ON ug.garment_id = uw.garment_id
                WHERE uw.user_uuid = %s AND ug.brand_sku = %s
                ORDER BY ug.observation_count_total DESC, ug.created_at ASC
                LIMIT 1
                """,
                (user_uuid, brand_sku),
            )
            row = cur.fetchone()
    return row[0] if row else None


def find_garment_by_sku(brand_sku: str) -> Optional[str]:
    """Any customer's unique garment identified as this brand SKU."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT garment_id::text FROM unique_garments
                WHERE brand_sku = %s
                ORDER BY observation_count_total DESC, created_at ASC
                LIMIT 1
                """,
                (brand_sku,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def set_garment_sku(garment_id: str, brand_sku: str) -> None:
    """Backfill the SKU on a fingerprint-matched garment that had none."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE unique_garments SET brand_sku = %s, updated_at = now()
                WHERE garment_id = %s AND brand_sku IS NULL
                """,
                (brand_sku, garment_id),
            )


def link_user_to_garment(
    *,
    user_uuid: str,
    garment_id: str,
    object_key: str,
    pin_code: str,
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_wardrobe (
                  user_uuid, garment_id, first_gcs_object_key, pin_code_at_first_sight
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (user_uuid, garment_id) DO NOTHING
                """,
                (user_uuid, garment_id, object_key, pin_code),
            )


def increment_total(*, garment_id: str) -> None:
    """One more photo of an existing garment (unique_garments.observation_count_total, read by Style)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE unique_garments
                SET observation_count_total = observation_count_total + 1,
                    updated_at = now()
                WHERE garment_id = %s
                """,
                (garment_id,),
            )
