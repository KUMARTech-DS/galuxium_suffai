"""k-anonymity suppression for aggregate cells."""

from __future__ import annotations

from typing import Any

from suffai import PRIVACY_K


def suppress_cell(
    distinct_users: int,
    value: Any,
    *,
    k: int = PRIVACY_K,
) -> dict[str, Any]:
    if distinct_users < k:
        return {
            "value": None,
            "cohort_size": distinct_users,
            "suppressed": True,
            "reason": f"Below privacy threshold k={k}",
        }
    return {
        "value": value,
        "cohort_size": distinct_users,
        "suppressed": False,
        "reason": None,
    }


def filter_export_rows(rows: list[dict[str, Any]], *, k: int = PRIVACY_K) -> list[dict[str, Any]]:
    """Drop or nullify suppressed rows for CSV export."""
    out = []
    for r in rows:
        if r.get("suppressed"):
            continue
        denom = r.get("denominator") or r.get("distinct_customers") or r.get("customers")
        if isinstance(denom, int) and denom < k:
            continue
        out.append(r)
    return out
