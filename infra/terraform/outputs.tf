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

# AlloyDB outputs removed when alloydb.tf was archived 2026-05-02. Wave F4 M9
# wrapped these in `try()` thinking that would survive a missing resource,
# but `terraform validate` fails at parse-time on the undeclared reference —
# `try()` only catches evaluation-time errors, not undeclared-resource ones.
# Restore by re-adding alongside alloydb.tf restore (see archive/README.md).

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

output "cloud_sql_service_account_email" {
  value       = var.use_cloud_sql ? "serviceAccount:${google_sql_database_instance.default[0].service_account_email_address}" : ""
  description = "Cloud SQL instance service account in IAM member form. Pass this to infra/recovery for writer-only archive access."
}

output "compute_images_repository_url" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.compute_images_repo.repository_id}"
  description = "The Artifact Registry URL for the project-baked compute images repository"
}

output "artifact_bucket_name" {
  value       = google_storage_bucket.artifacts.name
  description = "The GCS bucket for agent payloads and credentials"
}

output "local_cloudsql_proxy_service_account" {
  value       = google_service_account.local_cloudsql_proxy_sa.email
  description = "Dedicated Cloud SQL Auth Proxy identity for an external control-plane host"
}

output "local_worker_service_account" {
  value       = google_service_account.local_worker_sa.email
  description = "Dedicated background-worker runtime identity for an external control-plane host"
}

output "local_api_service_account" {
  value       = google_service_account.local_api_sa.email
  description = "Dedicated public-API runtime identity for an external control-plane host"
}
