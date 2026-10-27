"""Campaign fee migration and BQ audit schema stay deployable together."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fee_schema_has_sql_bq_and_feature_export():
    migration = (ROOT / "db" / "migrations" / "014_campaign_fee_intelligence.sql").read_text()
    bq = (ROOT / "command_center" / "scripts" / "migrate_bq_campaign_fee.sql").read_text()
    exporter = (ROOT / "command_center" / "scripts" / "build_campaign_fee_features.py").read_text()
    assert "platform_fee_recommended" in migration
    assert "campaign_fee_decisions" in migration
    assert "campaign_fee_recommendations" in bq
    assert "campaign_fee_features" in bq
    assert "customer_id" not in bq
    assert "campaign_fee_features" in exporter


def test_fee_api_and_checkout_ui_are_wired():
    main = (ROOT / "command_center" / "main.py").read_text()
    ui = (ROOT / "web" / "campaigns.html").read_text()
    assert "/fee-recommendation" in main
    assert "platform_fee_inr" in main
    assert 'id="platformFee"' in ui
    assert 'id="totalCheckout"' in ui
    assert "cost-model estimate" in ui.lower()
