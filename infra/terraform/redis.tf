# ------------------------------------------------------------------------------
# Memorystore for Redis (shared coordination layer)
# Used for: distributed provision semaphore (C5), webhook dedup (C8),
#           rate limiter backend (C13). All features fall back to per-instance
#           in-memory state when Redis is unavailable.
# ------------------------------------------------------------------------------

resource "google_redis_instance" "orchestrator" {
  name           = "blueprint-orchestrator-redis"
  tier           = "BASIC" # No HA needed — all features degrade gracefully
  memory_size_gb = 1
  region         = var.region

  authorized_network = google_compute_network.agent_vpc.id
  connect_mode       = "DIRECT_PEERING"

  redis_version = "REDIS_7_0"

  labels = {
    managed-by = "terraform"
    service    = "blueprint-orchestrator"
  }
}

# Store connection URL in Secret Manager (same pattern as DB password)
resource "google_secret_manager_secret" "redis_url" {
  secret_id = "REDIS_URL"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "redis_url" {
  secret      = google_secret_manager_secret.redis_url.id
  secret_data = "redis://${google_redis_instance.orchestrator.host}:${google_redis_instance.orchestrator.port}/0"
}

# Grant the Orchestrator SA access to read the Redis URL secret
resource "google_secret_manager_secret_iam_member" "orchestrator_redis_url_access" {
  secret_id = google_secret_manager_secret.redis_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
