"""PRD feature smoke tests (additive APIs; existing geo/auth still work)."""

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from suffai.garments.store import GarmentStore
from suffai.metrics.campaign import evaluate_campaign_fit
from suffai.metrics.research import product_opportunities
from suffai.ingest import load_source_collections, normalize_all
from suffai.metrics.eligibility import eligible_orders
from suffai.metrics.rfm import compute_rfm_snapshots

DATA = Path(__file__).resolve().parents[1] / "data_offline"
client = TestClient(app)


def _token():
    r = client.post("/v1/auth/brand/login", json={"brand": "Peter England", "password": "brand123"})
    assert r.status_code == 200
    return r.json()["data"]["access_token"]


def test_existing_geo_still_works():
    r = client.post("/v1/geo/query", json={"geo_ids": ["560103"]})
    assert r.status_code == 200
    assert "tiles" in r.json()["data"]


def test_product_opportunities_and_campaign_fit():
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    eligible = {o["order_id"] for o in eligible_orders(norm["orders"])}
    rfm = compute_rfm_snapshots(norm["orders"], norm["stores"])
    opps = product_opportunities(
        norm["order_commodity_lines"], eligible, [], [], my_brand="Peter England"
    )
    assert opps["opportunities"]
    assert opps["opportunities"][0]["opportunity_score"] >= 0

    evals = evaluate_campaign_fit(
        campaign_id="c1",
        metadata={"attributes": {"commodity_type": "shirt", "primary_color": "white", "material_type": "cotton"}},
        lines=norm["order_commodity_lines"],
        eligible_order_ids=eligible,
        rfm_snapshots=rfm,
        my_brand="Peter England",
    )
    assert evals
    assert any(e.get("fit_score") is not None or e.get("suppressed") for e in evals)


def test_garment_capture_review_flow(tmp_path, monkeypatch):
    store = GarmentStore(tmp_path)
    monkeypatch.setattr("api.deps.get_garment_store", lambda: store)
    # deps uses lru cache for other things; garment store is function each call — patch module used by router
    monkeypatch.setattr("api.routers.garments.get_garment_store", lambda: store)
    monkeypatch.setattr("api.routers.brands.get_garment_store", lambda: store)

    tok = _token()
    h = {"Authorization": f"Bearer {tok}"}
    consent = client.post(
        "/v1/garments/consent",
        json={"source_customer_id": "cust1", "purpose": "aggregated_b2b_analytics"},
        headers=h,
    )
    assert consent.status_code == 200
    cid = consent.json()["data"]["consent_id"]

    init = client.post(
        "/v1/garments/images:initiate",
        json={
            "source_customer_id": "cust1",
            "source_order_id": "ord1",
            "source_order_item_id": "item1",
            "source_order_commodity_line_id": "line1",
            "physical_item_index": 1,
            "consent_id": cid,
            "order_pin_code": "560103",
        },
        headers=h,
    )
    assert init.status_code == 200
    gid = init.json()["data"]["garment_instance"]["garment_instance_id"]

    patch = client.patch(
        f"/v1/garments/{gid}/attributes",
        json={
            "attributes": {
                "brand_name_raw": "Peter England",
                "commodity_type": "shirt",
                "material_type": "cotton",
                "primary_color": "navy",
            },
            "review_status": "completed",
        },
        headers=h,
    )
    assert patch.status_code == 200

    bench = client.get("/v1/brands/peterengland/benchmarks", headers=h)
    assert bench.status_code == 200
    body = bench.json()
    assert not body["errors"] or body["errors"][0]["code"] != "INSUFFICIENT_DATA"
    assert body["data"]["benchmarks"]


def test_campaign_evaluate_api():
    tok = _token()
    h = {"Authorization": f"Bearer {tok}", "Idempotency-Key": "test-camp-1"}
    created = client.post(
        "/v1/campaigns",
        json={"attributes": {"commodity_type": "shirt", "primary_color": "white", "material_type": "cotton"}},
        headers=h,
    )
    assert created.status_code == 200
    cid = created.json()["data"]["campaign"]["campaign_id"]
    ev = client.post(f"/v1/campaigns/{cid}/evaluate", json={}, headers=h)
    assert ev.status_code == 200
    assert ev.json()["data"]["evaluations"]
