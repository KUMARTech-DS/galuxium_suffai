"""Raw backend / model errors are logged, never returned to the page."""

import copy
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

import campaign_service as svc
import chatbot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "metrics.json"
METRICS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SECRET = "psycopg2.OperationalError: password authentication failed for user suffai at 10.1.2.3"


def _assert_masked(text: str):
    assert SECRET not in text
    for leak in ("Vertex", "Gemini", "GOOGLE_CLOUD_PROJECT", "ENABLE_VERTEX", "psycopg2"):
        assert leak not in text
    assert "Reference:" in text


def _fake_vertexai(monkeypatch, exc: Exception):
    class Model:
        def __init__(self, *_a, **_k):
            pass

        def generate_content(self, *_a, **_k):
            raise exc

    vertexai = types.ModuleType("vertexai")
    vertexai.init = lambda **_k: None
    gen = types.ModuleType("vertexai.generative_models")
    gen.GenerativeModel = Model
    vertexai.generative_models = gen
    monkeypatch.setitem(sys.modules, "vertexai", vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", gen)


def test_vertex_failure_detail_is_generic(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    _fake_vertexai(monkeypatch, RuntimeError(SECRET))
    with pytest.raises(HTTPException) as err:
        chatbot.call_vertex_chat("hi", {}, "req123")
    assert err.value.status_code == 503
    _assert_masked(err.value.detail)
    assert "req123" in err.value.detail


def test_missing_project_detail_is_generic(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    with pytest.raises(HTTPException) as err:
        chatbot.call_vertex_chat("hi", {}, "req1")
    _assert_masked(err.value.detail)


def test_chat_disabled_detail_is_generic(monkeypatch):
    monkeypatch.setenv("ENABLE_VERTEX", "false")
    res = chatbot.run_chat(chatbot.ChatRequest(message="hello"), lambda: copy.deepcopy(METRICS))
    body = json.loads(res.body)
    assert res.status_code == 503
    _assert_masked(body["detail"])


def test_metrics_summary_error_has_no_exception_text(monkeypatch):
    assert chatbot.slim_metrics(None) == {"error": "metrics_unavailable"}
    sent = {}

    def boom():
        raise RuntimeError(SECRET)

    monkeypatch.setenv("ENABLE_VERTEX", "true")
    monkeypatch.setattr(chatbot, "call_vertex_chat", lambda msg, ctx, rid: sent.update(ctx) or "ok")
    res = chatbot.run_chat(chatbot.ChatRequest(message="hello"), boom)
    assert res["answer"] == "ok" and sent["metrics"] == {"error": "metrics_unavailable"}
    assert SECRET not in json.dumps(sent)


@pytest.mark.parametrize("failure", ["http", "exception"])
def test_geo_brief_fallback_hides_cause(monkeypatch, failure):
    import main

    monkeypatch.setenv("ENABLE_VERTEX", "true")
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: copy.deepcopy(METRICS))

    def broken(*_a, **_k):
        if failure == "http":
            raise HTTPException(503, f"Vertex chat failed: {SECRET}")
        raise RuntimeError(SECRET)

    monkeypatch.setattr(chatbot, "call_vertex_chat", broken)
    body = main.GeoBriefRequest(
        focus_pin={"pin": "560008", "demand": 3, "brand_garments": 2, "brand_share": 0.5,
                   "est_footfall": 120, "heuristic_decision": "Yes", "score": 0.9},
        analysis_brand="Zara",
    )
    out = main.geo_brief_core(body, {"role": "admin"})
    assert out["fallback"] is True and out["reason"] == "ai_unavailable"
    text = json.dumps(out, default=str)
    assert SECRET not in text and "Gemini unavailable" not in text
    _assert_masked(out["detail"])


def test_geo_brief_disabled_reason(monkeypatch):
    import main

    monkeypatch.setenv("ENABLE_VERTEX", "false")
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: copy.deepcopy(METRICS))
    out = main.geo_brief_core(main.GeoBriefRequest(focus_pin={"pin": "560008", "heuristic_decision": "No"}), {"role": "admin"})
    assert out["reason"] == "ai_disabled" and "ENABLE_VERTEX" not in json.dumps(out, default=str)


def test_masked_http_carries_only_a_reference():
    import main

    try:
        raise RuntimeError(SECRET)
    except RuntimeError:
        exc = main._masked_http(503, "unit test")
    assert exc.status_code == 503
    _assert_masked(exc.detail)
