"""Cloud SQL dual-write for API operational records (GCP-native persistence)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator

from config import settings

log = logging.getLogger("suffai.ops_db")


def configured() -> bool:
    return bool(settings.DATABASE_URL)


@contextmanager
def connect() -> Iterator[Any]:
    import psycopg
    from psycopg.rows import dict_row

    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_adv_schema() -> bool:
    """Idempotent create of ADV/privacy/ops mirror tables (002 migration)."""
    if not configured():
        return False
    from pathlib import Path

    migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "002_adv_privacy_ops.sql"
    if not migration.exists():
        return False
    try:
        statements = [
            s.strip()
            for s in migration.read_text(encoding="utf-8").split(";")
            if s.strip() and not s.strip().startswith("--")
        ]
        with connect() as conn:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
        return True
    except Exception as exc:
        log.warning("ensure_adv_schema skipped: %s", exc)
        return False


def upsert_json_document(collection: str, doc_id: str, payload: Dict[str, Any]) -> bool:
    if not configured() or not doc_id:
        return False
    try:
        from psycopg.types.json import Json

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops_json_documents (collection, doc_id, payload, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (collection, doc_id)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                    """,
                    (collection, str(doc_id), Json(payload)),
                )
        return True
    except Exception as exc:
        log.debug("ops_json upsert failed (%s/%s): %s", collection, doc_id, exc)
        return False


def mirror_collection(collection: str, rows: list[Dict[str, Any]], id_keys: tuple[str, ...] = ("id",)) -> int:
    n = 0
    for row in rows:
        doc_id = None
        for k in id_keys:
            if row.get(k):
                doc_id = str(row[k])
                break
        if not doc_id:
            continue
        if upsert_json_document(collection, doc_id, row):
            n += 1
    return n


def insert_conversion(rec: Dict[str, Any]) -> bool:
    if not configured():
        return False
    try:
        from psycopg.types.json import Json

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversion_events (
                      event_id, tenant_id, brand, campaign_id, segment_key,
                      converted, revenue, channel, observed_at, payload
                    ) VALUES (
                      %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s
                    )
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        rec.get("event_id"),
                        rec.get("tenant_id"),
                        rec.get("brand"),
                        rec.get("campaign_id"),
                        rec.get("segment_key"),
                        bool(rec.get("converted", True)),
                        float(rec.get("revenue") or 0),
                        rec.get("channel"),
                        rec.get("observed_at"),
                        Json(rec),
                    ),
                )
        return True
    except Exception as exc:
        log.warning("conversion insert failed: %s", exc)
        return False


def register_customer_garment_image(
    *,
    image_id: str,
    gcs_object_key: str,
    user_uuid: str,
    pin_code: str,
    order_id: str,
    item_id: str,
    policy_version: str = "v1",
) -> Dict[str, str]:
    """Create consent, garment_instances and garment_images rows for an uploaded photo.

    Idempotent on gcs_object_key, so a retried completion returns the same IDs.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (gcs_object_key,))
            cur.execute(
                """
                SELECT id::text AS image_id, garment_instance_id::text AS garment_instance_id
                FROM garment_images WHERE gcs_object_key = %s AND deleted_at IS NULL LIMIT 1
                """,
                (gcs_object_key,),
            )
            existing = cur.fetchone()
            if existing:
                return dict(existing)
            cur.execute(
                """
                SELECT id::text AS id FROM consent_records
                WHERE source_customer_id = %s AND purpose = 'operational_processing'
                  AND status = 'granted' AND revoked_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_uuid,),
            )
            row = cur.fetchone()
            if row:
                consent_id = row["id"]
            else:
                cur.execute(
                    """
                    INSERT INTO consent_records (source_customer_id, purpose, policy_version, status, granted_at)
                    VALUES (%s, 'operational_processing', %s, 'granted', now())
                    RETURNING id::text AS id
                    """,
                    (user_uuid, policy_version),
                )
                consent_id = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO garment_instances (
                  source_customer_id, source_user_id, source_order_id, source_order_item_id,
                  source_order_commodity_line_id, physical_item_index, order_pin_code,
                  observed_at, consent_id, review_status
                ) VALUES (%s, %s, %s, %s, %s, 1, %s, now(), %s::uuid, 'needs_review')
                ON CONFLICT (source_order_id, source_order_commodity_line_id, physical_item_index)
                DO UPDATE SET order_pin_code = EXCLUDED.order_pin_code, updated_at = now()
                RETURNING id::text AS id
                """,
                (user_uuid, user_uuid, order_id, item_id, item_id, pin_code, consent_id),
            )
            garment_instance_id = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO garment_images (
                  id, garment_instance_id, consent_id, gcs_object_key, capture_source, image_type
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'customer_upload', 'garment')
                """,
                (image_id, garment_instance_id, consent_id, gcs_object_key),
            )
    return {"image_id": image_id, "garment_instance_id": garment_instance_id}


def upsert_inventory(rec: Dict[str, Any]) -> bool:
    if not configured():
        return False
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO inventory_levels (
                      inventory_id, brand_id, sku, warehouse, on_hand, as_of, updated_at
                    ) VALUES (
                      COALESCE(%s::uuid, gen_random_uuid()), %s, %s, %s, %s, %s::timestamptz, now()
                    )
                    ON CONFLICT (brand_id, sku, warehouse)
                    DO UPDATE SET on_hand = EXCLUDED.on_hand, as_of = EXCLUDED.as_of, updated_at = now()
                    """,
                    (
                        rec.get("inventory_id"),
                        rec.get("brand_id"),
                        rec.get("sku"),
                        rec.get("warehouse") or "default",
                        int(rec.get("on_hand") or 0),
                        rec.get("as_of") or rec.get("updated_at"),
                    ),
                )
        return True
    except Exception as exc:
        log.warning("inventory upsert failed: %s", exc)
        return False


def insert_media_activation(envelope: Dict[str, Any], delivery: Dict[str, Any]) -> bool:
    if not configured():
        return False
    try:
        from psycopg.types.json import Json

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO media_activation_log (
                      request_id, tenant_id, brand, campaign_id, channel,
                      segment_keys, payload, delivery
                    ) VALUES (
                      %s::uuid, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (request_id) DO NOTHING
                    """,
                    (
                        envelope.get("request_id"),
                        envelope.get("tenant_id"),
                        envelope.get("brand"),
                        envelope.get("campaign_id"),
                        envelope.get("channel"),
                        Json(envelope.get("segment_keys") or []),
                        Json(envelope.get("payload") or {}),
                        Json(delivery),
                    ),
                )
        return True
    except Exception as exc:
        log.warning("media activation insert failed: %s", exc)
        return False


def mirror_privacy_query(entry: Dict[str, Any], risk: Dict[str, Any]) -> bool:
    if not configured():
        return False
    try:
        from psycopg.types.json import Json

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO privacy_query_log (
                      fingerprint, tenant_id, filters, cohort_size, cohort_hash,
                      customer_ids, differencing_risk, risk_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        entry.get("fingerprint"),
                        entry.get("tenant_id"),
                        Json(entry.get("filters") or {}),
                        entry.get("cohort_size"),
                        entry.get("cohort_hash"),
                        Json(entry.get("customer_ids") or []),
                        bool(risk.get("differencing_risk")),
                        risk.get("reason"),
                    ),
                )
        return True
    except Exception as exc:
        log.debug("privacy log mirror failed: %s", exc)
        return False


def health() -> Dict[str, Any]:
    if not configured():
        return {"configured": False, "reachable": False}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
        return {"configured": True, "reachable": True, "instance": settings.CLOUD_SQL_INSTANCE or None}
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc)}
