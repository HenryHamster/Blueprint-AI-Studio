#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).with_name("local_postgres_backup_service.py")
SPEC = importlib.util.spec_from_file_location("local_postgres_backup_service", MODULE_PATH)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

POSTGRES_ID = f"sha256:{'a' * 64}"
UPLOADER_ID = f"sha256:{'b' * 64}"
PROJECT = "recovery-project-123"
NETWORK = "blueprint-control-plane"


def completed(argv, returncode=0, stdout="", stderr=""):
    return backup.subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class LocalPostgresBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage_patch = patch.object(
            backup, "_SECRET_STAGE_ROOT", self.root / "stage"
        )
        self.stage_patch.start()
        self.password = self.root / "postgres-password"
        self.credentials = self.root / "backup-writer.json"
        self.password.write_text("not-a-real-password\n", encoding="utf-8")
        self.credentials.write_text(
            json.dumps(
                {
                    "type": "service_account",
                    "project_id": PROJECT,
                    "client_email": (
                        f"blueprint-local-db-backup@{PROJECT}.iam.gserviceaccount.com"
                    ),
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            self.password.chmod(0o600)
            self.credentials.chmod(0o600)
        self.env = {
            "BLUEPRINT_POSTGRES_IMAGE_ID": POSTGRES_ID,
            "BLUEPRINT_POSTGRES_BACKUP_IMAGE_ID": UPLOADER_ID,
            "BLUEPRINT_CONTROL_PLANE_NETWORK": NETWORK,
            "BLUEPRINT_POSTGRES_SECRETS_FILE": str(self.password),
            "BLUEPRINT_POSTGRES_BACKUP_PROJECT_ID": PROJECT,
            "BLUEPRINT_POSTGRES_BACKUP_CREDENTIALS_FILE": str(self.credentials),
            "BLUEPRINT_POSTGRES_BACKUP_BUCKET": "gs://recovery-db-backups",
        }

    def tearDown(self):
        self.stage_patch.stop()
        self.temp.cleanup()

    def test_dump_uses_pipe_password_file_and_no_host_port(self):
        argv = backup._dump_command(POSTGRES_ID, NETWORK, self.password)
        joined = " ".join(map(str, argv))
        self.assertNotIn("not-a-real-password", joined)
        self.assertNotIn("--publish", argv)
        self.assertNotIn("-p", argv)
        self.assertIn("POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password", joined)
        self.assertIn("--format=plain", joined)
        self.assertIn("--host=blueprint-postgres", joined)
        self.assertEqual(argv[argv.index("--log-driver") + 1], "none")
        self.assertEqual(argv[argv.index("--memory") + 1], "1g")

    def test_uploader_has_writer_credentials_but_no_database_secret(self):
        argv = backup._upload_command(
            UPLOADER_ID,
            NETWORK,
            self.credentials,
            PROJECT,
            "gs://recovery-db-backups/object.sql.gz",
        )
        joined = " ".join(map(str, argv))
        self.assertIn(str(self.credentials), joined)
        self.assertNotIn(str(self.password), joined)
        self.assertNotIn("not-a-real-password", joined)
        self.assertIn("max-size=10m", argv)
        self.assertIn("max-file=1", argv)
        self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.assertEqual(argv[-1], "gs://recovery-db-backups/object.sql.gz")
        self.assertIn("gcs_create_only_upload.py", joined)
        self.assertIn("python3", argv)
        self.assertNotIn("storage", argv)
        self.assertIn("readonly", joined)

    def test_gzip_stage_is_networkless_and_bounded(self):
        argv = backup._gzip_command(POSTGRES_ID)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("no-new-privileges", argv)
        self.assertEqual(argv[-3:], ["gzip", "-6", "-c"])

    def test_mutable_images_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            backup._image_id("postgres:15", postgres=True)
        with self.assertRaisesRegex(ValueError, "immutable"):
            backup._image_id("google/cloud-sdk:latest", postgres=False)

    @unittest.skipIf(os.name == "nt", "POSIX mode contract")
    def test_credentials_require_0600(self):
        self.credentials.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            backup._credentials(str(self.credentials), PROJECT)

    def test_credentials_reject_non_writer_identity(self):
        payload = json.loads(self.credentials.read_text(encoding="utf-8"))
        payload["client_email"] = f"owner@{PROJECT}.iam.gserviceaccount.com"
        self.credentials.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dedicated writer"):
            backup._credentials(str(self.credentials), PROJECT)

    def test_credentials_reject_symlink(self):
        link = self.root / "credentials-link"
        try:
            link.symlink_to(self.credentials)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            backup._credentials(str(link), PROJECT)

    def test_bucket_must_not_include_object_prefix(self):
        env = dict(self.env)
        env["BLUEPRINT_POSTGRES_BACKUP_BUCKET"] = (
            "gs://recovery-db-backups/operator/chosen"
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(backup, "_single_flight", return_value=nullcontext()),
            redirect_stdout(io.StringIO()) as stream,
        ):
            self.assertEqual(backup.main([]), 1)
        self.assertIn("bucket-only", stream.getvalue())

    def test_database_container_rejects_image_mismatch(self):
        payload = {
            "Image": f"sha256:{'c' * 64}",
            "Config": {"Labels": {"dev.blueprint.service": "local-postgres"}},
            "State": {"Running": True, "Dead": False},
            "HostConfig": {"NetworkMode": NETWORK, "PortBindings": {}},
            "NetworkSettings": {
                "Networks": {NETWORK: {"Aliases": ["blueprint-postgres"]}}
            },
        }
        with (
            patch.object(backup, "_inspect", return_value=payload),
            self.assertRaisesRegex(RuntimeError, "different image"),
        ):
            backup._database_container(POSTGRES_ID, NETWORK)

    def test_database_container_rejects_published_port(self):
        payload = {
            "Image": POSTGRES_ID,
            "Config": {"Labels": {"dev.blueprint.service": "local-postgres"}},
            "State": {"Running": True, "Dead": False},
            "HostConfig": {
                "NetworkMode": NETWORK,
                "PortBindings": {"5432/tcp": [{"HostPort": "5432"}]},
            },
            "NetworkSettings": {
                "Networks": {NETWORK: {"Aliases": ["blueprint-postgres"]}}
            },
        }
        with (
            patch.object(backup, "_inspect", return_value=payload),
            self.assertRaisesRegex(RuntimeError, "unsafe networking"),
        ):
            backup._database_container(POSTGRES_ID, NETWORK)

    def test_recovery_iam_is_create_only_without_archive_or_kms_access(self):
        root = Path(__file__).parents[1]
        iam = (root / "infra" / "recovery" / "iam.tf").read_text(encoding="utf-8")
        start = iam.index(
            'resource "google_service_account" "local_postgres_backup_writer"'
        )
        end = iam.index(
            'resource "google_storage_bucket_iam_member" "recovery_cloud_sql_reader"'
        )
        writer = iam[start:end]
        self.assertIn('role   = "roles/storage.objectCreator"', writer)
        self.assertNotIn("objectViewer", writer)
        self.assertNotIn("objectAdmin", writer)
        self.assertNotIn("cloudkms", writer)
        self.assertNotIn("service_account_key", writer)

    @patch.object(backup.socket, "socket")
    def test_overlapping_run_is_refused(self, socket_type):
        lock = socket_type.return_value
        lock.bind.side_effect = OSError("in use")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            with backup._single_flight():
                self.fail("lock should refuse overlap")
        lock.close.assert_called_once()

    @patch.object(backup, "_run")
    def test_cloud_sdk_probe_is_credential_free_and_bounded(self, run):
        run.return_value = completed(["docker"], stdout="Google Cloud SDK")
        backup._assert_cloud_sdk(UPLOADER_ID)
        argv = run.call_args.args[0]
        self.assertIn("none", argv)
        self.assertIn("128m", argv)
        self.assertNotIn(str(self.credentials), " ".join(argv))
        self.assertIn("gcloud --version", argv[-1])
        self.assertIn("python3 --version", argv[-1])
        self.assertIn("curl --version", argv[-1])

    def test_stream_fails_when_dump_fails_even_if_upload_exits_zero(self):
        with patch.object(backup.subprocess, "Popen") as popen:
            dump = MagicMock()
            dump.stdout = MagicMock()
            dump.stderr = io.BytesIO(b"dump failed")
            dump.poll.return_value = 2
            dump.returncode = 2
            gzip = MagicMock()
            gzip.stdout = io.BytesIO(b"compressed")
            gzip.stderr = io.BytesIO()
            gzip.poll.return_value = 0
            gzip.returncode = 0
            upload = MagicMock()
            upload.stdin = io.BytesIO()
            upload.stdout = io.BytesIO()
            upload.stderr = io.BytesIO()
            upload.returncode = 0
            upload.poll.return_value = 0
            popen.side_effect = [dump, gzip, upload]
            result = backup._stream(["dump"], ["gzip"], ["upload"])
            self.assertEqual(result.dump_returncode, 2)
            self.assertEqual(result.gzip_returncode, 0)
            self.assertEqual(result.upload_returncode, 0)
            self.assertEqual(result.size_bytes, len(b"compressed"))

    def test_stream_terminates_both_sides_on_timeout(self):
        with (
            patch.object(backup.subprocess, "Popen") as popen,
            patch.object(backup.time, "monotonic", side_effect=[0, 4000]),
        ):
            dump = MagicMock()
            dump.stdout = MagicMock()
            dump.stderr = io.BytesIO()
            dump.poll.return_value = None
            gzip = MagicMock()
            gzip.stdout = io.BytesIO()
            gzip.stderr = io.BytesIO()
            gzip.poll.return_value = None
            upload = MagicMock()
            upload.stdin = io.BytesIO()
            upload.stdout = io.BytesIO()
            upload.stderr = io.BytesIO()
            upload.poll.return_value = None
            popen.side_effect = [dump, gzip, upload]
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                backup._stream(["dump"], ["gzip"], ["upload"])
            dump.terminate.assert_called_once()
            gzip.terminate.assert_called_once()
            upload.terminate.assert_called_once()

    def test_stream_terminates_pipeline_on_early_uploader_failure(self):
        with patch.object(backup.subprocess, "Popen") as popen:
            dump = MagicMock()
            dump.stdout = MagicMock()
            dump.stderr = io.BytesIO()
            dump.poll.return_value = None
            gzip = MagicMock()
            gzip.stdout = io.BytesIO()
            gzip.stderr = io.BytesIO()
            gzip.poll.return_value = None
            upload = MagicMock()
            upload.stdin = io.BytesIO()
            upload.stdout = io.BytesIO()
            upload.stderr = io.BytesIO(b"upload failed")
            upload.poll.return_value = 7
            upload.returncode = 7
            popen.side_effect = [dump, gzip, upload]

            with self.assertRaisesRegex(RuntimeError, "upload container failed"):
                backup._stream(["dump"], ["gzip"], ["upload"])

            dump.terminate.assert_called_once()
            gzip.terminate.assert_called_once()
            upload.terminate.assert_not_called()

    def test_secret_is_staged_to_private_file_and_removed(self):
        with backup._staged_secret(b"secret", "test secret") as staged:
            self.assertEqual(staged.parent, self.root / "stage")
            self.assertNotEqual(staged, self.password)
            self.assertEqual(staged.read_bytes(), b"secret")
            if os.name != "nt":
                self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertFalse(staged.exists())

    def test_main_emits_valid_structured_failure_without_secret(self):
        stream = io.StringIO()
        with (
            patch.object(backup, "backup", side_effect=RuntimeError('bad "failure"\n')),
            redirect_stdout(stream),
        ):
            self.assertEqual(backup.main([]), 1)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["outcome"], "failure")
        self.assertNotIn("not-a-real-password", stream.getvalue())

    def test_success_uses_unique_object_and_structured_event(self):
        objects: list[str] = []
        manifests: list[dict] = []

        def capture(_dump, _gzip, upload):
            objects.append(upload[-1])
            return backup.StreamResult(
                dump_returncode=0,
                gzip_returncode=0,
                upload_returncode=0,
                size_bytes=123,
                sha256="d" * 64,
            )

        def capture_manifest(_argv, payload):
            manifests.append(payload)

        stream = io.StringIO()
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(backup, "_single_flight", return_value=nullcontext()),
            patch.object(
                backup,
                "_image_id",
                side_effect=lambda _image, *, postgres: (
                    POSTGRES_ID if postgres else UPLOADER_ID
                ),
            ),
            patch.object(backup, "_assert_cloud_sdk"),
            patch.object(backup, "_network"),
            patch.object(backup, "_database_container"),
            patch.object(backup, "_stream", side_effect=capture),
            patch.object(backup, "_upload_manifest", side_effect=capture_manifest),
            redirect_stdout(stream),
        ):
            self.assertEqual(backup.backup(), 0)
            self.assertEqual(backup.backup(), 0)
        self.assertNotEqual(objects[0], objects[1])
        self.assertTrue(all(item.endswith(".sql.gz") for item in objects))
        self.assertEqual(len(manifests), 2)
        for obj, manifest in zip(objects, manifests, strict=True):
            self.assertEqual(manifest["archive_uri"], obj)
            self.assertEqual(manifest["format"], "plain-sql-gzip")
            self.assertEqual(manifest["sha256"], "d" * 64)
            self.assertEqual(manifest["size_bytes"], 123)
        for line in stream.getvalue().splitlines():
            self.assertEqual(json.loads(line)["outcome"], "success")

    def test_failed_dump_never_publishes_success_manifest(self):
        result = backup.StreamResult(2, 0, 0, 50, "e" * 64)
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(backup, "_single_flight", return_value=nullcontext()),
            patch.object(
                backup,
                "_image_id",
                side_effect=lambda _image, *, postgres: (
                    POSTGRES_ID if postgres else UPLOADER_ID
                ),
            ),
            patch.object(backup, "_assert_cloud_sdk"),
            patch.object(backup, "_network"),
            patch.object(backup, "_database_container"),
            patch.object(backup, "_stream", return_value=result),
            patch.object(backup, "_upload_manifest") as manifest,
            self.assertRaisesRegex(RuntimeError, "pg_dump"),
        ):
            backup.backup()
        manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
