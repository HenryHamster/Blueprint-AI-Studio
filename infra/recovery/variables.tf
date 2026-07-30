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

variable "recovery_admin_member" {
  description = "Recovery-admin Google Group in IAM member form, e.g. group:db-recovery@example.com. This group alone receives archive-view and KMS-decrypt access."
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
