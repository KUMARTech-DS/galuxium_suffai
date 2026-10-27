"""PIN area profile (area name, population, centroid) from Gemini + Google Search, kept in BigQuery pin_areas.

Every tab reads PIN areas from this table through the metrics payload (pin_geo). A PIN is looked up when
someone explores it on Geo, when an admin creates a customer for it, or by scripts/backfill_pin_areas.py.
Grounded answers carry their sources and the Google Search chip, which the UI must show with them.
Population buffers are estimates: PIN density x the area of a 1 / 2 / 4 km circle.
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
from typing import Any, Callable, Optional

import gemini_grounded as grounded
import geo_proximity as gp

log = logging.getLogger("command_center.pin_areas")

STATUSES = ("ok", "not_found", "out_of_city")
BUFFER_KM = (1, 2, 4)
LABEL = "Area profile via Gemini + Google Search; population is the latest published figure, buffers are estimates."
COLUMNS = (
    "pin_code", "city", "area_label", "population_pin", "population_year", "area_sq_km", "pop_buffer_1km",
    "pop_buffer_2km", "pop_buffer_4km", "centroid_lat", "centroid_lon", "status", "detail", "sources",
    "search_entry_html", "model", "fetched_at",
)
_TYPES = {
    "population_pin": "INT64", "population_year": "INT64", "area_sq_km": "FLOAT64", "pop_buffer_1km": "INT64",
    "pop_buffer_2km": "INT64", "pop_buffer_4km": "INT64", "centroid_lat": "FLOAT64", "centroid_lon": "FLOAT64",
}
_PIN_RE = re.compile(r"^[1-9]\d{5}$")
_NOT_OK_CACHE_SEC = 86400

Param = tuple[str, str, Any]

# Test hook: (sql, params) -> rows instead of BigQuery.
_runner: Optional[Callable[[str, list[Param]], list[dict[str, Any]]]] = None


class AreaUnavailable(RuntimeError):
    """BigQuery pin_areas cannot be read or written."""


class AreaRejected(ValueError):
    """The PIN cannot be used (bad format, not found, outside the city, lookup unavailable). Shown to the admin."""


class RateLimited(RuntimeError):
    pass


def valid_pin(pin: Any) -> bool:
    return bool(_PIN_RE.match(str(pin or "").strip()))


def cache_days() -> float:
    try:
        return max(0.0, float(os.getenv("GEO_AREA_CACHE_DAYS", "30")))
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


def _project(project: Optional[str] = None) -> str:
    p = project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not p:
        raise AreaUnavailable("GOOGLE_CLOUD_PROJECT not set")
    return p


def _table(project: Optional[str] = None, dataset: Optional[str] = None) -> str:
    return f"`{_project(project)}.{dataset or os.getenv('BQ_DATASET', 'suffai_ops')}.pin_areas`"


def _run(sql: str, params: list[Param], project: Optional[str] = None) -> list[dict[str, Any]]:
    if _runner is not None:
        return _runner(sql, params)
    try:
        from google.cloud import bigquery  # type: ignore

        client = bigquery.Client(project=_project(project))
        cfg = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter(n, t, v) for n, t, v in params]
        )
        return [dict(r) for r in client.query(sql, job_config=cfg).result()]
    except AreaUnavailable:
        raise
    except Exception as exc:
        raise AreaUnavailable(f"pin_areas query failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


def _iso(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _row(r: dict[str, Any]) -> dict[str, Any]:
    out = {c: r.get(c) for c in COLUMNS}
    out["pin_code"] = str(out["pin_code"] or "").strip()
    out["fetched_at"] = _iso(out["fetched_at"])
    return out


def load_all(project: Optional[str] = None, dataset: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Every stored PIN profile keyed by PIN. A dataset without the table (not migrated yet) gives {}."""
    try:
        rows = _run(f"SELECT {', '.join(COLUMNS)} FROM {_table(project, dataset)}", [], project)
    except AreaUnavailable as exc:
        if "notfound" in str(exc).lower().replace(" ", ""):
            log.warning("pin_areas table missing; run migrate_bq_consistency.sh")
            return {}
        raise
    return {r["pin_code"]: r for r in (_row(x) for x in rows) if r["pin_code"]}


def _load_one(pin: str) -> Optional[dict[str, Any]]:
    rows = _run(f"SELECT {', '.join(COLUMNS)} FROM {_table()} WHERE pin_code = @pin LIMIT 1",
                [("pin", "STRING", pin)])
    return _row(rows[0]) if rows else None


def _save(row: dict[str, Any]) -> None:
    cols = [c for c in COLUMNS if c != "fetched_at"]
    params: list[Param] = []
    for c in cols:
        v = row.get(c)
        if c == "sources":
            v = json.dumps(v or [])
        params.append((c, _TYPES.get(c, "STRING"), v))
    select = ", ".join(f"@{c} AS {c}" for c in cols)
    updates = ", ".join(f"{c} = s.{c}" for c in cols if c != "pin_code")
    _run(
        f"MERGE {_table()} t USING (SELECT {select}) s ON t.pin_code = s.pin_code "
        f"WHEN MATCHED THEN UPDATE SET {updates}, fetched_at = CURRENT_TIMESTAMP() "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}, fetched_at) "
        f"VALUES ({', '.join('s.' + c for c in cols)}, CURRENT_TIMESTAMP())",
        params,
    )


# ---------------------------------------------------------------------------
# Grounded lookup
# ---------------------------------------------------------------------------


def _prompt(pin: str, city: str) -> str:
    return (
        f"Use Google Search to look up the Indian postal PIN code {pin} in {city}, India. Report the main "
        "locality or area name the PIN covers, its residential population (latest census or a published "
        "estimate, with the year), its approximate land area in square kilometres, and the latitude and "
        "longitude of the centre of the PIN area in decimal degrees. If the PIN does not exist or is not in "
        f"{city}, set found to false and say why in notes. Use null where you cannot find a figure; do not "
        "guess numbers. Reply ONLY with minified JSON, no markdown:\n"
        '{"found":true,"pin_code":"","area_label":"","city":"","population":null,"population_year":null,'
        '"area_sq_km":null,"lat":null,"lon":null,"notes":""}'
    )


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _in_range(v: Any, lo: float, hi: float) -> Optional[float]:
    f = _num(v)
    return f if f is not None and lo <= f <= hi else None


def buffers(population: Optional[int], area_sq_km: Optional[float]) -> dict[str, Optional[int]]:
    """Residents within 1 / 2 / 4 km, assuming the PIN's density holds around its centre."""
    if not population or not area_sq_km:
        return {f"pop_buffer_{r}km": None for r in BUFFER_KM}
    density = population / area_sq_km
    return {f"pop_buffer_{r}km": int(round(density * math.pi * r * r)) for r in BUFFER_KM}


def normalize(data: Any, *, pin: str, city: str) -> dict[str, Any]:
    """Validated pin_areas row (without sources). status: ok | not_found | out_of_city."""
    d = data if isinstance(data, dict) else {}
    label = re.sub(r"\s+", " ", str(d.get("area_label") or "")).strip()[:80]
    returned = re.sub(r"\D", "", str(d.get("pin_code") or pin))
    lat = _in_range(d.get("lat"), -90, 90)
    lon = _in_range(d.get("lon") if d.get("lon") is not None else d.get("lng"), -180, 180)
    population = _in_range(d.get("population"), 100, 5_000_000)
    area = _in_range(d.get("area_sq_km"), 0.05, 2000)
    year = _in_range(d.get("population_year"), 1950, datetime.now(timezone.utc).year + 1)
    notes = re.sub(r"\s+", " ", str(d.get("notes") or "")).strip()[:200]

    if d.get("found") is False or returned != pin or not label:
        status, detail = "not_found", notes or f"PIN {pin} was not found in {city}."
    elif lat is None or lon is None:
        status, detail = "not_found", f"Could not locate PIN {pin} on the map."
    elif not gp._in_city(lat, lon, city):
        status, detail = "out_of_city", f"PIN {pin} is outside {city}."
    else:
        status, detail = "ok", notes or None
    ok = status == "ok"
    pop_i = int(population) if ok and population is not None else None
    area_f = round(area, 2) if ok and area is not None else None
    return {
        "pin_code": pin,
        "city": city,
        "area_label": label or None,
        "population_pin": pop_i,
        "population_year": int(year) if ok and year is not None else None,
        "area_sq_km": area_f,
        **buffers(pop_i, area_f),
        "centroid_lat": round(lat, 6) if ok else None,
        "centroid_lon": round(lon, 6) if ok else None,
        "status": status,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Cache + public API
# ---------------------------------------------------------------------------

_mem_lock = threading.Lock()
_mem: dict[str, tuple[float, dict[str, Any]]] = {}
_fetch_locks: dict[str, threading.Lock] = {}
_limiter = None


def clear_cache() -> None:
    with _mem_lock:
        _mem.clear()


def _limit_ok(key: Optional[str]) -> bool:
    global _limiter
    if not key:
        return True
    if _limiter is None:
        import aura_guard

        _limiter = aura_guard.RateLimiter(per_minute=int(os.getenv("GEO_AREA_RATE_PER_MIN", "10")))
    return _limiter.allow(key)


def _age_sec(row: dict[str, Any]) -> Optional[float]:
    try:
        at = datetime.fromisoformat(str(row.get("fetched_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


def _fresh(row: Optional[dict[str, Any]]) -> bool:
    if not row:
        return False
    age = _age_sec(row)
    ttl = cache_days() * 86400 if row.get("status") == "ok" else _NOT_OK_CACHE_SEC
    return age is not None and age < ttl


def stored(pin: str) -> Optional[dict[str, Any]]:
    """Saved profile for the PIN (memory, then BigQuery), fresh or not. None when never looked up."""
    with _mem_lock:
        hit = _mem.get(pin)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        row = _load_one(pin)
    except AreaUnavailable:
        log.warning("pin_areas read failed pin=%s", pin, exc_info=True)
        return None
    if row:
        with _mem_lock:
            _mem[pin] = (time.time() + 600, row)
    return row


def _sources(v: Any) -> list[dict[str, str]]:
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except ValueError:
            v = []
    return [{"title": str(s.get("title") or s.get("url") or "")[:160], "url": str(s.get("url") or "")}
            for s in (v if isinstance(v, list) else []) if isinstance(s, dict) and s.get("url")][:12]


def public(row: dict[str, Any]) -> dict[str, Any]:
    out = {c: row.get(c) for c in COLUMNS}
    out["sources"] = _sources(row.get("sources"))
    out["label"] = LABEL
    return out


def lookup(pin: Any, *, city: Optional[str] = None, refresh: bool = False, rate_key: Optional[str] = None,
           request_id: str = "-") -> dict[str, Any]:
    """
    Profile for one PIN: the saved row while fresh, otherwise a grounded lookup saved to BigQuery.
    status: ok | not_found | out_of_city | unavailable (lookup failed; nothing saved). Raises ValueError for
    a malformed PIN and RateLimited when rate_key is over its limit.
    """
    pin = str(pin or "").strip()
    if not valid_pin(pin):
        raise ValueError("PIN code must be 6 digits")
    city = (city or gp.default_city()).strip()
    with _mem_lock:
        lock = _fetch_locks.setdefault(pin, threading.Lock())
    with lock:
        old = stored(pin)
        if not refresh and _fresh(old):
            return {**public(old), "cached": True}
        if not _limit_ok(rate_key):
            raise RateLimited("too many PIN lookups")
        try:
            answer = grounded.ask(_prompt(pin, city), request_id=request_id)
            row = normalize(grounded.parse_json(answer.text), pin=pin, city=city)
        except (grounded.GroundingUnavailable, ValueError, TypeError) as exc:
            log.warning("request_id=%s pin area lookup unavailable pin=%s: %s", request_id, pin, exc)
            if old and old.get("status") == "ok":
                return {**public(old), "cached": True, "stale": True,
                        "detail": "Refresh is unavailable right now; showing the last saved profile."}
            return {"pin_code": pin, "city": city, "status": "unavailable", "sources": [], "label": LABEL,
                    "detail": "PIN area lookup is unavailable right now."}
        row.update(sources=answer.sources, search_entry_html=answer.search_entry_html, model=answer.model,
                   fetched_at=datetime.now(timezone.utc).isoformat())
        saved = True
        try:
            _save(row)
        except AreaUnavailable:
            saved = False
            log.warning("request_id=%s pin_areas write failed pin=%s", request_id, pin, exc_info=True)
        with _mem_lock:
            if len(_mem) > 2048:
                _mem.clear()
            _mem[pin] = (time.time() + 600, row)
        log.info("request_id=%s pin area pin=%s status=%s sources=%s saved=%s", request_id, pin, row["status"],
                 len(answer.sources), saved)
        return {**public(row), "cached": False, "saved": saved}


def require_ok(pin: Any, *, request_id: str = "-") -> dict[str, Any]:
    """Profile for a PIN a customer is created with; AreaRejected (message for the admin) unless status ok."""
    p = str(pin or "").strip()
    if not valid_pin(p):
        raise AreaRejected("PIN code must be 6 digits")
    res = lookup(p, request_id=request_id)
    status = res.get("status")
    if status == "ok" and res.get("centroid_lat") is not None and res.get("centroid_lon") is not None:
        return res
    if status == "unavailable":
        raise AreaRejected("The PIN could not be verified right now (Google Search lookup unavailable). Try again shortly.")
    raise AreaRejected(res.get("detail") or f"PIN {p} could not be verified.")
