# ------------------------------------------------------------------------------
# Orchestrator Service Account
# ------------------------------------------------------------------------------
resource "google_service_account" "orchestrator_sa" {
  account_id   = "orchestrator-sa"
  display_name = "Orchestrator Cloud Run Service Account"
}

# Grant Orchestrator SA permissions to manage Compute Engine instances
resource "google_project_iam_member" "orchestrator_compute_admin" {
  project = var.project_id
  role    = "roles/compute.admin"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to manage Secret Manager
resource "google_project_iam_member" "orchestrator_secret_admin" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to manage Cloud Storage (for signed URLs)
resource "google_project_iam_member" "orchestrator_storage_admin" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# ------------------------------------------------------------------------------
# Agent "Naked" Service Account
# ------------------------------------------------------------------------------
resource "google_service_account" "agent_sa" {
  account_id   = "agent-naked-sa"
  display_name = "Agent VM Restricted Service Account"
  description  = "Has ZERO default permissions. Orchestrator grants access dynamically."
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
