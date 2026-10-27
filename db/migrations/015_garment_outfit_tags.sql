-- Outfit tags from extraction (Complete your outfit) and SKU-first wardrobe dedup.

ALTER TABLE garment_instances
  ADD COLUMN IF NOT EXISTS garment_category TEXT,
  ADD COLUMN IF NOT EXISTS garment_subcategory TEXT,
  ADD COLUMN IF NOT EXISTS sleeve_length TEXT,
  ADD COLUMN IF NOT EXISTS pairs_with TEXT[];

ALTER TABLE brand_skus
  ADD COLUMN IF NOT EXISTS garment_category TEXT,
  ADD COLUMN IF NOT EXISTS garment_subcategory TEXT,
  ADD COLUMN IF NOT EXISTS sleeve_length TEXT,
  ADD COLUMN IF NOT EXISTS pairs_with TEXT[];

ALTER TABLE unique_garments
  ADD COLUMN IF NOT EXISTS brand_sku TEXT;

CREATE INDEX IF NOT EXISTS idx_unique_garments_brand_sku
  ON unique_garments (brand_sku)
  WHERE brand_sku IS NOT NULL;

ALTER TABLE garment_similarity_events
  DROP CONSTRAINT IF EXISTS garment_similarity_events_outcome_check;
ALTER TABLE garment_similarity_events
  ADD CONSTRAINT garment_similarity_events_outcome_check
  CHECK (outcome IN (
    'user_match', 'global_match_new_user_link', 'created_new', 'skipped_missing_context', 'error',
    'user_sku_match', 'global_sku_match'
  ));
