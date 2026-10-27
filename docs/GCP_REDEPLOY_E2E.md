# End-to-end GCP redeploy guide (services already live)

> To do all of this with one command, run `./deploy/deploy_all.sh`. See [DEPLOY_ALL.md](DEPLOY_ALL.md). The manual steps below remain for reference.

Use this when **Cloud Run / GCS / BigQuery / Cloud SQL / Pub/Sub already exist** and you need to ship the current changes (campaign Ads Lab, Style Assistant Feature 2, AURA, brand SKU tables, the unified BigQuery tables and the consistent-data release below).

Canonical folder:

```text
/Users/rahul.kumar/Downloads/AI_CUP/AI_CUP-main_Updated
```

Do **not** deploy from `AI_CUP-main/temp/` (pre–Feature 1 archive) or the older `AI_CUP_latest` copy.

> **Shipping the unified-tables release (customer photos → BigQuery, Admin customers & orders, fresh campaign after launch, AURA New chat)?** Do [Release: unified BigQuery tables](#release-unified-bigquery-tables-admin-customers--orders-fresh-campaigns-aura-new-chat) first — it changes the order of the steps and needs extraction redeployed.
>
> **Then the consistent-data release** (PIN areas from Google Search instead of `pin_population.json`, one spelling per value, RFM recomputed from orders, one garment count, reconcile script): follow [Release: one consistent dataset](#release-one-consistent-dataset-pin-areas-shared-values-rfm-reconcile) after it.
>
> **Then the privacy / SKU values / one count release** (private data kept out of images and uploads, brand SKUs on the shared values, BigQuery as the only garment count, Cloud SQL migration 011): follow [Release: private data out of deploys](#release-private-data-out-of-deploys-shared-sku-values-one-garment-count). Its deploy order is extraction, then Command Center, then similarity.
>
> **Then the one Geo store scorer release** (the map, the brief, the market check and AURA use the server's store scores): redeploy Command Center only, see [Release: one Geo store scorer](#release-one-geo-store-scorer-for-the-map-the-brief-and-aura).

---

## What gets redeployed

| Cloud Run service | Source in repo | Redeploy? | Why |
|-------------------|----------------|-----------|-----|
| `suffai-command-center` | `Dockerfile.command_center` → `command_center/` + `web/` | **Yes (required)** | Campaign API, Style Assistant (`/v1/style-assistant/*`), AURA (`/v1/agent/*`), Admin customers & orders, UI tabs |
| `suffai-similarity` | `Dockerfile.similarity` → `similarity/` | **Yes (required for both releases)** | Links each photo to its customer and order in BigQuery (same rule as extraction) and writes the observation; embeddings index for campaign ranking (`compare-pins`) |
| `suffai-extraction` | `Dockerfile.worker` → `worker/` | **Yes (required for the unified-tables release)** | Customer photos now become garment items in BigQuery `suffai_ops.commodities`; Ads Lab still calls `/v1/extract` |
| Jobs / Scheduler / CDC | `deploy/scheduler.yaml` | **No** for this feature set | Leave as-is |

Retired: `suffai-api` — do not recreate.

---

## Prerequisites (local)

```bash
cd /Users/rahul.kumar/Downloads/AI_CUP/AI_CUP-main_Updated

gcloud auth login
gcloud auth configure-docker asia-south1-docker.pkg.dev

export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID   # replace
export GCP_REGION=asia-south1
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
gcloud config set run/region "$GCP_REGION"

# Docker must be running for local build+push, OR use Cloud Build (below)
```

Confirm live services:

```bash
gcloud run services list --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='table(metadata.name,status.url)'
```

You should see at least `suffai-command-center`, `suffai-extraction`, `suffai-similarity`.

---

## Step 0 — Capture current URLs & env (do this first)

```bash
export EXTRACTION_URL=$(gcloud run services describe suffai-extraction \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')

export SIMILARITY_URL=$(gcloud run services describe suffai-similarity \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')

export CC_URL=$(gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')

echo "EXTRACTION_URL=$EXTRACTION_URL"
echo "SIMILARITY_URL=$SIMILARITY_URL"
echo "CC_URL=$CC_URL"

# Snapshot env (so you can restore DATABASE_URL / secrets after deploy)
gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='yaml(spec.template.spec.containers[0].env)' > /tmp/cc-env-before.yaml

gcloud run services describe suffai-similarity \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='yaml(spec.template.spec.containers[0].env)' > /tmp/sim-env-before.yaml
```

Note Cloud SQL instance connection name if used (e.g. `PROJECT:asia-south1:suffai-ops`):

```bash
gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(spec.template.metadata.annotations[run.googleapis.com/cloudsql-instances])'
```

Export for deploy scripts when needed:

```bash
# Example — use your real values from the snapshot / Secret Manager
# export DATABASE_URL='postgresql://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE'
# export CLOUD_SQL_INSTANCE='PROJECT:REGION:INSTANCE'
export GCS_IMAGE_BUCKET="${GCS_IMAGE_BUCKET:-${GOOGLE_CLOUD_PROJECT}-suffai-garment-images}"
export ENABLE_VERTEX=true
export VERTEX_LOCATION=asia-south1
export VERTEX_CHAT_MODEL=gemini-2.5-flash
```

---

## Step 0b — BigQuery login credentials (role ACL)

Required for `POST /v1/auth/login` (brand / customer / admin tabs). No new secrets — uses existing project ADC / Cloud Run SA for BQ.

```bash
python3 command_center/scripts/generate_login_creds.py
chmod +x command_center/scripts/load_login_creds_bq.sh
./command_center/scripts/load_login_creds_bq.sh "$GOOGLE_CLOUD_PROJECT"
# Keep command_center_data/auth/login_creds_plaintext.csv offline (gitignored).
```

Verify:

```bash
bq query --use_legacy_sql=false \
  "SELECT role, COUNT(*) AS n FROM \`${GOOGLE_CLOUD_PROJECT}.suffai_ops.login_cred_db\` GROUP BY role"
```

---

## Release: unified BigQuery tables, Admin customers & orders, fresh campaigns, AURA New chat

BigQuery `suffai_ops` is now the single source of truth. Customer garment photos named `{customer_id}_{order_id}_{item_id}.jpg` are extracted into `suffai_ops.commodities` (one row per item, keyed on order + item), similarity observations carry the same ids, and the `suffai_ops.garment_items` view marks repeat photos of the same garment so every dashboard counts a garment once. Admin can create customers and orders before any photo exists.

Run these in order, after Step 0 (URLs + env exported). Cloud SQL (Step 1) is unchanged by this release.

### R1. Migrate BigQuery once

```bash
./command_center/scripts/migrate_bq_unified.sh "$GOOGLE_CLOUD_PROJECT"   # optional 2nd arg: dataset (default suffai_ops)
```

It adds `source` / `created_at` to `customers` and `orders`; `item_id`, `gcs_object_key`, `image_status`, `source`, `model_version`, `extracted_at` (and `confidence_score` if missing) to `commodities`; tags the existing rows `source='legacy'` (commodities also `image_status='legacy_no_photo'`); adds `customer_id` / `order_id` / `item_id` to `garment_similarity_observations` (creating it if missing); and creates the `garment_items` view. Safe to re-run.

Verify:

```bash
bq query --use_legacy_sql=false \
  "SELECT source, image_status, COUNT(*) AS n FROM \`${GOOGLE_CLOUD_PROJECT}.suffai_ops.commodities\` GROUP BY 1, 2"
bq query --use_legacy_sql=false \
  "SELECT COUNTIF(is_repeat) AS repeats, COUNT(*) AS items FROM \`${GOOGLE_CLOUD_PROJECT}.suffai_ops.garment_items\`"
```

**Do not reload the CSVs after this.** `command_center/scripts/load_to_bq.sh` replaces whole tables; it now refuses any table that holds Admin-created or pipeline rows. `FORCE=1 ./command_center/scripts/load_to_bq.sh …` overrides it and **deletes** those rows.

### R2. IAM for the extraction service account

Extraction now writes to BigQuery (a lookup of the order, then a `MERGE` into `commodities`):

```bash
PROJECT_NUMBER=$(gcloud projects describe "$GOOGLE_CLOUD_PROJECT" --format='value(projectNumber)')
EXTRACT_SA=$(gcloud run services describe suffai-extraction \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)')
EXTRACT_SA="${EXTRACT_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

bq add-iam-policy-binding \
  --member="serviceAccount:${EXTRACT_SA}" \
  --role="roles/bigquery.dataEditor" \
  "${GOOGLE_CLOUD_PROJECT}:suffai_ops"

gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${EXTRACT_SA}" \
  --role="roles/bigquery.jobUser"
```

If your `bq` version rejects a dataset binding, grant **BigQuery Data Editor** on `suffai_ops` in the Console (BigQuery → `suffai_ops` → Sharing → Permissions). Similarity already needs `roles/bigquery.dataEditor` for its observation rows (Step 2c); if all services share the default Compute SA, one grant covers them.

### R3. Redeploy extraction

```bash
export GCS_IMAGE_BUCKET   # from Step 0
./deploy/deploy_extraction.sh            # USE_CLOUD_BUILD=1 to build with Cloud Build instead of local Docker
```

It deploys only `suffai-extraction` with `USE_BQ=true`, `BQ_DATASET=suffai_ops`, `GOOGLE_CLOUD_PROJECT`, `GCS_IMAGE_BUCKET` via `--update-env-vars` (existing keys such as `DATABASE_URL` / `ENABLE_VERTEX` stay). Do **not** use `deploy/deploy_cloud_run.sh` for this: it also recreates the retired `suffai-api` and overwrites env vars.

### R4. Redeploy similarity

```bash
./deploy/deploy_similarity.sh   # keeps DATABASE_URL / Cloud SQL if exported (Step 0)
```

New observations carry `customer_id`, `order_id`, `item_id`, so `garment_items` can flag repeats.

### R5. Redeploy Command Center + BigQuery write access

```bash
./deploy/04_integrate_command_center.sh

CC_SA=$(gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)')
CC_SA="${CC_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

bq add-iam-policy-binding \
  --member="serviceAccount:${CC_SA}" \
  --role="roles/bigquery.dataEditor" \
  "${GOOGLE_CLOUD_PROJECT}:suffai_ops"
```

Admin's **Customers & Orders** tab inserts into `suffai_ops.customers` / `orders` (Data Viewer is no longer enough). The tab shows the full `gs://` upload path from CC's `GCS_IMAGE_BUCKET`, which the deploy script sets; it must be the bucket whose notifications feed extraction.

New endpoints (all admin-only except the AURA one):

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/admin/records/options` | Form choices (genders, services, PINs), the segment new customers start in, photo name pattern, upload prefix |
| `POST /v1/admin/customers` | Create a customer (`source='admin'`) |
| `POST /v1/admin/orders` | Create an order with its expected number of clothes (no items yet) |
| `GET /v1/admin/orders?customer_id=&pending=true` | Orders with "photos received / expected" |
| `POST /v1/agent/reset` | AURA **New chat** (brand / admin): clears the conversation, open forms and pending confirmations; campaigns, ranking jobs and attached images stay |

### R6. Upload customer photos (after the order exists)

1. Admin → **Customers & Orders**: create the customer (or search an existing one), then **Add Order** with the number of clothes.
2. Copy the photo name from the orders table and upload one photo per garment, replacing `<item_id>` with `1`, `2`, `3`…:

```bash
gcloud storage cp ./shirt.jpg \
  "gs://${GCS_IMAGE_BUCKET}/garments/${CUSTOMER_ID}_${ORDER_ID}_1.jpg"
```

Rules: `.jpg`, `.jpeg` or `.png`; customer and order ids cannot contain `_` (Admin ids are 24 hex characters); `brands/` and `demo/` uploads are never treated as customer photos. A photo whose order doesn't exist (or belongs to another customer) is logged as `unlinked` and skipped — create the order, then upload the same file again.

Check the result:

```bash
bq query --use_legacy_sql=false \
  "SELECT item_id, name, image_status, source, extracted_at FROM \`${GOOGLE_CLOUD_PROJECT}.suffai_ops.commodities\` WHERE order_id = '${ORDER_ID}' ORDER BY item_id"
```

Admin-created customers and orders show on every tab at once (each create clears the caches). Items added by the photo pipeline show within about 30 s: the Command Center checks the BigQuery tables' last-modified time every `BQ_FRESHNESS_SEC` (default 30) and clears the metrics and Admin caches when extraction or similarity has written.

**BigQuery DML limit:** BigQuery queues about 20 mutating DML statements (`MERGE` / `UPDATE`) per table; beyond that it rejects them. Extraction returns 500 on any BigQuery error, so Pub/Sub retries the photo later. For bulk backfills, upload in batches of a few dozen photos rather than thousands at once, and keep the subscription's retry policy (exponential backoff) and dead-letter topic in place.

### R7. Smoke test this release

| Where | Action | Expect |
|-------|--------|--------|
| Admin → Customers & Orders | Create a customer, then an order for 3 clothes | Order row shows **New** and `0 / 3`; the copy button gives `garments/<customer>_<order>_<item_id>.jpg` |
| GCS | Upload `…_1.jpg` for that order | Row in `commodities` with `source=pipeline`, `image_status=extracted`; after the Admin cache expires the order shows `1 / 3` and the customer detail lists the garment |
| Ads Lab (brand) | Click **New Campaign** | Previous campaign, job card and audience cleared; step 1 open |
| Campaigns (brand) | Launch a 1P campaign | Banner "“…” is running (simulated)" with **Create Another Campaign**; Ads Lab is already fresh |
| AURA | Click the refresh icon (New chat) | Log cleared; greeting names the tab, audience and anything in flight; no ids shown |
| AURA on Geo | "Create a new campaign for this audience" | Switches to Ads Lab, starts a fresh campaign with the Geo audience and asks for SKU images |

Quick API checks (use a real admin / brand token from `/v1/auth/login`):

```bash
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$CC_URL/v1/admin/records/options" | python3 -m json.tool
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$CC_URL/v1/admin/orders?pending=true&size=5" | python3 -m json.tool
curl -sS -X POST -H "Authorization: Bearer $BRAND_TOKEN" -H 'Content-Type: application/json' \
  -d '{"context":{"active_tab":"geo"}}' "$CC_URL/v1/agent/reset" | python3 -m json.tool
```

---

## Release: one consistent dataset (PIN areas, shared values, RFM, reconcile)

Every tab and service now reads the same BigQuery `suffai_ops` data:

- **PIN areas.** Names, population, centroids and the 1 / 2 / 4 km buffers live in `suffai_ops.pin_areas`. They are filled by Gemini with Google Search, replacing `pin_population.json` / `web/metrics.json`. Geo has an area card with a Refresh button for admins. Admin **Create Customer** is blocked when the PIN can't be verified or is outside the city.
- **One spelling per value.** `suffai_ops.ref_values` maps every spelling to one canonical value, so "Levi's", "Levis" and "LEVI'S" become one brand. This applies to brand, colour, garment name and both genders. The migration rewrites existing rows, and extraction and Admin write canonical values.
- **RFM** is recomputed from orders (rule `rfm-v1`) after each Admin order and by `recompute_rfm.py`. New customers start in **New**.
- **Linking.** Similarity links a photo exactly as extraction does: by file name, only when the order exists and belongs to that customer, using the customer's current PIN. Other photos are `unlinked` and acknowledged. The first photo of an order supersedes its CSV items.
- **One garment count.** Ads Lab "Photographed garments" and Admin **Unique garments** both count distinct similarity garments under the customers' current PINs, read from BigQuery. The Admin proxy to similarity is gone. Campaign picks go to customers who match the audience in BigQuery and have a wardrobe in Cloud SQL (cap `CAMPAIGN_ELIGIBLE_USER_CAP`, default 200).
- **Admin can create a Style Assistant login** with a new customer. The password is shown once, and only its sha256 is stored.
- **Fresher dashboards.** Writes by extraction, similarity and loads show within about 30 s (`BQ_FRESHNESS_SEC`).

Run order, after Step 0 and after the unified-tables release (R1–R5):

1. `migrate_bq_consistency.sh`
2. Deploy extraction, then similarity, then Command Center
3. `recompute_rfm.py`
4. `backfill_pin_areas.py`
5. `reconcile_data.py` dry run, then `--apply`
6. Smoke checks

The scripts in steps 3–5 run from your machine with Application Default Credentials (`gcloud auth application-default login`) and the Command Center Python requirements (`pip install -r command_center/requirements.txt`). Each one is a dry run unless you pass `--apply`.

### C1. Migrate BigQuery

```bash
./command_center/scripts/migrate_bq_consistency.sh "$GOOGLE_CLOUD_PROJECT"   # optional 2nd arg: dataset
```

Creates `pin_areas` and `ref_values` and seeds the fixed vocabularies. Brand spellings come from brand logins first, then the most frequent spelling in the data. Rewrites `customers.gender` and `commodities` brand / colour / name / gender to the canonical spelling. Recreates `garment_items` without superseded CSV rows. Safe to re-run.

```bash
bq query --use_legacy_sql=false \
  "SELECT kind, COUNT(*) AS n FROM \`${GOOGLE_CLOUD_PROJECT}.suffai_ops.ref_values\` GROUP BY kind"
```

### C2. Deploy extraction, then similarity, then Command Center

```bash
./deploy/deploy_extraction.sh
./deploy/deploy_similarity.sh            # keeps DATABASE_URL / Cloud SQL if exported (Step 0)
./deploy/04_integrate_command_center.sh
```

Do them in this order so that no service writes values or links photos by the old rules after the next one has switched.

IAM for this release:

| Service account | Needs | For |
|-----------------|-------|-----|
| similarity | BigQuery Data Editor on `suffai_ops` + Job User | Owner lookup (orders ⋈ customers) and the observation `INSERT` |
| extraction | (unchanged from R2) | Order lookup, superseding CSV items, item `MERGE` |
| Command Center | BigQuery Data Editor on `suffai_ops` + Job User; Vertex AI User | `pin_areas` writes, RFM `MERGE`, customer logins in `login_cred_db`; grounded PIN lookups (`GEO_GROUNDING_LOCATION=global`) |

```bash
PROJECT_NUMBER=$(gcloud projects describe "$GOOGLE_CLOUD_PROJECT" --format='value(projectNumber)')
SIM_SA=$(gcloud run services describe suffai-similarity \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)')
SIM_SA="${SIM_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
bq add-iam-policy-binding --member="serviceAccount:${SIM_SA}" --role="roles/bigquery.dataEditor" \
  "${GOOGLE_CLOUD_PROJECT}:suffai_ops"
gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${SIM_SA}" --role="roles/bigquery.jobUser"
```

New Command Center knobs (defaults are fine; see `deploy/env.example`): `BQ_FRESHNESS_SEC=30`, `CAMPAIGN_ELIGIBLE_USER_CAP=200`, `GEO_AREA_CACHE_DAYS=30`, `GEO_AREA_RATE_PER_MIN=10`. `COMPARE_PIN_CANDIDATE_LIMIT` (default 2000) caps photographed garments per PIN; since the one-garment-count release it also sizes the list Command Center sends to `compare-pins`.

### C3. Recompute RFM

```bash
python3 command_center/scripts/recompute_rfm.py --project "$GOOGLE_CLOUD_PROJECT"          # dry run: counts per segment
python3 command_center/scripts/recompute_rfm.py --project "$GOOGLE_CLOUD_PROJECT" --apply  # writes changed segments only
```

### C4. Fill PIN areas

```bash
export ENABLE_VERTEX=true GEO_GROUNDING_LOCATION=global GEO_CITY=Bengaluru
python3 command_center/scripts/backfill_pin_areas.py --project "$GOOGLE_CLOUD_PROJECT"          # lists PINs to look up
python3 command_center/scripts/backfill_pin_areas.py --project "$GOOGLE_CLOUD_PROJECT" --apply  # one grounded prompt per PIN
```

Each lookup is a billed Gemini prompt with Google Search grounding. PINs that are already saved are skipped; `--refresh` redoes them. Malformed customer PINs are listed so you can fix the record. `not_found` / `out_of_city` results are saved too, and are retried only with `--refresh`.

### C5. Reconcile leftovers

The Cloud SQL part needs `DATABASE_URL`. From your machine, use the Cloud SQL Auth Proxy, for example `cloud-sql-proxy "$CLOUD_SQL_INSTANCE" --port 5433` and `DATABASE_URL=postgresql://USER:PASS@127.0.0.1:5433/DB`. Add `--skip-cloud-sql` to reconcile BigQuery only.

```bash
python3 command_center/scripts/reconcile_data.py --project "$GOOGLE_CLOUD_PROJECT"          # dry run: prints counts
python3 command_center/scripts/reconcile_data.py --project "$GOOGLE_CLOUD_PROJECT" --apply
```

| Where | What it changes |
|-------|-----------------|
| BigQuery | Deletes orders whose customer is missing, items whose order or customer is missing, and similarity observations of missing customers; sets `active = FALSE` on customer logins whose customer is missing |
| Cloud SQL | Deletes `user_wardrobe` / `campaign_user_recommendations` rows of missing customers; sets `user_wardrobe.pin_code_at_first_sight` to the current PIN; after migration 011, queues matched photos that have no BigQuery observation for similarity to resend (P6) |
| Report only | Observations waiting for BigQuery (count and oldest), `unique_garments` with no wardrobe link left (kept), and observed garments missing from `unique_garments` |

Read the dry-run counts before `--apply`. It refuses to run if `customers` is empty (a wrong `--project` / `--dataset`). It is safe while photos are uploading: a resent observation is the same row again, and `garment_items` keeps one. It never touches `garment_pin_counts` (unused since the one-garment-count release). It prints counts and garment ids only.

**Sample data next to live data (optional):** `APPEND=1 ./command_center/scripts/load_to_bq.sh "$GOOGLE_CLOUD_PROJECT"` keeps the tables and inserts only CSV rows whose id is new (orders / customers by `_id`, items by `order_id` + `commodity_id`). Then run C3–C5 again.

### C6. Smoke checks

`CC_URL` and `ADMIN_TOKEN` are set as in Step 5.

```bash
curl -sS "$CC_URL/health" | python3 -m json.tool   # admin_unique_garments: "bigquery"
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$CC_URL/v1/admin/unique-summary" | python3 -m json.tool
curl -sS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"pin":"560034"}' "$CC_URL/v1/geo/area-profile" | python3 -m json.tool
```

| Where | Action | Expect |
|-------|--------|--------|
| Geo | Pick a PIN | Area card with name, population, land area, 1 / 2 / 4 km buffers and sources; the map strip shows the same numbers. Admin sees **Refresh** |
| Admin → Customers & Orders | Create a customer with PIN 110001 | Blocked: "PIN 110001 is outside Bengaluru." Nothing saved |
| Admin → Customers & Orders | Create one with a Bengaluru PIN and **Create Style Assistant login** | "Customer … created in <area> (segment New)"; username + password shown once; **I've copied it** clears them |
| Admin → Customers & Orders | Add an order for that customer | Segment recomputed in the background; the CSV orders show "Legacy items (no photos)", not "awaiting photos" |
| Admin → Wardrobe & Garments → Image-Deduplicated Garments | Open it | PIN list = current customer PINs; counts match the Ads Lab tile for the same PINs |
| Ads Lab (brand) | Extract & Rank | Tiles "Photographed garments" and "Comparisons: image × photographed garment"; Customers shows "N with a wardrobe get picks" |
| GCS | Upload `garments/<customer>_<order>_1.jpg` | Extraction and similarity both link it; the order's CSV items become `superseded`; dashboards update within ~30 s |
| GCS | Upload a photo for an order that doesn't exist | Extraction and similarity both log `unlinked`; nothing written |

---

## Release: private data out of deploys, shared SKU values, one garment count

- **Private data stays on your machine.** `.dockerignore`, `.gcloudignore` and `.gitignore` keep `data_offline/`, the sample zips and `customers.csv`, `captured_garments/`, `archive/`, `.merge_backup`, `.env` and the plaintext / summary login CSVs out of Docker images, Cloud Build uploads and git. The files themselves are not moved. The Command Center image copies only `command_center_data/auth/login_cred_db.csv` (hashed passwords, for the CSV login fallback).
- **Brand SKUs use the same values as customer garments.** `/v1/extract` now returns `item_attributes`: the colour, garment name, brand, pattern, size, material, sleeve, collar and gender that extraction writes for customer photos. Command Center stores those on each brand SKU. Ranking and the Style Assistant compare shared values, so `navy` matches `Navy Blue` and `t_shirt` matches `T-Shirt`, including SKUs stored before this release.
- **One garment count.** BigQuery `garment_items` is the only count of garments per PIN. Command Center builds each PIN's garment list (the same one behind the Ads Lab tile and Admin **Unique garments**) and sends it to `compare-pins` as `candidates` and `pin_totals`; similarity only looks up embeddings. Similarity no longer reads or writes Cloud SQL `garment_pin_counts` (the table stays, unused; dropping it is up to you).
- **Similarity admin endpoints removed.** `GET /v1/admin/unique-garments` and `/v1/admin/unique-garments/summary` on `suffai-similarity` are gone. Admin already reads Command Center's own `/v1/admin/unique-garments` and `/v1/admin/unique-summary`, which use BigQuery.
- **No dropped observations.** Similarity records the photo's event in Cloud SQL, then writes its BigQuery observation. If that write fails, the push returns 500 and Pub/Sub retries; the retry resends the stored row with its original `event_ts` and does not match the photo again (the HTTP `/v1/similarity/process` returns 503). After each photo it processes, similarity also resends up to 10 events that have waited more than 10 minutes.

Run order (each step keeps the services that are still on the old version working):

1. Cloud SQL migration 011. It only adds columns, so the current similarity keeps working.
2. Deploy extraction. It adds `item_attributes`, which the current Command Center ignores.
3. Deploy Command Center. It sends `candidates`, which the current similarity ignores.
4. Deploy similarity. It requires `candidates` and uses the new columns.
5. Optional: `normalize_sku_attributes.py`, dry run then `--apply`, then re-rank campaigns.
6. `reconcile_data.py`, dry run first.

### P1. Cloud SQL migration 011

Through the Cloud SQL Auth Proxy (see C5) or Cloud SQL Studio:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/011_similarity_bq_sync.sql
```

Adds `order_id`, `item_id` and `bq_synced_at` to `garment_similarity_events`, plus an index on events not yet sent to BigQuery. Events from before this release are marked sent; reconcile (P6) finds any that never reached BigQuery. Safe to re-run. `./deploy/01_prepare_similarity_db.sh` also applies it.

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'garment_similarity_events' AND column_name IN ('order_id', 'item_id', 'bq_synced_at');
-- 3 rows
SELECT COUNT(*) FROM garment_similarity_events WHERE bq_synced_at IS NULL;
-- 0 right after the migration
```

### P2–P4. Deploy extraction, then Command Center, then similarity

```bash
gcloud meta list-files-for-upload | grep -E 'data_offline|plaintext|brand_logins_summary|customers\.csv|\.zip$|\.env$'
# no output: Cloud Build uploads none of the private files

./deploy/deploy_extraction.sh
./deploy/04_integrate_command_center.sh
./deploy/deploy_similarity.sh            # keeps DATABASE_URL / Cloud SQL if exported (Step 0)
```

No new IAM: Command Center already reads `suffai_ops` and similarity already writes observations (C2).

Knobs (defaults are fine):

| Service | Variable | Default | Meaning |
|---------|----------|---------|---------|
| Command Center | `COMPARE_PIN_CANDIDATE_LIMIT` | `2000` | Garments per PIN in the tile, Admin and the `compare-pins` list (most-photographed first). Now Command Center only |
| similarity | `COMPARE_MAX_CANDIDATES` | `100000` | Max garments per `compare-pins` request, all PINs together |
| similarity | `COMPARE_EMBED_CACHE_MAX` | `20000` | Max garment embeddings cached per instance (`COMPARE_CACHE_SEC`, default 300 s) |

### P5. Rewrite stored brand SKU attributes (optional)

Ranking already treats old and new spellings as one value. Run this to make the stored SKU columns match too:

```bash
export DATABASE_URL=postgresql://USER:PASS@127.0.0.1:5433/DB   # Cloud SQL Auth Proxy, as in C5
python3 command_center/scripts/normalize_sku_attributes.py           # dry run: counts and SKU ids
python3 command_center/scripts/normalize_sku_attributes.py --apply
```

Then open a campaign in Ads Lab and run **Extract & Rank** again to refresh its stored scores.

### P6. Reconcile

Run C5 (`reconcile_data.py`), dry run first. It no longer rebuilds `garment_pin_counts`; the Cloud SQL part now prints:

| Line | Meaning | `--apply` |
|------|---------|-----------|
| `observations waiting for BigQuery: N (oldest …)` | Events whose BigQuery write has not succeeded yet | Nothing; similarity resends them |
| `matched photos with no BigQuery observation: N` | Matched photos (customer still exists, order and item known) that BigQuery has no observation for | Queued for resend (`bq_synced_at` cleared); similarity resends them, up to 10 after each photo it processes |
| `observed garments missing from unique_garments: N` | Garments in BigQuery with no Cloud SQL embedding | Nothing; `compare-pins` skips them |
| `garment_similarity_events: apply db/migrations/011_similarity_bq_sync.sql …` | P1 has not run | Nothing |

### P7. Smoke checks

| Where | Action | Expect |
|-------|--------|--------|
| Ads Lab (brand) | Extract & Rank for a few PINs | Ranking succeeds; "Photographed garments" equals Admin **Unique garments** for the same PINs |
| Ads Lab (brand) | Upload a SKU photo | SKU colour / garment name use the shared spelling (e.g. `Navy Blue`, `T-Shirt`) |
| Similarity logs | During ranking | `compare_pins … candidates=N missing=0` (missing > 0: garments with no embedding of the same model) |
| GCS + Cloud SQL | Upload `garments/<customer>_<order>_1.jpg`, wait a few seconds | `SELECT COUNT(*) FROM garment_similarity_events WHERE bq_synced_at IS NULL` stays 0 |
| Similarity | `GET /v1/admin/unique-garments/summary` with an identity token | `404` |

---

## Release: one Geo store scorer for the map, the brief and AURA

- **One scorer.** `command_center/geo_analysis.py` is the only store score and Yes / Consider / No rule. The Geo map now calls the new `POST /v1/geo/scores`; the brief, the market check and AURA use the same rows (`command_center/geo_service.py`). `web/geo.html` no longer computes scores.
- **Rule change you'll see on the map.** Map dots follow the server rule: footfall under 40 is No; Yes needs a store score of at least 0.33, and so does Consider unless the brand share is 0.22 or more. For example, brand share 0.25 with footfall 30 moves from Consider to No.
- **Admin default brand.** With no single brand filter, admins get the brand with the most garments among the selected audience and PINs, on the map, in the brief and in AURA.
- **Request bodies.** `POST /v1/geo/brief` takes `{"pin", "filters", "analysis_brand"}` (numbers sent by a browser are ignored). `POST /v1/geo/market-enrichment` takes `filters` instead of `expansion_decision`.
- **Store section timing.** The store section updates about 250 ms plus a round trip after a filter change; the charts still update instantly. On a brand's first visit, PINs show unverified until the store lookup finishes, then re-score.

Redeploy **Command Center only** (it also serves `web/`). No BigQuery or Cloud SQL migration, no IAM change, no new environment variables; extraction and similarity are unchanged.

```bash
./deploy/04_integrate_command_center.sh
```

| Where | Action | Expect |
|-------|--------|--------|
| Geo (admin) | Change filters, click a few areas | Each dot's decision matches the Market brief's decision for that area (unless an external market check you ran lowered it) |
| Geo (brand) | Open Geo | Only the own brand and Competitors appear; no competitor names in `/v1/geo/scores` responses (browser network tab) |
| AURA | "Should we open a store in 560008?" | Same decision as the Geo dot for that PIN under the same filters |
| Logs | `request_id=… geo guardrail` | One line per brief: `expansion=` is the rule decision before the 4 km rule, `final=` matches the dot |

---

## Release: catalog ranking and brand SKU match

- **Ads Lab ranks from the warehouse catalog.** Brands pick up to 15 style × colour SKUs (sizes ignored) that already have a photo in Inventory › Catalog. Photo upload remains as a fallback. Ranking skips re-extraction and uses catalog attributes + the catalog photo for compare-pins.
- **Customer photos can inherit catalog metadata.** After Gemini, suffai-extraction matches the garment to the brand's Cloud SQL catalog (exact SKU / barcode / style code, else a strict four-attribute single match) and overwrites the BigQuery `commodities` row. Admin shows a **Brand SKU** badge.
- **Migrations:** Cloud SQL `013_catalog_images.sql` (`retail_product_images`, `brand_skus.source` / `retail_product_id` / `style_code`). BigQuery `migrate_bq_catalog_match.sh` (nullable columns on `commodities`). Details: [`RETAIL_SUITE.md → Catalog photos`](RETAIL_SUITE.md#catalog-photos-and-ads-lab-ranking).

Run order:

1. Cloud SQL migration 013 (safe after 012).
2. BigQuery catalog-match columns.
3. Redeploy **suffai-extraction** with Cloud SQL attached and `DATABASE_URL` (same instance as Command Center) so matching can run.
4. Redeploy Command Center.

### K1. Cloud SQL migration 013

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/013_catalog_images.sql
```

```sql
SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'retail_product_images';
-- 1
SELECT column_name FROM information_schema.columns
 WHERE table_name = 'brand_skus' AND column_name IN ('source','retail_product_id','style_code');
-- 3
```

### K2. BigQuery commodities columns

```bash
./command_center/scripts/migrate_bq_catalog_match.sh "$GOOGLE_CLOUD_PROJECT"   # optional 2nd arg: dataset
```

### K3. Redeploy extraction (Cloud SQL required for matching)

```bash
./deploy/deploy_extraction.sh
# Ensure DATABASE_URL and --set-cloudsql-instances match Command Center (bootstrap already does this on new projects).
```

Without `DATABASE_URL`, extraction still writes photo attributes; catalog matching is skipped.

### K4. Redeploy Command Center

```bash
./deploy/04_integrate_command_center.sh
```

### K5. Smoke checks

| Where | Action | Expect |
|-------|--------|--------|
| Inventory › Catalog (brand) | Open a style → Add photo for one colour | Thumbnail saved; colour card shows the image |
| Ads Lab (brand) | Step 2 → From your catalog → pick ≤15 with photos → Rank | Job runs; ranked cards use catalog colour / category; size not in attr fit |
| Ads Lab (brand) | Upload photos tab (no catalog) | Same as today's upload ranking |
| AURA (brand) | "List my catalog SKUs" then rank a few | `list_catalog_skus` / `rank_catalog_skus`; no internal IDs |
| Admin › garments | After a matched customer photo | **Brand SKU** badge; attributes match the catalog |
| Extraction logs | Photo of a labeled brand SKU that exists in catalog | `catalog match … how=sku` (or barcode / style_code / attributes) |

---

## Release: retail suite (Inventory, Warehouse, Store POS)

- **Three new tabs in the same Command Center.** Inventory (catalog, stock by location, stock moves, price tags), Warehouse (purchase orders and goods receipts, transfers, replenishment, new-store opening plans, bins, suppliers, staff logins, billing settings) and Store POS (GST bills, cash / card / UPI payments, returns with credit notes, day close). Same login, same shell, same Cloud SQL database. Details: [`RETAIL_SUITE.md`](RETAIL_SUITE.md).
- **Two new login roles.** `warehouse_staff` (Warehouse + Inventory) and `store_staff` (Store POS + Inventory), each tied to one location of one brand through the new BigQuery column `login_cred_db.location_code`. Brand logins create them on **Warehouse → Team**; the password is shown once and only its sha256 is stored. Brand and admin logins also get the three tabs.
- **Links to the existing tabs.** Geo has **Plan Opening Stock →**, which opens Warehouse with a stock plan for the focused area. Campaigns shows **Store Stock & Sales** (labelled *Actual (POS)*) for the opened campaign. AURA can read the brand's store numbers (read-only).
- **Payments are recorded, not processed.** No payment gateway; the cashier records the mode and amount.
- **AURA Stock Pilot.** Warehouse opens on a ranked list of restock, rebalance, reorder, stuck-document, slow and damaged-stock suggestions worked out from actual POS sales, stock and local wardrobe demand, each with its reasons. A person reviews the quantities and confirms; only a *draft* transfer or PO is created. AURA explains any suggestion and can prepare the same drafts behind a Confirm card. New code only (`retail_pilot.py`, `web/pilot.js`, `GET /v1/retail/pilot`, `POST /v1/retail/pilot/actions/{id}/apply`); no migration, no new variables, no new IAM. Transfers drafted this way are stored with `source = 'aura'` in the existing text column. Details: [`RETAIL_SUITE.md → AURA Stock Pilot`](RETAIL_SUITE.md#aura-stock-pilot).

Run order:

1. Cloud SQL migration 012. It only creates new `retail_*` tables; the running Command Center ignores them.
2. BigQuery `migrate_bq_retail.sh`. It only adds a nullable column; current logins keep working.
3. Deploy Command Center.

### S1. Cloud SQL migration 012

Through the Cloud SQL Auth Proxy (see C5) or Cloud SQL Studio, as the same user the Command Center connects with (so it owns the tables):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/012_retail_ops.sql
```

```sql
SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'retail\_%';
-- 20
```

Safe to re-run. A new project gets it from `deploy/bootstrap_new_project.sh`.

### S2. BigQuery login column

```bash
./command_center/scripts/migrate_bq_retail.sh "$GOOGLE_CLOUD_PROJECT"   # optional 2nd arg: dataset
```

Adds `location_code STRING` to `suffai_ops.login_cred_db`. Safe to re-run. Until it runs, logins still work but **Warehouse → Team** cannot create staff.

`load_login_creds_bq.sh` replaces the whole login table. It now stops if staff logins exist (they are only in BigQuery, never in the CSV); set `REPLACE_STAFF_LOGINS=1` to replace anyway.

### S3. Deploy Command Center

```bash
./deploy/04_integrate_command_center.sh
```

It needs `DATABASE_URL` and the Cloud SQL attachment it already has for campaigns, and the BigQuery write access it already has for Admin logins. No new IAM. Optional variables (defaults follow the apparel GST rule):

| Variable | Default | Meaning |
|----------|---------|---------|
| `RETAIL_GST_APPAREL_THRESHOLD_INR` | `2500` | Taxable value per piece up to which the low apparel rate applies |
| `RETAIL_GST_APPAREL_LOW_BP` | `500` | Low rate in basis points (5%) |
| `RETAIL_GST_APPAREL_HIGH_BP` | `1800` | Rate above the threshold (18%) |

### S4. Smoke checks

| Where | Action | Expect |
|-------|--------|--------|
| Warehouse (brand) | Settings: legal name, GSTIN, address. Locations: one warehouse and one store | Saved; GSTIN check digit validated |
| Inventory (brand) | Add a style with colours × sizes, MRP and HSN | One SKU per colour / size, each with a barcode |
| Warehouse (brand) | New purchase order → Order → Receive (part of it) | PO shows *partial*; Inventory shows the received units in the warehouse bin |
| Warehouse (brand) | Transfer to the store → Dispatch → Receive | Units move from the warehouse to the store |
| Warehouse → Team (brand) | Add a store login for the store | Password shown once; signing in with it shows only Store POS and Inventory |
| Store POS (store login) | Scan a barcode, take UPI payment, print | GST invoice with CGST + SGST that add up to the bill; number like `BLR012627-000001` |
| Store POS (store login) | Return one piece from that bill | Credit note; stock back at the store |
| Geo (brand) | Focus an area → **Plan Opening Stock →** | Warehouse opens with a plan for that PIN |
| Campaigns (brand) | Open a campaign | **Store Stock & Sales · Actual (POS)** below the simulated analysis |
| AURA (brand) | "How much stock do we have and what did stores sell this week?" | Numbers from the retail tabs, quoted as actual |
| Warehouse → Stock Pilot (brand) | After a few days of POS sales: open it, then **Review & create draft transfer** on a restock card → change a quantity → create | Toast with the new `TR…` number; the transfer is a draft on **Transfers**; the suggestion shrinks or disappears |
| Warehouse → Stock Pilot (brand) | **Ask AURA why** on a card, then **Confirm** in the chat | AURA explains with the card's numbers; Confirm creates the draft and opens it |
| Warehouse (warehouse login) | Open Stock Pilot | Only suggestions involving that warehouse; no AURA buttons; reorder cards locked to the brand |
| Inventory (store login) | Overview | Stock Pilot panel with that store's suggestions only |

---

## Step 1 — Cloud SQL migration (brand campaign tables)

Required for Ads Lab **campaign** persistence (`brand_campaigns`, `brand_skus`, `brand_sku_pin_scores`).

**Option A — script (if `psql` + `DATABASE_URL` work):**

```bash
chmod +x deploy/01_prepare_similarity_db.sh
# Prefer applying only 005 if 003/004 already applied:
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/005_brand_sku_campaigns.sql
# Or full prepare (003+004+005 + BQ DDL):
./deploy/01_prepare_similarity_db.sh
```

**Option B — Cloud SQL Studio / Console:** paste and run `db/migrations/005_brand_sku_campaigns.sql`.

Verify:

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('brand_campaigns','brand_skus','brand_sku_pin_scores');
```

---

## Step 2 — Redeploy `suffai-similarity` (if needed)

Skip if you only changed Command Center UI/BFF and the similarity image is already current with `compare-pins` taking `candidates` from Command Center. Similarity has no admin APIs any more; Admin reads BigQuery through Command Center.

### 2a. Build & deploy

```bash
chmod +x deploy/deploy_similarity.sh deploy/02_deploy_similarity.sh

# Pass DATABASE_URL + CLOUD_SQL_INSTANCE so they are not lost
./deploy/02_deploy_similarity.sh
# same as: ./deploy/deploy_similarity.sh
```

### 2b. Or Cloud Build then deploy image

```bash
gcloud builds submit --config=deploy/cloudbuild-similarity.yaml .

IMG="${GCP_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/suffai/suffai-similarity:latest"
gcloud run deploy suffai-similarity \
  --image="$IMG" \
  --region="$GCP_REGION" \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --no-allow-unauthenticated
```

After image-only deploy, **re-check** that `DATABASE_URL`, Cloud SQL attachment, and Pub/Sub push endpoint still point at the new revision (`deploy/deploy_similarity.sh` updates the push sub).

### 2c. IAM (usually already set)

- Pub/Sub push SA `pubsub-push@…` → `roles/run.invoker` on `suffai-similarity`
- Similarity SA: Cloud SQL Client, GCS objectViewer, Vertex AI User, BigQuery as configured

---

## Step 3 — Redeploy `suffai-command-center` (always for this release)

This ships Geo + Ads Lab campaign UI + Bot + Admin tab.

```bash
chmod +x deploy/deploy_command_center.sh deploy/04_integrate_command_center.sh

# Resolves EXTRACTION_URL + SIMILARITY_URL from live services, then builds/pushes/deploys CC
./deploy/04_integrate_command_center.sh
```

Or explicitly:

```bash
export EXTRACTION_URL SIMILARITY_URL   # from Step 0
./deploy/deploy_command_center.sh
```

### Cloud Build alternative

```bash
gcloud builds submit --config=deploy/cloudbuild-command-center.yaml .

IMG="${GCP_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/suffai/suffai-command-center:latest"
gcloud run deploy suffai-command-center \
  --image="$IMG" \
  --region="$GCP_REGION" \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --allow-unauthenticated \
  --memory=1Gi \
  --update-env-vars="USE_BQ=true,BQ_DATASET=suffai_ops,GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT},GCS_IMAGE_BUCKET=${GCS_IMAGE_BUCKET},EXTRACTION_URL=${EXTRACTION_URL},SIMILARITY_URL=${SIMILARITY_URL},ENABLE_VERTEX=true,VERTEX_LOCATION=${VERTEX_LOCATION},VERTEX_CHAT_MODEL=${VERTEX_CHAT_MODEL},CORS_ORIGINS=*"
```

Prefer `--update-env-vars` over `--set-env-vars` so existing keys (e.g. `DATABASE_URL` from Secret Manager) are not wiped.

### Attach Cloud SQL + DATABASE_URL on CC (campaign DB)

If campaign tables live on the same Postgres as extraction/similarity:

```bash
# Attach instance (idempotent)
gcloud run services update suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --add-cloudsql-instances="$CLOUD_SQL_INSTANCE"

# Set DATABASE_URL (or map from Secret Manager in Console)
gcloud run services update suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --update-env-vars="DATABASE_URL=${DATABASE_URL}"
```

### IAM: CC → extraction + similarity

Command Center calls private Cloud Run services with an ID token. Grant the **command-center runtime service account** `roles/run.invoker` on both:

```bash
CC_SA=$(gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(spec.template.spec.serviceAccountName)')
# If empty, default Compute SA:
# CC_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud run services add-iam-policy-binding suffai-extraction \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${CC_SA}" \
  --role="roles/run.invoker"

gcloud run services add-iam-policy-binding suffai-similarity \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:${CC_SA}" \
  --role="roles/run.invoker"
```

Also ensure CC SA has:

| Role / access | Needed for |
|---------------|------------|
| BigQuery Job User + Data Editor on `suffai_ops` | `GET /v1/metrics`; Admin creates customers / orders (Data Viewer is read-only) |
| Storage Object Creator/Viewer on garment bucket | Ads Lab upload `demo/` + `brands/` |
| Vertex AI User | `POST /v1/chat` |
| Cloud SQL Client | brand campaign tables |

---

## Step 4 — Redeploy extraction

Required for the unified-tables release (see R2–R3); otherwise only when `worker/` changed:

```bash
./deploy/deploy_extraction.sh
```

Avoid `./deploy/deploy_cloud_run.sh` on a live project: it also recreates the retired `suffai-api` and uses `--set-env-vars`, which wipes existing env vars on every service it touches.

---

## Step 5 — Verify end-to-end

```bash
chmod +x deploy/05_verify_similarity_e2e.sh
./deploy/05_verify_similarity_e2e.sh
```

Manual checks:

```bash
CC_URL=$(gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')

# Health flags
curl -sS "$CC_URL/health" | python3 -m json.tool
# Expect: use_bq, extraction_url, similarity_url, enable_vertex, ads_lab_campaign, style_assistant, auth true;
#         admin_unique_garments "bigquery"

# Auth (admin seed)
curl -sS -X POST "$CC_URL/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin1","password":"123@admin"}' | python3 -m json.tool
# Expect: token, role=admin, allowed_tabs including geo/ads/style/admin
export ADMIN_TOKEN=PASTE_TOKEN_FROM_RESPONSE

# Metrics (BQ)
curl -sS "$CC_URL/v1/metrics" | head -c 400; echo

# Admin unique garments (BigQuery; admin token from the login above)
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "$CC_URL/v1/admin/unique-summary" | head -c 400; echo

# Style Assistant (Feature 2)
# Customer login only; brand / admin tokens get 403
# curl -sS -H "Authorization: Bearer CUSTOMER_TOKEN" "$CC_URL/v1/style-assistant/feed" | head -c 400; echo

# Chat
curl -sS -X POST "$CC_URL/v1/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Summarize top brands","context":{"active_tab":"geo"},"filter_context":{"pin_code":"110001"}}'

# UI
open "${CC_URL}/login.html"   # admin1 / 123@admin or brand_*/cust_* from plaintext handoff
```

### UI smoke checklist

| Tab | Action | Expect |
|-----|--------|--------|
| Login | Sign in as brand / customer / admin | Tabs match role; deep-link to forbidden page redirects |
| Geo (brand) | Brand filter | Only own brand + Competitors |
| Ads Lab (brand) | Brand field locked; upload SKUs | Campaign posts forced brand; taste brands collapsed |
| Style (customer) | Customer locked; pick brand → Generate | Wardrobe for session `customer_id` only |
| Bot | Ask about filters (Geo/Ads only) | Vertex answer; hidden for customer / Style / Admin |
| Admin | Unique garments PIN/brand filters | Counts from BigQuery under current customer PINs |
| Admin | Customers & Orders → create customer + order | Order listed with `0 / N` photos and a copyable photo name |
| Ads Lab / Campaigns | New Campaign, or Create Another Campaign after a launch | Fresh Ads Lab; launched campaign stays on Campaigns |
| AURA | New chat icon | Cleared log + greeting with current tab and audience |

### Logs

```bash
gcloud run services logs read suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" --limit=50

gcloud run services logs read suffai-similarity \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" --limit=50
```

Look for `[command_center]` and campaign / chat / PIN area lookup errors.

---

## Recommended order (short)

```text
0. Load BQ login_cred_db (generate_login_creds.py + load_login_creds_bq.sh)
1. Apply db/migrations/005_brand_sku_campaigns.sql (already done on live projects)
2. ./command_center/scripts/migrate_bq_unified.sh "$GOOGLE_CLOUD_PROJECT"   (once; no CSV reload afterwards)
3. Grant BigQuery dataEditor on suffai_ops + jobUser to the extraction SA
4. ./deploy/deploy_extraction.sh   (USE_BQ=true, BQ_DATASET=suffai_ops)
5. ./deploy/deploy_similarity.sh
6. ./deploy/04_integrate_command_center.sh, then BigQuery dataEditor on suffai_ops for the CC SA
7. Ensure CC has DATABASE_URL + Cloud SQL + run.invoker on extraction & similarity
8. ./command_center/scripts/migrate_bq_consistency.sh "$GOOGLE_CLOUD_PROJECT"   (C1)
9. Redeploy extraction, then similarity, then Command Center; similarity SA BigQuery dataEditor + jobUser (C2)
10. recompute_rfm.py, then backfill_pin_areas.py (dry run, then --apply) (C3, C4)
11. reconcile_data.py dry run, then --apply (C5)
12. psql -f db/migrations/011_similarity_bq_sync.sql (P1)
13. Deploy extraction, then Command Center, then similarity (P2–P4)
14. Optional: normalize_sku_attributes.py dry run, then --apply; re-run Extract & Rank (P5)
15. reconcile_data.py dry run, then --apply (P6)
16. Admin: create customer + order, upload garments/<customer_id>_<order_id>_<item_id>.jpg
17. ./deploy/05_verify_similarity_e2e.sh + UI smoke (role logins, R7, C6, P7)
```

One-liner path when similarity and extraction are already current:

```bash
cd /Users/rahul.kumar/Downloads/AI_CUP/AI_CUP-main_Updated
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID GCP_REGION=asia-south1
# export DATABASE_URL=… CLOUD_SQL_INSTANCE=…   # if not already on the service
./deploy/04_integrate_command_center.sh
./deploy/05_verify_similarity_e2e.sh
```

---

## Release — outfit tags, SKU-first dedup

Customer photos now run in sequence: extraction (Gemini label card + outfit tags) → warehouse catalog match → BigQuery `commodities` → `garment-image-extracted` → similarity. When the catalog match is exact, the photo carries a `brand_sku` (`brand:style_code:color`) and similarity matches on it (this wardrobe, then any customer) before using fingerprints.

1. Cloud SQL: `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/015_garment_outfit_tags.sql`
2. BigQuery: `./command_center/scripts/migrate_bq_catalog_match.sh "$GOOGLE_CLOUD_PROJECT"` adds the outfit tag columns to `commodities`; re-run `migrate_bq_unified.sh` / `migrate_bq_consistency.sh` so `garment_items.is_repeat` also counts SKU repeats.
3. Pub/Sub: `./deploy/deploy_similarity.sh` creates `garment-image-extracted`, subscribes similarity to it, deletes the old `garment-image-uploaded-similarity-push`, and grants the extraction SA `roles/pubsub.publisher`. Run `./deploy/03_integrate_extraction_pubsub.sh` to verify.
4. Extraction: redeploy with `--update-env-vars=PUBSUB_EXTRACTED_TOPIC=garment-image-extracted` (`./deploy/deploy_extraction.sh`).
5. Redeploy `suffai-api` (upload `:complete` now needs `user_uuid` and `pin_code` and registers the garment), then Command Center.

If the extracted topic is missing, customer photo extractions return 500 and Pub/Sub retries them until it exists. Ads Lab (`brands/`, `demo/`) and `catalog/` images are unaffected.

---

## Common failures

| Symptom | Fix |
|---------|-----|
| `/health` → `similarity_url: false` | Re-run `./deploy/04_integrate_command_center.sh` with live `SIMILARITY_URL` |
| Campaign ranking 403/401 from similarity | CC SA missing `roles/run.invoker` on `suffai-similarity` |
| Admin / campaign 503 `DATABASE_URL` | Set `DATABASE_URL` + Cloud SQL on CC |
| Admin Unique garments 503 with a reference | CC can't read `suffai_ops` (BigQuery Job User + Data Viewer/Editor); the reference id is in the CC logs |
| Campaign extract fails | `EXTRACTION_URL` + CC SA invoker on extraction; GCS bucket writable |
| Chat 503 | `ENABLE_VERTEX=true` + Vertex AI User on CC SA; chat reads the live BigQuery metrics |
| Metrics 503 | `USE_BQ=true`, BQ tables/IAM. A missing `pin_areas` table does not fail metrics: PIN names and population stay blank until C1 / C4 run |
| Geo shows PIN codes instead of area names | `pin_areas` has no row for those PINs: run `backfill_pin_areas.py --apply` (C4) or open the PIN on Geo |
| Admin "Create Customer" → "The PIN could not be verified right now" | Grounded lookup unavailable: `ENABLE_VERTEX=true`, Vertex AI User on the CC SA, `GEO_GROUNDING_LOCATION=global`. Retry; nothing was saved |
| Similarity logs `unlinked photo …` | Same rule as extraction: the order is missing or belongs to another customer. Create the order, then upload the photo again |
| Customer login rejected after its customer was deleted | Expected: `reconcile_data.py --apply` deactivates logins whose customer is missing |
| Env vars disappeared after deploy | You used `--set-env-vars` wipe — restore from `/tmp/cc-env-before.yaml` or Secrets; prefer `--update-env-vars` |
| Customer photo uploaded but no item appears | Extraction logs `unlinked photo … (order missing or belongs to another customer)`: create the order in Admin, then upload the same file again. `customer photo ignored: USE_BQ=false` → redeploy with `./deploy/deploy_extraction.sh` |
| Extraction logs BigQuery `403 Access Denied` | Extraction SA lacks `roles/bigquery.dataEditor` on `suffai_ops` or `roles/bigquery.jobUser` (R2). Pub/Sub keeps retrying, so photos catch up once IAM is fixed |
| BigQuery "too many DML statements outstanding" | More than ~20 `MERGE`s queued on `commodities`; Pub/Sub retries them. Upload large backfills in smaller batches |
| Admin "Create Customer" → 503 with a reference | CC SA lacks BigQuery Data Editor on `suffai_ops` (R5), or the migration (R1) has not run; the reference id is in the CC logs |
| `load_csv_bq.py` / `load_to_bq.sh` refuses to load | The table holds Admin / pipeline rows that a CSV load would delete. Don't reload after the migration: use `APPEND=1` to add only new rows; `FORCE=1` only if you mean to wipe them |
| Dashboards count a garment twice | Similarity predates R4, so its observations have no order / item ids; redeploy similarity — new photos are deduped via `garment_items.is_repeat` |
| Campaign ranking 400 `candidates required` from similarity | Similarity was deployed before Command Center (P2–P4). Deploy Command Center |
| Campaign ranking 503 "Garment data unavailable" | Command Center can't read `suffai_ops` to build the PIN garment lists (BigQuery Job User + Data Viewer/Editor); see the CC logs |
| CC logs `extraction response has no item_attributes` | Extraction is older than Command Center; deploy extraction. SKU attributes stay blank until then |
| Similarity push 500s with `ObservationNotWritten` / "observations waiting for BigQuery" keeps growing | Similarity can't write `garment_similarity_observations` (BigQuery Data Editor + Job User, C2). Pub/Sub retries; once fixed, pending events are resent |
| Similarity fails every photo with a missing column (`order_id`, `item_id` or `bq_synced_at`) | Migration 011 (P1) has not run on that database |

---

## Related docs

- APIs, layout and first-time setup: [GCP_COMMAND_CENTER.md](./GCP_COMMAND_CENTER.md)
- Similarity E2E scripts: `deploy/01_…` through `deploy/05_…`
