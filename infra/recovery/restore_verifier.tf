locals {
  restore_verifier_enabled = local.recovery_enabled && var.enable_restore_verifier
  restore_janitor_enabled  = local.recovery_enabled && var.enable_restore_janitor
  recovery_job_enabled     = local.restore_verifier_enabled || local.restore_janitor_enabled
  recovery_job_env = local.recovery_job_enabled ? {
    RECOVERY_PROJECT_ID            = var.recovery_project_id
    RECOVERY_PROJECT_NUMBER        = tostring(data.google_project.recovery[0].number)
    SOURCE_PROJECT_ID              = var.source_project_id
    RECOVERY_BUCKET                = google_storage_bucket.db_backup[0].name
    RECOVERY_REGION                = var.recovery_region
    RECOVERY_PRIVATE_NETWORK       = "projects/${var.recovery_project_id}/global/networks/${var.restore_verifier_private_network}"
    RECOVERY_PRIVATE_SERVICE_RANGE = google_compute_global_address.restore_verifier_private_services[0].name
    EXPECTED_ALEMBIC_REVISION      = var.restore_verifier_expected_revision
    RECOVERY_ARCHIVE_PREFIX        = "local-postgres-exports/"
    EXPECTED_ARCHIVE_NAME          = var.restore_verifier_expected_archive_name
    EXPECTED_ARCHIVE_GENERATION    = var.restore_verifier_expected_archive_generation
    EXPECTED_MANIFEST_GENERATION   = var.restore_verifier_expected_manifest_generation
    EXPECTED_ARCHIVE_SHA256        = var.restore_verifier_expected_archive_sha256
    EXPECTED_ARCHIVE_SIZE_BYTES    = var.restore_verifier_expected_archive_size_bytes == 0 ? "" : tostring(var.restore_verifier_expected_archive_size_bytes)
  } : {}
}

resource "google_service_account" "restore_janitor" {
  count        = local.restore_janitor_enabled ? 1 : 0
  project      = var.recovery_project_id
  account_id   = "blueprint-restore-janitor"
  display_name = "Blueprint recovery restore janitor"
  description  = "Recovery-project-only identity that deletes owned expired restore-drill instances."

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "restore_janitor_sql" {
  count       = local.restore_janitor_enabled ? 1 : 0
  project     = var.recovery_project_id
  role_id     = "blueprintRestoreJanitorSql"
  title       = "Blueprint restore janitor Cloud SQL cleanup"
  description = "Narrow Cloud SQL list/get/delete permissions for owned restore-drill cleanup."
  permissions = [
    "cloudsql.instances.delete",
    "cloudsql.instances.get",
    "cloudsql.instances.list",
    "cloudsql.operations.get",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "restore_janitor_sql" {
  count   = local.restore_janitor_enabled ? 1 : 0
  project = var.recovery_project_id
  role    = google_project_iam_custom_role.restore_janitor_sql[0].name
  member  = "serviceAccount:${google_service_account.restore_janitor[0].email}"
}

resource "google_project_iam_member" "restore_janitor_logging" {
  count   = local.restore_janitor_enabled ? 1 : 0
  project = var.recovery_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.restore_janitor[0].email}"
}

resource "google_service_account" "restore_verifier" {
  count        = local.restore_verifier_enabled ? 1 : 0
  project      = var.recovery_project_id
  account_id   = "blueprint-restore-verifier"
  display_name = "Blueprint recovery restore verifier"
  description  = "Recovery-project-only identity for scheduled private restore drills."
}

resource "google_project_iam_custom_role" "restore_verifier_sql" {
  count       = local.restore_verifier_enabled ? 1 : 0
  project     = var.recovery_project_id
  role_id     = "blueprintRestoreVerifierSql"
  title       = "Blueprint restore verifier Cloud SQL lifecycle"
  description = "Narrow Cloud SQL create/import/probe/cleanup permissions for recovery drills."
  permissions = [
    "cloudsql.databases.create",
    "cloudsql.instances.connect",
    "cloudsql.instances.create",
    "cloudsql.instances.delete",
    "cloudsql.instances.get",
    "cloudsql.instances.list",
    "cloudsql.operations.get",
    "cloudsql.users.update",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "restore_verifier_sql" {
  count   = local.restore_verifier_enabled ? 1 : 0
  project = var.recovery_project_id
  role    = google_project_iam_custom_role.restore_verifier_sql[0].name
  member  = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_project_iam_member" "restore_verifier_logging" {
  count   = local.restore_verifier_enabled ? 1 : 0
  project = var.recovery_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_storage_bucket_iam_member" "restore_verifier_reader" {
  count  = local.restore_verifier_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_storage_bucket_iam_member" "restore_verifier_bucket_metadata_reader" {
  count  = local.restore_verifier_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_storage_bucket_iam_member" "restore_verifier_evidence_writer" {
  count  = local.restore_verifier_enabled ? 1 : 0
  bucket = google_storage_bucket.db_backup[0].name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_kms_crypto_key_iam_member" "restore_verifier_decrypter" {
  count         = local.restore_verifier_enabled ? 1 : 0
  crypto_key_id = google_kms_crypto_key.db_backup[0].id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_service_account.restore_verifier[0].email}"
}

resource "google_cloud_run_v2_job" "restore_drill" {
  count    = local.restore_verifier_enabled ? 1 : 0
  project  = var.recovery_project_id
  location = var.recovery_region
  name     = "blueprint-restore-drill"

  template {
    template {
      service_account = google_service_account.restore_verifier[0].email
      timeout         = "7200s"
      max_retries     = 0

      containers {
        image = var.restore_verifier_image
        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
        dynamic "env" {
          for_each = local.recovery_job_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }

      vpc_access {
        network_interfaces {
          network    = google_compute_network.restore_verifier[0].name
          subnetwork = google_compute_subnetwork.restore_verifier[0].name
          tags       = ["blueprint-restore-verifier"]
        }
        egress = "ALL_TRAFFIC"
      }
    }
  }

  depends_on = [
    terraform_data.restore_verifier_configuration,
    google_project_iam_member.restore_verifier_sql,
    google_project_iam_member.recovery_cloud_sql_service_agent,
    google_project_iam_member.recovery_service_networking_service_agent,
    google_storage_bucket_iam_member.restore_verifier_reader,
    google_storage_bucket_iam_member.restore_verifier_bucket_metadata_reader,
    google_storage_bucket_iam_member.restore_verifier_evidence_writer,
    google_kms_crypto_key_iam_member.restore_verifier_decrypter,
    google_compute_subnetwork.restore_verifier,
    google_service_networking_connection.restore_verifier,
    google_artifact_registry_repository_iam_member.cloud_run_reader,
    google_dns_record_set.restricted_googleapis,
    google_dns_record_set.googleapis_wildcard,
    google_dns_response_policy_rule.restricted_googleapis,
    google_dns_response_policy_rule.metadata,
    google_dns_response_policy_rule.googleapis_wildcard,
    google_dns_response_policy_rule.deny_other_names,
    google_compute_firewall.restore_allow_metadata,
    google_compute_firewall.restore_allow_googleapis,
    google_compute_firewall.restore_allow_cloud_sql,
    google_compute_firewall.restore_deny_other_egress,
  ]
}

resource "google_cloud_run_v2_job" "restore_janitor" {
  count    = local.restore_janitor_enabled ? 1 : 0
  project  = var.recovery_project_id
  location = var.recovery_region
  name     = "blueprint-restore-janitor"

  template {
    template {
      service_account = google_service_account.restore_janitor[0].email
      timeout         = "900s"
      max_retries     = 1

      containers {
        image = var.restore_verifier_image
        args  = ["--janitor-only"]
        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
        dynamic "env" {
          for_each = local.recovery_job_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }

      vpc_access {
        network_interfaces {
          network    = google_compute_network.restore_verifier[0].name
          subnetwork = google_compute_subnetwork.restore_verifier[0].name
          tags       = ["blueprint-restore-verifier"]
        }
        egress = "ALL_TRAFFIC"
      }
    }
  }

  depends_on = [
    terraform_data.restore_janitor_configuration,
    google_project_iam_member.restore_janitor_sql,
    google_project_iam_member.restore_janitor_logging,
    google_compute_subnetwork.restore_verifier,
    google_service_networking_connection.restore_verifier,
    google_artifact_registry_repository_iam_member.cloud_run_reader,
    google_dns_record_set.restricted_googleapis,
    google_dns_record_set.googleapis_wildcard,
    google_dns_response_policy_rule.restricted_googleapis,
    google_dns_response_policy_rule.metadata,
    google_dns_response_policy_rule.googleapis_wildcard,
    google_dns_response_policy_rule.deny_other_names,
    google_compute_firewall.restore_allow_metadata,
    google_compute_firewall.restore_allow_googleapis,
    google_compute_firewall.restore_allow_cloud_sql,
    google_compute_firewall.restore_deny_other_egress,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "restore_scheduler" {
  count = (
    local.restore_janitor_enabled ||
    (local.restore_verifier_enabled && var.enable_restore_drill_schedule)
  ) ? 1 : 0
  project      = var.recovery_project_id
  account_id   = "blueprint-restore-scheduler"
  display_name = "Blueprint restore scheduler invoker"
}

resource "google_cloud_run_v2_job_iam_member" "drill_scheduler_invoker" {
  count    = local.restore_verifier_enabled && var.enable_restore_drill_schedule ? 1 : 0
  project  = var.recovery_project_id
  location = var.recovery_region
  name     = google_cloud_run_v2_job.restore_drill[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.restore_scheduler[0].email}"
}

resource "google_cloud_run_v2_job_iam_member" "janitor_scheduler_invoker" {
  count    = local.restore_janitor_enabled ? 1 : 0
  project  = var.recovery_project_id
  location = var.recovery_region
  name     = google_cloud_run_v2_job.restore_janitor[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.restore_scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "restore_drill" {
  count       = local.restore_verifier_enabled && var.enable_restore_drill_schedule ? 1 : 0
  project     = var.recovery_project_id
  region      = var.recovery_region
  name        = "blueprint-restore-drill"
  description = "Run a complete independent database restore drill."
  schedule    = var.restore_drill_schedule
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://${var.recovery_region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.recovery_project_id}/jobs/${google_cloud_run_v2_job.restore_drill[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.restore_scheduler[0].email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.drill_scheduler_invoker]
}

resource "google_cloud_scheduler_job" "restore_janitor" {
  count       = local.restore_janitor_enabled ? 1 : 0
  project     = var.recovery_project_id
  region      = var.recovery_region
  name        = "blueprint-restore-janitor"
  description = "Reconcile expired or ambiguous Blueprint restore-drill instances."
  schedule    = var.restore_janitor_schedule
  time_zone   = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://${var.recovery_region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.recovery_project_id}/jobs/${google_cloud_run_v2_job.restore_janitor[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.restore_scheduler[0].email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.janitor_scheduler_invoker]

  lifecycle {
    prevent_destroy = true
  }
}
