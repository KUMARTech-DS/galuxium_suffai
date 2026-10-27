"""Photographed garments per PIN: one count for the Ads Lab tile, Admin Unique garments and ranking.

A photographed garment is the similarity garment (unique_garment_id) of a BigQuery garment_items row
that has a photo, counted under its customer's current PIN. compare-pins scores a brand SKU against
corpus(), so the ranking compares with exactly the garments the tile and Admin count.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence


def cap() -> int:
    """compare-pins compares a SKU with at most this many garments per PIN (the tile is capped alike)."""
    try:
        return max(1, int(os.getenv("COMPARE_PIN_CANDIDATE_LIMIT", "2000")))
    except ValueError:
        return 2000


def garment_id(item: Mapping[str, Any]) -> str:
    """The similarity garment of a garment item; '' unless the item has a photo and a match."""
    gid = str(item.get("unique_garment_id") or "").strip()
    return gid if gid and str(item.get("gcs_object_key") or "").strip() else ""


def by_pin(photos: Iterable[tuple[str, str]], pin_of: Mapping[str, Optional[str]]) -> dict[str, Counter]:
    """PIN -> garment id -> photos, from (customer_id, garment_id) photos and customer id -> current PIN.

    Photos of customers that no longer exist or have no PIN are left out.
    """
    out: dict[str, Counter] = defaultdict(Counter)
    for customer_id, gid in photos:
        pin = str(pin_of.get(str(customer_id)) or "").strip()
        if gid and pin:
            out[pin][gid] += 1
    return out


def corpus(data: Mapping[str, Any], pins: Sequence[str], *, limit: Optional[int] = None) -> dict[str, Any]:
    """compare-pins candidates from admin_intel.dataset(): each PIN's top garments and its total photos.

    candidates: PIN -> [[garment_id, photos], ...], most photographed first (then garment id), at most
    `limit` (cap()) per PIN; pin_totals: PIN -> photos of all its garments.
    """
    n = limit or cap()
    pin_of = {cid: c.get("pin_code") for cid, c in (data.get("customers") or {}).items()}
    counts = by_pin(((o["customer_id"], o["garment_id"]) for o in data.get("observations") or []), pin_of)
    candidates: dict[str, list[list[Any]]] = {}
    totals: dict[str, int] = {}
    for pin in pins:
        c = counts.get(pin) or Counter()
        candidates[pin] = [[g, k] for g, k in sorted(c.items(), key=lambda t: (-t[1], t[0]))[:n]]
        totals[pin] = sum(c.values())
    return {"candidates": candidates, "pin_totals": totals}
