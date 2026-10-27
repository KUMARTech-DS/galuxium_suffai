"""Store billing: GST tax invoices with recorded payments, returns as credit notes, end-of-day report.

Payments are recorded, not processed: the cashier enters what was collected by cash, card, UPI or wallet.
Invoice and credit note numbers run per store and financial year without gaps, because the number is
taken inside the same transaction that writes the document. A sale retried with the same client_ref
returns the first invoice instead of billing twice.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable, Optional

import retail_catalog as catalog
import retail_db as db
import retail_gst as gst
import retail_stock as stock
import retail_valid as v
from retail_db import RetailError, Tx
from retail_scope import Scope

log = logging.getLogger("command_center.retail_pos")

PAY_MODES = ("cash", "card", "upi", "wallet")
MAX_PAYMENTS = 6
RUNNING_CAMPAIGN_STATUSES = ("launched", "exported")

# Set by retail_api.init: (scope, campaign_id) -> campaign of the scope's brand or None, and
# (scope) -> the brand's campaigns. They read the campaign tables; the POS never writes them.
campaign_lookup: Optional[Callable[[Scope, str], Optional[dict[str, Any]]]] = None
campaign_list: Optional[Callable[[Scope], list[dict[str, Any]]]] = None


def _store(t: Tx, scope: Scope, location_id: Optional[str]) -> dict[str, Any]:
    lid = scope.default_location(location_id)
    scope.check_location(lid)
    return stock.location(t, scope, lid, kind="store")


def _seller(st: dict[str, Any], loc: dict[str, Any]) -> dict[str, Any]:
    if not st.get("legal_name"):
        raise RetailError("Add the brand's legal name and GSTIN in Warehouse > Settings before billing.", 409)
    gstin = loc.get("gstin")
    if not gstin and st.get("gstin") and st["gstin"][:2] == loc["state_code"]:
        gstin = st["gstin"]
    if not gstin:
        raise RetailError(f"Add this store's GSTIN ({loc.get('state') or loc['state_code']}) before billing.", 409)
    addr = ", ".join(x for x in (loc.get("address"), loc.get("city"), loc.get("pin_code")) if x)
    return {"legal_name": st["legal_name"], "trade_name": st.get("brand_name"), "gstin": gstin,
            "address": addr or st.get("address") or "", "state_code": loc["state_code"],
            "state": gst.STATES.get(loc["state_code"], ""), "store_code": loc["code"], "store_name": loc["name"],
            "phone": loc.get("phone"), "footer": st.get("invoice_footer")}


def _payments(raw: Any, field: str = "Payment") -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PAYMENTS:
        raise RetailError(f"Up to {MAX_PAYMENTS} payment entries.")
    out = []
    for p in raw:
        if not isinstance(p, dict):
            raise RetailError(f"{field} entries must be objects.")
        mode = str(p.get("mode") or "").strip().lower()
        if mode not in PAY_MODES:
            raise RetailError("Payment mode must be cash, card, UPI or wallet.")
        out.append({"mode": mode, "amount_paise": v.money(p.get("amount"), f"{field} ({mode})", lo_paise=1),
                    "reference": v.text(p.get("reference"), "Payment reference", hi=60, required=False)})
    return out


def settle(pays: list[dict[str, Any]], total: int) -> tuple[list[dict[str, Any]], int]:
    """Check the tender covers the bill. Change comes out of cash only; card, UPI and wallet can't exceed
    the bill. Returns the payments to record (cash net of change) and the change to hand back."""
    paid = sum(p["amount_paise"] for p in pays)
    cash = sum(p["amount_paise"] for p in pays if p["mode"] == "cash")
    if paid - cash > total:
        raise RetailError("Card, UPI and wallet payments add up to more than the bill.")
    if paid < total:
        raise RetailError(f"Payment is short by ₹{gst.rupees(total - paid):,.2f}.")
    change = paid - total
    if not change:
        return pays, 0
    if cash - change <= 0:
        raise RetailError("No cash is needed for this bill. Remove the cash entry.")
    non_cash = [p for p in pays if p["mode"] != "cash"]
    refs = [p["reference"] for p in pays if p["mode"] == "cash" and p["reference"]]
    return non_cash + [{"mode": "cash", "amount_paise": cash - change, "reference": refs[0] if refs else None}], change


def _check_campaign(scope: Scope, campaign_id: Optional[str]) -> Optional[str]:
    if not campaign_id:
        return None
    if campaign_lookup is None or not campaign_lookup(scope, campaign_id):
        raise RetailError("Campaign not found for this brand.", 404)
    return campaign_id


def _take_for_sale(t: Tx, scope: Scope, *, location_id: str, variant_id: str, qty: int, sku: str, ref_id: str,
                   allow_negative: bool) -> None:
    have = stock.sellable(t, location_id, [variant_id])[variant_id]
    if have >= qty or not allow_negative:
        stock.take_from_bins(t, scope_brand=scope.brand_key, location_id=location_id, variant_id=variant_id, qty=qty,
                             reason="sale", actor=scope.actor, ref_type="invoice", ref_id=ref_id, sku=sku)
        return
    if have:
        stock.take_from_bins(t, scope_brand=scope.brand_key, location_id=location_id, variant_id=variant_id, qty=have,
                             reason="sale", actor=scope.actor, ref_type="invoice", ref_id=ref_id, sku=sku)
    stock.apply_move(t, brand_key=scope.brand_key, location_id=location_id, bin_code="", variant_id=variant_id,
                     qty=-(qty - have), reason="sale", actor=scope.actor, ref_type="invoice", ref_id=ref_id,
                     note="sold without stock on hand", allow_negative=True, sku=sku)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

_INV_COLS = ("invoice_id, location_id, kind, invoice_number, original_invoice_id, business_date, fy, customer_name, "
             "customer_phone, customer_gstin, place_of_supply, campaign_id, units, gross_paise, discount_paise, "
             "taxable_paise, cgst_paise, sgst_paise, igst_paise, round_off_paise, total_paise, change_paise, reason, "
             "seller, created_by, created_at")


def _invoice(t: Tx, scope: Scope, invoice_id: str) -> dict[str, Any]:
    inv = t.one(f"SELECT {_INV_COLS} FROM retail_invoices WHERE invoice_id = %s AND brand_key = %s",
                (invoice_id, scope.brand_key))
    if not inv or (scope.is_staff and inv["location_id"] != scope.location_id):
        raise RetailError("Invoice not found.", 404)
    inv["seller"] = db.loads(inv["seller"]) or {}
    inv["customer_phone"] = v.mask_phone(inv["customer_phone"])
    inv["place_of_supply_name"] = gst.STATES.get(inv["place_of_supply"], "")
    inv["lines"] = t.all(
        "SELECT line_no, variant_id, sku, description, category, color, size, hsn_code, qty, mrp_paise, "
        "discount_paise, taxable_paise, gst_rate_bp, cgst_paise, sgst_paise, igst_paise, total_paise, "
        "original_line_no, restocked FROM retail_invoice_lines WHERE invoice_id = %s ORDER BY line_no", (invoice_id,))
    for ln in inv["lines"]:
        ln["restocked"] = bool(ln["restocked"])
    inv["payments"] = t.all("SELECT mode, amount_paise, reference, created_at FROM retail_payments WHERE invoice_id = %s "
                            "ORDER BY created_at, mode", (invoice_id,))
    inv["tax_summary"] = gst.tax_by_rate(inv["lines"])
    inv["amount_in_words"] = gst.amount_in_words(int(inv["total_paise"]))
    inv["inter_state"] = int(inv["igst_paise"]) > 0 or inv["place_of_supply"] != inv["seller"].get("state_code")
    if inv["kind"] == "credit_note":
        inv["original_invoice_number"] = t.scalar(
            "SELECT invoice_number FROM retail_invoices WHERE invoice_id = %s", (inv["original_invoice_id"],))
    else:
        done = _returned(t, invoice_id)
        inv["credit_notes"] = t.all("SELECT invoice_id, invoice_number, total_paise, created_at FROM retail_invoices "
                                    "WHERE original_invoice_id = %s ORDER BY created_at", (invoice_id,))
        for ln in inv["lines"]:
            ln["returned_qty"] = done.get(int(ln["line_no"]), {}).get("qty", 0)
            ln["returnable_qty"] = int(ln["qty"]) - ln["returned_qty"]
        last = date.fromisoformat(str(inv["business_date"])[:10]) + timedelta(
            days=int(stock.settings(t, scope)["return_days"]))
        inv["return_until"] = last.isoformat()
    return inv


def _returned(t: Tx, invoice_id: str) -> dict[int, dict[str, int]]:
    """original line_no -> qty and amounts already credited."""
    out: dict[int, dict[str, int]] = {}
    for r in t.all(
            "SELECT il.original_line_no, il.qty, il.discount_paise, il.taxable_paise, il.cgst_paise, il.sgst_paise, "
            "il.igst_paise, il.total_paise, il.mrp_paise FROM retail_invoice_lines il JOIN retail_invoices i "
            "ON i.invoice_id = il.invoice_id WHERE i.original_invoice_id = %s AND i.kind = 'credit_note'",
            (invoice_id,)):
        acc = out.setdefault(int(r["original_line_no"]), {k: 0 for k in ("qty",) + gst.AMOUNT_KEYS})
        acc["qty"] += int(r["qty"])
        acc["gross_paise"] += int(r["mrp_paise"]) * int(r["qty"])
        for k in gst.AMOUNT_KEYS:
            if k != "gross_paise":
                acc[k] += int(r[k])
    return out


def _write_document(t: Tx, scope: Scope, *, loc: dict[str, Any], kind: str, number: str, fy: str,
                    totals: dict[str, int], lines: list[dict[str, Any]], pays: list[dict[str, Any]],
                    customer: dict[str, Any], place: str, seller: dict[str, Any], change: int = 0,
                    original_id: Optional[str] = None, campaign_id: Optional[str] = None,
                    reason: Optional[str] = None, client_ref: Optional[str] = None) -> str:
    iid, now = db.new_id(), db.now_iso()
    t.run(
        "INSERT INTO retail_invoices (invoice_id, brand_key, location_id, kind, invoice_number, original_invoice_id, "
        "business_date, fy, customer_name, customer_phone, customer_gstin, place_of_supply, campaign_id, units, "
        "gross_paise, discount_paise, taxable_paise, cgst_paise, sgst_paise, igst_paise, round_off_paise, total_paise, "
        "change_paise, reason, seller, client_ref, created_by, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (iid, scope.brand_key, loc["location_id"], kind, number, original_id, db.today_ist(), fy,
         customer.get("name"), customer.get("phone"), customer.get("gstin"), place, campaign_id, totals["units"],
         totals["gross_paise"], totals["discount_paise"], totals["taxable_paise"], totals["cgst_paise"],
         totals["sgst_paise"], totals["igst_paise"], totals["round_off_paise"], totals["total_paise"], change, reason,
         db.dumps(seller), client_ref, scope.actor, now))
    for n, ln in enumerate(lines, start=1):
        t.run(
            "INSERT INTO retail_invoice_lines (invoice_id, line_no, variant_id, sku, description, category, color, size, "
            "hsn_code, qty, mrp_paise, discount_paise, taxable_paise, gst_rate_bp, cgst_paise, sgst_paise, igst_paise, "
            "total_paise, original_line_no, restocked) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (iid, n, ln["variant_id"], ln["sku"], ln["description"], ln.get("category"), ln.get("color"),
             ln.get("size"), ln["hsn_code"], ln["qty"], ln["mrp_paise"], ln["discount_paise"], ln["taxable_paise"],
             ln["gst_rate_bp"], ln["cgst_paise"], ln["sgst_paise"], ln["igst_paise"], ln["total_paise"],
             ln.get("original_line_no"), bool(ln.get("restocked", True))))
    for p in pays:
        t.run("INSERT INTO retail_payments (payment_id, invoice_id, mode, amount_paise, reference, created_at) "
              "VALUES (%s,%s,%s,%s,%s,%s)", (db.new_id(), iid, p["mode"], p["amount_paise"], p["reference"], now))
    return iid


# ---------------------------------------------------------------------------
# Sale
# ---------------------------------------------------------------------------


def _customer(raw: Any) -> dict[str, Any]:
    c = raw if isinstance(raw, dict) else {}
    out = {"name": v.text(c.get("name"), "Customer name", hi=80, required=False), "phone": v.phone(c.get("phone")),
           "gstin": v.gstin(c.get("gstin"))}
    if out["gstin"] and not out["name"]:
        raise RetailError("Enter the buyer's business name for a GST (B2B) invoice.")
    return out


def quote(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    """Price a cart without saving: per-line GST and the bill total (for the till screen)."""
    scope.require("pos.sell")
    with db.tx() as t:
        loc = _store(t, scope, data.get("location_id"))
        cust = _customer(data.get("customer"))
        place = cust["gstin"][:2] if cust["gstin"] else loc["state_code"]
        lines = _price(t, scope, v.lines(data.get("lines")), inter_state=place != loc["state_code"])
        have = stock.sellable(t, loc["location_id"], [ln["variant_id"] for ln in lines])
    for ln in lines:
        ln["available"] = have.get(ln["variant_id"], 0)
    return {"lines": lines, "totals": gst.bill_totals(lines), "place_of_supply": place,
            "inter_state": place != loc["state_code"]}


def _price(t: Tx, scope: Scope, items: list[dict[str, Any]], *, inter_state: bool) -> list[dict[str, Any]]:
    rows = catalog.require_variants(t, scope.brand_key, [str(i.get("variant_id") or "") for i in items], sellable=True)
    out = []
    for i in items:
        r = rows[str(i["variant_id"])]
        try:
            pl = gst.price_line(mrp_paise=int(r["mrp_paise"]), qty=v.qty(i.get("qty"), hi=999),
                                discount_bp=v.percent_bp(i.get("discount_pct") or 0), rule=r["gst_rule"],
                                fixed_bp=r["gst_rate_bp"], inter_state=inter_state)
        except ValueError as e:
            raise RetailError(str(e).capitalize() + ".") from None
        out.append({**pl, "variant_id": r["variant_id"], "sku": r["sku"], "description": r["name"],
                    "category": r["category"], "color": r["color"], "size": r["size"], "hsn_code": r["hsn_code"]})
    return out


def create_sale(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("pos.sell")
    items = v.lines(data.get("lines"))
    cust = _customer(data.get("customer"))
    pays_in = _payments(data.get("payments"))
    client_ref = v.text(data.get("client_ref"), "client_ref", hi=64, required=False)
    campaign_id = _check_campaign(scope, v.text(data.get("campaign_id"), "Campaign", hi=64, required=False))
    with db.tx() as t:
        if client_ref:
            prev = t.scalar("SELECT invoice_id FROM retail_invoices WHERE brand_key = %s AND client_ref = %s",
                            (scope.brand_key, client_ref))
            if prev:
                return {**_invoice(t, scope, prev), "duplicate": True}
        loc = _store(t, scope, data.get("location_id"))
        st = stock.settings(t, scope)
        seller = _seller(st, loc)
        place = cust["gstin"][:2] if cust["gstin"] else loc["state_code"]
        lines = _price(t, scope, items, inter_state=place != loc["state_code"])
        totals = gst.bill_totals(lines)
        pays, change = settle(pays_in, totals["total_paise"])
        fy = gst.financial_year(date.fromisoformat(db.today_ist()))
        number = gst.invoice_number(loc["code"], fy, db.next_number(t, loc["location_id"], "invoice", fy))
        iid = _write_document(t, scope, loc=loc, kind="invoice", number=number, fy=fy, totals=totals, lines=lines,
                              pays=pays, customer=cust, place=place, seller=seller, change=change,
                              campaign_id=campaign_id, client_ref=client_ref)
        need: dict[str, int] = {}
        for ln in lines:
            need[ln["variant_id"]] = need.get(ln["variant_id"], 0) + ln["qty"]
        skus = {ln["variant_id"]: ln["sku"] for ln in lines}
        for vid, q in need.items():
            _take_for_sale(t, scope, location_id=loc["location_id"], variant_id=vid, qty=q, sku=skus[vid], ref_id=iid,
                           allow_negative=st["allow_negative_stock"])
        log.info("brand=%s store=%s actor=%s invoice=%s units=%s", scope.brand_key, loc["code"], scope.actor, number,
                 totals["units"])
        return _invoice(t, scope, iid)


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------


def create_return(scope: Scope, invoice_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Credit note for returned units of an invoice, at the store that sold them and within the return
    window. Good units go back to sellable stock, damaged ones to the DMG bin. Refunds are recorded."""
    scope.require("pos.sell")
    items = v.lines(data.get("lines"))
    reason = v.text(data.get("reason"), "Return reason", hi=200)
    refunds_in = _payments(data.get("refunds"), "Refund")
    refund_mode = str(data.get("refund_mode") or "").strip().lower() or None
    if refund_mode and refund_mode not in PAY_MODES:
        raise RetailError("Refund mode must be cash, card, UPI or wallet.")
    client_ref = v.text(data.get("client_ref"), "client_ref", hi=64, required=False)
    with db.tx() as t:
        if client_ref:
            prev = t.scalar("SELECT invoice_id FROM retail_invoices WHERE brand_key = %s AND client_ref = %s",
                            (scope.brand_key, client_ref))
            if prev:
                return {**_invoice(t, scope, prev), "duplicate": True}
        inv = _invoice(t, scope, invoice_id)
        if inv["kind"] != "invoice":
            raise RetailError("Returns are taken against a sale invoice.")
        loc = _store(t, scope, inv["location_id"])
        if db.today_ist() > inv["return_until"]:
            raise RetailError(f"The return window for this invoice ended on {inv['return_until']}.", 409)
        orig = t.all("SELECT * FROM retail_invoice_lines WHERE invoice_id = %s", (invoice_id,))
        by_no = {int(r["line_no"]): r for r in orig}
        done = _returned(t, invoice_id)
        wanted: dict[int, dict[str, int]] = {}
        for i in items:
            no = int(i.get("line_no") or 0)
            if no not in by_no:
                raise RetailError("Unknown invoice line.")
            q = v.qty(i.get("qty"), lo=0, hi=999)
            if q:
                key = (no, bool(i.get("damaged")))
                acc = wanted.setdefault(no, {"good": 0, "damaged": 0})
                acc["damaged" if key[1] else "good"] += q
        if not wanted:
            raise RetailError("Pick at least one item to return.")
        lines: list[dict[str, Any]] = []
        for no, parts in sorted(wanted.items()):
            o = by_no[no]
            before = dict(done.get(no, {}))
            before.setdefault("qty", 0)
            left = int(o["qty"]) - before["qty"]
            if parts["good"] + parts["damaged"] > left:
                raise RetailError(f"{o['sku']}: only {left} left to return on this invoice.")
            for damaged, q in ((False, parts["good"]), (True, parts["damaged"])):
                if not q:
                    continue
                cl = gst.credit_line({**o, "gross_paise": int(o["mrp_paise"]) * int(o["qty"])}, qty=q,
                                     returned_before=before["qty"], credited_before=before)
                lines.append({**cl, "variant_id": o["variant_id"], "sku": o["sku"], "description": o["description"],
                              "category": o["category"], "color": o["color"], "size": o["size"],
                              "hsn_code": o["hsn_code"], "original_line_no": no, "restocked": not damaged})
                before["qty"] += q
                for k in gst.AMOUNT_KEYS:
                    before[k] = before.get(k, 0) + cl[k]
        totals = gst.bill_totals(lines)
        default_mode = refund_mode or (inv["payments"][0]["mode"] if inv["payments"] else "cash")
        refunds = refunds_in or ([{"mode": default_mode, "amount_paise": totals["total_paise"], "reference": None}]
                                 if totals["total_paise"] else [])
        if sum(p["amount_paise"] for p in refunds) != totals["total_paise"]:
            raise RetailError(f"Refunds must add up to ₹{gst.rupees(totals['total_paise']):,.2f}.")
        fy = gst.financial_year(date.fromisoformat(db.today_ist()))
        number = gst.invoice_number(loc["code"], fy, db.next_number(t, loc["location_id"], "credit_note", fy),
                                    credit_note=True)
        cust = t.one("SELECT customer_name AS name, customer_phone AS phone, customer_gstin AS gstin "
                     "FROM retail_invoices WHERE invoice_id = %s", (invoice_id,))
        cid = _write_document(t, scope, loc=loc, kind="credit_note", number=number, fy=fy, totals=totals,
                              lines=lines, pays=refunds, customer=cust, place=inv["place_of_supply"],
                              seller=inv["seller"], original_id=invoice_id, campaign_id=inv["campaign_id"],
                              reason=reason, client_ref=client_ref)
        for ln in lines:
            stock.apply_move(t, brand_key=scope.brand_key, location_id=loc["location_id"],
                             bin_code="" if ln["restocked"] else v.DAMAGED_BIN, variant_id=ln["variant_id"],
                             qty=ln["qty"], reason="return" if ln["restocked"] else "return_damaged",
                             actor=scope.actor, ref_type="credit_note", ref_id=cid, sku=ln["sku"])
        log.info("brand=%s store=%s actor=%s credit_note=%s units=%s", scope.brand_key, loc["code"], scope.actor,
                 number, totals["units"])
        return _invoice(t, scope, cid)


# ---------------------------------------------------------------------------
# Lookups and reports
# ---------------------------------------------------------------------------


def get_invoice(scope: Scope, invoice_id: str) -> dict[str, Any]:
    scope.require("pos.read")
    with db.tx() as t:
        return _invoice(t, scope, invoice_id)


def find_invoice(scope: Scope, number: str) -> dict[str, Any]:
    scope.require("pos.read")
    with db.tx() as t:
        iid = t.scalar("SELECT invoice_id FROM retail_invoices WHERE brand_key = %s AND invoice_number = %s",
                       (scope.brand_key, (number or "").strip().upper()))
        if not iid:
            raise RetailError("No invoice with that number.", 404)
        return _invoice(t, scope, iid)


def list_invoices(scope: Scope, *, location_id: Optional[str] = None, kind: str = "", q: str = "",
                  date_from: Optional[str] = None, date_to: Optional[str] = None, page: int = 1,
                  size: int = 50) -> dict[str, Any]:
    scope.require("pos.read")
    page, size = v.page(page, size)
    where, params = ["i.brand_key = %s"], [scope.brand_key]
    lid = scope.default_location(location_id)
    if lid:
        scope.check_location(lid)
        where.append("i.location_id = %s")
        params.append(lid)
    if kind in ("invoice", "credit_note"):
        where.append("i.kind = %s")
        params.append(kind)
    s = (q or "").strip()
    if s:
        digits = "".join(ch for ch in s if ch.isdigit())
        alt = ["UPPER(i.invoice_number) LIKE %s", "LOWER(COALESCE(i.customer_name, '')) LIKE %s"]
        params += ["%" + s.upper() + "%", "%" + s.lower() + "%"]
        if len(digits) >= 4:
            alt.append("COALESCE(i.customer_phone, '') LIKE %s")
            params.append("%" + digits)
        where.append("(" + " OR ".join(alt) + ")")
    d1 = v.iso_date(date_from, "From date")
    d2 = v.iso_date(date_to, "To date")
    if d1:
        where.append("i.business_date >= %s")
        params.append(d1)
    if d2:
        where.append("i.business_date <= %s")
        params.append(d2)
    cond = " AND ".join(where)
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n FROM retail_invoices i WHERE {cond}", params) or 0)
        rows = t.all(
            "SELECT i.invoice_id, i.invoice_number, i.kind, i.business_date, i.customer_name, i.customer_phone, "
            "i.units, i.total_paise, i.created_by, i.created_at, i.campaign_id, l.code AS store_code "
            f"FROM retail_invoices i JOIN retail_locations l ON l.location_id = i.location_id WHERE {cond} "
            "ORDER BY i.created_at DESC LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
    for r in rows:
        r["customer_phone"] = v.mask_phone(r["customer_phone"])
    return {"invoices": rows, "total": total, "page": page, "size": size}


def day_report(scope: Scope, *, location_id: Optional[str] = None, day: Optional[str] = None) -> dict[str, Any]:
    """End-of-day (Z) report for one store: sales, returns, tax by rate, collections by mode, expected cash."""
    scope.require("pos.read")
    day = v.iso_date(day, "Date") or db.today_ist()
    with db.tx() as t:
        loc = _store(t, scope, location_id)
        docs = t.all("SELECT invoice_id, kind, invoice_number, units, gross_paise, discount_paise, taxable_paise, "
                     "cgst_paise, sgst_paise, igst_paise, round_off_paise, total_paise, created_by "
                     "FROM retail_invoices WHERE location_id = %s AND business_date = %s ORDER BY invoice_number",
                     (loc["location_id"], day))
        pays = t.all("SELECT i.kind, p.mode, SUM(p.amount_paise) AS amount FROM retail_payments p JOIN retail_invoices i "
                     "ON i.invoice_id = p.invoice_id WHERE i.location_id = %s AND i.business_date = %s "
                     "GROUP BY i.kind, p.mode", (loc["location_id"], day))
        lines = t.all("SELECT i.kind, il.sku, il.description, il.qty, il.gst_rate_bp, il.taxable_paise, il.cgst_paise, "
                      "il.sgst_paise, il.igst_paise FROM retail_invoice_lines il JOIN retail_invoices i "
                      "ON i.invoice_id = il.invoice_id WHERE i.location_id = %s AND i.business_date = %s",
                      (loc["location_id"], day))
    keys = ("units",) + gst.AMOUNT_KEYS + ("round_off_paise",)

    def _sum(kind: str) -> dict[str, Any]:
        rows = [d for d in docs if d["kind"] == kind]
        out = {k: sum(int(d[k]) for d in rows) for k in keys}
        out["count"] = len(rows)
        out["first"] = rows[0]["invoice_number"] if rows else None
        out["last"] = rows[-1]["invoice_number"] if rows else None
        return out

    sales, returns = _sum("invoice"), _sum("credit_note")
    modes = {m: {"collected_paise": 0, "refunded_paise": 0} for m in PAY_MODES}
    for p in pays:
        modes[p["mode"]]["collected_paise" if p["kind"] == "invoice" else "refunded_paise"] += int(p["amount"] or 0)
    for m in modes.values():
        m["net_paise"] = m["collected_paise"] - m["refunded_paise"]
    sign = {"invoice": 1, "credit_note": -1}
    tax = gst.tax_by_rate([{**ln, **{k: sign[ln["kind"]] * int(ln[k]) for k in
                                     ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise")}} for ln in lines])
    items: dict[str, dict[str, Any]] = {}
    for ln in lines:
        it = items.setdefault(ln["sku"], {"sku": ln["sku"], "description": ln["description"], "units": 0})
        it["units"] += sign[ln["kind"]] * int(ln["qty"])
    cashiers: dict[str, dict[str, Any]] = {}
    for d in docs:
        c = cashiers.setdefault(d["created_by"], {"cashier": d["created_by"], "invoices": 0, "net_paise": 0})
        c["invoices"] += d["kind"] == "invoice"
        c["net_paise"] += sign[d["kind"]] * int(d["total_paise"])
    return {
        "store": {"location_id": loc["location_id"], "code": loc["code"], "name": loc["name"]}, "date": day,
        "sales": sales, "returns": returns, "net_sales_paise": sales["total_paise"] - returns["total_paise"],
        "net_units": sales["units"] - returns["units"], "payments": modes,
        "expected_cash_paise": modes["cash"]["net_paise"], "tax_by_rate": tax,
        "top_items": sorted(items.values(), key=lambda x: -x["units"])[:10],
        "cashiers": sorted(cashiers.values(), key=lambda x: -x["net_paise"]),
    }


def sales_summary(scope: Scope, *, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  location_id: Optional[str] = None) -> dict[str, Any]:
    """Net sales per day and store, and units by category (credit notes subtracted)."""
    scope.require("read")
    d2 = v.iso_date(date_to, "To date") or db.today_ist()
    d1 = v.iso_date(date_from, "From date") or (date.fromisoformat(d2) - timedelta(days=29)).isoformat()
    if d1 > d2:
        raise RetailError("From date is after the to date.")
    where, params = ["i.brand_key = %s", "i.business_date >= %s", "i.business_date <= %s"], [scope.brand_key, d1, d2]
    lid = scope.default_location(location_id)
    if lid:
        where.append("i.location_id = %s")
        params.append(lid)
    cond = " AND ".join(where)
    neg = "CASE WHEN i.kind = 'invoice' THEN 1 ELSE -1 END"
    with db.tx() as t:
        days = t.all(f"SELECT i.business_date AS day, l.code AS store_code, SUM({neg} * i.total_paise) AS net_paise, "
                     f"SUM({neg} * i.units) AS units, SUM(CASE WHEN i.kind = 'invoice' THEN 1 ELSE 0 END) AS invoices "
                     f"FROM retail_invoices i JOIN retail_locations l ON l.location_id = i.location_id WHERE {cond} "
                     "GROUP BY i.business_date, l.code ORDER BY i.business_date, l.code", params)
        cats = t.all(f"SELECT COALESCE(il.category, '') AS category, SUM({neg} * il.qty) AS units, "
                     f"SUM({neg} * il.total_paise) AS net_paise FROM retail_invoice_lines il JOIN retail_invoices i "
                     f"ON i.invoice_id = il.invoice_id WHERE {cond} GROUP BY COALESCE(il.category, '') "
                     "ORDER BY units DESC", params)
    for r in days:
        r["day"] = str(r["day"])[:10]
        for k in ("net_paise", "units", "invoices"):
            r[k] = int(r[k] or 0)
    for r in cats:
        r["units"], r["net_paise"] = int(r["units"] or 0), int(r["net_paise"] or 0)
    return {"from": d1, "to": d2, "days": days, "categories": cats,
            "net_paise": sum(r["net_paise"] for r in days), "units": sum(r["units"] for r in days),
            "invoices": sum(r["invoices"] for r in days), "label": "Actual (POS)"}


def running_campaigns(scope: Scope, *, location_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The brand's live campaigns for tagging a bill; those targeting the store's PIN first."""
    scope.require("pos.read")
    if campaign_list is None:
        return []
    with db.tx() as t:
        loc = _store(t, scope, location_id)
    today = db.today_ist()
    out = []
    try:
        rows = campaign_list(scope)
    except Exception:  # noqa: BLE001 - campaigns are optional on the till
        log.warning("campaign list unavailable for brand=%s", scope.brand_key, exc_info=True)
        return []
    for c in rows:
        if c.get("status") not in RUNNING_CAMPAIGN_STATUSES:
            continue
        if c.get("end_date") and str(c["end_date"])[:10] < today:
            continue
        pins = [str(p) for p in (c.get("pin_codes") or [])]
        out.append({"campaign_id": c["campaign_id"], "name": c.get("campaign_name") or c.get("title") or "Campaign",
                    "start_date": c.get("start_date"), "end_date": c.get("end_date"),
                    "targets_store": bool(loc.get("pin_code")) and loc["pin_code"] in pins})
    out.sort(key=lambda c: (not c["targets_store"], c["name"]))
    return out
