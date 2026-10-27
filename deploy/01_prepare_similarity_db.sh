#!/usr/bin/env bash
# Step 1 — Apply Cloud SQL migrations + BigQuery DDL for similarity.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
BQ_DATASET="${BQ_DATASET:-suffai_ops}"

echo "==> [1/5] Prepare DB / BQ for suffai-similarity"
echo "    Project=$PROJECT  BQ_DATASET=$BQ_DATASET"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "WARNING: DATABASE_URL not set."
  echo "  Skip Cloud SQL apply, or export DATABASE_URL then re-run."
  echo "  Example:"
  echo "    export DATABASE_URL='postgresql://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE'"
else
  if command -v psql >/dev/null 2>&1; then
    echo "Applying Cloud SQL migrations…"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$ROOT/db/migrations/003_garment_similarity.sql"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$ROOT/db/migrations/004_rename_user_wardrobe.sql"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$ROOT/db/migrations/011_similarity_bq_sync.sql"
    echo "Cloud SQL OK (unique_garments, user_wardrobe, garment_similarity_events, …)"
  else
    echo "ERROR: psql not found. Install Cloud SQL Auth Proxy + psql, or run SQL in Console."
    exit 1
  fi
fi

if command -v bq >/dev/null 2>&1; then
  echo "Ensuring BigQuery dataset $BQ_DATASET…"
  bq --project_id="$PROJECT" mk -d --location="${GCP_REGION:-asia-south1}" "$BQ_DATASET" 2>/dev/null || true
  # Rewrite dataset in DDL if needed
  TMP="$(mktemp)"
  sed "s/\`suffai_ops\./\`${BQ_DATASET}./g" \
    "$ROOT/similarity/scripts/create_bq_similarity_tables.sql" > "$TMP"
  bq --project_id="$PROJECT" query --use_legacy_sql=false < "$TMP"
  rm -f "$TMP"
  echo "BQ table ${BQ_DATASET}.garment_similarity_observations OK"
else
  echo "WARNING: bq CLI not found — create table manually from similarity/scripts/create_bq_similarity_tables.sql"
fi

echo "==> Step 1 done."
