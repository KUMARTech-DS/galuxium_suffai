"""RFM segmentation from eligible completed paid orders (SEG-01)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from suffai.metrics.eligibility import eligible_orders

RFM_RULE_VERSION = "rfm-v1"
WINDOW_DAYS = 365

# Labels per PRD §8
LABEL_NEW = "New"
LABEL_CHAMPION = "Champions"
LABEL_LOYAL = "Loyal"
LABEL_POTENTIAL = "Potential Loyalist"
LABEL_AT_RISK = "At Risk"
LABEL_HIBERNATING = "Hibernating"


def _as_of_default(orders: list[dict[str, Any]]) -> datetime:
    dates = [o["ordered_at"] for o in orders if o.get("ordered_at")]
    if not dates:
        return datetime.now(timezone.utc)
    return max(dates)


def _quintile_scores(values: list[float], higher_is_better: bool = True) -> dict[Any, int]:
    """Deterministic quintile 1..5. Ties broken by sorted unique thresholds."""
    if not values:
        return {}
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    n = len(indexed)
    scores: dict[int, int] = {}
    for rank_i, (orig_i, _) in enumerate(indexed):
        # 0..n-1 → quintile 1..5
        q = min(5, (rank_i * 5) // n + 1)
        scores[orig_i] = q if higher_is_better else (6 - q)
    return scores


def compute_rfm_snapshots(
    orders: list[dict[str, Any]],
    stores: list[dict[str, Any]] | None = None,
    *,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    eligible = eligible_orders(orders, as_of=as_of)
    if not eligible:
        return []

    as_of = as_of or _as_of_default(eligible)
    window_start = as_of - timedelta(days=WINDOW_DAYS)
    premium_stores = {
        s["store_id"] for s in (stores or []) if s.get("premium_laundry") and s.get("store_id")
    }

    by_cust: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in eligible:
        cid = o.get("customer_id")
        if cid:
            by_cust[cid].append(o)

    cust_ids = sorted(by_cust.keys())
    recency: list[float] = []
    frequency: list[float] = []
    monetary: list[float] = []
    meta: list[dict[str, Any]] = []

    for cid in cust_ids:
        os_ = by_cust[cid]
        last = max((o["ordered_at"] for o in os_ if o.get("ordered_at")), default=None)
        r_days = int((as_of - last).days) if last else 9999
        in_window = [
            o for o in os_
            if o.get("ordered_at") and window_start <= o["ordered_at"] <= as_of
        ]
        # Distinct order IDs
        f = len({o["order_id"] for o in in_window})
        m = sum(float(o.get("total_amount") or 0) for o in in_window)
        prem = sum(1 for o in in_window if o.get("store_id") in premium_stores)
        prem_frac = (prem / f) if f else 0.0

        # Insufficient history: no orders in window and only 1 lifetime or very old
        insufficient = f == 0

        recency.append(float(r_days))
        frequency.append(float(f))
        monetary.append(float(m))
        meta.append({
            "customer_id": cid,
            "recency_days": r_days,
            "frequency": f,
            "monetary": round(m, 2),
            "premium_store_preference": round(prem_frac, 4),
            "insufficient_history": insufficient,
            "last_order_at": last,
        })

    # Quintiles: lower recency better → invert
    r_scores = _quintile_scores(recency, higher_is_better=False)
    f_scores = _quintile_scores(frequency, higher_is_better=True)
    m_scores = _quintile_scores(monetary, higher_is_better=True)

    snapshots = []
    for i, mrow in enumerate(meta):
        if mrow["insufficient_history"]:
            label = LABEL_NEW
            r_s = f_s = m_s = 0
        else:
            r_s = r_scores.get(i, 1)
            f_s = f_scores.get(i, 1)
            m_s = m_scores.get(i, 1)
            label = _label(r_s, f_s, m_s, mrow["recency_days"], mrow["frequency"])

        snapshots.append({
            **mrow,
            "r_score": r_s,
            "f_score": f_s,
            "m_score": m_s,
            "rfm_segment": label,
            "as_of_date": as_of.date().isoformat(),
            "rule_version": RFM_RULE_VERSION,
            "window_days": WINDOW_DAYS,
        })
    return snapshots


def _label(r: int, f: int, m: int, recency_days: int, frequency: int) -> str:
    if frequency == 0:
        return LABEL_NEW
    if r >= 4 and f >= 4 and m >= 4:
        return LABEL_CHAMPION
    if f >= 4 and m >= 3:
        return LABEL_LOYAL
    if r >= 3 and f >= 2 and m >= 2:
        return LABEL_POTENTIAL
    if recency_days > 120 and frequency >= 2:
        return LABEL_AT_RISK
    if recency_days > 180:
        return LABEL_HIBERNATING
    return LABEL_POTENTIAL


def rfm_distribution(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for s in snapshots:
        counts[s["rfm_segment"]] += 1
    total = sum(counts.values()) or 1
    return [
        {
            "rfm_segment": seg,
            "customers": n,
            "share_pct": round(100.0 * n / total, 2),
        }
        for seg, n in sorted(counts.items(), key=lambda x: -x[1])
    ]
