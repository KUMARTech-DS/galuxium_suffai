#!/usr/bin/env bash
# Safe to re-run. Creates aggregate-only campaign fee audit and feature tables.
set -euo pipefail

PROJECT="${1:-}"
DATASET="${2:-${BQ_DATASET:-suffai_ops}}"
REGION="${GCP_REGION:-asia-south1}"
if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./migrate_bq_campaign_fee.sh YOUR_PROJECT_ID [dataset]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
sed "s/\`suffai_ops\./\`${DATASET}./g" "$SCRIPT_DIR/migrate_bq_campaign_fee.sql" \
  | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false
