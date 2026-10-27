"""campaign_service: role rules, input validation, state conflicts and storage failures (no real DB)."""

import pytest

import brand_campaign as bc
import campaign_service as svc

CID = "22222222-2222-4222-8222-222222222222"
ADMIN = {"role": "admin"}
ZARA = {"role": "brand", "brand_name": "Zara"}
IMG = svc.ImageInput("a.jpg", "image/jpeg", b"x")


def _code(fn, *a, **k):
    with pytest.raises(svc.ServiceError) as err:
        fn(*a, **k)
    return err.value.code, err.value.status


@pytest.fixture
def db(monkeypatch):
    """A fake campaign row reachable through load_campaign; writes are recorded."""
    row = {"campaign_id": CID, "brand_name": "Zara", "status": "ranked"}
    writes = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(bc, "get_campaign", lambda cid: dict(row) if cid == CID else None)
    monkeypatch.setattr(bc, "set_campaign_status", lambda cid, status, *a: writes.append(status))
    return row, writes


@pytest.mark.parametrize(
    "sess,expected",
    [(None, ("auth", 401)), ({"role": "customer"}, ("not_allowed", 403)), ({"role": "brand"}, ("not_allowed", 403))],
)
def test_require_campaign_role_rejects(sess, expected):
    assert _code(svc.require_campaign_role, sess) == expected


@pytest.mark.parametrize("sess,role", [(ADMIN, "admin"), (ZARA, "brand")])
def test_require_campaign_role_accepts(sess, role):
    assert svc.require_campaign_role(sess) == role


def test_check_access_is_case_insensitive_for_own_brand():
    svc.check_access({"brand_name": " zara "}, ZARA)


def test_check_access_rejects_other_brand():
    assert _code(svc.check_access, {"brand_name": "Nike"}, ZARA) == ("not_allowed", 403)


def test_admin_can_access_any_brand():
    svc.check_access({"brand_name": "Nike"}, ADMIN)


def test_resolve_brand_locks_brand_session_to_own_brand():
    assert svc.resolve_brand(ZARA, "Nike") == "Zara"


def test_resolve_brand_requires_name_for_admin():
    assert _code(svc.resolve_brand, ADMIN, "  ") == ("invalid_input", 400)


def test_validate_submission_requires_pins():
    assert _code(svc.validate_submission, ZARA, [IMG], brand_name=None, pin_codes=[" "],
                 age_group=None, gender=None, rfm_segment=None) == ("invalid_input", 400)


def test_validate_submission_requires_an_image():
    other = svc.ImageInput("a.pdf", "application/pdf", b"x")
    assert _code(svc.validate_submission, ZARA, [other], brand_name=None, pin_codes=["560008"],
                 age_group=None, gender=None, rfm_segment=None) == ("invalid_input", 400)


def test_validate_submission_rejects_oversized_image():
    big = svc.ImageInput("big.jpg", "image/jpeg", b"x" * 11)
    assert _code(svc.validate_submission, ZARA, [big], brand_name=None, pin_codes=["560008"],
                 age_group=None, gender=None, rfm_segment=None, max_image_bytes=10) == ("invalid_input", 400)


def test_validate_submission_normalises_fields():
    sub = svc.validate_submission(ZARA, [IMG], brand_name=None, pin_codes=[" 560008 ", ""] + ["1"] * 60,
                                  age_group=" ", gender="F", rfm_segment=None)
    assert sub.brand == "Zara" and sub.pins[0] == "560008" and len(sub.pins) == 50
    assert (sub.age, sub.gender, sub.rfm) == (None, "F", None)


def test_load_campaign_without_db_is_503(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert _code(svc.load_campaign, ADMIN, CID) == ("unavailable", 503)


@pytest.mark.parametrize("cid", ["not-a-uuid", "", None])
def test_load_campaign_bad_id_is_404(db, cid):
    assert _code(svc.load_campaign, ADMIN, cid) == ("not_found", 404)


def test_load_campaign_unknown_id_is_404(db):
    assert _code(svc.load_campaign, ADMIN, "33333333-3333-4333-8333-333333333333") == ("not_found", 404)


def test_load_campaign_db_error_is_503(db, monkeypatch):
    def boom(_cid):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bc, "get_campaign", boom)
    assert _code(svc.load_campaign, ADMIN, CID) == ("unavailable", 503)


def test_load_campaign_other_brand_is_403(db):
    db[0]["brand_name"] = "Nike"
    assert _code(svc.load_campaign, ZARA, CID) == ("not_allowed", 403)


@pytest.mark.parametrize("status", ["processing", "started", "launched"])
def test_discard_conflicting_status_is_409(db, status):
    db[0]["status"] = status
    assert _code(svc.discard, ZARA, CID) == ("invalid_input", 409)
    assert db[1] == []


def test_discard_ranked_campaign_marks_ignored(db):
    svc.discard(ZARA, CID)
    assert db[1] == ["ignored"]


def test_discard_storage_failure_is_503(db, monkeypatch):
    def boom(*_a):
        raise RuntimeError("db")

    monkeypatch.setattr(bc, "set_campaign_status", boom)
    assert _code(svc.discard, ZARA, CID) == ("unavailable", 503)


def test_launch_requires_configured_status_409(db):
    assert _code(svc.launch, ZARA, CID) == ("invalid_input", 409)


def test_launch_is_idempotent_when_already_launched(db):
    db[0].update(status="launched", impact={"reach": 1})
    out = svc.launch(ZARA, CID)
    assert out["already_launched"] is True and out["impact"] == {"reach": 1}


def test_configure_requires_ranked_status_409(db):
    db[0]["status"] = "draft"
    assert _code(svc.configure, ZARA, CID, campaign_name="A", objective="awareness", budget_inr=1, duration_days=7,
                 start_date=None, selected_sku_ids=[], creative=None) == ("invalid_input", 409)


@pytest.mark.parametrize("creative", [None, {}, {"headline": "   "}])
def test_configure_requires_headline(db, creative):
    db[0]["est_audience"] = 1000
    with pytest.raises(svc.ServiceError) as err:
        svc.configure(ZARA, CID, campaign_name="A", objective="awareness", budget_inr=1000, duration_days=7,
                      start_date=None, selected_sku_ids=[], creative=creative)
    assert (err.value.code, err.value.status, err.value.public) == ("invalid_input", 400, "headline is required")


def test_detail_signs_image_urls_only_when_asked(db, monkeypatch):
    import style_assistant as sa

    monkeypatch.setattr(bc, "list_campaign_skus", lambda cid: [
        {"sku_uuid": "a", "extract_ok": True, "rank_score": 80, "gcs_bucket": "bkt", "gcs_object_key": "k1"},
        {"sku_uuid": "b", "extract_ok": True, "rank_score": 90, "gcs_bucket": "bkt", "gcs_object_key": "k2"},
        {"sku_uuid": "c", "extract_ok": False, "rank_score": None, "gcs_bucket": None, "gcs_object_key": None},
    ])
    monkeypatch.setattr(bc, "sku_pin_fit", lambda cid: {})
    monkeypatch.setattr(sa, "signed_image_url", lambda b, k, **kw: f"https://signed/{k}" if b and k else None)
    assert all("image_url" not in s for s in svc.get_with_skus(ZARA, CID)["skus"])
    skus = svc.get_with_skus(ZARA, CID, with_images=True)["skus"]
    assert [s["image_url"] for s in skus] == ["https://signed/k1", "https://signed/k2", None]
    assert [s["rank"] for s in skus] == [2, 1, 3]


def test_list_for_brand_is_scoped_to_own_brand(db, monkeypatch):
    seen = {}
    monkeypatch.setattr(bc, "list_campaigns", lambda **k: seen.update(k) or [])
    svc.list_for(ZARA, brand_name="Nike")
    assert seen["brand_name"] == "Zara"


def test_require_pipeline_env_names_missing_setting(monkeypatch):
    for k in ("EXTRACTION_URL", "SIMILARITY_URL", "GCS_IMAGE_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert _code(svc.require_pipeline_env) == ("unavailable", 503)


def test_load_metrics_has_no_file_fallback(monkeypatch):
    def broken():
        raise RuntimeError("bq down")

    monkeypatch.setattr(svc, "_metrics_loader", broken)
    monkeypatch.setattr(svc, "_metrics_cache", {"at": 0.0, "data": None})
    assert _code(svc.load_metrics, fresh=True) == ("unavailable", 503)
    assert not hasattr(svc, "_metrics_json")


def test_load_metrics_without_any_source_is_503(monkeypatch):
    monkeypatch.setattr(svc, "_metrics_loader", None)
    monkeypatch.setattr(svc, "_metrics_cache", {"at": 0.0, "data": None})
    assert _code(svc.load_metrics, fresh=True) == ("unavailable", 503)


def test_known_brands_is_empty_when_metrics_unavailable(monkeypatch):
    def down(**_k):
        raise svc.ServiceError("unavailable", "x", 503)

    monkeypatch.setattr(svc, "load_metrics", down)
    assert svc.known_brands() == []


@pytest.mark.parametrize("values,expected", [(("unknown", " Red "), "Red"), (("", None, "N/A"), None), ((0,), "0")])
def test_known_value_skips_placeholders(values, expected):
    assert svc.known_value(*values) == expected
