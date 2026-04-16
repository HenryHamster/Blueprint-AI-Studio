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
      max_instance_count = 3
    }

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
  
  # Ignore image changes so Terraform doesn't revert CI/CD deployments
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].containers[0].env, # Ignore env changes if CI/CD injects secrets
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
