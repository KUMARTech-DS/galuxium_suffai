from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from services.auth_service import (
    BRAND_DEMO_PASSWORD,
    issue_access_token,
    list_brand_accounts,
    verify_brand_login,
)
from services.identity_platform import verify_identity_platform_token
from config import settings
from suffai.api.envelope import err, ok

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class BrandLoginRequest(BaseModel):
    brand: str = Field(..., description="Brand account name")
    password: str


@router.get("/brands")
def brands():
    return ok({"brands": list_brand_accounts(), "demo_hint": BRAND_DEMO_PASSWORD})


@router.post("/brand/login")
def brand_login(body: BrandLoginRequest):
    user = verify_brand_login(body.brand, body.password)
    if not user:
        return err("UNAUTHORIZED", "Invalid brand credentials")
    token = issue_access_token(user)
    return ok({"access_token": token, "token_type": "bearer", "user": user})


class IdentitySessionBody(BaseModel):
    id_token: str


@router.get("/config")
def auth_config():
    return ok({
        "demo_auth_enabled": settings.DEMO_AUTH_ENABLED,
        "identity_platform_enabled": settings.IDENTITY_PLATFORM_ENABLED,
        "identity_platform_project": settings.IDENTITY_PLATFORM_PROJECT or None,
        "identity_web_api_key": settings.IDENTITY_WEB_API_KEY or None,
    })


@router.post("/identity/session")
def identity_session(body: IdentitySessionBody):
    """Exchange Identity Platform / Firebase ID token for app session claims."""
    user = verify_identity_platform_token(body.id_token)
    if not user:
        return err(
            "UNAUTHORIZED",
            "Invalid Identity Platform token (enable IDENTITY_PLATFORM_ENABLED and project)",
        )
    # Frontend may continue using the ID token as Bearer; also issue demo-compatible JWT if allowed
    app_token = None
    if settings.DEMO_AUTH_ENABLED:
        app_token = issue_access_token(user)
    return ok({
        "access_token": app_token or body.id_token,
        "token_type": "bearer",
        "user": user,
        "auth_provider": "identity_platform",
    })
