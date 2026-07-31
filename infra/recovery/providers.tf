provider "google" {
  project = var.recovery_project_id
  region  = var.recovery_region

  user_project_override = true
  billing_project       = var.recovery_project_id
}

provider "google-beta" {
  project = var.recovery_project_id
  region  = var.recovery_region

  user_project_override = true
  billing_project       = var.recovery_project_id
}

data "google_project" "recovery" {
  count      = var.enable_recovery_backup_stack ? 1 : 0
  project_id = var.recovery_project_id

  # A newly created recovery project may not have the Resource Manager API
  # enabled yet. Defer this lookup until the root has bootstrapped its required
  # services instead of making the first plan fail with a 403.
  depends_on = [google_project_service.required]
}
