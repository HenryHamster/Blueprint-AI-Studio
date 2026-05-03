# ------------------------------------------------------------------------------
# Cloud Audit Logs — Cloud SQL
# ------------------------------------------------------------------------------
# Admin Activity logs are always on. This resource opts in to:
#   ADMIN_READ  — reads of IAM/policy/config (low volume, high signal)
#   DATA_WRITE  — any data-modifying API call (who changed what)
# DATA_READ is intentionally omitted: high log volume, high cost, and reads
# on this database only come from the Cloud Run service account over private IP.

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

# AlloyDB audit_log_config block was removed when alloydb.tf was archived.
# To restore: mirror the cloudsql block above with service =
# "alloydb.googleapis.com". See archive/README.md.
