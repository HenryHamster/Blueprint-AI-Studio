# Cloud SQL itself writes the export. This grant deliberately excludes object
# read/list/delete and excludes all KMS access, so the production project
# cannot recover or remove an archive after it has been written.
resource "google_storage_bucket_iam_member" "source_cloud_sql_writer" {
  count  = local.recovery_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectCreator"
  member = var.source_cloud_sql_service_account_email
}

# Post-cutover local pg_dump uploads use a separate recovery-project identity.
# It can create uniquely named objects but cannot read, list, overwrite, delete,
# or decrypt archives. No key is created or stored by Terraform.
resource "google_service_account" "local_postgres_backup_writer" {
  count        = local.recovery_enabled && var.enable_local_postgres_backup_writer ? 1 : 0
  project      = var.recovery_project_id
  account_id   = "blueprint-local-db-backup"
  display_name = "Blueprint local PostgreSQL backup writer"
  description  = "Writer-only identity for streamed local PostgreSQL recovery exports."

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "local_postgres_backup_writer" {
  count  = local.recovery_enabled && var.enable_local_postgres_backup_writer ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.local_postgres_backup_writer[0].email}"
}

# Cloud SQL imports the selected drill archive server-side. This recovery-only
# service identity can read archive objects and decrypt only this archive key;
# it receives no write/delete permission and no role in the source project.
resource "google_storage_bucket_iam_member" "recovery_cloud_sql_reader" {
  count  = local.recovery_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.cloud_sql[0].email}"
}

resource "google_kms_crypto_key_iam_member" "recovery_cloud_sql_decrypter" {
  count         = local.recovery_enabled ? 1 : 0
  crypto_key_id = google_kms_crypto_key.db_backup[0].id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_project_service_identity.cloud_sql[0].email}"

  depends_on = [time_sleep.service_agent_propagation]
}

# The explicit recovery administrator is the only application-level principal
# granted both archive reads and KMS decrypt. A personal deployment may use one
# Google user; larger deployments should use a separately managed group.
resource "google_storage_bucket_iam_member" "recovery_admin_reader" {
  count  = local.recovery_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectViewer"
  member = var.recovery_admin_member
}

resource "google_kms_crypto_key_iam_member" "recovery_admin_decrypter" {
  count         = local.recovery_enabled ? 1 : 0
  crypto_key_id = google_kms_crypto_key.db_backup[0].id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = var.recovery_admin_member
}
