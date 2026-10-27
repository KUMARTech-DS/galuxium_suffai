"""Order eligibility for RFM / monetary aggregates (PRD §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

ELIGIBLE_STATUSES = {"delivered_to_customer"}
PAID_STATUSES = {"paid", "paid (online)"}


def is_eligible_order(order: dict[str, Any], *, require_paid: bool = True) -> bool:
    status = (order.get("order_status") or "").strip().lower()
    if status not in ELIGIBLE_STATUSES:
        return False
    if order.get("refund_flag"):
        return False
    if require_paid:
        pay = (order.get("payment_status") or "").strip().lower()
        if pay not in PAID_STATUSES:
            return False
    return True


def eligible_orders(
    orders: Iterable[dict[str, Any]],
    *,
    as_of: datetime | None = None,
    require_paid: bool = True,
) -> list[dict[str, Any]]:
    out = []
    for o in orders:
        if not is_eligible_order(o, require_paid=require_paid):
            continue
        ts = o.get("ordered_at")
        if as_of is not None and ts is not None and ts > as_of:
            continue
        out.append(o)
    return out
