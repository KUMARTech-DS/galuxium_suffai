#!/usr/bin/env bash
# Build & deploy suffai-similarity + Pub/Sub push (aligned with suffai-extraction).
# Uses --update-env-vars so keys set outside this script (e.g. DATABASE_URL) survive redeploys.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="$REGION-docker.pkg.dev/$PROJECT/suffai/suffai-similarity:latest"

GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
ENABLE_VERTEX="${ENABLE_VERTEX:-true}"
SIMILARITY_THRESHOLD="${SIMILARITY_THRESHOLD:-0.92}"
VERTEX_LOCATION="${VERTEX_LOCATION:-$REGION}"
VERTEX_EMBED_MODEL="${VERTEX_EMBED_MODEL:-multimodalembedding@001}"
USE_BQ="${USE_BQ:-true}"
BQ_DATASET="${BQ_DATASET:-suffai_ops}"
# Similarity runs after extraction + brand SKU match are saved, so it listens to the extracted topic.
TOPIC="${PUBSUB_EXTRACTED_TOPIC:-garment-image-extracted}"
SUB="${PUBSUB_SIMILARITY_SUBSCRIPTION:-garment-image-extracted-similarity-push}"
UPLOAD_TOPIC="${PUBSUB_IMAGE_TOPIC:-garment-image-uploaded}"
LEGACY_SUB="${PUBSUB_LEGACY_SIMILARITY_SUBSCRIPTION:-garment-image-uploaded-similarity-push}"
PUSH_SA="${PUBSUB_PUSH_SA:-pubsub-push@${PROJECT}.iam.gserviceaccount.com}"

gcloud artifacts repositories create suffai \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT" 2>/dev/null || true

docker build -f "$ROOT/Dockerfile.similarity" -t "$IMG" "$ROOT"
docker push "$IMG"

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},ENABLE_VERTEX=${ENABLE_VERTEX},SIMILARITY_THRESHOLD=${SIMILARITY_THRESHOLD},VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_EMBED_MODEL=${VERTEX_EMBED_MODEL},USE_BQ=${USE_BQ},BQ_DATASET=${BQ_DATASET},CORS_ORIGINS=*"
if [[ -n "${DATABASE_URL:-}" ]]; then
  ENV_VARS="${ENV_VARS},DATABASE_URL=${DATABASE_URL}"
fi

# Match extraction: require auth (Pub/Sub OIDC invoker)
DEPLOY_ARGS=(
  --image="$IMG"
  --region="$REGION"
  --platform=managed
  --no-allow-unauthenticated
  --memory=1Gi
  --cpu=1
  --update-env-vars="$ENV_VARS"
  --project="$PROJECT"
)
if [[ -n "${CLOUD_SQL_INSTANCE:-}" ]]; then
  DEPLOY_ARGS+=(--add-cloudsql-instances="$CLOUD_SQL_INSTANCE")
fi

gcloud run deploy suffai-similarity "${DEPLOY_ARGS[@]}"

URL="$(gcloud run services describe suffai-similarity --region="$REGION" --project="$PROJECT" --format='value(status.url)')"

# Ensure push SA exists and can invoke Cloud Run (same pattern as extraction push)
gcloud iam service-accounts create pubsub-push \
  --display-name="Pub/Sub push to Cloud Run" \
  --project="$PROJECT" 2>/dev/null || true

gcloud run services add-iam-policy-binding suffai-similarity \
  --region="$REGION" \
  --project="$PROJECT" \
  --member="serviceAccount:${PUSH_SA}" \
  --role="roles/run.invoker" 2>/dev/null || true

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:service-$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator" 2>/dev/null || true

gcloud pubsub topics create "$TOPIC" --project="$PROJECT" 2>/dev/null || true

# Published by suffai-extraction after the extraction job and brand SKU match are saved
if gcloud pubsub subscriptions describe "$SUB" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud pubsub subscriptions update "$SUB" \
    --push-endpoint="${URL}/" \
    --push-auth-service-account="$PUSH_SA" \
    --push-auth-token-audience="$URL" \
    --project="$PROJECT"
else
  gcloud pubsub subscriptions create "$SUB" \
    --topic="$TOPIC" \
    --push-endpoint="${URL}/" \
    --push-auth-service-account="$PUSH_SA" \
    --push-auth-token-audience="$URL" \
    --project="$PROJECT"
fi

# Stop the old parallel run on the upload topic (it would dedupe before extraction finishes)
if [[ "$LEGACY_SUB" != "$SUB" ]] && gcloud pubsub subscriptions describe "$LEGACY_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  LEGACY_TOPIC="$(gcloud pubsub subscriptions describe "$LEGACY_SUB" --project="$PROJECT" --format='value(topic)')"
  if [[ "$LEGACY_TOPIC" == */topics/"$UPLOAD_TOPIC" ]]; then
    echo "Deleting legacy similarity subscription $LEGACY_SUB on $UPLOAD_TOPIC"
    gcloud pubsub subscriptions delete "$LEGACY_SUB" --project="$PROJECT" --quiet
  fi
fi

# suffai-extraction must be able to publish the extracted event
EXTRACT_SA="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
if [[ -z "$EXTRACT_SA" ]]; then
  EXTRACT_SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
fi
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:${EXTRACT_SA}" \
  --role="roles/pubsub.publisher" \
  --project="$PROJECT" >/dev/null

echo ""
echo "Similarity:     $URL"
echo "Health:         $URL/health  (needs identity token)"
echo "Process:        POST $URL/v1/similarity/process"
echo "Compare:        POST $URL/v1/similarity/compare-pins  (called by Command Center with each PIN's garments)"
echo "Pub/Sub:        $TOPIC → $SUB → $URL/ (OIDC via $PUSH_SA)"
echo "Unique garments are counted by Command Center from BigQuery garment_items (Admin tab)."
echo ""
echo "Apply: db/migrations/003_garment_similarity.sql (+ 004 if upgrading, then 011_similarity_bq_sync.sql and 015_garment_outfit_tags.sql for SKU-first dedup)"
echo "Extraction env: PUBSUB_EXTRACTED_TOPIC=$TOPIC (publisher: $EXTRACT_SA)"
echo "BQ DDL: similarity/scripts/create_bq_similarity_tables.sql (dataset $BQ_DATASET)"
echo "Set DATABASE_URL / Cloud SQL attach if not passed above."
echo "IAM: storage.objectViewer, aiplatform.user, bigquery.dataEditor, cloudsql.client"
