variable "project_id" { type = string }
variable "region" { type = string }
variable "enabled" { type = bool }
variable "scheduler_sa_email" {
  type    = string
  default = ""
}

# Cloud Run Jobs invoked by Scheduler (images must exist)
resource "google_cloud_run_v2_job" "bq_sync" {
  count    = var.enabled ? 1 : 0
  name     = "suffai-bq-sync"
  location = var.region
  project  = var.project_id

  template {
    template {
      containers {
        image   = "${var.region}-docker.pkg.dev/${var.project_id}/suffai/suffai-job:latest"
        command = ["python", "-m", "jobs.bq_sync"]
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "USE_BIGQUERY_MARTS"
          value = "true"
        }
      }
    }
  }
}

resource "google_cloud_run_v2_job" "cdc" {
  count    = var.enabled ? 1 : 0
  name     = "suffai-cdc"
  location = var.region
  project  = var.project_id

  template {
    template {
      containers {
        image   = "${var.region}-docker.pkg.dev/${var.project_id}/suffai/suffai-job:latest"
        command = ["python", "-m", "jobs.cdc_ingest"]
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
      }
    }
  }
}

resource "google_cloud_scheduler_job" "cdc_hourly" {
  count     = var.enabled && var.scheduler_sa_email != "" ? 1 : 0
  name      = "suffai-cdc-hourly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 * * * *"
  time_zone = "Asia/Kolkata"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/suffai-cdc:run"
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

resource "google_cloud_scheduler_job" "bq_daily" {
  count     = var.enabled && var.scheduler_sa_email != "" ? 1 : 0
  name      = "suffai-bq-marts-daily"
  project   = var.project_id
  region    = var.region
  schedule  = "15 2 * * *"
  time_zone = "Asia/Kolkata"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/suffai-bq-sync:run"
    oauth_token {
      service_account_email = var.scheduler_sa_email
    }
  }
}

output "job_names" {
  value = compact([
    try(google_cloud_run_v2_job.bq_sync[0].name, null),
    try(google_cloud_run_v2_job.cdc[0].name, null),
  ])
}
