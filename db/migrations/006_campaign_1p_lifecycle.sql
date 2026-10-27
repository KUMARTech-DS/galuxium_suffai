-- 1P campaign lifecycle: Start / Ignore + Style Assistant precomputed recs.

-- Widen status check for started / ignored
ALTER TABLE brand_campaigns DROP CONSTRAINT IF EXISTS brand_campaigns_status_check;
ALTER TABLE brand_campaigns
  ADD CONSTRAINT brand_campaigns_status_check
  CHECK (status IN ('draft', 'ranked', 'error', 'started', 'ignored'));

ALTER TABLE brand_campaigns
  ADD COLUMN IF NOT EXISTS title TEXT,
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS ignored_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS eligible_users INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS recommendations_computed INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS style_feed_serves INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS campaign_user_recommendations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id UUID NOT NULL REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  user_uuid TEXT NOT NULL,
  brand_name TEXT NOT NULL,
  fit_score DOUBLE PRECISION,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, user_uuid)
);

CREATE INDEX IF NOT EXISTS idx_campaign_user_recs_user
  ON campaign_user_recommendations (user_uuid);

CREATE INDEX IF NOT EXISTS idx_campaign_user_recs_campaign
  ON campaign_user_recommendations (campaign_id);

CREATE INDEX IF NOT EXISTS idx_brand_campaigns_status
  ON brand_campaigns (status);
