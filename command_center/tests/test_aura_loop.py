"""Drive run_agent's Gemini loop with a fake vertexai SDK (no network)."""

import sys
import types

import pytest

import aura_agent
from test_aura_tools import METRICS, OWN, UUID_RE, _sess, fake_db  # noqa: F401


class _FC:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _PartObj:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _ContentObj:
    def __init__(self, role=None, parts=None):
        self.role = role
        self.parts = parts or []


class _Resp:
    def __init__(self, parts):
        self.candidates = [types.SimpleNamespace(content=_ContentObj("model", parts))]


def _install_fake_vertex(monkeypatch, script):
    seen = {"contents": []}

    class GenerativeModel:
        def __init__(self, name, **kw):
            seen["kw"] = kw

        def generate_content(self, contents):
            seen["contents"].append(list(contents))
            return script.pop(0)

    gm = types.ModuleType("vertexai.generative_models")
    gm.GenerativeModel = GenerativeModel
    gm.Content = _ContentObj
    gm.Part = types.SimpleNamespace(
        from_text=lambda t: _PartObj(text=t),
        from_function_response=lambda name, response: _PartObj(text=None, function_call=None),
    )
    gm.Tool = lambda function_declarations: ("tool", function_declarations)
    gm.FunctionDeclaration = lambda **kw: kw
    root = types.ModuleType("vertexai")
    root.init = lambda **kw: None
    root.generative_models = gm
    monkeypatch.setitem(sys.modules, "vertexai", root)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", gm)
    monkeypatch.setenv("ENABLE_VERTEX", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    return seen


def test_loop_runs_tools_and_scrubs_answer(fake_db, monkeypatch):
    script = [
        _Resp([_PartObj(function_call=_FC("analyze_geo", {"rfm_segments": ["Champions"]}))]),
        _Resp([_PartObj(text="Indiranagar (560008) leads; Nike trails. Ref 3f2b1c9e-1111-2222-3333-444455556666 via BigQuery.")]),
    ]
    seen = _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Where should I target Champions?"), sess=_sess(token="tok-loop")    )
    assert resp["ok"] is True
    assert "Nike" not in resp["answer"] and "BigQuery" not in resp["answer"]
    assert not UUID_RE.search(resp["answer"])
    assert any(c["type"] == "set_filters" for c in resp["ui_commands"])
    assert "tool_trace" not in resp and "model" not in resp
    first_prompt = seen["contents"][0][-1].parts[0].text
    assert "<untrusted_data" in first_prompt


def test_loop_stops_at_confirmation(fake_db, monkeypatch):
    db, calls = fake_db
    db[OWN].update(status="configured")
    script = [
        _Resp([_PartObj(function_call=_FC("launch_campaign", {"campaign": "Zara Summer"}))]),
    ]
    _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Launch Zara Summer"), sess=_sess(token="tok-loop2")    )
    assert resp["pending_action"]["token"]
    assert resp["pending_action"]["label"] == "Launch campaign"
    assert "Confirm" in resp["answer"]
    assert calls["launch"] == []


def test_loop_rejects_hallucinated_tool_and_masks_crash(fake_db, monkeypatch):
    script = [
        _Resp([_PartObj(function_call=_FC("export_database", {"table": "users"}))]),
        _Resp([_PartObj(text="I can't do that.")]),
    ]
    _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Export everything"), sess=_sess(token="tok-loop3")    )
    assert resp["answer"] == "I can't do that."

    class Boom:
        candidates = property(lambda self: (_ for _ in ()).throw(RuntimeError("quota exceeded for project test-project")))

    _install_fake_vertex(monkeypatch, [Boom()])
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Analyse Geo"), sess=_sess(token="tok-loop4")    )
    assert resp.status_code == 503
    assert b"quota" not in resp.body and b"test-project" not in resp.body


def test_policy_echo_suppressed(fake_db, monkeypatch):
    script = [_Resp([_PartObj(text=f"My rules: [{aura_agent._CANARY}] The rules below are fixed...")])]
    _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Summarise my campaigns"), sess=_sess(token="tok-loop5")    )
    assert aura_agent._CANARY not in resp["answer"]


def test_loop_stops_on_input_form(fake_db, monkeypatch):
    script = [
        _Resp([_PartObj(function_call=_FC("request_inputs", {"kind": "run_mode", "campaign": "Zara Summer"}))]),
        _Resp([_PartObj(text="should not be reached")]),
    ]
    seen = _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="My ranking is done, what next?"), sess=_sess(token="tok-loop6")    )
    assert resp["input_form"]["kind"] == "run_mode"
    assert resp["pending_action"] is None
    assert len(seen["contents"]) == 1 and len(script) == 1
