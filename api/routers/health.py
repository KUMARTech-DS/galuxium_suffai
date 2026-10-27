from fastapi import APIRouter

from config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    sql = {"configured": bool(settings.DATABASE_URL or settings.CLOUD_SQL_INSTANCE)}
    try:
        from services.ops_db import health as sql_health

        sql = sql_health()
    except Exception as exc:
        sql = {**sql, "error": str(exc)}
    return {
        "status": "ok",
        "service": "suffai-api",
        "environment": settings.ENVIRONMENT,
        "data_dir": str(settings.DATA_DIR),
        "gcs_bucket": settings.GCS_BUCKET or None,
        "cloud_sql": sql,
        "cloud_sql_configured": bool(settings.DATABASE_URL or settings.CLOUD_SQL_INSTANCE),
        "bq_project": settings.BQ_PROJECT or None,
        "use_bigquery_marts": settings.USE_BIGQUERY_MARTS,
        "identity_platform_enabled": settings.IDENTITY_PLATFORM_ENABLED,
        "privacy_differencing": settings.PRIVACY_DIFFERENCING_ENABLED,
        "adv_enabled": settings.ENABLE_ADV,
    }


@router.get("/ready")
def ready():
    """Cloud Run readiness — data fixtures or GCS must be reachable."""
    ok = settings.DATA_DIR.exists() or bool(settings.GCS_BUCKET)
    return {"ready": ok, "data_dir_exists": settings.DATA_DIR.exists()}
