"""Pure helpers for extraction result normalization (no GCP imports)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from worker.settings import settings


# Buckets match command_center/style_assistant.CATEGORY_BUCKETS.
SUBCATEGORY_BUCKET: Dict[str, str] = {
    "tshirt": "top", "shirt": "top", "polo": "top", "kurta": "top", "blouse": "top",
    "tank": "top", "sweater": "top", "top": "top", "tunic": "top",
    "overshirt": "outer", "jacket": "outer", "hoodie": "outer", "blazer": "outer",
    "coat": "outer", "cardigan": "outer", "sweatshirt": "outer", "shrug": "outer",
    "jeans": "bottom", "trousers": "bottom", "chinos": "bottom", "shorts": "bottom",
    "skirt": "bottom", "joggers": "bottom", "leggings": "bottom", "palazzo": "bottom",
    "dress": "dress", "saree": "dress", "jumpsuit": "dress", "gown": "dress",
    "sneakers": "footwear", "loafers": "footwear", "boots": "footwear",
    "sandals": "footwear", "heels": "footwear", "formal_shoes": "footwear",
    "belt": "accessory", "cap": "accessory", "scarf": "accessory", "bag": "accessory",
    "watch": "accessory", "tie": "accessory",
}
GARMENT_CATEGORIES = ("top", "bottom", "outer", "dress", "footwear", "accessory", "unknown")
SLEEVE_LENGTHS = ("full", "half", "sleeveless", "unknown")
_SUB_ALIASES = {"t-shirt": "tshirt", "tee": "tshirt", "t shirt": "tshirt", "trouser": "trousers",
                "pants": "trousers", "sneaker": "sneakers", "loafer": "loafers", "boot": "boots",
                "sandal": "sandals", "heel": "heels", "shoes": "formal_shoes"}


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", " ").replace("  ", " ")


def subcategory(value: Any) -> Optional[str]:
    t = _token(value)
    t = _SUB_ALIASES.get(t, t).replace(" ", "_")
    return t if t in SUBCATEGORY_BUCKET else None


def outfit_tags(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce Gemini outfit tags to vocab; pairs_with never shares the garment's own bucket."""
    sub = subcategory(raw.get("garment_subcategory")) or subcategory(raw.get("commodity_type"))
    cat = _token(raw.get("garment_category"))
    if sub:
        cat = SUBCATEGORY_BUCKET[sub]
    elif cat not in GARMENT_CATEGORIES:
        cat = "unknown"
    sleeve = _token(raw.get("sleeve_length"))
    if sleeve not in SLEEVE_LENGTHS:
        sleeve = "unknown"
    pairs: List[str] = []
    items = raw.get("pairs_with") if isinstance(raw.get("pairs_with"), list) else []
    for item in items:
        p = subcategory(item)
        if not p or p == sub or p in pairs:
            continue
        if cat != "unknown" and SUBCATEGORY_BUCKET[p] == cat:
            continue
        pairs.append(p)
    return {"garment_category": cat, "garment_subcategory": sub, "sleeve_length": sleeve, "pairs_with": pairs}


def parse_json_response(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def normalize_result(raw: Dict[str, Any], *, model_version: str) -> Dict[str, Any]:
    evidence = raw.get("attribute_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}

    def field(name: str, default: str = "unknown"):
        val = raw.get(name)
        if val is None or val == "":
            return default
        return val

    return {
        "display_name": field("display_name"),
        "commodity_type": field("commodity_type"),
        "material_type": field("material_type"),
        "material_blend": raw.get("material_blend"),
        "primary_color": field("primary_color"),
        "secondary_colors": raw.get("secondary_colors") or [],
        "size_raw": raw.get("size_raw"),
        "size_normalized": raw.get("size_normalized"),
        "size_system": raw.get("size_system"),
        "brand_name": field("brand_name"),
        "brand_sku": raw.get("brand_sku"),
        "pattern": field("pattern"),
        "sleeve_type": field("sleeve_type"),
        "collar_type": field("collar_type"),
        "garment_department": field("garment_department"),
        "style_tags": raw.get("style_tags") or [],
        **outfit_tags(raw),
        "attribute_evidence": evidence,
        "notes": raw.get("notes"),
        "model_version": model_version,
        "raw_model_json": raw,
    }


def min_confidence(result: Dict[str, Any]) -> float:
    """Min confidence over core fields. Brand ignored when brand_name is unknown."""
    evidence = result.get("attribute_evidence") or {}
    keys = ["commodity_type", "material_type", "primary_color", "size_normalized"]
    brand = (result.get("brand_name") or "unknown").lower()
    if brand != "unknown":
        keys.append("brand_name")
    confs = []
    for key in keys:
        item = evidence.get(key) or {}
        if isinstance(item, dict) and item.get("confidence") is not None:
            try:
                confs.append(float(item["confidence"]))
            except (TypeError, ValueError):
                pass
    return min(confs) if confs else 0.0


def decide_review_status(result: Dict[str, Any]) -> str:
    brand = (result.get("brand_name") or "unknown").lower()
    brand_ev = (result.get("attribute_evidence") or {}).get("brand_name") or {}
    brand_conf = float(brand_ev.get("confidence") or 0)
    if brand != "unknown" and brand_conf < settings.AUTO_ACCEPT_CONFIDENCE:
        return "NEEDS_REVIEW"
    if min_confidence(result) >= settings.AUTO_ACCEPT_CONFIDENCE:
        return "COMPLETED"
    return "NEEDS_REVIEW"
