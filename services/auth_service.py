"""Brand tenant auth — demo JWT until Identity Platform is wired."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt

from config import settings

BRAND_DEMO_PASSWORD = "brand123"
_ALLOWED_DEMO = {BRAND_DEMO_PASSWORD, "demo", "suffai"}


def list_brand_accounts() -> List[str]:
    return list(settings.BRANDS)


def verify_brand_login(brand_name: str, password: str) -> Optional[Dict[str, Any]]:
    if not settings.DEMO_AUTH_ENABLED:
        return None
    if brand_name not in settings.BRANDS:
        return None
    if password.strip().lower() not in {p.lower() for p in _ALLOWED_DEMO}:
        return None
    safe = brand_name.lower().replace(" ", "").replace("'", "").replace("&", "and")
    return {
        "brand": brand_name,
        "brand_id": safe,
        "email": f"{safe}@brand.suffai.demo",
        "role": "brand_analyst",
        "tenant_id": f"tenant-{safe}",
    }


def issue_access_token(user: Dict[str, Any], *, hours: int = 12) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["email"],
        "brand": user["brand"],
        "brand_id": user["brand_id"],
        "tenant_id": user["tenant_id"],
        "role": user["role"],
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=hours)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    # Prefer Identity Platform when enabled
    from services.identity_platform import verify_identity_platform_token

    ip_user = verify_identity_platform_token(token)
    if ip_user:
        return ip_user

    if not settings.DEMO_AUTH_ENABLED:
        return None
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            issuer=settings.JWT_ISSUER,
        )
    except jwt.PyJWTError:
        return None
