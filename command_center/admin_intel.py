"""Admin-only intelligence for the Admin page (customers, garments, brands, campaigns).

Real data only: BigQuery customers / orders / commodities and Cloud SQL brand campaigns.
Person-level data (names, phones) is for admin sessions only; phones are masked unless an
admin explicitly reveals one (logged by the route). Never exposed to AURA or brand logins.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import bq_freshness
import pin_garments
import vocab

log = logging.getLogger("command_center.admin_intel")

PAGE_MAX = 100
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL"]
QUALIFY_MIN_ORDERS = 3
QUALIFY_RECENT_DAYS = 60
STATUSES = ("Active", "Scheduled", "Completed", "Draft", "Discarded")


class IntelUnavailable(RuntimeError):
    pass


def cache_sec() -> float:
    try:
        return max(0.0, float(os.getenv("ADMIN_INTEL_CACHE_SEC", "600")))
    except ValueError:
        return 600.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

# Test hooks: return (customers, orders, commodities) / campaign rows.
_dataset_loader: Optional[Callable[[], tuple[list[dict], list[dict], list[dict]]]] = None
_campaign_loader: Optional[Callable[[], list[dict]]] = None

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _bq_loader() -> tuple[list[dict], list[dict], list[dict]]:
    import aggregate_metrics as agg

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        log.error("admin intel: GOOGLE_CLOUD_PROJECT not set")
        raise IntelUnavailable("admin data is unavailable")
    return agg.load_from_bq(project, os.getenv("BQ_DATASET", "suffai_ops"), include_repeats=True)


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)


def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19].replace("Z", "")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


def digits(v: Any) -> str:
    return re.sub(r"\D", "", _s(v))


def mask_phone(v: Any) -> str:
    d = digits(v)[-10:]
    if len(d) < 6:
        return "xxxx" if d else "—"
    return f"+91 {d[:2]}xxx xx{d[-3:]}"


def menu_of(gender: Any) -> str:
    g = _s(gender).lower()
    if g in {"m", "male", "men", "man", "mens"}:
        return "Men"
    if g in {"f", "female", "women", "woman", "womens"}:
        return "Women"
    if g in {"kid", "kids", "boy", "girl", "boys", "girls", "child"}:
        return "Kids"
    if g in {"u", "unisex"}:
        return "Unisex"
    return "Unspecified"


def age_group(age: Any) -> str:
    try:
        a = int(float(age))
    except (TypeError, ValueError):
        return "unknown"
    for hi, label in ((17, "<18"), (24, "18-24"), (34, "25-34"), (44, "35-44"), (54, "45-54")):
        if a <= hi:
            return label
    return "55+"


def _tag(spend: float, orders: int) -> tuple[str, str]:
    rev = "High" if spend > 5000 else ("Med" if spend > 2000 else "Low")
    freq = "High" if orders > 6 else ("Med" if orders > 3 else "Low")
    return rev, freq


def _build(customers: list[dict], orders: list[dict], commodities: list[dict]) -> dict[str, Any]:
    orders_by: dict[str, list[dict]] = defaultdict(list)
    max_date: Optional[date] = None
    for o in orders:
        cid = _s(o.get("customer_id"))
        if not cid:
            continue
        d = _date(o.get("scheduled_date"))
        row = {"order_id": _s(o.get("_id")), "date": d, "service": _s(o.get("service")) or "Unspecified",
               "clothes": int(_num(o.get("total_number_of_clothes"))), "amount": _num(o.get("amount")),
               "source": _s(o.get("source")) or "legacy", "photos": 0, "legacy_items": 0,
               "created_at": _s(o.get("created_at"))}
        orders_by[cid].append(row)
        if d and (max_date is None or d > max_date):
            max_date = d
    for rows in orders_by.values():
        rows.sort(key=lambda r: (r["date"] or date.min))

    # Every photo counts towards its order; a repeat photo of the same garment is not a new garment.
    photos_by_order: Counter = Counter()
    legacy_by_order: Counter = Counter()
    garments_by: dict[str, list[dict]] = defaultdict(list)
    garments: list[dict] = []
    # Photos linked to a similarity garment (unique_garment_id), repeats included: the observations.
    observations: list[dict] = []
    for c in commodities:
        cid = _s(c.get("customer_id"))
        has_photo = bool(_s(c.get("gcs_object_key")))
        if has_photo:
            photos_by_order[_s(c.get("order_id"))] += 1
            if pin_garments.garment_id(c):
                observations.append({"garment_id": pin_garments.garment_id(c), "customer_id": cid,
                                     "brand": _s(c.get("brand")), "object_key": _s(c.get("gcs_object_key")),
                                     "at": _s(c.get("extracted_at"))})
        elif (_s(c.get("source")) or "legacy") == "legacy":
            legacy_by_order[_s(c.get("order_id"))] += 1
        if c.get("is_repeat") is True:
            continue
        g = {
            "garment_id": _s(c.get("commodity_id")) or _s(c.get("item_id")) or _s(c.get("_id")),
            "customer_id": cid,
            "order_id": _s(c.get("order_id")),
            "commodity": _s(c.get("name")),
            "brand": _s(c.get("brand")),
            "color": _s(c.get("color")),
            "pattern": _s(c.get("pattern")),
            "size": _s(c.get("size")),
            "material": _s(c.get("material")),
            "sleeve": _s(c.get("sleeve_type")),
            "collar": _s(c.get("collar_type")),
            "menu": menu_of(c.get("gender")),
            "source": _s(c.get("source")) or "legacy",
            "image_status": _s(c.get("image_status")) or ("needs_review" if has_photo else "legacy_no_photo"),
            "catalog_match": _s(c.get("catalog_match")),
            "brand_sku": _s(c.get("brand_sku")),
            "brand_style_code": _s(c.get("brand_style_code")),
        }
        garments.append(g)
        if cid:
            garments_by[cid].append(g)
    for rows in orders_by.values():
        for o in rows:
            o["photos"] = photos_by_order.get(o["order_id"], 0)
            o["legacy_items"] = legacy_by_order.get(o["order_id"], 0)

    cust: dict[str, dict] = {}
    cutoff = (max_date - timedelta(days=QUALIFY_RECENT_DAYS)) if max_date else None
    for c in customers:
        cid = _s(c.get("_id"))
        if not cid:
            continue
        ords = orders_by.get(cid, [])
        spend = sum(o["amount"] for o in ords)
        last = next((o["date"] for o in reversed(ords) if o["date"]), None)
        rev, freq = _tag(spend, len(ords))
        cust[cid] = {
            "customer_id": cid,
            "name": _s(c.get("name")) or f"User {cid[:6]}",
            "phone_digits": digits(c.get("number"))[-10:],
            "pin_code": _s(c.get("pin_code")),
            "age": c.get("age"),
            "age_group": age_group(c.get("age")),
            "gender": _s(c.get("gender")) or "unknown",
            "rfm_segment": _s(c.get("rfm_segment")) or "Unknown",
            "orders": len(ords),
            "spend": round(spend, 2),
            "last_order": last,
            "tag": f"Rev: {rev} | Freq: {freq}",
            "qualified": len(ords) >= QUALIFY_MIN_ORDERS and bool(last and cutoff and last >= cutoff),
        }
    return {"customers": cust, "orders_by": orders_by, "garments": garments, "garments_by": garments_by,
            "observations": observations, "max_date": max_date, "loaded_at": datetime.now(timezone.utc).isoformat()}


def dataset(*, fresh: bool = False) -> dict[str, Any]:
    bq_freshness.poke()
    now = time.time()
    with _lock:
        data = _cache["data"]
        if data is not None and not fresh and now - _cache["at"] < cache_sec():
            return data
    loader = _dataset_loader or _bq_loader
    try:
        customers, orders, commodities = loader()
    except IntelUnavailable:
        raise
    except Exception as exc:
        log.exception("admin intel dataset load failed")
        raise IntelUnavailable("customer data is unavailable") from exc
    data = _build(list(customers), list(orders), list(commodities))
    with _lock:
        _cache.update(at=now, data=data)
    log.info("admin intel dataset loaded customers=%s garments=%s", len(data["customers"]), len(data["garments"]))
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lc(values: Optional[Iterable[Any]]) -> set[str]:
    return {_s(v).lower() for v in (values or []) if _s(v)}


def _shares(counter: Counter, n: int = 6) -> list[dict[str, Any]]:
    total = sum(counter.values())
    if total <= 0:
        return []
    return [{"value": k, "count": v, "share": round(v * 100 / total, 1)} for k, v in counter.most_common(n)]


def _known(v: str) -> bool:
    return bool(v) and v.lower() not in {"unknown", "none", "n/a", "na", "null", "other"}


def _size_key(k: str) -> tuple[int, float, str]:
    u = k.strip().upper()
    if u in SIZE_ORDER:
        return (0, SIZE_ORDER.index(u), u)
    try:
        return (1, float(u), u)
    except ValueError:
        return (2, 0, u)


def _page(rows: list, page: int, size: int) -> tuple[list, int, int]:
    size = max(1, min(PAGE_MAX, size))
    page = max(1, page)
    return rows[(page - 1) * size : page * size], page, size


def _options(values: Iterable[str], cap: int = 200) -> list[str]:
    return sorted({v for v in values if _known(v)}, key=str.lower)[:cap]


def short_id(cid: str) -> str:
    return (cid or "")[:8]


# ---------------------------------------------------------------------------
# Tab 1: customers
# ---------------------------------------------------------------------------


def _customer_filter(data: dict, *, q: str = "", rfm: Optional[list[str]] = None,
                     pins: Optional[list[str]] = None, brands: Optional[list[str]] = None) -> list[dict]:
    rf, pf, bf = _lc(rfm), {_s(p) for p in (pins or []) if _s(p)}, _lc(brands)
    garments_by = data["garments_by"]
    q = _s(q)
    qd = digits(q) if len(digits(q)) >= 3 and digits(q) == re.sub(r"[\s+\-()]", "", q) else ""
    if qd and (q.lstrip().startswith("+91") or len(qd) > 10) and qd.startswith("91"):
        qd = qd[2:]
    ql = q.lower()
    out = []
    for c in data["customers"].values():
        if rf and c["rfm_segment"].lower() not in rf:
            continue
        if pf and c["pin_code"] not in pf:
            continue
        if bf and not any(g["brand"].lower() in bf for g in garments_by.get(c["customer_id"], ())):
            continue
        if q:
            if qd:
                if qd not in c["phone_digits"]:
                    continue
            elif ql not in c["name"].lower():
                continue
        out.append(c)
    return out


def customer_options(data: dict) -> dict[str, list[str]]:
    cs = data["customers"].values()
    return {"rfm_segments": _options(c["rfm_segment"] for c in cs),
            "pin_codes": _options(c["pin_code"] for c in cs),
            "brands": _options(g["brand"] for g in data["garments"])}


def customers_overview(*, rfm: Optional[list[str]] = None, pins: Optional[list[str]] = None,
                       brands: Optional[list[str]] = None) -> dict[str, Any]:
    data = dataset()
    allc = list(data["customers"].values())
    cohort = _customer_filter(data, rfm=rfm, pins=pins, brands=brands)
    rfm_c = Counter(c["rfm_segment"] for c in cohort)
    ages = sorted({c["age_group"] for c in cohort}, key=lambda a: (a == "unknown", a))
    genders = sorted({c["gender"] for c in cohort})
    spread = {g: [sum(1 for c in cohort if c["gender"] == g and c["age_group"] == a) for a in ages] for g in genders}
    return {
        "total_customers": len(allc),
        "qualified_users": sum(1 for c in allc if c["qualified"]),
        "cohort_size": len(cohort),
        "cohort_qualified": sum(1 for c in cohort if c["qualified"]),
        "cohort_spend": round(sum(c["spend"] for c in cohort), 2),
        "avg_orders": round(sum(c["orders"] for c in cohort) / len(cohort), 2) if cohort else 0,
        "rfm_breakdown": _shares(rfm_c, 12),
        "age_gender": {"ages": ages, "series": spread},
        "qualified_rule": f"At least {QUALIFY_MIN_ORDERS} orders and an order in the {QUALIFY_RECENT_DAYS} days "
                          "before the latest order date",
        "options": customer_options(data),
        "as_of": data["max_date"].isoformat() if data["max_date"] else None,
    }


def _customer_row(c: dict) -> dict[str, Any]:
    return {
        "customer_id": c["customer_id"],
        "short_id": short_id(c["customer_id"]),
        "name": c["name"],
        "phone_masked": mask_phone(c["phone_digits"]),
        "has_phone": bool(c["phone_digits"]),
        "pin_code": c["pin_code"] or None,
        "rfm_segment": c["rfm_segment"],
        "tag": c["tag"],
        "orders": c["orders"],
        "spend": c["spend"],
        "last_order": c["last_order"].isoformat() if c["last_order"] else None,
        "qualified": c["qualified"],
    }


def customers_page(*, q: str = "", rfm: Optional[list[str]] = None, pins: Optional[list[str]] = None,
                   brands: Optional[list[str]] = None, qualified_only: bool = False,
                   page: int = 1, size: int = 10) -> dict[str, Any]:
    data = dataset()
    rows = _customer_filter(data, q=q, rfm=rfm, pins=pins, brands=brands)
    if qualified_only:
        rows = [c for c in rows if c["qualified"]]
    rows.sort(key=lambda c: (-c["spend"], -c["orders"], c["name"].lower()))
    chunk, page, size = _page(rows, page, size)
    return {"total": len(rows), "page": page, "size": size, "items": [_customer_row(c) for c in chunk]}


def customer_detail(customer_id: str) -> Optional[dict[str, Any]]:
    data = dataset()
    c = data["customers"].get(_s(customer_id))
    if not c:
        return None
    ords = data["orders_by"].get(c["customer_id"], [])
    garms = data["garments_by"].get(c["customer_id"], [])
    dated = [o["date"] for o in ords if o["date"]]
    months = max(1.0, ((max(dated) - min(dated)).days / 30.0)) if len(dated) > 1 else 1.0
    monthly: dict[str, dict[str, float]] = {}
    for o in ords:
        if not o["date"]:
            continue
        m = monthly.setdefault(o["date"].strftime("%Y-%m"), {"orders": 0, "spend": 0.0})
        m["orders"] += 1
        m["spend"] += o["amount"]
    cum = 0.0
    series = []
    for month in sorted(monthly):
        m = monthly[month]
        cum += m["spend"]
        series.append({"month": month, "orders": int(m["orders"]), "spend": round(m["spend"], 2),
                       "aov": round(m["spend"] / m["orders"], 2) if m["orders"] else 0,
                       "cumulative_spend": round(cum, 2)})

    def ctr(field: str) -> Counter:
        return Counter(g[field] for g in garms if _known(g[field]))

    sizes = ctr("size")
    return {
        **_customer_row(c),
        "demographics": {
            "short_id": short_id(c["customer_id"]),
            "pin_code": c["pin_code"] or None,
            "age_group": c["age_group"],
            "gender": c["gender"],
            "menus": [m["value"] for m in _shares(Counter(g["menu"] for g in garms if g["menu"] != "Unspecified"), 3)],
            "ltv": c["spend"],
            "aov": round(c["spend"] / c["orders"], 2) if c["orders"] else 0,
            "orders_per_month": round(len(ords) / months, 2) if ords else 0,
            "first_order": min(dated).isoformat() if dated else None,
            "last_order": max(dated).isoformat() if dated else None,
        },
        "top_commodities": _shares(ctr("commodity"), 4),
        "colors": _shares(ctr("color"), 5),
        "sizes": sorted(_shares(sizes, 6), key=lambda s: _size_key(s["value"])),
        "service_split": _shares(Counter(o["service"] for o in ords), 8),
        "garments": [{k: g[k] for k in ("garment_id", "commodity", "brand", "color", "pattern", "size", "material",
                                        "image_status", "catalog_match", "brand_sku", "brand_style_code")}
                     for g in garms[:50]],
        "garment_count": len(garms),
        "momentum": series,
        "recent_orders": [order_row(o) for o in ords[-10:][::-1]],
    }


def order_row(o: dict) -> dict[str, Any]:
    """Order with photo progress: received photos against total_number_of_clothes.

    An order whose items came from the CSV and that has no photos is "legacy", never pending: its photos
    will not arrive. Its first photo supersedes those items (extraction) and it counts photos from then on.
    """
    expected = max(0, int(o["clothes"]))
    legacy = bool(o.get("legacy_items")) and not o["photos"]
    pending = 0 if legacy else max(0, expected - o["photos"])
    return {"order_id": o["order_id"], "date": o["date"].isoformat() if o["date"] else None,
            "service": o["service"], "clothes": o["clothes"], "amount": round(o["amount"], 2),
            "source": o["source"], "photos_received": o["photos"], "photos_expected": expected,
            "photos_pending": pending, "legacy_items": int(o.get("legacy_items") or 0),
            "photo_state": "legacy" if legacy else ("pending" if pending else "complete")}


def unique_garments(*, pin: Optional[str] = None, brand: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
    """Admin "Unique garments" from BigQuery garment_items: photographed garments (similarity garment id).

    Observations are the garment's photos, counted at the customer's current PIN; a garment's brand is the
    one its photos were read as most often. PIN options are the current customer PINs.
    """
    data = dataset()
    customers = data["customers"]
    pin_f = _s(pin)
    brand_f = vocab.value_key(brand) if _s(brand) else ""
    by_garment: dict[str, dict[str, Any]] = {}
    for o in data["observations"]:
        owner = customers.get(o["customer_id"])
        if not owner:
            continue
        g = by_garment.setdefault(o["garment_id"], {"brands": Counter(), "obs": []})
        g["obs"].append({**o, "pin_code": owner["pin_code"]})
        if _known(o["brand"]):
            g["brands"][o["brand"]] += 1

    brands: dict[str, dict[str, Any]] = {}
    users: set[str] = set()
    items: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    all_brands: set[str] = set()
    for gid, g in by_garment.items():
        brand_name = g["brands"].most_common(1)[0][0] if g["brands"] else "unknown"
        all_brands.add(brand_name)
        if brand_f and vocab.value_key(brand_name) != brand_f:
            continue
        obs = [o for o in g["obs"] if not pin_f or o["pin_code"] == pin_f]
        if not obs:
            continue
        kept.extend(obs)
        b = brands.setdefault(brand_name, {"brand": brand_name, "unique_garments": 0, "observations": 0})
        b["unique_garments"] += 1
        b["observations"] += len(obs)
        users.update(o["customer_id"] for o in obs)
        first = min(obs, key=lambda o: o["at"] or "~")
        items.append({"garment_id": gid, "brand_name": brand_name, "gcs_object_key": first["object_key"],
                      "observation_count_total": len(obs), "created_at": first["at"] or None,
                      "_last": max(o["at"] for o in obs)})
    items.sort(key=lambda r: r.pop("_last") or "", reverse=True)
    # Same per-PIN count as the Ads Lab tile and the compare-pins candidates (pin_garments).
    per_pin = pin_garments.by_pin(((o["customer_id"], o["garment_id"]) for o in kept),
                                  {cid: c["pin_code"] for cid, c in customers.items()})
    pins = [{"pin_code": p, "unique_garments": len(c), "observations": sum(c.values())} for p, c in per_pin.items()]
    return {
        "source": "bigquery",
        "kpis": {"unique_garments": len(items), "total_observations": len(kept), "unique_users": len(users)},
        "by_pin": sorted(pins, key=lambda r: (-r["unique_garments"], r["pin_code"])),
        "by_brand": sorted(brands.values(), key=lambda r: (-r["unique_garments"], r["brand"])),
        "items": items[:max(1, min(int(limit), 500))],
        "options": {"pin_codes": sorted({c["pin_code"] for c in customers.values() if c["pin_code"]}),
                    "brands": sorted(b for b in all_brands if _known(b))},
        "as_of": data["loaded_at"],
    }


def reveal_phone(customer_id: str) -> Optional[str]:
    c = dataset()["customers"].get(_s(customer_id))
    if not c:
        return None
    d = c["phone_digits"]
    return f"+91 {d[:5]} {d[5:]}" if len(d) == 10 else (d or None)


# ---------------------------------------------------------------------------
# Tab 2: garments
# ---------------------------------------------------------------------------

GARMENT_FILTERS = ("commodity", "brand", "material", "pattern", "size")


def garments(*, filters: Optional[dict[str, list[str]]] = None, pins: Optional[list[str]] = None,
             page: int = 1, size: int = 10) -> dict[str, Any]:
    data = dataset()
    customers = data["customers"]
    f = {k: _lc((filters or {}).get(k)) for k in GARMENT_FILTERS}
    pf = {_s(p) for p in (pins or []) if _s(p)}

    def pin_ok(g: dict) -> bool:
        owner = customers.get(g["customer_id"])
        return bool(owner) and owner["pin_code"] in pf

    rows = [g for g in data["garments"]
            if all(not f[k] or g[k].lower() in f[k] for k in GARMENT_FILTERS) and (not pf or pin_ok(g))]
    mats = Counter(g["material"] for g in rows if _known(g["material"]))
    pats = Counter(g["pattern"] for g in rows if _known(g["pattern"]))
    sizes = Counter(g["size"] for g in rows if _known(g["size"]))
    comms = Counter(g["commodity"] for g in rows if _known(g["commodity"]))
    top_c = [k for k, _ in comms.most_common(8)]
    top_m = [k for k, _ in mats.most_common(6)]
    matrix = {m: [sum(1 for g in rows if g["commodity"] == cc and g["material"] == m) for cc in top_c] for m in top_m}
    chunk, page, size = _page(rows, page, size)
    items = []
    for g in chunk:
        owner = customers.get(g["customer_id"])
        items.append({**{k: (g[k] or None) for k in ("garment_id", "commodity", "brand", "color", "pattern", "size",
                                                    "material", "sleeve", "collar", "menu", "source", "image_status",
                                                    "catalog_match", "brand_sku", "brand_style_code")},
                      "owner_id": short_id(g["customer_id"]) or None,
                      "owner_customer_id": g["customer_id"] or None,
                      "owner_name": owner["name"] if owner else None})
    total_sizes = sum(sizes.values())
    return {
        "total": len(rows),
        "total_tracked": len(data["garments"]),
        "page": page, "size": size, "items": items,
        "top_fabrics": _shares(mats, 5),
        "patterns": _shares(pats, 8),
        "size_curve": [{"value": k, "count": sizes[k], "share": round(sizes[k] * 100 / total_sizes, 1)}
                       for k in sorted(sizes, key=_size_key)] if total_sizes else [],
        "commodity_fabric": {"commodities": top_c, "series": matrix},
        "options": {**{k: _options(g[k] for g in data["garments"]) for k in GARMENT_FILTERS},
                    "pin_codes": _options(c["pin_code"] for c in customers.values())},
    }


# ---------------------------------------------------------------------------
# Tab 3: brands
# ---------------------------------------------------------------------------


def brands(*, brand_list: Optional[list[str]] = None, pins: Optional[list[str]] = None,
           rfm: Optional[list[str]] = None, commodity: Optional[list[str]] = None,
           menu: Optional[list[str]] = None) -> dict[str, Any]:
    data = dataset()
    customers = data["customers"]
    bf, rf, cf, mf = _lc(brand_list), _lc(rfm), _lc(commodity), _lc(menu)
    pf = {_s(p) for p in (pins or []) if _s(p)}
    rows = []
    for g in data["garments"]:
        if not _known(g["brand"]):
            continue
        c = customers.get(g["customer_id"])
        pin = c["pin_code"] if c else ""
        if bf and g["brand"].lower() not in bf:
            continue
        if cf and g["commodity"].lower() not in cf:
            continue
        if mf and g["menu"].lower() not in mf:
            continue
        if pf and pin not in pf:
            continue
        if rf and (not c or c["rfm_segment"].lower() not in rf):
            continue
        rows.append({**g, "pin_code": pin, "rfm_segment": c["rfm_segment"] if c else "Unknown"})

    total = len(rows)
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_brand[r["brand"]].append(r)
    ranking = sorted(by_brand, key=lambda b: -len(by_brand[b]))
    deep = []
    for b in ranking:
        rs = by_brand[b]
        comm = Counter(r["commodity"] for r in rs if _known(r["commodity"]))
        deep.append({
            "brand": b,
            "items_in_wardrobes": len(rs),
            "unique_customers": len({r["customer_id"] for r in rs if r["customer_id"]}),
            "share": round(len(rs) * 100 / total, 1) if total else 0,
            "catalog": [{"commodity": k, "count": v} for k, v in comm.most_common(10)],
            "catalog_size": len(comm),
            "top_commodities": [k for k, _ in comm.most_common(3)],
            "top_colors": [k for k, _ in Counter(r["color"] for r in rs if _known(r["color"])).most_common(3)],
        })

    top_b = ranking[:10]
    treemap = [{"name": b, "value": len(by_brand[b]),
                "children": [{"name": k, "value": v} for k, v in
                             Counter(r["commodity"] for r in by_brand[b] if _known(r["commodity"])).most_common(8)]}
               for b in top_b]
    owners: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r["customer_id"] and r["brand"] in top_b:
            owners[r["customer_id"]].add(r["brand"])
    overlap = [[0] * len(top_b) for _ in top_b]
    idx = {b: i for i, b in enumerate(top_b)}
    for bs in owners.values():
        for a in bs:
            for b in bs:
                overlap[idx[a]][idx[b]] += 1

    tree: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        tree[r["menu"]][r["commodity"] or "Unspecified"][r["color"] if _known(r["color"]) else "Unspecified"] += 1
    sunburst = [{"name": m, "children": [
        {"name": cm, "children": [{"name": col, "value": n} for col, n in cols.most_common(6)]}
        for cm, cols in sorted(cms.items(), key=lambda kv: -sum(kv[1].values()))[:8]]}
        for m, cms in tree.items()]

    sizes = Counter(r["size"] for r in rows if _known(r["size"]))
    size_axis = sorted([k for k, _ in sizes.most_common(10)], key=_size_key)
    comm_axis = [k for k, _ in Counter(r["commodity"] for r in rows if _known(r["commodity"])).most_common(6)]
    size_matrix = {cm: [sum(1 for r in rows if r["commodity"] == cm and r["size"] == s) for s in size_axis]
                   for cm in comm_axis}
    pin_axis = [k for k, _ in Counter(r["pin_code"] for r in rows if r["pin_code"]).most_common(12)]
    regional = {b: [sum(1 for r in by_brand[b] if r["pin_code"] == p) for p in pin_axis] for b in ranking[:8]}

    allg = [g for g in data["garments"] if _known(g["brand"])]
    return {
        "total_items": total,
        "brand_count": len(ranking),
        "ranking": [{"brand": d["brand"], "items": d["items_in_wardrobes"], "share": d["share"],
                     "unique_customers": d["unique_customers"]} for d in deep],
        "brands": deep[:24],
        "treemap": treemap,
        "overlap": {"brands": top_b, "matrix": overlap},
        "sunburst": sunburst,
        "size_matrix": {"sizes": size_axis, "series": size_matrix},
        "regional": {"pins": pin_axis, "series": regional},
        "menu_note": "Menu catalog is derived from each garment's gender field.",
        "options": {
            "brands": _options(g["brand"] for g in allg),
            "pin_codes": _options(c["pin_code"] for c in customers.values()),
            "rfm_segments": _options(c["rfm_segment"] for c in customers.values()),
            "commodities": _options(g["commodity"] for g in allg),
            "menus": _options(g["menu"] for g in allg),
        },
    }


# ---------------------------------------------------------------------------
# Tab 4: campaigns
# ---------------------------------------------------------------------------


def _load_campaigns() -> list[dict]:
    if _campaign_loader is not None:
        return _campaign_loader()
    import brand_campaign as bc

    rows = bc.list_campaigns(brand_name=None, limit=500)
    commodities: dict[str, list[str]] = defaultdict(list)
    with bc.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT campaign_id::text, commodity, COUNT(*) FROM brand_skus
                WHERE commodity IS NOT NULL AND commodity <> ''
                GROUP BY campaign_id, commodity ORDER BY campaign_id, COUNT(*) DESC
                """
            )
            for cid, comm, _n in cur.fetchall():
                commodities[cid].append(comm)
    for r in rows:
        r["featured_commodities"] = commodities.get(r["campaign_id"], [])[:4]
    return rows


def map_status(c: dict, *, today: Optional[date] = None) -> str:
    today = today or datetime.now(timezone.utc).date()
    status = _s(c.get("status")).lower()
    if status == "ignored":
        return "Discarded"
    if status == "launched":
        end = _date(c.get("end_date"))
        if end is None:
            start = _date(c.get("start_date")) or _date(c.get("launched_at"))
            days = c.get("duration_days")
            # Inclusive end, same as brand_campaign.update_campaign_config (start + days - 1).
            end = start + timedelta(days=int(days) - 1) if start and days else None
        return "Completed" if end and today > end else "Active"
    start = _date(c.get("start_date"))
    if status == "configured" and start and start > today:
        return "Scheduled"
    return "Draft"


def campaigns(*, brand: Optional[list[str]] = None, status: Optional[list[str]] = None,
              pins: Optional[list[str]] = None, rfm: Optional[list[str]] = None,
              today: Optional[date] = None) -> dict[str, Any]:
    try:
        rows = _load_campaigns()
    except Exception as exc:
        log.exception("admin campaigns load failed")
        raise IntelUnavailable("campaigns are unavailable") from exc
    bf, sf, rf = _lc(brand), _lc(status), _lc(rfm)
    pf = {_s(p) for p in (pins or []) if _s(p)}
    all_cards = []
    for c in rows:
        impact = c.get("impact") or {}
        reach = _num(impact.get("reach"))
        conv = _num(impact.get("conversions"))
        all_cards.append({
            "campaign_id": c.get("campaign_id"),
            "title": c.get("campaign_name") or c.get("title") or "Untitled campaign",
            "brand": c.get("brand_name") or "—",
            "status": map_status(c, today=today),
            "raw_status": c.get("status"),
            "target_pins": c.get("pin_codes") or [],
            "target_rfm": c.get("rfm_segment") or "All segments",
            "featured_commodities": c.get("featured_commodities") or [],
            "objective": c.get("objective"),
            "channels": c.get("channels") or [],
            "budget_inr": c.get("budget_inr"),
            "start_date": c.get("start_date"),
            "end_date": c.get("end_date"),
            "launched_at": c.get("launched_at"),
            "conversion_rate": round(conv * 100 / reach, 2) if reach > 0 else None,
            "redemptions": int(conv) if impact else None,
            "simulated": bool(impact),
            "raw": c,
        })
    def rfm_parts(card: dict) -> list[str]:
        return [p.strip() for p in card["target_rfm"].split(",") if p.strip()]

    cards = [
        x for x in all_cards
        if (not bf or x["brand"].lower() in bf)
        and (not sf or x["status"].lower() in sf)
        and (not pf or pf & set(x["target_pins"]))
        and (not rf or any(p.lower() in rf for p in rfm_parts(x)))
    ]
    rates = [x["conversion_rate"] for x in cards if x["conversion_rate"] is not None]
    by_brand: dict[str, dict[str, Any]] = {}
    for x in cards:
        b = by_brand.setdefault(x["brand"], {"redemptions": 0, "status": {s: 0 for s in STATUSES}})
        b["redemptions"] += x["redemptions"] or 0
        b["status"][x["status"]] += 1
    return {
        "items": cards,
        "total": len(cards),
        "summary": {
            "active": sum(1 for x in cards if x["status"] == "Active"),
            "avg_conversion_rate": round(sum(rates) / len(rates), 2) if rates else None,
            "total_redemptions": sum(x["redemptions"] or 0 for x in cards),
            "by_brand": by_brand,
            "simulated": True,
        },
        "options": {
            "brands": _options(x["brand"] for x in all_cards),
            "statuses": list(STATUSES),
            "pin_codes": _options(p for x in all_cards for p in x["target_pins"]),
            "rfm_segments": _options(p for x in all_cards for p in rfm_parts(x)),
        },
        "note": "Conversion rate and redemptions are simulated; launches deliver nothing to customers.",
    }
