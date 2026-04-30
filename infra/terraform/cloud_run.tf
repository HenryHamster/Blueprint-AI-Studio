# Cloud Run Service for the Orchestrator
resource "google_cloud_run_v2_service" "orchestrator" {
  name     = "blueprint-orchestrator"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.orchestrator_sa.email

    # Pin the min/max instance scaling so redeploys don't drift back to defaults.
    # Reaper + orchestrator background loops need min=1 to stay warm.
    scaling {
      min_instance_count = 1
      max_instance_count = 5
    }
    # Per-instance concurrency (default is 80). Bumped so the admin UI polling
    # + background loops don't hit "no available instance" 429s during cold
    # starts or scale-up lag. Cloud Run's 429 response bypasses our CORS
    # middleware, so the frontend sees CORS errors instead of a retryable 429.
    max_instance_request_concurrency = 120

    containers {
      # Placeholder image. CI/CD will overwrite this with the actual application image.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # Memory: 2Gi→4Gi on 2026-04-16. load_only() on hot queries reduces
      # steady-state usage; 4Gi gives headroom for analytics endpoints that
      # load 14-day task windows with full JSONB. CPU bumped to 2 vCPU to
      # match — GC pressure needs CPU too.
      resources {
        limits = {
          memory = "4Gi"
          cpu    = "2000m"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_ZONE"
        value = var.zone
      }
      env {
        name  = "VPC_NETWORK"
        value = google_compute_network.agent_vpc.name
      }
      env {
        name  = "VPC_SUBNET"
        value = google_compute_subnetwork.agent_subnet.name
      }
      env {
        name  = "AGENT_SERVICE_ACCOUNT_EMAIL"
        value = google_service_account.agent_sa.email
      }
      env {
        name  = "ARTIFACT_BUCKET_NAME"
        value = google_storage_bucket.artifacts.name
      }
      # Set after first deploy: terraform apply -var="orchestrator_url=https://..."
      env {
        name  = "ORCHESTRATOR_URL"
        value = var.orchestrator_url
      }

      # /health returns 503 when the orchestrator background loop hasn't
      # ticked in 60s (see backend/app/main.py /health). Without this probe
      # a hung instance keeps port 8080 open and never gets replaced —
      # GFE then returns 503 with no CORS headers and the admin UI surfaces
      # it as a CORS error. ~150s to replacement (60s delay + 3 × 30s).
      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 60
        period_seconds        = 30
        timeout_seconds       = 10
        failure_threshold     = 3
      }
    }

    # Connect Cloud Run to the VPC so it can reach the private AlloyDB instance
    vpc_access {
      network_interfaces {
        network    = google_compute_network.agent_vpc.name
        subnetwork = google_compute_subnetwork.agent_subnet.name
      }
      egress = "ALL_TRAFFIC"
    }
  }

  # Ignore image changes so Terraform doesn't revert CI/CD deployments.
  # R3-W4 C2 (audit 2026-04-30): env reverted onto ignore_changes after
  # Round 2's M5 narrowing caused a regression. cloudbuild.yaml's
  # --set-env-vars is the source of truth and declaratively replaces the
  # full env list with 11 vars (ALLOWED_ORIGINS, LOCAL_MODE,
  # MAX_CONCURRENT_VMS, INITIAL_ADMIN_EMAIL, plus 7 others); this file
  # only declares 7. Without ignore_changes on env, the next
  # `terraform apply` planned to delete the 4 cloudbuild-only variables
  # — taking ALLOWED_ORIGINS with them and triggering a CORS outage.
  # Ownership split: Terraform tracks the *structure* of the resource
  # (containers, volumes, scaling, vpc_access); CI tracks the env-var
  # *content*. Drift between cloudbuild.yaml and this file is reviewable
  # in source control because both are checked in.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].containers[0].env, # CI deploys via cloudbuild.yaml own the env-var set;
      # Terraform tracks structure, CI tracks content. See R3-W4 (audit 2026-04-30).
    ]
  }
}

# Allow unauthenticated access to the Cloud Run service (for webhooks and public API endpoints)
resource "google_cloud_run_service_iam_member" "public_access" {
  location = google_cloud_run_v2_service.orchestrator.location
  project  = google_cloud_run_v2_service.orchestrator.project
  service  = google_cloud_run_v2_service.orchestrator.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
