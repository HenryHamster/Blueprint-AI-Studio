# Cloud Run Service for the Orchestrator
resource "google_cloud_run_v2_service" "orchestrator" {
  name     = "blueprint-orchestrator"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.orchestrator_sa.email

    # Pin the min/max instance scaling so redeploys don't drift back to defaults.
    # Cloud Run is API-only; the off-cloud worker owns always-on background loops.
    #
    # max=1 keeps the API footprint bounded while local capacity owns execution.
    # Bumping it still requires a memory-sizing review; 4Gi/2vCPU remains sized
    # conservatively for one API instance.
    scaling {
      # The off-cloud worker owns all always-on background services. Cloud Run
      # is now the public API surface and may safely cold-start on demand.
      min_instance_count = 0
      max_instance_count = 1
    }
    # Per-instance concurrency (default is 80). Bumped so the admin UI polling
    # does not hit "no available instance" 429s during cold starts or scale-up
    # lag. Cloud Run's 429 response bypasses our CORS
    # middleware, so the frontend sees CORS errors instead of a retryable 429.
    max_instance_request_concurrency = 120

    containers {
      # Placeholder image. CI/CD will overwrite this with the actual application image.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # Reverted 2026-05-29: the 2026-05-02 cost-cut to 2Gi/1vCPU triggered its
      # own watchpoint. The singleton-leader orchestrator (orchestrator_worker,
      # reaper, sweeps, SR-trend compute, 14-day-window analytics endpoints)
      # exceeded 2Gi and was OOM-killed (~14×/day on 2026-05-29). Each OOM drops
      # the leader's DB connection → releases the pg_advisory_lock → a standby
      # watchdog respawns it ("prior leader died" hard-exit), i.e. the OOM drives
      # a leader-respawn restart loop, and requests killed mid-flight (e.g.
      # /api/admin/health-overview) surface as 500s. Back to the 2026-04-16
      # sizing (4Gi/2vCPU). Cost: ~+$75/mo vs the 2Gi/1vCPU cut.
      #
      # Watchpoint: OOM/day was *rising* before this revert (2→3→15 over the
      # week), so if `OOMKilled` returns at 4Gi it is scale growth, not undersize
      # — pursue the long-term fix (move heavy/analytics compute off the
      # singleton leader, stream/paginate the 14-day JSONB windows) rather than
      # bumping memory again.
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
        name  = "BLUEPRINT_PROCESS_ROLE"
        value = "api"
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
      # gcloud stamps these informational fields on every deploy. They do not
      # affect the serving revision and should not create perpetual drift.
      client,
      client_version,
      template[0].containers[0].image,
      # See WARNING block above — env-var changes in this file are silently
      # ignored. The authoritative source is cloudbuild.yaml's --set-env-vars.
      template[0].containers[0].env,
    ]
  }
}

# Allow unauthenticated access for webhooks and public API endpoints. During a
# local-control-plane cutover, explicitly setting the variable false removes
# this binding without destroying the service, preserving a rollback target.
moved {
  from = google_cloud_run_service_iam_member.public_access
  to   = google_cloud_run_service_iam_member.public_access[0]
}

resource "google_cloud_run_service_iam_member" "public_access" {
  count    = var.enable_public_orchestrator_access ? 1 : 0
  location = google_cloud_run_v2_service.orchestrator.location
  project  = google_cloud_run_v2_service.orchestrator.project
  service  = google_cloud_run_v2_service.orchestrator.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Dedicated service account for Cloud Run Job executions. Read-only LLM agent tasks only.
resource "google_service_account" "agent_job_sa" {
  account_id   = "agent-job-sa"
  display_name = "Agent Cloud Run Job Service Account"
  description  = "Dedicated SA for Cloud Run Job executions. Read-only LLM agent tasks only."
}

# ==============================================================================
# Cloud Run Job — lightweight execution path for read-only / non-merging tasks
# ==============================================================================
# Routes the ~20 LLM-only / non-merge task types (research, strategic, design,
# memo, review, panel_review, audit, web_research, ...) off a dedicated
# e2-standard-2 GCE VM and onto a scale-to-zero Cloud Run Job execution. The
# agent container image is reused verbatim — the only execution-environment
# differences are: run-to-completion (no inbound $PORT), billed per 100ms, and
# per-execution env overrides injected by the orchestrator's `run_job` call
# (see backend/app/compute_cloud_run.launch_agent_cloud_run_job).
#
# DARK BY DEFAULT: the backend dispatch path is gated behind an empty
# `cloud_run_task_type_allowlist` runtime setting. Applying this resource alone
# changes no task routing — activation still requires (1) a successful agent
# image deployment, (2) CLOUD_RUN_JOB_NAME in cloudbuild.yaml, and (3) a
# populated allowlist. See docs/cloud-run-jobs-activation.md for the full list.
#
# The job MUST be named exactly "blueprint-agent-job" so that the documented
# CLOUD_RUN_JOB_NAME value
#   projects/<project>/locations/us-central1/jobs/blueprint-agent-job
# resolves what compute_cloud_run.py dispatches against.

resource "google_cloud_run_v2_job" "blueprint_agent_job" {
  name     = "blueprint-agent-job"
  location = var.region

  labels = {
    app        = "blueprint-agent"
    managed-by = "terraform"
  }

  template {
    # taskTimeout = 4h, matching _TIMEOUT_SECONDS_STANDARD in
    # compute_cloud_run.py and the GCE reaper TTL. A stuck execution is reaped
    # before it accumulates meaningful cost. The orchestrator overrides this
    # per-execution for heavier task types via RunJobRequest.Overrides.timeout.
    task_count = 1

    template {
      service_account = google_service_account.agent_job_sa.email

      # Run-to-completion: a failed execution is surfaced to the orchestrator
      # via the agent's own status reporting, not retried by Cloud Run, so a
      # crash does not silently double-dispatch the task.
      max_retries = 0
      timeout     = "14400s"

      containers {
        # Bootstrap placeholder. The main deploy workflow builds and rolls out
        # agent/** changes; the ignore_changes below keeps Terraform from
        # reverting that CI-owned image back to the placeholder.
        image = "us-docker.pkg.dev/cloudrun/container/hello"

        # Tier A3 default sizing: 1 vCPU / 2 GiB. Zero-checkout (Tier A1) types
        # could run at 1 GiB — a follow-on tuning, not blocking activation.
        resources {
          limits = {
            memory = "2Gi"
            cpu    = "1000m"
          }
        }

        # Base environment — mirrors the orchestrator service's non-secret env.
        # Per-execution values (TASK_ID, VM_INSTANCE_ID, PAYLOAD_URI,
        # AGENT_SECRET_VERSION, CLOUD_RUN_JOB) are injected as container
        # overrides by launch_agent_cloud_run_job and are NOT declared here.
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
        env {
          name  = "ORCHESTRATOR_URL"
          value = var.orchestrator_url
        }
      }

      # Direct VPC egress (NOT a vpc_access_connector) — mirrors the
      # orchestrator service so the Job reaches the private Cloud SQL instance,
      # GitHub, and LLM APIs over the same network path.
      vpc_access {
        network_interfaces {
          network    = google_compute_network.agent_vpc.name
          subnetwork = google_compute_subnetwork.agent_subnet.name
        }
        egress = "ALL_TRAFFIC"
      }
    }
  }

  # CI owns the live agent image; do not let Terraform revert it back to the
  # bootstrap placeholder on the next apply.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# Grant the orchestrator SA least-privilege permissions to trigger executions
# of the Job with container env overrides. roles/run.jobsExecutorWithOverrides
# is the narrowest predefined role covering run.jobs.run and
# run.jobs.runWithOverrides — required because compute_cloud_run.py calls
# client.run_job with RunJobRequest.Overrides. The former broad
# roles/run.developer is replaced by this single narrower predefined role.
resource "google_cloud_run_v2_job_iam_member" "orchestrator_runs_agent_job" {
  location = google_cloud_run_v2_job.blueprint_agent_job.location
  project  = google_cloud_run_v2_job.blueprint_agent_job.project
  name     = google_cloud_run_v2_job.blueprint_agent_job.name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# ==============================================================================
# Cloud Armor — rate limiting and adaptive DDoS protection
# ==============================================================================
# The orchestrator Cloud Run service is publicly accessible only while
# enable_public_orchestrator_access=true (the backward-compatible default).
# for GitHub webhooks and agent callbacks. Cloud Armor sits in front of the
# service via a Global HTTPS Load Balancer with a Serverless NEG backend to
# enforce per-IP rate limits and Adaptive Protection layer-7 DDoS defense.
#
# Traffic path (after adding a Global LB frontend — see outputs.tf for the
# load balancer IP once created):
#   Internet → Global HTTPS LB → Cloud Armor policy → orchestrator NEG → Cloud Run

# Cloud Armor security policy: rate-limit (1 000 req/min per source IP) and
# Adaptive Protection (automatic layer-7 DDoS anomaly detection and rule proposals).
resource "google_compute_security_policy" "orchestrator" {
  name        = "orchestrator-security-policy"
  description = "Rate-limit and adaptive-protection policy for the Blueprint orchestrator"

  # Adaptive Protection continuously learns normal traffic patterns and proposes
  # WAF rules to block anomalous surges without manual intervention.
  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable = true
    }
  }

  # Rate-limit: throttle any single source IP to 1 000 requests per minute.
  # Traffic within the threshold is allowed; excess receives HTTP 429.
  rule {
    action      = "throttle"
    priority    = 1000
    description = "Rate-limit: 1000 req/min per source IP"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"

      rate_limit_threshold {
        count        = 1000
        interval_sec = 60
      }

      enforce_on_key = "IP"
    }
  }

  # Default rule: allow all traffic not matched by a higher-priority rule.
  rule {
    action      = "allow"
    priority    = 2147483647
    description = "Default allow"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# Serverless NEG — points the global backend service at the Cloud Run orchestrator
# so Cloud Armor can inspect and throttle inbound requests before they reach it.
resource "google_compute_region_network_endpoint_group" "orchestrator" {
  name                  = "orchestrator-serverless-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.orchestrator.name
  }
}

# Global backend service — wires the Cloud Armor policy to the serverless NEG.
# A Global HTTPS Load Balancer frontend (URL map + target proxy + forwarding
# rule) is the operator action needed to route external traffic through this
# backend and enforce the policy end-to-end.
resource "google_compute_backend_service" "orchestrator" {
  name                  = "orchestrator-backend"
  description           = "Backend for Blueprint orchestrator Cloud Run service with Cloud Armor"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.orchestrator.id

  backend {
    group = google_compute_region_network_endpoint_group.orchestrator.id
  }
}
