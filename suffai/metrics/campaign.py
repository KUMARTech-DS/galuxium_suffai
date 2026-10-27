"""Campaign image → geo × RFM fit ranking (CMP-01)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from suffai import METRIC_VERSION, PRIVACY_K
from suffai.privacy.suppress import suppress_cell


def _extract_tags(metadata: dict[str, Any]) -> dict[str, Any]:
    attrs = metadata.get("extracted_attributes") or metadata.get("attributes") or metadata
    return {
        "commodity_type": str(attrs.get("commodity_type") or attrs.get("commodity") or "unknown").lower(),
        "primary_color": str(attrs.get("primary_color") or attrs.get("color") or "unknown").lower(),
        "material_type": str(attrs.get("material_type") or attrs.get("material") or "unknown").lower(),
        "brand_name": str(attrs.get("brand_name") or attrs.get("brand") or "unknown"),
    }


def evaluate_campaign_fit(
    *,
    campaign_id: str,
    metadata: dict[str, Any],
    lines: list[dict[str, Any]],
    eligible_order_ids: set[str],
    rfm_snapshots: list[dict[str, Any]],
    my_brand: str,
    k: int = PRIVACY_K,
) -> list[dict[str, Any]]:
    """
    Fit = 30% category + 20% color/material + 20% geo demand + 15% segment
        + 10% seasonality proxy + 5% coverage (PRD §11).
    """
    tags = _extract_tags(metadata)
    seg_by_cust = {s["customer_id"]: s["rfm_segment"] for s in rfm_snapshots}

    # Aggregate demand by pin × segment × commodity token
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        if line.get("order_id") not in eligible_order_ids:
            continue
        pin = line.get("order_pin") or "UNKNOWN"
        cid = line.get("customer_id")
        seg = seg_by_cust.get(cid, "Unknown") if cid else "Unknown"
        key = (pin, seg)
        b = buckets.setdefault(key, {"qty": 0, "customers": set(), "names": defaultdict(int)})
        b["qty"] += int(line.get("quantity") or 0)
        if cid:
            b["customers"].add(cid)
        name = str(line.get("commodity_name") or "").lower()
        b["names"][name] += int(line.get("quantity") or 0)

    # Geo demand index
    pin_totals: dict[str, int] = defaultdict(int)
    for (pin, _), b in buckets.items():
        pin_totals[pin] += b["qty"]
    max_pin = max(pin_totals.values()) if pin_totals else 1

    evaluations = []
    commodity_token = tags["commodity_type"]
    color = tags["primary_color"]
    material = tags["material_type"]

    for (pin, seg), b in buckets.items():
        n_cust = len(b["customers"])
        cell = suppress_cell(n_cust, b["qty"], k=k)
        if cell["suppressed"]:
            evaluations.append({
                "segment_key": f"{pin}|{seg}",
                "pin_code": pin,
                "rfm_segment": seg,
                "fit_score": None,
                "suppressed": True,
                "support_count": n_cust,
                "explanation": f"Suppressed: cohort below k={k}",
                "components": None,
            })
            continue

        # Category affinity: share of qty whose name contains token
        cat_qty = sum(q for name, q in b["names"].items() if commodity_token != "unknown" and commodity_token in name)
        cat_aff = cat_qty / max(b["qty"], 1)
        # Color/material: weak proxy from name tokens
        cm_qty = sum(
            q for name, q in b["names"].items()
            if (color != "unknown" and color in name) or (material != "unknown" and material in name)
        )
        cm_aff = cm_qty / max(b["qty"], 1)
        geo_idx = pin_totals[pin] / max_pin
        # Segment affinity: Champions/Loyal get slight boost for brand campaigns
        seg_aff = 0.8 if seg in {"Champions", "Loyal", "Potential Loyalist"} else 0.5
        seasonality = 0.5  # no calendar labels in fixtures
        coverage = min(1.0, n_cust / float(k * 2))

        score = 100.0 * (
            0.30 * cat_aff
            + 0.20 * cm_aff
            + 0.20 * geo_idx
            + 0.15 * seg_aff
            + 0.10 * seasonality
            + 0.05 * coverage
        )
        evaluations.append({
            "segment_key": f"{pin}|{seg}",
            "pin_code": pin,
            "rfm_segment": seg,
            "fit_score": round(score, 2),
            "suppressed": False,
            "support_count": n_cust,
            "support_quantity": b["qty"],
            "components": {
                "category_affinity": round(cat_aff, 3),
                "color_material_affinity": round(cm_aff, 3),
                "geo_demand_index": round(geo_idx, 3),
                "segment_affinity": round(seg_aff, 3),
                "seasonality": seasonality,
                "coverage": round(coverage, 3),
            },
            "explanation": (
                f"Fit for {my_brand} creative tagged {commodity_token}/{color}/{material} "
                f"in {pin} × {seg}. Relevance score only — not predicted conversion."
            ),
            "campaign_id": campaign_id,
            "metric_version": METRIC_VERSION,
            "creative_tags": tags,
        })

    evaluations.sort(key=lambda e: (-1 if e["fit_score"] is None else -e["fit_score"]))
    return evaluations
