"""Static least-privilege and fresh-project invariants for DB recovery IaC."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_backup_runner_can_consume_enabled_cloud_sql_api():
    source = (REPO_ROOT / "infra" / "terraform" / "db_backup.tf").read_text()
    role = source.split(
        'resource "google_project_iam_custom_role" "db_backup_exporter"',
        maxsplit=1,
    )[1].split("\n}", maxsplit=1)[0]

    assert '"serviceusage.services.use"' in role


def test_recovery_bucket_materializes_storage_service_agent_before_kms_grant():
    source = (REPO_ROOT / "infra" / "recovery" / "storage.tf").read_text()

    assert 'data "google_storage_project_service_account" "recovery"' in source
    assert (
        "data.google_storage_project_service_account.recovery[0].email_address"
        in source
    )
    assert "@gs-project-accounts.iam.gserviceaccount.com" not in source
