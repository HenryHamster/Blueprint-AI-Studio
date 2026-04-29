# ------------------------------------------------------------------------------
# Cloud Audit Logs — AlloyDB
# ------------------------------------------------------------------------------
# Admin Activity logs are always on. This resource opts in to:
#   ADMIN_READ  — reads of IAM/policy/config (low volume, high signal)
#   DATA_WRITE  — any data-modifying API call (who changed what)
# DATA_READ is intentionally omitted: high log volume, high cost, and reads
# on this cluster only come from the Cloud Run service account over private IP.

resource "google_project_iam_audit_config" "alloydb" {
  project = var.project_id
  service = "alloydb.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# ------------------------------------------------------------------------------
# Cloud Audit Logs — Cloud SQL
# ------------------------------------------------------------------------------
# Same rationale as AlloyDB: ADMIN_READ + DATA_WRITE, omit DATA_READ for cost.
# Always enabled (regardless of var.use_cloud_sql) — the audit_config is a
# no-op until the Cloud SQL API is actually used, and pre-arming it means the
# logs are in place from the very first migration step.

resource "google_project_iam_audit_config" "cloudsql" {
  project = var.project_id
  service = "cloudsql.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
