-- Suffai operational schema (Cloud SQL PostgreSQL)
-- Source Mongo IDs retained as TEXT. New records use UUID PKs.
-- Do NOT create garment_instances from order-line quantity without physical evidence.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS tenants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tenant_memberships (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN (
    'suffai_admin', 'brand_analyst', 'brand_pm', 'brand_marketer', 'brand_executive'
  )),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS fashion_brands (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS brand_catalog_skus (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id UUID NOT NULL REFERENCES fashion_brands(id),
  brand_sku TEXT NOT NULL,
  category TEXT,
  material TEXT,
  color TEXT,
  size TEXT,
  price_band TEXT,
  availability TEXT,
  launch_date DATE,
  image_object_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  UNIQUE (brand_id, brand_sku)
);

CREATE TABLE IF NOT EXISTS consent_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_customer_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK (purpose IN (
    'operational_processing', 'aggregated_b2b_analytics', 'campaign_targeting'
  )),
  policy_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('granted', 'revoked')),
  granted_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS garment_instances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_customer_id TEXT NOT NULL,
  source_user_id TEXT,
  source_order_id TEXT NOT NULL,
  source_order_item_id TEXT NOT NULL,
  source_order_commodity_line_id TEXT NOT NULL,
  physical_item_index INT NOT NULL CHECK (physical_item_index > 0),
  source_catalog_commodity_id TEXT,
  source_store_id TEXT,
  source_service_id TEXT,
  bill_no TEXT,
  order_pin_code VARCHAR(6),
  observed_at TIMESTAMPTZ,
  commodity_type TEXT,
  material_type TEXT,
  material_blend JSONB,
  primary_color TEXT,
  secondary_colors TEXT[],
  size_raw TEXT,
  size_normalized TEXT,
  size_system TEXT,
  brand_id UUID REFERENCES fashion_brands(id),
  brand_name_raw TEXT,
  brand_sku TEXT,
  pattern TEXT,
  style_tags TEXT[],
  attribute_evidence JSONB,
  review_status TEXT NOT NULL DEFAULT 'needs_review',
  consent_id UUID REFERENCES consent_records(id),
  model_version TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  UNIQUE (source_order_id, source_order_commodity_line_id, physical_item_index)
);

CREATE TABLE IF NOT EXISTS garment_images (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  garment_instance_id UUID NOT NULL REFERENCES garment_instances(id),
  consent_id UUID NOT NULL REFERENCES consent_records(id),
  gcs_object_key TEXT NOT NULL UNIQUE,
  sha256 TEXT,
  capture_source TEXT,
  image_type TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS extraction_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  image_id UUID NOT NULL REFERENCES garment_images(id),
  status TEXT NOT NULL CHECK (status IN (
    'PENDING_UPLOAD', 'UPLOADED', 'QUEUED', 'PROCESSING',
    'NEEDS_REVIEW', 'COMPLETED', 'FAILED'
  )),
  attempt_count INT NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL UNIQUE,
  model_version TEXT,
  raw_result JSONB,
  error_code TEXT,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS attribute_corrections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  garment_instance_id UUID NOT NULL REFERENCES garment_instances(id),
  field_name TEXT NOT NULL,
  old_value JSONB,
  new_value JSONB,
  actor_user_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaigns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  image_object_key TEXT,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS campaign_evaluations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id UUID NOT NULL REFERENCES campaigns(id),
  segment_key TEXT NOT NULL,
  fit_score NUMERIC(5,2),
  components JSONB,
  explanation TEXT,
  support_count INT,
  suppressed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id TEXT,
  tenant_id UUID,
  action TEXT NOT NULL,
  resource_type TEXT,
  resource_id TEXT,
  detail JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_garment_instances_customer ON garment_instances(source_customer_id);
CREATE INDEX IF NOT EXISTS idx_garment_instances_brand ON garment_instances(brand_id);
CREATE INDEX IF NOT EXISTS idx_garment_instances_pin ON garment_instances(order_pin_code);
CREATE INDEX IF NOT EXISTS idx_consent_customer ON consent_records(source_customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_source_refs ON garment_instances(source_order_id);
