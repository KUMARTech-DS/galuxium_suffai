#!/usr/bin/env bash
# Opt-in: run Ads Lab campaign jobs on Cloud Tasks instead of a thread inside suffai-command-center.
# Safe to re-run. Without this script Command Center keeps using in-process threads.
#
#   GOOGLE_CLOUD_PROJECT=my-proj GCP_REGION=asia-south1 ./deploy/setup_cloud_tasks.sh
#
# Creates the queue + a task-invoker service account, lets Command Center's runtime account enqueue
# tasks and mint OIDC tokens as that account, then sets COMMAND_CENTER_URL / CLOUD_TASKS_QUEUE /
# TASKS_SERVICE_ACCOUNT on the service (--update-env-vars, so other settings are kept).
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
SERVICE="${COMMAND_CENTER_SERVICE:-suffai-command-center}"
QUEUE="${CAMPAIGN_TASKS_QUEUE:-campaign-jobs}"
TASKS_SA_NAME="${TASKS_SA_NAME:-cc-tasks}"
TASKS_SA="${TASKS_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

gcloud services enable cloudtasks.googleapis.com --project="$PROJECT"

if ! gcloud tasks queues describe "$QUEUE" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud tasks queues create "$QUEUE" --location="$REGION" --project="$PROJECT" \
    --max-attempts=10 --min-backoff=10s --max-backoff=300s
fi

if ! gcloud iam service-accounts describe "$TASKS_SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$TASKS_SA_NAME" \
    --display-name="Cloud Tasks -> Command Center campaign jobs" --project="$PROJECT"
fi

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
if [[ -z "$URL" ]]; then
  echo "ERROR: $SERVICE not found in $REGION. Deploy Command Center first (deploy/deploy_command_center.sh)." >&2
  exit 1
fi

RUNTIME_SA="$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)')"
if [[ -z "$RUNTIME_SA" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

# Runtime account: create tasks on the queue, and attach an OIDC token minted as TASKS_SA.
gcloud tasks queues add-iam-policy-binding "$QUEUE" --location="$REGION" --project="$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/cloudtasks.enqueuer" >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$TASKS_SA" --project="$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/iam.serviceAccountUser" >/dev/null
# Tasks account may call the service (also works if the service is later made private).
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --member="serviceAccount:${TASKS_SA}" --role="roles/run.invoker" >/dev/null

QUEUE_PATH="projects/${PROJECT}/locations/${REGION}/queues/${QUEUE}"
gcloud run services update "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --update-env-vars="COMMAND_CENTER_URL=${URL},CLOUD_TASKS_QUEUE=${QUEUE_PATH},TASKS_SERVICE_ACCOUNT=${TASKS_SA}"

echo ""
echo "Cloud Tasks enabled for $SERVICE"
echo "  Queue:           $QUEUE_PATH"
echo "  Task account:    $TASKS_SA (OIDC audience $URL)"
echo "  Runtime account: $RUNTIME_SA (cloudtasks.enqueuer + serviceAccountUser on task account)"
echo "  Worker endpoint: POST $URL/internal/campaign-jobs/{job_id}/run"
echo "To revert to thread mode: gcloud run services update $SERVICE --region=$REGION --remove-env-vars=CLOUD_TASKS_QUEUE"
