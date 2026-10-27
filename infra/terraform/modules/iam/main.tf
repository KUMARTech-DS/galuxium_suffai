variable "project_id" { type = string }
variable "enabled" { type = bool }

# Service accounts (created when enabled): web, api, ingestion, extraction, analytics
output "service_accounts" {
  value = var.enabled ? ["suffai-web", "suffai-api", "suffai-ingestion", "suffai-extraction", "suffai-analytics"] : []
}
