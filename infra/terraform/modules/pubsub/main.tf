variable "project_id" { type = string }
variable "enabled" { type = bool }

resource "google_pubsub_topic" "garment_uploaded" {
  count   = var.enabled ? 1 : 0
  name    = "garment-image-uploaded"
  project = var.project_id
}

resource "google_pubsub_topic" "garment_uploaded_dlq" {
  count   = var.enabled ? 1 : 0
  name    = "garment-image-uploaded-dlq"
  project = var.project_id
}

resource "google_pubsub_subscription" "garment_push" {
  count   = var.enabled ? 1 : 0
  name    = "garment-image-uploaded-push"
  topic   = google_pubsub_topic.garment_uploaded[0].name
  project = var.project_id

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.garment_uploaded_dlq[0].id
    max_delivery_attempts = 5
  }

  # push_config configured after Cloud Run worker URL is known
}

output "topics" {
  value = compact([
    try(google_pubsub_topic.garment_uploaded[0].name, null),
    try(google_pubsub_topic.garment_uploaded_dlq[0].name, null),
  ])
}
