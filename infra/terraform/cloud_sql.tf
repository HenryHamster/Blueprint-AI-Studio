# ------------------------------------------------------------------------------
# Cloud SQL for PostgreSQL — the primary database.
#
# `var.use_cloud_sql` defaults true (variables.tf) since alloydb.tf was archived
# 2026-05-02. Setting it false leaves the deploy with NO database; only do this
# if you're in the middle of restoring AlloyDB (see archive/README.md).
#
# VPC peering (google_compute_global_address.private_ip_alloc +
# google_service_networking_connection.vpc_connection) lives in vpc.tf. Both
# managed-DB engines route through the same Google service-networking
# connection, so the resources are colocated in vpc.tf where they belong.
# ------------------------------------------------------------------------------

resource "random_id" "cloud_sql_id_suffix" {
  count       = var.use_cloud_sql ? 1 : 0
  byte_length = 4
}

resource "random_password" "cloud_sql_db_password" {
  count            = var.use_cloud_sql ? 1 : 0
  length           = 24
  special          = true
  override_special = "_-#"
  # Cloud SQL's password_validation_policy requires all 4 character classes.
  # Without these min_* enforcements, the random generator probabilistically
  # produces all 4 at length=24 but not deterministically — when it doesn't,
  # `google_sql_user.postgres` create fails with INVALID_PASSWORD.
  min_lower   = 2
  min_upper   = 2
  min_numeric = 2
  min_special = 2
}

resource "google_sql_database_instance" "default" {
  count               = var.use_cloud_sql ? 1 : 0
  name                = "blueprint-cloudsql-${random_id.cloud_sql_id_suffix[0].hex}"
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = true

  settings {
    # Preserve the proven production shape during the local-worker cutover.
    # Database right-sizing is a separate, metrics-gated change: applying a
    # speculative shared-core tier here previously saturated immediately.
    tier                        = "db-custom-4-15360"
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL" # single-zone, no HA. Flip to "REGIONAL" to add HA (~2x cost).
    disk_size                   = 20      # GB. autoresize will grow it; this is the floor.
    disk_type                   = "PD_SSD"
    disk_autoresize             = true
    disk_autoresize_limit       = 200  # cap autoresize at 200GB so a runaway query can't bankrupt us
    deletion_protection_enabled = true # server-side guard, defense-in-depth alongside terraform deletion_protection above

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00" # UTC
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      # The off-cloud worker reaches Cloud SQL through the authenticated proxy,
      # which still needs the instance's public endpoint. TLS and IAM constrain
      # access; disabling this endpoint would strand the local worker.
      ipv4_enabled                                  = true
      private_network                               = google_compute_network.agent_vpc.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY" # require TLS on every connection
    }

    # Match the audit-logging surface of the AlloyDB instance.
    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }
    database_flags {
      name  = "pgaudit.log"
      value = "ddl,role,write"
    }
    # Surface slow queries / lock contention in Cloud Logging.
    database_flags {
      name  = "log_min_duration_statement"
      value = "1000" # ms
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
    # Server-side cap on connection count. Pairs with the application-side pool reduction
    # in backend/app/database.py — defense in depth so a misconfigured pool can't exhaust the DB.
    database_flags {
      name  = "max_connections"
      value = "400"
    }
    # Enables IAM-based DB auth so future code can connect with the orchestrator SA token
    # instead of a long-lived password. The password path still works; this just unlocks
    # the alternate auth mode.
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    password_validation_policy {
      enable_password_policy      = true
      min_length                  = 16
      complexity                  = "COMPLEXITY_DEFAULT"
      reuse_interval              = 3
      disallow_username_substring = true
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = false
      record_client_address   = false
    }

    maintenance_window {
      day          = 7 # Sunday
      hour         = 4 # 04:00 UTC
      update_track = "stable"
    }
  }

  depends_on = [google_service_networking_connection.vpc_connection]
}

# Set the postgres user's password — Cloud SQL creates the role automatically; this binds
# the password we generated above so the migration script can connect.
resource "google_sql_user" "postgres" {
  count    = var.use_cloud_sql ? 1 : 0
  name     = "postgres"
  instance = google_sql_database_instance.default[0].name
  password = random_password.cloud_sql_db_password[0].result
}

# ------------------------------------------------------------------------------
# Database Credentials (Secret Manager)
# ------------------------------------------------------------------------------

resource "google_secret_manager_secret" "cloud_sql_db_password_secret" {
  count     = var.use_cloud_sql ? 1 : 0
  secret_id = "cloud-sql-postgres-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "cloud_sql_db_password_version" {
  count       = var.use_cloud_sql ? 1 : 0
  secret      = google_secret_manager_secret.cloud_sql_db_password_secret[0].id
  secret_data = random_password.cloud_sql_db_password[0].result
}

resource "google_secret_manager_secret_iam_member" "orchestrator_cloud_sql_password_access" {
  count     = var.use_cloud_sql ? 1 : 0
  secret_id = google_secret_manager_secret.cloud_sql_db_password_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
