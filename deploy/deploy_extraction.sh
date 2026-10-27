#!/usr/bin/env bash
# Build & deploy suffai-extraction only. Customer photos named {customer_id}_{order_id}_{item_id}.jpg
# are written to BigQuery ${BQ_DATASET}.commodities, so the runtime SA needs BigQuery dataEditor + jobUser.
# Uses --update-env-vars so keys set outside this script (e.g. DATABASE_URL, ENABLE_VERTEX) survive redeploys.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-extraction:latest"

GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
USE_BQ="${USE_BQ:-true}"
BQ_DATASET="${BQ_DATASET:-suffai_ops}"
PUBSUB_EXTRACTED_TOPIC="${PUBSUB_EXTRACTED_TOPIC:-garment-image-extracted}"

gcloud artifacts repositories create suffai \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT" 2>/dev/null || true

if [[ "${USE_CLOUD_BUILD:-}" == "1" ]]; then
  gcloud builds submit --config="$ROOT/deploy/cloudbuild-worker.yaml" --project="$PROJECT" "$ROOT"
else
  docker build -f "$ROOT/Dockerfile.worker" -t "$IMG" "$ROOT"
  docker push "$IMG"
fi

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},USE_BQ=${USE_BQ},BQ_DATASET=${BQ_DATASET},PUBSUB_EXTRACTED_TOPIC=${PUBSUB_EXTRACTED_TOPIC}"
if [[ -n "${ENABLE_VERTEX:-}" ]]; then
  ENV_VARS="${ENV_VARS},ENABLE_VERTEX=${ENABLE_VERTEX}"
fi
if [[ -n "${DATABASE_URL:-}" ]]; then
  ENV_VARS="${ENV_VARS},DATABASE_URL=${DATABASE_URL}"
fi

DEPLOY_ARGS=(
  --image="$IMG"
  --region="$REGION"
  --platform=managed
  --no-allow-unauthenticated
  --update-env-vars="$ENV_VARS"
  --project="$PROJECT"
)
if [[ -n "${CLOUD_SQL_INSTANCE:-}" ]]; then
  DEPLOY_ARGS+=(--add-cloudsql-instances="$CLOUD_SQL_INSTANCE")
fi

gcloud run deploy suffai-extraction "${DEPLOY_ARGS[@]}"

URL="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
SA="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" --format='value(spec.template.spec.serviceAccountName)')"

echo ""
echo "Extraction:  $URL"
echo "Runtime SA:  ${SA:-<default compute SA>}"
echo "Env:         USE_BQ=${USE_BQ} BQ_DATASET=${BQ_DATASET} GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET}"
echo "IAM needed:  roles/bigquery.dataEditor on ${PROJECT}:${BQ_DATASET} + roles/bigquery.jobUser on the project"
echo "Publishes:   ${PUBSUB_EXTRACTED_TOPIC} (roles/pubsub.publisher, granted by deploy_similarity.sh)"
echo "Photos:      gs://${GCS_IMAGE_BUCKET}/garments/<customer_id>_<order_id>_<item_id>.jpg (create the order in Admin first)"
