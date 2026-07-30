output "recovery_backup_bucket_name" {
  value       = local.recovery_enabled ? google_storage_bucket.db_backup[0].name : ""
  description = "Recovery-project archive bucket name; pass this to the production db_backup Terraform variables only after this stack is applied."
}

output "recovery_backup_kms_key" {
  value       = local.recovery_enabled ? google_kms_crypto_key.db_backup[0].id : ""
  description = "Recovery-project CMEK protecting archive objects."
}
