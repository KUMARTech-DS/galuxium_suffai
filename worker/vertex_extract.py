"""Vertex AI Gemini garment / campaign attribute extraction."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel, Part

from worker import vocab
from worker.gcs_io import GcsImage
from worker.result_utils import (
    SUBCATEGORY_BUCKET,
    decide_review_status,
    min_confidence,
    normalize_result,
    parse_json_response,
)
from worker.settings import settings

log = logging.getLogger("suffai-extraction.vertex")

EXTRACTION_SCHEMA_HINT = {
    "display_name": "short product name, e.g. Formal Oxford Shirt, or unknown",
    "commodity_type": "shirt|tshirt|trouser|jeans|kurta|saree|dress|jacket|hoodie|home_linen|other|unknown",
    "material_type": "cotton|polyester|silk|wool|linen|denim|blend|unknown",
    "material_blend": {"cotton": 0.0, "polyester": 0.0},
    "primary_color": "normalized color name or unknown",
    "secondary_colors": ["..."],
    "size_raw": "text from label or null",
    "size_normalized": "S|M|L|XL|numeric|unknown|null",
    "size_system": "IN|US|EU|unknown|null",
    "brand_name": "canonical or unknown",
    "brand_sku": "null if not readable",
    "tag_codes": ["any SKU, barcode or style codes readable on the label, else empty list"],
    "pattern": "solid|striped|checked|printed|other|unknown",
    "sleeve_type": "short_sleeve|long_sleeve|three_quarter|sleeveless|not_applicable|unknown",
    "collar_type": "crew_neck|v_neck|polo|button_down|mandarin|hooded|collarless|not_applicable|unknown",
    "garment_department": "men|women|unisex|kids|unknown",
    "style_tags": ["casual", "..."],
    "garment_category": "top|bottom|outer|dress|footwear|accessory|unknown",
    "garment_subcategory": "|".join(SUBCATEGORY_BUCKET) + "|unknown",
    "sleeve_length": "full|half|sleeveless|unknown",
    "pairs_with": ["garment_subcategory values that complete an outfit with this piece"],
    "attribute_evidence": {
        "commodity_type": {"source": "visual_inference|care_label_ocr|brand_label_ocr", "confidence": 0.0},
        "material_type": {"source": "care_label_ocr|visual_inference", "confidence": 0.0},
        "primary_color": {"source": "visual_inference", "confidence": 0.0},
        "size_normalized": {"source": "size_label_ocr|unknown", "confidence": 0.0},
        "brand_name": {"source": "brand_label_ocr|visual_inference|unknown", "confidence": 0.0},
    },
    "notes": "short abstention reasons if any",
}

SYSTEM_PROMPT = """You are a garment attribute extractor for laundry/fashion intelligence.
Return ONLY valid JSON matching the schema. Rules:
- Prefer care-label OCR for material composition; if unreadable use "unknown".
- Prefer brand/size label OCR for brand and size; do not invent SKUs.
- Put every readable SKU, barcode or style code from the tag into tag_codes (and brand_sku when one is clear).
- Image-only material guesses must use source visual_inference and lower confidence.
- Never infer customer gender, age, or income. garment_department is how the garment itself is cut
  and sold (menswear, womenswear, kids); use unknown when the garment does not show it.
- unknown is a valid value. Do not hallucinate brands.
- confidence is 0.0–1.0 per field in attribute_evidence.
- garment_category is the outfit layer; garment_subcategory is the specific type.
- pairs_with lists subcategories from a DIFFERENT category that complete an outfit with
  this piece (e.g. tshirt: overshirt, jeans, sneakers; shirt: jacket, trousers, loafers).
  Never list the same subcategory or another piece of the same category.
"""


def preferred_values_hint() -> str:
    """Canonical spellings already used by the dashboards (ref_values), so new photos reuse them."""
    lines = []
    for kind, field, label in (("brand", "brand_name", "brands"), ("color", "primary_color", "colours"),
                               ("garment_name", "display_name", "garment names")):
        vals = vocab.canonical_values(kind, limit=60)
        if vals:
            lines.append(f"- {field}: when it matches, use one of these {label}: {', '.join(vals)}")
    if not lines:
        return ""
    return ("Preferred spellings (only when the photo shows that value; otherwise use the label text or unknown):\n"
            + "\n".join(lines) + "\n")


def extract_attributes(image: GcsImage) -> Dict[str, Any]:
    """
    Call Vertex Gemini on the image.
    Prefers GCS URI (no re-upload); falls back to inline bytes if URI fails.
    """
    if not settings.ENABLE_VERTEX:
        raise RuntimeError("ENABLE_VERTEX is false")
    if not settings.PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT / GCP_PROJECT is required for Vertex")

    model_name = "gemini-3.5-flash-lite"
    vertexai.init(project=settings.PROJECT_ID, location=settings.VERTEX_LOCATION)
    model = GenerativeModel(model_name)

    prompt = (
        SYSTEM_PROMPT
        + preferred_values_hint()
        + "\nJSON schema example (types/enums only):\n"
        + json.dumps(EXTRACTION_SCHEMA_HINT)
        + "\nExtract attributes from this garment/campaign product image."
    )
    gen_cfg = GenerationConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
    )

    try:
        part = Part.from_uri(image.gs_uri, mime_type=image.content_type)
        response = model.generate_content([prompt, part], generation_config=gen_cfg)
    except Exception as uri_exc:
        log.warning("Vertex GCS URI failed (%s); retrying with inline bytes", uri_exc)
        part = Part.from_data(data=image.data, mime_type=image.content_type)
        response = model.generate_content([prompt, part], generation_config=gen_cfg)

    text = getattr(response, "text", None) or ""
    if not text and getattr(response, "candidates", None):
        chunks = []
        for cand in response.candidates:
            for p in getattr(cand.content, "parts", []) or []:
                if getattr(p, "text", None):
                    chunks.append(p.text)
        text = "\n".join(chunks)

    parsed = parse_json_response(text)
    result = normalize_result(parsed, model_version=model_name)
    result["review_status"] = decide_review_status(result)
    log.info(
        "vertex_ok model=%s review_status=%s min_conf=%.2f brand=%s",
        model_name,
        result["review_status"],
        min_confidence(result),
        result.get("brand_name"),
    )
    return result
