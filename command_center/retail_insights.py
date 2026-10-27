"""Links between the retail suite and SuffAI intelligence.

opening_plan     Geo demand for a PIN (aggregated garment cells) -> an opening stock mix for a new store,
                 drawn from what a warehouse actually has.
campaign_check   Campaign PINs and SKUs -> stock at the stores in those PINs and actual POS sales in the
                 campaign window against the window before it.
snapshot         Brand-level operating numbers for the overview and AURA (no customer data).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional, Sequence

import geo_analysis as ga
import retail_db as db
import retail_gst as gst
import retail_stock as stock
import retail_valid as v
import retail_wms as wms
import vocab
from retail_db import RetailError
from retail_scope import Scope

log = logging.getLogger("command_center.retail_insights")

FLOOR_SHARE = 0.03       # categories with no demand signal still get a little range
FLOOR_TOTAL = 0.20       # ... but together never more than this
EPS = 0.02               # colours / sizes not seen in demand keep a small weight
MAX_PLAN_UNITS = 50_000
ACTUAL_LABEL = "Actual (POS)"


# ---------------------------------------------------------------------------
# Demand from the Geo metrics payload
# ---------------------------------------------------------------------------


def _garments_for(metrics: dict[str, Any], pins: set[str]) -> list[dict[str, Any]]:
    return [g for g in (metrics.get("garment_cells") or [])
            if isinstance(g, dict) and str(g.get("pin_code") or "") in pins]


def demand_mix(metrics: dict[str, Any], pin_code: str) -> dict[str, Any]:
    """Commodity, colour and size shares around a PIN. Uses the PIN itself, else every PIN in the same
    3-digit postal district. High-value segments count HV_GARMENT_WEIGHT times, as in Geo."""
    pin = str(pin_code or "").strip()
    cells, basis, pins = _garments_for(metrics, {pin}), "pin", [pin]
    if not cells and len(pin) == 6:
        near = sorted({str(g.get("pin_code") or "") for g in (metrics.get("garment_cells") or [])
                       if isinstance(g, dict) and str(g.get("pin_code") or "")[:3] == pin[:3]})
        cells, basis, pins = _garments_for(metrics, set(near)), "district", near
    if not cells:
        return {"basis": "none", "pins": [], "garments": 0, "commodity": {}, "color": {}, "size": {}}
    com: dict[str, float] = {}
    col: dict[str, float] = {}
    siz: dict[str, float] = {}
    labels: dict[str, str] = {}
    for g in cells:
        w = ga.HV_GARMENT_WEIGHT if ga.is_high_value(g.get("rfm_segment")) else 1.0
        for dim, dst, items in (("commodity", com, g.get("names") or g.get("commodities")),
                                ("color", col, g.get("colors")), ("size", siz, g.get("sizes"))):
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                raw = str(it.get("value") or it.get("name") or "").strip()
                key = raw.upper() if dim == "size" else vocab.attr_key(dim, raw)
                if not key or vocab.is_unknown(raw):
                    continue
                dst[key] = dst.get(key, 0.0) + float(it.get("count") or 1) * w
                labels.setdefault(f"{dim}:{key}", raw)

    def share(d: dict[str, float]) -> dict[str, float]:
        tot = sum(d.values())
        return {k: x / tot for k, x in d.items()} if tot > 0 else {}

    return {"basis": basis, "pins": pins[:50], "garments": int(sum(float(g.get("garments") or 0) for g in cells)),
            "commodity": share(com), "color": share(col), "size": share(siz), "labels": labels}


def _top(shares: dict[str, float], labels: dict[str, str], dim: str, n: int = 6) -> list[dict[str, Any]]:
    return [{"value": labels.get(f"{dim}:{k}", k), "share": round(s, 3)}
            for k, s in sorted(shares.items(), key=lambda kv: -kv[1])[:n]]


def _category_demand(category: str, names: Sequence[str], commodity: dict[str, float]) -> float:
    """Share of demand a catalog category answers: commodity keys equal to, inside or containing it."""
    keys = {vocab.attr_key("commodity", category)} | {vocab.attr_key("commodity", n) for n in names}
    keys.discard("")
    total = 0.0
    for ck, s in commodity.items():
        if any(ck == k or (len(ck) >= 3 and ck in k) or (len(k) >= 3 and k in ck) for k in keys):
            total += s
    return total


def largest_remainder(weights: dict[str, float], total: int) -> dict[str, int]:
    tot = sum(weights.values())
    if total <= 0 or tot <= 0:
        return {k: 0 for k in weights}
    exact = {k: total * w / tot for k, w in weights.items()}
    out = {k: int(x) for k, x in exact.items()}
    left = total - sum(out.values())
    for k in sorted(exact, key=lambda k: -(exact[k] - out[k]))[:left]:
        out[k] += 1
    return out


def capped_allocation(weights: dict[str, float], total: int, caps: dict[str, int]) -> dict[str, int]:
    """largest_remainder where no key gets more than its cap; what a capped key can't take is shared
    among the others by weight. Allocates min(total, sum of caps)."""
    out = {k: 0 for k in weights}
    open_keys = [k for k in weights if caps.get(k, 0) > 0]
    left = total
    while left > 0 and open_keys:
        share = largest_remainder({k: weights[k] for k in open_keys}, left)
        if not any(share.values()):
            break
        for k, q in share.items():
            give = min(q, caps[k] - out[k])
            out[k] += give
            left -= give
        open_keys = [k for k in open_keys if out[k] < caps[k]]
    return out


# ---------------------------------------------------------------------------
# Opening stock plan
# ---------------------------------------------------------------------------


def opening_plan(scope: Scope, metrics: dict[str, Any], *, pin_code: str, units: int,
                 source_location_id: str) -> dict[str, Any]:
    scope.require("read")
    source_location_id = scope.default_location(source_location_id) or ""
    pin = v.pin_code(pin_code, required=True)
    units = v.qty(units, "Units", lo=1, hi=MAX_PLAN_UNITS)
    mix = demand_mix(metrics, pin)
    with db.tx() as t:
        src = stock.location(t, scope, source_location_id, kind="warehouse")
        rows = t.all("SELECT vv.variant_id, vv.sku, vv.color, vv.size, vv.mrp_paise, p.name, p.category, p.style_code "
                     "FROM retail_variants vv JOIN retail_products p ON p.product_id = vv.product_id "
                     "WHERE vv.brand_key = %s AND vv.active = TRUE AND p.active = TRUE", (scope.brand_key,))
        avail = stock.sellable(t, src["location_id"], [r["variant_id"] for r in rows])
        store = t.one("SELECT location_id, code, name FROM retail_locations WHERE brand_key = %s AND kind = 'store' "
                      "AND pin_code = %s AND active = TRUE ORDER BY code LIMIT 1", (scope.brand_key, pin))
    rows = [r for r in rows if avail.get(r["variant_id"], 0) > 0]
    if not rows:
        raise RetailError(f"{src['name']} has no sellable stock to plan from. Receive stock there first.", 409)

    cats: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    demand = {c: _category_demand(c, sorted({r["name"] for r in rs}), mix["commodity"]) for c, rs in cats.items()}
    seen = {c: d for c, d in demand.items() if d > 0}
    unseen = [c for c in cats if c not in seen]
    if seen:
        floor = min(FLOOR_SHARE, FLOOR_TOTAL / len(unseen)) if unseen else 0.0
        scale = (1.0 - floor * len(unseen)) / sum(seen.values())
        cat_share = {**{c: d * scale for c, d in seen.items()}, **{c: floor for c in unseen}}
    else:
        cat_share = {c: 1.0 / len(cats) for c in cats}

    cat_units = largest_remainder(cat_share, units)
    lines, planned = [], 0
    for c, rs in cats.items():
        inner = {}
        for r in rs:
            cw = mix["color"].get(vocab.attr_key("color", r["color"]), 0.0) + EPS
            sw = mix["size"].get(str(r["size"]).upper(), 0.0) + EPS
            inner[r["variant_id"]] = cw * sw
        give = capped_allocation(inner, cat_units[c], {r["variant_id"]: avail.get(r["variant_id"], 0) for r in rs})
        for r in rs:
            q = give[r["variant_id"]]
            if q <= 0:
                continue
            planned += q
            lines.append({"variant_id": r["variant_id"], "sku": r["sku"], "name": r["name"],
                          "style_code": r["style_code"], "category": r["category"], "color": r["color"],
                          "size": r["size"], "qty": q, "available": avail.get(r["variant_id"], 0),
                          "mrp": gst.rupees(r["mrp_paise"])})
    short = units - planned
    lines.sort(key=lambda x: (-cat_share.get(x["category"], 0), x["category"], -x["qty"], x["sku"]))
    by_cat = [{"category": c, "share": round(cat_share[c], 3), "demand_seen": c in seen,
               "units": sum(x["qty"] for x in lines if x["category"] == c)}
              for c in sorted(cat_share, key=lambda c: -cat_share[c])]
    basis_note = {"pin": f"Demand from PIN {pin}.",
                  "district": f"No garments recorded in {pin}; used the {len(mix['pins'])} PINs of district {pin[:3]}.",
                  "none": "No Geo demand near this PIN; the mix is spread evenly across your categories."}[mix["basis"]]
    return {
        "pin_code": pin, "source": {"location_id": src["location_id"], "code": src["code"], "name": src["name"]},
        "store": store, "units_requested": units, "units_planned": planned, "units_short": short,
        "lines": [x for x in lines if x["qty"] > 0], "categories": by_cat,
        "demand": {"basis": mix["basis"], "note": basis_note, "garments": mix["garments"],
                   "top_commodities": _top(mix["commodity"], mix.get("labels", {}), "commodity"),
                   "top_colors": _top(mix["color"], mix.get("labels", {}), "color"),
                   "size_curve": [{"size": s["value"], "share": s["share"]} for s in
                                  _top(mix["size"], mix.get("labels", {}), "size", 12)]},
        "method": (f"Category share follows Geo demand ({ga.HV_GARMENT_WEIGHT:g}x weight for high-value segments); "
                   f"colour and size within a category follow the area's colour mix and size curve. Categories "
                   f"with no demand signal keep up to {int(FLOOR_SHARE * 100)}% each. When the warehouse runs out "
                   f"of a SKU, its units go to the next best sizes and colours of the same category."),
    }


def create_opening_transfer(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    """Turn a reviewed plan into a draft transfer from the warehouse to the new store."""
    scope.require("plan.write")
    lines = [x for x in v.lines(data.get("lines")) if int(x.get("qty") or 0) > 0]
    if not lines:
        raise RetailError("All quantities are 0.")
    with db.tx() as t:
        dst = stock.location(t, scope, str(data.get("store_id") or ""), kind="store")
        tid = wms.create_transfer_tx(t, scope, from_id=str(data.get("source_location_id") or ""),
                                     to_id=dst["location_id"], items=lines, source="opening_plan",
                                     note=v.text(data.get("note"), "Note", hi=300, required=False)
                                     or f"Opening stock for {dst['name']}")
    return wms.get_transfer(scope, tid)


# ---------------------------------------------------------------------------
# Campaign stock and actual sales
# ---------------------------------------------------------------------------


def _window(campaign: dict[str, Any], today: str) -> Optional[tuple[str, str]]:
    start = (campaign.get("start_date") or (campaign.get("launched_at") or "")[:10] or "").strip()
    if not start:
        return None
    end = (campaign.get("end_date") or "").strip()
    if not end and campaign.get("duration_days"):
        end = (date.fromisoformat(start) + timedelta(days=int(campaign["duration_days"]) - 1)).isoformat()
    end = min(end or today, today)
    return (start, end) if start <= end else None


def _match_variants(rows: list[dict[str, Any]], skus: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """campaign sku_uuid -> catalog variant ids.

    Catalog-sourced SKUs match exactly on retail_product_id + colour. Uploaded SKUs keep the
    commodity + colour fuzzy match.
    """
    out: dict[str, list[str]] = {}
    for s in skus:
        a = s.get("attributes") or {}
        colk = vocab.attr_key("color", a.get("color"))
        pid = str(s.get("retail_product_id") or "").strip()
        hits: list[str] = []
        if pid:
            for r in rows:
                if str(r.get("product_id") or "") != pid:
                    continue
                if colk and vocab.attr_key("color", r["color"]) != colk:
                    continue
                hits.append(r["variant_id"])
            out[str(s.get("sku_uuid"))] = hits
            continue
        ck = vocab.attr_key("commodity", a.get("commodity") or a.get("name"))
        for r in rows:
            keys = {vocab.attr_key("commodity", r["category"]), vocab.attr_key("commodity", r["name"])} - {""}
            if ck and not any(ck == k or (len(ck) >= 3 and ck in k) for k in keys):
                continue
            if colk and vocab.attr_key("color", r["color"]) != colk:
                continue
            if ck or colk:
                hits.append(r["variant_id"])
        out[str(s.get("sku_uuid"))] = hits
    return out


def _sales(t: Any, brand_key: str, store_ids: list[str], d1: str, d2: str,
           variant_ids: Optional[list[str]] = None) -> dict[str, int]:
    if not store_ids:
        return {"units": 0, "net_paise": 0}
    marks = ",".join(["%s"] * len(store_ids))
    sql = (f"SELECT COALESCE(SUM(CASE WHEN i.kind = 'invoice' THEN il.qty ELSE -il.qty END), 0) AS units, "
           f"COALESCE(SUM(CASE WHEN i.kind = 'invoice' THEN il.total_paise ELSE -il.total_paise END), 0) AS net "
           f"FROM retail_invoice_lines il JOIN retail_invoices i ON i.invoice_id = il.invoice_id "
           f"WHERE i.brand_key = %s AND i.location_id IN ({marks}) AND i.business_date >= %s AND i.business_date <= %s")
    params: list[Any] = [brand_key] + store_ids + [d1, d2]
    if variant_ids is not None:
        if not variant_ids:
            return {"units": 0, "net_paise": 0}
        sql += f" AND il.variant_id IN ({','.join(['%s'] * len(variant_ids))})"
        params += variant_ids
    r = t.one(sql, params) or {}
    return {"units": int(r.get("units") or 0), "net_paise": int(r.get("net") or 0)}


def _change(now: int, before: int) -> Optional[float]:
    return round((now - before) / before * 100, 1) if before else None


def campaign_check(scope: Scope, campaign: dict[str, Any], skus: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Stock readiness at the campaign's stores and actual POS sales in the campaign window."""
    scope.require("read")
    pins = sorted({str(p) for p in (campaign.get("pin_codes") or []) if str(p).strip()})
    selected = {str(x) for x in (campaign.get("selected_sku_ids") or [])}
    chosen = [s for s in skus if not selected or str(s.get("sku_uuid")) in selected][:50]
    today = db.today_ist()
    with db.tx() as t:
        stores = t.all("SELECT location_id, code, name, pin_code FROM retail_locations WHERE brand_key = %s AND "
                       "kind = 'store' AND active = TRUE ORDER BY code", (scope.brand_key,))
        in_area = [s for s in stores if s["pin_code"] in pins]
        nearby = [s for s in stores if s not in in_area and any((s["pin_code"] or "")[:3] == p[:3] for p in pins)]
        rows = t.all("SELECT vv.variant_id, vv.product_id, vv.color, p.name, p.category FROM retail_variants vv "
                     "JOIN retail_products p ON p.product_id = vv.product_id "
                     "WHERE vv.brand_key = %s AND vv.active = TRUE", (scope.brand_key,))
        matches = _match_variants(rows, chosen)
        matched = sorted({vid for ids in matches.values() for vid in ids})
        store_ids = [s["location_id"] for s in in_area + nearby]
        have = {s["location_id"]: stock.sellable(t, s["location_id"], matched) for s in in_area + nearby}
        wh_units = int(t.scalar(
            "SELECT COALESCE(SUM(s.on_hand), 0) AS n FROM retail_stock s JOIN retail_locations l "
            "ON l.location_id = s.location_id WHERE l.brand_key = %s AND l.kind = 'warehouse' AND s.bin_code <> %s "
            f"AND s.variant_id IN ({','.join(['%s'] * len(matched)) or 'NULL'})",
            [scope.brand_key, v.DAMAGED_BIN] + matched) or 0) if matched else 0
        win = _window(campaign, today)
        sales = None
        if win:
            d1, d2 = win
            days = (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1
            p2 = (date.fromisoformat(d1) - timedelta(days=1)).isoformat()
            p1 = (date.fromisoformat(d1) - timedelta(days=days)).isoformat()
            area_ids = [s["location_id"] for s in in_area] or store_ids
            now_all, before_all = _sales(t, scope.brand_key, area_ids, d1, d2), _sales(t, scope.brand_key, area_ids, p1, p2)
            now_sku = _sales(t, scope.brand_key, area_ids, d1, d2, matched)
            before_sku = _sales(t, scope.brand_key, area_ids, p1, p2, matched)
            tagged = t.one("SELECT COUNT(*) AS invoices, COALESCE(SUM(units), 0) AS units, "
                           "COALESCE(SUM(total_paise), 0) AS net FROM retail_invoices WHERE brand_key = %s AND "
                           "campaign_id = %s AND kind = 'invoice'", (scope.brand_key, str(campaign.get("campaign_id"))))
            sales = {
                "window": {"from": d1, "to": d2, "days": days}, "prior_window": {"from": p1, "to": p2},
                "stores_basis": "campaign PINs" if in_area else ("nearby district" if nearby else "none"),
                "all_items": {**now_all, "prior": before_all,
                              "change_pct": _change(now_all["net_paise"], before_all["net_paise"])},
                "campaign_items": {**now_sku, "prior": before_sku,
                                   "change_pct": _change(now_sku["units"], before_sku["units"])},
                "tagged_bills": {"invoices": int(tagged["invoices"] or 0), "units": int(tagged["units"] or 0),
                                 "net_paise": int(tagged["net"] or 0)},
            }
    store_rows = [{"location_id": s["location_id"], "code": s["code"], "name": s["name"], "pin_code": s["pin_code"],
                   "in_campaign_pins": s in in_area, "units": sum(have[s["location_id"]].values())}
                  for s in in_area + nearby]
    readiness = []
    for s in chosen:
        ids = matches.get(str(s.get("sku_uuid")), [])
        at_stores = sum(have[sid].get(vid, 0) for sid in have for vid in ids)
        a = s.get("attributes") or {}
        label = " ".join(x for x in (str(a.get("color") or "").strip(), str(a.get("commodity") or "").strip()) if x)
        readiness.append({"sku_uuid": s.get("sku_uuid"), "label": label or s.get("original_filename") or "SKU",
                          "matched_variants": len(ids), "store_units": at_stores,
                          "status": "not_in_catalog" if not ids else ("ready" if at_stores >= max(3, len(in_area) * 3)
                                                                      else ("low" if at_stores else "out"))})
    return {"label": ACTUAL_LABEL, "campaign_id": campaign.get("campaign_id"), "pins": pins, "stores": store_rows,
            "matched_variants": len(matched), "warehouse_units": wh_units, "readiness": readiness, "sales": sales,
            "notes": ("Stock and sales come from this brand's own POS and warehouse records. Catalog items are matched "
                      "to campaign SKUs by garment type and colour.")}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot(scope: Scope) -> dict[str, Any]:
    """Operating numbers for the overview tiles and AURA. Aggregates only."""
    scope.require("read")
    today = db.today_ist()
    d7 = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    d30 = (date.fromisoformat(today) - timedelta(days=29)).isoformat()
    b = scope.brand_key
    neg = "CASE WHEN kind = 'invoice' THEN total_paise ELSE -total_paise END"
    with db.tx() as t:
        locs = t.all("SELECT kind, COUNT(*) AS n FROM retail_locations WHERE brand_key = %s AND active = TRUE "
                     "GROUP BY kind", (b,))
        styles = int(t.scalar("SELECT COUNT(*) AS n FROM retail_products WHERE brand_key = %s AND active = TRUE", (b,)) or 0)
        skus = int(t.scalar("SELECT COUNT(*) AS n FROM retail_variants WHERE brand_key = %s AND active = TRUE", (b,)) or 0)
        low = int(t.scalar(
            "SELECT COUNT(*) AS n FROM retail_par_levels pl WHERE pl.brand_key = %s AND pl.min_qty > "
            "(SELECT COALESCE(SUM(s.on_hand), 0) FROM retail_stock s WHERE s.location_id = pl.location_id "
            "AND s.variant_id = pl.variant_id AND s.bin_code <> %s)", (b, v.DAMAGED_BIN)) or 0)
        today_net = int(t.scalar(f"SELECT COALESCE(SUM({neg}), 0) AS n FROM retail_invoices WHERE brand_key = %s "
                                 "AND business_date = %s", (b, today)) or 0)
        week_net = int(t.scalar(f"SELECT COALESCE(SUM({neg}), 0) AS n FROM retail_invoices WHERE brand_key = %s "
                                "AND business_date >= %s", (b, d7)) or 0)
        bills_today = int(t.scalar("SELECT COUNT(*) AS n FROM retail_invoices WHERE brand_key = %s AND kind = 'invoice' "
                                   "AND business_date = %s", (b, today)) or 0)
        open_pos = int(t.scalar("SELECT COUNT(*) AS n FROM retail_purchase_orders WHERE brand_key = %s AND status IN "
                                "('ordered', 'partial')", (b,)) or 0)
        transit = int(t.scalar("SELECT COUNT(*) AS n FROM retail_transfers WHERE brand_key = %s AND "
                               "status = 'in_transit'", (b,)) or 0)
        top_cat = t.all("SELECT il.category, SUM(CASE WHEN i.kind = 'invoice' THEN il.qty ELSE -il.qty END) AS units "
                        "FROM retail_invoice_lines il JOIN retail_invoices i ON i.invoice_id = il.invoice_id "
                        "WHERE i.brand_key = %s AND i.business_date >= %s GROUP BY il.category ORDER BY units DESC "
                        "LIMIT 5", (b, d30))
        st = stock.settings(t, scope)
    val = stock.valuation(scope)
    kinds = {r["kind"]: int(r["n"]) for r in locs}
    return {
        "label": ACTUAL_LABEL, "date": today, "configured": st["configured"],
        "warehouses": kinds.get("warehouse", 0), "stores": kinds.get("store", 0), "styles": styles, "skus": skus,
        "units_on_hand": val["units"], "damaged_units": val["damaged"], "stock_value_cost": val["value_cost"],
        "stock_value_mrp": val["value_mrp"], "low_stock_skus": low, "open_purchase_orders": open_pos,
        "transfers_in_transit": transit, "net_sales_today": gst.rupees(today_net), "bills_today": bills_today,
        "net_sales_7d": gst.rupees(week_net),
        "top_categories_30d": [{"category": r["category"], "units": int(r["units"] or 0)} for r in top_cat],
        "by_location": val["locations"],
    }
