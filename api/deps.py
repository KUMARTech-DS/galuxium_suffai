"""Shared data bootstrap for Cloud Run API."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException

from config import settings
from services.auth_service import decode_access_token
from suffai.garments.store import GarmentStore
from suffai.ingest import load_source_collections, normalize_all
from suffai.metrics.rfm import compute_rfm_snapshots


@lru_cache(maxsize=1)
def get_normalized() -> Dict[str, Any]:
    raw = load_source_collections(settings.DATA_DIR)
    return normalize_all(raw)


@lru_cache(maxsize=1)
def get_rfm():
    norm = get_normalized()
    return compute_rfm_snapshots(norm["orders"], norm["stores"])


def get_garment_store() -> GarmentStore:
    return GarmentStore(settings.DERIVED_DIR)


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        if settings.DEMO_AUTH_ENABLED and settings.ENVIRONMENT == "local":
            # Local anonymous read for health/dev tools; protected routes still prefer token
            return {
                "brand": "Peter England",
                "brand_id": "peterengland",
                "tenant_id": "tenant-peterengland",
                "role": "brand_analyst",
                "sub": "dev@local",
            }
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return claims


def require_user(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return user
