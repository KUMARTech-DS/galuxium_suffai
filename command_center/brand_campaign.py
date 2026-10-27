"""Brand campaign persistence + segment preference + ranking helpers."""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Sequence

import vocab

log = logging.getLogger("command_center.brand_campaign")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def brand_slug(brand_name: str) -> str:
    s = _SLUG_RE.sub("-", (brand_name or "").strip().lower()).strip("-")
    return s or "brand"


@contextmanager
def connect() -> Iterator[Any]:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_campaign(
    *,
    brand_name: str,
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
) -> str:
    campaign_id = str(uuid.uuid4())
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brand_campaigns (
                  campaign_id, brand_name, pin_codes, age_group, gender, rfm_segment, status
                ) VALUES (%s,%s,%s,%s,%s,%s,'draft')
                """,
                (
                    campaign_id,
                    brand_name,
                    list(pin_codes),
                    age_group or None,
                    gender or None,
                    rfm_segment or None,
                ),
            )
    return campaign_id


def seed_synthetic_impact(campaign_id: str) -> None:
    """Persist demo marketing KPIs once at campaign create (no-op if already present)."""
    reach = random.randint(5_000, 80_000)
    impressions = int(reach * random.uniform(1.2, 3.5))
    ctr = round(random.uniform(0.8, 4.5), 2)
    conversions = max(1, int(impressions * (ctr / 100.0) * random.uniform(0.05, 0.25)))
    revenue = round(conversions * random.uniform(400.0, 2500.0), 2)
    lift = round(random.uniform(1.0, 12.0), 2)
    roas = round(random.uniform(1.2, 6.5), 2)
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO campaign_impact_metrics (
                      campaign_id, reach, impressions, ctr, conversions,
                      revenue, lift, roas, source
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'synthetic')
                    ON CONFLICT (campaign_id) DO NOTHING
                    """,
                    (campaign_id, reach, impressions, ctr, conversions, revenue, lift, roas),
                )
    except Exception as e:
        log.warning("seed_synthetic_impact failed for %s: %s", campaign_id, e)


def _impact_from_row(
    reach: Any,
    impressions: Any,
    ctr: Any,
    conversions: Any,
    revenue: Any,
    lift: Any,
    roas: Any,
    source: Any,
    spend: Any = None,
    clicks: Any = None,
) -> Optional[dict[str, Any]]:
    if reach is None and impressions is None:
        return None
    return {
        "reach": int(reach or 0),
        "impressions": int(impressions or 0),
        "ctr": float(ctr or 0),
        "conversions": int(conversions or 0),
        "revenue": float(revenue or 0),
        "lift": float(lift or 0),
        "roas": float(roas or 0),
        "spend": float(spend) if spend is not None else None,
        "clicks": int(clicks) if clicks is not None else None,
        "source": source or "synthetic",
    }


OBJECTIVES = ("awareness", "consideration", "conversion")

# Simulated media benchmarks per objective (INR). Not real ad-network rates.
_OBJECTIVE_BENCH: dict[str, dict[str, float]] = {
    "awareness": {"cpm": 120.0, "ctr": 0.9, "cvr": 1.0, "lift": 3.0},
    "consideration": {"cpm": 180.0, "ctr": 1.8, "cvr": 1.6, "lift": 5.0},
    "conversion": {"cpm": 250.0, "ctr": 2.8, "cvr": 2.5, "lift": 7.0},
}
_AOV_RANGE = (1100.0, 1600.0)
_FREQ_RANGE = (1.8, 2.6)


def objective_benchmark(objective: Optional[str]) -> dict[str, float]:
    return dict(_OBJECTIVE_BENCH.get(objective or "", _OBJECTIVE_BENCH["consideration"]))


def _outcome_point(
    budget: float,
    days: int,
    audience: int,
    bench: dict[str, float],
    *,
    cpm_mult: float,
    freq: float,
    ctr_mult: float,
    cvr_mult: float,
    aov: float,
    lift_mult: float,
) -> dict[str, Any]:
    impressions = int(budget / (bench["cpm"] * cpm_mult) * 1000) if budget > 0 else 0
    reach = int(impressions / freq) if impressions else 0
    if audience > 0:
        reach = min(reach, audience)
    ctr = round(bench["ctr"] * ctr_mult, 2)
    clicks = int(impressions * ctr / 100.0)
    conversions = int(clicks * bench["cvr"] * cvr_mult / 100.0)
    revenue = round(conversions * aov, 2)
    lift = round(bench["lift"] * lift_mult * min(1.0, 0.6 + days / 30.0), 2)
    roas = round(revenue / budget, 2) if budget > 0 else 0.0
    return {
        "reach": reach,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": ctr,
        "conversions": conversions,
        "revenue": revenue,
        "lift": lift,
        "roas": roas,
        "spend": round(budget, 2),
    }


CHANNELS = ("in_app", "whatsapp", "sms")
CHANNEL_LABELS = {"in_app": "In-app", "whatsapp": "WhatsApp", "sms": "SMS"}
MESSAGE_LIMITS = {"whatsapp": 1000, "sms": 160}

# Simulated delivery economics per channel. Not real vendor rates.
_REACH_SHARE = {"awareness": 0.8, "consideration": 0.6, "conversion": 0.4}
_CHANNEL_FREQ = {
    "in_app": {"awareness": 3.0, "consideration": 2.5, "conversion": 2.0},
    "whatsapp": {"awareness": 2.0, "consideration": 1.5, "conversion": 1.5},
    "sms": {"awareness": 2.0, "consideration": 1.5, "conversion": 1.0},
}
_MESSAGE_COST = {"whatsapp": 0.80, "sms": 0.20}
_MESSAGE_MODEL = {
    "whatsapp": {"delivered": 0.95, "read": 0.70, "click": (8.0, 10.0, 12.0), "lift_mult": 1.2},
    "sms": {"delivered": 0.92, "read": 1.0, "click": (2.0, 2.5, 3.0), "lift_mult": 0.8},
}
MIN_BUDGET_INR = 5000.0
MIN_DAILY_INR = 500.0
_SCENARIOS = ("low", "expected", "high")


def _fee_number(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a non-secret fee policy value; invalid deploy config safely falls back."""
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def fee_model_version() -> str:
    return (os.getenv("FEE_MODEL_VERSION") or "cost_model_v1").strip()[:80] or "cost_model_v1"


def recommend_platform_fee(
    *,
    audience: int,
    sku_count: int,
    pin_count: int,
    comparisons: int,
    process_seconds: float,
    objective: Optional[str],
    channels: Optional[Sequence[str]],
    duration_days: int,
    media_budget_inr: float,
) -> dict[str, Any]:
    """Explainable 1P platform fee for a cold start; this is not an ML prediction.

    It deliberately uses only campaign-level aggregates. Real accepted/final prices are
    logged separately and will be the future supervised-learning labels.
    """
    obj = _objective_key(objective)
    chans = normalize_channels(channels) or ["in_app"]
    audience = max(0, int(audience or 0))
    sku_count = max(0, int(sku_count or 0))
    pin_count = max(0, int(pin_count or 0))
    comparisons = max(0, int(comparisons or 0))
    process_seconds = max(0.0, float(process_seconds or 0))
    days = max(1, int(duration_days or 1))
    budget = max(0.0, float(media_budget_inr or 0))

    base = _fee_number("FEE_BASE_INR", 1500.0)
    per_1k = _fee_number("FEE_PER_1K_AUDIENCE", 35.0)
    per_sku = _fee_number("FEE_PER_SKU_INR", 125.0)
    per_pin = _fee_number("FEE_PER_PIN_INR", 45.0)
    per_10k_compare = _fee_number("FEE_PER_10K_COMPARISONS_INR", 20.0)
    per_process_minute = _fee_number("FEE_PER_PROCESS_MINUTE_INR", 12.0)
    channel_surcharge = _fee_number("FEE_PER_EXTRA_CHANNEL_INR", 250.0)
    daily = _fee_number("FEE_PER_DAY_INR", 10.0)
    objective_mult = {"awareness": 0.9, "consideration": 1.0, "conversion": 1.15}[obj]
    parts = [
        ("Base 1P campaign operations", base),
        ("Audience operations", math.ceil(audience / 1000) * per_1k),
        ("SKU ranking and merchandising", sku_count * per_sku),
        ("PIN targeting", pin_count * per_pin),
        ("Ranking comparisons", math.ceil(comparisons / 10_000) * per_10k_compare),
        ("Ranking processing", math.ceil(process_seconds / 60) * per_process_minute),
        ("Additional delivery channels", max(0, len(chans) - 1) * channel_surcharge),
        ("Campaign duration operations", days * daily),
    ]
    raw = sum(value for _, value in parts) * objective_mult
    min_fee = _fee_number("FEE_MIN_INR", 1500.0)
    max_pct = _fee_number("FEE_MAX_PCT_OF_BUDGET", 0.35, minimum=0.01)
    max_fee = max(min_fee, budget * max_pct) if budget else max(min_fee, raw * 1.5)
    recommended = min(max_fee, max(min_fee, _round_to(raw, 100.0)))
    features = {
        "audience_count": audience, "sku_count": sku_count, "pin_count": pin_count,
        "comparisons": comparisons, "process_seconds": round(process_seconds, 2),
        "channels": chans, "objective": obj, "duration_days": days,
        "media_budget_inr": round(budget, 2),
    }
    return {
        "model_version": fee_model_version(),
        "source": "cost_model_v1",
        "cold_start": True,
        "features": features,
        "min": float(min_fee),
        "recommended": float(recommended),
        "max": float(max_fee),
        "media_budget_inr": round(budget, 2),
        "total_checkout_inr": round(budget + recommended, 2),
        "breakdown": [
            {"label": label, "fee_inr": round(value * objective_mult, 2)}
            for label, value in parts if value > 0
        ],
        "reasons": [
            "This is an explainable cost-and-complexity estimate, not a trained ML price.",
            f"Includes {audience:,} audience members, {sku_count} SKUs, {pin_count} PINs and {len(chans)} channel(s).",
            "SuffAI will learn from accepted and final fees after real campaign history is available.",
        ],
    }


def normalize_channels(channels: Optional[Sequence[str]]) -> list[str]:
    out: list[str] = []
    for c in channels or []:
        key = str(c or "").strip().lower().replace("-", "_")
        if key == "inapp":
            key = "in_app"
        if key in CHANNELS and key not in out:
            out.append(key)
    return out


def _objective_key(objective: Optional[str]) -> str:
    return objective if objective in OBJECTIVES else "consideration"


def _channel_unit_cost(channel: str, bench: dict[str, float]) -> float:
    """Cost of one delivery unit (one impression for in-app, one message otherwise)."""
    if channel == "in_app":
        return bench["cpm"] / 1000.0
    return _MESSAGE_COST[channel]


def _channel_need(channel: str, target_reach: int, objective: str, bench: dict[str, float]) -> dict[str, Any]:
    freq = _CHANNEL_FREQ[channel][objective]
    units = int(round(target_reach * freq))
    unit_cost = _channel_unit_cost(channel, bench)
    return {
        "channel": channel,
        "label": CHANNEL_LABELS[channel],
        "frequency": freq,
        "units": units,
        "unit": "impressions" if channel == "in_app" else "messages",
        "unit_cost": round(unit_cost, 4),
        "cost": round(units * unit_cost, 2),
    }


def channel_split(audience: int, objective: Optional[str], channels: Sequence[str]) -> dict[str, float]:
    """Budget share per channel, proportional to what each channel needs to hit the target reach."""
    chans = normalize_channels(channels) or ["in_app"]
    obj = _objective_key(objective)
    bench = objective_benchmark(obj)
    target = max(1, int(max(0, audience) * _REACH_SHARE[obj]))
    costs = {c: _channel_need(c, target, obj, bench)["cost"] for c in chans}
    total = sum(costs.values())
    if total <= 0:
        return {c: 1.0 / len(chans) for c in chans}
    return {c: v / total for c, v in costs.items()}


def _round_to(v: float, step: float = 500.0) -> float:
    return float(max(step, round(v / step) * step))


def recommend_budget(
    audience: int,
    days: int,
    objective: Optional[str],
    channels: Optional[Sequence[str]],
) -> dict[str, Any]:
    """
    Simulated budget recommendation: reach a share of the audience (by objective) at a
    channel-specific frequency and unit cost, with a minimum total and daily floor.
    """
    obj = _objective_key(objective)
    chans = normalize_channels(channels) or ["in_app"]
    days = max(1, int(days or 1))
    audience = max(0, int(audience or 0))
    bench = objective_benchmark(obj)
    share = _REACH_SHARE[obj]
    target = int(audience * share)
    needs = [_channel_need(c, target, obj, bench) for c in chans]
    raw = sum(n["cost"] for n in needs)
    floor = max(MIN_BUDGET_INR, MIN_DAILY_INR * days)
    cap: Optional[float] = None
    if "in_app" not in chans and audience > 0:
        cap = sum(_channel_need(c, audience, obj, bench)["cost"] for c in chans)
    wanted = max(raw, floor)
    if cap is not None:
        wanted = min(wanted, max(raw, cap))
    recommended = _round_to(wanted)
    reasons = [
        f"Target reach is {share:.0%} of the {audience:,}-customer audience "
        f"({target:,} customers) for a {obj} objective.",
    ]
    for n in needs:
        plural = "" if n["frequency"] == 1 else "s"
        if n["channel"] == "in_app":
            reasons.append(
                f"In-app: {n['frequency']:g} impression{plural} per customer at a simulated "
                f"₹{bench['cpm']:.0f} CPM ≈ ₹{n['cost']:,.0f}."
            )
        else:
            reasons.append(
                f"{n['label']}: {n['frequency']:g} message{plural} per customer at ₹{n['unit_cost']:.2f} "
                f"per message ≈ ₹{n['cost']:,.0f}."
            )
    if cap is not None:
        reasons.append(
            f"Messaging reaches at most every customer at the planned frequency (≈ ₹{cap:,.0f}); "
            "budget above that would go unspent."
        )
    if raw < floor and recommended > _round_to(raw):
        rule = f"₹{MIN_BUDGET_INR:,.0f} total or ₹{MIN_DAILY_INR:,.0f}/day"
        if recommended >= floor:
            reasons.append(f"Raised to the ₹{floor:,.0f} minimum ({rule}) so delivery is not throttled.")
        else:
            reasons.append(
                f"Raised to ₹{recommended:,.0f}; the usual ₹{floor:,.0f} minimum ({rule}) "
                "is more than messaging can spend for this audience."
            )
    scale = recommended / raw if raw > 0 else 0.0
    breakdown = []
    for n in needs:
        budget_c = round(n["cost"] * scale, 2) if raw > 0 else round(recommended / len(needs), 2)
        breakdown.append({**n, "budget": budget_c, "share_pct": round(budget_c / recommended * 100, 1)})
    est = estimate_outcomes(recommended, days, audience, obj, chans)
    return {
        "objective": obj,
        "channels": chans,
        "audience": audience,
        "target_reach": target,
        "duration_days": days,
        "min": _round_to(recommended * 0.7),
        "recommended": recommended,
        "max": max(recommended, _round_to(min(recommended * 1.5, cap if cap is not None else math.inf))),
        "daily_budget": round(recommended / days, 2),
        "breakdown": breakdown,
        "expected_roas": est["expected"]["roas"],
        "expected": est["expected"],
        "reasons": reasons,
        "source": "synthetic",
    }


def _message_point(
    budget: float,
    days: int,
    audience: int,
    bench: dict[str, float],
    channel: str,
    objective: str,
    *,
    scenario: int,
    cvr_mult: float,
    aov: float,
    lift_mult: float,
) -> dict[str, Any]:
    model = _MESSAGE_MODEL[channel]
    freq = _CHANNEL_FREQ[channel][objective]
    unit = _MESSAGE_COST[channel]
    messages = int(budget / unit) if budget > 0 else 0
    if audience > 0:
        messages = min(messages, int(audience * freq))
    reach = min(audience, int(messages / freq)) if audience > 0 else int(messages / freq)
    delivered = int(messages * model["delivered"])
    impressions = int(delivered * model["read"])
    click_rate = model["click"][scenario]
    clicks = int(impressions * click_rate / 100.0)
    conversions = int(clicks * bench["cvr"] * cvr_mult / 100.0)
    revenue = round(conversions * aov, 2)
    spend = round(messages * unit, 2)
    lift = round(bench["lift"] * model["lift_mult"] * lift_mult * min(1.0, 0.6 + days / 30.0), 2)
    return {
        "reach": reach,
        "messages": messages,
        "delivered": delivered,
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        "conversions": conversions,
        "revenue": revenue,
        "lift": lift,
        "roas": round(revenue / spend, 2) if spend > 0 else 0.0,
        "spend": spend,
    }


def _union_reach(reaches: Sequence[int], audience: int) -> int:
    if audience <= 0:
        return int(sum(reaches))
    miss = 1.0
    for r in reaches:
        miss *= 1.0 - min(1.0, r / audience)
    return int(round(audience * (1.0 - miss)))


def _combine_points(points: Sequence[dict[str, Any]], audience: int, budget: float) -> dict[str, Any]:
    impressions = sum(p["impressions"] for p in points)
    clicks = sum(p["clicks"] for p in points)
    conversions = sum(p["conversions"] for p in points)
    revenue = round(sum(p["revenue"] for p in points), 2)
    spend = round(sum(p["spend"] for p in points), 2)
    lift = round(sum(p["lift"] * p["spend"] for p in points) / spend, 2) if spend > 0 else 0.0
    return {
        "reach": _union_reach([p["reach"] for p in points], audience),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        "conversions": conversions,
        "revenue": revenue,
        "lift": lift,
        "roas": round(revenue / spend, 2) if spend > 0 else 0.0,
        "spend": spend,
        "unspent": round(max(0.0, budget - spend), 2),
    }


def estimate_outcomes(
    budget: float,
    days: int,
    audience: int,
    objective: Optional[str],
    channels: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Deterministic low / expected / high simulated outcome ranges, split across channels."""
    budget = max(0.0, float(budget or 0))
    days = max(1, int(days or 1))
    audience = max(0, int(audience or 0))
    obj = _objective_key(objective)
    bench = objective_benchmark(obj)
    chans = normalize_channels(channels) or ["in_app"]
    split = channel_split(audience, obj, chans)
    mid_aov = sum(_AOV_RANGE) / 2
    mid_freq = sum(_FREQ_RANGE) / 2
    scen = {
        "low": dict(cpm_mult=1.1, freq=_FREQ_RANGE[1], ctr_mult=0.9, cvr_mult=0.9,
                    aov=_AOV_RANGE[0], lift_mult=0.75),
        "expected": dict(cpm_mult=1.0, freq=mid_freq, ctr_mult=1.0, cvr_mult=1.0,
                         aov=mid_aov, lift_mult=1.0),
        "high": dict(cpm_mult=0.9, freq=_FREQ_RANGE[0], ctr_mult=1.1, cvr_mult=1.1,
                     aov=_AOV_RANGE[1], lift_mult=1.25),
    }
    by_channel: dict[str, Any] = {}
    for ch in chans:
        b = round(budget * split[ch], 2)
        row: dict[str, Any] = {"label": CHANNEL_LABELS[ch], "budget": b, "share_pct": round(split[ch] * 100, 1)}
        for i, name in enumerate(_SCENARIOS):
            s = scen[name]
            if ch == "in_app":
                row[name] = _outcome_point(b, days, audience, bench, **s)
            else:
                row[name] = _message_point(
                    b, days, audience, bench, ch, obj, scenario=i,
                    cvr_mult=s["cvr_mult"], aov=s["aov"], lift_mult=s["lift_mult"],
                )
        by_channel[ch] = row
    totals = {
        name: _combine_points([by_channel[c][name] for c in chans], audience, budget)
        for name in _SCENARIOS
    }
    return {
        "objective": obj,
        "channels": chans,
        "budget_inr": budget,
        "duration_days": days,
        "audience": audience,
        "daily_budget": round(budget / days, 2),
        "benchmark": bench,
        **totals,
        "by_channel": by_channel,
        "source": "synthetic",
    }


def default_message_template(brand: str, campaign_name: Optional[str] = None) -> dict[str, str]:
    name = (campaign_name or "our new collection").strip()
    return {
        "whatsapp": (
            f"Hi {{first_name}}! {brand} picked styles from {name} that match your wardrobe. "
            "Tap to see your picks: {link}"
        ),
        "sms": f"{brand}: styles picked for you from {name}. See them: {{link}}",
    }


def validate_message_template(
    channels: Sequence[str], template: Optional[dict[str, Any]]
) -> dict[str, str]:
    out: dict[str, str] = {}
    tpl = template if isinstance(template, dict) else {}
    for ch in ("whatsapp", "sms"):
        text = str(tpl.get(ch) or "").strip()
        if ch not in channels:
            continue
        if not text:
            raise ValueError(f"a {CHANNEL_LABELS[ch]} message is required when {CHANNEL_LABELS[ch]} is selected")
        if len(text) > MESSAGE_LIMITS[ch]:
            raise ValueError(f"{CHANNEL_LABELS[ch]} message must be at most {MESSAGE_LIMITS[ch]} characters")
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ValueError(f"{CHANNEL_LABELS[ch]} message contains unsupported characters")
        out[ch] = text
    return out


def update_campaign_config(
    campaign_id: str,
    *,
    campaign_name: str,
    objective: str,
    budget_inr: float,
    duration_days: int,
    start_date: Optional[str],
    selected_sku_ids: Sequence[str],
    creative: Optional[dict[str, Any]],
    source_context: Optional[dict[str, Any]],
    est_audience: Optional[int],
    channels: Optional[Sequence[str]] = None,
    message_template: Optional[dict[str, Any]] = None,
    budget_recommended: Optional[float] = None,
    platform_fee_recommended: Optional[float] = None,
    platform_fee_final: Optional[float] = None,
    fee_model_version_value: Optional[str] = None,
    fee_features: Optional[dict[str, Any]] = None,
) -> None:
    import datetime as _dt

    name = (campaign_name or "").strip()
    if not name:
        raise ValueError("campaign_name is required")
    if not str((creative or {}).get("headline") or "").strip():
        raise ValueError("headline is required")
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    budget = float(budget_inr or 0)
    if budget <= 0:
        raise ValueError("budget_inr must be > 0")
    days = int(duration_days or 0)
    if days < 1 or days > 365:
        raise ValueError("duration_days must be between 1 and 365")
    chans = normalize_channels(channels if channels is not None else ["in_app"])
    if not chans:
        raise ValueError("select at least one channel (In-app, WhatsApp or SMS)")
    templates = validate_message_template(chans, message_template)
    if start_date:
        start = _dt.date.fromisoformat(str(start_date)[:10])
    else:
        start = _dt.date.today()
    end = start + _dt.timedelta(days=days - 1)
    sku_ids = [str(uuid.UUID(str(s))) for s in (selected_sku_ids or []) if str(s).strip()]
    if not sku_ids:
        raise ValueError("select at least one SKU")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM brand_skus
                WHERE campaign_id = %s AND sku_uuid = ANY(%s::uuid[])
                """,
                (campaign_id, sku_ids),
            )
            found = int(cur.fetchone()[0] or 0)
            if found != len(sku_ids):
                raise ValueError("selected SKUs must belong to this campaign")
            cur.execute(
                """
                UPDATE brand_campaigns
                SET campaign_name = %s, title = COALESCE(title, %s), objective = %s,
                    budget_inr = %s, duration_days = %s, start_date = %s, end_date = %s,
                    selected_sku_ids = %s::uuid[], creative = %s::jsonb,
                    source_context = COALESCE(%s::jsonb, source_context),
                    est_audience = %s, status = 'configured', run_mode = '1p',
                    channels = %s, message_template = %s::jsonb,
                    budget_recommended = COALESCE(%s, budget_recommended),
                    platform_fee_recommended = COALESCE(%s, platform_fee_recommended),
                    platform_fee_final = COALESCE(%s, platform_fee_final),
                    fee_model_version = COALESCE(%s, fee_model_version),
                    fee_features = COALESCE(%s::jsonb, fee_features),
                    fee_recommended_at = CASE WHEN %s IS NULL THEN fee_recommended_at ELSE now() END,
                    fee_accepted_at = CASE WHEN %s IS NULL THEN fee_accepted_at ELSE now() END,
                    error_message = NULL, updated_at = now()
                WHERE campaign_id = %s
                """,
                (
                    name,
                    name,
                    objective,
                    budget,
                    days,
                    start,
                    end,
                    sku_ids,
                    json.dumps(creative or {}),
                    json.dumps(source_context) if source_context else None,
                    int(est_audience) if est_audience is not None else None,
                    chans,
                    json.dumps(templates),
                    float(budget_recommended) if budget_recommended else None,
                    float(platform_fee_recommended) if platform_fee_recommended is not None else None,
                    float(platform_fee_final) if platform_fee_final is not None else None,
                    fee_model_version_value,
                    json.dumps(fee_features) if fee_features else None,
                    platform_fee_recommended,
                    platform_fee_final,
                    campaign_id,
                ),
            )


def mark_exported(campaign_id: str) -> None:
    """3P run: the brand takes the ranking to another platform; nothing runs on SuffAI."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brand_campaigns
                SET status = 'exported', run_mode = '3p', error_message = NULL, updated_at = now()
                WHERE campaign_id = %s AND status IN ('ranked', 'configured', 'exported')
                """,
                (campaign_id,),
            )


def _daily_weights(days: int, rng: random.Random) -> list[float]:
    ramp = min(4, max(1, days // 4))
    weights = []
    for d in range(days):
        base = 0.45 + 0.55 * min(1.0, (d + 1) / ramp)
        weights.append(max(0.05, base * rng.uniform(0.85, 1.15)))
    total = sum(weights)
    return [w / total for w in weights]


def _jittered_channel(exp: dict[str, Any], budget_c: float, rng: random.Random) -> dict[str, Any]:
    def jitter(v: float) -> float:
        return v * rng.uniform(0.85, 1.15)

    impressions = max(1, int(jitter(exp["impressions"])))
    reach = max(1, min(impressions, int(jitter(exp["reach"]))))
    ctr = round(jitter(exp["ctr"]), 2)
    clicks = int(impressions * ctr / 100.0)
    conversions = max(0, int(jitter(exp["conversions"])))
    revenue = round(jitter(exp["revenue"]), 2)
    lift = round(jitter(exp["lift"]), 2)
    spend = round(budget_c * rng.uniform(0.94, 1.0), 2)
    out = {
        "reach": reach, "impressions": impressions, "ctr": ctr, "clicks": clicks,
        "conversions": conversions, "revenue": revenue, "lift": lift, "spend": spend,
        "roas": round(revenue / spend, 2) if spend > 0 else 0.0,
    }
    if "messages" in exp:
        out["messages"] = max(0, int(jitter(exp["messages"])))
        out["delivered"] = min(out["messages"], max(0, int(jitter(exp["delivered"]))))
    return out


def finalize_synthetic_impact(campaign_id: str) -> dict[str, Any]:
    """Persist budget-scaled simulated KPIs + per-channel daily series at launch (replaces older rows)."""
    import datetime as _dt

    campaign = get_campaign(campaign_id)
    if not campaign:
        raise LookupError("campaign not found")
    budget = float(campaign.get("budget_inr") or 0)
    days = int(campaign.get("duration_days") or 0)
    if budget <= 0 or days < 1:
        raise ValueError("campaign budget / duration not configured")
    audience = int(campaign.get("est_audience") or 0)
    est = estimate_outcomes(
        budget, days, audience, campaign.get("objective"), campaign.get("channels") or ["in_app"]
    )
    rng = random.Random()
    by_channel = {
        ch: {"label": row["label"], **_jittered_channel(row["expected"], row["budget"], rng)}
        for ch, row in est["by_channel"].items()
    }
    chans = list(by_channel)
    impressions = sum(c["impressions"] for c in by_channel.values())
    clicks = sum(c["clicks"] for c in by_channel.values())
    conversions = sum(c["conversions"] for c in by_channel.values())
    revenue = round(sum(c["revenue"] for c in by_channel.values()), 2)
    spend = round(sum(c["spend"] for c in by_channel.values()), 2)
    reach = max(1, _union_reach([c["reach"] for c in by_channel.values()], audience))
    ctr = round(clicks / impressions * 100, 2) if impressions else 0.0
    lift = round(sum(c["lift"] * c["spend"] for c in by_channel.values()) / spend, 2) if spend > 0 else 0.0
    roas = round(revenue / spend, 2) if spend > 0 else 0.0

    start = campaign.get("start_date")
    start_d = _dt.date.fromisoformat(start) if start else _dt.date.today()
    weights = _daily_weights(days, rng)

    def split(total: float, as_int: bool) -> list[Any]:
        vals = [total * w for w in weights]
        if as_int:
            ints = [int(v) for v in vals]
            ints[-1] += int(total) - sum(ints)
            return ints
        return [round(v, 2) for v in vals]

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaign_impact_metrics (
                  campaign_id, reach, impressions, ctr, conversions,
                  revenue, lift, roas, spend, clicks, channel_breakdown, source, generated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'synthetic', now())
                ON CONFLICT (campaign_id) DO UPDATE SET
                  reach = EXCLUDED.reach, impressions = EXCLUDED.impressions,
                  ctr = EXCLUDED.ctr, conversions = EXCLUDED.conversions,
                  revenue = EXCLUDED.revenue, lift = EXCLUDED.lift,
                  roas = EXCLUDED.roas, spend = EXCLUDED.spend,
                  clicks = EXCLUDED.clicks, channel_breakdown = EXCLUDED.channel_breakdown,
                  source = 'synthetic', generated_at = now()
                """,
                (campaign_id, reach, impressions, ctr, conversions,
                 revenue, lift, roas, spend, clicks, json.dumps(by_channel)),
            )
            cur.execute("DELETE FROM campaign_impact_daily WHERE campaign_id = %s", (campaign_id,))
            for ch in chans:
                c = by_channel[ch]
                d_impr = split(c["impressions"], True)
                d_reach = split(c["reach"], True)
                d_clicks = split(c["clicks"], True)
                d_conv = split(c["conversions"], True)
                d_rev = split(c["revenue"], False)
                d_spend = split(c["spend"], False)
                for i in range(days):
                    cur.execute(
                        """
                        INSERT INTO campaign_impact_daily (
                          campaign_id, day, channel, impressions, reach, clicks,
                          conversions, revenue, spend
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            campaign_id,
                            start_d + _dt.timedelta(days=i),
                            ch,
                            d_impr[i], d_reach[i], d_clicks[i],
                            d_conv[i], d_rev[i], d_spend[i],
                        ),
                    )
            cur.execute(
                """
                UPDATE brand_campaigns
                SET status = 'launched', launched_at = COALESCE(launched_at, now()),
                    run_mode = '1p', error_message = NULL, updated_at = now()
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
    return {
        "reach": reach, "impressions": impressions, "ctr": ctr, "clicks": clicks,
        "conversions": conversions, "revenue": revenue, "lift": lift,
        "roas": roas, "spend": spend, "channel_breakdown": by_channel, "source": "synthetic",
    }


def list_impact_daily(campaign_id: str, *, by_channel: bool = False) -> list[dict[str, Any]]:
    """Daily simulated series summed across channels, or one row per (day, channel)."""
    group = "day, channel" if by_channel else "day"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {group}, SUM(impressions), SUM(reach), SUM(clicks),
                       SUM(conversions), SUM(revenue), SUM(spend)
                FROM campaign_impact_daily
                WHERE campaign_id = %s
                GROUP BY {group}
                ORDER BY {group}
                """,
                (campaign_id,),
            )
            out = []
            for r in cur.fetchall():
                off = 2 if by_channel else 1
                row = {
                    "day": r[0].isoformat(),
                    "impressions": int(r[off] or 0),
                    "reach": int(r[off + 1] or 0),
                    "clicks": int(r[off + 2] or 0),
                    "conversions": int(r[off + 3] or 0),
                    "revenue": float(r[off + 4] or 0),
                    "spend": float(r[off + 5] or 0),
                }
                if by_channel:
                    row["channel"] = r[1]
                out.append(row)
            return out


def split_impact_daily(daily: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Days up to today are 'delivered'; later days are 'projected'."""
    import datetime as _dt

    today = _dt.date.today().isoformat()
    return {
        "delivered": [d for d in daily if d["day"] <= today],
        "projected": [d for d in daily if d["day"] > today],
    }


def monitor_insights(
    campaign: dict[str, Any],
    delivered: list[dict[str, Any]],
    projected: list[dict[str, Any]],
) -> list[str]:
    impact = campaign.get("impact") or {}
    out: list[str] = []
    if delivered:
        best = max(delivered, key=lambda d: d["conversions"])
        out.append(
            f"Best delivered day so far: {best['day']} with {best['conversions']:,} "
            f"conversions and ₹{round(best['revenue']):,} revenue."
        )
    budget = float(campaign.get("budget_inr") or 0)
    spent = sum(d["spend"] for d in delivered)
    total_days = len(delivered) + len(projected)
    if budget > 0 and total_days:
        spent_pct = spent / budget * 100
        time_pct = len(delivered) / total_days * 100
        if not delivered:
            out.append("Campaign starts on its scheduled date; all figures shown are projected.")
        elif spent_pct > time_pct + 10:
            out.append(
                f"Pacing ahead: {spent_pct:.0f}% of budget used vs {time_pct:.0f}% of schedule elapsed."
            )
        elif spent_pct < time_pct - 10:
            out.append(
                f"Pacing behind: {spent_pct:.0f}% of budget used vs {time_pct:.0f}% of schedule elapsed."
            )
        else:
            out.append(
                f"Pacing on track: {spent_pct:.0f}% of budget used, {time_pct:.0f}% of schedule elapsed."
            )
    bench = objective_benchmark(campaign.get("objective"))
    ctr = float(impact.get("ctr") or 0)
    if ctr:
        diff = ctr - bench["ctr"]
        rel = "above" if diff >= 0 else "below"
        out.append(
            f"CTR {ctr:.2f}% is {abs(diff):.2f} pts {rel} the "
            f"{campaign.get('objective') or 'consideration'} benchmark of {bench['ctr']:.1f}%."
        )
    roas = float(impact.get("roas") or 0)
    if roas:
        verdict = "healthy" if roas >= 3 else ("break-even range" if roas >= 1.5 else "below target")
        out.append(f"ROAS {roas:.1f}x is {verdict} for a simulated 1P campaign.")
    reach = int(impact.get("reach") or 0)
    audience = int(campaign.get("est_audience") or 0)
    if reach and audience:
        out.append(
            f"Simulated reach covers {min(100.0, reach / audience * 100):.0f}% of the "
            f"{audience:,}-customer target audience."
        )
    return out[:5]


def sku_pin_fit(campaign_id: str) -> dict[str, float]:
    """Average PIN fit score (0–1 scale from compare-pins) per SKU in a campaign."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.sku_uuid::text, AVG(p.fit_score)
                FROM brand_sku_pin_scores p
                JOIN brand_skus s ON s.sku_uuid = p.sku_uuid
                WHERE s.campaign_id = %s
                GROUP BY p.sku_uuid
                """,
                (campaign_id,),
            )
            return {r[0]: float(r[1] or 0) for r in cur.fetchall()}


def insert_sku(
    *,
    sku_uuid: str,
    campaign_id: str,
    brand_name: str,
    original_filename: Optional[str],
    gcs_bucket: str,
    gcs_object_key: str,
    attributes: dict[str, Any],
    raw_result: Any,
    extract_ok: bool,
    extract_error: Optional[str] = None,
    process_status: str = "done",
    source: str = "upload",
    retail_product_id: Optional[str] = None,
    style_code: Optional[str] = None,
) -> None:
    src = source if source in ("upload", "catalog") else "upload"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brand_skus (
                  sku_uuid, campaign_id, brand_name, original_filename,
                  gcs_bucket, gcs_object_key,
                  color, pattern, material, size, commodity,
                  sleeve_type, collar_type, confidence, raw_result,
                  extract_ok, extract_error, process_status,
                  source, retail_product_id, style_code
                ) VALUES (
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    sku_uuid,
                    campaign_id,
                    brand_name,
                    original_filename,
                    gcs_bucket,
                    gcs_object_key,
                    attributes.get("color"),
                    attributes.get("pattern"),
                    attributes.get("material"),
                    attributes.get("size"),
                    attributes.get("name") or attributes.get("commodity"),
                    attributes.get("sleeve_type"),
                    attributes.get("collar_type"),
                    str(attributes.get("confidence")) if attributes.get("confidence") is not None else None,
                    json.dumps(raw_result) if raw_result is not None else None,
                    extract_ok,
                    extract_error,
                    process_status,
                    src,
                    retail_product_id,
                    style_code,
                ),
            )


def update_sku_extract(
    sku_uuid: str,
    *,
    attributes: dict[str, Any],
    raw_result: Any,
    extract_ok: bool,
    extract_error: Optional[str],
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brand_skus
                SET color = %s, pattern = %s, material = %s, size = %s, commodity = %s,
                    sleeve_type = %s, collar_type = %s, garment_category = %s,
                    garment_subcategory = %s, sleeve_length = %s, pairs_with = %s,
                    confidence = %s, raw_result = %s,
                    extract_ok = %s, extract_error = %s
                WHERE sku_uuid = %s
                """,
                (
                    attributes.get("color"),
                    attributes.get("pattern"),
                    attributes.get("material"),
                    attributes.get("size"),
                    attributes.get("name") or attributes.get("commodity"),
                    attributes.get("sleeve_type"),
                    attributes.get("collar_type"),
                    attributes.get("garment_category"),
                    attributes.get("garment_subcategory"),
                    attributes.get("sleeve_length"),
                    list(attributes.get("pairs_with") or []),
                    str(attributes.get("confidence")) if attributes.get("confidence") is not None else None,
                    json.dumps(raw_result) if raw_result is not None else None,
                    extract_ok,
                    extract_error,
                    sku_uuid,
                ),
            )


def update_sku_rank(
    sku_uuid: str,
    *,
    rank_score: Optional[float],
    sim_fit: float,
    attr_fit: float,
    freq_coverage: float,
    compare_error: Optional[str],
    process_status: str,
    process_seconds: Optional[float],
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brand_skus
                SET rank_score = %s, sim_fit = %s, attr_fit = %s, freq_coverage = %s,
                    compare_error = %s, process_status = %s, process_seconds = %s,
                    processed_at = now()
                WHERE sku_uuid = %s
                """,
                (rank_score, sim_fit, attr_fit, freq_coverage, compare_error,
                 process_status, process_seconds, sku_uuid),
            )


def list_pending_skus(campaign_id: str, *, limit: int) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sku_uuid::text, original_filename, gcs_object_key,
                       COALESCE(source, 'upload'), color, pattern, material, size, commodity,
                       brand_name, style_code, retail_product_id
                FROM brand_skus
                WHERE campaign_id = %s AND process_status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (campaign_id, limit),
            )
            return [
                {
                    "sku_uuid": r[0], "filename": r[1], "gcs_object_key": r[2], "source": r[3],
                    "attributes": {
                        "color": r[4], "pattern": r[5], "material": r[6], "size": r[7],
                        "commodity": r[8], "name": r[8], "brand": r[9],
                    },
                    "style_code": r[10], "retail_product_id": r[11],
                }
                for r in cur.fetchall()
            ]


def sku_progress(campaign_id: str) -> dict[str, Any]:
    """Counts by process status plus the observed average seconds per processed SKU."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE process_status = 'pending'),
                  COUNT(*) FILTER (WHERE process_status = 'done'),
                  COUNT(*) FILTER (WHERE process_status = 'failed'),
                  AVG(process_seconds) FILTER (WHERE process_seconds IS NOT NULL)
                FROM brand_skus WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            r = cur.fetchone() or (0, 0, 0, None)
            return {
                "pending": int(r[0] or 0),
                "done": int(r[1] or 0),
                "failed": int(r[2] or 0),
                "avg_seconds": float(r[3]) if r[3] is not None else None,
            }


def update_sku_embedding(
    sku_uuid: str, embedding: List[float], model: str, dim: int
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brand_skus
                SET embedding = %s::jsonb, embedding_model = %s, embedding_dim = %s
                WHERE sku_uuid = %s
                """,
                (json.dumps(embedding), model, dim, sku_uuid),
            )


def upsert_pin_scores(
    sku_uuid: str, per_pin: dict[str, Any]
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            for pin, detail in (per_pin or {}).items():
                cur.execute(
                    """
                    INSERT INTO brand_sku_pin_scores (sku_uuid, pin_code, fit_score, top_matches)
                    VALUES (%s,%s,%s,%s::jsonb)
                    ON CONFLICT (sku_uuid, pin_code) DO UPDATE SET
                      fit_score = EXCLUDED.fit_score,
                      top_matches = EXCLUDED.top_matches,
                      computed_at = now()
                    """,
                    (
                        sku_uuid,
                        str(pin),
                        float(detail.get("pin_fit") or 0),
                        json.dumps(detail.get("top_matches") or []),
                    ),
                )


def set_campaign_status(
    campaign_id: str, status: str, error_message: Optional[str] = None
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            extra = ""
            if status == "started":
                extra = ", started_at = COALESCE(started_at, now()), ignored_at = NULL"
            elif status == "ignored":
                extra = ", ignored_at = COALESCE(ignored_at, now())"
            cur.execute(
                f"""
                UPDATE brand_campaigns
                SET status = %s, error_message = %s, updated_at = now(){extra}
                WHERE campaign_id = %s
                """,
                (status, error_message, campaign_id),
            )


_CAMPAIGN_SELECT = """
    SELECT c.campaign_id::text, c.brand_name, c.pin_codes, c.age_group, c.gender,
           c.rfm_segment, c.status, c.error_message, c.title,
           c.started_at, c.ignored_at, c.created_at, c.updated_at,
           COALESCE(c.eligible_users, 0),
           COALESCE(c.recommendations_computed, 0),
           COALESCE(c.style_feed_serves, 0),
           i.reach, i.impressions, i.ctr, i.conversions,
           i.revenue, i.lift, i.roas, i.source, i.spend, i.clicks,
           c.campaign_name, c.objective, c.budget_inr, c.duration_days,
           c.start_date, c.end_date, c.selected_sku_ids, c.creative,
           c.source_context, c.est_audience, c.launched_at,
           (SELECT COUNT(*) FROM brand_skus s WHERE s.campaign_id = c.campaign_id),
           c.run_mode, c.channels, c.message_template, c.budget_recommended,
           i.channel_breakdown,
           c.platform_fee_recommended, c.platform_fee_final, c.fee_model_version,
           c.fee_features, c.fee_recommended_at, c.fee_accepted_at
    FROM brand_campaigns c
    LEFT JOIN campaign_impact_metrics i ON i.campaign_id = c.campaign_id
"""


def get_campaign(campaign_id: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _CAMPAIGN_SELECT + " WHERE c.campaign_id = %s",
                (campaign_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _campaign_row(row)


def _json_field(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _campaign_row(row: Sequence[Any]) -> dict[str, Any]:
    pins = row[2] or []
    if not isinstance(pins, list):
        pins = list(pins) if pins else []
    out: dict[str, Any] = {
        "campaign_id": row[0],
        "brand_name": row[1],
        "pin_codes": [str(p) for p in pins],
        "age_group": row[3],
        "gender": row[4],
        "rfm_segment": row[5],
        "status": row[6],
        "error_message": row[7],
        "title": row[8],
        "started_at": row[9].isoformat() if row[9] else None,
        "ignored_at": row[10].isoformat() if row[10] else None,
        "created_at": row[11].isoformat() if row[11] else None,
        "updated_at": row[12].isoformat() if row[12] else None,
        "eligible_users": int(row[13] or 0),
        "recommendations_computed": int(row[14] or 0),
        "style_feed_serves": int(row[15] or 0),
    }
    impact = _impact_from_row(
        row[16], row[17], row[18], row[19], row[20], row[21], row[22], row[23],
        row[24], row[25],
    )
    if impact:
        out["impact"] = impact
    sku_ids = row[32] or []
    out.update(
        {
            "campaign_name": row[26],
            "objective": row[27],
            "budget_inr": float(row[28]) if row[28] is not None else None,
            "duration_days": int(row[29]) if row[29] is not None else None,
            "start_date": row[30].isoformat() if row[30] else None,
            "end_date": row[31].isoformat() if row[31] else None,
            "selected_sku_ids": [str(s) for s in sku_ids],
            "creative": _json_field(row[33]) or {},
            "source_context": _json_field(row[34]),
            "est_audience": int(row[35]) if row[35] is not None else None,
            "launched_at": row[36].isoformat() if row[36] else None,
            "sku_count": int(row[37] or 0),
            "run_mode": row[38],
            "channels": [str(c) for c in (row[39] or [])] or None,
            "message_template": _json_field(row[40]) or {},
            "budget_recommended": float(row[41]) if row[41] is not None else None,
            "platform_fee_recommended": float(row[43]) if row[43] is not None else None,
            "platform_fee_final": float(row[44]) if row[44] is not None else None,
            "fee_model_version": row[45],
            "fee_features": _json_field(row[46]) or {},
            "fee_recommended_at": row[47].isoformat() if row[47] else None,
            "fee_accepted_at": row[48].isoformat() if row[48] else None,
        }
    )
    if impact:
        impact["channel_breakdown"] = _json_field(row[42]) or None
    return out


def list_campaigns(
    *,
    brand_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if brand_name:
        where.append("lower(c.brand_name) = lower(%s)")
        params.append(brand_name)
    if status:
        where.append("c.status = %s")
        params.append(status)
    sql = _CAMPAIGN_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.created_at DESC LIMIT %s"
    params.append(limit)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [_campaign_row(row) for row in cur.fetchall()]


def list_campaign_skus(campaign_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sku_uuid::text, brand_name, original_filename, gcs_bucket,
                       gcs_object_key, color, pattern, material, size, commodity,
                       extract_ok, extract_error, created_at,
                       rank_score, process_status, compare_error,
                       COALESCE(source, 'upload'), retail_product_id, style_code
                FROM brand_skus
                WHERE campaign_id = %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (campaign_id, limit),
            )
            rows = []
            for r in cur.fetchall():
                rows.append(
                    {
                        "sku_uuid": r[0],
                        "brand_name": r[1],
                        "original_filename": r[2],
                        "gcs_bucket": r[3],
                        "gcs_object_key": r[4],
                        "attributes": {
                            "color": r[5],
                            "pattern": r[6],
                            "material": r[7],
                            "size": r[8],
                            "commodity": r[9],
                            "name": r[9],
                        },
                        "extract_ok": bool(r[10]),
                        "extract_error": r[11],
                        "created_at": r[12].isoformat() if r[12] else None,
                        "rank_score": float(r[13]) if r[13] is not None else None,
                        "process_status": r[14] or "done",
                        "compare_error": r[15],
                        "source": r[16] or "upload",
                        "retail_product_id": r[17],
                        "style_code": r[18],
                    }
                )
            return rows


def campaign_fee_job_stats(campaign_id: str) -> dict[str, Any]:
    """Aggregate ranking work for pricing without reading any individual customer records."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(comparisons), 0),
                       COALESCE(SUM(EXTRACT(EPOCH FROM (finished_at - started_at))), 0)
                FROM campaign_jobs
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            row = cur.fetchone() or (0, 0)
    return {"comparisons": int(row[0] or 0), "process_seconds": float(row[1] or 0)}


def record_fee_decision(
    campaign_id: str,
    *,
    brand_name: str,
    event_type: str,
    model_version: str,
    source: str,
    recommended_fee_inr: float,
    final_fee_inr: Optional[float],
    media_budget_inr: Optional[float],
    features: dict[str, Any],
    actor_role: Optional[str],
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaign_fee_decisions (
                  decision_id, campaign_id, brand_name, event_type, model_version, source,
                  recommended_fee_inr, final_fee_inr, media_budget_inr, features, actor_role
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    str(uuid.uuid4()), campaign_id, brand_name, event_type, model_version, source,
                    float(recommended_fee_inr), float(final_fee_inr) if final_fee_inr is not None else None,
                    float(media_budget_inr) if media_budget_inr is not None else None,
                    json.dumps(features), actor_role,
                ),
            )


def wardrobe_owners(user_uuids: Sequence[str]) -> set[str]:
    """Which of these customers have at least one garment in user_wardrobe (Cloud SQL)."""
    ids = sorted({str(u).strip() for u in user_uuids if str(u).strip()})
    if not ids:
        return set()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_uuid FROM user_wardrobe WHERE user_uuid = ANY(%s)", (ids,))
            return {str(r[0]) for r in cur.fetchall()}


def upsert_user_recommendation(
    *,
    campaign_id: str,
    user_uuid: str,
    brand_name: str,
    fit_score: Optional[float],
    payload: dict[str, Any],
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaign_user_recommendations (
                  campaign_id, user_uuid, brand_name, fit_score, payload
                ) VALUES (%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (campaign_id, user_uuid) DO UPDATE SET
                  brand_name = EXCLUDED.brand_name,
                  fit_score = EXCLUDED.fit_score,
                  payload = EXCLUDED.payload,
                  computed_at = now()
                """,
                (
                    campaign_id,
                    user_uuid,
                    brand_name,
                    fit_score,
                    json.dumps(payload),
                ),
            )


def set_campaign_impact(
    campaign_id: str,
    *,
    eligible_users: Optional[int] = None,
    recommendations_computed: Optional[int] = None,
    bump_style_feed_serves: int = 0,
) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            sets = ["updated_at = now()"]
            args: list[Any] = []
            if eligible_users is not None:
                sets.append("eligible_users = %s")
                args.append(int(eligible_users))
            if recommendations_computed is not None:
                sets.append("recommendations_computed = %s")
                args.append(int(recommendations_computed))
            if bump_style_feed_serves:
                sets.append("style_feed_serves = COALESCE(style_feed_serves, 0) + %s")
                args.append(int(bump_style_feed_serves))
            args.append(campaign_id)
            cur.execute(
                f"UPDATE brand_campaigns SET {', '.join(sets)} WHERE campaign_id = %s",
                tuple(args),
            )


# Campaigns whose saved per-customer picks are shown in the Style Assistant
# (from the moment ranking finishes; discarded / draft / error campaigns are hidden).
STYLE_FEED_STATUSES = ("ranked", "configured", "launched", "exported", "started")


def list_recs_for_user(user_uuid: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.campaign_id::text, r.user_uuid, r.brand_name, r.fit_score,
                       r.payload, r.computed_at, c.status, c.creative, c.channels
                FROM campaign_user_recommendations r
                JOIN brand_campaigns c ON c.campaign_id = r.campaign_id
                WHERE r.user_uuid = %s AND c.status = ANY(%s)
                ORDER BY COALESCE(r.fit_score, 0) DESC, r.computed_at DESC
                LIMIT %s
                """,
                (user_uuid, list(STYLE_FEED_STATUSES), limit),
            )
            out = []
            for row in cur.fetchall():
                payload = row[4]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                out.append(
                    {
                        "campaign_id": row[0],
                        "user_uuid": row[1],
                        "brand_name": row[2],
                        "fit_score": float(row[3]) if row[3] is not None else None,
                        "payload": payload,
                        "computed_at": row[5].isoformat() if row[5] else None,
                        "status": row[6],
                        "creative": _json_field(row[7]) or {},
                        "channels": list(row[8] or []),
                    }
                )
            return out


def ensure_campaign_title(campaign_id: str, brand_name: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brand_campaigns
                SET title = COALESCE(NULLIF(trim(title), ''), %s)
                WHERE campaign_id = %s
                """,
                (f"{brand_name} · 1P", campaign_id),
            )


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def audience_values(v: Any) -> set[str]:
    """Audience filters may hold several comma-separated choices ("25-34, 35-44"); empty set = any."""
    return {p for p in (_norm(x) for x in str(v or "").split(",")) if p}


def _cell_matches(
    cell: dict[str, Any],
    *,
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
) -> bool:
    pins = {str(p).strip() for p in pin_codes if str(p).strip()}
    cell_pin = str(cell.get("pin_code") or cell.get("pin") or "").strip()
    if pins and cell_pin and cell_pin not in pins:
        return False
    ages = audience_values(age_group)
    if ages:
        ca = _norm(cell.get("age_group") or cell.get("age"))
        if ca and ca not in ages:
            return False
    genders = {g[0] for g in audience_values(gender)}
    if genders:
        cg = _norm(cell.get("gender"))
        if cg and cg[0] not in genders:
            return False
    rfms = audience_values(rfm_segment)
    if rfms:
        cr = _norm(cell.get("rfm_segment") or cell.get("rfm"))
        if cr and cr not in rfms:
            return False
    return True


def audience_size(
    metrics: dict[str, Any],
    *,
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
) -> int:
    """Customers in metrics customer_cells matching the campaign audience filters."""
    cells = metrics.get("customer_cells") or []
    if not isinstance(cells, list):
        return 0
    total = 0
    for c in cells:
        if isinstance(c, dict) and _cell_matches(
            c,
            pin_codes=pin_codes,
            age_group=age_group,
            gender=gender,
            rfm_segment=rfm_segment,
        ):
            try:
                total += int(c.get("customers") or 0)
            except (TypeError, ValueError):
                continue
    return total


def build_segment_profile(
    metrics: dict[str, Any],
    *,
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
) -> dict[str, Any]:
    """Preference counts from metrics garment cells filtered by campaign audience."""
    cells = metrics.get("garment_segment_cells") or metrics.get("garment_cells") or []
    if not isinstance(cells, list):
        cells = []

    filtered = [
        c
        for c in cells
        if isinstance(c, dict)
        and _cell_matches(
            c,
            pin_codes=pin_codes,
            age_group=age_group,
            gender=gender,
            rfm_segment=rfm_segment,
        )
    ]
    # Progressive relax if empty (keep PINs if possible)
    if not filtered and (age_group or gender or rfm_segment):
        filtered = [
            c
            for c in cells
            if isinstance(c, dict)
            and _cell_matches(
                c, pin_codes=pin_codes, age_group=None, gender=None, rfm_segment=None
            )
        ]

    dist: dict[str, dict[str, float]] = {
        "color": {},
        "pattern": {},
        "material": {},
        "size": {},
        "commodity": {},
        "brand": {},
    }
    key_map = {
        "color": ("colors", "color"),
        "pattern": ("patterns", "pattern"),
        "material": ("materials", "material"),
        "size": ("sizes", "size"),
        "commodity": ("commodities", "commodity", "name"),
        "brand": ("brands", "brand"),
    }

    for cell in filtered:
        n = float(cell.get("garments") or cell.get("count") or 1)
        for dim, keys in key_map.items():
            pairs = None
            for k in keys:
                if isinstance(cell.get(k), list):
                    pairs = cell.get(k)
                    break
            if pairs:
                for p in pairs:
                    if isinstance(p, dict):
                        val = p.get("value") or p.get("name")
                        cnt = float(p.get("count") or n)
                    else:
                        val, cnt = p, n
                    key = vocab.attr_key(dim, val)
                    if key:
                        dist[dim][key] = dist[dim].get(key, 0) + cnt
            else:
                for k in keys:
                    if cell.get(k) and not isinstance(cell.get(k), list):
                        key = vocab.attr_key(dim, cell.get(k))
                        if key:
                            dist[dim][key] = dist[dim].get(key, 0) + n
                        break

    return {
        "cell_count": len(filtered),
        "distributions": dist,
        "filters": {
            "pin_codes": list(pin_codes),
            "age_group": age_group,
            "gender": gender,
            "rfm_segment": rfm_segment,
        },
    }


def attr_fit_score(attrs: dict[str, Any], profile: dict[str, Any]) -> float:
    """0–1 weighted preference overlap (mirrors Ads Lab matchScore idea).

    Profile distributions are keyed by vocab.attr_key, so 'navy' on a SKU matches 'Navy Blue' garments.
    """
    weights = {
        "color": 25,
        "pattern": 20,
        "commodity": 20,
        "material": 15,
        "size": 10,
        "brand": 10,
    }
    dist = (profile or {}).get("distributions") or {}
    mapping = {
        "color": "color",
        "pattern": "pattern",
        "commodity": "name",
        "material": "material",
        "size": "size",
        "brand": "brand",
    }
    total_w = 0.0
    scored = 0.0
    for dim, attr_key in mapping.items():
        distribution = dist.get(dim) or {}
        pairs = [(k, float(v)) for k, v in distribution.items() if float(v) > 0]
        val = vocab.attr_key(dim, attrs.get(attr_key) or attrs.get(dim))
        if not val or not pairs:
            continue
        mx = max(v for _, v in pairs) or 1.0
        found = next((v for k, v in pairs if k == val), 0.0)
        ratio = found / mx
        w = weights[dim]
        scored += ratio * w
        total_w += w
    if total_w <= 0:
        return 0.0
    return float(scored / total_w)


def blend_rank_score(
    *, sim_fit: float, freq_coverage: float, attr_fit: float
) -> float:
    return round(
        100.0 * (0.55 * sim_fit + 0.15 * freq_coverage + 0.30 * attr_fit), 2
    )


def freq_coverage_from_compare(per_pin: dict[str, Any]) -> float:
    """Share of PIN observations covered by top matches (soft 0–1)."""
    if not per_pin:
        return 0.0
    scores = []
    for detail in per_pin.values():
        total = float(detail.get("total_observations") or 0)
        top = detail.get("top_matches") or []
        covered = sum(float(m.get("observation_count") or 0) for m in top)
        if total <= 0:
            scores.append(0.0)
        else:
            scores.append(min(1.0, covered / total))
    return float(sum(scores) / len(scores)) if scores else 0.0
