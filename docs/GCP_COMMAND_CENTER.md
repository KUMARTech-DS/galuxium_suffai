# Command Center — layout, APIs, deploy

## Answers

1. **UI lives in `web/`** — static HTML (login, Geo, Ads Lab), not the old Next.js app.  
2. **GCP-native metrics:** UI calls `GET /v1/metrics` every load. Data comes **only from BigQuery**. There is **no** local CSV or `metrics.json` fallback — failures return HTTP errors so you can fix BQ/IAM.  
3. **All of this is under `AI_CUP_latest/`** (canonical folder for git push / redeploy).  
4. **`suffai-api` is retired.** BFF is `command_center/` on Cloud Run `suffai-command-center`. Extraction (`worker/`) and similarity remain separate.

**Services already live?** Use the redeploy runbook: **[GCP_REDEPLOY_E2E.md](./GCP_REDEPLOY_E2E.md)**.

---

## Repo layout

```text
AI_CUP_latest/
  web/                      # UI (login, Geo, Ads Lab, Admin)
  command_center/           # FastAPI BFF (metrics + extract + campaign + chat + admin)
    main.py
    brand_campaign.py
    pin_areas.py            # PIN area names / population via Gemini + Google Search → BQ pin_areas
    pin_garments.py         # garments per PIN from BQ garment_items: Ads Lab tile, Admin, compare-pins list
    requirements.txt
    scripts/                # aggregate_metrics.py, load_to_bq.sh, create_bq_tables.sql,
                            # migrate_bq_consistency.sh, recompute_rfm.py,
                            # backfill_pin_areas.py, reconcile_data.py, normalize_sku_attributes.py
  command_center_data/      # sample CSVs (load into BQ only) + auth handoff files;
                            # only auth/login_cred_db.csv goes into the image
  Dockerfile.command_center
  deploy/deploy_command_center.sh
  deploy/04_integrate_command_center.sh
  deploy/cloudbuild-command-center.yaml
  docs/GCP_REDEPLOY_E2E.md  # redeploy when services already exist
```

`_new_approach/` has been **removed** (content folded into `web/` + `command_center/`).

---

## APIs on Cloud Run service `suffai-command-center`

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health + env flags |
| `POST` | `/v1/auth/login` | Role login against BQ `login_cred_db` (CSV fallback only when BigQuery errors) → session token + `allowed_tabs` |
| `GET` | `/v1/auth/me` | Validate Bearer / `X-Session-Token` → session payload |
| `POST` | `/v1/auth/logout` | Invalidate in-memory session token |
| `GET` | `/v1/metrics` | Brand / admin session required. Geo aggregates from **BigQuery only** (+ optional `extraction_jobs_count`); brand sessions get Competitors masking |
| `POST` | `/v1/ads-lab/extract` | Upload → GCS `demo/` → existing `suffai-extraction` `/v1/extract` (baseline extract; still available) |
| `POST` | `/v1/ads-lab/campaign` | **Primary Ads Lab path:** brand SKU campaign — GCS `brands/{brand}/…` → extract (brand overwrite; SKU attributes from `item_attributes`) → similarity compare-pins against each PIN's BigQuery garment list → rank with PIN+age+gender+RFM; brand role forces `brand_name` |
| `POST` | `/v1/chat` | Brand / admin session required (brand sessions get competitor masking). Corner bot on Command Center (not legacy `api/`): Vertex Gemini + a slim summary of the live BigQuery metrics + Geo/Ads UI + optional `filter_context` |
| `POST` | `/v1/geo/area-profile` | Brand / admin session required. Body `{"pin", "refresh"}`. Area name, population, area and 1 / 2 / 4 km buffer population for a PIN from BQ `pin_areas`; fetched once with Gemini + Google Search and saved. `refresh` is admin only |
| `POST` | `/v1/geo/scores` | Brand / admin session required (403 for a brand login without a brand name). Body `{"filters": {"pins", "age", "gender", "rfm"}, "analysis_brand"}`. Store score, Champions & Loyalists demand, estimated footfall (simulated), brand share, Yes / Consider / No and the 4 km zone for **every** PIN the filters cover, plus KPIs and top colours / patterns / sizes / brands. Reads the store cache only (never calls Gemini): PINs stay `unverified` until `GET /v1/geo/stores` has looked the brand up. Brand logins always score their own brand on masked metrics |
| `POST` | `/v1/geo/brief` | Brand / admin session required. Body `{"pin", "filters", "analysis_brand"}`; any numbers in the body are ignored. The server scores the PINs (same rows as `/v1/geo/scores`) and asks Vertex Gemini for the Market brief from those **PII-free** aggregates; template fallback in UI if Vertex off. Server enforces the 4 km store rule (the rule's decision is binding over Gemini's) and returns `decision_label`, `mode`, `proximity`, `decision_logic`, `stock_mix` |
| `GET` | `/v1/geo/stores` | Existing stores of the analysed brand (Gemini + Google Search, cached) with 4 km catchment PINs; `?refresh=true` admin only |
| `POST` | `/v1/geo/routing-guide` | Regional stock & assortment routing guide for a PIN within 4 km of an existing store (aggregated personas, min group size) |
| `POST` | `/v1/geo/market-enrichment` | Body `{"pin", "analysis_brand", "filters", "refresh"}`. Grounded footfall / rent / competition for a whitespace PIN (> 4 km); 409 within 4 km; competitor names masked for brands. `decision_before` is the server's decision for that PIN under `filters`; `decision_after` can only be lower |
| `GET` | `/v1/admin/unique-summary` | Admin only. Unique similarity garments by the customers' current PIN and brand, from BQ `garment_items` (`pin`, `brand`) |
| `GET` | `/v1/admin/unique-garments` | Admin only. Most recently photographed unique garments, same filters, from BQ `garment_items` (`limit` ≤ 200) |
| `GET` | `/v1/admin/intel/customers-overview` | Admin only. Total / qualified users, cohort size, RFM breakdown, age × gender (`rfm`, `pin` CSV filters) |
| `GET` | `/v1/admin/intel/customers` | Admin only. Paged directory; `q` = name or phone digits, `rfm`, `pin`, `qualified`, `page`, `size`. Phones masked (`+91 98xxx xx210`) |
| `GET` | `/v1/admin/intel/customers/{id}` | Admin only. Person-level profile: commodities, colours, sizes, service split, garment attributes, monthly spending momentum |
| `POST` | `/v1/admin/intel/customers/{id}/reveal-phone` | Admin only. Returns the full phone and logs `admin=<user> customer=<short id> action=reveal_phone` (never the number) |
| `GET` | `/v1/admin/intel/garments` | Admin only. Garment cards + fabric / pattern / size / category × fabric aggregates (`commodity`, `brand`, `fabric`, `pattern`, `garment_size`) |
| `GET` | `/v1/admin/intel/brands` | Admin only. Share ranking, treemap, co-ownership heatmap, sunburst, size matrix, regional skew, deep dives (`brands`, `pin`, `rfm`, `commodity`, `menu`) |
| `GET` | `/v1/admin/intel/campaigns` | Admin only. Campaign cards with mapped status + **simulated** conversion / redemptions (`brand`, `status`, `pin`, `rfm`) |
| `GET` | `/v1/style-assistant/feed` | Customer only (403 for brand / admin). Saved picks from Extract &amp; Rank for the logged-in customer (`brand`, `limit`) |
| static | `/login.html`, `/index.html`, `/geo.html`, `/ads_lab.html`, `/style_assistant.html`, `/admin.html`, `/session.js` | UI |

**Extraction:** `suffai-extraction` → `POST /v1/extract` (Vertex-enabled in your env). Its response carries `item_attributes`, the same colour / garment name / brand / pattern / size / material / sleeve / collar / gender values it writes to `commodities` for customer photos; Command Center stores brand SKUs with them. An older extraction without `item_attributes` leaves SKU attributes blank and Command Center logs a warning.

**Similarity:** `suffai-similarity` keeps the embeddings and serves `POST /v1/similarity/compare-pins` and `/v1/similarity/process`. It has no admin endpoints (`/v1/admin/unique-garments*` were removed) and no longer keeps Cloud SQL `garment_pin_counts`; BigQuery `garment_items` is the only count of garments per PIN.

**Combined surface (baseline + Feature 1):** Geo UI + Ads Lab campaign ranking + corner Bot chat + legacy extract endpoint. Do **not** overwrite `command_center/` or `web/` from the `temp/` archive (pre–Feature 1 baseline). Chat lives on CC `POST /v1/chat` only — the retired `suffai-api` / `api/routers/chat.py` path is not restored.

**Chat UI:** floating corner **Bot** on `index.html` (Geo + Ads Lab only; hidden on Admin). Uses a summary of the live BigQuery metrics on the server. Request body may include optional `filter_context` (PIN/age/gender/RFM/brand) merged into the Vertex prompt.

**Admin tab:** `admin.html` has four tabs: Customer Intelligence, Wardrobe & Garments, Brands Intelligence and Campaigns Management. Every `/v1/admin/*` route requires an admin session and returns `Cache-Control: no-store` for intel payloads.
- **Data:** real data only. BigQuery `customers` / `orders` / `commodities` (cached in-process for `ADMIN_INTEL_CACHE_SEC`, default 600, and dropped within `BQ_FRESHNESS_SEC`, default 30, of a write from outside the app) plus Cloud SQL `brand_campaigns` / `brand_skus`. Missing fields render as "No data"; nothing is synthesised. The image-deduplicated garments section reads BigQuery `garment_items` under the customers' current PINs, the same count as the Ads Lab "Photographed garments" tile.
- **Qualified users:** at least 3 orders and an order within 60 days of the latest order date in the dataset.
- **Menu catalog** is derived from each garment's `gender` field.
- **Campaign status:** `launched` within its end date (or launch + duration) → Active, past it → Completed; `configured` with a future `start_date` → Scheduled; other `configured` / `ranked` → Draft; `ignored` → Discarded. Conversion % = simulated conversions ÷ simulated reach; redemptions = simulated conversions. Both are labelled Simulated.
- **PII:** names and masked phones are visible to admin sessions only. Full phones need an explicit Reveal (logged server-side without the number). None of this reaches AURA, the corner Bot or brand logins; load errors are returned as a generic 503.

**Ads Lab campaign (Part 1):** brand name + multi-PIN + age/gender/RFM → `POST /v1/ads-lab/campaign`. Brand overwritten in BFF. Ranking blends PIN unique-garment similarity (`suffai-similarity` `/v1/similarity/compare-pins`) with metrics taste for the full audience. Command Center sends each selected PIN's garments (`candidates`, most-photographed first, up to `COMPARE_PIN_CANDIDATE_LIMIT`, default 2000) and photo totals (`pin_totals`) from BigQuery `garment_items`, so the comparison uses the same garments as the Ads Lab "Photographed garments" tile and Admin **Unique garments**. Attribute fit and the Style Assistant compare shared values (`navy` = `Navy Blue`, `t_shirt` = `T-Shirt`). Tables: `brand_campaigns`, `brand_skus`, `brand_sku_pin_scores` (migration `005_brand_sku_campaigns.sql`). Requires `SIMILARITY_URL` + `DATABASE_URL` on command-center. `scripts/normalize_sku_attributes.py` (dry run, then `--apply`) rewrites SKU attributes stored before the shared values.

**Style Assistant (Part 2):** tab `style_assistant.html` — pick `user_uuid` + advertising brand → `POST /v1/style-assistant/recommend`. Response sections: **`complete_outfit`** (complementary pairs + Vertex copy), **`similar_likes`** (same-category “you may also like”), **`wardrobe`**. Feature 1 blend for scoring. Buy CTA uses optional `STYLE_BUY_URL_TEMPLATE` (`{sku_uuid}`, `{brand_name}`). Needs `DATABASE_URL`, `ENABLE_VERTEX=true`, GCS signed URLs. No new migration (005 + wardrobe tables).

**Geo store placement:** the server scores PINs and the browser only draws them. `command_center/geo_analysis.py` is the only store scorer and decision rule, and the Geo map (`POST /v1/geo/scores`), the brief, the market check and AURA (`get_metrics_summary`, `analyze_geo`, `run_geo_brief`) all go through `command_center/geo_service.py`, so the same PIN and filters get the same score and Yes / Consider / No everywhere. Hub **centroid is fixed at pincode** (`pin_geo`). Demo filters change Estimated footfall (simulated). The score blends Champions & Loyalists-weighted demand (60% of the demand term), brand garments, catchment population, revenue per customer and **brand vs competitor garment share** at the PIN. The rule: footfall under 40 is No; with a known share, Yes needs share ≥ 0.35, footfall ≥ 60 and score ≥ 0.33, and Consider needs share ≥ 0.22, or 3+ brand garments and score ≥ 0.33; without a share, Yes at score ≥ 0.66 and Consider at ≥ 0.33; Yes needs Champions & Loyalists demand. Admins with no single brand filter get the brand with the most garments among the selected audience and PINs. `POST /v1/geo/brief` then asks Gemini for `decision` + full `explanation` + `brief` (JSON); the rule's decision stays binding, and the same decision is returned if Vertex fails (`fallback=true`). The one difference from the map: if the external market check has been run for that PIN, the brief applies it, and it can only lower the decision. No per-user lat/lon or PII in the prompt. A PIN within 4 km of an existing store of the brand is always **No · Service via Existing Store** (see [DEPLOY_CAMPAIGN_IMPACT_METRICS.md](./DEPLOY_CAMPAIGN_IMPACT_METRICS.md) Step 4d).

---

## Role-based login (ACL)

**Source of truth:** BigQuery `suffai_ops.login_cred_db` (passwords stored as **SHA-256 hex**). Command Center also falls back to `command_center_data/auth/login_cred_db.csv` when BQ is unreachable (`AUTH_SOURCE=auto|bq|csv`).

| Role | Tabs | Brand visibility | Notes |
|------|------|------------------|-------|
| `brand` | Geo, Ads Lab | Own brand + **Competitors** | Ads brand field locked; campaign API forces `brand_name` |
| `customer` | Style Assistant | n/a | Customer dropdown locked to session `customer_id`; recommend API forces `user_uuid` |
| `admin` | Geo, Ads Lab, Style, Admin | All brands | Full access |

Sessions: random token, ~12h TTL (`AUTH_SESSION_TTL_SEC`), **in-memory** on the Cloud Run instance (restart clears sessions). UI stores `localStorage.suffai_session` and sends `Authorization: Bearer …` / `X-Session-Token`.

### Seed credentials + load into BQ

```bash
# Generate hashed BQ CSV + local plaintext handoff (gitignored)
python3 command_center/scripts/generate_login_creds.py
# → command_center_data/auth/login_cred_db.csv
# → command_center_data/auth/login_creds_plaintext.csv  (distribute offline; never commit)

# Create table (also in create_bq_tables.sql) and load
chmod +x command_center/scripts/load_login_creds_bq.sh
./command_center/scripts/load_login_creds_bq.sh "$GOOGLE_CLOUD_PROJECT"
```

Default seed: **admin1** / **123@admin**; 8 brand users (one per commodity brand); 70 customer users (one per `customers.csv` `_id`). No signup UI.

Schema example: `command_center_data/auth/login_cred_db.example.csv`.

**Smoke (CSV local or live URL):**

```bash
# Local CSV roles
AUTH_SOURCE=csv python3 command_center/scripts/smoke_role_auth.py

# After deploy — login + GET /v1/metrics for admin/brand/customer
CC_URL=$(gcloud run services describe suffai-command-center \
  --region="${GCP_REGION:-asia-south1}" --format='value(status.url)')
CC_URL="$CC_URL" ./command_center/scripts/smoke_role_auth.sh
```

If only `admin1` works in GCP, reload `login_cred_db` (step above) and use the **new** plaintext passwords for brand/customer. Live UI is served from `web/` only — do not deploy conflicted copies under `docs/`.

---

## Errors you will see (no silent fallback)

| Cause | Response |
|--------|----------|
| `USE_BQ=false` | `503` — `BigQuery required: set USE_BQ=true` |
| Project unset | `503` — `GOOGLE_CLOUD_PROJECT is not set` |
| BQ tables / IAM fail | `503` — `BigQuery metrics failed (check tables/IAM): …` |
| Geo UI | Full-page error with HTTP detail |
| Ads Lab filters | Red message under target areas |

Cloud Run logs use prefix `[command_center]`.

---

## Deploy (GCP)

**Already live in GCP?** Prefer **[GCP_REDEPLOY_E2E.md](./GCP_REDEPLOY_E2E.md)** (migration 005 → similarity if needed → command-center → IAM → verify). For the privacy / SKU values / one count release, follow its run order: Cloud SQL migration 011, extraction, Command Center, similarity, optional `normalize_sku_attributes.py --apply` and re-rank, then `reconcile_data.py` (dry run first).

**What gets uploaded:** `.dockerignore` and `.gcloudignore` keep `data_offline/`, the sample zips and `customers.csv`, `captured_garments/`, `archive/`, `.merge_backup`, `.env` and the plaintext / summary login CSVs out of images and Cloud Build uploads; `.gitignore` keeps the raw data and plaintext logins out of git. `Dockerfile.command_center` copies only `command_center_data/auth/login_cred_db.csv` (hashed passwords for the CSV login fallback). Check with `gcloud meta list-files-for-upload`.

Prerequisites: `gcloud` authenticated, Docker running, billing enabled, Artifact Registry + Cloud Run + BigQuery APIs. Prefer region `asia-south1`.

### Step 1 — Project & region

```bash
cd /Users/rahul.kumar/Downloads/AI_CUP/AI_CUP_latest

export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT
export GCP_REGION=asia-south1
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
```

### Step 2 — Load BigQuery tables (required for Geo)

Sample CSVs are for **loading into BQ only**, not runtime fallback:

```bash
chmod +x command_center/scripts/load_to_bq.sh
./command_center/scripts/load_to_bq.sh "$GOOGLE_CLOUD_PROJECT" ./command_center_data/sample

# Login credentials (required for role-based UI)
python3 command_center/scripts/generate_login_creds.py
chmod +x command_center/scripts/load_login_creds_bq.sh
./command_center/scripts/load_login_creds_bq.sh "$GOOGLE_CLOUD_PROJECT"
```

Cloud Run service account needs BigQuery Job User + Data Viewer (or equivalent) on dataset `suffai_ops` (including `login_cred_db`). No new secrets beyond existing ADC / SA for BQ.

### Step 3 — Point Ads Lab extract at extraction service

```bash
export EXTRACTION_URL=$(gcloud run services describe suffai-extraction \
  --region="$GCP_REGION" \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')
```

### Step 4 — Build & deploy `suffai-command-center`

```bash
chmod +x deploy/deploy_command_center.sh
./deploy/deploy_command_center.sh
```

Env set by the script (defaults):

- `USE_BQ=true`
- `BQ_DATASET=suffai_ops`
- `GOOGLE_CLOUD_PROJECT`
- `GCS_IMAGE_BUCKET` (default `${PROJECT}-suffai-garment-images`)
- `EXTRACTION_URL` (if set)
- `CORS_ORIGINS=*`
- `ENABLE_VERTEX=true` (required for `/v1/chat`)
- `VERTEX_LOCATION=asia-south1`
- `VERTEX_CHAT_MODEL=gemini-2.5-flash`

Optional: `COMPARE_PIN_CANDIDATE_LIMIT` (default 2000) caps garments per PIN in the Ads Lab tile, Admin and the `compare-pins` list. It is a Command Center variable now; similarity only bounds a request (`COMPARE_MAX_CANDIDATES`, default 100000; `COMPARE_EMBED_CACHE_MAX`, default 20000).

Cloud Run SA needs **Vertex AI User** for chat.

### Step 5 — (Optional) Cloud SQL for `extraction_jobs_count`

Attach Cloud SQL and set `DATABASE_URL` via Secret Manager. Without it, Geo still works; job count is omitted.

### Step 6 — Verify

```bash
URL=$(gcloud run services describe suffai-command-center \
  --region="$GCP_REGION" --project="$GOOGLE_CLOUD_PROJECT" \
  --format='value(status.url)')

curl -sS "$URL/health"
curl -sS "$URL/v1/metrics" | head
# Chat (needs ENABLE_VERTEX=true; reads the live BigQuery metrics):
curl -sS -X POST "$URL/v1/chat" -H 'Content-Type: application/json' \
  -d '{"message":"Summarize top brands","context":{"active_tab":"geo"}}'
# Open: $URL/login.html  — use admin1 / brand_* / cust_* from plaintext handoff
# (demo seed admin: admin1 / 123@admin). Corner Bot on Geo/Ads for brand+admin only.

# Admin intelligence (admin token required; brand token → 403, none → 401)
TOKEN=$(curl -sS -X POST "$URL/v1/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin1","password":"..."}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -sS "$URL/v1/admin/intel/customers-overview" -H "X-Session-Token: $TOKEN" | head -c 400
curl -sS "$URL/v1/admin/intel/customers?q=98&size=5" -H "X-Session-Token: $TOKEN" | head -c 400
curl -sS "$URL/v1/admin/intel/campaigns" -H "X-Session-Token: $TOKEN" | head -c 400
```

**Admin smoke checklist:** each of the four tabs loads with charts; customer search by name and by phone digits works; Details expands the profile; Reveal shows the full phone and Cloud Logging shows `action=reveal_phone` with no number; garment and brand filters change the charts; campaign cards show Simulated labels and "All columns" shows the full table; a brand login gets 403 on `/v1/admin/*`.

### Alternative — Cloud Build

```bash
gcloud builds submit --config=deploy/cloudbuild-command-center.yaml .
```

---

## Local (still needs BigQuery)

```bash
cd /Users/rahul.kumar/Downloads/AI_CUP/AI_CUP_latest
export USE_BQ=true
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT
export BQ_DATASET=suffai_ops
# ADC: gcloud auth application-default login

pip install -r command_center/requirements.txt
uvicorn command_center.main:app --host 127.0.0.1 --port 8083
# → http://127.0.0.1:8083/login.html
```

Or: `docker compose up --build command_center` (with the same env).

---

## Notes

- Does **not** replace `suffai-extraction` or `suffai-similarity`.  
- `reconcile_data.py` reports similarity events still waiting for BigQuery and, with `--apply`, queues matched photos that BigQuery has no observation for so similarity resends them. It no longer rebuilds `garment_pin_counts` (unused; the table is left in place).  
- Pub/Sub + Scheduler CDC / BQ sync jobs are part of the wider stack (`deploy/scheduler.yaml`); leave them as configured.  
- Old Next.js UI under `web/` has been removed; `web/` is HTML only.
