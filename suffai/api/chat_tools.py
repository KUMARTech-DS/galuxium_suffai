"""Allowlisted analytics tools for Ask Suffai. No generated SQL."""

from __future__ import annotations

import re
from typing import Any, Callable

from suffai.garments.store import GarmentStore
from suffai.metrics.geo import geo_query, commodity_popularity, PIN_AREA_LABELS
from suffai.metrics.eligibility import eligible_orders
from suffai.metrics.rfm import rfm_distribution


ToolFn = Callable[..., dict[str, Any]]


def build_tool_context(
    orders: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    rfm_snapshots: list[dict[str, Any]],
    garment_store: GarmentStore,
) -> dict[str, Any]:
    return {
        "orders": orders,
        "lines": lines,
        "rfm_snapshots": rfm_snapshots,
        "garment_store": garment_store,
    }


def tool_geo_active(ctx: dict[str, Any], pin: str | None = None) -> dict[str, Any]:
    geo_ids = [pin] if pin else None
    return geo_query(
        ctx["orders"], ctx["lines"], ctx["rfm_snapshots"], geo_ids=geo_ids
    )


def tool_commodity_trend(ctx: dict[str, Any], pin: str | None = None) -> dict[str, Any]:
    eligible_ids = {o["order_id"] for o in eligible_orders(ctx["orders"])}
    pins = [pin] if pin else None
    rows = commodity_popularity(ctx["lines"], eligible_ids, pin_codes=pins)
    return {"commodities": rows[:15], "pin": pin}


def tool_rfm_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"distribution": rfm_distribution(ctx["rfm_snapshots"])}


def tool_brand_coverage(ctx: dict[str, Any]) -> dict[str, Any]:
    return ctx["garment_store"].brand_coverage_summary()


TOOLS: dict[str, ToolFn] = {
    "geo_active_customers": tool_geo_active,
    "commodity_popularity": tool_commodity_trend,
    "rfm_summary": tool_rfm_summary,
    "brand_coverage": tool_brand_coverage,
}


def route_chat_query(query: str, ctx: dict[str, Any]) -> dict[str, Any]:
    q = query.lower().strip()
    pin_match = re.search(r"\b(560\d{3})\b", q)
    pin = pin_match.group(1) if pin_match else None

    # Refuse unsupported market-share / revenue claims without verified garments
    if any(w in q for w in ("market share", "sales revenue", "purchase share", "sold units")):
        return {
            "answer": (
                "I can't report fashion market share or purchase revenue from laundry "
                "service data. Brand share requires verified consented garment observations."
            ),
            "tool": None,
            "result": None,
            "refused": True,
        }

    if "brand" in q and ("competitor" in q or "share" in q or "benchmark" in q):
        result = tool_brand_coverage(ctx)
        return {
            "answer": result.get("empty_state")
            or f"Verified branded observations: {result['verified_branded_observations']}.",
            "tool": "brand_coverage",
            "result": result,
            "refused": False,
        }

    if "opportunit" in q or "new product" in q or "should we prioritize" in q or "kurta" in q:
        return {
            "answer": (
                "Use Product Research (`POST /v1/product-opportunities`) for ranked "
                "commodity opportunities with component scores. I won't invent supply gaps "
                "without catalog coverage."
            ),
            "tool": "product_opportunities_hint",
            "result": None,
            "refused": False,
        }

    if "campaign" in q and ("fit" in q or "segment" in q or "rank" in q):
        return {
            "answer": (
                "Campaign fit ranking is available via Campaign Lab "
                "(`POST /v1/campaigns/{id}/evaluate`). Scores are relevance, not conversion lift."
            ),
            "tool": "campaign_hint",
            "result": None,
            "refused": False,
        }

    if "rfm" in q or "segment" in q or "champion" in q:
        result = tool_rfm_summary(ctx)
        top = result["distribution"][:5]
        parts = [f"{r['rfm_segment']} ({r['customers']})" for r in top]
        return {
            "answer": "RFM distribution (eligible paid delivered orders): " + ", ".join(parts),
            "tool": "rfm_summary",
            "result": result,
            "refused": False,
        }

    if "commodity" in q or "garment type" in q or "shirt" in q or "grew" in q or "popular" in q:
        result = tool_commodity_trend(ctx, pin=pin)
        top = result["commodities"][:5]
        area = PIN_AREA_LABELS.get(pin or "", pin or "all zones")
        lines_txt = []
        for r in top:
            if r.get("suppressed"):
                lines_txt.append(f"{r['commodity_name']}: suppressed (k-anon)")
            else:
                lines_txt.append(f"{r['commodity_name']}: qty {r['quantity']}")
        return {
            "answer": (
                f"Top service commodity quantities in {area} "
                f"(not purchase counts): " + "; ".join(lines_txt)
            ),
            "tool": "commodity_popularity",
            "result": result,
            "refused": False,
            "filters": {"pin": pin},
        }

    if "pin" in q or "zone" in q or "geo" in q or "density" in q or pin:
        result = tool_geo_active(ctx, pin=pin)
        tiles = [t for t in result["tiles"] if not t.get("suppressed")][:5]
        parts = [
            f"{t['area']} ({t['geo_key']}): {t['active_customers']} active customers"
            for t in tiles if t.get("active_customers") is not None
        ]
        return {
            "answer": (
                "Active customers by order PIN (eligible orders). "
                + ("; ".join(parts) if parts else "No unsuppressed zones for this filter.")
                + " "
                + result["meta"]["coverage_note"]
            ),
            "tool": "geo_active_customers",
            "result": result,
            "refused": False,
            "filters": {"pin": pin},
        }

    return {
        "answer": (
            "I can answer geo active customers, commodity service popularity, RFM mix, "
            "and brand coverage. Unsupported: unrestricted SQL, raw PII, or market-share "
            "claims without verified garments."
        ),
        "tool": None,
        "result": None,
        "refused": False,
    }
