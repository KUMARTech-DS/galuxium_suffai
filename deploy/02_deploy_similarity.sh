#!/usr/bin/env bash
# Step 2 — Build & deploy Cloud Run suffai-similarity + Pub/Sub push subscription.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> [2/5] Deploy suffai-similarity"
exec "$ROOT/deploy/deploy_similarity.sh"
