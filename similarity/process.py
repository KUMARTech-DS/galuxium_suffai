"""Core similarity pipeline: check the photo's order → embed → match user → optional global → observation.

Linking follows suffai-extraction: customer / order / item come from the photo name only, the order must
belong to that customer, and the PIN is the customer's current one in BigQuery.

Each matched photo becomes one BigQuery observation row (the garment-per-PIN count lives there, view
garment_items). The Cloud SQL event is recorded first; when the BigQuery insert fails the request fails
so Pub/Sub retries, and the retry only resends the row (bq_synced_at tracks it).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from similarity import bq_sync, db
from similarity.embed import cosine_similarity, embed_image
from similarity.gcs_io import read_image
from similarity.photo_key import parse_photo_key, short
from similarity.settings import settings

log = logging.getLogger("similarity.process")

PIN_RE = re.compile(r"^\d{6}$")
RESEND_LIMIT = 10
RESEND_AFTER_SEC = 600


def send_observation(event: Dict[str, Any]) -> bool:
    """Write the BigQuery observation row of a recorded event and mark it synced.

    Raises bq_sync.ObservationNotWritten on failure; False (not marked) while BigQuery is off.
    """
    order_id, item_id = event.get("order_id"), event.get("item_id")
    if not (order_id and item_id):
        photo = parse_photo_key(event.get("gcs_object_key"))
        order_id, item_id = (photo.order_id, photo.item_id) if photo else (order_id, item_id)
    written = bq_sync.sync_observation(
        garment_id=str(event["matched_garment_id"]),
        user_uuid=event.get("user_uuid"),
        pin_code=event.get("pin_code"),
        brand_name=event.get("brand_name"),
        outcome=event["outcome"],
        gcs_object_key=event.get("gcs_object_key"),
        similarity_score=event.get("similarity_score"),
        order_id=order_id,
        item_id=item_id,
        event_ts=event["created_at"],
    )
    if written:
        db.mark_bq_synced(event["id"])
    return written


def resend_pending(*, limit: int = RESEND_LIMIT, older_than_sec: int = RESEND_AFTER_SEC) -> int:
    """Best effort: resend observations still unsynced after older_than_sec (retries that ran out).

    Stops at the first failure; returns how many were written.
    """
    if not bq_sync.enabled():
        return 0
    try:
        events = db.pending_bq_events(limit=limit, older_than_sec=older_than_sec)
    except Exception as exc:
        log.warning("pending observation lookup failed err=%s", type(exc).__name__)
        return 0
    sent = 0
    for event in events:
        try:
            if send_observation(event):
                sent += 1
        except bq_sync.ObservationNotWritten:
            break
        except Exception as exc:
            log.warning("pending observation resend failed event=%s err=%s", event.get("id"), type(exc).__name__)
            break
    if sent:
        log.info("resent pending BigQuery observations n=%s", sent)
    return sent


def _best_match(
    query: List[float], candidates: List[Tuple[str, List[float], Optional[str]]]
) -> Tuple[Optional[str], float]:
    best_id: Optional[str] = None
    best_score = -1.0
    for gid, emb, _brand in candidates:
        score = cosine_similarity(query, emb)
        if score > best_score:
            best_score = score
            best_id = gid
    return best_id, best_score


def _brand(payload: Dict[str, Any]) -> Optional[str]:
    """Optional brand hint for the global search: request body, Pub/Sub attributes or the object's metadata."""
    attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    for source in (payload, attrs, meta):
        for k in ("brand_name", "brand", "brandName"):
            v = str(source.get(k) or "").strip()
            if v:
                return v
    return None


def resolve_context(payload: Dict[str, Any], object_key: str) -> Optional[Dict[str, Optional[str]]]:
    """customer / order / item from the photo name only; None when the key is not a customer photo."""
    photo = parse_photo_key(object_key)
    if photo is None:
        return None
    return {
        "user_uuid": photo.customer_id,
        "order_uuid": photo.order_id,
        "item_uuid": photo.item_id,
        "brand_name": _brand(payload),
    }


def process_upload(payload: Dict[str, Any]) -> Dict[str, Any]:
    object_key = (
        payload.get("gcs_object_key")
        or payload.get("object_key")
        or payload.get("name")
        or ""
    ).strip()
    bucket = payload.get("gcs_bucket") or payload.get("bucket")
    idem = (
        payload.get("idempotency_key")
        or payload.get("message_id")
        or f"sim:{object_key}"
    )

    if not object_key:
        raise ValueError("gcs_object_key is required")

    ctx = resolve_context(payload, object_key)
    if ctx is None:
        log.info("ignored object (brands/, demo/ or not {customer_id}_{order_id}_{item_id}.jpg)")
        return {"status": "ignored", "reason": "not a customer photo name", "gcs_object_key": object_key}
    user_uuid = ctx["user_uuid"]
    brand_name = ctx["brand_name"]
    ids = {"customer_id": user_uuid, "order_id": ctx["order_uuid"], "item_id": ctx["item_uuid"]}

    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")

    prior = db.event_for_key(str(idem))
    if prior is not None:
        if prior["bq_synced_at"] is None and prior["outcome"] in db.SYNC_OUTCOMES and prior["matched_garment_id"]:
            # Processed earlier but its BigQuery row was not written: resend it, do not match again.
            if send_observation(prior):
                log.info("BigQuery observation resent key=%s garment=%s", idem, prior["matched_garment_id"])
                return {"status": "bq_resynced", "idempotency_key": idem, "garment_id": prior["matched_garment_id"]}
        log.info("idempotent skip key=%s", idem)
        return {"status": "already_processed", "idempotency_key": idem}

    owner = bq_sync.lookup_photo_owner(user_uuid, ctx["order_uuid"])
    if owner is None:
        log.warning(
            "unlinked photo customer=%s order=%s item=%s (order missing or belongs to another customer)",
            short(user_uuid), short(ctx["order_uuid"]), short(ctx["item_uuid"]),
        )
        return {"status": "unlinked", "reason": "order not found for this customer", "gcs_object_key": object_key, **ids}
    pin_code = owner["pin_code"]
    if not PIN_RE.match(pin_code):
        raise ValueError(f"customer {short(user_uuid)} has no valid 6-digit PIN")

    image = read_image(object_key, bucket=bucket if isinstance(bucket, str) else None)
    # Warehouse style × colour from extraction's catalog match; a label code Gemini read never counts.
    brand_sku = str(payload.get("brand_sku") or "").strip() or None

    def _record(outcome: str, garment_id: str, sim_score: Optional[float]) -> None:
        """Cloud SQL event first, then its BigQuery observation (raises so the photo is retried)."""
        event = {
            "gcs_object_key": image.object_key,
            "user_uuid": user_uuid,
            "pin_code": pin_code,
            "brand_name": brand_name,
            "outcome": outcome,
            "matched_garment_id": garment_id,
            "similarity_score": sim_score,
            "order_id": ctx.get("order_uuid"),
            "item_id": ctx.get("item_uuid"),
        }
        event.update(db.record_event(gcs_bucket=image.bucket, idempotency_key=str(idem), **event))
        send_observation(event)

    # 0) Garment already identified as this brand SKU: exact match, no fingerprint call
    if brand_sku:
        garment_id, link = db.find_user_garment_by_sku(user_uuid, brand_sku), False
        if not garment_id:
            garment_id, link = db.find_garment_by_sku(brand_sku), True
        if garment_id:
            outcome = "global_sku_match" if link else "user_sku_match"
            if link:
                db.link_user_to_garment(
                    user_uuid=user_uuid, garment_id=garment_id, object_key=image.object_key, pin_code=pin_code
                )
            db.increment_total(garment_id=garment_id)
            _record(outcome, garment_id, None)
            log.info("%s user=%s garment=%s sku=%s pin=%s", outcome, user_uuid, garment_id, brand_sku, pin_code)
            return {
                "status": outcome,
                "garment_id": garment_id,
                "brand_sku": brand_sku,
                "similarity_score": None,
                "added_to_user_wardrobe": link,
            }

    embedding, model = embed_image(image)
    threshold = settings.SIMILARITY_THRESHOLD

    # 1) Match against this user's wardrobe
    user_cands = db.load_user_embeddings(user_uuid)
    matched_id, score = _best_match(embedding, user_cands)
    if matched_id and score >= threshold:
        if brand_sku:
            db.set_garment_sku(matched_id, brand_sku)
        db.increment_total(garment_id=matched_id)
        _record("user_match", matched_id, score)
        log.info(
            "user_match user=%s garment=%s score=%.4f pin=%s",
            user_uuid,
            matched_id,
            score,
            pin_code,
        )
        return {
            "status": "user_match",
            "garment_id": matched_id,
            "similarity_score": score,
            "added_to_user_wardrobe": False,
        }

    # 2) No user match → try global nearest neighbor
    global_cands = db.load_global_embeddings(
        brand_name=brand_name, limit=settings.GLOBAL_CANDIDATE_LIMIT
    )
    # exclude already-compared user garments from global? fine to include; won't link twice due to UNIQUE
    g_id, g_score = _best_match(embedding, global_cands)
    if g_id and g_score >= threshold:
        if brand_sku:
            db.set_garment_sku(g_id, brand_sku)
        db.link_user_to_garment(
            user_uuid=user_uuid,
            garment_id=g_id,
            object_key=image.object_key,
            pin_code=pin_code,
        )
        db.increment_total(garment_id=g_id)
        _record("global_match_new_user_link", g_id, g_score)
        log.info(
            "global_match user=%s garment=%s score=%.4f",
            user_uuid,
            g_id,
            g_score,
        )
        return {
            "status": "global_match_new_user_link",
            "garment_id": g_id,
            "similarity_score": g_score,
            "added_to_user_wardrobe": True,
        }

    # 3) Create new unique garment + wardrobe row
    new_id = db.create_unique_garment(
        bucket=image.bucket,
        object_key=image.object_key,
        embedding=embedding,
        model=model,
        user_uuid=user_uuid,
        pin_code=pin_code,
        brand_name=brand_name,
        brand_sku=brand_sku,
    )
    _record("created_new", new_id, None)
    log.info("created_new user=%s garment=%s pin=%s brand=%s", user_uuid, new_id, pin_code, brand_name)
    return {
        "status": "created_new",
        "garment_id": new_id,
        "similarity_score": None,
        "added_to_user_wardrobe": True,
        "embedding_model": model,
    }
