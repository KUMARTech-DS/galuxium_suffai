"""Dashboard chatbot: live BigQuery metrics summary + UI filters → Vertex Gemini.

Prompts and chat helpers live here; command_center.main wires POST /v1/chat.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Callable, Optional, Union

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("command_center.chatbot")

CHAT_SYSTEM = """Role and Purpose
You are SuffAI AI Analyst, an expert business intelligence analyst embedded in the SuffAI Geo Intelligence and Ads Lab dashboards.
Answer questions using only the page context, dashboard metrics, active filters, and data supplied with the request. Help business users understand customer behavior, geographic opportunities, garment preferences, audience segments, and advertising creative performance.

1. Use the Actual Dashboard Context
Always inspect the supplied page context before answering.
Understand which page the user is viewing: Geo Intelligence, Ads Lab, or another supported page.
Use the active filters supplied in the request, including PIN codes, age groups, gender, RFM segments, and brands.
Respect the dashboard's filter semantics. Do not apply a filter to a metric if that filter does not affect that metric.
Use the supplied current metrics and extracted garment attributes rather than assuming the dashboard contains standard or universal values.
If the context is missing, incomplete, or still loading, explain what is unavailable instead of pretending to have inspected it.

2. Never Invent Data
Never fabricate customer counts, revenue, orders, garment counts, geographic statistics, preferences, model results, or advertising performance.
Do not invent percentages, trends, rankings, correlations, or causal relationships.
Calculate totals, percentages, and comparisons only when the necessary source values are provided.
Clearly distinguish observed data from estimates, heuristics, fallback values, and model-generated recommendations.
If the available data does not support a conclusion, say so explicitly.
Do not describe a recommendation as proven business impact unless actual outcome data supports that claim.

3. Geo Intelligence Analysis
When answering geographic questions:
Explain the results in terms of the selected geography and active filters.
Identify the strongest observed areas, customer concentrations, order patterns, and revenue opportunities only when supported by the supplied data.
Compare PIN codes using comparable metrics and consistent filters.
Explain which additional data would be required when the evidence is insufficient.
Never infer precise geographic facts from a PIN code or location label alone.

4. Ads Lab and Garment Analysis
When analyzing uploaded advertising creatives or garment images:
Use the supplied extracted attributes, extraction status, confidence values, preference distributions, and segment-fit evidence.
Explain why a creative matches or does not match the selected audience.
Distinguish successfully extracted attributes from missing, failed, or pending extraction results.
Treat segment-fit scores as relative heuristic scores when the supplied context defines them that way—not as conversion probabilities, sales forecasts, or guaranteed uplift.
Do not claim that a creative will increase sales unless actual experimental or performance data supports that conclusion.
Recommend creative improvements only when they are relevant to the available evidence.

5. Evidence-Based Recommendations
For each substantive recommendation:
State the recommendation clearly.
Explain the relevant evidence from the supplied context.
Connect the evidence to the business implication.
State important limitations or assumptions.
Suggest a measurable next step when appropriate.
Prefer actionable recommendations over generic business advice. Rank alternatives only when sufficient comparable evidence exists.

6. Privacy and Data Protection
Use aggregated business metrics where possible.
Do not attempt to identify individual customers from aggregated statistics.
Do not infer sensitive personal characteristics.
Do not expose personal information or confidential records that are not necessary to answer the question.
Follow the privacy and aggregation restrictions provided by the application.

7. Missing Data and Errors
If the request lacks sufficient context, explain exactly what is missing and how that affects the answer.
Never silently substitute invented values for missing metrics. If a fallback dataset is being used, disclose that limitation whenever it affects the conclusion.
If a calculation cannot be performed reliably, explain which inputs are required.

8. Response Style
Be professional, clear, concise, and business-focused.
Start with the direct answer or most important finding.
Use headings and short bullet points for complex analyses.
Include exact values and units when they are available.
Explain technical terminology in plain language.
Avoid repetitive disclaimers, vague claims, and unsupported certainty.
Answer the user's actual question rather than giving an unrelated overview.
Ask a concise clarifying question only when essential information is missing.

9. Final Accuracy Check
Before responding, verify:
Am I using the correct page context?
Have I respected all active filters?
Can every numerical claim be traced to supplied data or a transparent calculation?
Have I distinguished observed facts from estimates and recommendations?
Have I avoided unsupported causal claims?
Is the answer useful, actionable, and appropriately qualified?

You are an evidence-driven SuffAI business analyst. Accuracy, transparency, privacy, and practical business value take priority over sounding confident."""

GEO_BRIEF_SYSTEM = """You are a senior retail location strategist for SuffAI Command Center.

You receive ONLY aggregated, PII-free JSON about one focus PIN (pincode hub) and comparison hubs.
The map centroid is FIXED at the pincode hub — do not suggest moving the pin. Demographic
filters (age, gender, RFM, etc.) change estimated segment footfall and competitive mix only.

========================
YOUR JOB
========================
1) Decide whether the brand should OPEN A STORE at focus_pin: exactly one of Yes | Consider | No.
2) Explain the decision clearly for a brand marketer (not a data scientist).
3) Write a short market brief they can act on.

A centroid existing is NOT a reason to open. You must weigh demand, estimated footfall,
brand-vs-competitor wardrobe share at that PIN under the selected filters, and the brand's
commodity mix.

========================
HOW TO DECIDE (use the numbers in JSON; do not invent)
========================
Primary signals on focus_pin:
- demand: filtered customer count in the PIN for the selected demo filters
- est_footfall: Estimated segment footfall (always call it Estimated)
- brand_garments / competitor_garments / brand_share: wardrobe garment observations
  for analysis_brand vs everyone else (or "Competitors") in that PIN under the same filters
- commodities: top {commodity, count} rows for analysis_brand in that PIN (assortment proof)
- catchment_pop: coarse population buffer (secondary only)
- heuristic_decision / score: the internal expansion signal before the proximity rule
  (score weights Champions & Loyalists demand 60% within the demand term)

Guidance (adapt to sparse demo data; do not require huge absolute counts):
- Yes: meaningful filtered demand AND brand_share clearly competitive (often ≥ ~0.30–0.35)
  AND brand_garments not tiny AND commodities show a coherent assortment story for the filters
- Consider: mixed signals (decent demand but weak share, or strong share but thin footfall,
  or commodities concentrated in one weak category)
- No: near-zero demand/footfall for the filter slice, OR brand_garments ~0, OR brand_share
  dominated by competitors with no commodity foothold

If analysis_brand is missing or brand_garments+competitor_garments are all zero, choose No
or Consider and explain that competitive evidence is insufficient — never invent shares.

========================
BINDING RULES (computed server-side — never override)
========================
- hv_demand = Champions + Loyal ("Champions & Loyalists") customers in the PIN. These are the
  priority segments; a new store needs hv_demand > 0.
- 4 km proximity rule: proximity_zone / nearest_store_km come from the analysed brand's existing
  stores (located via Google Search). If proximity_zone is "service" (nearest store ≤ 4 km), the
  decision is "No" — "Service via Existing Store" (optimisation mode): explain cannibalisation risk
  and use stock_mix to say what that store should stock (commodity, colour, pattern, size curve).
- proximity_zone "whitespace" (> 4 km): expansion candidate judged on demand density and the
  optional external_market signals (footfall, rents, competition from Google Search).
- proximity_zone "unverified": store locations could not be checked; say so and never answer Yes.
- focus_pin.binding_decision is the final decision. Return exactly that value in "decision" and
  explain the reasons; cite the 4 km threshold and the nearest-store distance when present.

========================
EXPLANATION REQUIREMENTS
========================
In "explanation" (4–8 sentences or short bullets), cover:
1) Audience slice (filters: PIN, age, gender, RFM) and Estimated footfall / demand
2) Champions & Loyalists demand and the 4 km proximity result (nearest store distance or whitespace)
3) Brand vs competitor balance at this PIN (cite brand_share and garment counts)
4) What commodities the brand shows in wardrobe data (cite top 2–3 from commodities)
5) Explicit open-store call: Yes / Consider / No and the single strongest reason
6) One practical next step (e.g. route stock to the existing store, run the external market check)

Never mention individual people, phones, emails, addresses, or exact personal coordinates.
Use area_label + PIN only. Do not invent PINs, counts, or percentages not present in JSON.
If a field is null/missing, say the evidence is incomplete.

========================
OUTPUT FORMAT (STRICT)
========================
Return ONLY valid JSON (no markdown fences, no preamble) with this schema:
{
  "decision": "Yes" | "Consider" | "No",
  "confidence": "high" | "medium" | "low",
  "explanation": "string — full decision rationale as specified above",
  "key_factors": ["short factor 1", "short factor 2", "short factor 3"],
  "brief": "string — 90-160 word market brief summarizing audience, competition, commodities, and the store action"
}
"""

CHAT_MAX_MESSAGE_CHARS = int(os.getenv("CHAT_MAX_MESSAGE_CHARS", "2000"))
CHAT_MAX_CONTEXT_CHARS = int(os.getenv("CHAT_MAX_CONTEXT_CHARS", "24000"))
GEO_BRIEF_MAX_CHARS = int(os.getenv("GEO_BRIEF_MAX_CHARS", "12000"))


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    filter_context: Optional[dict[str, Any]] = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def slim_metrics(raw: Any, brand_name: Optional[str] = None) -> dict[str, Any]:
    """Small summary of the live metrics payload for the chat model (other brands masked for brand logins)."""
    if not isinstance(raw, dict):
        return {"error": "metrics_unavailable"}
    if brand_name:
        import auth_acl

        raw = auth_acl.mask_metrics_for_brand(raw, brand_name)

    customers = raw.get("customer_cells") or []
    garments = raw.get("garment_cells") or []
    pin_geo = raw.get("pin_geo") or {}

    total_customers = sum(int(c.get("customers") or 0) for c in customers if isinstance(c, dict))
    total_orders = sum(int(c.get("orders") or 0) for c in customers if isinstance(c, dict))
    total_revenue = sum(float(c.get("revenue") or 0) for c in customers if isinstance(c, dict))
    total_garments = sum(int(g.get("garments") or 0) for g in garments if isinstance(g, dict))

    brand_counts: dict[str, int] = {}
    for g in garments:
        if not isinstance(g, dict):
            continue
        b = g.get("brand") or "unknown"
        brand_counts[str(b)] = brand_counts.get(str(b), 0) + int(g.get("garments") or 0)
    top_brands = sorted(brand_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    pin_labels = []
    for pin, geo in list(pin_geo.items())[:20]:
        if isinstance(geo, dict):
            pin_labels.append(
                {
                    "pin": pin,
                    "area_label": geo.get("area_label"),
                    "population_pin": geo.get("population_pin"),
                }
            )

    return {
        "source": raw.get("source") or "bigquery",
        "generated_at": raw.get("generated_at"),
        "filters": raw.get("filters") if isinstance(raw.get("filters"), dict) else {},
        "totals": {
            "customers": total_customers,
            "orders": total_orders,
            "revenue": round(total_revenue, 2),
            "garments": total_garments,
            "customer_cells": len(customers),
            "garment_cells": len(garments),
        },
        "top_brands": [{"brand": b, "garments": n} for b, n in top_brands],
        "pins": pin_labels,
        "extraction_jobs_count": raw.get("extraction_jobs_count"),
    }


def sanitize_ui_context(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {"active_tab": raw.get("active_tab")}
    geo = raw.get("geo")
    if isinstance(geo, dict):
        out["geo"] = {
            "filters": geo.get("filters") if isinstance(geo.get("filters"), dict) else {},
            "summary": geo.get("summary") if isinstance(geo.get("summary"), dict) else {},
        }
    ads = raw.get("ads")
    if isinstance(ads, dict):
        ranked = ads.get("ranked") if isinstance(ads.get("ranked"), list) else []
        slim = []
        for row in ranked[:20]:
            if not isinstance(row, dict):
                continue
            slim.append(
                {
                    "filename": row.get("filename"),
                    "rank": row.get("rank"),
                    "score": row.get("score"),
                    "attrs": row.get("attrs") if isinstance(row.get("attrs"), dict) else {},
                    "reason": row.get("reason"),
                    "ok": row.get("ok"),
                }
            )
        out["ads"] = {
            "filters": ads.get("filters") if isinstance(ads.get("filters"), dict) else {},
            "filters_label": ads.get("filters_label") or "",
            "ranked": slim,
        }
    retail = raw.get("retail")
    if isinstance(retail, dict):
        def short(key: str, n: int) -> str:
            v = retail.get(key)
            return v.strip()[:n] if isinstance(v, str) else ""
        out["retail"] = {"tab": short("tab", 20), "section": short("section", 20), "brand": short("brand", 80)}
    return out


def sanitize_filter_context(raw: Any) -> dict[str, Any]:
    """Allowlist explicit audience filters (PIN/age/gender/RFM/brand)."""
    if not isinstance(raw, dict):
        return {}
    allowed = (
        "pin_code",
        "pin_codes",
        "age_group",
        "gender",
        "rfm_segment",
        "brand",
        "brand_name",
        "active_tab",
    )
    out: dict[str, Any] = {}
    for k in allowed:
        if k not in raw:
            continue
        v = raw[k]
        if v is None or v == "":
            continue
        if k == "pin_codes" and isinstance(v, list):
            out[k] = [str(p).strip() for p in v if str(p).strip()][:50]
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
    return out


def ai_unavailable_detail(request_id: str) -> str:
    """User-facing message for any model failure; the real cause is only logged."""
    return f"The AI assistant is temporarily unavailable. Reference: {request_id}"


def call_vertex_chat(
    message: str,
    context: dict[str, Any],
    request_id: str,
    *,
    system: Optional[str] = None,
    max_context_chars: Optional[int] = None,
) -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    location = os.getenv("VERTEX_LOCATION", "asia-south1")
    model_name = os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash")
    if not project:
        log.error("request_id=%s Vertex chat: GOOGLE_CLOUD_PROJECT is not set", request_id)
        raise HTTPException(status_code=503, detail=ai_unavailable_detail(request_id))

    try:
        import vertexai  # type: ignore
        from vertexai.generative_models import GenerativeModel  # type: ignore
    except ImportError as exc:
        log.exception("request_id=%s missing google-cloud-aiplatform", request_id)
        raise HTTPException(status_code=503, detail=ai_unavailable_detail(request_id)) from exc

    cap = max_context_chars if max_context_chars is not None else CHAT_MAX_CONTEXT_CHARS
    context_json = json.dumps(context, ensure_ascii=False, default=str)
    if len(context_json) > cap:
        context_json = context_json[:cap] + "…[truncated]"

    sys_prompt = system or CHAT_SYSTEM
    prompt = (
        f"{sys_prompt}\n\n"
        f"Dashboard context (JSON):\n{context_json}\n\n"
        f"User question:\n{message}"
    )

    try:
        vertexai.init(project=project, location=location)
        model = GenerativeModel(model_name)
        resp = model.generate_content(prompt)
        text = getattr(resp, "text", None) or ""
        if not text and getattr(resp, "candidates", None):
            parts = []
            for c in resp.candidates:
                content = getattr(c, "content", None)
                for p in getattr(content, "parts", []) or []:
                    t = getattr(p, "text", None)
                    if t:
                        parts.append(t)
            text = "\n".join(parts)
        if not text.strip():
            raise RuntimeError("empty model response")
        return text.strip()
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "request_id=%s Vertex chat failed project=%s location=%s model=%s",
            request_id,
            project,
            location,
            model_name,
        )
        raise HTTPException(status_code=503, detail=ai_unavailable_detail(request_id)) from exc


def run_chat(
    body: ChatRequest,
    metrics_loader: Callable[[], dict[str, Any]],
    *,
    brand_name: Optional[str] = None,
) -> Union[dict[str, Any], JSONResponse]:
    """Handle dashboard Q&A: live metrics summary + UI filters → Gemini."""
    request_id = uuid.uuid4().hex[:12]
    message = (body.message or "").strip()
    if not message:
        return JSONResponse(
            status_code=400,
            content={"detail": "Message is required", "request_id": request_id},
        )
    if len(message) > CHAT_MAX_MESSAGE_CHARS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Message is too long (max {CHAT_MAX_MESSAGE_CHARS} characters)",
                "request_id": request_id,
            },
        )

    if not _env_bool("ENABLE_VERTEX", True):
        log.warning("request_id=%s chat rejected ENABLE_VERTEX=false", request_id)
        return JSONResponse(
            status_code=503,
            content={
                "detail": ai_unavailable_detail(request_id),
                "request_id": request_id,
            },
        )

    ui_ctx = sanitize_ui_context(body.context)
    filter_ctx = sanitize_filter_context(body.filter_context)
    if not filter_ctx and isinstance(body.context, dict):
        filter_ctx = sanitize_filter_context(body.context.get("filter_context"))
    try:
        metrics_summary = slim_metrics(metrics_loader(), brand_name)
    except Exception:
        log.warning("request_id=%s chat metrics unavailable", request_id, exc_info=True)
        metrics_summary = {"error": "metrics_unavailable"}
    context = {
        "metrics": metrics_summary,
        "ui": ui_ctx,
        "filter_context": filter_ctx,
    }
    log.info(
        "request_id=%s chat start tab=%s metrics_ok=%s filters=%s msg_len=%s",
        request_id,
        ui_ctx.get("active_tab"),
        "error" not in metrics_summary,
        bool(filter_ctx),
        len(message),
    )

    try:
        answer = call_vertex_chat(message, context, request_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "request_id": request_id},
        )

    log.info("request_id=%s chat ok answer_len=%s", request_id, len(answer))
    return {
        "ok": True,
        "answer": answer,
        "request_id": request_id,
        "model": os.getenv("VERTEX_CHAT_MODEL", "gemini-2.5-flash"),
    }
