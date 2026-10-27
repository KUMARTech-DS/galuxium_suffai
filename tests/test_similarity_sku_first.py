"""Similarity: warehouse SKU match before fingerprints (no GCP / DB)."""

import sys
import types
from types import SimpleNamespace

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

from similarity import process  # noqa: E402

USER, PIN, SKU = "c1", "560008", "levis:511:indigo"
KEY = f"garments/{USER}_o1_1.jpg"


class FakeDB:
    SYNC_OUTCOMES = process.db.SYNC_OUTCOMES

    def __init__(self, user_sku=None, global_sku=None, user_cands=None, global_cands=None):
        self.user_sku, self.global_sku = user_sku, global_sku
        self.user_cands, self.global_cands = user_cands or [], global_cands or []
        self.calls = []

    def event_for_key(self, key):
        return None

    def find_user_garment_by_sku(self, user, sku):
        self.calls.append(("find_user_sku", user, sku))
        return self.user_sku

    def find_garment_by_sku(self, sku):
        self.calls.append(("find_sku", sku))
        return self.global_sku

    def load_user_embeddings(self, user):
        return self.user_cands

    def load_global_embeddings(self, *, brand_name, limit):
        return self.global_cands

    def increment_total(self, *, garment_id):
        self.calls.append(("count", garment_id))

    def link_user_to_garment(self, *, user_uuid, garment_id, object_key, pin_code):
        self.calls.append(("link", user_uuid, garment_id, pin_code))

    def set_garment_sku(self, garment_id, sku):
        self.calls.append(("set_sku", garment_id, sku))

    def create_unique_garment(self, **kw):
        self.calls.append(("create", kw.get("brand_sku")))
        return "g-new"

    def record_event(self, **kw):
        self.calls.append(("event", kw["outcome"], kw.get("matched_garment_id")))
        return {"id": "e1"}


@pytest.fixture
def env(monkeypatch):
    embeds = []
    monkeypatch.setattr(process.settings, "DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(process, "read_image", lambda key, bucket=None: SimpleNamespace(
        bucket="b", object_key=key, metadata={}))
    monkeypatch.setattr(process, "embed_image", lambda img: embeds.append(img) or ([1.0, 0.0], "mm@001"))
    monkeypatch.setattr(process, "send_observation", lambda event: True)
    monkeypatch.setattr(process.bq_sync, "lookup_photo_owner", lambda user, order: {"pin_code": PIN})

    def run(fake, **extra):
        monkeypatch.setattr(process, "db", fake)
        return process.process_upload({"gcs_object_key": KEY, "idempotency_key": "sim:1", **extra})

    return run, embeds


def test_user_sku_match_counts_without_embedding(env):
    run, embeds = env
    fake = FakeDB(user_sku="g-mine", global_sku="g-other")
    out = run(fake, brand_sku=SKU)
    assert out["status"] == "user_sku_match" and out["garment_id"] == "g-mine"
    assert embeds == []
    assert ("count", "g-mine") in fake.calls
    assert not any(c[0] == "link" for c in fake.calls)
    assert ("find_sku", SKU) not in fake.calls


def test_global_sku_match_links_and_counts(env):
    run, embeds = env
    fake = FakeDB(global_sku="g-other")
    out = run(fake, brand_sku=SKU)
    assert out["status"] == "global_sku_match" and out["added_to_user_wardrobe"] is True
    assert embeds == []
    assert ("link", USER, "g-other", PIN) in fake.calls and ("count", "g-other") in fake.calls
    assert ("event", "global_sku_match", "g-other") in fake.calls


def test_no_sku_uses_fingerprints(env):
    run, embeds = env
    fake = FakeDB(user_cands=[("g-mine", [1.0, 0.0], None)])
    out = run(fake)
    assert out["status"] == "user_match"
    assert len(embeds) == 1
    assert not any(c[0] in ("find_user_sku", "find_sku", "set_sku") for c in fake.calls)


def test_fingerprint_match_backfills_sku(env):
    run, embeds = env
    fake = FakeDB(user_cands=[("g-mine", [1.0, 0.0], None)])
    out = run(fake, brand_sku=SKU)
    assert out["status"] == "user_match"
    assert ("set_sku", "g-mine", SKU) in fake.calls and len(embeds) == 1


def test_global_fingerprint_match_backfills_sku(env):
    run, _ = env
    fake = FakeDB(global_cands=[("g-other", [1.0, 0.0], None)])
    out = run(fake, brand_sku=SKU)
    assert out["status"] == "global_match_new_user_link"
    assert ("set_sku", "g-other", SKU) in fake.calls


def test_new_garment_stores_sku(env):
    run, _ = env
    fake = FakeDB()
    out = run(fake, brand_sku=SKU)
    assert out["status"] == "created_new" and ("create", SKU) in fake.calls


def test_sku_outcomes_write_bigquery_observations():
    assert {"user_sku_match", "global_sku_match"} <= set(process.db.SYNC_OUTCOMES)
