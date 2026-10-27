-- Analytical star schema (Blueprint §4). Partition by date where applicable.

CREATE TABLE IF NOT EXISTS suffai_staging.dim_geo (
  geo_key STRING NOT NULL,
  pin_code STRING,
  city STRING,
  area_label STRING
);

CREATE TABLE IF NOT EXISTS suffai_staging.dim_customer (
  customer_key STRING NOT NULL,
  customer_id STRING NOT NULL,
  user_id STRING,
  age_band STRING,
  gender_optional STRING
);

CREATE TABLE IF NOT EXISTS suffai_staging.dim_store (
  store_key STRING NOT NULL,
  store_id STRING NOT NULL,
  service_tier STRING,
  premium_laundry BOOL
);

CREATE TABLE IF NOT EXISTS suffai_staging.dim_service (
  service_key STRING NOT NULL,
  service_id STRING NOT NULL,
  name STRING
);

CREATE TABLE IF NOT EXISTS suffai_staging.dim_commodity (
  commodity_key STRING NOT NULL,
  commodity_id STRING NOT NULL,
  normalized_type STRING,
  catalog_name STRING
);

CREATE TABLE IF NOT EXISTS suffai_staging.dim_fashion_brand (
  brand_key STRING NOT NULL,
  brand_id STRING NOT NULL,
  canonical_name STRING
);

CREATE TABLE IF NOT EXISTS suffai_staging.fact_order (
  order_id STRING NOT NULL,
  customer_key STRING,
  store_key STRING,
  geo_key STRING,
  order_date DATE,
  net_paid NUMERIC,
  order_status STRING,
  payment_status STRING,
  eligible_default BOOL,
  source_watermark TIMESTAMP
)
PARTITION BY order_date;

CREATE TABLE IF NOT EXISTS suffai_staging.fact_order_commodity (
  line_id STRING NOT NULL,
  order_id STRING NOT NULL,
  service_key STRING,
  commodity_key STRING,
  quantity INT64,
  line_price NUMERIC,
  order_date DATE
)
PARTITION BY order_date;

-- One actually observed physical item (photo/tag evidence). Never fabricated from qty.
CREATE TABLE IF NOT EXISTS suffai_staging.fact_garment_observation (
  garment_id STRING NOT NULL,
  customer_key STRING,
  brand_key STRING,
  geo_key STRING,
  observed_date DATE,
  material STRING,
  color STRING,
  size STRING,
  review_status STRING,
  consent_policy_version STRING
)
PARTITION BY observed_date;

CREATE TABLE IF NOT EXISTS suffai_staging.fact_customer_segment_snapshot (
  snapshot_key STRING NOT NULL,
  customer_key STRING NOT NULL,
  as_of_date DATE NOT NULL,
  recency_days INT64,
  frequency INT64,
  monetary NUMERIC,
  rfm_segment STRING,
  rule_version STRING
)
PARTITION BY as_of_date;

CREATE TABLE IF NOT EXISTS suffai_marts.mart_geo_trends (
  aggregate_key STRING NOT NULL,
  geo_key STRING,
  period_start DATE,
  metric_key STRING,
  metric_value FLOAT64,
  cohort_size INT64,
  suppressed BOOL,
  metric_version STRING,
  source_watermark TIMESTAMP,
  taxonomy_version STRING
)
PARTITION BY period_start;

-- Written by jobs/bq_sync.py. Quantities are service observations, not garment purchases.
CREATE TABLE IF NOT EXISTS suffai_marts.mart_commodity_popularity (
  commodity_name STRING NOT NULL,
  quantity INT64,
  line_count INT64,
  distinct_customers INT64,
  suppressed BOOL,
  metric_key STRING,
  metric_version STRING,
  period_start DATE,
  source_watermark TIMESTAMP
)
PARTITION BY period_start;

-- Written by jobs/bq_sync.py; read by suffai/warehouse/bq_reader.py.
CREATE TABLE IF NOT EXISTS suffai_marts.mart_rfm_distribution (
  rfm_segment STRING NOT NULL,
  customers INT64,
  share_pct FLOAT64,
  rule_version STRING,
  period_start DATE,
  source_watermark TIMESTAMP
)
PARTITION BY period_start;

CREATE TABLE IF NOT EXISTS suffai_marts.mart_brand_benchmarks (
  aggregate_key STRING NOT NULL,
  brand_key STRING,
  period_start DATE,
  cohort_key STRING,
  metric_value FLOAT64,
  cohort_size INT64,
  suppressed BOOL,
  metric_version STRING,
  source_watermark TIMESTAMP
)
PARTITION BY period_start;

-- One row appended per jobs/bq_sync.py run.
CREATE TABLE IF NOT EXISTS suffai_raw.ingest_runs (
  run_at TIMESTAMP NOT NULL,
  orders INT64,
  lines INT64,
  rfm INT64
);
