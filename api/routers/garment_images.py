from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from google.cloud import storage, pubsub_v1

from api.deps import require_user
from config import settings
from services import ops_db
from suffai.api.envelope import ok

router = APIRouter(
    prefix="/v1/garments/images",
    tags=["garment-images"],
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


_ID_RE = r"^[A-Za-z0-9_-]{1,100}$"


class CustomerContext(BaseModel):
    """Who owns the garment and where it was seen; similarity needs user and PIN."""

    user_uuid: str = Field(pattern=_ID_RE)
    pin_code: str = Field(pattern=r"^\d{6}$")
    order_id: Optional[str] = Field(default=None, pattern=_ID_RE)
    item_id: Optional[str] = Field(default=None, pattern=_ID_RE)


class InitiateRequest(CustomerContext):
    content_type: str
    filename: str = Field(min_length=1, max_length=180)
    consent_confirmed: bool


class CompleteRequest(CustomerContext):
    image_id: str
    object_key: str


def _identity(user: dict) -> tuple[str, str]:
    brand_id = str(user.get("brand_id", ""))
    tenant_id = str(user.get("tenant_id", ""))

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", brand_id):
        raise HTTPException(403, "Invalid brand identity")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", tenant_id):
        raise HTTPException(403, "Invalid tenant identity")

    return tenant_id, brand_id


def _bucket_name() -> str:
    name = settings.GCS_IMAGE_BUCKET or settings.GCS_BUCKET
    if not name:
        raise HTTPException(503, "Image storage is not configured")
    return name


def _publisher():
    if not settings.GCP_PROJECT:
        raise HTTPException(503, "GCP project is not configured")
    return pubsub_v1.PublisherClient()


@router.post(":initiate")
def initiate(
    body: InitiateRequest,
    user: dict = Depends(require_user),
):
    if not body.consent_confirmed:
        raise HTTPException(400, "Consent confirmation is required")
    if body.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, "Use JPEG, PNG, or WebP images")

    tenant_id, brand_id = _identity(user)
    image_id = str(uuid.uuid4())
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }[body.content_type]

    object_key = (
        f"garments/{tenant_id}/{brand_id}/{image_id}.{extension}"
    )

    client = storage.Client()
    blob = client.bucket(_bucket_name()).blob(object_key)
    blob.metadata = {
        "image_id": image_id,
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "consent_confirmed": "true",
    }

    try:
        upload_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=15),
            method="PUT",
            content_type=body.content_type,
        )
    except Exception:
        raise HTTPException(
            503,
            "Could not create signed upload URL; check Cloud Run signing permissions",
        )

    return ok({
        "image_id": image_id,
        "object_key": object_key,
        "upload_url": upload_url,
        "method": "PUT",
        "required_headers": {"Content-Type": body.content_type},
        "expires_in_seconds": 900,
        "max_bytes": MAX_IMAGE_BYTES,
        "note": (
            "Upload directly to the private bucket. Then call the completion "
            "endpoint. The browser must not expose or log the signed URL."
        ),
    })


@router.post(":complete")
def complete(
    body: CompleteRequest,
    user: dict = Depends(require_user),
):
    tenant_id, brand_id = _identity(user)

    # Never allow a tenant to submit another tenant's object key.
    prefix = f"garments/{tenant_id}/{brand_id}/"
    if not body.object_key.startswith(prefix):
        raise HTTPException(403, "Object does not belong to this brand")

    if not re.fullmatch(r"[0-9a-f-]{36}", body.image_id):
        raise HTTPException(400, "Invalid image ID")

    client = storage.Client()
    bucket = client.bucket(_bucket_name())
    blob = bucket.blob(body.object_key)

    if not blob.exists():
        raise HTTPException(409, "Uploaded image was not found")

    blob.reload()
    if blob.size is None or blob.size <= 0 or blob.size > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Image size is invalid or exceeds 10 MB")
    if blob.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, "Uploaded content type is not allowed")
    if not body.object_key.rsplit("/", 1)[-1].startswith(body.image_id):
        raise HTTPException(400, "Image ID does not match object key")

    if not ops_db.configured():
        raise HTTPException(503, "Garment registry is not configured")
    try:
        registered = ops_db.register_customer_garment_image(
            image_id=body.image_id,
            gcs_object_key=body.object_key,
            user_uuid=body.user_uuid,
            pin_code=body.pin_code,
            order_id=body.order_id or body.image_id,
            item_id=body.item_id or body.image_id,
        )
    except Exception:
        raise HTTPException(503, "Could not register the garment; retry completion")
    garment_instance_id = registered["garment_instance_id"]

    # Bind consent and tenant metadata to the object; do not treat extraction
    # output as verified until human review is implemented.
    blob.metadata = {
        **(blob.metadata or {}),
        "image_id": registered["image_id"],
        "garment_instance_id": garment_instance_id,
        "user_uuid": body.user_uuid,
        "pin_code": body.pin_code,
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "consent_confirmed": "true",
    }
    blob.patch()

    publisher = _publisher()
    topic_path = publisher.topic_path(
        settings.GCP_PROJECT, settings.PUBSUB_IMAGE_TOPIC
    )
    event = {
        "image_id": registered["image_id"],
        "garment_instance_id": garment_instance_id,
        "user_uuid": body.user_uuid,
        "pin_code": body.pin_code,
        "target_type": "garment",
        "gcs_object_key": body.object_key,
        "idempotency_key": body.image_id,
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "content_type": blob.content_type,
        "size_bytes": blob.size,
        "consent_confirmed": True,
    }

    try:
        publisher.publish(
            topic_path,
            json.dumps(event).encode("utf-8"),
            image_id=body.image_id,
            tenant_id=tenant_id,
        ).result(timeout=15)
    except Exception:
        raise HTTPException(
            503,
            "Image exists, but event publishing failed; retry completion",
        )

    return ok({
        "image_id": registered["image_id"],
        "garment_instance_id": garment_instance_id,
        "status": "uploaded_queued_for_extraction",
        "note": "Extraction and human review are not yet confirmation of a verified observation.",
    })
