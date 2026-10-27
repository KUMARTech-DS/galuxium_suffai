#!/usr/bin/env bash
# Step 5 — Verify similarity deploy + extraction Pub/Sub + command-center Admin integration.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT}"
REGION="${GCP_REGION:-asia-south1}"
TOPIC="${PUBSUB_EXTRACTED_TOPIC:-garment-image-extracted}"
SIM_SUB="${PUBSUB_SIMILARITY_SUBSCRIPTION:-garment-image-extracted-similarity-push}"
FAIL=0

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=1; }
info() { echo "  INFO  $*"; }

echo "==> [5/5] Verify similarity E2E"
echo "    Project=$PROJECT Region=$REGION"
echo ""

# --- Cloud Run services ---
echo "-- Cloud Run --"
SIM_URL="$(gcloud run services describe suffai-similarity --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)"
CC_URL="$(gcloud run services describe suffai-command-center --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)"
EX_URL="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" --format='value(status.url)' 2>/dev/null || true)"

[[ -n "$SIM_URL" ]] && pass "suffai-similarity = $SIM_URL" || fail "suffai-similarity missing"
[[ -n "$CC_URL" ]] && pass "suffai-command-center = $CC_URL" || fail "suffai-command-center missing"
[[ -n "$EX_URL" ]] && pass "suffai-extraction = $EX_URL" || fail "suffai-extraction missing (needed for full stack)"

# --- Identity token for private similarity ---
echo ""
echo "-- Similarity health --"
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  fail "Could not get identity token (gcloud auth login / application-default)"
else
  if [[ -n "$SIM_URL" ]]; then
    CODE="$(curl -sS -o /tmp/sim_health.json -w '%{http_code}' \
      -H "Authorization: Bearer $TOKEN" "$SIM_URL/health" || echo 000)"
    if [[ "$CODE" == "200" ]]; then
      pass "GET similarity /health → 200"
      cat /tmp/sim_health.json | head -c 400; echo
    else
      fail "GET similarity /health → HTTP $CODE (check IAM / audience)"
    fi
  fi
fi

# --- Command center public health + Admin unique garments route ---
echo ""
echo "-- Command Center --"
if [[ -n "$CC_URL" ]]; then
  CODE="$(curl -sS -o /tmp/cc_health.json -w '%{http_code}' "$CC_URL/health" || echo 000)"
  if [[ "$CODE" == "200" ]]; then
    pass "GET command-center /health → 200"
    if grep -q '"similarity_url": true' /tmp/cc_health.json 2>/dev/null || grep -q 'similarity_url.: true' /tmp/cc_health.json; then
      pass "command-center has SIMILARITY_URL configured"
    else
      fail "command-center health shows similarity_url false — re-run ./deploy/04_integrate_command_center.sh"
    fi
  else
    fail "command-center /health → HTTP $CODE"
  fi

  # Unique garments now read BigQuery garment_items in the Command Center; the route needs an admin login.
  CODE="$(curl -sS -o /tmp/cc_admin.json -w '%{http_code}' "$CC_URL/v1/admin/unique-summary" || echo 000)"
  if [[ "$CODE" == "200" || "$CODE" == "401" ]]; then
    pass "GET command-center /v1/admin/unique-summary → $CODE (route up; admin login required)"
  else
    fail "Admin unique-summary HTTP $CODE — check the Command Center BigQuery access (garment_items view)"
    head -c 300 /tmp/cc_admin.json 2>/dev/null; echo
  fi

  CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$CC_URL/admin.html" || echo 000)"
  [[ "$CODE" == "200" ]] && pass "admin.html served" || fail "admin.html HTTP $CODE"
fi

# --- Pub/Sub ---
echo ""
echo "-- Pub/Sub --"
if gcloud pubsub topics describe "$TOPIC" --project="$PROJECT" >/dev/null 2>&1; then
  pass "topic $TOPIC exists"
else
  fail "topic $TOPIC missing"
fi
if gcloud pubsub subscriptions describe "$SIM_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  PUSH="$(gcloud pubsub subscriptions describe "$SIM_SUB" --project="$PROJECT" --format='value(pushConfig.pushEndpoint)')"
  pass "sub $SIM_SUB push=$PUSH"
else
  fail "sub $SIM_SUB missing"
fi

# --- Env consistency (GCS) ---
echo ""
echo "-- Shared GCS env (best-effort) --"
if [[ -n "$SIM_URL" && -n "$EX_URL" ]]; then
  SIM_GCS="$(gcloud run services describe suffai-similarity --region="$REGION" --project="$PROJECT" \
    --format='yaml(spec.template.spec.containers[0].env)' 2>/dev/null | grep -A1 GCS_IMAGE_BUCKET | tail -1 | tr -d ' ' || true)"
  EX_GCS="$(gcloud run services describe suffai-extraction --region="$REGION" --project="$PROJECT" \
    --format='yaml(spec.template.spec.containers[0].env)' 2>/dev/null | grep -A1 GCS_IMAGE_BUCKET | tail -1 | tr -d ' ' || true)"
  info "similarity GCS line: ${SIM_GCS:-unknown}"
  info "extraction GCS line: ${EX_GCS:-unknown}"
fi

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  echo "==> ALL CHECKS PASSED"
  echo "Manual UI: open $CC_URL/login  (brand / brand123) → $CC_URL/admin"
  echo "Optional smoke publish (the object must be named CUSTOMER_ORDER_ITEM.jpg for an existing order of that customer;"
  echo "  the PIN comes from BigQuery customers, other names are acknowledged as ignored or unlinked):"
  echo "  gcloud pubsub topics publish $TOPIC --message='{\"gcs_object_key\":\"garments/CUSTOMER_ORDER_1.jpg\",\"gcs_bucket\":\"BUCKET\"}'"
  exit 0
else
  echo "==> SOME CHECKS FAILED (see FAIL lines above)"
  exit 1
fi
