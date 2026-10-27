"""Route-level auth matrix, login/health behaviour and the masked error paths in main.py."""

import pytest
from fastapi.testclient import TestClient

import auth_acl
import campaign_service as svc
import main

SESSIONS = {
    "adm": {"role": "admin", "username": "ops-admin"},
    "brd": {"role": "brand", "username": "zara-user", "brand_name": "Zara"},
    "nob": {"role": "brand", "username": "orphan"},
    "cus": {"role": "customer", "username": "c1", "customer_id": "u1"},
}
CID = "11111111-1111-4111-8111-111111111111"
IMG = {"files": [("files", ("a.jpg", b"x", "image/jpeg"))]}
JOB_FORM = {**IMG, "data": {"pin_codes": "560008"}}

GEO = [
    ("get", "/v1/metrics", {}),
    ("post", "/v1/geo/scores", {"json": {}}),
    ("post", "/v1/geo/brief", {"json": {}}),
    ("get", "/v1/geo/stores", {}),
    ("post", "/v1/geo/routing-guide", {"json": {}}),
    ("post", "/v1/geo/market-enrichment", {"json": {"pin": "560008"}}),
    ("post", "/v1/chat", {"json": {"message": "hi"}}),
]
CAMPAIGNS = [
    ("post", "/v1/ads-lab/extract", IMG),
    ("post", "/v1/ads-lab/campaign", {**IMG, "data": {"brand_name": "Zara", "pin_codes": "560008"}}),
    ("post", "/v1/ads-lab/campaign-jobs", JOB_FORM),
    ("get", "/v1/ads-lab/campaign-jobs", {}),
    ("get", f"/v1/ads-lab/campaign-jobs/{CID}", {}),
    ("get", "/v1/ads-lab/campaigns", {}),
    ("get", f"/v1/ads-lab/campaigns/{CID}", {}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/start", {}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/ignore", {}),
    ("patch", f"/v1/ads-lab/campaigns/{CID}/config",
     {"json": {"campaign_name": "A", "objective": "awareness", "budget_inr": 10, "duration_days": 7}}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/budget-recommendation", {"json": {}}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/export", {}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/estimate", {"json": {}}),
    ("post", f"/v1/ads-lab/campaigns/{CID}/launch", {}),
    ("get", f"/v1/ads-lab/campaigns/{CID}/monitor", {}),
]
ADMIN = [
    ("get", "/v1/admin/campaigns", {}),
    ("get", "/v1/admin/intel/customers-overview", {}),
    ("get", "/v1/admin/intel/customers", {}),
    ("get", "/v1/admin/intel/customers/c1", {}),
    ("post", "/v1/admin/intel/customers/c1/reveal-phone", {}),
    ("get", "/v1/admin/intel/garments", {}),
    ("get", "/v1/admin/intel/brands", {}),
    ("get", "/v1/admin/intel/campaigns", {}),
    ("get", "/v1/admin/unique-summary", {}),
    ("get", "/v1/admin/unique-garments", {}),
]
STYLE = [("get", "/v1/style-assistant/feed", {})]
ALL = GEO + CAMPAIGNS + ADMIN + STYLE + [("get", "/v1/auth/me", {})]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    return TestClient(main.app, raise_server_exceptions=False)


def _call(client, method, path, kw, token=None):
    headers = {"X-Session-Token": token} if token else {}
    return getattr(client, method)(path, headers=headers, **kw)


@pytest.mark.parametrize("method,path,kw", ALL, ids=[f"{m} {p}" for m, p, _ in ALL])
def test_anonymous_is_401(client, method, path, kw):
    assert _call(client, method, path, kw).status_code == 401


@pytest.mark.parametrize("method,path,kw", ALL, ids=[f"{m} {p}" for m, p, _ in ALL])
def test_unknown_token_is_401(client, method, path, kw):
    assert _call(client, method, path, kw, token="forged").status_code == 401


@pytest.mark.parametrize("method,path,kw", GEO + CAMPAIGNS + ADMIN, ids=[f"{m} {p}" for m, p, _ in GEO + CAMPAIGNS + ADMIN])
def test_customer_is_403_outside_style(client, method, path, kw):
    assert _call(client, method, path, kw, token="cus").status_code == 403


@pytest.mark.parametrize("method,path,kw", ADMIN + STYLE, ids=[f"{m} {p}" for m, p, _ in ADMIN + STYLE])
def test_brand_is_403_on_admin_and_style(client, method, path, kw):
    assert _call(client, method, path, kw, token="brd").status_code == 403


@pytest.mark.parametrize("method,path,kw", STYLE)
def test_admin_is_403_on_style(client, method, path, kw):
    assert _call(client, method, path, kw, token="adm").status_code == 403


@pytest.mark.parametrize("path", ["/v1/metrics", "/v1/ads-lab/campaigns"])
def test_brand_without_brand_name_is_403(client, path):
    assert client.get(path, headers={"X-Session-Token": "nob"}).status_code == 403


@pytest.mark.parametrize("path", ["/v1/geo/scores", "/v1/geo/brief"])
def test_geo_scoring_without_brand_name_is_403(client, path):
    assert client.post(path, json={}, headers={"X-Session-Token": "nob"}).status_code == 403


def test_token_accepted_from_bearer_header_and_query(client):
    assert client.get("/v1/auth/me", headers={"Authorization": "Bearer brd"}).json()["role"] == "brand"
    assert client.get("/v1/auth/me", params={"token": "cus"}).json()["role"] == "customer"


def test_x_session_token_wins_over_bearer(client):
    r = client.get("/v1/auth/me", headers={"X-Session-Token": "adm", "Authorization": "Bearer brd"})
    assert r.json()["role"] == "admin"


def test_logout_without_token_is_ok(client):
    assert client.post("/v1/auth/logout").json() == {"ok": True}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _login(client, monkeypatch, effect):
    def fake(username, password):
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(auth_acl, "authenticate", fake)
    return client.post("/v1/auth/login", json={"username": "u", "password": "p"})


def test_login_success_returns_session(client, monkeypatch):
    r = _login(client, monkeypatch, {"token": "t", "role": "brand", "username": "u"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["token"] == "t"


def test_login_bad_credentials_is_401(client, monkeypatch):
    assert _login(client, monkeypatch, PermissionError("x")).status_code == 401


def test_login_blank_username_is_400(client, monkeypatch):
    assert _login(client, monkeypatch, ValueError("username and password required")).status_code == 400


def test_login_backend_failure_is_masked_503(client, monkeypatch):
    r = _login(client, monkeypatch, RuntimeError("bigquery 10.0.0.1 denied"))
    assert r.status_code == 503 and "10.0.0.1" not in r.text and "Reference:" in r.json()["detail"]


def test_login_missing_fields_is_422(client):
    assert client.post("/v1/auth/login", json={"username": "u"}).status_code == 422


# ---------------------------------------------------------------------------
# Health + error masking
# ---------------------------------------------------------------------------


def test_health_works_when_db_and_bq_are_down(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("health must not touch BigQuery or Cloud SQL")

    monkeypatch.setattr(main, "compute_metrics", boom)
    monkeypatch.setattr(main.brand_camp, "connect", boom)
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["project"] is False


def test_unhandled_exception_is_masked_500(client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("psycopg2 password=hunter2")

    monkeypatch.setattr(main.camp_svc, "list_for", boom)
    r = client.get("/v1/ads-lab/campaigns", headers={"X-Session-Token": "brd"})
    body = r.json()
    assert r.status_code == 500 and "hunter2" not in r.text
    assert body["detail"].endswith(body["request_id"]) and "Reference:" in body["detail"]


def test_svc_http_masks_server_errors():
    exc = main._svc_http(svc.ServiceError("unavailable", "Campaign processing is not configured (EXTRACTION_URL)", 503))
    assert exc.status_code == 503 and "EXTRACTION_URL" not in exc.detail and "Reference:" in exc.detail


def test_svc_http_keeps_client_error_text():
    exc = main._svc_http(svc.ServiceError("invalid_input", "Select at least one PIN code", 400))
    assert (exc.status_code, exc.detail) == (400, "Select at least one PIN code")


def test_campaign_storage_not_configured_is_masked_503(client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = client.get("/v1/ads-lab/campaigns", headers={"X-Session-Token": "brd"})
    assert r.status_code == 503 and "Reference:" in r.json()["detail"]


def test_metrics_with_bq_disabled_is_masked_503(client, monkeypatch):
    monkeypatch.setenv("USE_BQ", "false")
    monkeypatch.setattr(main, "_metrics_cache", {"fn": None, "at": 0.0, "payload": None})
    r = client.get("/v1/metrics", headers={"X-Session-Token": "adm"})
    assert r.status_code == 503 and "USE_BQ" not in r.text


def test_admin_intel_unavailable_is_masked_503(client, monkeypatch):
    def down(**_k):
        raise main.admin_intel.IntelUnavailable("admin data is unavailable")

    monkeypatch.setattr(main.admin_intel, "customers_page", down)
    r = client.get("/v1/admin/intel/customers", headers={"X-Session-Token": "adm"})
    assert r.status_code == 503 and "Reference:" in r.json()["detail"]


def test_unique_garments_unavailable_is_masked_503(client, monkeypatch):
    def down(**_k):
        raise main.admin_intel.IntelUnavailable("BigQuery Forbidden proj.suffai_ops.garment_items")

    monkeypatch.setattr(main.admin_intel, "unique_garments", down)
    r = client.get("/v1/admin/unique-summary", headers={"X-Session-Token": "adm"})
    assert r.status_code == 503 and "suffai_ops" not in r.text and "Reference:" in r.json()["detail"]


def test_geo_stores_without_metrics_is_masked_503(client, monkeypatch):
    monkeypatch.setattr(main, "_geo_metrics_or_none", lambda: None)
    r = client.get("/v1/geo/stores", headers={"X-Session-Token": "adm"})
    assert r.status_code == 503 and "Reference:" in r.json()["detail"]


def test_geo_routing_guide_needs_pin_or_store(client, monkeypatch):
    monkeypatch.setattr(main, "_geo_metrics_or_none", lambda: {"pin_geo": {}})
    monkeypatch.setattr(main, "_geo_lookup", lambda *a, **k: {"status": "ok", "stores": []})
    r = client.post("/v1/geo/routing-guide", json={}, headers={"X-Session-Token": "adm"})
    assert r.status_code == 400


def test_geo_stores_refresh_is_admin_only(client):
    assert client.get("/v1/geo/stores", params={"refresh": "true"}, headers={"X-Session-Token": "brd"}).status_code == 403


def test_style_feed_customer_without_id_is_400(client, monkeypatch):
    monkeypatch.setitem(SESSIONS, "cus", {"role": "customer", "username": "c1"})
    assert client.get("/v1/style-assistant/feed", headers={"X-Session-Token": "cus"}).status_code == 400


# ---------------------------------------------------------------------------
# Campaign start: state conflicts and partial failures
# ---------------------------------------------------------------------------


@pytest.fixture
def start_env(monkeypatch):
    calls = []
    monkeypatch.setattr(main.camp_svc, "load_campaign", lambda sess, cid: {"campaign_id": CID, "status": "ranked"})
    monkeypatch.setattr(main.brand_camp, "set_campaign_status", lambda cid, status, *a: calls.append(status))
    return calls


def test_start_requires_ranked_status_409(client, monkeypatch, start_env):
    monkeypatch.setattr(main.camp_svc, "load_campaign", lambda sess, cid: {"campaign_id": CID, "status": "draft"})
    assert client.post(f"/v1/ads-lab/campaigns/{CID}/start", headers={"X-Session-Token": "brd"}).status_code == 409
    assert start_env == []


def test_start_failure_marks_error_and_masks(client, monkeypatch, start_env):
    def boom(_cid):
        raise RuntimeError("psycopg2 down")

    monkeypatch.setattr(main.style_asst, "materialize_campaign_recommendations", boom)
    r = client.post(f"/v1/ads-lab/campaigns/{CID}/start", headers={"X-Session-Token": "brd"})
    assert r.status_code == 503 and "psycopg2" not in r.text
    assert start_env == ["started", "error"]


def test_start_survives_refresh_failure(client, monkeypatch, start_env):
    def boom(_cid):
        raise RuntimeError("db gone")

    monkeypatch.setattr(main.style_asst, "materialize_campaign_recommendations", lambda cid: {"customers": 3})
    monkeypatch.setattr(main.brand_camp, "get_campaign", boom)
    r = client.post(f"/v1/ads-lab/campaigns/{CID}/start", headers={"X-Session-Token": "brd"})
    assert r.status_code == 200 and r.json()["campaign"]["status"] == "started"


def test_start_missing_campaign_is_404_without_id(client, monkeypatch, start_env):
    def gone(cid):
        raise LookupError(f"campaign not found: {cid}")

    monkeypatch.setattr(main.style_asst, "materialize_campaign_recommendations", gone)
    r = client.post(f"/v1/ads-lab/campaigns/{CID}/start", headers={"X-Session-Token": "brd"})
    assert r.status_code == 404 and CID not in r.text


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,pins",
    [("", []), ('["560008", " 560001 ", ""]', ["560008", "560001"]), ("560008; 560001,", ["560008", "560001"]),
     ("not json", ["not json"])],
)
def test_parse_pin_codes(raw, pins):
    assert main._parse_pin_codes(raw) == pins


def test_csv_param_trims_and_drops_blanks():
    assert main._csv_param(" a, ,b,") == ["a", "b"] and main._csv_param(None) == []


@pytest.mark.parametrize(
    "row,decision",
    [
        (None, "No"),
        ({"demand": 0, "brand_garments": 3, "score": 0.9}, "No"),
        ({"demand": 5, "brand_garments": 0, "score": 0.9, "est_footfall": 90}, "No"),
        # Footfall under 40 is No, whatever the share or score
        ({"demand": 5, "brand_garments": 2, "brand_share": 0.4, "est_footfall": 30, "score": 0.9}, "No"),
        ({"demand": 5, "brand_garments": 1, "brand_share": 0.25, "est_footfall": 30, "score": 0.5}, "No"),
        # No share known: the score decides
        ({"demand": 5, "brand_garments": 2, "est_footfall": 50, "score": 0.2}, "No"),
        ({"demand": 5, "brand_garments": 2, "est_footfall": 50, "score": 0.4}, "Consider"),
        ({"demand": 5, "brand_garments": 2, "est_footfall": 50, "score": 0.7, "hv_demand": 2}, "Yes"),
        # Share known: Yes needs share 0.35, footfall 60 and score 0.33
        ({"demand": 5, "brand_garments": 2, "brand_share": 0.4, "est_footfall": 80, "score": 0.5}, "Yes"),
        ({"demand": 5, "brand_garments": 2, "brand_share": 0.4, "est_footfall": 80, "score": 0.2}, "Consider"),
        ({"demand": 5, "brand_garments": 2, "brand_share": 0.4, "est_footfall": 50, "score": 0.5}, "Consider"),
        ({"demand": 5, "brand_garments": 3, "brand_share": 0.1, "est_footfall": 90, "score": 0.4}, "Consider"),
        ({"demand": 5, "brand_garments": 3, "brand_share": 0.1, "est_footfall": 90, "score": 0.2}, "No"),
        # Yes needs Champions/Loyal demand when it is known
        ({"demand": 5, "brand_garments": 2, "brand_share": 0.4, "est_footfall": 80, "score": 0.5, "hv_demand": 0}, "Consider"),
        # A sanitised brief focus carries floats; strings and junk are tolerated
        ({"demand": "5", "brand_garments": "2", "brand_share": "0.4", "est_footfall": "80", "score": "0.5"}, "Yes"),
        ({"demand": 5, "brand_garments": 2, "est_footfall": "lots", "score": 0.9}, "No"),
    ],
)
def test_decision_for_row(row, decision):
    assert main.geo_analysis.decision_for_row(row) == decision


def test_parse_geo_brief_json_tolerates_fences_and_defaults():
    out = main._parse_geo_brief_model_json('```json\n{"decision": "Yes", "explanation": "Strong", "confidence": "huge"}\n```')
    assert out == {"decision": "Yes", "confidence": "medium", "explanation": "Strong", "brief": "Strong", "key_factors": []}


@pytest.mark.parametrize("text", ["", "no json here", '{"decision": "Maybe", "explanation": "x"}', '{"decision": "Yes"}'])
def test_parse_geo_brief_json_rejects_bad_output(text):
    with pytest.raises(ValueError):
        main._parse_geo_brief_model_json(text)


def test_sanitize_geo_brief_drops_pii_and_bad_numbers():
    body = main.GeoBriefRequest(
        kpis={"customers": "12", "revenue": "lots", "email": "a@b.c"},
        focus_pin={"pin": "560008", "decision": "Maybe", "score": "0.5", "lat": 12.9, "commodities": [{"name": "Shirt", "count": 2}]},
        analysis_brand="all",
        role="root",
    )
    out = main._sanitize_geo_brief_payload(body)
    assert out["kpis"] == {"customers": 12.0}
    assert out["role"] == "admin" and out.get("analysis_brand") is None
    focus = out["focus_pin"]
    assert focus["heuristic_decision"] == "Consider" and focus["score"] == 0.5 and "lat" not in focus
    assert focus["commodities"] == [{"commodity": "Shirt", "count": 2.0}]
