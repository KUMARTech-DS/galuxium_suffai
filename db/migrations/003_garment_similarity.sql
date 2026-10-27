-- Garment similarity / unique-wardrobe tables (independent of extraction garment_instances).
-- Used by Cloud Run service suffai-similarity.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS unique_garments (
  garment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  representative_gcs_bucket TEXT NOT NULL,
  representative_gcs_object_key TEXT NOT NULL,
  brand_name TEXT,
  embedding JSONB NOT NULL,
  embedding_model TEXT NOT NULL DEFAULT 'multimodalembedding@001',
  embedding_dim INT,
  first_user_uuid TEXT,
  first_pin_code VARCHAR(6),
  observation_count_total BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_unique_garments_brand
  ON unique_garments (brand_name)
  WHERE brand_name IS NOT NULL;

-- Per-user unique wardrobe (Option 2). Insert only when garment is new for that user.
CREATE TABLE IF NOT EXISTS user_wardrobe (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_uuid TEXT NOT NULL,
  garment_id UUID NOT NULL REFERENCES unique_garments(garment_id),
  first_gcs_object_key TEXT,
  pin_code_at_first_sight VARCHAR(6),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_uuid, garment_id)
);

CREATE INDEX IF NOT EXISTS idx_user_wardrobe_user
  ON user_wardrobe (user_uuid);

CREATE TABLE IF NOT EXISTS garment_pin_counts (
  garment_id UUID NOT NULL REFERENCES unique_garments(garment_id),
  pin_code VARCHAR(6) NOT NULL,
  observation_count BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (garment_id, pin_code)
);

CREATE INDEX IF NOT EXISTS idx_garment_pin_counts_pin
  ON garment_pin_counts (pin_code);

CREATE TABLE IF NOT EXISTS garment_similarity_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  gcs_bucket TEXT,
  gcs_object_key TEXT NOT NULL,
  user_uuid TEXT,
  pin_code VARCHAR(6),
  brand_name TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN (
    'user_match', 'global_match_new_user_link', 'created_new', 'skipped_missing_context', 'error'
  )),
  matched_garment_id UUID REFERENCES unique_garments(garment_id),
  similarity_score DOUBLE PRECISION,
  error_message TEXT,
  idempotency_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_garment_similarity_events_idem
  ON garment_similarity_events (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_garment_similarity_events_created
  ON garment_similarity_events (created_at DESC);
