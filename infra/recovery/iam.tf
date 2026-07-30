# Cloud SQL itself writes the export. This grant deliberately excludes object
# read/list/delete and excludes all KMS access, so the production project
# cannot recover or remove an archive after it has been written.
resource "google_storage_bucket_iam_member" "source_cloud_sql_writer" {
  count  = local.recovery_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectCreator"
  member = var.source_cloud_sql_service_account_email
}

# The recovery-admin group is controlled outside the production project. It is
# the only application-level principal granted both archive reads and KMS
# decrypt, so recovery remains possible without hamsterwheel credentials.
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
