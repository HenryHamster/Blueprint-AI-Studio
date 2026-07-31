#!/usr/bin/env python3
from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_postgres_cutover as cutover
import local_postgres_rehearsal as rehearsal


class _DumpClient:
    container = "blueprint-precutover-rehearsal-test-source"

    def __init__(self) -> None:
        self.options: tuple[str, ...] = ()

    def pg_tool_argv(self, tool: str, *options: str) -> list[str]:
        self.options = options
        return ["docker", "exec", self.container, tool, *options]

    def environment(self, *, app_name: str | None = None) -> dict[str, str]:
        return {"PGAPPNAME": app_name or ""}


class _DumpProcess:
    returncode = 0

    def __init__(self, argv, *, stdout, **_kwargs) -> None:
        self.argv = argv
        os.write(stdout, b"PGDMPconsistent")

    def communicate(self, *, timeout: int):
        return b"", b"notice"


class RehearsalTests(unittest.TestCase):
    def test_snapshot_keeper_forces_machine_readable_psql_output(self) -> None:
        snapshot_id = "00000003-0000001B-1"
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(snapshot_id + "\n")
        process.stderr = io.StringIO("")
        process.poll.return_value = None
        process.communicate.return_value = ("", "")
        client = mock.Mock(database="postgres")
        client.argv.return_value = ["/usr/bin/docker", "exec", "source", "psql"]
        popen = mock.Mock(return_value=process)

        keeper = rehearsal._ExportedSnapshot(client, popen=popen)
        self.assertEqual(keeper.snapshot_id, snapshot_id)
        argv = popen.call_args.args[0]
        self.assertIn("--tuples-only", argv)
        self.assertIn("--no-align", argv)
        keeper.close()

    def test_live_dump_requests_serializable_deferrable_snapshot(self) -> None:
        client = _DumpClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.dump"
            with mock.patch.object(
                cutover, "_pg15_version", return_value="pg_dump (PostgreSQL) 15.13"
            ):
                result = cutover.create_custom_dump(
                    client,
                    str(output),
                    timeout_seconds=60,
                    serializable_deferrable=True,
                    popen=_DumpProcess,
                )
        self.assertIn("--serializable-deferrable", client.options)
        self.assertEqual(result["size_bytes"], len(b"PGDMPconsistent"))

    def test_final_cutover_dump_default_does_not_change(self) -> None:
        client = _DumpClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.dump"
            with mock.patch.object(
                cutover, "_pg15_version", return_value="pg_dump (PostgreSQL) 15.13"
            ):
                cutover.create_custom_dump(
                    client,
                    str(output),
                    timeout_seconds=60,
                    popen=_DumpProcess,
                )
        self.assertNotIn("--serializable-deferrable", client.options)

    def test_work_root_must_not_overlap_permanent_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permanent = root / "postgres"
            permanent.mkdir()
            work = permanent / "rehearsals"
            work.mkdir()
            os.chmod(work, 0o700)
            with self.assertRaisesRegex(
                rehearsal.RehearsalError, "work_root_overlaps_permanent_data"
            ):
                rehearsal.validate_isolated_roots(str(work), str(permanent))

    def test_evidence_must_not_be_written_under_permanent_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permanent = root / "postgres"
            permanent.mkdir()
            os.chmod(permanent, 0o700)
            args = SimpleNamespace(
                artifact_output=str(permanent / "source.dump"),
                manifest_output=str(permanent / "manifest.json"),
                source_snapshot_output=str(permanent / "source-snapshot.json"),
                snapshot_output=str(permanent / "snapshot.json"),
                result_output=str(permanent / "result.json"),
            )
            with self.assertRaisesRegex(
                rehearsal.RehearsalError, "evidence_overlaps_permanent_data"
            ):
                rehearsal._validate_evidence_outputs(args, permanent)

    def test_cleanup_refuses_foreign_or_permanent_container(self) -> None:
        with self.assertRaisesRegex(
            rehearsal.RehearsalError, "refusing_permanent_container"
        ):
            rehearsal._remove_owned_container(
                "/usr/bin/docker", "blueprint-local-postgres", "abc", run=mock.Mock()
            )

        payload = [
            {
                "Name": "/blueprint-precutover-rehearsal-abc-target",
                "Config": {"Labels": {rehearsal._OWNER_LABEL: "someone-else"}},
            }
        ]
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(payload), stderr=""
            )
        )
        with self.assertRaisesRegex(
            rehearsal.RehearsalError, "refusing_unowned_container"
        ):
            rehearsal._remove_owned_container(
                "/usr/bin/docker",
                "blueprint-precutover-rehearsal-abc-target",
                "abc",
                run=run,
            )
        self.assertEqual(run.call_count, 1)

    def test_target_isolation_rejects_network_or_port_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            password = root / "password"
            data.mkdir()
            password.write_text("secret", encoding="utf-8")
            payload = [
                {
                    "Name": "/blueprint-precutover-rehearsal-abc-target",
                    "Image": "sha256:" + "a" * 64,
                    "Config": {
                        "Labels": {
                            rehearsal._OWNER_LABEL: rehearsal._OWNER_VALUE,
                            rehearsal._SESSION_LABEL: "abc",
                        }
                    },
                    "HostConfig": {
                        "NetworkMode": "bridge",
                        "PortBindings": {"5432/tcp": [{"HostPort": "5432"}]},
                    },
                    "Mounts": [
                        {
                            "Destination": "/var/lib/postgresql/data",
                            "Source": str(data),
                            "RW": True,
                        },
                        {
                            "Destination": "/run/secrets/postgres_password",
                            "Source": str(password),
                            "RW": False,
                        },
                    ],
                }
            ]
            run = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(payload), stderr=""
                )
            )
            with self.assertRaisesRegex(
                rehearsal.RehearsalError, "isolation_mismatch"
            ):
                rehearsal._assert_target_isolation(
                    "/usr/bin/docker",
                    "blueprint-precutover-rehearsal-abc-target",
                    "abc",
                    "sha256:" + "a" * 64,
                    data,
                    password,
                    run=run,
                )

    def test_source_isolation_rejects_mounts(self) -> None:
        image_id = "sha256:" + "a" * 64
        payload = [
            {
                "Name": "/blueprint-precutover-rehearsal-abc-source",
                "Image": image_id,
                "Config": {
                    "Labels": {
                        rehearsal._OWNER_LABEL: rehearsal._OWNER_VALUE,
                        rehearsal._SESSION_LABEL: "abc",
                    }
                },
                "HostConfig": {
                    "PortBindings": {},
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                },
                "NetworkSettings": {
                    "Networks": {"blueprint-control-plane": {}}
                },
                "Mounts": [{"Destination": "/host", "Source": "/"}],
            }
        ]
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(payload), stderr=""
            )
        )
        with self.assertRaisesRegex(rehearsal.RehearsalError, "isolation_mismatch"):
            rehearsal._assert_source_isolation(
                "/usr/bin/docker",
                "blueprint-precutover-rehearsal-abc-source",
                "abc",
                image_id,
                "blueprint-control-plane",
                run=run,
            )

    def test_manifest_is_explicitly_not_final_cutover_evidence(self) -> None:
        args = SimpleNamespace(
            source_project_id="blueprint-orchestrator",
            source_instance="blueprint-cloudsql-1234",
            source_database="postgres",
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "source-snapshot.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            payload = rehearsal._artifact_manifest(
                args,
                {
                    "path": Path("/evidence/source.dump"),
                    "sha256": "a" * 64,
                    "size_bytes": 123,
                    "client_version": "pg_dump (PostgreSQL) 15.13",
                    "stderr_sha256": "b" * 64,
                    "stderr_bytes": 0,
                },
                snapshot_path,
                {"connection_name": "project:region:instance"},
            )
        self.assertFalse(payload["final_cutover_eligible"])
        self.assertEqual(
            payload["consistency"]["method"],
            "single_exported_serializable_deferrable_snapshot",
        )

    def test_existing_evidence_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "manifest.json"
            existing.write_text("old evidence", encoding="utf-8")
            with self.assertRaisesRegex(
                rehearsal.RehearsalError, "evidence_create_failed"
            ):
                rehearsal._create_json(str(existing), {"replacement": True})
            self.assertEqual(existing.read_text(encoding="utf-8"), "old evidence")

    def test_snapshot_dump_flag_is_exact_and_conflicts_with_deferrable(self) -> None:
        client = _DumpClient()
        snapshot_id = "00000003-0000001B-1"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.dump"
            with mock.patch.object(
                cutover, "_pg15_version", return_value="pg_dump (PostgreSQL) 15.13"
            ):
                cutover.create_custom_dump(
                    client,
                    str(output),
                    timeout_seconds=60,
                    snapshot_id=snapshot_id,
                    popen=_DumpProcess,
                )
        self.assertIn(f"--snapshot={snapshot_id}", client.options)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                cutover, "_pg15_version", return_value="pg_dump (PostgreSQL) 15.13"
            ):
                with self.assertRaisesRegex(
                    cutover.CutoverError, "conflicting_dump_snapshot_options"
                ):
                    cutover.create_custom_dump(
                        client,
                        str(Path(directory) / "source.dump"),
                        timeout_seconds=60,
                        snapshot_id=snapshot_id,
                        serializable_deferrable=True,
                        popen=_DumpProcess,
                    )

    def test_live_comparison_can_exclude_non_mvcc_sequence_state(self) -> None:
        source = {field: [] for field in cutover._COMPARISON_FIELDS}
        target = {field: [] for field in cutover._COMPARISON_FIELDS}
        source["sequences"] = [{"last_value": 10}]
        target["sequences"] = [{"last_value": 11}]
        with mock.patch.object(cutover, "validate_snapshot"):
            cutover.compare_snapshots(source, target, compare_sequences=False)
            with self.assertRaisesRegex(cutover.CutoverError, "mismatch_sequences"):
                cutover.compare_snapshots(source, target)

    def test_pg15_preflight_uses_trusted_client_docker_binary(self) -> None:
        client = mock.Mock(
            docker_binary="/usr/bin/docker",
            container="blueprint-precutover-rehearsal-source",
        )
        result = subprocess.CompletedProcess(
            [], 0, stdout="pg_dump (PostgreSQL) 15.13\n", stderr=""
        )
        with mock.patch.object(subprocess, "run", return_value=result) as run:
            cutover._pg15_version(client, "pg_dump")
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/docker")

    def test_snapshot_close_failure_still_cleans_containers_and_preserves_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            data = session / "postgres"
            password = session / "password"
            session.mkdir()
            data.mkdir()
            password.write_text("generated", encoding="utf-8")
            args = SimpleNamespace(
                docker_binary="/usr/bin/docker",
                postgres_image_id="sha256:" + "a" * 64,
                source_network="blueprint-control-plane",
                work_root=str(root),
                permanent_data_dir=str(root / "permanent"),
                source_password_file=str(root / "source-password"),
                source_project_id="blueprint-orchestrator",
                source_instance="blueprint-cloudsql-1234",
                expected_alembic_revision="revision123",
                source_database="postgres",
                source_user="postgres",
                source_host="172.22.0.1",
                source_port=5433,
                expected_proxy_address="172.22.0.1",
            )
            keeper = mock.Mock(snapshot_id="00000003-0000001B-1")
            keeper.close.side_effect = OSError("snapshot close failed")
            with (
                mock.patch.object(os, "geteuid", return_value=0, create=True),
                mock.patch.object(rehearsal, "_trusted_binary", return_value="/usr/bin/docker"),
                mock.patch.object(rehearsal, "_inspect_image", return_value=args.postgres_image_id),
                mock.patch.object(
                    rehearsal, "_validate_network", return_value="172.22.0.1"
                ),
                mock.patch.object(rehearsal, "validate_isolated_roots", return_value=(root, root / "permanent")),
                mock.patch.object(rehearsal, "_validate_evidence_outputs"),
                mock.patch.object(rehearsal, "_validate_source_password"),
                mock.patch.object(rehearsal, "_validate_cloud_sql_and_proxy", return_value={"connection_name": "p:r:i"}),
                mock.patch.object(rehearsal, "_create_session", return_value=(session, data, password)),
                mock.patch.object(rehearsal, "_start_source_client"),
                mock.patch.object(rehearsal, "_assert_source_isolation"),
                mock.patch.object(cutover, "DockerPsql", return_value=mock.Mock(database="postgres")),
                mock.patch.object(rehearsal, "_ExportedSnapshot", return_value=keeper),
                mock.patch.object(cutover, "snapshot_database", side_effect=cutover.CutoverError("database_snapshot", "failed")),
                mock.patch.object(rehearsal, "_remove_owned_container") as remove_container,
                mock.patch.object(rehearsal, "_remove_session_dir") as remove_session,
            ):
                with self.assertRaisesRegex(
                    rehearsal.RehearsalError, "cleanup:incomplete_cleanup"
                ):
                    rehearsal.run_rehearsal(args)
            keeper.close.assert_called_once()
            self.assertEqual(remove_container.call_count, 2)
            remove_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
