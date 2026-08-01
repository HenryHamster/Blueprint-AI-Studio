resource "google_kms_key_ring" "db_backup" {
  count    = local.recovery_enabled ? 1 : 0
  name     = "blueprint-db-backup"
  location = var.recovery_kms_location

  depends_on = [
    terraform_data.recovery_configuration,
    google_project_service.required,
  ]
}

resource "google_kms_crypto_key" "db_backup" {
  count           = local.recovery_enabled ? 1 : 0
  name            = "archive"
  key_ring        = google_kms_key_ring.db_backup[0].id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

data "google_storage_project_service_account" "recovery" {
  count   = local.recovery_enabled ? 1 : 0
  project = var.recovery_project_id

  depends_on = [google_project_service.required["storage.googleapis.com"]]
}

resource "google_kms_crypto_key_iam_member" "storage_service_agent" {
  count         = local.recovery_enabled ? 1 : 0
  crypto_key_id = google_kms_crypto_key.db_backup[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.recovery[0].email_address}"

  depends_on = [time_sleep.service_agent_propagation]

  # The Google-managed Storage identity is immutable for a project. Enabling
  # an unrelated API can defer the data-source read until apply; do not turn
  # that unknown value into a transient destroy/recreate of archive encryption
  # access.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [member]
  }
}

resource "google_storage_bucket" "db_backup" {
  count                       = local.recovery_enabled ? 1 : 0
  name                        = var.recovery_backup_bucket_name
  project                     = var.recovery_project_id
  location                    = var.recovery_bucket_location
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  encryption {
    default_kms_key_name = google_kms_crypto_key.db_backup[0].id
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.retention_period_seconds
    is_locked        = var.lock_retention_policy
  }

  lifecycle_rule {
    condition {
      age = var.archive_expiry_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.storage_service_agent,
  ]
}
