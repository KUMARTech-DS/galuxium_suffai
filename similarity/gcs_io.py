"""Read images and object metadata from GCS."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from typing import Optional

from google.cloud import storage

from similarity.settings import settings

log = logging.getLogger("similarity.gcs")


@dataclass
class GcsImage:
    bucket: str
    object_key: str
    gs_uri: str
    content_type: str
    data: bytes
    size_bytes: int
    metadata: dict[str, str]


def resolve_bucket(explicit: Optional[str] = None) -> str:
    bucket = (explicit or settings.GCS_IMAGE_BUCKET or "").strip()
    if not bucket:
        raise ValueError("GCS_IMAGE_BUCKET not set and no gcs_bucket in payload")
    return bucket


def read_image(object_key: str, *, bucket: Optional[str] = None) -> GcsImage:
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
        raise ValueError(f"Image too large ({len(data)} bytes)")
    if not data:
        raise ValueError(f"Empty GCS object: gs://{bkt_name}/{key}")

    content_type = blob.content_type or mimetypes.guess_type(key)[0] or "image/jpeg"
    if not content_type.startswith("image/"):
        raise ValueError(f"Unsupported content type: {content_type}")

    meta = {str(k).lower(): str(v) for k, v in (blob.metadata or {}).items()}
    log.info("gcs_read bucket=%s key=%s bytes=%s meta_keys=%s", bkt_name, key, len(data), list(meta))
    return GcsImage(
        bucket=bkt_name,
        object_key=key,
        gs_uri=f"gs://{bkt_name}/{key}",
        content_type=content_type,
        data=data,
        size_bytes=len(data),
        metadata=meta,
    )
