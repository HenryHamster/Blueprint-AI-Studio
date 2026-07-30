# Recovery restore verifier

This image performs a destructive restore drill only inside the dedicated
recovery project. It selects the newest
`local-postgres-exports/*.sql.gz.manifest.json` success marker, requires an
exact matching archive, streams that archive through SHA-256 verification,
and ignores corrupt or orphaned objects. It then creates a uniquely named
PostgreSQL 15 Cloud SQL instance with private IP only, imports the plain SQL
gzip archive, connects through the bundled digest-verified Cloud SQL Auth
Proxy, runs non-sensitive schema and aggregate checks, records evidence in
Cloud Logging and the recovery bucket, and deletes the instance.

Every drill instance is labelled `blueprint-purpose=restore-drill` and receives
an `expires-epoch` label before creation. Cleanup intent is set before the
create request. The Terraform root deploys a separate 30-minute janitor Job
that deletes expired or ambiguous instances only when the owned name prefix
and purpose label both match. Normal completion, including failed
verification, waits for deletion.

Required environment:

- `RECOVERY_PROJECT_ID`
- `RECOVERY_PROJECT_NUMBER` (the Terraform output, checked against bucket metadata)
- `SOURCE_PROJECT_ID` (must differ from the recovery project)
- `RECOVERY_BUCKET`
- `RECOVERY_REGION`
- `RECOVERY_PRIVATE_NETWORK`, in the recovery project
- `EXPECTED_ALEMBIC_REVISION`

Optional bounded settings:

- `MAX_RPO_HOURS` (default 24, maximum 168)
- `DRILL_INSTANCE_TTL_MINUTES` (default 180, maximum 1440)
- `DRILL_OPERATION_TIMEOUT_SECONDS` (default 3600, maximum 7200)
- `DRILL_PROXY_START_TIMEOUT_SECONDS` (default 60, maximum 120)
- `RECOVERY_ARCHIVE_PREFIX` and `RECOVERY_EVIDENCE_PREFIX`

`--janitor-only` runs reconciliation without creating a drill instance. The
Terraform-scheduled janitor uses this mode; normal invocations reconcile first
and then perform the full drill.

The runtime identity must be recovery-project-only. It needs narrowly scoped
Cloud SQL lifecycle/import/connect permissions, archive read and CMEK decrypt,
evidence-object creation, and Cloud Logging write. It must not receive any IAM
permission in the production project.

The Job must have direct access to the recovery VPC used for Cloud SQL private
services access. No public IPv4 address or authorized network is created. The
database password is generated in memory, passed only through HTTPS to the
Admin API and the in-process database driver, cleared before evidence writing,
and never placed in a process command or log.
