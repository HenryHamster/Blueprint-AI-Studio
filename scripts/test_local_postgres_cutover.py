#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_postgres_cutover as cutover


class FakeSnapshotClient:
    database = "blueprint"

    def query_json(self, sql: str):
        if sql == cutover._TABLES_SQL:
            return [
                {"schema": "public", "name": name, "kind": "r", "is_partition": False}
                for name in ("alembic_version", "alloweduser", "artifact", "project", "task")
            ]
        if "jsonb_agg(version_num" in sql:
            return ["0711"]
        if sql == cutover._COLUMNS_SQL:
            return [{"schema": "public", "table": "task", "name": "id", "ordinal": 1}]
        if sql == cutover._CONSTRAINTS_SQL:
            return [
                {
                    "schema": "public",
                    "table": "task",
                    "name": "task_pkey",
                    "type": "p",
                    "deferrable": False,
                    "initially_deferred": False,
                    "validated": True,
                    "definition": "PRIMARY KEY (id)",
                }
            ]
        if sql == cutover._INDEXES_SQL:
            return [
                {
                    "schema": "public",
                    "table": "task",
                    "name": "task_pkey",
                    "definition": (
                        "CREATE UNIQUE INDEX task_pkey "
                        "ON public.task USING btree (id)"
                    ),
                    "owner": "postgres",
                    "acl": [],
                    "valid": True,
                    "ready": True,
                    "live": True,
                }
            ]
        if sql == cutover._TRIGGERS_SQL:
            return []
        if sql == cutover._VIEWS_SQL:
            return [{"schema": "public", "name": "task_view", "kind": "v", "owner": "postgres", "acl": [], "options": [], "definition": " SELECT task.id FROM task;"}]
        if sql == cutover._ROUTINES_SQL:
            return [{"schema": "public", "name": "protect_task", "identity_arguments": "", "kind": "f", "owner": "postgres", "acl": [], "definition": "CREATE FUNCTION protect_task() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;"}]
        if sql == cutover._TYPES_SQL:
            return [{"schema": "public", "name": "taskstatus", "kind": "e", "owner": "postgres", "acl": [], "enum_labels": ["PENDING", "COMPLETED"]}]
        if sql == cutover._EXTENSIONS_SQL:
            return [{"name": "pgcrypto", "version": "1.3", "schema": "public", "owner": "postgres", "relocatable": True}]
        if sql == cutover._CRITICAL_SQL:
            return {
                "alloweduser_count": 2,
                "project_by_status": {"DEPLOYING": 1},
                "task_by_status": {"COMPLETED": 3},
                "artifact_by_type": {"PULL_REQUEST": 1},
                "orphan_tasks": 0,
                "orphan_parent_tasks": 0,
                "orphan_artifacts": 0,
            }
        if sql == cutover._SEQUENCE_OWNERS_SQL:
            return []
        if "count(*)" in sql:
            return 1
        raise AssertionError(sql)


def snapshot(captured_at: str = "2026-07-30T10:03:00Z") -> dict:
    value = cutover.snapshot_database(
        FakeSnapshotClient(), label="source-frozen", instance="blueprint-cloudsql-1234"
    )
    value["captured_at"] = captured_at
    return value


def _raw(directory: Path, name: str, value) -> dict:
    path = directory / f"{name}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return {
        "kind": name,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def fence(directory: Path) -> dict:
    raw = [
        _raw(
            directory,
            "cloud_run_fence",
            {
                "schema_version": 1,
                "kind": "cloud-run-write-fence",
                "request": {
                    "project_id": "blueprint-orchestrator",
                    "region": "us-central1",
                    "service": "blueprint-orchestrator",
                },
                "project": {
                    "projectId": "blueprint-orchestrator",
                    "projectNumber": "131312485296",
                },
                "service": {
                    "metadata": {
                        "name": "blueprint-orchestrator",
                        "namespace": "131312485296",
                        "labels": {"cloud.googleapis.com/location": "us-central1"},
                    }
                },
                "policy": {"bindings": [], "etag": "BwY"},
            },
        ),
        _raw(
            directory,
            "cloud_sql_instance",
            {
                "name": "blueprint-cloudsql-1234",
                "databaseVersion": "POSTGRES_15",
                "region": "us-central1",
                "connectionName": "blueprint-orchestrator:us-central1:blueprint-cloudsql-1234",
            },
        ),
        _raw(
            directory,
            "password_rotation_operation",
            {
                "status": "DONE",
                "operationType": "UPDATE_USER",
                "targetId": "blueprint-cloudsql-1234",
                "targetProject": "blueprint-orchestrator",
            },
        ),
    ]
    observations = []
    for index in range(3):
        item = _raw(directory, f"session_{index}", [])
        item.pop("kind")
        observations.append(
            {
                **item,
                "captured_at": f"2026-07-30T10:0{index + 1}:00Z",
                "old_session_count": 0,
            }
        )
    return {
        "schema_version": 1,
        "kind": cutover.FENCE_KIND,
        "source": {
            "project_id": "blueprint-orchestrator",
            "instance": "blueprint-cloudsql-1234",
            "database": "blueprint",
            "cloud_run_service": "blueprint-orchestrator",
            "cloud_run_region": "us-central1",
        },
        "password_rotated": True,
        "old_sessions_terminated": True,
        "raw_evidence": raw,
        "session_observations": observations,
    }


def manifest(artifact: Path, source_sha: str, fence_sha: str) -> dict:
    return {
        "schema_version": 1,
        "kind": cutover.MANIFEST_KIND,
        "created_at": "2026-07-30T10:05:00Z",
        "source": {
            "project_id": "blueprint-orchestrator",
            "instance": "blueprint-cloudsql-1234",
            "database": "blueprint",
            "cloud_run_service": "blueprint-orchestrator",
            "cloud_run_region": "us-central1",
        },
        "source_snapshot_sha256": source_sha,
        "source_fence_sha256": fence_sha,
        "live_session_observations": [
            {
                "label": "before_snapshot",
                "captured_at": "2026-07-30T10:02:30Z",
                "session_count": 0,
                "sha256": "a" * 64,
            },
            {
                "label": "after_dump",
                "captured_at": "2026-07-30T10:04:00Z",
                "session_count": 0,
                "sha256": "b" * 64,
            },
        ],
        "artifact_path": str(artifact),
        "artifact": {
            "size_bytes": artifact.stat().st_size,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        },
        "pg_dump": {"format": "custom", "major_version": 15},
    }


class CutoverTests(unittest.TestCase):
    def test_cloud_run_capture_binds_request_service_and_policy(self):
        responses = [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "projectId": "blueprint-orchestrator",
                        "projectNumber": "131312485296",
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "metadata": {
                            "name": "blueprint-orchestrator",
                            "namespace": "131312485296",
                            "labels": {"cloud.googleapis.com/location": "us-central1"},
                        }
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"bindings": [], "etag": "BwY"}),
                stderr="",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cloud-run-fence.json"
            args = SimpleNamespace(
                project_id="blueprint-orchestrator",
                region="us-central1",
                service="blueprint-orchestrator",
                output=str(output),
                gcloud_executable="gcloud",
            )
            run = mock.Mock(side_effect=responses)
            payload = cutover.capture_cloud_run_fence(args, run=run)
            self.assertEqual(payload["request"]["service"], "blueprint-orchestrator")
            self.assertEqual(payload["policy"]["bindings"], [])
            self.assertEqual(run.call_args_list[2].args[0][3], "get-iam-policy")

    def test_password_is_forwarded_without_argv_value(self):
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory) / "password"
            password.write_text("top-secret", encoding="utf-8")
            os.chmod(password, 0o600)
            client = cutover.DockerPsql(
                container="postgres", database="blueprint", user="blueprint", password_file=str(password)
            )
            argv = client.argv(sql="SELECT 1")
            self.assertNotIn("top-secret", " ".join(argv))
            self.assertIn("PGPASSWORD", argv)
            self.assertEqual(client.environment()["PGPASSWORD"], "top-secret")

    def test_restore_argv_is_single_transaction_and_shell_free(self):
        client = cutover.DockerPsql(container="postgres", database="blueprint", user="blueprint")
        argv = client.pg_tool_argv(
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
        )
        self.assertEqual(argv[:3], ["docker", "exec", "--interactive"])
        self.assertIn("--single-transaction", argv)
        self.assertIn("--exit-on-error", argv)
        self.assertIn("pg_restore", argv)
        self.assertNotIn("sh", argv)
        self.assertNotIn("-c", argv)

    def test_source_proxy_host_and_port_are_plain_argv(self):
        client = cutover.DockerPsql(
            container="postgres",
            database="blueprint",
            user="postgres",
            host="172.22.0.1",
            port=5433,
        )
        argv = client.argv(sql="SELECT 1")
        self.assertEqual(argv[argv.index("--host") + 1], "172.22.0.1")
        self.assertEqual(argv[argv.index("--port") + 1], "5433")
        with self.assertRaisesRegex(cutover.CutoverError, "host_and_port_required_together"):
            cutover.DockerPsql(
                container="postgres", database="blueprint", user="postgres", host="172.22.0.1"
            )
        observed = client.argv(
            sql="SELECT 1", app_name="blueprint-cutover-observer"
        )
        self.assertIn("PGAPPNAME", observed)
        self.assertNotIn("blueprint-cutover-observer", observed)

    def test_snapshot_captures_required_evidence(self):
        value = snapshot()
        self.assertEqual(value["alembic_revisions"], ["0711"])
        self.assertEqual(set(value["row_counts"]), {"alembic_version", "alloweduser", "artifact", "project", "task"})
        self.assertEqual(value["unvalidated_constraints"], [])
        self.assertEqual(value["public_constraints"][0]["definition"], "PRIMARY KEY (id)")
        self.assertEqual(value["public_indexes"][0]["name"], "task_pkey")
        self.assertTrue(value["public_indexes"][0]["valid"])
        self.assertEqual(value["critical_aggregates"]["orphan_tasks"], 0)

    def test_snapshot_rejects_unhealthy_or_malformed_index_state(self):
        missing = object()

        class UnhealthyIndexClient(FakeSnapshotClient):
            def __init__(self, field, field_value):
                self.field = field
                self.field_value = field_value

            def query_json(self, sql: str):
                value = super().query_json(sql)
                if sql == cutover._INDEXES_SQL:
                    if self.field_value is missing:
                        value[0].pop(self.field)
                    else:
                        value[0][self.field] = self.field_value
                return value

        cases = (
            ("valid", False, "unhealthy_indexes_present"),
            ("ready", False, "unhealthy_indexes_present"),
            ("live", False, "unhealthy_indexes_present"),
            ("valid", None, "invalid_index_inventory"),
            ("ready", "false", "invalid_index_inventory"),
            ("live", missing, "invalid_index_inventory"),
        )
        for field, field_value, expected_error in cases:
            with self.subTest(field=field, field_value=field_value):
                with self.assertRaisesRegex(cutover.CutoverError, expected_error):
                    cutover.snapshot_database(
                        UnhealthyIndexClient(field, field_value),
                        label="source-frozen",
                        instance="blueprint-cloudsql-1234",
                    )

    def test_target_must_be_exactly_empty(self):
        client = mock.Mock()
        client.query_json.return_value = {
            "exists": True,
            "relations": [{"name": "alembic_version", "kind": "r"}],
            "routines": [],
            "user_types": [],
        }
        with self.assertRaisesRegex(cutover.CutoverError, "public_schema_not_empty"):
            cutover.target_public_schema_empty(client)

    def test_validates_fence_manifest_and_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "final.dump"
            artifact.write_bytes(b"PGDMPfixture")
            source = snapshot()
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
            proof = fence(root)
            fence_path = root / "fence.json"
            fence_path.write_text(json.dumps(proof), encoding="utf-8")
            fence_sha = hashlib.sha256(fence_path.read_bytes()).hexdigest()
            result = cutover.validate_direct_manifest(
                source_snapshot=source,
                source_snapshot_sha256=source_sha,
                fence=proof,
                fence_sha256=fence_sha,
                manifest=manifest(artifact, source_sha, fence_sha),
            )
            try:
                self.assertEqual(
                    result["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest()
                )
            finally:
                os.close(result["fd"])

    def test_fence_rejects_active_source_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = fence(Path(directory))
            proof["session_observations"][1]["old_session_count"] = 1
            with self.assertRaisesRegex(cutover.CutoverError, "old_sessions_present"):
                cutover.validate_durable_fence(proof)

    def test_fence_rejects_public_invoker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof = fence(root)
            item = next(
                item
                for item in proof["raw_evidence"]
                if item["kind"] == "cloud_run_fence"
            )
            path = Path(item["path"])
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["policy"] = {
                "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(cutover.CutoverError, "cloud_run_public_invoker_enabled"):
                cutover.validate_durable_fence(proof)

    def test_fence_rejects_malformed_cloud_run_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = fence(Path(directory))
            item = next(
                item
                for item in proof["raw_evidence"]
                if item["kind"] == "cloud_run_fence"
            )
            path = Path(item["path"])
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["policy"] = None
            path.write_text(json.dumps(raw), encoding="utf-8")
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(cutover.CutoverError, "cloud_run_identity_mismatch"):
                cutover.validate_durable_fence(proof)

    def test_manifest_rejects_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "final.dump"
            artifact.write_bytes(b"PGDMPfixture")
            source = snapshot()
            proof = fence(root)
            fence_sha = "e" * 64
            broken = manifest(artifact, "c" * 64, fence_sha)
            broken["artifact"]["sha256"] = "d" * 64
            with self.assertRaisesRegex(cutover.CutoverError, "artifact_integrity_mismatch"):
                cutover.validate_direct_manifest(
                    source_snapshot=source,
                    source_snapshot_sha256="c" * 64,
                    fence=proof,
                    fence_sha256=fence_sha,
                    manifest=broken,
                )

    def test_artifact_fd_is_held_and_rewound(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "final.dump"
            artifact.write_bytes(b"PGDMPfixture")
            descriptor, _path, _info = cutover._open_held_regular(
                str(artifact), "artifact"
            )
            try:
                digest, size = cutover._fd_sha256(descriptor)
                self.assertEqual(size, len(b"PGDMPfixture"))
                self.assertEqual(
                    digest, hashlib.sha256(b"PGDMPfixture").hexdigest()
                )
                self.assertEqual(os.read(descriptor, 5), b"PGDMP")
            finally:
                os.close(descriptor)

    def test_compare_is_exact(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        target["row_counts"]["task"] += 1
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_row_counts"):
            cutover.compare_snapshots(
                source, target, source_owner="postgres", target_owner="postgres"
            )

    def test_compare_requires_exact_constraint_definition(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        target["public_constraints"][0]["definition"] = "PRIMARY KEY (id) DEFERRABLE"
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target)

    def test_compare_densifies_only_column_ordinal_holes(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_columns"] = [
            {"schema": "public", "table": "task", "name": "id", "ordinal": 1},
            {
                "schema": "public",
                "table": "task",
                "name": "status",
                "ordinal": 3,
            },
        ]
        target["public_columns"] = json.loads(json.dumps(source["public_columns"]))
        target["public_columns"][1]["ordinal"] = 2

        cutover.compare_snapshots(source, target, target_owner="postgres")

        target["public_columns"].reverse()
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_columns"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_preserves_nonordinal_column_differences(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_columns"][0]["ordinal"] = 2
        source["public_columns"][0]["data_type"] = "uuid"
        target["public_columns"][0]["ordinal"] = 1
        target["public_columns"][0]["data_type"] = "text"

        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_columns"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_normalizes_pg15_text_array_constraint_deparse(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_constraints"][0]["definition"] = (
            "CHECK (status::text = ANY (ARRAY['queued'::character varying, "
            "'running'::character varying]::text[]))"
        )
        target["public_constraints"][0]["definition"] = (
            "CHECK (status::text = ANY (ARRAY['queued'::character varying::text, "
            "'running'::character varying::text]))"
        )

        cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_normalizes_pg15_text_array_index_deparse(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_indexes"][0]["definition"] = (
            "CREATE INDEX task_pkey ON public.task USING btree (status) "
            "WHERE ((status)::text = ANY ((ARRAY['QUEUED'::character varying, "
            "'IN_PROGRESS'::character varying])::text[]))"
        )
        target["public_indexes"][0]["definition"] = (
            "CREATE INDEX task_pkey ON public.task USING btree (status) "
            "WHERE ((status)::text = ANY (ARRAY[('QUEUED'::character varying)::text, "
            "('IN_PROGRESS'::character varying)::text]))"
        )

        cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_pg15_deparse_normalization_rejects_semantic_changes(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_constraints"][0]["definition"] = (
            "CHECK (status::text = ANY (ARRAY['queued'::character varying, "
            "'running'::character varying]::text[]))"
        )
        target["public_constraints"][0]["definition"] = (
            "CHECK (status::text = ANY (ARRAY['queued'::character varying::text, "
            "'failed'::character varying::text]))"
        )

        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

        target["public_constraints"][0]["definition"] = (
            "CHECK (status::text = ANY (ARRAY[lower('queued')::text, "
            "'running'::character varying::text]))"
        )
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_does_not_change_an_uncast_varchar_array_type(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_constraints"][0]["definition"] = (
            "CHECK (pg_typeof(ARRAY['a'::character varying]) = "
            "'character varying[]'::regtype)"
        )
        target["public_constraints"][0]["definition"] = (
            "CHECK (pg_typeof(ARRAY['a'::text]) = 'character varying[]'::regtype)"
        )

        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_pg15_deparse_normalization_ignores_quoted_sql_text(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_constraints"][0]["definition"] = (
            'CHECK ("ARRAY[\'queued\'::character varying]" IS NOT NULL)'
        )
        target["public_constraints"][0]["definition"] = (
            'CHECK ("ARRAY[\'queued\'::text]" IS NOT NULL)'
        )

        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

        source["public_constraints"][0]["definition"] = (
            "CHECK ('ARRAY[''queued''::character varying]' IS NOT NULL)"
        )
        target["public_constraints"][0]["definition"] = (
            "CHECK ('ARRAY[''queued''::text]' IS NOT NULL)"
        )
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_constraints"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_rejects_malformed_definition_inventory(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        del source["public_constraints"][0]["definition"]
        target["public_constraints"][0]["definition"] = None

        with self.assertRaisesRegex(
            cutover.CutoverError, "invalid_source_public_constraints"
        ):
            cutover.compare_snapshots(source, target, target_owner="postgres")

        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_indexes"][0]["definition"] = None
        target["public_indexes"][0]["definition"] = 7
        with self.assertRaisesRegex(cutover.CutoverError, "invalid_source_public_indexes"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_normalizes_database_and_cutover_owner(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["database"] = "postgres"
        target["database"] = "blueprint"
        for field in (
            "public_indexes",
            "public_views",
            "public_routines",
            "public_types",
            "extensions",
        ):
            target[field][0]["owner"] = "blueprint"
        cutover.compare_snapshots(
            source,
            target,
            source_owner="postgres",
            target_owner="blueprint",
        )

    def test_compare_does_not_normalize_owner_words_in_definitions(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        source["public_views"][0]["definition"] = "SELECT 'postgres'"
        target["public_views"][0]["definition"] = "SELECT 'blueprint'"
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_views"):
            cutover.compare_snapshots(source, target, target_owner="postgres")

    def test_compare_normalizes_only_acl_principal_and_grantor(self):
        source = snapshot()
        target = json.loads(json.dumps(source))
        for field in (
            "public_indexes",
            "public_views",
            "public_routines",
            "public_types",
            "extensions",
        ):
            for item in target[field]:
                item["owner"] = "blueprint"
        source["public_views"][0]["acl"] = ["postgres=r/postgres"]
        target["public_views"][0]["acl"] = ["blueprint=r/blueprint"]
        cutover.compare_snapshots(source, target)
        target["public_views"][0]["acl"] = ["blueprint=w/blueprint"]
        with self.assertRaisesRegex(cutover.CutoverError, "mismatch_public_views"):
            cutover.compare_snapshots(source, target)

    def test_foreign_session_query_excludes_only_exact_observer(self):
        self.assertIn(
            "application_name <> 'blueprint-cutover-observer'",
            cutover._FOREIGN_SESSIONS_SQL,
        )
        self.assertNotIn("NOT LIKE", cutover._FOREIGN_SESSIONS_SQL)

    def test_fence_requires_meaningful_observation_window(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = fence(Path(directory))
            proof["session_observations"][0]["captured_at"] = "2026-07-30T10:00:00Z"
            proof["session_observations"][1]["captured_at"] = "2026-07-30T10:00:01Z"
            proof["session_observations"][2]["captured_at"] = "2026-07-30T10:00:02Z"
            with self.assertRaisesRegex(
                cutover.CutoverError, "session_observations_too_close"
            ):
                cutover.validate_durable_fence(proof)

    def test_sequence_rejects_uncalled_value_equal_to_max(self):
        client = mock.Mock()
        client.query_json.side_effect = [
            [
                {
                    "sequence_schema": "public",
                    "sequence": "task_id_seq",
                    "table_schema": "public",
                    "table": "task",
                    "column": "id",
                    "increment": 1,
                }
            ],
            {"last_value": 4, "is_called": False},
            4,
        ]
        with self.assertRaisesRegex(
            cutover.CutoverError, "sequence_behind_owned_column"
        ):
            cutover._sequence_snapshot(client)

    def test_dump_source_uses_pg15_custom_format(self):
        class Process:
            returncode = 0

            def __init__(self, **kwargs):
                os.write(kwargs["stdout"], b"PGDMPfixture")

            def communicate(self, timeout):
                return b"", b"notice"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.dump"
            client = cutover.DockerPsql(
                container="postgres",
                database="postgres",
                user="postgres",
                host="172.22.0.1",
                port=5433,
            )
            with mock.patch.object(
                cutover,
                "_pg15_version",
                return_value="pg_dump (PostgreSQL) 15.13",
            ):
                result = cutover.create_custom_dump(
                    client,
                    str(output),
                    timeout_seconds=60,
                    popen=lambda *args, **kwargs: Process(**kwargs),
                )
            self.assertEqual(output.read_bytes()[:5], b"PGDMP")
            self.assertEqual(result["size_bytes"], len(b"PGDMPfixture"))

    def test_restore_timeout_terminates_backend_and_proves_empty(self):
        class Process:
            returncode = None

            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()

            def poll(self):
                return None

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "source.dump"
            artifact.write_bytes(b"PGDMPfixture")
            descriptor = os.open(artifact, os.O_RDONLY)
            client = cutover.DockerPsql(
                container="postgres", database="blueprint", user="blueprint"
            )
            try:
                with (
                    mock.patch.object(cutover, "_pg15_version"),
                    mock.patch.object(cutover, "_terminate_restore_backend") as terminate,
                    mock.patch.object(cutover, "target_public_schema_empty") as empty,
                    mock.patch.object(cutover.time, "monotonic", side_effect=[0, 61]),
                ):
                    with self.assertRaisesRegex(
                        cutover.CutoverError, "timeout_rolled_back"
                    ):
                        cutover.stream_restore(
                            client,
                            descriptor,
                            app_name="blueprint-cutover-restore-test",
                            timeout_seconds=60,
                            popen=lambda *args, **kwargs: Process(),
                        )
                terminate.assert_called_once()
                empty.assert_called_once()
            finally:
                os.close(descriptor)

    def test_atomic_evidence_is_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            cutover._atomic_json(str(output), {"outcome": "success"})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            else:
                self.assertTrue(output.is_file())
            self.assertEqual(json.loads(output.read_text())["outcome"], "success")

    def test_failure_result_contains_no_exception_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            with mock.patch.object(cutover, "verify_import", side_effect=cutover.CutoverError("source_fence", "active_source_writers")):
                rc = cutover.main(
                    [
                        "verify-import",
                        "--source-snapshot", str(Path(directory) / "s"),
                        "--source-fence", str(Path(directory) / "f"),
                        "--export-manifest", str(Path(directory) / "m"),
                        "--target-container", "postgres",
                        "--target-database", "blueprint",
                        "--target-user", "blueprint",
                        "--target-instance", "hamsterwheel",
                        "--target-snapshot-output", str(Path(directory) / "target.json"),
                        "--result-output", str(result),
                    ]
                )
            self.assertEqual(rc, 1)
            payload = json.loads(result.read_text())
            self.assertEqual(payload["code"], "active_source_writers")
            self.assertEqual(set(payload), {"schema_version", "kind", "outcome", "failed_at", "stage", "code"})


if __name__ == "__main__":
    unittest.main()
