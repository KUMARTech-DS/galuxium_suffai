"""
Command Center BFF: serves web/ UI + metrics + ads-lab extract/campaign + chat + admin.

GCP-native: metrics always from BigQuery (no local CSV or JSON fallback).
Chat answers from the live metrics summary + UI filters via Vertex Gemini.
Admin unique garments read BigQuery garment_items (the similarity outcome per photo).
Does NOT modify worker/ (extraction) or similarity/ core ingest.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlencode

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [command_center] %(message)s",
)
log = logging.getLogger("command_center")


def _masked_http(status: int, what: str) -> HTTPException:
    """Log the cause (with traceback when inside an except block) and return a generic error with a reference."""
    ref = uuid.uuid4().hex[:12]
    log.error("%s reference=%s", what, ref, exc_info=sys.exc_info()[0] is not None)
    return HTTPException(status, f"This service is temporarily unavailable. Reference: {ref}")

REPO = Path(__file__).resolve().parents[1]
WEB_DIR = REPO / "web"
DATA_DIR = REPO / "command_center_data"
CC_DIR = Path(__file__).resolve().parent
SCRIPTS = CC_DIR / "scripts"
sys.path.insert(0, str(CC_DIR))
sys.path.insert(0, str(SCRIPTS))

import admin_intel  # noqa: E402
import aggregate_metrics as agg  # noqa: E402
import auth_acl  # noqa: E402
import bq_freshness  # noqa: E402
import brand_campaign as brand_camp  # noqa: E402
import aura_agent  # noqa: E402
import aura_guard  # noqa: E402
import campaign_jobs  # noqa: E402
import campaign_service as camp_svc  # noqa: E402
import chatbot  # noqa: E402
import geo_analysis  # noqa: E402
import geo_market  # noqa: E402
import geo_proximity as geo_prox  # noqa: E402
import geo_service  # noqa: E402
import ops_records  # noqa: E402
import pin_areas  # noqa: E402
import retail_api  # noqa: E402
import retail_db  # noqa: E402
import style_assistant as style_asst  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

app = FastAPI(title="SuffAI Command Center", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/agent/"):
        rid = uuid.uuid4().hex[:12]
        log.warning("request_id=%s agent request validation failed: %s", rid, exc.errors()[:3])
        return JSONResponse(status_code=400, content=aura_guard.error_body("invalid_input", rid))
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception):
    """Last resort: log the traceback, return a generic 500 with a reference (never the exception text)."""
    ref = uuid.uuid4().hex[:12]
    log.error("unhandled error %s %s reference=%s", request.method, request.url.path, ref,
              exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(
        status_code=500,
        content={"detail": f"Something went wrong on our side. Reference: {ref}", "request_id": ref},
    )


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _count_extraction_jobs() -> Optional[int]:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        log.debug("DATABASE_URL not set; skipping extraction_jobs count")
        return None
    try:
        # psycopg2's own `with conn` only commits; brand_camp.connect() also closes the connection.
        with brand_camp.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM extraction_jobs")
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        log.exception("Cloud SQL extraction_jobs count failed")
        return None


def compute_metrics() -> dict[str, Any]:
    """Load aggregates from BigQuery only. Fail loudly if BQ is unavailable."""
    if not _env_bool("USE_BQ", True):
        raise _masked_http(503, "metrics: USE_BQ is false; CSV fallback removed, set USE_BQ=true")

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    dataset = os.getenv("BQ_DATASET", "suffai_ops")
    if not project:
        raise _masked_http(503, "metrics: GOOGLE_CLOUD_PROJECT not set")

    try:
        log.info("Loading metrics from BigQuery project=%s dataset=%s", project, dataset)
        customers, orders, commodities = agg.load_from_bq(project, dataset)
        log.info(
            "BQ rows customers=%s orders=%s commodities=%s",
            len(customers),
            len(orders),
            len(commodities),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _masked_http(503, f"BigQuery metrics failed project={project} dataset={dataset}") from exc

    try:
        areas = pin_areas.load_all(project, dataset)
    except pin_areas.AreaUnavailable:
        log.warning("pin_areas unavailable; PIN names and population stay blank until it is readable", exc_info=True)
        areas = {}
    payload = agg.build(customers, orders, commodities, areas)
    jobs_n = _count_extraction_jobs()
    if jobs_n is not None:
        payload["extraction_jobs_count"] = jobs_n
    payload["source"] = "bigquery"
    return payload


METRICS_CACHE_SEC = float(os.getenv("METRICS_CACHE_SEC", "120"))
_metrics_cache: dict[str, Any] = {"fn": None, "at": 0.0, "payload": None}
_metrics_lock = threading.Lock()


def cached_metrics() -> dict[str, Any]:
    """compute_metrics() reused for METRICS_CACHE_SEC (BigQuery load takes seconds; every tab asks for it).

    Returns a private deep copy so per-request brand masking can never touch the shared payload.
    The entry is tied to the current compute_metrics function, so a swapped loader (tests) never sees stale data.
    """
    bq_freshness.poke()
    fn = compute_metrics
    with _metrics_lock:
        hit = _metrics_cache["fn"] is fn and _metrics_cache["payload"] is not None \
            and time.monotonic() - _metrics_cache["at"] < METRICS_CACHE_SEC
        if not hit:
            _metrics_cache.update(fn=fn, at=time.monotonic(), payload=fn())
        return copy.deepcopy(_metrics_cache["payload"])


camp_svc.init(metrics_loader=lambda: cached_metrics())


def _svc_http(exc: camp_svc.ServiceError) -> HTTPException:
    """4xx text is written for the UI; 5xx (storage/config/cloud failures) is masked behind a reference."""
    if exc.status >= 500:
        return _masked_http(exc.status, f"service error code={exc.code} detail={exc.public}")
    return HTTPException(exc.status, exc.public)


class GeoBriefRequest(BaseModel):
    """Aggregated, PII-free geo store payload for Gemini brief + store decision."""

    filters: dict[str, Any] = Field(default_factory=dict)
    kpis: dict[str, Any] = Field(default_factory=dict)
    focus_pin: Optional[dict[str, Any]] = None
    top_pins: list[dict[str, Any]] = Field(default_factory=list)
    trends: dict[str, Any] = Field(default_factory=dict)
    analysis_brand: Optional[str] = None
    role: str = Field(default="admin")


class GeoScoresRequest(BaseModel):
    """Geo filters {pins, age, gender, rfm}; the server scores every PIN they cover."""

    filters: dict[str, Any] = Field(default_factory=dict)
    analysis_brand: Optional[str] = Field(default=None, max_length=64)


class GeoBriefQuery(BaseModel):
    """Brief for one PIN under the Geo filters. Scores come from the server, never from this body."""

    pin: Optional[str] = Field(default=None, max_length=12)
    filters: dict[str, Any] = Field(default_factory=dict)
    analysis_brand: Optional[str] = Field(default=None, max_length=64)


class GeoRoutingRequest(BaseModel):
    pin: Optional[str] = Field(default=None, max_length=12)
    store_name: Optional[str] = Field(default=None, max_length=120)
    analysis_brand: Optional[str] = Field(default=None, max_length=64)


class GeoAreaRequest(BaseModel):
    pin: str = Field(..., min_length=1, max_length=12)
    refresh: bool = False


class GeoMarketRequest(BaseModel):
    pin: str = Field(..., min_length=3, max_length=12)
    analysis_brand: Optional[str] = Field(default=None, max_length=64)
    filters: dict[str, Any] = Field(default_factory=dict)
    refresh: bool = False


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def _token_from_request(
    request: Request,
    authorization: Optional[str] = None,
    x_session_token: Optional[str] = None,
) -> Optional[str]:
    if x_session_token and x_session_token.strip():
        return x_session_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    # Also accept query ?token= for simple static HTML fetches
    q = request.query_params.get("token")
    return q.strip() if q else None


def _require_session(
    request: Request,
    authorization: Optional[str] = None,
    x_session_token: Optional[str] = None,
) -> dict[str, Any]:
    token = _token_from_request(request, authorization, x_session_token)
    sess = auth_acl.get_session(token)
    if not sess:
        raise HTTPException(401, "You are not signed in. Please sign in again.")
    return sess


retail_api.init(metrics_loader=lambda: cached_metrics(), token_resolver=_token_from_request)
retail_api.install(app)

_strip_pii_keys = aura_guard.strip_pii_keys


def _sanitize_geo_brief_payload(body: GeoBriefRequest) -> dict[str, Any]:
    """Allowlist aggregates only — no customer rows, no lat/lon, no personal names."""

    def _commodities(raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return out
        for item in raw[:12]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("value") or item.get("commodity") or "").strip()
            if not name or len(name) > 80:
                continue
            try:
                count = float(item.get("count") or item.get("garments") or 0)
            except (TypeError, ValueError):
                continue
            # Use "commodity" not "name" — "name" is stripped as PII by _strip_pii_keys
            out.append({"commodity": name[:64], "count": count})
        return out

    def _pin_row(raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        pin = str(raw.get("pin") or raw.get("pin_code") or "").strip()
        if not pin:
            return None
        heuristic = str(raw.get("heuristic_decision") or raw.get("decision") or "").strip()
        if heuristic not in {"Yes", "Consider", "No"}:
            heuristic = "Consider"
        row: dict[str, Any] = {
            "pin": pin[:12],
            "area_label": str(raw.get("area_label") or pin)[:80],
            "heuristic_decision": heuristic,
            "decision_prior": heuristic,
        }
        for num_key in (
            "score",
            "est_footfall",
            "demand",
            "hv_demand",
            "catchment_pop",
            "garment_pull",
            "revenue",
            "brand_garments",
            "competitor_garments",
            "brand_share",
        ):
            if num_key not in raw:
                continue
            try:
                row[num_key] = float(raw[num_key])
            except (TypeError, ValueError):
                continue
        commodities = _commodities(raw.get("commodities"))
        if commodities:
            row["commodities"] = commodities
        return row

    filters_in = body.filters if isinstance(body.filters, dict) else {}
    filters = {
        "pins": [str(p)[:12] for p in (filters_in.get("pins") or filters_in.get("pin") or []) if p][:20],
        "age": [str(a)[:32] for a in (filters_in.get("age") or []) if a][:20],
        "gender": [str(g)[:32] for g in (filters_in.get("gender") or []) if g][:10],
        "rfm": [str(r)[:64] for r in (filters_in.get("rfm") or []) if r][:20],
        "brand": [str(b)[:64] for b in (filters_in.get("brand") or []) if b][:20],
    }

    kpis_in = body.kpis if isinstance(body.kpis, dict) else {}
    kpis: dict[str, Any] = {}
    for k in ("customers", "high_value_customers", "orders", "revenue", "garments"):
        if k not in kpis_in:
            continue
        try:
            kpis[k] = float(kpis_in[k])
        except (TypeError, ValueError):
            continue

    trends_in = body.trends if isinstance(body.trends, dict) else {}
    trends = {
        k: str(trends_in[k])[:64]
        for k in ("top_commodity", "top_color", "top_brand", "top_pattern", "top_size")
        if trends_in.get(k)
    }

    top_pins = []
    for item in (body.top_pins or [])[:8]:
        row = _pin_row(item)
        if row:
            top_pins.append(row)

    focus = _pin_row(body.focus_pin) if body.focus_pin else None
    role = str(body.role or "admin").strip().lower()
    if role not in {"brand", "customer", "admin"}:
        role = "admin"
    analysis_brand = str(body.analysis_brand or "").strip()[:64] or None
    if analysis_brand and analysis_brand.lower() in {"competitors", "all", "unknown"}:
        analysis_brand = None

    payload = {
        "filters": filters,
        "kpis": kpis,
        "analysis_brand": analysis_brand,
        "focus_pin": focus,
        "top_pins": top_pins,
        "trends": trends,
        "role": role,
        "notes": {
            "centroid": "fixed_at_pincode_hub",
            "footfall": "estimated_from_filtered_demand",
            "competition": "brand_garments_vs_competitor_garments_in_PIN_under_filters",
            "proximity": f"server_enforced_{geo_prox.PROXIMITY_KM:g}km_rule",
            "pii": "none_in_payload",
        },
    }
    return _strip_pii_keys(payload)


def _parse_geo_brief_model_json(text: str) -> dict[str, Any]:
    """Extract decision JSON from model output; tolerate optional fences."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start_i = raw.find("{")
    end_i = raw.rfind("}")
    if start_i < 0 or end_i <= start_i:
        raise ValueError("no JSON object in model response")
    data = json.loads(raw[start_i : end_i + 1])
    if not isinstance(data, dict):
        raise ValueError("model JSON is not an object")
    decision = str(data.get("decision") or "").strip()
    if decision not in {"Yes", "Consider", "No"}:
        raise ValueError(f"invalid decision: {decision!r}")
    explanation = str(data.get("explanation") or "").strip()
    brief = str(data.get("brief") or "").strip()
    if not explanation:
        raise ValueError("missing explanation")
    if not brief:
        brief = explanation
    confidence = str(data.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    factors = data.get("key_factors") or []
    if not isinstance(factors, list):
        factors = []
    key_factors = [str(x).strip()[:120] for x in factors if str(x).strip()][:8]
    return {
        "decision": decision,
        "confidence": confidence,
        "explanation": explanation[:4000],
        "brief": brief[:4000],
        "key_factors": key_factors,
    }


# ---------------------------------------------------------------------------
# Geo: 4 km proximity guardrail, routing guide, external market check
# ---------------------------------------------------------------------------


def _geo_metrics_or_none() -> Optional[dict[str, Any]]:
    try:
        return camp_svc.load_metrics()
    except camp_svc.ServiceError:
        log.warning("geo metrics unavailable", exc_info=True)
        return None


def _geo_session(request: Request, authorization: Optional[str], x_session_token: Optional[str]) -> dict[str, Any]:
    sess = _require_session(request, authorization, x_session_token)
    if sess.get("role") not in {"brand", "admin"}:
        raise HTTPException(403, "Geo is available to brand and admin logins")
    return sess


def _geo_brand(sess: Optional[dict[str, Any]], requested: Optional[str], metrics: dict[str, Any]) -> Optional[str]:
    role = (sess or {}).get("role") or "admin"
    return geo_analysis.resolve_analysis_brand(
        [g for g in metrics.get("garment_cells") or [] if isinstance(g, dict)],
        role=role,
        session_brand=(sess or {}).get("brand_name"),
        requested=requested,
    )


def _geo_lookup(brand: Optional[str], metrics: dict[str, Any], *, refresh: bool = False, request_id: str = "-") -> dict[str, Any]:
    return geo_prox.locate_brand_stores(
        brand, pin_geo=metrics.get("pin_geo") or {}, refresh=refresh, request_id=request_id
    )


def _geo_stock_mix(metrics: dict[str, Any], store: Optional[dict[str, Any]], sess: Optional[dict[str, Any]],
                   brand: Optional[str]) -> Optional[dict[str, Any]]:
    """Compact routing-guide summary for the store's 4 km catchment (aggregated, PII-free)."""
    if not store:
        return None
    pins = [p for p, _ in geo_prox.catchment_pins(store, metrics.get("pin_geo") or {})]
    if not pins:
        return None
    guide = geo_analysis.routing_guide(
        metrics, pins=pins, role=(sess or {}).get("role") or "admin",
        session_brand=(sess or {}).get("brand_name"), analysis_brand=brand,
    )
    a = guide["assortment"]
    return {
        "store": store.get("name"),
        "catchment_pins": pins[:20],
        "high_value_customers": guide["high_value_customers"],
        "commodity": [m["value"] for m in a["commodity"][:3]],
        "color": [m["value"] for m in a["color"][:3]],
        "pattern": [m["value"] for m in a["pattern"][:2]],
        "size_curve": a["size_curve"],
        "actions": guide["actions"],
    }


def _geo_decision_logic(
    *, focus: dict[str, Any], hv: float, expansion: str, zone: str, km: Optional[float],
    store: Optional[dict[str, Any]], lookup_status: str, market: Optional[dict[str, Any]],
    market_note: Optional[str], final: str, label: str, stock_mix: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    t = geo_prox.PROXIMITY_KM
    steps: list[dict[str, Any]] = [{
        "step": f"High-value demand ({geo_prox.HIGH_VALUE_LABEL})",
        "status": "pass" if hv > 0 else "fail",
        "detail": f"{int(hv)} Champions/Loyal customers in PIN {focus.get('pin')}."
                  + ("" if hv > 0 else " A new store needs high-value demand, so Yes is capped at Consider."),
    }]
    if zone == "service":
        steps.append({
            "step": f"{t:g} km proximity rule", "status": "fail",
            "detail": f"Nearest existing store {store.get('name') if store else ''} is {km:.2f} km away "
                      f"(≤ {t:g} km): opening here would cannibalise it.",
        })
    elif zone == "whitespace":
        steps.append({
            "step": f"{t:g} km proximity rule", "status": "pass",
            "detail": (f"Nearest existing store is {km:.2f} km away (> {t:g} km)." if km is not None
                       else "No existing store of this brand found in the city.") + " Whitespace candidate.",
        })
    else:
        steps.append({
            "step": f"{t:g} km proximity rule", "status": "warn",
            "detail": "Existing store locations could not be verified, so Yes is capped at Consider.",
        })
    steps.append({
        "step": "Demand density & brand fit", "status": "info",
        "detail": f"Score {focus.get('score', 'n/a')}, est. footfall {focus.get('est_footfall', 'n/a')} "
                  f"(simulated), brand share {focus.get('brand_share', 'n/a')}: expansion signal {expansion}.",
    })
    if zone != "service":
        if market and market.get("status") == "ok":
            m = market.get("market") or {}
            steps.append({
                "step": "External market (Google Search)",
                "status": "fail" if m.get("viability") == "unfavourable" else "pass",
                "detail": f"Viability {m.get('viability')}; footfall {(m.get('footfall') or {}).get('level')}, "
                          f"competition {(m.get('competition') or {}).get('level')}. "
                          + (market_note or "External signals can only lower the decision."),
            })
        else:
            steps.append({"step": "External market (Google Search)", "status": "info",
                          "detail": "Not run yet for this PIN; the decision uses internal signals only."})
    elif stock_mix:
        steps.append({
            "step": "Stock routing", "status": "info",
            "detail": "Route " + ", ".join(stock_mix.get("commodity") or ["the core range"])
                      + f" to {stock_mix.get('store')}; colours " + ", ".join(stock_mix.get("color") or ["n/a"]) + ".",
        })
    steps.append({"step": "Decision", "status": "info", "detail": f"{final}: {label}."})
    return steps


def _geo_guardrail(payload: dict[str, Any], sess: Optional[dict[str, Any]], request_id: str) -> dict[str, Any]:
    """Recompute proximity server-side for focus + top PINs; never trust client distances."""
    metrics = _geo_metrics_or_none() or {}
    brand = payload.get("analysis_brand") or (_geo_brand(sess, None, metrics) if metrics else None)
    lookup = _geo_lookup(brand, metrics, request_id=request_id) if metrics else {"status": "unavailable", "stores": []}
    pin_geo = metrics.get("pin_geo") or {}
    focus = payload.get("focus_pin") or {}
    rows = [r for r in [focus, *(payload.get("top_pins") or [])] if r]
    for r in rows:
        r["expansion_decision"] = r.get("heuristic_decision") or "No"
    geo_prox.apply_proximity(rows, lookup, pin_geo)

    ctx: dict[str, Any] = {"lookup": lookup, "brand": brand, "metrics": metrics}
    if not focus:
        return ctx
    c = geo_prox.pin_centroid(pin_geo, focus["pin"])
    store, km = geo_prox.nearest_store(*c, lookup.get("stores") or []) if c else (None, None)
    zone = focus["proximity_zone"]
    market = None
    market_note = None
    final, label = focus["decision"], focus["decision_label"]
    if zone != "service" and metrics:
        market = geo_market.cached(focus["pin"], brand=brand, role=(sess or {}).get("role") or "admin")
        adjusted, market_note = geo_market.adjust_decision(final, market)
        if adjusted != final:
            final = adjusted
            label = geo_prox.UNVERIFIED_LABEL if zone == "unverified" else geo_prox.WHITESPACE_LABELS.get(final, final)
    stock_mix = _geo_stock_mix(metrics, store, sess, brand) if zone == "service" else None
    focus["binding_decision"] = final
    focus["decision_label"] = label
    ctx.update(
        final=final, label=label, mode=focus["mode"], zone=zone, store=store, km=km, market=market,
        stock_mix=stock_mix,
        decision_logic=_geo_decision_logic(
            focus=focus, hv=focus.get("hv_demand") or 0, expansion=focus["expansion_decision"], zone=zone, km=km,
            store=store, lookup_status=lookup.get("status") or "unavailable", market=market,
            market_note=market_note, final=final, label=label, stock_mix=stock_mix,
        ),
    )
    payload["proximity_rule"] = {
        "threshold_km": geo_prox.PROXIMITY_KM,
        "store_lookup": lookup.get("status"),
        "binding_decision": final,
        "decision_label": label,
        "mode": focus["mode"],
    }
    if stock_mix:
        payload["stock_mix"] = {k: v for k, v in stock_mix.items() if k != "catchment_pins"}
    if market and market.get("status") == "ok":
        payload["external_market"] = market.get("market")
    log.info("request_id=%s geo guardrail pin=%s zone=%s km=%s expansion=%s final=%s",
             request_id, focus.get("pin"), zone, km, focus["expansion_decision"], final)
    return ctx


def _geo_proximity_response(ctx: dict[str, Any]) -> dict[str, Any]:
    if "final" not in ctx:
        return {}
    return {
        "decision_label": ctx["label"],
        "mode": ctx["mode"],
        "proximity": {
            "threshold_km": geo_prox.PROXIMITY_KM,
            "zone": ctx["zone"],
            "nearest_store": geo_prox.store_public(ctx["store"], ctx["km"]),
            "nearest_store_km": round(ctx["km"], 2) if ctx["km"] is not None else None,
            "store_lookup": ctx["lookup"].get("status"),
            "store_count": len(ctx["lookup"].get("stores") or []),
        },
        "decision_logic": ctx["decision_logic"],
        "stock_mix": ctx["stock_mix"],
        "external_market": (ctx["market"] or {}).get("market") if (ctx["market"] or {}).get("status") == "ok" else None,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "suffai-command-center",
        "use_bq": _env_bool("USE_BQ", True),
        "project": bool(os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")),
        "extraction_url": bool(os.getenv("EXTRACTION_URL")),
        "gcs_bucket": bool(os.getenv("GCS_IMAGE_BUCKET")),
        "database_url": bool(os.getenv("DATABASE_URL")),
        "enable_vertex": _env_bool("ENABLE_VERTEX", True),
        "vertex_chat_model": os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash"),
        "chat": True,
        "similarity_url": bool(os.getenv("SIMILARITY_URL")),
        "ads_lab_campaign": True,
        "admin_unique_garments": "bigquery",
        "style_assistant": True,
        "auth": True,
        "geo_brief": True,
        "retail": retail_db.configured(),
    }


@app.post("/v1/auth/login")
def auth_login(body: LoginRequest):
    try:
        sess = auth_acl.authenticate(body.username, body.password)
    except PermissionError:
        raise HTTPException(401, "Invalid username or password") from None
    except ValueError:
        raise HTTPException(400, "Username and password are required") from None
    except Exception as exc:
        raise _masked_http(503, "auth login failed") from exc
    log.info("auth login ok user=%s role=%s", sess.get("username"), sess.get("role"))
    return {"ok": True, **sess}


@app.get("/v1/auth/me")
def auth_me(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_session(request, authorization, x_session_token)
    return {"ok": True, **sess}


@app.post("/v1/auth/logout")
def auth_logout(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    token = _token_from_request(request, authorization, x_session_token)
    auth_acl.destroy_session(token)
    return {"ok": True}


@app.get("/v1/metrics")
def metrics(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
):
    """Geo metrics from BQ (brand/admin session; brand role gets Competitors masking)."""
    sess = _geo_session(request, authorization, x_session_token)
    brand = str(sess.get("brand_name") or "").strip()
    if sess.get("role") == "brand" and not brand:
        raise HTTPException(403, "This brand account has no brand name assigned")
    try:
        payload = cached_metrics()
    except HTTPException:
        raise
    except Exception as exc:
        raise _masked_http(500, "metrics endpoint unexpected error") from exc
    if sess.get("role") == "brand":
        try:
            payload = auth_acl.mask_metrics_for_brand(payload, brand)
        except Exception as exc:
            # Fail closed: never hand a brand login unmasked competitor data.
            raise _masked_http(500, "metrics brand masking failed") from exc
    return payload


@app.post("/v1/ads-lab/extract")
async def ads_lab_extract(
    request: Request,
    files: list[UploadFile] = File(...),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    try:
        camp_svc.require_campaign_role(_require_session(request, authorization, x_session_token))
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    extraction_url = (os.getenv("EXTRACTION_URL") or "").rstrip("/")
    bucket = os.getenv("GCS_IMAGE_BUCKET") or ""
    if not extraction_url:
        raise _masked_http(503, "ads-lab extract: EXTRACTION_URL not set")
    if not bucket:
        raise _masked_http(503, "ads-lab extract: GCS_IMAGE_BUCKET not set")

    try:
        from google.cloud import storage  # type: ignore
        import google.auth.transport.requests  # type: ignore
        import google.oauth2.id_token  # type: ignore
        import urllib.request
    except ImportError as exc:
        raise _masked_http(503, "ads-lab extract: missing GCP client libraries") from exc

    try:
        client = storage.Client()
        bkt = client.bucket(bucket)
    except Exception as exc:
        raise _masked_http(503, f"ads-lab extract: GCS client init failed bucket={bucket}") from exc

    results: list[dict[str, Any]] = []

    auth_req = google.auth.transport.requests.Request()
    try:
        token = google.oauth2.id_token.fetch_id_token(auth_req, extraction_url)
    except Exception:
        log.warning("Could not fetch ID token for EXTRACTION_URL; calling without auth")
        token = None

    for f in files:
        if not f.content_type or not f.content_type.startswith("image/"):
            log.warning("Skipping non-image upload filename=%s type=%s", f.filename, f.content_type)
            continue
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(
            f.content_type, "jpg"
        )
        object_key = f"demo/{uuid.uuid4().hex}.{ext}"
        data = await f.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, f"{f.filename} too large (max 10MB)")

        try:
            blob = bkt.blob(object_key)
            blob.upload_from_string(data, content_type=f.content_type)
            log.info("Uploaded to GCS gs://%s/%s filename=%s", bucket, object_key, f.filename)
        except Exception:
            log.exception("GCS upload failed key=%s", object_key)
            results.append(
                {
                    "filename": f.filename,
                    "object_key": object_key,
                    "ok": False,
                    "error": "Upload failed. Please try again.",
                }
            )
            continue

        idem = f"ads-lab-{uuid.uuid4().hex}"
        body = json.dumps(
            {
                "gcs_object_key": object_key,
                "gcs_bucket": bucket,
                "target_type": "garment",
                "idempotency_key": idem,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{extraction_url}/v1/extract",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            log.info(
                "Extract ok filename=%s status=%s image_id=%s",
                f.filename,
                payload.get("status"),
                payload.get("image_id"),
            )
        except Exception:
            log.exception("Extract call failed filename=%s key=%s", f.filename, object_key)
            results.append(
                {
                    "filename": f.filename,
                    "object_key": object_key,
                    "ok": False,
                    "error": "Attribute extraction failed. Please try again.",
                }
            )
            continue

        attrs = payload.get("result") or {}
        results.append(
            {
                "filename": f.filename,
                "object_key": object_key,
                "ok": True,
                "status": payload.get("status"),
                "image_id": payload.get("image_id"),
                "attributes": camp_svc.extracted_attributes(payload, None),
                "raw_result": attrs,
            }
        )

    if not results:
        raise HTTPException(400, "No valid images uploaded")

    return {"ok": True, "items": results}


def _parse_pin_codes(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(p).strip() for p in data if str(p).strip()]
    except Exception:
        pass
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


@app.post("/v1/ads-lab/campaign")
async def ads_lab_campaign(
    request: Request,
    files: list[UploadFile] = File(...),
    brand_name: str = Form(...),
    pin_codes: str = Form(...),
    age_group: str = Form(""),
    gender: str = Form(""),
    rfm_segment: str = Form(""),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """
    Brand SKU campaign: GCS brands/{brand}/… → extract (brand overwritten) →
    compare-pins → rank with PIN + age/gender/RFM metrics profile.
    Does not modify suffai-extraction.
    """
    sess = _require_session(request, authorization, x_session_token)
    if not files:
        raise HTTPException(400, "At least one image file is required")
    images = await _read_images(files)
    try:
        return await run_in_threadpool(
            camp_svc.create_campaign_from_images,
            sess,
            images,
            brand_name=brand_name,
            pin_codes=_parse_pin_codes(pin_codes),
            age_group=age_group,
            gender=gender,
            rfm_segment=rfm_segment,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc


async def _read_images(files: list[UploadFile]) -> list[camp_svc.ImageInput]:
    out: list[camp_svc.ImageInput] = []
    for f in files or []:
        if not f.content_type or not f.content_type.startswith("image/"):
            continue
        out.append(
            camp_svc.ImageInput(filename=f.filename or "sku", content_type=f.content_type, data=await f.read())
        )
    return out


@app.get("/v1/ads-lab/catalog-skus")
def ads_lab_catalog_skus(
    request: Request,
    brand_name: str = Query(""),
    q: str = Query(""),
    category: str = Query(""),
    with_image_only: bool = Query(False),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Style × colour groups from the brand's warehouse catalog for ranking (max 15 picks)."""
    import retail_catalog
    import vocab
    from retail_db import RetailError, RetailUnavailable
    from retail_scope import Scope

    sess = _require_session(request, authorization, x_session_token)
    try:
        brand = camp_svc.resolve_brand(sess, brand_name)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    empty = {"ok": True, "items": [], "total": 0, "categories": [], "brand_name": brand,
             "has_catalog": False, "max_skus": campaign_jobs.max_catalog_skus()}
    if not retail_db.configured():
        return empty
    try:
        scope = Scope(str(sess.get("role") or "brand"), str(sess.get("username") or ""),
                      vocab.value_key(brand), brand)
        data = retail_catalog.list_style_colours(
            scope, q=q, category=category, with_image_only=with_image_only,
        )
    except RetailError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except RetailUnavailable:
        return empty
    except Exception as exc:
        log.exception("catalog-skus failed brand=%s", brand)
        raise HTTPException(503, "Catalog is temporarily unavailable.") from exc
    data["ok"] = True
    data["max_skus"] = campaign_jobs.max_catalog_skus()
    return data


@app.post("/v1/ads-lab/campaign-jobs")
async def ads_lab_submit_campaign_job(
    request: Request,
    files: list[UploadFile] = File(...),
    brand_name: str = Form(""),
    pin_codes: str = Form(...),
    age_group: str = Form(""),
    gender: str = Form(""),
    rfm_segment: str = Form(""),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Async extract & rank: uploads images, returns the job with counts + ETA immediately."""
    sess = _require_session(request, authorization, x_session_token)
    images = await _read_images(files)
    try:
        job = await run_in_threadpool(
            campaign_jobs.submit_job,
            sess,
            images,
            brand_name=brand_name,
            pin_codes=_parse_pin_codes(pin_codes),
            age_group=age_group,
            gender=gender,
            rfm_segment=rfm_segment,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "job": job}


class CatalogJobBody(BaseModel):
    brand_name: str = ""
    pin_codes: list[str] = Field(default_factory=list)
    age_group: str = ""
    gender: str = ""
    rfm_segment: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/v1/ads-lab/campaign-jobs/catalog")
async def ads_lab_submit_catalog_job(
    body: CatalogJobBody,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Async rank of style × colour SKUs picked from the warehouse catalog (max 15)."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        job = await run_in_threadpool(
            campaign_jobs.submit_catalog_job,
            sess,
            body.items,
            brand_name=body.brand_name,
            pin_codes=body.pin_codes,
            age_group=body.age_group,
            gender=body.gender,
            rfm_segment=body.rfm_segment,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "job": job}


@app.get("/v1/ads-lab/campaign-jobs")
def ads_lab_list_campaign_jobs(
    request: Request,
    active: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_session(request, authorization, x_session_token)
    try:
        jobs = campaign_jobs.list_jobs(sess, active=active, limit=limit)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


@app.get("/v1/ads-lab/campaign-jobs/{job_id}")
def ads_lab_get_campaign_job(
    job_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_session(request, authorization, x_session_token)
    try:
        job = campaign_jobs.get_job(sess, job_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "job": job}


@app.post("/v1/ads-lab/campaign-jobs/{job_id}/cancel")
def ads_lab_cancel_campaign_job(
    job_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Stop a queued / running extract & rank job and discard its campaign (409 once it has finished)."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        job = campaign_jobs.cancel_job(sess, job_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "job": job}


@app.post("/internal/campaign-jobs/{job_id}/run", include_in_schema=False)
def internal_run_campaign_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
):
    """Cloud Tasks worker. Accepts only an OIDC token minted for TASKS_SERVICE_ACCOUNT."""
    if not campaign_jobs.verify_task_request(authorization):
        raise HTTPException(403, "Forbidden")
    try:
        jid = str(uuid.UUID(job_id))
    except ValueError:
        return {"ok": True, "skipped": True}
    try:
        return campaign_jobs.run_chunk(jid)
    except Exception as exc:
        log.exception("campaign job chunk failed job=%s", jid)
        raise HTTPException(500, "chunk failed; will retry") from exc


@app.get("/v1/ads-lab/campaigns")
def ads_lab_list_campaigns(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None),
):
    sess = _require_session(request, authorization, x_session_token)
    try:
        rows = camp_svc.list_for(sess, status=status, limit=limit)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "campaigns": rows, "count": len(rows)}


@app.get("/v1/ads-lab/campaigns/{campaign_id}")
def ads_lab_campaign_detail(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_session(request, authorization, x_session_token)
    try:
        data = camp_svc.get_with_skus(sess, campaign_id, with_images=True)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, **data}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/start")
def ads_lab_campaign_start(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Publish ranked campaign to Style Assistant for filter-matched customers (1P)."""
    campaign, _ = _load_campaign_for(campaign_id, request, authorization, x_session_token)
    campaign_id = campaign["campaign_id"]
    if campaign.get("status") not in {"ranked", "started"}:
        raise HTTPException(
            409,
            f"Campaign must be ranked before it can start (status={campaign.get('status')})",
        )
    try:
        brand_camp.set_campaign_status(campaign_id, "started")
        result = style_asst.materialize_campaign_recommendations(campaign_id)
    except LookupError as exc:
        raise HTTPException(404, "Campaign not found") from exc
    except Exception as exc:
        try:
            brand_camp.set_campaign_status(campaign_id, "error", "Campaign start failed")
        except Exception:
            log.exception("campaign start: could not mark id=%s as error", campaign_id)
        raise _masked_http(503, f"campaign start failed id={campaign_id}") from exc
    try:
        refreshed = brand_camp.get_campaign(campaign_id) or campaign
    except Exception:
        log.warning("campaign start: refresh failed id=%s; returning the pre-start row", campaign_id, exc_info=True)
        refreshed = {**campaign, "status": "started"}
    return {"ok": True, "campaign": refreshed, "materialize": result}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/ignore")
def ads_lab_campaign_ignore(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_session(request, authorization, x_session_token)
    try:
        campaign = camp_svc.discard(sess, campaign_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "campaign": campaign}


class CampaignConfigRequest(BaseModel):
    campaign_name: str = Field(..., min_length=1, max_length=120)
    objective: str
    budget_inr: float = Field(..., gt=0)
    duration_days: int = Field(..., ge=1, le=365)
    start_date: Optional[str] = None
    selected_sku_ids: list[str] = Field(default_factory=list)
    creative: dict[str, Any] = Field(default_factory=dict)
    source_context: Optional[dict[str, Any]] = None
    channels: Optional[list[str]] = None
    message_template: Optional[dict[str, str]] = None
    platform_fee_inr: Optional[float] = Field(None, gt=0)


class CampaignEstimateRequest(BaseModel):
    objective: Optional[str] = None
    budget_inr: float = Field(0, ge=0)
    duration_days: int = Field(14, ge=1, le=365)
    channels: Optional[list[str]] = None


class BudgetRecommendationRequest(BaseModel):
    objective: Optional[str] = None
    duration_days: int = Field(14, ge=1, le=365)
    channels: Optional[list[str]] = None


class FeeRecommendationRequest(BaseModel):
    objective: Optional[str] = None
    duration_days: int = Field(14, ge=1, le=365)
    channels: Optional[list[str]] = None
    media_budget_inr: Optional[float] = Field(None, ge=0)


def _load_campaign_for(
    campaign_id: str,
    request: Request,
    authorization: Optional[str],
    x_session_token: Optional[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    sess = _require_session(request, authorization, x_session_token)
    try:
        campaign = camp_svc.load_campaign(sess, campaign_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return campaign, sess


@app.patch("/v1/ads-lab/campaigns/{campaign_id}/config")
def ads_lab_campaign_config(
    campaign_id: str,
    body: CampaignConfigRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Save campaign name, objective, SKUs, creative, budget and schedule (status → configured)."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        campaign = camp_svc.configure(
            sess,
            campaign_id,
            campaign_name=body.campaign_name,
            objective=body.objective,
            budget_inr=body.budget_inr,
            duration_days=body.duration_days,
            start_date=body.start_date,
            selected_sku_ids=body.selected_sku_ids,
            creative=body.creative,
            source_context=body.source_context,
            channels=body.channels,
            message_template=body.message_template,
            platform_fee_inr=body.platform_fee_inr,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "campaign": campaign}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/budget-recommendation")
def ads_lab_budget_recommendation(
    campaign_id: str,
    body: BudgetRecommendationRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Simulated budget recommendation from audience, objective, duration and channels."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        rec = camp_svc.budget_recommendation(
            sess, campaign_id, objective=body.objective,
            duration_days=body.duration_days, channels=body.channels,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "recommendation": rec}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/fee-recommendation")
def ads_lab_fee_recommendation(
    campaign_id: str,
    body: FeeRecommendationRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Cold-start 1P platform-fee recommendation; media spend stays separate."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        rec = camp_svc.fee_recommendation(
            sess, campaign_id, objective=body.objective, duration_days=body.duration_days,
            channels=body.channels, media_budget_inr=body.media_budget_inr,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "recommendation": rec}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/export")
def ads_lab_campaign_export(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """3P: mark exported and return the ranked SKUs + audience for running on another platform."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        data = camp_svc.export_3p(sess, campaign_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, **data}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/estimate")
def ads_lab_campaign_estimate(
    campaign_id: str,
    body: CampaignEstimateRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Simulated outcome ranges from budget, duration and audience size (nothing persisted)."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        est = camp_svc.estimate(
            sess,
            campaign_id,
            objective=body.objective,
            budget_inr=body.budget_inr,
            duration_days=body.duration_days,
            channels=body.channels,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    return {"ok": True, "estimate": est}


@app.post("/v1/ads-lab/campaigns/{campaign_id}/launch")
def ads_lab_campaign_launch(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """
    Simulated launch: persist status + final synthetic KPIs + daily series.
    No Style Assistant recommendations are created and nothing is delivered to customers.
    """
    sess = _require_session(request, authorization, x_session_token)
    try:
        result = camp_svc.launch(sess, campaign_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    if result["already_launched"]:
        return {"ok": True, "campaign": result["campaign"], "already_launched": True}
    return {"ok": True, "campaign": result["campaign"], "impact": result["impact"]}


@app.get("/v1/ads-lab/campaigns/{campaign_id}/monitor")
def ads_lab_campaign_monitor(
    campaign_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Campaign config + selected SKUs + simulated KPIs + daily series + insights."""
    sess = _require_session(request, authorization, x_session_token)
    try:
        return camp_svc.monitor_payload(sess, campaign_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc


def _require_admin(request: Request, authorization: Optional[str], x_session_token: Optional[str]) -> dict:
    sess = _require_session(request, authorization, x_session_token)
    if sess.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return sess


def _csv_param(v: Optional[str]) -> list[str]:
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _no_store(payload: Any) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _intel_call(fn, *args, **kwargs) -> JSONResponse:
    try:
        return _no_store({"ok": True, **fn(*args, **kwargs)})
    except admin_intel.IntelUnavailable as exc:
        raise _masked_http(503, f"admin intel: {exc}") from exc


@app.get("/v1/admin/intel/customers-overview")
def admin_intel_customers_overview(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    rfm: Optional[str] = Query(None),
    pin: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
):
    _require_admin(request, authorization, x_session_token)
    return _intel_call(admin_intel.customers_overview, rfm=_csv_param(rfm), pins=_csv_param(pin),
                       brands=_csv_param(brand))


@app.get("/v1/admin/intel/customers")
def admin_intel_customers(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    q: str = Query("", max_length=80),
    rfm: Optional[str] = Query(None),
    pin: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    qualified: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
):
    _require_admin(request, authorization, x_session_token)
    return _intel_call(
        admin_intel.customers_page, q=q, rfm=_csv_param(rfm), pins=_csv_param(pin),
        brands=_csv_param(brand), qualified_only=qualified, page=page, size=size,
    )


@app.get("/v1/admin/intel/customers/{customer_id}")
def admin_intel_customer_detail(
    customer_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    _require_admin(request, authorization, x_session_token)
    try:
        detail = admin_intel.customer_detail(customer_id)
    except admin_intel.IntelUnavailable as exc:
        raise _masked_http(503, f"admin intel: {exc}") from exc
    if detail is None:
        raise HTTPException(404, "Customer not found")
    return _no_store({"ok": True, "customer": detail})


@app.post("/v1/admin/intel/customers/{customer_id}/reveal-phone")
def admin_intel_reveal_phone(
    customer_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_admin(request, authorization, x_session_token)
    try:
        phone = admin_intel.reveal_phone(customer_id)
    except admin_intel.IntelUnavailable as exc:
        raise _masked_http(503, f"admin intel: {exc}") from exc
    if phone is None:
        raise HTTPException(404, "No phone on file")
    log.info(
        "admin=%s customer=%s action=reveal_phone",
        sess.get("username") or "?", admin_intel.short_id(customer_id),
    )
    return _no_store({"ok": True, "phone": phone})


@app.get("/v1/admin/intel/garments")
def admin_intel_garments(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    commodity: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    fabric: Optional[str] = Query(None),
    pattern: Optional[str] = Query(None),
    size_filter: Optional[str] = Query(None, alias="garment_size"),
    pin: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
):
    _require_admin(request, authorization, x_session_token)
    filters = {
        "commodity": _csv_param(commodity), "brand": _csv_param(brand), "material": _csv_param(fabric),
        "pattern": _csv_param(pattern), "size": _csv_param(size_filter),
    }
    return _intel_call(admin_intel.garments, filters=filters, pins=_csv_param(pin), page=page, size=size)


@app.get("/v1/admin/intel/brands")
def admin_intel_brands(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    brands: Optional[str] = Query(None),
    pin: Optional[str] = Query(None),
    rfm: Optional[str] = Query(None),
    commodity: Optional[str] = Query(None),
    menu: Optional[str] = Query(None),
):
    _require_admin(request, authorization, x_session_token)
    return _intel_call(
        admin_intel.brands, brand_list=_csv_param(brands), pins=_csv_param(pin), rfm=_csv_param(rfm),
        commodity=_csv_param(commodity), menu=_csv_param(menu),
    )


@app.get("/v1/admin/intel/campaigns")
def admin_intel_campaigns(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    brand: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    pin: Optional[str] = Query(None),
    rfm: Optional[str] = Query(None),
):
    _require_admin(request, authorization, x_session_token)
    return _intel_call(
        admin_intel.campaigns, brand=_csv_param(brand), status=_csv_param(status),
        pins=_csv_param(pin), rfm=_csv_param(rfm),
    )


class AdminCustomerCreate(BaseModel):
    name: str = Field(..., max_length=120)
    phone: str = Field(..., max_length=20)
    address: str = Field(..., max_length=240)
    age: int
    gender: str = Field(..., max_length=12)
    pin_code: str = Field(..., max_length=6)
    create_login: bool = False


class AdminOrderCreate(BaseModel):
    customer_id: str = Field(..., max_length=64)
    scheduled_date: Optional[str] = Field(default=None, max_length=10)
    service: str = Field(..., max_length=60)
    total_number_of_clothes: int
    amount: Optional[float] = None


def _records_call(fn, *args, **kwargs) -> JSONResponse:
    try:
        return _no_store({"ok": True, **fn(*args, **kwargs)})
    except ops_records.RecordError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (ops_records.RecordsUnavailable, admin_intel.IntelUnavailable) as exc:
        raise _masked_http(503, f"admin records: {exc}") from exc


def _clear_metrics_cache() -> None:
    camp_svc.clear_metrics_cache()
    with _metrics_lock:
        _metrics_cache.update(payload=None, at=0.0)


def _after_record_write() -> None:
    """New customers / orders must show on every tab at once, not after the cache TTLs."""
    admin_intel.clear_cache()
    _clear_metrics_cache()


bq_freshness.on_change(_after_record_write)


@app.get("/v1/admin/records/options")
def admin_record_options(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    _require_admin(request, authorization, x_session_token)
    return _records_call(lambda: {"options": ops_records.form_options(), "new_segment": ops_records.NEW_SEGMENT,
                                  "photo_pattern": ops_records.PHOTO_PATTERN,
                                  "upload_prefix": ops_records.upload_prefix()})


@app.post("/v1/admin/customers")
def admin_create_customer(
    body: AdminCustomerCreate,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_admin(request, authorization, x_session_token)
    out = _records_call(ops_records.create_customer, body.model_dump(), admin=sess.get("username") or "?",
                        request_id=uuid.uuid4().hex[:12])
    _after_record_write()
    return out


def _recompute_rfm_after_order(admin: str) -> None:
    try:
        if ops_records.recompute_rfm(admin=admin)["applied"]:
            _after_record_write()
    except Exception as exc:  # the order is saved; segments catch up on the next recompute
        log.warning("rfm recompute after order failed: %s", type(exc).__name__)


@app.post("/v1/admin/orders")
def admin_create_order(
    body: AdminOrderCreate,
    request: Request,
    background: BackgroundTasks,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    sess = _require_admin(request, authorization, x_session_token)
    admin = sess.get("username") or "?"
    out = _records_call(ops_records.create_order, body.model_dump(), admin=admin)
    _after_record_write()
    background.add_task(_recompute_rfm_after_order, admin)
    return out


@app.get("/v1/admin/orders")
def admin_list_orders(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    customer_id: Optional[str] = Query(None, max_length=64),
    pending: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    _require_admin(request, authorization, x_session_token)
    return _records_call(ops_records.list_orders, customer_id=(customer_id or "").strip() or None,
                         pending_only=pending, page=page, size=size)


@app.get("/v1/admin/campaigns")
def admin_list_campaigns(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    limit: int = Query(100, ge=1, le=500),
):
    sess = _require_session(request, authorization, x_session_token)
    if sess.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    try:
        rows = brand_camp.list_campaigns(brand_name=None, limit=limit)
    except Exception as exc:
        raise _masked_http(503, "admin campaigns failed") from exc
    return {"ok": True, "campaigns": rows, "count": len(rows)}


@app.get("/v1/style-assistant/feed")
def style_assistant_feed(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    limit: int = Query(24, ge=1, le=60),
    brand: Optional[str] = Query(None, max_length=120),
):
    """Saved Style Assistant picks from every campaign the customer was ranked for (read only)."""
    sess = _require_session(request, authorization, x_session_token)
    if sess.get("role") != "customer":
        raise HTTPException(403, "Style Assistant is available to customer accounts only")
    user_uuid = (sess.get("customer_id") or "").strip()
    if not user_uuid:
        raise HTTPException(400, "This customer account has no customer ID")
    try:
        payload = style_asst.customer_style_feed(user_uuid, brand=brand, limit=limit)
    except Exception as exc:
        raise _masked_http(503, "style feed failed") from exc
    return payload


@app.get("/v1/admin/unique-summary")
def admin_unique_summary(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    pin: Optional[str] = Query(None, max_length=12),
    brand: Optional[str] = Query(None, max_length=80),
):
    """Unique garments by current customer PIN and brand, from BigQuery garment_items."""
    _require_admin(request, authorization, x_session_token)
    return _intel_call(admin_intel.unique_garments, pin=pin, brand=brand, limit=1)


@app.get("/v1/admin/unique-garments")
def admin_unique_garments(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    pin: Optional[str] = Query(None, max_length=12),
    brand: Optional[str] = Query(None, max_length=80),
    limit: int = Query(50, ge=1, le=200),
):
    """Most recently photographed unique garments, from BigQuery garment_items."""
    _require_admin(request, authorization, x_session_token)
    return _intel_call(admin_intel.unique_garments, pin=pin, brand=brand, limit=limit)


def _geo_scoring_session(request: Request, authorization: Optional[str], x_session_token: Optional[str]) -> dict[str, Any]:
    sess = _geo_session(request, authorization, x_session_token)
    if sess.get("role") == "brand" and not str(sess.get("brand_name") or "").strip():
        raise HTTPException(403, "This brand account has no brand name assigned")
    return sess


@app.post("/v1/geo/scores")
def geo_scores(
    body: GeoScoresRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """
    Store score and Yes / Consider / No for every PIN the Geo filters cover (the page only draws them).
    Reads the store cache only: PINs stay unverified until GET /v1/geo/stores has looked the brand up.
    """
    sess = _geo_scoring_session(request, authorization, x_session_token)
    request_id = uuid.uuid4().hex[:12]
    try:
        out = geo_service.analysis_for(
            role=sess.get("role") or "admin", session_brand=sess.get("brand_name"), filters=body.filters,
            analysis_brand=body.analysis_brand, top_n=None, stores="cached", request_id=request_id,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    except Exception as exc:
        raise _masked_http(503, f"geo scores failed request_id={request_id}") from exc
    return {"ok": True, "request_id": request_id, "threshold_km": geo_prox.PROXIMITY_KM, **out}


@app.post("/v1/geo/brief")
def geo_brief(
    body: GeoBriefQuery,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """
    Gemini store decision + Market brief for one PIN under the Geo filters. The server scores the PINs
    (same rows as /v1/geo/scores); only aggregates reach the model.
    """
    sess = _geo_scoring_session(request, authorization, x_session_token)
    try:
        payload = geo_service.brief_input(
            role=sess.get("role") or "admin", session_brand=sess.get("brand_name"), pin=body.pin,
            filters=body.filters, analysis_brand=body.analysis_brand,
        )
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    except Exception as exc:
        raise _masked_http(503, "geo brief scoring failed") from exc
    return geo_brief_core(GeoBriefRequest(**payload), sess)


def geo_brief_core(body: GeoBriefRequest, sess: Optional[dict[str, Any]]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    role = (sess or {}).get("role") or body.role or "admin"

    try:
        body.role = str(role)
        if sess and sess.get("role") == "brand" and sess.get("brand_name"):
            # Brand sessions always analyze their own brand (ignore spoofed analysis_brand)
            body.analysis_brand = str(sess.get("brand_name"))
        payload = _sanitize_geo_brief_payload(body)
    except Exception as exc:
        log.exception("request_id=%s geo_brief sanitize failed", request_id)
        raise HTTPException(400, f"Invalid geo brief payload. Reference: {request_id}") from exc

    focus = payload.get("focus_pin") or {}
    if focus and not focus.get("heuristic_decision"):
        focus["heuristic_decision"] = geo_analysis.decision_for_row(focus)
    try:
        geo_ctx = _geo_guardrail(payload, sess, request_id)
    except Exception:
        log.exception("request_id=%s geo guardrail failed; capping as unverified", request_id)
        geo_ctx = {}
        for r in [focus, *(payload.get("top_pins") or [])]:
            if r:
                r["expansion_decision"] = r.get("heuristic_decision") or "No"
                r["decision"], r["decision_label"], r["mode"] = geo_prox.gate_decision(
                    r["expansion_decision"], "unverified")
                r["proximity_zone"] = "unverified"
                r["binding_decision"] = r["decision"]
    heuristic = focus.get("binding_decision") or geo_analysis.decision_for_row(focus)
    log.info(
        "request_id=%s geo_brief start role=%s brand=%s focus_pin=%s "
        "brand_share=%s brand_g=%s comp_g=%s heuristic=%s",
        request_id,
        role,
        payload.get("analysis_brand"),
        focus.get("pin"),
        focus.get("brand_share"),
        focus.get("brand_garments"),
        focus.get("competitor_garments"),
        heuristic,
    )

    def _fallback_payload(reason: str, cause: str = "") -> dict[str, Any]:
        """Always return usable decision+brief (HTTP 200) when Vertex fails. `cause` is logged only."""
        explanation = (
            f"Heuristic decision ({heuristic}) for PIN {focus.get('pin') or 'n/a'}: "
            f"brand_share={focus.get('brand_share')}, "
            f"brand_garments={focus.get('brand_garments')}, "
            f"competitor_garments={focus.get('competitor_garments')}, "
            f"est_footfall={focus.get('est_footfall')}, demand={focus.get('demand')}."
        )
        commodities = focus.get("commodities") or []
        top_c = ", ".join(
            f"{c.get('commodity') or c.get('name')} ({c.get('count')})"
            for c in commodities[:3]
            if isinstance(c, dict)
        )
        brief = (
            f"Open store? {heuristic} ({focus.get('decision_label') or heuristic}) at "
            f"{focus.get('area_label') or focus.get('pin') or 'hub'} "
            f"(PIN {focus.get('pin') or '—'}). Estimated footfall ~{focus.get('est_footfall') or 0} (simulated). "
            f"Brand vs competitors: share {focus.get('brand_share') if focus.get('brand_share') is not None else 'n/a'}. "
            f"Commodities: {top_c or 'none in filter slice'}."
        )
        if focus.get("proximity_zone") == "service":
            explanation = (
                f"PIN {focus.get('pin')} is within {geo_prox.PROXIMITY_KM:g} km of an existing store, so the "
                f"answer is No ({geo_prox.SERVICE_LABEL}). " + explanation
            )
        log.warning(
            "request_id=%s geo_brief fallback decision=%s reason=%s cause=%s",
            request_id,
            heuristic,
            reason,
            cause,
        )
        return {
            "ok": True,
            "fallback": True,
            "reason": reason,
            "detail": chatbot.ai_unavailable_detail(request_id),
            "request_id": request_id,
            "decision": heuristic,
            "confidence": "low",
            "explanation": explanation,
            "key_factors": [
                f"heuristic={heuristic}",
                f"brand_share={focus.get('brand_share')}",
                f"est_footfall={focus.get('est_footfall')}",
            ],
            "brief": brief,
            "model": "heuristic",
            **_geo_proximity_response(geo_ctx),
        }

    if not _env_bool("ENABLE_VERTEX", True):
        return _fallback_payload("ai_disabled", "ENABLE_VERTEX=false")

    try:
        raw = chatbot.call_vertex_chat(
            "Return the store-opening decision JSON now.",
            payload,
            request_id,
            system=chatbot.GEO_BRIEF_SYSTEM,
            max_context_chars=chatbot.GEO_BRIEF_MAX_CHARS,
        )
        parsed = _parse_geo_brief_model_json(raw)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        log.error("request_id=%s geo_brief vertex HTTP status=%s detail=%s", request_id, exc.status_code, detail)
        return _fallback_payload("ai_unavailable", detail)
    except Exception as exc:
        log.exception("request_id=%s geo_brief model parse/call failed", request_id)
        return _fallback_payload("ai_unavailable", f"{type(exc).__name__}: {exc}")

    model_decision = parsed["decision"]
    binding = focus.get("binding_decision")
    if binding and model_decision != binding:
        log.warning("request_id=%s geo_brief model decision %s overridden by guardrail %s",
                    request_id, model_decision, binding)
        parsed["decision"] = binding
        parsed["explanation"] = (
            f"Decision set to {binding} ({focus.get('decision_label')}) by the store-proximity and demand rules. "
            + parsed["explanation"]
        )
    log.info(
        "request_id=%s geo_brief ok decision=%s confidence=%s brief_len=%s",
        request_id,
        parsed["decision"],
        parsed["confidence"],
        len(parsed["brief"]),
    )
    return {
        "ok": True,
        "fallback": False,
        "request_id": request_id,
        "model": os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash"),
        "decision": parsed["decision"],
        "model_decision": model_decision,
        "confidence": parsed["confidence"],
        "explanation": parsed["explanation"],
        "key_factors": parsed["key_factors"],
        "brief": parsed["brief"],
        **_geo_proximity_response(geo_ctx),
    }


@app.get("/v1/geo/stores")
def geo_stores(
    request: Request,
    brand: Optional[str] = Query(None, max_length=64),
    refresh: bool = Query(False),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Existing stores of the analysed brand (Gemini + Google Search, cached) with 4 km catchments."""
    sess = _geo_session(request, authorization, x_session_token)
    request_id = uuid.uuid4().hex[:12]
    if refresh and sess.get("role") != "admin":
        raise HTTPException(403, "Only admins can refresh store locations")
    metrics = _geo_metrics_or_none()
    if metrics is None:
        raise _masked_http(503, "geo metrics unavailable")
    analysis_brand = _geo_brand(sess, brand, metrics)
    lookup = _geo_lookup(analysis_brand, metrics, refresh=refresh, request_id=request_id)
    pin_geo = metrics.get("pin_geo") or {}
    stores = geo_prox.stores_for_map(lookup)
    for s in stores:
        s["catchment_pins"] = [{"pin": p, "distance_km": km} for p, km in geo_prox.catchment_pins(s, pin_geo)]
    return {
        "ok": True,
        "request_id": request_id,
        "analysis_brand": analysis_brand,
        "city": lookup.get("city"),
        "status": lookup.get("status"),
        "detail": lookup.get("detail"),
        "threshold_km": geo_prox.PROXIMITY_KM,
        "stores": stores,
        "sources": lookup.get("sources") or [],
        "search_entry_html": lookup.get("search_entry_html"),
        "fetched_at": lookup.get("fetched_at"),
        "cached": bool(lookup.get("cached")),
        "label": "Store locations found via Gemini + Google Search; verify low-confidence pins.",
    }


@app.post("/v1/geo/routing-guide")
def geo_routing_guide(
    body: GeoRoutingRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Regional Stock & Assortment Routing Guide for an existing store's 4 km catchment."""
    sess = _geo_session(request, authorization, x_session_token)
    request_id = uuid.uuid4().hex[:12]
    metrics = _geo_metrics_or_none()
    if metrics is None:
        raise _masked_http(503, "geo metrics unavailable")
    analysis_brand = _geo_brand(sess, body.analysis_brand, metrics)
    lookup = _geo_lookup(analysis_brand, metrics, request_id=request_id)
    stores = lookup.get("stores") or []
    if lookup.get("status") == "unavailable":
        raise HTTPException(503, "Store locations are unavailable right now; try again shortly")
    pin_geo = metrics.get("pin_geo") or {}
    store, km = None, None
    if body.store_name:
        store = next((s for s in stores if str(s.get("name") or "").lower() == body.store_name.strip().lower()), None)
        if not store:
            raise HTTPException(404, "Store not found for this brand")
    elif body.pin:
        c = geo_prox.pin_centroid(pin_geo, body.pin.strip())
        if not c:
            raise HTTPException(404, "Unknown PIN")
        store, km = geo_prox.nearest_store(*c, stores)
        if store is None or km is None or not geo_prox.within_threshold(km):
            raise HTTPException(
                409, f"PIN is not within {geo_prox.PROXIMITY_KM:g} km of an existing store; use the expansion view"
            )
    else:
        raise HTTPException(400, "Provide a PIN or a store name")
    catchment = geo_prox.catchment_pins(store, pin_geo)
    guide = geo_analysis.routing_guide(
        metrics, pins=[p for p, _ in catchment], role=sess.get("role") or "admin",
        session_brand=sess.get("brand_name"), analysis_brand=analysis_brand,
    )
    dist = dict(catchment)
    for row in guide["pins"]:
        row["distance_km"] = dist.get(row["pin"])
    log.info("request_id=%s routing guide brand=%s store=%s pins=%s", request_id, analysis_brand,
             store.get("name"), len(catchment))
    return {
        "ok": True,
        "request_id": request_id,
        "threshold_km": geo_prox.PROXIMITY_KM,
        "store": geo_prox.store_public(store, km),
        "mode": "optimize",
        "decision_label": geo_prox.SERVICE_LABEL,
        **guide,
    }


@app.post("/v1/geo/area-profile")
def geo_area_profile(
    body: GeoAreaRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """PIN area name, population and centroid (Gemini + Google Search), saved to BigQuery pin_areas."""
    sess = _geo_session(request, authorization, x_session_token)
    request_id = uuid.uuid4().hex[:12]
    if body.refresh and sess.get("role") != "admin":
        raise HTTPException(403, "Only admins can refresh a PIN area profile")
    pin = body.pin.strip()
    if not pin_areas.valid_pin(pin):
        raise HTTPException(400, "PIN code must be 6 digits")
    if sess.get("role") != "admin":
        metrics = _geo_metrics_or_none()
        if metrics is None:
            raise _masked_http(503, "geo metrics unavailable")
        if pin not in (metrics.get("pin_geo") or {}):
            raise HTTPException(404, "Unknown PIN")
    try:
        res = pin_areas.lookup(pin, refresh=body.refresh, rate_key=sess.get("username") or "?",
                               request_id=request_id)
    except pin_areas.RateLimited:
        raise HTTPException(429, "Too many PIN lookups; try again in a minute") from None
    if res.get("saved"):
        _clear_metrics_cache()
    return {"ok": True, "request_id": request_id, **res}


@app.post("/v1/geo/market-enrichment")
def geo_market_enrichment(
    body: GeoMarketRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Grounded footfall / rent / competition for a whitespace PIN (> 4 km from existing stores)."""
    sess = _geo_session(request, authorization, x_session_token)
    request_id = uuid.uuid4().hex[:12]
    if body.refresh and sess.get("role") != "admin":
        raise HTTPException(403, "Only admins can refresh the external market check")
    metrics = _geo_metrics_or_none()
    if metrics is None:
        raise _masked_http(503, "geo metrics unavailable")
    pin = body.pin.strip()
    if pin not in (metrics.get("pin_geo") or {}):
        raise HTTPException(404, "Unknown PIN")
    role, session_brand = sess.get("role") or "admin", sess.get("brand_name")
    try:
        row = geo_service.row_for(role=role, session_brand=session_brand, pin=pin, filters=body.filters,
                                  analysis_brand=body.analysis_brand, request_id=request_id)
    except camp_svc.ServiceError as exc:
        raise _svc_http(exc) from exc
    except Exception as exc:
        raise _masked_http(503, f"geo market scoring failed request_id={request_id}") from exc
    analysis_brand = row["analysis_brand"] if row else geo_service.brand_for(
        metrics, role=role, session_brand=session_brand, filters=body.filters, analysis_brand=body.analysis_brand)
    lookup = _geo_lookup(analysis_brand, metrics, request_id=request_id)
    try:
        res = geo_market.enrich_whitespace(
            pin, metrics=metrics, brand=analysis_brand, role=role,
            stores_lookup=lookup, refresh=body.refresh, request_id=request_id,
        )
    except geo_market.EnrichmentRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    expansion = (row or {}).get("expansion_decision") or ""
    if expansion in {"Yes", "Consider", "No"}:
        if res.get("proximity_zone") == "unverified" and expansion == "Yes":
            expansion = "Consider"
        adjusted, note = geo_market.adjust_decision(expansion, res)
        res["decision_before"] = expansion
        res["decision_after"] = adjusted
        res["adjustment_note"] = note
    return {"ok": True, "request_id": request_id, "analysis_brand": analysis_brand, **res}


def _aura_geo_brief(payload: dict[str, Any], sess: dict[str, Any]) -> dict[str, Any]:
    return geo_brief_core(GeoBriefRequest(**payload), sess)


aura_agent.configure(geo_brief=_aura_geo_brief)


@app.post("/v1/chat")
def chat(
    body: chatbot.ChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Dashboard Q&A from the live metrics summary + Geo/Ads UI filters (read-only; brand/admin session)."""
    sess = _geo_session(request, authorization, x_session_token)
    brand = str(sess.get("brand_name") or "").strip() if sess.get("role") == "brand" else None
    if sess.get("role") == "brand" and not brand:
        raise HTTPException(403, "This brand account has no brand name assigned")
    return chatbot.run_chat(body, cached_metrics, brand_name=brand)


@app.post("/v1/agent/chat")
async def agent_chat(
    body: aura_agent.AgentChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """AURA brand operating agent — guarded tool calls for Geo + Ads Lab (session required)."""
    token = _token_from_request(request, authorization, x_session_token)
    sess = auth_acl.get_session(token) if token else None
    return await run_in_threadpool(aura_agent.run_agent, body, sess=sess)


@app.post("/v1/agent/reset")
async def agent_reset(
    body: aura_agent.AgentChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """AURA New chat: clears the conversation, open forms and pending confirms; keeps campaigns and jobs."""
    token = _token_from_request(request, authorization, x_session_token)
    sess = auth_acl.get_session(token) if token else None
    return await run_in_threadpool(aura_agent.reset_chat, body, sess=sess)


@app.post("/v1/agent/uploads")
async def agent_uploads(
    request: Request,
    files: list[UploadFile] = File(...),
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Stage SKU images for AURA (validated, metadata stripped, session-scoped, short TTL)."""
    token = _token_from_request(request, authorization, x_session_token)
    sess = auth_acl.get_session(token) if token else None
    max_bytes = aura_guard.AURA_MAX_UPLOAD_MB * 1024 * 1024
    raw: list[tuple[str, Optional[str], bytes]] = []
    for f in (files or [])[: aura_guard.AURA_MAX_UPLOADS + 1]:
        raw.append((f.filename or "", f.content_type, await f.read(max_bytes + 1)))
    return await run_in_threadpool(aura_agent.stage_uploads, sess, raw)


@app.delete("/v1/agent/uploads")
def agent_uploads_clear(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    token = _token_from_request(request, authorization, x_session_token)
    sess = auth_acl.get_session(token) if token else None
    return aura_agent.clear_uploads(sess)


# Clean page URLs: /geo, /ads-lab, … serve the app shell (it picks the tab from the path); /login the sign-in page.
SHELL_PATHS = ("geo", "ads-lab", "campaigns", "style-assistant", "admin", "inventory", "warehouse", "pos")


def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name, media_type="text/html", headers={"Cache-Control": "no-cache"})


def _with_query(path: str, request: Request) -> str:
    return path + ("?" + request.url.query if request.url.query else "")


def _shell_page():
    return _page("index.html")


for _p in SHELL_PATHS:
    app.add_api_route(f"/{_p}", _shell_page, methods=["GET"], include_in_schema=False)


@app.get("/login", include_in_schema=False)
def login_page():
    return _page("login.html")


@app.get("/index.html", include_in_schema=False)
def legacy_shell(request: Request):
    # Old links (index.html#campaigns, ?tab=…&campaign_id=…): the browser keeps the #fragment across the redirect
    return RedirectResponse(_with_query("/", request), status_code=301)


@app.get("/login.html", include_in_schema=False)
def legacy_login(request: Request):
    return RedirectResponse(_with_query("/login", request), status_code=301)


@app.get("/{page}/", include_in_schema=False)
def trailing_slash(page: str, request: Request):
    if page in SHELL_PATHS or page == "login":
        return RedirectResponse(_with_query(f"/{page}", request), status_code=301)
    raise HTTPException(404, "Not found")


@app.get("/")
def root():
    if (WEB_DIR / "index.html").exists():
        return _shell_page()  # signed out → the shell sends you to /login
    return {
        "service": "suffai-command-center",
        "endpoints": [
            "/health",
            "/v1/metrics",
            "POST /v1/ads-lab/extract",
            "POST /v1/ads-lab/campaign",
            "GET /v1/ads-lab/campaigns",
            "POST /v1/ads-lab/campaigns/{id}/start",
            "POST /v1/ads-lab/campaigns/{id}/ignore",
            "PATCH /v1/ads-lab/campaigns/{id}/config",
            "POST /v1/ads-lab/campaigns/{id}/estimate",
            "POST /v1/ads-lab/campaigns/{id}/launch",
            "GET /v1/ads-lab/campaigns/{id}/monitor",
            "POST /v1/ads-lab/campaigns/{id}/budget-recommendation",
            "POST /v1/ads-lab/campaigns/{id}/fee-recommendation",
            "POST /v1/ads-lab/campaigns/{id}/export",
            "POST /v1/ads-lab/campaign-jobs",
            "GET /v1/ads-lab/campaign-jobs",
            "GET /v1/ads-lab/campaign-jobs/{id}",
            "POST /v1/chat",
            "POST /v1/agent/chat",
            "POST /v1/agent/reset",
            "POST /v1/agent/uploads",
            "DELETE /v1/agent/uploads",
            "POST /v1/geo/scores",
            "POST /v1/geo/brief",
            "GET /v1/geo/stores",
            "POST /v1/geo/routing-guide",
            "POST /v1/geo/area-profile",
            "POST /v1/geo/market-enrichment",
            "GET /v1/admin/unique-summary",
            "GET /v1/admin/unique-garments",
            "GET /v1/admin/campaigns",
            "GET /v1/admin/records/options",
            "POST /v1/admin/customers",
            "GET /v1/admin/orders",
            "POST /v1/admin/orders",
            "GET /v1/style-assistant/feed",
            "GET /v1/retail/me",
            "GET /v1/retail/products",
            "GET /v1/retail/stock",
            "POST /v1/retail/purchase-orders",
            "POST /v1/retail/transfers",
            "POST /v1/retail/pos/sales",
            "POST /v1/retail/pos/invoices/{id}/returns",
            "GET /v1/retail/pos/day-report",
            "POST /v1/retail/opening-plan",
            "GET /v1/retail/campaigns/{id}",
            "POST /v1/auth/login",
            "GET /v1/auth/me",
            "POST /v1/auth/logout",
        ],
    }


class _UIStaticFiles(StaticFiles):
    """Pages/scripts revalidate on every load (ETag → cheap 304), so UI updates show without a hard refresh."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if not path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


if WEB_DIR.is_dir():
    app.mount("/", _UIStaticFiles(directory=str(WEB_DIR), html=True), name="ui")
