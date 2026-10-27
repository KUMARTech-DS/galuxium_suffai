"""Retail routes: who may call what, scope from the session, error mapping, and one flow over HTTP."""

import pytest
from fastapi.testclient import TestClient

import auth_acl
import main
import retail_db as db
import retail_staff as staff
from test_retail_flow import gstin
from test_route_auth import CAMPAIGNS, GEO

SESSIONS = {
    "brd": {"role": "brand", "username": "zara_hq", "brand_name": "Zara"},
    "hm": {"role": "brand", "username": "hm_hq", "brand_name": "H&M"},
    "adm": {"role": "admin", "username": "ops"},
    "cus": {"role": "customer", "username": "c1", "customer_id": "u1"},
    "wh": {"role": "warehouse_staff", "username": "wh_ravi", "brand_name": "Zara", "location_code": "WH1"},
    "till": {"role": "store_staff", "username": "blr_asha", "brand_name": "Zara", "location_code": "BLR01"},
    "lost": {"role": "store_staff", "username": "x", "brand_name": "Zara", "location_code": "NOPE"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.use_sqlite(str(tmp_path / "api.db"))
    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    yield TestClient(main.app, raise_server_exceptions=False)
    db.reset()


def call(c, method, path, token, **kw):
    return getattr(c, method)("/v1/retail" + path, headers={"X-Session-Token": token}, **kw)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def _setup(c):
    ok(call(c, "put", "/settings", "brd", json={"legal_name": "Zara India Retail Pvt Ltd", "gstin": gstin("29")}))
    wh = ok(call(c, "post", "/locations", "brd", json={"kind": "warehouse", "code": "WH1", "name": "DC",
                                                         "state_code": "29", "pin_code": "562123"}))
    st = ok(call(c, "post", "/locations", "brd", json={"kind": "store", "code": "BLR01", "name": "Indiranagar",
                                                         "state_code": "29", "pin_code": "560038",
                                                         "gstin": gstin("29")}))
    p = ok(call(c, "post", "/products", "brd", json={"style_code": "TS1", "name": "Tee", "category": "T-shirt",
                                                       "hsn_code": "6109", "colors": ["Black"], "sizes": ["M"],
                                                       "mrp": "999"}))
    return wh, st, p["variants"][0]


@pytest.mark.parametrize("path", ["/me", "/products", "/stock", "/pos/invoices", "/snapshot"])
def test_anonymous_and_customer_are_rejected(client, path):
    assert client.get("/v1/retail" + path).status_code == 401
    assert call(client, "get", path, "cus").status_code == 403


@pytest.mark.parametrize("method,path,kw", GEO + CAMPAIGNS, ids=[f"{m} {p}" for m, p, _ in GEO + CAMPAIGNS])
@pytest.mark.parametrize("token", ["wh", "till"])
def test_staff_cannot_use_geo_or_ads_lab(client, token, method, path, kw):
    r = getattr(client, method)(path, headers={"X-Session-Token": token}, **kw)
    assert r.status_code == 403


def test_staff_location_must_exist(client):
    _setup(client)
    assert call(client, "get", "/me", "lost").status_code == 403
    me = ok(call(client, "get", "/me", "till"))
    assert me["location"]["code"] == "BLR01" and me["can"]["pos.sell"] and not me["can"]["catalog.write"]


def test_admin_must_name_a_brand(client):
    assert call(client, "get", "/products", "adm").status_code == 400
    _setup(client)
    assert ok(call(client, "get", "/products", "adm", params={"brand": "zara"}))["total"] == 1


def test_flow_over_http_and_brand_isolation(client):
    wh, st, var = _setup(client)
    sup = ok(call(client, "post", "/suppliers", "brd", json={"name": "Knits"}))
    po = ok(call(client, "post", "/purchase-orders", "brd", json={
        "supplier_id": sup["supplier_id"], "location_id": wh["location_id"],
        "lines": [{"variant_id": var["variant_id"], "qty": 10, "unit_cost": "300"}]}))
    ok(call(client, "post", f"/purchase-orders/{po['po_id']}/order", "brd"))
    assert call(client, "post", f"/purchase-orders/{po['po_id']}/receive", "till",
                json={"lines": [{"line_no": 1, "qty": 10}]}).status_code == 403
    ok(call(client, "post", f"/purchase-orders/{po['po_id']}/receive", "wh", json={"lines": [{"line_no": 1, "qty": 10}]}))
    tr = ok(call(client, "post", "/transfers", "wh", json={"to_location_id": st["location_id"],
                                                             "lines": [{"variant_id": var["variant_id"], "qty": 4}]}))
    ok(call(client, "post", f"/transfers/{tr['transfer_id']}/dispatch", "wh"))
    ok(call(client, "post", f"/transfers/{tr['transfer_id']}/receive", "till"))

    found = ok(call(client, "get", "/variants/search", "till", params={"q": var["barcode"]}))["results"]
    assert found[0]["exact"] and found[0]["available"] == 4
    inv = ok(call(client, "post", "/pos/sales", "till", json={
        "lines": [{"variant_id": var["variant_id"], "qty": 1}], "payments": [{"mode": "cash", "amount": "1000"}]}))
    assert inv["invoice_number"] == "BLR012627-000001" and inv["change_paise"] == 100
    assert call(client, "post", "/pos/sales", "wh", json={}).status_code == 403
    assert ok(call(client, "get", f"/pos/invoices/by-number/{inv['invoice_number'].lower()}", "brd"))["invoice_id"] \
        == inv["invoice_id"]
    assert call(client, "get", f"/pos/invoices/{inv['invoice_id']}", "hm").status_code == 404
    assert ok(call(client, "get", "/products", "hm"))["total"] == 0
    rep = ok(call(client, "get", "/pos/day-report", "till"))
    assert rep["sales"]["count"] == 1 and rep["expected_cash_paise"] == 99900
    snap = ok(call(client, "get", "/snapshot", "brd"))
    assert snap["label"] == "Actual (POS)" and snap["bills_today"] == 1

    short = call(client, "post", "/pos/sales", "till", json={
        "lines": [{"variant_id": var["variant_id"], "qty": 50}], "payments": [{"mode": "cash", "amount": "50000"}]})
    assert short.status_code == 409 and "Only 3" in short.json()["detail"]


def test_import_errors_come_back_per_row(client):
    _setup(client)
    r = call(client, "post", "/catalog/import", "brd", json={
        "csv": "style_code,name,category,hsn_code,color,size,mrp\nA1,Tee,T-shirt,61,Red,M,999\n"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Nothing was imported" in detail["message"] and detail["errors"][0]["row"] == 2


def test_storage_failure_is_masked(client, monkeypatch):
    _setup(client)

    def broken():
        raise db.sqlite3.OperationalError("disk I/O error at /secret/path")

    monkeypatch.setattr(db, "_factory", broken)
    r = call(client, "get", "/products", "brd")
    assert r.status_code == 503 and "Reference:" in r.json()["detail"] and "secret" not in r.text


def test_staff_logins_are_brand_scoped(client, monkeypatch):
    _setup(client)
    calls = []

    def fake_bq(sql, params):
        calls.append((sql, dict((n, v) for n, _, v in params)))
        if sql.startswith("SELECT username FROM") and "role IN" not in sql:
            return []
        return [{"username": "blr_asha", "role": "store_staff", "location_code": "BLR01", "active": True}]

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setattr(staff, "_runner", fake_bq)
    assert call(client, "post", "/staff", "till", json={}).status_code == 403
    bad = call(client, "post", "/staff", "brd", json={"role": "warehouse_staff", "username": "new.one",
                                                       "display_name": "N", "location_code": "BLR01"})
    assert bad.status_code == 400 and "store" in bad.json()["detail"]
    made = ok(call(client, "post", "/staff", "brd", json={"role": "store_staff", "username": "new.one",
                                                            "display_name": "N", "location_code": "blr01"}))
    assert made["password"] and made["location_code"] == "BLR01"
    insert = next(p for s, p in calls if s.startswith("INSERT"))
    assert insert["brand_name"] == "Zara" and insert["password_hash"] == auth_acl.sha256_hex(made["password"])
    assert made["password"] not in repr(calls)
    assert ok(call(client, "get", "/staff", "brd"))["staff"][0]["username"] == "blr_asha"
    assert calls[-1][1]["brand_key"] == "zara"
