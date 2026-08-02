# Recovery restore verifier

This image performs a destructive restore drill only inside the dedicated
recovery project. It validates a
`local-postgres-exports/*.sql.gz.manifest.json` success marker against a
complete operator-supplied immutable pin, streams that exact generation through
second-pass SHA-256 and gzip validation, and ignores corrupt or orphaned
objects. It then creates a uniquely named PostgreSQL 15 Cloud SQL instance with
private IP only, connects through the bundled digest-verified Cloud SQL Auth
Proxy, restores through a pinned PostgreSQL 15.18 `psql` client in restricted
mode and a single transaction, runs non-sensitive schema and aggregate checks,
records evidence in
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

For a one-shot pre-cutover drill, all five of these must be supplied together:

- `EXPECTED_ARCHIVE_NAME`
- `EXPECTED_ARCHIVE_GENERATION`
- `EXPECTED_MANIFEST_GENERATION`
- `EXPECTED_ARCHIVE_SHA256`
- `EXPECTED_ARCHIVE_SIZE_BYTES`

The verifier never substitutes a newer archive. It reopens only the specified
GCS generation, revalidates compressed size and SHA-256 before allowing the
single transaction to commit, bounds decompression, and requires the matching
PostgreSQL dump restrict envelope. Recurring unpinned drills are deliberately
disabled until future backups use custom-format dumps with an independently
trusted provenance contract.

Optional bounded settings:

- `MAX_RPO_HOURS` (default 24, maximum 168)
- `DRILL_INSTANCE_TTL_MINUTES` (default 180, maximum 1440)
- `DRILL_OPERATION_TIMEOUT_SECONDS` (default 3600, maximum 7200)
- `DRILL_PROXY_START_TIMEOUT_SECONDS` (default 60, maximum 120)
- `DRILL_TASK_WORK_TIMEOUT_SECONDS` (default 6000, maximum 6600; combined
  with cleanup limits so at least 300 seconds remain for evidence emission)
- `DRILL_CLEANUP_TIMEOUT_SECONDS` (default and maximum 600)
- `RECOVERY_ARCHIVE_PREFIX` and `RECOVERY_EVIDENCE_PREFIX`

`--janitor-only` runs reconciliation without creating a drill instance. The
Terraform-scheduled janitor uses this mode; normal invocations reconcile first
and then perform the full drill.

The runtime identity must be recovery-project-only. It needs narrowly scoped
Cloud SQL lifecycle/connect permissions, archive read and CMEK decrypt,
evidence-object creation, and Cloud Logging write. It must not receive any IAM
permission in the production project.

The Job must have direct access to the recovery VPC used for Cloud SQL private
services access. No public IPv4 address or authorized network is created. The
Terraform deployment routes all container egress through that VPC without NAT
and allows only metadata/DNS, restricted Google APIs, and the private Cloud SQL
proxy port. The image is accepted only from the dedicated recovery-project
repository by digest; its base image and Python dependency graph are also
content-pinned. The
database password is generated in memory, passed through HTTPS to the Admin API,
and exposed to `psql` only through a temporary owner-only passfile that is
removed before evidence writing. It is never placed in a process command,
environment variable, or log.

After intentionally changing `requirements.txt`, regenerate
`requirements.lock` for Python 3.12 Linux amd64 with `uv pip compile
requirements.txt --generate-hashes --python-version 3.12 --python-platform
x86_64-manylinux_2_28 --no-header --output-file requirements.lock`, then rebuild
and deploy the resulting image only by repository digest.
