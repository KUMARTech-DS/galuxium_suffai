"""Publish garment-image-extracted once extraction (and the warehouse catalog match) is saved."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from worker.photo_key import SKIP_PREFIXES
from worker.settings import settings

log = logging.getLogger("suffai-extraction.events")

Publisher = Callable[[str, bytes, Dict[str, str]], Any]


def should_publish(*, target_type: str, object_key: str, persist_info: Dict[str, Any]) -> bool:
    if not settings.PUBSUB_EXTRACTED_TOPIC:
        return False
    if (target_type or "garment").lower() != "garment":
        return False
    if (object_key or "").lstrip("/").startswith(SKIP_PREFIXES):
        return False
    return not persist_info.get("skipped")


def should_publish_photo(out: Dict[str, Any]) -> bool:
    """Customer photo: only once its commodities row is written (not ignored or unlinked)."""
    if not settings.PUBSUB_EXTRACTED_TOPIC:
        return False
    return (out.get("persist") or {}).get("bigquery") == "commodities"


def _known(value: Any) -> Optional[str]:
    s = str(value or "").strip()
    return s if s and s.lower() not in ("unknown", "none", "null") else None


def extracted_event(payload: Dict[str, Any], result: Dict[str, Any], *, status: str,
                    bucket: str, object_key: str, idempotency_key: str,
                    image_id: Optional[str], garment_instance_id: Optional[str]) -> Dict[str, Any]:
    return {
        "gcs_bucket": bucket,
        "gcs_object_key": object_key,
        "image_id": image_id,
        "garment_instance_id": garment_instance_id,
        "user_uuid": payload.get("user_uuid"),
        "pin_code": payload.get("pin_code") or payload.get("order_pin_code"),
        "brand_name": _known(result.get("brand_name")),
        "brand_sku": None,
        "review_status": status,
        "idempotency_key": f"sim:{idempotency_key}",
    }


def photo_event(out: Dict[str, Any], *, bucket: Optional[str], idempotency_key: str) -> Dict[str, Any]:
    """brand_sku is the matched warehouse style × colour, so every size of one garment dedups together."""
    persist = out.get("persist") or {}
    return {
        "gcs_bucket": bucket,
        "gcs_object_key": out.get("gcs_object_key"),
        "user_uuid": out.get("customer_id"),
        "brand_name": _known(persist.get("brand")),
        "brand_sku": persist.get("garment_sku"),
        "review_status": out.get("status"),
        "idempotency_key": f"sim:{idempotency_key}",
    }


def _pubsub_publish(topic: str, data: bytes, attrs: Dict[str, str]) -> Any:
    from google.cloud import pubsub_v1

    publisher = pubsub_v1.PublisherClient()
    path = publisher.topic_path(settings.PROJECT_ID, topic)
    return publisher.publish(path, data, **attrs).result(timeout=15)


def publish_extracted(event: Dict[str, Any], *, publisher: Optional[Publisher] = None) -> Any:
    """Raises RuntimeError on failure so Pub/Sub redelivers the upload event."""
    if not settings.PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required to publish garment-image-extracted")
    attrs = {k: str(event[k]) for k in ("image_id", "user_uuid") if event.get(k)}
    try:
        return (publisher or _pubsub_publish)(
            settings.PUBSUB_EXTRACTED_TOPIC, json.dumps(event).encode("utf-8"), attrs
        )
    except Exception as exc:
        log.error("publish garment-image-extracted failed key=%s: %s", event.get("gcs_object_key"), exc)
        raise RuntimeError("garment-image-extracted publish failed") from exc
