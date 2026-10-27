#!/usr/bin/env bash
# Step 4 — Redeploy / update suffai-command-center with EXTRACTION_URL + SIMILARITY_URL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"

echo "==> [4/5] Integrate with suffai-command-center"

export EXTRACTION_URL="${EXTRACTION_URL:-$(gcloud run services describe suffai-extraction \
  --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)}"
export SIMILARITY_URL="${SIMILARITY_URL:-$(gcloud run services describe suffai-similarity \
  --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)}"

if [[ -z "${SIMILARITY_URL:-}" ]]; then
  echo "ERROR: SIMILARITY_URL empty. Deploy similarity first (./deploy/02_deploy_similarity.sh)."
  exit 1
fi

echo "EXTRACTION_URL=${EXTRACTION_URL:-'(optional — Ads Lab extract needs it)'}"
echo "SIMILARITY_URL=$SIMILARITY_URL"

"$ROOT/deploy/deploy_command_center.sh"

echo "==> Step 4 done. Admin tab proxies to similarity; Ads Lab uses extraction."
