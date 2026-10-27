"""AURA follows what the brand does in the tabs without it (ui_focus + live filters)."""

import pytest

import aura_agent
import campaign_jobs
from test_aura_loop import _install_fake_vertex, _PartObj, _Resp
from test_aura_tools import JOB_ID, METRICS, OTHER, OWN, UUID_RE, _campaign, _ctx, _job, _sess, fake_db  # noqa: F401

OLD = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def two_zara(fake_db):
    db, calls = fake_db
    db[OLD] = _campaign(OLD, "Zara", "configured", "Zara Winter")
    return db, calls


def _no_lookups(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("lookup not expected")

    monkeypatch.setattr(campaign_jobs, "get_job", boom)
    monkeypatch.setattr(campaign_jobs, "list_jobs", boom)


def test_campaign_opened_in_the_tabs_becomes_current(two_zara):
    ctx = _ctx(_sess(token="tok-ui-camp"))
    ctx.state.active_campaign_id = OLD  # AURA worked on another campaign earlier
    on_screen = aura_agent._sync_with_ui(ctx, {"campaign_id": OWN})
    assert on_screen == {"campaign": "Zara Summer", "status": "ranked"}
    assert aura_agent._resolve_campaign(ctx, "current")["campaign_id"] == OWN

    # AURA's own later action stands while the tabs show the same campaign
    ctx.state.active_campaign_id = OLD
    aura_agent._sync_with_ui(ctx, {"campaign_id": OWN})
    assert ctx.state.active_campaign_id == OLD


def test_ranking_started_with_the_ads_lab_button_is_tracked(fake_db, monkeypatch):
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("running", processed_images=1, progress_pct=40.0))
    monkeypatch.setattr(campaign_jobs, "list_jobs", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fallback used")))
    ctx = _ctx(_sess(token="tok-ui-job"))
    on_screen = aura_agent._sync_with_ui(ctx, {"job_id": JOB_ID})
    assert on_screen == {"ranking_job": {"status": "running", "progress_pct": 40.0}}
    assert ctx.state.active_job_id == JOB_ID and ctx.state.active_campaign_id == OWN
    out = aura_agent.execute_tool("get_ranking_job", {}, ctx)
    assert out.result["status"] == "running" and out.result["progress_pct"] == 40.0


def test_finished_or_cancelled_job_in_the_tabs(two_zara, monkeypatch):
    ctx = _ctx(_sess(token="tok-ui-jobend"))
    ctx.state.active_job_id, ctx.state.active_campaign_id = JOB_ID, OLD
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("cancelled", processed_images=1))
    assert aura_agent._sync_with_ui(ctx, {"job_id": JOB_ID}) == {}
    assert ctx.state.active_job_id is None and ctx.state.active_campaign_id == OLD

    other_job = "55555555-5555-4555-8555-555555555555"
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("succeeded", job_id=jid, processed_images=2))
    aura_agent._sync_with_ui(ctx, {"job_id": other_job, "campaign_id": OWN})
    assert ctx.state.active_campaign_id == OWN and ctx.state.active_job_id is None


def test_foreign_and_malformed_refs_are_ignored(two_zara, monkeypatch):
    ctx = _ctx(_sess(token="tok-ui-foreign"))
    ctx.state.active_campaign_id = OLD
    assert aura_agent._sync_with_ui(ctx, {"campaign_id": OTHER}) == {}  # Nike's campaign, Zara session
    assert ctx.state.active_campaign_id == OLD

    _no_lookups(monkeypatch)
    for bad in ({"campaign_id": "not-a-uuid", "job_id": "1; drop table"}, {"campaign_id": 7}, "x", None):
        assert aura_agent._sync_with_ui(ctx, bad) == {}
    assert ctx.state.active_campaign_id == OLD


def test_filters_changed_in_the_tabs_replace_an_earlier_aura_audience(fake_db):
    ctx = _ctx(_sess(token="tok-ui-aud"))
    ctx.ui = {"ads": {"filters": {"pin_codes": ["560008"], "age_group": "25-34"}}}
    aura_agent._sync_with_ui(ctx, None)
    ctx.state.draft_audience = {"pin_codes": ["560001"], "age_groups": [], "genders": [], "rfm_segments": []}

    aura_agent._sync_with_ui(ctx, None)  # tabs unchanged: the AURA form's audience stays
    assert ctx.state.draft_audience["pin_codes"] == ["560001"]

    ctx.ui = {"ads": {"filters": {"pin_codes": ["560034"]}}}
    aura_agent._sync_with_ui(ctx, None)
    assert ctx.state.draft_audience is None
    aud = aura_agent._audience_from(ctx, aura_agent.RankArgs(), True)
    assert aud["pin_codes"] == ["560034"]


def test_on_screen_reaches_the_model_without_ids(fake_db, monkeypatch):
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("running", progress_pct=25.0))
    seen = _install_fake_vertex(monkeypatch, [_Resp([_PartObj(text="Your ranking is 25% done.")])])
    resp = aura_agent.run_agent(
        aura_agent.AgentChatRequest(message="How is my ranking going?", ui_focus={"campaign_id": OWN, "job_id": JOB_ID}),
        sess=_sess(token="tok-ui-model"),
    )
    assert resp["ok"] is True
    prompt = seen["contents"][0][-1].parts[0].text
    assert "on_screen" in prompt and "Zara Summer" in prompt and '"progress_pct": 25.0' in prompt
    assert not UUID_RE.search(prompt)
