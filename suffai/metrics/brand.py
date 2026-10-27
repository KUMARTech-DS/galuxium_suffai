"""Verified garment brand benchmarks (BRD-01)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from suffai import METRIC_VERSION, PRIVACY_K
from suffai.privacy.suppress import suppress_cell


def brand_benchmarks(
    verified_garments: list[dict[str, Any]],
    *,
    my_brand: str,
    geo_ids: list[str] | None = None,
    k: int = PRIVACY_K,
) -> dict[str, Any]:
    rows = verified_garments
    if geo_ids:
        rows = [g for g in rows if (g.get("order_pin_code") or "UNKNOWN") in geo_ids]

    by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in rows:
        name = g.get("brand_name_raw") or g.get("brand_id") or "unknown"
        if name == "unknown":
            continue
        by_brand[name].append(g)

    total = sum(len(v) for v in by_brand.values()) or 0
    customers = {g.get("source_customer_id") for g in rows if g.get("source_customer_id")}
    cell = suppress_cell(len(customers), total, k=k)

    benches = []
    for brand, items in sorted(by_brand.items(), key=lambda x: -len(x[1])):
        n = len(items)
        cust = {i.get("source_customer_id") for i in items if i.get("source_customer_id")}
        c = suppress_cell(len(cust), n, k=k)
        benches.append({
            "brand": brand,
            "observed_garments": c["value"],
            "distinct_customers": None if c["suppressed"] else len(cust),
            "share_of_verified_pct": None if cell["suppressed"] or not total else round(100.0 * n / total, 2),
            "suppressed": c["suppressed"],
            "is_my_brand": brand.lower() == my_brand.lower(),
            "note": "Share of verified consented garment observations — not market sales share.",
        })

    # Attribute mix for my brand
    mine = [g for g in rows if (g.get("brand_name_raw") or "").lower() == my_brand.lower()]
    def attr_mix(field: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        for g in mine:
            counts[str(g.get(field) or "unknown")] += 1
        return [{"value": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]

    return {
        "my_brand": my_brand,
        "benchmarks": benches,
        "cohort_garments": cell["value"],
        "cohort_customers": None if cell["suppressed"] else len(customers),
        "suppressed": cell["suppressed"],
        "my_attribute_mix": {
            "commodity_type": attr_mix("commodity_type"),
            "material_type": attr_mix("material_type"),
            "primary_color": attr_mix("primary_color"),
            "size_normalized": attr_mix("size_normalized"),
        },
        "metric_version": METRIC_VERSION,
        "definition": (
            "Competitive share = share of verified consented reviewed garment observations "
            "with identifiable brand. Not laundry volume or purchase market share."
        ),
    }
