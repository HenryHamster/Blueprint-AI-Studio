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

# --- Multi-Region Subnets, Routers, and NAT for cross-region VM fallback ---

locals {
  additional_regions = {
    "us-east4" = "10.0.2.0/24"
    "us-east1" = "10.0.3.0/24"
    "us-west1" = "10.0.4.0/24"
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
