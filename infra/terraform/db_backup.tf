# ------------------------------------------------------------------------------
# Stage 2A: independent off-host Cloud SQL export.
#
# Dark by default. The production root cannot infer a bucket or image: an
# operator must explicitly enable it and name a bucket in a different project.
# Cloud SQL performs the export server-side; neither this Job nor hamsterwheel
# receives a database password, archive read permission, or CMEK access.
# ------------------------------------------------------------------------------

locals {
  offhost_db_backup_enabled = var.enable_offhost_db_backup
}

resource "terraform_data" "offhost_db_backup_configuration" {
  count = local.offhost_db_backup_enabled ? 1 : 0

  input = {
    recovery_project_id = var.recovery_backup_project_id
    recovery_bucket     = var.recovery_backup_bucket
  }

  lifecycle {
    precondition {
      condition     = var.recovery_backup_project_id != "" && var.recovery_backup_project_id != var.project_id
      error_message = "recovery_backup_project_id must be a non-production project before off-host backup can be enabled."
    }
    precondition {
      condition     = var.use_cloud_sql
      error_message = "use_cloud_sql must be true before off-host Cloud SQL backup can be enabled."
    }
    precondition {
      condition     = can(regex("^gs://[a-z0-9._-]+$", var.recovery_backup_bucket))
      error_message = "recovery_backup_bucket must be a bucket-only gs:// URI (no path), supplied explicitly from infra/recovery."
    }
    precondition {
      condition     = can(regex("^.+@sha256:[a-f0-9]{64}$", var.db_backup_image))
      error_message = "db_backup_image must be an immutable image digest, not a mutable tag."
    }
    precondition {
      condition     = var.alert_email != ""
      error_message = "alert_email must be configured before the off-host backup scheduler can be enabled."
    }
  }
}

resource "google_project_service" "db_backup_cloud_scheduler" {
  count              = local.offhost_db_backup_enabled ? 1 : 0
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false

  depends_on = [terraform_data.offhost_db_backup_configuration]
}

resource "google_service_account" "db_backup_runner" {
  count        = local.offhost_db_backup_enabled ? 1 : 0
  account_id   = "db-backup-runner"
  display_name = "Blueprint off-host database backup runner"
  description  = "Starts and observes Cloud SQL exports only; no archive or database credentials."
}

resource "google_project_iam_custom_role" "db_backup_exporter" {
  count       = local.offhost_db_backup_enabled ? 1 : 0
  role_id     = "blueprintDbBackupExporter"
  title       = "Blueprint DB Backup Exporter"
  description = "Start and observe Cloud SQL exports without database, Storage, Secret Manager, or IAM access."
  permissions = [
    "cloudsql.instances.export",
    "cloudsql.instances.get",
    "cloudsql.operations.get",
    "cloudsql.operations.list",
    "serviceusage.services.use",
  ]
}

resource "google_project_iam_member" "db_backup_runner_exporter" {
  count   = local.offhost_db_backup_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.db_backup_exporter[0].id
  member  = "serviceAccount:${google_service_account.db_backup_runner[0].email}"
}

resource "google_project_iam_member" "db_backup_runner_log_writer" {
  count   = local.offhost_db_backup_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.db_backup_runner[0].email}"
}

resource "google_service_account" "db_backup_scheduler" {
  count        = local.offhost_db_backup_enabled ? 1 : 0
  account_id   = "db-backup-scheduler"
  display_name = "Blueprint off-host database backup scheduler"
  description  = "Can invoke only the dedicated database backup Cloud Run Job."
}

resource "google_cloud_run_v2_job" "db_backup" {
  count    = local.offhost_db_backup_enabled ? 1 : 0
  name     = "blueprint-db-offhost-backup"
  location = var.region

  labels = {
    app        = "blueprint-db-backup"
    managed-by = "terraform"
    tier       = "recovery"
  }

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.db_backup_runner[0].email
      max_retries     = 0
      timeout         = "7200s"

      containers {
        # Supplied only as a digest by the enabling operator. A public image is
        # acceptable here because it contains the Cloud SDK only; the job has
        # no secret, DB, Storage-read, or KMS-decrypt capability.
        image   = var.db_backup_image
        command = ["/bin/sh"]
        args = [
          "-ceu",
          "umask 077; script_path=$(mktemp); trap 'rm -f \"$script_path\"' EXIT; printf '%s' '${filebase64("${path.module}/../db_recovery/export.sh")}' | base64 -d > \"$script_path\"; /bin/sh \"$script_path\"",
        ]

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }

        env {
          name  = "PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "INSTANCE_NAME"
          value = google_sql_database_instance.default[0].name
        }
        env {
          name  = "DATABASE_NAME"
          value = var.db_backup_database_name
        }
        env {
          name  = "BACKUP_BUCKET"
          value = var.recovery_backup_bucket
        }
      }
    }
  }

  depends_on = [
    terraform_data.offhost_db_backup_configuration,
    google_project_iam_member.db_backup_runner_exporter,
    google_project_iam_member.db_backup_runner_log_writer,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "db_backup_scheduler_invoker" {
  count    = local.offhost_db_backup_enabled ? 1 : 0
  location = google_cloud_run_v2_job.db_backup[0].location
  project  = var.project_id
  name     = google_cloud_run_v2_job.db_backup[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.db_backup_scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "db_backup" {
  count       = local.offhost_db_backup_enabled ? 1 : 0
  name        = "blueprint-db-offhost-backup"
  description = "Starts the daily independent Cloud SQL export job."
  region      = var.region
  schedule    = var.db_backup_schedule
  time_zone   = "Etc/UTC"

  # Cloud Run's Jobs API acknowledges execution creation quickly. Do not retry
  # the request: the next scheduled run is safer than a duplicate export.
  attempt_deadline = "180s"
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.db_backup[0].name}:run"

    oauth_token {
      service_account_email = google_service_account.db_backup_scheduler[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.db_backup_scheduler_invoker,
    google_project_service.db_backup_cloud_scheduler,
  ]
}
