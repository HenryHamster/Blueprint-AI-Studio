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

  attribute_condition = "assertion.repository_owner == \"${var.github_owner}\" && assertion.repository == \"${var.github_repository}\""

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

resource "google_artifact_registry_repository_iam_member" "github_actions_compute_images_writer" {
  count      = var.enable_cicd ? 1 : 0
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Orchestrator SA needs artifactregistry.writer on compute_images_repo so it can
# push newly-built project compute images and manage their protection tags.
resource "google_artifact_registry_repository_iam_member" "orchestrator_compute_images_writer" {
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Agent SA needs artifactregistry.reader on compute_images_repo so GCP compute
# VMs (running as this SA) can pull project-baked compute images from the
# private Artifact Registry repository.
resource "google_artifact_registry_repository_iam_member" "agent_compute_images_reader" {
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.agent_sa.email}"
}

# image_builder_sa needs artifactregistry.writer to push newly-built project
# compute images. Mirrors orchestrator_compute_images_writer above; scoped to
# the compute-images repo only — no other roles granted on this SA.
resource "google_artifact_registry_repository_iam_member" "image_builder_compute_images_writer" {
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.image_builder_sa.email}"
}

# Isolated compute-image builder SA (see iam.tf) needs artifactregistry.writer
# on compute_images_repo so the regional Cloud Build can publish project-baked
# images. This repo is the builder's ONLY publish target — it holds no writer
# role on the orchestrator (blueprint-repo) or agent-images repos, so a
# malicious build cannot overwrite the orchestrator's own deploy image.
resource "google_artifact_registry_repository_iam_member" "compute_image_builder_compute_images_writer" {
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.compute_image_builder_sa.email}"
}

# image_builder_sa needs artifactregistry.reader so the builder VM can pull a
# private base/parent image from the same repo before running docker build.
# Mirrors agent_compute_images_reader above.
resource "google_artifact_registry_repository_iam_member" "image_builder_compute_images_reader" {
  project    = google_artifact_registry_repository.compute_images_repo.project
  location   = google_artifact_registry_repository.compute_images_repo.location
  repository = google_artifact_registry_repository.compute_images_repo.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.image_builder_sa.email}"
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

# Updating an existing Cloud Run Job requires iam.serviceAccounts.actAs on the
# execution service account attached to the Job, even when the update mask only
# changes the container image. Keep this binding scoped to the dedicated,
# least-privilege agent Job service account rather than granting project-wide
# service account usage.
resource "google_service_account_iam_member" "github_actions_act_as_agent_job" {
  count              = var.enable_cicd ? 1 : 0
  service_account_id = google_service_account.agent_job_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Access secrets so Cloud Run can mount them at deploy time
resource "google_project_iam_member" "github_actions_secret_accessor" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# List secrets — separate from secretAccessor (which only grants reading
# values). The deploy.yml's "Verify GCP permissions" step calls
# `gcloud secrets list` which requires `secretmanager.secrets.list`,
# delivered by `roles/secretmanager.viewer`. Without this, the deploy
# workflow fails at the permission-verification gate before any actual
# deploy work runs.
resource "google_project_iam_member" "github_actions_secret_viewer" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/secretmanager.viewer"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Deploy to Firebase Hosting
resource "google_project_iam_member" "github_actions_firebase_hosting" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/firebasehosting.admin"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

resource "google_project_iam_member" "github_actions_firebase_admin" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/firebase.admin"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Execute Cloud Run jobs (database migrations)
resource "google_project_iam_member" "github_actions_run_invoker" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Compute Engine permissions for Packer agent image builds
resource "google_project_iam_member" "github_actions_compute_instance_admin" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

resource "google_project_iam_member" "github_actions_compute_image_user" {
  count   = var.enable_cicd ? 1 : 0
  project = var.project_id
  role    = "roles/compute.imageUser"
  member  = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}

# Packer needs to create VMs as the default Compute Engine SA
data "google_compute_default_service_account" "default" {}

resource "google_service_account_iam_member" "github_actions_use_default_compute_sa" {
  count              = var.enable_cicd ? 1 : 0
  service_account_id = data.google_compute_default_service_account.default.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_actions_sa[0].email}"
}
