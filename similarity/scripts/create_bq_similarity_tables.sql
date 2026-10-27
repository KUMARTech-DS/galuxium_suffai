-- Analytics tables for garment similarity (same dataset pattern as command center).
-- Cloud SQL keeps the embeddings index used by SKU ranking; BQ links each observation
-- to the customer / order / item from the photo name for Admin and the dashboards.
--
--   bq query --use_legacy_sql=false < similarity/scripts/create_bq_similarity_tables.sql
-- Or set PROJECT/DATASET and run via bq mk.

CREATE TABLE IF NOT EXISTS `suffai_ops.garment_similarity_observations` (
  event_ts TIMESTAMP,
  garment_id STRING,
  user_uuid STRING,
  pin_code STRING,
  brand_name STRING,
  outcome STRING,
  gcs_object_key STRING,
  similarity_score FLOAT64,
  customer_id STRING,
  order_id STRING,
  item_id STRING
);
