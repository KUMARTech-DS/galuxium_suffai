#!/usr/bin/env python3
"""Dashboard metrics payload. The Command Center builds it live from BigQuery (main.compute_metrics);
this CLI writes a snapshot file for offline inspection or test fixtures. No tab reads such a file.

Usage (local sample CSVs; PIN area names and population stay blank):
  python3 aggregate_metrics.py --out /tmp/metrics.json

Usage (BigQuery, including pin_areas):
  python3 aggregate_metrics.py --bq --project YOUR_PROJECT --out /tmp/metrics.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pin_garments  # noqa: E402
import vocab  # noqa: E402

REPO = Path(__file__).resolve().parents[2]  # AI_CUP_updated
DEFAULT_SAMPLE = REPO / "command_center_data" / "sample"

log = logging.getLogger("command_center.aggregate_metrics")


def age_group(age) -> str:
    try:
        a = int(float(age))
    except (TypeError, ValueError):
        return "unknown"
    if a < 18:
        return "<18"
    if a <= 24:
        return "18-24"
    if a <= 34:
        return "25-34"
    if a <= 44:
        return "35-44"
    if a <= 54:
        return "45-54"
    return "55+"


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_from_bq(project: str, dataset: str = "suffai_ops", *, include_repeats: bool = False):
    """customers, orders and garment items.

    Items come from the garment_items view (commodities + similarity outcome). A repeat photo of the same
    customer's garment is skipped unless include_repeats, so each real garment is counted once. Until
    migrate_bq_unified.sql has created the view, the plain commodities table is used.
    """
    from google.cloud import bigquery  # type: ignore

    client = bigquery.Client(project=project)

    def q(sql: str) -> list[dict]:
        return [dict(r) for r in client.query(sql).result()]

    def table(name: str) -> str:
        return f"`{project}.{dataset}.{name}`"

    items_sql = f"SELECT * FROM {table('garment_items')}"
    if not include_repeats:
        items_sql += " WHERE NOT IFNULL(is_repeat, FALSE)"
    try:
        items = q(items_sql)
    except Exception as exc:
        if "not found" not in str(exc).lower():
            raise
        log.warning("garment_items view missing in %s; run migrate_bq_unified.sql (using commodities)", dataset)
        items = q(f"SELECT * FROM {table('commodities')}")
    return q(f"SELECT * FROM {table('customers')}"), q(f"SELECT * FROM {table('orders')}"), items


def cell_key(pin, age_g, gender, rfm) -> tuple:
    return (str(pin or ""), str(age_g or "unknown"), str(gender or "unknown"), str(rfm or "unknown"))


def bump_count(store: dict, key: str, n: int = 1):
    store[key] = store.get(key, 0) + n


def confidence_score(row: dict):
    """Extraction confidence as a float, or None when the row was never scored."""
    raw = row.get("confidence_score")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


_AREA_FIELDS = ("area_label", "population_pin", "population_year", "area_sq_km", "pop_buffer_1km",
                "pop_buffer_2km", "pop_buffer_4km", "centroid_lat", "centroid_lon")


def pin_geo_fields(row) -> dict:
    """pin_geo fields from a BigQuery pin_areas row; only a row with status ok counts (pin_areas.py)."""
    ok = isinstance(row, dict) and row.get("status") == "ok"
    out = {k: (row.get(k) if ok else None) for k in _AREA_FIELDS}
    out["area_status"] = (row.get("status") if isinstance(row, dict) else None) or "missing"
    out["area_fetched_at"] = row.get("fetched_at") if isinstance(row, dict) else None
    return out


def build(customers, orders, commodities, pin_areas: dict) -> dict:
    """Dashboard payload. pin_areas: BigQuery pin_areas rows keyed by PIN (pin_areas.load_all)."""
    cust_by_id = {str(c["_id"]): c for c in customers}
    orders_by_cust: dict[str, list] = defaultdict(list)
    for o in orders:
        orders_by_cust[str(o["customer_id"])].append(o)

    # customer_cells: pin × age × gender × rfm
    customer_cells: dict[tuple, dict] = {}
    for c in customers:
        cid = str(c["_id"])
        key = cell_key(c.get("pin_code"), age_group(c.get("age")), c.get("gender"), c.get("rfm_segment"))
        cell = customer_cells.setdefault(
            key,
            {
                "pin_code": key[0],
                "age_group": key[1],
                "gender": key[2],
                "rfm_segment": key[3],
                "customers": 0,
                "orders": 0,
                "revenue": 0.0,
                "clothes": 0,
                "lat_sum": 0.0,
                "lon_sum": 0.0,
                "coord_n": 0,
                "services": {},
                "orders_by_month": {},
            },
        )
        cell["customers"] += 1
        try:
            lat, lon = float(c["lat"]), float(c["lon"])
            cell["lat_sum"] += lat
            cell["lon_sum"] += lon
            cell["coord_n"] += 1
        except (TypeError, ValueError, KeyError):
            pass

        for o in orders_by_cust.get(cid, []):
            cell["orders"] += 1
            try:
                cell["revenue"] += float(o.get("amount") or 0)
            except (TypeError, ValueError):
                pass
            try:
                cell["clothes"] += int(float(o.get("total_number_of_clothes") or 0))
            except (TypeError, ValueError):
                pass
            svc = str(o.get("service") or "unknown")
            bump_count(cell["services"], svc)
            month = str(o.get("scheduled_date") or "")[:7] or "unknown"
            bump_count(cell["orders_by_month"], month)

    # garment_cells: same dimensions + brand, retained for brand-level drilldowns.
    garment_cells: dict[tuple, dict] = {}
    # garment_segment_cells: exact geo × age_group × gender × RFM cells pooled across brands.
    # The UI can roll these up over any unselected dimension, e.g. geo × age with all genders/RFM.
    garment_segment_cells: dict[tuple, dict] = {}
    # Photographed garments per customer's current PIN: the garments compare-pins ranks against.
    photo_garments = pin_garments.by_pin(
        ((str(g.get("customer_id")), pin_garments.garment_id(g)) for g in commodities),
        {cid: str(c.get("pin_code") or "") for cid, c in cust_by_id.items()},
    )
    for g in commodities:
        c = cust_by_id.get(str(g.get("customer_id")))
        if not c:
            continue
        brand = str(g.get("brand") or "unknown")
        key = cell_key(c.get("pin_code"), age_group(c.get("age")), c.get("gender"), c.get("rfm_segment")) + (brand,)
        cell = garment_cells.setdefault(
            key,
            {
                "pin_code": key[0],
                "age_group": key[1],
                "gender": key[2],
                "rfm_segment": key[3],
                "brand": brand,
                "garments": 0,
                "sizes": {},
                "colors": {},
                "patterns": {},
                "materials": {},
                "sleeve_types": {},
                "collar_types": {},
                "names": {},
                "confidence_sum": 0.0,
                "confidence_n": 0,
                "low_confidence_n": 0,
            },
        )
        cell["garments"] += 1
        bump_count(cell["sizes"], str(g.get("size") or "unknown"))
        bump_count(cell["colors"], str(g.get("color") or "unknown"))
        bump_count(cell["patterns"], str(g.get("pattern") or "unknown"))
        bump_count(cell["materials"], str(g.get("material") or "unknown"))
        bump_count(cell["sleeve_types"], str(g.get("sleeve_type") or "unknown"))
        bump_count(cell["collar_types"], str(g.get("collar_type") or "unknown"))
        bump_count(cell["names"], str(g.get("name") or "unknown"))
        score = confidence_score(g)
        if score is not None:
            cell["confidence_sum"] += score
            cell["confidence_n"] += 1
            if score < 0.8:
                cell["low_confidence_n"] += 1

        # Pool the same garment observation into the four-dimension segment row.
        seg_key = cell_key(c.get("pin_code"), age_group(c.get("age")), c.get("gender"), c.get("rfm_segment"))
        seg = garment_segment_cells.setdefault(
            seg_key,
            {
                "pin_code": seg_key[0], "age_group": seg_key[1], "gender": seg_key[2], "rfm_segment": seg_key[3],
                "garments": 0, "brands": {}, "sizes": {}, "colors": {}, "patterns": {}, "materials": {},
                "sleeve_types": {}, "collar_types": {}, "names": {},
                "confidence_sum": 0.0, "confidence_n": 0, "low_confidence_n": 0,
            },
        )
        seg["garments"] += 1
        bump_count(seg["brands"], brand)
        bump_count(seg["sizes"], str(g.get("size") or "unknown"))
        bump_count(seg["colors"], str(g.get("color") or "unknown"))
        bump_count(seg["patterns"], str(g.get("pattern") or "unknown"))
        bump_count(seg["materials"], str(g.get("material") or "unknown"))
        bump_count(seg["sleeve_types"], str(g.get("sleeve_type") or "unknown"))
        bump_count(seg["collar_types"], str(g.get("collar_type") or "unknown"))
        bump_count(seg["names"], str(g.get("name") or "unknown"))
        score = confidence_score(g)
        if score is not None:
            seg["confidence_sum"] += score
            seg["confidence_n"] += 1
            if score < 0.8:
                seg["low_confidence_n"] += 1

    def finalize_customer(cell: dict) -> dict:
        n = cell.pop("coord_n")
        lat_sum = cell.pop("lat_sum")
        lon_sum = cell.pop("lon_sum")
        return {
            **{k: cell[k] for k in ("pin_code", "age_group", "gender", "rfm_segment", "customers", "orders", "revenue", "clothes")},
            "centroid_lat": round(lat_sum / n, 5) if n else None,
            "centroid_lon": round(lon_sum / n, 5) if n else None,
            "services": [{"value": k, "count": v} for k, v in sorted(cell["services"].items(), key=lambda x: -x[1])],
            "orders_by_month": [{"month": k, "orders": v} for k, v in sorted(cell["orders_by_month"].items())],
        }

    def finalize_garment(cell: dict) -> dict:
        return {
            **{k: cell[k] for k in ("pin_code", "age_group", "gender", "rfm_segment", "brand", "garments")},
            "sizes": [{"value": k, "count": v} for k, v in sorted(cell["sizes"].items(), key=lambda x: -x[1])],
            "colors": [{"value": k, "count": v} for k, v in sorted(cell["colors"].items(), key=lambda x: -x[1])],
            "patterns": [{"value": k, "count": v} for k, v in sorted(cell["patterns"].items(), key=lambda x: -x[1])],
            "materials": [{"value": k, "count": v} for k, v in sorted(cell["materials"].items(), key=lambda x: -x[1])],
            "sleeve_types": [{"value": k, "count": v} for k, v in sorted(cell["sleeve_types"].items(), key=lambda x: -x[1])],
            "collar_types": [{"value": k, "count": v} for k, v in sorted(cell["collar_types"].items(), key=lambda x: -x[1])],
            "names": [{"value": k, "count": v} for k, v in sorted(cell["names"].items(), key=lambda x: -x[1])[:10]],
            "confidence_sum": round(cell["confidence_sum"], 4),
            "confidence_n": cell["confidence_n"],
            "low_confidence_n": cell["low_confidence_n"],
        }

    def finalize_segment_garment(cell: dict) -> dict:
        return {
            **{k: cell[k] for k in ("pin_code", "age_group", "gender", "rfm_segment", "garments")},
            "brands": [{"value": k, "count": v} for k, v in sorted(cell["brands"].items(), key=lambda x: -x[1])],
            "sizes": [{"value": k, "count": v} for k, v in sorted(cell["sizes"].items(), key=lambda x: -x[1])],
            "colors": [{"value": k, "count": v} for k, v in sorted(cell["colors"].items(), key=lambda x: -x[1])],
            "patterns": [{"value": k, "count": v} for k, v in sorted(cell["patterns"].items(), key=lambda x: -x[1])],
            "materials": [{"value": k, "count": v} for k, v in sorted(cell["materials"].items(), key=lambda x: -x[1])],
            "sleeve_types": [{"value": k, "count": v} for k, v in sorted(cell["sleeve_types"].items(), key=lambda x: -x[1])],
            "collar_types": [{"value": k, "count": v} for k, v in sorted(cell["collar_types"].items(), key=lambda x: -x[1])],
            "names": [{"value": k, "count": v} for k, v in sorted(cell["names"].items(), key=lambda x: -x[1])[:10]],
            "confidence_sum": round(cell["confidence_sum"], 4),
            "confidence_n": cell["confidence_n"],
            "low_confidence_n": cell["low_confidence_n"],
        }

    cust_list = [finalize_customer(c) for c in customer_cells.values()]
    garm_list = [finalize_garment(c) for c in garment_cells.values()]
    segment_garm_list = [finalize_segment_garment(c) for c in garment_segment_cells.values()]

    # PIN hubs for the map: PINs with customers only; name, population and centroid from pin_areas
    # (customer average centroid until the PIN has been looked up).
    pin_agg: dict[str, dict] = {}
    for c in cust_list:
        pin = c["pin_code"]
        hub = pin_agg.setdefault(pin, {"lat_sum": 0.0, "lon_sum": 0.0, "n": 0, "customers": 0, "orders": 0, "revenue": 0.0})
        if c["centroid_lat"] is not None:
            hub["lat_sum"] += c["centroid_lat"] * c["customers"]
            hub["lon_sum"] += c["centroid_lon"] * c["customers"]
            hub["n"] += c["customers"]
        hub["customers"] += c["customers"]
        hub["orders"] += c["orders"]
        hub["revenue"] += c["revenue"]

    pin_geo = {}
    for pin, hub in pin_agg.items():
        if not pin or pin.startswith("_"):
            continue
        area = pin_geo_fields((pin_areas or {}).get(pin))
        from_area = area["centroid_lat"] is not None and area["centroid_lon"] is not None
        pin_geo[pin] = {
            **area,
            "area_label": area["area_label"] or pin,
            "centroid_lat": area["centroid_lat"] if from_area else (round(hub["lat_sum"] / hub["n"], 5) if hub["n"] else None),
            "centroid_lon": area["centroid_lon"] if from_area else (round(hub["lon_sum"] / hub["n"], 5) if hub["n"] else None),
            "centroid_source": "pin_areas" if from_area else ("customers" if hub["n"] else None),
            "customers": hub["customers"],
            "orders": hub["orders"],
            "revenue": round(hub["revenue"], 2),
            "photo_garments": min(len(photo_garments.get(pin, ())), pin_garments.cap()),
        }

    brands = sorted({g["brand"] for g in garm_list if not vocab.is_unknown(g["brand"])})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "pin_codes": sorted(pin_geo.keys()),
            "age_groups": sorted({c["age_group"] for c in cust_list}),
            "genders": sorted({c["gender"] for c in cust_list}),
            "rfm_segments": sorted({c["rfm_segment"] for c in cust_list}),
            "brands": brands,
        },
        "pin_geo": pin_geo,
        # Exact audience counts by all four dimensions. Browser rollups may sum over unselected fields.
        "customer_cells": cust_list,
        "audience_cells": cust_list,
        # Brand-level detail is retained; segment-level profile pools across brands so any combination
        # of selected filters (e.g. geo + age with all genders/RFM) can be matched and aggregated.
        "garment_cells": garm_list,
        "garment_segment_cells": segment_garm_list,
        "aggregation_meta": {
            "audience_dimensions": ["pin_code", "age_group", "gender", "rfm_segment"],
            "garment_dimensions": ["pin_code", "age_group", "gender", "rfm_segment"],
            "garment_profile_pools_brands": True,
            "supports_rollups_over_unselected_dimensions": True,
            "age_group_definition": ["<18", "18-24", "25-34", "35-44", "45-54", "55+"],
            "note": "A missing cell means no source observations for that exact cohort; it must be shown as a transparent fallback, not fabricated as exact data."
        },
    }


def main():
    p = argparse.ArgumentParser(description="Aggregate metrics for the command-center dashboard")
    p.add_argument("--customers", type=Path, default=DEFAULT_SAMPLE / "customers.csv")
    p.add_argument("--orders", type=Path, default=DEFAULT_SAMPLE / "orders.csv")
    p.add_argument("--commodities", type=Path, default=DEFAULT_SAMPLE / "commodities.csv")
    p.add_argument("--out", type=Path, required=True, help="Where to write the snapshot JSON")
    p.add_argument("--bq", action="store_true", help="Read tables from BigQuery instead of CSV")
    p.add_argument("--project", default="", help="GCP project id (required with --bq)")
    p.add_argument("--dataset", default="suffai_ops")
    args = p.parse_args()

    areas: dict = {}
    if args.bq:
        if not args.project:
            raise SystemExit("--project is required with --bq")
        customers, orders, commodities = load_from_bq(args.project, args.dataset)
        import pin_areas

        areas = pin_areas.load_all(args.project, args.dataset)
    else:
        customers = read_csv(args.customers)
        orders = read_csv(args.orders)
        commodities = read_csv(args.commodities)

    payload = build(customers, orders, commodities, areas)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.out} ({len(payload['customer_cells'])} audience cells, {len(payload['garment_cells'])} brand cells, {len(payload['garment_segment_cells'])} segment garment cells)")


if __name__ == "__main__":
    main()
