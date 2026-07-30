# Independent Cloud SQL recovery archive (Stage 2A)

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

This root is intentionally not disposable after it contains recovery data:
the KMS key has Terraform `prevent_destroy`, the bucket has `force_destroy =
false`, retained objects cannot be removed before their retention deadline,
and a locked retention policy is irreversible. Disable the production export
job separately; do not destroy this root as a way to pause backups.

Cloud SQL SQL exports omit triggers and stored procedures. Confirm that this
application has no required database objects of those kinds in the first
independent restore drill; otherwise add a schema supplement before relying on
this archive tier.
