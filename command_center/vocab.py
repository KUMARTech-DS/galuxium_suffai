"""Canonical values from BigQuery suffai_ops.ref_values (same rules as worker/vocab.py).

value_key() must give the same key as worker/vocab.py and the SQL in migrate_bq_consistency.sql:
REGEXP_REPLACE(LOWER(TRIM(x)), r'[^a-z0-9]', ''). So "Levi's", "Levis" and "LEVI'S" all map to levis.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("command_center.vocab")

KINDS = ("brand", "color", "garment_name", "customer_gender", "garment_gender")
UNKNOWN_KEYS = frozenset({"", "unknown", "none", "null", "na", "notapplicable"})
CACHE_SEC = 600
RETRY_SEC = 60
_KEY_RE = re.compile(r"[^a-z0-9]")

# Fixed vocabularies, also seeded into ref_values; they apply even when the table cannot be read.
DEFAULTS: dict[str, dict[str, str]] = {
    "customer_gender": {"female": "Female", "f": "Female", "woman": "Female", "women": "Female",
                        "male": "Male", "m": "Male", "man": "Male", "men": "Male", "other": "Other", "o": "Other"},
    "garment_gender": {"male": "Male", "m": "Male", "men": "Male", "mens": "Male", "female": "Female",
                       "f": "Female", "women": "Female", "womens": "Female", "unisex": "Unisex", "u": "Unisex",
                       "other": "Unisex", "kids": "Kids", "kid": "Kids", "boys": "Kids", "girls": "Kids"},
}

# Test hook: returns ref_values rows ({kind, canonical, alias_key}) instead of BigQuery.
_loader: Optional[Callable[[], list[dict[str, Any]]]] = None
_lock = threading.Lock()
_cache: dict[str, Any] = {"until": 0.0, "map": None}


def value_key(v: Any) -> str:
    return _KEY_RE.sub("", str(v if v is not None else "").strip().lower())


def is_unknown(v: Any) -> bool:
    return value_key(v) in UNKNOWN_KEYS


def clear_cache() -> None:
    with _lock:
        _cache.update(until=0.0, map=None)


def _load_rows() -> list[dict[str, Any]]:
    if _loader is not None:
        return _loader()
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT not set")
    from google.cloud import bigquery  # type: ignore

    table = f"`{project}.{os.getenv('BQ_DATASET', 'suffai_ops')}.ref_values`"
    client = bigquery.Client(project=project)
    return [dict(r) for r in client.query(f"SELECT kind, canonical, alias_key FROM {table}").result()]


def mapping() -> dict[str, dict[str, str]]:
    """kind -> alias_key -> canonical, cached CACHE_SEC (RETRY_SEC after a failed read)."""
    now = time.time()
    with _lock:
        if _cache["map"] is not None and now < _cache["until"]:
            return _cache["map"]
    out: dict[str, dict[str, str]] = {k: dict(v) for k, v in DEFAULTS.items()}
    ttl = CACHE_SEC
    try:
        for r in _load_rows():
            kind, canon, key = str(r.get("kind") or ""), str(r.get("canonical") or "").strip(), str(r.get("alias_key") or "")
            if kind in KINDS and canon and key:
                out.setdefault(kind, {})[key] = canon
    except Exception as exc:
        log.warning("ref_values unavailable (%s); using fixed vocabularies only", type(exc).__name__)
        ttl = RETRY_SEC
    with _lock:
        _cache.update(until=now + ttl, map=out)
    return out


def canonical(kind: str, value: Any, default: str = "unknown") -> str:
    """Canonical spelling of value, the value itself when it has none, `default` when it is unknown."""
    key = value_key(value)
    if key in UNKNOWN_KEYS:
        return default
    return mapping().get(kind, {}).get(key) or str(value).strip()


_ATTR_KINDS = {"color": "color", "name": "garment_name", "commodity": "garment_name", "brand": "brand"}


def attr_key(dim: str, value: Any) -> str:
    """Comparison key for a garment attribute: 'navy' and 'Navy Blue', 't_shirt' and 'T-Shirt' share one.

    Colour, garment name and brand go through their canonical spelling first; '' when unknown.
    """
    kind = _ATTR_KINDS.get(dim)
    key = value_key(canonical(kind, value, default="") if kind else value)
    return "" if key in UNKNOWN_KEYS else key


def canonical_values(kind: str, limit: int = 60) -> list[str]:
    vals = sorted({v for v in mapping().get(kind, {}).values() if v})
    return vals[:limit]
