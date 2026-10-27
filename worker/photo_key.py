"""Customer garment photo names: {customer_id}_{order_id}_{item_id}.jpg|jpeg|png in any folder.

Keep in step with similarity/photo_key.py (suffai-similarity) so both services link a photo to the same
item; tests/test_photo_key_parity.py feeds both parsers the same keys.
Brand SKU images (brands/), warehouse catalog photos (catalog/) and Ads Lab demo uploads (demo/)
are never customer photos.
"""

from __future__ import annotations

import re
from typing import Any, Dict, NamedTuple, Optional

SKIP_PREFIXES = ("brands/", "demo/", "catalog/")
MAX_ID_LEN = 64
_PHOTO_RE = re.compile(
    r"^(?P<customer>[^/_]+)_(?P<order>[^/_]+)_(?P<item>[^/]+)\.(?:jpe?g|png)$",
    re.IGNORECASE,
)


class PhotoKey(NamedTuple):
    customer_id: str
    order_id: str
    item_id: str


def parse_photo_key(object_key: Optional[str]) -> Optional[PhotoKey]:
    key = (object_key or "").strip().lstrip("/")
    if not key or key.startswith(SKIP_PREFIXES):
        return None
    m = _PHOTO_RE.match(key.rsplit("/", 1)[-1])
    if not m:
        return None
    ids = PhotoKey(m.group("customer").strip(), m.group("order").strip(), m.group("item").strip())
    if not all(ids) or any(len(v) > MAX_ID_LEN for v in ids):
        return None
    return ids


def needs_extraction(payload: Dict[str, Any]) -> bool:
    """Bucket notifications cover every object; only customer photos, demo/ uploads and registered images
    need extraction. Brand SKU images are extracted through /v1/extract by the Command Center."""
    if payload.get("image_id") or payload.get("garment_instance_id"):
        return True
    key = str(payload.get("gcs_object_key") or "").strip().lstrip("/")
    return key.startswith("demo/") or parse_photo_key(key) is not None


def short(v: str) -> str:
    """Log-safe short form of an id."""
    v = str(v or "")
    return v[:6] + "…" if len(v) > 8 else v
