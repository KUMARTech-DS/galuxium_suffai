"""ADV-01 — conversion feedback, uplift estimate, media activation, inventory."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import get_garment_store, get_normalized, get_rfm, require_user
from config import settings
from suffai.api.envelope import err, ok
from suffai.metrics.eligibility import eligible_orders

router = APIRouter(prefix="/v1/activation", tags=["activation"])


def _adv_path(name: str) -> Path:
    p = settings.DERIVED_DIR / name
    if not p.exists():
        p.write_text("[]\n", encoding="utf-8")
    return p


def _load(name: str) -> list:
    return json.loads(_adv_path(name).read_text(encoding="utf-8"))


def _save(name: str, rows: list) -> None:
    _adv_path(name).write_text(json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8")


class ConversionEvent(BaseModel):
    campaign_id: str
    segment_key: Optional[str] = None
    converted: bool = True
    revenue: float = 0.0
    channel: str = "unknown"
    observed_at: Optional[str] = None


class UpliftRequest(BaseModel):
    campaign_id: str
    treatment_segment_keys: List[str] = Field(default_factory=list)
    control_segment_keys: List[str] = Field(default_factory=list)


class MediaPushBody(BaseModel):
    campaign_id: str
    segment_keys: List[str]
    channel: str = "external_dsp"
    payload: Dict[str, Any] = Field(default_factory=dict)


class InventoryBody(BaseModel):
    brand_id: str
    sku: str
    on_hand: int
    warehouse: str = "default"
    as_of: Optional[str] = None


@router.post("/conversions")
def record_conversion(body: ConversionEvent, user: Dict[str, Any] = Depends(require_user)):
    if not settings.ENABLE_ADV:
        return err("FORBIDDEN", "ADV features disabled")
    rows = _load("conversion_events.json")
    rec = {
        "event_id": str(uuid.uuid4()),
        "tenant_id": user.get("tenant_id"),
        "brand": user.get("brand"),
        **body.model_dump(),
        "observed_at": body.observed_at or datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows.append(rec)
    _save("conversion_events.json", rows)
    try:
        from services.ops_db import insert_conversion

        insert_conversion(rec)
    except Exception:
        pass
    return ok(rec)


@router.post("/uplift/estimate")
def estimate_uplift(body: UpliftRequest, user: Dict[str, Any] = Depends(require_user)):
    """
    Simple difference-in-conversion-rates between treatment and control segments.
    Not a full causal design — labeled as observational proxy until experiments land.
    """
    if not settings.ENABLE_ADV:
        return err("FORBIDDEN", "ADV features disabled")
    events = [
        e for e in _load("conversion_events.json")
        if e.get("campaign_id") == body.campaign_id and e.get("tenant_id") == user.get("tenant_id")
    ]
    def rate(keys: List[str]) -> Dict[str, Any]:
        subset = [e for e in events if not keys or e.get("segment_key") in keys]
        n = len(subset)
        conv = sum(1 for e in subset if e.get("converted"))
        rev = sum(float(e.get("revenue") or 0) for e in subset)
        return {
            "n": n,
            "conversions": conv,
            "cvr": round(conv / n, 4) if n else None,
            "revenue": round(rev, 2),
        }

    t = rate(body.treatment_segment_keys)
    c = rate(body.control_segment_keys)
    uplift = None
    if t["cvr"] is not None and c["cvr"] is not None:
        uplift = round(t["cvr"] - c["cvr"], 4)

    return ok({
        "campaign_id": body.campaign_id,
        "treatment": t,
        "control": c,
        "uplift_cvr_pp": uplift,
        "method": "observational_diff_in_cvr",
        "caveat": (
            "Observational proxy only. Do not claim causal incremental lift without "
            "randomized experiment / proper CUPED design."
        ),
    })


@router.post("/media/push")
def media_push(body: MediaPushBody, user: Dict[str, Any] = Depends(require_user)):
    if not settings.ENABLE_ADV:
        return err("FORBIDDEN", "ADV features disabled")
    envelope = {
        "request_id": str(uuid.uuid4()),
        "tenant_id": user.get("tenant_id"),
        "brand": user.get("brand"),
        "campaign_id": body.campaign_id,
        "segment_keys": body.segment_keys,
        "channel": body.channel,
        "payload": body.payload,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    rows = _load("media_activation_log.json")
    delivery = {"mode": "logged_only", "ok": True}
    webhook = settings.MEDIA_ACTIVATION_WEBHOOK
    if webhook:
        try:
            resp = httpx.post(webhook, json=envelope, timeout=20.0)
            delivery = {"mode": "webhook", "status_code": resp.status_code, "ok": resp.is_success}
        except Exception as exc:
            delivery = {"mode": "webhook", "ok": False, "error": str(exc)}
    rows.append({**envelope, "delivery": delivery})
    _save("media_activation_log.json", rows)
    try:
        from services.ops_db import insert_media_activation

        insert_media_activation(envelope, delivery)
    except Exception:
        pass
    return ok({"activation": envelope, "delivery": delivery})


@router.post("/inventory")
def upsert_inventory(body: InventoryBody, user: Dict[str, Any] = Depends(require_user)):
    rows = _load("inventory_levels.json")
    found = None
    for r in rows:
        if r.get("brand_id") == body.brand_id and r.get("sku") == body.sku and r.get("warehouse") == body.warehouse:
            r.update(body.model_dump())
            r["updated_at"] = datetime.now(timezone.utc).isoformat()
            found = r
            break
    if not found:
        found = {
            **body.model_dump(),
            "inventory_id": str(uuid.uuid4()),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        rows.append(found)
    _save("inventory_levels.json", rows)
    try:
        from services.ops_db import upsert_inventory

        upsert_inventory(found)
    except Exception:
        pass
    # also mirror into catalog availability
    store = get_garment_store()
    store.upsert_catalog_sku({
        "brand_id": body.brand_id,
        "brand_sku": body.sku,
        "availability": f"on_hand:{body.on_hand}",
    })
    return ok(found)


@router.get("/inventory")
def list_inventory(brand_id: Optional[str] = None, user: Dict[str, Any] = Depends(require_user)):
    rows = _load("inventory_levels.json")
    if brand_id:
        rows = [r for r in rows if r.get("brand_id") == brand_id]
    return ok({"inventory": rows})


@router.get("/snapshots/schedule-status")
def schedule_status(user: Dict[str, Any] = Depends(require_user)):
    """EXP-01 scheduled snapshots — status of Cloud Scheduler → CDC/BQ jobs."""
    wm = {}
    if settings.CDC_WATERMARK_PATH.exists():
        wm = json.loads(settings.CDC_WATERMARK_PATH.read_text(encoding="utf-8"))
    return ok({
        "cdc_watermark": wm,
        "use_bigquery_marts": settings.USE_BIGQUERY_MARTS,
        "scheduler_jobs": [
            "suffai-cdc-hourly",
            "suffai-bq-marts-daily",
        ],
        "note": "Jobs defined in infra/terraform and deploy/scheduler.yaml",
    })
