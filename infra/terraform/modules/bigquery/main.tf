variable "project_id" { type = string }
variable "enabled" { type = bool }

resource "google_bigquery_dataset" "raw" {
  count       = var.enabled ? 1 : 0
  dataset_id  = "suffai_raw"
  project     = var.project_id
  location    = "asia-south1"
  description = "Immutable source snapshots"
}

resource "google_bigquery_dataset" "staging" {
  count       = var.enabled ? 1 : 0
  dataset_id  = "suffai_staging"
  project     = var.project_id
  location    = "asia-south1"
  description = "Validated / unnested staging"
}

resource "google_bigquery_dataset" "marts" {
  count       = var.enabled ? 1 : 0
  dataset_id  = "suffai_marts"
  project     = var.project_id
  location    = "asia-south1"
  description = "Privacy-safe aggregates"
}

output "datasets" {
  value = compact([
    try(google_bigquery_dataset.raw[0].dataset_id, null),
    try(google_bigquery_dataset.staging[0].dataset_id, null),
    try(google_bigquery_dataset.marts[0].dataset_id, null),
  ])
}
