"""Canonical values on read: brand masking by key, no 'unknown' brand filter."""

import aggregate_metrics as agg
import auth_acl
import vocab


def test_mask_matches_own_brand_by_key():
    payload = {
        "filters": {"brands": ["Levis", "Zara", "unknown"]},
        "garment_cells": [{"brand": "Levis", "garments": 2}, {"brand": "LEVI'S", "garments": 1},
                          {"brand": "Zara", "garments": 4}],
        "garment_segment_cells": [{"brands": [{"value": "Levis", "count": 3}, {"value": "Zara", "count": 4}]}],
    }
    out = auth_acl.mask_metrics_for_brand(payload, "Levi's")
    assert [g["brand"] for g in out["garment_cells"]] == ["Levi's", "Levi's", "Competitors"]
    assert out["garment_segment_cells"][0]["brands"] == [{"value": "Competitors", "count": 4.0},
                                                          {"value": "Levi's", "count": 3.0}]
    assert out["filters"]["brands"] == ["Levi's", "Competitors"]


def test_unknown_brand_is_not_a_filter_value():
    customers = [{"_id": "c1", "pin_code": "560034", "age": 30, "gender": "Female", "rfm_segment": "Loyal"}]
    items = [{"customer_id": "c1", "brand": "unknown"}, {"customer_id": "c1", "brand": "N/A"},
             {"customer_id": "c1", "brand": "Zara"}]
    payload = agg.build(customers, [], items, {})
    assert payload["filters"]["brands"] == ["Zara"]
    assert {g["brand"] for g in payload["garment_cells"]} == {"unknown", "N/A", "Zara"}  # counts stay complete


def test_canonical_uses_ref_values(monkeypatch):
    monkeypatch.setattr(vocab, "_loader", lambda: [{"kind": "brand", "canonical": "H&M", "alias_key": "hm"}])
    vocab.clear_cache()
    try:
        assert vocab.canonical("brand", "H & M") == "H&M"
        assert vocab.canonical("customer_gender", "F") == "Female"
        assert vocab.canonical("brand", "") == "unknown"
        assert vocab.canonical_values("brand") == ["H&M"]
    finally:
        vocab.clear_cache()


def test_attr_key_matches_raw_and_canonical_spellings(monkeypatch):
    monkeypatch.setattr(vocab, "_loader", lambda: [
        {"kind": "color", "canonical": "Navy Blue", "alias_key": "navy"},
        {"kind": "color", "canonical": "Navy Blue", "alias_key": "navyblue"},
        {"kind": "garment_name", "canonical": "T-Shirt", "alias_key": "tee"},
    ])
    vocab.clear_cache()
    try:
        assert vocab.attr_key("color", "navy") == vocab.attr_key("color", "Navy Blue") == "navyblue"
        assert vocab.attr_key("name", "t_shirt") == vocab.attr_key("commodity", "T-Shirt") == "tshirt"
        assert vocab.attr_key("commodity", "tee") == "tshirt"
        assert vocab.attr_key("sleeve_type", "long_sleeve") == vocab.attr_key("sleeve_type", "Long Sleeve")
        assert vocab.attr_key("color", "unknown") == "" and vocab.attr_key("pattern", None) == ""
    finally:
        vocab.clear_cache()


def test_sku_attr_fit_and_style_overlap_use_shared_values(monkeypatch):
    import brand_campaign as bc
    import style_assistant as sa

    monkeypatch.setattr(vocab, "_loader", lambda: [{"kind": "color", "canonical": "Navy Blue", "alias_key": "navy"}])
    vocab.clear_cache()
    try:
        metrics = {"garment_segment_cells": [{"pin_code": "560034", "garments": 3,
                                              "colors": [{"value": "Navy Blue", "count": 3}],
                                              "patterns": [{"value": "Solid", "count": 3}]}]}
        profile = bc.build_segment_profile(metrics, pin_codes=["560034"], age_group=None, gender=None, rfm_segment=None)
        assert profile["distributions"]["color"] == {"navyblue": 3.0}
        raw_sku = {"color": "navy", "pattern": "solid"}
        assert bc.attr_fit_score(raw_sku, profile) == 1.0
        wardrobe = [{"attributes": {"color": "Navy Blue", "pattern": "Solid", "material": "Cotton"}}]
        taste = sa.build_wardrobe_taste_profile(wardrobe)
        assert bc.attr_fit_score(raw_sku, taste) == 1.0
        assert sa._attr_overlap({"attributes": {**raw_sku, "material": "cotton"}}, wardrobe[0]) == 3
    finally:
        vocab.clear_cache()
