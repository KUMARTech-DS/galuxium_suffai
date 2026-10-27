#!/usr/bin/env bash
# Run migrate_bq_consistency.sql against a project / dataset (safe to re-run). Usage:
#   ./migrate_bq_consistency.sh YOUR_PROJECT_ID [dataset]
# Then: recompute_rfm.py, backfill_pin_areas.py, reconcile_data.py (see docs/GCP_REDEPLOY_E2E.md).
set -euo pipefail

PROJECT="${1:-}"
DATASET="${2:-${BQ_DATASET:-suffai_ops}}"
REGION="${GCP_REGION:-asia-south1}"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./migrate_bq_consistency.sh YOUR_PROJECT_ID [dataset]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Migrating ${PROJECT}:${DATASET} (pin_areas, ref_values, canonical values, garment_items view)"
sed "s/\`suffai_ops\./\`${DATASET}./g" "$SCRIPT_DIR/migrate_bq_consistency.sql" \
  | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false
echo "Done. Tables: ${PROJECT}.${DATASET}.pin_areas / ref_values"
