"""Normalize Mongo-style $oid / $date / scalar IDs. Never invent replacements for source IDs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def extract_id(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, dict) and "$oid" in val:
        return str(val["$oid"]).strip() or None
    if isinstance(val, float) and val != val:  # NaN
        return None
    s = str(val).strip()
    return s or None


def extract_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, dict) and "$date" in val:
        d_val = val["$date"]
        if isinstance(d_val, dict) and "$numberLong" in d_val:
            ms = int(d_val["$numberLong"])
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return _parse_iso(str(d_val))
    return _parse_iso(str(val))


def _parse_iso(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_pin(val: Any) -> str | None:
    """Return 6-digit PIN if present; never invent a fallback."""
    if val is None:
        return None
    import re

    m = re.search(r"\b(\d{6})\b", str(val))
    return m.group(1) if m else None
