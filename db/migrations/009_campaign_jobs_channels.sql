-- Async extract & rank jobs, 1P / 3P run mode, channels (in-app / WhatsApp / SMS), budget recommendation.

ALTER TABLE brand_campaigns DROP CONSTRAINT IF EXISTS brand_campaigns_status_check;
ALTER TABLE brand_campaigns
  ADD CONSTRAINT brand_campaigns_status_check
  CHECK (status IN ('draft', 'processing', 'ranked', 'error', 'started', 'ignored',
                    'configured', 'launched', 'exported'));

ALTER TABLE brand_campaigns
  ADD COLUMN IF NOT EXISTS run_mode TEXT,
  ADD COLUMN IF NOT EXISTS channels TEXT[],
  ADD COLUMN IF NOT EXISTS message_template JSONB,
  ADD COLUMN IF NOT EXISTS budget_recommended NUMERIC;

ALTER TABLE brand_campaigns DROP CONSTRAINT IF EXISTS brand_campaigns_run_mode_check;
ALTER TABLE brand_campaigns
  ADD CONSTRAINT brand_campaigns_run_mode_check
  CHECK (run_mode IS NULL OR run_mode IN ('1p', '3p'));

-- Per-SKU processing state + persisted rank components (set by the job worker).
ALTER TABLE brand_skus
  ADD COLUMN IF NOT EXISTS process_status TEXT NOT NULL DEFAULT 'done',
  ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS process_seconds DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS rank_score DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS sim_fit DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS attr_fit DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS freq_coverage DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS compare_error TEXT;

ALTER TABLE brand_skus DROP CONSTRAINT IF EXISTS brand_skus_process_status_check;
ALTER TABLE brand_skus
  ADD CONSTRAINT brand_skus_process_status_check
  CHECK (process_status IN ('pending', 'done', 'failed'));

CREATE INDEX IF NOT EXISTS idx_brand_skus_pending
  ON brand_skus (campaign_id) WHERE process_status = 'pending';

CREATE TABLE IF NOT EXISTS campaign_jobs (
  job_id UUID PRIMARY KEY,
  campaign_id UUID NOT NULL REFERENCES brand_campaigns(campaign_id) ON DELETE CASCADE,
  brand_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  total_images INT NOT NULL DEFAULT 0,
  processed_images INT NOT NULL DEFAULT 0,
  failed_images INT NOT NULL DEFAULT 0,
  total_customers INT NOT NULL DEFAULT 0,
  total_garments INT NOT NULL DEFAULT 0,
  comparisons BIGINT NOT NULL DEFAULT 0,
  est_seconds INT NOT NULL DEFAULT 0,
  error_code TEXT,
  lease_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_jobs_brand_created
  ON campaign_jobs (brand_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_campaign_jobs_campaign
  ON campaign_jobs (campaign_id);

ALTER TABLE campaign_impact_metrics
  ADD COLUMN IF NOT EXISTS channel_breakdown JSONB;

-- Daily simulated series per channel ('all' rows keep older single-channel launches valid).
ALTER TABLE campaign_impact_daily
  ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'all';
ALTER TABLE campaign_impact_daily DROP CONSTRAINT IF EXISTS campaign_impact_daily_pkey;
ALTER TABLE campaign_impact_daily
  ADD CONSTRAINT campaign_impact_daily_pkey PRIMARY KEY (campaign_id, day, channel);
