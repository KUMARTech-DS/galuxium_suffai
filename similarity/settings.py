"""Similarity service settings from env."""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


class SimilaritySettings:
    # Shared with extraction / command-center naming
    PORT = int(os.getenv("PORT", "8080"))
    PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    GCS_IMAGE_BUCKET = os.getenv("GCS_IMAGE_BUCKET") or os.getenv("GCS_BUCKET") or ""
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    ENABLE_VERTEX = _bool("ENABLE_VERTEX", True)
    VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "asia-south1")
    # Extraction uses VERTEX_MODEL (Gemini); similarity uses multimodal embeddings
    VERTEX_EMBED_MODEL = os.getenv("VERTEX_EMBED_MODEL", "multimodalembedding@001")
    EMBED_DIMENSION = int(os.getenv("EMBED_DIMENSION", "1408"))
    SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.92"))
    GLOBAL_CANDIDATE_LIMIT = int(os.getenv("GLOBAL_CANDIDATE_LIMIT", "500"))
    MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))
    # compare-pins (brand SKU vs the PIN garments Command Center sends; read-only). Command Center
    # owns the per-PIN limit (COMPARE_PIN_CANDIDATE_LIMIT there); this only bounds a request.
    COMPARE_MAX_CANDIDATES = int(os.getenv("COMPARE_MAX_CANDIDATES", "100000"))
    COMPARE_CACHE_SEC = int(os.getenv("COMPARE_CACHE_SEC", "300"))
    COMPARE_EMBED_CACHE_MAX = int(os.getenv("COMPARE_EMBED_CACHE_MAX", "20000"))
    COMPARE_MAX_PINS = int(os.getenv("COMPARE_MAX_PINS", "50"))
    # Same BQ flags as command_center
    USE_BQ = _bool("USE_BQ", True)
    BQ_DATASET = os.getenv("BQ_DATASET", "suffai_ops")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")


settings = SimilaritySettings()
