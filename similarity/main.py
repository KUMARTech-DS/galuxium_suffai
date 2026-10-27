"""
Garment similarity Cloud Run service (Pub/Sub push + compare-pins for Ads Lab ranking).

Independent of suffai-extraction / command_center core logic. Garment counts per PIN are served by
Command Center from BigQuery garment_items (Admin Unique garments), not here.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import traceback
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from similarity import db
from similarity.compare import compare_pins
from similarity.process import process_upload, resend_pending
from similarity.settings import settings

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s [similarity] %(message)s",
)
log = logging.getLogger("similarity")

app = FastAPI(title="Suffai Garment Similarity", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessRequest(BaseModel):
    """Customer, order, item and PIN come from the photo name and BigQuery, never from the request."""

    gcs_object_key: str
    gcs_bucket: Optional[str] = None
    brand: Optional[str] = None
    brand_name: Optional[str] = None
    brand_sku: Optional[str] = None
    idempotency_key: Optional[str] = None


def parse_key_object_metadata(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Pub/Sub GCS notifications — same shape as worker/main.py."""
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}
    metadata: Dict[str, Any] = {}
    if message.get("data"):
        try:
            metadata = json.loads(base64.b64decode(message["data"]).decode("utf-8"))
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            log.exception("failed to decode pubsub data")
            metadata = {}

    object_key = (
        attributes.get("objectId")
        or metadata.get("name")
        or metadata.get("gcs_object_key")
        or metadata.get("object_key")
    )
    bucket = (
        attributes.get("bucketId")
        or metadata.get("bucket")
        or metadata.get("gcs_bucket")
    )
    if not object_key:
        raise ValueError("gcs_object_key is required")

    merged = {
        **metadata,
        "attributes": attributes,
        "gcs_object_key": object_key,
        "gcs_bucket": bucket,
        "event_type": attributes.get("eventType"),
        "object_generation": attributes.get("objectGeneration"),
    }
    for k in ("brand", "brand_name"):
        if attributes.get(k) and not merged.get(k):
            merged[k] = attributes[k]
    return merged


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "suffai-similarity",
        "enable_vertex": settings.ENABLE_VERTEX,
        "use_bq": settings.USE_BQ,
        "bq_dataset": settings.BQ_DATASET,
        "project": bool(settings.PROJECT_ID),
        "gcs_bucket": bool(settings.GCS_IMAGE_BUCKET),
        "database_url": bool(settings.DATABASE_URL),
        "similarity_threshold": settings.SIMILARITY_THRESHOLD,
        "vertex_location": settings.VERTEX_LOCATION,
        "vertex_embed_model": settings.VERTEX_EMBED_MODEL,
    }


@app.post("/v1/similarity/process")
def process_http(body: ProcessRequest):
    payload = body.model_dump()
    if body.brand and not body.brand_name:
        payload["brand_name"] = body.brand
    try:
        return process_upload(payload)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        log.exception("process_http failed")
        raise HTTPException(500, str(exc)) from exc


class ComparePinsRequest(BaseModel):
    gcs_object_key: str
    gcs_bucket: Optional[str] = None
    pin_codes: list[str]
    # PIN -> [[garment_id, photos], ...] from Command Center (BigQuery garment_items per current PIN).
    candidates: Optional[Dict[str, list]] = None
    pin_totals: Optional[Dict[str, int]] = None
    topk: int = 5
    include_embedding: bool = True


@app.post("/v1/similarity/compare-pins")
def compare_pins_http(body: ComparePinsRequest):
    """Read-only: score a brand SKU image against the PIN garments Command Center sends."""
    try:
        return compare_pins(
            gcs_object_key=body.gcs_object_key,
            gcs_bucket=body.gcs_bucket,
            pin_codes=body.pin_codes,
            candidates=body.candidates,
            pin_totals=body.pin_totals,
            topk=body.topk,
            include_embedding=body.include_embedding,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        log.exception("compare_pins failed")
        raise HTTPException(500, str(exc)) from exc


@app.post("/")
async def pubsub_push(request: Request):
    """Pub/Sub push endpoint — same ack/retry semantics as suffai-extraction.

    brands/, demo/ and other non-photo keys and unlinked photos are acknowledged (200) before any download;
    a failed BigQuery order check or observation insert returns 500 so Pub/Sub retries. After each
    message, observations whose retries ran out are resent (process.resend_pending).
    """
    envelope = await request.json()
    message = envelope.get("message") or {}
    data_b64 = message.get("data")
    message_id = message.get("messageId") or message.get("message_id") or "unknown"
    payload: Dict[str, Any] = {}

    if not data_b64 and not (message.get("attributes") or {}).get("objectId"):
        log.warning("empty pubsub payload message_id=%s", message_id)
        return JSONResponse({"status": "ignored"}, status_code=200)

    try:
        payload = parse_key_object_metadata(envelope)
        payload["idempotency_key"] = payload.get("idempotency_key") or message_id
        payload["message_id"] = message_id
        result = process_upload(payload)
        resend_pending()
        return result
    except (ValueError, FileNotFoundError) as exc:
        # Non-retryable → 200 (avoid infinite Pub/Sub retry), same as extraction
        log.error("client_error message_id=%s err=%s", message_id, exc)
        try:
            if settings.DATABASE_URL:
                db.record_event(
                    gcs_bucket=payload.get("gcs_bucket"),
                    gcs_object_key=str(payload.get("gcs_object_key") or "unknown"),
                    user_uuid=payload.get("user_uuid"),
                    pin_code=payload.get("pin_code"),
                    brand_name=payload.get("brand_name") or payload.get("brand"),
                    outcome="error",
                    error_message=str(exc),
                    idempotency_key=str(message_id),
                )
        except Exception:
            log.exception("failed to record error event")
        return JSONResponse(
            {"status": "FAILED", "error": str(exc), "message_id": message_id},
            status_code=200,
        )
    except Exception as exc:
        # Retryable → 500 so Pub/Sub retries / DLQ
        log.error(
            "similarity failed message_id=%s err=%s\n%s",
            message_id,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            {"status": "FAILED", "error": str(exc), "message_id": message_id},
            status_code=500,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", settings.PORT)))
