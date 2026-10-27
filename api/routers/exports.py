"""EXP-01 — aggregate CSV export + saved views."""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_garment_store, get_normalized, get_rfm, require_user
from suffai.api.envelope import err, ok
from suffai.metrics.geo import geo_query
from suffai.privacy.differencing import enforce_export_role
from suffai.privacy.suppress import filter_export_rows

router = APIRouter(prefix="/v1/exports", tags=["exports"])


class SaveViewBody(BaseModel):
    name: str
    page: str
    filters: Dict[str, Any] = Field(default_factory=dict)


@router.get("/geo.csv")
def export_geo_csv(
    pin: Optional[str] = None,
    user: Dict[str, Any] = Depends(require_user),
):
    if not enforce_export_role(user.get("role")):
        return err("FORBIDDEN", f"Role {user.get('role')} cannot export aggregates")
    norm = get_normalized()
    geo_ids = [pin] if pin else None
    result = geo_query(
        norm["orders"],
        norm["order_commodity_lines"],
        get_rfm(),
        geo_ids=geo_ids,
    )
    tiles = filter_export_rows(result.get("tiles") or [])
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["geo_key", "pin_code", "area", "active_customers", "denominator", "metric_key"],
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in tiles:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=geo_aggregates.csv"},
    )


@router.post("/saved-views")
def save_view(body: SaveViewBody, user: Dict[str, Any] = Depends(require_user)):
    store = get_garment_store()
    rec = store.save_view(
        tenant_id=str(user.get("tenant_id") or user.get("brand_id")),
        name=body.name,
        page=body.page,
        filters=body.filters,
    )
    return ok(rec)


@router.get("/saved-views")
def list_views(user: Dict[str, Any] = Depends(require_user)):
    store = get_garment_store()
    return ok({"views": store.list_saved_views(str(user.get("tenant_id") or user.get("brand_id")))})
