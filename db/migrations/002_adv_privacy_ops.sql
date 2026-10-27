-- ADV-01 activation, privacy query log, JSON ops mirror (Cloud SQL)

CREATE TABLE IF NOT EXISTS conversion_events (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT,
  brand TEXT,
  campaign_id TEXT NOT NULL,
  segment_key TEXT,
  converted BOOLEAN NOT NULL DEFAULT TRUE,
  revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
  channel TEXT,
  observed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload JSONB
);

CREATE TABLE IF NOT EXISTS inventory_levels (
  inventory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  warehouse TEXT NOT NULL DEFAULT 'default',
  on_hand INT NOT NULL DEFAULT 0,
  as_of TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (brand_id, sku, warehouse)
);

CREATE TABLE IF NOT EXISTS media_activation_log (
  request_id UUID PRIMARY KEY,
  tenant_id TEXT,
  brand TEXT,
  campaign_id TEXT,
  channel TEXT,
  segment_keys JSONB,
  payload JSONB,
  delivery JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS privacy_query_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fingerprint TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  filters JSONB,
  cohort_size INT,
  cohort_hash TEXT,
  customer_ids JSONB,
  differencing_risk BOOLEAN DEFAULT FALSE,
  risk_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Flexible mirror for JSON GarmentStore collections when relational FKs are not yet mapped
CREATE TABLE IF NOT EXISTS ops_json_documents (
  collection TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (collection, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_conversion_campaign ON conversion_events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_privacy_tenant ON privacy_query_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_json_collection ON ops_json_documents(collection);
