import pytest

import brand_campaign as bc
import campaign_jobs as jobs
import campaign_service as svc
import style_assistant as style_asst

JOB = "33333333-3333-4333-8333-333333333333"
CID = "44444444-4444-4444-8444-444444444444"


# ---------------------------------------------------------------------------
# ETA + counts
# ---------------------------------------------------------------------------


def test_eta_scales_with_images_garments_and_parallelism(monkeypatch):
    for k in ("CAMPAIGN_ETA_EXTRACT_SEC", "CAMPAIGN_ETA_COMPARE_BASE_SEC",
              "CAMPAIGN_ETA_COMPARE_PER_1K_SEC", "CAMPAIGN_JOB_PARALLELISM"):
        monkeypatch.delenv(k, raising=False)
    assert jobs.seconds_per_image(0) == pytest.approx(12.0)
    assert jobs.seconds_per_image(10_000) == pytest.approx(27.0)
    assert jobs.estimate_seconds(10, 10_000) == 270
    assert jobs.estimate_seconds(0, 10_000) == 0
    assert jobs.estimate_seconds(10, 0, per_image=3.0) == 30
    monkeypatch.setenv("CAMPAIGN_JOB_PARALLELISM", "3")
    assert jobs.estimate_seconds(10, 10_000) == 90


def test_garments_in_pins_sums_photographed_garments_of_selected_pins():
    metrics = {
        "pin_geo": {
            "560008": {"photo_garments": 15},
            "110001": {"photo_garments": 99},
            "560034": {"photo_garments": "bad"},
            "560001": "junk",
        },
        # CSV item rows are not compared by compare-pins and must not be counted
        "garment_cells": [{"pin_code": "560008", "garments": 500}],
    }
    assert jobs.garments_in_pins(metrics, ["560008", "560034", "560001", " 560008 "]) == 15
    assert jobs.garments_in_pins(metrics, []) == 0
    assert jobs.garments_in_pins({}, ["560008"]) == 0


def test_eligible_count_is_saved_on_the_campaign(monkeypatch):
    impact = []
    monkeypatch.setattr(style_asst, "eligible_customers", lambda aud: ["u1", "u2"] if aud["pin_codes"] == ["560008"] else [])
    monkeypatch.setattr(bc, "set_campaign_impact", lambda cid, **kw: impact.append((cid, kw)))
    assert jobs._eligible_count(CID, {"pin_codes": ["560008"]}) == 2
    assert impact == [(CID, {"eligible_users": 2})]

    def down(aud):
        raise RuntimeError("bq down")

    monkeypatch.setattr(style_asst, "eligible_customers", down)
    assert jobs._eligible_count(CID, {"pin_codes": ["560008"]}) is None  # never blocks the job


def test_get_job_reports_eligible_customers(monkeypatch):
    job = {"job_id": JOB, "campaign_id": CID, "brand_name": "Zara", "status": "running", "total_images": 4,
           "processed_images": 1, "failed_images": 0, "total_customers": 50, "total_garments": 300,
           "comparisons": 1200, "est_seconds": 60, "error_code": None, "created_at": None, "started_at": None,
           "finished_at": None}
    monkeypatch.setattr(jobs, "_load_job_for", lambda sess, jid: dict(job))
    monkeypatch.setattr(jobs, "_maybe_recover", lambda j: None)
    monkeypatch.setattr(bc, "sku_progress", lambda cid: {"done": 1, "failed": 0, "pending": 3, "avg_seconds": 5.0})
    monkeypatch.setattr(bc, "get_campaign", lambda cid: {"eligible_users": 7, "status": "processing"})
    out = jobs.get_job({"role": "brand"}, JOB)
    assert out["eligible_customers"] == 7 and out["total_customers"] == 50 and "campaign_name" not in out


def test_job_view_progress_and_public_error():
    job = {
        "job_id": JOB, "campaign_id": CID, "brand_name": "Zara", "status": "running",
        "total_images": 4, "processed_images": 1, "failed_images": 0, "total_customers": 100,
        "total_garments": 0, "comparisons": 0, "est_seconds": 48, "error_code": None,
        "created_at": None, "started_at": None, "finished_at": None,
    }
    v = jobs.job_view(job, {"avg_seconds": 10.0})
    assert v["stage"] == "ranking"
    assert jobs.job_view({**job, "processed_images": 4})["stage"] == "personalising"
    assert jobs.job_view({**job, "status": "succeeded", "processed_images": 4})["stage"] == "succeeded"
    assert v["progress_pct"] == 25.0
    assert v["eta_seconds"] == 30
    assert v["error"] is None
    done = jobs.job_view({**job, "status": "failed", "error_code": "all_failed"})
    assert done["eta_seconds"] == 0
    assert "images" in done["error"]
    assert jobs.svc_public_error("weird_internal") == "The job did not complete."


# ---------------------------------------------------------------------------
# Extraction worker result → SKU attributes
# ---------------------------------------------------------------------------


def _extract_response(result, mapped):
    return {"status": "COMPLETED", "result": result, "item_attributes": mapped}


def test_extracted_attributes_use_worker_item_attributes():
    raw = {
        "commodity_type": "t_shirt", "material_type": "cotton", "primary_color": "navy",
        "attribute_evidence": {
            "commodity_type": {"confidence": 0.9}, "material_type": {"confidence": 0.6},
            "primary_color": {"confidence": 0.8}, "size_normalized": {"confidence": 0.7},
        },
    }
    mapped = {"name": "T-Shirt", "color": "Navy Blue", "brand": "unknown", "pattern": "Solid", "size": "M",
              "material": "Cotton", "sleeve_type": "Long Sleeve", "collar_type": "unknown", "gender": "Male"}
    a = svc.extracted_attributes(_extract_response(raw, mapped), "Zara")
    assert a == {"color": "Navy Blue", "brand": "Zara", "size": "M", "material": "Cotton", "pattern": "Solid",
                 "sleeve_type": "Long Sleeve", "collar_type": None, "name": "T-Shirt", "confidence": 0.6}


def test_extracted_attributes_unknowns_and_read_brand():
    mapped = {k: "unknown" for k in ("name", "color", "pattern", "size", "material", "sleeve_type", "collar_type")}
    a = svc.extracted_attributes(_extract_response({}, {**mapped, "brand": "Levi's"}), None)
    assert all(a[k] is None for k in ("color", "size", "material", "pattern", "name"))
    assert a["brand"] == "Levi's"
    old_worker = svc.extracted_attributes({"result": {"primary_color": "red", "confidence": 0.5}}, "Zara")
    assert old_worker["color"] is None and old_worker["confidence"] == 0.5


def test_extracted_attributes_feed_attr_fit():
    profile = bc.build_segment_profile(
        {"garment_segment_cells": [{"pin_code": "560034", "garments": 4, "colors": [{"value": "Navy Blue", "count": 5}],
                                    "commodities": [{"value": "T-Shirt", "count": 3}],
                                    "materials": [{"value": "Cotton", "count": 2}]}]},
        pin_codes=["560034"], age_group=None, gender=None, rfm_segment=None)
    mapped = {"name": "T-Shirt", "color": "Navy Blue", "material": "Cotton"}
    a = svc.extracted_attributes(_extract_response({}, mapped), "Zara")
    assert bc.attr_fit_score(a, profile) > 0.9
    # SKU rows stored before item_attributes keep raw values; they still match.
    assert bc.attr_fit_score({"color": "navy blue", "name": "t_shirt", "material": "cotton"}, profile) > 0.9


# ---------------------------------------------------------------------------
# Budget + channels
# ---------------------------------------------------------------------------


def test_normalize_channels():
    assert bc.normalize_channels(["WhatsApp", "inapp", "sms", "sms", "email", None]) == ["whatsapp", "in_app", "sms"]
    assert bc.normalize_channels(None) == []


def test_recommend_budget_applies_floor_for_small_audience():
    rec = bc.recommend_budget(50, 14, "conversion", ["in_app"])
    assert rec["recommended"] >= max(bc.MIN_BUDGET_INR, bc.MIN_DAILY_INR * 14)
    assert rec["min"] <= rec["recommended"] <= rec["max"]
    assert rec["source"] == "synthetic"
    assert any("minimum" in r for r in rec["reasons"])


def test_recommend_budget_breakdown_sums_to_recommended():
    rec = bc.recommend_budget(500_000, 14, "awareness", ["in_app", "whatsapp", "sms"])
    assert [b["channel"] for b in rec["breakdown"]] == ["in_app", "whatsapp", "sms"]
    assert sum(b["budget"] for b in rec["breakdown"]) == pytest.approx(rec["recommended"], rel=0.01)
    assert sum(b["share_pct"] for b in rec["breakdown"]) == pytest.approx(100, abs=0.5)
    assert rec["daily_budget"] == pytest.approx(rec["recommended"] / 14, abs=0.01)


def test_recommend_budget_messaging_only_capped_at_saturation():
    audience = 200
    rec = bc.recommend_budget(audience, 30, "conversion", ["sms"])
    saturation = audience * bc._CHANNEL_FREQ["sms"]["conversion"] * bc._MESSAGE_COST["sms"]
    assert rec["recommended"] < bc.MIN_DAILY_INR * 30
    assert rec["max"] <= max(rec["recommended"], bc._round_to(saturation))
    assert any("unspent" in r for r in rec["reasons"])


def test_estimate_by_channel_and_spend_accounting():
    est = bc.estimate_outcomes(20_000, 14, 10_000, "conversion", ["in_app", "whatsapp"])
    assert set(est["by_channel"]) == {"in_app", "whatsapp"}
    assert sum(r["budget"] for r in est["by_channel"].values()) == pytest.approx(20_000, abs=1)
    for name in ("low", "expected", "high"):
        p = est[name]
        assert p["spend"] + p["unspent"] == pytest.approx(20_000, abs=1)
        assert p["reach"] <= 10_000
    assert est["low"]["conversions"] <= est["high"]["conversions"]
    wa = est["by_channel"]["whatsapp"]["expected"]
    assert wa["messages"] <= 10_000 * bc._CHANNEL_FREQ["whatsapp"]["conversion"]


def test_estimate_defaults_to_in_app():
    est = bc.estimate_outcomes(10_000, 7, 1_000, "awareness")
    assert est["channels"] == ["in_app"]


def test_validate_message_template():
    assert bc.validate_message_template(["in_app"], None) == {}
    assert bc.validate_message_template(["sms"], {"sms": " Hi {link} ", "whatsapp": "ignored"}) == {"sms": "Hi {link}"}
    with pytest.raises(ValueError):
        bc.validate_message_template(["whatsapp"], {})
    with pytest.raises(ValueError):
        bc.validate_message_template(["sms"], {"sms": "x" * 161})
    with pytest.raises(ValueError):
        bc.validate_message_template(["sms"], {"sms": "bad\x07char"})
    tpl = bc.default_message_template("Zara", "Summer")
    assert len(tpl["sms"]) <= bc.MESSAGE_LIMITS["sms"]
    assert bc.validate_message_template(["whatsapp", "sms"], tpl) == tpl


# ---------------------------------------------------------------------------
# Worker auth
# ---------------------------------------------------------------------------


@pytest.fixture
def task_env(monkeypatch):
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT", "tasks@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMMAND_CENTER_URL", "https://cc.example.run.app/")


def test_verify_task_request_accepts_matching_sa(task_env):
    seen = {}

    def verifier(token, audience):
        seen.update(token=token, audience=audience)
        return {"email": "Tasks@proj.iam.gserviceaccount.com", "email_verified": True}

    assert jobs.verify_task_request("Bearer abc", verifier=verifier) is True
    assert seen == {"token": "abc", "audience": "https://cc.example.run.app"}


@pytest.mark.parametrize("auth,claims", [
    (None, {"email": "tasks@proj.iam.gserviceaccount.com", "email_verified": True}),
    ("Basic abc", {"email": "tasks@proj.iam.gserviceaccount.com", "email_verified": True}),
    ("Bearer ", {"email": "tasks@proj.iam.gserviceaccount.com", "email_verified": True}),
    ("Bearer abc", {"email": "attacker@evil.com", "email_verified": True}),
    ("Bearer abc", {"email": "tasks@proj.iam.gserviceaccount.com", "email_verified": False}),
])
def test_verify_task_request_rejects(task_env, auth, claims):
    assert jobs.verify_task_request(auth, verifier=lambda t, a: claims) is False


def test_verify_task_request_rejects_bad_token_and_missing_env(task_env, monkeypatch):
    def boom(t, a):
        raise ValueError("bad signature")

    assert jobs.verify_task_request("Bearer abc", verifier=boom) is False
    monkeypatch.delenv("TASKS_SERVICE_ACCOUNT")
    ok = {"email": "tasks@proj.iam.gserviceaccount.com", "email_verified": True}
    assert jobs.verify_task_request("Bearer abc", verifier=lambda t, a: ok) is False


def test_internal_worker_route_requires_oidc(monkeypatch):
    from fastapi.testclient import TestClient

    import main

    called = []
    monkeypatch.setattr(jobs, "run_chunk", lambda *a, **k: called.append(a) or {"ok": True})
    client = TestClient(main.app)
    r = client.post(f"/internal/campaign-jobs/{JOB}/run", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 403
    assert called == []


# ---------------------------------------------------------------------------
# Chunked worker
# ---------------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch):
    skus = {f"sku-{i}": "pending" for i in range(7)}
    state = {"lease": True, "finished": None, "released": 0, "enqueued": 0, "status": [], "processed": [],
             "personalised": [], "cancel_after": None}
    fail = set()
    monkeypatch.setenv("CAMPAIGN_JOB_CHUNK", "3")
    monkeypatch.setattr(jobs, "_lease", lambda jid: {"job_id": jid, "campaign_id": CID} if state["lease"] else None)
    monkeypatch.setattr(bc, "get_campaign", lambda cid: {"campaign_id": cid, "brand_name": "Zara", "pin_codes": ["560008"]})
    monkeypatch.setattr(
        bc, "list_pending_skus",
        lambda cid, limit: [{"sku_uuid": s, "gcs_object_key": f"k/{s}"} for s, st in skus.items() if st == "pending"][:limit],
    )
    monkeypatch.setattr(svc, "pipeline_context", lambda **kw: object())

    def process(pctx, *, sku_uuid, object_key, source="upload", attributes=None):
        state["processed"].append(sku_uuid)
        if sku_uuid in fail:
            raise RuntimeError("model error")
        skus[sku_uuid] = "done"

    monkeypatch.setattr(svc, "process_sku", process)
    monkeypatch.setattr(bc, "update_sku_rank", lambda sku, **kw: skus.__setitem__(sku, kw["process_status"]))

    def progress(cid):
        vals = list(skus.values())
        return {"pending": vals.count("pending"), "done": vals.count("done"),
                "failed": vals.count("failed"), "avg_seconds": 1.0}

    monkeypatch.setattr(bc, "sku_progress", progress)
    monkeypatch.setattr(bc, "set_campaign_status", lambda cid, st, msg=None: state["status"].append(st))
    monkeypatch.setattr(jobs, "_release", lambda jid, p=None: state.__setitem__("released", state["released"] + 1))

    def cancelled():
        return state["cancel_after"] is not None and len(state["processed"]) >= state["cancel_after"]

    monkeypatch.setattr(jobs, "_is_active", lambda jid: not cancelled())

    def finish(jid, st, **kw):
        if cancelled():
            return False
        state["finished"] = (st, kw.get("error_code"))
        if kw.get("campaign_status"):
            state["status"].append(kw["campaign_status"])
        return True

    monkeypatch.setattr(jobs, "_finish", finish)
    monkeypatch.setattr(jobs, "_enqueue_override", lambda jid: state.__setitem__("enqueued", state["enqueued"] + 1))
    monkeypatch.setattr(jobs, "_mark_personalising", lambda jid, p: None)

    def personalise(cid):
        state["personalised"].append((cid, list(state["status"])))
        return {"recommendations_computed": 1}

    monkeypatch.setattr(style_asst, "personalise_campaign", personalise)
    return skus, state, fail


def test_run_chunk_processes_in_chunks_then_succeeds(worker):
    skus, state, _ = worker
    r1 = jobs.run_chunk(JOB)
    assert r1["continue"] is True and len(state["processed"]) == 3
    assert state["enqueued"] == 1 and state["released"] == 1
    jobs.run_chunk(JOB)
    r3 = jobs.run_chunk(JOB)
    assert r3["status"] == "succeeded"
    assert state["finished"] == ("succeeded", None)
    assert state["status"] == ["ranked"]
    assert sorted(state["processed"]) == sorted(skus)
    assert state["enqueued"] == 2
    assert state["personalised"] == [(CID, [])]


def test_personalise_failure_still_ranks(worker, monkeypatch):
    _, state, _ = worker

    def boom(cid):
        raise RuntimeError("wardrobe db down")

    monkeypatch.setattr(style_asst, "personalise_campaign", boom)
    for _ in range(3):
        jobs.run_chunk(JOB)
    assert state["status"] == ["ranked"] and state["finished"] == ("succeeded", None)


def test_run_chunk_is_idempotent_after_completion(worker):
    skus, state, _ = worker
    for _ in range(3):
        jobs.run_chunk(JOB)
    count = len(state["processed"])
    jobs.run_chunk(JOB)
    assert len(state["processed"]) == count


def test_run_chunk_skips_when_lease_held(worker):
    _, state, _ = worker
    state["lease"] = False
    assert jobs.run_chunk(JOB) == {"ok": True, "skipped": True}
    assert state["processed"] == []


def test_run_chunk_marks_failed_skus_and_continues(worker):
    skus, state, fail = worker
    fail.add("sku-0")
    for _ in range(3):
        jobs.run_chunk(JOB)
    assert skus["sku-0"] == "failed"
    assert state["finished"] == ("succeeded", None)


def test_run_chunk_all_failed(worker):
    skus, state, fail = worker
    fail.update(skus)
    for _ in range(3):
        jobs.run_chunk(JOB)
    assert state["finished"] == ("failed", "all_failed")
    assert state["status"] == ["error"]


def test_local_runner_drains_without_enqueue(worker):
    skus, state, _ = worker
    jobs._run_locally(JOB)
    assert all(v == "done" for v in skus.values())
    assert state["enqueued"] == 0


def test_cancel_stops_worker_before_next_image(worker):
    skus, state, _ = worker
    state["cancel_after"] = 2
    r = jobs.run_chunk(JOB)
    assert r == {"ok": True, "status": "cancelled"}
    assert len(state["processed"]) == 2
    assert state["enqueued"] == 0 and state["finished"] is None and state["status"] == []


def test_local_runner_exits_on_cancel(worker):
    skus, state, _ = worker
    state["cancel_after"] = 4
    jobs._run_locally(JOB)
    assert len(state["processed"]) == 4
    assert state["finished"] is None and state["status"] == [] and state["personalised"] == []


def test_cancel_during_personalise_does_not_mark_ranked(worker, monkeypatch):
    skus, state, _ = worker

    def personalise(cid):
        state["cancel_after"] = 0  # brand cancels while picks are being saved
        return {}

    monkeypatch.setattr(style_asst, "personalise_campaign", personalise)
    for _ in range(3):
        r = jobs.run_chunk(JOB)
    assert r["status"] == "cancelled"
    assert state["finished"] is None and state["status"] == []


# ---------------------------------------------------------------------------
# Cancel API
# ---------------------------------------------------------------------------


def _job_row(**over):
    row = {
        "job_id": JOB, "campaign_id": CID, "brand_name": "Zara", "status": "running",
        "total_images": 4, "processed_images": 1, "failed_images": 0, "total_customers": 100,
        "total_garments": 0, "comparisons": 0, "est_seconds": 48, "error_code": None,
        "created_at": None, "started_at": None, "finished_at": None,
    }
    row.update(over)
    return row


def test_job_view_reports_cancelled_without_error():
    v = jobs.job_view(_job_row(status="failed", error_code=jobs.CANCELLED, processed_images=2))
    assert v["status"] == "cancelled" and v["stage"] == "cancelled"
    assert v["error"] is None and v["eta_seconds"] == 0 and v["progress_pct"] == 50.0


@pytest.fixture
def cancel_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    rows = {JOB: _job_row()}
    calls = []
    monkeypatch.setattr(jobs, "fetch_job", lambda jid: dict(rows[jid]) if jid in rows else None)

    def cancel(jid, cid):
        calls.append((jid, cid))
        if rows[jid]["status"] not in ("queued", "running"):
            return None
        rows[jid].update(status="failed", error_code=jobs.CANCELLED)
        return dict(rows[jid])

    monkeypatch.setattr(jobs, "_cancel", cancel)
    return rows, calls


def test_cancel_job_stops_active_job(cancel_env):
    rows, calls = cancel_env
    out = jobs.cancel_job({"token": "t", "role": "brand", "brand_name": "Zara"}, JOB)
    assert out["status"] == "cancelled" and out["job_id"] == JOB
    assert calls == [(JOB, CID)]


def test_cancel_job_rejects_finished_and_foreign(cancel_env):
    rows, calls = cancel_env
    rows[JOB]["status"] = "succeeded"
    with pytest.raises(svc.ServiceError) as exc:
        jobs.cancel_job({"token": "t", "role": "admin"}, JOB)
    assert exc.value.status == 409 and calls == []
    rows[JOB]["status"] = "running"
    with pytest.raises(svc.ServiceError) as exc:
        jobs.cancel_job({"token": "t", "role": "brand", "brand_name": "Nike"}, JOB)
    assert exc.value.status == 404 and calls == []
    with pytest.raises(svc.ServiceError) as exc:
        jobs.cancel_job({"token": "t", "role": "customer", "customer_id": "c1"}, JOB)
    assert exc.value.status in (401, 403) and calls == []


def test_cancel_job_race_with_completion_is_409(cancel_env, monkeypatch):
    rows, calls = cancel_env
    monkeypatch.setattr(jobs, "_cancel", lambda jid, cid: None)
    with pytest.raises(svc.ServiceError) as exc:
        jobs.cancel_job({"token": "t", "role": "brand", "brand_name": "Zara"}, JOB)
    assert exc.value.status == 409


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_job_of_other_brand_is_not_found(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    job = {"job_id": JOB, "campaign_id": CID, "brand_name": "Nike", "status": "running"}
    monkeypatch.setattr(jobs, "fetch_job", lambda jid: dict(job) if jid == JOB else None)
    zara = {"token": "t", "role": "brand", "brand_name": "Zara"}
    for jid in (JOB, "not-a-uuid", "55555555-5555-4555-8555-555555555555"):
        with pytest.raises(svc.ServiceError) as exc:
            jobs._load_job_for(zara, jid)
        assert exc.value.status == 404
    nike = {"token": "t", "role": "brand", "brand_name": "Nike"}
    assert jobs._load_job_for(nike, JOB)["brand_name"] == "Nike"
    assert jobs._load_job_for({"token": "t", "role": "admin"}, JOB)["job_id"] == JOB
