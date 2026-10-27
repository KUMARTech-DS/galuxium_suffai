"""AURA New chat, a fresh campaign after a 1P launch, and requests that belong to another page."""

import json

import aura_agent
from test_aura_loop import _FC, _install_fake_vertex, _PartObj, _Resp
from test_aura_tools import METRICS, OWN, UUID_RE, _ctx, _no_uuid, _png, _sess, _submit, fake_db  # noqa: F401

GEO_UI = {"active_tab": "geo", "geo": {"filters": {"pin": ["560008"], "rfm": ["Champions"]}}}
JOB = "33333333-3333-4333-8333-333333333333"


def _reset(sess, **body):
    return aura_agent.reset_chat(aura_agent.AgentChatRequest(**body), sess=sess)


def _ctx_ui(sess, ui):
    ctx = _ctx(sess)
    ctx.ui = ui
    return ctx


# ---------------------------------------------------------------------------
# New chat
# ---------------------------------------------------------------------------


def test_reset_clears_conversation_forms_and_confirms_but_keeps_campaign(fake_db):
    db, calls = fake_db
    sess = _sess(token="tok-reset1")
    ctx = _ctx(sess)
    form = aura_agent.execute_tool("open_campaign_setup", {"campaign": "Zara Summer"}, ctx).form
    db[OWN].update(status="configured")
    pending = aura_agent.execute_tool("launch_campaign", {"campaign": "Zara Summer"}, ctx).pending
    ctx.state.history = [("user", "hi"), ("model", "hello")]

    resp = _reset(sess, context={"active_tab": "campaigns"})
    assert resp["ok"] is True and resp["reset"] is True and resp["ui_commands"] == []
    st = aura_agent._state_for(sess["token"])
    assert st.history == [] and st.forms == {}
    assert st.active_campaign_id == OWN
    assert "\"Zara Summer\", is set up and ready to launch" in resp["answer"]
    assert not UUID_RE.search(resp["answer"])

    late = aura_agent.run_agent(aura_agent.AgentChatRequest(confirm_token=pending["token"]), sess=sess)
    assert late.status_code == 410 and calls["launch"] == []
    assert _submit(sess, form["form_id"], {}).status_code == 410


def test_reset_only_revokes_its_own_confirmations(fake_db):
    db, calls = fake_db
    db[OWN].update(status="configured")
    other = _sess(token="tok-reset2b")
    keep = aura_agent.execute_tool("launch_campaign", {"campaign": "Zara Summer"}, _ctx(other)).pending
    _reset(_sess(token="tok-reset2a"))
    resp = aura_agent.run_agent(aura_agent.AgentChatRequest(confirm_token=keep["token"]), sess=other)
    assert resp["ok"] is True and calls["launch"] == [OWN]


def test_reset_greeting_says_where_the_brand_is(fake_db):
    sess = _sess(token="tok-reset3")
    assert aura_agent.stage_uploads(sess, [("a.png", "image/png", _png())])["staged"] == 1
    resp = _reset(sess, context=GEO_UI)
    answer = resp["answer"]
    assert answer.startswith("Fresh chat. You're on Geo with PIN 560008 · Champions.")
    assert "1 SKU image still attached" in answer
    assert "start a campaign" in answer
    assert resp["attached_images"] == 1


def test_reset_with_launched_campaign_on_screen(fake_db):
    db, _ = fake_db
    db[OWN].update(status="launched")
    sess = _sess(token="tok-reset4")
    st = aura_agent._state_for(sess["token"])
    st.active_campaign_id = OWN
    resp = _reset(sess, context={"active_tab": "campaigns"}, ui_focus={"campaign_id": OWN})
    assert "running (simulated)" in resp["answer"]
    assert "Campaign performance figures are simulated." in resp["answer"]
    assert st.active_campaign_id is None and st.last_launched_campaign_id == OWN


def test_reset_gates_and_rate_limit(fake_db, monkeypatch):
    assert _reset(None).status_code == 401
    assert _reset(_sess(token="tok-reset5", role="customer")).status_code == 403
    assert _reset(_sess(token="tok-reset5", brand="")).status_code == 403
    assert _reset(_sess(token="tok-reset5a", role="admin", brand=None))["ok"] is True
    monkeypatch.setattr(aura_agent._chat_rate, "allow", lambda key: False)
    assert _reset(_sess(token="tok-reset5b")).status_code == 429


def test_reset_greeting_failure_is_masked(fake_db, monkeypatch):
    def boom(ctx, on_screen):
        raise RuntimeError("connection to 10.0.0.7 refused")

    monkeypatch.setattr(aura_agent, "_fresh_greeting", boom)
    resp = _reset(_sess(token="tok-reset6"))
    assert resp["answer"] == "Fresh chat. How can I help?"


def test_reset_route(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    import auth_acl
    import main

    monkeypatch.setattr(auth_acl, "get_session", lambda tok: _sess(token=tok) if tok == "tok-reset-http" else None)
    client = TestClient(main.app)
    r = client.post("/v1/agent/reset", json={})
    assert r.status_code == 401 and set(r.json()) == {"ok", "error", "request_id"}
    r = client.post("/v1/agent/reset", json={"context": {"active_tab": "ads"}}, headers={"X-Session-Token": "tok-reset-http"})
    assert r.status_code == 200 and r.json()["reset"] is True
    assert "You're on Ads Lab" in r.json()["answer"]


# ---------------------------------------------------------------------------
# Fresh campaign after a launch
# ---------------------------------------------------------------------------


def test_launch_clears_campaign_for_the_next_one(fake_db):
    db, calls = fake_db
    db[OWN].update(status="configured")
    ctx = _ctx(_sess(token="tok-fresh1"))
    ctx.state.active_campaign_id = OWN
    ctx.state.draft_audience = {"pin_codes": ["560008"]}
    msg, cmds = aura_agent._do_launch(ctx, {"campaign_id": OWN})
    assert calls["launch"] == [OWN]
    assert "Ads Lab is cleared" in msg and "simulated" in msg
    assert cmds[-1] == {"type": "campaign_launched"}
    assert {"type": "load_campaign", "campaign_id": OWN, "focus": "analysis"} in cmds
    st = ctx.state
    assert (st.active_campaign_id, st.active_job_id, st.ranked, st.draft_audience) == (None, None, [], None)
    assert st.last_launched_campaign_id == OWN
    # "how is the campaign doing" still reaches the campaign that was just launched
    assert aura_agent._resolve_campaign(ctx, "this", statuses={"launched"})["campaign_id"] == OWN


def test_ui_launch_clears_aura_campaign(fake_db):
    db, _ = fake_db
    ctx = _ctx(_sess(token="tok-fresh2"))
    ctx.state.active_campaign_id = OWN
    db[OWN].update(status="launched")
    on_screen = aura_agent._sync_with_ui(ctx, {"campaign_id": OWN})
    assert on_screen == {"campaign": "Zara Summer", "status": "launched"}
    assert ctx.state.active_campaign_id is None and ctx.state.last_launched_campaign_id == OWN


def test_start_new_campaign_with_dashboard_audience(fake_db):
    ctx = _ctx_ui(_sess(token="tok-fresh3"), GEO_UI)
    ctx.state.active_campaign_id = OWN
    ctx.state.ranked = [{"rank": 1}]
    ctx.state.active_job_id = JOB
    out = aura_agent.execute_tool("start_new_campaign", {}, ctx)
    assert out.result["ok"] is True and out.result["fresh_campaign"] is True
    assert out.result["audience"]["pin_codes"] == ["560008"]
    assert "keeps running" in out.result["note"]
    types = [c["type"] for c in out.ui_commands]
    assert types == ["new_campaign", "switch_tab", "prefill_campaign"]
    assert out.ui_commands[1]["tab"] == "ads"
    prefill = out.ui_commands[2]["context"]
    assert prefill["pins"] == ["560008"] and prefill["rfm_segment"] == "Champions" and prefill["source"] == "aura"
    assert ctx.state.active_campaign_id is None and ctx.state.ranked == [] and ctx.state.active_job_id is None
    assert ctx.state.draft_audience["pin_codes"] == ["560008"]
    assert out.form is None and "attach" in out.result["next"].lower()
    assert _no_uuid(out.result)


def test_start_new_campaign_ignores_previous_campaign_form_audience(fake_db):
    ctx = _ctx_ui(_sess(token="tok-fresh4"), {"active_tab": "campaigns"})
    ctx.state.draft_audience = {"pin_codes": ["560008"], "age_groups": [], "genders": [], "rfm_segments": []}
    out = aura_agent.execute_tool("start_new_campaign", {}, ctx)
    assert "audience" not in out.result
    assert out.form and out.form["kind"] == "audience" and out.form["submit_label"] == "Use this audience"
    assert [c["type"] for c in out.ui_commands] == ["new_campaign", "switch_tab"]

    pin = next(f for f in out.form["fields"] if f["id"] == "pin_codes")["options"][0]["value"]
    resp = _submit(ctx.sess, out.form["form_id"], {"pin_codes": [pin], "age_groups": "", "genders": "", "rfm_segments": ""})
    assert resp["ok"] is True and "Attach your SKU images" in resp["answer"]
    prefill = next(c for c in resp["ui_commands"] if c["type"] == "prefill_campaign")
    assert prefill["context"]["pins"] == [pin]
    assert {"type": "request_upload"} in resp["ui_commands"]


def test_start_new_campaign_with_images_shows_rank_form(fake_db):
    sess = _sess(token="tok-fresh5")
    assert aura_agent.stage_uploads(sess, [("a.png", "image/png", _png())])["staged"] == 1
    out = aura_agent.execute_tool("start_new_campaign", {"pin_codes": ["560008"]}, _ctx_ui(sess, GEO_UI))
    assert out.result["attached_images"] == 1
    assert out.form and out.form["kind"] == "audience" and out.form["submit_label"] == "Start ranking"


def test_start_new_campaign_needs_ads_tab(fake_db):
    sess = {**_sess(token="tok-fresh6"), "allowed_tabs": ["geo"]}
    out = aura_agent.execute_tool("start_new_campaign", {}, _ctx(sess))
    assert out.result["ok"] is False and out.result["error_code"] == "not_allowed"
    assert out.ui_commands == []


def test_start_new_campaign_is_registered_as_a_write():
    spec = aura_agent.TOOLS["start_new_campaign"]
    assert spec.writes is True and spec.confirm is False
    assert "start_new_campaign" in aura_agent.AURA_SYSTEM and "PAGES" in aura_agent.AURA_SYSTEM


# ---------------------------------------------------------------------------
# Requests that belong to another page
# ---------------------------------------------------------------------------


def test_route_hint_cases():
    hint = aura_agent._route_hint("Can you create a campaign for these PINs?", "geo")
    assert hint == {"intent": "create_campaign", "page": "Ads Lab", "act_with": "start_new_campaign", "you_are_on": "Geo"}
    assert aura_agent._route_hint("start over", "campaigns")["intent"] == "create_campaign"
    assert aura_agent._route_hint("I want another campaign", "campaigns")["act_with"] == "start_new_campaign"
    hint = aura_agent._route_hint("How is my campaign performing?", "ads")
    assert hint["intent"] == "monitor_campaign" and hint["page"] == "Campaigns" and hint["you_are_on"] == "Ads Lab"
    assert aura_agent._route_hint("rank my SKU images", "geo")["act_with"] == "rank_uploaded_skus"
    hint = aura_agent._route_hint("Which PIN codes have demand for kurtas?", "geo")
    assert hint == {"intent": "geo_analysis", "page": "Geo", "act_with": "analyze_geo"}
    assert aura_agent._route_hint("hello there", "geo") is None
    assert aura_agent._route_hint("create a campaign", "geo", ["geo"]) is None


def test_create_campaign_from_geo_runs_in_ads_lab(fake_db, monkeypatch):
    script = [
        _Resp([_PartObj(function_call=_FC("start_new_campaign", {}))]),
        _Resp([_PartObj(text="Opened a fresh campaign in Ads Lab for PIN 560008. Attach your SKU images.")]),
    ]
    seen = _install_fake_vertex(monkeypatch, script)
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="Create a new campaign for this audience", context=GEO_UI),
        sess=_sess(token="tok-route1"),
    )
    assert resp["ok"] is True
    types = [c["type"] for c in resp["ui_commands"]]
    assert types[:2] == ["new_campaign", "switch_tab"] and "prefill_campaign" in types
    assert {"type": "switch_tab", "tab": "ads"} in resp["ui_commands"]
    prompt = seen["contents"][0][-1].parts[0].text
    hint = {"intent": "create_campaign", "page": "Ads Lab", "act_with": "start_new_campaign", "you_are_on": "Geo"}
    assert json.dumps(hint)[1:-1] in prompt or json.dumps(hint, separators=(",", ":"))[1:-1] in prompt
