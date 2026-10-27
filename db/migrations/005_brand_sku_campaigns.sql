-- Brand campaign SKUs for Ads Lab regional ranking (Part 1).
-- Independent of user_wardrobe ingestion; reused later for wardrobe→SKU recs (Part 2).

CREATE TABLE IF NOT EXISTS brand_campaigns (
  campaign_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_name TEXT NOT NULL,
  pin_codes TEXT[] NOT NULL,
  age_group TEXT,
  gender TEXT,
  rfm_segment TEXT,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'ranked', 'error')),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_brand_campaigns_brand
  ON brand_campaigns (brand_name);

CREATE INDEX IF NOT EXISTS idx_brand_campaigns_created
  ON brand_campaigns (created_at DESC);

CREATE TABLE IF NOT EXISTS brand_skus (
  sku_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id UUID NOT NULL REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  brand_name TEXT NOT NULL,
  original_filename TEXT,
  gcs_bucket TEXT NOT NULL,
  gcs_object_key TEXT NOT NULL,
  color TEXT,
  pattern TEXT,
  material TEXT,
  size TEXT,
  commodity TEXT,
  sleeve_type TEXT,
  collar_type TEXT,
  confidence TEXT,
  raw_result JSONB,
  embedding JSONB,
  embedding_model TEXT,
  embedding_dim INT,
  extract_ok BOOLEAN NOT NULL DEFAULT false,
  extract_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_brand_skus_campaign
  ON brand_skus (campaign_id);

CREATE INDEX IF NOT EXISTS idx_brand_skus_brand
  ON brand_skus (brand_name);

CREATE TABLE IF NOT EXISTS brand_sku_pin_scores (
  sku_uuid UUID NOT NULL REFERENCES brand_skus(sku_uuid) ON DELETE CASCADE,
  pin_code VARCHAR(6) NOT NULL,
  fit_score DOUBLE PRECISION,
  top_matches JSONB,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (sku_uuid, pin_code)
);

CREATE INDEX IF NOT EXISTS idx_brand_sku_pin_scores_pin
  ON brand_sku_pin_scores (pin_code);
