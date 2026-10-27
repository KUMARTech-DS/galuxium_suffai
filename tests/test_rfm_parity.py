"""command_center/rfm.py (BigQuery orders) gives the same segments as suffai/metrics/rfm.py (rfm-v1)."""

import importlib.util
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from suffai.metrics.rfm import compute_rfm_snapshots

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("cc_rfm", ROOT / "command_center" / "rfm.py")
cc_rfm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc_rfm)


def _orders(seed: int, customers: int = 60) -> list[dict]:
    rnd = random.Random(seed)
    start = date(2025, 1, 1)
    out = []
    for c in range(customers):
        for k in range(rnd.choice([0, 1, 1, 2, 3, 5, 8, 13])):
            d = start + timedelta(days=rnd.randrange(0, 640))
            out.append({"_id": f"o{c}-{k}", "customer_id": f"c{c:03d}", "scheduled_date": d.isoformat(),
                        "amount": rnd.choice([0, 199, 450, 1200, 3800, None])})
    return out


def _reference(orders: list[dict]) -> dict[str, str]:
    as_ref = [{"order_id": o["_id"], "customer_id": o["customer_id"], "total_amount": o["amount"],
               "ordered_at": datetime.fromisoformat(o["scheduled_date"]).replace(tzinfo=timezone.utc),
               "order_status": "delivered_to_customer", "payment_status": "paid", "refund_flag": False}
              for o in orders]
    return {s["customer_id"]: s["rfm_segment"] for s in compute_rfm_snapshots(as_ref)}


@pytest.mark.parametrize("seed", range(8))
def test_segments_match_rfm_v1(seed):
    orders = _orders(seed)
    ids = [f"c{c:03d}" for c in range(60)]
    got = cc_rfm.segments(ids, orders)
    ref = _reference(orders)
    assert {c: got[c] for c in ref} == ref
    assert all(got[c] == cc_rfm.NEW for c in ids if c not in ref)  # no orders at all
    assert set(got.values()) <= set(cc_rfm.SEGMENTS)


def test_window_and_ids():
    orders = [
        {"_id": "a1", "customer_id": "A", "scheduled_date": "2026-10-01", "amount": 500},
        {"_id": "a1", "customer_id": "A", "scheduled_date": "2026-10-01", "amount": 500},  # same order twice
        {"_id": "b1", "customer_id": "B", "scheduled_date": "2025-06-01", "amount": 9000},  # outside 365 days
        {"_id": "x1", "customer_id": "", "scheduled_date": "2026-10-01", "amount": 1},
        {"_id": "y1", "customer_id": "C", "scheduled_date": None, "amount": 1},
    ]
    got = cc_rfm.segments(["A", "B", "C", "D"], orders)
    assert got["B"] == got["C"] == got["D"] == cc_rfm.NEW
    assert got["A"] != cc_rfm.NEW
    assert cc_rfm.segments(["A"], []) == {"A": cc_rfm.NEW}
