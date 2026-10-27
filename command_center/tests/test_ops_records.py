"""Admin-created customers and orders: validation, BigQuery writes, photo progress, admin-only routes."""

from __future__ import annotations

import logging
import re

import pytest

import admin_intel as ai
import ops_records as rec
import pin_areas

REAL_REQUIRE_OK = pin_areas.require_ok

CUSTOMERS = [
    {"_id": "cust-aaaa-0001", "name": "Priya Sharma", "number": "919845012210", "age": 29, "gender": "Female",
     "pin_code": "560038", "rfm_segment": "Champions"},
    {"_id": "cust-new-0009", "name": "Kiran Das", "number": "919900011122", "age": 35, "gender": "Male",
     "pin_code": "560001", "rfm_segment": "New Customers", "source": "admin", "created_at": "2026-10-19T08:00:00+00:00"},
]
ORDERS = [
    {"_id": "o1", "customer_id": "cust-aaaa-0001", "scheduled_date": "2026-07-05", "service": "Dry Clean",
     "total_number_of_clothes": 3, "amount": 1500},
    {"_id": "o9", "customer_id": "cust-new-0009", "scheduled_date": "2026-10-19", "service": "Ironing",
     "total_number_of_clothes": 4, "amount": 0, "source": "admin", "created_at": "2026-10-19T08:05:00+00:00"},
]
COMMODITIES = [
    {"customer_id": "cust-new-0009", "order_id": "o9", "item_id": "1", "name": "Shirt", "gcs_object_key": "garments/a.jpg",
     "image_status": "extracted", "source": "pipeline", "is_repeat": False},
    {"customer_id": "cust-new-0009", "order_id": "o9", "item_id": "2", "name": "Shirt", "gcs_object_key": "garments/b.jpg",
     "image_status": "extracted", "source": "pipeline", "is_repeat": True},
    {"customer_id": "cust-aaaa-0001", "order_id": "o1", "commodity_id": "g1", "name": "Kurta"},
]


@pytest.fixture(autouse=True)
def data(monkeypatch):
    monkeypatch.setattr(ai, "_dataset_loader", lambda: (CUSTOMERS, ORDERS, COMMODITIES))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("BQ_DATASET", "suffai_ops")
    monkeypatch.setenv("GCS_IMAGE_BUCKET", "proj-images")
    ai.clear_cache()
    yield
    ai.clear_cache()


@pytest.fixture
def bq(monkeypatch):
    calls = []
    known = {c["_id"] for c in CUSTOMERS}

    def runner(sql, params):
        calls.append((sql, {n: (t, v) for n, t, v in params}))
        if "rfm_segment FROM" in sql:
            return [{"id": c["_id"], "rfm_segment": c["rfm_segment"]} for c in CUSTOMERS]
        if "scheduled_date, amount FROM" in sql:
            return [dict(o) for o in ORDERS]
        if sql.startswith("SELECT"):
            return [{"id": params[0][2]}] if params[0][2] in known else []
        return []

    monkeypatch.setattr(rec, "_runner", runner)
    return calls


@pytest.fixture(autouse=True)
def areas(monkeypatch):
    """560034 is a verified Bengaluru PIN; anything else is rejected like an out-of-city lookup."""
    looked_up = []

    def require_ok(pin, request_id="-"):
        looked_up.append(pin)
        if pin != "560034":
            raise pin_areas.AreaRejected(f"PIN {pin} is outside Bengaluru.")
        return {"status": "ok", "pin_code": pin, "area_label": "Koramangala", "centroid_lat": 12.93, "centroid_lon": 77.62}

    monkeypatch.setattr(pin_areas, "require_ok", require_ok)
    return looked_up


GOOD = {"name": "Meera  Iyer", "phone": "+91 98450-77788", "address": "12, 4th Cross, Koramangala", "age": "31",
        "gender": "female", "pin_code": "560034", "rfm_segment": ""}


def test_create_customer_inserts_admin_row_and_logs_no_pii(bq, caplog):
    caplog.set_level(logging.INFO, logger="command_center")
    out = rec.create_customer({**GOOD, "rfm_segment": "Champions"}, admin="ops-admin")
    assert re.fullmatch(r"[0-9a-f]{24}", out["customer_id"])
    assert out["rfm_segment"] == "New" == rec.NEW_SEGMENT and out["pin_code"] == "560034"
    assert out["area_label"] == "Koramangala"
    sql, params = bq[-1]
    assert sql.startswith("INSERT INTO `proj.suffai_ops.customers`") and "'admin', CURRENT_TIMESTAMP()" in sql
    assert params["name"] == ("STRING", "Meera Iyer")
    assert params["number"] == ("STRING", "919845077788")
    assert params["gender"] == ("STRING", "Female")
    assert params["age"] == ("INT64", 31)
    assert params["lat"] == ("FLOAT64", 12.93) and params["lon"] == ("FLOAT64", 77.62)
    assert params["rfm_segment"] == ("STRING", "New")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "create_customer" in logged
    for pii in ("Meera", "98450", "Koramangala", out["customer_id"]):
        assert pii not in logged


@pytest.mark.parametrize("field, value, message", [
    ("phone", "12345", "10-digit"),
    ("phone", "5845012210", "10-digit"),
    ("pin_code", "56003", "PIN"),
    ("name", "M", "Name"),
    ("name", "Robert'); DROP TABLE x;--", "unsupported"),
    ("age", "7", "Age"),
    ("gender", "robot", "Gender"),
    ("gender", "Unisex", "Gender"),
    ("pin_code", "400001", "outside Bengaluru"),
])
def test_create_customer_validation(bq, areas, field, value, message):
    with pytest.raises(rec.RecordError, match=message):
        rec.create_customer({**GOOD, field: value})
    assert not [c for c in bq if c[0].startswith("INSERT")]
    if field != "pin_code":
        assert areas == []  # bad input is rejected before any Google Search lookup


@pytest.mark.parametrize("raw, canonical", [("F", "Female"), ("male", "Male"), (" Other ", "Other")])
def test_create_customer_gender_is_canonical(bq, raw, canonical):
    rec.create_customer({**GOOD, "gender": raw})
    assert bq[-1][1]["gender"] == ("STRING", canonical)


def test_create_customer_blocked_when_pin_cannot_be_verified(bq, monkeypatch):
    monkeypatch.setattr(pin_areas, "require_ok", REAL_REQUIRE_OK)  # Google Search is offline in tests
    with pytest.raises(rec.RecordError, match="could not be verified"):
        rec.create_customer(GOOD)
    assert not [c for c in bq if c[0].startswith("INSERT")]


def test_create_customer_login_is_returned_once_and_stored_hashed(bq, caplog):
    import auth_acl

    caplog.set_level(logging.INFO, logger="command_center")
    out = rec.create_customer({**GOOD, "create_login": True}, admin="ops-admin")
    login = out["login"]
    assert login["username"] == "cust-" + out["short_id"] and len(login["password"]) >= 12
    sql, params = bq[-1]
    assert sql.startswith("INSERT INTO `proj.suffai_ops.login_cred_db`") and "'customer'" in sql
    assert params["password_hash"] == ("STRING", auth_acl.sha256_hex(login["password"]))
    assert params["customer_id"] == ("STRING", out["customer_id"])
    assert "Meera" not in params["display_name"][1]
    assert login["password"] not in str(params)  # only the hash is written
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "create_login" in logged and login["password"] not in logged and auth_acl.sha256_hex(login["password"]) not in logged


def test_create_customer_without_login_writes_no_login(bq):
    out = rec.create_customer({**GOOD, "create_login": "yes"})  # only a real true creates one
    assert "login" not in out and not [c for c in bq if "login_cred_db" in c[0]]


def test_login_username_taken_uses_full_id(bq, monkeypatch):
    def runner(sql, params):
        bq.append((sql, {n: (t, v) for n, t, v in params}))
        return [{"username": params[0][2]}] if "FROM `proj.suffai_ops.login_cred_db`" in sql else []

    monkeypatch.setattr(rec, "_runner", runner)
    assert rec.create_customer_login("abcdef0123456789abcdef01")["username"] == "cust-abcdef0123456789abcdef01"


def test_login_failure_keeps_the_customer(bq, monkeypatch):
    real = rec._runner

    def runner(sql, params):
        if "login_cred_db" in sql:
            raise rec.RecordsUnavailable("BigQuery write failed: Forbidden")
        return real(sql, params)

    monkeypatch.setattr(rec, "_runner", runner)
    out = rec.create_customer({**GOOD, "create_login": True})
    assert out["customer_id"] and "login" not in out and "could not be created" in out["login_error"]


def test_create_order_needs_existing_customer(bq):
    with pytest.raises(rec.RecordError, match="not found"):
        rec.create_order({"customer_id": "nobody", "service": "Dry Clean", "total_number_of_clothes": 2})
    with pytest.raises(rec.RecordError, match="Choose a customer"):
        rec.create_order({"customer_id": "bad id!", "service": "Dry Clean", "total_number_of_clothes": 2})


def test_create_order_inserts_and_returns_photo_name(bq):
    out = rec.create_order({"customer_id": "cust-aaaa-0001", "service": "Dry Clean", "scheduled_date": "",
                            "total_number_of_clothes": "5", "amount": "899.5"}, admin="ops-admin")
    sql, params = bq[-1]
    assert sql.startswith("INSERT INTO `proj.suffai_ops.orders`") and "'admin'" in sql
    assert params["scheduled_date"][0] == "DATE"
    assert params["clothes"] == ("INT64", 5) and params["amount"] == ("FLOAT64", 899.5)
    assert out["photos_expected"] == 5 and out["photos_received"] == 0
    assert out["photo_name"] == f"garments/cust-aaaa-0001_{out['order_id']}_<item_id>.jpg"
    assert out["upload_prefix"] == "gs://proj-images/"
    assert "_" not in out["order_id"]  # ids must not break the {customer}_{order}_{item} name


@pytest.mark.parametrize("values, message", [
    ({"total_number_of_clothes": 0}, "clothes"),
    ({"total_number_of_clothes": 2, "scheduled_date": "2020-01-01"}, "within a year"),
    ({"total_number_of_clothes": 2, "scheduled_date": "19/10/2026"}, "YYYY-MM-DD"),
    ({"total_number_of_clothes": 2, "amount": -5}, "Amount"),
    ({"total_number_of_clothes": 2, "service": "x"}, "Service"),
])
def test_create_order_validation(bq, values, message):
    with pytest.raises(rec.RecordError, match=message):
        rec.create_order({"customer_id": "cust-aaaa-0001", "service": "Dry Clean", **values})


def test_bigquery_failure_is_masked(monkeypatch):
    def boom(sql, params):
        raise rec.RecordsUnavailable("BigQuery write failed: Forbidden")

    monkeypatch.setattr(rec, "_runner", boom)
    with pytest.raises(rec.RecordsUnavailable):
        rec.create_customer(GOOD)


def test_form_options_have_no_segment_choice():
    o = rec.form_options()
    assert o["genders"] == ["Female", "Male", "Other"]
    assert "rfm_segments" not in o  # the segment is recomputed from orders (rfm-v1), never picked
    assert o["services"] == ["Dry Clean", "Ironing"]
    assert o["pin_codes"] == ["560001", "560038"]


def test_list_orders_photo_progress_newest_first():
    d = rec.list_orders()
    assert [o["order_id"] for o in d["items"]] == ["o9", "o1"]
    new = d["items"][0]
    # both photos count towards the order, even though the second one is a repeat of the same garment
    assert (new["photos_received"], new["photos_expected"], new["photos_pending"]) == (2, 4, 2)
    assert new["customer_name"] == "Kiran Das" and new["source"] == "admin"
    assert new["photo_name"] == "garments/cust-new-0009_o9_<item_id>.jpg"
    assert new["photo_state"] == "pending"
    old = d["items"][1]
    assert (old["photos_received"], old["photos_expected"], old["source"]) == (0, 3, "legacy")
    # its items came from the CSV: no photos will arrive, so it is not awaiting any
    assert (old["photo_state"], old["photos_pending"], old["legacy_items"]) == ("legacy", 0, 1)
    assert rec.list_orders(customer_id="cust-aaaa-0001")["total"] == 1
    assert [o["order_id"] for o in rec.list_orders(pending_only=True)["items"]] == ["o9"]


def test_new_order_without_items_is_pending(monkeypatch):
    orders = ORDERS + [{"_id": "o10", "customer_id": "cust-aaaa-0001", "scheduled_date": "2026-10-19",
                        "service": "Dry Clean", "total_number_of_clothes": 2, "amount": 0, "source": "admin"}]
    monkeypatch.setattr(ai, "_dataset_loader", lambda: (CUSTOMERS, orders, COMMODITIES))
    ai.clear_cache()
    row = next(o for o in rec.list_orders()["items"] if o["order_id"] == "o10")
    assert (row["photo_state"], row["photos_pending"]) == ("pending", 2)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

SESSIONS = {
    "adm": {"role": "admin", "username": "ops-admin"},
    "brd": {"role": "brand", "username": "zara-user", "brand_name": "Zara"},
}


@pytest.fixture
def client(monkeypatch, bq):
    from fastapi.testclient import TestClient

    import auth_acl
    import campaign_service
    import main

    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    cleared = []
    monkeypatch.setattr(campaign_service, "clear_metrics_cache", lambda: cleared.append("campaigns"))
    real_clear = ai.clear_cache
    monkeypatch.setattr(ai, "clear_cache", lambda: (cleared.append("intel"), real_clear()))
    c = TestClient(main.app)
    c.cleared = cleared
    c.main = main
    return c


ORDER = {"customer_id": "cust-aaaa-0001", "service": "Dry Clean", "total_number_of_clothes": 3}


@pytest.mark.parametrize("method, path, body", [
    ("get", "/v1/admin/records/options", None),
    ("get", "/v1/admin/orders", None),
    ("post", "/v1/admin/customers", {**GOOD, "age": 31}),
    ("post", "/v1/admin/orders", ORDER),
])
def test_record_routes_are_admin_only(client, bq, method, path, body):
    kw = {"json": body} if body else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers={"X-Session-Token": "brd"}, **kw).status_code == 403
    assert not [c for c in bq if c[0].startswith("INSERT")]


def test_create_customer_route_clears_caches(client, bq):
    client.main._metrics_cache.update(payload={"stale": True}, at=1.0)
    r = client.post("/v1/admin/customers", json={**GOOD, "age": 31}, headers={"X-Session-Token": "adm"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["ok"] is True and r.json()["pin_code"] == "560034"
    assert client.main._metrics_cache["payload"] is None
    assert {"intel", "campaigns"} <= set(client.cleared)
    assert bq[-1][1]["lat"] == ("FLOAT64", 12.93)


def test_create_customer_route_returns_login_without_caching(client, bq):
    r = client.post("/v1/admin/customers", json={**GOOD, "age": 31, "create_login": True}, headers={"X-Session-Token": "adm"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["login"]["username"].startswith("cust-") and r.json()["login"]["password"]


def test_create_routes_return_400_with_reason(client):
    h = {"X-Session-Token": "adm"}
    r = client.post("/v1/admin/customers", json={**GOOD, "age": 31, "pin_code": "12"}, headers=h)
    assert r.status_code == 400 and "PIN" in r.json()["detail"]
    r = client.post("/v1/admin/customers", json={**GOOD, "age": 31, "pin_code": "400001"}, headers=h)
    assert r.status_code == 400 and "outside Bengaluru" in r.json()["detail"]
    r = client.post("/v1/admin/orders", json={"customer_id": "nobody", "service": "Dry Clean", "total_number_of_clothes": 2}, headers=h)
    assert r.status_code == 400 and "not found" in r.json()["detail"]


def test_create_order_route_and_list(client):
    h = {"X-Session-Token": "adm"}
    r = client.post("/v1/admin/orders", json=ORDER, headers=h)
    assert r.status_code == 200 and r.json()["photos_expected"] == 3
    r = client.get("/v1/admin/orders", params={"pending": "true", "size": 5}, headers=h)
    assert r.status_code == 200 and r.json()["size"] == 5
    assert all(o["photos_pending"] > 0 for o in r.json()["items"])
    r = client.get("/v1/admin/records/options", headers=h)
    assert r.json()["new_segment"] == rec.NEW_SEGMENT and r.json()["photo_pattern"] == rec.PHOTO_PATTERN


def test_recompute_rfm_writes_only_changed_segments(bq):
    res = rec.recompute_rfm()
    merge = [c for c in bq if c[0].startswith("MERGE")]
    assert len(merge) == 1 and "UNNEST(@rows)" in merge[0][0]
    typ, rows = merge[0][1]["rows"]
    # one order each in the window: neither has the frequency for Champions any more
    assert typ == "STRUCTS" and rows == [{"id": "cust-aaaa-0001", "seg": "Potential Loyalist"},
                                         {"id": "cust-new-0009", "seg": "Potential Loyalist"}]
    assert res == {"rule_version": "rfm-v1", "customers": 2, "changed": 2, "applied": True,
                   "by_segment": {"Potential Loyalist": 2}}


def test_recompute_rfm_dry_run_and_no_change(bq, monkeypatch):
    assert rec.recompute_rfm(apply=False)["applied"] is False
    assert not [c for c in bq if c[0].startswith("MERGE")]

    def settled(sql, params):
        bq.append((sql, {}))
        if "rfm_segment FROM" in sql:
            return [{"id": c["_id"], "rfm_segment": "Potential Loyalist"} for c in CUSTOMERS]
        return [dict(o) for o in ORDERS]

    monkeypatch.setattr(rec, "_runner", settled)
    assert rec.recompute_rfm()["changed"] == 0
    assert not [c for c in bq if c[0].startswith("MERGE")]


def test_order_route_recomputes_rfm_after_saving(client, bq, monkeypatch):
    h = {"X-Session-Token": "adm"}
    assert client.post("/v1/admin/orders", json=ORDER, headers=h).status_code == 200
    sqls = [c[0] for c in bq]
    assert sqls.index(next(s for s in sqls if s.startswith("INSERT"))) < sqls.index(next(s for s in sqls if s.startswith("MERGE")))

    def boom(**kw):
        raise rec.RecordsUnavailable("BigQuery write failed: Forbidden")

    monkeypatch.setattr(rec, "recompute_rfm", boom)
    assert client.post("/v1/admin/orders", json=ORDER, headers=h).status_code == 200  # order saved; segments catch up later


def test_records_unavailable_is_masked(client, monkeypatch):
    def boom(sql, params):
        raise rec.RecordsUnavailable("BigQuery write failed: Forbidden proj.suffai_ops")

    monkeypatch.setattr(rec, "_runner", boom)
    r = client.post("/v1/admin/customers", json={**GOOD, "age": 31}, headers={"X-Session-Token": "adm"})
    assert r.status_code == 503
    assert "Forbidden" not in r.text and "suffai_ops" not in r.text
