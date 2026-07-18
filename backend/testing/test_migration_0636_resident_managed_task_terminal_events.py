"""SQLite upgrade-path coverage for managed Task terminal outbox triggers."""

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
        / "0636_resident_managed_task_terminal_events.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0636_resident_managed_task_terminal_events",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0636_schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    receipt = sa.Table(
        "strategicoperationreceipt",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("turn_task_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("expected_revisions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
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
        sa.Column("context", sa.JSON(), nullable=False),
    )
    metadata.create_all(connection)
    return project, receipt, task


def test_sqlite_core_terminal_update_emits_once_and_downgrade_drops_triggers():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        project, receipt, task = _pre_0636_schema(connection)
        project_id = uuid.uuid4()
        receipt_id = uuid.uuid4()
        turn_task_id = uuid.uuid4()
        task_id = uuid.uuid4()
        context_hash = "a" * 64
        connection.execute(project.insert().values(id=project_id))
        connection.execute(
            receipt.insert().values(
                id=receipt_id,
                project_id=project_id,
                turn_task_id=turn_task_id,
                capability="task.code_change.create",
                expected_revisions={"context_hash": context_hash},
                status="applied",
            )
        )
        connection.execute(
            task.insert().values(
                id=task_id,
                project_id=project_id,
                status="QUEUED",
                context={
                    "_resident_managed_task": {
                        "receipt_id": str(receipt_id),
                        "capability_id": "task.code_change.create",
                        "source_turn_task_id": str(turn_task_id),
                        "context_hash": context_hash,
                    }
                },
            )
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER preserve_existing_task_fence "
            "BEFORE DELETE ON task BEGIN SELECT 1; END"
        )

        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            migrated_task = sa.Table(
                "task", sa.MetaData(), autoload_with=connection
            )
            event = sa.Table(
                "strategicresidenttaskterminalevent",
                sa.MetaData(),
                autoload_with=connection,
            )
            connection.execute(
                migrated_task.update()
                .where(migrated_task.c.id == task_id)
                .values(
                    status="FAILED",
                    # SQLite reflection exposes Alembic's UUID column as
                    # CHAR(32), so bind the canonical stored representation.
                    strategic_operation_receipt_id=receipt_id.hex,
                )
            )
            assert connection.scalar(sa.select(sa.func.count()).select_from(event)) == 1

            connection.execute(
                migrated_task.update()
                .where(migrated_task.c.id == task_id)
                .values(status="FAILED")
            )
            assert connection.scalar(sa.select(sa.func.count()).select_from(event)) == 1
            trigger_names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE "
                    "'trg_resident_managed_task_terminal_event_%'"
                ).scalars()
            )
            assert trigger_names == {
                migration._SQLITE_INSERT_TRIGGER,
                migration._SQLITE_UPDATE_TRIGGER,
            }
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'preserve_existing_task_fence'"
            ).scalar_one() == 1

            migration.downgrade()
            remaining = connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE "
                "'trg_resident_managed_task_terminal_event_%'"
            ).scalar_one()
            assert remaining == 0
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'preserve_existing_task_fence'"
            ).scalar_one() == 1
        finally:
            migration.op = original_op
