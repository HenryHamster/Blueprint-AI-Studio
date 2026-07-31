variable "project_id" {
  description = "The GCP Project ID"
  type        = string
}

variable "region" {
  description = "The GCP Region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "The GCP Zone"
  type        = string
  default     = "us-central1-a"
}

variable "github_owner" {
  description = "The GitHub organization or user that owns the repository (e.g., 'HenryHamster')"
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "The GitHub repository (e.g., 'owner/repo') that is allowed to deploy via Workload Identity. Set to empty string to skip CI/CD setup."
  type        = string
  default     = ""
}

variable "enable_cicd" {
  description = "Whether to create Workload Identity Federation resources for GitHub Actions CI/CD"
  type        = bool
  default     = false
}

variable "orchestrator_url" {
  description = "The public URL of the Cloud Run orchestrator (set after first deploy)"
  type        = string
  default     = ""
}

variable "enable_public_orchestrator_access" {
  description = "Grant roles/run.invoker to allUsers on the Cloud Run orchestrator. Defaults true to preserve existing public API/webhook behavior; set false only as an explicit local-control-plane cutover fence."
  type        = bool
  default     = true
}

variable "use_cloud_sql" {
  description = "Provision a Cloud SQL Postgres instance. Default true since alloydb.tf was archived 2026-05-02. Setting false will leave the deploy with NO database — only do this if you're restoring AlloyDB from archive/."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email address that receives Cloud Monitoring alert pages (CPU, disk, connections, backup failure). Empty string disables alerting — no notification channel is created and alert policies skip themselves."
  type        = string
  default     = ""
}

variable "billing_account_id" {
  description = "GCP billing account ID (e.g. '01ABCD-234567-EFGH89'). Required to create a budget alert. Empty string disables the budget resource. Find via `gcloud billing accounts list`."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly GCP budget target in USD. Email alerts fire at 50/90/100%% of this amount. This does not automatically stop spend."
  type        = number
  default     = 200
}

variable "enable_offhost_db_backup" {
  description = "Enable the scheduled independent recovery-project Cloud SQL export. Disabled by default; enable only after infra/recovery has created a distinct recovery bucket."
  type        = bool
  default     = false
}

variable "recovery_backup_project_id" {
  description = "Dedicated recovery GCP project ID. Empty default prevents the backup job from targeting any bucket."
  type        = string
  default     = ""
}

variable "recovery_backup_bucket" {
  description = "Bucket-only gs:// URI output by infra/recovery, e.g. gs://blueprint-recovery-db-backups. Empty default prevents activation."
  type        = string
  default     = ""
}

variable "db_backup_image" {
  description = "Immutable Cloud SDK image digest for the backup Job, e.g. gcr.io/google.com/cloudsdktool/cloud-sdk@sha256:<64 hex>. Empty default prevents activation."
  type        = string
  default     = ""
}

variable "db_backup_database_name" {
  description = "Cloud SQL database to export. postgres is the current default database; verify live before first enablement."
  type        = string
  default     = "postgres"
}

variable "db_backup_schedule" {
  description = "UTC cron schedule for off-host exports. Twice-daily default supports a 23-hour missing-success alert without false positives."
  type        = string
  default     = "15 04,16 * * *"
}
