# Cloud Run Service for the Orchestrator
resource "google_cloud_run_v2_service" "orchestrator" {
  name     = "blueprint-orchestrator"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.orchestrator_sa.email

    # Pin the min/max instance scaling so redeploys don't drift back to defaults.
    # Reaper + orchestrator background loops need min=1 to stay warm.
    #
    # R6-W2-CRIT (audit 2026-04-30): max=1 is intentional. The lifespan in
    # backend/app/lifecycle.py spawns 9 background services (reaper,
    # orchestrator_worker, gcp_cleanup, gcp_image_cleanup, secret_reconciliation,
    # stale_branch_cleanup, auto_archive, config_sync, availability_refresh).
    # None of them have leader election — per-row safety holds via
    # task_lock(skip_locked=True) and FOR UPDATE, but side effects are not
    # per-row: GitHub/GCP API poll rates would multiply Nx, _provision_coroutines
    # is a per-instance dict racing against _get_actual_gcp_vm_count, stale-branch
    # cleanup and image pruner would issue N redundant deletes.
    # Until pg_advisory_lock leader election lands in lifecycle.py
    # (see TODO there), max MUST stay at 1 — DO NOT bump without first
    # implementing leader election.
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
    # Per-instance concurrency (default is 80). Bumped so the admin UI polling
    # + background loops don't hit "no available instance" 429s during cold
    # starts or scale-up lag. Cloud Run's 429 response bypasses our CORS
    # middleware, so the frontend sees CORS errors instead of a retryable 429.
    max_instance_request_concurrency = 120

    containers {
      # Placeholder image. CI/CD will overwrite this with the actual application image.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # Cost-optimization 2026-05-02: 4Gi/2vCPU → 2Gi/1vCPU.
      #
      # CPU is the dominant always-on cost on no-cpu-throttling Cloud Run
      # (~$62/mo per vCPU; memory is ~$6.50/mo per GiB). Halving CPU saves
      # ~$62/mo, halving memory saves ~$13/mo. Total ~$75/mo savings.
      #
      # The 4Gi/2vCPU bump on 2026-04-16 was for analytics endpoints that
      # load 14-day task windows with full JSONB. Those endpoints will be
      # slower at 2Gi/1vCPU — bump back if Cloud Run logs show OOMKilled
      # or sustained CPU saturation under analytics load.
      #
      # Watchpoint: `OOMKilled` events in Cloud Run logs, /health 503s
      # under analytics load, GC pressure on long-running endpoints.
      resources {
        limits = {
          memory = "2Gi"
          cpu    = "1000m"
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

    # Connect Cloud Run to the VPC so it can reach the private Cloud SQL instance
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
  # full env list with 13 vars as of 2026-05-02 (ALLOWED_ORIGINS, LOCAL_MODE,
  # MAX_CONCURRENT_VMS, INITIAL_ADMIN_EMAIL, VM_PROVISIONING_MODEL,
  # DISABLED_REGIONS, plus 7 others); this file only declares 7. Without
  # ignore_changes on env, the next `terraform apply` planned to delete the
  # cloudbuild-only variables — taking ALLOWED_ORIGINS with them and
  # triggering a CORS outage.
  # Ownership split: Terraform tracks the *structure* of the resource
  # (containers, volumes, scaling, vpc_access); CI tracks the env-var
  # *content*. Drift between cloudbuild.yaml and this file is reviewable
  # in source control because both are checked in.
  #
  # ============================================================
  # WARNING TO OPERATORS — env-var changes here are SILENTLY IGNORED
  # ============================================================
  # `template[0].containers[0].env` is on ignore_changes. That means:
  #   - Adding a new `env { name = "X" value = "y" }` block in this file
  #     will NOT push that variable to the running Cloud Run revision.
  #   - `terraform plan` may even show "no changes" for env-var edits.
  #
  # To actually add or modify a Cloud Run env var:
  #   1. Edit `backend/cloudbuild.yaml`'s `--set-env-vars` argument list
  #      (THIS is what the deploy step runs and it owns env-var content).
  #   2. OPTIONALLY mirror the new var here in this file for documentation
  #      (drift-readability — operators reading cloud_run.tf can see what
  #      the running service has).
  #
  # Do NOT expect `terraform apply` to update env vars on the running
  # revision. The shadow declarations in this file exist only so terraform
  # plan stays clean against the existing state. See R3-W4 (audit
  # 2026-04-30) and R4-W4 M6 (audit 2026-04-29) for the ownership rationale.
  # ============================================================
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      # See WARNING block above — env-var changes in this file are silently
      # ignored. The authoritative source is cloudbuild.yaml's --set-env-vars.
      template[0].containers[0].env,
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
