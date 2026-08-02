# Independent database recovery archive

This is a separate Terraform root for the recovery project. It is disabled by
default and deliberately has no production-project default. It creates the
encrypted archive bucket and the one-way writer permission needed by the
production Cloud SQL service account.

## Required operators and inputs

Before enabling it, supply all of the following:

1. A separate recovery GCP project with billing enabled and a dedicated,
   versioned Terraform-state bucket. Its administrator credentials must never
   be installed on hamsterwheel.
2. An explicit recovery administrator supplied as `recovery_admin_member`.
   Personal deployments may use `user:owner@example.com`; multi-operator
   deployments should use a separately managed `group:...`. This principal,
   not hamsterwheel, gets archive-view and CMEK decrypt permission.
3. The production Cloud SQL *instance service account* in IAM member form,
   obtained from the production `cloud_sql_service_account_email` output.
4. A globally unique recovery bucket name, a location/retention policy, and a
   successful restore drill before setting `lock_retention_policy=true`.
5. The billing account ID. This root creates a recovery-project-only monthly
   budget (USD 20 by default) with alerts at 25%, 50%, and 100% to billing
   account recipients. It is an alert, not a hard spending cap.

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

First set `enable_restore_verifier_prerequisites=true` and supply the dedicated
network/subnetwork names. This creates an isolated VPC, Direct VPC egress
subnet, Private Services Access range/connection, and dedicated Artifact
Registry repository without creating or running a restore job. Build and push
the verifier image to the `restore_verifier_image_repository` output, record
its repository digest, and only then set `enable_restore_verifier=true` with:

- an immutable `restore_verifier_image` pinned by `@sha256`;
- the recovery VPC network and subnetwork;
- the exact expected Alembic revision.

For the first pre-cutover drill, also set the exact archive name, archive
generation, completion-manifest generation, SHA-256, and byte size produced by
the rehearsal. Leave `enable_restore_drill_schedule=false`; execute the job
manually and require its evidence to repeat those immutable values and prove
temporary-instance deletion. Enable the recurring drill schedule only after
that gate succeeds. Scheduled runs select and hash only the newest completion
manifest. A corrupt newest completed backup fails the drill instead of silently
certifying an older archive, and archive-retention growth does not make
verification work unbounded.

The prerequisite layer enables `compute.googleapis.com`,
`servicenetworking.googleapis.com`, `dns.googleapis.com`, and
`artifactregistry.googleapis.com` and manages the network path. Recovery jobs
send all traffic through that VPC without Cloud NAT; private DNS sends supported
Google APIs through `restricted.googleapis.com`, and targeted egress firewall
rules allow only metadata/DNS, the restricted API VIP, and Cloud SQL's private
proxy port. A network response policy sinkholes every non-Google API DNS name,
closing DNS as an alternate egress channel; longest-match rules retain only
`*.googleapis.com` through the restricted VIP and the exact
`metadata.google.internal` name through the metadata IP for service-identity
tokens. No wildcard under `google.internal` is allowed. The verifier image must come
from this recovery project's dedicated
repository and be deployed by immutable digest. Tags are immutable and no
automatic cleanup may remove a digest still referenced by the drill or its
janitor; verifier image storage is intentionally retained as a small recovery
dependency. Keep
`enable_restore_verifier=false` until Terraform has applied the prerequisite
layer and the verifier image exists by immutable digest.

Terraform then creates two recovery-project Cloud Run jobs. The janitor has a
separate identity that cannot read archives or create Cloud SQL
instances and remains enabled independently of the full drill. Once explicitly
invoked with a complete immutable archive pin, the drill creates a private
PostgreSQL 15 Cloud SQL instance, streams the verified archive through a
restricted PostgreSQL 15.18 client over the private Auth Proxy, probes it,
writes evidence, and deletes it. Recurring drills remain fail-closed until
future backups use a trusted custom-format provenance contract. A separate
30-minute janitor lists only recovery-project Cloud SQL
instances and deletes expired or ambiguous instances only when both their
`bp-restore-` name and `blueprint-purpose=restore-drill` label prove
ownership. Cleanup intent is recorded before every create request, so an
ambiguous create response still triggers immediate deletion and later janitor
reconciliation.

The janitor service account, job, and schedule are protected from ordinary
Terraform destruction. Retiring them is a deliberate break-glass change: first
prove that the recovery project has zero restore-drill instances, retain that
evidence, and then explicitly remove the lifecycle guards in a reviewed change.

The verifier requires `RECOVERY_PROJECT_NUMBER` and reloads the archive bucket
before any list or evidence write. It refuses to proceed unless the bucket's
authoritative project number matches. The verifier alone receives archive read
and key-decrypt access; ephemeral Cloud SQL identities receive no archive or
key access.

This root is intentionally not disposable after it contains recovery data:
the KMS key has Terraform `prevent_destroy`, the bucket has `force_destroy =
false`, retained objects cannot be removed before their retention deadline,
and a locked retention policy is irreversible. Disable the production export
job separately; do not destroy this root as a way to pause backups.

Cloud SQL SQL exports omit triggers and stored procedures. Confirm that this
application has no required database objects of those kinds in the first
independent restore drill; otherwise add a schema supplement before relying on
this archive tier.
