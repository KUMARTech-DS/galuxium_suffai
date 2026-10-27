#!/usr/bin/env bash
# Build & deploy suffai-command-center only (does not change extraction / similarity app code).
# Uses --update-env-vars so existing keys (e.g. DATABASE_URL from Secret Manager) are preserved.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-command-center:latest"

# Optional overrides
USE_BQ="${USE_BQ:-true}"
BQ_DATASET="${BQ_DATASET:-suffai_ops}"
EXTRACTION_URL="${EXTRACTION_URL:-}"
SIMILARITY_URL="${SIMILARITY_URL:-}"
GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
ENABLE_VERTEX="${ENABLE_VERTEX:-true}"
VERTEX_LOCATION="${VERTEX_LOCATION:-asia-south1}"
VERTEX_CHAT_MODEL="${VERTEX_CHAT_MODEL:-gemini-2.5-flash}"
FEE_MODEL_VERSION="${FEE_MODEL_VERSION:-cost_model_v1}"
FEE_BASE_INR="${FEE_BASE_INR:-1500}"
FEE_PER_1K_AUDIENCE="${FEE_PER_1K_AUDIENCE:-35}"
FEE_PER_SKU_INR="${FEE_PER_SKU_INR:-125}"
FEE_PER_PIN_INR="${FEE_PER_PIN_INR:-45}"
FEE_PER_10K_COMPARISONS_INR="${FEE_PER_10K_COMPARISONS_INR:-20}"
FEE_PER_PROCESS_MINUTE_INR="${FEE_PER_PROCESS_MINUTE_INR:-12}"
FEE_PER_EXTRA_CHANNEL_INR="${FEE_PER_EXTRA_CHANNEL_INR:-250}"
FEE_PER_DAY_INR="${FEE_PER_DAY_INR:-10}"
FEE_MIN_INR="${FEE_MIN_INR:-1500}"
FEE_MAX_PCT_OF_BUDGET="${FEE_MAX_PCT_OF_BUDGET:-0.35}"
FEE_VERTEX_SHADOW="${FEE_VERTEX_SHADOW:-false}"
VERTEX_FEE_ENDPOINT_ID="${VERTEX_FEE_ENDPOINT_ID:-}"

gcloud artifacts repositories create suffai \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT" 2>/dev/null || true

docker build -f "$ROOT/Dockerfile.command_center" -t "$IMG" "$ROOT"
docker push "$IMG"

ENV_VARS="USE_BQ=${USE_BQ},BQ_DATASET=${BQ_DATASET},GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},CORS_ORIGINS=*,ENABLE_VERTEX=${ENABLE_VERTEX},VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_CHAT_MODEL=${VERTEX_CHAT_MODEL},FEE_MODEL_VERSION=${FEE_MODEL_VERSION},FEE_BASE_INR=${FEE_BASE_INR},FEE_PER_1K_AUDIENCE=${FEE_PER_1K_AUDIENCE},FEE_PER_SKU_INR=${FEE_PER_SKU_INR},FEE_PER_PIN_INR=${FEE_PER_PIN_INR},FEE_PER_10K_COMPARISONS_INR=${FEE_PER_10K_COMPARISONS_INR},FEE_PER_PROCESS_MINUTE_INR=${FEE_PER_PROCESS_MINUTE_INR},FEE_PER_EXTRA_CHANNEL_INR=${FEE_PER_EXTRA_CHANNEL_INR},FEE_PER_DAY_INR=${FEE_PER_DAY_INR},FEE_MIN_INR=${FEE_MIN_INR},FEE_MAX_PCT_OF_BUDGET=${FEE_MAX_PCT_OF_BUDGET},FEE_VERTEX_SHADOW=${FEE_VERTEX_SHADOW},VERTEX_FEE_ENDPOINT_ID=${VERTEX_FEE_ENDPOINT_ID}"
if [[ -n "$EXTRACTION_URL" ]]; then
  ENV_VARS="${ENV_VARS},EXTRACTION_URL=${EXTRACTION_URL}"
fi
if [[ -n "$SIMILARITY_URL" ]]; then
  ENV_VARS="${ENV_VARS},SIMILARITY_URL=${SIMILARITY_URL}"
fi
if [[ -n "${DATABASE_URL:-}" ]]; then
  ENV_VARS="${ENV_VARS},DATABASE_URL=${DATABASE_URL}"
fi

DEPLOY_ARGS=(
  --image="$IMG"
  --region="$REGION"
  --platform=managed
  --allow-unauthenticated
  --memory=1Gi
  --cpu=1
  --update-env-vars="$ENV_VARS"
  --project="$PROJECT"
)
if [[ -n "${CLOUD_SQL_INSTANCE:-}" ]]; then
  DEPLOY_ARGS+=(--add-cloudsql-instances="$CLOUD_SQL_INSTANCE")
fi

gcloud run deploy suffai-command-center "${DEPLOY_ARGS[@]}"

URL="$(gcloud run services describe suffai-command-center --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
echo ""
echo "Command Center: $URL/login   (tabs: /geo /ads-lab /campaigns /style-assistant /admin)"
echo "Health:         $URL/health"
echo "Metrics:        $URL/v1/metrics"
echo "Extract:        POST $URL/v1/ads-lab/extract"
echo "Campaign:       POST $URL/v1/ads-lab/campaign  (needs SIMILARITY_URL + DATABASE_URL)"
echo "Chat:           POST $URL/v1/chat  (ENABLE_VERTEX=${ENABLE_VERTEX}; live BigQuery metrics)"
echo "PIN areas:      POST $URL/v1/geo/area-profile  (Gemini + Google Search, saved to BigQuery pin_areas)"
echo "Admin:          GET  $URL/v1/admin/unique-summary  (BigQuery garment_items, admin login)"
echo "Style:          GET  $URL/v1/style-assistant/feed|users"
echo ""
echo "Redeploy guide: docs/GCP_REDEPLOY_E2E.md"
