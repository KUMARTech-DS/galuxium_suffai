variable "project_id" { type = string }
variable "region" { type = string }
variable "enabled" { type = bool }

resource "google_storage_bucket" "raw" {
  count                       = var.enabled ? 1 : 0
  name                        = "${var.project_id}-suffai-raw"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  versioning { enabled = true }
  force_destroy = false
}

resource "google_storage_bucket" "garment_images" {
  count                       = var.enabled ? 1 : 0
  name                        = "${var.project_id}-suffai-garment-images"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = false
}

output "buckets" {
  value = compact([
    try(google_storage_bucket.raw[0].name, null),
    try(google_storage_bucket.garment_images[0].name, null),
  ])
}
