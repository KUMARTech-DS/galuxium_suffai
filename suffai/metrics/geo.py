"""Geo intelligence aggregates from service observations (GEO-01)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from suffai import METRIC_VERSION, PRIVACY_K
from suffai.metrics.eligibility import eligible_orders
from suffai.privacy.suppress import suppress_cell


# Demo area labels for Bengaluru PINs (display only; not invented when PIN missing)
PIN_AREA_LABELS = {
    "560102": "HSR Layout",
    "560103": "Bellandur",
    "560034": "Koramangala",
    "560068": "BTM Layout",
    "560035": "Sarjapur Road",
    "560087": "Marathahalli",
    "560037": "Brookefield",
    "560066": "Whitefield",
    "560048": "Mahadevapura",
    "560078": "JP Nagar",
    "560099": "Electronic City",
    "560119": "Bannerghatta Road",
}


def _in_range(ts: datetime | None, date_from: datetime | None, date_to: datetime | None) -> bool:
    if ts is None:
        return False
    if date_from and ts < date_from:
        return False
    if date_to and ts > date_to:
        return False
    return True


def geo_active_customers(
    orders: list[dict[str, Any]],
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    k: int = PRIVACY_K,
) -> list[dict[str, Any]]:
    """Distinct eligible customers per order PIN (not census density)."""
    eligible = eligible_orders(orders)
    by_pin: dict[str | None, set[str]] = defaultdict(set)
    for o in eligible:
        if not _in_range(o.get("ordered_at"), date_from, date_to):
            continue
        pin = o.get("order_pin")  # None → Unknown; never invent
        cid = o.get("customer_id")
        if cid:
            by_pin[pin].add(cid)

    rows = []
    for pin, cids in sorted(by_pin.items(), key=lambda x: (-len(x[1]), str(x[0]))):
        n = len(cids)
        suppressed = n < k and pin is not None  # still report Unknown totals
        rows.append({
            "geo_key": pin or "UNKNOWN",
            "pin_code": pin,
            "area": PIN_AREA_LABELS.get(pin or "", "Unknown" if pin is None else pin),
            "active_customers": None if (suppressed and pin is not None) else n,
            "suppressed": suppressed and pin is not None,
            "denominator": n,
            "metric_key": "active_customers",
            "metric_version": METRIC_VERSION,
        })
    return rows


def commodity_popularity(
    lines: list[dict[str, Any]],
    eligible_order_ids: set[str],
    *,
    pin_codes: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    k: int = PRIVACY_K,
) -> list[dict[str, Any]]:
    """Sum ordered quantities — service observations, not purchases."""
    by_name: dict[str, dict[str, Any]] = {}
    customers: dict[str, set[str]] = defaultdict(set)

    for line in lines:
        if line.get("order_id") not in eligible_order_ids:
            continue
        if not _in_range(line.get("ordered_at"), date_from, date_to):
            continue
        pin = line.get("order_pin")
        if pin_codes is not None and pin not in pin_codes:
            continue
        name = str(line.get("commodity_name") or "Unknown")
        bucket = by_name.setdefault(name, {"quantity": 0, "lines": 0})
        bucket["quantity"] += int(line.get("quantity") or 0)
        bucket["lines"] += 1
        if line.get("customer_id"):
            customers[name].add(line["customer_id"])

    rows = []
    for name, agg in sorted(by_name.items(), key=lambda x: -x[1]["quantity"]):
        n_cust = len(customers[name])
        cell = suppress_cell(n_cust, agg["quantity"], k=k)
        rows.append({
            "commodity_name": name,
            "quantity": cell["value"],
            "line_count": agg["lines"],
            "distinct_customers": cell["cohort_size"],
            "suppressed": cell["suppressed"],
            "metric_key": "commodity_popularity",
            "metric_version": METRIC_VERSION,
            "note": "Service observation quantity, not garment purchase count.",
        })
    return rows


def geo_query(
    orders: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    rfm_snapshots: list[dict[str, Any]] | None = None,
    *,
    geo_ids: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    rfm_segments: list[str] | None = None,
    k: int = PRIVACY_K,
) -> dict[str, Any]:
    """Governed geo response: tiles + commodity breakdown + RFM mix."""
    eligible = eligible_orders(orders)
    eligible_ids = {o["order_id"] for o in eligible}
    segment_by_cust = {
        s["customer_id"]: s["rfm_segment"] for s in (rfm_snapshots or [])
    }

    if rfm_segments:
        allowed_cust = {cid for cid, seg in segment_by_cust.items() if seg in rfm_segments}
        eligible = [o for o in eligible if o.get("customer_id") in allowed_cust]
        eligible_ids = {o["order_id"] for o in eligible}

    tiles = geo_active_customers(eligible, date_from=date_from, date_to=date_to, k=k)
    if geo_ids:
        tiles = [t for t in tiles if t["geo_key"] in geo_ids or t["pin_code"] in geo_ids]

    pins = geo_ids
    commodities = commodity_popularity(
        lines, eligible_ids, pin_codes=pins, date_from=date_from, date_to=date_to, k=k
    )

    # RFM mix in selected geos
    rfm_counts: dict[str, set[str]] = defaultdict(set)
    for o in eligible:
        if not _in_range(o.get("ordered_at"), date_from, date_to):
            continue
        pin = o.get("order_pin") or "UNKNOWN"
        if geo_ids and pin not in geo_ids and "UNKNOWN" not in geo_ids:
            continue
        cid = o.get("customer_id")
        if not cid:
            continue
        seg = segment_by_cust.get(cid, "Unknown")
        rfm_counts[seg].add(cid)

    rfm_mix = []
    for seg, cids in sorted(rfm_counts.items(), key=lambda x: -len(x[1])):
        n = len(cids)
        rfm_mix.append({
            "rfm_segment": seg,
            "customers": None if n < k else n,
            "suppressed": n < k,
        })

    mapped = sum(t["denominator"] for t in tiles if t["geo_key"] != "UNKNOWN")
    unknown = next((t["denominator"] for t in tiles if t["geo_key"] == "UNKNOWN"), 0)

    return {
        "tiles": tiles,
        "commodities": commodities[:50],
        "rfm_mix": rfm_mix,
        "meta": {
            "metric_version": METRIC_VERSION,
            "privacy_k": k,
            "mapped_active_customers": mapped,
            "unknown_pin_customers": unknown,
            "eligible_orders": len(eligible_ids),
            "coverage_note": (
                "Material/color/size/brand distributions require consented "
                "reviewed garment observations — empty until IMG pipeline data exists."
            ),
        },
    }
