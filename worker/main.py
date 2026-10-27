"""
Garment / campaign image extraction worker (Cloud Run + Pub/Sub push).

Pipeline:
  1) Read image from GCS
  2) Extract attributes with Vertex AI Gemini (ENABLE_VERTEX=true)
  3) Persist:
     - customer photos {customer_id}_{order_id}_{item_id}.jpg -> BigQuery suffai_ops.commodities
       (order must exist; one row per item)
     - demo/ uploads and registered images -> Cloud SQL (extraction_jobs + garment_instances / campaigns.metadata)

State machine:
  PENDING_UPLOAD → UPLOADED → QUEUED → PROCESSING → NEEDS_REVIEW | COMPLETED | FAILED
"""

from __future__ import annotations

import base64
import json
import logging
import os
import traceback
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from worker import bq_items, customer_photos, db, events
from worker.gcs_io import read_image
from worker.photo_key import needs_extraction, parse_photo_key
from worker.settings import settings
from worker.vertex_extract import extract_attributes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("suffai-extraction")

app = FastAPI(title="Suffai Extraction Worker", version="1.1.0")


class ExtractRequest(BaseModel):
    """Direct invoke (Cloud Run / curl) — same fields as Pub/Sub payload."""

    gcs_object_key: str
    gcs_bucket: Optional[str] = None
    image_id: Optional[str] = None
    garment_instance_id: Optional[str] = None
    campaign_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    target_type: str = Field(default="garment", description="garment | campaign")


import base64
import json

def parse_key_object_metadata(envelope):
    """Normalize Pub/Sub GCS notifications."""

    message = envelope.get("message", {})
    attributes = message.get("attributes", {})

    metadata = {}

    if message.get("data"):
        metadata = json.loads(
            base64.b64decode(message["data"]).decode("utf-8")
        )

    object_key = (
        attributes.get("objectId")
        or metadata.get("name")
        or metadata.get("gcs_object_key")
    )

    bucket = (
        attributes.get("bucketId")
        or metadata.get("bucket")
        or metadata.get("gcs_bucket")
    )

    if not object_key:
        raise ValueError("gcs_object_key is required")

    return {
        **metadata,
        "gcs_object_key": object_key,
        "gcs_bucket": bucket,
        "event_type": attributes.get("eventType"),
        "object_generation": attributes.get("objectGeneration"),
    }
  
def _abstain_result(reason: str) -> Dict[str, Any]:
    return {
        "commodity_type": "unknown",
        "material_type": "unknown",
        "primary_color": "unknown",
        "size_normalized": None,
        "brand_name": "unknown",
        "brand_sku": None,
        "pattern": "unknown",
        "attribute_evidence": {
            "note": {"source": "worker_stub", "confidence": 0.0, "reason": reason}
        },
        "review_status": "needs_review",
        "model_version": settings.VERTEX_MODEL if settings.ENABLE_VERTEX else "stub-v1",
    }


def run_extraction(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Core pipeline used by Pub/Sub and POST /v1/extract."""
    object_key = payload.get("gcs_object_key") or payload.get("object_key")
    bucket = payload.get("gcs_bucket") or payload.get("bucket")
    image_id = payload.get("image_id")
    garment_instance_id = payload.get("garment_instance_id")
    campaign_id = payload.get("campaign_id")
    target_type = (payload.get("target_type") or "garment").lower()
    idempotency_key = (
        payload.get("idempotency_key")
        or payload.get("message_id")
        or f"extract:{object_key}"
    )
    demo_prefix = "demo/"

    photo = (
        parse_photo_key(object_key)
        if target_type == "garment" and not image_id and not garment_instance_id
        else None
    )
    if photo is not None:
        out = customer_photos.process(
            photo,
            object_key=object_key,
            bucket=bucket,
            read_image=read_image,
            extract=extract_attributes if settings.ENABLE_VERTEX else None,
            abstain=_abstain_result,
        )
        out["published_to"] = None
        if events.should_publish_photo(out):
            events.publish_extracted(events.photo_event(out, bucket=bucket, idempotency_key=idempotency_key))
            out["published_to"] = settings.PUBSUB_EXTRACTED_TOPIC
        return out

    if (
        target_type == "garment"
        and settings.DATABASE_URL
        and object_key
        and object_key.startswith(demo_prefix)
        and not image_id
        and not garment_instance_id):
        registration = db.register_gcs_image(
            gcs_object_key=object_key,
        )

        image_id = registration["image_id"]
        garment_instance_id = registration["garment_instance_id"]

        log.info(
            "Demo image registration completed image_id=%s garment_id=%s",
            image_id,
            garment_instance_id)
    if not object_key:
        raise ValueError("gcs_object_key is required")

    # 1) Mark PROCESSING in Cloud SQL (best-effort)
    if settings.DATABASE_URL:
        try:
            db.mark_job_processing(
                idempotency_key=idempotency_key,
                image_id=image_id,
                model_version=settings.VERTEX_MODEL,
            )
        except db.DatabaseNotConfigured:
            pass
        except Exception as exc:
            log.warning("mark_job_processing failed: %s", exc)

    # 2) Read image from GCS
    image = read_image(object_key, bucket=bucket)

    # 3) Vertex Gemini (or abstain stub)
    if settings.ENABLE_VERTEX:
        result = extract_attributes(image)
        status = result.get("review_status") or "NEEDS_REVIEW"
        if status not in {"NEEDS_REVIEW", "COMPLETED", "FAILED"}:
            status = "NEEDS_REVIEW"
    else:
        result = _abstain_result("ENABLE_VERTEX=false; abstain without paid API call")
        status = "NEEDS_REVIEW"

    persist_info: Dict[str, Any] = {"skipped": True}
    # 4) Write to Cloud SQL
    if not settings.DATABASE_URL:
        persist_info = {
            "skipped": True,
            "reason": "DATABASE_URL not configured on worker",
        }
    else:
        try:
            if target_type == "campaign" and campaign_id:
                ok = db.persist_campaign_extraction(
                    campaign_id=campaign_id,
                    result=result,
                    status=status,
                )
                persist_info = {"campaign_updated": ok, "campaign_id": campaign_id}
            else:
                persist_info = db.persist_garment_extraction(
                    idempotency_key=idempotency_key,
                    image_id=image_id,
                    garment_instance_id=garment_instance_id,
                    result=result,
                    status=status,
                )
        except Exception as exc:
            log.exception("Cloud SQL persist failed")
            # Best-effort mark FAILED
            try:
                db.persist_garment_extraction(
                    idempotency_key=idempotency_key,
                    image_id=image_id,
                    garment_instance_id=None,
                    result={**result, "notes": f"persist_failed: {exc}"},
                    status="FAILED",
                    error_code="DB_PERSIST_FAILED",
                )
            except Exception:
                pass
            raise

    # 5) Hand off to similarity only after the job and garment are saved
    published = None
    if events.should_publish(target_type=target_type, object_key=object_key, persist_info=persist_info):
        events.publish_extracted(events.extracted_event(
            payload, result, status=status, bucket=image.bucket, object_key=image.object_key,
            idempotency_key=idempotency_key, image_id=image_id,
            garment_instance_id=garment_instance_id,
        ))
        published = settings.PUBSUB_EXTRACTED_TOPIC

    # Do not return raw image bytes
    safe_result = {k: v for k, v in result.items() if k != "raw_model_json"}
    return {
        "status": status,
        "idempotency_key": idempotency_key,
        "gcs_uri": image.gs_uri,
        "content_type": image.content_type,
        "size_bytes": image.size_bytes,
        "target_type": target_type,
        "image_id": image_id,
        "garment_instance_id": garment_instance_id,
        "campaign_id": campaign_id,
        "persist": persist_info,
        "published_to": published,
        "result": safe_result,
        "item_attributes": bq_items.item_attributes(result),
        "vertex_enabled": settings.ENABLE_VERTEX,
    }


@app.get("/health")
def health():
    db_ok = False
    if settings.DATABASE_URL:
        db_ok = db.database_ready()
    return {
        "status": "ok",
        "service": "suffai-extraction",
        "version": "1.1.0",
        "vertex_enabled": settings.ENABLE_VERTEX,
        "vertex_model": settings.VERTEX_MODEL,
        "vertex_location": settings.VERTEX_LOCATION,
        "gcs_image_bucket": settings.GCS_IMAGE_BUCKET or None,
        "database_configured": bool(settings.DATABASE_URL),
        "database_ready": db_ok,
        "project": settings.PROJECT_ID or None,
    }


@app.post("/v1/extract")
def extract_http(body: ExtractRequest):
    """
    Manual / integration test endpoint (does not require Pub/Sub).
    Requires worker env: GCS_IMAGE_BUCKET, ENABLE_VERTEX, DATABASE_URL, GOOGLE_CLOUD_PROJECT.
    """
    try:
        return run_extraction(body.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("extract_http failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/")
async def pubsub_push(request: Request):
    """Pub/Sub push endpoint — idempotent extraction job."""
    envelope = await request.json()
    message = envelope.get("message") or {}
    data_b64 = message.get("data")
    message_id = message.get("messageId") or message.get("message_id") or "unknown"

    if not data_b64:
        log.warning("empty pubsub payload message_id=%s", message_id)
        return JSONResponse({"status": "ignored"}, status_code=200)

    try:
        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as exc:
        log.exception("bad payload: %s", exc)
        return JSONResponse({"status": "bad_payload"}, status_code=200)

    if isinstance(payload, dict) and "idempotency_key" not in payload:
        payload["idempotency_key"] = payload.get("idempotency_key") or message_id
        payload["message_id"] = message_id

    try:
        normalized_payload = parse_key_object_metadata(envelope)
        normalized_payload["idempotency_key"] = message_id
        normalized_payload["message_id"] = message_id
        if not needs_extraction(normalized_payload):
            log.info("pubsub ignored (not a customer photo) message_id=%s", message_id)
            return JSONResponse({"status": "ignored", "message_id": message_id}, status_code=200)
        result = run_extraction(normalized_payload)
        # result = run_extraction(payload if isinstance(payload, dict) else {})
        return result
    except Exception as exc:
        # Ack with 200 only for poison non-retryable? Prefer 500 so Pub/Sub retries / DLQ.
        log.error("extraction failed message_id=%s err=%s\n%s", message_id, exc, traceback.format_exc())
        # Non-retryable client errors → 200 to avoid infinite retry
        if isinstance(exc, (ValueError, FileNotFoundError)):
            if settings.DATABASE_URL:
                try:
                    db.persist_garment_extraction(
                        idempotency_key=str(
                            (payload or {}).get("idempotency_key") or message_id
                        ),
                        image_id=(payload or {}).get("image_id"),
                        garment_instance_id=None,
                        result=_abstain_result(str(exc)),
                        status="FAILED",
                        error_code="CLIENT_ERROR",
                    )
                except Exception:
                    pass
            return JSONResponse(
                {"status": "FAILED", "error": str(exc), "message_id": message_id},
                status_code=200,
            )
        return JSONResponse(
            {"status": "FAILED", "error": str(exc), "message_id": message_id},
            status_code=500,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", settings.PORT)))
