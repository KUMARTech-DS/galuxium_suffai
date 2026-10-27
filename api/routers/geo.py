from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import get_normalized, get_rfm, require_user
from suffai.api.envelope import err, ok
from suffai.geo.polygons import choropleth_features, load_pin_polygons
from suffai.metrics.eligibility import eligible_orders
from suffai.metrics.geo import geo_active_customers, geo_query
from suffai.privacy.differencing import register_query
from suffai.warehouse.bq_reader import load_geo_tiles_from_marts

router = APIRouter(prefix="/v1/geo", tags=["geo"])


class GeoQueryRequest(BaseModel):
    geo_level: str = "pin"
    geo_ids: Optional[List[str]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    rfm_segments: Optional[List[str]] = None
    metrics: List[str] = Field(default_factory=lambda: ["active_customers", "commodity_popularity"])


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=datetime.now().astimezone().tzinfo)


def _tiles_source() -> tuple[list, str]:
    bq = load_geo_tiles_from_marts()
    if bq is not None:
        return bq, "bigquery_marts"
    return geo_active_customers(get_normalized()["orders"]), "local_normalize"


@router.get("/tiles")
def tiles(metric: str = "active_customers", user: Dict[str, Any] = Depends(require_user)):
    rows, source = _tiles_source()
    return ok({"metric": metric, "tiles": rows}, coverage={"tile_count": len(rows), "source": source})


@router.get("/polygons")
def polygons(user: Dict[str, Any] = Depends(require_user)):
    """Base PIN polygons (approximate demo boundaries unless replaced with licensed data)."""
    return ok(load_pin_polygons())


@router.get("/choropleth")
def choropleth(metric: str = "active_customers", user: Dict[str, Any] = Depends(require_user)):
    rows, source = _tiles_source()
    fc = choropleth_features(rows, metric_key=metric)
    return ok(fc, coverage={"features": len(fc.get("features") or []), "source": source})


@router.post("/query")
def query(body: GeoQueryRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    norm = get_normalized()
    result = geo_query(
        norm["orders"],
        norm["order_commodity_lines"],
        get_rfm(),
        geo_ids=body.geo_ids,
        date_from=_parse_date(body.date_from),
        date_to=_parse_date(body.date_to),
        rfm_segments=body.rfm_segments,
    )

    # Privacy differencing check on eligible customer cohort in filter
    elig = eligible_orders(norm["orders"])
    if body.geo_ids:
        elig = [o for o in elig if (o.get("order_pin") or "UNKNOWN") in body.geo_ids]
    if body.rfm_segments:
        seg_map = {s["customer_id"]: s["rfm_segment"] for s in get_rfm()}
        elig = [o for o in elig if seg_map.get(o.get("customer_id")) in body.rfm_segments]
    cohort = [o.get("customer_id") for o in elig if o.get("customer_id")]
    risk = register_query(
        tenant_id=str(user.get("tenant_id") or user.get("brand_id") or "anon"),
        filters={
            "geo_ids": body.geo_ids,
            "rfm_segments": body.rfm_segments,
            "date_from": body.date_from,
            "date_to": body.date_to,
        },
        cohort_customer_ids=cohort,
    )
    result["choropleth"] = choropleth_features(result.get("tiles") or [])
    result["privacy"] = risk
    if risk.get("differencing_risk"):
        return err(
            "INSUFFICIENT_DATA",
            risk.get("reason") or "Differencing risk — result suppressed",
            data={"privacy": risk, "tiles": [], "commodities": [], "rfm_mix": []},
        )
    return ok(result, coverage={**(result.get("meta") or {}), "privacy": risk})
