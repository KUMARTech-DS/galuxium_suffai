#!/usr/bin/env bash
# Load the three CSVs into BigQuery. Usage:
#   ./load_to_bq.sh YOUR_PROJECT_ID [path/to/csv_dir]
# Each load replaces its table. A table holding Admin-created or photo-pipeline rows is refused
# unless FORCE=1 is set (those rows would be deleted). APPEND=1 keeps the tables and adds only rows
# whose id is new; then run recompute_rfm.py, backfill_pin_areas.py and reconcile_data.py.
set -euo pipefail

PROJECT="${1:-}"
DATA_DIR="${2:-$(cd "$(dirname "$0")/../../command_center_data/sample" && pwd)}"
REGION="${GCP_REGION:-asia-south1}"
DATASET="suffai_ops"
BUCKET="${PROJECT}-cmd-center-raw"

if [[ -z "$PROJECT" ]]; then
  echo "Usage: ./load_to_bq.sh YOUR_PROJECT_ID [csv_dir]"
  exit 1
fi

echo "Project=$PROJECT  DataDir=$DATA_DIR  Dataset=$DATASET"

gcloud config set project "$PROJECT"
gcloud services enable bigquery.googleapis.com storage.googleapis.com --project="$PROJECT"

# Raw bucket (create if missing)
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT" --location="$REGION"
fi

gcloud storage cp \
  "$DATA_DIR/customers.csv" \
  "$DATA_DIR/orders.csv" \
  "$DATA_DIR/commodities.csv" \
  "gs://${BUCKET}/tables/"

bq --location="$REGION" mk --dataset --force "${PROJECT}:${DATASET}" || true

# Column types come from create_bq_tables.sql (no autodetect).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for TABLE in customers orders commodities; do
  python3 "$SCRIPT_DIR/load_csv_bq.py" \
    --project "$PROJECT" --dataset "$DATASET" --location "$REGION" --table "$TABLE" \
    --header "$DATA_DIR/${TABLE}.csv" --uri "gs://${BUCKET}/tables/${TABLE}.csv" \
    ${FORCE:+--force} ${APPEND:+--append}
done

echo "Done. Tables: ${PROJECT}.${DATASET}.customers / orders / commodities"
