"""A failed BigQuery observation insert is retried and resent, never dropped (garment counts live in BigQuery)."""

import base64
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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

from similarity import bq_sync, db, process  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "garments/c1_o1_1.jpg"
FIRST_SEEN = datetime(2026, 10, 19, 8, 30, tzinfo=timezone.utc)


def _event(**over):
    base = {"id": "e1", "gcs_bucket": "b", "gcs_object_key": KEY, "user_uuid": "c1", "pin_code": "560034",
            "brand_name": None, "outcome": "created_new", "matched_garment_id": "g1", "similarity_score": None,
            "order_id": "o1", "item_id": "1", "idempotency_key": "m1", "created_at": FIRST_SEEN,
            "bq_synced_at": None}
    return {**base, **over}


@pytest.fixture
def sim(monkeypatch):
    """Cloud SQL events in memory, BigQuery through bq_sync._runner, no image download allowed."""
    state = {"events": {}, "inserts": [], "fail_insert": False, "synced": []}

    def runner(sql, params):
        if sql.startswith("INSERT"):
            if state["fail_insert"]:
                raise RuntimeError("bq 503")
            state["inserts"].append({n: (t, v) for n, t, v in params})
            return []
        return [{"pin_code": "560034"}]

    def mark(event_id):
        state["synced"].append(event_id)
        for e in state["events"].values():
            if e["id"] == event_id:
                e["bq_synced_at"] = datetime.now(timezone.utc)

    def no_download(*a, **k):
        raise AssertionError("a retry must not download or match the photo again")

    s = process.settings
    monkeypatch.setattr(s, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(s, "USE_BQ", True)
    monkeypatch.setattr(s, "PROJECT_ID", "proj")
    monkeypatch.setattr(bq_sync, "_runner", runner)
    monkeypatch.setattr(process, "read_image", no_download)
    monkeypatch.setattr(process.db, "event_for_key", lambda k: state["events"].get(k))
    monkeypatch.setattr(process.db, "mark_bq_synced", mark)
    monkeypatch.setattr(process.db, "pending_bq_events", lambda **kw: [
        e for e in state["events"].values() if e["bq_synced_at"] is None][: kw["limit"]])
    return state


def test_failed_insert_raises_and_leaves_the_event_pending(sim):
    sim["fail_insert"] = True
    with pytest.raises(bq_sync.ObservationNotWritten):
        process.send_observation(_event())
    assert sim["synced"] == []


def test_retry_resends_the_pending_row_without_matching_again(sim):
    sim["events"]["m1"] = _event()
    out = process.process_upload({"gcs_object_key": KEY, "idempotency_key": "m1"})
    assert out == {"status": "bq_resynced", "idempotency_key": "m1", "garment_id": "g1"}
    row = sim["inserts"][-1]
    assert row["event_ts"] == ("TIMESTAMP", FIRST_SEEN)  # same row as the first attempt
    assert row["garment_id"] == ("STRING", "g1") and row["order_id"] == ("STRING", "o1")
    assert sim["synced"] == ["e1"]


def test_retry_while_bigquery_is_still_down_fails_again(sim):
    sim["events"]["m1"] = _event()
    sim["fail_insert"] = True
    with pytest.raises(bq_sync.ObservationNotWritten):
        process.process_upload({"gcs_object_key": KEY, "idempotency_key": "m1"})
    assert sim["events"]["m1"]["bq_synced_at"] is None


def test_synced_and_error_events_are_skipped(sim):
    sim["events"]["m1"] = _event(bq_synced_at=FIRST_SEEN)
    sim["events"]["m2"] = _event(id="e2", outcome="error", matched_garment_id=None)
    for key in ("m1", "m2"):
        assert process.process_upload({"gcs_object_key": KEY, "idempotency_key": key})["status"] == "already_processed"
    assert sim["inserts"] == []


def test_resend_pending_reads_order_and_item_from_the_photo_name(sim):
    sim["events"]["old"] = _event(id="e9", order_id=None, item_id=None)
    assert process.resend_pending() == 1
    row = sim["inserts"][-1]
    assert (row["order_id"], row["item_id"], row["event_ts"]) == (("STRING", "o1"), ("STRING", "1"),
                                                                  ("TIMESTAMP", FIRST_SEEN))


def test_resend_pending_stops_at_the_first_failure_and_never_raises(sim):
    sim["events"]["a"] = _event(id="ea")
    sim["events"]["b"] = _event(id="eb")
    sim["fail_insert"] = True
    assert process.resend_pending() == 0
    assert sim["synced"] == []


def test_resend_pending_is_off_without_bigquery(sim, monkeypatch):
    sim["events"]["a"] = _event()
    monkeypatch.setattr(process.settings, "USE_BQ", False)
    assert process.resend_pending() == 0 and sim["inserts"] == []


def test_pubsub_returns_500_for_a_retry_and_http_503(sim, monkeypatch):
    from fastapi.testclient import TestClient

    from similarity import main

    sim["events"]["m1"] = _event()
    sim["fail_insert"] = True
    c = TestClient(main.app)
    data = base64.b64encode(json.dumps({"name": KEY, "bucket": "b"}).encode()).decode()
    r = c.post("/", json={"message": {"data": data, "messageId": "m1", "attributes": {"objectId": KEY}}})
    assert r.status_code == 500
    r = c.post("/v1/similarity/process", json={"gcs_object_key": KEY, "idempotency_key": "m1"})
    assert r.status_code == 503


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return ("e1", FIRST_SEEN)


def test_record_event_returns_id_and_marks_events_without_a_row_synced(monkeypatch):
    log = []

    @contextmanager
    def fake_connect():
        yield types.SimpleNamespace(cursor=lambda: _Cur(log))

    monkeypatch.setattr(db, "connect", fake_connect)
    common = dict(gcs_bucket="b", gcs_object_key=KEY, user_uuid="c1", pin_code="560034", brand_name=None)
    assert db.record_event(outcome="user_match", matched_garment_id="g1", order_id="o1", item_id="1",
                           **common) == {"id": "e1", "created_at": FIRST_SEEN}
    sql, params = log[-1]
    assert "RETURNING id::text, created_at" in sql and "bq_synced_at" in sql
    assert params[-3:] == ("o1", "1", True)  # pending until BigQuery has the row
    db.record_event(outcome="error", error_message="bad", **common)
    assert log[-1][1][-1] is False  # nothing to send: synced at insert


def test_migration_adds_tracking_columns_once():
    sql = (ROOT / "db" / "migrations" / "011_similarity_bq_sync.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS order_id TEXT" in sql and "ADD COLUMN IF NOT EXISTS item_id TEXT" in sql
    assert "column_name = 'bq_synced_at'" in sql  # existing events marked synced only when the column is new
    assert "WHERE bq_synced_at IS NULL" in sql
    assert "garment_pin_counts" not in sql.split("\n", 4)[-1]
