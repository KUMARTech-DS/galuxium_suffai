"""External market check for whitespace PINs (> 4 km from an existing store) via grounded Gemini.

Signals: footfall, retail rental levels / yields, apparel retail competition. Brand logins never see
competitor names. External signals can only lower the internal expansion decision, never raise it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import gemini_grounded as grounded
import geo_proximity as gp
import vocab

log = logging.getLogger("command_center.geo_market")

VIABILITY = ("supportive", "neutral", "unfavourable")
LEVELS = ("high", "medium", "low", "unknown")
_LOWER = {"Yes": "Consider", "Consider": "No", "No": "No"}


class EnrichmentRefused(ValueError):
    """The PIN is served by an existing store; external whitespace checks do not apply."""


def cache_days() -> float:
    try:
        return max(0.0, float(os.getenv("GEO_MARKET_CACHE_DAYS", "7")))
    except ValueError:
        return 7.0


def _prompt(pin: str, area: str, city: str, brand: Optional[str], role: str) -> str:
    who = f"the apparel brand \"{brand}\"" if brand else "an apparel brand"
    competition = (
        '"competition":{"level":"high|medium|low|unknown","apparel_store_count_estimate":null,'
        '"malls_nearby":[""],"notes":""}'
        if role == "brand"
        else '"competition":{"level":"high|medium|low|unknown","apparel_store_count_estimate":null,'
        '"malls_nearby":[""],"competitors":[""],"notes":""}'
    )
    naming = (
        "Never name any apparel brand other than the one above; refer to them only as competitors. "
        if role == "brand"
        else "List up to 6 notable competing apparel brands with stores near the area under competitors. "
    )
    return (
        f"Use Google Search to assess {area} (PIN {pin}), {city}, India as a location for a new physical "
        f"store of {who}. Report: (1) retail footfall — level and the evidence (high streets, malls, metro "
        "stations, offices); (2) commercial retail rents — typical monthly rent in INR per sq ft for ground-"
        "floor high-street or mall retail, and rental yield percent if published; (3) apparel retail "
        f"competition density. {naming}Use null where you cannot find a figure; do not guess numbers. "
        "Then give overall viability for a new apparel store: supportive, neutral or unfavourable, with "
        "a one-sentence reason. Reply ONLY with minified JSON, no markdown:\n"
        '{"footfall":{"level":"high|medium|low|unknown","drivers":[""],"notes":""},'
        '"rent":{"inr_per_sqft_month_low":null,"inr_per_sqft_month_high":null,"rental_yield_pct":null,'
        f'"notes":""}},{competition},'
        '"viability":"supportive|neutral|unfavourable","summary":""}'
    )


def _level(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in LEVELS else "unknown"


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f >= 0 else None


def _text(v: Any, n: int = 400) -> str:
    return str(v or "").strip()[:n]


def _strs(v: Any, n: int = 6) -> list[str]:
    return [_text(x, 80) for x in (v if isinstance(v, list) else []) if _text(x, 80)][:n]


def mask_text(text: str, names: list[str]) -> str:
    out = text or ""
    for name in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
        out = re.sub(rf"(?i)(?<![\w]){re.escape(name)}(?![\w])", "Competitors", out)
    return out


def normalize(data: Any, *, role: str, brand: Optional[str], known_brands: list[str]) -> dict[str, Any]:
    d = data if isinstance(data, dict) else {}
    ff = d.get("footfall") if isinstance(d.get("footfall"), dict) else {}
    rent = d.get("rent") if isinstance(d.get("rent"), dict) else {}
    comp = d.get("competition") if isinstance(d.get("competition"), dict) else {}
    viability = str(d.get("viability") or "").strip().lower()
    out = {
        "footfall": {"level": _level(ff.get("level")), "drivers": _strs(ff.get("drivers")),
                     "notes": _text(ff.get("notes"))},
        "rent": {"inr_per_sqft_month_low": _num(rent.get("inr_per_sqft_month_low")),
                 "inr_per_sqft_month_high": _num(rent.get("inr_per_sqft_month_high")),
                 "rental_yield_pct": _num(rent.get("rental_yield_pct")), "notes": _text(rent.get("notes"))},
        "competition": {"level": _level(comp.get("level")),
                        "apparel_store_count_estimate": _num(comp.get("apparel_store_count_estimate")),
                        "malls_nearby": _strs(comp.get("malls_nearby")), "notes": _text(comp.get("notes"))},
        "viability": viability if viability in VIABILITY else "neutral",
        "summary": _text(d.get("summary"), 600),
    }
    competitors = _strs(comp.get("competitors"))
    own = vocab.value_key(brand)
    if role == "brand":
        names = [b for b in known_brands + competitors if vocab.value_key(b) != own]
        out["competition"]["notes"] = mask_text(out["competition"]["notes"], names)
        out["summary"] = mask_text(out["summary"], names)
        out["footfall"]["notes"] = mask_text(out["footfall"]["notes"], names)
        out["footfall"]["drivers"] = [mask_text(x, names) for x in out["footfall"]["drivers"]]
        out["competition"]["malls_nearby"] = [mask_text(x, names) for x in out["competition"]["malls_nearby"]]
        out["competition"]["competitor_count"] = len([c for c in competitors if vocab.value_key(c) != own])
    else:
        out["competition"]["competitors"] = competitors
    return out


def adjust_decision(expansion_decision: str, enrichment: Optional[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """External signals may only lower the decision (one step when viability is unfavourable)."""
    if not enrichment or enrichment.get("status") != "ok":
        return expansion_decision, None
    if (enrichment.get("market") or {}).get("viability") == "unfavourable":
        lowered = _LOWER.get(expansion_decision, expansion_decision)
        if lowered != expansion_decision:
            return lowered, "External market check is unfavourable, so the decision was lowered one step."
    return expansion_decision, None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_mem_lock = threading.Lock()
_mem: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    with _mem_lock:
        _mem.clear()


def _db_load(pin: str, key: str) -> Optional[dict[str, Any]]:
    import brand_campaign as bc

    with bc.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload, sources, search_entry_html, model, fetched_at FROM geo_market_enrichment
                WHERE pin_code = %s AND brand_key = %s AND fetched_at > now() - make_interval(secs => %s)
                """,
                (pin, key, cache_days() * 86400),
            )
            r = cur.fetchone()
    if not r:
        return None
    load = lambda v: v if isinstance(v, (dict, list)) else json.loads(v or "null")  # noqa: E731
    return {"market": load(r[0]), "sources": load(r[1]) or [], "search_entry_html": r[2], "model": r[3],
            "fetched_at": r[4].isoformat() if r[4] else None}


def _db_save(pin: str, key: str, res: dict[str, Any]) -> None:
    import brand_campaign as bc

    with bc.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO geo_market_enrichment (pin_code, brand_key, payload, sources, search_entry_html, model,
                                                   fetched_at)
                VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s,now())
                ON CONFLICT (pin_code, brand_key) DO UPDATE SET payload = EXCLUDED.payload,
                  sources = EXCLUDED.sources, search_entry_html = EXCLUDED.search_entry_html,
                  model = EXCLUDED.model, fetched_at = now()
                """,
                (pin, key, json.dumps(res["market"]), json.dumps(res["sources"]), res.get("search_entry_html"),
                 res.get("model")),
            )


def _cache_key(brand: Optional[str], role: str) -> str:
    return f"{gp.brand_key(brand or 'all')}|{'brand' if role == 'brand' else 'admin'}"


def _cache_get(pin: str, key: str) -> Optional[dict[str, Any]]:
    with _mem_lock:
        hit = _mem.get((pin, key))
    if hit and hit[0] > time.time():
        return hit[1]
    if gp._db_enabled():
        try:
            row = _db_load(pin, key)
        except Exception:
            log.warning("geo market cache read failed pin=%s", pin, exc_info=True)
            return None
        if row:
            with _mem_lock:
                _mem[(pin, key)] = (time.time() + 600, row)
            return row
    return None


def cached(pin: str, *, brand: Optional[str], role: str) -> Optional[dict[str, Any]]:
    """Previously fetched enrichment for this PIN (no grounded call), or None."""
    row = _cache_get(str(pin or "").strip(), _cache_key(brand, role))
    return {**row, "status": "ok", "cached": True} if row else None


def _known_brands(metrics: dict[str, Any]) -> list[str]:
    return sorted({str(g.get("brand") or "").strip() for g in metrics.get("garment_cells") or []
                   if isinstance(g, dict) and str(g.get("brand") or "").strip() not in {"", "Competitors"}})


def enrich_whitespace(
    pin: str,
    *,
    metrics: dict[str, Any],
    brand: Optional[str],
    role: str,
    stores_lookup: dict[str, Any],
    refresh: bool = False,
    request_id: str = "-",
) -> dict[str, Any]:
    """Grounded footfall / rent / competition for one PIN. Raises EnrichmentRefused within 4 km."""
    pin = str(pin or "").strip()
    pin_geo = metrics.get("pin_geo") if isinstance(metrics.get("pin_geo"), dict) else {}
    c = gp.pin_centroid(pin_geo, pin)
    store, km = gp.nearest_store(*c, stores_lookup.get("stores") or []) if c else (None, None)
    zone = gp.zone_for(km, stores_lookup.get("status") or "unavailable")
    if zone == "service":
        raise EnrichmentRefused(
            f"PIN {pin} is {km:.1f} km from an existing store (within {gp.PROXIMITY_KM:g} km): "
            "serve it via the existing store instead."
        )
    area = str((pin_geo.get(pin) or {}).get("area_label") or pin)
    city = stores_lookup.get("city") or gp.default_city()
    scope = "brand" if role == "brand" else "admin"
    key = _cache_key(brand, role)
    base = {"pin": pin, "area_label": area, "proximity_zone": zone,
            "nearest_store": gp.store_public(store, km), "threshold_km": gp.PROXIMITY_KM,
            "label": "External signals via Google Search (grounded); verify before committing capex."}

    if not refresh:
        row = _cache_get(pin, key)
        if row:
            return {**base, **row, "status": "ok", "cached": True}

    try:
        answer = grounded.ask(_prompt(pin, area, city, brand, scope), request_id=request_id)
        market = normalize(grounded.parse_json(answer.text), role=scope, brand=brand,
                           known_brands=_known_brands(metrics))
    except (grounded.GroundingUnavailable, ValueError, TypeError) as exc:
        log.warning("request_id=%s market enrichment unavailable pin=%s: %s", request_id, pin, exc)
        return {**base, "status": "unavailable", "detail": "External market check is unavailable right now."}

    sources = answer.sources
    entry_html = answer.search_entry_html
    if scope == "brand":
        names = [b for b in _known_brands(metrics) if b.strip().lower() != (brand or "").strip().lower()]
        sources = [{"title": mask_text(s["title"], names), "url": s["url"]} for s in sources]
    res = {"market": market, "sources": sources, "search_entry_html": entry_html, "model": answer.model,
           "fetched_at": datetime.now(timezone.utc).isoformat()}
    if gp._db_enabled():
        try:
            _db_save(pin, key, res)
        except Exception:
            log.warning("geo market cache write failed pin=%s", pin, exc_info=True)
    with _mem_lock:
        if len(_mem) > 512:
            _mem.clear()
        _mem[(pin, key)] = (time.time() + max(600.0, cache_days() * 86400), res)
    return {**base, **res, "status": "ok", "cached": False}
