# ------------------------------------------------------------------------------
# Cloud Monitoring — alert policies + email notification channel
#
# Phase A4 of the database remediation plan (HIGH H-O4): the project previously
# had zero google_monitoring_alert_policy resources, so CPU/disk/connection/
# backup-failure incidents were silent until they showed up as user-visible
# 500s. This file fills that gap.
#
# All policies are gated behind var.alert_email — empty string disables alerting
# (no notification channel created, no alert policies created). Set the variable
# in terraform.tfvars or via -var="alert_email=foo@bar.com" to enable.
#
# Coverage during the AlloyDB → Cloud SQL migration window:
#   - Cloud SQL CPU / disk / connections / availability (count = use_cloud_sql)
#   - Cloud SQL backup failure (log-based metric, count = alerting_enabled — Wave F4 M3)
#   - AlloyDB CPU mirror so the same dashboards work pre-cutover (count =
#     alerting_enabled — Wave F4 M4 changed the gate from `!use_cloud_sql`
#     so the alert stays armed during cutover when both stacks are live)
# Once alloydb.tf is removed post-cutover, the AlloyDB policy block becomes
# inert (the missing resource reference triggers Terraform deletion) and can
# be pruned.
#
# Documentation links target docs/db-migration-plan.md (Operational continuity
# section) which carries the runbook for each incident type.
# ------------------------------------------------------------------------------

locals {
  alerting_enabled = var.alert_email != ""
}

resource "google_monitoring_notification_channel" "email" {
  count        = local.alerting_enabled ? 1 : 0
  display_name = "Blueprint ops email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

# ------------------------------------------------------------------------------
# Cloud SQL — CPU > 80% sustained 5 min
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_cpu" {
  count        = local.alerting_enabled && var.use_cloud_sql ? 1 : 0
  display_name = "Cloud SQL CPU > 80% (5 min)"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "CPU utilization above 80%"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "Cloud SQL CPU has been above 80% for 5 minutes. Runbook: docs/db-migration-plan.md (Operational continuity). Likely action: bump tier from db-g1-small → db-custom-1-3840."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# Cloud SQL — disk usage > 75% of disk_autoresize_limit (= 150 GB of 200 GB)
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_disk" {
  count        = local.alerting_enabled && var.use_cloud_sql ? 1 : 0
  display_name = "Cloud SQL disk > 75% of autoresize cap"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Disk bytes used above 150 GB"
    condition_threshold {
      filter = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/disk/bytes_used\""
      # 75% of 200 GB autoresize limit (cloud_sql.tf disk_autoresize_limit = 200) = 150 GB.
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 161061273600 # 150 * 1024^3 bytes

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "Cloud SQL disk usage exceeded 150 GB (75% of 200 GB autoresize cap). At 200 GB autoresize stops; queries that grow tables will start failing. Runbook: docs/db-migration-plan.md (Operational continuity)."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# Cloud SQL — connections > 80% of max_connections (= 160 of 200)
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_connections" {
  count        = local.alerting_enabled && var.use_cloud_sql ? 1 : 0
  display_name = "Cloud SQL connections > 80% of cap"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "num_backends above 160"
    condition_threshold {
      filter = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/postgresql/num_backends\""
      # 80% of max_connections=200 (cloud_sql.tf database_flag) = 160.
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 160

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "Cloud SQL has more than 160 active backends (80% of the 200-connection cap). At 200, new connections are refused and the orchestrator pool starts timing out. Likely cause: pool_size + max_overflow misconfigured across Cloud Run instances, or a connection leak. Runbook: docs/db-migration-plan.md (Operational continuity)."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# Cloud SQL — backup health (instance availability flapping)
#
# Cloud Monitoring does not expose a direct "backup failed" metric for Cloud
# SQL; the closest signal is `database/up` flapping (instance unavailable for
# the backup window) or backup-failure events in audit logs. We use the
# `database/up` signal here. A log-based metric on backup failure events can
# be added in Phase B when the audit-log pipeline is wired up.
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_up" {
  count        = local.alerting_enabled && var.use_cloud_sql ? 1 : 0
  display_name = "Cloud SQL instance unavailable"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "database/up = 0 for 5 min"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/up\""
      duration        = "300s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "Cloud SQL instance has been unavailable for 5 minutes. This catches both planned-maintenance overruns and backup-failure-induced outages. Runbook: docs/db-migration-plan.md (Operational continuity)."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# Cloud SQL — backup failure (log-based metric + alert)
#
# Wave F4 M3: complements `cloudsql_up` (which catches instance-availability
# flapping) with a direct signal on backup operations failing. Cloud SQL
# emits audit-log events under method names matching `cloudsql.instances.backup`
# at severity=ERROR when a backup attempt fails — we count those into a
# log-based metric and fire on any non-zero count over 5 minutes.
# ------------------------------------------------------------------------------
resource "google_logging_metric" "cloud_sql_backup_failure" {
  count       = local.alerting_enabled ? 1 : 0
  name        = "cloud_sql_backup_failure"
  description = "Count of failed Cloud SQL backup operations from audit logs (Wave F4 M3, R3-W4 M6, R4-W4 M8)."
  # R3-W4 M6 (audit 2026-04-30): broadened from the original
  # `cloudsql.instances.backup.*` regex, which missed the actual method
  # names Cloud SQL emits in audit logs. Real method names include:
  #   - cloudsql.instances.createBackup   (on-demand backup)
  #   - cloudsql.backupRuns.insert        (scheduled backup runs)
  #   - cloudsql.backupRuns.create        (alternate scheduled path)
  # `cloudsql.instances.export` is intentionally excluded (export is a
  # separate concern, not a backup). The escaped backslashes below are
  # required because Cloud Logging filter syntax interprets `.` as
  # any-character; we want literal `.` matches between method-name
  # segments.
  #
  # R4-W4 M8 (audit 2026-04-29): severity widened from `ERROR` to
  # `(ERROR OR WARNING)`. Cloud SQL emits backup-termination events at
  # severity=WARNING for transient causes (e.g. temporary IO contention,
  # maintenance-window collision) that still leave the recovery window
  # unhealthy. With ERROR-only filtering, those incidents went unnoticed
  # and were only caught when the next-day backup also failed and the
  # operator hit the 7-day retention floor. The false-positive cost
  # (alert pings for a transient WARNING that self-recovers) is
  # acceptable — the alert documentation directs the operator to verify
  # via `gcloud sql backups list`, which surfaces the actual recovery
  # window in seconds. A real-failed-backup test in the next restore
  # drill should validate the regex captures both severity bands; if
  # WARNING-only events are too noisy, narrow back to ERROR with a note
  # that transient WARNING-only failures will be missed.
  filter = <<-EOT
    resource.type="cloudsql_database"
    AND (
      protoPayload.methodName=~"cloudsql\.instances\.(create)?[Bb]ackup.*"
      OR protoPayload.methodName=~"cloudsql\.backupRuns\.(insert|create)"
    )
    AND (severity=ERROR OR severity=WARNING)
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "cloudsql_backup_failure" {
  count        = local.alerting_enabled ? 1 : 0
  display_name = "Cloud SQL: backup failure"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Any backup-failure event in 5 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.cloud_sql_backup_failure[0].name}\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "Cloud SQL emitted a backup-failure event (severity=ERROR or WARNING). The 7-day retention of `cloudsql_backups` means a single failure is recoverable, but two consecutive failures shrink the recoverable window. R4-W4 M8 (audit 2026-04-29): severity was widened to include WARNING, so this alert may fire on transient backup terminations that self-recover — verify the actual recovery window via `gcloud sql backups list --instance=<name>` before declaring an outage. Runbook: docs/db-operations.md (Restore procedure) — verify the instance is healthy and re-trigger via `gcloud sql backups create --instance=<name>`."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# AlloyDB — CPU > 80% (mirror of Cloud SQL CPU policy)
#
# Active for the duration that AlloyDB resources still exist in the Terraform
# state (regardless of `use_cloud_sql`). Wave F4 M4: previously gated on
# `!var.use_cloud_sql` so the alert went silent the moment Cloud SQL was
# enabled — but during the cutover window both stacks are live and AlloyDB
# is still the authoritative DB until the runbook flips DATABASE_URL.
# Keeping the alert armed during cutover catches any AlloyDB CPU spike that
# would mask a botched migration.
#
# R3-W4 M5 (audit 2026-04-30): the previous comment claimed this alert
# auto-deletes when alloydb.tf is removed. That is WRONG — the filter is
# a literal Cloud Monitoring resource.type string
# (`alloydb.googleapis.com/Instance`), not a Terraform
# google_alloydb_instance reference, so Terraform has no dependency edge
# to drive deletion. After alloydb.tf is removed, this alert resource
# will persist as a no-op (zero series matched, no notifications fire).
# The cutover commit MUST also remove this alert block alongside
# alloydb.tf, or the policy stays in the project's monitoring console
# forever as cosmetic noise.
# ------------------------------------------------------------------------------
resource "google_monitoring_alert_policy" "alloydb_cpu" {
  count        = local.alerting_enabled ? 1 : 0
  display_name = "AlloyDB CPU > 80% (5 min)"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "CPU utilization above 80%"
    condition_threshold {
      filter          = "resource.type = \"alloydb.googleapis.com/Instance\" AND metric.type = \"alloydb.googleapis.com/instance/cpu/utilization\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "AlloyDB instance CPU has been above 80% for 5 minutes. This is dual-coverage during the AlloyDB → Cloud SQL migration window. Runbook: docs/db-migration-plan.md (Operational continuity)."
    mime_type = "text/markdown"
  }
}
