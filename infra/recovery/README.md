# Independent database recovery archive

This is a separate Terraform root for the recovery project. It is disabled by
default and deliberately has no production-project default. It creates the
encrypted archive bucket and the one-way writer permission needed by the
production Cloud SQL service account.

## Required operators and inputs

Before enabling it, supply all of the following:

1. A separate recovery GCP project with billing enabled and a dedicated,
   versioned Terraform-state bucket. Recovery-project administrators must be
   independent from the production/hamsterwheel operator.
2. A Google Group with at least two recovery custodians, supplied as
   `recovery_admin_member=group:...`. This group, not hamsterwheel, gets
   archive-view and CMEK decrypt permission.
3. The production Cloud SQL *instance service account* in IAM member form,
   obtained from the production `cloud_sql_service_account_email` output.
4. A globally unique recovery bucket name, a location/retention policy, and a
   successful restore drill before setting `lock_retention_policy=true`.

Initialize this root with recovery-project state, never the production state:

```sh
terraform init -backend-config="bucket=<recovery-project>-tf-state"
terraform apply -var-file=recovery.tfvars
```

After this root is applied, enable the production `enable_offhost_db_backup`
variables. The production job asks Cloud SQL to export server-side to
`gs://<recovery-bucket>` twice daily by default; it cannot read the archive or
decrypt its CMEK.

After local PostgreSQL cutover, enable
`enable_local_postgres_backup_writer=true`. Install that identity's
writer-only credential on the database host and schedule
`scripts/local_postgres_backup_service.py` at least twice daily. Each run
streams a plain SQL dump through gzip into a unique
`local-postgres-exports/*.sql.gz` object. The archive is not eligible for
recovery until a matching `.manifest.json` success marker has been created
after `pg_dump`, gzip, and the archive upload all exit successfully. The
manifest records the immutable archive name, byte count, SHA-256 digest,
format, database, and PostgreSQL major version. Failed uploads and orphan
archives therefore remain safely ignored despite immutable retention.

The local writer remains create-only. It cannot list, read, overwrite, delete,
or decrypt any recovery object. Use the `local_postgres_backup_writer_email`
output to validate its credential identity; do not grant its key any project
role.

## Scheduled restore verification and janitor

Set `enable_restore_verifier=true` only after supplying:

- an immutable `restore_verifier_image` pinned by `@sha256`;
- the recovery VPC network and subnetwork;
- the exact expected Alembic revision.

Terraform then creates two recovery-project Cloud Run jobs. A weekly drill
integrity-checks the newest manifest/archive pair, creates a private
PostgreSQL 15 Cloud SQL instance, imports and probes it, writes evidence, and
deletes it. A separate 30-minute janitor lists only recovery-project Cloud SQL
instances and deletes expired or ambiguous instances only when both their
`bp-restore-` name and `blueprint-purpose=restore-drill` label prove
ownership. Cleanup intent is recorded before every create request, so an
ambiguous create response still triggers immediate deletion and later janitor
reconciliation.

The verifier requires `RECOVERY_PROJECT_NUMBER` and reloads the archive bucket
before any list or evidence write. It refuses to proceed unless the bucket's
authoritative project number matches. Cloud SQL's recovery-project service
identity receives object-view and key-decrypt access only to this archive
bucket/key; it receives no production-project or archive-write permission.

This root is intentionally not disposable after it contains recovery data:
the KMS key has Terraform `prevent_destroy`, the bucket has `force_destroy =
false`, retained objects cannot be removed before their retention deadline,
and a locked retention policy is irreversible. Disable the production export
job separately; do not destroy this root as a way to pause backups.

Cloud SQL SQL exports omit triggers and stored procedures. Confirm that this
application has no required database objects of those kinds in the first
independent restore drill; otherwise add a schema supplement before relying on
this archive tier.
