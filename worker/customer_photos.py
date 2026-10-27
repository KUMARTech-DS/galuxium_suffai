"""Customer garment photo pipeline: check the order, extract with Gemini, write the item row to BigQuery.

The order's CSV-loaded (legacy) items are superseded by its photos, so an order never counts both.

Dependencies are passed in by worker.main so this module stays free of GCS / Vertex imports.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from worker import bq_items, catalog_match, vocab
from worker.photo_key import PhotoKey, short

log = logging.getLogger("suffai-extraction.photos")

REVIEW_STATUSES = {"NEEDS_REVIEW", "COMPLETED", "FAILED"}


def garment_sku(row: Dict[str, Any]) -> Optional[str]:
    """Warehouse style × colour of a catalog-matched item (size-independent), for wardrobe dedup."""
    style = vocab.value_key(row.get("brand_style_code"))
    if not style:
        return None
    return f"{vocab.value_key(row.get('brand'))}:{style}:{vocab.value_key(row.get('color'))}"


def process(
    photo: PhotoKey,
    *,
    object_key: str,
    bucket: Optional[str],
    read_image: Callable[..., Any],
    extract: Optional[Callable[[Any], Dict[str, Any]]],
    abstain: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    ids = {"customer_id": photo.customer_id, "order_id": photo.order_id, "item_id": photo.item_id}
    if not bq_items.enabled():
        log.warning("customer photo ignored: USE_BQ=false or GOOGLE_CLOUD_PROJECT unset")
        return {"status": "ignored", "reason": "bigquery_disabled", "gcs_object_key": object_key, **ids}

    order = bq_items.lookup_order(photo.order_id, photo.customer_id)
    if not order:
        log.warning(
            "unlinked photo customer=%s order=%s item=%s (order or customer missing, or another customer's order)",
            short(photo.customer_id), short(photo.order_id), short(photo.item_id),
        )
        return {"status": "unlinked", "reason": "order not found for this customer", "gcs_object_key": object_key, **ids}

    image = read_image(object_key, bucket=bucket)
    if extract is not None:
        result = extract(image)
    else:
        result = abstain("ENABLE_VERTEX=false; abstain without paid API call")
    status = str(result.get("review_status") or "NEEDS_REVIEW").upper()
    if status not in REVIEW_STATUSES:
        status = "NEEDS_REVIEW"

    row = bq_items.item_row(photo, object_key=image.object_key, result=result, status=status)
    photo_attrs = bq_items.item_attributes(result)
    try:
        overwrite = catalog_match.match(result, photo_attrs, image if extract is not None else None)
    except Exception:
        log.exception("catalog match failed order=%s item=%s", short(photo.order_id), short(photo.item_id))
        overwrite = None
    if overwrite:
        catalog_match.apply_to_row(row, overwrite)
        log.info(
            "catalog match order=%s item=%s how=%s style=%s",
            short(photo.order_id), short(photo.item_id), overwrite.get("catalog_match"),
            overwrite.get("brand_style_code"),
        )
    bq_items.supersede_legacy(photo.order_id)
    bq_items.upsert_item(row)
    log.info(
        "item saved customer=%s order=%s item=%s status=%s",
        short(photo.customer_id), short(photo.order_id), short(photo.item_id), row["image_status"],
    )
    persist = {"bigquery": "commodities", "image_status": row["image_status"], "brand": row.get("brand")}
    if overwrite:
        persist["catalog_match"] = overwrite.get("catalog_match")
        persist["brand_style_code"] = overwrite.get("brand_style_code")
        persist["garment_sku"] = garment_sku(row)
        if overwrite.get("catalog_score") is not None:
            persist["catalog_score"] = overwrite["catalog_score"]
    return {
        "status": status,
        "target_type": "customer_photo",
        "gcs_uri": getattr(image, "gs_uri", None),
        "gcs_object_key": image.object_key,
        **ids,
        "persist": persist,
        "result": {k: v for k, v in result.items() if k != "raw_model_json"},
    }
