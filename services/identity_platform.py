"""Google Identity Platform / Firebase ID token verification (GCP-native AuthN)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Dict, Optional

import jwt
import urllib.request

from config import settings

log = logging.getLogger("suffai.identity")

_GOOGLE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)


@lru_cache(maxsize=1)
def _load_tenant_map() -> Dict[str, Any]:
    try:
        return json.loads(settings.IDENTITY_TENANT_MAP_JSON or "{}")
    except json.JSONDecodeError:
        return {}


@lru_cache(maxsize=2)
def _fetch_certs() -> Dict[str, str]:
    with urllib.request.urlopen(_GOOGLE_CERTS_URL, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _map_claims_to_tenant(email: str, firebase_claims: Dict[str, Any]) -> Dict[str, Any]:
    custom = firebase_claims.get("suffai") or {}
    if isinstance(custom, dict) and custom.get("brand"):
        brand = custom["brand"]
        safe = brand.lower().replace(" ", "").replace("'", "").replace("&", "and")
        return {
            "brand": brand,
            "brand_id": custom.get("brand_id") or safe,
            "tenant_id": custom.get("tenant_id") or f"tenant-{safe}",
            "role": custom.get("role") or "brand_analyst",
            "email": email,
            "sub": firebase_claims.get("user_id") or firebase_claims.get("sub") or email,
            "auth_provider": "identity_platform",
        }

    mapping = _load_tenant_map()
    domain = "@" + email.split("@")[-1] if "@" in email else ""
    entry = mapping.get(email) or mapping.get(domain) or {}
    if entry:
        brand = entry.get("brand") or settings.BRANDS[0]
        safe = brand.lower().replace(" ", "").replace("'", "").replace("&", "and")
        return {
            "brand": brand,
            "brand_id": entry.get("brand_id") or safe,
            "tenant_id": entry.get("tenant_id") or f"tenant-{safe}",
            "role": entry.get("role") or "brand_analyst",
            "email": email,
            "sub": firebase_claims.get("user_id") or email,
            "auth_provider": "identity_platform",
        }

    brand = settings.BRANDS[0]
    safe = brand.lower().replace(" ", "").replace("'", "").replace("&", "and")
    return {
        "brand": brand,
        "brand_id": safe,
        "tenant_id": f"tenant-{safe}",
        "role": "brand_executive",
        "email": email,
        "sub": firebase_claims.get("user_id") or email,
        "auth_provider": "identity_platform",
        "warning": "No tenant map entry; defaulted to read-only executive",
    }


def verify_identity_platform_token(token: str) -> Optional[Dict[str, Any]]:
    if not settings.IDENTITY_PLATFORM_ENABLED:
        return None
    project = settings.IDENTITY_PLATFORM_PROJECT
    if not project:
        return None

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        certs = _fetch_certs()
        if kid not in certs:
            _fetch_certs.cache_clear()
            certs = _fetch_certs()
        if kid not in certs:
            return None

        pem = certs[kid]
        claims = jwt.decode(
            token,
            pem,
            algorithms=["RS256"],
            audience=project,
            issuer=f"https://securetoken.google.com/{project}",
        )
        email = claims.get("email") or claims.get("user_id") or claims.get("sub")
        if not email:
            return None
        return _map_claims_to_tenant(str(email), claims)
    except Exception as exc:
        log.info("Identity Platform verify failed: %s", exc)
        return None
