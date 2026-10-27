"""Differencing-attack resistance for overlapping aggregate queries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from config import settings


def _fingerprint(filters: dict[str, Any]) -> str:
    payload = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _cohort_tokens(cohort: list[str]) -> list[str]:
    """Raw IDs locally; salted hashes in staging/prod (never persist PII)."""
    if settings.ENVIRONMENT not in {"staging", "prod"}:
        return cohort
    salt = settings.JWT_SECRET
    return [hashlib.sha256(f"{salt}:{c}".encode()).hexdigest()[:20] for c in cohort]


def _load_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_log(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows[-500:], indent=2) + "\n", encoding="utf-8")


def _detect_risk(set_a: set[str], prior: list[dict[str, Any]], tenant_id: str) -> dict[str, Any]:
    risk = {"differencing_risk": False, "reason": None, "paired_fingerprint": None}
    k = settings.PRIVACY_K
    for p in reversed(prior):
        if p.get("tenant_id") != tenant_id:
            continue
        if int(p.get("cohort_size") or 0) < k:
            continue
        set_b = set(p.get("customer_ids") or [])
        if not set_b:
            continue
        if set_a.issubset(set_b) or set_b.issubset(set_a):
            delta = abs(len(set_a) - len(set_b))
            if 0 < delta < k:
                return {
                    "differencing_risk": True,
                    "reason": f"Cohort differencing delta={delta} < k={k}",
                    "paired_fingerprint": p.get("fingerprint"),
                }
        sym = set_a.symmetric_difference(set_b)
        if 0 < len(sym) < k and len(set_a & set_b) >= k:
            return {
                "differencing_risk": True,
                "reason": f"Symmetric difference {len(sym)} < k={k}",
                "paired_fingerprint": p.get("fingerprint"),
            }
    return risk


def register_query(
    *,
    tenant_id: str,
    filters: dict[str, Any],
    cohort_customer_ids: Iterable[str],
) -> dict[str, Any]:
    """
    Record a query cohort. If PRIVACY_DIFFERENCING_ENABLED and the symmetric
    difference vs a prior same-tenant query is < k while both cohorts ≥ k,
    flag differencing risk so callers can suppress the response.
    """
    cohort = sorted({str(c) for c in cohort_customer_ids if c})
    tokens = _cohort_tokens(cohort)
    fp = _fingerprint({"tenant": tenant_id, **filters})
    entry = {
        "fingerprint": fp,
        "tenant_id": tenant_id,
        "filters": filters,
        "cohort_size": len(cohort),
        "cohort_hash": hashlib.sha256(",".join(tokens).encode()).hexdigest()[:24],
        "customer_ids": tokens[:5000],
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    risk = {"differencing_risk": False, "reason": None, "paired_fingerprint": None}
    prior = _load_log(settings.PRIVACY_QUERY_LOG_PATH)
    if settings.PRIVACY_DIFFERENCING_ENABLED and len(cohort) >= settings.PRIVACY_K:
        risk = _detect_risk(set(tokens), prior, tenant_id)

    prior.append(entry)
    _save_log(settings.PRIVACY_QUERY_LOG_PATH, prior)

    try:
        from services.ops_db import mirror_privacy_query

        mirror_privacy_query(entry, risk)
    except Exception:
        pass

    return {**risk, "fingerprint": fp, "cohort_size": len(cohort)}


def enforce_export_role(role: Optional[str]) -> bool:
    required = settings.EXPORT_REQUIRE_ROLE
    if not required:
        return True
    allowed = {required, "suffai_admin", "brand_admin", "brand_analyst"}
    return (role or "") in allowed
