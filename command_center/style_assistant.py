"""Feature 2 — Style Assistant: per-customer picks computed at Extract & Rank, served read-only."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, List, Optional, Sequence

import brand_campaign as brand_camp
import vocab

log = logging.getLogger("command_center.style_assistant")

COSINE_MATCH_THRESHOLD = float(os.getenv("STYLE_COSINE_THRESHOLD", "0.75"))
SIGNED_URL_MINUTES = int(os.getenv("STYLE_SIGNED_URL_MINUTES", "60"))

# Outfit completion: same bucket must never pair (no shirt↔shirt / jeans↔jeans).
COMPLEMENTARY: dict[str, frozenset[str]] = {
    "top": frozenset({"bottom", "outer", "footwear", "accessory", "dress"}),
    "bottom": frozenset({"top", "outer", "footwear", "accessory"}),
    "outer": frozenset({"top", "bottom", "dress", "footwear", "accessory"}),
    "dress": frozenset({"outer", "footwear", "accessory", "bottom"}),
    "footwear": frozenset({"top", "bottom", "outer", "dress", "accessory"}),
    "accessory": frozenset({"top", "bottom", "outer", "dress", "footwear"}),
    "unknown": frozenset(),  # resolved per-item with soft rules
}

# Longer / more specific phrases first
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "outer",
        (
            "overshirt",
            "overcoat",
            "trench",
            "parka",
            "blazer",
            "bomber",
            "windbreaker",
            "raincoat",
            "hoodie",
            "sweatshirt",
            "cardigan",
            "jacket",
            "coat",
            "vest",
            "gilet",
            "shrug",
        ),
    ),
    (
        "bottom",
        (
            "jeans",
            "denim",
            "chinos",
            "trousers",
            "pants",
            "leggings",
            "joggers",
            "shorts",
            "skirt",
            "culottes",
            "track pant",
            "trackpants",
        ),
    ),
    (
        "dress",
        ("dress", "gown", "jumpsuit", "romper", "playsuit", "saree", "kurta set"),
    ),
    (
        "footwear",
        (
            "sneaker",
            "shoes",
            "shoe",
            "boot",
            "sandal",
            "loafer",
            "heel",
            "footwear",
            "slipper",
        ),
    ),
    (
        "accessory",
        (
            "belt",
            "scarf",
            "hat",
            "cap",
            "bag",
            "handbag",
            "watch",
            "tie",
            "sock",
            "gloves",
            "jewellery",
            "jewelry",
            "necklace",
            "sunglass",
        ),
    ),
    (
        "top",
        (
            "t-shirt",
            "tshirt",
            "tee",
            "shirt",
            "blouse",
            "polo",
            "kurta",
            "top",
            "tank",
            "camisole",
            "sweater",
            "jumper",
            "tunic",
        ),
    ),
]


def infer_category_bucket(*texts: Any) -> str:
    """Map free-text garment type / commodity / filename → outfit bucket."""
    blob = " ".join(str(t or "") for t in texts).lower()
    blob = re.sub(r"[_\-.]+", " ", blob)
    if not blob.strip():
        return "unknown"
    for bucket, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in blob:
                return bucket
    return "unknown"


def categories_complementary(a: str, b: str, *, strict: bool = False) -> bool:
    """True if a and b can complete an outfit together (never same bucket).

    strict=True also rejects unknown categories, so a pairing is only made when
    both pieces are known to be different, compatible garment types.
    """
    ca = a if a in COMPLEMENTARY else "unknown"
    cb = b if b in COMPLEMENTARY else "unknown"
    if strict and (ca == "unknown" or cb == "unknown"):
        return False
    if ca == "unknown" or cb == "unknown":
        # Unknown × known: allow (cannot prove same category).
        # Unknown × unknown: allow soft; ranking still prefers complementary when known.
        return ca != cb or ca == "unknown"
    if ca == cb:
        return False
    return cb in COMPLEMENTARY[ca]


def _tag(item: dict[str, Any], key: str) -> Any:
    value = item.get(key)
    return value if value not in (None, "", []) else (item.get("attributes") or {}).get(key)


def item_subcategory(item: dict[str, Any]) -> Optional[str]:
    sub = str(_tag(item, "garment_subcategory") or "").strip().lower()
    return sub if sub and sub != "unknown" else None


def item_pairs_with(item: dict[str, Any]) -> set[str]:
    return {str(p).strip().lower() for p in (_tag(item, "pairs_with") or []) if str(p).strip()}


def can_complete(sku: dict[str, Any], owned: dict[str, Any]) -> bool:
    """Complete-your-outfit gate: different known buckets, different subcategories, and
    when extraction tagged pairs_with, the other piece must be listed."""
    sku_cat = sku.get("category_bucket") or annotate_item_category(sku)
    own_cat = owned.get("category_bucket") or annotate_item_category(owned)
    if not categories_complementary(sku_cat, own_cat, strict=True):
        return False
    sku_sub, own_sub = item_subcategory(sku), item_subcategory(owned)
    if sku_sub and sku_sub == own_sub:
        return False
    for item, other_sub in ((sku, own_sub), (owned, sku_sub)):
        pairs = item_pairs_with(item)
        if pairs and other_sub and other_sub not in pairs:
            return False
    return True


def annotate_item_category(item: dict[str, Any]) -> str:
    stored = str(_tag(item, "garment_category") or "").strip().lower()
    if stored in COMPLEMENTARY and stored != "unknown":
        item["category_bucket"] = stored
        return stored
    attrs = item.get("attributes") or {}
    bucket = infer_category_bucket(
        attrs.get("commodity"),
        attrs.get("name"),
        attrs.get("garment_type"),
        attrs.get("category"),
        item.get("label"),
        item.get("commodity_type"),
        item.get("original_filename"),
        item.get("gcs_object_key"),
    )
    item["category_bucket"] = bucket
    return bucket


def _parse_embedding(raw: Any) -> Optional[List[float]]:
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
        except Exception:
            return None
    return None


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx, fy = float(x), float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0 or nb <= 0:
        return 0.0
    return float(dot / (math.sqrt(na) * math.sqrt(nb)))


def sim_fit_01(cosine: float) -> float:
    """Map cosine (−1..1) to 0..1 for blend_rank_score."""
    return max(0.0, min(1.0, (float(cosine) + 1.0) / 2.0))


def _wardrobe_rows_from_cursor(cur_rows: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for r in cur_rows:
        emb = _parse_embedding(r[4])
        brand = r[1] or (r[12] if len(r) > 12 else None)
        commodity = r[8] if len(r) > 8 else None
        attrs: dict[str, Any] = {}
        if brand:
            attrs["brand"] = brand
        if commodity:
            attrs["commodity"] = commodity
            attrs["name"] = commodity
        if len(r) > 9 and r[9]:
            attrs["color"] = r[9]
        if len(r) > 10 and r[10]:
            attrs["material"] = r[10]
        if len(r) > 11 and r[11]:
            attrs["pattern"] = r[11]
        label = commodity or brand or "Wardrobe piece"
        first_key = r[7] if len(r) > 7 else None
        item = {
            "garment_id": r[0],
            "brand_name": brand,
            "gcs_bucket": r[2],
            "gcs_object_key": r[3] or first_key,
            "embedding": emb,
            "pin_code": r[5],
            "observation_count_total": int(r[6] or 0),
            "commodity_type": commodity,
            "label": label,
            "attributes": attrs,
        }
        if len(r) > 16:
            item["garment_category"] = r[14]
            item["garment_subcategory"] = r[15]
            item["pairs_with"] = list(r[16] or [])
        annotate_item_category(item)
        rows.append(item)
    return rows


_ITEM_ATTR_FIELDS = ("name", "color", "material", "pattern", "brand", "size", "sleeve_type", "collar_type")
_ITEM_TAG_FIELDS = ("garment_category", "garment_subcategory", "pairs_with")
_UNKNOWN_ATTR = {"", "unknown", "none", "null", "n/a"}

# Test hook: gcs_object_keys -> {key: attribute row}
_item_attr_loader: Optional[Any] = None


def _bq_item_attrs(keys: list[str]) -> dict[str, dict[str, Any]]:
    """Attributes the photo pipeline stored in suffai_ops.commodities, by photo key."""
    keys = sorted({k for k in keys if k})
    if not keys:
        return {}
    if _item_attr_loader is not None:
        return _item_attr_loader(keys)
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project or os.getenv("USE_BQ", "true").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    try:
        from google.cloud import bigquery  # type: ignore

        dataset = os.getenv("BQ_DATASET", "suffai_ops")
        client = bigquery.Client(project=project)
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("keys", "STRING", keys)])
    except Exception as exc:
        log.warning("wardrobe BigQuery attributes unavailable (%s); using Cloud SQL attributes only", type(exc).__name__)
        return {}
    # Outfit tag columns come from migrate_bq_catalog_match.sql; read without them until it has run.
    for fields in (_ITEM_ATTR_FIELDS + _ITEM_TAG_FIELDS, _ITEM_ATTR_FIELDS):
        sql = (
            f"SELECT gcs_object_key, {', '.join(fields)} FROM `{project}.{dataset}.commodities` "
            "WHERE gcs_object_key IN UNNEST(@keys)"
        )
        try:
            rows = client.query(sql, job_config=cfg).result()
            return {r["gcs_object_key"]: dict(r) for r in rows}
        except Exception as exc:
            log.warning("wardrobe BigQuery attributes query failed (tags=%s, %s)",
                        len(fields) > len(_ITEM_ATTR_FIELDS), type(exc).__name__)
    return {}


def enrich_from_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill wardrobe attributes from the garment item row of the same photo (BigQuery wins over Cloud SQL)."""
    found = _bq_item_attrs([r.get("gcs_object_key") or "" for r in rows])
    for item in rows:
        src = found.get(item.get("gcs_object_key") or "")
        if not src:
            continue
        attrs = item.setdefault("attributes", {})
        for f in _ITEM_ATTR_FIELDS:
            v = str(src.get(f) or "").strip()
            if v.lower() not in _UNKNOWN_ATTR:
                attrs[f] = v
        if attrs.get("name"):
            attrs["commodity"] = attrs["name"]
            item["commodity_type"] = attrs["name"]
            item["label"] = attrs["name"]
        if attrs.get("brand") and not item.get("brand_name"):
            item["brand_name"] = attrs["brand"]
        for f in ("garment_category", "garment_subcategory"):
            v = str(src.get(f) or "").strip().lower()
            if v and v not in _UNKNOWN_ATTR:
                item[f] = v
        pairs = [p.strip().lower() for p in str(src.get("pairs_with") or "").split(",") if p.strip()]
        if pairs:
            item["pairs_with"] = pairs
        item.pop("category_bucket", None)
        annotate_item_category(item)
    return rows


def load_user_wardrobe_rich(user_uuid: str, *, limit: int = 80) -> list[dict[str, Any]]:
    """Load wardrobe; attributes come from the photo's garment item in BigQuery, else garment_instances."""
    return enrich_from_items(_load_user_wardrobe_sql(user_uuid, limit=limit))


def _load_user_wardrobe_sql(user_uuid: str, *, limit: int = 80) -> list[dict[str, Any]]:
    enriched_sql = """
                    SELECT * FROM (
                      SELECT DISTINCT ON (ug.garment_id)
                             ug.garment_id::text,
                             ug.brand_name,
                             ug.representative_gcs_bucket,
                             ug.representative_gcs_object_key,
                             ug.embedding,
                             uw.pin_code_at_first_sight,
                             ug.observation_count_total,
                             uw.first_gcs_object_key,
                             ginst.commodity_type,
                             ginst.primary_color,
                             ginst.material_type,
                             ginst.pattern,
                             ginst.brand_name_raw,
                             ug.updated_at{tags}
                      FROM user_wardrobe uw
                      JOIN unique_garments ug ON ug.garment_id = uw.garment_id
                      LEFT JOIN garment_images gim ON (
                          gim.gcs_object_key = ug.representative_gcs_object_key
                          OR gim.gcs_object_key = uw.first_gcs_object_key
                      )
                      LEFT JOIN garment_instances ginst ON ginst.id = gim.garment_instance_id
                        AND ginst.deleted_at IS NULL
                      WHERE uw.user_uuid = %s
                      ORDER BY ug.garment_id, ginst.updated_at DESC NULLS LAST
                    ) enriched
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT %s
                    """
    tag_cols = ",\n                             ginst.garment_category, ginst.garment_subcategory, ginst.pairs_with"
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            for tags in (tag_cols, ""):
                try:
                    cur.execute(enriched_sql.replace("{tags}", tags), (user_uuid, limit))
                    return _wardrobe_rows_from_cursor(cur.fetchall())
                except Exception as exc:
                    log.warning("wardrobe enrich query failed (tags=%s): %s", bool(tags), exc)
                    conn.rollback()
            cur.execute(
                """
                SELECT ug.garment_id::text,
                       ug.brand_name,
                       ug.representative_gcs_bucket,
                       ug.representative_gcs_object_key,
                       ug.embedding,
                       uw.pin_code_at_first_sight,
                       ug.observation_count_total,
                       uw.first_gcs_object_key
                FROM user_wardrobe uw
                JOIN unique_garments ug ON ug.garment_id = uw.garment_id
                WHERE uw.user_uuid = %s
                ORDER BY ug.updated_at DESC
                LIMIT %s
                """,
                (user_uuid, limit),
            )
            return _wardrobe_rows_from_cursor(cur.fetchall())


def _sku_rows_to_items(rows: list[Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        attrs = {
            "color": r[4],
            "pattern": r[5],
            "material": r[6],
            "size": r[7],
            "name": r[8],
            "commodity": r[8],
            "sleeve_type": r[9],
            "collar_type": r[10],
            "brand": r[1],
        }
        item = {
            "sku_uuid": r[0],
            "brand_name": r[1],
            "gcs_bucket": r[2],
            "gcs_object_key": r[3],
            "attributes": attrs,
            "embedding": _parse_embedding(r[11]),
            "original_filename": r[12],
            "extract_ok": bool(r[13]),
            "label": r[8] or r[12] or r[1] or "SKU",
        }
        if len(r) > 16:
            item["garment_category"] = r[14]
            item["garment_subcategory"] = r[15]
            item["pairs_with"] = list(r[16] or [])
        annotate_item_category(item)
        out.append(item)
    return out


def list_campaign_skus_rich(campaign_id: str, *, limit: int = 80) -> list[dict[str, Any]]:
    sql = """
                SELECT sku_uuid::text, brand_name, gcs_bucket, gcs_object_key,
                       color, pattern, material, size, commodity,
                       sleeve_type, collar_type, embedding, original_filename,
                       extract_ok{tags}
                FROM brand_skus
                WHERE campaign_id = %s
                ORDER BY
                  CASE WHEN embedding IS NOT NULL THEN 0 ELSE 1 END,
                  CASE WHEN extract_ok THEN 0 ELSE 1 END,
                  created_at ASC
                LIMIT %s
                """
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.replace("{tags}", ", garment_category, garment_subcategory, pairs_with"),
                    (campaign_id, limit),
                )
            except Exception as exc:
                log.warning("SKU outfit tags unavailable (%s); run migration 011", exc)
                conn.rollback()
                cur.execute(sql.replace("{tags}", ""), (campaign_id, limit))
            return _sku_rows_to_items(cur.fetchall())


def build_wardrobe_taste_profile(wardrobe: list[dict[str, Any]]) -> dict[str, Any]:
    dist: dict[str, dict[str, float]] = {
        "color": {},
        "pattern": {},
        "material": {},
        "size": {},
        "commodity": {},
        "brand": {},
    }
    for item in wardrobe:
        attrs = item.get("attributes") or {}
        pairs = {
            "color": attrs.get("color"),
            "pattern": attrs.get("pattern"),
            "material": attrs.get("material"),
            "size": attrs.get("size"),
            "commodity": attrs.get("name") or attrs.get("commodity"),
            "brand": item.get("brand_name") or attrs.get("brand"),
        }
        for dim, raw in pairs.items():
            val = vocab.attr_key(dim, raw)
            if not val:
                continue
            dist[dim][val] = dist[dim].get(val, 0.0) + 1.0
    return {"cell_count": len(wardrobe), "distributions": dist}


def _complementary_wardrobe(
    sku: dict[str, Any], wardrobe: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Wardrobe pieces that complete an outfit with this SKU: known, different, compatible categories."""
    return [w for w in wardrobe if can_complete(sku, w)]


def _score_sku_against_partners(
    sku: dict[str, Any],
    partners: list[dict[str, Any]],
    profile: dict[str, Any],
    cos: Optional[dict[tuple[str, str], float]] = None,
) -> Optional[dict[str, Any]]:
    if not partners:
        return None
    partners_emb = [w for w in partners if w.get("embedding")]
    sku_emb = sku.get("embedding")
    best_cos = -1.0
    best_matches: list[tuple[str, float]] = []

    if sku_emb and partners_emb:
        for w in partners_emb:
            key = (sku.get("sku_uuid"), w["garment_id"])
            c = cos[key] if cos is not None and key in cos else cosine_similarity(sku_emb, w["embedding"] or [])
            best_matches.append((w["garment_id"], c))
            if c > best_cos:
                best_cos = c
        best_matches.sort(key=lambda t: t[1], reverse=True)
        top_own = [gid for gid, _ in best_matches[:2]]
        sim = sim_fit_01(best_cos)
        above = sum(1 for _, c in best_matches if c >= COSINE_MATCH_THRESHOLD)
        freq = above / max(1, len(best_matches))
    else:
        sim = 0.0
        freq = 0.0
        by_overlap = sorted(partners, key=lambda w: -_attr_overlap(sku, w))
        top_own = [w["garment_id"] for w in by_overlap[:2]]
        best_cos = -1.0

    a_fit = brand_camp.attr_fit_score(sku.get("attributes") or {}, profile)
    score = brand_camp.blend_rank_score(
        sim_fit=sim, freq_coverage=freq, attr_fit=a_fit
    )
    return {
        "sku": sku,
        "score": score,
        "sim_fit": round(sim, 4),
        "freq_coverage": round(freq, 4),
        "attr_fit": round(a_fit, 4),
        "best_cosine": round(best_cos, 4) if best_cos > -1 else None,
        "own_garment_ids": top_own,
        "sku_category": sku.get("category_bucket"),
        "own_categories": [
            next(
                (
                    w.get("category_bucket")
                    for w in partners
                    if w["garment_id"] == gid
                ),
                "unknown",
            )
            for gid in top_own
        ],
    }


def _attr_overlap(sku: dict[str, Any], w: dict[str, Any]) -> int:
    a, b = sku.get("attributes") or {}, w.get("attributes") or {}
    return sum(
        1 for k in ("color", "pattern", "material")
        if vocab.attr_key(k, a.get(k)) and vocab.attr_key(k, a.get(k)) == vocab.attr_key(k, b.get(k))
    )


def sku_buy_fields(sku: dict[str, Any]) -> dict[str, Any]:
    """Fields for the Buy this item CTA (optional URL template)."""
    tmpl = (os.getenv("STYLE_BUY_URL_TEMPLATE") or "").strip()
    sku_uuid = str(sku.get("sku_uuid") or "")
    brand = str(sku.get("brand_name") or "")
    buy_url = None
    if tmpl:
        try:
            buy_url = tmpl.format(
                sku_uuid=sku_uuid,
                brand_name=brand,
                brand=brand,
            )
        except Exception:
            buy_url = tmpl
    return {
        "sku_uuid": sku_uuid,
        "brand_name": brand,
        "label": sku.get("label"),
        "gcs_bucket": sku.get("gcs_bucket"),
        "gcs_object_key": sku.get("gcs_object_key"),
        "image_url": sku.get("image_url"),
        "attributes": sku.get("attributes") or {},
        "category_bucket": sku.get("category_bucket"),
        "buy_url": buy_url,
    }


def rank_skus_for_user(
    wardrobe: list[dict[str, Any]],
    skus: list[dict[str, Any]],
    *,
    limit: int = 12,
    cos: Optional[dict[tuple[str, str], float]] = None,
) -> list[dict[str, Any]]:
    """
    Rank brand SKUs to *complete* a wardrobe outfit.
    Same category pairs (shirt↔shirt, jeans↔jeans) and unknown categories are excluded.
    Similarity is computed only against complementary wardrobe pieces.
    """
    for w in wardrobe:
        annotate_item_category(w)
    for s in skus:
        annotate_item_category(s)

    profile = build_wardrobe_taste_profile(wardrobe)
    ranked: list[dict[str, Any]] = []

    for sku in skus:
        partners = _complementary_wardrobe(sku, wardrobe)
        if not partners:
            log.debug(
                "skip sku=%s cat=%s — no complementary wardrobe pieces",
                sku.get("sku_uuid"),
                sku.get("category_bucket"),
            )
            continue
        row = _score_sku_against_partners(sku, partners, profile, cos)
        if row:
            ranked.append(row)

    ranked.sort(key=lambda r: (-(r["score"] or 0), r["sku"]["sku_uuid"]))
    return ranked[:limit]


def signed_image_url(
    bucket: Optional[str], object_key: Optional[str], *, minutes: int = SIGNED_URL_MINUTES
) -> Optional[str]:
    if not bucket or not object_key:
        return None
    try:
        from google.cloud import storage  # type: ignore
        import google.auth  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore

        credentials, _ = google.auth.default()
        credentials.refresh(Request())
        client = storage.Client(credentials=credentials)
        blob = client.bucket(bucket).blob(object_key)
        kwargs: dict[str, Any] = {
            "version": "v4",
            "expiration": timedelta(minutes=minutes),
            "method": "GET",
        }
        email = getattr(credentials, "service_account_email", None)
        token = getattr(credentials, "token", None)
        if email and token:
            kwargs["service_account_email"] = email
            kwargs["access_token"] = token
        return blob.generate_signed_url(**kwargs)
    except Exception as exc:
        log.warning("signed URL failed gs://%s/%s: %s", bucket, object_key, exc)
        return None


def attach_image_urls(items: list[dict[str, Any]]) -> None:
    keys = list(dict.fromkeys((item.get("gcs_bucket"), item.get("gcs_object_key")) for item in items))
    if len(keys) > 1:
        # each signature is a remote IAM signBlob call: sign concurrently so a 20-image campaign isn't 20 round trips
        with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
            signed = dict(zip(keys, pool.map(lambda k: signed_image_url(*k), keys)))
    else:
        signed = {k: signed_image_url(*k) for k in keys}
    for item in items:
        item["image_url"] = signed[(item.get("gcs_bucket"), item.get("gcs_object_key"))]


# ---------------------------------------------------------------------------
# Per-customer picks: computed once at the end of Extract & Rank
# ---------------------------------------------------------------------------

PICKS_PER_CUSTOMER = int(os.getenv("STYLE_PICKS_PER_CUSTOMER", "6"))


def _cosine_lookup(
    skus: list[dict[str, Any]], wardrobe: list[dict[str, Any]]
) -> dict[tuple[str, str], float]:
    """All SKU × wardrobe cosines for one customer in a single matrix product."""
    s_emb = [(s["sku_uuid"], s["embedding"]) for s in skus if s.get("embedding")]
    w_emb = [(w["garment_id"], w["embedding"]) for w in wardrobe if w.get("embedding")]
    if not s_emb or not w_emb:
        return {}
    if len({len(e) for _, e in s_emb + w_emb}) == 1:
        try:
            import numpy as np  # type: ignore

            a = np.asarray([e for _, e in s_emb], dtype=float)
            b = np.asarray([e for _, e in w_emb], dtype=float)
            a /= np.clip(np.linalg.norm(a, axis=1, keepdims=True), 1e-12, None)
            b /= np.clip(np.linalg.norm(b, axis=1, keepdims=True), 1e-12, None)
            m = a @ b.T
            return {
                (sid, gid): float(m[i, j])
                for i, (sid, _) in enumerate(s_emb)
                for j, (gid, _) in enumerate(w_emb)
            }
        except ImportError:
            pass
    return {(sid, gid): cosine_similarity(se, we) for sid, se in s_emb for gid, we in w_emb}


def _piece(w: dict[str, Any]) -> dict[str, Any]:
    """Stored wardrobe piece: GCS location only (URLs are signed when the feed is read)."""
    return {
        "garment_id": w.get("garment_id"),
        "label": w.get("label"),
        "brand_name": w.get("brand_name"),
        "category_bucket": w.get("category_bucket"),
        "color": (w.get("attributes") or {}).get("color"),
        "gcs_bucket": w.get("gcs_bucket"),
        "gcs_object_key": w.get("gcs_object_key"),
    }


def _stored_buy(sku: dict[str, Any]) -> dict[str, Any]:
    buy = sku_buy_fields(sku)
    buy.pop("image_url", None)
    return buy


def rank_recommended_for_user(
    wardrobe: list[dict[str, Any]],
    skus: list[dict[str, Any]],
    *,
    limit: int = 12,
    cos: Optional[dict[tuple[str, str], float]] = None,
) -> list[dict[str, Any]]:
    """'Since you have X, you may like Y': closest wardrobe piece in any category."""
    for w in wardrobe:
        annotate_item_category(w)
    for s in skus:
        annotate_item_category(s)
    profile = build_wardrobe_taste_profile(wardrobe)
    ranked = [r for r in (_score_sku_against_partners(s, wardrobe, profile, cos) for s in skus) if r]
    ranked.sort(key=lambda r: (-(r["score"] or 0), r["sku"]["sku_uuid"]))
    return ranked[:limit]


def _why_it_works(owned: dict[str, Any], sku: dict[str, Any]) -> str:
    own_name = item_subcategory(owned) or owned.get("category_bucket") or "piece"
    sku_name = item_subcategory(sku) or sku.get("category_bucket") or "piece"
    verb = "layers over it" if sku.get("category_bucket") == "outer" else "completes it"
    return f"Your {own_name} stays. This {sku_name} {verb}."


def build_complete_looks(
    *, wardrobe: list[dict[str, Any]], ranked: list[dict[str, Any]], brand_name: str, limit: int
) -> list[dict[str, Any]]:
    by_id = {w["garment_id"]: w for w in wardrobe}
    looks: list[dict[str, Any]] = []
    for r in ranked[:limit]:
        sku = r["sku"]
        own = [by_id[g] for g in (r.get("own_garment_ids") or []) if g in by_id][:1]
        if not own:
            continue
        owned = own[0]
        owned_color = (owned.get("attributes") or {}).get("color")
        owned_name = " ".join(part for part in (owned_color, owned.get("label") or "piece") if part)
        sku_label = sku.get("label") or "piece"
        sentence = f"Your {owned_name}, finished with this {sku_label}."
        looks.append({
            "rank": len(looks) + 1,
            "score": r["score"],
            "sim_fit": r["sim_fit"],
            "attr_fit": r["attr_fit"],
            "own": [_piece(owned)],
            "buy": _stored_buy(sku),
            "title": sentence,
            "description": sentence,
            "why_it_works": _why_it_works(owned, sku),
        })
    return looks


def build_recommended(
    *, wardrobe: list[dict[str, Any]], ranked: list[dict[str, Any]], brand_name: str, limit: int
) -> list[dict[str, Any]]:
    by_id = {w["garment_id"]: w for w in wardrobe}
    picks: list[dict[str, Any]] = []
    for r in ranked[:limit]:
        sku = r["sku"]
        liked = next((by_id[g] for g in (r.get("own_garment_ids") or []) if g in by_id), None)
        if liked is None:
            continue
        liked_label = liked.get("label") or "your piece"
        color = (sku.get("attributes") or {}).get("color")
        picks.append({
            "rank": len(picks) + 1,
            "score": r["score"],
            "sim_fit": r["sim_fit"],
            "attr_fit": r["attr_fit"],
            "liked": _piece(liked),
            "buy": _stored_buy(sku),
            "title": f"Since you have your {liked_label}",
            "description": (
                f"You may like {sku.get('label') or 'this piece'}{f' in {color}' if color else ''} "
                f"from {brand_name}."
            ),
            "why_it_works": f"Closest match to your {liked_label} in style and look. Fit score {r['score']}.",
        })
    return picks


def build_customer_picks(
    wardrobe: list[dict[str, Any]],
    skus: list[dict[str, Any]],
    brand_name: str,
    *,
    limit: int = PICKS_PER_CUSTOMER,
) -> tuple[dict[str, Any], Optional[float]]:
    """Stored payload (no signed URLs, no customer identity) and the best fit score."""
    for w in wardrobe:
        annotate_item_category(w)
    for s in skus:
        annotate_item_category(s)
    cos = _cosine_lookup(skus, wardrobe)
    complete_ranked = rank_skus_for_user(wardrobe, skus, limit=limit, cos=cos)
    rec_ranked = rank_recommended_for_user(wardrobe, skus, limit=limit, cos=cos)
    payload = {
        "brand_name": brand_name,
        "complete_outfit": build_complete_looks(wardrobe=wardrobe, ranked=complete_ranked, brand_name=brand_name, limit=limit),
        "recommended": build_recommended(wardrobe=wardrobe, ranked=rec_ranked, brand_name=brand_name, limit=limit),
        "wardrobe": [_piece(w) for w in wardrobe],
    }
    scores = [float(r["score"] or 0) for r in complete_ranked[:1] + rec_ranked[:1]]
    return payload, (max(scores) if scores else None)


def eligible_cap() -> int:
    try:
        return max(1, int(os.getenv("CAMPAIGN_ELIGIBLE_USER_CAP", "200")))
    except ValueError:
        return 200


def eligible_customers(campaign: dict[str, Any]) -> list[str]:
    """The Ads Lab audience who can get picks: BigQuery customers at a campaign PIN (their current PIN)
    matching every age / gender / RFM filter, who own a wardrobe in Cloud SQL. Capped at
    CAMPAIGN_ELIGIBLE_USER_CAP. Raises admin_intel.IntelUnavailable when BigQuery can't be read.
    """
    import admin_intel

    pins = sorted({str(p).strip() for p in (campaign.get("pin_codes") or []) if str(p).strip()})
    if not pins:
        return []
    customers = admin_intel.dataset()["customers"]
    audience = sorted(
        cid for cid, c in customers.items()
        if c.get("pin_code") in pins and brand_camp._cell_matches(
            c, pin_codes=pins, age_group=campaign.get("age_group"), gender=campaign.get("gender"),
            rfm_segment=campaign.get("rfm_segment"),
        )
    )
    if not audience:
        return []
    owners = brand_camp.wardrobe_owners(audience)
    return [u for u in audience if u in owners][:eligible_cap()]


def personalise_campaign(campaign_id: str) -> dict[str, Any]:
    """Eligible customers × campaign SKUs → campaign_user_recommendations (runs inside the ranking job)."""
    campaign = brand_camp.get_campaign(campaign_id)
    if not campaign:
        raise LookupError(f"campaign not found: {campaign_id}")
    brand_name = campaign["brand_name"]
    skus = [s for s in list_campaign_skus_rich(campaign_id) if s.get("embedding") or s.get("extract_ok")]
    users = eligible_customers(campaign) if skus else []
    brand_camp.set_campaign_impact(campaign_id, eligible_users=len(users))

    computed = errors = 0
    for user_uuid in users:
        try:
            wardrobe = load_user_wardrobe_rich(user_uuid, limit=200)
            if not wardrobe:
                continue
            payload, fit = build_customer_picks(wardrobe, skus, brand_name)
            if not payload["complete_outfit"] and not payload["recommended"]:
                continue
            brand_camp.upsert_user_recommendation(
                campaign_id=campaign_id,
                user_uuid=user_uuid,
                brand_name=brand_name,
                fit_score=fit,
                payload=payload,
            )
            computed += 1
        except Exception as exc:
            errors += 1
            log.warning("personalise skip campaign=%s err=%s", campaign_id, exc)

    brand_camp.set_campaign_impact(campaign_id, recommendations_computed=computed)
    log.info("personalised campaign=%s eligible=%s computed=%s errors=%s", campaign_id, len(users), computed, errors)
    return {
        "campaign_id": campaign_id,
        "eligible_users": len(users),
        "recommendations_computed": computed,
        "errors": errors,
    }


def materialize_campaign_recommendations(campaign_id: str) -> dict[str, Any]:
    """Legacy Start route: same saved picks as Extract & Rank."""
    brand_camp.ensure_campaign_title(campaign_id, (brand_camp.get_campaign(campaign_id) or {}).get("brand_name") or "")
    return personalise_campaign(campaign_id)


# ---------------------------------------------------------------------------
# Customer feed: reads saved picks only (no ranking at request time)
# ---------------------------------------------------------------------------


def _merge_picks(lists: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for items in lists:
        for item in items:
            buy = item.get("buy") if isinstance(item.get("buy"), dict) else None
            sku = str((buy or {}).get("sku_uuid") or "")
            if not sku:
                continue
            if sku not in best or float(item.get("score") or 0) > float(best[sku].get("score") or 0):
                best[sku] = item
    merged = sorted(best.values(), key=lambda i: -float(i.get("score") or 0))[:limit]
    return [{**item, "rank": n} for n, item in enumerate(merged, start=1)]


def _sign_feed(complete: list[dict[str, Any]], recommended: list[dict[str, Any]], wardrobe: list[dict[str, Any]]) -> None:
    items: list[dict[str, Any]] = list(wardrobe)
    for look in complete:
        look["buy"] = dict(look["buy"])
        look["own"] = [dict(p) for p in look.get("own") or []]
        items += [look["buy"], *look["own"]]
    for pick in recommended:
        pick["buy"] = dict(pick["buy"])
        pick["liked"] = dict(pick.get("liked") or {})
        items += [pick["buy"], pick["liked"]]
    attach_image_urls(items)


def _feed_ad(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """In-app hero ad: the best-fit launched In-app campaign with a headline (rows come fit-desc). No campaign_id."""
    for r in rows:
        creative = r.get("creative") or {}
        if r.get("status") != "launched" or "in_app" not in (r.get("channels") or []) or not creative.get("headline"):
            continue
        payload = r.get("payload") or {}
        buys = [p["buy"] for p in (payload.get("complete_outfit") or []) + (payload.get("recommended") or []) if isinstance(p.get("buy"), dict)]
        hero = next((b for b in buys if str(b.get("sku_uuid")) == str(creative.get("hero_sku_id"))), buys[0] if buys else None)
        image = None
        if hero:
            hero = dict(hero)
            attach_image_urls([hero])
            image = hero.get("image_url")
        url = str(creative.get("landing_url") or "")
        return {
            "brand_name": r.get("brand_name"),
            "headline": creative.get("headline"),
            "cta": creative.get("cta") or "Shop now",
            "offer_code": creative.get("offer_code") or None,
            "landing_url": url if url.startswith(("https://", "http://")) else None,
            "image_url": image,
            "label": (hero or {}).get("label"),
        }
    return None


def customer_style_feed(user_uuid: str, *, brand: Optional[str] = None, limit: int = 24) -> dict[str, Any]:
    """
    Style feed for a customer from every campaign they were ranked for.
    Response deliberately omits campaign_id for client-facing cards.
    """
    rows = brand_camp.list_recs_for_user(user_uuid)
    brands = sorted({r["brand_name"] for r in rows if r.get("brand_name")}, key=str.lower)
    want = (brand or "").strip().lower()
    chosen = [r for r in rows if not want or str(r.get("brand_name") or "").lower() == want]

    complete = _merge_picks([(r.get("payload") or {}).get("complete_outfit") or [] for r in chosen], limit)
    recommended = _merge_picks([(r.get("payload") or {}).get("recommended") or [] for r in chosen], limit)

    try:
        wardrobe = [_piece(w) for w in load_user_wardrobe_rich(user_uuid, limit=200)]
    except Exception as exc:
        log.warning("wardrobe read failed; using saved copy: %s", exc)
        wardrobe = next((list((r.get("payload") or {}).get("wardrobe") or []) for r in rows if (r.get("payload") or {}).get("wardrobe")), [])
        wardrobe = [dict(w) for w in wardrobe]

    _sign_feed(complete, recommended, wardrobe)

    shown = {
        r["campaign_id"]
        for r in chosen
        if (r.get("payload") or {}).get("complete_outfit") or (r.get("payload") or {}).get("recommended")
    }
    for cid in shown:
        try:
            brand_camp.set_campaign_impact(cid, bump_style_feed_serves=1)
        except Exception:  # a lost serve counter must never block the customer's feed
            log.warning("style feed serve counter failed campaign=%s", cid, exc_info=True)

    return {
        "ok": True,
        "source": "style_feed",
        "brand": brand if want else None,
        "brands": brands,
        "complete_outfit": complete,
        "recommended": recommended,
        "wardrobe": wardrobe,
        "wardrobe_count": len(wardrobe),
        "ad": _feed_ad(chosen),
    }


