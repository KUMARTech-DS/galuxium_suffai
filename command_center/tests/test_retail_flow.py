"""Retail suite end to end on SQLite (same migration and SQL as Cloud SQL)."""

from datetime import datetime, timedelta, timezone

import pytest

import retail_catalog as catalog
import retail_db as db
import retail_gst as gst
import retail_pos as pos
import retail_stock as stock
import retail_wms as wms
from retail_db import RetailError
from retail_scope import Scope

HQ = Scope("brand", "zara_hq", "zara", "Zara")
OTHER = Scope("brand", "hm_hq", "h_m", "H&M")


def gstin(state, pan="ABCDE1234F"):
    body = f"{state}{pan}1Z"
    return body + gst.gstin_check_char(body)


@pytest.fixture
def clock(tmp_path, monkeypatch):
    db.use_sqlite(str(tmp_path / "retail.db"))
    now = {"t": datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(db, "_clock", lambda: now["t"])
    yield now
    db.reset()


def _variant(product, color, size):
    return next(x for x in product["variants"] if x["color"] == color and x["size"] == size)


@pytest.fixture
def shop(clock):
    stock.save_settings(HQ, {"legal_name": "Zara India Retail Pvt Ltd", "gstin": gstin("29"), "return_days": 30})
    wh = stock.create_location(HQ, {"kind": "warehouse", "code": "WH1", "name": "Bengaluru DC", "state_code": "29",
                                    "pin_code": "562123", "gstin": gstin("29")})
    blr = stock.create_location(HQ, {"kind": "store", "code": "BLR01", "name": "Indiranagar", "state_code": "29",
                                     "pin_code": "560038", "city": "Bengaluru", "gstin": gstin("29")})
    other = stock.create_location(HQ, {"kind": "store", "code": "BLR02", "name": "Koramangala", "state_code": "29",
                                       "pin_code": "560034", "gstin": gstin("29")})
    stock.create_bins(HQ, wh["location_id"], {"bin_codes": "A-01, A-02", "zone": "A"})
    shirt = catalog.create_product(HQ, {"style_code": "TS100", "name": "Linen shirt", "category": "Shirt",
                                        "hsn_code": "6205", "colors": ["White", "Navy"], "sizes": ["S", "M", "L"],
                                        "mrp": "1999"})
    jacket = catalog.create_product(HQ, {"style_code": "JK200", "name": "Wool jacket", "category": "Jacket",
                                         "hsn_code": "6201", "colors": ["Black"], "sizes": ["M", "L"], "mrp": "4999"})
    sup = wms.create_supplier(HQ, {"name": "Tiruppur Knits"})
    return {"wh": wh, "blr": blr, "other": other, "shirt": shirt, "jacket": jacket, "sup": sup,
            "wh_staff": Scope("warehouse_staff", "wh_ravi", "zara", "Zara", wh["location_id"], "WH1", "warehouse"),
            "cashier": Scope("store_staff", "blr_asha", "zara", "Zara", blr["location_id"], "BLR01", "store")}


def _stock_up(shop):
    """PO for every SKU, received into the warehouse in two GRNs, then a transfer to BLR01."""
    s = shop
    lines = [{"variant_id": x["variant_id"], "qty": 20, "unit_cost": "600"} for x in s["shirt"]["variants"]]
    lines += [{"variant_id": x["variant_id"], "qty": 5, "unit_cost": "2000"} for x in s["jacket"]["variants"]]
    po = wms.create_po(HQ, {"supplier_id": s["sup"]["supplier_id"], "location_id": s["wh"]["location_id"],
                            "lines": lines})
    assert po["po_number"] == "PO2627-00001" and po["status"] == "draft"
    with pytest.raises(RetailError):
        wms.receive_po(s["wh_staff"], po["po_id"], {"lines": [{"line_no": 1, "qty": 1}]})
    wms.set_po_status(HQ, po["po_id"], "order")
    half = [{"line_no": ln["line_no"], "qty": 10, "bin_code": "A-01"} for ln in po["lines"][:6]]
    r1 = wms.receive_po(s["wh_staff"], po["po_id"], {"lines": half, "supplier_invoice": "TK/991"})
    assert r1["purchase_order"]["status"] == "partial" and r1["units"] == 60
    rest = [{"line_no": ln["line_no"], "qty": ln["open_qty"], "bin_code": "A-02" if ln["line_no"] > 6 else "A-01"}
            for ln in r1["purchase_order"]["lines"]]
    r2 = wms.receive_po(s["wh_staff"], po["po_id"], {"lines": rest})
    assert r2["purchase_order"]["status"] == "received" and r2["grn_number"] == "GRN2627-00002"
    with pytest.raises(RetailError, match="still open"):
        wms.receive_po(s["wh_staff"], po["po_id"], {"lines": [{"line_no": 1, "qty": 1}]})

    white_m, jacket_m = _variant(s["shirt"], "White", "M"), _variant(s["jacket"], "Black", "M")
    tr = wms.create_transfer(s["wh_staff"], {"to_location_id": s["blr"]["location_id"],
                                             "lines": [{"variant_id": white_m["variant_id"], "qty": 6},
                                                       {"variant_id": jacket_m["variant_id"], "qty": 2}]})
    assert tr["status"] == "draft" and tr["from_code"] == "WH1"
    picked = wms.get_transfer(s["wh_staff"], tr["transfer_id"])
    assert picked["lines"][0]["pick"] == [{"bin_code": "A-01", "qty": 6}]
    sent = wms.dispatch_transfer(s["wh_staff"], tr["transfer_id"], {})
    assert sent["status"] == "in_transit" and sent["units_sent"] == 8
    with pytest.raises(RetailError):
        wms.receive_transfer(s["wh_staff"], tr["transfer_id"], {})
    got = wms.receive_transfer(s["cashier"], tr["transfer_id"],
                               {"lines": [{"line_no": 1, "qty": 5}, {"line_no": 2, "qty": 2}]})
    assert got["status"] == "received" and got["shortage"] == 1
    return white_m, jacket_m


def test_purchase_transfer_sale_return_day_close(shop):
    white_m, jacket_m = _stock_up(shop)
    s, cashier = shop, shop["cashier"]
    with db.tx() as t:
        assert stock.sellable(t, s["wh"]["location_id"], [white_m["variant_id"]])[white_m["variant_id"]] == 14
        assert stock.sellable(t, s["blr"]["location_id"], [white_m["variant_id"]])[white_m["variant_id"]] == 5
    prod = catalog.get_product(HQ, s["shirt"]["product_id"])
    assert _variant(prod, "White", "M")["avg_cost"] == 600.0

    sale = {"lines": [{"variant_id": white_m["variant_id"], "qty": 2, "discount_pct": 10},
                      {"variant_id": jacket_m["variant_id"], "qty": 1}],
            "customer": {"name": "Meera", "phone": "+91 98450 12345"},
            "payments": [{"mode": "card", "amount": "4000"}, {"mode": "cash", "amount": "5000"}],
            "client_ref": "till1-0001"}
    inv = pos.create_sale(cashier, sale)
    shirt_ln, jacket_ln = inv["lines"]
    assert inv["invoice_number"] == "BLR012627-000001"
    assert shirt_ln["gst_rate_bp"] == 500 and jacket_ln["gst_rate_bp"] == 1800
    assert shirt_ln["discount_paise"] == 39980 and shirt_ln["taxable_paise"] == 342686
    assert shirt_ln["cgst_paise"] == shirt_ln["sgst_paise"] == 8567 and jacket_ln["igst_paise"] == 0
    assert inv["total_paise"] == 859700 and inv["round_off_paise"] == -20
    assert inv["change_paise"] == 40300
    assert {p["mode"]: p["amount_paise"] for p in inv["payments"]} == {"card": 400000, "cash": 459700}
    assert inv["customer_phone"].endswith("2345") and "98450" not in inv["customer_phone"]
    assert inv["amount_in_words"] == "Rupees Eight Thousand Five Hundred Ninety Seven Only"

    again = pos.create_sale(cashier, sale)
    assert again["duplicate"] and again["invoice_id"] == inv["invoice_id"]
    with db.tx() as t:
        assert stock.sellable(t, s["blr"]["location_id"], [white_m["variant_id"]])[white_m["variant_id"]] == 3

    cn = pos.create_return(cashier, inv["invoice_id"], {
        "reason": "Size exchange",
        "lines": [{"line_no": 1, "qty": 1}, {"line_no": 2, "qty": 1, "damaged": True}],
        "refunds": [{"mode": "cash", "amount": "4597"}, {"mode": "card", "amount": "2201"}]})
    assert cn["kind"] == "credit_note" and cn["invoice_number"] == "BLR012627R000001"
    assert cn["original_invoice_number"] == inv["invoice_number"]
    assert cn["total_paise"] == 679800
    with db.tx() as t:
        assert stock.sellable(t, s["blr"]["location_id"], [white_m["variant_id"]])[white_m["variant_id"]] == 4
        assert t.scalar("SELECT on_hand FROM retail_stock WHERE location_id = %s AND bin_code = 'DMG' AND variant_id = %s",
                        (s["blr"]["location_id"], jacket_m["variant_id"])) == 1
    with pytest.raises(RetailError, match="only 1 left"):
        pos.create_return(cashier, inv["invoice_id"], {"reason": "x", "lines": [{"line_no": 1, "qty": 2}]})

    with pytest.raises(RetailError, match="Refund mode"):
        pos.create_return(cashier, inv["invoice_id"], {"reason": "x", "lines": [{"line_no": 1, "qty": 1}],
                                                       "refund_mode": "cheque"})
    last = pos.create_return(cashier, inv["invoice_id"], {"reason": "Changed mind", "lines": [{"line_no": 1, "qty": 1}]})
    credited = sum(int(ln["taxable_paise"]) for c in (cn, last) for ln in c["lines"] if ln["original_line_no"] == 1)
    assert credited == shirt_ln["taxable_paise"]
    assert last["payments"][0]["mode"] == "card"

    rep = pos.day_report(cashier)
    assert rep["sales"]["count"] == 1 and rep["returns"]["count"] == 2
    assert rep["net_sales_paise"] == inv["total_paise"] - cn["total_paise"] - last["total_paise"]
    assert rep["expected_cash_paise"] == 0
    assert rep["payments"]["card"]["net_paise"] == 400000 - 220100 - 179900
    assert rep["net_units"] == 0
    assert sum(r["taxable_paise"] for r in rep["tax_by_rate"]) == 0

    summary = pos.sales_summary(HQ)
    assert summary["label"] == "Actual (POS)" and summary["units"] == 0


def test_inter_state_buyer_gets_igst(shop):
    white_m, _ = _stock_up(shop)
    inv = pos.create_sale(shop["cashier"], {
        "lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
        "customer": {"name": "Acme Traders", "gstin": gstin("27", "AAACA1234Q")},
        "payments": [{"mode": "upi", "amount": "1999", "reference": "UPI123"}]})
    ln = inv["lines"][0]
    assert inv["place_of_supply"] == "27" and inv["inter_state"]
    assert ln["cgst_paise"] == ln["sgst_paise"] == 0 and ln["igst_paise"] == 9519
    cn = pos.create_return(shop["cashier"], inv["invoice_id"], {"reason": "Defect", "refund_mode": "cash",
                                                                "lines": [{"line_no": 1, "qty": 1}]})
    assert cn["inter_state"] and cn["lines"][0]["igst_paise"] == 9519
    assert cn["payments"] == [{**cn["payments"][0], "mode": "cash", "amount_paise": 199900}]


def test_failed_sale_keeps_numbering_and_stock(shop):
    white_m, _ = _stock_up(shop)
    with pytest.raises(RetailError, match="Only 5"):
        pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 6}],
                                          "payments": [{"mode": "cash", "amount": "20000"}]})
    with pytest.raises(RetailError, match="short"):
        pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
                                          "payments": [{"mode": "cash", "amount": "100"}]})
    inv = pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
                                            "payments": [{"mode": "cash", "amount": "1999"}]})
    assert inv["invoice_number"].endswith("-000001")


def test_negative_stock_when_brand_allows_it(shop):
    white_m, _ = _stock_up(shop)
    stock.save_settings(HQ, {"legal_name": "Zara India Retail Pvt Ltd", "gstin": gstin("29"),
                             "allow_negative_stock": True})
    pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 7}],
                                      "payments": [{"mode": "card", "amount": "13993"}]})
    with db.tx() as t:
        assert t.scalar("SELECT SUM(on_hand) FROM retail_stock WHERE location_id = %s AND variant_id = %s",
                        (shop["blr"]["location_id"], white_m["variant_id"])) == -2


def test_settle_rules():
    cash = lambda a: {"mode": "cash", "amount_paise": a, "reference": None}  # noqa: E731
    card = lambda a: {"mode": "card", "amount_paise": a, "reference": "x"}  # noqa: E731
    assert pos.settle([cash(50000)], 45000) == ([cash(45000)], 5000)
    assert pos.settle([], 0) == ([], 0)
    with pytest.raises(RetailError, match="more than the bill"):
        pos.settle([card(50000)], 45000)
    with pytest.raises(RetailError, match="No cash"):
        pos.settle([card(45000), cash(500)], 45000)


def test_return_window(shop, clock):
    white_m, _ = _stock_up(shop)
    inv = pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
                                            "payments": [{"mode": "cash", "amount": "1999"}]})
    clock["t"] += timedelta(days=31)
    with pytest.raises(RetailError, match="return window"):
        pos.create_return(shop["cashier"], inv["invoice_id"], {"reason": "late", "lines": [{"line_no": 1, "qty": 1}]})


def test_staff_are_limited_to_their_location_and_role(shop):
    white_m, _ = _stock_up(shop)
    cashier, wh_staff = shop["cashier"], shop["wh_staff"]
    sale = {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}], "payments": [{"mode": "cash", "amount": "1999"}]}
    with pytest.raises(RetailError) as e:
        pos.create_sale(cashier, {**sale, "location_id": shop["other"]["location_id"]})
    assert e.value.status == 403
    with pytest.raises(RetailError) as e:
        pos.create_sale(wh_staff, sale)
    assert e.value.status == 403
    with pytest.raises(RetailError) as e:
        catalog.create_product(cashier, {"style_code": "X1", "name": "x", "category": "x", "hsn_code": "6109",
                                         "colors": ["Red"], "sizes": ["M"], "mrp": "999"})
    assert e.value.status == 403
    with pytest.raises(RetailError) as e:
        stock.adjust(wh_staff, shop["blr"]["location_id"], {"reason": "lost", "lines": [
            {"variant_id": white_m["variant_id"], "qty": 1}]})
    assert e.value.status == 403
    inv = pos.create_sale(cashier, sale)
    other_cashier = Scope("store_staff", "k_ram", "zara", "Zara", shop["other"]["location_id"], "BLR02", "store")
    with pytest.raises(RetailError) as e:
        pos.get_invoice(other_cashier, inv["invoice_id"])
    assert e.value.status == 404
    with pytest.raises(RetailError) as e:
        pos.get_invoice(OTHER, inv["invoice_id"])
    assert e.value.status == 404
    assert catalog.list_products(OTHER)["total"] == 0

    wh_id = shop["wh"]["location_id"]
    for read in (lambda: stock.stock_view(cashier, location_id=wh_id), lambda: stock.moves(cashier, location_id=wh_id),
                 lambda: stock.bin_stock(cashier, wh_id), lambda: stock.list_bins(cashier, wh_id),
                 lambda: catalog.search_variants(cashier, q="TS100", location_id=cashier.default_location(wh_id))):
        with pytest.raises(RetailError) as e:
            read()
        assert e.value.status == 403
    own = stock.stock_view(cashier)
    assert own["location_id"] == shop["blr"]["location_id"]
    assert {r["location_code"] for r in own["rows"]} == {"BLR01"}
    assert [x["code"] for x in stock.valuation(cashier)["locations"]] == ["BLR01"]
    assert {x["code"] for x in stock.valuation(HQ)["locations"]} >= {"WH1", "BLR01"}


def test_replenishment_fills_par_from_warehouse(shop):
    white_m, _ = _stock_up(shop)
    navy_s = _variant(shop["shirt"], "Navy", "S")
    stock.set_par_levels(HQ, shop["blr"]["location_id"], {"lines": [
        {"variant_id": white_m["variant_id"], "min_qty": 6, "max_qty": 10},
        {"variant_id": navy_s["variant_id"], "min_qty": 2, "max_qty": 30}]})
    plan = wms.replenishment(shop["wh_staff"], warehouse_id=shop["wh"]["location_id"])
    store = next(g for g in plan["stores"] if g["store_code"] == "BLR01")
    by_sku = {ln["sku"]: ln for ln in store["lines"]}
    assert by_sku[white_m["sku"]]["qty"] == 5
    assert by_sku[navy_s["sku"]]["qty"] == 20 and by_sku[navy_s["sku"]]["short"] == 10
    made = wms.create_replenishment(shop["wh_staff"], {"warehouse_id": shop["wh"]["location_id"], "stores": [
        {"store_id": store["store_id"], "lines": store["lines"]}]})
    assert made["transfers"][0]["source"] == "replenishment" and made["transfers"][0]["units_requested"] == 25


def test_catalog_import_is_all_or_nothing(shop):
    bad = "style_code,name,category,hsn_code,color,size,mrp\nPT1,Chino,Trousers,6203,Beige,32,2499\nPT2,,Trousers,62,Blue,32,x\n"
    with pytest.raises(RetailError) as e:
        catalog.import_catalog_csv(HQ, bad)
    assert e.value.status == 422 and e.value.errors and e.value.errors[0]["row"] == 3
    assert catalog.list_products(HQ, q="PT1")["total"] == 0
    good = "style_code,name,category,hsn_code,color,size,mrp\nPT1,Chino,Trousers,6203,Beige,32,2499\nPT1,Chino,Trousers,6203,Beige,34,2499\n"
    out = catalog.import_catalog_csv(HQ, good)
    assert out["rows"] == 2
    assert len(catalog.list_products(HQ, q="PT1")["products"][0]["variants"]) == 2


def test_billing_needs_seller_gstin(clock):
    loc = stock.create_location(HQ, {"kind": "store", "code": "DEL01", "name": "Saket", "state_code": "07",
                                     "pin_code": "110017"})
    p = catalog.create_product(HQ, {"style_code": "TE1", "name": "Tee", "category": "T-shirt", "hsn_code": "6109",
                                    "colors": ["Red"], "sizes": ["M"], "mrp": "799"})
    vid = p["variants"][0]["variant_id"]
    with pytest.raises(RetailError, match="legal name"):
        pos.create_sale(HQ, {"location_id": loc["location_id"], "lines": [{"variant_id": vid, "qty": 1}],
                             "payments": [{"mode": "cash", "amount": "799"}]})
    stock.save_settings(HQ, {"legal_name": "Zara India Retail Pvt Ltd", "gstin": gstin("29")})
    with pytest.raises(RetailError, match="GSTIN"):
        pos.create_sale(HQ, {"location_id": loc["location_id"], "lines": [{"variant_id": vid, "qty": 1}],
                             "payments": [{"mode": "cash", "amount": "799"}]})
