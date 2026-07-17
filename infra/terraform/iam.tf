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

# Grant Orchestrator SA permission to add new versions to existing secrets.
# Runtime paths: backend/app/compute.py (_store_agent_secret) and
# backend/app/secrets_manager.py (store_user_secret) call add_secret_version
# to write per-agent credentials and user token secrets dynamically.
resource "google_project_iam_member" "orchestrator_secret_version_adder" {
  project = var.project_id
  role    = "roles/secretmanager.secretVersionAdder"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permission to create new secret resources.
# Runtime paths: backend/app/compute.py (_store_agent_secret) and
# backend/app/secrets_manager.py (store_user_secret) call create_secret
# to provision per-agent credential secrets and user token secrets dynamically.
# Excludes secretmanager.admin: delete calls are best-effort (logged on failure,
# not fatal) and per-agent IAM grants (secretAccessor on agent-creds-* secrets
# for agent VMs) should be managed via google_secret_manager_secret_iam_member
# bindings per secret rather than project-wide IAM-management authority.
resource "google_project_iam_member" "orchestrator_secret_creator" {
  project = var.project_id
  role    = "roles/secretmanager.secretCreator"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permission to read logs (Cloud Logging viewer for error dashboard)
resource "google_project_iam_member" "orchestrator_log_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA permission to read Cloud Run services (to resolve serving SHA)
resource "google_project_iam_member" "orchestrator_run_viewer" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Grant Orchestrator SA self-impersonation for GCS signed URL signing.
#
# This self-referential binding lets orchestrator_sa call
# iamcredentials.generateIdToken / signBlob on itself, which is required by
# the storage.generate_signed_url() calls in backend/app/storage.py
# (e.g. the viewport-shot and artifact download-URL endpoints).
#
# SCOPE: orchestrator Cloud Run *service* ONLY.
# The binding is on orchestrator_sa.name and is consumed exclusively by the
# Cloud Run service whose service_account = orchestrator_sa.email. It is NOT
# a project-wide permission and does NOT propagate to other identities.
#
# EXPLICITLY EXCLUDED — agent_job_sa:
# The Cloud Run Job runs as agent_job_sa (a separate dedicated SA created for
# job executions). agent_job_sa MUST NOT receive roles/iam.serviceAccountTokenCreator
# — job executions are read-only LLM workloads (research, audit, review) that
# never need to sign GCS URLs. Adding tokenCreator to agent_job_sa would let a
# compromised job execution mint arbitrary credentials on its own SA — an
# unnecessary privilege escalation vector.
# When adding IAM bindings for agent_job_sa, verify no tokenCreator role is
# included. The two permitted bindings are:
#   roles/secretmanager.secretAccessor  (read long-lived secrets at boot)
#   roles/logging.logWriter             (write execution logs)
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

# Grant Agent SA secretAccessor scoped to agent-creds-* secrets only.
# The IAM condition restricts access to secrets whose name contains
# "secrets/agent-creds-" (all per-agent credential secrets created by
# _store_agent_secret in compute.py follow the pattern agent-creds-{vm_instance_id}).
# A compromised agent VM cannot read DB passwords, JWT signing keys,
# GitHub tokens, or any other project secret outside the agent-creds-* family.
resource "google_project_iam_member" "agent_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"

  condition {
    title       = "AgentCredsOnly"
    description = "Restrict agent SA to agent-creds-* secrets only"
    expression  = "resource.name.extract('secrets/{secret}').startsWith('agent-creds-')"
  }
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

  # Bundle tarballs and transfer archives expire at 30 days.
  lifecycle_rule {
    condition {
      age            = 30
      matches_suffix = [".tar.gz"]
    }
    action { type = "Delete" }
  }

  # Task payloads, credentials, screenshots, and agent logs live under
  # tasks/<uuid>/... and expire at 30 days.  Viewable compute-job media
  # (compute-jobs/<uuid>/artifacts/viewable/...) is deliberately excluded
  # from GCS lifecycle rules — it is retired by the reaper at 180 days
  # once its owning ComputeJob has been terminal long enough.
  lifecycle_rule {
    condition {
      age             = 30
      matches_prefix  = ["tasks/"]
    }
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

# ------------------------------------------------------------------------------
# Agent Cloud Run Job Service Account IAM
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "agent_job_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.agent_job_sa.email}"

  condition {
    title       = "AgentJobSecretsOnly"
    description = "Restrict Cloud Run Job SA to agent-creds-* secrets only"
    expression  = "resource.name.extract('secrets/{secret}').startsWith('agent-creds-')"
  }
}

resource "google_project_iam_member" "agent_job_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.agent_job_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "migrator_cloud_sql_password_access" {
  count     = var.use_cloud_sql ? 1 : 0
  secret_id = google_secret_manager_secret.cloud_sql_db_password_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrator_sa.email}"
}

# ------------------------------------------------------------------------------
# Isolated Compute-Image Builder (regional Cloud Build)
# ------------------------------------------------------------------------------
# Project-baked compute images are built by an isolated, regional Cloud Build
# run — NOT in-process on the orchestrator host (production Cloud Run has no
# docker daemon, and repo-controlled Dockerfile RUN steps must not execute
# co-tenant with the orchestrator identity). The build runs as this dedicated,
# least-privilege service account, which is granted ONLY what a build needs:
#   - read the disposable, short-retention build-source bucket (its own inputs)
#   - publish to the compute-images Artifact Registry repo ONLY (see
#     workload_identity.tf: compute_image_builder_compute_images_writer)
#   - write its own build logs
#
# It is DELIBERATELY denied:
#   - Secret Manager access (no secretAccessor / secretVersionAdder / creator)
#   - any platform or runtime credential
#   - access to the general agent-artifacts bucket
# A compromised or malicious Dockerfile RUN step therefore cannot read platform
# secrets, agent payloads/credentials, or general agent artifacts.
resource "google_service_account" "compute_image_builder_sa" {
  account_id   = "compute-image-builder-sa"
  display_name = "Isolated Compute-Image Builder Service Account"
  description  = "Least-privilege SA for regional Cloud Build project-image builds. No Secret Manager, no platform creds, no agent-artifacts access."
}

# Disposable, short-retention source store for build contexts. The orchestrator
# uploads the (untrusted) repo build context here; the regional Cloud Build
# reads it as the builder SA. Kept SEPARATE from the general agent-artifacts
# bucket so the builder's read grant is scoped to build inputs only. 1-day
# lifecycle: source tarballs are consumed at build time and never needed after.
resource "google_storage_bucket" "compute_build_source" {
  name                        = "${var.project_id}-compute-build-source"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 1 }
    action { type = "Delete" }
  }
}

# Builder can read ONLY the disposable build-source bucket (its own inputs).
# It has NO binding on the general agent-artifacts bucket.
resource "google_storage_bucket_iam_member" "compute_image_builder_source_reader" {
  bucket = google_storage_bucket.compute_build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.compute_image_builder_sa.email}"
}

# Orchestrator uploads (and cleans up) build contexts in the disposable
# source bucket before triggering a build.
resource "google_storage_bucket_iam_member" "orchestrator_compute_build_source_admin" {
  bucket = google_storage_bucket.compute_build_source.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Builder can write its own build logs. Nothing else project-wide.
resource "google_project_iam_member" "compute_image_builder_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.compute_image_builder_sa.email}"
}

# Orchestrator creates/gets/cancels builds in regional Cloud Build.
# roles/cloudbuild.builds.editor is the predefined role covering
# cloudbuild.builds.create/get/list/cancel — the orchestrator only ever submits,
# polls, and cancels project-image builds. It does NOT run builds itself.
resource "google_project_iam_member" "orchestrator_cloud_build_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Orchestrator must actAs the builder SA to submit a build that runs as it.
# Scoped to the builder SA ONLY (not a project-wide serviceAccountUser grant),
# so the orchestrator cannot impersonate any other identity.
resource "google_service_account_iam_member" "orchestrator_act_as_compute_image_builder" {
  service_account_id = google_service_account.compute_image_builder_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
