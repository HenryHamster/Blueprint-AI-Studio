"""SQLite upgrade-path coverage for admitted ordinary Task outcome triggers."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0637_resident_ordinary_task_terminal_events.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0637_resident_ordinary_task_terminal_events",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0637_schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )
    task = sa.Table(
        "task",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("strategic_operation_receipt_id", sa.Uuid(), nullable=True),
    )
    metadata.create_all(connection)
    return project, task


def test_sqlite_terminal_transition_is_exact_filtered_and_rollback_safe():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        project, task = _pre_0637_schema(connection)
        project_id = uuid.uuid4()
        connection.execute(project.insert().values(id=project_id))
        connection.exec_driver_sql(
            "CREATE TRIGGER preserve_existing_task_fence "
            "BEFORE DELETE ON task BEGIN SELECT 1; END"
        )

        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            event = sa.Table(
                "strategicresidentordinarytaskterminalevent",
                sa.MetaData(),
                autoload_with=connection,
            )
            ids = {
                name: uuid.uuid4() for name in ("user", "pipeline", "child", "managed")
            }
            connection.execute(
                task.insert(),
                [
                    {
                        "id": ids["user"],
                        "project_id": project_id,
                        "status": "QUEUED",
                        "created_by": "USER",
                        "parent_task_id": None,
                        "strategic_operation_receipt_id": None,
                    },
                    {
                        "id": ids["pipeline"],
                        "project_id": project_id,
                        "status": "QUEUED",
                        "created_by": "ORCHESTRATOR",
                        "parent_task_id": None,
                        "strategic_operation_receipt_id": None,
                    },
                    {
                        "id": ids["child"],
                        "project_id": project_id,
                        "status": "QUEUED",
                        "created_by": "USER",
                        "parent_task_id": ids["pipeline"],
                        "strategic_operation_receipt_id": None,
                    },
                    {
                        "id": ids["managed"],
                        "project_id": project_id,
                        "status": "QUEUED",
                        "created_by": "USER",
                        "parent_task_id": None,
                        "strategic_operation_receipt_id": uuid.uuid4(),
                    },
                ],
            )
            for task_id in ids.values():
                connection.execute(
                    task.update().where(task.c.id == task_id).values(status="FAILED")
                )
            rows = connection.execute(sa.select(event)).mappings().all()
            assert len(rows) == 1
            assert uuid.UUID(str(rows[0]["project_id"])) == project_id
            assert uuid.UUID(str(rows[0]["task_id"])) == ids["user"]
            assert rows[0]["event_kind"] == "FAILED"
            assert rows[0]["task_created_by"] == "USER"

            connection.execute(
                task.update().where(task.c.id == ids["user"]).values(status="FAILED")
            )
            assert connection.scalar(sa.select(sa.func.count()).select_from(event)) == 1
            connection.execute(
                task.update().where(task.c.id == ids["user"]).values(status="QUEUED")
            )
            connection.execute(
                task.update().where(task.c.id == ids["user"]).values(status="COMPLETED")
            )
            rows = (
                connection.execute(sa.select(event).order_by(event.c.sequence))
                .mappings()
                .all()
            )
            assert [row["event_kind"] for row in rows] == ["FAILED", "COMPLETED"]
            assert rows[1]["sequence"] > rows[0]["sequence"]

            migration.downgrade()
            remaining = connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'trg_resident_ordinary_task_terminal_event_%'"
            ).scalar_one()
            assert remaining == 0
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'preserve_existing_task_fence'"
                ).scalar_one()
                == 1
            )
        finally:
            migration.op = original_op


def test_sqlite_terminal_insert_is_captured_without_context_or_logs():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, task = _pre_0637_schema(connection)
        project_id = uuid.uuid4()
        task_id = uuid.uuid4()
        connection.execute(project.insert().values(id=project_id))
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            connection.execute(
                task.insert().values(
                    id=task_id,
                    project_id=project_id,
                    status="CANCELLED",
                    created_by="USER",
                    parent_task_id=None,
                    strategic_operation_receipt_id=None,
                )
            )
            event = sa.Table(
                "strategicresidentordinarytaskterminalevent",
                sa.MetaData(),
                autoload_with=connection,
            )
            row = connection.execute(sa.select(event)).mappings().one()
            assert set(row) == {
                "sequence",
                "project_id",
                "task_id",
                "event_kind",
                "task_created_by",
            }
            assert uuid.UUID(str(row["task_id"])) == task_id
            assert row["event_kind"] == "CANCELLED"
            assert row["task_created_by"] == "USER"
        finally:
            migration.op = original_op
