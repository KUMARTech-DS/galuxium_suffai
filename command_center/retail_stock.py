"""Brand settings, locations and bins, and the stock ledger.

Stock lives in retail_stock per (location, bin, SKU) and changes only through apply_move(), which writes
the matching retail_stock_moves row in the same transaction. Decrements are conditional updates, so two
tills selling the last unit at once cannot both succeed. Units in the DMG bin are not sellable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import retail_catalog as catalog
import retail_db as db
import retail_gst as gst
import retail_valid as v
from retail_db import RetailError, Tx
from retail_scope import Scope

log = logging.getLogger("command_center.retail_stock")

KINDS = ("warehouse", "store")
ADJUST_REASONS = {
    "damaged": "Move to the damaged bin (DMG)",
    "lost": "Lost or stolen",
    "found": "Found / extra",
    "correction": "Correction (+ or -)",
    "write_off": "Write off damaged units",
}
MOVE_REASONS = ("opening", "grn", "sale", "return", "return_damaged", "transfer_out", "transfer_in", "adjust",
                "count", "putaway", "damaged", "lost", "found", "correction", "write_off")
OPENING_REQUIRED = ("location_code", "qty")
OPENING_OPTIONAL = ("sku", "barcode", "bin", "unit_cost")


# ---------------------------------------------------------------------------
# Brand settings
# ---------------------------------------------------------------------------


def settings(t: Tx, scope: Scope) -> dict[str, Any]:
    row = t.one("SELECT brand_key, brand_name, legal_name, gstin, address, invoice_footer, return_days, "
                "allow_negative_stock FROM retail_brand_settings WHERE brand_key = %s", (scope.brand_key,))
    if not row:
        return {"brand_key": scope.brand_key, "brand_name": scope.brand_name, "legal_name": None, "gstin": None,
                "address": None, "invoice_footer": None, "return_days": 30, "allow_negative_stock": False,
                "configured": False}
    return {**row, "allow_negative_stock": bool(row["allow_negative_stock"]), "configured": True}


def get_settings(scope: Scope) -> dict[str, Any]:
    scope.require("read")
    with db.tx() as t:
        return settings(t, scope)


def save_settings(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("settings.write")
    legal = v.text(data.get("legal_name"), "Legal name", hi=120)
    gstin = v.gstin(data.get("gstin"))
    address = v.text(data.get("address"), "Registered address", hi=300, required=False)
    footer = v.text(data.get("invoice_footer"), "Invoice footer", hi=300, required=False)
    days = v.qty(data.get("return_days", 30), "Return window (days)", lo=0, hi=365)
    neg = bool(data.get("allow_negative_stock"))
    with db.tx() as t:
        t.run(
            "INSERT INTO retail_brand_settings (brand_key, brand_name, legal_name, gstin, address, invoice_footer, "
            "return_days, allow_negative_stock, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (brand_key) DO UPDATE SET brand_name = excluded.brand_name, legal_name = excluded.legal_name, "
            "gstin = excluded.gstin, address = excluded.address, invoice_footer = excluded.invoice_footer, "
            "return_days = excluded.return_days, allow_negative_stock = excluded.allow_negative_stock, "
            "updated_at = excluded.updated_at",
            (scope.brand_key, scope.brand_name, legal, gstin, address, footer, days, neg, db.now_iso()),
        )
        db.audit(t, scope.brand_key, scope.actor, "update", "settings", scope.brand_key,
                 {"gstin": gstin, "return_days": days, "allow_negative_stock": neg})
        return settings(t, scope)


# ---------------------------------------------------------------------------
# Locations and bins
# ---------------------------------------------------------------------------

_LOC_COLS = "location_id, code, name, kind, address, city, pin_code, state_code, gstin, phone, active, created_at"


def _loc_public(r: dict[str, Any]) -> dict[str, Any]:
    return {**r, "active": bool(r["active"]), "state": gst.STATES.get(r["state_code"] or "", "")}


def location(t: Tx, scope: Scope, location_id: Optional[str], *, kind: Optional[str] = None,
             active: bool = True) -> dict[str, Any]:
    if not location_id:
        raise RetailError("Pick a location.")
    r = t.one(f"SELECT {_LOC_COLS} FROM retail_locations WHERE location_id = %s AND brand_key = %s",
              (location_id, scope.brand_key))
    if not r:
        raise RetailError("Location not found.", 404)
    if kind and r["kind"] != kind:
        raise RetailError(f"{r['name']} is not a {kind}.")
    if active and not r["active"]:
        raise RetailError(f"{r['name']} is closed (inactive).")
    return _loc_public(r)


def location_by_code(t: Tx, brand_key: str, code: str) -> Optional[dict[str, Any]]:
    r = t.one(f"SELECT {_LOC_COLS} FROM retail_locations WHERE brand_key = %s AND code = %s",
              (brand_key, (code or "").strip().upper()))
    return _loc_public(r) if r else None


def list_locations(scope: Scope, *, kind: Optional[str] = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    scope.require("read")
    where, params = ["brand_key = %s"], [scope.brand_key]
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if not include_inactive:
        where.append("active = TRUE")
    with db.tx() as t:
        rows = t.all(f"SELECT {_LOC_COLS} FROM retail_locations WHERE {' AND '.join(where)} ORDER BY kind, code", params)
        units = {r["location_id"]: int(r["units"] or 0) for r in t.all(
            "SELECT location_id, SUM(on_hand) AS units FROM retail_stock WHERE brand_key = %s GROUP BY location_id",
            (scope.brand_key,))}
    return [{**_loc_public(r), "units": units.get(r["location_id"], 0)} for r in rows]


def _location_fields(data: dict[str, Any], kind: str) -> dict[str, Any]:
    state = v.state_code(data.get("state_code"))
    return {
        "name": v.text(data.get("name"), "Location name", hi=80),
        "address": v.text(data.get("address"), "Address", hi=300, required=False),
        "city": v.text(data.get("city"), "City", hi=60, required=False),
        "pin_code": v.pin_code(data.get("pin_code"), required=kind == "store"),
        "state_code": state,
        "gstin": v.gstin(data.get("gstin"), state=state),
        "phone": v.phone(data.get("phone")),
    }


def create_location(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("locations.write")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise RetailError("Location type must be warehouse or store.")
    code = v.location_code(data.get("code"))
    f = _location_fields(data, kind)
    with db.tx() as t:
        if location_by_code(t, scope.brand_key, code):
            raise RetailError(f"Location code {code} is already used.", 409)
        lid = db.new_id()
        t.run(f"INSERT INTO retail_locations ({_LOC_COLS}, brand_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
              (lid, code, f["name"], kind, f["address"], f["city"], f["pin_code"], f["state_code"], f["gstin"],
               f["phone"], db.now_iso(), scope.brand_key))
        db.audit(t, scope.brand_key, scope.actor, "create", "location", lid, {"code": code, "kind": kind})
        return location(t, scope, lid)


def update_location(scope: Scope, location_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("locations.write")
    with db.tx() as t:
        cur = location(t, scope, location_id, active=False)
        f = _location_fields({**cur, **data}, cur["kind"])
        active = bool(data["active"]) if "active" in data else cur["active"]
        t.run("UPDATE retail_locations SET name=%s, address=%s, city=%s, pin_code=%s, state_code=%s, gstin=%s, "
              "phone=%s, active=%s WHERE location_id=%s",
              (f["name"], f["address"], f["city"], f["pin_code"], f["state_code"], f["gstin"], f["phone"], active,
               location_id))
        db.audit(t, scope.brand_key, scope.actor, "update", "location", location_id, {**f, "active": active})
        return location(t, scope, location_id, active=False)


def list_bins(scope: Scope, location_id: str) -> list[dict[str, Any]]:
    scope.require("read")
    scope.default_location(location_id)
    with db.tx() as t:
        location(t, scope, location_id, active=False)
        bins = t.all("SELECT bin_code, zone FROM retail_bins WHERE location_id = %s ORDER BY bin_code", (location_id,))
        units = {r["bin_code"]: int(r["units"] or 0) for r in t.all(
            "SELECT bin_code, SUM(on_hand) AS units FROM retail_stock WHERE location_id = %s GROUP BY bin_code",
            (location_id,))}
    named = {b["bin_code"] for b in bins}
    out = [{"bin_code": "", "zone": "Unassigned", "units": units.get("", 0)}]
    out += [{**b, "units": units.get(b["bin_code"], 0)} for b in bins]
    if v.DAMAGED_BIN not in named:
        out.append({"bin_code": v.DAMAGED_BIN, "zone": "Damaged (not sellable)", "units": units.get(v.DAMAGED_BIN, 0)})
    return out


def create_bins(scope: Scope, location_id: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    scope.require("stock.write")
    scope.check_location(location_id)
    codes = data.get("bin_codes") or []
    if isinstance(codes, str):
        codes = [c for c in codes.replace(",", "\n").splitlines()]
    codes = [v.bin_code(c, allow_blank=False) for c in codes if str(c).strip()]
    if not codes or len(codes) > 500:
        raise RetailError("Enter 1-500 bin codes.")
    zone = v.text(data.get("zone"), "Zone", hi=40, required=False)
    with db.tx() as t:
        location(t, scope, location_id)
        for c in dict.fromkeys(codes):
            t.run("INSERT INTO retail_bins (location_id, bin_code, zone, created_at) VALUES (%s,%s,%s,%s) "
                  "ON CONFLICT (location_id, bin_code) DO NOTHING", (location_id, c, zone, db.now_iso()))
    return list_bins(scope, location_id)


def _known_bin(t: Tx, location_id: str, bin_code: str) -> None:
    if bin_code in ("", v.DAMAGED_BIN):
        return
    if not t.one("SELECT 1 AS x FROM retail_bins WHERE location_id = %s AND bin_code = %s", (location_id, bin_code)):
        raise RetailError(f"Bin {bin_code} doesn't exist at this location. Add it first.")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def apply_move(t: Tx, *, brand_key: str, location_id: str, bin_code: str, variant_id: str, qty: int, reason: str,
               actor: str, ref_type: Optional[str] = None, ref_id: Optional[str] = None, note: Optional[str] = None,
               allow_negative: bool = False, sku: Optional[str] = None) -> int:
    """Change on-hand by qty (signed) and log it. Returns the balance after the move."""
    if qty == 0:
        raise ValueError("qty must be non-zero")
    if reason not in MOVE_REASONS:
        raise ValueError(f"unknown reason {reason}")
    now = db.now_iso()
    t.run("INSERT INTO retail_stock (location_id, bin_code, variant_id, brand_key, on_hand, updated_at) "
          "VALUES (%s,%s,%s,%s,0,%s) ON CONFLICT (location_id, bin_code, variant_id) DO NOTHING",
          (location_id, bin_code, variant_id, brand_key, now))
    if qty > 0 or allow_negative:
        bal = t.scalar("UPDATE retail_stock SET on_hand = on_hand + %s, updated_at = %s WHERE location_id = %s "
                       "AND bin_code = %s AND variant_id = %s RETURNING on_hand",
                       (qty, now, location_id, bin_code, variant_id))
    else:
        bal = t.scalar("UPDATE retail_stock SET on_hand = on_hand + %s, updated_at = %s WHERE location_id = %s "
                       "AND bin_code = %s AND variant_id = %s AND on_hand + %s >= 0 RETURNING on_hand",
                       (qty, now, location_id, bin_code, variant_id, qty))
        if bal is None:
            have = int(t.scalar("SELECT on_hand FROM retail_stock WHERE location_id = %s AND bin_code = %s AND "
                                "variant_id = %s", (location_id, bin_code, variant_id)) or 0)
            where = f"bin {bin_code}" if bin_code else "stock"
            raise RetailError(f"Only {max(have, 0)} of {sku or 'this SKU'} in {where} here, {-qty} needed.", 409)
    t.run("INSERT INTO retail_stock_moves (move_id, brand_key, location_id, bin_code, variant_id, qty, balance_after, "
          "reason, ref_type, ref_id, note, actor, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
          (db.new_id(), brand_key, location_id, bin_code, variant_id, qty, int(bal), reason, ref_type, ref_id, note,
           actor, now))
    return int(bal)


def sellable_by_bin(t: Tx, location_id: str, variant_ids: Sequence[str]) -> dict[str, list[tuple[str, int]]]:
    """variant -> [(bin, units)] with units > 0, unassigned first then bins in pick-path (code) order."""
    ids = list(dict.fromkeys(variant_ids))
    out: dict[str, list[tuple[str, int]]] = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join(["%s"] * len(ids))
    for r in t.all(f"SELECT variant_id, bin_code, on_hand FROM retail_stock WHERE location_id = %s AND bin_code <> %s "
                   f"AND on_hand > 0 AND variant_id IN ({marks}) ORDER BY bin_code",
                   [location_id, v.DAMAGED_BIN] + ids):
        out[r["variant_id"]].append((r["bin_code"], int(r["on_hand"])))
    return out


def sellable(t: Tx, location_id: str, variant_ids: Sequence[str]) -> dict[str, int]:
    return {k: sum(n for _, n in bins) for k, bins in sellable_by_bin(t, location_id, variant_ids).items()}


def take_from_bins(t: Tx, *, scope_brand: str, location_id: str, variant_id: str, qty: int, reason: str, actor: str,
                   ref_type: str, ref_id: str, sku: str) -> list[dict[str, Any]]:
    """Remove qty sellable units, emptying bins in pick order. Raises if the location has fewer."""
    bins = sellable_by_bin(t, location_id, [variant_id])[variant_id]
    have = sum(n for _, n in bins)
    if have < qty:
        raise RetailError(f"Only {have} of {sku} available here, {qty} needed.", 409)
    picks, left = [], qty
    for b, n in bins:
        take = min(n, left)
        apply_move(t, brand_key=scope_brand, location_id=location_id, bin_code=b, variant_id=variant_id, qty=-take,
                   reason=reason, actor=actor, ref_type=ref_type, ref_id=ref_id, sku=sku)
        picks.append({"bin_code": b, "qty": take})
        left -= take
        if not left:
            break
    return picks


# ---------------------------------------------------------------------------
# Adjustments, counts, putaway, par levels
# ---------------------------------------------------------------------------


def adjust(scope: Scope, location_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("stock.write")
    scope.check_location(location_id)
    reason = str(data.get("reason") or "").strip().lower()
    if reason not in ADJUST_REASONS:
        raise RetailError("Pick a reason: damaged, lost, found, correction or write_off.")
    note = v.text(data.get("note"), "Note", hi=200, required=reason == "correction")
    items = v.lines(data.get("lines"))
    with db.tx() as t:
        location(t, scope, location_id)
        rows = catalog.require_variants(t, scope.brand_key, [str(i.get("variant_id") or "") for i in items])
        out = []
        for i in items:
            vid = str(i["variant_id"])
            b = v.bin_code(i.get("bin_code"))
            _known_bin(t, location_id, b)
            q = v.signed_qty(i.get("qty")) if reason == "correction" else v.qty(i.get("qty"))
            common = dict(brand_key=scope.brand_key, location_id=location_id, variant_id=vid, actor=scope.actor,
                          ref_type="adjustment", note=note, sku=rows[vid]["sku"])
            if reason == "damaged":
                if b == v.DAMAGED_BIN:
                    raise RetailError("Pick the bin the damaged units are in now.")
                apply_move(t, bin_code=b, qty=-q, reason="damaged", **common)
                bal = apply_move(t, bin_code=v.DAMAGED_BIN, qty=q, reason="damaged", **common)
            elif reason == "write_off":
                bal = apply_move(t, bin_code=v.DAMAGED_BIN, qty=-q, reason="write_off", **common)
            else:
                bal = apply_move(t, bin_code=b, qty=-q if reason == "lost" else q, reason=reason, **common)
            out.append({"variant_id": vid, "sku": rows[vid]["sku"], "balance": bal})
        log.info("brand=%s actor=%s adjust reason=%s lines=%s", scope.brand_key, scope.actor, reason, len(out))
        return {"reason": reason, "lines": out}


def count(scope: Scope, location_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Cycle count: post the difference between counted and recorded units for each SKU in one bin."""
    scope.require("stock.write")
    scope.check_location(location_id)
    b = v.bin_code(data.get("bin_code"))
    items = v.lines(data.get("lines"))
    with db.tx() as t:
        location(t, scope, location_id)
        _known_bin(t, location_id, b)
        rows = catalog.require_variants(t, scope.brand_key, [str(i.get("variant_id") or "") for i in items])
        out, ref = [], db.new_id()
        for i in items:
            vid = str(i["variant_id"])
            counted = v.qty(i.get("counted"), "Counted", lo=0)
            have = int(t.scalar("SELECT on_hand FROM retail_stock WHERE location_id = %s AND bin_code = %s AND "
                                "variant_id = %s", (location_id, b, vid)) or 0)
            diff = counted - have
            if diff:
                apply_move(t, brand_key=scope.brand_key, location_id=location_id, bin_code=b, variant_id=vid, qty=diff,
                           reason="count", actor=scope.actor, ref_type="count", ref_id=ref, allow_negative=True,
                           sku=rows[vid]["sku"])
            out.append({"variant_id": vid, "sku": rows[vid]["sku"], "recorded": have, "counted": counted, "diff": diff})
        return {"bin_code": b, "lines": out, "variance_units": sum(abs(x["diff"]) for x in out)}


def putaway(scope: Scope, location_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("stock.write")
    scope.check_location(location_id)
    src, dst = v.bin_code(data.get("from_bin")), v.bin_code(data.get("to_bin"))
    if src == dst:
        raise RetailError("Pick two different bins.")
    if v.DAMAGED_BIN in (src, dst):
        raise RetailError("Use an adjustment (damaged / write off) for the DMG bin.")
    items = v.lines(data.get("lines"))
    with db.tx() as t:
        location(t, scope, location_id)
        _known_bin(t, location_id, src)
        _known_bin(t, location_id, dst)
        rows = catalog.require_variants(t, scope.brand_key, [str(i.get("variant_id") or "") for i in items])
        ref = db.new_id()
        for i in items:
            vid, q = str(i["variant_id"]), v.qty(i.get("qty"))
            common = dict(brand_key=scope.brand_key, location_id=location_id, variant_id=vid, reason="putaway",
                          actor=scope.actor, ref_type="putaway", ref_id=ref, sku=rows[vid]["sku"])
            apply_move(t, bin_code=src, qty=-q, **common)
            apply_move(t, bin_code=dst, qty=q, **common)
        return {"from_bin": src, "to_bin": dst, "lines": len(items)}


def set_par_levels(scope: Scope, location_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("par.write")
    items = v.lines(data.get("lines"))
    with db.tx() as t:
        location(t, scope, location_id)
        catalog.require_variants(t, scope.brand_key, [str(i.get("variant_id") or "") for i in items])
        n = 0
        for i in items:
            lo = v.qty(i.get("min_qty"), "Min", lo=0)
            hi = v.qty(i.get("max_qty"), "Max", lo=0)
            if hi < lo:
                raise RetailError("Max must be at least Min.")
            if lo == 0 and hi == 0:
                t.run("DELETE FROM retail_par_levels WHERE location_id = %s AND variant_id = %s",
                      (location_id, i["variant_id"]))
                continue
            t.run("INSERT INTO retail_par_levels (location_id, variant_id, brand_key, min_qty, max_qty, updated_at) "
                  "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (location_id, variant_id) DO UPDATE SET "
                  "min_qty = excluded.min_qty, max_qty = excluded.max_qty, updated_at = excluded.updated_at",
                  (location_id, i["variant_id"], scope.brand_key, lo, hi, db.now_iso()))
            n += 1
        return {"saved": n}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def stock_view(scope: Scope, *, location_id: Optional[str] = None, q: str = "", category: str = "",
               low_only: bool = False, page: int = 1, size: int = 100) -> dict[str, Any]:
    """One row per location x SKU with sellable, damaged, par levels, cost and MRP value."""
    scope.require("read")
    page, size = v.page(page, size, max_size=500)
    location_id = scope.default_location(location_id)
    where, params = ["s.brand_key = %s"], [scope.brand_key]
    if location_id:
        where.append("s.location_id = %s")
        params.append(location_id)
    if q.strip():
        like = "%" + q.strip().lower() + "%"
        where.append("(LOWER(vv.sku) LIKE %s OR vv.barcode = %s OR LOWER(p.name) LIKE %s OR LOWER(p.style_code) LIKE %s)")
        params += [like, q.strip(), like, like]
    if category.strip():
        where.append("LOWER(p.category) = %s")
        params.append(category.strip().lower())
    base = (
        "SELECT s.location_id, s.variant_id, "
        "SUM(CASE WHEN s.bin_code = %s THEN 0 ELSE s.on_hand END) AS sellable, "
        "SUM(CASE WHEN s.bin_code = %s THEN s.on_hand ELSE 0 END) AS damaged "
        "FROM retail_stock s JOIN retail_variants vv ON vv.variant_id = s.variant_id "
        "JOIN retail_products p ON p.product_id = vv.product_id "
        f"WHERE {' AND '.join(where)} GROUP BY s.location_id, s.variant_id"
    )
    sql = (
        "SELECT b.location_id, b.variant_id, b.sellable, b.damaged, l.code AS location_code, l.name AS location_name, "
        "l.kind, vv.sku, vv.barcode, vv.color, vv.size, vv.mrp_paise, vv.avg_cost_paise, p.name, p.style_code, "
        "p.category, pl.min_qty, pl.max_qty "
        f"FROM ({base}) b JOIN retail_locations l ON l.location_id = b.location_id "
        "JOIN retail_variants vv ON vv.variant_id = b.variant_id JOIN retail_products p ON p.product_id = vv.product_id "
        "LEFT JOIN retail_par_levels pl ON pl.location_id = b.location_id AND pl.variant_id = b.variant_id "
    )
    head = [v.DAMAGED_BIN, v.DAMAGED_BIN] + params
    if low_only:
        sql += "WHERE pl.min_qty IS NOT NULL AND b.sellable < pl.min_qty "
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n FROM ({sql}) x", head) or 0)
        rows = t.all(sql + "ORDER BY l.kind, l.code, p.style_code, vv.color, vv.size LIMIT %s OFFSET %s",
                     head + [size, (page - 1) * size])
    out = []
    for r in rows:
        sell, dmg = int(r["sellable"] or 0), int(r["damaged"] or 0)
        out.append({
            "location_id": r["location_id"], "location_code": r["location_code"], "location_name": r["location_name"],
            "kind": r["kind"], "variant_id": r["variant_id"], "sku": r["sku"], "barcode": r["barcode"],
            "name": r["name"], "style_code": r["style_code"], "category": r["category"], "color": r["color"],
            "size": r["size"], "sellable": sell, "damaged": dmg, "min_qty": r["min_qty"], "max_qty": r["max_qty"],
            "low": r["min_qty"] is not None and sell < int(r["min_qty"]),
            "mrp": gst.rupees(r["mrp_paise"]), "avg_cost": gst.rupees(r["avg_cost_paise"]),
            "value_cost": gst.rupees(sell * int(r["avg_cost_paise"])), "value_mrp": gst.rupees(sell * int(r["mrp_paise"])),
        })
    return {"rows": out, "total": total, "page": page, "size": size, "location_id": location_id}


def bin_stock(scope: Scope, location_id: str, *, variant_id: Optional[str] = None,
              bin_code: Optional[str] = None) -> list[dict[str, Any]]:
    scope.require("read")
    scope.default_location(location_id)
    where, params = ["s.location_id = %s", "s.on_hand <> 0"], [location_id]
    if variant_id:
        where.append("s.variant_id = %s")
        params.append(variant_id)
    if bin_code is not None:
        where.append("s.bin_code = %s")
        params.append(v.bin_code(bin_code))
    with db.tx() as t:
        location(t, scope, location_id, active=False)
        rows = t.all("SELECT s.bin_code, s.variant_id, s.on_hand, vv.sku, vv.color, vv.size, p.name FROM retail_stock s "
                     "JOIN retail_variants vv ON vv.variant_id = s.variant_id JOIN retail_products p "
                     f"ON p.product_id = vv.product_id WHERE {' AND '.join(where)} ORDER BY s.bin_code, vv.sku LIMIT 1000",
                     params)
    return [{**r, "on_hand": int(r["on_hand"])} for r in rows]


def moves(scope: Scope, *, location_id: Optional[str] = None, q: str = "", reason: str = "",
          date_from: Optional[str] = None, date_to: Optional[str] = None, page: int = 1, size: int = 100) -> dict[str, Any]:
    scope.require("read")
    page, size = v.page(page, size, max_size=500)
    location_id = scope.default_location(location_id)
    where, params = ["m.brand_key = %s"], [scope.brand_key]
    if location_id:
        where.append("m.location_id = %s")
        params.append(location_id)
    if reason:
        if reason not in MOVE_REASONS:
            raise RetailError("Unknown movement type.")
        where.append("m.reason = %s")
        params.append(reason)
    if q.strip():
        where.append("(LOWER(vv.sku) LIKE %s OR vv.barcode = %s)")
        params += ["%" + q.strip().lower() + "%", q.strip()]
    d1, d2 = v.iso_date(date_from, "From"), v.iso_date(date_to, "To")
    if d1:
        where.append("m.created_at >= %s")
        params.append(db.ist_day_start_utc(d1))
    if d2:
        where.append("m.created_at < %s")
        params.append(db.ist_day_start_utc(_next_day(d2)))
    cond = " AND ".join(where)
    join = ("FROM retail_stock_moves m JOIN retail_variants vv ON vv.variant_id = m.variant_id "
            "JOIN retail_locations l ON l.location_id = m.location_id ")
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n {join} WHERE {cond}", params) or 0)
        rows = t.all(f"SELECT m.move_id, m.created_at, m.reason, m.qty, m.balance_after, m.bin_code, m.ref_type, m.ref_id, "
                     f"m.note, m.actor, vv.sku, vv.color, vv.size, l.code AS location_code {join} WHERE {cond} "
                     f"ORDER BY m.created_at DESC, m.move_id LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
    return {"moves": rows, "total": total, "page": page, "size": size}


def _next_day(d: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def valuation(scope: Scope) -> dict[str, Any]:
    scope.require("read")
    own = " AND s.location_id = %s" if scope.is_staff else ""
    with db.tx() as t:
        rows = t.all(
            "SELECT l.location_id, l.code, l.name, l.kind, "
            "SUM(CASE WHEN s.bin_code = %s THEN 0 ELSE s.on_hand END) AS units, "
            "SUM(CASE WHEN s.bin_code = %s THEN s.on_hand ELSE 0 END) AS damaged, "
            "SUM(CASE WHEN s.bin_code = %s THEN 0 ELSE s.on_hand * vv.avg_cost_paise END) AS cost, "
            "SUM(CASE WHEN s.bin_code = %s THEN 0 ELSE s.on_hand * vv.mrp_paise END) AS mrp "
            "FROM retail_stock s JOIN retail_variants vv ON vv.variant_id = s.variant_id "
            f"JOIN retail_locations l ON l.location_id = s.location_id WHERE s.brand_key = %s{own} "
            "GROUP BY l.location_id, l.code, l.name, l.kind ORDER BY l.kind, l.code",
            (v.DAMAGED_BIN,) * 4 + (scope.brand_key,) + ((scope.location_id,) if scope.is_staff else ()))
    locs = [{"location_id": r["location_id"], "code": r["code"], "name": r["name"], "kind": r["kind"],
             "units": int(r["units"] or 0), "damaged": int(r["damaged"] or 0),
             "value_cost": gst.rupees(r["cost"] or 0), "value_mrp": gst.rupees(r["mrp"] or 0)} for r in rows]
    return {"locations": locs, "units": sum(x["units"] for x in locs), "damaged": sum(x["damaged"] for x in locs),
            "value_cost": round(sum(x["value_cost"] for x in locs), 2),
            "value_mrp": round(sum(x["value_mrp"] for x in locs), 2)}


# ---------------------------------------------------------------------------
# Opening stock import
# ---------------------------------------------------------------------------


def update_avg_cost(t: Tx, brand_key: str, variant_id: str, qty: int, unit_cost_paise: int) -> None:
    """Moving average over the brand's units on hand (call before the receipt is added)."""
    cur = t.one("SELECT avg_cost_paise FROM retail_variants WHERE variant_id = %s", (variant_id,))
    have = max(int(t.scalar("SELECT COALESCE(SUM(on_hand), 0) AS n FROM retail_stock WHERE brand_key = %s AND "
                            "variant_id = %s", (brand_key, variant_id)) or 0), 0)
    old = int(cur["avg_cost_paise"] or 0) if cur else 0
    new = gst.div_round(old * have + unit_cost_paise * qty, have + qty) if have + qty > 0 else unit_cost_paise
    t.run("UPDATE retail_variants SET avg_cost_paise = %s, updated_at = %s WHERE variant_id = %s",
          (new, db.now_iso(), variant_id))


def import_opening_csv(scope: Scope, text: str) -> dict[str, Any]:
    """Columns location_code, sku or barcode, qty, bin (optional), unit_cost (optional). Adds to what is there."""
    scope.require("catalog.write")
    rows = catalog.parse_csv(text, required=OPENING_REQUIRED, optional=OPENING_OPTIONAL, max_rows=50_000)
    errors, ready = [], []
    with db.tx() as t:
        locs = {r["code"]: r for r in t.all("SELECT location_id, code, active FROM retail_locations WHERE brand_key = %s",
                                            (scope.brand_key,))}
        vars_ = t.all("SELECT variant_id, sku, barcode FROM retail_variants WHERE brand_key = %s", (scope.brand_key,))
        by_sku, by_code = {r["sku"]: r for r in vars_}, {r["barcode"]: r for r in vars_}
        bins = {(r["location_id"], r["bin_code"]) for r in t.all(
            "SELECT b.location_id, b.bin_code FROM retail_bins b JOIN retail_locations l ON l.location_id = b.location_id "
            "WHERE l.brand_key = %s", (scope.brand_key,))}
        for i, r in enumerate(rows, start=2):
            try:
                loc = locs.get((r.get("location_code") or "").strip().upper())
                if not loc or not loc["active"]:
                    raise RetailError(f"Unknown or closed location {r.get('location_code')!r}.")
                var = by_sku.get((r.get("sku") or "").strip().upper()) or by_code.get((r.get("barcode") or "").strip())
                if not var:
                    raise RetailError("SKU / barcode not in the catalog.")
                b = v.bin_code(r.get("bin"))
                if b not in ("", v.DAMAGED_BIN) and (loc["location_id"], b) not in bins:
                    raise RetailError(f"Bin {b} doesn't exist at {loc['code']}.")
                q = v.qty(r.get("qty"))
                cost = v.money(r["unit_cost"], "Unit cost") if r.get("unit_cost") else None
                ready.append((loc["location_id"], b, var, q, cost))
            except RetailError as exc:
                errors.append({"row": i, "error": str(exc)})
                if len(errors) >= catalog.MAX_IMPORT_ERRORS:
                    break
        if errors:
            raise RetailError(f"{len(errors)} rows need fixing. Nothing was imported.", 422, errors)
        ref = db.new_id()
        for lid, b, var, q, cost in ready:
            if cost is not None:
                update_avg_cost(t, scope.brand_key, var["variant_id"], q, cost)
            apply_move(t, brand_key=scope.brand_key, location_id=lid, bin_code=b, variant_id=var["variant_id"], qty=q,
                       reason="opening", actor=scope.actor, ref_type="opening_import", ref_id=ref, sku=var["sku"])
        db.audit(t, scope.brand_key, scope.actor, "import", "opening_stock", ref,
                 {"rows": len(ready), "units": sum(x[3] for x in ready)})
    return {"rows": len(ready), "units": sum(x[3] for x in ready)}
