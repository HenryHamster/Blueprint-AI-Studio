"""Executable coverage for migration 0682 and the amended Workday chain."""

import ast
import asyncio
import contextlib
import importlib.util
import json
import os
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database


from alembic.operations import Operations

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="requires PostgreSQL",
)


def _migration():
    path = Path(__file__).resolve().parents[1] / (
        "alembic/versions/0682_repair_workday_session_and_telemetry.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0682", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("design_doc", sa.JSON(), nullable=False),
    )
    telemetry = sa.Table(
        "project_telemetry",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "key"),
    )
    task = sa.Table(
        "task",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
    )
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("from_type", sa.String(), nullable=False),
        sa.Column("from_task_id", sa.String()),
        sa.Column("reply_to_message_id", sa.String()),
        sa.Column("context", sa.JSON()),
        sa.Column("resident_session_id", sa.String()),
    )
    metadata.create_all(connection)
    return project, telemetry, task, message


def _postgres_schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("design_doc", postgresql.JSONB(), nullable=False),
    )
    telemetry = sa.Table(
        "project_telemetry",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "key",
            name="uq_project_telemetry_project_key",
        ),
    )
    task = sa.Table(
        "task",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
    )
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_type", sa.String(), nullable=False),
        sa.Column("from_task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resident_session_id", sa.String()),
    )
    metadata.create_all(connection)
    return project, telemetry, task, message


def test_upgrade_repairs_json_and_only_evidenced_session_links():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, telemetry, task, message = _schema(connection)
        feedback = {"answer": "ship", "scores": [1, 2]}
        connection.execute(
            project.insert(),
            [
                {"id": "p1", "design_doc": {migration._TELEMETRY_KEY: feedback}},
                {"id": "p2", "design_doc": {migration._TELEMETRY_KEY: "123"}},
                {"id": "p3", "design_doc": {migration._TELEMETRY_KEY: "true"}},
                {"id": "p4", "design_doc": {migration._TELEMETRY_KEY: "[1]"}},
                {
                    "id": "p5",
                    "design_doc": {migration._TELEMETRY_KEY: {"version": "stale"}},
                },
            ],
        )
        live_timestamp = datetime(2026, 7, 22, tzinfo=timezone.utc)
        connection.execute(
            telemetry.insert(),
            {
                "id": "live-id",
                "project_id": "p5",
                "key": migration._TELEMETRY_KEY,
                "value": json.dumps({"version": "live"}),
                "updated_at": live_timestamp,
            },
        )
        before_existing = connection.execute(
            sa.select(telemetry).where(telemetry.c.project_id == "p5")
        ).one()
        connection.execute(
            task.insert(),
            [
                {
                    "id": "resident",
                    "project_id": "p1",
                    "context": {
                        "_resident_session": True,
                        "strategic_session_id": "session-a",
                    },
                },
                {
                    "id": "ordinary",
                    "project_id": "p1",
                    "context": {"strategic_session_id": "wrong"},
                },
            ],
        )
        for row in [
            {"id": "direct", "project_id": "p1", "from_type": "user", "context": {}},
            {"id": "covered", "project_id": "p1", "from_type": "user", "context": {}},
            {
                "id": "reply",
                "project_id": "p1",
                "from_type": "strategic",
                "from_task_id": "resident",
                "reply_to_message_id": "direct",
                "context": {"_resident_reply_covered_message_ids": ["covered"]},
            },
            {
                "id": "unsupported",
                "project_id": "p1",
                "from_type": "strategic",
                "from_task_id": "ordinary",
                "context": {},
            },
            {
                "id": "preserved",
                "project_id": "p1",
                "from_type": "user",
                "context": {},
                "resident_session_id": "existing",
            },
        ]:
            connection.execute(message.insert(), row)
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            migration.upgrade()
        finally:
            migration.op = original_op
        payloads = dict(
            connection.execute(
                sa.select(telemetry.c.project_id, telemetry.c.value)
            ).all()
        )
        assert json.loads(payloads["p1"]) == feedback
        assert "'answer'" not in payloads["p1"]
        assert json.loads(payloads["p2"]) == "123"
        assert json.loads(payloads["p3"]) == "true"
        assert json.loads(payloads["p4"]) == "[1]"
        after_existing = connection.execute(
            sa.select(telemetry).where(telemetry.c.project_id == "p5")
        ).one()
        assert after_existing == before_existing
        assert dict(
            connection.execute(
                sa.select(message.c.id, message.c.resident_session_id)
            ).all()
        ) == {
            "direct": None,
            "covered": None,
            "reply": "session-a",
            "unsupported": None,
            "preserved": "existing",
        }


def test_recovery_only_uses_direct_resident_task_anchors():
    migration = _migration()
    tasks = [
        {
            "id": "ta",
            "project_id": "p1",
            "context": {"_resident_session": True, "strategic_session_id": "a"},
        },
        {
            "id": "tb",
            "project_id": "p1",
            "context": {"_resident_session": True, "strategic_session_id": "b"},
        },
    ]
    messages = [
        {"id": "target", "project_id": "p1", "from_type": "user", "context": {}},
        {
            "id": "a",
            "project_id": "p1",
            "from_type": "strategic",
            "from_task_id": "ta",
            "context": {"_resident_reply_covered_message_ids": ["target", "foreign"]},
        },
        {
            "id": "b",
            "project_id": "p1",
            "from_type": "strategic",
            "from_task_id": "tb",
            "reply_to_message_id": "target",
            "context": {},
        },
        {"id": "foreign", "project_id": "p2", "from_type": "user", "context": {}},
        {
            "id": "foreign-anchor",
            "project_id": "p2",
            "from_type": "strategic",
            "from_task_id": "ta",
            "context": {},
        },
        {
            "id": "legacy-strategic",
            "project_id": "p1",
            "from_type": "strategic",
            "context": {},
        },
        {
            "id": "current-operator-reply",
            "project_id": "p1",
            "from_type": "user",
            "from_task_id": "ta",
            "reply_to_message_id": "legacy-strategic",
            "context": {},
            "resident_session_id": "a",
        },
    ]
    assert migration._recover_session_updates(tasks, messages) == {
        "a": "a",
        "b": "b",
    }


def _migration_operation(migration, connection, operation):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        operation()
    finally:
        migration.op = original_op


@requires_postgres
@pytest.mark.asyncio
async def test_postgresql_uuid_jsonb_existing_row_rerun_and_downgrade():
    migration = _migration()
    project_ids = {name: uuid.uuid4() for name in ("missing", "string", "existing")}
    existing_id = uuid.uuid4()
    existing_time = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    resident_task_id = uuid.uuid4()
    resident_reply_id = uuid.uuid4()
    legacy_user_id = uuid.uuid4()

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        async with engine.begin() as connection:
            project, telemetry, task, message = await connection.run_sync(
                _postgres_schema
            )
            await connection.execute(
                project.insert(),
                [
                    {
                        "id": project_ids["missing"],
                        "design_doc": {migration._TELEMETRY_KEY: {"version": "seed"}},
                    },
                    {
                        "id": project_ids["string"],
                        "design_doc": {migration._TELEMETRY_KEY: "123"},
                    },
                    {
                        "id": project_ids["existing"],
                        "design_doc": {migration._TELEMETRY_KEY: {"version": "stale"}},
                    },
                ],
            )
            await connection.execute(
                telemetry.insert(),
                {
                    "id": existing_id,
                    "project_id": project_ids["existing"],
                    "key": migration._TELEMETRY_KEY,
                    "value": {"version": "writer-v1"},
                    "updated_at": existing_time,
                },
            )
            await connection.execute(
                task.insert(),
                {
                    "id": resident_task_id,
                    "project_id": project_ids["missing"],
                    "context": {
                        "_resident_session": True,
                        "strategic_session_id": "session-pg",
                    },
                },
            )
            await connection.execute(
                message.insert(),
                [
                    {
                        "id": resident_reply_id,
                        "project_id": project_ids["missing"],
                        "from_type": "strategic",
                        "from_task_id": resident_task_id,
                    },
                    {
                        "id": legacy_user_id,
                        "project_id": project_ids["missing"],
                        "from_type": "user",
                        "from_task_id": None,
                    },
                ],
            )
            await connection.run_sync(
                lambda sync: _migration_operation(migration, sync, migration.upgrade)
            )
            await connection.run_sync(
                lambda sync: _migration_operation(migration, sync, migration.upgrade)
            )
            await connection.run_sync(
                lambda sync: _migration_operation(migration, sync, migration.downgrade)
            )

        async with engine.connect() as connection:
            rows = (await connection.execute(sa.select(telemetry))).mappings().all()
            by_project = {row["project_id"]: row for row in rows}
            assert by_project[project_ids["missing"]]["value"] == {"version": "seed"}
            assert by_project[project_ids["string"]]["value"] == "123"
            json_type = await connection.scalar(
                sa.text(
                    "SELECT jsonb_typeof(value) FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_ids["string"]},
            )
            assert json_type == "string"
            existing = by_project[project_ids["existing"]]
            assert (existing["id"], existing["value"], existing["updated_at"]) == (
                existing_id,
                {"version": "writer-v1"},
                existing_time,
            )
            sessions = dict(
                (
                    await connection.execute(
                        sa.select(message.c.id, message.c.resident_session_id)
                    )
                ).all()
            )
            assert sessions == {resident_reply_id: "session-pg", legacy_user_id: None}


@requires_postgres
@pytest.mark.asyncio
async def test_postgresql_writer_wins_unique_conflict_race():
    migration = _migration()
    project_id = uuid.uuid4()
    writer_id = uuid.uuid4()
    writer_time = datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc)

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        async with engine.begin() as connection:
            project, telemetry, _task, _message = await connection.run_sync(
                _postgres_schema
            )
            await connection.execute(
                project.insert(),
                {
                    "id": project_id,
                    "design_doc": {migration._TELEMETRY_KEY: {"version": "migration"}},
                },
            )

        async with (
            engine.connect() as writer,
            engine.connect() as migrator,
            engine.connect() as observer,
        ):
            writer_tx = await writer.begin()
            migrator_tx = await migrator.begin()
            migrator_pid = await migrator.scalar(sa.text("SELECT pg_backend_pid()"))
            await writer.execute(
                telemetry.insert(),
                {
                    "id": writer_id,
                    "project_id": project_id,
                    "key": migration._TELEMETRY_KEY,
                    "value": {"version": "writer-race"},
                    "updated_at": writer_time,
                },
            )
            migration_task = asyncio.create_task(
                migrator.run_sync(migration._repair_telemetry)
            )
            try:
                for _ in range(100):
                    if migration_task.done():
                        await migration_task
                        pytest.fail(
                            "migration completed before waiting on unique conflict"
                        )
                    wait_event_type = await observer.scalar(
                        sa.text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": migrator_pid},
                    )
                    if wait_event_type == "Lock":
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("migration did not enter a PostgreSQL lock wait")

                await writer_tx.commit()
                await asyncio.wait_for(migration_task, timeout=5)
                await migrator_tx.commit()
            finally:
                if not migration_task.done():
                    migration_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await migration_task
                if writer_tx.is_active:
                    await writer_tx.rollback()
                if migrator_tx.is_active:
                    await migrator_tx.rollback()

        async with engine.connect() as connection:
            raced = (await connection.execute(sa.select(telemetry))).mappings().one()
            assert (raced["id"], raced["value"], raced["updated_at"]) == (
                writer_id,
                {"version": "writer-race"},
                writer_time,
            )


def _migration_sources():
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    for path in sorted(versions.glob("*.py")):
        if path.name[:4].isdigit() and path.name[:4] >= "0641":
            yield path, path.read_text(encoding="utf-8")


def _literal_assignment(tree, name):
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    return None


def _function(tree, name):
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ),
        None,
    )


def _is_noop(function):
    if function is None:
        return False
    body = [
        node
        for node in function.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    return not body or all(isinstance(node, ast.Pass) for node in body)


def test_future_noop_migrations_cannot_have_throwing_downgrades():
    grandfathered = {"0644"}
    violations = []
    for path, source in _migration_sources():
        tree = ast.parse(source, filename=str(path))
        revision = _literal_assignment(tree, "revision")
        if revision in grandfathered or not _is_noop(_function(tree, "upgrade")):
            continue
        downgrade = _function(tree, "downgrade")
        if downgrade and any(
            isinstance(node, ast.Raise) for node in ast.walk(downgrade)
        ):
            violations.append(path.name)
    assert not violations, (
        "no-op migrations must also downgrade without throwing; "
        f"only immutable revision 0644 is grandfathered: {violations}"
    )


def test_future_migration_docstrings_match_executable_revision_metadata():
    grandfathered = {"0641"}
    violations = []
    for path, source in _migration_sources():
        tree = ast.parse(source, filename=str(path))
        revision = _literal_assignment(tree, "revision")
        down_revision = _literal_assignment(tree, "down_revision")
        if revision in grandfathered:
            continue
        docstring = ast.get_docstring(tree) or ""
        revision_header = re.search(
            r"^Revision ID:\s*(\S+)", docstring, flags=re.MULTILINE
        )
        revises_header = re.search(r"^Revises:\s*(\S+)", docstring, flags=re.MULTILINE)
        if revision_header and revision_header.group(1) != str(revision):
            violations.append(
                f"{path.name}: Revision ID {revision_header.group(1)} != {revision}"
            )
        if revises_header and revises_header.group(1) != str(down_revision):
            violations.append(
                f"{path.name}: Revises {revises_header.group(1)} != {down_revision}"
            )

    assert not violations, (
        "migration docstring metadata must match executable revision values; "
        f"only immutable revision 0641 is grandfathered: {violations}"
    )
