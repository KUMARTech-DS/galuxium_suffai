#!/usr/bin/env bash
# End-to-end: prepare DB → deploy similarity → Pub/Sub w/ extraction → command center → verify.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT (export GOOGLE_CLOUD_PROJECT=…)}"
REGION="${GCP_REGION:-asia-south1}"

echo "=============================================="
echo " SuffAI Similarity — E2E deploy + integrate"
echo " Project=$PROJECT  Region=$REGION"
echo "=============================================="
echo ""
echo "Prerequisites:"
echo "  - gcloud authenticated, billing on, Docker running"
echo "  - suffai-extraction already deployed (recommended)"
echo "  - DATABASE_URL (+ CLOUD_SQL_INSTANCE) for Cloud SQL"
echo ""

"$ROOT/deploy/01_prepare_similarity_db.sh"
echo ""
"$ROOT/deploy/02_deploy_similarity.sh"
echo ""
"$ROOT/deploy/03_integrate_extraction_pubsub.sh"
echo ""
"$ROOT/deploy/04_integrate_command_center.sh"
echo ""
"$ROOT/deploy/05_verify_similarity_e2e.sh"

echo ""
echo "E2E complete. Guide: docs/DEPLOY_SIMILARITY_E2E.md"
