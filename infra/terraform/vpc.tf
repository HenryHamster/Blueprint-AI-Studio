# Custom VPC Network for the Agent Sandbox
resource "google_compute_network" "agent_vpc" {
  name                    = "agent-sandbox-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

# Private Subnet for Agent VMs
resource "google_compute_subnetwork" "agent_subnet" {
  name                     = "agent-sandbox-subnet"
  ip_cidr_range            = "10.0.1.0/24"
  region                   = var.region
  network                  = google_compute_network.agent_vpc.id
  private_ip_google_access = true
}

# Cloud Router for NAT
resource "google_compute_router" "nat_router" {
  name    = "agent-nat-router"
  region  = var.region
  network = google_compute_network.agent_vpc.id
}

# Cloud NAT to allow outbound internet access for the private subnet
resource "google_compute_router_nat" "agent_nat" {
  name                               = "agent-nat"
  router                             = google_compute_router.nat_router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# ------------------------------------------------------------------------------
# Multi-region NAT — us-east1 capacity fallback
#
# 4-region NAT (us-central1 + us-east1 + us-east4 + us-west1) cost roughly
# $130/mo (4 × $32), so east4 and west1 remain disabled. us-east1 is kept as
# the single paid fallback because a 2026-07-15 Iowa stockout affected several
# CPU families while us-east1 N2 capacity remained available.
#
# compute.py is configured via DISABLED_REGIONS to skip east4 and west1.
#
# To restore another region, add it to additional_regions, terraform apply,
# remove it from DISABLED_REGIONS in backend/cloudbuild.yaml, and redeploy.
# ------------------------------------------------------------------------------

locals {
  additional_regions = {
    "us-east1" = "10.0.3.0/24"
  }
}

resource "google_compute_subnetwork" "agent_subnet_extra" {
  for_each                 = local.additional_regions
  name                     = "agent-sandbox-subnet-${each.key}"
  ip_cidr_range            = each.value
  region                   = each.key
  network                  = google_compute_network.agent_vpc.id
  private_ip_google_access = true
}

resource "google_compute_router" "nat_router_extra" {
  for_each = local.additional_regions
  name     = "agent-nat-router-${each.key}"
  region   = each.key
  network  = google_compute_network.agent_vpc.id
}

resource "google_compute_router_nat" "agent_nat_extra" {
  for_each                           = local.additional_regions
  name                               = "agent-nat-${each.key}"
  router                             = google_compute_router.nat_router_extra[each.key].name
  region                             = each.key
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Firewall Rule: Allow IAP SSH (Identity-Aware Proxy) for debugging agents securely
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "allow-iap-ssh"
  network = google_compute_network.agent_vpc.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # IAP IP range
  source_ranges = ["35.235.240.0/20"]
}

# ------------------------------------------------------------------------------
# Private Services Access for Google managed databases (Cloud SQL, AlloyDB)
#
# Both engines route through the same managed-services VPC peering. Originally
# defined in alloydb.tf; relocated here when AlloyDB was archived so cloud_sql.tf
# can still depend on these resources without resurrecting alloydb.tf.
#
# Resource addresses are unchanged from the alloydb.tf era — Terraform state
# lookup by `google_compute_global_address.private_ip_alloc` /
# `google_service_networking_connection.vpc_connection` keeps working without
# a `terraform state mv`.
# ------------------------------------------------------------------------------

# Reserve a /24 CIDR block for Google Managed Services
resource "google_compute_global_address" "private_ip_alloc" {
  name          = "alloydb-private-ip-alloc"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 24
  network       = google_compute_network.agent_vpc.id
}

# Create a private connection between the VPC and Google's service networking
resource "google_service_networking_connection" "vpc_connection" {
  network                 = google_compute_network.agent_vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_alloc.name]
}
