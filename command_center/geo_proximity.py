"""4 km cannibalization guardrail: brand store locations (Gemini + Google Search, cached) and PIN zoning.

A PIN whose centroid is within PROXIMITY_KM of an existing store of the analysed brand is never a
new-store candidate: decision "No", served via the existing store (optimisation mode).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import gemini_grounded as grounded

log = logging.getLogger("command_center.geo_proximity")

PROXIMITY_KM = 4.0
HIGH_VALUE_RFM = ("Champions", "Loyal")
HIGH_VALUE_LABEL = "Champions & Loyalists"
SERVICE_LABEL = "Service via Existing Store"
UNVERIFIED_LABEL = "Store proximity unverified"
WHITESPACE_LABELS = {"Yes": "Open new store", "Consider": "Evaluate whitespace", "No": "Whitespace, not viable"}

# (min_lat, max_lat, min_lon, max_lon)
CITY_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    "bengaluru": (12.70, 13.25, 77.35, 77.90),
}
PIN_MISMATCH_KM = 6.0
_PIN_RE = re.compile(r"\b(\d{6})\b")


def default_city() -> str:
    return os.getenv("GEO_CITY", "Bengaluru")


def cache_days() -> float:
    try:
        return max(0.0, float(os.getenv("GEO_STORE_CACHE_DAYS", "7")))
    except ValueError:
        return 7.0


def brand_key(brand: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (brand or "").strip().lower()).strip("-")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def pin_centroid(pin_geo: dict[str, Any], pin: str) -> Optional[tuple[float, float]]:
    g = pin_geo.get(str(pin)) if isinstance(pin_geo, dict) else None
    if not isinstance(g, dict):
        return None
    try:
        return float(g["centroid_lat"]), float(g["centroid_lon"])
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Store validation
# ---------------------------------------------------------------------------


def _in_city(lat: float, lon: float, city: str) -> bool:
    b = CITY_BOUNDS.get(city.strip().lower())
    if not b:
        return -90 <= lat <= 90 and -180 <= lon <= 180
    return b[0] <= lat <= b[1] and b[2] <= lon <= b[3]


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def validate_stores(raw: Any, *, pin_geo: dict[str, Any], city: str) -> list[dict[str, Any]]:
    """Keep stores with plausible in-city coordinates; fall back to the PIN centroid; flag mismatches."""
    out: list[dict[str, Any]] = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or s.get("store_name") or "").strip()[:120]
        if not name:
            continue
        address = str(s.get("address") or "").strip()[:300] or None
        pin = str(s.get("pin_code") or "").strip()
        if not re.fullmatch(r"\d{6}", pin):
            m = _PIN_RE.search(address or "")
            pin = m.group(1) if m else ""
        lat, lon = _num(s.get("lat")), _num(s.get("lon") if s.get("lon") is not None else s.get("lng"))
        centroid = pin_centroid(pin_geo, pin) if pin else None
        source = "model"
        confidence = "medium"
        if lat is None or lon is None or not _in_city(lat, lon, city):
            if not centroid:
                continue
            lat, lon = centroid
            source, confidence = "pin_centroid", "low"
        elif centroid and haversine_km(lat, lon, *centroid) > PIN_MISMATCH_KM:
            confidence = "low"
        elif centroid:
            confidence = "high"
        key = (round(lat, 4), round(lon, 4))
        if any((round(x["lat"], 4), round(x["lon"], 4)) == key for x in out):
            continue
        out.append({
            "name": name,
            "address": address,
            "area": str(s.get("area") or "").strip()[:80] or None,
            "pin_code": pin or None,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "location_source": source,
            "confidence": confidence,
            "source_url": str(s.get("source_url") or "").strip()[:500] or None,
        })
    return out[:200]


def _locator_prompt(brand: str, city: str) -> str:
    return (
        f"Use Google Search to list every currently operating physical retail store of the apparel brand "
        f"\"{brand}\" in {city}, India: exclusive brand outlets and mall stores selling under the brand's own "
        "name. Exclude online-only listings, warehouses, closed stores and multi-brand retailers that merely "
        "stock the brand. For each store give the name, full street address, area/locality, 6-digit PIN code, "
        "latitude and longitude (decimal degrees; null if you cannot find them reliably) and the URL you "
        "used. Do not invent stores. Reply ONLY with minified JSON, no markdown:\n"
        '{"stores":[{"name":"","address":"","area":"","pin_code":"","lat":null,"lon":null,"source_url":""}]}'
    )


# ---------------------------------------------------------------------------
# Cache (Cloud SQL when available, process memory otherwise)
# ---------------------------------------------------------------------------

_mem_lock = threading.Lock()
_mem: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_fetch_locks: dict[tuple[str, str], threading.Lock] = {}
_UNAVAILABLE_RETRY_SEC = 120


def _db_enabled() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def _db_load(bkey: str, city: str) -> Optional[dict[str, Any]]:
    import brand_campaign as bc

    with bc.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT brand_name, status, sources, search_entry_html, model, fetched_at
                FROM geo_store_lookups
                WHERE brand_key = %s AND city = %s
                  AND fetched_at > now() - make_interval(secs => %s)
                """,
                (bkey, city, cache_days() * 86400),
            )
            head = cur.fetchone()
            if not head:
                return None
            cur.execute(
                """
                SELECT store_name, address, area, pin_code, lat, lon, location_source, confidence, source_url
                FROM geo_brand_stores WHERE brand_key = %s AND city = %s ORDER BY store_name
                """,
                (bkey, city),
            )
            stores = [
                {"name": r[0], "address": r[1], "area": r[2], "pin_code": r[3], "lat": float(r[4]),
                 "lon": float(r[5]), "location_source": r[6], "confidence": r[7], "source_url": r[8]}
                for r in cur.fetchall()
            ]
    return {
        "brand": head[0], "city": city, "status": head[1], "stores": stores,
        "sources": head[2] if isinstance(head[2], list) else json.loads(head[2] or "[]"),
        "search_entry_html": head[3], "model": head[4],
        "fetched_at": head[5].isoformat() if head[5] else None,
    }


def _db_save(bkey: str, result: dict[str, Any]) -> None:
    import brand_campaign as bc

    with bc.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM geo_store_lookups WHERE brand_key = %s AND city = %s", (bkey, result["city"]))
            cur.execute(
                """
                INSERT INTO geo_store_lookups (brand_key, city, brand_name, status, sources, search_entry_html, model)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                """,
                (bkey, result["city"], result["brand"], result["status"], json.dumps(result["sources"]),
                 result.get("search_entry_html"), result.get("model")),
            )
            for s in result["stores"]:
                cur.execute(
                    """
                    INSERT INTO geo_brand_stores (brand_key, city, store_name, address, area, pin_code, lat, lon,
                                                  location_source, confidence, source_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (bkey, result["city"], s["name"], s.get("address"), s.get("area"), s.get("pin_code"),
                     s["lat"], s["lon"], s["location_source"], s["confidence"], s.get("source_url")),
                )


def _mem_get(key: tuple[str, str]) -> Optional[dict[str, Any]]:
    with _mem_lock:
        hit = _mem.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _mem_put(key: tuple[str, str], value: dict[str, Any], ttl: float) -> None:
    with _mem_lock:
        if len(_mem) > 256:
            _mem.clear()
        _mem[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    with _mem_lock:
        _mem.clear()


def locate_brand_stores(
    brand: Optional[str],
    *,
    pin_geo: dict[str, Any],
    city: Optional[str] = None,
    refresh: bool = False,
    request_id: str = "-",
) -> dict[str, Any]:
    """
    Existing stores of `brand` in `city`. status: ok | empty (none found) | unavailable (lookup failed,
    proximity cannot be verified). Never raises.
    """
    city = (city or default_city()).strip()
    brand = (brand or "").strip()
    if not brand:
        return {"brand": None, "city": city, "status": "unavailable", "stores": [], "sources": [],
                "detail": "No brand selected."}
    bkey = brand_key(brand)
    key = (bkey, city.lower())
    with _mem_lock:
        lock = _fetch_locks.setdefault(key, threading.Lock())
    with lock:
        return _locate_locked(brand, bkey, key, city, pin_geo, refresh, request_id)


def _cache_hit(bkey: str, key: tuple[str, str], city: str) -> Optional[dict[str, Any]]:
    hit = _mem_get(key)
    if hit:
        return {**hit, "cached": True}
    if _db_enabled():
        try:
            row = _db_load(bkey, city)
        except Exception:
            log.warning("geo store cache read failed brand=%s", bkey, exc_info=True)
            row = None
        if row:
            _mem_put(key, row, 600)
            return {**row, "cached": True}
    return None


def cached_brand_stores(brand: Optional[str], *, city: Optional[str] = None) -> dict[str, Any]:
    """
    Stores of `brand` from the memory / Cloud SQL cache only, same shape as locate_brand_stores. Never calls
    Gemini and never waits for a lookup in progress; a miss is status "unavailable" (PINs stay unverified).
    """
    city = (city or default_city()).strip()
    brand = (brand or "").strip()
    if not brand:
        return {"brand": None, "city": city, "status": "unavailable", "stores": [], "sources": [],
                "detail": "No brand selected."}
    bkey = brand_key(brand)
    hit = _cache_hit(bkey, (bkey, city.lower()), city)
    if hit:
        return hit
    return {"brand": brand, "city": city, "status": "unavailable", "stores": [], "sources": [],
            "detail": "Store locations have not been looked up yet.", "cached": False}


def _locate_locked(brand: str, bkey: str, key: tuple[str, str], city: str, pin_geo: dict[str, Any],
                   refresh: bool, request_id: str) -> dict[str, Any]:
    if not refresh:
        hit = _cache_hit(bkey, key, city)
        if hit:
            return hit

    try:
        answer = grounded.ask(_locator_prompt(brand, city), request_id=request_id)
        data = grounded.parse_json(answer.text)
        raw = data.get("stores") if isinstance(data, dict) else data
        stores = validate_stores(raw, pin_geo=pin_geo, city=city)
    except (grounded.GroundingUnavailable, ValueError, TypeError) as exc:
        log.warning("request_id=%s store lookup unavailable brand=%s: %s", request_id, bkey, exc)
        result = {"brand": brand, "city": city, "status": "unavailable", "stores": [], "sources": [],
                  "detail": "Store lookup is unavailable right now."}
        _mem_put(key, result, _UNAVAILABLE_RETRY_SEC)
        return {**result, "cached": False}

    result = {
        "brand": brand, "city": city, "status": "ok" if stores else "empty", "stores": stores,
        "sources": answer.sources, "search_entry_html": answer.search_entry_html, "model": answer.model,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if _db_enabled():
        try:
            _db_save(bkey, result)
        except Exception:
            log.warning("geo store cache write failed brand=%s", bkey, exc_info=True)
    _mem_put(key, result, max(600.0, cache_days() * 86400))
    log.info("request_id=%s store lookup brand=%s city=%s stores=%s", request_id, bkey, city, len(stores))
    return {**result, "cached": False}


# ---------------------------------------------------------------------------
# Zoning
# ---------------------------------------------------------------------------


def nearest_store(lat: float, lon: float, stores: Sequence[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[float]]:
    best, best_km = None, None
    for s in stores:
        km = haversine_km(lat, lon, s["lat"], s["lon"])
        if best_km is None or km < best_km:
            best, best_km = s, km
    return best, best_km


def zone_for(distance_km: Optional[float], lookup_status: str) -> str:
    if lookup_status == "unavailable":
        return "unverified"
    if distance_km is not None and within_threshold(distance_km):
        return "service"
    return "whitespace"


def within_threshold(distance_km: float) -> bool:
    """Metre precision, so float noise never flips a PIN at exactly 4 km."""
    return round(distance_km, 3) <= PROXIMITY_KM


def gate_decision(expansion_decision: str, zone: str) -> tuple[str, str, str]:
    """(decision, decision_label, mode) after the 4 km rule."""
    if zone == "service":
        return "No", SERVICE_LABEL, "optimize"
    if zone == "unverified":
        return ("Consider" if expansion_decision == "Yes" else expansion_decision), UNVERIFIED_LABEL, "expansion"
    return expansion_decision, WHITESPACE_LABELS.get(expansion_decision, expansion_decision), "expansion"


def store_public(s: Optional[dict[str, Any]], km: Optional[float] = None) -> Optional[dict[str, Any]]:
    if not s:
        return None
    out = {k: s.get(k) for k in ("name", "area", "pin_code", "confidence", "location_source")}
    if km is not None:
        out["distance_km"] = round(km, 2)
    return out


def apply_proximity(rows: list[dict[str, Any]], lookup: dict[str, Any], pin_geo: dict[str, Any]) -> list[dict[str, Any]]:
    """Annotate scored PIN rows in place; `heuristic_decision` is the pre-guardrail expansion call."""
    status = lookup.get("status") or "unavailable"
    stores = lookup.get("stores") or []
    for r in rows:
        expansion = r.get("expansion_decision") or r.get("heuristic_decision") or "No"
        r["expansion_decision"] = expansion
        c = pin_centroid(pin_geo, r.get("pin"))
        store, km = nearest_store(*c, stores) if (c and stores) else (None, None)
        zone = zone_for(km, status)
        decision, label, mode = gate_decision(expansion, zone)
        r.update(
            nearest_store=store_public(store, km),
            nearest_store_km=round(km, 2) if km is not None else None,
            proximity_zone=zone,
            decision=decision,
            decision_label=label,
            mode=mode,
        )
    return rows


def catchment_pins(store: dict[str, Any], pin_geo: dict[str, Any]) -> list[tuple[str, float]]:
    """PINs whose centroid is within PROXIMITY_KM of the store, nearest first."""
    out = []
    for pin in pin_geo or {}:
        c = pin_centroid(pin_geo, pin)
        if not c:
            continue
        km = haversine_km(c[0], c[1], store["lat"], store["lon"])
        if within_threshold(km):
            out.append((str(pin), round(km, 2)))
    return sorted(out, key=lambda t: t[1])


def stores_for_map(lookup: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {k: s.get(k) for k in ("name", "address", "area", "pin_code", "lat", "lon", "confidence",
                               "location_source", "source_url")}
        for s in lookup.get("stores") or []
    ]
