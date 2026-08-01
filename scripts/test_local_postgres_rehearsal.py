#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
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
    def test_session_files_are_owned_by_unprivileged_postgres(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(os, "name", "posix"),
                mock.patch.object(os, "chown") as chown,
            ):
                _session, data, password = rehearsal._create_session(root, "abc")

            self.assertEqual(
                chown.call_args_list,
                [
                    mock.call(
                        data,
                        rehearsal._POSTGRES_UID,
                        rehearsal._POSTGRES_GID,
                    ),
                    mock.call(
                        password,
                        rehearsal._POSTGRES_UID,
                        rehearsal._POSTGRES_GID,
                    ),
                ],
            )
            self.assertEqual(password.stat().st_mode & 0o777, 0o600)

    def test_target_runs_as_unprivileged_postgres_with_all_caps_dropped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            password = root / "password"
            run = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="id\n", stderr=""
                )
            )
            with mock.patch.object(rehearsal, "_container_payload", return_value=None):
                rehearsal._start_target(
                    "/usr/bin/docker",
                    "blueprint-precutover-rehearsal-abc-target",
                    "abc",
                    "sha256:" + "a" * 64,
                    data,
                    password,
                    run=run,
                )

            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--user") + 1], "999:999")
            self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
            self.assertEqual(
                argv[argv.index("--security-opt") + 1], "no-new-privileges"
            )

    def test_target_isolation_requires_unprivileged_postgres_user(self) -> None:
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
                        "User": "0:0",
                        "Labels": {
                            rehearsal._OWNER_LABEL: rehearsal._OWNER_VALUE,
                            rehearsal._SESSION_LABEL: "abc",
                        },
                    },
                    "HostConfig": {"NetworkMode": "none", "PortBindings": {}},
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

            with self.assertRaisesRegex(rehearsal.RehearsalError, "isolation_mismatch"):
                rehearsal._assert_target_isolation(
                    "/usr/bin/docker",
                    "blueprint-precutover-rehearsal-abc-target",
                    "abc",
                    "sha256:" + "a" * 64,
                    data,
                    password,
                    run=run,
                )

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

        owned = [
            {
                "Name": "/blueprint-precutover-rehearsal-abc-target",
                "Config": {
                    "Labels": {
                        rehearsal._OWNER_LABEL: rehearsal._OWNER_VALUE,
                        rehearsal._SESSION_LABEL: "abc",
                    }
                },
            }
        ]
        present = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(owned), stderr=""
        )
        removed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        absent = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: No such container"
        )
        run = mock.Mock(side_effect=[present, removed, absent])
        rehearsal._remove_owned_container(
            "/usr/bin/docker",
            "blueprint-precutover-rehearsal-abc-target",
            "abc",
            run=run,
        )
        self.assertEqual(
            run.call_args_list[1].args[0][1:],
            [
                "container",
                "rm",
                "--force",
                "--volumes",
                "blueprint-precutover-rehearsal-abc-target",
            ],
        )

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
                    "Tmpfs": rehearsal._SOURCE_CLIENT_TMPFS,
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

    def test_source_client_masks_postgres_image_volume_with_tmpfs(self) -> None:
        image_id = "sha256:" + "a" * 64
        run = mock.Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="Error: No such object"
                ),
                subprocess.CompletedProcess([], 0, stdout="container-id\n", stderr=""),
            ]
        )

        rehearsal._start_source_client(
            "/usr/bin/docker",
            "blueprint-precutover-rehearsal-abc-source",
            "abc",
            image_id,
            "blueprint-control-plane",
            run=run,
        )

        argv = run.call_args_list[1].args[0]
        tmpfs_values = [
            argv[index + 1]
            for index, value in enumerate(argv)
            if value == "--tmpfs"
        ]
        self.assertEqual(
            tmpfs_values,
            [
                f"/tmp:{rehearsal._SOURCE_CLIENT_TMPFS['/tmp']}",
                (
                    "/var/lib/postgresql/data:"
                    f"{rehearsal._SOURCE_CLIENT_TMPFS['/var/lib/postgresql/data']}"
                ),
            ],
        )

    def test_source_isolation_requires_exact_tmpfs_contract(self) -> None:
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
                    "Tmpfs": {"/tmp": rehearsal._SOURCE_CLIENT_TMPFS["/tmp"]},
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                },
                "NetworkSettings": {
                    "Networks": {"blueprint-control-plane": {}}
                },
                "Mounts": [],
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
            source_user="postgres",
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
        self.assertEqual(payload["source"]["owner"], "postgres")
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

    def test_success_verification_uses_snapshot_schema_keys(self) -> None:
        summary = rehearsal._verification_summary(
            {
                "alembic_revisions": ["0719"],
                "critical_aggregates": {"orphan_tasks": 0},
                "row_counts": {"task": 123},
            }
        )
        self.assertEqual(summary["alembic_revisions"], ["0719"])
        self.assertEqual(summary["critical_aggregates"]["orphan_tasks"], 0)
        self.assertEqual(summary["row_counts"]["task"], 123)

    def test_metadata_diff_is_keyed_and_does_not_emit_definition_values(self) -> None:
        source = {
            "public_columns": [
                {
                    "schema": "public",
                    "table": "task",
                    "name": "id",
                    "ordinal": 1,
                    "default": "nextval('task_id_seq'::regclass)",
                }
            ],
            "public_constraints": [],
            "public_indexes": [],
        }
        target = {
            **source,
            "public_columns": [
                {
                    **source["public_columns"][0],
                    "default": "nextval('public.task_id_seq'::regclass)",
                }
            ],
        }
        value = rehearsal._metadata_diff(
            source, target, source_owner="postgres", target_owner="blueprint"
        )
        change = value["public_columns"]["changes"][0]
        self.assertEqual(change["identity"]["name"], "id")
        self.assertEqual(change["changed_properties"], ["default"])
        self.assertNotIn("source_value", change)
        self.assertNotIn("target_value", change)
        self.assertNotEqual(change["source_sha256"], change["target_sha256"])

    def test_metadata_diff_rejects_ambiguous_inventory_identity(self) -> None:
        column = {
            "schema": "public",
            "table": "task",
            "name": "id",
            "ordinal": 1,
        }
        source = {
            "public_columns": [column, dict(column)],
            "public_constraints": [],
            "public_indexes": [],
        }
        with self.assertRaisesRegex(
            rehearsal.RehearsalError,
            "ambiguous_source_public_columns_identity",
        ):
            rehearsal._metadata_diff(
                source, source, source_owner="postgres", target_owner="blueprint"
            )

    def test_metadata_diff_distinguishes_missing_property_from_null(self) -> None:
        source = {
            "public_columns": [
                {"schema": "public", "table": "task", "name": "id", "default": None}
            ],
            "public_constraints": [],
            "public_indexes": [],
        }
        target = {
            "public_columns": [
                {"schema": "public", "table": "task", "name": "id"}
            ],
            "public_constraints": [],
            "public_indexes": [],
        }
        value = rehearsal._metadata_diff(
            source, target, source_owner="postgres", target_owner="blueprint"
        )
        self.assertEqual(
            value["public_columns"]["changes"][0]["changed_properties"],
            ["default"],
        )

    def test_artifact_replay_is_offline_isolated_and_cleans_up(self) -> None:
        snapshot = {
            "schema_version": cutover.SCHEMA_VERSION,
            "kind": cutover.SNAPSHOT_KIND,
            "captured_at": "2026-08-01T00:00:00+00:00",
            "instance": "blueprint-cloudsql-1234",
            "database": "postgres",
            **{field: [] for field in cutover._COMPARISON_FIELDS},
            "alembic_revisions": ["0719"],
            "unvalidated_constraints": [],
            "critical_aggregates": {
                "orphan_tasks": 0,
                "orphan_parent_tasks": 0,
                "orphan_artifacts": 0,
            },
        }
        target_snapshot = {**snapshot, "database": "blueprint"}
        args = SimpleNamespace(
            docker_binary="/usr/bin/docker",
            postgres_image_id="postgres:15@sha256:" + "a" * 64,
            work_root="/var/lib/blueprint/rehearsal",
            permanent_data_dir="/var/lib/blueprint/postgres",
            artifact="/evidence/source.dump",
            manifest="/evidence/manifest.json",
            source_snapshot="/evidence/source.json",
            source_owner="postgres",
            target_owner="blueprint",
            snapshot_output="/evidence/replay/target.json",
            diagnostic_output="/evidence/replay/diagnostic.json",
            expected_alembic_revision="0719",
            restore_timeout_seconds=60,
            target_start_timeout_seconds=60,
        )
        target = mock.Mock(database="blueprint")
        target.query_text.return_value = "15"
        created: list[str] = []

        def create_json(path: str, _value: object) -> Path:
            created.append(path)
            return Path(path)

        patches = [
            mock.patch.object(os, "geteuid", return_value=0, create=True),
            mock.patch.object(
                rehearsal, "_trusted_binary", return_value=args.docker_binary
            ),
            mock.patch.object(
                rehearsal, "_inspect_image", return_value="sha256:" + "a" * 64
            ),
            mock.patch.object(
                rehearsal,
                "validate_isolated_roots",
                return_value=(Path(args.work_root), Path(args.permanent_data_dir)),
            ),
            mock.patch.object(rehearsal, "_validate_replay_outputs"),
            mock.patch.object(
                rehearsal,
                "_validate_replay_inputs",
                return_value=(snapshot, "b" * 64, "c" * 64, 123, {}, "d" * 64),
            ),
            mock.patch.object(
                rehearsal,
                "_open_validated_replay_artifact",
                return_value=(123, Path(args.artifact)),
            ),
            mock.patch.object(
                rehearsal,
                "_create_session",
                return_value=(
                    Path("/session"),
                    Path("/session/data"),
                    Path("/session/password"),
                ),
            ),
            mock.patch.object(rehearsal, "_start_target"),
            mock.patch.object(rehearsal, "_wait_healthy"),
            mock.patch.object(rehearsal, "_assert_target_isolation"),
            mock.patch.object(rehearsal, "_create_replay_json", side_effect=create_json),
            mock.patch.object(rehearsal, "_remove_owned_container"),
            mock.patch.object(rehearsal, "_remove_session_dir"),
            mock.patch.object(rehearsal, "_start_source_client"),
            mock.patch.object(rehearsal, "_validate_cloud_sql_and_proxy"),
            mock.patch.object(cutover, "DockerPsql", return_value=target),
            mock.patch.object(cutover, "target_public_schema_empty"),
            mock.patch.object(
                cutover,
                "stream_restore",
                return_value={"stderr_sha256": "e" * 64},
            ),
            mock.patch.object(cutover, "snapshot_database", return_value=target_snapshot),
            mock.patch.object(cutover, "_file_sha256", return_value="f" * 64),
            mock.patch.object(os, "close"),
        ]
        with contextlib.ExitStack() as stack:
            active = [stack.enter_context(patch) for patch in patches]
            result = rehearsal.run_artifact_replay(args)
        self.assertEqual(result["outcome"], "match")
        self.assertEqual(created, [args.snapshot_output, args.diagnostic_output])
        active[14].assert_not_called()
        active[15].assert_not_called()
        active[12].assert_called_once()
        active[13].assert_called_once()
        active[18].assert_called_once()
        self.assertEqual(active[18].call_args.args[1], 123)

    def test_replay_artifact_digest_mismatch_closes_held_descriptor(self) -> None:
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_size=3)
        with (
            mock.patch.object(
                cutover,
                "_open_held_regular",
                return_value=(123, Path("/evidence/source.dump"), info),
            ),
            mock.patch.object(os, "read", side_effect=[b"abc", b""]),
            mock.patch.object(os, "close") as close,
        ):
            with self.assertRaisesRegex(
                rehearsal.RehearsalError, "artifact_digest_mismatch"
            ):
                rehearsal._open_validated_replay_artifact(
                    "/evidence/source.dump", "0" * 64, 3
                )
        close.assert_called_once_with(123)

    def test_replay_session_creation_failure_closes_artifact(self) -> None:
        args = SimpleNamespace(
            docker_binary="/usr/bin/docker",
            postgres_image_id="postgres:15@sha256:" + "a" * 64,
            work_root="/var/lib/blueprint/rehearsal",
            permanent_data_dir="/var/lib/blueprint/postgres",
            artifact="/evidence/source.dump",
            manifest="/evidence/manifest.json",
            source_snapshot="/evidence/source.json",
            source_owner="postgres",
            snapshot_output="/evidence/replay/target.json",
            diagnostic_output="/evidence/replay/diagnostic.json",
            expected_alembic_revision="0719",
            restore_timeout_seconds=60,
            target_start_timeout_seconds=60,
        )
        source = {"alembic_revisions": ["0719"]}
        with (
            mock.patch.object(os, "geteuid", return_value=0, create=True),
            mock.patch.object(
                rehearsal, "_trusted_binary", return_value=args.docker_binary
            ),
            mock.patch.object(
                rehearsal, "_inspect_image", return_value="sha256:" + "a" * 64
            ),
            mock.patch.object(
                rehearsal,
                "validate_isolated_roots",
                return_value=(Path(args.work_root), Path(args.permanent_data_dir)),
            ),
            mock.patch.object(rehearsal, "_validate_replay_outputs"),
            mock.patch.object(
                rehearsal,
                "_validate_replay_inputs",
                return_value=(source, "b" * 64, "c" * 64, 123, {}, "d" * 64),
            ),
            mock.patch.object(
                rehearsal,
                "_open_validated_replay_artifact",
                return_value=(123, Path(args.artifact)),
            ),
            mock.patch.object(
                rehearsal, "_create_session", side_effect=OSError("fixture")
            ),
            mock.patch.object(rehearsal, "_remove_owned_container") as remove,
            mock.patch.object(os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "fixture"):
                rehearsal.run_artifact_replay(args)
        close.assert_called_once_with(123)
        remove.assert_called_once()

    def test_replay_manifest_rejects_contradictory_provenance(self) -> None:
        source = {
            "instance": "blueprint-cloudsql-1234",
            "database": "postgres",
        }
        manifest = {
            "schema_version": rehearsal.SCHEMA_VERSION,
            "kind": rehearsal.MANIFEST_KIND,
            "purpose": "precutover_restore_rehearsal_only",
            "final_cutover_eligible": False,
            "source": {
                "instance": source["instance"],
                "database": source["database"],
            },
            "consistency": {
                "method": "single_exported_serializable_deferrable_snapshot",
                "production_write_fence_required": False,
            },
            "artifact_path": "/evidence/source.dump",
            "artifact": {
                "format": "postgres-custom",
                "sha256": "b" * 64,
                "size_bytes": 123,
            },
            "source_snapshot_sha256": "a" * 64,
            "pg_dump": {
                "major_version": 15,
                "client_version": "pg_dump (PostgreSQL) 15.13",
                "serializable_deferrable_snapshot_export": True,
                "no_owner": True,
                "no_acl": True,
                "stderr_sha256": "c" * 64,
                "stderr_bytes": 0,
            },
        }
        args = SimpleNamespace(
            manifest="/evidence/manifest.json",
            source_snapshot="/evidence/source.json",
            artifact="/evidence/source.dump",
            source_owner="postgres",
        )
        mutations = {
            "artifact_format": lambda value: value["artifact"].update(format="plain"),
            "consistency": lambda value: value["consistency"].update(method="none"),
            "write_fence": lambda value: value["consistency"].update(
                production_write_fence_required=True
            ),
            "snapshot_export": lambda value: value["pg_dump"].update(
                serializable_deferrable_snapshot_export=False
            ),
            "client_version": lambda value: value["pg_dump"].update(
                client_version="pg_dump (PostgreSQL) 16.1"
            ),
            "stderr_hash": lambda value: value["pg_dump"].update(
                stderr_sha256="invalid"
            ),
            "source_instance": lambda value: value["source"].update(
                instance="another-instance"
            ),
            "source_database": lambda value: value["source"].update(
                database="another-database"
            ),
            "source_owner": lambda value: value["source"].update(owner="other"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(manifest))
                mutate(changed)
                with (
                    mock.patch.object(
                        rehearsal,
                        "_read_root_json",
                        side_effect=[
                            (changed, Path(args.manifest), "d" * 64),
                            (source, Path(args.source_snapshot), "a" * 64),
                        ],
                    ),
                    mock.patch.object(cutover, "validate_snapshot"),
                ):
                    with self.assertRaisesRegex(
                        rehearsal.RehearsalError, "manifest_binding_mismatch"
                    ):
                        rehearsal._validate_replay_inputs(args)

    def test_comparison_failure_preserves_target_snapshot_before_cleanup(self) -> None:
        source_snapshot = {
            "alembic_revisions": ["revision123"],
            "critical_aggregates": {"orphan_tasks": 0},
            "row_counts": {"task": 1},
        }
        target_snapshot = {
            **source_snapshot,
            "row_counts": {"task": 2},
        }
        args = SimpleNamespace(
            docker_binary="/usr/bin/docker",
            postgres_image_id="sha256:" + "a" * 64,
            source_network="blueprint-control-plane",
            source_host="172.22.0.1",
            source_port=5433,
            source_database="postgres",
            source_user="postgres",
            source_password_file="/root/source-password",
            source_project_id="blueprint-orchestrator",
            source_instance="blueprint-cloudsql-1234",
            expected_proxy_address="172.22.0.1",
            expected_alembic_revision="revision123",
            work_root="/var/lib/blueprint/rehearsal",
            permanent_data_dir="/var/lib/blueprint/postgres",
            artifact_output="/evidence/source.dump",
            manifest_output="/evidence/manifest.json",
            source_snapshot_output="/evidence/source.json",
            snapshot_output="/evidence/target.json",
            result_output="/evidence/result.json",
            dump_timeout_seconds=60,
            restore_timeout_seconds=60,
            target_start_timeout_seconds=60,
        )
        keeper = mock.Mock(snapshot_id="00000003-0000001B-1")
        archive = {
            "path": Path(args.artifact_output),
            "sha256": "b" * 64,
            "size_bytes": 123,
            "client_version": "pg_dump (PostgreSQL) 15.13",
            "stderr_sha256": "c" * 64,
            "stderr_bytes": 0,
        }
        created: list[str] = []

        def create_json(path: str, _value: object) -> Path:
            created.append(path)
            return Path(path)

        patches = [
            mock.patch.object(os, "geteuid", return_value=0, create=True),
            mock.patch.object(rehearsal, "_trusted_binary", return_value=args.docker_binary),
            mock.patch.object(rehearsal, "_inspect_image", return_value=args.postgres_image_id),
            mock.patch.object(rehearsal, "_validate_network", return_value=args.source_host),
            mock.patch.object(
                rehearsal,
                "validate_isolated_roots",
                return_value=(Path(args.work_root), Path(args.permanent_data_dir)),
            ),
            mock.patch.object(rehearsal, "_validate_evidence_outputs"),
            mock.patch.object(rehearsal, "_validate_source_password"),
            mock.patch.object(
                rehearsal,
                "_validate_cloud_sql_and_proxy",
                return_value={"connection_name": "p:r:i"},
            ),
            mock.patch.object(
                rehearsal,
                "_create_session",
                return_value=(
                    Path("/session"),
                    Path("/session/data"),
                    Path("/session/password"),
                ),
            ),
            mock.patch.object(rehearsal, "_start_source_client"),
            mock.patch.object(rehearsal, "_assert_source_isolation"),
            mock.patch.object(rehearsal, "_start_target"),
            mock.patch.object(rehearsal, "_wait_healthy"),
            mock.patch.object(rehearsal, "_assert_target_isolation"),
            mock.patch.object(rehearsal, "_ExportedSnapshot", return_value=keeper),
            mock.patch.object(rehearsal, "_artifact_manifest", return_value={}),
            mock.patch.object(rehearsal, "_create_json", side_effect=create_json),
            mock.patch.object(rehearsal, "_remove_owned_container"),
            mock.patch.object(rehearsal, "_remove_session_dir"),
            mock.patch.object(cutover, "DockerPsql", return_value=mock.Mock(database="postgres")),
            mock.patch.object(
                cutover,
                "snapshot_database",
                side_effect=[source_snapshot, target_snapshot],
            ),
            mock.patch.object(cutover, "create_custom_dump", return_value=archive),
            mock.patch.object(cutover, "target_public_schema_empty"),
            mock.patch.object(
                cutover,
                "_open_held_regular",
                return_value=(123, Path(args.artifact_output), mock.Mock()),
            ),
            mock.patch.object(cutover, "stream_restore", return_value={"stderr_sha256": "d" * 64}),
            mock.patch.object(
                cutover,
                "compare_snapshots",
                side_effect=cutover.CutoverError(
                    "comparison", "mismatch_row_counts"
                ),
            ),
            mock.patch.object(os, "close"),
        ]
        with contextlib.ExitStack() as stack:
            active = [stack.enter_context(patch) for patch in patches]
            remove_container = active[17]
            with self.assertRaisesRegex(cutover.CutoverError, "mismatch_row_counts"):
                rehearsal.run_rehearsal(args)
        self.assertIn(args.snapshot_output, created)
        self.assertNotIn(args.result_output, created)
        self.assertEqual(remove_container.call_count, 3)

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
