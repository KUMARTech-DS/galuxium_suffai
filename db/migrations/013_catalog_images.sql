-- Catalog photos for style × colour (Ads Lab ranking from the warehouse catalog) and a link from
-- brand_skus rows that came from the catalog. Idempotent. retail_product_images is used by both
-- Cloud SQL and the SQLite test schema; the brand_skus ALTER lines are Cloud SQL only (SQLite retail
-- tests skip them — brand_skus lives in the campaign database).

-- One photo per style colour. color_key is vocab.value_key of the colour (e.g. navyblue).
CREATE TABLE IF NOT EXISTS retail_product_images (
  product_id TEXT NOT NULL REFERENCES retail_products (product_id),
  brand_key TEXT NOT NULL,
  color_key TEXT NOT NULL,
  color TEXT NOT NULL,
  gcs_bucket TEXT NOT NULL,
  gcs_object_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (product_id, color_key)
);

CREATE INDEX IF NOT EXISTS idx_retail_product_images_brand
  ON retail_product_images (brand_key);

-- Catalog-sourced campaign SKUs: skip re-extraction; match to retail for stock readiness.
ALTER TABLE brand_skus ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'upload';
ALTER TABLE brand_skus ADD COLUMN IF NOT EXISTS retail_product_id TEXT;
ALTER TABLE brand_skus ADD COLUMN IF NOT EXISTS style_code TEXT;
