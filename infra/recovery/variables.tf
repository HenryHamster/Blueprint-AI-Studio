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

variable "recovery_billing_account_id" {
  description = "Billing account ID used only to create the recovery project's dedicated monthly budget alert."
  type        = string
  default     = ""
}

variable "recovery_monthly_budget_usd" {
  description = "Monthly USD alert budget scoped only to the recovery project."
  type        = number
  default     = 20

  validation {
    condition     = var.recovery_monthly_budget_usd >= 5 && var.recovery_monthly_budget_usd <= 100 && floor(var.recovery_monthly_budget_usd) == var.recovery_monthly_budget_usd
    error_message = "recovery_monthly_budget_usd must be a whole-dollar amount between 5 and 100."
  }
}

variable "enable_restore_janitor" {
  description = "Keep the recovery janitor job and 30-minute schedule active independently of recurring full restore drills."
  type        = bool
  default     = false
}

variable "enable_restore_drill_schedule" {
  description = "Enable recurring full restore drills only after a manual exact-generation drill and cleanup proof succeed."
  type        = bool
  default     = false
}

variable "enable_restore_verifier_prerequisites" {
  description = "Create the dedicated recovery VPC, private-services connection, and immutable verifier image repository before enabling restore jobs."
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

variable "restore_verifier_subnetwork_cidr" {
  description = "CIDR for Cloud Run Direct VPC egress used only by the recovery restore verifier."
  type        = string
  default     = "10.73.0.0/24"

  validation {
    condition     = can(cidrnetmask(var.restore_verifier_subnetwork_cidr))
    error_message = "restore_verifier_subnetwork_cidr must be a valid IPv4 CIDR."
  }
}

variable "restore_verifier_private_service_prefix_length" {
  description = "Prefix length of the Google-managed private-services range used by temporary recovery Cloud SQL instances."
  type        = number
  default     = 24

  validation {
    condition     = var.restore_verifier_private_service_prefix_length >= 16 && var.restore_verifier_private_service_prefix_length <= 24
    error_message = "restore_verifier_private_service_prefix_length must be between 16 and 24."
  }
}

variable "restore_verifier_artifact_repository" {
  description = "Recovery-project Artifact Registry repository for immutable restore-verifier images."
  type        = string
  default     = "blueprint-recovery"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,62}$", var.restore_verifier_artifact_repository))
    error_message = "restore_verifier_artifact_repository must be a valid Artifact Registry repository ID."
  }
}

variable "restore_verifier_expected_revision" {
  description = "Exact Alembic revision required by successful restore drills."
  type        = string
  default     = ""
}

variable "restore_verifier_expected_archive_name" {
  description = "Optional exact archive object name required by a manual pre-cutover drill."
  type        = string
  default     = ""
}

variable "restore_verifier_expected_archive_generation" {
  description = "Optional immutable GCS generation of the exact archive required by a manual pre-cutover drill."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_verifier_expected_archive_generation == "" || can(regex("^[0-9]+$", var.restore_verifier_expected_archive_generation))
    error_message = "restore_verifier_expected_archive_generation must be empty or numeric."
  }
}

variable "restore_verifier_expected_manifest_generation" {
  description = "Optional immutable GCS generation of the exact completion manifest required by a manual pre-cutover drill."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_verifier_expected_manifest_generation == "" || can(regex("^[0-9]+$", var.restore_verifier_expected_manifest_generation))
    error_message = "restore_verifier_expected_manifest_generation must be empty or numeric."
  }
}

variable "restore_verifier_expected_archive_sha256" {
  description = "Optional SHA-256 of the exact archive required by a manual pre-cutover drill."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_verifier_expected_archive_sha256 == "" || can(regex("^[0-9a-f]{64}$", var.restore_verifier_expected_archive_sha256))
    error_message = "restore_verifier_expected_archive_sha256 must be empty or a lowercase SHA-256 digest."
  }
}

variable "restore_verifier_expected_archive_size_bytes" {
  description = "Optional byte size of the exact archive required by a manual pre-cutover drill; zero disables exact pinning."
  type        = number
  default     = 0

  validation {
    condition     = var.restore_verifier_expected_archive_size_bytes >= 0 && floor(var.restore_verifier_expected_archive_size_bytes) == var.restore_verifier_expected_archive_size_bytes
    error_message = "restore_verifier_expected_archive_size_bytes must be a non-negative integer."
  }
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
