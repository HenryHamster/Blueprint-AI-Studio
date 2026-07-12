# Artifact Registry for Docker Images
resource "google_artifact_registry_repository" "orchestrator_repo" {
  location      = var.region
  repository_id = "blueprint-repo"
  description   = "Docker repository for the Orchestrator backend"
  format        = "DOCKER"

  # Bound image-storage growth: $0.10/GB-month reclaimed on stale/untagged layers.
  # Real saving scales with repo size — a 100 GB estate saves $10/mo; 400 GB saves $40/mo.
  # KEEP policy runs before DELETE, so the 5 most recent tagged images are always preserved.
  cleanup_policy_dry_run = false
  cleanup_policy {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policy {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
  cleanup_policy {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "7776000s" # 90 days; KEEP keep-recent-tagged runs first and protects the 5 most recent
    }
  }
}

# Artifact Registry for Project-Baked Compute Images
resource "google_artifact_registry_repository" "compute_images_repo" {
  location      = var.region
  repository_id = "compute-images"
  description   = "Docker repository for per-project baked compute images"
  format        = "DOCKER"

  # Bound image-storage growth: $0.10/GB-month reclaimed on stale/untagged layers.
  # Real saving scales with repo size — a 100 GB estate saves $10/mo; 400 GB saves $40/mo.
  # KEEP policy runs before DELETE, so the 5 most recent tagged images are always preserved.
  cleanup_policy_dry_run = false
  cleanup_policy {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  # DB-aware GC guard: the orchestrator reaper
  # (sweep_protect_referenced_compute_image_digests) stamps an `inuse-<shorthash>`
  # tag on any digest a live/recent compute job still depends on. This KEEP
  # policy runs before the DELETE policies below and shields anything so tagged,
  # so a running experiment's (or a recent result's provenance) image is never
  # reclaimed out from under it. The reaper prunes `inuse-` tags once the digest
  # is no longer referenced, letting delete-old-tagged reclaim it normally.
  cleanup_policy {
    id     = "keep-inuse-tagged"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["inuse-"]
    }
  }
  cleanup_policy {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
  cleanup_policy {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "7776000s" # 90 days; KEEP keep-recent-tagged + keep-inuse-tagged run first
    }
  }
}

# Artifact Registry for Custom Agent VM OS Images (if using Packer/Docker to build them)
resource "google_artifact_registry_repository" "agent_images_repo" {
  location      = var.region
  repository_id = "agent-images-repo"
  description   = "Repository for custom Agent VM OS images or binaries"
  format        = "DOCKER"

  # Bound image-storage growth: $0.10/GB-month reclaimed on stale/untagged layers.
  # Agent VM images are less frequent; keep 3 most recent tagged to allow rollback.
  cleanup_policy_dry_run = false
  cleanup_policy {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }
  cleanup_policy {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
  cleanup_policy {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "7776000s" # 90 days; KEEP keep-recent-tagged runs first and protects the 3 most recent
    }
  }
}
