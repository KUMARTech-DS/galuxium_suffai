"""Optional GCS signed URLs + Pub/Sub publish. Safe no-ops when not configured."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Optional

from config import settings

log = logging.getLogger("suffai.gcp_io")


def gcs_configured() -> bool:
    return bool(settings.GCS_IMAGE_BUCKET or settings.GCS_BUCKET)


def signed_upload_url(
    object_key: str,
    *,
    content_type: str = "image/jpeg",
    minutes: int = 15,
) -> dict[str, Any]:
    """
    Return a V4 signed PUT URL. If GCS is not configured, return a local API upload path
    so deployed dashboards can still complete the flow in demo mode.
    """
    bucket = settings.GCS_IMAGE_BUCKET or settings.GCS_BUCKET
    if not bucket:
        return {
            "mode": "local",
            "upload_url": f"{settings.API_PUBLIC_URL.rstrip('/')}/v1/garments/images/upload-local",
            "method": "POST",
            "gcs_bucket": None,
            "gcs_object_key": object_key,
            "headers": {},
            "expires_minutes": minutes,
        }

    try:
        from google.cloud import storage

        client = storage.Client(project=settings.GCP_PROJECT or None)
        blob = client.bucket(bucket).blob(object_key)
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=minutes),
            method="PUT",
            content_type=content_type,
        )
        return {
            "mode": "gcs",
            "upload_url": url,
            "method": "PUT",
            "gcs_bucket": bucket,
            "gcs_object_key": object_key,
            "headers": {"Content-Type": content_type},
            "expires_minutes": minutes,
        }
    except Exception as exc:
        log.warning("signed URL failed (%s); falling back to local upload", exc)
        return {
            "mode": "local",
            "upload_url": f"{settings.API_PUBLIC_URL.rstrip('/')}/v1/garments/images/upload-local",
            "method": "POST",
            "gcs_bucket": bucket,
            "gcs_object_key": object_key,
            "headers": {},
            "expires_minutes": minutes,
            "warning": str(exc),
        }


def publish_extraction_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Publish to Pub/Sub when configured; otherwise return deferred status."""
    topic = settings.PUBSUB_IMAGE_TOPIC
    project = settings.GCP_PROJECT or settings.BQ_PROJECT
    if not topic or not project:
        return {"published": False, "reason": "PUBSUB_IMAGE_TOPIC or project not set"}

    try:
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient()
        path = publisher.topic_path(project, topic)
        future = publisher.publish(path, json.dumps(payload).encode("utf-8"))
        msg_id = future.result(timeout=30)
        return {"published": True, "message_id": msg_id, "topic": topic}
    except Exception as exc:
        log.warning("pubsub publish failed: %s", exc)
        return {"published": False, "reason": str(exc)}


def trigger_worker_http(payload: dict[str, Any]) -> dict[str, Any]:
    """Optional direct call to suffai-extraction /v1/extract."""
    import os

    import httpx

    base = os.getenv("EXTRACTION_WORKER_URL", "").rstrip("/")
    if not base:
        return {"triggered": False, "reason": "EXTRACTION_WORKER_URL not set"}
    try:
        resp = httpx.post(f"{base}/v1/extract", json=payload, timeout=120.0)
        return {
            "triggered": True,
            "status_code": resp.status_code,
            "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        }
    except Exception as exc:
        return {"triggered": False, "reason": str(exc)}
