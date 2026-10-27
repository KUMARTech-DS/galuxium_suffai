"""Canonical values from BigQuery suffai_ops.ref_values (same rules as command_center/vocab.py).

value_key() must give the same key as command_center/vocab.py and the SQL in migrate_bq_consistency.sql:
REGEXP_REPLACE(LOWER(TRIM(x)), r'[^a-z0-9]', ''). So "Levi's", "Levis" and "LEVI'S" all map to levis.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("suffai-extraction.vocab")

KINDS = ("brand", "color", "garment_name", "customer_gender", "garment_gender")
UNKNOWN_KEYS = frozenset({"", "unknown", "none", "null", "na", "notapplicable"})
CACHE_SEC = 600
RETRY_SEC = 60
_KEY_RE = re.compile(r"[^a-z0-9]")

# Fixed vocabularies, also seeded into ref_values; they apply even when the table cannot be read.
DEFAULTS: Dict[str, Dict[str, str]] = {
    "customer_gender": {"female": "Female", "f": "Female", "woman": "Female", "women": "Female",
                        "male": "Male", "m": "Male", "man": "Male", "men": "Male", "other": "Other", "o": "Other"},
    "garment_gender": {"male": "Male", "m": "Male", "men": "Male", "mens": "Male", "female": "Female",
                       "f": "Female", "women": "Female", "womens": "Female", "unisex": "Unisex", "u": "Unisex",
                       "other": "Unisex", "kids": "Kids", "kid": "Kids", "boys": "Kids", "girls": "Kids"},
}

# Test hook: returns ref_values rows ({kind, canonical, alias_key}) instead of BigQuery.
_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None
_lock = threading.Lock()
_cache: Dict[str, Any] = {"until": 0.0, "map": None}


def value_key(v: Any) -> str:
    return _KEY_RE.sub("", str(v if v is not None else "").strip().lower())


def is_unknown(v: Any) -> bool:
    return value_key(v) in UNKNOWN_KEYS


def clear_cache() -> None:
    with _lock:
        _cache.update(until=0.0, map=None)


def _load_rows() -> List[Dict[str, Any]]:
    if _loader is not None:
        return _loader()
    from worker import bq_items

    return bq_items._run(f"SELECT kind, canonical, alias_key FROM {bq_items.table('ref_values')}", [])


def mapping() -> Dict[str, Dict[str, str]]:
    """kind -> alias_key -> canonical, cached CACHE_SEC (RETRY_SEC after a failed read)."""
    now = time.time()
    with _lock:
        if _cache["map"] is not None and now < _cache["until"]:
            return _cache["map"]
    out: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in DEFAULTS.items()}
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
    """Comparison key for a garment attribute (same rules as command_center/vocab.attr_key)."""
    kind = _ATTR_KINDS.get(dim)
    key = value_key(canonical(kind, value, default="") if kind else value)
    return "" if key in UNKNOWN_KEYS else key


def canonical_values(kind: str, limit: int = 60) -> List[str]:
    vals = sorted({v for v in mapping().get(kind, {}).values() if v})
    return vals[:limit]
