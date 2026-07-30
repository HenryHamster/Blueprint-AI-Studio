provider "google" {
  project = var.recovery_project_id
  region  = var.recovery_region

  user_project_override = true
  billing_project       = var.recovery_project_id
}

data "google_project" "recovery" {
  count      = var.enable_recovery_backup_stack ? 1 : 0
  project_id = var.recovery_project_id
}
