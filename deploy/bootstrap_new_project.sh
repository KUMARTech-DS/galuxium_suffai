#!/usr/bin/env bash
# Deploy SuffAI end to end into a new, empty GCP project:
#   APIs, Artifact Registry, one service account per service, GCS image bucket + Pub/Sub (with dead-letter),
#   BigQuery suffai_ops (tables, sample data, logins, migrations, RFM, PIN areas),
#   Cloud SQL PostgreSQL (all db/migrations, DATABASE_URL in Secret Manager),
#   suffai-extraction, suffai-similarity and suffai-command-center on Cloud Run, IAM, Cloud Tasks.
# Safe to re-run: every step keeps what already exists.
#
#   GOOGLE_CLOUD_PROJECT=my-new-project ./deploy/bootstrap_new_project.sh
#   GOOGLE_CLOUD_PROJECT=my-new-project BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./deploy/bootstrap_new_project.sh
#     (the second form also creates the project and links billing)
#
# Run from the repo root in Cloud Shell, or anywhere with the Google Cloud SDK (gcloud + bq), python3,
# curl, openssl and `gcloud auth application-default login`. Docker, psql and the Cloud SQL proxy are
# not needed: images are built by Cloud Build and migrations are imported with `gcloud sql import`.
#
# Options (environment variables):
#   GCP_REGION          asia-south1
#   GCS_IMAGE_BUCKET    <project>-suffai-garment-images
#   SQL_INSTANCE        suffai-ops          SQL_TIER  db-g1-small      SQL_VERSION  POSTGRES_15
#   GEMINI_MODEL        gemini-2.5-flash    (extraction, chat/AURA and grounded Geo lookups)
#   GEO_CITY            Bengaluru
#   LOAD_SAMPLE_DATA    1  load command_center_data/sample/*.csv (customer names, numbers, addresses)
#                          into BigQuery when customers is empty; 0 starts with empty tables
#   BACKFILL_PIN_AREAS  1  one billed Gemini + Google Search prompt per PIN; 0 skips (Geo fills on demand)
#   ENABLE_CLOUD_TASKS  1  run Ads Lab ranking jobs on Cloud Tasks (deploy/setup_cloud_tasks.sh)
#
# BigQuery dataset is always suffai_ops (the load scripts write there). Logins come from
# command_center_data/auth/login_cred_db.csv (hashes only); the plaintext file is never uploaded.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to the new project id}"
REGION="${GCP_REGION:-asia-south1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATASET="suffai_ops"
GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
RAW_BUCKET="${PROJECT}-cmd-center-raw"
SQL_INSTANCE="${SQL_INSTANCE:-suffai-ops}"
SQL_TIER="${SQL_TIER:-db-g1-small}"
SQL_VERSION="${SQL_VERSION:-POSTGRES_15}"
DB_NAME="suffai"
DB_USER="suffai"
DB_SECRET="suffai-database-url"
CONN="${PROJECT}:${REGION}:${SQL_INSTANCE}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
VERTEX_LOCATION="${VERTEX_LOCATION:-$REGION}"
GEO_CITY="${GEO_CITY:-Bengaluru}"
LOAD_SAMPLE_DATA="${LOAD_SAMPLE_DATA:-1}"
BACKFILL_PIN_AREAS="${BACKFILL_PIN_AREAS:-1}"
ENABLE_CLOUD_TASKS="${ENABLE_CLOUD_TASKS:-1}"
TOPIC="garment-image-uploaded"
DLQ_TOPIC="garment-image-uploaded-dlq"
EXTRACT_SUB="garment-image-uploaded-push"
# Similarity runs after extraction has saved the photo and its warehouse SKU match.
EXTRACTED_TOPIC="garment-image-extracted"
SIM_SUB="garment-image-extracted-similarity-push"
LEGACY_SIM_SUB="garment-image-uploaded-similarity-push"
IMG_BASE="${REGION}-docker.pkg.dev/${PROJECT}/suffai"
VENV="${SUFFAI_VENV:-${TMPDIR:-/tmp}/suffai-bootstrap-venv}"

CC_SA="suffai-cc@${PROJECT}.iam.gserviceaccount.com"
EX_SA="suffai-extraction@${PROJECT}.iam.gserviceaccount.com"
SIM_SA="suffai-similarity@${PROJECT}.iam.gserviceaccount.com"
PUSH_SA="pubsub-push@${PROJECT}.iam.gserviceaccount.com"

STEP=0
log() { STEP=$((STEP + 1)); printf '\n==> [%s] %s\n' "$STEP" "$*"; }
info() { printf '    %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "\nFailed at line %s. Fix the cause and re-run; finished steps are skipped.\n" "$LINENO" >&2' ERR

# New service accounts and bindings take a minute to propagate.
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge 6 ]] && return 1
    sleep $((n * 10))
  done
}

grant_project() { # member role
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member="$1" --role="$2" \
    --condition=None --quiet >/dev/null
}

grant_dataset_editor() { # service account email
  local i
  for i in 1 2; do
    if bq --project_id="$PROJECT" add-iam-policy-binding --member="serviceAccount:$1" \
        --role=roles/bigquery.dataEditor "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 15
  done
  info "bq could not grant on ${DATASET}; granting BigQuery Data Editor on the project to $1"
  grant_project "serviceAccount:$1" roles/bigquery.dataEditor
}

bq_count() { # table
  bq --project_id="$PROJECT" --location="$REGION" --headless --quiet query --use_legacy_sql=false \
    --format=csv "SELECT COUNT(*) AS n FROM \`${DATASET}.$1\`" | tail -1
}

service_url() {
  gcloud run services describe "$1" --region="$REGION" --project="$PROJECT" --format='value(status.url)'
}

ensure_sa() { # name display-name
  local email="$1@${PROJECT}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$email" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$1" --display-name="$2" --project="$PROJECT" >/dev/null
    info "created $email"
  fi
}

ensure_bucket() {
  if ! gcloud storage buckets describe "gs://$1" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$1" --project="$PROJECT" --location="$REGION" \
      --uniform-bucket-level-access --public-access-prevention >/dev/null
    info "created gs://$1"
  fi
}

ensure_topic() {
  gcloud pubsub topics describe "$1" --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud pubsub topics create "$1" --project="$PROJECT" >/dev/null
}

# Bucket notifications publish as the Cloud Storage service agent
# (service-NUMBER@gs-project-accounts.iam.gserviceaccount.com). On a new project
# that account does not exist until the Storage API is asked for its email, and
# IAM often rejects the grant for several minutes after the email comes back.
grant_gcs_publisher() { # topic
  local email n=0 err
  info "asking Cloud Storage to create its service agent"
  email="$(curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://storage.googleapis.com/storage/v1/projects/${PROJECT}/serviceAccount" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["email_address"])')"
  [[ -n "$email" ]] || die "Cloud Storage did not return a service agent email."
  info "Pub/Sub publisher on $1 for $email (can take several minutes on a new project)"
  err="$(mktemp)"
  until gcloud pubsub topics add-iam-policy-binding "$1" --project="$PROJECT" \
      --member="serviceAccount:$email" --role=roles/pubsub.publisher >"$err" 2>&1; do
    n=$((n + 1))
    if [[ "$n" -ge 21 ]]; then
      cat "$err" >&2
      rm -f "$err"
      die "Cloud Storage service agent $email is still not visible to IAM. Wait a few minutes and re-run."
    fi
    info "not visible to IAM yet; waiting ($n/21)"
    sleep 20
    curl -fsS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      "https://storage.googleapis.com/storage/v1/projects/${PROJECT}/serviceAccount" >/dev/null
  done
  rm -f "$err"
}

ensure_push_sub() { # subscription service-url [topic]
  local args=(
    --push-endpoint="$2/" --push-auth-service-account="$PUSH_SA" --push-auth-token-audience="$2"
    --ack-deadline=300 --dead-letter-topic="$DLQ_TOPIC" --max-delivery-attempts=5
    --min-retry-delay=10s --max-retry-delay=600s --project="$PROJECT"
  )
  if gcloud pubsub subscriptions describe "$1" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud pubsub subscriptions update "$1" "${args[@]}" >/dev/null
  else
    gcloud pubsub subscriptions create "$1" --topic="${3:-$TOPIC}" "${args[@]}" >/dev/null
  fi
  retry gcloud pubsub subscriptions add-iam-policy-binding "$1" --project="$PROJECT" \
    --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.subscriber >/dev/null
}

# ---------------------------------------------------------------------------------------------------
log "Preflight"
cd "$ROOT"
for tool in gcloud bq python3 curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found. Install the Google Cloud SDK / $tool, or run this in Cloud Shell."
done
[[ -n "$(gcloud config get-value account 2>/dev/null)" ]] || die "No gcloud login. Run: gcloud auth login"
gcloud auth application-default print-access-token >/dev/null 2>&1 \
  || die "No Application Default Credentials (used by the BigQuery loader and backfills). Run: gcloud auth application-default login"
[[ -f command_center_data/auth/login_cred_db.csv ]] \
  || die "command_center_data/auth/login_cred_db.csv missing. Run: python3 command_center/scripts/generate_login_creds.py"
if [[ "$LOAD_SAMPLE_DATA" == "1" ]]; then
  for f in customers orders commodities; do
    [[ -f "command_center_data/sample/$f.csv" ]] || die "command_center_data/sample/$f.csv missing (or set LOAD_SAMPLE_DATA=0)"
  done
fi
info "Project=$PROJECT  Region=$REGION  Account=$(gcloud config get-value account 2>/dev/null)"

log "Project and billing"
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  [[ -n "${BILLING_ACCOUNT:-}" ]] \
    || die "Project $PROJECT not found. Create it in the Console, or set BILLING_ACCOUNT so this script creates it."
  gcloud projects create "$PROJECT" --name="SuffAI" >/dev/null
  info "created project $PROJECT"
fi
if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
  gcloud billing projects link "$PROJECT" --billing-account="$BILLING_ACCOUNT" >/dev/null
fi
BILLING="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)"
if [[ "$BILLING" == "False" ]]; then
  die "Billing is not enabled on $PROJECT. Link a billing account (Console > Billing) or set BILLING_ACCOUNT."
elif [[ -z "$BILLING" ]]; then
  info "Could not read the billing status; continuing (API enablement fails if billing is off)."
fi
gcloud config set project "$PROJECT" >/dev/null
gcloud config set run/region "$REGION" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
export GOOGLE_CLOUD_PROJECT="$PROJECT" GOOGLE_CLOUD_QUOTA_PROJECT="$PROJECT" GCP_REGION="$REGION" BQ_DATASET="$DATASET"

log "Enable APIs (a few minutes on a new project)"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com compute.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com cloudresourcemanager.googleapis.com \
  storage.googleapis.com bigquery.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  pubsub.googleapis.com aiplatform.googleapis.com cloudtasks.googleapis.com logging.googleapis.com \
  --project="$PROJECT"

log "Cloud SQL instance $SQL_INSTANCE (created in the background; about 10 minutes)"
SQL_OP=""
if gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT" >/dev/null 2>&1; then
  info "exists"
else
  SQL_OP="$(gcloud sql instances create "$SQL_INSTANCE" --project="$PROJECT" --region="$REGION" \
    --database-version="$SQL_VERSION" --edition=ENTERPRISE --tier="$SQL_TIER" \
    --storage-size=10 --storage-auto-increase --async --quiet --format='value(name)')"
  info "creation started (operation $SQL_OP)"
fi

log "Artifact Registry, service accounts, buckets"
gcloud artifacts repositories describe suffai --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create suffai --repository-format=docker --location="$REGION" --project="$PROJECT" >/dev/null
ensure_sa suffai-cc "SuffAI Command Center"
ensure_sa suffai-extraction "SuffAI extraction"
ensure_sa suffai-similarity "SuffAI similarity"
ensure_sa pubsub-push "Pub/Sub push to Cloud Run"
# New projects build with the Compute Engine default account, which has no roles by default.
grant_project "serviceAccount:$COMPUTE_SA" roles/cloudbuild.builds.builder
ensure_bucket "$GCS_IMAGE_BUCKET"
ensure_bucket "$RAW_BUCKET"

log "BigQuery $DATASET: dataset and tables"
bq --project_id="$PROJECT" --location="$REGION" --headless show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1 \
  || bq --project_id="$PROJECT" --location="$REGION" --headless mk --dataset "${PROJECT}:${DATASET}" >/dev/null
bq --project_id="$PROJECT" --location="$REGION" --headless --quiet query --use_legacy_sql=false \
  < command_center/scripts/create_bq_tables.sql >/dev/null
bq --project_id="$PROJECT" --location="$REGION" --headless --quiet query --use_legacy_sql=false \
  < similarity/scripts/create_bq_similarity_tables.sql >/dev/null

log "Python environment for the loaders and backfills ($VENV)"
[[ -x "$VENV/bin/python3" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r command_center/requirements.txt
export PATH="$VENV/bin:$PATH"

log "BigQuery data: sample CSVs and logins (only into empty tables)"
if [[ "$LOAD_SAMPLE_DATA" == "1" ]]; then
  N="$(bq_count customers)"
  if [[ "$N" == "0" ]]; then
    ./command_center/scripts/load_to_bq.sh "$PROJECT"
  else
    info "customers already has $N rows; sample data not reloaded"
  fi
fi
N="$(bq_count login_cred_db)"
if [[ "$N" == "0" ]]; then
  ./command_center/scripts/load_login_creds_bq.sh "$PROJECT"
else
  info "login_cred_db already has $N rows; not replaced (it may hold logins created in Admin)"
fi

log "BigQuery migrations (unified tables, consistent dataset, retail staff logins, catalog match, campaign fees)"
./command_center/scripts/migrate_bq_unified.sh "$PROJECT" "$DATASET" >/dev/null
./command_center/scripts/migrate_bq_consistency.sh "$PROJECT" "$DATASET" >/dev/null
./command_center/scripts/migrate_bq_retail.sh "$PROJECT" "$DATASET" >/dev/null
./command_center/scripts/migrate_bq_catalog_match.sh "$PROJECT" "$DATASET" >/dev/null
./command_center/scripts/migrate_bq_campaign_fee.sh "$PROJECT" "$DATASET" >/dev/null
info "garment_items view, pin_areas, ref_values, login_cred_db.location_code, catalog match and fee tables ready"

if [[ "$(bq_count customers)" != "0" ]]; then
  log "RFM segments and PIN areas"
  python3 command_center/scripts/recompute_rfm.py --project "$PROJECT" --apply
  if [[ "$BACKFILL_PIN_AREAS" == "1" ]]; then
    ENABLE_VERTEX=true GEO_GROUNDING_LOCATION=global GEO_GROUNDING_MODEL="$GEMINI_MODEL" GEO_CITY="$GEO_CITY" \
      python3 command_center/scripts/backfill_pin_areas.py --project "$PROJECT" --apply
  else
    info "PIN area backfill skipped (BACKFILL_PIN_AREAS=0); Geo looks PINs up when opened"
  fi
  info "Reconcile dry run (nothing is deleted):"
  python3 command_center/scripts/reconcile_data.py --project "$PROJECT" --skip-cloud-sql || true
fi

log "Build images with Cloud Build (private files are excluded by .gcloudignore)"
if gcloud meta list-files-for-upload "$ROOT" 2>/dev/null \
    | grep -E 'data_offline|login_creds_plaintext|brand_logins_summary|command_center_data/sample|\.zip$|(^|/)\.env$'; then
  die "The files above would be uploaded to Cloud Build. Fix .gcloudignore first."
fi
for cfg in worker similarity command-center; do
  info "building deploy/cloudbuild-$cfg.yaml"
  gcloud builds submit "$ROOT" --project="$PROJECT" --config="$ROOT/deploy/cloudbuild-$cfg.yaml" \
    --substitutions="_REGION=$REGION" --quiet
done

log "Wait for Cloud SQL"
if [[ -n "$SQL_OP" ]]; then
  gcloud sql operations wait "$SQL_OP" --project="$PROJECT" --timeout=unlimited >/dev/null
fi
for _ in $(seq 1 90); do
  STATE="$(gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT" --format='value(state)' 2>/dev/null || true)"
  [[ "$STATE" == "RUNNABLE" ]] && break
  sleep 20
done
[[ "$STATE" == "RUNNABLE" ]] || die "Cloud SQL $SQL_INSTANCE is $STATE after 30 minutes. Check the Console (an org policy may block public IP)."

log "Cloud SQL database, user and DATABASE_URL secret"
gcloud sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT" >/dev/null
NEW_SECRET=0
if gcloud secrets describe "$DB_SECRET" --project="$PROJECT" >/dev/null 2>&1; then
  DB_URL="$(gcloud secrets versions access latest --secret="$DB_SECRET" --project="$PROJECT")"
  DB_PASSWORD="$(printf '%s' "$DB_URL" | sed -nE 's#^postgresql://[^:]+:([^@]+)@.*#\1#p')"
  [[ -n "$DB_PASSWORD" ]] || die "Secret $DB_SECRET is not a postgresql://USER:PASSWORD@/... URL."
else
  NEW_SECRET=1
  DB_PASSWORD="$(openssl rand -hex 24)"
  DB_URL="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONN}"
fi
if gcloud sql users list --instance="$SQL_INSTANCE" --project="$PROJECT" --format='value(name)' | grep -qx "$DB_USER"; then
  if [[ "$NEW_SECRET" == "1" ]]; then
    gcloud sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --project="$PROJECT" --password="$DB_PASSWORD" >/dev/null
  fi
else
  gcloud sql users create "$DB_USER" --instance="$SQL_INSTANCE" --project="$PROJECT" --password="$DB_PASSWORD" >/dev/null
fi
if [[ "$NEW_SECRET" == "1" ]]; then
  printf '%s' "$DB_URL" | gcloud secrets create "$DB_SECRET" --project="$PROJECT" \
    --replication-policy=automatic --data-file=- >/dev/null
fi
unset DB_URL DB_PASSWORD
info "user $DB_USER on database $DB_NAME; DATABASE_URL stored in Secret Manager ($DB_SECRET)"

log "Cloud SQL migrations (gcloud sql import as $DB_USER)"
MIG_FILE="$(mktemp)"
for f in db/migrations/0*.sql; do
  printf -- '-- ===== %s =====\n' "$(basename "$f")"
  cat "$f"
  printf '\n'
done > "$MIG_FILE"
gcloud storage cp "$MIG_FILE" "gs://${RAW_BUCKET}/migrations/cloudsql_all.sql" --project="$PROJECT" >/dev/null
rm -f "$MIG_FILE"
SQL_AGENT="$(gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT" --format='value(serviceAccountEmailAddress)')"
retry gcloud storage buckets add-iam-policy-binding "gs://${RAW_BUCKET}" --project="$PROJECT" \
  --member="serviceAccount:${SQL_AGENT}" --role=roles/storage.objectViewer >/dev/null
# Retried: the bucket grant above can take a minute to reach Cloud SQL; every migration is idempotent.
retry gcloud sql import sql "$SQL_INSTANCE" "gs://${RAW_BUCKET}/migrations/cloudsql_all.sql" \
  --database="$DB_NAME" --user="$DB_USER" --project="$PROJECT" --quiet

log "IAM for the service accounts"
for sa in "$CC_SA" "$EX_SA" "$SIM_SA"; do
  for role in roles/bigquery.jobUser roles/aiplatform.user roles/cloudsql.client; do
    grant_project "serviceAccount:$sa" "$role"
  done
  grant_dataset_editor "$sa"
  retry gcloud secrets add-iam-policy-binding "$DB_SECRET" --project="$PROJECT" \
    --member="serviceAccount:$sa" --role=roles/secretmanager.secretAccessor >/dev/null
done
retry gcloud storage buckets add-iam-policy-binding "gs://$GCS_IMAGE_BUCKET" --project="$PROJECT" \
  --member="serviceAccount:$CC_SA" --role=roles/storage.objectAdmin >/dev/null
for sa in "$EX_SA" "$SIM_SA"; do
  retry gcloud storage buckets add-iam-policy-binding "gs://$GCS_IMAGE_BUCKET" --project="$PROJECT" \
    --member="serviceAccount:$sa" --role=roles/storage.objectViewer >/dev/null
done
# Signed image URLs: Command Center signs as its own account (IAM signBlob).
retry gcloud iam service-accounts add-iam-policy-binding "$CC_SA" --project="$PROJECT" \
  --member="serviceAccount:$CC_SA" --role=roles/iam.serviceAccountTokenCreator >/dev/null
info "BigQuery Job User + Data Editor, Vertex AI User, Cloud SQL Client, secret access; bucket roles"

log "Deploy suffai-extraction"
gcloud run deploy suffai-extraction --project="$PROJECT" --region="$REGION" \
  --image="$IMG_BASE/suffai-extraction:latest" --service-account="$EX_SA" \
  --no-allow-unauthenticated --memory=1Gi --cpu=1 --timeout=300 \
  --set-cloudsql-instances="$CONN" --update-secrets="DATABASE_URL=${DB_SECRET}:latest" \
  --update-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},USE_BQ=true,BQ_DATASET=${DATASET},ENABLE_VERTEX=true,VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_MODEL=${GEMINI_MODEL},PUBSUB_EXTRACTED_TOPIC=${EXTRACTED_TOPIC}" \
  --quiet
EXTRACTION_URL="$(service_url suffai-extraction)"

log "Deploy suffai-similarity"
gcloud run deploy suffai-similarity --project="$PROJECT" --region="$REGION" \
  --image="$IMG_BASE/suffai-similarity:latest" --service-account="$SIM_SA" \
  --no-allow-unauthenticated --memory=1Gi --cpu=1 --timeout=300 \
  --set-cloudsql-instances="$CONN" --update-secrets="DATABASE_URL=${DB_SECRET}:latest" \
  --update-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},ENABLE_VERTEX=true,SIMILARITY_THRESHOLD=0.92,VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_EMBED_MODEL=multimodalembedding@001,USE_BQ=true,BQ_DATASET=${DATASET},CORS_ORIGINS=*" \
  --quiet
SIMILARITY_URL="$(service_url suffai-similarity)"

log "Who may call extraction and similarity (Pub/Sub push + Command Center)"
for svc in suffai-extraction suffai-similarity; do
  for sa in "$PUSH_SA" "$CC_SA"; do
    retry gcloud run services add-iam-policy-binding "$svc" --region="$REGION" --project="$PROJECT" \
      --member="serviceAccount:$sa" --role=roles/run.invoker >/dev/null
  done
done

log "Pub/Sub: upload and extracted topics, dead-letter topic, push subscriptions, bucket notification"
ensure_topic "$TOPIC"
ensure_topic "$EXTRACTED_TOPIC"
ensure_topic "$DLQ_TOPIC"
gcloud pubsub subscriptions describe "${DLQ_TOPIC}-sub" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create "${DLQ_TOPIC}-sub" --topic="$DLQ_TOPIC" \
       --message-retention-duration=7d --project="$PROJECT" >/dev/null
grant_project "serviceAccount:$PUBSUB_AGENT" roles/iam.serviceAccountTokenCreator
retry gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" --project="$PROJECT" \
  --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.publisher >/dev/null
retry gcloud pubsub topics add-iam-policy-binding "$EXTRACTED_TOPIC" --project="$PROJECT" \
  --member="serviceAccount:$EX_SA" --role=roles/pubsub.publisher >/dev/null
ensure_push_sub "$EXTRACT_SUB" "$EXTRACTION_URL"
ensure_push_sub "$SIM_SUB" "$SIMILARITY_URL" "$EXTRACTED_TOPIC"
if gcloud pubsub subscriptions describe "$LEGACY_SIM_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud pubsub subscriptions delete "$LEGACY_SIM_SUB" --project="$PROJECT" --quiet >/dev/null
  info "deleted $LEGACY_SIM_SUB (similarity no longer reads the upload topic)"
fi
grant_gcs_publisher "$TOPIC"
if gcloud storage buckets notifications list "gs://$GCS_IMAGE_BUCKET" --format=json 2>/dev/null \
    | grep -q "/topics/${TOPIC}\""; then
  info "bucket notification exists"
else
  gcloud storage buckets notifications create "gs://$GCS_IMAGE_BUCKET" \
    --topic="projects/${PROJECT}/topics/${TOPIC}" --event-types=OBJECT_FINALIZE --payload-format=json >/dev/null
  info "gs://$GCS_IMAGE_BUCKET uploads -> $TOPIC -> extraction -> $EXTRACTED_TOPIC -> similarity"
fi

log "Deploy suffai-command-center"
# One instance: login sessions, AURA state and staged uploads live in memory. CPU always on so
# background work (thread-mode jobs, cache refresh) is not paused between requests.
gcloud run deploy suffai-command-center --project="$PROJECT" --region="$REGION" \
  --image="$IMG_BASE/suffai-command-center:latest" --service-account="$CC_SA" \
  --allow-unauthenticated --memory=1Gi --cpu=1 --timeout=300 \
  --max-instances=1 --session-affinity --no-cpu-throttling \
  --set-cloudsql-instances="$CONN" --update-secrets="DATABASE_URL=${DB_SECRET}:latest" \
  --update-env-vars="USE_BQ=true,BQ_DATASET=${DATASET},GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},CORS_ORIGINS=*,ENABLE_VERTEX=true,VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_CHAT_MODEL=${GEMINI_MODEL},GEO_GROUNDING_MODEL=${GEMINI_MODEL},GEO_GROUNDING_LOCATION=global,GEO_CITY=${GEO_CITY},EXTRACTION_URL=${EXTRACTION_URL},SIMILARITY_URL=${SIMILARITY_URL}" \
  --quiet
CC_URL="$(service_url suffai-command-center)"

if [[ "$ENABLE_CLOUD_TASKS" == "1" ]]; then
  log "Cloud Tasks queue for Ads Lab ranking jobs"
  ./deploy/setup_cloud_tasks.sh
fi

log "Verify"
FAIL=0
CODE="$(curl -sS -o /tmp/suffai_cc_health.json -w '%{http_code}' "$CC_URL/health" || echo 000)"
if [[ "$CODE" == "200" ]]; then
  info "PASS  Command Center /health"
  python3 - /tmp/suffai_cc_health.json <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))
for k in ("use_bq", "database_url", "enable_vertex", "extraction_url", "similarity_url", "admin_unique_garments"):
    print(f"          {k}: {h.get(k)}")
PY
else
  FAIL=1
  info "FAIL  Command Center /health -> HTTP $CODE"
  if [[ "$CODE" == "403" ]]; then
    info "      403: an organization policy may block public (allUsers) access to Cloud Run."
  fi
fi
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
for svc in suffai-extraction suffai-similarity; do
  URL="$(service_url "$svc")"
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$URL/health" || echo 000)"
  if [[ "$CODE" == "200" ]]; then info "PASS  $svc /health"; else FAIL=1; info "FAIL  $svc /health -> HTTP $CODE"; fi
done

RESULT="Done"
if [[ "$FAIL" -ne 0 ]]; then RESULT="Done, with failed checks above"; fi
cat <<EOF

==> $RESULT

Command Center:  $CC_URL/login
Logins:          the accounts in command_center_data/auth/login_cred_db.csv; passwords are in your offline
                 login_creds_plaintext.csv (never uploaded)
Customer photos: create the customer and order in Admin, then upload
                 gs://$GCS_IMAGE_BUCKET/garments/<customer_id>_<order_id>_<item_id>.jpg
DATABASE_URL:    Secret Manager secret $DB_SECRET, mounted on all three services
Dead letters:    ${DLQ_TOPIC}-sub (photos that failed 5 times)

Later redeploys: ./deploy/deploy_extraction.sh, ./deploy/04_integrate_command_center.sh, ./deploy/deploy_similarity.sh
                 (export GOOGLE_CLOUD_PROJECT and GCP_REGION only; do NOT export DATABASE_URL here, it is a secret)
Reconcile:       python3 command_center/scripts/reconcile_data.py --project $PROJECT --skip-cloud-sql [--apply]
Running cost:    Cloud SQL ($SQL_TIER) runs all the time; Command Center is billed while its instance is up.
Remove it all:   gcloud projects delete $PROJECT
EOF
exit "$FAIL"
