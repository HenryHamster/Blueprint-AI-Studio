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
  cleanup_policies {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
  cleanup_policies {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "7776000s" # 90 days; KEEP keep-recent-tagged runs first and protects the 5 most recent
    }
  }
}

# Artifact Registry for Project-Baked Compute Images
#
# ⚠️ RETENTION SAFETY (audit finding #8). This repo is NOT like the other two.
# Its contents are DIGEST-PINNED BY THE DATABASE: `project_compute_image` rows map
# dep_hash -> `sha256:...`, and every compute job records the exact digest it ran
# (`polling_metadata.container_image`). A deleted digest is therefore not "a stale
# layer we can rebuild" — it is an unrecoverable break: the dep_hash no longer
# resolves, and the provenance of every past result that ran on it is gone (a
# rebuild produces a DIFFERENT digest). Time/count-based retention cannot see any
# of that: the registry has no view of the DB.
#
# The ONLY thing standing between the DELETE policies below and a live digest is
# the orchestrator's DB-aware guard sweep
# (reaper_sweeps.sweep_protect_referenced_compute_image_digests), which stamps an
# `inuse-<shorthash>` tag on current/default and job-referenced digests for the `keep-inuse-tagged` KEEP
# policy to shield. That guard is BEST-EFFORT by construction: hourly cadence, all
# registry helpers fail open, and it is brand-new code that has never run against a
# populated prod registry. Retention here is therefore configured to survive the
# guard being wrong or down, not to assume it is right.
resource "google_artifact_registry_repository" "compute_images_repo" {
  location      = var.region
  repository_id = "compute-images"
  description   = "Docker repository for per-project baked compute images"
  format        = "DOCKER"

  # DRY-RUN FIRST (audit finding #8). Deletion is EVALUATED and LOGGED but not
  # performed. This repo is new in this release, so its first real cleanup run in
  # prod would be the first irreversible delete ever performed against it — with an
  # unproven DB-aware guard as the only thing protecting in-use digests.
  #
  # OPERATOR: arm this (flip to `false`) only after confirming, from the Artifact
  # Registry cleanup dry-run logs, that (a) the guard is actually stamping
  # `inuse-` tags on current project defaults and live/recent ComputeJob digests,
  # and (b) nothing on the would-delete list is a digest the DB still
  # pins. Until then the cost of over-retention is a few $/month; the cost of a
  # wrong delete is an unrunnable dep_hash and destroyed result provenance.
  cleanup_policy_dry_run = true

  cleanup_policies {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  # DB-aware GC guard: the orchestrator reaper
  # (sweep_protect_referenced_compute_image_digests) stamps an `inuse-<shorthash>`
  # tag on a current project default or any digest a live/recent compute job still
  # depends on. This KEEP
  # policy runs before the DELETE policies below and shields anything so tagged,
  # so a running experiment's (or a recent result's provenance) image is never
  # reclaimed out from under it. The reaper prunes `inuse-` tags once the digest
  # is no longer referenced, letting delete-old-tagged reclaim it normally.
  cleanup_policies {
    id     = "keep-inuse-tagged"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["inuse-"]
    }
  }
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state = "UNTAGGED"
      # 30 days — deliberately NOT the 7 days used by the other repos.
      #
      # An in-use digest goes UNTAGGED the moment a rebake moves its build tag,
      # while the DB keeps pinning it. Its ONLY protection is the guard sweep
      # re-tagging it `inuse-`. A 7-day window meant a guard outage of one week
      # (the reaper wedging is a recurring failure mode in this system) was enough
      # to permanently delete digests live jobs pin. The window must not be
      # SHORTER than the guard's own reference horizon
      # (reaper_sweeps._COMPUTE_IMAGE_PROTECT_WINDOW_SECONDS = 30 days), so a
      # month-long guard outage still cannot outrun retention.
      older_than = "2592000s" # 30 days — keep in sync with _COMPUTE_IMAGE_PROTECT_WINDOW_SECONDS
    }
  }
  cleanup_policies {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state = "TAGGED"
      # 90 days. KEEP keep-recent-tagged + keep-inuse-tagged run first. The guard
      # (sweep_protect_referenced_compute_image_digests) stamps an `inuse-` tag
      # on the newest usable row for each project/image/registry identity, even
      # before its first job. Superseded rows stay protected only while a live or
      # recent (30-day provenance window) job references their digest, then their
      # inuse tag is pruned so this policy can reclaim them.
      older_than = "7776000s" # 90 days
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
  cleanup_policies {
    id     = "keep-recent-tagged"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
  cleanup_policies {
    id     = "delete-old-tagged"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "7776000s" # 90 days; KEEP keep-recent-tagged runs first and protects the 3 most recent
    }
  }
}
