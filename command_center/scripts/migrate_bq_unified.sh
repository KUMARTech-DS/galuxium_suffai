#!/usr/bin/env bash
# Run migrate_bq_unified.sql once against a project / dataset. Usage:
#   ./migrate_bq_unified.sh YOUR_PROJECT_ID [dataset]
set -euo pipefail

PROJECT="${1:-}"
DATASET="${2:-${BQ_DATASET:-suffai_ops}}"
REGION="${GCP_REGION:-asia-south1}"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./migrate_bq_unified.sh YOUR_PROJECT_ID [dataset]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Migrating ${PROJECT}:${DATASET} (customers / orders / commodities / garment_items view)"
sed "s/\`suffai_ops\./\`${DATASET}./g" "$SCRIPT_DIR/migrate_bq_unified.sql" \
  | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false
echo "Done. View: ${PROJECT}.${DATASET}.garment_items"
