"""suffai-similarity links a photo exactly like suffai-extraction: same name rule, order check, no streaming."""

import base64
import json
import sys
import types
from datetime import datetime, timezone

import pytest

if "google.cloud.storage" not in sys.modules:
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        google = sys.modules.setdefault("google", types.ModuleType("google"))
        cloud = types.ModuleType("google.cloud")
        cloud.storage = types.ModuleType("google.cloud.storage")
        google.cloud = cloud
        sys.modules["google.cloud"] = cloud
        sys.modules["google.cloud.storage"] = cloud.storage

from similarity import bq_sync, photo_key as sim_key, process  # noqa: E402
from worker import photo_key as ext_key  # noqa: E402

KEYS = [
    "garments/c1_o1_1.jpg", "c1_o1_1.JPEG", "uploads/2026/c1_o1_item-3.png", "/garments/c1_o1_1.jpg",
    "brands/zara/c1_o1_1.jpg", "demo/c1_o1_1.jpg", "garments/c1/560034/photo.jpg", "garments/c1_o1.jpg",
    "garments/c1_o1_1.gif", "garments/c1__1.jpg", "garments/c1_o1_ .jpg", "garments/c1_o1_a_b.jpg",
    "garments/" + "x" * 65 + "_o1_1.jpg", "garments/" + "x" * 64 + "_o1_1.jpg", "", None, "c 1_o1_1.jpg",
]


@pytest.mark.parametrize("key", KEYS)
def test_both_parsers_agree(key):
    a, b = ext_key.parse_photo_key(key), sim_key.parse_photo_key(key)
    assert (tuple(a) if a else None) == (tuple(b) if b else None)


def test_same_rule_constants():
    assert sim_key.SKIP_PREFIXES == ext_key.SKIP_PREFIXES == ("brands/", "demo/", "catalog/")
    assert sim_key.MAX_ID_LEN == ext_key.MAX_ID_LEN
    assert (sim_key._PHOTO_RE.pattern, sim_key._PHOTO_RE.flags) == (ext_key._PHOTO_RE.pattern, ext_key._PHOTO_RE.flags)


class _Image:
    bucket, object_key, gs_uri, metadata = "b", "garments/c1_o1_1.jpg", "gs://b/garments/c1_o1_1.jpg", {}


@pytest.fixture
def svc(monkeypatch):
    """Cloud SQL, GCS and Vertex stubbed; BigQuery answers through bq_sync._runner."""
    calls = {"read": 0, "events": [], "bq": []}
    owners = {("c1", "o1"): "560034"}

    def runner(sql, params):
        p = {n: v for n, _, v in params}
        calls["bq"].append((sql, params))
        if sql.startswith("SELECT"):
            pin = owners.get((p["customer_id"], p["order_id"]))
            return [{"pin_code": pin}] if pin else []
        return []

    def read(key, bucket=None):
        calls["read"] += 1
        return _Image()

    s = process.settings
    monkeypatch.setattr(s, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(s, "USE_BQ", True)
    monkeypatch.setattr(s, "PROJECT_ID", "proj")
    monkeypatch.setattr(bq_sync, "_runner", runner)
    monkeypatch.setattr(process, "read_image", read)
    monkeypatch.setattr(process, "embed_image", lambda img: ([1.0, 0.0], "m"))
    def record(**kw):
        calls["events"].append(kw)
        return {"id": f"e{len(calls['events'])}", "created_at": datetime(2026, 10, 20, tzinfo=timezone.utc)}

    monkeypatch.setattr(process.db, "event_for_key", lambda k: None)
    monkeypatch.setattr(process.db, "record_event", record)
    monkeypatch.setattr(process.db, "mark_bq_synced", lambda event_id: calls.setdefault("synced", []).append(event_id))
    monkeypatch.setattr(process.db, "pending_bq_events", lambda **kw: [])
    monkeypatch.setattr(process.db, "load_user_embeddings", lambda u: [])
    monkeypatch.setattr(process.db, "load_global_embeddings", lambda **kw: [])
    monkeypatch.setattr(process.db, "create_unique_garment", lambda **kw: "g-new")
    calls["owners"] = owners
    return calls


@pytest.mark.parametrize("key", ["brands/zara/sku1.jpg", "demo/c1_o1_1.jpg", "garments/c1/560034/photo.jpg"])
def test_non_photos_are_ignored_before_download(svc, key):
    assert process.process_upload({"gcs_object_key": key})["status"] == "ignored"
    assert svc["read"] == 0 and not svc["bq"] and not svc["events"]


def test_unlinked_photo_is_not_downloaded(svc):
    out = process.process_upload({"gcs_object_key": "garments/c1_o2_1.jpg"})
    assert out["status"] == "unlinked" and (out["customer_id"], out["order_id"], out["item_id"]) == ("c1", "o2", "1")
    assert svc["read"] == 0 and not svc["events"]
    sql = svc["bq"][0][0]
    assert "JOIN" in sql and "customers" in sql and "@customer_id" in sql and "@order_id" in sql


def test_linked_photo_uses_customer_pin_and_dml_insert(svc):
    payload = {"gcs_object_key": "garments/c1_o1_1.jpg", "user_uuid": "other", "pin_code": "110001",
               "attributes": {"user_uuid": "other"}, "metadata": {"pin_code": "110001", "brand": "Zara"}}
    out = process.process_upload(payload)
    assert out["status"] == "created_new" and svc["read"] == 1
    ev = svc["events"][-1]
    assert (ev["user_uuid"], ev["pin_code"], ev["brand_name"]) == ("c1", "560034", "Zara")
    assert (ev["order_id"], ev["item_id"]) == ("o1", "1")
    sql, params = svc["bq"][-1]
    assert sql.startswith("INSERT INTO `proj.suffai_ops.garment_similarity_observations`")
    assert "CURRENT_TIMESTAMP()" not in sql
    p = {n: (t, v) for n, t, v in params}
    assert p["pin_code"] == ("STRING", "560034") and p["customer_id"] == ("STRING", "c1")
    assert p["order_id"] == ("STRING", "o1") and p["item_id"] == ("STRING", "1")
    assert p["similarity_score"] == ("FLOAT64", None)
    assert p["event_ts"] == ("TIMESTAMP", datetime(2026, 10, 20, tzinfo=timezone.utc))  # the event's created_at
    assert svc["synced"] == ["e1"]


def test_observation_insert_failure_fails_the_photo_for_a_retry(svc, monkeypatch):
    def runner(sql, params):
        if sql.startswith("INSERT"):
            raise RuntimeError("bq 500")
        return [{"pin_code": "560034"}]

    monkeypatch.setattr(bq_sync, "_runner", runner)
    with pytest.raises(bq_sync.ObservationNotWritten):
        process.process_upload({"gcs_object_key": "garments/c1_o1_1.jpg"})
    assert len(svc["events"]) == 1 and not svc.get("synced")


def test_bad_customer_pin_is_not_retried(svc):
    svc["owners"][("c1", "o1")] = "5600"
    with pytest.raises(ValueError):
        process.process_upload({"gcs_object_key": "garments/c1_o1_1.jpg"})
    assert svc["read"] == 0


def _push(client, key):
    data = base64.b64encode(json.dumps({"name": key, "bucket": "b"}).encode()).decode()
    return client.post("/", json={"message": {"data": data, "messageId": "m1", "attributes": {"objectId": key}}})


def test_pubsub_acks_ignored_and_unlinked_and_retries_lookup_failures(svc, monkeypatch):
    from fastapi.testclient import TestClient

    from similarity import main

    c = TestClient(main.app)
    r = _push(c, "brands/zara/sku1.jpg")
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    r = _push(c, "garments/c1_o9_1.jpg")
    assert r.status_code == 200 and r.json()["status"] == "unlinked"

    def down(sql, params):
        raise RuntimeError("bq unavailable")

    monkeypatch.setattr(bq_sync, "_runner", down)
    assert _push(c, "garments/c1_o1_1.jpg").status_code == 500
    assert svc["read"] == 0
