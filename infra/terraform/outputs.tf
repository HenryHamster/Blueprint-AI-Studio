output "vpc_name" {
  value       = google_compute_network.agent_vpc.name
  description = "The name of the VPC network"
}

output "subnet_name" {
  value       = google_compute_subnetwork.agent_subnet.name
  description = "The name of the agent subnet"
}

output "orchestrator_url" {
  value       = google_cloud_run_v2_service.orchestrator.uri
  description = "The public URL of the Orchestrator Cloud Run service"
}

output "orchestrator_service_account" {
  value       = google_service_account.orchestrator_sa.email
  description = "The Service Account email used by the Orchestrator"
}

output "agent_service_account" {
  value       = google_service_account.agent_sa.email
  description = "The restricted Service Account email used by the Agent VMs"
}

output "workload_identity_provider" {
  value       = var.enable_cicd ? google_iam_workload_identity_pool_provider.github_provider[0].name : ""
  description = "The Workload Identity Provider ID to use in GitHub Actions"
}

output "github_actions_service_account" {
  value       = var.enable_cicd ? google_service_account.github_actions_sa[0].email : ""
  description = "The Service Account email that GitHub Actions will impersonate"
}

# Wave F4 M9: wrapped in `try()` so when alloydb.tf is removed post-cutover,
# `terraform plan` does not fail trying to resolve a missing
# `google_alloydb_instance.primary` / `db_password_secret`. HCL's `try()`
# short-circuits on a missing-reference error and falls back to the empty
# string. This lets the migration runbook delete alloydb.tf in one commit
# without also having to delete these outputs in the same commit (a
# previous note in this file claimed that was infeasible — `try()` is the
# fix). Once alloydb.tf is gone these outputs will resolve to "" and can
# be cleaned up at leisure.
output "alloydb_ip_address" {
  value       = try(google_alloydb_instance.primary.ip_address, "")
  description = "Private IP of AlloyDB primary (empty once alloydb.tf is removed)"
}

output "alloydb_password_secret_id" {
  value       = try(google_secret_manager_secret.db_password_secret.secret_id, "")
  description = "Secret Manager ID of AlloyDB password (empty once alloydb.tf is removed)"
}

output "cloud_sql_ip_address" {
  value       = var.use_cloud_sql ? google_sql_database_instance.default[0].private_ip_address : ""
  description = "The private IP address of the Cloud SQL Postgres instance (empty unless use_cloud_sql=true)"
}

output "cloud_sql_password_secret_id" {
  value       = var.use_cloud_sql ? google_secret_manager_secret.cloud_sql_db_password_secret[0].secret_id : ""
  description = "The Secret Manager ID where the Cloud SQL password is stored (empty unless use_cloud_sql=true)"
}

output "cloud_sql_instance_name" {
  value       = var.use_cloud_sql ? google_sql_database_instance.default[0].name : ""
  description = "The Cloud SQL instance name (used by gcloud sql commands)"
}

output "artifact_bucket_name" {
  value       = google_storage_bucket.artifacts.name
  description = "The GCS bucket for agent payloads and credentials"
}
