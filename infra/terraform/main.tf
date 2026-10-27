# Suffai GCP skeleton — do not apply until project/region/billing confirmed.
# Projects: suffai-dev, suffai-staging, suffai-prod

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  # backend "gcs" {}  # enable after bucket exists
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type        = string
  description = "GCP project (e.g. suffai-dev)"
}

variable "region" {
  type        = string
  description = "Primary region — confirm residency before apply"
  default     = "asia-south1"
}

variable "enable_resources" {
  type        = bool
  description = "Safety latch: false skips resource creation"
  default     = false
}

locals {
  labels = {
    app     = "suffai"
    managed = "terraform"
  }
}

# Placeholder module wiring — implementations under modules/*
module "iam" {
  source       = "./modules/iam"
  project_id   = var.project_id
  enabled      = var.enable_resources
}

module "gcs" {
  source     = "./modules/gcs"
  project_id = var.project_id
  region     = var.region
  enabled    = var.enable_resources
}

module "bigquery" {
  source     = "./modules/bigquery"
  project_id = var.project_id
  enabled    = var.enable_resources
}

module "pubsub" {
  source     = "./modules/pubsub"
  project_id = var.project_id
  enabled    = var.enable_resources
}

module "cloud_sql" {
  source     = "./modules/cloud_sql"
  project_id = var.project_id
  region     = var.region
  enabled    = var.enable_resources
}

module "cloud_run" {
  source     = "./modules/cloud_run"
  project_id = var.project_id
  region     = var.region
  enabled    = var.enable_resources
}

variable "scheduler_sa_email" {
  type        = string
  description = "Service account for Cloud Scheduler → Cloud Run Jobs OIDC"
  default     = ""
}

module "scheduler" {
  source              = "./modules/scheduler"
  project_id          = var.project_id
  region              = var.region
  enabled             = var.enable_resources
  scheduler_sa_email  = var.scheduler_sa_email
}

output "notice" {
  value = "Terraform skeleton only. Set enable_resources=true after project/region/billing approval."
}

output "scheduler_jobs" {
  value = module.scheduler.job_names
}
