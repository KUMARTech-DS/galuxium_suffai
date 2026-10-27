#!/usr/bin/env bash
# Deploy API + Web + Worker to Cloud Run. Requires: gcloud auth, project set.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Project=$PROJECT Region=$REGION"

gcloud artifacts repositories create suffai \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT" 2>/dev/null || true

API_IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-api:latest"
CC_IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-command-center:latest"
WORKER_IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-extraction:latest"

docker build -f "$ROOT/Dockerfile.api" -t "$API_IMG" "$ROOT"
docker push "$API_IMG"

gcloud run deploy suffai-api \
  --image="$API_IMG" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars="ENVIRONMENT=staging,DEMO_AUTH_ENABLED=true,GCP_REGION=$REGION,GOOGLE_CLOUD_PROJECT=$PROJECT" \
  --project="$PROJECT"

API_URL="$(gcloud run services describe suffai-api --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
echo "API_URL=$API_URL"

docker build -f "$ROOT/Dockerfile.worker" -t "$WORKER_IMG" "$ROOT"
docker push "$WORKER_IMG"

gcloud run deploy suffai-extraction \
  --image="$WORKER_IMG" \
  --region="$REGION" \
  --platform=managed \
  --no-allow-unauthenticated \
  --set-env-vars="ENABLE_VERTEX=false" \
  --project="$PROJECT"

WORKER_URL="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" --format='value(status.url)')"

# Command Center (Geo + Ads Lab) — replaces Next.js web for this product surface
docker build -f "$ROOT/Dockerfile.command_center" -t "$CC_IMG" "$ROOT"
docker push "$CC_IMG"

gcloud run deploy suffai-command-center \
  --image="$CC_IMG" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --memory=1Gi \
  --set-env-vars="USE_BQ=true,BQ_DATASET=suffai_ops,GOOGLE_CLOUD_PROJECT=$PROJECT,GCS_IMAGE_BUCKET=${PROJECT}-suffai-garment-images,EXTRACTION_URL=$WORKER_URL,CORS_ORIGINS=*" \
  --project="$PROJECT"

CC_URL="$(gcloud run services describe suffai-command-center --region="$REGION" --project="$PROJECT" --format='value(status.url)')"

# Point legacy API CORS at command center (api code unchanged; env only)
gcloud run services update suffai-api \
  --region="$REGION" \
  --project="$PROJECT" \
  --update-env-vars="WEB_ORIGIN=$CC_URL,API_PUBLIC_URL=$API_URL" || true

echo "Command Center: $CC_URL/login"
echo "API:            $API_URL"
echo "Extraction:     $WORKER_URL"
echo "Done."
