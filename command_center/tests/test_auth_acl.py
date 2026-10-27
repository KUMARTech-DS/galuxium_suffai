"""auth_acl: credential lookup (CSV / BigQuery), sessions, expiry, role → tab rules, brand masking."""

import pytest

import auth_acl

PASSWORD = "s3cret!"
HEADER = "username,password_hash,role,brand_name,customer_id,display_name,active,location_code\n"
RETAIL = ["inventory", "warehouse", "pos"]


@pytest.fixture
def creds(tmp_path, monkeypatch):
    h = auth_acl.sha256_hex(PASSWORD)
    path = tmp_path / "creds.csv"
    path.write_text(
        HEADER
        + f"zara,{h},brand,Zara,,Zara Team,true,\n"
        + f"cust,{h},customer,,u-1,,true,\n"
        + f"ops,{h},ADMIN,,,,,\n"
        + f"gone,{h},brand,Nike,,,false,\n"
        + f"odd,{h},superuser,,,,true,\n"
        + f"till,{h},store_staff,Zara,,Asha,true,blr01\n"
        + f"floating,{h},warehouse_staff,Zara,,,true,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOGIN_CREDS_CSV", str(path))
    monkeypatch.setenv("AUTH_SOURCE", "csv")
    monkeypatch.setattr(auth_acl, "_SESSIONS", {})
    return path


def test_csv_login_returns_public_session(creds):
    sess = auth_acl.authenticate("zara", PASSWORD)
    assert sess["role"] == "brand" and sess["brand_name"] == "Zara" and sess["display_name"] == "Zara Team"
    assert sess["allowed_tabs"] == ["geo", "ads", "campaigns"] + RETAIL
    assert sess["location_code"] is None
    assert "expires_at" not in sess and "password_hash" not in sess and len(sess["token"]) > 30


def test_store_staff_login_carries_location(creds):
    sess = auth_acl.authenticate("till", PASSWORD)
    assert sess["role"] == "store_staff" and sess["brand_name"] == "Zara" and sess["location_code"] == "BLR01"
    assert sess["allowed_tabs"] == ["pos", "inventory"]


def test_staff_login_without_location_cannot_log_in(creds):
    with pytest.raises(PermissionError):
        auth_acl.authenticate("floating", PASSWORD)


def test_destroy_user_sessions_signs_out_every_session(creds):
    a = auth_acl.authenticate("till", PASSWORD)["token"]
    b = auth_acl.authenticate("till", PASSWORD)["token"]
    keep = auth_acl.authenticate("zara", PASSWORD)["token"]
    assert auth_acl.destroy_user_sessions("till") == 2
    assert auth_acl.get_session(a) is None and auth_acl.get_session(b) is None
    assert auth_acl.get_session(keep) is not None


def test_login_trims_username_and_lowercases_role(creds):
    assert auth_acl.authenticate("  ops ", PASSWORD)["role"] == "admin"


def test_wrong_password_is_permission_error(creds):
    with pytest.raises(PermissionError):
        auth_acl.authenticate("zara", "nope")


def test_unknown_user_is_permission_error(creds):
    with pytest.raises(PermissionError):
        auth_acl.authenticate("nobody", PASSWORD)


def test_inactive_user_cannot_log_in(creds):
    with pytest.raises(PermissionError):
        auth_acl.authenticate("gone", PASSWORD)


def test_unknown_role_cannot_log_in(creds):
    with pytest.raises(PermissionError):
        auth_acl.authenticate("odd", PASSWORD)


@pytest.mark.parametrize("user,pw", [("", PASSWORD), ("   ", PASSWORD), ("zara", "")])
def test_blank_credentials_are_value_error(creds, user, pw):
    with pytest.raises(ValueError):
        auth_acl.authenticate(user, pw)


def test_missing_csv_is_permission_error(creds, monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIN_CREDS_CSV", str(tmp_path / "absent.csv"))
    with pytest.raises(PermissionError):
        auth_acl.authenticate("zara", PASSWORD)


def test_auto_source_falls_back_to_csv_when_bigquery_fails(creds, monkeypatch):
    def bq_down(*_a):
        raise RuntimeError("bq offline")

    monkeypatch.setenv("AUTH_SOURCE", "auto")
    monkeypatch.setattr(auth_acl, "lookup_user_from_bq", bq_down)
    assert auth_acl.authenticate("cust", PASSWORD)["customer_id"] == "u-1"


def test_auto_source_does_not_use_csv_when_bigquery_has_no_active_user(creds, monkeypatch):
    """Deactivating a login in BigQuery works even though the CSV still lists it as active."""
    monkeypatch.setenv("AUTH_SOURCE", "auto")
    monkeypatch.setattr(auth_acl, "lookup_user_from_bq", lambda *_a: None)
    with pytest.raises(PermissionError):
        auth_acl.authenticate("cust", PASSWORD)


def test_bq_only_source_propagates_bigquery_failure(creds, monkeypatch):
    def bq_down(*_a):
        raise RuntimeError("bq offline")

    monkeypatch.setenv("AUTH_SOURCE", "bq")
    monkeypatch.setattr(auth_acl, "lookup_user_from_bq", bq_down)
    with pytest.raises(RuntimeError):
        auth_acl.authenticate("zara", PASSWORD)


def test_bq_lookup_requires_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    with pytest.raises(RuntimeError):
        auth_acl.lookup_user_from_bq("zara", "h")


def test_session_round_trip_and_logout(creds):
    tok = auth_acl.authenticate("zara", PASSWORD)["token"]
    assert auth_acl.get_session(tok)["username"] == "zara"
    auth_acl.destroy_session(tok)
    assert auth_acl.get_session(tok) is None


def test_session_expires(creds, monkeypatch):
    tok = auth_acl.authenticate("zara", PASSWORD)["token"]
    now = auth_acl.time.time()
    monkeypatch.setattr(auth_acl.time, "time", lambda: now + auth_acl.SESSION_TTL_SEC + 1)
    assert auth_acl.get_session(tok) is None
    assert tok not in auth_acl._SESSIONS


@pytest.mark.parametrize("token", [None, "", "not-a-token"])
def test_get_session_rejects_missing_or_unknown_token(token):
    assert auth_acl.get_session(token) is None


def test_destroy_session_ignores_empty_token():
    auth_acl.destroy_session(None)
    auth_acl.destroy_session("")


@pytest.mark.parametrize(
    "role,tabs",
    [
        ("brand", ["geo", "ads", "campaigns"] + RETAIL),
        ("customer", ["style"]),
        ("admin", ["geo", "ads", "campaigns", "admin"] + RETAIL),
        ("warehouse_staff", ["warehouse", "inventory"]),
        ("store_staff", ["pos", "inventory"]),
        ("guest", []),
    ],
)
def test_tabs_for_role(role, tabs):
    assert auth_acl.tabs_for_role(role) == tabs


def test_tabs_for_role_returns_a_copy():
    auth_acl.tabs_for_role("brand").append("admin")
    assert "admin" not in auth_acl.tabs_for_role("brand")


def test_mask_metrics_rewrites_other_brands():
    payload = {
        "filters": {"brands": ["Zara", "Nike", "H&M"]},
        "garment_cells": [{"brand": "nike"}, {"brand": "zara"},
                          {"brand": "x", "brands": [{"value": "Nike", "count": 2}, {"value": "H&M", "count": 3}, "Zara"]}],
    }
    out = auth_acl.mask_metrics_for_brand(payload, "Zara")
    assert out["filters"]["brands"] == ["Zara", "Competitors"]
    assert [c["brand"] for c in out["garment_cells"]] == ["Competitors", "Zara", "Competitors"]
    assert out["garment_cells"][2]["brands"] == [{"value": "Competitors", "count": 5.0}, {"value": "Zara", "count": 1.0}]
    assert payload["garment_cells"][0]["brand"] == "nike"  # input untouched


def test_mask_metrics_without_brand_is_identity():
    payload = {"garment_cells": [{"brand": "Nike"}]}
    assert auth_acl.mask_metrics_for_brand(payload, "") is payload
