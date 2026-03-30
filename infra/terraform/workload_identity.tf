# ------------------------------------------------------------------------------
# Workload Identity Federation for GitHub Actions (optional — skipped if enable_cicd=false)
# ------------------------------------------------------------------------------

resource "google_iam_workload_identity_pool" "github_pool" {
  count                     = var.enable_cicd ? 1 : 0
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Identity pool for GitHub Actions deployments"
}

resource "google_iam_workload_identity_pool_provider" "github_provider" {
  count                              = var.enable_cicd ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_pool[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions-provider"
  display_name                       = "GitHub Actions Provider"

  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.actor"            = "assertion.actor"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
  }

  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# ------------------------------------------------------------------------------
# CI/CD Service Account
# ------------------------------------------------------------------------------

resource "google_service_account" "github_actions_sa" {
  count        = var.enable_cicd ? 1 : 0
  account_id   = "github-actions-sa"
  display_name = "GitHub Actions Service Account"
}

resource "google_service_account_iam_member" "github_actions_impersonation" {
  count              = var.enable_cicd ? 1 : 0
  service_account_id = google_service_account.github_actions_sa[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_pool[0].name}/attribute.repository/${var.github_repository}"
}

resource "google_artifact_registry_repository_iam_member" "github_actions_ar_writer" {
  count      = var.enable_cicd ? 1 : 0
  project    = google_artifact_registry_repository.orchestrator_repo.project
  location   = google_artifact_registry_repository.orchestrator_repo.location
  repository = google_artifact_registry_repository.orchestrator_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

resource "google_project_iam_member" "github_actions_run_admin" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

resource "google_service_account_iam_member" "github_actions_act_as_orchestrator" {
  count              = var.enable_cicd ? 1 : 0
  service_account_id = google_service_account.orchestrator_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}
