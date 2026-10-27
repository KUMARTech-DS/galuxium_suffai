-- Retail suite logins. Safe to re-run.
--
--   ./migrate_bq_retail.sh YOUR_PROJECT_ID [dataset]
--
-- login_cred_db gains location_code: the warehouse or store code (e.g. WH1, BLR01) that a
-- warehouse_staff or store_staff login works at. Brand, admin and customer logins leave it empty.
-- Staff logins are created by the brand on Warehouse > Team; only the sha256 of a password is stored.
-- Run before deploying the Command Center build with the Inventory, Warehouse and Store POS tabs.

CREATE TABLE IF NOT EXISTS `suffai_ops.login_cred_db` (
  username STRING NOT NULL,
  password_hash STRING NOT NULL,
  role STRING NOT NULL,
  brand_name STRING,
  customer_id STRING,
  display_name STRING,
  active BOOL,
  created_at TIMESTAMP,
  location_code STRING
);

ALTER TABLE `suffai_ops.login_cred_db` ADD COLUMN IF NOT EXISTS location_code STRING;
