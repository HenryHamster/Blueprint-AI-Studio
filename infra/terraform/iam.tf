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

# Grant Orchestrator SA project-wide read on Secret Manager.
# This is intentionally project-wide because Cloud Run --set-secrets in cloudbuild.yaml
# loads ~10 secrets at instance boot (DATABASE_URL, AGENT_JWT_SECRET, WS_TICKET_SECRET,
# GITHUB_*, ADMIN_API_KEY, REDIS_URL). Tightening this to per-secret bindings is a
# follow-up: setup.sh's create_secret_if_missing would need to also bind orchestrator_sa
# secretAccessor on each secret it creates. Out of scope for the AlloyDB→Cloud SQL cutover.
resource "google_project_iam_member" "orchestrator_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permissions to create, update IAM on, list, and delete
# runtime-managed secrets. Runtime paths in backend/app/compute.py and
# backend/app/secrets_manager.py provision per-agent credentials and user token
# secrets dynamically, so secretVersionAdder is insufficient here.
resource "google_project_iam_member" "orchestrator_secret_creator" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permission to read logs (Cloud Logging viewer for error dashboard)
resource "google_project_iam_member" "orchestrator_log_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
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
  name                        = "${var.project_id}-agent-artifacts"
  location                    = var.region
  force_destroy               = false
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

# ------------------------------------------------------------------------------
# Migrator service account — kept for the inverse-direction restore drill.
# Originally added for the AlloyDB→Cloud SQL cutover (now archived). Retained
# because the documented Cloud SQL→AlloyDB restore path
# (archive/README.md → "Restoring AlloyDB") reuses the same migrator VM
# pattern. Has read access to the Cloud SQL password secret and nothing else;
# the AlloyDB-password binding was removed when alloydb.tf was archived and
# restoration appends it back manually.
# ------------------------------------------------------------------------------
resource "google_service_account" "migrator_sa" {
  account_id   = "migrator-sa"
  display_name = "DB Migrator SA (transient, used during AlloyDB→Cloud SQL cutover)"
}

# migrator_alloydb_password_access binding removed when alloydb.tf was
# archived. Migrator-sa retains its Cloud SQL password binding below for
# the existing migration script's reverse direction (see archive/README.md).

resource "google_secret_manager_secret_iam_member" "migrator_cloud_sql_password_access" {
  count     = var.use_cloud_sql ? 1 : 0
  secret_id = google_secret_manager_secret.cloud_sql_db_password_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrator_sa.email}"
}
