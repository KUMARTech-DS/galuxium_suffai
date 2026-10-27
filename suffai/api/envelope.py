from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from suffai import METRIC_VERSION


def ok(data: Any, *, coverage: dict | None = None, as_of: str | None = None) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "request_id": str(uuid.uuid4()),
            "as_of": as_of or datetime.now(timezone.utc).isoformat(),
            "source_watermark": None,
            "metric_version": METRIC_VERSION,
            "coverage": coverage or {},
        },
        "errors": [],
    }


def err(code: str, message: str, *, data: Any = None) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "request_id": str(uuid.uuid4()),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "source_watermark": None,
            "metric_version": METRIC_VERSION,
            "coverage": {},
        },
        "errors": [{"code": code, "message": message}],
    }
