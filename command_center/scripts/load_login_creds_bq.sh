#!/usr/bin/env bash
# Load login_cred_db.csv into BigQuery. This REPLACES the whole table: logins created in the app
# (Admin, and warehouse / store staff from Warehouse > Team) are deleted. It stops when staff logins
# exist unless REPLACE_STAFF_LOGINS=1.
# Usage:
#   ./load_login_creds_bq.sh YOUR_PROJECT_ID [path/to/login_cred_db.csv]
set -euo pipefail

PROJECT="${1:-}"
CSV="${2:-$(cd "$(dirname "$0")/../../command_center_data/auth" && pwd)/login_cred_db.csv}"
REGION="${GCP_REGION:-asia-south1}"
DATASET="suffai_ops"
BUCKET="${PROJECT}-cmd-center-raw"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./load_login_creds_bq.sh YOUR_PROJECT_ID [login_cred_db.csv]"
  exit 1
fi
if [[ ! -f "$CSV" ]]; then
  echo "ERROR: CSV not found: $CSV"
  echo "  Run: python3 command_center/scripts/generate_login_creds.py"
  exit 1
fi

echo "Project=$PROJECT  CSV=$CSV  Dataset=$DATASET"

gcloud config set project "$PROJECT"

if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT" --location="$REGION"
fi

gcloud storage cp "$CSV" "gs://${BUCKET}/tables/login_cred_db.csv"

bq --location="$REGION" mk --dataset --force "${PROJECT}:${DATASET}" || true

# Ensure table exists (schema from DDL)
bq query --use_legacy_sql=false --project_id="$PROJECT" <<SQL || true
CREATE TABLE IF NOT EXISTS \`${PROJECT}.${DATASET}.login_cred_db\` (
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
SQL

STAFF="$(bq query --use_legacy_sql=false --project_id="$PROJECT" --format=csv \
  "SELECT COUNT(*) FROM \`${PROJECT}.${DATASET}.login_cred_db\` WHERE role IN ('warehouse_staff', 'store_staff')" \
  2>/dev/null | tail -n 1 || true)"
if [[ "$STAFF" =~ ^[0-9]+$ && "$STAFF" -gt 0 && "${REPLACE_STAFF_LOGINS:-0}" != "1" ]]; then
  echo "ERROR: login_cred_db holds $STAFF warehouse / store staff logins created on Warehouse > Team."
  echo "  This load replaces the whole table and would delete them."
  echo "  Re-run with REPLACE_STAFF_LOGINS=1 to replace anyway."
  exit 1
fi

# Older CSVs have no location_code column: --allow_jagged_rows leaves it empty.
bq load --replace --allow_jagged_rows --source_format=CSV --skip_leading_rows=1 \
  --project_id="$PROJECT" \
  "${DATASET}.login_cred_db" \
  "gs://${BUCKET}/tables/login_cred_db.csv" \
  username:STRING,password_hash:STRING,role:STRING,brand_name:STRING,customer_id:STRING,display_name:STRING,active:BOOLEAN,created_at:TIMESTAMP,location_code:STRING

echo "Done. ${PROJECT}.${DATASET}.login_cred_db loaded from $CSV"
