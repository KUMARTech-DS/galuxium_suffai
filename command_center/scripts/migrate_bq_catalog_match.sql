-- Add brand catalog match and outfit tag columns on commodities (safe to re-run).
-- Usage: ./migrate_bq_catalog_match.sh YOUR_PROJECT_ID [dataset]

ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS brand_sku STRING;
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS brand_style_code STRING;
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS catalog_match STRING;

-- Outfit tags from extraction (Complete your outfit). pairs_with is comma-separated.
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS garment_category STRING;
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS garment_subcategory STRING;
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS sleeve_length STRING;
ALTER TABLE `suffai_ops.commodities` ADD COLUMN IF NOT EXISTS pairs_with STRING;
