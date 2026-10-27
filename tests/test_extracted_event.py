"""Worker hands off to similarity only after extraction is saved (no GCP)."""

import json

import pytest

from worker import events
from worker.customer_photos import garment_sku
from worker.settings import settings


@pytest.fixture(autouse=True)
def topic(monkeypatch):
    monkeypatch.setattr(settings, "PUBSUB_EXTRACTED_TOPIC", "garment-image-extracted")
    monkeypatch.setattr(settings, "PROJECT_ID", "proj")


def test_publishes_only_after_persist_for_customer_garments():
    saved = {"job_id": "j1", "garment_updated": True}
    assert events.should_publish(target_type="garment", object_key="garments/t/b/x.jpg", persist_info=saved)
    assert not events.should_publish(target_type="garment", object_key="garments/x.jpg", persist_info={"skipped": True})
    assert not events.should_publish(target_type="garment", object_key="brands/zara/c/x.jpg", persist_info=saved)
    assert not events.should_publish(target_type="garment", object_key="demo/x.jpg", persist_info=saved)
    assert not events.should_publish(target_type="garment", object_key="catalog/x.jpg", persist_info=saved)
    assert not events.should_publish(target_type="campaign", object_key="garments/x.jpg", persist_info=saved)


def test_customer_photo_publishes_only_once_written_to_commodities():
    assert events.should_publish_photo({"status": "COMPLETED", "persist": {"bigquery": "commodities"}})
    assert not events.should_publish_photo({"status": "unlinked"})
    assert not events.should_publish_photo({"status": "ignored", "reason": "bigquery_disabled"})


def test_disabled_topic_does_not_publish(monkeypatch):
    monkeypatch.setattr(settings, "PUBSUB_EXTRACTED_TOPIC", "")
    assert not events.should_publish(target_type="garment", object_key="garments/x.jpg", persist_info={})
    assert not events.should_publish_photo({"persist": {"bigquery": "commodities"}})


def test_photo_event_carries_customer_and_matched_style_colour():
    out = {"status": "COMPLETED", "gcs_object_key": "garments/c1_o1_1.jpg", "customer_id": "c1",
           "persist": {"bigquery": "commodities", "brand": "Levi's", "garment_sku": "levis:511:indigo"}}
    ev = events.photo_event(out, bucket="b", idempotency_key="msg-1")
    assert ev["user_uuid"] == "c1" and ev["gcs_bucket"] == "b"
    assert ev["brand_sku"] == "levis:511:indigo" and ev["brand_name"] == "Levi's"
    assert ev["idempotency_key"] == "sim:msg-1"
    unmatched = events.photo_event({"customer_id": "c1", "persist": {"brand": "unknown"}}, bucket="b",
                                   idempotency_key="m")
    assert unmatched["brand_sku"] is None and unmatched["brand_name"] is None


def test_legacy_event_never_trusts_a_label_sku():
    payload = {"user_uuid": "u1", "pin_code": "560008"}
    ev = events.extracted_event(payload, {"brand_name": "unknown", "brand_sku": "LABEL-9"}, status="NEEDS_REVIEW",
                                bucket="b", object_key="k", idempotency_key="m", image_id=None,
                                garment_instance_id="gi")
    assert ev["brand_sku"] is None and ev["brand_name"] is None
    assert ev["pin_code"] == "560008" and ev["garment_instance_id"] == "gi"


def test_garment_sku_ignores_size_and_needs_style_and_colour():
    assert garment_sku({"brand": "Levi's", "brand_style_code": "511", "color": "Indigo"}) == garment_sku(
        {"brand": "levis", "brand_style_code": "511", "color": "indigo", "size": "32"})
    assert garment_sku({"brand": "Levi's", "color": "Indigo"}) is None


def test_publish_sends_json_and_failure_is_retryable():
    sent = []
    ev = {"gcs_object_key": "garments/x.jpg", "image_id": "img", "user_uuid": "u1"}
    events.publish_extracted(ev, publisher=lambda topic, data, attrs: sent.append((topic, json.loads(data), attrs)))
    assert sent == [("garment-image-extracted", ev, {"image_id": "img", "user_uuid": "u1"})]

    def down(*_a):
        raise OSError("pubsub down")

    with pytest.raises(RuntimeError):
        events.publish_extracted(ev, publisher=down)
