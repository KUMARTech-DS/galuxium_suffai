"""Identity / privacy / polygons / ADV / auth config smoke tests."""

from fastapi.testclient import TestClient

from api.main import app
from suffai.geo.polygons import choropleth_features, load_pin_polygons
from suffai.privacy.differencing import enforce_export_role, register_query

client = TestClient(app)


def _token():
    r = client.post("/v1/auth/brand/login", json={"brand": "Peter England", "password": "brand123"})
    assert r.status_code == 200
    return r.json()["data"]["access_token"]


def test_auth_config_and_identity_session_rejects_without_ip():
    r = client.get("/v1/auth/config")
    assert r.status_code == 200
    cfg = r.json()["data"]
    assert "demo_auth_enabled" in cfg
    assert "identity_platform_enabled" in cfg

    bad = client.post("/v1/auth/identity/session", json={"id_token": "not.a.jwt"})
    assert bad.status_code == 200
    assert bad.json()["errors"]


def test_pin_polygons_and_choropleth_api():
    fc = load_pin_polygons()
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) >= 3

    joined = choropleth_features(
        [{"pin_code": "560103", "active_customers": 40, "suppressed": False, "geo_key": "560103"}],
        metric_key="active_customers",
    )
    assert any(f["properties"].get("metric_value") == 40 for f in joined["features"])

    tok = _token()
    h = {"Authorization": f"Bearer {tok}"}
    r = client.get("/v1/geo/choropleth", headers=h)
    assert r.status_code == 200
    assert r.json()["data"]["type"] == "FeatureCollection"
    assert r.json()["data"]["features"]


def test_differencing_and_export_role(tmp_path, monkeypatch):
    log_path = tmp_path / "privacy.json"
    monkeypatch.setattr("suffai.privacy.differencing.settings.PRIVACY_QUERY_LOG_PATH", log_path)
    monkeypatch.setattr("suffai.privacy.differencing.settings.PRIVACY_DIFFERENCING_ENABLED", True)
    monkeypatch.setattr("suffai.privacy.differencing.settings.PRIVACY_K", 5)
    monkeypatch.setattr("suffai.privacy.differencing.settings.ENVIRONMENT", "local")

    base = [f"c{i}" for i in range(10)]
    a = register_query(tenant_id="t1", filters={"pin": "A"}, cohort_customer_ids=base)
    assert not a["differencing_risk"]
    # drop 2 customers — delta < k
    b = register_query(tenant_id="t1", filters={"pin": "B"}, cohort_customer_ids=base[:-2])
    assert b["differencing_risk"]

    assert enforce_export_role("brand_analyst")
    assert not enforce_export_role("brand_executive")


def test_adv_activation_flow():
    tok = _token()
    h = {"Authorization": f"Bearer {tok}"}
    cid = "camp-adv-test"
    c = client.post(
        "/v1/activation/conversions",
        headers=h,
        json={"campaign_id": cid, "segment_key": "Champions", "converted": True, "revenue": 120},
    )
    assert c.status_code == 200
    assert not c.json()["errors"]

    u = client.post(
        "/v1/activation/uplift/estimate",
        headers=h,
        json={"campaign_id": cid, "treatment_segment_keys": ["Champions"], "control_segment_keys": []},
    )
    assert u.status_code == 200
    assert u.json()["data"]["method"] == "observational_diff_in_cvr"
    assert "caveat" in u.json()["data"]

    inv = client.post(
        "/v1/activation/inventory",
        headers=h,
        json={"brand_id": "peterengland", "sku": "PE-001", "on_hand": 42},
    )
    assert inv.status_code == 200
    listed = client.get("/v1/activation/inventory?brand_id=peterengland", headers=h)
    assert any(r["sku"] == "PE-001" for r in listed.json()["data"]["inventory"])

    sched = client.get("/v1/activation/snapshots/schedule-status", headers=h)
    assert sched.status_code == 200
    assert "suffai-cdc-hourly" in sched.json()["data"]["scheduler_jobs"]


def test_health_exposes_gcp_flags():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "identity_platform_enabled" in body
    assert "use_bigquery_marts" in body
    assert "privacy_differencing" in body
