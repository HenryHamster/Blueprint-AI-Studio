# Database operations reference

Operational posture for the Blueprint Cloud SQL Postgres database. This is the
single source of truth for recovery objectives, backup configuration, capacity
planning, and rotation cadence. Updated whenever the underlying Terraform or
runtime defaults change.

Pointers:
- Migration runbook (AlloyDB → Cloud SQL): `docs/db-migration-plan.md`
- Concurrency / locking contract: `docs/concurrency.md`
- Deferred items + revisit triggers: `docs/db-deferrals.md`
- Quarterly drill script: `infra/restore-drill.sh`
- Alert policies: `infra/terraform/monitoring.tf`
- Cloud SQL / AlloyDB Terraform: `infra/terraform/cloud_sql.tf`, `infra/terraform/alloydb.tf`

---

## 1. RTO / RPO targets

Recovery objectives the operator commits to. Quarterly restore drills exist to
prove these are real, not aspirational.

| Objective | Target | Mechanism |
|---|---|---|
| **RTO** — from the latest daily backup | ~30 min | `gcloud sql backups restore` provisions a new instance from the most recent daily backup, then `DATABASE_URL` secret is updated and Cloud Run picks it up on its next revision. |
| **RTO** — from PITR (point-in-time recovery) | ~2 min | Same restore mechanic, target a specific timestamp within the 7-day transaction-log window. Used for "rollback to before that bad migration five minutes ago." |
| **RPO** — within the day | 5 min | Cloud SQL writes WAL to durable storage continuously; worst-case loss is unflushed transactions at the moment of failure. |
| **RPO** — at the edge of retention | 7 days | `transaction_log_retention_days = 7` in `cloud_sql.tf`. Anything older than that window is unrecoverable. |

These targets reflect a single-zone (`ZONAL`) configuration. Flipping to
`REGIONAL` (see section 4) tightens RTO meaningfully because failover happens
automatically without operator intervention.

---

## 2. Backup configuration

### Cloud SQL (authoritative post-cutover)

Configured in `infra/terraform/cloud_sql.tf` under `backup_configuration`:

- **Cadence:** daily at `03:00 UTC`.
- **Retained backups:** 7 (rolling 7-day window of daily backups).
- **PITR enabled:** yes (`point_in_time_recovery_enabled = true`).
- **Transaction log retention:** 7 days (`transaction_log_retention_days = 7`).

### AlloyDB (pre-cutover, obsolete after migration)

`infra/terraform/alloydb.tf` does not configure custom backup settings; AlloyDB
applies Google's default automated backup policy. Once the AlloyDB → Cloud SQL
cutover (see `docs/db-migration-plan.md`) lands and AlloyDB is decommissioned,
this section becomes irrelevant.

---

## 3. Restore procedure

Restore drills are quarterly. The runnable script lives at
`infra/restore-drill.sh` and walks the operator through:

1. List recent backups for the live instance.
2. Pick the latest backup (or accept an operator-supplied backup ID).
3. Restore into a transient `blueprint-restore-test` instance.
4. Run `alembic check` against the restored database (must report no pending
   migrations).
5. Sample row counts on the tables that matter (`task`, `project`,
   `alloweduser`).
6. Destroy the test instance.
7. Append the result to `docs/db-restore-drill-log.md` (the script creates the
   log file on first drill).

Run it with:

```bash
bash infra/restore-drill.sh
```

The script is interactive (`confirm()` prompts before each destructive step)
and idempotent — safe to re-run after a partial failure.

---

## 4. HA + maintenance

### Availability mode

Cloud SQL is configured `availability_type = "ZONAL"` in `cloud_sql.tf` —
single-zone, no automatic failover. A zone outage requires manual restore from
backup (RTO ~30 min) rather than automatic regional failover.

**Trigger to flip to `REGIONAL`:** first paying user OR an externally
committed uptime SLA. Cost impact: roughly 2x the base instance cost (Cloud SQL
charges for both the primary and the standby).

To flip, change `availability_type = "REGIONAL"` in `cloud_sql.tf` and
`terraform apply`. Cloud SQL handles the conversion online; expect a brief
(<30s) blip while the standby attaches.

### Maintenance window

Cloud SQL `maintenance_window` is configured in `cloud_sql.tf` for **Sunday
04:00 UTC** with `update_track = "stable"`. This puts maintenance during the
quietest period (Sunday early morning UTC = Saturday evening US Pacific).

AlloyDB has no equivalent `maintenance_update_policy` configured — Google
manages restart timing. Once AlloyDB is decommissioned post-cutover, this
asymmetry becomes irrelevant.

---

## 5. Capacity / disk runway

Configured in `cloud_sql.tf`:

- `disk_size = 20` (GB) — the floor; Cloud SQL never shrinks below this.
- `disk_autoresize = true` — Cloud SQL grows the disk automatically as data
  fills it.
- `disk_autoresize_limit = 200` (GB) — hard cap so a runaway query can't
  bankrupt us.

### Alerting

`monitoring.tf` defines `cloudsql_disk` to fire when disk usage exceeds **75%
of the autoresize cap = 150 GB of 200 GB**, sustained 5 minutes. The
runbook content of the alert points back to this document.

### Projected runway

At a projected ~50 GB/month growth at the 10k-user scale tier (per the
scalability audit), the 200 GB autoresize cap represents roughly **4 months of
headroom** at full scale. At the current solo-dev workload, growth is closer to
~1 GB/month and runway is effectively unbounded for the purposes of this
section.

**Re-evaluation point:** when the live instance crosses 100 GB
(~50% of cap), revisit whether to bump `disk_autoresize_limit` to 500 GB or
trigger a tier upgrade with more attached storage. Plan the revisit at the same
time the first paying user arrives — both signals tend to coincide.

---

## 6. Connection pool

The pool is sized end-to-end so a misconfigured Cloud Run scale-up cannot
exhaust Cloud SQL's connection cap.

### Application side (`backend/app/database.py`)

```
pool_size = 10
max_overflow = 15
```

Per Cloud Run instance, this gives a ceiling of `10 + 15 = 25` connections.
With `cloud_run.tf` capping `max_instance_count = 5`, the worst-case fleet-wide
connection count is `5 × 25 = 125`.

### Server side (`infra/terraform/cloud_sql.tf`)

```
database_flag max_connections = 200
```

Server cap is 200 connections. Total fleet ceiling (125) leaves **75
connections of headroom** for `psql`, pgAdmin, alembic migrations, and the
quarterly restore-drill instance bridge.

### Alerting

`monitoring.tf` defines `cloudsql_connections` to fire when active backends
exceed **80% of the cap = 160**, sustained 5 minutes. That threshold is below
both the application ceiling (125) and the server cap (200), so the alert
fires before either side starts refusing connections.

### Tier-upgrade trigger

Bump from `db-g1-small` (1 shared vCPU, 1.7 GB) to
`db-custom-1-3840` (1 dedicated vCPU, 3.75 GB, ~$60-70/mo) when **either**:

- CPU sustained >80% for 5+ minutes (the `cloudsql_cpu` alert fires), or
- Connection waits become visible in `pg_stat_activity` (queries blocking on
  `ClientRead` or `ClientWrite` because the pool is saturated).

The upgrade is a Terraform diff (`tier = "db-custom-1-3840"`) followed by
`terraform apply`. Expect 2-5 minutes downtime on `ZONAL`; <30 seconds on
`REGIONAL` once HA is enabled.

---

## 7. Password rotation

### Current posture

`random_password.cloud_sql_db_password` (in `cloud_sql.tf`) is generated **once**
by Terraform on first apply and persists. The value is stored in Secret Manager
as `cloud-sql-postgres-password` and read by Cloud Run via `--set-secrets
DATABASE_URL=DATABASE_URL:latest`. There is no scheduled rotation.

### Rotation procedure

Run the rotation script — it does the full chain (taint → apply → re-build
DATABASE_URL → secret-add → Cloud Run revision bump → readiness poll) in one
shot. Bare `terraform taint` alone leaves Cloud Run reading a STALE
`DATABASE_URL` constructed against the prior password; the script closes that
gap (Wave F4 H6).

```bash
bash infra/rotate-db-password.sh
```

The script is interactive (`confirm()` prompts before each destructive step)
and idempotent — safe to re-run after a partial failure.

If you must run the steps manually (e.g. the script is unavailable in your
environment), the equivalent sequence is:

```bash
cd infra/terraform
terraform taint random_password.cloud_sql_db_password
terraform apply -var="use_cloud_sql=true"
# Then read the new password, URL-encode, build the URL, add a new
# DATABASE_URL secret version, and force a new Cloud Run revision —
# see infra/rotate-db-password.sh for the exact gcloud commands.
```

### Rotation cadence

**Trigger:** quarterly cadence (calendar reminder) OR a security event (suspected
compromise, departing operator with previous access). The
`password_validation_policy.complexity = "COMPLEXITY_DEFAULT"` flag in
`cloud_sql.tf` is cosmetic for a Terraform-generated 24-character random
password — it would only become load-bearing if additional human-managed roles
were added.

---

## 8. Monitoring + alerts

The five Cloud Monitoring alert policies live in `infra/terraform/monitoring.tf`
and page on the email channel defined by `var.alert_email`:

| Policy | Threshold | Source |
|---|---|---|
| `cloudsql_cpu` | CPU > 80% sustained 5 min | `cloudsql.googleapis.com/database/cpu/utilization` |
| `cloudsql_disk` | Disk usage > 150 GB (75% of 200 GB cap) sustained 5 min | `cloudsql.googleapis.com/database/disk/bytes_used` |
| `cloudsql_connections` | Backends > 160 (80% of 200 cap) sustained 5 min | `cloudsql.googleapis.com/database/postgresql/num_backends` |
| `cloudsql_up` | Instance unavailable for 5 min | `cloudsql.googleapis.com/database/up` |
| `alloydb_cpu` | CPU > 80% sustained 5 min (pre-cutover dual-coverage) | `alloydb.googleapis.com/instance/cpu/utilization` |

Each policy's `documentation.content` field points back to this document for
the runbook content. Empty `var.alert_email` disables alerting entirely (the
notification channel and all policies become `count = 0`).

The `alloydb_cpu` policy is gated `count = !var.use_cloud_sql` so it deactivates
automatically post-cutover.
