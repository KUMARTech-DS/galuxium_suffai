-- Geo store intelligence: brand store locations found via Gemini + Google Search (cached),
-- and grounded external market checks for whitespace PINs (> 4 km from an existing store).

CREATE TABLE IF NOT EXISTS geo_store_lookups (
  brand_key TEXT NOT NULL,
  city TEXT NOT NULL,
  brand_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'empty')),
  sources JSONB NOT NULL DEFAULT '[]'::jsonb,
  search_entry_html TEXT,
  model TEXT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (brand_key, city)
);

CREATE TABLE IF NOT EXISTS geo_brand_stores (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_key TEXT NOT NULL,
  city TEXT NOT NULL,
  store_name TEXT NOT NULL,
  address TEXT,
  area TEXT,
  pin_code VARCHAR(6),
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  location_source TEXT NOT NULL DEFAULT 'model',
  confidence TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
  source_url TEXT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (brand_key, city) REFERENCES geo_store_lookups (brand_key, city) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_geo_brand_stores_brand ON geo_brand_stores (brand_key, city);

CREATE TABLE IF NOT EXISTS geo_market_enrichment (
  pin_code VARCHAR(6) NOT NULL,
  brand_key TEXT NOT NULL,
  payload JSONB NOT NULL,
  sources JSONB NOT NULL DEFAULT '[]'::jsonb,
  search_entry_html TEXT,
  model TEXT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (pin_code, brand_key)
);
