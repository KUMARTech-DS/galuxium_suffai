"""RFM segment per customer from BigQuery suffai_ops.orders: rule rfm-v1 (same as suffai/metrics/rfm.py).

BigQuery orders have no status or payment columns, so every dated order counts. Recency is days since the
customer's last order, frequency the distinct orders and monetary the summed amount, both within 365 days
of the latest order date. Customers with no order in that window (or none at all) are New.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

RULE_VERSION = "rfm-v1"
WINDOW_DAYS = 365

NEW = "New"
CHAMPIONS = "Champions"
LOYAL = "Loyal"
POTENTIAL = "Potential Loyalist"
AT_RISK = "At Risk"
HIBERNATING = "Hibernating"
SEGMENTS = (CHAMPIONS, LOYAL, POTENTIAL, AT_RISK, HIBERNATING, NEW)


def _day(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v or "").strip()[:10])
    except ValueError:
        return None


def _amount(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _quintile_scores(values: list[float], higher_is_better: bool = True) -> dict[int, int]:
    """Quintile 1..5 by rank; ties keep input order (same as rfm-v1)."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    n = len(indexed)
    scores: dict[int, int] = {}
    for rank_i, (orig_i, _) in enumerate(indexed):
        q = min(5, (rank_i * 5) // n + 1)
        scores[orig_i] = q if higher_is_better else (6 - q)
    return scores


def _label(r: int, f: int, m: int, recency_days: int, frequency: int) -> str:
    if frequency == 0:
        return NEW
    if r >= 4 and f >= 4 and m >= 4:
        return CHAMPIONS
    if f >= 4 and m >= 3:
        return LOYAL
    if r >= 3 and f >= 2 and m >= 2:
        return POTENTIAL
    if recency_days > 120 and frequency >= 2:
        return AT_RISK
    if recency_days > 180:
        return HIBERNATING
    return POTENTIAL


def segments(customer_ids: Iterable[str], orders: Iterable[dict[str, Any]], *,
             as_of: Optional[date] = None) -> dict[str, str]:
    """customer_id -> segment. orders: BigQuery rows with _id, customer_id, scheduled_date, amount."""
    by_cust: dict[str, list[tuple[str, date, float]]] = defaultdict(list)
    for o in orders:
        d = _day(o.get("scheduled_date"))
        cid = str(o.get("customer_id") or "").strip()
        if not cid or d is None or (as_of is not None and d > as_of):
            continue
        by_cust[cid].append((str(o.get("_id") or ""), d, _amount(o.get("amount"))))

    out = {str(c): NEW for c in customer_ids}
    if not by_cust:
        return out
    as_of = as_of or max(d for rows in by_cust.values() for _, d, _ in rows)
    start = as_of - timedelta(days=WINDOW_DAYS)

    cust_ids = sorted(by_cust)
    recency, frequency, monetary, meta = [], [], [], []
    for cid in cust_ids:
        rows = by_cust[cid]
        r_days = (as_of - max(d for _, d, _ in rows)).days
        window = [(oid, a) for oid, d, a in rows if start <= d <= as_of]
        f = len({oid for oid, _ in window})
        m = sum(a for _, a in window)
        recency.append(float(r_days))
        frequency.append(float(f))
        monetary.append(float(m))
        meta.append((cid, r_days, f))

    r_s = _quintile_scores(recency, higher_is_better=False)
    f_s = _quintile_scores(frequency)
    m_s = _quintile_scores(monetary)
    for i, (cid, r_days, f) in enumerate(meta):
        out[cid] = NEW if f == 0 else _label(r_s[i], f_s[i], m_s[i], r_days, f)
    return out
