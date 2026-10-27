#!/usr/bin/env bash
# Run migrate_bq_catalog_match.sql (safe to re-run). Usage:
#   ./migrate_bq_catalog_match.sh YOUR_PROJECT_ID [dataset]
# Adds commodities.brand_sku, brand_style_code, catalog_match (warehouse SKU overwrite) and outfit tags.
set -euo pipefail

PROJECT="${1:-}"
DATASET="${2:-${BQ_DATASET:-suffai_ops}}"
REGION="${GCP_REGION:-asia-south1}"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./migrate_bq_catalog_match.sh YOUR_PROJECT_ID [dataset]"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Migrating ${PROJECT}:${DATASET} (commodities catalog match and outfit tag columns)"
sed "s/\`suffai_ops\./\`${DATASET}./g" "$SCRIPT_DIR/migrate_bq_catalog_match.sql" \
  | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false
echo "Done. ${PROJECT}.${DATASET}.commodities has brand_sku, brand_style_code, catalog_match and outfit tags"
