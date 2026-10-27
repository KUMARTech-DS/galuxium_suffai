-- Demo marketing KPIs per brand campaign (synthetic until real analytics exist).

CREATE TABLE IF NOT EXISTS campaign_impact_metrics (
  campaign_id UUID PRIMARY KEY REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  reach INT NOT NULL DEFAULT 0,
  impressions INT NOT NULL DEFAULT 0,
  ctr DOUBLE PRECISION NOT NULL DEFAULT 0,
  conversions INT NOT NULL DEFAULT 0,
  revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
  lift DOUBLE PRECISION NOT NULL DEFAULT 0,
  roas DOUBLE PRECISION NOT NULL DEFAULT 0,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source TEXT NOT NULL DEFAULT 'synthetic'
);

CREATE INDEX IF NOT EXISTS idx_campaign_impact_generated
  ON campaign_impact_metrics (generated_at DESC);
