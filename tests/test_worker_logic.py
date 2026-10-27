"""Unit tests for worker helpers that do not call GCP."""

from worker.result_utils import (
    decide_review_status,
    min_confidence,
    normalize_result,
    outfit_tags,
    parse_json_response,
)


def test_outfit_tags_coerced_to_vocab():
    tags = outfit_tags({
        "garment_category": "Topwear",
        "garment_subcategory": "T-Shirt",
        "sleeve_length": "Full",
        "pairs_with": ["Overshirt", "jeans", "sneaker", "tshirt", "shirt", "spaceship", "jeans"],
    })
    assert tags == {
        "garment_category": "top",
        "garment_subcategory": "tshirt",
        "sleeve_length": "full",
        "pairs_with": ["overshirt", "jeans", "sneakers"],
    }


def test_shirt_never_pairs_with_shirt_or_other_tops():
    tags = outfit_tags({"garment_subcategory": "shirt", "pairs_with": ["shirt", "polo", "jacket", "trousers"]})
    assert tags["garment_category"] == "top"
    assert tags["pairs_with"] == ["jacket", "trousers"]


def test_unknown_tags_fall_back_safely():
    tags = outfit_tags({"commodity_type": "jeans", "sleeve_length": "long-ish", "pairs_with": "jacket"})
    assert tags == {"garment_category": "bottom", "garment_subcategory": "jeans",
                    "sleeve_length": "unknown", "pairs_with": []}
    empty = outfit_tags({})
    assert empty["garment_category"] == "unknown" and empty["garment_subcategory"] is None
    result = normalize_result({"garment_subcategory": "hoodie"}, model_version="m")
    assert result["garment_category"] == "outer" and result["pairs_with"] == []


def test_parse_json_embedded():
    raw = parse_json_response('prefix {"commodity_type":"shirt","brand_name":"unknown"} suffix')
    assert raw["commodity_type"] == "shirt"


def test_normalize_and_review_status_needs_review():
    result = normalize_result(
        {
            "commodity_type": "shirt",
            "material_type": "unknown",
            "primary_color": "navy",
            "brand_name": "unknown",
            "attribute_evidence": {
                "commodity_type": {"source": "visual_inference", "confidence": 0.7},
                "material_type": {"source": "care_label_ocr", "confidence": 0.2},
                "primary_color": {"source": "visual_inference", "confidence": 0.8},
                "brand_name": {"source": "unknown", "confidence": 0.0},
            },
        },
        model_version="gemini-test",
    )
    assert min_confidence(result) < 0.9
    assert decide_review_status(result) == "NEEDS_REVIEW"


def test_normalize_high_confidence_completed():
    result = normalize_result(
        {
            "commodity_type": "shirt",
            "material_type": "cotton",
            "primary_color": "white",
            "brand_name": "unknown",
            "size_normalized": "M",
            "attribute_evidence": {
                "commodity_type": {"confidence": 0.95},
                "material_type": {"confidence": 0.96},
                "primary_color": {"confidence": 0.94},
                "brand_name": {"confidence": 0.1},
                "size_normalized": {"confidence": 0.93},
            },
        },
        model_version="gemini-test",
    )
    assert decide_review_status(result) == "COMPLETED"
