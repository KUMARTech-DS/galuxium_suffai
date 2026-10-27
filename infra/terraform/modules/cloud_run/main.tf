variable "project_id" { type = string }
variable "region" { type = string }
variable "enabled" { type = bool }
variable "api_image" {
  type    = string
  default = ""
}
variable "web_image" {
  type    = string
  default = ""
}
variable "worker_image" {
  type    = string
  default = ""
}

resource "google_cloud_run_v2_service" "api" {
  count    = var.enabled && var.api_image != "" ? 1 : 0
  name     = "suffai-api"
  location = var.region
  project  = var.project_id

  template {
    containers {
      image = var.api_image
      ports { container_port = 8080 }
      env {
        name  = "ENVIRONMENT"
        value = "staging"
      }
      env {
        name  = "PORT"
        value = "8080"
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "web" {
  count    = var.enabled && var.web_image != "" ? 1 : 0
  name     = "suffai-web"
  location = var.region
  project  = var.project_id

  template {
    containers {
      image = var.web_image
      ports { container_port = 8080 }
      env {
        name  = "PORT"
        value = "8080"
      }
    }
  }
}

resource "google_cloud_run_v2_service" "worker" {
  count    = var.enabled && var.worker_image != "" ? 1 : 0
  name     = "suffai-extraction"
  location = var.region
  project  = var.project_id

  template {
    containers {
      image = var.worker_image
      ports { container_port = 8080 }
      env {
        name  = "ENABLE_VERTEX"
        value = "false"
      }
    }
  }
}

output "services" {
  value = compact([
    try(google_cloud_run_v2_service.api[0].name, null),
    try(google_cloud_run_v2_service.web[0].name, null),
    try(google_cloud_run_v2_service.worker[0].name, null),
  ])
}
