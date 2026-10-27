"""Warehouse catalog matching for customer garment photos (worker/catalog_match.py)."""

from __future__ import annotations

import pytest

from worker import catalog_match, vocab


@pytest.fixture(autouse=True)
def _clear():
    catalog_match.clear_cache()
    yield
    catalog_match.clear_cache()


ROWS = [
    {
        "variant_id": "v1", "sku": "TS100-NAVY-M", "barcode": "2000000000053",
        "color": "Navy Blue", "size": "M",
        "product_id": "p1", "style_code": "TS100", "name": "Relaxed cotton tee",
        "category": "T-Shirt", "material": "Cotton", "pattern": "Solid", "gender": "Unisex",
    },
    {
        "variant_id": "v2", "sku": "TS100-NAVY-L", "barcode": "2000000000060",
        "color": "Navy Blue", "size": "L",
        "product_id": "p1", "style_code": "TS100", "name": "Relaxed cotton tee",
        "category": "T-Shirt", "material": "Cotton", "pattern": "Solid", "gender": "Unisex",
    },
    {
        "variant_id": "v3", "sku": "TS100-WHITE-M", "barcode": "2000000000022",
        "color": "White", "size": "M",
        "product_id": "p1", "style_code": "TS100", "name": "Relaxed cotton tee",
        "category": "T-Shirt", "material": "Cotton", "pattern": "Solid", "gender": "Unisex",
    },
    {
        "variant_id": "v4", "sku": "JK200-CAMEL-M", "barcode": "2000000000100",
        "color": "Camel", "size": "M",
        "product_id": "p2", "style_code": "JK200", "name": "Camel jacket",
        "category": "Jacket", "material": "Cotton", "pattern": "Solid", "gender": "Men",
    },
]


def _attrs(**kw):
    base = {
        "name": "T-Shirt", "color": "Navy Blue", "brand": "Zara",
        "pattern": "Solid", "size": "M", "material": "Cotton",
        "sleeve_type": "Short Sleeve", "collar_type": "Crew Neck", "gender": "Unisex",
    }
    base.update(kw)
    return base


def test_no_database_url_skips(monkeypatch):
    monkeypatch.setattr(catalog_match.settings, "DATABASE_URL", "")
    assert catalog_match.match({"brand_sku": "TS100-NAVY-M"}, _attrs()) is None


def test_brand_without_catalog_skips(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: [])
    assert catalog_match.match({"brand_sku": "TS100-NAVY-M"}, _attrs()) is None


def test_exact_sku_overwrites_size(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    ov = catalog_match.match({"brand_sku": "TS100-NAVY-M", "brand_name": "Zara"}, _attrs(size="XL"))
    assert ov and ov["catalog_match"] == "sku"
    assert ov["size"] == "M" and ov["brand_style_code"] == "TS100"
    assert ov["force_extracted"] and ov["confidence"] == 1.0
    row = {"name": "x", "color": "x", "brand": "x", "size": "XL", "reason": "from photo",
           "material": "x", "pattern": "x", "gender": "x", "image_status": "needs_review"}
    catalog_match.apply_to_row(row, ov)
    assert row["size"] == "M" and row["image_status"] == "extracted"
    assert row["catalog_match"] == "sku" and "Matched brand catalog" in row["reason"]


def test_exact_barcode(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    ov = catalog_match.match({"tag_codes": ["2000000000053"]}, _attrs())
    assert ov and ov["catalog_match"] == "barcode" and ov["brand_sku"] == "TS100-NAVY-M"


def test_style_code_with_colour(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    ov = catalog_match.match({"tag_codes": ["TS100"]}, _attrs(color="White", size="S"))
    assert ov and ov["catalog_match"] == "style_code"
    assert ov["color"] == vocab.canonical("color", "White")
    assert "size" not in ov  # keep photo size


def test_style_code_single_colour_product(monkeypatch):
    only = [r for r in ROWS if r["product_id"] == "p2"]
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: only)
    ov = catalog_match.match({"brand_sku": "JK200"}, _attrs(name="Jacket", color="Beige", brand="Zara"))
    assert ov and ov["catalog_match"] == "style_code"
    assert ov["brand_style_code"] == "JK200"


def test_strict_attribute_single_candidate(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    ov = catalog_match.match({}, _attrs())  # Navy T-Shirt cotton solid → unique style×colour
    assert ov and ov["catalog_match"] == "attributes"
    assert ov["brand_style_code"] == "TS100"
    assert "size" not in ov and not ov.get("force_extracted")


def test_strict_attribute_tie_means_no_match(monkeypatch):
    # Two products same category/colour/material/pattern → ambiguous.
    twin = ROWS + [{
        "variant_id": "v9", "sku": "TS999-NAVY-M", "barcode": "9",
        "color": "Navy Blue", "size": "M",
        "product_id": "p9", "style_code": "TS999", "name": "Other tee",
        "category": "T-Shirt", "material": "Cotton", "pattern": "Solid", "gender": "Unisex",
    }]
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: twin)
    assert catalog_match.match({}, _attrs()) is None


def test_missing_attribute_means_no_match(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    assert catalog_match.match({}, _attrs(material="unknown")) is None


PHOTOS = [
    {"product_id": "p1", "color_key": "navyblue", "color": "Navy Blue", "gcs_bucket": "b",
     "gcs_object_key": "catalog/zara/p1/a.jpg", "embedding": [], "embedding_model": None,
     "embedding_dim": None, "embedded_object_key": None, "style_code": "TS100",
     "name": "Relaxed cotton tee", "category": "T-Shirt", "material": "Cotton", "pattern": "Solid",
     "gender": "Unisex"},
    {"product_id": "p2", "color_key": "camel", "color": "Camel", "gcs_bucket": "b",
     "gcs_object_key": "catalog/zara/p2/b.jpg", "embedding": [0.0, 1.0], "embedding_model": "mm",
     "embedding_dim": 2, "embedded_object_key": "catalog/zara/p2/b.jpg", "style_code": "JK200",
     "name": "Camel jacket", "category": "Jacket", "material": "Cotton", "pattern": "Solid",
     "gender": "Men"},
]
CATALOG_VECS = {"catalog/zara/p1/a.jpg": [1.0, 0.02], "catalog/zara/p2/b.jpg": [0.0, 1.0]}


class _Img:
    def __init__(self, key):
        self.object_key = key


def _photo_result(size_conf=0.95):
    return {
        "brand_name": "Zara", "size_normalized": "XL",
        "attribute_evidence": {
            "commodity_type": {"confidence": 0.5}, "material_type": {"confidence": 0.3},
            "primary_color": {"confidence": 0.6}, "brand_name": {"confidence": 0.4},
            "size_normalized": {"confidence": size_conf},
        },
    }


def _visual(monkeypatch, photos, *, photo_vec=(1.0, 0.0), result=None):
    monkeypatch.setattr(catalog_match.settings, "CATALOG_MATCH_THRESHOLD", 0.92)
    saved = []

    def embedder(img):
        return (list(photo_vec) if img.object_key == "customer.jpg" else CATALOG_VECS[img.object_key]), "mm"

    ov = catalog_match.visual_match(
        result or _photo_result(), _attrs(name="Shirt", color="Black", material="Silk"), _Img("customer.jpg"),
        embedder=embedder, reader=lambda key, bucket=None: _Img(key),
        loader=lambda key: [dict(p) for p in photos], save=lambda row, emb, model: saved.append(row["product_id"]),
    )
    return ov, saved


def test_visual_match_overwrites_style_keeps_photo_size(monkeypatch):
    ov, saved = _visual(monkeypatch, PHOTOS)
    assert saved == ["p1"]  # only the photo without a fingerprint is embedded and stored
    assert ov and ov["catalog_match"] == "visual" and ov["brand_style_code"] == "TS100"
    assert ov["color"] == vocab.canonical("color", "Navy Blue")
    assert ov["material"] == "Cotton" and ov["brand_sku"] is None and "size" not in ov
    assert ov["force_extracted"] and ov["confidence"] == 0.95 and ov["catalog_score"] >= 0.92
    row = {"name": "Shirt", "color": "Black", "brand": "Zara", "size": "XL", "reason": "",
           "material": "Silk", "pattern": "x", "gender": "x", "image_status": "needs_review"}
    catalog_match.apply_to_row(row, ov)
    assert row["size"] == "XL" and row["material"] == "Cotton" and row["image_status"] == "extracted"


def test_visual_match_weak_size_stays_in_review(monkeypatch):
    ov, _ = _visual(monkeypatch, PHOTOS, result=_photo_result(size_conf=0.3))
    assert ov and not ov["force_extracted"] and ov["confidence"] == 0.3


def test_visual_two_lookalike_styles_do_not_overwrite(monkeypatch):
    twin = PHOTOS + [{**PHOTOS[0], "product_id": "p9", "color_key": "navy", "style_code": "TS999",
                      "gcs_object_key": "catalog/zara/p9/c.jpg"}]
    CATALOG_VECS["catalog/zara/p9/c.jpg"] = [0.99, 0.05]
    ov, _ = _visual(monkeypatch, twin)
    assert ov is None


def test_visual_below_threshold_and_replaced_photo_reembedded(monkeypatch):
    stale = [{**PHOTOS[1], "gcs_object_key": "catalog/zara/p2/new.jpg"}]
    CATALOG_VECS["catalog/zara/p2/new.jpg"] = [0.0, 1.0]
    ov, saved = _visual(monkeypatch, stale)
    assert saved == ["p2"] and ov is None


def test_visual_skipped_without_image_or_brand(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    called = []
    monkeypatch.setattr(catalog_match, "_load_images", lambda key: called.append(key) or PHOTOS)
    assert catalog_match.match({}, _attrs(material="unknown")) is None
    assert catalog_match.visual_match({}, _attrs(brand="unknown"), _Img("x"), embedder=lambda i: ([1.0], "m")) is None
    assert called == []


def test_exact_sku_beats_visual(monkeypatch):
    monkeypatch.setattr(catalog_match, "_load_brand", lambda key: ROWS)
    monkeypatch.setattr(catalog_match, "visual_match", lambda *a, **k: pytest.fail("visual ran"))
    ov = catalog_match.match({"brand_sku": "TS100-NAVY-M"}, _attrs(), _Img("x"))
    assert ov["catalog_match"] == "sku"


def test_ddl_item_columns_and_migration_agree():
    from pathlib import Path
    from worker import bq_items

    root = Path(__file__).resolve().parents[1]
    scripts = root / "command_center" / "scripts"
    ddl = (scripts / "create_bq_tables.sql").read_text(encoding="utf-8")
    mig = (scripts / "migrate_bq_catalog_match.sql").read_text(encoding="utf-8")
    cols = {c for c, _ in bq_items.ITEM_COLUMNS}
    for name in ("brand_sku", "brand_style_code", "catalog_match"):
        assert name in cols
        assert f"{name} STRING" in ddl
        assert f"ADD COLUMN IF NOT EXISTS {name} STRING" in mig
    assert (scripts / "migrate_bq_catalog_match.sh").is_file()
    assert (root / "db" / "migrations" / "013_catalog_images.sql").is_file()
