# AlloyDB → Cloud SQL Postgres migration

## Why

AlloyDB is currently costing ~$15/day (~$450/mo) for a workload that has no external users
and uses none of AlloyDB's distinguishing features (columnar engine, read pool nodes,
HA, multi-region). Cloud SQL Postgres on `db-g1-small` covers the same SQL surface for
~$25-30/mo. Same dialect, same drivers, same Alembic migrations — the application code
doesn't change.

Memory pointer: `project_not_public.md` ("no external users yet") and the design doc
in `feedback_guardrails_not_straitjackets.md` (oversize loose bounds) — both support
swapping AlloyDB out until scale actually justifies it.

## Cost trajectory after migration

| Stage | Tier | $/mo | Trigger |
|---|---|---|---|
| Solo dev (now) | `db-g1-small` (1 shared vCPU / 1.7GB) | ~$25 | — |
| ~100 active users | `db-custom-1-3840` (1 dedicated vCPU / 3.75GB) | ~$60-70 | CPU steal visible in Cloud SQL Insights |
| ~1k active users | `db-custom-2-7680` + HA (REGIONAL) | ~$290-310 | First user-visible slow-query incident |
| ~10k active users | `db-custom-4-15360` + HA + 1 read replica | ~$700-850 | Dashboard read traffic dominates |
| Beyond | Migrate back to AlloyDB | (then-current) | Sustained >70% utilization on largest tier, OR multi-region needed |

Each step is a console-tier change or a single Terraform diff. No application code
changes at any step until the read-replica step, which needs 1 day —
`database.py` exposes a single engine consumed by 254 sites; mixed read+write
sessions in the orchestrator main loop and 54 `with_for_update` sites mean
adding read/write splitting requires marking endpoints read-safe and ensuring
no `FOR UPDATE` lands on a replica. Recommend introducing an `AsyncSessionRO`
factory now (even unused) so the seam exists.

## Surface of the change

The whole cutover hinges on **one Secret Manager secret**: `DATABASE_URL`. Cloud Run
pulls it via `--set-secrets DATABASE_URL=DATABASE_URL:latest` (`backend/cloudbuild.yaml:38`),
so swapping the secret value swaps the database. No image rebuild, no env-var diff,
no app code change.

```
[ Cloud Run orchestrator ]
        │
        │ --set-secrets DATABASE_URL=DATABASE_URL:latest
        ▼
[ Secret Manager: DATABASE_URL ]
        │
        │ (postgresql+asyncpg://...@<IP>:5432/postgres)
        ▼
[ AlloyDB ]   ← currently
[ Cloud SQL ] ← after cutover
```

Both DBs sit on the same `agent-sandbox-vpc`, both use private IPs only, both reuse the
existing `google_service_networking_connection.vpc_connection`. Cloud Run reaches them
the same way (VPC egress = all-traffic).

## What's already prepped (committed in this branch)

| File | Purpose |
|---|---|
| `infra/terraform/cloud_sql.tf` | New: Cloud SQL instance, password secret, IAM grant. Gated by `var.use_cloud_sql`. |
| `infra/terraform/variables.tf` | New `use_cloud_sql` flag (default false). |
| `infra/terraform/outputs.tf` | New outputs for Cloud SQL IP, password secret, instance name. |
| `infra/migrate-alloydb-to-cloudsql.sh` | Interactive runbook that does dump → restore → secret swap → redeploy. |

## Two cutover paths

### Path A — Migrate (preserves state)

Bring AlloyDB back briefly (~1-2 hours of billing, ~$1-2), dump it, restore into Cloud SQL,
swap the secret, tear down. Preserves: task history, user accounts, projects, pool
accounts (Max plan tokens), GitHub OAuth state. Sequence below.

### Path B — Fresh start (faster, loses state)

Skip the dump entirely. Don't reactivate AlloyDB. Spin up empty Cloud SQL, let
Alembic create the schema, re-paste pool tokens via the admin UI, re-do GitHub OAuth.
~30 min total wall time vs Path A's ~24-48h safety window. Loses: all task history,
project configs, user accounts (you'd be the only user anyway).

```bash
cd infra/terraform
terraform apply -var="use_cloud_sql=true" -target=google_sql_database_instance.default \
  -target=google_sql_user.postgres -target=google_sql_database.app \
  -target=google_secret_manager_secret.cloud_sql_db_password_secret \
  -target=google_secret_manager_secret_version.cloud_sql_db_password_version \
  -target=google_secret_manager_secret_iam_member.orchestrator_cloud_sql_password_access

# Construct new DATABASE_URL
CLOUDSQL_IP=$(terraform output -raw cloud_sql_ip_address)
CLOUDSQL_PASSWORD=$(gcloud secrets versions access latest --secret=cloud-sql-postgres-password)
NEW_URL="postgresql+asyncpg://postgres:${CLOUDSQL_PASSWORD}@${CLOUDSQL_IP}:5432/postgres"
printf '%s' "$NEW_URL" | gcloud secrets versions add DATABASE_URL --data-file=-

# Run alembic to create schema in fresh DB
gcloud run jobs execute blueprint-migration-job --region=us-central1  # if a job exists; otherwise:
# Or run migrations from a temporary VM with bash run_migrations.sh

# Force Cloud Run to pick up the new secret
gcloud run services update blueprint-orchestrator --region=us-central1 \
  --update-env-vars="MIGRATION_REDEPLOY=$(date -u +%Y%m%dT%H%M%SZ)"

# Sign back into admin UI, paste pool tokens, re-auth GitHub.
# Then destroy AlloyDB:
rm infra/terraform/alloydb.tf
# First disable deletion_protection if it's set on AlloyDB resources, then:
terraform apply
```

Recommend Path B for this situation specifically — DB is already down, no live users, the
state isn't load-bearing. If you've got pool tokens or project configs you'd hate to recreate,
take Path A.

## Path A: Cutover sequence (when billing is restored)

```bash
# 1. Bring up Cloud SQL alongside AlloyDB
cd infra/terraform
terraform apply -var="use_cloud_sql=true" -var="enable_cicd=true"
# Cloud SQL provisioning takes ~10 min the first time.

# 2. Run the migration runbook
cd ..
bash migrate-alloydb-to-cloudsql.sh
# Walks through: drain → provision migrator VM → pg_dump → pg_restore → verify
# row counts → swap DATABASE_URL secret → redeploy Cloud Run → tear down migrator.

# 3. Smoke-test
curl https://<orchestrator-url>/healthz
# Open admin UI, verify task list loads.

# 4. Wait 24-48h with both DBs running. AlloyDB billing continues during this
#    window — that's the cost of safe rollback.

# 5. Tear down AlloyDB
rm infra/terraform/alloydb.tf
# Also delete the alloydb_ip_address and alloydb_password_secret_id outputs from
# infra/terraform/outputs.tf — terraform plan fails to resolve the now-missing
# google_alloydb_instance.primary reference otherwise.
cd infra/terraform
terraform apply
# This destroys: alloydb cluster, alloydb instance, alloydb password secret.
# Cost-bleed stops here.
```

Total active hands-on time: ~30-60 min. Wall-clock including the safety window: 24-48h.

## Rollback

If the new DB shows errors after cutover, rollback is fast (DATABASE_URL secret has
versioning — the AlloyDB version is one `gcloud secrets versions add` away):

```bash
# Find the AlloyDB version (the one before today's swap)
gcloud secrets versions list DATABASE_URL --limit=5

# Re-promote it to latest
gcloud secrets versions access <PRIOR_VERSION> --secret=DATABASE_URL > /tmp/prev.url
gcloud secrets versions add DATABASE_URL --data-file=/tmp/prev.url

# Force Cloud Run to pull the new "latest"
gcloud run services update blueprint-orchestrator --region=us-central1 \
  --update-env-vars="ROLLBACK_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)"
```

Recovery time: ~2 min (the time it takes Cloud Run to spin up a new revision).

## Operational continuity

Recovery objectives and the procedure that proves we can hit them. Owned by Phase A4
of the database remediation plan; revisit when the data-loss tolerance changes (e.g.
first paying user → tighten RPO; compliance requirement → add CMEK + tighten RTO).

### Recovery Time Objective (RTO)

- **From latest daily backup:** ~30 min. `gcloud sql backups restore` provisions a new
  Cloud SQL instance from the most recent backup, then DATABASE_URL secret is updated
  and Cloud Run picks up the new value on its next revision.
- **From PITR (point-in-time recovery):** ~2 min within the 7-day transaction-log
  retention window. Same restore mechanic, target a specific timestamp.

### Recovery Point Objective (RPO)

- **Within the day:** 5 min. Cloud SQL writes WAL to durable storage continuously;
  the worst-case loss is the unflushed transactions at the moment of failure.
- **At the edge of the retention window:** 7 days
  (`backup_configuration.transaction_log_retention_days = 7` in `cloud_sql.tf`).
  Anything older than that is unrecoverable and lost.

### Restore-drill cadence

Quarterly. The drill restores a backup into a side-by-side instance, verifies the
schema migrates cleanly and that key tables are populated, then tears the test
instance down. The procedure:

1. List recent backups:
   ```bash
   gcloud sql backups list --instance=<cloudsql-instance>
   ```
2. Restore the most recent backup into a fresh test instance:
   ```bash
   gcloud sql backups restore <backup-id> \
     --restore-instance=blueprint-restore-test
   ```
3. Connect to `blueprint-restore-test`, run `alembic check` (must report no pending
   migrations), and sanity-count rows on the tables that matter:
   ```sql
   SELECT count(*) FROM task;
   SELECT count(*) FROM project;
   SELECT count(*) FROM alloweduser;
   ```
   Compare against the live instance's counts within ±5% of the backup window.
4. Destroy the test instance:
   ```bash
   gcloud sql instances delete blueprint-restore-test
   ```
5. Document the result in `docs/db-restore-drill-log.md` (create on first drill if
   missing). Capture: drill date, backup id used, alembic check outcome, row counts,
   any anomalies, total wall time. The log is the audit trail proving RTO/RPO are
   real, not aspirational.

### Alerting

Cloud Monitoring alert policies live in `infra/terraform/monitoring.tf` and page on:

- Cloud SQL CPU > 80% sustained 5 min
- Cloud SQL disk usage > 75% of `disk_autoresize_limit` (= 150 GB of 200 GB)
- Cloud SQL connections > 80% of `max_connections` (= 160 of 200)
- Cloud SQL instance unavailable for 5 min (proxy for backup-window outages)
- AlloyDB CPU > 80% (dual-coverage during the migration window)

Notification target is the email in `var.alert_email`. Empty string disables
alerting (no channel created, no policies created) — that's the default for
clean checkouts; production runs must set this variable in `terraform.tfvars`.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| In-flight writes during dump → restore inconsistency | Run during a quiet window. Solo dev → trivially achievable. Optionally scale Cloud Run to 0 during dump (~5 min). |
| Cloud SQL Postgres dialect differences vs AlloyDB | Both are vanilla Postgres 15. AlloyDB-specific features (read pool, columnar) aren't used by Blueprint. Audited models.py — no AlloyDB-only syntax. |
| `pgaudit` extension unavailable on Cloud SQL | Cloud SQL supports `cloudsql.enable_pgaudit` flag (set in cloud_sql.tf). Same audit surface. |
| `password.enforce_complexity` flag missing | Cloud SQL has its own complexity-check flag; existing password is already 24 chars + special. Not load-bearing for this migration. |
| Connection pooling differs (AlloyDB uses connection-multiplexer behind scenes) | See "Performance expectations" — `database.py` reduced to `pool_size=10, max_overflow=15` (was 40/60); `cloud_sql.tf` sets `max_connections=200` flag. Monitor `pg_stat_activity` post-cutover. |
| Migrator VM exposes data via dump file on disk | VM is private-IP only (no external address), torn down at end of script. Disk is ephemeral persistent disk in the same VPC. Dump file lives there <5 min. |
| Cloud SQL deletion_protection blocks accidental destroy | Enabled. To intentionally destroy, must set deletion_protection=false in TF first, apply, then destroy. |
| Backups stop on AlloyDB once removed | Cloud SQL has its own daily backups + 7d PITR. After cutover, the AlloyDB backup stream is irrelevant. |

## Performance expectations

`db-g1-small` is shared-core with ~25 default `max_connections` (formula: 25 +
memory_GB × 50, floored on shared-core). Blueprint runs up to 5 Cloud Run instances;
each opens up to `pool_size + max_overflow` connections. We address this two ways
simultaneously:
- App side: `database.py` uses `pool_size=10, max_overflow=15` → max 125 across 5 instances.
- Server side: `cloud_sql.tf` sets `max_connections=200` via database_flags, leaving 75-conn headroom for `psql` / pgAdmin / migrations.

When tier upgrades to `db-custom-1-3840` (1 dedicated vCPU, 3.75GB), default rises
to ~50; the explicit `max_connections=200` flag stays effective and gives further
headroom.

For Blueprint's current workload (orchestrator polling, agent heartbeats every 30s,
admin UI dashboard polling), this is plenty. Reference points:

- Total DB rows: probably <1M across all tables (verify with `SELECT pg_database_size('postgres')`)
- Peak QPS: ~20-50 from orchestrator + agent heartbeats
- Connection count: usually 5-15, capped at 25 per Cloud Run instance by `pool_size + max_overflow`

If post-cutover you see CPU >70% sustained or query times spike: bump to
`db-custom-1-3840` (~$60-70/mo). Console action; 2-5 min downtime on ZONAL;
<30s on REGIONAL once HA is on. No app change.

## Decision log

- **Why not Neon / Supabase / CockroachDB?** Cross-cloud latency (GCP↔AWS, ~30-50ms per
  query) compounds at scale; Blueprint's workload is connection-bursty. Staying GCP-internal
  preserves <1ms intra-VPC latency. The Cloud SQL → AlloyDB upgrade path is a
  DMS-supported homogeneous migration with logical replication; ~30 min downtime
  cutover, requires `pgaudit` and other extension parity at the destination. Not a
  click-button operation, but the third-party alternatives don't offer even that.
- **Why not self-host on a VM?** Cheapest now (~$10/mo) but every scale step is manual:
  resize VM, configure replication, set up failover, restore from backups. Cloud SQL
  amortizes that operational debt for ~$15/mo extra.
- **Why ZONAL not REGIONAL initially?** REGIONAL doubles cost. No external users → no
  uptime SLA pressure. Flip to REGIONAL when first paying customer signs.
- **Why deletion_protection=true?** Default-on so no `terraform destroy` accident vaporizes
  the production DB. Removing AlloyDB explicitly will require setting
  `deletion_protection = false` and re-applying first.
