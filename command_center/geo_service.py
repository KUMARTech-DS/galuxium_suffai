"""One Geo store scorer for the map, the brief, the market check and AURA.

Every caller scores PINs through analysis_for(): geo_analysis.analyze + decision_from_score, then the
4 km rule. The same PIN and filters therefore get the same score and Yes / Consider / No everywhere.
Filters are {pins, age, gender, rfm}; brand logins always score their own brand on masked metrics.
"""

from __future__ import annotations

from typing import Any, Optional

import auth_acl
import campaign_service as camp_svc
import geo_analysis
import geo_proximity as gp

_LIMITS = {"pins": 50, "age": 20, "gender": 4, "rfm": 20}


def _as_list(raw: Any, limit: int) -> list[str]:
    items = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
    return [str(x).strip()[:64] for x in items if str(x or "").strip()][:limit]


def clean_filters(filters: Any) -> dict[str, list[str]]:
    f = filters if isinstance(filters, dict) else {}
    return {
        "pins": _as_list(f.get("pins") if f.get("pins") is not None else f.get("pin"), _LIMITS["pins"]),
        "age": _as_list(f.get("age"), _LIMITS["age"]),
        "gender": _as_list(f.get("gender"), _LIMITS["gender"]),
        "rfm": _as_list(f.get("rfm"), _LIMITS["rfm"]),
    }


def metrics_for(role: str, session_brand: Optional[str]) -> dict[str, Any]:
    """Geo metrics as this login may see them (brand logins: other brands are Competitors). Fails closed."""
    metrics = camp_svc.load_metrics()
    if role == "brand":
        if not (session_brand or "").strip():
            raise camp_svc.ServiceError("not_allowed", "This brand account has no brand name assigned", 403)
        metrics = auth_acl.mask_metrics_for_brand(metrics, session_brand)
    return metrics


def brand_for(
    metrics: dict[str, Any], *, role: str, session_brand: Optional[str], filters: Any,
    analysis_brand: Optional[str] = None,
) -> Optional[str]:
    f = clean_filters(filters)
    return geo_analysis.analysis_brand_for(
        metrics, role=role, session_brand=session_brand, requested=analysis_brand,
        pins=f["pins"], ages=f["age"], genders=f["gender"], rfms=f["rfm"],
    )


def analysis_for(
    *,
    role: str,
    session_brand: Optional[str],
    filters: Any,
    analysis_brand: Optional[str] = None,
    top_n: Optional[int] = 8,
    stores: str = "lookup",
    request_id: str = "-",
    metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Scored PINs for these filters. stores="lookup" runs the full store lookup (may call Gemini, blocks);
    stores="cached" reads the store cache only, so PINs stay unverified until a lookup has run.
    `metrics`, when given, must already be what metrics_for() returns for this login.
    """
    if metrics is None:
        metrics = metrics_for(role, session_brand)
    f = clean_filters(filters)
    brand = brand_for(metrics, role=role, session_brand=session_brand, filters=f, analysis_brand=analysis_brand)
    if stores == "cached":
        lookup = gp.cached_brand_stores(brand)
    else:
        lookup = gp.locate_brand_stores(brand, pin_geo=metrics.get("pin_geo") or {}, request_id=request_id)
    return geo_analysis.analyze(
        metrics, role=role, session_brand=session_brand, pins=f["pins"], ages=f["age"], genders=f["gender"],
        rfms=f["rfm"], analysis_brand=brand, top_n=top_n, stores_lookup=lookup,
    )


def brief_input(
    *,
    role: str,
    session_brand: Optional[str],
    pin: Optional[str],
    filters: Any,
    analysis_brand: Optional[str] = None,
    request_id: str = "-",
) -> dict[str, Any]:
    """GeoBriefRequest payload for `pin` under these filters, built from server scores only."""
    analysis = analysis_for(role=role, session_brand=session_brand, filters=filters, analysis_brand=analysis_brand,
                            top_n=None, request_id=request_id)
    return geo_analysis.brief_payload(analysis, focus_pin=(pin or "").strip() or None, role=role)


def row_for(
    *,
    role: str,
    session_brand: Optional[str],
    pin: str,
    filters: Any,
    analysis_brand: Optional[str] = None,
    request_id: str = "-",
) -> Optional[dict[str, Any]]:
    """The scored row of one PIN (with the analysed brand), or None when the filters leave it out."""
    analysis = analysis_for(role=role, session_brand=session_brand, filters=filters, analysis_brand=analysis_brand,
                            top_n=None, request_id=request_id)
    row = next((r for r in analysis["pins"] if r["pin"] == pin), None)
    return {**row, "analysis_brand": analysis["analysis_brand"]} if row else None
