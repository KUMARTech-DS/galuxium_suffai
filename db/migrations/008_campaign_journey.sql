-- End-to-end brand campaign journey: config, simulated launch, daily simulated KPIs.

ALTER TABLE brand_campaigns DROP CONSTRAINT IF EXISTS brand_campaigns_status_check;
ALTER TABLE brand_campaigns
  ADD CONSTRAINT brand_campaigns_status_check
  CHECK (status IN ('draft', 'ranked', 'error', 'started', 'ignored', 'configured', 'launched'));

ALTER TABLE brand_campaigns
  ADD COLUMN IF NOT EXISTS campaign_name TEXT,
  ADD COLUMN IF NOT EXISTS objective TEXT,
  ADD COLUMN IF NOT EXISTS budget_inr NUMERIC,
  ADD COLUMN IF NOT EXISTS duration_days INT,
  ADD COLUMN IF NOT EXISTS start_date DATE,
  ADD COLUMN IF NOT EXISTS end_date DATE,
  ADD COLUMN IF NOT EXISTS selected_sku_ids UUID[],
  ADD COLUMN IF NOT EXISTS creative JSONB,
  ADD COLUMN IF NOT EXISTS source_context JSONB,
  ADD COLUMN IF NOT EXISTS est_audience INT,
  ADD COLUMN IF NOT EXISTS launched_at TIMESTAMPTZ;

ALTER TABLE brand_campaigns DROP CONSTRAINT IF EXISTS brand_campaigns_objective_check;
ALTER TABLE brand_campaigns
  ADD CONSTRAINT brand_campaigns_objective_check
  CHECK (objective IS NULL OR objective IN ('awareness', 'consideration', 'conversion'));

ALTER TABLE campaign_impact_metrics
  ADD COLUMN IF NOT EXISTS spend DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS clicks INT;

CREATE TABLE IF NOT EXISTS campaign_impact_daily (
  campaign_id UUID NOT NULL REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  day DATE NOT NULL,
  impressions INT NOT NULL DEFAULT 0,
  reach INT NOT NULL DEFAULT 0,
  clicks INT NOT NULL DEFAULT 0,
  conversions INT NOT NULL DEFAULT 0,
  revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
  spend DOUBLE PRECISION NOT NULL DEFAULT 0,
  PRIMARY KEY (campaign_id, day)
);

CREATE INDEX IF NOT EXISTS idx_brand_campaigns_launched
  ON brand_campaigns (launched_at DESC);
