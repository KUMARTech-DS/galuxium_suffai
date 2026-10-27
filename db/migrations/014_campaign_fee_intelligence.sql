-- 1P platform-fee recommendations. Media budget remains in brand_campaigns.budget_inr.
-- The fee is separately auditable and is never inferred from synthetic impact metrics.

ALTER TABLE brand_campaigns
  ADD COLUMN IF NOT EXISTS platform_fee_recommended NUMERIC,
  ADD COLUMN IF NOT EXISTS platform_fee_final NUMERIC,
  ADD COLUMN IF NOT EXISTS fee_model_version TEXT,
  ADD COLUMN IF NOT EXISTS fee_features JSONB,
  ADD COLUMN IF NOT EXISTS fee_recommended_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS fee_accepted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS campaign_fee_decisions (
  decision_id UUID PRIMARY KEY,
  campaign_id UUID NOT NULL REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  brand_name TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('recommended', 'configured', 'launched', 'override')),
  model_version TEXT NOT NULL,
  source TEXT NOT NULL,
  recommended_fee_inr NUMERIC NOT NULL,
  final_fee_inr NUMERIC,
  media_budget_inr NUMERIC,
  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  actor_role TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_fee_decisions_campaign_created
  ON campaign_fee_decisions (campaign_id, created_at DESC);
