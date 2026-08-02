locals {
  restore_verifier_prerequisites_enabled = (
    local.recovery_enabled && var.enable_restore_verifier_prerequisites
  )
}

resource "google_compute_network" "restore_verifier" {
  count                   = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project                 = var.recovery_project_id
  name                    = var.restore_verifier_private_network
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [
    google_project_service.restore_verifier_prerequisite,
    terraform_data.restore_verifier_prerequisite_configuration,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_subnetwork" "restore_verifier" {
  count                    = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project                  = var.recovery_project_id
  region                   = var.recovery_region
  name                     = var.restore_verifier_private_subnetwork
  network                  = google_compute_network.restore_verifier[0].id
  ip_cidr_range            = var.restore_verifier_subnetwork_cidr
  private_ip_google_access = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_managed_zone" "restricted_googleapis" {
  count       = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project     = var.recovery_project_id
  name        = "blueprint-restricted-googleapis"
  dns_name    = "googleapis.com."
  description = "Resolve Google APIs through the restricted VIP for recovery jobs."
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.restore_verifier[0].id
    }
  }

  depends_on = [google_project_service.restore_verifier_prerequisite]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_record_set" "restricted_googleapis" {
  count        = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project      = var.recovery_project_id
  managed_zone = google_dns_managed_zone.restricted_googleapis[0].name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "googleapis_wildcard" {
  count        = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project      = var.recovery_project_id
  managed_zone = google_dns_managed_zone.restricted_googleapis[0].name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

# Cloud Run Direct VPC egress still reaches the VPC metadata resolver. A
# response policy makes that resolver fail closed: Google API names resolve to
# the restricted VIP, while every less-specific name is sinkholed locally.
resource "google_dns_response_policy" "restore_verifier" {
  count                = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project              = var.recovery_project_id
  response_policy_name = "blueprint-restore-egress"
  description          = "Allow only restricted Google API DNS for recovery jobs."

  networks {
    network_url = google_compute_network.restore_verifier[0].id
  }

  depends_on = [google_project_service.restore_verifier_prerequisite]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_response_policy_rule" "restricted_googleapis" {
  count           = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project         = var.recovery_project_id
  response_policy = google_dns_response_policy.restore_verifier[0].response_policy_name
  rule_name       = "restricted-googleapis"
  dns_name        = "restricted.googleapis.com."

  local_data {
    local_datas {
      name    = "restricted.googleapis.com."
      type    = "A"
      ttl     = 300
      rrdatas = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Cloud Run ADC resolves this exact hostname before calling the metadata IP.
# Keep the exception exact so it cannot carry attacker-controlled subdomains.
resource "google_dns_response_policy_rule" "metadata" {
  count           = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project         = var.recovery_project_id
  response_policy = google_dns_response_policy.restore_verifier[0].response_policy_name
  rule_name       = "metadata-google-internal"
  dns_name        = "metadata.google.internal."

  local_data {
    local_datas {
      name    = "metadata.google.internal."
      type    = "A"
      ttl     = 300
      rrdatas = ["169.254.169.254"]
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_response_policy_rule" "googleapis_wildcard" {
  count           = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project         = var.recovery_project_id
  response_policy = google_dns_response_policy.restore_verifier[0].response_policy_name
  rule_name       = "googleapis-wildcard"
  dns_name        = "*.googleapis.com."

  local_data {
    local_datas {
      name    = "*.googleapis.com."
      type    = "CNAME"
      ttl     = 300
      rrdatas = ["restricted.googleapis.com."]
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_dns_response_policy_rule" "deny_other_names" {
  count           = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project         = var.recovery_project_id
  response_policy = google_dns_response_policy.restore_verifier[0].response_policy_name
  rule_name       = "deny-other-names"
  dns_name        = "*."

  local_data {
    local_datas {
      name    = "*."
      type    = "A"
      ttl     = 300
      rrdatas = ["0.0.0.0"]
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_global_address" "restore_verifier_private_services" {
  count         = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project       = var.recovery_project_id
  name          = "blueprint-restore-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.restore_verifier_private_service_prefix_length
  network       = google_compute_network.restore_verifier[0].id

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_networking_connection" "restore_verifier" {
  count                   = local.restore_verifier_prerequisites_enabled ? 1 : 0
  network                 = google_compute_network.restore_verifier[0].id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.restore_verifier_private_services[0].name]
  deletion_policy         = "ABANDON"

  depends_on = [
    google_project_service.restore_verifier_prerequisite,
    google_project_iam_member.recovery_service_networking_service_agent,
    terraform_data.restore_verifier_prerequisite_configuration,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository" "restore_verifier" {
  count         = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project       = var.recovery_project_id
  location      = var.recovery_region
  repository_id = var.restore_verifier_artifact_repository
  description   = "Immutable images for Blueprint recovery-only restore verification."
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  depends_on = [
    google_project_service.restore_verifier_prerequisite,
    terraform_data.restore_verifier_prerequisite_configuration,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository_iam_member" "cloud_run_reader" {
  count      = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project    = var.recovery_project_id
  location   = var.recovery_region
  repository = google_artifact_registry_repository.restore_verifier[0].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_project_service_identity.cloud_run[0].email}"
}

resource "google_compute_firewall" "restore_allow_metadata" {
  count              = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project            = var.recovery_project_id
  name               = "blueprint-restore-allow-metadata"
  network            = google_compute_network.restore_verifier[0].name
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["169.254.169.254/32"]
  target_tags        = ["blueprint-restore-verifier"]

  allow {
    protocol = "tcp"
    ports    = ["53", "80"]
  }
  allow {
    protocol = "udp"
    ports    = ["53"]
  }
}

resource "google_compute_firewall" "restore_allow_googleapis" {
  count              = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project            = var.recovery_project_id
  name               = "blueprint-restore-allow-googleapis"
  network            = google_compute_network.restore_verifier[0].name
  direction          = "EGRESS"
  priority           = 910
  destination_ranges = ["199.36.153.4/30"]
  target_tags        = ["blueprint-restore-verifier"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "restore_allow_cloud_sql" {
  count       = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project     = var.recovery_project_id
  name        = "blueprint-restore-allow-cloud-sql"
  network     = google_compute_network.restore_verifier[0].name
  direction   = "EGRESS"
  priority    = 920
  target_tags = ["blueprint-restore-verifier"]
  destination_ranges = [
    "${google_compute_global_address.restore_verifier_private_services[0].address}/${google_compute_global_address.restore_verifier_private_services[0].prefix_length}",
  ]

  allow {
    protocol = "tcp"
    ports    = ["3307"]
  }
}

resource "google_compute_firewall" "restore_deny_other_egress" {
  count              = local.restore_verifier_prerequisites_enabled ? 1 : 0
  project            = var.recovery_project_id
  name               = "blueprint-restore-deny-other-egress"
  network            = google_compute_network.restore_verifier[0].name
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["blueprint-restore-verifier"]

  deny {
    protocol = "all"
  }
}
