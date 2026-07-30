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
  description = "Count of failed Cloud SQL backup operations from audit logs (Wave F4 M3, R3-W4 M6, R4-W4 M8, R5-W3-C)."
  # R3-W4 M6 (audit 2026-04-30): broadened from the original
  # `cloudsql.instances.backup.*` regex, which missed the actual method
  # names Cloud SQL emits in audit logs. Real method names include:
  #   - cloudsql.instances.createBackup   (operator-initiated on-demand)
  #   - cloudsql.backupRuns.insert        (scheduled backup runs)
  #   - cloudsql.backupRuns.create        (alternate scheduled path)
  # `cloudsql.instances.export` is intentionally excluded (export is a
  # separate concern, not a backup). The escaped backslashes below are
  # required because Cloud Logging filter syntax interprets `.` as
  # any-character; we want literal `.` matches between method-name
  # segments.
  #
  # R5-W3-C (audit 2026-04-30): two changes from R4-W4 M8:
  #
  # 1. Narrow regex: drop `cloudsql.instances.createBackup` from the
  #    match. That method is operator-initiated (e.g. an audit, a manual
  #    pre-deploy snapshot, or scripted `gcloud sql backups create`) and
  #    its failure is a known-state to whoever triggered it — paging the
  #    on-call here generates noise that drowns out the actual signal we
  #    care about (scheduled backups failing). Keep only
  #    `cloudsql.backupRuns.(insert|create)`, which is the scheduled
  #    backup path emitted by Cloud SQL's automatic backup engine.
  #
  # 2. Revert severity to ERROR-only. The R4-W4 widening to
  #    `(ERROR OR WARNING)` was based on an unverified assumption that
  #    Cloud SQL emits WARNING-severity events for failed backups. We
  #    haven't been able to verify this against actual logs (the most
  #    reliable confirmation would be a real-failed-backup test in the
  #    next restore drill, which hasn't run yet). The conservative
  #    choice is ERROR-only: WARNING-severity backup events that aren't
  #    actually failures (e.g. routine IO contention, maintenance-window
  #    collisions, on-demand `createBackup` audits) were the audit-D M1
  #    noise source. If the next restore drill confirms WARNINGs are
  #    real backup-failure signals, we can re-broaden — paired with the
  #    `alert_strategy` rate-limit below so a WARNING burst can't spam.
  filter = <<-EOT
    resource.type="cloudsql_database"
    AND protoPayload.methodName=~"cloudsql\.backupRuns\.(insert|create)"
    AND severity=ERROR
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
      # Cloud Monitoring requires a `resource.type` restriction in the alert
      # filter (the underlying log-based metric at `cloud_sql_backup_failure`
      # already filters resource.type=cloudsql_database, so we mirror that
      # restriction here to satisfy the API).
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.cloud_sql_backup_failure[0].name}\" AND resource.type=\"cloudsql_database\""
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

  # R5-W3-C (audit 2026-04-30, audit-D M1): defense-in-depth against
  # alert-burst spam. `auto_close` (30 min) ensures stale incidents close
  # automatically once the metric stops firing, so re-firing represents
  # a NEW incident.
  #
  # Note: notification_rate_limit was removed because GCP rejects it on
  # non-log-based alert policies ("only log-based alert policies may
  # specify a notification rate limit"). The rate limit was intended to
  # cap re-pages during a transient backup-engine outage; in its absence,
  # each ERROR event will fire a separate notification. Acceptable since
  # backup failures are rare and the auto_close window keeps stale
  # incidents from accumulating.
  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "Cloud SQL emitted a scheduled-backup failure event (severity=ERROR on cloudsql.backupRuns.insert/create). The 7-day retention of `cloudsql_backups` means a single failure is recoverable, but two consecutive failures shrink the recoverable window. R5-W3-C narrowed this filter to scheduled backups only and severity=ERROR only — operator-initiated `createBackup` failures and WARNING-severity routine events no longer fire this alert. Runbook: docs/db-operations.md (Restore procedure) — verify the instance is healthy via `gcloud sql backups list --instance=<name>` and re-trigger via `gcloud sql backups create --instance=<name>` if needed."
    mime_type = "text/markdown"
  }
}

# ------------------------------------------------------------------------------
# Independent off-host database export — failure and missing-success alerts.
# Both resources are disabled unless the opt-in backup system and an alert
# email are configured. The job emits structured JSON only after the Cloud SQL
# operation is terminal; a scheduler HTTP 2xx alone is not backup evidence.
# ------------------------------------------------------------------------------
resource "google_logging_metric" "offhost_db_backup_success" {
  count       = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  name        = "offhost_db_backup_success"
  description = "Completed independent Cloud SQL export operations. Absence for 23 hours pages operations."
  filter      = <<-EOT
    resource.type="cloud_run_job"
    AND resource.labels.job_name="blueprint-db-offhost-backup"
    AND jsonPayload.event="db_offhost_backup"
    AND jsonPayload.outcome="success"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "offhost_db_backup_failure" {
  count       = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  name        = "offhost_db_backup_failure"
  description = "Failed independent Cloud SQL export operations."
  filter      = <<-EOT
    resource.type="cloud_run_job"
    AND resource.labels.job_name="blueprint-db-offhost-backup"
    AND jsonPayload.event="db_offhost_backup"
    AND jsonPayload.outcome="failure"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "offhost_db_backup_runtime_error" {
  count       = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  name        = "offhost_db_backup_runtime_error"
  description = "Cloud Run backup job errors, including failures before export.sh can emit its structured event."
  filter      = <<-EOT
    resource.type="cloud_run_job"
    AND resource.labels.job_name="blueprint-db-offhost-backup"
    AND severity>=ERROR
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "offhost_db_backup_scheduler_error" {
  count       = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  name        = "offhost_db_backup_scheduler_error"
  description = "Cloud Scheduler invocation failures for the independent database backup job."
  filter      = <<-EOT
    resource.type="cloud_scheduler_job"
    AND resource.labels.job_id="blueprint-db-offhost-backup"
    AND severity>=ERROR
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "offhost_db_backup_failure" {
  count        = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  display_name = "Database: independent backup export failed"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "Structured export failure in 15 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.offhost_db_backup_failure[0].name}\" AND resource.type=\"cloud_run_job\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  conditions {
    display_name = "Backup job runtime error in 15 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.offhost_db_backup_runtime_error[0].name}\" AND resource.type=\"cloud_run_job\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  conditions {
    display_name = "Backup scheduler error in 15 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.offhost_db_backup_scheduler_error[0].name}\" AND resource.type=\"cloud_scheduler_job\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "The independent recovery-project Cloud SQL export failed or refused to overlap another Cloud SQL operation. Check the `blueprint-db-offhost-backup` Cloud Run Job logs and the Cloud SQL operation. Do not grant the source runner Storage-reader or KMS-decrypt access as a workaround."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "offhost_db_backup_missing" {
  count        = local.alerting_enabled && var.enable_offhost_db_backup ? 1 : 0
  display_name = "Database: independent backup missing for 23 hours"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "No completed off-host export for 23 hours"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.offhost_db_backup_success[0].name}\" AND resource.type=\"cloud_run_job\""
      duration = "82800s"

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  documentation {
    content   = "No successful independent recovery-project export was recorded for 23 hours. The default schedule runs twice daily; verify Scheduler execution, Cloud Run Job logs, the Cloud SQL operation, and the recovery bucket writer-only IAM binding. Immediate Scheduler and Cloud Run error conditions cover failures before the first success metric exists. Native Cloud SQL PITR remains the short-window recovery tier."
    mime_type = "text/markdown"
  }
}

# AlloyDB CPU alert was removed when alloydb.tf was archived (Cloud SQL CPU
# alert above remains the active DB CPU monitor). To restore, mirror the
# cloudsql_cpu block above and target resource.type
# "alloydb.googleapis.com/Instance" / metric.type
# "alloydb.googleapis.com/instance/cpu/utilization". See archive/README.md.

# ------------------------------------------------------------------------------
# Reaper crash-loop alert (log-based metric + alert)
#
# Production incident 2026-05-04: a MissingGreenlet regression at
# reaper.py:724 made the reaper crash on every cycle for 80+ minutes
# before the operator noticed via stuck task heartbeat ages. The reaper
# logs "[REAPER] Error: <exc>" at logger.error on every uncaught
# exception in its main loop, so a log-based metric counting those
# entries gives a direct crash-loop signal — the alert fires inside
# 5 successful-cycle windows (~5 min) regardless of bug class.
#
# This is broader than reaper.py:724-specific telemetry: any future
# regression that crashes the reaper loop (DB exhaustion, dialect
# breakage, deferred-import circular, etc.) trips this same alert.
# ------------------------------------------------------------------------------
resource "google_logging_metric" "reaper_loop_error" {
  count       = local.alerting_enabled ? 1 : 0
  name        = "reaper_loop_error"
  description = "Count of '[REAPER] Error:' log entries from the orchestrator reaper main loop. Increments only on uncaught exceptions in the reaper service body."
  filter      = <<-EOT
    resource.type="cloud_run_revision"
    AND resource.labels.service_name="blueprint-orchestrator"
    AND severity=ERROR
    AND textPayload=~"\[REAPER\] Error:"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "reaper_loop_error" {
  count        = local.alerting_enabled ? 1 : 0
  display_name = "Reaper: crash loop"
  combiner     = "OR"
  enabled      = true

  conditions {
    display_name = "≥3 reaper loop errors in 5 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.reaper_loop_error[0].name}\" AND resource.type=\"cloud_run_revision\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 2

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The reaper main loop has logged ≥3 errors in 5 min — likely crash-looping and not reaping orphan tasks. Stuck-IN_PROGRESS tasks accumulate while this fires. Runbook: check `gcloud logging read 'resource.labels.service_name=\"blueprint-orchestrator\" AND severity>=ERROR'` for the traceback; common shapes are MissingGreenlet on async/sync mismatch (see reaper.py:709 expire_all guard), DB pool exhaustion (see /metrics blueprint_db_pool_invalidations_total), and deferred-import cycles."
    mime_type = "text/markdown"
  }
}
