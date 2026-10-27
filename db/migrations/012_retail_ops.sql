-- Retail suite: catalog, locations and bins, stock ledger, purchasing, transfers and POS billing.
-- Every row carries brand_key (vocab.value_key of the brand name); the API scopes every read and write to it.
-- Money is in paise (BIGINT), quantities are whole units, ids are UUID strings made by the app and
-- timestamps are written by the app in UTC. Idempotent. The same file builds the SQLite test schema,
-- so it sticks to SQL both engines accept (no server-side uuid defaults, no JSONB, no arrays).

CREATE TABLE IF NOT EXISTS retail_brand_settings (
  brand_key TEXT PRIMARY KEY,
  brand_name TEXT NOT NULL,
  legal_name TEXT,
  gstin TEXT,
  address TEXT,
  invoice_footer TEXT,
  return_days INTEGER NOT NULL DEFAULT 30,
  allow_negative_stock BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- kind: warehouse | store. code (up to 5 letters / digits) prefixes the store's invoice numbers.
-- state_code is the 2-digit GST state code (29 = Karnataka) and decides CGST + SGST vs IGST.
CREATE TABLE IF NOT EXISTS retail_locations (
  location_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('warehouse', 'store')),
  address TEXT,
  city TEXT,
  pin_code TEXT,
  state_code TEXT NOT NULL,
  gstin TEXT,
  phone TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, code)
);

CREATE INDEX IF NOT EXISTS idx_retail_locations_brand ON retail_locations (brand_key, kind);

-- Warehouse bins. Stock without a bin sits in bin_code '' and the DMG bin holds damaged, unsellable units.
CREATE TABLE IF NOT EXISTS retail_bins (
  location_id TEXT NOT NULL REFERENCES retail_locations (location_id),
  bin_code TEXT NOT NULL,
  zone TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (location_id, bin_code)
);

-- A style. gst_rule apparel: 5% up to Rs 2,500 sale value per piece, 18% above; fixed: gst_rate_bp
-- (basis points, 500 = 5%).
CREATE TABLE IF NOT EXISTS retail_products (
  product_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  style_code TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  gender TEXT,
  material TEXT,
  pattern TEXT,
  season TEXT,
  hsn_code TEXT NOT NULL,
  gst_rule TEXT NOT NULL DEFAULT 'apparel' CHECK (gst_rule IN ('apparel', 'fixed')),
  gst_rate_bp INTEGER,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, style_code)
);

CREATE INDEX IF NOT EXISTS idx_retail_products_brand ON retail_products (brand_key, category);

-- A sellable SKU: one colour and size of a style. mrp_paise includes GST; avg_cost_paise is the
-- moving average purchase cost across the brand.
CREATE TABLE IF NOT EXISTS retail_variants (
  variant_id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES retail_products (product_id),
  brand_key TEXT NOT NULL,
  sku TEXT NOT NULL,
  barcode TEXT NOT NULL,
  color TEXT NOT NULL,
  size TEXT NOT NULL,
  mrp_paise BIGINT NOT NULL CHECK (mrp_paise > 0),
  avg_cost_paise BIGINT NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, sku),
  UNIQUE (brand_key, barcode)
);

CREATE INDEX IF NOT EXISTS idx_retail_variants_product ON retail_variants (product_id);

-- Units on hand per location, bin and SKU. Only the stock module writes here, always together with
-- a retail_stock_moves row.
CREATE TABLE IF NOT EXISTS retail_stock (
  location_id TEXT NOT NULL,
  bin_code TEXT NOT NULL DEFAULT '',
  variant_id TEXT NOT NULL,
  brand_key TEXT NOT NULL,
  on_hand INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (location_id, bin_code, variant_id)
);

CREATE INDEX IF NOT EXISTS idx_retail_stock_variant ON retail_stock (brand_key, variant_id);

-- Stock ledger: one signed row per change. reason: opening | grn | sale | return | return_damaged |
-- transfer_out | transfer_in | shortage | adjust | count | putaway.
CREATE TABLE IF NOT EXISTS retail_stock_moves (
  move_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  location_id TEXT NOT NULL,
  bin_code TEXT NOT NULL DEFAULT '',
  variant_id TEXT NOT NULL,
  qty INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  note TEXT,
  actor TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retail_moves_brand ON retail_stock_moves (brand_key, created_at);
CREATE INDEX IF NOT EXISTS idx_retail_moves_location ON retail_stock_moves (location_id, variant_id);

-- Store / warehouse par levels: replenish up to max_qty once sellable stock falls below min_qty.
CREATE TABLE IF NOT EXISTS retail_par_levels (
  location_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  brand_key TEXT NOT NULL,
  min_qty INTEGER NOT NULL CHECK (min_qty >= 0),
  max_qty INTEGER NOT NULL CHECK (max_qty >= min_qty),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (location_id, variant_id)
);

CREATE TABLE IF NOT EXISTS retail_suppliers (
  supplier_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  name TEXT NOT NULL,
  gstin TEXT,
  phone TEXT,
  email TEXT,
  address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, name)
);

-- status: draft -> ordered -> partial -> received; draft / ordered -> cancelled.
CREATE TABLE IF NOT EXISTS retail_purchase_orders (
  po_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  po_number TEXT NOT NULL,
  supplier_id TEXT NOT NULL REFERENCES retail_suppliers (supplier_id),
  location_id TEXT NOT NULL REFERENCES retail_locations (location_id),
  status TEXT NOT NULL CHECK (status IN ('draft', 'ordered', 'partial', 'received', 'cancelled')),
  expected_date DATE,
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, po_number)
);

CREATE INDEX IF NOT EXISTS idx_retail_po_brand ON retail_purchase_orders (brand_key, created_at);

CREATE TABLE IF NOT EXISTS retail_po_lines (
  po_id TEXT NOT NULL REFERENCES retail_purchase_orders (po_id),
  line_no INTEGER NOT NULL,
  variant_id TEXT NOT NULL,
  qty_ordered INTEGER NOT NULL CHECK (qty_ordered > 0),
  qty_received INTEGER NOT NULL DEFAULT 0,
  unit_cost_paise BIGINT NOT NULL CHECK (unit_cost_paise >= 0),
  PRIMARY KEY (po_id, line_no)
);

-- Goods received note: one receipt against a purchase order.
CREATE TABLE IF NOT EXISTS retail_grns (
  grn_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  grn_number TEXT NOT NULL,
  po_id TEXT NOT NULL REFERENCES retail_purchase_orders (po_id),
  location_id TEXT NOT NULL,
  supplier_invoice TEXT,
  note TEXT,
  received_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, grn_number)
);

CREATE TABLE IF NOT EXISTS retail_grn_lines (
  grn_id TEXT NOT NULL REFERENCES retail_grns (grn_id),
  line_no INTEGER NOT NULL,
  po_line_no INTEGER NOT NULL,
  variant_id TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty > 0),
  bin_code TEXT NOT NULL DEFAULT '',
  unit_cost_paise BIGINT NOT NULL,
  PRIMARY KEY (grn_id, line_no)
);

-- status: draft -> in_transit (dispatched) -> received; draft -> cancelled.
-- source: manual | replenishment | opening_plan.
CREATE TABLE IF NOT EXISTS retail_transfers (
  transfer_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  transfer_number TEXT NOT NULL,
  from_location_id TEXT NOT NULL REFERENCES retail_locations (location_id),
  to_location_id TEXT NOT NULL REFERENCES retail_locations (location_id),
  status TEXT NOT NULL CHECK (status IN ('draft', 'in_transit', 'received', 'cancelled')),
  source TEXT NOT NULL DEFAULT 'manual',
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dispatched_by TEXT,
  dispatched_at TIMESTAMPTZ,
  received_by TEXT,
  received_at TIMESTAMPTZ,
  UNIQUE (brand_key, transfer_number)
);

CREATE INDEX IF NOT EXISTS idx_retail_transfers_brand ON retail_transfers (brand_key, created_at);

CREATE TABLE IF NOT EXISTS retail_transfer_lines (
  transfer_id TEXT NOT NULL REFERENCES retail_transfers (transfer_id),
  line_no INTEGER NOT NULL,
  variant_id TEXT NOT NULL,
  qty_requested INTEGER NOT NULL CHECK (qty_requested > 0),
  qty_sent INTEGER NOT NULL DEFAULT 0,
  qty_received INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (transfer_id, line_no)
);

-- Gap-free document numbers. scope_key is a location_id (invoices, credit notes) or a brand_key
-- (POs, GRNs, transfers, barcodes); period is the financial year, e.g. 2627 for Apr 2026 - Mar 2027.
CREATE TABLE IF NOT EXISTS retail_doc_sequences (
  scope_key TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  period TEXT NOT NULL,
  next_no INTEGER NOT NULL,
  PRIMARY KEY (scope_key, doc_type, period)
);

-- POS documents. kind invoice (sale) or credit_note (return against original_invoice_id); amounts
-- are positive on both and reports subtract credit notes. Issued documents are never edited.
-- seller is a JSON snapshot of the seller details printed on the document.
CREATE TABLE IF NOT EXISTS retail_invoices (
  invoice_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  location_id TEXT NOT NULL REFERENCES retail_locations (location_id),
  kind TEXT NOT NULL CHECK (kind IN ('invoice', 'credit_note')),
  invoice_number TEXT NOT NULL,
  original_invoice_id TEXT,
  business_date DATE NOT NULL,
  fy TEXT NOT NULL,
  customer_name TEXT,
  customer_phone TEXT,
  customer_gstin TEXT,
  place_of_supply TEXT NOT NULL,
  campaign_id TEXT,
  units INTEGER NOT NULL,
  gross_paise BIGINT NOT NULL,
  discount_paise BIGINT NOT NULL,
  taxable_paise BIGINT NOT NULL,
  cgst_paise BIGINT NOT NULL,
  sgst_paise BIGINT NOT NULL,
  igst_paise BIGINT NOT NULL,
  round_off_paise BIGINT NOT NULL,
  total_paise BIGINT NOT NULL,
  change_paise BIGINT NOT NULL DEFAULT 0,
  reason TEXT,
  seller TEXT NOT NULL,
  client_ref TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (brand_key, invoice_number),
  UNIQUE (brand_key, client_ref)
);

CREATE INDEX IF NOT EXISTS idx_retail_invoices_day ON retail_invoices (brand_key, business_date);
CREATE INDEX IF NOT EXISTS idx_retail_invoices_location ON retail_invoices (location_id, business_date);
CREATE INDEX IF NOT EXISTS idx_retail_invoices_original ON retail_invoices (original_invoice_id);
CREATE INDEX IF NOT EXISTS idx_retail_invoices_campaign ON retail_invoices (campaign_id);

CREATE TABLE IF NOT EXISTS retail_invoice_lines (
  invoice_id TEXT NOT NULL REFERENCES retail_invoices (invoice_id),
  line_no INTEGER NOT NULL,
  variant_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  description TEXT NOT NULL,
  category TEXT,
  color TEXT,
  size TEXT,
  hsn_code TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty > 0),
  mrp_paise BIGINT NOT NULL,
  discount_paise BIGINT NOT NULL,
  taxable_paise BIGINT NOT NULL,
  gst_rate_bp INTEGER NOT NULL,
  cgst_paise BIGINT NOT NULL,
  sgst_paise BIGINT NOT NULL,
  igst_paise BIGINT NOT NULL,
  total_paise BIGINT NOT NULL,
  original_line_no INTEGER,
  restocked BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (invoice_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_retail_invoice_lines_variant ON retail_invoice_lines (variant_id);

-- Payments on an invoice and refunds on a credit note (mode cash | card | upi | wallet).
-- Payment gateways are not integrated: the cashier records what was collected.
CREATE TABLE IF NOT EXISTS retail_payments (
  payment_id TEXT PRIMARY KEY,
  invoice_id TEXT NOT NULL REFERENCES retail_invoices (invoice_id),
  mode TEXT NOT NULL CHECK (mode IN ('cash', 'card', 'upi', 'wallet')),
  amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
  reference TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retail_payments_invoice ON retail_payments (invoice_id);

-- Settings, price and master-data changes (who changed what).
CREATE TABLE IF NOT EXISTS retail_audit (
  event_id TEXT PRIMARY KEY,
  brand_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT,
  detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retail_audit_brand ON retail_audit (brand_key, created_at);
