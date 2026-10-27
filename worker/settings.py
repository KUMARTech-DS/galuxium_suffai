"""Worker-only settings from env (does not change shared API config module)."""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


class WorkerSettings:
    PORT = int(os.getenv("PORT", "8080"))
    PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    GCS_IMAGE_BUCKET = os.getenv("GCS_IMAGE_BUCKET") or os.getenv("GCS_BUCKET") or ""
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    ENABLE_VERTEX = _bool("ENABLE_VERTEX", False)
    VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "asia-south1")
    VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.0-flash")
    # Auto-complete when min field confidence >= this; else NEEDS_REVIEW
    AUTO_ACCEPT_CONFIDENCE = float(os.getenv("AUTO_ACCEPT_CONFIDENCE", "0.90"))
    MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))
    # Customer photos ({customer_id}_{order_id}_{item_id}.jpg) are written to BigQuery suffai_ops.commodities
    USE_BQ = _bool("USE_BQ", True)
    BQ_DATASET = os.getenv("BQ_DATASET", "suffai_ops")
    # Similarity subscribes here, so wardrobe dedup starts only after extraction is saved
    PUBSUB_EXTRACTED_TOPIC = os.getenv("PUBSUB_EXTRACTED_TOPIC", "garment-image-extracted")
    # Visual warehouse match; model and dimension must equal suffai-similarity's
    VERTEX_EMBED_MODEL = os.getenv("VERTEX_EMBED_MODEL", "multimodalembedding@001")
    EMBED_DIMENSION = int(os.getenv("EMBED_DIMENSION", "1408"))
    CATALOG_MATCH_THRESHOLD = float(os.getenv("CATALOG_MATCH_THRESHOLD", "0.92"))
    # Catalog photos fingerprinted per customer photo when a brand's catalog has no fingerprints yet
    CATALOG_EMBED_PER_REQUEST = int(os.getenv("CATALOG_EMBED_PER_REQUEST", "40"))


settings = WorkerSettings()
