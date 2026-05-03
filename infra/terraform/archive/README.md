# Archived Terraform resources

Files in this directory have a `.disabled` suffix and are NOT loaded by terraform
(it only reads `*.tf` from the directory it's run in, not subdirectories).
They're kept here as restore references for resources we've stripped from the
active config.

## Why archive instead of delete?

Resources here were intentionally removed to control costs or simplify the active
deploy. The full original config is preserved (rather than relying on `git log`)
because:

- restoring requires not just the file but also reverting cleanups in
  `iam.tf`, `audit_logs.tf`, `monitoring.tf` etc. that referenced these
  resources
- the migration scripts (e.g. `infra/migrate-alloydb-to-cloudsql.sh`) work
  in tandem with these resources and document the data-movement runbook

## Restore procedures

### `alloydb.tf.disabled` — restore AlloyDB

When to consider:
- Sustained 1000+ concurrent users with heavy write contention
- Analytical queries needing AlloyDB's columnar engine
- Multi-region HA requirements that Cloud SQL's regional setup can't meet

Steps:
1. **Enable the API**: re-add `alloydb.googleapis.com` to `setup.sh`'s
   `gcloud services enable` list (it was removed when AlloyDB was archived).
2. **Copy resource file back**: `cp archive/alloydb.tf.disabled alloydb.tf`
   (rename drops the `.disabled` suffix so terraform loads it).
3. **VPC peering already lives in `vpc.tf`** — `google_compute_global_address.private_ip_alloc`
   and `google_service_networking_connection.vpc_connection`. DO NOT add them
   back to alloydb.tf or you'll get a double-declare error.
4. **Re-add the AlloyDB audit-log sink** in `audit_logs.tf`: mirror the existing
   `google_project_iam_audit_config "cloudsql"` block, but with
   `service = "alloydb.googleapis.com"`.
5. **Re-add CPU alert** in `monitoring.tf` if you want monitoring (the
   `alloydb_cpu` policy block was removed when AlloyDB was archived).
6. **Re-add migrator IAM binding** in `iam.tf` —
   `google_secret_manager_secret_iam_member.migrator_alloydb_password_access`,
   bound to `google_secret_manager_secret.db_password_secret.id` (which the
   restored `alloydb.tf` declares).
7. **Restore AlloyDB outputs** in `outputs.tf`. `terraform validate` rejects
   the references at parse time (NOT at apply time — `try()` doesn't help here),
   so this step is required to make `migrate-alloydb-to-cloudsql.sh` work:
   ```hcl
   output "alloydb_ip_address" {
     value       = google_alloydb_instance.primary.ip_address
     description = "Private IP of AlloyDB primary"
   }
   output "alloydb_password_secret_id" {
     value       = google_secret_manager_secret.db_password_secret.secret_id
     description = "Secret Manager ID of AlloyDB password"
   }
   ```
8. **`terraform apply`** provisions the cluster (~15 min).
9. **Run the migration**. `infra/migrate-alloydb-to-cloudsql.sh` is the
   one-shot template that ran the *original* AlloyDB→Cloud SQL cutover. To go
   in the reverse direction (Cloud SQL→AlloyDB) you need to MODIFY the script:
   - swap the source/target hosts (DB_IP and CLOUD_SQL_IP)
   - swap which secret the migrator reads from (alloydb password, not Cloud SQL password)
   - the pg_dump/pg_restore commands are symmetric, no schema changes needed
10. **Update `DATABASE_URL` secret** to point at the AlloyDB private IP.
11. **Redeploy Cloud Run** (`./setup.sh -f` or `gcloud builds submit`) so the
    orchestrator picks up the new `DATABASE_URL`.

Total effort: ~half a day.

### Restoring Redis (`redis.tf` was deleted, not archived)

`redis.tf` was deleted outright (not preserved here) — restore from git history:

```bash
git log --oneline -- infra/terraform/redis.tf  # find the SHA before deletion
git checkout <SHA>~1 -- infra/terraform/redis.tf
```

Then add `REDIS_URL=REDIS_URL:latest` back to `backend/cloudbuild.yaml`'s
`--set-secrets` argument list (it was removed in the same commit that deleted
redis.tf).

When to consider: only after the leader-election TODO in `lifecycle.py` lands
and Cloud Run can scale beyond `max_instance_count=1`. With a single instance,
Redis is doing nothing useful (`redis_client.py:1-7` documents the in-memory
fallback that takes over).
