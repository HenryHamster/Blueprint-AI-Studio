# ------------------------------------------------------------------------------
# Private Services Access (Required for AlloyDB)
# ------------------------------------------------------------------------------

# Reserve a /24 CIDR block for Google Managed Services (like AlloyDB)
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

# ------------------------------------------------------------------------------
# AlloyDB Cluster & Instance
# ------------------------------------------------------------------------------

# Generate a random suffix for the cluster ID to avoid naming collisions
resource "random_id" "cluster_id_suffix" {
  byte_length = 4
}

# Generate a secure random password for the default 'postgres' user
resource "random_password" "db_password" {
  length           = 16
  special          = true
  override_special = "_%@"
}

resource "google_alloydb_cluster" "default" {
  cluster_id = "orchestrator-alloydb-${random_id.cluster_id_suffix.hex}"
  location   = var.region
  
  network_config {
    network = google_compute_network.agent_vpc.id
  }

  initial_user {
    user     = "postgres"
    password = random_password.db_password.result
  }
  
  # Ensure the private connection is established before creating the cluster
  depends_on = [google_service_networking_connection.vpc_connection]
}

resource "google_alloydb_instance" "primary" {
  cluster       = google_alloydb_cluster.default.name
  instance_id   = "orchestrator-alloydb-primary"
  instance_type = "PRIMARY"
  
  machine_config {
    cpu_count = 2 # Minimum vCPUs for AlloyDB
  }
}

# ------------------------------------------------------------------------------
# Database Credentials (Secret Manager)
# ------------------------------------------------------------------------------

# Store the database password in Secret Manager so Cloud Run can access it securely
resource "google_secret_manager_secret" "db_password_secret" {
  secret_id = "alloydb-postgres-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password_version" {
  secret      = google_secret_manager_secret.db_password_secret.id
  secret_data = random_password.db_password.result
}

# Grant the Orchestrator Service Account access to read the database password
resource "google_secret_manager_secret_iam_member" "orchestrator_db_password_access" {
  secret_id = google_secret_manager_secret.db_password_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
