# suffai-extraction (image worker)

Cloud Run service that:

1. Reads an image from **GCS**
2. Extracts attributes with **Vertex AI Gemini**
3. Writes results to **Cloud SQL** (`extraction_jobs`, `garment_instances`, or `campaigns.metadata`)

Does **not** require redeploying `suffai-api` or Command Center.

## Env vars (Cloud Run → `suffai-extraction`)

| Name | Example | Required |
|------|---------|----------|
| `PORT` | `8080` | yes (Cloud Run) |
| `GOOGLE_CLOUD_PROJECT` | your project id | yes for Vertex |
| `GCS_IMAGE_BUCKET` | `{PROJECT}-suffai-garment-images` | yes |
| `ENABLE_VERTEX` | `true` | yes to call Gemini |
| `VERTEX_LOCATION` | `asia-south1` | recommended |
| `VERTEX_MODEL` | `gemini-2.0-flash` | optional |
| `DATABASE_URL` | see below | yes to persist |
| `AUTO_ACCEPT_CONFIDENCE` | `0.90` | optional |

### `DATABASE_URL` (Cloud SQL unix socket)

```text
postgresql://USER:PASSWORD@/DBNAME?host=/cloudsql/PROJECT:REGION:INSTANCE
```

Also attach the Cloud SQL instance on the Cloud Run service (`suffai-ops` or your instance id).

## IAM (worker runtime SA)

- `roles/storage.objectViewer` on the image bucket
- `roles/aiplatform.user` (Vertex)
- Cloud SQL Client on the instance
- Secret access if password comes from Secret Manager

## Pub/Sub payload (`garment-image-uploaded`)

```json
{
  "gcs_object_key": "garments/order123/item1.jpg",
  "gcs_bucket": "optional-override",
  "image_id": "uuid-of-garment_images-row",
  "garment_instance_id": "uuid-of-garment_instances-row",
  "idempotency_key": "unique-per-upload",
  "target_type": "garment"
}
```

Campaign creative:

```json
{
  "gcs_object_key": "campaigns/creative1.jpg",
  "campaign_id": "uuid-of-campaigns-row",
  "target_type": "campaign",
  "idempotency_key": "campaign-extract-1"
}
```

## Manual test (no Pub/Sub)

```bash
WORKER_URL=$(gcloud run services describe suffai-extraction --region=asia-south1 --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token)

curl -sS -X POST "$WORKER_URL/v1/extract" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gcs_object_key": "garments/sample.jpg",
    "garment_instance_id": "YOUR-GARMENT-UUID",
    "image_id": "YOUR-IMAGE-UUID",
    "idempotency_key": "manual-test-1",
    "target_type": "garment"
  }'
```

## Rebuild / redeploy only the worker

```bash
gcloud builds submit --config=deploy/cloudbuild-worker.yaml .
# then deploy image asia-south1-docker.pkg.dev/$PROJECT/suffai/suffai-extraction:latest
# to Cloud Run service suffai-extraction with the env vars above
```

Apply SQL once if not already applied: `db/migrations/001_operational.sql`.
