#!/usr/bin/env bash
# End-to-end SuffAI deploy from Cloud Shell or a local terminal (VS Code). No Console clicks.
# Needs only an existing project ID; it creates every API, account, bucket, database and service.
#
#   ./deploy/deploy_all.sh my-project-id                  # everything, asks once before changing anything
#   ./deploy/deploy_all.sh my-project-id --dry-run        # print the commands, change nothing
#   ./deploy/deploy_all.sh my-project-id --only migrate,deploy
#   ./deploy/deploy_all.sh my-project-id --region us-central1 --yes
#
# Options: --region R (default asia-south1), --billing-account ID, --env-file FILE (optional
# overrides, see deploy/deploy.env.example), --only/--skip STEPS, --dry-run, --yes.
# Steps, in order: apis iam storage sql migrate bigquery pubsub build deploy wire jobs verify
# Every step is safe to re-run: it creates what is missing and leaves existing resources in place.
# Guide: docs/DEPLOY_ALL.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ALL_STEPS="apis iam storage sql migrate bigquery pubsub build deploy wire jobs verify"

ENV_FILE=""
PROJECT_ARG=""
ONLY=""
SKIP=""
DRY_RUN=false
ASSUME_YES=false

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ARG="$2"; shift 2 ;;
    --region) export GCP_REGION="$2"; shift 2 ;;
    --billing-account) export BILLING_ACCOUNT="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; [[ -f "$ENV_FILE" ]] || { echo "No such file: $ENV_FILE" >&2; exit 1; }; shift 2 ;;
    --only) ONLY="${2//,/ }"; shift 2 ;;
    --skip) SKIP="${2//,/ }"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --yes|-y) ASSUME_YES=true; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "Unknown option: $1" >&2; usage 1 ;;
    *)
      [[ -z "$PROJECT_ARG" ]] || { echo "Unexpected argument: $1" >&2; usage 1; }
      PROJECT_ARG="$1"; shift ;;
  esac
done

# --- Settings: built-in defaults, overridden by an optional env file, then by shell exports ---
load_env_file() {
  local line key val
  [[ -f "$1" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    val="${val%%#*}"
    val="${val%"${val##*[![:space:]]}"}"
    val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      export "$key=$val"
    fi
  done < "$1"
}
[[ -n "$ENV_FILE" ]] && load_env_file "$ENV_FILE"

# Project: argument, then GOOGLE_CLOUD_PROJECT (Cloud Shell sets it), then the gcloud default.
PROJECT="${PROJECT_ARG:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$PROJECT" || "$PROJECT" == "your-project-id" ]] && command -v gcloud >/dev/null 2>&1; then
  PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [[ -z "$PROJECT" && -t 0 ]]; then
  printf 'GCP project ID to deploy into: '
  read -r PROJECT
fi
[[ -n "$PROJECT" && "$PROJECT" != "your-project-id" ]] \
  || { echo "ERROR: No project. Run: ./deploy/deploy_all.sh <project-id>" >&2; exit 1; }
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
  || { echo "ERROR: '$PROJECT' is not a project ID (lowercase letters, digits, hyphens; 6-30 chars). Use the ID, not the display name." >&2; exit 1; }
REGION="${GCP_REGION:-asia-south1}"
BUILD_MODE="${BUILD_MODE:-cloudbuild}"
GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${PROJECT}-suffai-garment-images}"
GCS_RAW_BUCKET="${GCS_RAW_BUCKET:-${PROJECT}-cmd-center-raw}"
SQL_INSTANCE="${SQL_INSTANCE:-suffai-ops}"
SQL_DATABASE="${SQL_DATABASE:-suffai}"
SQL_USER="${SQL_USER:-suffai}"
SQL_TIER="${SQL_TIER:-db-g1-small}"
SQL_VERSION="${SQL_VERSION:-POSTGRES_15}"
DB_PASSWORD="${DB_PASSWORD:-}"
RESET_DB_PASSWORD="${RESET_DB_PASSWORD:-false}"
PROXY_PORT="${PROXY_PORT:-5433}"
CLOUD_SQL_PROXY_VERSION="${CLOUD_SQL_PROXY_VERSION:-v2.14.1}"
BQ_DATASET="${BQ_DATASET:-suffai_ops}"
LOAD_SAMPLE_DATA="${LOAD_SAMPLE_DATA:-true}"
FORCE_SAMPLE_DATA="${FORCE_SAMPLE_DATA:-false}"
LOAD_LOGINS="${LOAD_LOGINS:-true}"
FORCE_LOGINS="${FORCE_LOGINS:-false}"
BQ_ANALYTICS="${BQ_ANALYTICS:-true}"
REFRESH_METRICS="${REFRESH_METRICS:-false}"
ENABLE_VERTEX="${ENABLE_VERTEX:-true}"
VERTEX_LOCATION="${VERTEX_LOCATION:-$REGION}"
VERTEX_CHAT_MODEL="${VERTEX_CHAT_MODEL:-gemini-2.5-flash}"
EXTRACTION_MODEL="${EXTRACTION_MODEL:-gemini-2.0-flash}"
VERTEX_EMBED_MODEL="${VERTEX_EMBED_MODEL:-multimodalembedding@001}"
EMBED_DIMENSION="${EMBED_DIMENSION:-1408}"
SIMILARITY_THRESHOLD="${SIMILARITY_THRESHOLD:-0.92}"
CATALOG_MATCH_THRESHOLD="${CATALOG_MATCH_THRESHOLD:-0.92}"
CATALOG_EMBED_PER_REQUEST="${CATALOG_EMBED_PER_REQUEST:-40}"
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
ENABLE_FEE_FEATURES_JOB="${ENABLE_FEE_FEATURES_JOB:-true}"
FEE_FEATURES_SCHEDULE="${FEE_FEATURES_SCHEDULE:-45 2 * * *}"
SCHEDULER_TIME_ZONE="${SCHEDULER_TIME_ZONE:-Asia/Kolkata}"
GEO_CITY="${GEO_CITY:-Bengaluru}"
BACKFILL_PIN_AREAS="${BACKFILL_PIN_AREAS:-false}"
DEPLOY_API="${DEPLOY_API:-false}"
PUBSUB_IMAGE_TOPIC="${PUBSUB_IMAGE_TOPIC:-garment-image-uploaded}"
PUBSUB_EXTRACTED_TOPIC="${PUBSUB_EXTRACTED_TOPIC:-garment-image-extracted}"
ENABLE_GCS_NOTIFICATION="${ENABLE_GCS_NOTIFICATION:-true}"
GCS_NOTIFICATION_PREFIX="${GCS_NOTIFICATION_PREFIX:-garments/}"
ENABLE_CLOUD_TASKS="${ENABLE_CLOUD_TASKS:-false}"
CREATE_PROJECT="${CREATE_PROJECT:-false}"
PROJECT_NAME="${PROJECT_NAME:-}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
ORG_ID="${ORG_ID:-}"
FOLDER_ID="${FOLDER_ID:-}"

AR_REPO="suffai"
SVC_CC="suffai-command-center"
SVC_EXTRACT="suffai-extraction"
SVC_SIM="suffai-similarity"
SVC_API="suffai-api"
JOB_FEE="suffai-campaign-fee-features"
SCHED_FEE="suffai-campaign-fee-features-daily"
EXTRACT_SUB="${PUBSUB_IMAGE_TOPIC}-push"
SIM_SUB="${PUBSUB_EXTRACTED_TOPIC}-similarity-push"
LEGACY_SIM_SUB="${PUBSUB_IMAGE_TOPIC}-similarity-push"
SECRET_DB_PASSWORD="suffai-db-password"
SECRET_DB_URL="suffai-database-url"
SECRET_JWT="suffai-jwt-secret"
TAG_FILE="$ROOT/deploy/.last_image_tag"
VENV="$ROOT/.venv-deploy"

SA_CC="suffai-command-center@${PROJECT}.iam.gserviceaccount.com"
SA_EXTRACT="suffai-extraction@${PROJECT}.iam.gserviceaccount.com"
SA_SIM="suffai-similarity@${PROJECT}.iam.gserviceaccount.com"
SA_API="suffai-api@${PROJECT}.iam.gserviceaccount.com"
SA_PUSH="pubsub-push@${PROJECT}.iam.gserviceaccount.com"
SA_BUILD="suffai-build@${PROJECT}.iam.gserviceaccount.com"
SA_SCHED="suffai-scheduler@${PROJECT}.iam.gserviceaccount.com"

# --- Helpers ---------------------------------------------------------------------------------
c_bold=$'\033[1m'; c_dim=$'\033[2m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
[[ -t 1 ]] || { c_bold=""; c_dim=""; c_red=""; c_yel=""; c_off=""; }

step() { printf '\n%s==> [%s] %s%s\n' "$c_bold" "$(date +%H:%M:%S)" "$*" "$c_off"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s    WARNING: %s%s\n' "$c_yel" "$*" "$c_off" >&2; }
die() { printf '%sERROR: %s%s\n' "$c_red" "$*" "$c_off" >&2; exit 1; }
is_true() { [[ "$1" == "true" || "$1" == "1" || "$1" == "yes" ]]; }

# Mutating commands go through run so --dry-run prints them instead.
run() {
  if $DRY_RUN; then
    { printf '%s    + ' "$c_dim"; printf '%q ' "$@"; printf '%s\n' "$c_off"; } >&2
  else
    "$@"
  fi
}

# Retries a command. IAM bindings fail for up to a couple of minutes after an account or a
# service agent is created, which happens throughout a first run on a new project.
retry() {
  local n=0
  until run "$@"; do
    n=$((n + 1))
    [[ $n -ge 8 ]] && return 1
    info "Retrying in $((n * 5))s: $1 $2 $3" >&2
    sleep $((n * 5))
  done
}

want() {
  local s="$1"
  if [[ -n "$ONLY" ]]; then
    [[ " $ONLY " == *" $s "* ]] || return 1
  fi
  [[ " $SKIP " == *" $s "* ]] && return 1
  return 0
}

svc_url() {
  gcloud run services describe "$1" --region="$REGION" --project="$PROJECT" \
    --format='value(status.url)' 2>/dev/null || true
}

svc_exists() {
  gcloud run services describe "$1" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1
}

project_role() {
  retry gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$1" \
    --role="$2" --condition=None --quiet >/dev/null
}

secret_exists() { gcloud secrets describe "$1" --project="$PROJECT" >/dev/null 2>&1; }

secret_get() {
  gcloud secrets versions access latest --secret="$1" --project="$PROJECT" 2>/dev/null || true
}

# Creates the secret, or adds a version only when the value changed.
secret_put() {
  local name="$1" value="$2"
  if $DRY_RUN; then
    info "+ store secret $name" >&2
    return 0
  fi
  if ! secret_exists "$name"; then
    printf '%s' "$value" | gcloud secrets create "$name" --project="$PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
    info "Created secret $name"
  elif [[ "$(secret_get "$name")" != "$value" ]]; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --project="$PROJECT" --data-file=- >/dev/null
    info "Updated secret $name"
  fi
}

secret_access() {
  retry gcloud secrets add-iam-policy-binding "$1" --project="$PROJECT" \
    --member="serviceAccount:$2" --role="roles/secretmanager.secretAccessor" >/dev/null
}

sql_conn() {
  local conn
  conn="$(gcloud sql instances describe "$SQL_INSTANCE" --project="$PROJECT" \
    --format='value(connectionName)' 2>/dev/null || true)"
  if [[ -z "$conn" ]]; then
    $DRY_RUN && conn="${PROJECT}:${REGION}:${SQL_INSTANCE}" || die "Cloud SQL instance $SQL_INSTANCE not found. Run the sql step first."
  fi
  printf '%s' "$conn"
}

db_password() {
  if [[ -n "$DB_PASSWORD" ]]; then
    printf '%s' "$DB_PASSWORD"
  else
    secret_get "$SECRET_DB_PASSWORD"
  fi
}

urlencode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

# Python with google-cloud-bigquery, in a private venv so system Python is never modified.
py_bq() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    info "Creating $VENV for the BigQuery loaders"
    python3 -m venv "$VENV"
  fi
  if ! "$VENV/bin/python" -c 'import google.cloud.bigquery' >/dev/null 2>&1; then
    "$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null
    "$VENV/bin/python" -m pip install -q -r "$ROOT/command_center/scripts/requirements.txt"
  fi
  printf '%s' "$VENV/bin/python"
}

# Bills API quota to the target project, not whichever project the local credentials default to.
export GOOGLE_CLOUD_QUOTA_PROJECT="$PROJECT"

need_adc() {
  gcloud auth application-default print-access-token >/dev/null 2>&1 \
    || die "Application Default Credentials missing. Run: gcloud auth application-default login"
}

bq_rows() {
  bq --project_id="$PROJECT" show --format=json "${PROJECT}:${BQ_DATASET}.$1" 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("numRows", "0"))' 2>/dev/null \
    || echo 0
}

bq_sql() {
  if $DRY_RUN; then
    info "+ bq query < ${1#"$ROOT"/} (dataset $BQ_DATASET)" >&2
    return 0
  fi
  sed "s/suffai_ops\./${BQ_DATASET}./g" "$1" \
    | bq --project_id="$PROJECT" --location="$REGION" query --use_legacy_sql=false --quiet >/dev/null
}

# Runs a command whose arguments include a password; --dry-run prints it masked.
run_secret() {
  local secret="$1"; shift
  if $DRY_RUN; then
    local args="$*"
    info "+ ${args//$secret/********}" >&2
  else
    "$@"
  fi
}

# Drops a plain env var so the same name can be mounted from Secret Manager.
drop_plain_env() {
  local svc="$1" key="$2" plain
  svc_exists "$svc" || return 0
  plain="$(gcloud run services describe "$svc" --region="$REGION" --project="$PROJECT" --format=json 2>/dev/null \
    | python3 -c '
import json, sys
key = sys.argv[1]
env = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])
print(any(e.get("name") == key and "value" in e for e in env))' "$key" 2>/dev/null || echo False)"
  if [[ "$plain" == "True" ]]; then
    info "$svc: moving $key from a plain env var to Secret Manager"
    run gcloud run services update "$svc" --region="$REGION" --project="$PROJECT" \
      --remove-env-vars="$key" --quiet >/dev/null
  fi
}

# deploy_service NAME IMAGE SERVICE_ACCOUNT public|private MEMORY ENV_VARS SECRETS [EXTRA_FLAGS...]
deploy_service() {
  local name="$1" image="$2" sa="$3" access="$4" memory="$5" env="$6" secrets="$7" key
  shift 7
  local auth_flag="--no-allow-unauthenticated"
  [[ "$access" == "public" ]] && auth_flag="--allow-unauthenticated"
  for key in $(printf '%s' "$secrets" | tr ',' '\n' | cut -d= -f1); do
    drop_plain_env "$name" "$key"
  done
  info "Deploying $name ($image)"
  run gcloud run deploy "$name" \
    --image="$image" \
    --region="$REGION" \
    --project="$PROJECT" \
    --platform=managed \
    --service-account="$sa" \
    "$auth_flag" \
    --memory="$memory" \
    --cpu=1 \
    --timeout=600 \
    --add-cloudsql-instances="$SQL_CONN" \
    --update-env-vars="$env" \
    --update-secrets="$secrets" \
    "$@" \
    --quiet
}

ensure_topic() {
  gcloud pubsub topics describe "$1" --project="$PROJECT" >/dev/null 2>&1 \
    || run gcloud pubsub topics create "$1" --project="$PROJECT" >/dev/null
}

# ensure_push_sub NAME TOPIC URL DLQ_TOPIC
ensure_push_sub() {
  local name="$1" topic="$2" url="$3" dlq="$4"
  local common=(
    --push-endpoint="${url}/"
    --push-auth-service-account="$SA_PUSH"
    --push-auth-token-audience="$url"
    --ack-deadline=600
    --dead-letter-topic="projects/${PROJECT}/topics/${dlq}"
    --max-delivery-attempts=5
    --min-retry-delay=10s
    --max-retry-delay=600s
    --project="$PROJECT"
  )
  if gcloud pubsub subscriptions describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    run gcloud pubsub subscriptions update "$name" "${common[@]}" >/dev/null
  else
    run gcloud pubsub subscriptions create "$name" --topic="$topic" "${common[@]}" >/dev/null
  fi
  retry gcloud pubsub subscriptions add-iam-policy-binding "$name" --project="$PROJECT" \
    --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.subscriber" >/dev/null
  info "$topic -> $name -> ${url}/"
}

ensure_pull_sub() {
  gcloud pubsub subscriptions describe "$1" --project="$PROJECT" >/dev/null 2>&1 \
    || run gcloud pubsub subscriptions create "$1" --topic="$2" --project="$PROJECT" \
         --message-retention-duration=7d >/dev/null
}

invoker() {
  retry gcloud run services add-iam-policy-binding "$1" --region="$REGION" --project="$PROJECT" \
    --member="serviceAccount:$2" --role="roles/run.invoker" --quiet >/dev/null
}

image_tag() {
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s' "$IMAGE_TAG"
  elif [[ -f "$TAG_FILE" ]]; then
    cat "$TAG_FILE"
  else
    die "No image tag. Run the build step first, or set IMAGE_TAG."
  fi
}

image() { printf '%s-docker.pkg.dev/%s/%s/%s:%s' "$REGION" "$PROJECT" "$AR_REPO" "$1" "$(image_tag)"; }

PROXY_PID=""
cleanup() { [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cloud_sql_proxy_bin() {
  local bin os arch cache
  if command -v cloud-sql-proxy >/dev/null 2>&1; then
    command -v cloud-sql-proxy
    return 0
  fi
  case "$(uname -s)" in Darwin) os=darwin ;; Linux) os=linux ;; *) die "Unsupported OS for cloud-sql-proxy" ;; esac
  case "$(uname -m)" in x86_64|amd64) arch=amd64 ;; arm64|aarch64) arch=arm64 ;; *) die "Unsupported CPU for cloud-sql-proxy" ;; esac
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/suffai"
  bin="$cache/cloud-sql-proxy-${CLOUD_SQL_PROXY_VERSION}"
  if [[ ! -x "$bin" ]]; then
    mkdir -p "$cache"
    info "Downloading cloud-sql-proxy ${CLOUD_SQL_PROXY_VERSION} (${os}/${arch})" >&2
    curl -fsSL -o "$bin" \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/${CLOUD_SQL_PROXY_VERSION}/cloud-sql-proxy.${os}.${arch}"
    chmod +x "$bin"
  fi
  printf '%s' "$bin"
}

psql_q() { PGPASSWORD="$PGPASS" psql "host=127.0.0.1 port=${PROXY_PORT} dbname=${SQL_DATABASE} user=${SQL_USER} sslmode=disable" -v ON_ERROR_STOP=1 -qAt "$@"; }

# SQL that is true when an existing database already has the migration's objects. Used once, to
# fill the ledger for databases migrated before this script existed.
migration_marker() {
  local col="SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name"
  case "$1" in
    001_*) echo "SELECT to_regclass('public.tenants') IS NOT NULL" ;;
    002_*) echo "SELECT to_regclass('public.privacy_query_log') IS NOT NULL" ;;
    003_*) echo "SELECT to_regclass('public.unique_garments') IS NOT NULL" ;;
    004_*) echo "SELECT to_regclass('public.user_wardrobe') IS NOT NULL AND to_regclass('public.user_unique_garments') IS NULL" ;;
    005_*) echo "SELECT to_regclass('public.brand_campaigns') IS NOT NULL" ;;
    006_*) echo "$col = 'brand_campaigns' AND column_name = 'title')" ;;
    007_*) echo "SELECT to_regclass('public.campaign_impact_metrics') IS NOT NULL" ;;
    008_*) echo "$col = 'brand_campaigns' AND column_name = 'campaign_name')" ;;
    009_*) echo "SELECT to_regclass('public.campaign_jobs') IS NOT NULL" ;;
    010_*) echo "SELECT to_regclass('public.geo_store_lookups') IS NOT NULL" ;;
    011_*) echo "$col = 'garment_similarity_events' AND column_name = 'bq_synced_at')" ;;
    012_*) echo "SELECT to_regclass('public.retail_products') IS NOT NULL" ;;
    013_*) echo "SELECT to_regclass('public.retail_product_images') IS NOT NULL" ;;
    014_*) echo "$col = 'brand_campaigns' AND column_name = 'platform_fee_final')" ;;
    015_*) echo "$col = 'garment_instances' AND column_name = 'garment_category')" ;;
    016_*) echo "$col = 'retail_product_images' AND column_name = 'embedded_object_key')" ;;
    *) echo "SELECT false" ;;
  esac
}
# --- 7. Pub/Sub topics -----------------------------------------------------------------------
# if want pubsub; then
#   step "Pub/Sub topics and dead-letter queues"
#   for t in "$PUBSUB_IMAGE_TOPIC" "${PUBSUB_IMAGE_TOPIC}-dlq" "$PUBSUB_EXTRACTED_TOPIC" "${PUBSUB_EXTRACTED_TOPIC}-dlq"; do
#     ensure_topic "$t"
#   done
#   ensure_pull_sub "${PUBSUB_IMAGE_TOPIC}-dlq-pull" "${PUBSUB_IMAGE_TOPIC}-dlq"
#   ensure_pull_sub "${PUBSUB_EXTRACTED_TOPIC}-dlq-pull" "${PUBSUB_EXTRACTED_TOPIC}-dlq"
#   for t in "${PUBSUB_IMAGE_TOPIC}-dlq" "${PUBSUB_EXTRACTED_TOPIC}-dlq"; do
#     retry gcloud pubsub topics add-iam-policy-binding "$t" --project="$PROJECT" \
#       --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.publisher" >/dev/null
#   done
#   retry gcloud pubsub topics add-iam-policy-binding "$PUBSUB_EXTRACTED_TOPIC" --project="$PROJECT" \
#     --member="serviceAccount:${SA_EXTRACT}" --role="roles/pubsub.publisher" >/dev/null
#   is_true "$DEPLOY_API" && retry gcloud pubsub topics add-iam-policy-binding "$PUBSUB_IMAGE_TOPIC" --project="$PROJECT" \
#     --member="serviceAccount:${SA_API}" --role="roles/pubsub.publisher" >/dev/null

#   if is_true "$ENABLE_GCS_NOTIFICATION"; then
#     GCS_AGENT="$(gcloud storage service-agent --project="$PROJECT" 2>/dev/null || true)"
#     [[ -n "$GCS_AGENT" ]] && retry gcloud pubsub topics add-iam-policy-binding "$PUBSUB_IMAGE_TOPIC" --project="$PROJECT" \
#       --member="serviceAccount:${GCS_AGENT}" --role="roles/pubsub.publisher" >/dev/null
#     if gcloud storage buckets notifications list "gs://$GCS_IMAGE_BUCKET" --format=json 2>/dev/null | grep -q "/topics/${PUBSUB_IMAGE_TOPIC}\""; then
#       info "GCS notification to $PUBSUB_IMAGE_TOPIC already exists"
#     else
#       run gcloud storage buckets notifications create "gs://$GCS_IMAGE_BUCKET" \
#         --topic="projects/${PROJECT}/topics/${PUBSUB_IMAGE_TOPIC}" --event-types=OBJECT_FINALIZE \
#         --object-prefix="$GCS_NOTIFICATION_PREFIX" --payload-format=json >/dev/null
#     fi
#   fi
#   info "Topics: $PUBSUB_IMAGE_TOPIC, $PUBSUB_EXTRACTED_TOPIC (each with a -dlq topic and -dlq-pull subscription)"
# fi

# --- 8. Build images -------------------------------------------------------------------------
if want build; then
  step "Build container images ($BUILD_MODE)"
  gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
    || run gcloud artifacts repositories create "$AR_REPO" --repository-format=docker \
         --location="$REGION" --project="$PROJECT" --quiet

  if is_true "$REFRESH_METRICS"; then
    if $DRY_RUN; then
      info "+ rebuild web/metrics.json from BigQuery"
    else
      need_adc
      "$(py_bq)" "$ROOT/command_center/scripts/aggregate_metrics.py" --bq --project "$PROJECT" --dataset "$BQ_DATASET"
    fi
  fi

  TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
  if [[ "$BUILD_MODE" == "docker" ]]; then
    run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet >/dev/null
    build_push() {
      local img="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/$1:${TAG}"
      run docker build --platform=linux/amd64 -f "$ROOT/$2" -t "$img" "$ROOT"
      run docker push "$img"
    }
    build_push suffai-similarity Dockerfile.similarity
    build_push suffai-extraction Dockerfile.worker
    build_push suffai-command-center Dockerfile.command_center
    if is_true "$DEPLOY_API"; then build_push suffai-api Dockerfile.api; fi
  else
    if gcloud meta list-files-for-upload "$ROOT" 2>/dev/null \
        | grep -E 'data_offline|login_creds_plaintext|brand_logins_summary|command_center_data/sample|\.zip$|(^|/)\.env$'; then
      die "The files above would be uploaded to Cloud Build. Fix .gcloudignore first."
    fi
    run gcloud builds submit "$ROOT" --project="$PROJECT" --region="$REGION" \
      --service-account="projects/${PROJECT}/serviceAccounts/${SA_BUILD}" \
      --config="$ROOT/deploy/cloudbuild-all.yaml" \
      --substitutions="_REGION=${REGION},_TAG=${TAG},_BUILD_API=$(is_true "$DEPLOY_API" && echo true || echo false)"
  fi
  $DRY_RUN || printf '%s\n' "$TAG" > "$TAG_FILE"
  IMAGE_TAG="$TAG"
  info "Image tag: $TAG"
fi

# --- 9. Deploy services ----------------------------------------------------------------------
if want deploy; then
  step "Deploy Cloud Run services"
  SQL_CONN="$(sql_conn)"
  DB_SECRET="DATABASE_URL=${SECRET_DB_URL}:latest"

  deploy_service "$SVC_SIM" "$(image suffai-similarity)" "$SA_SIM" private 1Gi \
    "GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},ENABLE_VERTEX=${ENABLE_VERTEX},SIMILARITY_THRESHOLD=${SIMILARITY_THRESHOLD},VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_EMBED_MODEL=${VERTEX_EMBED_MODEL},EMBED_DIMENSION=${EMBED_DIMENSION},USE_BQ=true,BQ_DATASET=${BQ_DATASET},CORS_ORIGINS=*" \
    "$DB_SECRET"
  SIM_URL="$(svc_url "$SVC_SIM")"

  deploy_service "$SVC_EXTRACT" "$(image suffai-extraction)" "$SA_EXTRACT" private 1Gi \
    "GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},USE_BQ=true,BQ_DATASET=${BQ_DATASET},ENABLE_VERTEX=${ENABLE_VERTEX},VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_MODEL=${EXTRACTION_MODEL},PUBSUB_EXTRACTED_TOPIC=${PUBSUB_EXTRACTED_TOPIC},VERTEX_EMBED_MODEL=${VERTEX_EMBED_MODEL},EMBED_DIMENSION=${EMBED_DIMENSION},CATALOG_MATCH_THRESHOLD=${CATALOG_MATCH_THRESHOLD},CATALOG_EMBED_PER_REQUEST=${CATALOG_EMBED_PER_REQUEST}" \
    "$DB_SECRET"
  EXTRACT_URL="$(svc_url "$SVC_EXTRACT")"

  if is_true "$DEPLOY_API"; then
    if [[ -z "$(secret_get "$SECRET_JWT")" ]]; then
      secret_put "$SECRET_JWT" "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    fi
    secret_access "$SECRET_JWT" "$SA_API"
    deploy_service "$SVC_API" "$(image suffai-api)" "$SA_API" public 2Gi \
      "ENVIRONMENT=staging,DEMO_AUTH_ENABLED=true,GOOGLE_CLOUD_PROJECT=${PROJECT},GCP_REGION=${REGION},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},PUBSUB_IMAGE_TOPIC=${PUBSUB_IMAGE_TOPIC},EXTRACTION_WORKER_URL=${EXTRACT_URL}" \
      "${DB_SECRET},JWT_SECRET=${SECRET_JWT}:latest"
  fi

  FEE_ENV="FEE_MODEL_VERSION=${FEE_MODEL_VERSION},FEE_BASE_INR=${FEE_BASE_INR},FEE_PER_1K_AUDIENCE=${FEE_PER_1K_AUDIENCE},FEE_PER_SKU_INR=${FEE_PER_SKU_INR},FEE_PER_PIN_INR=${FEE_PER_PIN_INR},FEE_PER_10K_COMPARISONS_INR=${FEE_PER_10K_COMPARISONS_INR},FEE_PER_PROCESS_MINUTE_INR=${FEE_PER_PROCESS_MINUTE_INR},FEE_PER_EXTRA_CHANNEL_INR=${FEE_PER_EXTRA_CHANNEL_INR},FEE_PER_DAY_INR=${FEE_PER_DAY_INR},FEE_MIN_INR=${FEE_MIN_INR},FEE_MAX_PCT_OF_BUDGET=${FEE_MAX_PCT_OF_BUDGET},FEE_VERTEX_SHADOW=${FEE_VERTEX_SHADOW},VERTEX_FEE_ENDPOINT_ID=${VERTEX_FEE_ENDPOINT_ID}"
  # One instance: login sessions, AURA state and staged uploads live in memory; CPU stays on for
  # background jobs and cache refresh.
  deploy_service "$SVC_CC" "$(image suffai-command-center)" "$SA_CC" public 1Gi \
    "USE_BQ=true,BQ_DATASET=${BQ_DATASET},GOOGLE_CLOUD_PROJECT=${PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},EXTRACTION_URL=${EXTRACT_URL},SIMILARITY_URL=${SIM_URL},ENABLE_VERTEX=${ENABLE_VERTEX},VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_CHAT_MODEL=${VERTEX_CHAT_MODEL},GEO_GROUNDING_MODEL=${VERTEX_CHAT_MODEL},GEO_GROUNDING_LOCATION=global,GEO_CITY=${GEO_CITY},CORS_ORIGINS=*,${FEE_ENV}" \
    "$DB_SECRET" --max-instances=1 --session-affinity --no-cpu-throttling

  if is_true "$DEPLOY_API"; then
    CC_URL="$(svc_url "$SVC_CC")"
    run gcloud run services update "$SVC_API" --region="$REGION" --project="$PROJECT" \
      --update-env-vars="WEB_ORIGIN=${CC_URL},API_PUBLIC_URL=$(svc_url "$SVC_API")" --quiet >/dev/null
  fi
fi

# --- 10. Wire services -----------------------------------------------------------------------
if want wire; then
  step "Wire services (invoker access, push subscriptions)"
  SIM_URL="$(svc_url "$SVC_SIM")"
  EXTRACT_URL="$(svc_url "$SVC_EXTRACT")"
  if [[ -z "$SIM_URL" || -z "$EXTRACT_URL" ]]; then
    $DRY_RUN || die "Extraction or similarity is not deployed. Run the deploy step first."
    SIM_URL="${SIM_URL:-https://${SVC_SIM}-DRYRUN.a.run.app}"
    EXTRACT_URL="${EXTRACT_URL:-https://${SVC_EXTRACT}-DRYRUN.a.run.app}"
  fi
  for svc in "$SVC_EXTRACT" "$SVC_SIM"; do
    invoker "$svc" "$SA_CC"
    invoker "$svc" "$SA_PUSH"
  done
  is_true "$DEPLOY_API" && invoker "$SVC_EXTRACT" "$SA_API"

  ensure_push_sub "$EXTRACT_SUB" "$PUBSUB_IMAGE_TOPIC" "$EXTRACT_URL" "${PUBSUB_IMAGE_TOPIC}-dlq"
  ensure_push_sub "$SIM_SUB" "$PUBSUB_EXTRACTED_TOPIC" "$SIM_URL" "${PUBSUB_EXTRACTED_TOPIC}-dlq"

  # Similarity must run after extraction, never in parallel on the upload topic.
  if gcloud pubsub subscriptions describe "$LEGACY_SIM_SUB" --project="$PROJECT" >/dev/null 2>&1; then
    info "Deleting legacy $LEGACY_SIM_SUB (similarity on the upload topic)"
    run gcloud pubsub subscriptions delete "$LEGACY_SIM_SUB" --project="$PROJECT" --quiet
  fi
  others="$(gcloud pubsub topics list-subscriptions "$PUBSUB_IMAGE_TOPIC" --project="$PROJECT" 2>/dev/null \
    | grep -o 'subscriptions/[A-Za-z0-9._~+%-]*' | sed 's|subscriptions/||' | grep -vx -e "$EXTRACT_SUB" | tr '\n' ' ' || true)"
  if [[ -n "${others// /}" ]]; then
    warn "Other subscriptions on $PUBSUB_IMAGE_TOPIC (check they don't process photos twice): $others"
  fi

  if is_true "$ENABLE_CLOUD_TASKS"; then
    info "Enabling Cloud Tasks for campaign jobs"
    run env GOOGLE_CLOUD_PROJECT="$PROJECT" GCP_REGION="$REGION" bash "$ROOT/deploy/setup_cloud_tasks.sh"
  fi
fi

# --- 11. Scheduled jobs ----------------------------------------------------------------------
if want jobs; then
  step "Scheduled jobs (Cloud Run Jobs + Cloud Scheduler)"
  if ! is_true "$ENABLE_FEE_FEATURES_JOB"; then
    info "Fee features job skipped (ENABLE_FEE_FEATURES_JOB=false)"
  else
    SQL_CONN="$(sql_conn)"
    # Same image as Command Center; the script exports aggregate 1P fee features to BigQuery.
    job_args=(
      --image="$(image suffai-command-center)"
      --region="$REGION" --project="$PROJECT"
      --service-account="$SA_CC"
      --set-cloudsql-instances="$SQL_CONN"
      --set-secrets="DATABASE_URL=${SECRET_DB_URL}:latest"
      --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},BQ_DATASET=${BQ_DATASET}"
      --command=python --args=command_center/scripts/build_campaign_fee_features.py
      --tasks=1 --max-retries=1 --task-timeout=900s --memory=512Mi
      --quiet
    )
    if gcloud run jobs describe "$JOB_FEE" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
      run gcloud run jobs update "$JOB_FEE" "${job_args[@]}" >/dev/null
    else
      run gcloud run jobs create "$JOB_FEE" "${job_args[@]}" >/dev/null
    fi
    retry gcloud run jobs add-iam-policy-binding "$JOB_FEE" --region="$REGION" --project="$PROJECT" \
      --member="serviceAccount:$SA_SCHED" --role="roles/run.invoker" --quiet >/dev/null

    sched_args=(
      --location="$REGION" --project="$PROJECT"
      --schedule="$FEE_FEATURES_SCHEDULE" --time-zone="$SCHEDULER_TIME_ZONE"
      --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB_FEE}:run"
      --http-method=POST
      --oauth-service-account-email="$SA_SCHED"
      --quiet
    )
    if gcloud scheduler jobs describe "$SCHED_FEE" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
      run gcloud scheduler jobs update http "$SCHED_FEE" "${sched_args[@]}" >/dev/null
    else
      run gcloud scheduler jobs create http "$SCHED_FEE" "${sched_args[@]}" >/dev/null
    fi
    info "$SCHED_FEE ($FEE_FEATURES_SCHEDULE $SCHEDULER_TIME_ZONE) -> Cloud Run Job $JOB_FEE"
  fi
fi

# --- 12. Verify ------------------------------------------------------------------------------
if want verify; then
  step "Verify"
  CC_URL="$(svc_url "$SVC_CC")"
  if [[ -z "$CC_URL" ]]; then
    warn "Command Center is not deployed yet."
  elif $DRY_RUN; then
    info "+ curl $CC_URL/health"
  else
    if health="$(curl -fsS --max-time 30 "$CC_URL/health")"; then
      printf '%s' "$health" | python3 -m json.tool | sed 's/^/    /'
    else
      warn "Health check failed: $CC_URL/health"
    fi
  fi
  for sub in "$EXTRACT_SUB" "$SIM_SUB"; do
    endpoint="$(gcloud pubsub subscriptions describe "$sub" --project="$PROJECT" --format='value(pushConfig.pushEndpoint)' 2>/dev/null || true)"
    info "$sub -> ${endpoint:-missing}"
  done
  echo ""
  info "${c_bold}Command Center: ${CC_URL:-not deployed}/login.html${c_off}"
  info "Extraction:     $(svc_url "$SVC_EXTRACT")"
  info "Similarity:     $(svc_url "$SVC_SIM")"
  is_true "$DEPLOY_API" && info "Legacy API:     $(svc_url "$SVC_API")"
  if is_true "$ENABLE_FEE_FEATURES_JOB"; then
    sched="$(gcloud scheduler jobs describe "$SCHED_FEE" --location="$REGION" --project="$PROJECT" --format='value(schedule)' 2>/dev/null || true)"
    if [[ -n "$sched" ]]; then
      info "Fee features:   $JOB_FEE (cron '$sched' $SCHEDULER_TIME_ZONE)"
    else
      info "Fee features:   not scheduled; run --only jobs"
    fi
  fi
  info "Logins:         command_center_data/auth/login_creds_plaintext.csv (offline; never uploaded)"
  info "Dead letters:   ${PUBSUB_IMAGE_TOPIC}-dlq-pull, ${PUBSUB_EXTRACTED_TOPIC}-dlq-pull"
fi

step "Done"
