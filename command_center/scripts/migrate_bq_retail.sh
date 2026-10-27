#!/usr/bin/env bash
# Run migrate_bq_retail.sql against a project / dataset (safe to re-run). Usage:
#   ./migrate_bq_retail.sh YOUR_PROJECT_ID [dataset]
# Adds login_cred_db.location_code for warehouse and store staff logins (see docs/RETAIL_SUITE.md).
set -euo pipefail

PROJECT="${1:-}"
DATASET="${2:-${BQ_DATASET:-suffai_ops}}"
REGION="${GCP_REGION:-asia-south1}"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./migrate_bq_retail.sh YOUR_PROJECT_ID [dataset]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Migrating ${PROJECT}:${DATASET} (login_cred_db.location_code for staff logins)"
sed "s/\`suffai_ops\./\`${DATASET}./g" "$SCRIPT_DIR/migrate_bq_retail.sql" \
  | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false
echo "Done. ${PROJECT}.${DATASET}.login_cred_db has location_code"
