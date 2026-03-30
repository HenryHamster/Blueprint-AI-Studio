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

output "alloydb_ip_address" {
  value       = google_alloydb_instance.primary.ip_address
  description = "The private IP address of the AlloyDB primary instance"
}

output "alloydb_password_secret_id" {
  value       = google_secret_manager_secret.db_password_secret.secret_id
  description = "The Secret Manager ID where the AlloyDB password is stored"
}

output "artifact_bucket_name" {
  value       = google_storage_bucket.artifacts.name
  description = "The GCS bucket for agent payloads and credentials"
}
