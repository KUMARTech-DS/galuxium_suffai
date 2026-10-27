"""Cold-start 1P platform-fee policy and aggregate feature wiring."""

from __future__ import annotations

import brand_campaign as bc
import campaign_service as svc

CID = "44444444-4444-4444-8444-444444444444"
SESS = {"role": "brand", "brand_name": "Zara"}


def _rec(**overrides):
    base = {
        "audience": 10_000, "sku_count": 3, "pin_count": 2, "comparisons": 50_000,
        "process_seconds": 120.0, "objective": "consideration", "channels": ["in_app"],
        "duration_days": 14, "media_budget_inr": 50_000.0,
    }
    return bc.recommend_platform_fee(**{**base, **overrides})


def test_fee_is_separate_from_media_and_has_aggregate_features():
    rec = _rec()
    assert rec["media_budget_inr"] == 50_000
    assert rec["total_checkout_inr"] == rec["media_budget_inr"] + rec["recommended"]
    assert rec["source"] == "cost_model_v1" and rec["cold_start"] is True
    assert rec["features"]["audience_count"] == 10_000
    assert "customer_id" not in str(rec["features"])


def test_fee_increases_with_campaign_complexity():
    low = _rec(audience=1_000, sku_count=1, pin_count=1, comparisons=0, process_seconds=0, channels=["in_app"])
    high = _rec(audience=100_000, sku_count=12, pin_count=10, comparisons=1_000_000,
                process_seconds=1200, channels=["in_app", "whatsapp", "sms"])
    assert high["recommended"] > low["recommended"]
    assert low["min"] <= low["recommended"] <= low["max"]


def test_fee_policy_env_is_versioned(monkeypatch):
    monkeypatch.setenv("FEE_MODEL_VERSION", "cost_model_v7")
    monkeypatch.setenv("FEE_BASE_INR", "3000")
    assert _rec()["model_version"] == "cost_model_v7"
    assert _rec()["recommended"] >= 3000


def test_service_uses_aggregate_skus_and_job_stats(monkeypatch):
    campaign = {
        "campaign_id": CID, "brand_name": "Zara", "status": "ranked", "est_audience": 8_000,
        "pin_codes": ["560008", "560034"], "channels": ["in_app"], "objective": "conversion",
    }
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(bc, "get_campaign", lambda _cid: campaign)
    monkeypatch.setattr(bc, "list_campaign_skus", lambda _cid: [{"sku_uuid": "a"}, {"sku_uuid": "b"}])
    monkeypatch.setattr(bc, "campaign_fee_job_stats", lambda _cid: {"comparisons": 123, "process_seconds": 8.5})
    monkeypatch.setattr(bc, "record_fee_decision", lambda *a, **k: None)
    rec = svc.fee_recommendation(SESS, CID, objective=None, duration_days=7, channels=None,
                                 media_budget_inr=20_000)
    assert rec["features"]["sku_count"] == 2
    assert rec["features"]["comparisons"] == 123
    assert rec["features"]["pin_count"] == 2
