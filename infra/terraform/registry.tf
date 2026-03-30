# Artifact Registry for Docker Images
resource "google_artifact_registry_repository" "orchestrator_repo" {
  location      = var.region
  repository_id = "blueprint-repo"
  description   = "Docker repository for the Orchestrator backend"
  format        = "DOCKER"
}

# Artifact Registry for Custom Agent VM OS Images (if using Packer/Docker to build them)
resource "google_artifact_registry_repository" "agent_images_repo" {
  location      = var.region
  repository_id = "agent-images-repo"
  description   = "Repository for custom Agent VM OS images or binaries"
  format        = "DOCKER"
}
