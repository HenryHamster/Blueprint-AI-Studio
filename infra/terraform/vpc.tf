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
