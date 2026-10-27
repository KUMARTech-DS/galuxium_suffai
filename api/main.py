"""
Suffai API — Cloud Run BFF.

  Local:  uvicorn api.main:app --host 0.0.0.0 --port 8080
  Cloud:  Dockerfile.api → Cloud Run service suffai-api
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import auth, brands, chat, geo, health, integrity, segments, garment_images, garments, campaigns, research, exports, activation 
from config import settings

log = logging.getLogger("suffai.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.DATABASE_URL:
        try:
            from services.ops_db import ensure_adv_schema

            ok = ensure_adv_schema()
            log.info("Cloud SQL ADV/ops schema ensure=%s", ok)
        except Exception as exc:
            log.warning("Cloud SQL schema bootstrap skipped: %s", exc)
    yield


app = FastAPI(
    title="Suffai Fashion Intelligence API",
    version=settings.APP_VERSION,
    description=(
        "Governed B2B analytics on GCP. Laundry service observations are not "
        "garment purchases. Brand share requires verified consented garments."
    ),
    lifespan=lifespan,
)

origins = [o.strip() for o in settings.WEB_ORIGIN.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(integrity.router)
app.include_router(segments.router)
app.include_router(geo.router)
app.include_router(brands.router)
app.include_router(garments.router)
app.include_router(campaigns.router)
app.include_router(research.router)
app.include_router(exports.router)
app.include_router(activation.router)
app.include_router(chat.router)
app.include_router(garment_images.router)


@app.get("/")
def root():
    return {
        "service": "suffai-api",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "docs": "/docs",
        "gcp_project": settings.GCP_PROJECT or None,
        "region": settings.GCP_REGION,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", settings.PORT)),
        reload=settings.ENVIRONMENT == "local",
    )
