from datetime import datetime, timezone
from pathlib import Path

from suffai.garments.store import GarmentStore
from suffai.ingest import load_source_collections, normalize_all
from suffai.metrics.eligibility import eligible_orders, is_eligible_order
from suffai.metrics.rfm import compute_rfm_snapshots
from suffai.privacy.suppress import suppress_cell

DATA = Path(__file__).resolve().parents[1] / "data_offline"


def test_eligibility_excludes_cancel_and_unpaid():
    assert not is_eligible_order({"order_status": "cancel", "payment_status": "paid", "refund_flag": False})
    assert not is_eligible_order({"order_status": "delivered_to_customer", "payment_status": None, "refund_flag": False})
    assert is_eligible_order({"order_status": "delivered_to_customer", "payment_status": "paid", "refund_flag": False})


def test_rfm_reproducible_and_no_zero_spend_label_for_missing():
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    a = compute_rfm_snapshots(norm["orders"], norm["stores"])
    b = compute_rfm_snapshots(norm["orders"], norm["stores"])
    assert len(a) == len(b)
    assert a[0]["customer_id"] == b[0]["customer_id"]
    assert a[0]["rfm_segment"] == b[0]["rfm_segment"]
    # New/insufficient must not be forced to zero-spend Champion
    news = [s for s in a if s["rfm_segment"] == "New"]
    for s in news[:20]:
        assert s["insufficient_history"] or s["frequency"] == 0


def test_privacy_k30():
    cell = suppress_cell(10, 100, k=30)
    assert cell["suppressed"] is True
    assert cell["value"] is None
    cell_ok = suppress_cell(30, 100, k=30)
    assert cell_ok["suppressed"] is False
    assert cell_ok["value"] == 100


def test_garment_requires_evidence_fields(tmp_path):
    store = GarmentStore(tmp_path)
    consent = store.create_consent("cust1", "aggregated_b2b_analytics")
    g = store.register_garment_instance({
        "source_customer_id": "cust1",
        "source_order_id": "ord1",
        "source_order_item_id": "item1",
        "source_order_commodity_line_id": "line1",
        "physical_item_index": 1,
        "consent_id": consent["consent_id"],
        "brand_name_raw": "unknown",
        "review_status": "needs_review",
    })
    assert g["garment_instance_id"]
    assert store.brand_coverage_summary()["brand_analytics_available"] is False


def test_eligible_order_count_sane():
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    elig = eligible_orders(norm["orders"])
    assert 10000 < len(elig) < len(norm["orders"])
