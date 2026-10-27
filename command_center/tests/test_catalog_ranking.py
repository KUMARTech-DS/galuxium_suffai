"""Catalog photos, style×colour listing, size-independent ranking, catalog job guards."""

from __future__ import annotations

import pytest

import brand_campaign as bc
import campaign_jobs
import campaign_service as svc
import retail_catalog as catalog
import retail_db as db
from retail_scope import Scope


@pytest.fixture
def scope(tmp_path):
    db.use_sqlite(str(tmp_path / "cat.db"))
    yield Scope("brand", "zara_hq", "zara", "Zara")
    db.reset()


def _seed(scope: Scope):
    p = catalog.create_product(scope, {
        "style_code": "TS100", "name": "Relaxed cotton tee", "category": "T-Shirt",
        "hsn_code": "6109", "material": "Cotton", "pattern": "Solid",
        "colors": ["Navy", "White"], "sizes": ["S", "M"], "mrp": "1999",
    })
    with db.tx() as t:
        t.run(
            "INSERT INTO retail_product_images (product_id, brand_key, color_key, color, gcs_bucket, "
            "gcs_object_key, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (p["product_id"], "zara", "navy", "Navy", "b", "catalog/zara/p/navy.jpg", db.now_iso()),
        )
    return p


def test_list_style_colours_groups_sizes(scope):
    _seed(scope)
    data = catalog.list_style_colours(scope)
    assert data["has_catalog"]
    navy = next(i for i in data["items"] if i["color"] == "Navy")
    white = next(i for i in data["items"] if i["color"] == "White")
    assert set(navy["sizes"]) == {"S", "M"}
    assert navy["has_image"] is True
    assert white["has_image"] is False
    assert navy["style_code"] == "TS100"


def test_list_style_colours_with_image_only(scope):
    _seed(scope)
    data = catalog.list_style_colours(scope, with_image_only=True)
    assert len(data["items"]) == 1 and data["items"][0]["color"] == "Navy"


def test_catalog_prefix_skipped_by_photo_key():
    from worker import photo_key as ext
    from similarity import photo_key as sim
    assert ext.parse_photo_key("catalog/zara/pid/abc.jpg") is None
    assert sim.parse_photo_key("catalog/zara/pid/abc.jpg") is None
    assert not ext.needs_extraction({"gcs_object_key": "catalog/zara/pid/abc.jpg"})


def test_ranking_attr_fit_ignores_size():
    profile = {
        "distributions": {
            "color": {"navyblue": 10.0},
            "commodity": {"tshirt": 10.0},
            "material": {"cotton": 10.0},
            "pattern": {"solid": 10.0},
            "size": {"m": 10.0, "s": 1.0},
            "brand": {"zara": 10.0},
        }
    }
    base = {"color": "Navy Blue", "name": "T-Shirt", "material": "Cotton", "pattern": "Solid",
            "brand": "Zara", "size": "S"}
    cleared = {**base, "size": None}
    assert bc.attr_fit_score(cleared, profile) == bc.attr_fit_score(
        {k: v for k, v in base.items() if k != "size"}, profile
    )
    # Style Assistant still uses size when present (different weight denominator).
    assert bc.attr_fit_score(base, profile) != bc.attr_fit_score(cleared, profile)


def test_process_sku_skips_extract_for_catalog(monkeypatch):
    called = {"extract": 0, "compare": 0}

    def fake_extract(*a, **k):
        called["extract"] += 1
        return {"attributes": {}, "raw_result": {}, "extract_ok": False, "extract_error": "no"}

    def fake_compare(pctx, sku_uuid, object_key, attrs):
        called["compare"] += 1
        assert attrs["color"] == "Navy"
        return {"rank_score": 0.5, "sim_fit": 0.5, "attr_fit": 0.5, "freq_coverage": 0.1,
                "compare_error": None, "per_pin": {}}

    monkeypatch.setattr(svc, "extract_sku", fake_extract)
    monkeypatch.setattr(svc, "compare_sku", fake_compare)
    monkeypatch.setattr(bc, "update_sku_rank", lambda *a, **k: None)
    monkeypatch.setattr(bc, "update_sku_extract", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no extract update")))

    pctx = svc.PipelineContext(
        brand="Zara", pins=["560034"], extraction_url="", similarity_url="", bucket="b",
        bkt=None, profile={}, extract_token=None, sim_token=None, corpus={},
    )
    out = svc.process_sku(
        pctx, sku_uuid="u1", object_key="catalog/zara/x.jpg", source="catalog",
        attributes={"color": "Navy", "name": "T-Shirt", "material": "Cotton"},
    )
    assert out["ok"] and called["extract"] == 0 and called["compare"] == 1


def test_max_catalog_skus_capped():
    assert 1 <= campaign_jobs.max_catalog_skus() <= 15


def test_product_public_includes_images(scope):
    p = _seed(scope)
    got = catalog.get_product(scope, p["product_id"])
    assert len(got["images"]) == 1
    assert got["images"][0]["color"] == "Navy"
    navy_vars = [v for v in got["variants"] if v["color"] == "Navy"]
    assert all(v["has_image"] for v in navy_vars)
