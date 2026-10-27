"""Regression tests for the backend audit fixes (auth gaps, leaks, connection close, off-by-one)."""

import copy
import json
import sys
import time
import types
from datetime import date
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "metrics.json"
METRICS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SECRET = "psycopg2.OperationalError: password authentication failed for user suffai at 10.1.2.3"

SESSIONS = {
    "adm": {"role": "admin", "username": "ops-admin"},
    "brd": {"role": "brand", "username": "zara-user", "brand_name": "Zara"},
    "cus": {"role": "customer", "username": "c1", "customer_id": "u1"},
}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import auth_acl
    import main

    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    monkeypatch.setattr(main, "compute_metrics", lambda: copy.deepcopy(METRICS))
    return TestClient(main.app)


@pytest.mark.parametrize(
    "method,path,kw",
    [
        ("get", "/v1/metrics", {}),
        ("post", "/v1/geo/brief", {"json": {}}),
        ("post", "/v1/chat", {"json": {"message": "hi"}}),
        ("post", "/v1/ads-lab/extract", {"files": [("files", ("a.jpg", b"x", "image/jpeg"))]}),
    ],
)
def test_routes_require_brand_or_admin(client, method, path, kw):
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers={"X-Session-Token": "cus"}, **kw).status_code == 403


def test_metrics_masks_brand_and_fails_closed(client, monkeypatch):
    import auth_acl

    body = client.get("/v1/metrics", headers={"X-Session-Token": "brd"}).json()
    assert {g["brand"] for g in body["garment_cells"]} <= {"Zara", "Competitors"}

    def boom(*_a, **_k):
        raise RuntimeError("mask bug")

    monkeypatch.setattr(auth_acl, "mask_metrics_for_brand", boom)
    r = client.get("/v1/metrics", headers={"X-Session-Token": "brd"})
    assert r.status_code == 500 and "garment_cells" not in r.text


def test_chat_summary_masks_competitors_for_brand():
    import chatbot

    out = chatbot.slim_metrics(copy.deepcopy(METRICS), "Zara")
    assert {b["brand"] for b in out["top_brands"]} <= {"Zara", "Competitors"}


def test_bq_failure_detail_is_masked(monkeypatch):
    from fastapi import HTTPException

    import aggregate_metrics as agg
    import main

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("USE_BQ", "true")

    def broken(*_a, **_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(agg, "load_from_bq", broken)
    with pytest.raises(HTTPException) as err:
        main.compute_metrics()
    assert SECRET not in err.value.detail and "Reference:" in err.value.detail


def test_extraction_jobs_count_closes_connection(monkeypatch):
    import main

    closed = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *_a):
            pass

        def fetchone(self):
            return (7,)

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=lambda dsn: Conn()))
    assert main._count_extraction_jobs() == 7
    assert closed == [True]


def test_extract_error_is_generic(monkeypatch):
    import campaign_service as svc

    def broken(**_k):
        raise RuntimeError("urlopen error http://10.0.0.5:8080/v1/extract refused")

    monkeypatch.setattr(svc, "_call_extraction", broken)
    pctx = svc.PipelineContext(brand="Zara", pins=["560008"], extraction_url="u", similarity_url="s",
                               bucket="b", bkt=None, profile={}, extract_token=None, sim_token=None)
    out = svc.extract_sku(pctx, "k")
    assert out["extract_ok"] is False and out["extract_error"] == "attribute extraction failed"


def test_launched_duration_end_is_inclusive():
    import admin_intel as ai

    row = {"status": "launched", "start_date": "2026-10-04", "duration_days": 14}  # last day 2026-10-17
    assert ai.map_status(row, today=date(2026, 10, 17)) == "Active"
    assert ai.map_status(row, today=date(2026, 10, 18)) == "Completed"


def test_expired_sessions_are_purged(monkeypatch):
    import auth_acl

    monkeypatch.setattr(auth_acl, "_SESSIONS", {"old": {"expires_at": time.time() - 1}})
    auth_acl.create_session({"role": "admin", "username": "a"})
    assert "old" not in auth_acl._SESSIONS and len(auth_acl._SESSIONS) == 1
