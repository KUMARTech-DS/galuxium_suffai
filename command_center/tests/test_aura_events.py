"""Shell-driven AURA events: images attached, ranking job finished or failed (no model call)."""

import json

import pytest

import aura_agent
import campaign_jobs
import campaign_service as svc
from test_aura_tools import JOB_ID, METRICS, OWN, UUID_RE, _job, _png, _sess, fake_db  # noqa: F401


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setenv("ENABLE_VERTEX", "true")
    monkeypatch.setattr(aura_agent, "_tool_declarations", lambda: (_ for _ in ()).throw(AssertionError("model reached")))


def _event(sess, event, job_id=None):
    return aura_agent.run_agent(
        aura_agent.AgentChatRequest(event=event, job_id=job_id), sess=sess,
    )


def test_uploads_ready_returns_audience_form(fake_db):
    sess = _sess(token="tok-ev-up")
    assert aura_agent.stage_uploads(sess, [("a.png", "image/png", _png()), ("b.png", "image/png", _png())])["staged"] == 2
    resp = _event(sess, "uploads_ready")
    assert resp["ok"] is True and resp["answer"].startswith("2 image(s) attached")
    form = resp["input_form"]
    assert form["kind"] == "audience" and form["submit_label"] == "Start ranking" and "meta" not in form
    assert resp["attached_images"] == 2


def test_uploads_ready_without_images_asks_to_attach(fake_db):
    resp = _event(_sess(token="tok-ev-empty"), "uploads_ready")
    assert resp["ok"] is True and resp["input_form"] is None
    assert "paperclip" in resp["answer"]


def test_ranking_done_posts_summary_and_run_mode(fake_db, monkeypatch):
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("succeeded", processed_images=2, eta_seconds=0))
    resp = _event(_sess(token="tok-ev-done"), "ranking_done", JOB_ID)
    assert resp["ok"] is True
    assert resp["answer"].startswith("Ranking finished: 2 of 2 SKU(s) ranked for 1,200 customers.")
    assert "#1 Tee" in resp["answer"] and not UUID_RE.search(resp["answer"])
    assert resp["input_form"]["kind"] == "run_mode"
    assert {"type": "refresh_campaigns"} in resp["ui_commands"]


def test_ranking_done_while_running_reports_progress(fake_db, monkeypatch):
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("running", processed_images=1, progress_pct=50.0))
    resp = _event(_sess(token="tok-ev-run"), "ranking_done", JOB_ID)
    assert "50.0% done" in resp["answer"] and resp["input_form"] is None


def test_ranking_failed_returns_masked_problem(fake_db, monkeypatch):
    msg = campaign_jobs.svc_public_error("all_failed")
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("failed", error=msg))
    resp = _event(_sess(token="tok-ev-fail"), "ranking_failed", JOB_ID)
    assert resp["ok"] is True and msg in resp["answer"]
    assert resp["input_form"] is None


def test_cancelled_job_is_reported_as_cancelled(fake_db, monkeypatch):
    monkeypatch.setattr(campaign_jobs, "get_job", lambda sess, jid: _job("cancelled", processed_images=1))
    resp = _event(_sess(token="tok-ev-cancel"), "ranking_failed", JOB_ID)
    assert resp["ok"] is True and resp["answer"].startswith("The ranking job was cancelled")
    assert "did not complete" not in resp["answer"] and resp["input_form"] is None


def test_foreign_or_bad_job_id_is_masked(fake_db, monkeypatch):
    def foreign(sess, jid):
        raise svc.ServiceError("not_found", "job not found", 404)

    monkeypatch.setattr(campaign_jobs, "get_job", foreign)
    resp = _event(_sess(token="tok-ev-foreign"), "ranking_done", JOB_ID)
    assert resp.status_code == 404
    assert JOB_ID not in resp.body.decode()
    bad = _event(_sess(token="tok-ev-bad"), "ranking_done", "not-a-uuid")
    assert bad.status_code == 400
    assert json.loads(bad.body)["error"]["code"] == "invalid_input"
