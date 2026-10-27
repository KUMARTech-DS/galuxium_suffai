"""Product opportunity scoring (RES-01)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from suffai import METRIC_VERSION, PRIVACY_K
from suffai.privacy.suppress import suppress_cell


def _norm_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def product_opportunities(
    lines: list[dict[str, Any]],
    eligible_order_ids: set[str],
    verified_garments: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    my_brand: str,
    geo_ids: list[str] | None = None,
    k: int = PRIVACY_K,
) -> dict[str, Any]:
    """
    Score = 30% demand growth proxy + 25% underserved gap + 20% segment affinity
          + 15% breadth + 10% evidence quality (PRD §10).
    Underserved gap omitted (renormalized) when catalog empty.
    """
    # Demand from service commodity lines
    demand: dict[str, dict[str, Any]] = {}
    pins_by_comm: dict[str, set[str]] = defaultdict(set)
    cust_by_comm: dict[str, set[str]] = defaultdict(set)

    for line in lines:
        if line.get("order_id") not in eligible_order_ids:
            continue
        pin = line.get("order_pin")
        if geo_ids is not None and pin not in geo_ids:
            continue
        name = str(line.get("commodity_name") or "Unknown")
        bucket = demand.setdefault(name, {"quantity": 0})
        bucket["quantity"] += int(line.get("quantity") or 0)
        if pin:
            pins_by_comm[name].add(pin)
        if line.get("customer_id"):
            cust_by_comm[name].add(line["customer_id"])

    # Brand presence from verified garments
    brand_counts: dict[str, int] = defaultdict(int)
    for g in verified_garments:
        if geo_ids is not None and g.get("order_pin_code") not in geo_ids:
            continue
        if (g.get("brand_name_raw") or "").lower() == my_brand.lower():
            key = str(g.get("commodity_type") or "unknown")
            brand_counts[key] += 1

    has_catalog = bool(catalog)
    weights = {
        "demand": 0.30,
        "underserved": 0.25 if has_catalog else 0.0,
        "affinity": 0.20,
        "breadth": 0.15,
        "evidence": 0.10,
    }
    wsum = sum(weights.values()) or 1.0
    weights = {k: v / wsum for k, v in weights.items()}

    names = sorted(demand.keys(), key=lambda n: -demand[n]["quantity"])[:40]
    raw_demand = [float(demand[n]["quantity"]) for n in names]
    nd = _norm_scores(raw_demand)
    breadth = _norm_scores([float(len(pins_by_comm[n])) for n in names])

    opportunities = []
    for i, name in enumerate(names):
        cust_n = len(cust_by_comm[name])
        cell = suppress_cell(cust_n, demand[name]["quantity"], k=k)
        if cell["suppressed"]:
            continue

        # Affinity: inverse of my brand saturation in similar commodity token
        token = name.split()[0].lower() if name else "unknown"
        mine = brand_counts.get(token, 0) + brand_counts.get(name.lower(), 0)
        affinity = 1.0 - min(1.0, mine / max(demand[name]["quantity"], 1))

        underserved = 0.0
        if has_catalog:
            cat_hits = [
                c for c in catalog
                if token in str(c.get("category") or "").lower()
                or token in str(c.get("brand_sku") or "").lower()
            ]
            # Gap high when demand high and catalog thin
            underserved = nd[i] * (1.0 if not cat_hits else 0.3)

        evidence = 0.6 if verified_garments else 0.3
        score = 100.0 * (
            weights["demand"] * nd[i]
            + weights["underserved"] * underserved
            + weights["affinity"] * affinity
            + weights["breadth"] * breadth[i]
            + weights["evidence"] * evidence
        )
        opportunities.append({
            "rank": 0,
            "commodity": name,
            "opportunity_score": round(score, 2),
            "components": {
                "demand_growth_proxy": round(nd[i], 3),
                "underserved_gap": round(underserved, 3) if has_catalog else None,
                "segment_affinity": round(affinity, 3),
                "breadth": round(breadth[i], 3),
                "evidence_quality": evidence,
            },
            "weights": weights,
            "support_quantity": cell["value"],
            "distinct_customers": cust_n,
            "zones_touched": len(pins_by_comm[name]),
            "underserved_measured": has_catalog,
            "confidence": "medium" if verified_garments else "low",
            "note": (
                "Scores use service observation demand; garment purchase claims are not made. "
                + ("Underserved gap unmeasured (no catalog)." if not has_catalog else "")
            ),
        })

    opportunities.sort(key=lambda x: -x["opportunity_score"])
    for i, o in enumerate(opportunities[:5], start=1):
        o["rank"] = i

    return {
        "my_brand": my_brand,
        "opportunities": opportunities[:5],
        "metric_version": METRIC_VERSION,
        "catalog_skus": len(catalog),
        "verified_garments": len(verified_garments),
    }
