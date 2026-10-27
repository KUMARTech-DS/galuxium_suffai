"""Server-side Geo Intelligence analysis: the only store scorer and decision rule.

The Geo map, the Geo brief, the market check and AURA all score PINs here (through geo_service).
Works on the aggregated metrics payload only (customer_cells, garment_cells, pin_geo);
output never contains customer-level rows, coordinates, or competitor brand names.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Sequence

import geo_proximity as gp

STORE_W = {"demand": 0.45, "garment": 0.25, "catchment": 0.20, "revenue": 0.10}
STORE_YES = 0.66
STORE_CONSIDER = 0.33
# Demand term = HV_DEMAND_W * norm(Champions + Loyal customers) + (1 - HV_DEMAND_W) * norm(all customers)
HV_DEMAND_W = 0.6
HV_GARMENT_WEIGHT = 2.0
_HV = {r.lower() for r in gp.HIGH_VALUE_RFM}
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL"]


def persona_min_customers() -> int:
    try:
        return max(1, int(os.getenv("GEO_PERSONA_MIN_CUSTOMERS", "3")))
    except ValueError:
        return 3


def is_high_value(rfm: Any) -> bool:
    return str(rfm or "").strip().lower() in _HV


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def _clean_list(values: Optional[Sequence[Any]], limit: int = 20) -> list[str]:
    return [str(v).strip() for v in (values or []) if str(v or "").strip()][:limit]


def _matches(cell: dict[str, Any], *, ages: list[str], genders: list[str], rfms: list[str]) -> bool:
    if ages and _norm(cell.get("age_group")) not in {_norm(a) for a in ages}:
        return False
    if genders:
        cg = _norm(cell.get("gender"))[:1]
        if cg and cg not in {_norm(g)[:1] for g in genders}:
            return False
    if rfms and _norm(cell.get("rfm_segment")) not in {_norm(r) for r in rfms}:
        return False
    return True


def _norm01(values: list[float]):
    nums = [v for v in values if v is not None]
    if not nums:
        return lambda v: 0.0
    lo, hi = min(nums), max(nums)
    if hi <= lo:
        return lambda v: 1.0 if hi > 0 else 0.0
    return lambda v: (float(v) - lo) / (hi - lo)


def decision_from_score(
    score: float,
    demand: float,
    est_footfall: float,
    brand_share: Optional[float],
    brand_garments: float,
    hv_demand: Optional[float] = None,
) -> str:
    """Expansion call before the 4 km guardrail; "Yes" needs Champions/Loyal demand when it is known."""
    d = _decision_from_score(score, demand, est_footfall, brand_share, brand_garments)
    if d == "Yes" and hv_demand is not None and hv_demand <= 0:
        return "Consider"
    return d


def _decision_from_score(
    score: float, demand: float, est_footfall: float, brand_share: Optional[float], brand_garments: float
) -> str:
    if not demand or demand <= 0:
        return "No"
    if not brand_garments or brand_garments <= 0:
        return "No"
    if est_footfall is not None and est_footfall < 40:
        return "No"
    if brand_share is not None:
        if brand_share >= 0.35 and est_footfall >= 60 and score >= STORE_CONSIDER:
            return "Yes"
        if brand_share >= 0.22 or (brand_garments >= 3 and score >= STORE_CONSIDER):
            return "Consider"
        return "No"
    if score >= STORE_YES:
        return "Yes"
    if score >= STORE_CONSIDER:
        return "Consider"
    return "No"


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _num_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decision_for_row(row: Optional[dict[str, Any]]) -> str:
    """Expansion call (before the 4 km rule) for one scored PIN row: an analyze() row or a brief focus."""
    r = row or {}
    return decision_from_score(
        _num(r.get("score")), _num(r.get("demand")), _num(r.get("est_footfall")),
        _num_or_none(r.get("brand_share")), _num(r.get("brand_garments")), _num_or_none(r.get("hv_demand")),
    )


def _top(counter: dict[str, float], n: int = 5) -> list[dict[str, Any]]:
    return [
        {"value": k, "count": round(v, 2)}
        for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n]
    ]


_NO_VALUE = {"", "unknown", "na", "n/a", "none"}


def _add_values(dst: dict[str, float], items: Any) -> None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        val = str(item.get("value") or "").strip()
        if val.lower() not in _NO_VALUE:
            dst[val] = dst.get(val, 0.0) + float(item.get("count") or 1)


def resolve_analysis_brand(
    garments: list[dict[str, Any]], *, role: str, session_brand: Optional[str], requested: Optional[str]
) -> Optional[str]:
    if role == "brand":
        return (session_brand or "").strip() or None
    req = (requested or "").strip()
    if req and req.lower() != "competitors":
        return req
    counts: dict[str, float] = {}
    for g in garments:
        b = str(g.get("brand") or "").strip()
        if b and b != "Competitors":
            counts[b] = counts.get(b, 0) + float(g.get("garments") or 0)
    return max(counts, key=counts.get) if counts else None


def _audience_garments(
    metrics: dict[str, Any], *, pins: list[str], ages: list[str], genders: list[str], rfms: list[str]
) -> list[dict[str, Any]]:
    pin_set = set(pins)
    return [
        g for g in (metrics.get("garment_cells") or [])
        if isinstance(g, dict) and _matches(g, ages=ages, genders=genders, rfms=rfms)
        and (not pin_set or str(g.get("pin_code") or "") in pin_set)
    ]


def analysis_brand_for(
    metrics: dict[str, Any],
    *,
    role: str,
    session_brand: Optional[str],
    requested: Optional[str] = None,
    pins: Optional[Sequence[str]] = None,
    ages: Optional[Sequence[str]] = None,
    genders: Optional[Sequence[str]] = None,
    rfms: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Brand to score against Competitors: the brand login's own brand, else the requested brand, else the
    brand with the most garments among the selected audience and PINs."""
    garm = _audience_garments(metrics, pins=_clean_list(pins, 50), ages=_clean_list(ages),
                              genders=_clean_list(genders, 4), rfms=_clean_list(rfms))
    return resolve_analysis_brand(garm, role=role, session_brand=session_brand, requested=requested)


def analyze(
    metrics: dict[str, Any],
    *,
    role: str,
    session_brand: Optional[str],
    pins: Optional[Sequence[str]] = None,
    ages: Optional[Sequence[str]] = None,
    genders: Optional[Sequence[str]] = None,
    rfms: Optional[Sequence[str]] = None,
    analysis_brand: Optional[str] = None,
    top_n: Optional[int] = 8,
    stores_lookup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Score PINs for the analysed brand; `top_n=None` returns every row. With `stores_lookup`
    (geo_proximity.locate_brand_stores / cached_brand_stores) every row also gets the 4 km guardrail:
    PINs within PROXIMITY_KM of an existing store become "No" / "Service via Existing Store".
    """
    pin_f = _clean_list(pins, 50)
    age_f = _clean_list(ages)
    gen_f = _clean_list(genders, 4)
    rfm_f = _clean_list(rfms)
    pin_set = set(pin_f)

    customers = [c for c in (metrics.get("customer_cells") or []) if isinstance(c, dict)]
    pin_geo = metrics.get("pin_geo") if isinstance(metrics.get("pin_geo"), dict) else {}

    cust = [c for c in customers if _matches(c, ages=age_f, genders=gen_f, rfms=rfm_f)]
    garm = _audience_garments(metrics, pins=pin_f, ages=age_f, genders=gen_f, rfms=rfm_f)
    brand = resolve_analysis_brand(garm, role=role, session_brand=session_brand, requested=analysis_brand)
    own = _norm(brand)

    by_pin: dict[str, dict[str, Any]] = {}

    def row_for(pin: str) -> dict[str, Any]:
        return by_pin.setdefault(
            pin,
            {"demand": 0.0, "hv_demand": 0.0, "orders": 0.0, "revenue": 0.0, "brand_garments": 0.0,
             "competitor_garments": 0.0, "commodities": {}},
        )

    # High-value demand ignores the RFM filter so narrowing the view never hides Champions/Loyal signal.
    for c in customers:
        pin = str(c.get("pin_code") or "")
        if not pin or (pin_set and pin not in pin_set) or not is_high_value(c.get("rfm_segment")):
            continue
        if _matches(c, ages=age_f, genders=gen_f, rfms=[]):
            row_for(pin)["hv_demand"] += float(c.get("customers") or 0)

    segments: dict[str, dict[str, float]] = {"rfm_segment": {}, "age_group": {}, "gender": {}}
    for c in cust:
        pin = str(c.get("pin_code") or "")
        if not pin or (pin_set and pin not in pin_set):
            continue
        r = row_for(pin)
        n = float(c.get("customers") or 0)
        r["demand"] += n
        r["orders"] += float(c.get("orders") or 0)
        r["revenue"] += float(c.get("revenue") or 0)
        for key in segments:
            val = str(c.get(key) or "").strip()
            if val:
                segments[key][val] = segments[key].get(val, 0) + n

    colors: dict[str, float] = {}
    patterns: dict[str, float] = {}
    sizes: dict[str, float] = {}
    brands: dict[str, float] = {}
    own_commodities: dict[str, float] = {}
    for g in garm:
        pin = str(g.get("pin_code") or "")
        if not pin:
            continue
        r = row_for(pin)
        n = float(g.get("garments") or 0)
        if own and _norm(g.get("brand")) == own:
            r["brand_garments"] += n
            for item in (g.get("names") or g.get("commodities") or []):
                name = (item or {}).get("value") or (item or {}).get("name")
                if name:
                    cnt = float((item or {}).get("count") or 1)
                    r["commodities"][name] = r["commodities"].get(name, 0) + cnt
                    own_commodities[name] = own_commodities.get(name, 0) + cnt
        else:
            r["competitor_garments"] += n
        _add_values(colors, g.get("colors"))
        _add_values(patterns, g.get("patterns"))
        _add_values(sizes, g.get("sizes"))
        b = str(g.get("brand") or "").strip()
        if b and role == "brand" and _norm(b) != own:
            b = "Competitors"
        if b:
            brands[b] = brands.get(b, 0.0) + n

    rows: list[dict[str, Any]] = []
    for pin in (pin_f or list(pin_geo.keys())):
        geo = pin_geo.get(pin) or {}
        agg = by_pin.get(pin) or row_for(pin)
        catchment = float(geo.get("pop_buffer_2km") or geo.get("population_pin") or 0)
        demand = agg["demand"]
        est_footfall = round(demand * 18 + (catchment * 0.012 if demand > 0 else 0))
        wardrobe = agg["brand_garments"] + agg["competitor_garments"]
        share = (agg["brand_garments"] / wardrobe) if wardrobe > 0 else None
        rows.append(
            {
                "pin": pin,
                "area_label": geo.get("area_label") or pin,
                "demand": demand,
                "hv_demand": agg["hv_demand"],
                "orders": agg["orders"],
                "revenue": round(agg["revenue"], 2),
                "garment_pull": wardrobe,
                "brand_garments": agg["brand_garments"],
                "competitor_garments": agg["competitor_garments"],
                "brand_share": round(share, 4) if share is not None else None,
                "catchment_pop": catchment,
                "revenue_intensity": (agg["revenue"] / demand) if demand > 0 else 0.0,
                "est_footfall": est_footfall,
                "commodities": [
                    {"commodity": k, "count": v}
                    for k, v in sorted(agg["commodities"].items(), key=lambda kv: -kv[1])[:8]
                ],
            }
        )

    nd = _norm01([r["demand"] for r in rows])
    nh = _norm01([r["hv_demand"] for r in rows])
    ng = _norm01([r["brand_garments"] for r in rows])
    nc = _norm01([r["catchment_pop"] for r in rows])
    nr = _norm01([r["revenue_intensity"] for r in rows])
    ns = _norm01([r["brand_share"] or 0 for r in rows])
    for r in rows:
        catch = STORE_W["catchment"] * nc(r["catchment_pop"]) if r["demand"] > 0 else 0.0
        demand_term = HV_DEMAND_W * nh(r["hv_demand"]) + (1 - HV_DEMAND_W) * nd(r["demand"])
        score = (
            STORE_W["demand"] * demand_term
            + STORE_W["garment"] * ng(r["brand_garments"])
            + catch
            + STORE_W["revenue"] * nr(r["revenue_intensity"])
            + 0.15 * ns(r["brand_share"] or 0)
        )
        # Decided on the rounded score, so a brief that re-checks this row gets the same answer.
        r["score"] = round(score, 3)
        r["heuristic_decision"] = decision_for_row(r)
        r["decision"] = r["heuristic_decision"]
        r.pop("revenue_intensity", None)
    if stores_lookup is not None:
        gp.apply_proximity(rows, stores_lookup, pin_geo)
    rows.sort(key=lambda r: (-r["score"], -r["hv_demand"], -r["demand"], -(r["brand_share"] or 0)))

    total_g = sum(r["garment_pull"] for r in rows)
    total_own = sum(r["brand_garments"] for r in rows)
    proximity = None
    if stores_lookup is not None:
        zones = [r.get("proximity_zone") for r in rows]
        proximity = {
            "threshold_km": gp.PROXIMITY_KM,
            "store_lookup": stores_lookup.get("status"),
            "store_count": len(stores_lookup.get("stores") or []),
            "service_pins": zones.count("service"),
            "whitespace_pins": zones.count("whitespace"),
            "unverified_pins": zones.count("unverified"),
        }
    return {
        "analysis_brand": brand if role != "brand" else session_brand,
        "filters": {"pins": pin_f, "age": age_f, "gender": gen_f, "rfm": rfm_f},
        "kpis": {
            "customers": sum(r["demand"] for r in rows),
            "high_value_customers": sum(r["hv_demand"] for r in rows),
            "orders": sum(r["orders"] for r in rows),
            "revenue": round(sum(r["revenue"] for r in rows), 2),
            "garments": total_g,
        },
        "brand_share_overall": round(total_own / total_g, 4) if total_g else None,
        "pins": rows if top_n is None else rows[:top_n],
        "pin_count": len(rows),
        "top_segments": {k: _top(v, 4) for k, v in segments.items()},
        "top_colors": _top(colors, 5),
        "top_patterns": _top(patterns, 5),
        "top_sizes": _top(sizes, 5),
        "top_brands": _top(brands, 5),
        "top_brand_commodities": _top(own_commodities, 5),
        "high_value_segments": gp.HIGH_VALUE_LABEL,
        "proximity": proximity,
        "notes": "Aggregated, PII-free view. Other brands are combined as Competitors.",
    }


def brief_payload(analysis: dict[str, Any], *, focus_pin: Optional[str], role: str) -> dict[str, Any]:
    """Build a GeoBriefRequest-shaped payload from an analysis result."""
    rows = analysis.get("pins") or []
    focus = None
    if focus_pin:
        focus = next((r for r in rows if r.get("pin") == focus_pin), None)
    if focus is None and rows:
        focus = rows[0]

    def first(key: str) -> Optional[str]:
        return (analysis.get(key) or [{}])[0].get("value")

    trends = {
        "top_commodity": first("top_brand_commodities"),
        "top_color": first("top_colors"),
        "top_brand": first("top_brands"),
        "top_pattern": first("top_patterns"),
        "top_size": first("top_sizes"),
    }
    return {
        "filters": analysis.get("filters") or {},
        "kpis": analysis.get("kpis") or {},
        "focus_pin": focus,
        "top_pins": rows[:8],
        "trends": {k: v for k, v in trends.items() if v},
        "analysis_brand": analysis.get("analysis_brand"),
        "role": role,
        "proximity": analysis.get("proximity"),
    }


# ---------------------------------------------------------------------------
# Regional Stock & Assortment Routing Guide (PINs served by an existing store)
# ---------------------------------------------------------------------------


def _mix(counter: dict[str, float], n: int = 6) -> list[dict[str, Any]]:
    total = sum(counter.values())
    if total <= 0:
        return []
    return [
        {"value": k, "share": round(v / total, 3)}
        for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n]
    ]


def _size_curve(counter: dict[str, float]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    if total <= 0:
        return []

    def order(k: str) -> tuple[int, float, str]:
        u = k.strip().upper()
        if u in SIZE_ORDER:
            return (0, SIZE_ORDER.index(u), u)
        try:
            return (1, float(u), u)
        except ValueError:
            return (2, 0, u)

    return [{"size": k, "share": round(counter[k] / total, 3)} for k in sorted(counter, key=order)]


def _add_items(dst: dict[str, float], items: Any, weight: float) -> None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        val = str(item.get("value") or item.get("name") or "").strip()
        if val and val.lower() not in {"unknown", "none", "n/a", "other"}:
            dst[val] = dst.get(val, 0.0) + float(item.get("count") or 1) * weight


def routing_guide(
    metrics: dict[str, Any],
    *,
    pins: Sequence[str],
    role: str,
    session_brand: Optional[str],
    analysis_brand: Optional[str] = None,
    min_customers: Optional[int] = None,
) -> dict[str, Any]:
    """
    Aggregated personas and assortment for PINs served by an existing store. Personas are
    RFM x age x gender groups; groups below `min_customers` are merged into "Other segments" and the
    merged group is suppressed if it is still too small. No brand names besides the analysed brand.
    """
    k = min_customers or persona_min_customers()
    pin_set = set(_clean_list(pins, 50))
    customers = [c for c in (metrics.get("customer_cells") or [])
                 if isinstance(c, dict) and str(c.get("pin_code") or "") in pin_set]
    garments = [g for g in (metrics.get("garment_cells") or [])
                if isinstance(g, dict) and str(g.get("pin_code") or "") in pin_set]
    pin_geo = metrics.get("pin_geo") if isinstance(metrics.get("pin_geo"), dict) else {}
    brand = resolve_analysis_brand(garments, role=role, session_brand=session_brand, requested=analysis_brand)
    own = _norm(brand)

    def pkey(c: dict[str, Any]) -> tuple[str, str, str]:
        return (str(c.get("rfm_segment") or "Unknown").strip(), str(c.get("age_group") or "Unknown").strip(),
                str(c.get("gender") or "U").strip()[:1].upper() or "U")

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    ages: dict[str, float] = {}
    genders: dict[str, float] = {}
    rfm_mix: dict[str, float] = {}
    for c in customers:
        n = float(c.get("customers") or 0)
        g = groups.setdefault(pkey(c), {"customers": 0.0, "orders": 0.0, "revenue": 0.0,
                                         "commodity": {}, "color": {}, "size": {}})
        g["customers"] += n
        g["orders"] += float(c.get("orders") or 0)
        g["revenue"] += float(c.get("revenue") or 0)
        a, gd, r = pkey(c)[1], pkey(c)[2], pkey(c)[0]
        ages[a] = ages.get(a, 0) + n
        genders[gd] = genders.get(gd, 0) + n
        rfm_mix[r] = rfm_mix.get(r, 0) + n

    commodity: dict[str, float] = {}
    color: dict[str, float] = {}
    pattern: dict[str, float] = {}
    material: dict[str, float] = {}
    size: dict[str, float] = {}
    own_commodity: dict[str, float] = {}
    for gc in garments:
        w = HV_GARMENT_WEIGHT if is_high_value(gc.get("rfm_segment")) else 1.0
        names = gc.get("names") or gc.get("commodities")
        _add_items(commodity, names, w)
        _add_items(color, gc.get("colors"), w)
        _add_items(pattern, gc.get("patterns"), w)
        _add_items(material, gc.get("materials"), w)
        _add_items(size, gc.get("sizes"), w)
        if own and _norm(gc.get("brand")) == own:
            _add_items(own_commodity, names, 1.0)
        grp = groups.get(pkey(gc))
        if grp is not None:
            _add_items(grp["commodity"], names, 1.0)
            _add_items(grp["color"], gc.get("colors"), 1.0)
            _add_items(grp["size"], gc.get("sizes"), 1.0)

    personas: list[dict[str, Any]] = []
    other = {"customers": 0.0, "orders": 0.0, "revenue": 0.0, "groups": 0}
    for (rfm, age, gender), g in groups.items():
        if g["customers"] < k:
            other["customers"] += g["customers"]
            other["orders"] += g["orders"]
            other["revenue"] += g["revenue"]
            other["groups"] += 1
            continue
        personas.append({
            "persona": f"{rfm} · {age} · {gender}",
            "rfm_segment": rfm, "age_group": age, "gender": gender,
            "high_value": is_high_value(rfm),
            "customers": int(g["customers"]),
            "orders": int(g["orders"]),
            "avg_order_value": round(g["revenue"] / g["orders"], 2) if g["orders"] else None,
            "top_commodities": [m["value"] for m in _mix(g["commodity"], 3)],
            "top_colors": [m["value"] for m in _mix(g["color"], 3)],
            "top_sizes": [m["value"] for m in _mix(g["size"], 3)],
        })
    personas.sort(key=lambda p: (not p["high_value"], -p["customers"]))
    suppressed = 0
    if other["groups"]:
        if other["customers"] >= k:
            personas.append({
                "persona": "Other segments", "rfm_segment": None, "age_group": None, "gender": None,
                "high_value": False, "customers": int(other["customers"]), "orders": int(other["orders"]),
                "avg_order_value": round(other["revenue"] / other["orders"], 2) if other["orders"] else None,
                "merged_groups": other["groups"], "top_commodities": [], "top_colors": [], "top_sizes": [],
            })
        else:
            suppressed = int(other["customers"])

    total_cust = sum(ages.values())
    pin_rows = [
        {"pin": p, "area_label": (pin_geo.get(p) or {}).get("area_label") or p,
         "customers": int(sum(float(c.get("customers") or 0) for c in customers if c.get("pin_code") == p)),
         "high_value_customers": int(sum(float(c.get("customers") or 0) for c in customers
                                         if c.get("pin_code") == p and is_high_value(c.get("rfm_segment"))))}
        for p in sorted(pin_set)
    ]
    hv_total = sum(r["high_value_customers"] for r in pin_rows)
    top_c = [m["value"] for m in _mix(commodity, 3)]
    top_col = [m["value"] for m in _mix(color, 3)]
    curve = _size_curve(size)
    peak = [s["size"] for s in sorted(curve, key=lambda s: -s["share"])[:2]]
    actions = []
    if top_c:
        actions.append(f"Lead the floor with {', '.join(top_c)}.")
    if top_col:
        actions.append(f"Bias colour depth to {', '.join(top_col)}.")
    if peak:
        actions.append(f"Weight size runs toward {' / '.join(peak)}.")
    if hv_total:
        actions.append(f"Prioritise {gp.HIGH_VALUE_LABEL} ({hv_total} customers) with replenishment and clienteling.")
    return {
        "analysis_brand": brand if role != "brand" else session_brand,
        "pins": pin_rows,
        "customers": int(total_cust),
        "high_value_customers": hv_total,
        "personas": personas,
        "persona_min_customers": k,
        "suppressed_customers": suppressed,
        "demographics": {"age": _mix(ages, 8), "gender": _mix(genders, 3), "rfm": _mix(rfm_mix, 6)},
        "assortment": {
            "commodity": _mix(commodity), "color": _mix(color), "pattern": _mix(pattern, 5),
            "material": _mix(material, 5), "size_curve": curve,
            "brand_commodities": _mix(own_commodity, 5),
        },
        "weighting": f"{gp.HIGH_VALUE_LABEL} garments count {HV_GARMENT_WEIGHT:g}x in the assortment mix.",
        "actions": actions,
        "notes": "Aggregated segment personas only; no individual customers. Other brands are not named.",
    }
