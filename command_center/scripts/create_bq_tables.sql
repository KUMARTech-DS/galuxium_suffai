-- Explicit BQ tables for the command-center dashboard (suffai_ops).
-- Does not modify existing suffai_staging / suffai_marts star schema.
-- Run via: bq query --use_legacy_sql=false < scripts/create_bq_tables.sql
-- Or load_to_bq.sh (creates dataset + loads CSVs).
--
-- suffai_ops is the source of truth for customers, orders and garment items:
--   source = legacy (CSV load) | admin (created on the Admin tab) | pipeline (photo extraction)
-- An order without photos has no commodities rows; total_number_of_clothes is the expected count.
-- Existing datasets: run migrate_bq_unified.sql once to add the newer columns and the garment_items view.

CREATE TABLE IF NOT EXISTS suffai_ops.customers (
  _id STRING,
  name STRING,
  number STRING,
  address STRING,
  age INT64,
  gender STRING,
  pin_code STRING,
  rfm_segment STRING,
  lat FLOAT64,
  lon FLOAT64,
  source STRING,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS suffai_ops.orders (
  _id STRING,
  customer_id STRING,
  scheduled_date DATE,
  service STRING,
  total_number_of_clothes INT64,
  amount FLOAT64,
  source STRING,
  created_at TIMESTAMP
);

-- One row per garment item. Photo items are keyed by (order_id, item_id) from
-- garments/{customer_id}_{order_id}_{item_id}.jpg and written only by suffai-extraction.
CREATE TABLE IF NOT EXISTS suffai_ops.commodities (
  customer_id STRING,
  order_id STRING,
  commodity_id STRING,
  name STRING,
  color STRING,
  brand STRING,
  pattern STRING,
  size STRING,
  confidence_score FLOAT64,
  reason STRING,
  sleeve_type STRING,
  collar_type STRING,
  gender STRING,
  material STRING,
  item_id STRING,
  gcs_object_key STRING,
  image_status STRING,
  source STRING,
  model_version STRING,
  extracted_at TIMESTAMP,
  brand_sku STRING,
  brand_style_code STRING,
  catalog_match STRING,
  garment_category STRING,
  garment_subcategory STRING,
  sleeve_length STRING,
  pairs_with STRING
);

-- PIN area name, population and centroid from Gemini + Google Search (pin_areas.py); status ok / not_found / out_of_city.
-- Buffers are density x area of a 1 / 2 / 4 km circle. sources is a JSON list of title + url.
CREATE TABLE IF NOT EXISTS suffai_ops.pin_areas (
  pin_code STRING NOT NULL,
  city STRING,
  area_label STRING,
  population_pin INT64,
  population_year INT64,
  area_sq_km FLOAT64,
  pop_buffer_1km INT64,
  pop_buffer_2km INT64,
  pop_buffer_4km INT64,
  centroid_lat FLOAT64,
  centroid_lon FLOAT64,
  status STRING,
  detail STRING,
  sources STRING,
  search_entry_html STRING,
  model STRING,
  fetched_at TIMESTAMP
);

-- Canonical value per kind and alias_key: lower case, letters and digits only.
-- Kinds: brand, color, garment_name, customer_gender, garment_gender. Seeded by migrate_bq_consistency.sql.
CREATE TABLE IF NOT EXISTS suffai_ops.ref_values (
  kind STRING NOT NULL,
  canonical STRING NOT NULL,
  alias_key STRING NOT NULL
);

-- Role-based Command Center login (load via login_cred_db.csv). location_code = the warehouse / store
-- of a warehouse_staff or store_staff login (migrate_bq_retail.sql adds it to older tables).
CREATE TABLE IF NOT EXISTS suffai_ops.login_cred_db (
  username STRING NOT NULL,
  password_hash STRING NOT NULL,
  role STRING NOT NULL,
  brand_name STRING,
  customer_id STRING,
  display_name STRING,
  active BOOL,
  created_at TIMESTAMP,
  location_code STRING
);
