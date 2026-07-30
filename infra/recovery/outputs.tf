output "recovery_backup_bucket_name" {
  value       = local.recovery_enabled ? google_storage_bucket.db_backup[0].name : ""
  description = "Recovery-project archive bucket name; pass this to the production db_backup Terraform variables only after this stack is applied."
}

output "recovery_backup_kms_key" {
  value       = local.recovery_enabled ? google_kms_crypto_key.db_backup[0].id : ""
  description = "Recovery-project CMEK protecting archive objects."
}

output "local_postgres_backup_writer_email" {
  value       = local.recovery_enabled && var.enable_local_postgres_backup_writer ? google_service_account.local_postgres_backup_writer[0].email : ""
  description = "Dedicated writer-only service-account email expected in the local PostgreSQL backup credential file."
}

output "recovery_project_number" {
  value       = local.recovery_enabled ? tostring(data.google_project.recovery[0].number) : ""
  description = "Authoritative project number required by the restore verifier bucket-scope check."
}

output "recovery_cloud_sql_service_identity" {
  value       = local.recovery_enabled ? google_project_service_identity.cloud_sql[0].email : ""
  description = "Recovery-only Cloud SQL service identity granted archive read/decrypt for restore drills."
}
