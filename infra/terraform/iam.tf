# ------------------------------------------------------------------------------
# Orchestrator Service Account
# ------------------------------------------------------------------------------
resource "google_service_account" "orchestrator_sa" {
  account_id   = "orchestrator-sa"
  display_name = "Orchestrator Cloud Run Service Account"
}

# Grant Orchestrator SA permissions to manage Compute Engine instances
resource "google_project_iam_member" "orchestrator_compute_instance_admin" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to read secrets
resource "google_project_iam_member" "orchestrator_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to create secrets and add versions
resource "google_project_iam_member" "orchestrator_secret_version_adder" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# secretmanager.admin needed for create_secret() — no narrower project-level role exists
resource "google_project_iam_member" "orchestrator_secret_creator" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to sign blobs (for GCS signed URLs)
resource "google_service_account_iam_member" "orchestrator_sa_token_creator" {
  service_account_id = google_service_account.orchestrator_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# ------------------------------------------------------------------------------
# Agent "Naked" Service Account
# ------------------------------------------------------------------------------
resource "google_service_account" "agent_sa" {
  account_id   = "agent-naked-sa"
  display_name = "Agent VM Restricted Service Account"
  description  = "Has ZERO default permissions. Orchestrator grants access dynamically."
}

# Grant Agent SA permission to write logs (Stackdriver/Cloud Logging)
resource "google_project_iam_member" "agent_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Grant Orchestrator SA permission to attach the Agent SA to new VMs
resource "google_service_account_iam_member" "orchestrator_act_as_agent" {
  service_account_id = google_service_account.agent_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# ------------------------------------------------------------------------------
# Artifact Bucket (payloads, credentials, screenshots)
# ------------------------------------------------------------------------------
resource "google_storage_bucket" "artifacts" {
  name          = "${var.project_id}-agent-artifacts"
  location      = var.region
  force_destroy = false
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 7 }
    action { type = "Delete" }
  }
}

# Orchestrator can read/write to the bucket
resource "google_storage_bucket_iam_member" "orchestrator_bucket_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Agent VMs can only read from the bucket (download payloads + credentials)
resource "google_storage_bucket_iam_member" "agent_bucket_reader" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.agent_sa.email}"
}
