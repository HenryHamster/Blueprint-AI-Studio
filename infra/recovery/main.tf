locals {
  recovery_enabled = var.enable_recovery_backup_stack
}

# Fail closed: an apply cannot quietly create a bucket in the production
# project, without an explicit recovery administrator, or with invalid archive
# retention.
resource "terraform_data" "recovery_configuration" {
  count = local.recovery_enabled ? 1 : 0

  input = {
    recovery_project_id = var.recovery_project_id
    source_project_id   = var.source_project_id
  }

  lifecycle {
    precondition {
      condition     = var.recovery_project_id != "" && var.recovery_project_id != var.source_project_id
      error_message = "recovery_project_id must be set and must differ from source_project_id."
    }
    precondition {
      condition     = var.recovery_backup_bucket_name != ""
      error_message = "recovery_backup_bucket_name must be set when enable_recovery_backup_stack=true."
    }
    precondition {
      condition     = lower(var.recovery_kms_location) == lower(var.recovery_bucket_location)
      error_message = "recovery_kms_location and recovery_bucket_location must match so the bucket can use the CMEK."
    }
    precondition {
      condition     = can(regex("^serviceAccount:[^@]+@[^@]+$", var.source_cloud_sql_service_account_email))
      error_message = "source_cloud_sql_service_account_email must use IAM member form, e.g. serviceAccount:..."
    }
    precondition {
      condition = can(regex(
        "^(user|group):[^@[:space:]]+@[^@[:space:]]+$",
        var.recovery_admin_member,
      ))
      error_message = "recovery_admin_member must be a user or Google Group in IAM member form."
    }
    precondition {
      condition     = var.retention_period_seconds >= 7776000 && var.archive_expiry_days >= 90
      error_message = "Recovery archives require at least 90 days of retention and expiry."
    }
  }
}

resource "google_project_service" "required" {
  for_each = local.recovery_enabled ? toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudscheduler.googleapis.com",
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]) : toset([])

  project            = var.recovery_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "terraform_data" "restore_verifier_configuration" {
  count = local.recovery_enabled && var.enable_restore_verifier ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.restore_verifier_image != "" &&
        var.restore_verifier_private_network != "" &&
        var.restore_verifier_private_subnetwork != "" &&
        var.restore_verifier_expected_revision != ""
      )
      error_message = "restore verifier image, network, subnetwork, and expected revision are required."
    }
  }
}

resource "google_project_service_identity" "cloud_sql" {
  provider = google-beta
  count    = local.recovery_enabled ? 1 : 0
  project  = var.recovery_project_id
  service  = "sqladmin.googleapis.com"

  depends_on = [google_project_service.required]
}

# Google can return newly created managed service-agent addresses before those
# principals are visible to Cloud KMS IAM. Give IAM propagation a bounded head
# start so a first apply does not fail halfway through with "does not exist".
resource "time_sleep" "service_agent_propagation" {
  count           = local.recovery_enabled ? 1 : 0
  create_duration = "30s"

  depends_on = [
    data.google_storage_project_service_account.recovery,
    google_project_service_identity.cloud_sql,
  ]
}
