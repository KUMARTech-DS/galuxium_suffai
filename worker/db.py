"""Persist extraction results to Cloud SQL PostgreSQL."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from worker.settings import settings
DEMO_CONSENT_ID = "c84dc911-d658-4cb6-b970-1ca2029daf88"
DEMO_CUSTOMER_ID = "SUFFAI_DEMO_CUSTOMER"

log = logging.getLogger("suffai-extraction.db")


class DatabaseNotConfigured(RuntimeError):
    pass


def _require_database_url() -> str:
    if not settings.DATABASE_URL:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set on suffai-extraction. "
            "Example: postgresql://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE"
        )
    return settings.DATABASE_URL


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(_require_database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)

def register_gcs_image(
    *,
    gcs_object_key: str,
    consent_id: str = DEMO_CONSENT_ID,
) -> Dict[str, str]:
    """
    Register an authorized demo GCS image in Cloud SQL.

    Returns:
        {
            "image_id": "...",
            "garment_instance_id": "..."
        }
    """

    if not gcs_object_key:
        raise ValueError("gcs_object_key is required")

    # This function is restricted to explicitly authorized demo uploads.
    # Do not call it for arbitrary customer images.

    with connect() as conn:
        with conn.cursor() as cur:

            # Serialize registration for the same GCS object.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (gcs_object_key,),
            )

            # 1. Check whether the image already exists.
            cur.execute(
                """
                SELECT
                    id::text AS image_id,
                    garment_instance_id::text AS garment_instance_id
                FROM garment_images
                WHERE gcs_object_key = %s
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (gcs_object_key,),
            )

            existing = cur.fetchone()

            if existing:
                log.info(
                    "GCS image already registered key=%s image_id=%s",
                    gcs_object_key,
                    existing["image_id"],
                )
                return existing

            # 2. Validate demo consent.
            cur.execute(
                """
                SELECT source_customer_id
                FROM consent_records
                WHERE id = %s::uuid
                  AND source_customer_id = %s
                  AND purpose = 'operational_processing'
                  AND status = 'granted'
                  AND revoked_at IS NULL
                """,
                (consent_id, DEMO_CUSTOMER_ID),
            )

            consent = cur.fetchone()

            if not consent:
                raise ValueError(
                    "Valid authorized demo consent not found"
                )

            # 3. Create a synthetic garment instance.
            garment_instance_id = str(uuid.uuid4())
            source_id = str(uuid.uuid4())

            cur.execute(
                """
                INSERT INTO garment_instances (
                    id,
                    source_customer_id,
                    source_order_id,
                    source_order_item_id,
                    source_order_commodity_line_id,
                    physical_item_index,
                    consent_id,
                    review_status
                )
                VALUES (
                    %s::uuid,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::uuid,
                    'needs_review'
                )
                """,
                (
                    garment_instance_id,
                    DEMO_CUSTOMER_ID,
                    f"DEMO_ORDER_{source_id}",
                    f"DEMO_ITEM_{source_id}",
                    f"DEMO_LINE_{source_id}",
                    1,
                    consent_id,
                ),
            )

            # 4. Register the image.
            image_id = str(uuid.uuid4())

            cur.execute(
                """
                INSERT INTO garment_images (
                    id,
                    garment_instance_id,
                    consent_id,
                    gcs_object_key,
                    capture_source,
                    image_type
                )
                VALUES (
                    %s::uuid,
                    %s::uuid,
                    %s::uuid,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    image_id,
                    garment_instance_id,
                    consent_id,
                    gcs_object_key,
                    "suffai_demo",
                    "garment",
                ),
            )

            log.info(
                "GCS image registered key=%s image_id=%s garment_id=%s",
                gcs_object_key,
                image_id,
                garment_instance_id,
            )

            return {
                "image_id": image_id,
                "garment_instance_id": garment_instance_id,
            }


def mark_job_processing(
    *,
    idempotency_key: str,
    image_id: Optional[str],
    model_version: str,
) -> Optional[str]:
    """Set job to PROCESSING; create row if image_id exists and job missing. Returns job id."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_jobs
                SET status = 'PROCESSING',
                    attempt_count = attempt_count + 1,
                    model_version = %s,
                    started_at = COALESCE(started_at, %s),
                    error_code = NULL
                WHERE idempotency_key = %s
                RETURNING id::text
                """,
                (model_version, _now(), idempotency_key),
            )
            row = cur.fetchone()
            if row:
                return row["id"]

            if not image_id:
                return None

            job_id = str(uuid.uuid4())
            try:
                cur.execute(
                    """
                    INSERT INTO extraction_jobs (
                      id, image_id, status, attempt_count, idempotency_key,
                      model_version, started_at
                    ) VALUES (%s, %s::uuid, 'PROCESSING', 1, %s, %s, %s)
                    RETURNING id::text
                    """,
                    (job_id, image_id, idempotency_key, model_version, _now()),
                )
                created = cur.fetchone()
                return created["id"] if created else job_id
            except psycopg.Error as exc:
                # FK missing — job table not writable yet; continue without job row
                log.warning("extraction_jobs insert skipped: %s", exc)
                return None


def persist_garment_extraction(
    *,
    idempotency_key: str,
    image_id: Optional[str],
    garment_instance_id: Optional[str],
    result: Dict[str, Any],
    status: str,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Write raw_result to extraction_jobs and attributes to garment_instances."""
    model_version = result.get("model_version")
    raw_payload = {
        "normalized": {k: v for k, v in result.items() if k != "raw_model_json"},
        "raw_model_json": result.get("raw_model_json"),
    }

    out: Dict[str, Any] = {"job_id": None, "garment_updated": False}

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extraction_jobs
                SET status = %s,
                    model_version = %s,
                    raw_result = %s,
                    error_code = %s,
                    completed_at = %s
                WHERE idempotency_key = %s
                RETURNING id::text
                """,
                (
                    status,
                    model_version,
                    Json(raw_payload),
                    error_code,
                    _now(),
                    idempotency_key,
                ),
            )
            job = cur.fetchone()
            if job:
                out["job_id"] = job["id"]
            elif image_id:
                job_id = str(uuid.uuid4())
                try:
                    cur.execute(
                        """
                        INSERT INTO extraction_jobs (
                          id, image_id, status, attempt_count, idempotency_key,
                          model_version, raw_result, error_code, started_at, completed_at
                        ) VALUES (
                          %s, %s::uuid, %s, 1, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING id::text
                        """,
                        (
                            job_id,
                            image_id,
                            status,
                            idempotency_key,
                            model_version,
                            Json(raw_payload),
                            error_code,
                            _now(),
                            _now(),
                        ),
                    )
                    created = cur.fetchone()
                    out["job_id"] = created["id"] if created else job_id
                except psycopg.Error as exc:
                    log.warning("extraction_jobs final write skipped: %s", exc)

            if garment_instance_id and status in {"NEEDS_REVIEW", "COMPLETED"}:
                secondary = result.get("secondary_colors") or []
                if not isinstance(secondary, list):
                    secondary = []
                style_tags = result.get("style_tags") or []
                if not isinstance(style_tags, list):
                    style_tags = []
                # garment_instances.review_status uses lowercase (needs_review / completed)
                garment_review = (result.get("review_status") or status).lower()

                cur.execute(
                    """
                    UPDATE garment_instances
                    SET commodity_type = %s,
                        material_type = %s,
                        material_blend = %s,
                        primary_color = %s,
                        secondary_colors = %s,
                        size_raw = %s,
                        size_normalized = %s,
                        size_system = %s,
                        brand_name_raw = %s,
                        brand_sku = %s,
                        pattern = %s,
                        style_tags = %s,
                        garment_category = %s,
                        garment_subcategory = %s,
                        sleeve_length = %s,
                        pairs_with = %s,
                        attribute_evidence = %s,
                        review_status = %s,
                        model_version = %s,
                        updated_at = %s
                    WHERE id = %s::uuid
                      AND deleted_at IS NULL
                    RETURNING id::text
                    """,
                    (
                        result.get("commodity_type"),
                        result.get("material_type"),
                        Json(result.get("material_blend"))
                        if result.get("material_blend") is not None
                        else None,
                        result.get("primary_color"),
                        secondary,
                        result.get("size_raw"),
                        result.get("size_normalized"),
                        result.get("size_system"),
                        result.get("brand_name"),
                        result.get("brand_sku"),
                        result.get("pattern"),
                        style_tags,
                        result.get("garment_category"),
                        result.get("garment_subcategory"),
                        result.get("sleeve_length"),
                        list(result.get("pairs_with") or []),
                        Json(result.get("attribute_evidence") or {}),
                        garment_review,
                        model_version,
                        _now(),
                        garment_instance_id,
                    ),
                )
                updated = cur.fetchone()
                out["garment_updated"] = bool(updated)
                if not updated:
                    log.warning(
                        "garment_instances id=%s not updated (missing or deleted)",
                        garment_instance_id,
                    )

    return out


def persist_campaign_extraction(
    *,
    campaign_id: str,
    result: Dict[str, Any],
    status: str,
) -> bool:
    """Merge extracted attributes into campaigns.metadata JSONB."""
    patch = {
        "extracted_attributes": {k: v for k, v in result.items() if k != "raw_model_json"},
        "extraction_status": status,
        "extracted_at": _now().isoformat(),
        "model_version": result.get("model_version"),
    }
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaigns
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING id::text
                """,
                (json.dumps(patch), campaign_id),
            )
            row = cur.fetchone()
            return bool(row)


def database_ready() -> bool:
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                return True
    except Exception as exc:
        log.warning("database_ready=false: %s", exc)
        return False
