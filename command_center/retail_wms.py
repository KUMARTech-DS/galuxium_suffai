"""Warehouse operations: suppliers, purchase orders and goods receipts, transfers, replenishment."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

import retail_catalog as catalog
import retail_db as db
import retail_gst as gst
import retail_stock as stock
import retail_valid as v
from retail_db import RetailError, Tx
from retail_scope import Scope

log = logging.getLogger("command_center.retail_wms")

TRANSFER_SOURCES = ("manual", "replenishment", "opening_plan", "aura")


def _fy() -> str:
    return gst.financial_year(date.fromisoformat(db.today_ist()))


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------


def list_suppliers(scope: Scope) -> list[dict[str, Any]]:
    scope.require("read")
    with db.tx() as t:
        return t.all("SELECT supplier_id, name, gstin, phone, email, address FROM retail_suppliers WHERE brand_key = %s "
                     "ORDER BY name", (scope.brand_key,))


def create_supplier(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("purchase.write")
    name = v.text(data.get("name"), "Supplier name", hi=120)
    row = {
        "supplier_id": db.new_id(), "name": name, "gstin": v.gstin(data.get("gstin")),
        "phone": v.text(data.get("phone"), "Phone", hi=20, required=False),
        "email": v.text(data.get("email"), "Email", hi=120, required=False),
        "address": v.text(data.get("address"), "Address", hi=300, required=False),
    }
    with db.tx() as t:
        if t.one("SELECT 1 AS x FROM retail_suppliers WHERE brand_key = %s AND LOWER(name) = %s",
                 (scope.brand_key, name.lower())):
            raise RetailError(f"Supplier {name} already exists.", 409)
        t.run("INSERT INTO retail_suppliers (supplier_id, brand_key, name, gstin, phone, email, address, created_at) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
              (row["supplier_id"], scope.brand_key, name, row["gstin"], row["phone"], row["email"], row["address"],
               db.now_iso()))
        db.audit(t, scope.brand_key, scope.actor, "create", "supplier", row["supplier_id"], {"name": name})
    return row


# ---------------------------------------------------------------------------
# Purchase orders and goods receipts
# ---------------------------------------------------------------------------


def _po_row(t: Tx, scope: Scope, po_id: str) -> dict[str, Any]:
    po = t.one("SELECT po.po_id, po.po_number, po.status, po.supplier_id, po.location_id, po.expected_date, po.note, "
               "po.created_by, po.created_at, po.updated_at, s.name AS supplier_name, l.code AS location_code, "
               "l.name AS location_name FROM retail_purchase_orders po JOIN retail_suppliers s "
               "ON s.supplier_id = po.supplier_id JOIN retail_locations l ON l.location_id = po.location_id "
               "WHERE po.po_id = %s AND po.brand_key = %s", (po_id, scope.brand_key))
    if not po:
        raise RetailError("Purchase order not found.", 404)
    return po


def _po(t: Tx, scope: Scope, po_id: str) -> dict[str, Any]:
    po = _po_row(t, scope, po_id)
    lines = t.all("SELECT pl.line_no, pl.variant_id, pl.qty_ordered, pl.qty_received, pl.unit_cost_paise, vv.sku, "
                  "vv.color, vv.size, p.name FROM retail_po_lines pl JOIN retail_variants vv ON vv.variant_id = pl.variant_id "
                  "JOIN retail_products p ON p.product_id = vv.product_id WHERE pl.po_id = %s ORDER BY pl.line_no",
                  (po_id,))
    grns = t.all("SELECT grn_id, grn_number, supplier_invoice, received_by, created_at FROM retail_grns WHERE po_id = %s "
                 "ORDER BY created_at", (po_id,))
    for ln in lines:
        ln["unit_cost"] = gst.rupees(ln["unit_cost_paise"])
        ln["open_qty"] = int(ln["qty_ordered"]) - int(ln["qty_received"])
    po["lines"] = lines
    po["grns"] = grns
    po["value"] = round(sum(ln["unit_cost"] * int(ln["qty_ordered"]) for ln in lines), 2)
    po["units_ordered"] = sum(int(ln["qty_ordered"]) for ln in lines)
    po["units_received"] = sum(int(ln["qty_received"]) for ln in lines)
    return po


def create_po(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("purchase.write")
    items = v.lines(data.get("lines"))
    expected = v.iso_date(data.get("expected_date"), "Expected date")
    note = v.text(data.get("note"), "Note", hi=300, required=False)
    with db.tx() as t:
        sup = t.one("SELECT supplier_id FROM retail_suppliers WHERE supplier_id = %s AND brand_key = %s",
                    (str(data.get("supplier_id") or ""), scope.brand_key))
        if not sup:
            raise RetailError("Pick a supplier.")
        loc = stock.location(t, scope, str(data.get("location_id") or ""), kind="warehouse")
        vids = [str(i.get("variant_id") or "") for i in items]
        if len(set(vids)) != len(vids):
            raise RetailError("Each SKU can appear once per purchase order.")
        rows = catalog.require_variants(t, scope.brand_key, vids)
        po_id, now = db.new_id(), db.now_iso()
        number = gst.document_number("PO", _fy(), db.next_number(t, scope.brand_key, "po", _fy()))
        t.run("INSERT INTO retail_purchase_orders (po_id, brand_key, po_number, supplier_id, location_id, status, "
              "expected_date, note, created_by, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s)",
              (po_id, scope.brand_key, number, sup["supplier_id"], loc["location_id"], expected, note, scope.actor, now, now))
        for n, i in enumerate(items, start=1):
            cost = v.money(i.get("unit_cost"), "Unit cost") if i.get("unit_cost") not in (None, "") \
                else int(rows[str(i["variant_id"])]["avg_cost_paise"] or 0)
            t.run("INSERT INTO retail_po_lines (po_id, line_no, variant_id, qty_ordered, qty_received, unit_cost_paise) "
                  "VALUES (%s,%s,%s,%s,0,%s)", (po_id, n, str(i["variant_id"]), v.qty(i.get("qty")), cost))
        db.audit(t, scope.brand_key, scope.actor, "create", "purchase_order", po_id, {"po_number": number})
        return _po(t, scope, po_id)


def set_po_status(scope: Scope, po_id: str, action: str) -> dict[str, Any]:
    scope.require("purchase.write")
    with db.tx() as t:
        po = _po(t, scope, po_id)
        if action == "order":
            if po["status"] != "draft":
                raise RetailError("Only a draft can be sent to the supplier.")
            new = "ordered"
        elif action == "cancel":
            if po["status"] not in ("draft", "ordered") or po["units_received"]:
                raise RetailError("Only a draft or ordered PO with nothing received can be cancelled.")
            new = "cancelled"
        elif action == "close":
            if po["status"] != "partial":
                raise RetailError("Only a partly received PO can be closed short.")
            new = "received"
        else:
            raise RetailError("Unknown action.")
        t.run("UPDATE retail_purchase_orders SET status = %s, updated_at = %s WHERE po_id = %s",
              (new, db.now_iso(), po_id))
        db.audit(t, scope.brand_key, scope.actor, action, "purchase_order", po_id, {"status": [po["status"], new]})
        return _po(t, scope, po_id)


def list_pos(scope: Scope, *, status: str = "", page: int = 1, size: int = 50) -> dict[str, Any]:
    scope.require("read")
    page, size = v.page(page, size)
    where, params = ["po.brand_key = %s"], [scope.brand_key]
    if status:
        where.append("po.status = %s")
        params.append(status)
    if scope.is_staff:
        where.append("po.location_id = %s")
        params.append(scope.location_id)
    cond = " AND ".join(where)
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n FROM retail_purchase_orders po WHERE {cond}", params) or 0)
        rows = t.all(
            "SELECT po.po_id, po.po_number, po.status, po.expected_date, po.created_at, s.name AS supplier_name, "
            "l.code AS location_code, "
            "(SELECT COALESCE(SUM(qty_ordered), 0) FROM retail_po_lines x WHERE x.po_id = po.po_id) AS units_ordered, "
            "(SELECT COALESCE(SUM(qty_received), 0) FROM retail_po_lines x WHERE x.po_id = po.po_id) AS units_received, "
            "(SELECT COALESCE(SUM(qty_ordered * unit_cost_paise), 0) FROM retail_po_lines x WHERE x.po_id = po.po_id) AS value "
            "FROM retail_purchase_orders po JOIN retail_suppliers s ON s.supplier_id = po.supplier_id "
            f"JOIN retail_locations l ON l.location_id = po.location_id WHERE {cond} "
            "ORDER BY po.created_at DESC LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
    for r in rows:
        r["value"] = gst.rupees(r["value"])
    return {"purchase_orders": rows, "total": total, "page": page, "size": size}


def get_po(scope: Scope, po_id: str) -> dict[str, Any]:
    scope.require("read")
    with db.tx() as t:
        po = _po(t, scope, po_id)
    if scope.is_staff and po["location_id"] != scope.location_id:
        raise RetailError("Purchase order not found.", 404)
    return po


def receive_po(scope: Scope, po_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Goods receipt: add the received units to stock (bin per line), update cost and the PO status."""
    scope.require("purchase.receive")
    items = v.lines(data.get("lines"))
    supplier_invoice = v.text(data.get("supplier_invoice"), "Supplier invoice no.", hi=40, required=False)
    note = v.text(data.get("note"), "Note", hi=300, required=False)
    with db.tx() as t:
        po = _po(t, scope, po_id)
        scope.check_location(po["location_id"])
        if po["status"] in ("received", "cancelled"):
            raise RetailError(f"This PO is already {po['status']}; nothing is still open on it.")
        if po["status"] != "ordered" and po["status"] != "partial":
            raise RetailError("Mark the PO as ordered before receiving against it.")
        by_line = {int(ln["line_no"]): ln for ln in po["lines"]}
        grn_id = db.new_id()
        number = gst.document_number("GRN", _fy(), db.next_number(t, scope.brand_key, "grn", _fy()))
        t.run("INSERT INTO retail_grns (grn_id, brand_key, grn_number, po_id, location_id, supplier_invoice, note, "
              "received_by, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
              (grn_id, scope.brand_key, number, po_id, po["location_id"], supplier_invoice, note, scope.actor,
               db.now_iso()))
        units, n = 0, 0
        for i in items:
            q = v.qty(i.get("qty"), lo=0)
            if q == 0:
                continue
            ln = by_line.get(int(i.get("line_no") or 0))
            if not ln:
                raise RetailError("Unknown PO line.")
            if q > ln["open_qty"]:
                raise RetailError(f"{ln['sku']}: only {ln['open_qty']} still open on this PO.")
            b = v.bin_code(i.get("bin_code"))
            stock._known_bin(t, po["location_id"], b)
            cost = int(ln["unit_cost_paise"])
            stock.update_avg_cost(t, scope.brand_key, ln["variant_id"], q, cost)
            stock.apply_move(t, brand_key=scope.brand_key, location_id=po["location_id"], bin_code=b,
                             variant_id=ln["variant_id"], qty=q, reason="grn", actor=scope.actor, ref_type="grn",
                             ref_id=grn_id, sku=ln["sku"])
            n += 1
            t.run("INSERT INTO retail_grn_lines (grn_id, line_no, po_line_no, variant_id, qty, bin_code, unit_cost_paise) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s)", (grn_id, n, ln["line_no"], ln["variant_id"], q, b, cost))
            t.run("UPDATE retail_po_lines SET qty_received = qty_received + %s WHERE po_id = %s AND line_no = %s",
                  (q, po_id, ln["line_no"]))
            ln["open_qty"] -= q
            units += q
        if not units:
            raise RetailError("Enter the quantity received on at least one line.")
        status = "received" if all(ln["open_qty"] == 0 for ln in by_line.values()) else "partial"
        t.run("UPDATE retail_purchase_orders SET status = %s, updated_at = %s WHERE po_id = %s",
              (status, db.now_iso(), po_id))
        log.info("brand=%s actor=%s grn=%s units=%s", scope.brand_key, scope.actor, number, units)
        out = _po(t, scope, po_id)
    return {"grn_id": grn_id, "grn_number": number, "units": units, "purchase_order": out}


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def _transfer(t: Tx, scope: Scope, transfer_id: str) -> dict[str, Any]:
    tr = t.one("SELECT tr.transfer_id, tr.transfer_number, tr.status, tr.source, tr.note, tr.from_location_id, "
               "tr.to_location_id, tr.created_by, tr.created_at, tr.dispatched_by, tr.dispatched_at, tr.received_by, "
               "tr.received_at, f.code AS from_code, f.name AS from_name, d.code AS to_code, d.name AS to_name "
               "FROM retail_transfers tr JOIN retail_locations f ON f.location_id = tr.from_location_id "
               "JOIN retail_locations d ON d.location_id = tr.to_location_id "
               "WHERE tr.transfer_id = %s AND tr.brand_key = %s", (transfer_id, scope.brand_key))
    if not tr:
        raise RetailError("Transfer not found.", 404)
    if scope.is_staff and scope.location_id not in (tr["from_location_id"], tr["to_location_id"]):
        raise RetailError("Transfer not found.", 404)
    tr["lines"] = t.all(
        "SELECT tl.line_no, tl.variant_id, tl.qty_requested, tl.qty_sent, tl.qty_received, vv.sku, vv.barcode, "
        "vv.color, vv.size, p.name FROM retail_transfer_lines tl JOIN retail_variants vv ON vv.variant_id = tl.variant_id "
        "JOIN retail_products p ON p.product_id = vv.product_id WHERE tl.transfer_id = %s ORDER BY tl.line_no",
        (transfer_id,))
    tr["units_requested"] = sum(int(x["qty_requested"]) for x in tr["lines"])
    tr["units_sent"] = sum(int(x["qty_sent"]) for x in tr["lines"])
    tr["units_received"] = sum(int(x["qty_received"]) for x in tr["lines"])
    tr["shortage"] = tr["units_sent"] - tr["units_received"] if tr["status"] == "received" else 0
    return tr


def create_transfer_tx(t: Tx, scope: Scope, *, from_id: str, to_id: str, items: list[dict[str, Any]], note: Optional[str],
                       source: str = "manual") -> str:
    if from_id == to_id:
        raise RetailError("Pick two different locations.")
    src = stock.location(t, scope, from_id)
    stock.location(t, scope, to_id)
    if src["kind"] != "warehouse" and scope.role != "brand" and scope.role != "admin":
        raise RetailError("Only brand admins can send stock out of a store.", 403)
    merged: dict[str, int] = {}
    for i in items:
        vid = str(i.get("variant_id") or "")
        merged[vid] = merged.get(vid, 0) + v.qty(i.get("qty"))
    catalog.require_variants(t, scope.brand_key, list(merged))
    tid = db.new_id()
    number = gst.document_number("TR", _fy(), db.next_number(t, scope.brand_key, "transfer", _fy()))
    t.run("INSERT INTO retail_transfers (transfer_id, brand_key, transfer_number, from_location_id, to_location_id, "
          "status, source, note, created_by, created_at) VALUES (%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s)",
          (tid, scope.brand_key, number, from_id, to_id, source, note, scope.actor, db.now_iso()))
    for n, (vid, q) in enumerate(merged.items(), start=1):
        t.run("INSERT INTO retail_transfer_lines (transfer_id, line_no, variant_id, qty_requested, qty_sent, qty_received) "
              "VALUES (%s,%s,%s,%s,0,0)", (tid, n, vid, q))
    db.audit(t, scope.brand_key, scope.actor, "create", "transfer", tid, {"number": number, "source": source})
    return tid


def create_transfer(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("transfer.write")
    from_id = str(data.get("from_location_id") or scope.location_id or "")
    scope.check_location(from_id)
    source = str(data.get("source") or "manual")
    if source not in TRANSFER_SOURCES:
        raise RetailError("Unknown transfer source.")
    with db.tx() as t:
        tid = create_transfer_tx(t, scope, from_id=from_id, to_id=str(data.get("to_location_id") or ""),
                                 items=v.lines(data.get("lines")),
                                 note=v.text(data.get("note"), "Note", hi=300, required=False), source=source)
        return _transfer(t, scope, tid)


def get_transfer(scope: Scope, transfer_id: str) -> dict[str, Any]:
    scope.require("read")
    with db.tx() as t:
        tr = _transfer(t, scope, transfer_id)
        if tr["status"] == "draft":
            bins = stock.sellable_by_bin(t, tr["from_location_id"], [x["variant_id"] for x in tr["lines"]])
            for ln in tr["lines"]:
                left, picks = int(ln["qty_requested"]), []
                for b, n in bins.get(ln["variant_id"], []):
                    if left <= 0:
                        break
                    take = min(n, left)
                    picks.append({"bin_code": b, "qty": take})
                    left -= take
                ln["pick"] = picks
                ln["available"] = sum(n for _, n in bins.get(ln["variant_id"], []))
        return tr


def list_transfers(scope: Scope, *, status: str = "", direction: str = "", page: int = 1, size: int = 50) -> dict[str, Any]:
    scope.require("read")
    page, size = v.page(page, size)
    where, params = ["tr.brand_key = %s"], [scope.brand_key]
    if status:
        where.append("tr.status = %s")
        params.append(status)
    if scope.is_staff:
        if direction == "out":
            where.append("tr.from_location_id = %s")
            params.append(scope.location_id)
        elif direction == "in":
            where.append("tr.to_location_id = %s")
            params.append(scope.location_id)
        else:
            where.append("(tr.from_location_id = %s OR tr.to_location_id = %s)")
            params += [scope.location_id, scope.location_id]
    cond = " AND ".join(where)
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n FROM retail_transfers tr WHERE {cond}", params) or 0)
        rows = t.all(
            "SELECT tr.transfer_id, tr.transfer_number, tr.status, tr.source, tr.created_at, tr.dispatched_at, "
            "tr.received_at, f.code AS from_code, d.code AS to_code, tr.from_location_id, tr.to_location_id, "
            "(SELECT COALESCE(SUM(qty_requested), 0) FROM retail_transfer_lines x WHERE x.transfer_id = tr.transfer_id) AS units_requested, "
            "(SELECT COALESCE(SUM(qty_sent), 0) FROM retail_transfer_lines x WHERE x.transfer_id = tr.transfer_id) AS units_sent "
            "FROM retail_transfers tr JOIN retail_locations f ON f.location_id = tr.from_location_id "
            f"JOIN retail_locations d ON d.location_id = tr.to_location_id WHERE {cond} "
            "ORDER BY tr.created_at DESC LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
    return {"transfers": rows, "total": total, "page": page, "size": size}


def dispatch_transfer(scope: Scope, transfer_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Pick and ship: take the units out of the source bins (pick order) and mark the transfer in transit.
    lines (optional) [{line_no, qty}] sends less than requested; default sends what was requested."""
    scope.require("transfer.write")
    overrides = {int(i.get("line_no") or 0): v.qty(i.get("qty"), lo=0) for i in (data.get("lines") or [])
                 if isinstance(i, dict)}
    with db.tx() as t:
        tr = _transfer(t, scope, transfer_id)
        scope.check_location(tr["from_location_id"])
        if tr["status"] != "draft":
            raise RetailError("Only a draft transfer can be dispatched.")
        sent = 0
        for ln in tr["lines"]:
            q = overrides.get(int(ln["line_no"]), int(ln["qty_requested"]))
            if q > int(ln["qty_requested"]):
                raise RetailError(f"{ln['sku']}: can't send more than requested ({ln['qty_requested']}).")
            if q:
                stock.take_from_bins(t, scope_brand=scope.brand_key, location_id=tr["from_location_id"],
                                     variant_id=ln["variant_id"], qty=q, reason="transfer_out", actor=scope.actor,
                                     ref_type="transfer", ref_id=transfer_id, sku=ln["sku"])
            t.run("UPDATE retail_transfer_lines SET qty_sent = %s WHERE transfer_id = %s AND line_no = %s",
                  (q, transfer_id, ln["line_no"]))
            sent += q
        if not sent:
            raise RetailError("Nothing to send. Enter at least one quantity.")
        t.run("UPDATE retail_transfers SET status = 'in_transit', dispatched_by = %s, dispatched_at = %s "
              "WHERE transfer_id = %s", (scope.actor, db.now_iso(), transfer_id))
        log.info("brand=%s actor=%s transfer=%s dispatched units=%s", scope.brand_key, scope.actor,
                 tr["transfer_number"], sent)
        return _transfer(t, scope, transfer_id)


def receive_transfer(scope: Scope, transfer_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Book the units in at the destination (unassigned bin). Missing units are recorded as a shortage."""
    scope.require("transfer.receive")
    got = {int(i.get("line_no") or 0): v.qty(i.get("qty"), lo=0) for i in (data.get("lines") or [])
           if isinstance(i, dict)}
    with db.tx() as t:
        tr = _transfer(t, scope, transfer_id)
        scope.check_location(tr["to_location_id"])
        if tr["status"] != "in_transit":
            raise RetailError("Only an in-transit transfer can be received.")
        for ln in tr["lines"]:
            q = got.get(int(ln["line_no"]), int(ln["qty_sent"]))
            if q > int(ln["qty_sent"]):
                raise RetailError(f"{ln['sku']}: received more than was sent ({ln['qty_sent']}).")
            if q:
                stock.apply_move(t, brand_key=scope.brand_key, location_id=tr["to_location_id"], bin_code="",
                                 variant_id=ln["variant_id"], qty=q, reason="transfer_in", actor=scope.actor,
                                 ref_type="transfer", ref_id=transfer_id, sku=ln["sku"])
            t.run("UPDATE retail_transfer_lines SET qty_received = %s WHERE transfer_id = %s AND line_no = %s",
                  (q, transfer_id, ln["line_no"]))
        t.run("UPDATE retail_transfers SET status = 'received', received_by = %s, received_at = %s WHERE transfer_id = %s",
              (scope.actor, db.now_iso(), transfer_id))
        return _transfer(t, scope, transfer_id)


def cancel_transfer(scope: Scope, transfer_id: str) -> dict[str, Any]:
    scope.require("transfer.write")
    with db.tx() as t:
        tr = _transfer(t, scope, transfer_id)
        scope.check_location(tr["from_location_id"])
        if tr["status"] != "draft":
            raise RetailError("Only a draft transfer can be cancelled.")
        t.run("UPDATE retail_transfers SET status = 'cancelled' WHERE transfer_id = %s", (transfer_id,))
        db.audit(t, scope.brand_key, scope.actor, "cancel", "transfer", transfer_id, {})
        return _transfer(t, scope, transfer_id)


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------


def sales_velocity(t: Tx, brand_key: str, location_id: str, days: int) -> dict[str, float]:
    """Net units sold per day at a store over the last `days` days (returns subtract)."""
    since = (date.fromisoformat(db.today_ist()) - timedelta(days=days)).isoformat()
    rows = t.all("SELECT il.variant_id, SUM(CASE WHEN i.kind = 'invoice' THEN il.qty ELSE -il.qty END) AS units "
                 "FROM retail_invoice_lines il JOIN retail_invoices i ON i.invoice_id = il.invoice_id "
                 "WHERE i.brand_key = %s AND i.location_id = %s AND i.business_date > %s GROUP BY il.variant_id",
                 (brand_key, location_id, since))
    return {r["variant_id"]: max(int(r["units"] or 0), 0) / days for r in rows}


def replenishment(scope: Scope, *, warehouse_id: str, store_id: Optional[str] = None, cover_days: int = 14,
                  lookback_days: int = 28) -> dict[str, Any]:
    """What each store should get from the warehouse: up to max where par levels exist, otherwise enough
    for `cover_days` of recent sales. Capped by what the warehouse can send, stores in need first."""
    scope.require("replenish")
    if scope.role == "warehouse_staff":
        scope.check_location(warehouse_id)
    cover_days = max(1, min(int(cover_days), 90))
    lookback_days = max(7, min(int(lookback_days), 180))
    with db.tx() as t:
        wh = stock.location(t, scope, warehouse_id, kind="warehouse")
        stores = [s for s in t.all("SELECT location_id, code, name FROM retail_locations WHERE brand_key = %s AND "
                                   "kind = 'store' AND active = TRUE ORDER BY code", (scope.brand_key,))
                  if not store_id or s["location_id"] == store_id]
        wanted: list[dict[str, Any]] = []
        for s in stores:
            par = {r["variant_id"]: r for r in t.all(
                "SELECT variant_id, min_qty, max_qty FROM retail_par_levels WHERE location_id = %s", (s["location_id"],))}
            vel = sales_velocity(t, scope.brand_key, s["location_id"], lookback_days)
            have = stock.sellable(t, s["location_id"], list(set(par) | set(vel)))
            in_transit = {r["variant_id"]: int(r["units"] or 0) for r in t.all(
                "SELECT tl.variant_id, SUM(tl.qty_sent) AS units FROM retail_transfer_lines tl JOIN retail_transfers tr "
                "ON tr.transfer_id = tl.transfer_id WHERE tr.to_location_id = %s AND tr.status = 'in_transit' "
                "GROUP BY tl.variant_id", (s["location_id"],))}
            for vid in set(par) | set(vel):
                on_hand = have.get(vid, 0) + in_transit.get(vid, 0)
                if vid in par:
                    if on_hand >= int(par[vid]["min_qty"]):
                        continue
                    need, basis = int(par[vid]["max_qty"]) - on_hand, "par"
                else:
                    target = int(round(vel[vid] * cover_days + 0.4999))
                    need, basis = target - on_hand, "sales"
                if need > 0:
                    wanted.append({"store_id": s["location_id"], "store_code": s["code"], "variant_id": vid,
                                   "on_hand": have.get(vid, 0), "in_transit": in_transit.get(vid, 0), "need": need,
                                   "basis": basis, "daily_sales": round(vel.get(vid, 0.0), 2),
                                   "urgency": (on_hand / max(vel.get(vid, 0.0), 1e-9)) if vel.get(vid) else 0.0})
        avail = stock.sellable(t, warehouse_id, [w["variant_id"] for w in wanted])
        info = catalog.variant_rows(t, scope.brand_key, [w["variant_id"] for w in wanted])
    wanted.sort(key=lambda w: (w["urgency"], -w["need"]))
    left = dict(avail)
    by_store: dict[str, dict[str, Any]] = {}
    for w in wanted:
        give = min(w["need"], left.get(w["variant_id"], 0))
        left[w["variant_id"]] = left.get(w["variant_id"], 0) - give
        r = info.get(w["variant_id"], {})
        grp = by_store.setdefault(w["store_id"], {"store_id": w["store_id"], "store_code": w["store_code"], "lines": []})
        grp["lines"].append({**{k: w[k] for k in ("variant_id", "on_hand", "in_transit", "need", "basis", "daily_sales")},
                             "qty": give, "short": w["need"] - give, "sku": r.get("sku"), "name": r.get("name"),
                             "color": r.get("color"), "size": r.get("size")})
    groups = [g for g in by_store.values()]
    for g in groups:
        g["units"] = sum(x["qty"] for x in g["lines"])
    return {"warehouse": {"location_id": wh["location_id"], "code": wh["code"], "name": wh["name"]},
            "cover_days": cover_days, "lookback_days": lookback_days, "stores": groups,
            "units": sum(g["units"] for g in groups)}


def create_replenishment(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    """One draft transfer per store from the reviewed suggestion lines."""
    scope.require("replenish")
    wh = str(data.get("warehouse_id") or "")
    if scope.role == "warehouse_staff":
        scope.check_location(wh)
    stores = data.get("stores")
    if not isinstance(stores, list) or not stores:
        raise RetailError("Nothing to create.")
    made = []
    with db.tx() as t:
        for s in stores:
            lines = [x for x in (s.get("lines") or []) if isinstance(x, dict) and int(x.get("qty") or 0) > 0]
            if not lines:
                continue
            made.append(create_transfer_tx(t, scope, from_id=wh, to_id=str(s.get("store_id") or ""), items=lines,
                                           note="Replenishment", source="replenishment"))
        if not made:
            raise RetailError("All quantities are 0.")
        return {"transfers": [_transfer(t, scope, tid) for tid in made]}
