# ------------------------------------------------------------------------------
# Cloud Build remote-build lane for project-baked compute images
#
# The orchestrator (Cloud Run, no local docker daemon) uploads a source tarball
# to this disposable bucket, submits a Cloud Build job that runs AS
# image_builder_sa, then deletes the staged object when the build completes.
#
# Matching env vars wired in backend/cloudbuild.yaml:
#   COMPUTE_IMAGE_BUILD_SOURCE_BUCKET = ${project_id}-compute-build-source
#   COMPUTE_IMAGE_BUILDER_SA          = image-builder-sa@...
#   COMPUTE_IMAGE_BUILD_LOCATION      = (region)
# ------------------------------------------------------------------------------

# Disposable staging bucket for build-context tarballs.
# Deliberately separate from ${var.project_id}-agent-artifacts (google_storage_bucket.artifacts)
# — the builder SA must NOT be able to read the agent-artifacts bucket.
resource "google_storage_bucket" "compute_build_source" {
  name                        = "${var.project_id}-compute-build-source"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  # Source tarballs staged under build-sources/ are disposable.
  # A 1-day TTL ensures orphaned objects (e.g. from a cancelled build before the
  # orchestrator can delete them) never accumulate.
  lifecycle_rule {
    condition {
      age            = 1
      matches_prefix = ["build-sources/"]
    }
    action { type = "Delete" }
  }
}

# Orchestrator SA: submit, poll, and cancel Cloud Build jobs (project-level).
# This lets the orchestrator call cloudbuild.builds.{create,get,cancel} without
# granting project-wide Cloud Build admin.
resource "google_project_iam_member" "orchestrator_cloudbuild_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Orchestrator SA: upload the source tarball and delete it after the build.
# Scoped to the build-source bucket only — the orchestrator already has
# objectAdmin on the agent-artifacts bucket (google_storage_bucket.artifacts)
# via orchestrator_bucket_admin in iam.tf; that binding stays unchanged.
resource "google_storage_bucket_iam_member" "orchestrator_compute_build_source_admin" {
  bucket = google_storage_bucket.compute_build_source.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# Builder SA: pull its StorageSource from the build-source bucket.
# Cloud Build reads the staged tarball before starting build steps; without
# this the build fails at the StorageSource fetch stage.
# Scoped to this bucket only — the builder must NOT touch agent-artifacts.
resource "google_storage_bucket_iam_member" "image_builder_compute_build_source_reader" {
  bucket = google_storage_bucket.compute_build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.image_builder_sa.email}"
}

# Builder SA: write build logs when the build specifies a custom service_account
# with logging=CLOUD_LOGGING_ONLY.  This grant is MANDATORY — Cloud Build
# immediately fails any build whose custom SA lacks logging.logWriter.
resource "google_project_iam_member" "image_builder_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.image_builder_sa.email}"
}
