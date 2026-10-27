"""Read garment / campaign images from private GCS."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass

from google.cloud import storage

from worker.settings import settings

log = logging.getLogger("suffai-extraction.gcs")


@dataclass
class GcsImage:
    bucket: str
    object_key: str
    gs_uri: str
    content_type: str
    data: bytes
    size_bytes: int


def _guess_content_type(object_key: str, blob_content_type: str | None) -> str:
    if blob_content_type and blob_content_type != "application/octet-stream":
        return blob_content_type
    guessed, _ = mimetypes.guess_type(object_key)
    return guessed or "image/jpeg"


def resolve_bucket(explicit_bucket: str | None = None) -> str:
    bucket = (explicit_bucket or settings.GCS_IMAGE_BUCKET or "").strip()
    if not bucket:
        raise ValueError(
            "GCS bucket missing. Set GCS_IMAGE_BUCKET on suffai-extraction "
            "or pass gcs_bucket in the Pub/Sub payload."
        )
    return bucket


def read_image(object_key: str, *, bucket: str | None = None) -> GcsImage:
    """Download image bytes from GCS and return metadata + payload."""
    if not object_key or not str(object_key).strip():
        raise ValueError("gcs_object_key is required")

    bkt_name = resolve_bucket(bucket)
    key = object_key.strip().lstrip("/")
    client = storage.Client(project=settings.PROJECT_ID or None)
    blob = client.bucket(bkt_name).blob(key)

    if not blob.exists():
        raise FileNotFoundError(f"GCS object not found: gs://{bkt_name}/{key}")

    blob.reload()
    data = blob.download_as_bytes()
    if len(data) > settings.MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image too large ({len(data)} bytes > MAX_IMAGE_BYTES={settings.MAX_IMAGE_BYTES})"
        )
    if not data:
        raise ValueError(f"Empty GCS object: gs://{bkt_name}/{key}")

    content_type = _guess_content_type(key, blob.content_type)
    if not content_type.startswith("image/"):
        raise ValueError(f"Unsupported content type for extraction: {content_type}")

    log.info(
        "gcs_read bucket=%s key=%s bytes=%s content_type=%s",
        bkt_name,
        key,
        len(data),
        content_type,
    )
    return GcsImage(
        bucket=bkt_name,
        object_key=key,
        gs_uri=f"gs://{bkt_name}/{key}",
        content_type=content_type,
        data=data,
        size_bytes=len(data),
    )
