#!/usr/bin/env bash
# Step 3 — Wire / verify Pub/Sub: upload topic → extraction, extracted topic → similarity.
# Similarity starts only after extraction and the brand SKU match are saved.
# Does NOT redeploy extraction.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
TOPIC="${PUBSUB_IMAGE_TOPIC:-garment-image-uploaded}"
EXTRACTED_TOPIC="${PUBSUB_EXTRACTED_TOPIC:-garment-image-extracted}"
EXTRACT_SUB="${PUBSUB_EXTRACTION_SUBSCRIPTION:-garment-image-uploaded-push}"
SIM_SUB="${PUBSUB_SIMILARITY_SUBSCRIPTION:-garment-image-extracted-similarity-push}"

echo "==> [3/5] Integrate with suffai-extraction (Pub/Sub only)"
echo "    Upload topic=$TOPIC  Extracted topic=$EXTRACTED_TOPIC"

gcloud pubsub topics create "$TOPIC" --project="$PROJECT" 2>/dev/null || true
gcloud pubsub topics create "$EXTRACTED_TOPIC" --project="$PROJECT" 2>/dev/null || true

EXTRACT_URL="$(gcloud run services describe suffai-extraction \
  --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)"
SIM_URL="$(gcloud run services describe suffai-similarity \
  --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)"

if [[ -z "$EXTRACT_URL" ]]; then
  echo "WARNING: suffai-extraction not found in $REGION — deploy extraction first if you need attribute extract."
else
  echo "Extraction URL: $EXTRACT_URL"
fi
if [[ -z "$SIM_URL" ]]; then
  echo "ERROR: suffai-similarity not found. Run ./deploy/02_deploy_similarity.sh first."
  exit 1
fi
echo "Similarity URL: $SIM_URL"

echo ""
echo "Subscriptions on topic $TOPIC:"
gcloud pubsub topics list-subscriptions "$TOPIC" --project="$PROJECT" 2>/dev/null || true
echo "Subscriptions on topic $EXTRACTED_TOPIC:"
gcloud pubsub topics list-subscriptions "$EXTRACTED_TOPIC" --project="$PROJECT" 2>/dev/null || true

# Verify similarity push endpoint
if gcloud pubsub subscriptions describe "$SIM_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  PUSH="$(gcloud pubsub subscriptions describe "$SIM_SUB" --project="$PROJECT" --format='value(pushConfig.pushEndpoint)')"
  SIM_TOPIC="$(gcloud pubsub subscriptions describe "$SIM_SUB" --project="$PROJECT" --format='value(topic)')"
  echo "Similarity sub $SIM_SUB ($SIM_TOPIC) → $PUSH"
  if [[ "$SIM_TOPIC" != */topics/"$EXTRACTED_TOPIC" ]]; then
    echo "ERROR: similarity must subscribe to $EXTRACTED_TOPIC. Re-run ./deploy/deploy_similarity.sh"
    exit 1
  fi
  if [[ "$PUSH" != "${SIM_URL}/"* && "$PUSH" != "${SIM_URL}" && "$PUSH" != "${SIM_URL}/" ]]; then
    echo "Updating similarity push endpoint to ${SIM_URL}/ …"
    gcloud pubsub subscriptions update "$SIM_SUB" \
      --push-endpoint="${SIM_URL}/" \
      --project="$PROJECT"
  fi
else
  echo "ERROR: subscription $SIM_SUB missing. Re-run ./deploy/02_deploy_similarity.sh"
  exit 1
fi

if gcloud pubsub subscriptions describe "$EXTRACT_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  EPUSH="$(gcloud pubsub subscriptions describe "$EXTRACT_SUB" --project="$PROJECT" --format='value(pushConfig.pushEndpoint)')"
  echo "Extraction sub $EXTRACT_SUB → ${EPUSH:-'(pull or unset)'}"
else
  echo "NOTE: extraction subscription $EXTRACT_SUB not found (name may differ). Extraction can still use its own sub — we do not change it."
fi

# Shared GCS bucket env check
GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
echo ""
echo "Expected shared bucket: $GCS_IMAGE_BUCKET"
echo "Confirm both Cloud Run services have GCS_IMAGE_BUCKET set to the same value."

echo "Confirm suffai-extraction has PUBSUB_EXTRACTED_TOPIC=$EXTRACTED_TOPIC and roles/pubsub.publisher on it."
echo "==> Step 3 done. Extraction consumes $TOPIC; similarity consumes $EXTRACTED_TOPIC"
