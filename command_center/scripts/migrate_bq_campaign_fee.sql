-- Append-only aggregate-only audit trail for 1P platform fee recommendations.
-- Usage: ./migrate_bq_campaign_fee.sh YOUR_PROJECT_ID [dataset]

CREATE TABLE IF NOT EXISTS `suffai_ops.campaign_fee_recommendations` (
  event_id STRING NOT NULL,
  campaign_id STRING NOT NULL,
  brand_name STRING,
  event_type STRING NOT NULL,
  model_version STRING NOT NULL,
  source STRING NOT NULL,
  recommended_fee_inr FLOAT64 NOT NULL,
  final_fee_inr FLOAT64,
  media_budget_inr FLOAT64,
  total_checkout_inr FLOAT64,
  audience_count INT64,
  sku_count INT64,
  pin_count INT64,
  comparisons INT64,
  process_seconds FLOAT64,
  channels STRING,
  objective STRING,
  duration_days INT64,
  features_json STRING,
  actor_role STRING,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS `suffai_ops.campaign_fee_features` (
  campaign_id STRING NOT NULL,
  as_of_at TIMESTAMP NOT NULL,
  audience_count INT64,
  sku_count INT64,
  pin_count INT64,
  comparisons INT64,
  process_seconds FLOAT64,
  channels STRING,
  objective STRING,
  duration_days INT64,
  media_budget_inr FLOAT64,
  recommended_fee_inr FLOAT64,
  final_fee_inr FLOAT64,
  fee_accepted BOOL,
  model_version STRING
);
