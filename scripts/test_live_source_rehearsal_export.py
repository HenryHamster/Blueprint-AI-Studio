import datetime as dt
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import live_source_rehearsal_export as producer


NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.timezone.utc)
ARCHIVE_ID = "a" * 32
BUCKET = "gs://blueprint-recovery-archives"


def _load_verifier():
    path = (
        Path(__file__).parents[1]
        / "infra"
        / "recovery"
        / "restore_verifier"
        / "verify_restore.py"
    )
    spec = importlib.util.spec_from_file_location("rehearsal_restore_verifier", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Blob:
    def __init__(self, *, name, contents, generation, created_at):
        self.name = name
        self._contents = contents
        self.generation = generation
        self.time_created = created_at
        self.updated = created_at
        self.size = len(contents)

    def reload(self, timeout):
        return None

    def download_as_text(self, timeout):
        return self._contents.decode()

    def open(self, mode):
        return io.BytesIO(self._contents)


class ManifestContractTests(unittest.TestCase):
    def test_exact_manifest_is_accepted_by_unchanged_independent_verifier(self):
        verifier = _load_verifier()
        compressed = b"\x1f\x8bproduction-shaped-rehearsal"
        archive_name = (
            f"local-postgres-exports/20260730T120000Z-{ARCHIVE_ID}.sql.gz"
        )
        manifest = producer.completion_manifest(
            bucket=BUCKET,
            archive_name=archive_name,
            archive_id=ARCHIVE_ID,
            sha256=hashlib.sha256(compressed).hexdigest(),
            size_bytes=len(compressed),
            created_at=NOW,
        )
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "archive_id",
                "archive_uri",
                "archive_name",
                "created_at",
                "database",
                "format",
                "postgres_major",
                "sha256",
                "size_bytes",
            },
        )
        marker_bytes = json.dumps(manifest).encode()
        marker = _Blob(
            name=f"{archive_name}.manifest.json",
            contents=marker_bytes,
            generation=2,
            created_at=NOW,
        )
        archive = _Blob(
            name=archive_name,
            contents=compressed,
            generation=1,
            created_at=NOW,
        )
        bucket = Mock()
        bucket.project_number = 123
        bucket.blob.return_value = archive
        client = Mock()
        client.bucket.return_value = bucket
        client.list_blobs.return_value = [marker]
        config = SimpleNamespace(
            bucket_name="blueprint-recovery-archives",
            recovery_project_number="123",
            archive_prefix="local-postgres-exports/",
            max_rpo_seconds=86400,
        )
        selected = verifier.RecoveryStorage(client, config).latest_archive(NOW)
        self.assertEqual(selected.name, archive_name)
        self.assertEqual(selected.sha256, manifest["sha256"])
        self.assertEqual(selected.size, manifest["size_bytes"])

    def test_manifest_rejects_name_id_mismatch(self):
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "archive_id_mismatch"
        ):
            producer.completion_manifest(
                bucket=BUCKET,
                archive_name=(
                    "local-postgres-exports/20260730T120000Z-"
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.sql.gz"
                ),
                archive_id=ARCHIVE_ID,
                sha256="c" * 64,
                size_bytes=1,
                created_at=NOW,
            )


class PublicationOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "source.sql.gz"
        self.archive.write_bytes(b"\x1f\x8barchive")
        self.args = SimpleNamespace(
            bucket=BUCKET,
            recovery_project_id="recovery-project-123",
            writer_credentials_file="/secure/writer.json",
            docker_binary="/usr/bin/docker",
            uploader_image_id="sha256:" + "d" * 64,
            control_plane_network="blueprint-control-plane",
            secret_stage_root=self.root,
        )
        self.metadata = {
            "path": self.archive,
            "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "size_bytes": self.archive.stat().st_size,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_upload_precedes_completion_marker(self):
        calls = []

        def upload(**kwargs):
            calls.append((kwargs["source"], kwargs["destination"]))

        manifest = producer.publish_archive(
            self.args,
            self.metadata,
            b"unused",
            now=lambda: NOW,
            archive_id=ARCHIVE_ID,
            upload=upload,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], self.archive)
        self.assertEqual(calls[0][1], manifest["archive_uri"])
        self.assertTrue(calls[1][1].endswith(".sql.gz.manifest.json"))
        self.assertFalse(calls[1][0].exists())

    def test_archive_upload_failure_never_attempts_marker(self):
        calls = []

        def upload(**kwargs):
            calls.append(kwargs["destination"])
            raise producer.RehearsalExportError("upload", "object_create_failed")

        with self.assertRaisesRegex(producer.RehearsalExportError, "object_create_failed"):
            producer.publish_archive(
                self.args,
                self.metadata,
                b"unused",
                now=lambda: NOW,
                archive_id=ARCHIVE_ID,
                upload=upload,
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].endswith(".manifest.json"))

    def test_marker_upload_failure_leaves_no_local_marker(self):
        calls = []

        def upload(**kwargs):
            calls.append(kwargs["destination"])
            if len(calls) == 2:
                raise producer.RehearsalExportError("upload", "object_create_failed")

        with self.assertRaisesRegex(producer.RehearsalExportError, "object_create_failed"):
            producer.publish_archive(
                self.args,
                self.metadata,
                b"unused",
                now=lambda: NOW,
                archive_id=ARCHIVE_ID,
                upload=upload,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(self.root.glob(".rehearsal-manifest-*")), [])


class ArchiveCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "live-source.sql.gz"
        self.args = SimpleNamespace(
            archive_output=str(self.output.resolve()),
            timeout_seconds=60,
            docker_binary="/usr/bin/docker",
            container="blueprint-local-postgres",
            host="172.22.0.1",
            port=5433,
            user="postgres",
            source_database="postgres",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_pg_dump_failure_removes_incomplete_archive_and_hides_password_from_argv(self):
        seen = {}

        class Process:
            def __init__(self):
                self.stdout = io.BytesIO(b"-- partial SQL")
                self.stderr = io.BytesIO(b"source disconnected")
                self.returncode = None

            def poll(self):
                if self.returncode is None:
                    self.returncode = 1
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.poll()

        def popen(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs["env"]
            return Process()

        password = b"not-in-process-arguments"
        with self.assertRaisesRegex(producer.RehearsalExportError, "pg_dump_failed"):
            producer.create_archive(self.args, password, popen=popen)
        self.assertNotIn(password.decode(), seen["argv"])
        self.assertEqual(seen["env"]["PGPASSWORD"], password.decode())
        self.assertEqual(
            seen["argv"][seen["argv"].index("--dbname") + 1],
            "postgres",
        )
        self.assertFalse(self.output.exists())


class ContainerUploadTests(unittest.TestCase):
    def test_upload_uses_pinned_container_and_create_only_precondition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sql.gz"
            credentials = root / "writer.json"
            source.write_bytes(b"archive")
            credentials.write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            run = Mock(return_value=completed)
            image_id = "sha256:" + "d" * 64

            producer._upload(
                docker_binary="/usr/bin/docker",
                uploader_image_id=image_id,
                control_plane_network="blueprint-control-plane",
                source=source,
                destination="gs://recovery/local-postgres-exports/a.sql.gz",
                credential_file=credentials,
                recovery_project_id="recovery-project-123",
                run=run,
            )

            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "/usr/bin/docker")
            self.assertIn(image_id, argv)
            self.assertIn("--if-generation-match=0", argv)
            self.assertNotIn("/usr/bin/gcloud", argv)
            self.assertIn("no-new-privileges", argv)
            self.assertIn("readonly", " ".join(argv))

    def test_security_sensitive_images_require_repository_digests(self):
        raw_id = "sha256:" + "d" * 64
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "uploader_image_not_immutable"
        ):
            producer._validate_uploader_image(
                "/usr/bin/docker", raw_id, run=Mock()
            )
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "postgres_image_not_official_digest"
        ):
            producer._validate_postgres_image(
                "/usr/bin/docker", raw_id, run=Mock()
            )


class ProxyBindingTests(unittest.TestCase):
    def setUp(self):
        self.connection = (
            "blueprint-orchestrator:us-central1:blueprint-cloudsql-1234"
        )
        self.base = [
            "/usr/local/bin/cloud-sql-proxy",
            "--address=172.22.0.1",
            "--port=5433",
        ]

    def test_exact_single_instance_owns_base_listener(self):
        self.assertEqual(
            producer._validated_proxy_listener(
                [*self.base, self.connection],
                connection_name=self.connection,
                expected_address="172.22.0.1",
                expected_port=5433,
            ),
            ("172.22.0.1", 5433),
        )

    def test_expected_instance_second_or_per_instance_port_is_rejected(self):
        other = "blueprint-orchestrator:us-central1:other-instance"
        for argv in (
            [*self.base, other, self.connection],
            [*self.base, f"{self.connection}?port=5434"],
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(
                producer.RehearsalExportError, "proxy_connection_mismatch"
            ):
                producer._validated_proxy_listener(
                    argv,
                    connection_name=self.connection,
                    expected_address="172.22.0.1",
                    expected_port=5433,
                )

    def test_duplicate_flags_and_address_mismatch_are_rejected(self):
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "duplicate_proxy_port_option"
        ):
            producer._validated_proxy_listener(
                [*self.base, "--port", "5433", self.connection],
                connection_name=self.connection,
                expected_address="172.22.0.1",
                expected_port=5433,
            )
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "proxy_listener_mismatch"
        ):
            producer._validated_proxy_listener(
                [*self.base, self.connection],
                connection_name=self.connection,
                expected_address="172.22.0.2",
                expected_port=5433,
            )


class OwnedClientTests(unittest.TestCase):
    def test_client_has_no_mounts_and_cleanup_proves_absence(self):
        image = "sha256:" + "a" * 64
        name = "blueprint-rehearsal-export-" + "b" * 32
        session = "b" * 32
        args = SimpleNamespace(
            docker_binary="/usr/bin/docker",
            postgres_image_id=image,
            control_plane_network="blueprint-control-plane",
        )
        absent = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: No such container"
        )
        payload = [
            {
                "Name": f"/{name}",
                "Image": image,
                "Config": {
                    "User": "999:999",
                    "Labels": {
                        producer._CLIENT_OWNER_LABEL: producer._CLIENT_OWNER_VALUE,
                        producer._CLIENT_SESSION_LABEL: session,
                    }
                },
                "HostConfig": {
                    "PortBindings": {},
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "NetworkMode": "blueprint-control-plane",
                    "PidsLimit": 64,
                    "Memory": 256 * 1024 * 1024,
                    "NanoCpus": 500_000_000,
                    "LogConfig": {"Type": "none", "Config": {}},
                    "Tmpfs": {
                        "/tmp": "rw,noexec,nosuid,nodev,size=64m"
                    },
                },
                "NetworkSettings": {
                    "Networks": {"blueprint-control-plane": {}}
                },
                "Mounts": [],
                "State": {"Running": True},
            }
        ]
        present = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload), stderr=""
        )
        success = subprocess.CompletedProcess([], 0, stdout=name, stderr="")
        run = Mock(side_effect=[absent, success, present])
        producer._start_client(args, name, session, run=run)
        start_argv = run.call_args_list[1].args[0]
        self.assertNotIn("--mount", start_argv)
        self.assertIn("--read-only", start_argv)
        self.assertIn("no-new-privileges", start_argv)

        run = Mock(side_effect=[present, success, absent])
        producer._remove_client(args, name, session, run=run)
        self.assertEqual(
            run.call_args_list[1].args[0][1:4],
            ["container", "rm", "--force"],
        )

    def test_cleanup_refuses_foreign_label(self):
        image = "sha256:" + "a" * 64
        name = "blueprint-rehearsal-export-" + "b" * 32
        payload = [
            {
                "Name": f"/{name}",
                "Image": image,
                "Config": {"Labels": {}},
            }
        ]
        run = Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(payload), stderr=""
            )
        )
        args = SimpleNamespace(
            docker_binary="/usr/bin/docker",
            postgres_image_id=image,
            control_plane_network="blueprint-control-plane",
        )
        with self.assertRaisesRegex(
            producer.RehearsalExportError, "refusing_unowned_client"
        ):
            producer._remove_client(args, name, "b" * 32, run=run)
        self.assertEqual(run.call_count, 1)


class SourceEvidenceTests(unittest.TestCase):
    def test_source_evidence_hash_binds_proxy_instance_and_planned_object(self):
        args = SimpleNamespace(
            source_project_id="blueprint-orchestrator",
            source_instance="blueprint-cloudsql-1234",
            source_database="postgres",
            host="172.22.0.1",
            port=5433,
            postgres_image_id="sha256:" + "a" * 64,
            postgres_image_ref="postgres@sha256:" + "c" * 64,
            uploader_image_id=(
                "gcr.io/google.com/cloudsdktool/cloud-sdk@sha256:" + "d" * 64
            ),
        )
        archive = {
            "path": Path(__file__).resolve(),
            "sha256": "b" * 64,
            "size_bytes": 42,
        }
        manifest = producer.completion_manifest(
            bucket=BUCKET,
            archive_name=(
                f"local-postgres-exports/20260730T120000Z-{ARCHIVE_ID}.sql.gz"
            ),
            archive_id=ARCHIVE_ID,
            sha256="b" * 64,
            size_bytes=42,
            created_at=NOW,
        )
        evidence = producer._source_evidence(
            args,
            instance={
                "sha256": "c" * 64,
                "connection_name": "blueprint-orchestrator:us-central1:blueprint-cloudsql-1234",
            },
            proxy={
                "executable_sha256": "d" * 64,
                "argv_sha256": "e" * 64,
                "connection_name": "blueprint-orchestrator:us-central1:blueprint-cloudsql-1234",
                "address": "172.22.0.1",
                "port": 5433,
            },
            archive=archive,
            planned_manifest=manifest,
        )
        self.assertEqual(evidence["archive"]["sha256"], "b" * 64)
        self.assertEqual(
            evidence["proxy"]["connection_name"],
            evidence["cloud_sql_instance"]["connection_name"],
        )
        self.assertEqual(
            len(evidence["archive"]["planned_manifest_sha256"]), 64
        )
        self.assertTrue(evidence["producer"]["postgres_image_ref"].startswith("postgres@"))
        self.assertTrue(
            evidence["producer"]["uploader_image_ref"].startswith(
                "gcr.io/google.com/cloudsdktool/cloud-sdk@"
            )
        )


if __name__ == "__main__":
    unittest.main()
