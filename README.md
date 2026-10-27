# Suffai — Fashion Intelligence (GCP)

Multi-tenant B2B fashion demand intelligence on **Google Cloud**.

| Unit | Cloud Run service | Stack |
|------|-------------------|--------|
| Web | Command Center | FastAPI, serves `web/*.html` |
| API / BFF | `suffai-api` | FastAPI + governed `suffai` core |
| Extraction worker | `suffai-extraction` | FastAPI Pub/Sub push consumer |
| Ops DB | Cloud SQL PostgreSQL | `db/migrations/` |
| Analytics | BigQuery | `analytics/bigquery/` |
| Images / raw | GCS | private buckets |
| Events | Pub/Sub + DLQ | garment upload |

## Architecture

```
Command Center (FastAPI, serves web/*.html) → FastAPI BFF (Cloud Run)
                         ├─ Cloud SQL (garments, consent, tenants, campaigns)
                         ├─ BigQuery marts (geo, RFM, brand aggregates)
                         ├─ GCS signed uploads
                         └─ Vertex Gemini (opt-in)
GCS upload event → Pub/Sub → extraction worker (Cloud Run)
```

Laundry service quantities are **not** garment purchases. Brand share requires verified consented garment observations.

## Local GCP-parity

```bash
# API
python3 -m pip install -r requirements.txt --index-url https://pypi.org/simple
PYTHONPATH=. uvicorn api.main:app --host 0.0.0.0 --port 8080

# Or full stack via Docker
docker compose up --build
# API http://localhost:8080/docs  ·  Command Center (web UI) http://localhost:8083
```

Demo brand login: any listed brand · password `brand123`.

## Tests

```bash
PYTHONPATH=. python3 -m pytest tests/ -q   # includes the frontend suite when Node 20+ is installed
node --test tests/frontend/                 # frontend only (web/ pages, no npm install needed)
python3 scripts/run_integrity_report.py
```

## Deploy to GCP

**One command (recommended):** `./deploy/deploy_all.sh YOUR_PROJECT_ID` from Cloud Shell or VS Code sets up and deploys everything into an otherwise empty project. See [docs/DEPLOY_ALL.md](docs/DEPLOY_ALL.md).

**Manual redeploy:** [docs/GCP_REDEPLOY_E2E.md](docs/GCP_REDEPLOY_E2E.md) — migrations, Pub/Sub, and service redeploy order per release.

**Command Center APIs, layout and first-time setup:** [docs/GCP_COMMAND_CENTER.md](docs/GCP_COMMAND_CENTER.md).

1. Confirm **project ID, region/residency, billing** (required before apply).
2. Copy `deploy/env.example` → set secrets in Secret Manager (not git).
3. Terraform (gated):

```bash
cd infra/terraform
terraform init
terraform plan -var="project_id=YOUR_PROJECT" -var="enable_resources=false"
# Only after approval:
terraform apply -var="project_id=YOUR_PROJECT" -var="enable_resources=true"
```

4. Build/deploy services:

```bash
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT
export GCP_REGION=asia-south1
chmod +x deploy/deploy_cloud_run.sh
./deploy/deploy_cloud_run.sh
```

Or use `cloudbuild.yaml` with Artifact Registry.

## Repository layout

```
api/                 FastAPI Cloud Run BFF
web/                 Command Center dashboard (static HTML)
command_center/      Command Center FastAPI (Geo, Ads Lab, Campaigns, AURA)
similarity/          Similarity Pub/Sub service
worker/              Extraction Pub/Sub worker
suffai/              Governed ingest + metrics + privacy + garments
config/settings.py   GCP env contract
db/migrations/       Cloud SQL
analytics/bigquery/  Warehouse DDL
infra/terraform/     IAM, GCS, BQ, Pub/Sub, Cloud Run, SQL
contracts/openapi.yaml
deploy/              env example + deploy scripts
```

## Environment (Cloud Run)

| Variable | Purpose |
|----------|---------|
| `PORT` | Cloud Run port (8080) |
| `WEB_ORIGIN` | CORS allowlist (web service URL) |
| `DATA_DIR` / `GCS_BUCKET` | Fixture path or GCS sync |
| `DATABASE_URL` / `CLOUD_SQL_INSTANCE` | Operational PostgreSQL |
| `BQ_PROJECT` | BigQuery project |
| `JWT_SECRET` | Demo auth (replace with Identity Platform) |
| `ENABLE_VERTEX` | Extraction paid API latch (default false) |

See [docs/SUFFAI_PRD.md](docs/SUFFAI_PRD.md) and [docs/SUFFAI_PROJECT_OVERVIEW.md](docs/SUFFAI_PROJECT_OVERVIEW.md).

## PRD features (v2.1) — redeploy note

Existing geo / auth / segments / chat contracts are unchanged. New additive APIs power Capture, Review, Brand catalog, Research, Campaign Lab, and CSV export.

Redeploy **`suffai-api`** and Command Center only (worker already has GCS→Vertex→SQL). Optional env: `EXTRACTION_WORKER_URL`, `GCS_IMAGE_BUCKET`, `PUBSUB_IMAGE_TOPIC`.
