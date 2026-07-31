variable "enable_recovery_backup_stack" {
  description = "Create recovery-project KMS, bucket, and writer-only source Cloud SQL access. Disabled by default."
  type        = bool
  default     = false
}

variable "recovery_project_id" {
  description = "Dedicated recovery GCP project ID. It must not be the production project."
  type        = string
  default     = ""
}

variable "recovery_region" {
  description = "Region for future recovery compute and provider defaults."
  type        = string
  default     = "us-central1"
}

variable "recovery_kms_location" {
  description = "Location for recovery-project KMS resources. Must match recovery_bucket_location (case-insensitive)."
  type        = string
  default     = "us"
}

variable "recovery_bucket_location" {
  description = "GCS location for recovery archives. US multi-region is the portable default."
  type        = string
  default     = "US"
}

variable "recovery_backup_bucket_name" {
  description = "Globally unique recovery bucket name. Empty default prevents accidental creation."
  type        = string
  default     = ""
}

variable "source_project_id" {
  description = "Production project ID whose Cloud SQL instance service account may write backup objects."
  type        = string
  default     = ""
}

variable "source_cloud_sql_service_account_email" {
  description = "Cloud SQL instance service-account email from the production Terraform output. It receives writer-only bucket access."
  type        = string
  default     = ""
}

variable "enable_local_postgres_backup_writer" {
  description = "Create the dedicated writer-only identity used for post-cutover local PostgreSQL exports. Disabled until the local database backup path is ready."
  type        = bool
  default     = false
}

variable "recovery_admin_member" {
  description = "Recovery administrator in IAM member form. Use user:... for a personal deployment or group:... for multi-custodian operation. This principal alone receives archive-view and KMS-decrypt access."
  type        = string
  default     = ""
}

variable "retention_period_seconds" {
  description = "Minimum immutable retention period for each archive. Lock only after a successful independent restore drill."
  type        = number
  default     = 7776000 # 90 days
}

variable "archive_expiry_days" {
  description = "Delete noncurrent/current archive objects after this age, subject to the retention period."
  type        = number
  default     = 365
}

variable "lock_retention_policy" {
  description = "Permanently lock bucket retention. Leave false until a successful independent restore drill is recorded."
  type        = bool
  default     = false
}

variable "enable_restore_verifier" {
  description = "Deploy scheduled restore-drill and janitor Cloud Run jobs in the recovery project."
  type        = bool
  default     = false
}

variable "restore_verifier_image" {
  description = "Immutable restore-verifier container image, including an @sha256 digest."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_verifier_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.restore_verifier_image))
    error_message = "restore_verifier_image must be empty or pinned by @sha256 digest."
  }
}

variable "restore_verifier_private_network" {
  description = "Recovery-project VPC network name used by the private Cloud SQL drill."
  type        = string
  default     = ""
}

variable "restore_verifier_private_subnetwork" {
  description = "Recovery-project subnetwork name used for Direct VPC egress."
  type        = string
  default     = ""
}

variable "restore_verifier_expected_revision" {
  description = "Exact Alembic revision required by successful restore drills."
  type        = string
  default     = ""
}

variable "restore_drill_schedule" {
  description = "Cloud Scheduler cron for full restore drills."
  type        = string
  default     = "0 9 * * 0"
}

variable "restore_janitor_schedule" {
  description = "Cloud Scheduler cron for abandoned-drill reconciliation."
  type        = string
  default     = "*/30 * * * *"
}
