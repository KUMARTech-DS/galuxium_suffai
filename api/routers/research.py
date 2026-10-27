"""RES-01 — product opportunities."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import get_garment_store, get_normalized, require_user
from suffai.api.envelope import ok
from suffai.metrics.eligibility import eligible_orders
from suffai.metrics.research import product_opportunities

router = APIRouter(prefix="/v1/product-opportunities", tags=["research"])


class OpportunityBody(BaseModel):
    brand_id: Optional[str] = None
    geo_ids: Optional[List[str]] = None
    target_segment: Optional[str] = None
    candidate_categories: List[str] = Field(default_factory=list)


@router.post("")
def opportunities(body: OpportunityBody, user: Dict[str, Any] = Depends(require_user)):
    store = get_garment_store()
    norm = get_normalized()
    eligible_ids = {o["order_id"] for o in eligible_orders(norm["orders"])}
    my_brand = user.get("brand") or body.brand_id or ""
    result = product_opportunities(
        norm["order_commodity_lines"],
        eligible_ids,
        store.list_verified_brand_observations(),
        store.list_catalog(brand_id=user.get("brand_id")),
        my_brand=str(my_brand),
        geo_ids=body.geo_ids,
    )
    return ok(result)
