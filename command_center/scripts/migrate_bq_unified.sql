-- One-time migration: make suffai_ops the single source of truth for customers, orders and garment items.
-- Safe to re-run (ADD COLUMN IF NOT EXISTS, legacy tagging only touches untagged rows, view is replaced).
--
--   ./migrate_bq_unified.sh YOUR_PROJECT_ID [dataset]
-- or
--   bq query --use_legacy_sql=false < command_center/scripts/migrate_bq_unified.sql
--
-- After this, do not reload the CSVs: load_csv_bq.py refuses to truncate tables holding admin / pipeline rows.

ALTER TABLE `suffai_ops.customers`
  ADD COLUMN IF NOT EXISTS source STRING,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;

ALTER TABLE `suffai_ops.orders`
  ADD COLUMN IF NOT EXISTS source STRING,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;

ALTER TABLE `suffai_ops.commodities`
  ADD COLUMN IF NOT EXISTS confidence_score FLOAT64,
  ADD COLUMN IF NOT EXISTS item_id STRING,
  ADD COLUMN IF NOT EXISTS gcs_object_key STRING,
  ADD COLUMN IF NOT EXISTS image_status STRING,
  ADD COLUMN IF NOT EXISTS source STRING,
  ADD COLUMN IF NOT EXISTS model_version STRING,
  ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMP;

-- Rows loaded from CSV before this migration are legacy items with no photo.
UPDATE `suffai_ops.customers` SET source = 'legacy' WHERE source IS NULL;
UPDATE `suffai_ops.orders` SET source = 'legacy' WHERE source IS NULL;
UPDATE `suffai_ops.commodities`
SET source = 'legacy', image_status = 'legacy_no_photo'
WHERE source IS NULL;

CREATE TABLE IF NOT EXISTS `suffai_ops.garment_similarity_observations` (
  event_ts TIMESTAMP,
  garment_id STRING,
  user_uuid STRING,
  pin_code STRING,
  brand_name STRING,
  outcome STRING,
  gcs_object_key STRING,
  similarity_score FLOAT64
);

ALTER TABLE `suffai_ops.garment_similarity_observations`
  ADD COLUMN IF NOT EXISTS customer_id STRING,
  ADD COLUMN IF NOT EXISTS order_id STRING,
  ADD COLUMN IF NOT EXISTS item_id STRING;

-- Garment items with their similarity outcome. is_repeat = the same customer's garment photographed again,
-- so dashboards count each real garment once. Legacy rows have no observation and count until a photo
-- of the same order supersedes them (same view as migrate_bq_consistency.sql).
CREATE OR REPLACE VIEW `suffai_ops.garment_items` AS
WITH latest_obs AS (
  SELECT order_id, item_id, garment_id, outcome, similarity_score
  FROM `suffai_ops.garment_similarity_observations`
  WHERE order_id IS NOT NULL AND item_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id, item_id ORDER BY event_ts DESC) = 1
)
SELECT
  c.*,
  o.garment_id AS unique_garment_id,
  o.outcome AS similarity_outcome,
  o.similarity_score AS similarity_score,
  IFNULL(o.outcome IN ('user_match', 'user_sku_match'), FALSE) AS is_repeat
FROM `suffai_ops.commodities` AS c
LEFT JOIN latest_obs AS o
  ON o.order_id = c.order_id AND o.item_id = c.item_id
WHERE IFNULL(c.image_status, '') != 'superseded';
