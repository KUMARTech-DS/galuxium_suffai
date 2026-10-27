variable "project_id" { type = string }
variable "region" { type = string }
variable "enabled" { type = bool }
# Private IP PostgreSQL for operational entities
output "instance_name" {
  value = var.enabled ? "suffai-ops" : null
}
