"""Brand catalog: styles (products) and their colour / size variants (SKUs), barcodes and CSV import."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import uuid
from typing import Any, Optional, Sequence

import brand_campaign as brand_camp
import retail_db as db
import retail_gst as gst
import retail_valid as v
import vocab
from retail_db import RetailError, Tx
from retail_scope import Scope

log = logging.getLogger("command_center.retail_catalog")

MAX_CATALOG_IMAGE_BYTES = 10 * 1024 * 1024
_IMG_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

GST_RULES = ("apparel", "fixed")
MAX_IMPORT_ROWS = 20_000
MAX_IMPORT_ERRORS = 50
IMPORT_REQUIRED = ("style_code", "name", "category", "hsn_code", "color", "size", "mrp")
IMPORT_OPTIONAL = ("gender", "material", "pattern", "season", "gst_rule", "gst_rate", "cost", "sku", "barcode")

_PRODUCT_COLS = ("product_id, style_code, name, category, gender, material, pattern, season, hsn_code, gst_rule, "
                 "gst_rate_bp, active, created_at, updated_at")
_VARIANT_COLS = ("v.variant_id, v.product_id, v.sku, v.barcode, v.color, v.size, v.mrp_paise, v.avg_cost_paise, "
                 "v.active")


def ean13_check(body12: str) -> str:
    s = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(body12))
    return str((10 - s % 10) % 10)


def _new_barcode(t: Tx, brand_key: str) -> str:
    """In-store EAN-13 (prefix 20) from the brand's barcode sequence, skipping any already in use."""
    for _ in range(20):
        n = db.next_number(t, brand_key, "barcode", "")
        body = "20" + f"{n:010d}"
        code = body + ean13_check(body)
        if not t.one("SELECT 1 AS x FROM retail_variants WHERE brand_key = %s AND barcode = %s", (brand_key, code)):
            return code
    raise RetailError("Could not generate a free barcode. Enter one instead.", 409)


def _sku_for(style: str, color: str, size: str) -> str:
    part = re.sub(r"[^A-Z0-9]", "", color.upper())[:6] or "C"
    return v.sku(f"{style}-{part}-{size}"[:48])


def _unique_sku(t: Tx, brand_key: str, base: str, taken: set[str]) -> str:
    cand, n = base, 2
    while cand in taken or t.one("SELECT 1 AS x FROM retail_variants WHERE brand_key = %s AND sku = %s",
                                 (brand_key, cand)):
        suffix = f"-{n}"
        cand, n = base[: 48 - len(suffix)] + suffix, n + 1
    taken.add(cand)
    return cand


def _gst_fields(rule: Any, rate: Any) -> tuple[str, Optional[int]]:
    r = str(rule or "apparel").strip().lower()
    if r not in GST_RULES:
        raise RetailError("GST rule must be apparel (5% up to ₹2,500 a piece, 18% above) or fixed.")
    if r == "apparel":
        return r, None
    if rate in (None, ""):
        raise RetailError("Enter the fixed GST rate, e.g. 5 or 18.")
    bp = v.percent_bp(rate, "GST rate")
    if bp not in gst.ALLOWED_FIXED_BP:
        raise RetailError("Fixed GST rate must be one of 0, 2.5, 3, 5, 12, 18, 28 or 40%.")
    return r, bp


def _product_fields(data: dict[str, Any]) -> dict[str, Any]:
    hsn = str(data.get("hsn_code") or "").strip()
    if not gst.valid_hsn(hsn):
        raise RetailError("HSN code must be 4, 6 or 8 digits (apparel: chapter 61 knitted, 62 woven).")
    rule, rate = _gst_fields(data.get("gst_rule"), data.get("gst_rate"))
    return {
        "name": v.text(data.get("name"), "Product name", hi=120),
        "category": v.text(data.get("category"), "Category", hi=40),
        "gender": v.text(data.get("gender"), "Gender", hi=20, required=False),
        "material": v.text(data.get("material"), "Material", hi=60, required=False),
        "pattern": v.text(data.get("pattern"), "Pattern", hi=40, required=False),
        "season": v.text(data.get("season"), "Season", hi=20, required=False),
        "hsn_code": hsn,
        "gst_rule": rule,
        "gst_rate_bp": rate,
    }


def _variant_specs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """variants: [{color, size, mrp, cost?, sku?, barcode?}], or colors x sizes with one mrp."""
    raw = data.get("variants")
    if not raw:
        colors = [c for c in (data.get("colors") or []) if str(c).strip()]
        sizes = [s for s in (data.get("sizes") or []) if str(s).strip()]
        if not colors or not sizes:
            raise RetailError("Add at least one colour and one size.")
        raw = [{"color": c, "size": s, "mrp": data.get("mrp"), "cost": data.get("cost")} for c in colors for s in sizes]
    if not isinstance(raw, list) or len(raw) > 400:
        raise RetailError("A style can have up to 400 variants.")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise RetailError("Each variant must be an object.")
        color = v.text(item.get("color"), "Colour", hi=40)
        size = v.size(item.get("size"))
        key = (color.lower(), size)
        if key in seen:
            raise RetailError(f"{color} / {size} is listed twice.")
        seen.add(key)
        out.append({
            "color": color, "size": size,
            "mrp_paise": v.money(item.get("mrp"), "MRP", lo_paise=100),
            "cost_paise": v.money(item.get("cost"), "Cost") if item.get("cost") not in (None, "") else 0,
            "sku": v.sku(item["sku"]) if item.get("sku") else None,
            "barcode": v.barcode(item.get("barcode")),
        })
    return out


def _insert_variants(t: Tx, scope: Scope, product_id: str, style: str, specs: Sequence[dict[str, Any]]) -> list[str]:
    ids, taken = [], set()
    for s in specs:
        sku = s["sku"] or _unique_sku(t, scope.brand_key, _sku_for(style, s["color"], s["size"]), taken)
        if s["sku"] and t.one("SELECT 1 AS x FROM retail_variants WHERE brand_key = %s AND sku = %s",
                              (scope.brand_key, sku)):
            raise RetailError(f"SKU {sku} already exists.", 409)
        code = s["barcode"]
        if code and t.one("SELECT 1 AS x FROM retail_variants WHERE brand_key = %s AND barcode = %s",
                          (scope.brand_key, code)):
            raise RetailError(f"Barcode {code} is already on another SKU.", 409)
        code = code or _new_barcode(t, scope.brand_key)
        vid = db.new_id()
        now = db.now_iso()
        t.run(
            "INSERT INTO retail_variants (variant_id, product_id, brand_key, sku, barcode, color, size, mrp_paise, "
            "avg_cost_paise, active, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
            (vid, product_id, scope.brand_key, sku, code, s["color"], s["size"], s["mrp_paise"], s["cost_paise"],
             now, now),
        )
        ids.append(vid)
    return ids


def create_product(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("catalog.write")
    style = v.style_code(data.get("style_code"))
    fields = _product_fields(data)
    specs = _variant_specs(data)
    with db.tx() as t:
        if t.one("SELECT 1 AS x FROM retail_products WHERE brand_key = %s AND style_code = %s",
                 (scope.brand_key, style)):
            raise RetailError(f"Style {style} already exists. Add variants to it instead.", 409)
        pid, now = db.new_id(), db.now_iso()
        t.run(
            "INSERT INTO retail_products (product_id, brand_key, style_code, name, category, gender, material, "
            "pattern, season, hsn_code, gst_rule, gst_rate_bp, active, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
            (pid, scope.brand_key, style, fields["name"], fields["category"], fields["gender"], fields["material"],
             fields["pattern"], fields["season"], fields["hsn_code"], fields["gst_rule"], fields["gst_rate_bp"],
             now, now),
        )
        _insert_variants(t, scope, pid, style, specs)
        db.audit(t, scope.brand_key, scope.actor, "create", "product", pid, {"style_code": style, "variants": len(specs)})
        return _product(t, scope, pid)


def add_variants(scope: Scope, product_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("catalog.write")
    specs = _variant_specs(data)
    with db.tx() as t:
        p = _product_row(t, scope, product_id)
        existing = {(r["color"].lower(), r["size"]) for r in
                    t.all("SELECT color, size FROM retail_variants WHERE product_id = %s", (product_id,))}
        dup = [f"{s['color']} / {s['size']}" for s in specs if (s["color"].lower(), s["size"]) in existing]
        if dup:
            raise RetailError(f"Already in this style: {', '.join(dup[:5])}.", 409)
        _insert_variants(t, scope, product_id, p["style_code"], specs)
        db.audit(t, scope.brand_key, scope.actor, "add_variants", "product", product_id, {"variants": len(specs)})
        return _product(t, scope, product_id)


def update_product(scope: Scope, product_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("catalog.write")
    with db.tx() as t:
        p = _product_row(t, scope, product_id)
        merged = {**p, **{k: data[k] for k in data if k in {"name", "category", "gender", "material", "pattern",
                                                              "season", "hsn_code", "gst_rule", "gst_rate"}}}
        if "gst_rate" not in data and p.get("gst_rate_bp") is not None:
            merged["gst_rate"] = p["gst_rate_bp"] / 100
        fields = _product_fields(merged)
        active = bool(data["active"]) if "active" in data else bool(p["active"])
        t.run(
            "UPDATE retail_products SET name=%s, category=%s, gender=%s, material=%s, pattern=%s, season=%s, "
            "hsn_code=%s, gst_rule=%s, gst_rate_bp=%s, active=%s, updated_at=%s WHERE product_id=%s",
            (fields["name"], fields["category"], fields["gender"], fields["material"], fields["pattern"],
             fields["season"], fields["hsn_code"], fields["gst_rule"], fields["gst_rate_bp"], active, db.now_iso(),
             product_id),
        )
        changed = {k: fields[k] for k in fields if fields[k] != p.get(k)}
        db.audit(t, scope.brand_key, scope.actor, "update", "product", product_id, {**changed, "active": active})
        return _product(t, scope, product_id)


def update_variant(scope: Scope, variant_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scope.require("catalog.write")
    with db.tx() as t:
        row = t.one("SELECT variant_id, product_id, mrp_paise, barcode, active FROM retail_variants "
                    "WHERE variant_id = %s AND brand_key = %s", (variant_id, scope.brand_key))
        if not row:
            raise RetailError("SKU not found.", 404)
        mrp = v.money(data["mrp"], "MRP", lo_paise=100) if "mrp" in data else int(row["mrp_paise"])
        code = v.barcode(data.get("barcode")) if "barcode" in data else row["barcode"]
        if not code:
            raise RetailError("Barcode can't be blank.")
        if code != row["barcode"] and t.one(
                "SELECT 1 AS x FROM retail_variants WHERE brand_key = %s AND barcode = %s", (scope.brand_key, code)):
            raise RetailError(f"Barcode {code} is already on another SKU.", 409)
        active = bool(data["active"]) if "active" in data else bool(row["active"])
        t.run("UPDATE retail_variants SET mrp_paise=%s, barcode=%s, active=%s, updated_at=%s WHERE variant_id=%s",
              (mrp, code, active, db.now_iso(), variant_id))
        db.audit(t, scope.brand_key, scope.actor, "update", "variant", variant_id,
                 {"mrp_paise": [int(row["mrp_paise"]), mrp], "barcode": code, "active": active})
        return _product(t, scope, row["product_id"])


def _product_row(t: Tx, scope: Scope, product_id: str) -> dict[str, Any]:
    p = t.one(f"SELECT {_PRODUCT_COLS} FROM retail_products WHERE product_id = %s AND brand_key = %s",
              (product_id, scope.brand_key))
    if not p:
        raise RetailError("Product not found.", 404)
    return p


def _variant_public(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "variant_id": r["variant_id"], "product_id": r["product_id"], "sku": r["sku"], "barcode": r["barcode"],
        "color": r["color"], "size": r["size"], "mrp": gst.rupees(r["mrp_paise"]),
        "avg_cost": gst.rupees(r["avg_cost_paise"]), "active": bool(r["active"]),
    }


def _images_for(t: Tx, product_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """product_id -> [{color, color_key, gcs_bucket, gcs_object_key}] (no signed URL yet)."""
    out: dict[str, list[dict[str, Any]]] = {pid: [] for pid in product_ids}
    if not product_ids:
        return out
    marks = ",".join(["%s"] * len(product_ids))
    for r in t.all(
        f"SELECT product_id, color, color_key, gcs_bucket, gcs_object_key FROM retail_product_images "
        f"WHERE product_id IN ({marks}) ORDER BY color",
        list(product_ids),
    ):
        out.setdefault(r["product_id"], []).append({
            "color": r["color"], "color_key": r["color_key"],
            "gcs_bucket": r["gcs_bucket"], "gcs_object_key": r["gcs_object_key"],
        })
    return out


def _sign_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach signed image_url; drop GCS keys from the public payload."""
    if not images:
        return []
    try:
        from style_assistant import attach_image_urls
        attach_image_urls(images)
    except Exception:
        log.exception("catalog image sign failed")
        for im in images:
            im.setdefault("image_url", None)
    return [{
        "color": im["color"], "color_key": im["color_key"],
        "image_url": im.get("image_url"), "has_image": True,
    } for im in images]


def _product_public(p: dict[str, Any], variants: list[dict[str, Any]],
                    images: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    signed = _sign_images(list(images or []))
    by_key = {im["color_key"]: im for im in signed}
    for var in variants:
        im = by_key.get(vocab.value_key(var.get("color")))
        var["image_url"] = im["image_url"] if im else None
        var["has_image"] = bool(im)
    return {
        "product_id": p["product_id"], "style_code": p["style_code"], "name": p["name"], "category": p["category"],
        "gender": p["gender"], "material": p["material"], "pattern": p["pattern"], "season": p["season"],
        "hsn_code": p["hsn_code"], "gst_rule": p["gst_rule"],
        "gst_rate": (p["gst_rate_bp"] / 100) if p.get("gst_rate_bp") is not None else None,
        "active": bool(p["active"]), "variants": variants, "images": signed,
    }


def _product(t: Tx, scope: Scope, product_id: str) -> dict[str, Any]:
    p = _product_row(t, scope, product_id)
    vs = t.all(f"SELECT {_VARIANT_COLS} FROM retail_variants v WHERE v.product_id = %s ORDER BY v.color, v.size",
               (product_id,))
    units = {r["variant_id"]: int(r["units"] or 0) for r in t.all(
        "SELECT variant_id, SUM(on_hand) AS units FROM retail_stock WHERE brand_key = %s AND variant_id IN "
        "(SELECT variant_id FROM retail_variants WHERE product_id = %s) GROUP BY variant_id",
        (scope.brand_key, product_id))}
    imgs = _images_for(t, [product_id]).get(product_id, [])
    return _product_public(p, [{**_variant_public(r), "units": units.get(r["variant_id"], 0)} for r in vs], imgs)


def get_product(scope: Scope, product_id: str) -> dict[str, Any]:
    scope.require("read")
    with db.tx() as t:
        return _product(t, scope, product_id)


def list_products(scope: Scope, *, q: str = "", category: str = "", active: Optional[bool] = None,
                  page: int = 1, size: int = 50) -> dict[str, Any]:
    scope.require("read")
    page, size = v.page(page, size)
    where, params = ["p.brand_key = %s"], [scope.brand_key]
    if q.strip():
        like = "%" + q.strip().lower() + "%"
        where.append("(LOWER(p.style_code) LIKE %s OR LOWER(p.name) LIKE %s OR p.product_id IN "
                     "(SELECT product_id FROM retail_variants WHERE brand_key = %s AND "
                     "(LOWER(sku) LIKE %s OR barcode = %s)))")
        params += [like, like, scope.brand_key, like, q.strip()]
    if category.strip():
        where.append("LOWER(p.category) = %s")
        params.append(category.strip().lower())
    if active is not None:
        where.append("p.active = %s")
        params.append(active)
    cond = " AND ".join(where)
    with db.tx() as t:
        total = int(t.scalar(f"SELECT COUNT(*) AS n FROM retail_products p WHERE {cond}", params) or 0)
        rows = t.all(f"SELECT {', '.join('p.' + c.strip() for c in _PRODUCT_COLS.split(','))} FROM retail_products p "
                     f"WHERE {cond} ORDER BY p.style_code LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
        ids = [r["product_id"] for r in rows]
        variants: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        if ids:
            marks = ",".join(["%s"] * len(ids))
            units = {r["variant_id"]: int(r["units"] or 0) for r in t.all(
                f"SELECT s.variant_id, SUM(s.on_hand) AS units FROM retail_stock s JOIN retail_variants v "
                f"ON v.variant_id = s.variant_id WHERE v.product_id IN ({marks}) GROUP BY s.variant_id", ids)}
            for r in t.all(f"SELECT {_VARIANT_COLS} FROM retail_variants v WHERE v.product_id IN ({marks}) "
                           f"ORDER BY v.color, v.size", ids):
                variants[r["product_id"]].append({**_variant_public(r), "units": units.get(r["variant_id"], 0)})
        cats = [r["category"] for r in t.all(
            "SELECT DISTINCT category FROM retail_products WHERE brand_key = %s ORDER BY category", (scope.brand_key,))]
        imgs = _images_for(t, ids)
    return {"products": [_product_public(r, variants[r["product_id"]], imgs.get(r["product_id"])) for r in rows],
            "total": total, "page": page, "size": size, "categories": cats}


def variant_rows(t: Tx, brand_key: str, variant_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """variant_id -> variant + product fields needed to price, move or describe it."""
    ids = list(dict.fromkeys(variant_ids))
    if not ids:
        return {}
    marks = ",".join(["%s"] * len(ids))
    rows = t.all(
        f"SELECT v.variant_id, v.sku, v.barcode, v.color, v.size, v.mrp_paise, v.avg_cost_paise, v.active, "
        f"p.product_id, p.style_code, p.name, p.category, p.hsn_code, p.gst_rule, p.gst_rate_bp, p.active AS p_active "
        f"FROM retail_variants v JOIN retail_products p ON p.product_id = v.product_id "
        f"WHERE v.brand_key = %s AND v.variant_id IN ({marks})",
        [brand_key] + ids,
    )
    return {r["variant_id"]: r for r in rows}


def require_variants(t: Tx, brand_key: str, variant_ids: Sequence[str], *, sellable: bool = False
                     ) -> dict[str, dict[str, Any]]:
    rows = variant_rows(t, brand_key, variant_ids)
    missing = [i for i in variant_ids if i not in rows]
    if missing:
        raise RetailError("One or more SKUs were not found in your catalog.", 404)
    if sellable:
        off = [r["sku"] for r in rows.values() if not r["active"] or not r["p_active"]]
        if off:
            raise RetailError(f"Not on sale (inactive): {', '.join(off[:5])}.")
    return rows


def search_variants(scope: Scope, *, q: str, location_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    """Barcode / SKU exact match first, then style, name or SKU contains. With location_id, adds sellable units."""
    scope.require("read")
    q = (q or "").strip()
    if not q:
        return []
    like = "%" + q.lower() + "%"
    with db.tx() as t:
        rows = t.all(
            "SELECT v.variant_id, v.sku, v.barcode, v.color, v.size, v.mrp_paise, v.active, p.name, p.style_code, "
            "p.category, p.active AS p_active, "
            "CASE WHEN v.barcode = %s OR UPPER(v.sku) = %s THEN 0 ELSE 1 END AS exact "
            "FROM retail_variants v JOIN retail_products p ON p.product_id = v.product_id "
            "WHERE v.brand_key = %s AND (v.barcode = %s OR UPPER(v.sku) = %s OR LOWER(v.sku) LIKE %s "
            "OR LOWER(p.style_code) LIKE %s OR LOWER(p.name) LIKE %s) "
            "ORDER BY exact, p.style_code, v.color, v.size LIMIT %s",
            (q, q.upper(), scope.brand_key, q, q.upper(), like, like, like, max(1, min(int(limit), 50))),
        )
        stock: dict[str, int] = {}
        if location_id and rows:
            marks = ",".join(["%s"] * len(rows))
            stock = {r["variant_id"]: int(r["units"] or 0) for r in t.all(
                f"SELECT variant_id, SUM(on_hand) AS units FROM retail_stock WHERE location_id = %s AND bin_code <> %s "
                f"AND variant_id IN ({marks}) GROUP BY variant_id",
                [location_id, v.DAMAGED_BIN] + [r["variant_id"] for r in rows])}
    return [{
        "variant_id": r["variant_id"], "sku": r["sku"], "barcode": r["barcode"], "name": r["name"],
        "style_code": r["style_code"], "category": r["category"], "color": r["color"], "size": r["size"],
        "mrp": gst.rupees(r["mrp_paise"]), "on_sale": bool(r["active"]) and bool(r["p_active"]),
        "exact": r["exact"] == 0, **({"available": stock.get(r["variant_id"], 0)} if location_id else {}),
    } for r in rows]


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def parse_csv(text: str, *, required: Sequence[str], optional: Sequence[str], max_rows: int) -> list[dict[str, str]]:
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    headers = {_norm_header(h): h for h in (reader.fieldnames or [])}
    missing = [c for c in required if c not in headers]
    if missing:
        raise RetailError(f"The CSV is missing columns: {', '.join(missing)}.")
    rows = []
    for raw in reader:
        rows.append({k: (raw.get(h) or "").strip() for k, h in headers.items() if k in set(required) | set(optional)})
        if len(rows) > max_rows:
            raise RetailError(f"At most {max_rows:,} rows per file. Split the file and import each part.")
    if not rows:
        raise RetailError("The CSV has no rows.")
    return rows


def import_catalog_csv(scope: Scope, text: str) -> dict[str, Any]:
    """Upsert styles and SKUs. Every row is checked first; if any row is wrong nothing is imported."""
    scope.require("catalog.write")
    rows = parse_csv(text, required=IMPORT_REQUIRED, optional=IMPORT_OPTIONAL, max_rows=MAX_IMPORT_ROWS)
    errors: list[dict[str, Any]] = []
    styles: dict[str, dict[str, Any]] = {}
    specs: list[tuple[int, str, dict[str, Any]]] = []
    seen_sku: dict[str, int] = {}
    seen_combo: dict[tuple[str, str, str], int] = {}
    for i, r in enumerate(rows, start=2):
        try:
            style = v.style_code(r.get("style_code"))
            fields = _product_fields(r)
            if style in styles and (styles[style]["name"], styles[style]["category"]) != (fields["name"], fields["category"]):
                raise RetailError(f"Style {style} has a different name or category on another row.")
            styles.setdefault(style, fields)
            spec = _variant_specs({"variants": [{k: r.get(k) for k in ("color", "size", "mrp", "cost", "sku", "barcode")}]})[0]
            combo = (style, spec["color"].lower(), spec["size"])
            if combo in seen_combo:
                raise RetailError(f"Same style, colour and size as row {seen_combo[combo]}.")
            seen_combo[combo] = i
            if spec["sku"]:
                if spec["sku"] in seen_sku:
                    raise RetailError(f"SKU {spec['sku']} is also on row {seen_sku[spec['sku']]}.")
                seen_sku[spec["sku"]] = i
            specs.append((i, style, spec))
        except RetailError as exc:
            errors.append({"row": i, "error": str(exc)})
            if len(errors) >= MAX_IMPORT_ERRORS:
                break
    if errors:
        raise RetailError(f"{len(errors)}{'+' if len(errors) >= MAX_IMPORT_ERRORS else ''} rows need fixing. "
                          "Nothing was imported.", 422, errors)

    created_p = updated_p = created_v = updated_v = 0
    with db.tx() as t:
        prods = {r["style_code"]: r for r in t.all(
            f"SELECT {_PRODUCT_COLS} FROM retail_products WHERE brand_key = %s", (scope.brand_key,))}
        vars_ = t.all("SELECT variant_id, product_id, sku, barcode, color, size, mrp_paise, avg_cost_paise "
                      "FROM retail_variants WHERE brand_key = %s", (scope.brand_key,))
        by_sku = {r["sku"]: r for r in vars_}
        by_combo = {(r["product_id"], r["color"].lower(), r["size"]): r for r in vars_}
        by_code = {r["barcode"]: r for r in vars_}
        now = db.now_iso()
        for style, f in styles.items():
            p = prods.get(style)
            if p is None:
                pid = db.new_id()
                t.run("INSERT INTO retail_products (product_id, brand_key, style_code, name, category, gender, "
                      "material, pattern, season, hsn_code, gst_rule, gst_rate_bp, active, created_at, updated_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
                      (pid, scope.brand_key, style, f["name"], f["category"], f["gender"], f["material"], f["pattern"],
                       f["season"], f["hsn_code"], f["gst_rule"], f["gst_rate_bp"], now, now))
                prods[style] = {"product_id": pid}
                created_p += 1
            else:
                t.run("UPDATE retail_products SET name=%s, category=%s, gender=COALESCE(%s, gender), "
                      "material=COALESCE(%s, material), pattern=COALESCE(%s, pattern), season=COALESCE(%s, season), "
                      "hsn_code=%s, gst_rule=%s, gst_rate_bp=%s, updated_at=%s WHERE product_id=%s",
                      (f["name"], f["category"], f["gender"], f["material"], f["pattern"], f["season"], f["hsn_code"],
                       f["gst_rule"], f["gst_rate_bp"], now, p["product_id"]))
                updated_p += 1
        taken: set[str] = set(by_sku)
        for row_no, style, s in specs:
            pid = prods[style]["product_id"]
            cur = by_sku.get(s["sku"]) if s["sku"] else by_combo.get((pid, s["color"].lower(), s["size"]))
            if cur is not None and cur["product_id"] != pid:
                raise RetailError(f"Row {row_no}: SKU {cur['sku']} belongs to another style.", 422,
                                  [{"row": row_no, "error": "SKU belongs to another style"}])
            if s["barcode"] and s["barcode"] in by_code and (cur is None or by_code[s["barcode"]]["variant_id"] != cur["variant_id"]):
                raise RetailError(f"Row {row_no}: barcode {s['barcode']} is already on SKU {by_code[s['barcode']]['sku']}.",
                                  422, [{"row": row_no, "error": "barcode already used"}])
            if cur is None:
                sku = s["sku"] or _unique_sku(t, scope.brand_key, _sku_for(style, s["color"], s["size"]), taken)
                taken.add(sku)
                code = s["barcode"] or _new_barcode(t, scope.brand_key)
                vid = db.new_id()
                t.run("INSERT INTO retail_variants (variant_id, product_id, brand_key, sku, barcode, color, size, "
                      "mrp_paise, avg_cost_paise, active, created_at, updated_at) VALUES "
                      "(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)",
                      (vid, pid, scope.brand_key, sku, code, s["color"], s["size"], s["mrp_paise"], s["cost_paise"],
                       now, now))
                by_code[code] = {"variant_id": vid, "sku": sku}
                created_v += 1
            else:
                cost = int(cur["avg_cost_paise"]) or s["cost_paise"]
                code = s["barcode"] or cur["barcode"]
                t.run("UPDATE retail_variants SET mrp_paise=%s, barcode=%s, avg_cost_paise=%s, updated_at=%s "
                      "WHERE variant_id=%s", (s["mrp_paise"], code, cost, now, cur["variant_id"]))
                updated_v += 1
        db.audit(t, scope.brand_key, scope.actor, "import", "catalog", None,
                 {"rows": len(specs), "styles_created": created_p, "skus_created": created_v, "skus_updated": updated_v})
    log.info("brand=%s actor=%s catalog import rows=%s", scope.brand_key, scope.actor, len(specs))
    return {"rows": len(specs), "styles_created": created_p, "styles_updated": updated_p,
            "skus_created": created_v, "skus_updated": updated_v}


# ---------------------------------------------------------------------------
# Style × colour photos (Ads Lab ranking from the catalog)
# ---------------------------------------------------------------------------


def _resolve_color(t: Tx, scope: Scope, product_id: str, color: str) -> tuple[str, str]:
    """Return (display color, color_key) for a colour that exists on the product."""
    color = (color or "").strip()
    if not color:
        raise RetailError("Choose a colour.")
    key = vocab.value_key(color)
    if not key:
        raise RetailError("Choose a colour.")
    rows = t.all(
        "SELECT DISTINCT color FROM retail_variants WHERE product_id = %s AND brand_key = %s",
        (product_id, scope.brand_key),
    )
    for r in rows:
        if vocab.value_key(r["color"]) == key:
            return r["color"], key
    raise RetailError(f"No SKU in colour “{color}” on this style.", 404)


def set_product_image(scope: Scope, product_id: str, *, color: str, data: bytes,
                      content_type: str, filename: str = "sku.jpg") -> dict[str, Any]:
    """Upload or replace the photo for one style × colour. Needs catalog.write and GCS."""
    scope.require("catalog.write")
    if not content_type or not content_type.startswith("image/"):
        raise RetailError("Upload a JPEG, PNG or WebP image.")
    ext = _IMG_EXT.get(content_type)
    if not ext:
        raise RetailError("Upload a JPEG, PNG or WebP image.")
    if len(data) > MAX_CATALOG_IMAGE_BYTES:
        raise RetailError(f"Image too large (max {MAX_CATALOG_IMAGE_BYTES // (1024 * 1024)} MB).")
    bucket = (os.getenv("GCS_IMAGE_BUCKET") or "").strip()
    if not bucket:
        raise RetailError("Catalog photos are not configured (GCS_IMAGE_BUCKET).", 503)

    with db.tx() as t:
        p = _product_row(t, scope, product_id)
        style_code = p["style_code"]
        display, color_key = _resolve_color(t, scope, product_id, color)
        object_key = f"catalog/{brand_camp.brand_slug(scope.brand_name)}/{product_id}/{uuid.uuid4().hex}.{ext}"
        try:
            from google.cloud import storage as gcs
            gcs.Client().bucket(bucket).blob(object_key).upload_from_string(data, content_type=content_type)
        except Exception as exc:
            log.exception("catalog image upload failed product=%s", product_id)
            raise RetailError("Could not store the photo. Try again.", 503) from exc
        now = db.now_iso()
        existing = t.one(
            "SELECT gcs_object_key FROM retail_product_images WHERE product_id = %s AND color_key = %s",
            (product_id, color_key),
        )
        if existing:
            t.run(
                "UPDATE retail_product_images SET color = %s, gcs_bucket = %s, gcs_object_key = %s, created_at = %s "
                "WHERE product_id = %s AND color_key = %s",
                (display, bucket, object_key, now, product_id, color_key),
            )
        else:
            t.run(
                "INSERT INTO retail_product_images (product_id, brand_key, color_key, color, gcs_bucket, "
                "gcs_object_key, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (product_id, scope.brand_key, color_key, display, bucket, object_key, now),
            )
        db.audit(t, scope.brand_key, scope.actor, "set_image", "product", product_id,
                 {"style_code": style_code, "color": display})
    out = _sign_images([{"color": display, "color_key": color_key, "gcs_bucket": bucket,
                         "gcs_object_key": object_key}])[0]
    return {"product_id": product_id, "style_code": style_code, **out}


def delete_product_image(scope: Scope, product_id: str, *, color: str) -> dict[str, Any]:
    scope.require("catalog.write")
    with db.tx() as t:
        p = _product_row(t, scope, product_id)
        style_code = p["style_code"]
        display, color_key = _resolve_color(t, scope, product_id, color)
        row = t.one(
            "SELECT gcs_bucket, gcs_object_key FROM retail_product_images "
            "WHERE product_id = %s AND color_key = %s",
            (product_id, color_key),
        )
        if not row:
            raise RetailError(f"No photo for {style_code} · {display}.", 404)
        t.run("DELETE FROM retail_product_images WHERE product_id = %s AND color_key = %s",
              (product_id, color_key))
        db.audit(t, scope.brand_key, scope.actor, "delete_image", "product", product_id,
                 {"style_code": style_code, "color": display})
    return {"ok": True, "style_code": style_code, "color": display}


def list_style_colours(scope: Scope, *, q: str = "", category: str = "",
                       with_image_only: bool = False) -> dict[str, Any]:
    """Style × colour groups for Ads Lab ranking (sizes listed, not ranked)."""
    scope.require("read")
    where = ["p.brand_key = %s", "p.active = TRUE", "v.active = TRUE"]
    params: list[Any] = [scope.brand_key]
    if q.strip():
        like = "%" + q.strip().lower() + "%"
        where.append("(LOWER(p.style_code) LIKE %s OR LOWER(p.name) LIKE %s OR LOWER(v.color) LIKE %s "
                     "OR LOWER(v.sku) LIKE %s)")
        params += [like, like, like, like]
    if category.strip():
        where.append("LOWER(p.category) = %s")
        params.append(category.strip().lower())
    cond = " AND ".join(where)
    with db.tx() as t:
        rows = t.all(
            f"SELECT p.product_id, p.style_code, p.name, p.category, p.material, p.pattern, p.gender, "
            f"v.color, v.size, v.mrp_paise, v.variant_id, "
            f"COALESCE((SELECT SUM(s.on_hand) FROM retail_stock s WHERE s.variant_id = v.variant_id "
            f"AND s.bin_code <> %s), 0) AS units "
            f"FROM retail_variants v JOIN retail_products p ON p.product_id = v.product_id "
            f"WHERE {cond} ORDER BY p.style_code, v.color, v.size",
            [v.DAMAGED_BIN] + params,
        )
        imgs = _images_for(t, list({r["product_id"] for r in rows}))
        cats = [r["category"] for r in t.all(
            "SELECT DISTINCT category FROM retail_products WHERE brand_key = %s AND active = TRUE ORDER BY category",
            (scope.brand_key,))]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["product_id"], vocab.value_key(r["color"]))
        g = groups.get(key)
        if g is None:
            g = {
                "product_id": r["product_id"], "style_code": r["style_code"], "name": r["name"],
                "category": r["category"], "material": r["material"], "pattern": r["pattern"],
                "gender": r["gender"], "color": r["color"], "color_key": key[1],
                "sizes": [], "units": 0, "mrp_min": None, "mrp_max": None,
            }
            groups[key] = g
        if r["size"] not in g["sizes"]:
            g["sizes"].append(r["size"])
        g["units"] += int(r["units"] or 0)
        mrp = gst.rupees(r["mrp_paise"])
        g["mrp_min"] = mrp if g["mrp_min"] is None else min(g["mrp_min"], mrp)
        g["mrp_max"] = mrp if g["mrp_max"] is None else max(g["mrp_max"], mrp)
    items = []
    for (pid, ckey), g in groups.items():
        im_list = [im for im in imgs.get(pid, []) if im["color_key"] == ckey]
        signed = _sign_images(im_list)
        has = bool(signed)
        if with_image_only and not has:
            continue
        items.append({
            **g,
            "has_image": has,
            "image_url": signed[0]["image_url"] if signed else None,
            "mrp": g["mrp_min"] if g["mrp_min"] == g["mrp_max"] else None,
            "mrp_range": [g["mrp_min"], g["mrp_max"]],
        })
    items.sort(key=lambda x: (x["style_code"], x["color"]))
    return {"items": items, "total": len(items), "categories": cats,
            "brand_name": scope.brand_name, "has_catalog": bool(items)}
