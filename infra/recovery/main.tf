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
    precondition {
      condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.recovery_billing_account_id))
      error_message = "recovery_billing_account_id is required so the isolated recovery project has its own cost alert."
    }
  }
}

resource "google_project_service" "required" {
  for_each = local.recovery_enabled ? toset([
    "cloudresourcemanager.googleapis.com",
    "billingbudgets.googleapis.com",
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

# The restore verifier has heavier networking/build prerequisites than the
# archive writer. Keep them behind a separate switch so operators can create
# and validate the recovery network and image repository before enabling a
# job that is allowed to create temporary Cloud SQL instances.
resource "google_project_service" "restore_verifier_prerequisite" {
  for_each = local.recovery_enabled && var.enable_restore_verifier_prerequisites ? toset([
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "servicenetworking.googleapis.com",
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
        var.enable_restore_verifier_prerequisites &&
        var.enable_restore_janitor &&
        var.restore_verifier_image != "" &&
        startswith(
          var.restore_verifier_image,
          "${var.recovery_region}-docker.pkg.dev/${var.recovery_project_id}/${var.restore_verifier_artifact_repository}/restore-verifier@sha256:",
        ) &&
        var.restore_verifier_private_network != "" &&
        var.restore_verifier_private_subnetwork != "" &&
        var.restore_verifier_expected_revision != ""
      )
      error_message = "restore verifier prerequisites, janitor, recovery-project image, network, subnetwork, and expected revision are required."
    }
    precondition {
      condition = (
        (
          var.restore_verifier_expected_archive_name == "" &&
          var.restore_verifier_expected_archive_generation == "" &&
          var.restore_verifier_expected_manifest_generation == "" &&
          var.restore_verifier_expected_archive_sha256 == "" &&
          var.restore_verifier_expected_archive_size_bytes == 0
        ) ||
        (
          var.restore_verifier_expected_archive_name != "" &&
          var.restore_verifier_expected_archive_generation != "" &&
          var.restore_verifier_expected_manifest_generation != "" &&
          var.restore_verifier_expected_archive_sha256 != "" &&
          var.restore_verifier_expected_archive_size_bytes > 0
        )
      )
      error_message = "the exact archive name, both generations, SHA-256, and byte size must be supplied together or all omitted."
    }
    precondition {
      condition     = !var.enable_restore_drill_schedule
      error_message = "recurring restore drills are disabled until archives use an independently trusted custom-format provenance contract."
    }
  }
}

resource "terraform_data" "restore_janitor_configuration" {
  count = local.recovery_enabled && var.enable_restore_janitor ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.enable_restore_verifier_prerequisites &&
        var.restore_verifier_image != "" &&
        startswith(
          var.restore_verifier_image,
          "${var.recovery_region}-docker.pkg.dev/${var.recovery_project_id}/${var.restore_verifier_artifact_repository}/restore-verifier@sha256:",
        ) &&
        var.restore_verifier_private_network != "" &&
        var.restore_verifier_private_subnetwork != "" &&
        var.restore_verifier_expected_revision != ""
      )
      error_message = "restore janitor requires managed prerequisites, the recovery-project verifier image, network, subnetwork, and expected revision."
    }
  }
}

resource "terraform_data" "restore_verifier_prerequisite_configuration" {
  count = local.recovery_enabled && var.enable_restore_verifier_prerequisites ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.restore_verifier_private_network != "" &&
        var.restore_verifier_private_subnetwork != ""
      )
      error_message = "restore verifier network and subnetwork names are required when prerequisites are enabled."
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

resource "google_project_service_identity" "cloud_run" {
  provider = google-beta
  count    = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project  = var.recovery_project_id
  service  = "run.googleapis.com"

  depends_on = [google_project_service.required]
}

resource "google_project_service_identity" "service_networking" {
  provider = google-beta
  count    = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project  = var.recovery_project_id
  service  = "servicenetworking.googleapis.com"

  depends_on = [google_project_service.restore_verifier_prerequisite]
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
