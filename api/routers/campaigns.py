"""CMP-01 — campaign create + evaluate."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from api.deps import get_garment_store, get_normalized, get_rfm, require_user
from config import settings
from suffai.api.envelope import err, ok
from suffai.gcp_io import publish_extraction_event, signed_upload_url, trigger_worker_http
from suffai.metrics.campaign import evaluate_campaign_fit
from suffai.metrics.eligibility import eligible_orders

router = APIRouter(prefix="/v1/campaigns", tags=["campaigns"])


class CreateCampaignBody(BaseModel):
    content_type: str = "image/jpeg"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    attributes: Optional[Dict[str, Any]] = None  # optional marketer-provided tags


class EvaluateBody(BaseModel):
    attributes: Optional[Dict[str, Any]] = None


@router.post("")
def create_campaign(
    body: CreateCampaignBody,
    user: Dict[str, Any] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    store = get_garment_store()
    object_key = f"campaigns/{user.get('tenant_id')}/{uuid.uuid4().hex}.jpg"
    meta = {**(body.metadata or {})}
    if body.attributes:
        meta["attributes"] = body.attributes
    campaign = store.create_campaign(
        tenant_id=str(user.get("tenant_id") or user.get("brand_id")),
        image_object_key=object_key,
        metadata=meta,
    )
    upload = signed_upload_url(object_key, content_type=body.content_type)
    return ok({
        "campaign": campaign,
        "upload": upload,
        "idempotency_key": idempotency_key or campaign["campaign_id"],
    })


@router.post("/{campaign_id}/evaluate")
def evaluate_campaign(
    campaign_id: str,
    body: EvaluateBody,
    user: Dict[str, Any] = Depends(require_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    store = get_garment_store()
    campaign = store.get_campaign(campaign_id)
    if not campaign or campaign.get("deleted_at"):
        return err("NOT_FOUND", "campaign not found")

    meta = dict(campaign.get("metadata") or {})
    if body.attributes:
        meta["attributes"] = {**(meta.get("attributes") or {}), **body.attributes}
        # persist marketer corrections
        campaigns = store._load("campaigns")
        for c in campaigns:
            if c.get("campaign_id") == campaign_id:
                c["metadata"] = meta
        store._save("campaigns", campaigns)

    # Optional: trigger extraction on campaign image
    payload = {
        "gcs_object_key": campaign.get("image_object_key"),
        "gcs_bucket": settings.GCS_IMAGE_BUCKET or settings.GCS_BUCKET,
        "campaign_id": campaign_id,
        "target_type": "campaign",
        "idempotency_key": idempotency_key or f"campaign-eval-{campaign_id}",
    }
    pub = publish_extraction_event(payload)
    worker = trigger_worker_http(payload)

    # Refresh metadata if worker returned attributes
    if worker.get("triggered") and isinstance(worker.get("body"), dict):
        result = (worker["body"] or {}).get("result")
        if result:
            meta["extracted_attributes"] = result
            campaigns = store._load("campaigns")
            for c in campaigns:
                if c.get("campaign_id") == campaign_id:
                    c["metadata"] = meta
            store._save("campaigns", campaigns)

    norm = get_normalized()
    eligible_ids = {o["order_id"] for o in eligible_orders(norm["orders"])}
    evaluations = evaluate_campaign_fit(
        campaign_id=campaign_id,
        metadata=meta,
        lines=norm["order_commodity_lines"],
        eligible_order_ids=eligible_ids,
        rfm_snapshots=get_rfm(),
        my_brand=str(user.get("brand") or ""),
    )
    saved = store.save_campaign_evaluations(campaign_id, evaluations[:100])
    return ok({
        "campaign_id": campaign_id,
        "evaluations": saved,
        "pubsub": pub,
        "worker": worker,
        "note": "Fit is relevance, not promised conversion lift.",
    })


@router.get("/{campaign_id}/evaluations")
def list_evaluations(campaign_id: str, user: Dict[str, Any] = Depends(require_user)):
    store = get_garment_store()
    return ok({"campaign_id": campaign_id, "evaluations": store.list_campaign_evaluations(campaign_id)})
