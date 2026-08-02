"""Unit tests for the recovery-project restore verifier."""

from __future__ import annotations

import json
import hashlib
import io
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import verify_restore as verifier

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def config(**overrides):
    values = {
        "RECOVERY_PROJECT_ID": "blueprint-recovery",
        "RECOVERY_PROJECT_NUMBER": "123456789012",
        "SOURCE_PROJECT_ID": "blueprint-production",
        "RECOVERY_BUCKET": "blueprint-recovery-archives",
        "RECOVERY_REGION": "us-central1",
        "RECOVERY_PRIVATE_NETWORK": (
            "projects/blueprint-recovery/global/networks/recovery"
        ),
        "RECOVERY_PRIVATE_SERVICE_RANGE": "blueprint-restore-private-services",
        "EXPECTED_ALEMBIC_REVISION": "0711",
        "CLOUD_SQL_PROXY_BINARY": str(Path(__file__).resolve()),
    }
    values.update(overrides)
    return verifier.Config.from_env(values)


def archive():
    return verifier.Archive(
        bucket="blueprint-recovery-archives",
        name="local-postgres-exports/20260730T110000Z-a1b2.sql.gz",
        generation="123",
        created_at=NOW - timedelta(hours=1),
        size=4096,
        sha256="a" * 64,
        manifest_generation="124",
    )


class FakeStorage:
    def __init__(self, selected=None, *, fail_writes=False):
        self.selected = selected or archive()
        self.fail_writes = fail_writes
        self.writes = []
        self.import_generation_checks = []

    def latest_archive(self, now):
        return self.selected

    def write_evidence(self, name, payload):
        if self.fail_writes:
            raise RuntimeError("hidden storage error")
        self.writes.append((name, json.loads(json.dumps(payload))))

    def assert_import_generation_current(self, selected):
        self.import_generation_checks.append(selected.archive_id)


class FakeLogger:
    def __init__(self, *, fail_writes=False):
        self.fail_writes = fail_writes
        self.writes = []

    def write(self, payload):
        if self.fail_writes:
            raise RuntimeError("hidden logging error")
        self.writes.append(json.loads(json.dumps(payload)))


class FakeSql:
    def __init__(self, *, fail_stage=None, cleanup_fails=False):
        self.fail_stage = fail_stage
        self.cleanup_fails = cleanup_fails
        self.calls = []
        self.instances = []

    def list_instances(self):
        self.calls.append(("list_instances",))
        return self.instances

    def create_instance(self, name, body):
        self.calls.append(("create_instance", name, body))
        return "op-create"

    def get_instance(self, name):
        self.calls.append(("get_instance", name))
        return {
            "project": "blueprint-recovery",
            "databaseVersion": "POSTGRES_15",
            "sqlNetworkArchitecture": "NEW_NETWORK_ARCHITECTURE",
            "connectionName": f"blueprint-recovery:us-central1:{name}",
            "settings": {
                "ipConfiguration": {
                    "ipv4Enabled": False,
                    "privateNetwork": (
                        "https://compute.googleapis.com/compute/v1/"
                        "projects/blueprint-recovery/global/networks/recovery"
                    ),
                    "allocatedIpRange": "blueprint-restore-private-services",
                }
            },
            "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.2.0.3"}],
        }

    def get_instance_if_exists(self, name):
        self.calls.append(("get_instance_if_exists", name))
        return None

    def create_database(self, instance, database):
        self.calls.append(("create_database", instance, database))
        return "op-database"

    def set_postgres_password(self, instance, password):
        self.calls.append(("set_password", instance, password))
        return "op-password"

    def import_archive(self, instance, uri):
        self.calls.append(("import_archive", instance, uri))
        return "op-import"

    def get_operation(self, operation, *, stage):
        self.calls.append(("get_operation", operation, stage))
        if stage == self.fail_stage:
            return {"status": "DONE", "error": {"errors": [{"code": "hidden"}]}}
        return {"status": "DONE"}

    def delete_instance(self, name):
        self.calls.append(("delete_instance", name))
        if self.cleanup_fails:
            raise verifier.DrillError("cleanup", "hidden")
        return "op-delete"


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, *, revision="0711", missing_schema=False):
        self.revision = revision
        self.missing_schema = missing_schema
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query = " ".join(str(query).split())
        self.params = params

    def fetchone(self):
        if self.query == "SELECT 1":
            return (1,)
        if "server_version_num" in self.query:
            return (150012,)
        if 'count(*) FROM "alloweduser"' in self.query:
            return (2,)
        if 'count(*) FROM "project"' in self.query:
            return (4,)
        if 'count(*) FROM "task"' in self.query:
            return (20,)
        if 'count(*) FROM "artifact"' in self.query:
            return (12,)
        if "LEFT JOIN project" in self.query:
            return (0,)
        if "LEFT JOIN task" in self.query:
            return (0,)
        if "pg_constraint" in self.query:
            return (0,)
        raise AssertionError(f"unexpected fetchone query: {self.query}")

    def fetchall(self):
        if "FROM alembic_version" in self.query:
            return [(self.revision,)]
        if "information_schema.columns" in self.query:
            rows = [
                (table, column)
                for table, columns in verifier._REQUIRED_COLUMNS.items()
                for column in columns
            ]
            if self.missing_schema:
                rows.remove(("task", "status"))
            return rows
        raise AssertionError(f"unexpected fetchall query: {self.query}")


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def runtime(
    *,
    sql=None,
    storage=None,
    logger=None,
    revision="0711",
    missing_schema=False,
):
    process = FakeProcess()
    proxy_commands = []
    passwords = []

    def popen(command, **kwargs):
        proxy_commands.append((command, kwargs))
        return process

    def password(_length):
        value = "secret-password-" + "x" * 48
        passwords.append(value)
        return value

    times = iter([NOW, NOW + timedelta(minutes=7), NOW + timedelta(minutes=8)])
    result = verifier.Runtime(
        storage=storage or FakeStorage(),
        logger=logger or FakeLogger(),
        sql=sql or FakeSql(),
        db_connect=lambda **_kwargs: FakeConnection(
            FakeCursor(revision=revision, missing_schema=missing_schema)
        ),
        popen=popen,
        create_connection=lambda *_args, **_kwargs: FakeSocket(),
        now=lambda: next(times),
        monotonic=lambda: 0,
        sleep=lambda _seconds: None,
        password=password,
        suffix=lambda _bytes: "a1b2c3",
    )
    return result, process, proxy_commands, passwords


class ConfigTests(unittest.TestCase):
    def test_normalizes_legacy_leading_slash_network_input(self):
        cfg = config(
            RECOVERY_PRIVATE_NETWORK=(
                "/projects/blueprint-recovery/global/networks/recovery"
            )
        )
        self.assertEqual(
            cfg.private_network,
            "projects/blueprint-recovery/global/networks/recovery",
        )

    def test_requires_independent_projects_and_recovery_network(self):
        with self.assertRaisesRegex(verifier.DrillError, "projects_not_independent"):
            config(SOURCE_PROJECT_ID="blueprint-recovery")
        with self.assertRaisesRegex(
            verifier.DrillError, "network_not_in_recovery_project"
        ):
            config(
                RECOVERY_PRIVATE_NETWORK=(
                    "projects/blueprint-production/global/networks/default"
                )
            )
        with self.assertRaisesRegex(
            verifier.DrillError, "invalid_private_service_range"
        ):
            config(RECOVERY_PRIVATE_SERVICE_RANGE="projects/unsafe/ranges/escape")

    def test_bounds_cost_and_timeout_controls(self):
        with self.assertRaisesRegex(verifier.DrillError, "invalid_max_rpo_hours"):
            config(MAX_RPO_HOURS="169")
        with self.assertRaisesRegex(
            verifier.DrillError, "invalid_drill_instance_ttl_minutes"
        ):
            config(DRILL_INSTANCE_TTL_MINUTES="9999")
        with self.assertRaisesRegex(
            verifier.DrillError, "invalid_drill_operation_timeout_seconds"
        ):
            config(DRILL_OPERATION_TIMEOUT_SECONDS="forever")

    def test_exact_archive_pin_is_all_or_nothing(self):
        with self.assertRaisesRegex(
            verifier.DrillError, "incomplete_expected_archive"
        ):
            config(EXPECTED_ARCHIVE_NAME="local-postgres-exports/only.sql.gz")
        with self.assertRaisesRegex(
            verifier.DrillError, "invalid_expected_archive_name"
        ):
            config(
                EXPECTED_ARCHIVE_NAME="local-postgres-exports/../escape.sql.gz",
                EXPECTED_ARCHIVE_GENERATION="1",
                EXPECTED_MANIFEST_GENERATION="2",
                EXPECTED_ARCHIVE_SHA256="a" * 64,
                EXPECTED_ARCHIVE_SIZE_BYTES="10",
            )


class StorageTests(unittest.TestCase):
    @staticmethod
    def client_with_archive(*, content=b"compressed", age_hours=1, corrupt=False):
        archive_name = "local-postgres-exports/new.sql.gz"
        digest = hashlib.sha256(content).hexdigest()
        manifest = {
            "schema_version": 1,
            "archive_id": "a" * 32,
            "archive_uri": f"gs://blueprint-recovery-archives/{archive_name}",
            "archive_name": archive_name,
            "created_at": NOW.isoformat(),
            "database": "blueprint",
            "format": "plain-sql-gzip",
            "postgres_major": 15,
            "sha256": ("0" * 64 if corrupt else digest),
            "size_bytes": len(content),
        }
        manifest_blob = SimpleNamespace(
            name=f"{archive_name}.manifest.json",
            size=len(json.dumps(manifest)),
            generation=2,
            download_as_text=lambda timeout: json.dumps(manifest),
        )
        archive_blob = Mock()
        archive_blob.generation = 1
        archive_blob.size = len(content)
        archive_blob.time_created = NOW - timedelta(hours=age_hours)
        archive_blob.updated = None
        archive_blob.open.return_value = io.BytesIO(content)
        bucket = Mock()
        bucket.project_number = 123456789012
        bucket.blob.return_value = archive_blob
        client = Mock()
        client.bucket.return_value = bucket
        client.list_blobs.return_value = [manifest_blob]
        return client

    def test_selects_only_integrity_verified_manifest_archive_and_enforces_rpo(self):
        client = self.client_with_archive()
        selected = verifier.RecoveryStorage(client, config()).latest_archive(NOW)
        self.assertEqual(selected.name, "local-postgres-exports/new.sql.gz")
        self.assertEqual(selected.generation, "1")
        client.list_blobs.assert_called_once_with(
            "blueprint-recovery-archives",
            prefix="local-postgres-exports/",
            timeout=60,
        )

        stale_client = self.client_with_archive(age_hours=25)
        with self.assertRaisesRegex(verifier.DrillError, "archive_exceeds_rpo"):
            verifier.RecoveryStorage(stale_client, config()).latest_archive(NOW)

    def test_exact_archive_pin_binds_both_generations_digest_and_size(self):
        content = b"exact-generation"
        client = self.client_with_archive(content=content)
        bucket = client.bucket.return_value
        archive_blob = bucket.blob.return_value
        manifest_blob = client.list_blobs.return_value[0]
        manifest_blob.reload = Mock()

        def blob_for_name(name, generation=None):
            if name.endswith(".manifest.json"):
                self.assertEqual(generation, 2)
                return manifest_blob
            self.assertEqual(name, "local-postgres-exports/new.sql.gz")
            self.assertEqual(generation, 1)
            return archive_blob

        bucket.blob.side_effect = blob_for_name
        selected = verifier.RecoveryStorage(
            client,
            config(
                EXPECTED_ARCHIVE_NAME="local-postgres-exports/new.sql.gz",
                EXPECTED_ARCHIVE_GENERATION="1",
                EXPECTED_MANIFEST_GENERATION="2",
                EXPECTED_ARCHIVE_SHA256=hashlib.sha256(content).hexdigest(),
                EXPECTED_ARCHIVE_SIZE_BYTES=str(len(content)),
            ),
        ).latest_archive(NOW)

        self.assertEqual(
            selected.archive_id,
            "blueprint-recovery-archives/local-postgres-exports/new.sql.gz#1",
        )
        self.assertEqual(selected.manifest_generation, "2")
        client.list_blobs.assert_not_called()

    def test_latest_selection_hashes_only_newest_completed_candidate(self):
        client = self.client_with_archive()
        newest = client.list_blobs.return_value[0]
        newest.time_created = NOW
        newest.updated = None
        older_downloads = []
        manifests = [newest]
        for index in range(10):
            download = Mock(return_value="not-json")
            older_downloads.append(download)
            manifests.append(
                SimpleNamespace(
                    name=f"local-postgres-exports/old-{index}.sql.gz.manifest.json",
                    size=8,
                    generation=index + 10,
                    time_created=NOW - timedelta(hours=index + 2),
                    updated=None,
                    download_as_text=download,
                )
            )
        client.list_blobs.return_value = manifests

        selected = verifier.RecoveryStorage(client, config()).latest_archive(NOW)

        self.assertEqual(selected.name, "local-postgres-exports/new.sql.gz")
        self.assertEqual(sum(item.call_count for item in older_downloads), 0)

    def test_invalid_size_on_newest_manifest_does_not_fall_back(self):
        client = self.client_with_archive()
        older = client.list_blobs.return_value[0]
        older.time_created = NOW - timedelta(hours=2)
        older.updated = None
        older_download = Mock(wraps=older.download_as_text)
        older.download_as_text = older_download
        newest_download = Mock(return_value="not-json")
        newest = SimpleNamespace(
            name="local-postgres-exports/newest.sql.gz.manifest.json",
            size=0,
            generation=3,
            time_created=NOW,
            updated=None,
            download_as_text=newest_download,
        )
        client.list_blobs.return_value = [older, newest]

        with self.assertRaisesRegex(
            verifier.DrillError, "newest_archive_invalid"
        ):
            verifier.RecoveryStorage(client, config()).latest_archive(NOW)

        newest_download.assert_not_called()
        older_download.assert_not_called()

    def test_import_generation_guard_rejects_current_object_replacement(self):
        client = self.client_with_archive()
        storage = verifier.RecoveryStorage(client, config())
        selected = storage.latest_archive(NOW)
        replacement = Mock()
        replacement.generation = 99
        replacement.size = selected.size
        client.bucket.return_value.blob.return_value = replacement

        with self.assertRaisesRegex(
            verifier.DrillError, "archive_generation_changed"
        ):
            storage.assert_import_generation_current(selected)

    def test_skips_corrupt_archive_and_rejects_wrong_project_bucket(self):
        corrupt = self.client_with_archive(corrupt=True)
        with self.assertRaisesRegex(verifier.DrillError, "newest_archive_invalid"):
            verifier.RecoveryStorage(corrupt, config()).latest_archive(NOW)

        wrong_project = self.client_with_archive()
        wrong_project.bucket.return_value.project_number = 999999999999
        with self.assertRaisesRegex(
            verifier.DrillError, "bucket_not_in_recovery_project"
        ):
            verifier.RecoveryStorage(wrong_project, config()).latest_archive(NOW)

    def test_bucket_metadata_failure_has_fixed_diagnostic(self):
        client = self.client_with_archive()
        client.bucket.return_value.reload.side_effect = PermissionError(
            "sensitive provider detail"
        )

        with self.assertRaises(verifier.DrillError) as raised:
            verifier.RecoveryStorage(client, config()).latest_archive(NOW)

        self.assertEqual(raised.exception.stage, "storage_scope")
        self.assertEqual(raised.exception.code, "bucket_metadata_unavailable")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_evidence_write_is_create_only_and_structured(self):
        blob = Mock()
        client = Mock()
        client.bucket.return_value.project_number = 123456789012
        client.bucket.return_value.blob.return_value = blob
        verifier.RecoveryStorage(client, config()).write_evidence(
            "restore-drill-evidence/2026/result.json",
            {"event": "db_restore_drill", "outcome": "success"},
        )
        args, kwargs = blob.upload_from_string.call_args
        self.assertEqual(json.loads(args[0])["outcome"], "success")
        self.assertEqual(kwargs["if_generation_match"], 0)
        self.assertEqual(kwargs["timeout"], 60)


class CloudSqlContractTests(unittest.TestCase):
    def test_managed_service_agents_have_explicit_standard_roles(self):
        recovery_dir = Path(__file__).parents[1]
        main = (recovery_dir / "main.tf").read_text(encoding="utf-8")
        iam = (recovery_dir / "iam.tf").read_text(encoding="utf-8")
        prerequisites = (recovery_dir / "restore_verifier_prerequisites.tf").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'resource "google_project_service_identity" "service_networking"', main
        )
        self.assertIn('service  = "servicenetworking.googleapis.com"', main)
        self.assertIn('role    = "roles/cloudsql.serviceAgent"', iam)
        self.assertIn(
            "google_project_service_identity.cloud_sql[0].email", iam
        )
        self.assertIn('role    = "roles/servicenetworking.serviceAgent"', iam)
        self.assertIn(
            "google_project_service_identity.service_networking[0].email", iam
        )
        self.assertIn(
            "google_project_iam_member.recovery_service_networking_service_agent,",
            prerequisites,
        )

    def test_restore_verifier_can_read_only_its_bucket_metadata(self):
        terraform = (
            Path(__file__).parents[1] / "restore_verifier.tf"
        ).read_text(encoding="utf-8")
        role_start = terraform.index(
            'resource "google_storage_bucket_iam_member" '
            '"restore_verifier_bucket_metadata_reader"'
        )
        role_end = terraform.index(
            'resource "google_storage_bucket_iam_member" '
            '"restore_verifier_evidence_writer"'
        )
        role = terraform[role_start:role_end]

        self.assertIn('role   = "roles/storage.legacyBucketReader"', role)
        self.assertIn("bucket = google_storage_bucket.db_backup[0].name", role)
        self.assertIn("google_service_account.restore_verifier[0].email", role)
        self.assertIn(
            "google_storage_bucket_iam_member."
            "restore_verifier_bucket_metadata_reader,",
            terraform,
        )

    def test_restore_verifier_sql_role_can_consume_enabled_api(self):
        terraform = (
            Path(__file__).parents[1] / "restore_verifier.tf"
        ).read_text(encoding="utf-8")
        role_start = terraform.index(
            'resource "google_project_iam_custom_role" "restore_verifier_sql"'
        )
        role_end = terraform.index(
            'resource "google_project_iam_member" "restore_verifier_sql"'
        )
        self.assertIn(
            '"serviceusage.services.use"',
            terraform[role_start:role_end],
        )

    def test_instance_payload_is_postgres15_private_only_and_expiring(self):
        cfg = config()
        name = "bp-restore-202607301200-a1b2c3"
        body = verifier._instance_body(cfg, name, NOW + timedelta(hours=3))
        self.assertEqual(body["databaseVersion"], "POSTGRES_15")
        self.assertEqual(
            body["sqlNetworkArchitecture"], "NEW_NETWORK_ARCHITECTURE"
        )
        settings = body["settings"]
        self.assertNotIn("sqlNetworkArchitecture", settings)
        self.assertEqual(settings["tier"], "db-custom-1-3840")
        self.assertEqual(settings["dataDiskSizeGb"], "20")
        self.assertFalse(settings["storageAutoResize"])
        self.assertFalse(settings["deletionProtectionEnabled"])
        ip_config = settings["ipConfiguration"]
        self.assertIs(ip_config["ipv4Enabled"], False)
        self.assertEqual(ip_config["authorizedNetworks"], [])
        self.assertEqual(
            ip_config["privateNetwork"],
            "https://compute.googleapis.com/compute/v1/"
            "projects/blueprint-recovery/global/networks/recovery",
        )
        self.assertEqual(
            ip_config["allocatedIpRange"], "blueprint-restore-private-services"
        )
        self.assertNotIn("enablePrivatePathForGoogleCloudServices", ip_config)
        self.assertEqual(
            settings["userLabels"]["blueprint-purpose"], "restore-drill"
        )
        self.assertIn("expires-epoch", settings["userLabels"])

    def test_rest_client_is_recovery_project_scoped_and_has_request_timeout(self):
        response = SimpleNamespace(
            status_code=200,
            content=b'{"name":"op-1"}',
            json=lambda: {"name": "op-1"},
        )
        session = Mock()
        session.request.return_value = response
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        operation = client.import_archive(
            "bp-restore-202607301200-a1b2c3",
            "gs://blueprint-recovery-archives/archive.sql.gz",
        )
        self.assertEqual(operation, "op-1")
        _, url = session.request.call_args.args
        self.assertIn("/projects/blueprint-recovery/", url)
        self.assertNotIn("blueprint-production", url)
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertEqual(
            kwargs["json"]["importContext"]["fileType"], "SQL"
        )

    def test_rest_client_preserves_only_safe_failure_diagnostics(self):
        response = SimpleNamespace(
            status_code=400,
            content=b"secret",
            json=lambda: {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "database password super-secret-token",
                    "errors": [{"reason": "badRequest", "message": "secret"}],
                }
            },
        )
        session = Mock()
        session.request.return_value = response
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        with self.assertRaises(verifier.AdminApiError) as raised:
            client.create_instance("bp-restore-202607301200-a1b2c3", {})
        self.assertTrue(raised.exception.definitive_rejection)
        self.assertEqual(raised.exception.code, "admin_api_bad_request")
        self.assertEqual(
            raised.exception.diagnostic,
            {
                "http_status": 400,
                "provider_status": "INVALID_ARGUMENT",
                "provider_reason": "BAD_REQUEST",
                "provider_message_redacted": True,
            },
        )
        self.assertNotIn("secret", json.dumps(raised.exception.diagnostic))

    def test_rest_client_preserves_safe_bounded_instance_create_message(self):
        message = (
            "Project blueprint-recovery has invalid private network name "
            "/projects/blueprint-recovery/global/networks/recovery"
        )
        response = SimpleNamespace(
            status_code=400,
            content=b"safe",
            json=lambda: {
                "error": {
                    "message": message,
                    "errors": [{"reason": "invalid", "message": message}],
                }
            },
        )
        session = Mock()
        session.request.return_value = response
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        with self.assertRaises(verifier.AdminApiError) as raised:
            client.create_instance("bp-restore-202607301200-a1b2c3", {})
        self.assertEqual(
            raised.exception.diagnostic,
            {
                "http_status": 400,
                "provider_reason": "INVALID",
                "provider_message": message,
            },
        )

    def test_provider_message_filter_rejects_secret_like_or_high_entropy_data(self):
        self.assertIsNone(
            verifier._safe_provider_message("Invalid bearer token abc123")
        )
        self.assertIsNone(
            verifier._safe_provider_message("Invalid value " + "a" * 64)
        )
        self.assertIsNone(
            verifier._safe_provider_message(
                "Invalid value "
                "sk-proj-abcdefghijklmnopqrstuvwx-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            )
        )
        self.assertIsNone(
            verifier._safe_provider_message("Invalid network\x00name")
        )
        self.assertEqual(
            verifier._safe_provider_message("Invalid network\n name"),
            "Invalid network name",
        )

    def test_rest_client_transport_failure_is_ambiguous_and_non_sensitive(self):
        session = Mock()
        session.request.side_effect = RuntimeError("secret transport detail")
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        with self.assertRaises(verifier.AdminApiError) as raised:
            client.create_instance("bp-restore-202607301200-a1b2c3", {})
        self.assertFalse(raised.exception.definitive_rejection)
        self.assertEqual(raised.exception.code, "admin_api_transport_failed")
        self.assertEqual(raised.exception.diagnostic, {})

    def test_instance_verification_rejects_any_public_address(self):
        instance = {
            "project": "blueprint-recovery",
            "databaseVersion": "POSTGRES_15",
            "sqlNetworkArchitecture": "NEW_NETWORK_ARCHITECTURE",
            "connectionName": (
                "blueprint-recovery:us-central1:"
                "bp-restore-202607301200-a1b2c3"
            ),
            "settings": {
                "ipConfiguration": {
                    "ipv4Enabled": False,
                    "privateNetwork": (
                        "projects/blueprint-recovery/global/networks/recovery"
                    ),
                    "allocatedIpRange": "blueprint-restore-private-services",
                }
            },
            "ipAddresses": [{"type": "PRIMARY", "ipAddress": "35.1.2.3"}],
        }
        with self.assertRaisesRegex(verifier.DrillError, "public_ip_present"):
            verifier._verify_private_instance(instance, config())

    def test_instance_verification_rejects_network_architecture_or_range_drift(self):
        base = FakeSql().get_instance("bp-restore-202607301200-a1b2c3")
        for mutate in (
            lambda value: value.pop("sqlNetworkArchitecture"),
            lambda value: value.__setitem__(
                "sqlNetworkArchitecture", "OLD_NETWORK_ARCHITECTURE"
            ),
            lambda value: value["settings"]["ipConfiguration"].__setitem__(
                "allocatedIpRange", "unexpected-range"
            ),
        ):
            instance = json.loads(json.dumps(base))
            mutate(instance)
            with self.subTest(instance=instance):
                with self.assertRaisesRegex(
                    verifier.DrillError,
                    "instance_not_private_recovery_only",
                ):
                    verifier._verify_private_instance(instance, config())

    def test_instance_verification_rejects_noncanonical_or_cross_project_network(self):
        base = FakeSql().get_instance("bp-restore-202607301200-a1b2c3")
        for private_network in (
            "/projects/blueprint-recovery/global/networks/recovery",
            "projects/blueprint-production/global/networks/recovery",
            "projects/blueprint-recovery/global/networks/other",
            (
                "https://attacker.invalid/compute/v1/"
                "projects/blueprint-recovery/global/networks/recovery"
            ),
        ):
            instance = json.loads(json.dumps(base))
            instance["settings"]["ipConfiguration"][
                "privateNetwork"
            ] = private_network
            with self.subTest(private_network=private_network):
                with self.assertRaisesRegex(
                    verifier.DrillError,
                    "instance_not_private_recovery_only",
                ):
                    verifier._verify_private_instance(instance, config())

    def test_instance_verification_accepts_documented_network_resource_path(self):
        instance = FakeSql().get_instance("bp-restore-202607301200-a1b2c3")
        instance["settings"]["ipConfiguration"]["privateNetwork"] = (
            "projects/blueprint-recovery/global/networks/recovery"
        )
        self.assertEqual(
            verifier._verify_private_instance(instance, config()),
            "blueprint-recovery:us-central1:bp-restore-202607301200-a1b2c3",
        )

    def test_operation_failure_and_timeout_fail_closed(self):
        failed_sql = FakeSql(fail_stage="archive_import")
        rt, *_ = runtime(sql=failed_sql)
        with self.assertRaisesRegex(verifier.DrillError, "operation_failed"):
            verifier._wait_operation(
                rt, config(), "op-import", stage="archive_import"
            )

    def test_janitor_deletes_only_owned_expired_or_ambiguous_drills(self):
        sql = FakeSql()
        sql.instances = [
            {
                "name": "bp-restore-202607291200-a1b2c3",
                "settings": {
                    "userLabels": {
                        "blueprint-purpose": "restore-drill",
                        "expires-epoch": str(int((NOW - timedelta(hours=1)).timestamp())),
                    }
                },
            },
            {
                "name": "bp-restore-202607301200-b1c2d3",
                "settings": {
                    "userLabels": {
                        "blueprint-purpose": "restore-drill",
                        "expires-epoch": "invalid",
                    }
                },
            },
            {
                "name": "bp-restore-202607301200-c1d2e3",
                "settings": {
                    "userLabels": {
                        "blueprint-purpose": "restore-drill",
                        "expires-epoch": str(int((NOW + timedelta(hours=1)).timestamp())),
                    }
                },
            },
            {
                "name": "production-db",
                "settings": {
                    "userLabels": {
                        "blueprint-purpose": "restore-drill",
                        "expires-epoch": "0",
                    }
                },
            },
        ]
        rt, *_ = runtime(sql=sql)
        removed = verifier._reconcile_drill_instances(rt, config(), NOW)
        self.assertEqual(
            removed,
            [
                "bp-restore-202607291200-a1b2c3",
                "bp-restore-202607301200-b1c2d3",
            ],
        )
        deleted = [call[1] for call in sql.calls if call[0] == "delete_instance"]
        self.assertEqual(deleted, removed)

        pending_sql = FakeSql()
        pending_sql.get_operation = Mock(return_value={"status": "RUNNING"})
        ticks = iter([0, 0, 7201])
        rt, *_ = runtime(sql=pending_sql)
        rt.monotonic = lambda: next(ticks)
        with self.assertRaisesRegex(verifier.DrillError, "operation_timed_out"):
            verifier._wait_operation(
                rt, config(), "op-import", stage="archive_import"
            )


class ProbeTests(unittest.TestCase):
    def test_schema_revision_and_required_columns_are_exact(self):
        rt, *_ = runtime(revision="0710")
        with self.assertRaisesRegex(
            verifier.DrillError, "schema_revision_mismatch"
        ):
            verifier._database_checks(rt, config(), "not-logged")

        rt, *_ = runtime(missing_schema=True)
        with self.assertRaisesRegex(
            verifier.DrillError, "required_schema_missing"
        ):
            verifier._database_checks(rt, config(), "not-logged")


class DrillLifecycleTests(unittest.TestCase):
    def test_success_imports_probes_writes_evidence_and_deletes(self):
        sql = FakeSql()
        storage = FakeStorage()
        logger = FakeLogger()
        rt, process, proxy_commands, passwords = runtime(
            sql=sql, storage=storage, logger=logger
        )
        exit_code, evidence = verifier.run_drill(config(), rt)

        self.assertEqual(exit_code, 0)
        self.assertEqual(evidence["outcome"], "success")
        self.assertEqual(evidence["observed_revision"], "0711")
        self.assertEqual(evidence["archive"]["rpo_seconds"], 3600)
        self.assertEqual(evidence["rto_seconds"], 420)
        self.assertTrue(evidence["cleanup"]["completed"])
        self.assertTrue(process.terminated)
        self.assertEqual(len(storage.writes), 1)
        self.assertEqual(len(storage.import_generation_checks), 2)
        self.assertTrue(evidence["archive"]["generation_stable_during_import"])
        self.assertEqual(len(logger.writes), 1)
        self.assertTrue(
            any(call[0] == "import_archive" for call in sql.calls)
        )
        self.assertTrue(
            any(call[0] == "delete_instance" for call in sql.calls)
        )
        proxy_command = proxy_commands[0][0]
        self.assertIn("--private-ip", proxy_command)
        self.assertIn("--address=127.0.0.1", proxy_command)
        serialized = json.dumps(evidence)
        self.assertNotIn(passwords[0], serialized)
        self.assertNotIn(passwords[0], " ".join(proxy_command))
        check_names = {item["name"] for item in evidence["checks"]}
        self.assertIn("archive_decryption_and_import", check_names)
        self.assertIn("app_health_query", check_names)
        self.assertIn("app_schema_compatible", check_names)
        self.assertIn("non_sensitive_row_counts", check_names)

    def test_import_failure_still_deletes_and_records_safe_failure(self):
        sql = FakeSql(fail_stage="archive_import")
        storage = FakeStorage()
        logger = FakeLogger()
        rt, *_ = runtime(sql=sql, storage=storage, logger=logger)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["stage"], "archive_import")
        self.assertEqual(evidence["failure_code"], "operation_failed")
        self.assertTrue(evidence["cleanup"]["completed"])
        self.assertNotIn("hidden", json.dumps(evidence))
        self.assertEqual(len(storage.writes), 1)
        self.assertEqual(len(logger.writes), 1)

    def test_ambiguous_create_without_owned_target_defers_to_janitor(self):
        class AmbiguousCreateSql(FakeSql):
            def create_instance(self, name, body):
                self.calls.append(("create_instance", name, body))
                raise verifier.DrillError("instance_create", "request_ambiguous")

        sql = AmbiguousCreateSql()
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertTrue(evidence["cleanup"]["intent_recorded"])
        self.assertTrue(evidence["cleanup"]["ownership_check_attempted"])
        self.assertTrue(evidence["cleanup"]["ownership_check_completed"])
        self.assertTrue(evidence["cleanup"]["deferred_to_janitor"])
        self.assertFalse(evidence["cleanup"]["attempted"])
        self.assertFalse(evidence["cleanup"]["completed"])
        self.assertFalse(any(call[0] == "delete_instance" for call in sql.calls))

    def test_definitive_create_rejection_does_not_issue_delete(self):
        class RejectedCreateSql(FakeSql):
            def create_instance(self, name, body):
                self.calls.append(("create_instance", name, body))
                raise verifier.AdminApiError(
                    "instance_create",
                    "admin_api_bad_request",
                    definitive_rejection=True,
                    diagnostic={"http_status": 400, "provider_status": "INVALID_ARGUMENT"},
                )

        sql = RejectedCreateSql()
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["failure_code"], "admin_api_bad_request")
        self.assertEqual(evidence["failure_diagnostic"]["http_status"], 400)
        self.assertTrue(evidence["cleanup"]["intent_recorded"])
        self.assertFalse(evidence["cleanup"]["attempted"])
        self.assertTrue(evidence["cleanup"]["completed"])
        self.assertTrue(evidence["cleanup"]["not_required"])
        self.assertFalse(any(call[0] == "delete_instance" for call in sql.calls))

    def test_create_conflict_is_definitive_and_never_deletes(self):
        response = SimpleNamespace(
            status_code=409,
            content=b'{"error":{"status":"ALREADY_EXISTS"}}',
            json=lambda: {"error": {"status": "ALREADY_EXISTS"}},
        )
        session = Mock()
        session.request.return_value = response
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        with self.assertRaises(verifier.AdminApiError) as raised:
            client.create_instance("bp-restore-202607301200-a1b2c3", {})
        self.assertTrue(raised.exception.definitive_rejection)
        self.assertEqual(raised.exception.code, "admin_api_conflict")

    def test_rate_limit_is_ambiguous_for_ownership_gated_reconciliation(self):
        response = SimpleNamespace(
            status_code=429,
            content=b'{"error":{"status":"RESOURCE_EXHAUSTED"}}',
            json=lambda: {"error": {"status": "RESOURCE_EXHAUSTED"}},
        )
        session = Mock()
        session.request.return_value = response
        client = verifier.SqlAdminRest(session, "blueprint-recovery")
        with self.assertRaises(verifier.AdminApiError) as raised:
            client.create_instance("bp-restore-202607301200-a1b2c3", {})
        self.assertFalse(raised.exception.definitive_rejection)
        self.assertEqual(raised.exception.code, "admin_api_rate_limited")

    def test_ambiguous_admin_api_failure_still_issues_delete(self):
        class AmbiguousCreateSql(FakeSql):
            def create_instance(self, name, body):
                self.calls.append(("create_instance", name, body))
                raise verifier.AdminApiError(
                    "instance_create",
                    "admin_api_unavailable",
                    definitive_rejection=False,
                    diagnostic={"http_status": 503},
                )

            def get_instance_if_exists(self, name):
                self.calls.append(("get_instance_if_exists", name))
                return {
                    "project": "blueprint-recovery",
                    "name": name,
                    "settings": {
                        "userLabels": {
                            "blueprint-purpose": "restore-drill",
                            "blueprint-drill": name.removeprefix("bp-restore-"),
                            "expires-epoch": str(
                                int((NOW + timedelta(hours=3)).timestamp())
                            ),
                        }
                    },
                }

        sql = AmbiguousCreateSql()
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["failure_diagnostic"], {"http_status": 503})
        self.assertTrue(evidence["cleanup"]["attempted"])
        self.assertTrue(evidence["cleanup"]["completed"])
        self.assertTrue(any(call[0] == "delete_instance" for call in sql.calls))

    def test_ambiguous_create_never_deletes_absent_or_unowned_instance(self):
        class AmbiguousCreateSql(FakeSql):
            def create_instance(self, name, body):
                self.calls.append(("create_instance", name, body))
                raise verifier.AdminApiError(
                    "instance_create",
                    "admin_api_transport_failed",
                    definitive_rejection=False,
                )

            def get_instance_if_exists(self, name):
                self.calls.append(("get_instance_if_exists", name))
                return {
                    "project": "blueprint-recovery",
                    "name": name,
                    "settings": {"userLabels": {"blueprint-purpose": "foreign"}},
                }

        sql = AmbiguousCreateSql()
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertTrue(evidence["cleanup"]["ownership_check_attempted"])
        self.assertTrue(evidence["cleanup"]["ownership_check_completed"])
        self.assertTrue(evidence["cleanup"]["deferred_to_janitor"])
        self.assertFalse(evidence["cleanup"]["attempted"])
        self.assertFalse(evidence["cleanup"]["completed"])
        self.assertFalse(any(call[0] == "delete_instance" for call in sql.calls))

    def test_ambiguous_create_records_reconciliation_failure(self):
        class AmbiguousCreateSql(FakeSql):
            def create_instance(self, name, body):
                self.calls.append(("create_instance", name, body))
                raise verifier.AdminApiError(
                    "instance_create",
                    "admin_api_transport_failed",
                    definitive_rejection=False,
                )

            def get_instance_if_exists(self, name):
                self.calls.append(("get_instance_if_exists", name))
                raise verifier.AdminApiError(
                    "cleanup_reconcile",
                    "admin_api_forbidden",
                    definitive_rejection=True,
                    diagnostic={"http_status": 403},
                )

        sql = AmbiguousCreateSql()
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertTrue(evidence["cleanup"]["ownership_check_attempted"])
        self.assertFalse(evidence["cleanup"]["ownership_check_completed"])
        self.assertEqual(
            evidence["cleanup"]["ownership_check_failure_code"],
            "cleanup_reconcile_failed",
        )
        self.assertTrue(evidence["cleanup"]["deferred_to_janitor"])
        self.assertFalse(any(call[0] == "delete_instance" for call in sql.calls))

    def test_primary_failure_survives_cleanup_failure(self):
        sql = FakeSql(fail_stage="archive_import", cleanup_fails=True)
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["stage"], "archive_import")
        self.assertEqual(evidence["failure_code"], "operation_failed")
        self.assertEqual(
            evidence["cleanup"]["failure_code"], "instance_cleanup_failed"
        )

    def test_bucket_evidence_failure_is_logged_as_a_failed_drill(self):
        storage = FakeStorage(fail_writes=True)
        logger = FakeLogger()
        rt, *_ = runtime(storage=storage, logger=logger)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["failure_code"], "evidence_bucket_write_failed")
        self.assertEqual(logger.writes[0]["outcome"], "failure")
        self.assertEqual(logger.writes[0]["stage"], "evidence_write")
        self.assertEqual(
            evidence["evidence_write"]["failure_code"],
            "evidence_bucket_write_failed",
        )

    def test_bucket_evidence_failure_preserves_primary_drill_failure(self):
        sql = FakeSql(fail_stage="archive_import")
        storage = FakeStorage(fail_writes=True)
        logger = FakeLogger()
        rt, *_ = runtime(sql=sql, storage=storage, logger=logger)

        exit_code, evidence = verifier.run_drill(config(), rt)

        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["stage"], "archive_import")
        self.assertEqual(evidence["failure_code"], "operation_failed")
        self.assertEqual(
            evidence["evidence_write"],
            {
                "completed": False,
                "failure_code": "evidence_bucket_write_failed",
            },
        )
        self.assertEqual(logger.writes[0]["failure_code"], "operation_failed")

    def test_logging_failure_adds_failure_supplement_to_bucket(self):
        storage = FakeStorage()
        logger = FakeLogger(fail_writes=True)
        rt, *_ = runtime(storage=storage, logger=logger)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            evidence["failure_code"], "evidence_logging_write_failed"
        )
        self.assertEqual(len(storage.writes), 2)
        self.assertTrue(storage.writes[1][0].endswith("-failure.json"))
        self.assertEqual(storage.writes[1][1]["outcome"], "failure")

    def test_logging_failure_preserves_primary_drill_failure(self):
        sql = FakeSql(fail_stage="archive_import")
        storage = FakeStorage()
        logger = FakeLogger(fail_writes=True)
        rt, *_ = runtime(sql=sql, storage=storage, logger=logger)

        exit_code, evidence = verifier.run_drill(config(), rt)

        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["stage"], "archive_import")
        self.assertEqual(evidence["failure_code"], "operation_failed")
        self.assertEqual(
            evidence["logging_write"],
            {
                "completed": False,
                "failure_code": "evidence_logging_write_failed",
            },
        )
        self.assertEqual(len(storage.writes), 2)
        self.assertTrue(storage.writes[1][0].endswith("-failure.json"))

    def test_cleanup_failure_overrides_success_and_expiry_label_remains(self):
        sql = FakeSql(cleanup_fails=True)
        rt, *_ = runtime(sql=sql)
        exit_code, evidence = verifier.run_drill(config(), rt)
        self.assertEqual(exit_code, 1)
        self.assertEqual(evidence["stage"], "cleanup")
        self.assertEqual(evidence["failure_code"], "instance_cleanup_failed")
        self.assertFalse(evidence["cleanup"]["completed"])
        create_body = next(
            call[2] for call in sql.calls if call[0] == "create_instance"
        )
        self.assertIn("expires-epoch", create_body["settings"]["userLabels"])

    def test_proxy_failure_never_includes_database_password_in_command(self):
        rt, process, commands, passwords = runtime()
        process.returncode = 1
        with self.assertRaisesRegex(verifier.DrillError, "proxy_exited"):
            verifier._start_proxy(
                rt,
                config(),
                (
                    "blueprint-recovery:us-central1:"
                    "bp-restore-202607301200-a1b2c3"
                ),
            )
        self.assertFalse(passwords)
        self.assertNotIn("password", " ".join(commands[0][0]).lower())

    def test_proxy_start_timeout_terminates_spawned_process(self):
        rt, process, _commands, _passwords = runtime()
        ticks = iter([0, 0, 121])
        rt.monotonic = lambda: next(ticks)
        rt.create_connection = Mock(side_effect=OSError("not ready"))
        with self.assertRaisesRegex(verifier.DrillError, "proxy_timed_out"):
            verifier._start_proxy(
                rt,
                config(),
                (
                    "blueprint-recovery:us-central1:"
                    "bp-restore-202607301200-a1b2c3"
                ),
            )
        self.assertTrue(process.terminated)

    def test_stop_proxy_escalates_after_bounded_grace(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("proxy", 10),
            0,
        ]
        verifier._stop_proxy(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
