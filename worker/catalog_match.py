"""Match a customer garment photo to a brand's warehouse catalog SKU (Cloud SQL retail_*).

When DATABASE_URL is unset, or the brand Gemini read has no catalog, matching is skipped and the
photo's attributes are kept. Order: printed SKU / barcode / style code, then a visual match against
the brand's catalog photos (one per style × colour), then strict attributes. Exact SKU / barcode hits
overwrite size; every other match keeps the photo's size. Sleeve and collar always stay from the photo.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from worker import vocab
from worker.result_utils import decide_review_status, min_confidence
from worker.settings import settings

log = logging.getLogger("suffai-extraction.catalog_match")

_CACHE_TTL = 300.0
_cache: dict[str, tuple[float, List[dict[str, Any]]]] = {}
_warned_no_db = False

_CODE_RE = re.compile(r"[^A-Z0-9]+")


def _norm_code(v: Any) -> str:
    return _CODE_RE.sub("", str(v or "").strip().upper())


def _codes_from_result(result: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("brand_sku", "style_code", "barcode"):
        c = _norm_code(result.get(key))
        if c and c not in out:
            out.append(c)
    tags = result.get("tag_codes")
    if isinstance(tags, list):
        for t in tags:
            c = _norm_code(t)
            if c and c not in out:
                out.append(c)
    return out


def _load_brand(brand_key: str) -> List[dict[str, Any]]:
    """Active variants + product fields for one brand_key. Empty when DB unavailable."""
    global _warned_no_db
    if not (settings.DATABASE_URL or "").strip():
        if not _warned_no_db:
            log.info("catalog match skipped: DATABASE_URL unset")
            _warned_no_db = True
        return []
    now = time.monotonic()
    hit = _cache.get(brand_key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        from worker.db import connect
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT v.variant_id, v.sku, v.barcode, v.color, v.size,
                           p.product_id, p.style_code, p.name, p.category, p.material, p.pattern, p.gender
                    FROM retail_variants v
                    JOIN retail_products p ON p.product_id = v.product_id
                    WHERE v.brand_key = %s AND p.brand_key = %s AND v.active AND p.active
                    """,
                    (brand_key, brand_key),
                )
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        log.exception("catalog load failed brand=%s", brand_key)
        return []
    _cache[brand_key] = (now, rows)
    return rows


def clear_cache() -> None:
    _cache.clear()
    _image_cache.clear()


# --- Visual match ---------------------------------------------------------------------------

Embedder = Callable[[Any], Tuple[List[float], str]]
Reader = Callable[..., Any]
_image_cache: dict[str, tuple[float, List[dict[str, Any]]]] = {}


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def embed_image(image: Any) -> Tuple[List[float], str]:
    """Vertex multimodal fingerprint, same model and size as suffai-similarity."""
    import vertexai
    from vertexai.vision_models import Image, MultiModalEmbeddingModel

    if not settings.PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT / GCP_PROJECT is required for Vertex")
    vertexai.init(project=settings.PROJECT_ID, location=settings.VERTEX_LOCATION)
    model = MultiModalEmbeddingModel.from_pretrained(settings.VERTEX_EMBED_MODEL)
    try:
        img = Image.load_from_file(image.gs_uri)
    except Exception:
        img = Image(image_bytes=image.data)
    emb = list(model.get_embeddings(image=img, dimension=settings.EMBED_DIMENSION).image_embedding or [])
    if not emb:
        raise RuntimeError("Vertex returned empty image embedding")
    return emb, settings.VERTEX_EMBED_MODEL


def _load_images(brand_key: str) -> List[dict[str, Any]]:
    """Catalog photos (style × colour) of active products, with any stored fingerprint."""
    if not (settings.DATABASE_URL or "").strip():
        return []
    now = time.monotonic()
    hit = _image_cache.get(brand_key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    from worker.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.product_id, i.color_key, i.color, i.gcs_bucket, i.gcs_object_key,
                       i.embedding, i.embedding_model, i.embedding_dim, i.embedded_object_key,
                       p.style_code, p.name, p.category, p.material, p.pattern, p.gender
                FROM retail_product_images i
                JOIN retail_products p ON p.product_id = i.product_id
                WHERE i.brand_key = %s AND p.active
                """,
                (brand_key,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        emb = r.get("embedding")
        r["embedding"] = [float(x) for x in (json.loads(emb) if isinstance(emb, str) else emb or [])]
    _image_cache[brand_key] = (now, rows)
    return rows


def _save_embedding(row: dict[str, Any], emb: List[float], model: str) -> None:
    from worker.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE retail_product_images SET embedding = %s::jsonb, embedding_model = %s, "
                "embedding_dim = %s, embedded_object_key = %s WHERE product_id = %s AND color_key = %s",
                (json.dumps(emb), model, len(emb), row["gcs_object_key"], row["product_id"], row["color_key"]),
            )


def _fresh(row: dict[str, Any], model: str, dim: int) -> bool:
    return (
        bool(row.get("embedding"))
        and row.get("embedded_object_key") == row.get("gcs_object_key")
        and (row.get("embedding_model") or model) == model
        and int(row.get("embedding_dim") or len(row["embedding"])) == dim
    )


def _ensure_embeddings(
    rows: List[dict[str, Any]], model: str, dim: int, *, embedder: Embedder, reader: Reader,
    save: Callable[[dict[str, Any], List[float], str], None],
) -> None:
    """Fingerprint catalog photos that have none (or a stale one), a capped number per call."""
    budget = max(0, settings.CATALOG_EMBED_PER_REQUEST)
    for row in rows:
        if budget <= 0:
            break
        if _fresh(row, model, dim):
            continue
        budget -= 1
        try:
            emb, used = embedder(reader(row["gcs_object_key"], bucket=row.get("gcs_bucket")))
        except Exception as exc:
            log.warning("catalog photo embed failed product=%s colour=%s: %s", row["product_id"], row["color_key"], exc)
            continue
        if used != model or len(emb) != dim:
            continue
        row.update(embedding=emb, embedding_model=used, embedding_dim=dim, embedded_object_key=row["gcs_object_key"])
        try:
            save(row, emb, used)
        except Exception as exc:
            log.warning("catalog embedding not saved product=%s: %s", row["product_id"], exc)


def _catalog_confidence(result: Dict[str, Any]) -> Tuple[str, float]:
    """Review status and min confidence once product fields come from the catalog (size still from photo)."""
    evidence = dict(result.get("attribute_evidence") or {})
    for key in ("commodity_type", "material_type", "primary_color", "pattern", "brand_name"):
        evidence[key] = {"source": "brand_catalog", "confidence": 1.0}
    boosted = {**result, "attribute_evidence": evidence}
    return decide_review_status(boosted), round(min_confidence(boosted), 3)


def visual_match(
    result: Dict[str, Any],
    photo_attrs: Dict[str, str],
    image: Any,
    *,
    embedder: Optional[Embedder] = None,
    reader: Optional[Reader] = None,
    loader: Optional[Callable[[str], List[dict[str, Any]]]] = None,
    save: Optional[Callable[[dict[str, Any], List[float], str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Overwrite payload when exactly one catalog style × colour looks like the photo; else None."""
    if image is None or (embedder is None and not settings.ENABLE_VERTEX):
        return None
    brand_raw = photo_attrs.get("brand") or result.get("brand_name")
    brand_key = vocab.value_key(brand_raw)
    if not brand_key or brand_key in ("unknown", "none"):
        return None
    try:
        rows = (loader or _load_images)(brand_key)
        if not rows:
            return None
        embedder = embedder or embed_image
        if reader is None:
            from worker.gcs_io import read_image as reader
        emb, model = embedder(image)
        _ensure_embeddings(rows, model, len(emb), embedder=embedder, reader=reader, save=save or _save_embedding)
    except Exception as exc:
        log.warning("visual catalog match skipped brand=%s: %s", brand_key, exc)
        return None
    scored = [
        (_cosine(emb, r["embedding"]), r) for r in rows if _fresh(r, model, len(emb))
    ]
    above = [(s, r) for s, r in scored if s >= settings.CATALOG_MATCH_THRESHOLD]
    if len(above) != 1:
        if len(above) > 1:
            log.info("visual catalog match ambiguous brand=%s styles=%s", brand_key,
                     [f"{r['style_code']}:{r['color_key']}" for _, r in above])
        return None
    score, row = above[0]
    out = _overwrite(row, vocab.canonical("brand", brand_raw), "visual", size_from_catalog=False)
    status, confidence = _catalog_confidence(result)
    out["confidence"] = confidence
    out["force_extracted"] = status == "COMPLETED"
    out["catalog_score"] = round(score, 4)
    return out


def _style_colours(rows: List[dict[str, Any]]) -> dict[Tuple[str, str], List[dict[str, Any]]]:
    groups: dict[Tuple[str, str], List[dict[str, Any]]] = {}
    for r in rows:
        key = (str(r["product_id"]), vocab.value_key(r["color"]))
        groups.setdefault(key, []).append(r)
    return groups


def match(
    result: Dict[str, Any],
    photo_attrs: Dict[str, str],
    image: Any = None,
    *,
    embedder: Optional[Embedder] = None,
) -> Optional[Dict[str, Any]]:
    """Return an overwrite payload or None.

    Payload keys: catalog_match, brand_sku, brand_style_code, name, color, brand, material,
    pattern, gender, size (optional), confidence (optional 1.0), force_extracted (bool).
    """
    brand_raw = photo_attrs.get("brand") or result.get("brand_name")
    brand_key = vocab.value_key(brand_raw)
    if not brand_key or brand_key in ("unknown", "none"):
        return None
    rows = _load_brand(brand_key)
    if not rows:
        return None

    brand_display = vocab.canonical("brand", brand_raw)

    codes = _codes_from_result(result)
    by_sku = {_norm_code(r["sku"]): r for r in rows}
    by_bar = {_norm_code(r["barcode"]): r for r in rows if r.get("barcode")}
    by_style: dict[str, List[dict[str, Any]]] = {}
    for r in rows:
        by_style.setdefault(_norm_code(r["style_code"]), []).append(r)

    # Exact: SKU or barcode → one variant (size from catalog).
    for c in codes:
        hit = by_sku.get(c) or by_bar.get(c)
        if hit:
            how = "sku" if c in by_sku else "barcode"
            return _overwrite(hit, brand_display, how, size_from_catalog=True)

    # Exact: style code → product; colour must match, or only one colour on the style.
    photo_col = vocab.attr_key("color", photo_attrs.get("color"))
    for c in codes:
        variants = by_style.get(c) or []
        if not variants:
            continue
        colours = {vocab.value_key(v["color"]): v for v in variants}
        if photo_col and photo_col in colours:
            return _overwrite(colours[photo_col], brand_display, "style_code", size_from_catalog=False)
        uniq = {vocab.value_key(v["color"]) for v in variants}
        if len(uniq) == 1:
            return _overwrite(variants[0], brand_display, "style_code", size_from_catalog=False)

    visual = visual_match(result, photo_attrs, image, embedder=embedder)
    if visual:
        return visual

    # Strict attributes: category + colour + material + pattern, exactly one style × colour.
    need = ("name", "color", "material", "pattern")
    keys = {d: vocab.attr_key(d if d != "name" else "commodity", photo_attrs.get(d)) for d in need}
    # name uses garment_name / commodity attr_key
    keys["name"] = vocab.attr_key("commodity", photo_attrs.get("name"))
    if not all(keys.values()):
        return None
    groups = _style_colours(rows)
    candidates: List[dict[str, Any]] = []
    for group in groups.values():
        r0 = group[0]
        cat_keys = {
            vocab.attr_key("commodity", r0.get("category")),
            vocab.attr_key("commodity", r0.get("name")),
        } - {""}
        if keys["name"] not in cat_keys:
            continue
        if vocab.attr_key("color", r0.get("color")) != keys["color"]:
            continue
        if vocab.attr_key("material", r0.get("material")) != keys["material"]:
            continue
        if vocab.attr_key("pattern", r0.get("pattern")) != keys["pattern"]:
            continue
        candidates.append(r0)
    if len(candidates) != 1:
        return None
    return _overwrite(candidates[0], brand_display, "attributes", size_from_catalog=False)


def _overwrite(row: dict[str, Any], brand: str, how: str, *, size_from_catalog: bool) -> Dict[str, Any]:
    name = vocab.canonical("garment_name", row.get("category") or row.get("name") or "")
    color = vocab.canonical("color", row.get("color") or "")
    out: Dict[str, Any] = {
        "catalog_match": how,
        "brand_sku": row.get("sku") if how in ("sku", "barcode") else None,
        "brand_style_code": row.get("style_code"),
        "name": name,
        "color": color,
        "brand": brand,
        "material": str(row.get("material") or "").strip() or None,
        "pattern": str(row.get("pattern") or "").strip() or None,
        "gender": str(row.get("gender") or "").strip() or None,
        "force_extracted": how in ("sku", "barcode"),
    }
    if how in ("sku", "barcode"):
        out["confidence"] = 1.0
    if size_from_catalog and row.get("size"):
        out["size"] = str(row["size"]).strip().upper()
    return out


def apply_to_row(row: Dict[str, Any], overwrite: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate a commodities row with catalog overwrite fields."""
    for k in ("name", "color", "brand", "material", "pattern", "gender"):
        if overwrite.get(k):
            row[k] = overwrite[k]
    if overwrite.get("size"):
        row["size"] = overwrite["size"]
    row["brand_sku"] = overwrite.get("brand_sku")
    row["brand_style_code"] = overwrite.get("brand_style_code")
    row["catalog_match"] = overwrite.get("catalog_match")
    if overwrite.get("confidence") is not None:
        row["confidence_score"] = overwrite["confidence"]
    if overwrite.get("force_extracted"):
        row["image_status"] = "extracted"
    reason = row.get("reason") or ""
    tag = f"Matched brand catalog ({overwrite.get('catalog_match')})"
    row["reason"] = (tag + ("; " + reason if reason else ""))[:300]
    return row
