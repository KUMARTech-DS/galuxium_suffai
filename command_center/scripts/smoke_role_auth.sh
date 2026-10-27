#!/usr/bin/env bash
# Smoke role login + metrics (local CSV or live Cloud Run URL).
#
# Usage:
#   # Local CSV auth (no GCP):
#   AUTH_SOURCE=csv PYTHONPATH=command_center python3 command_center/scripts/smoke_role_auth.py
#
#   # Against deployed CC:
#   CC_URL=https://suffai-command-center-....run.app ./command_center/scripts/smoke_role_auth.sh
#
#   # Load BQ creds then smoke (needs gcloud/bq):
#   ./command_center/scripts/load_login_creds_bq.sh "$GOOGLE_CLOUD_PROJECT"
#   CC_URL=$(gcloud run services describe suffai-command-center --region="${GCP_REGION:-asia-south1}" --format='value(status.url)')
#   CC_URL="$CC_URL" ./command_center/scripts/smoke_role_auth.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
exec python3 command_center/scripts/smoke_role_auth.py "$@"
