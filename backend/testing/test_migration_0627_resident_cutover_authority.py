"""Upgrade-path coverage for the post-0611 Resident cutover fence repair."""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="Resident cutover forward-migration smoke test requires Postgres",
)

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migrations():
    return (
        _load(
            _VERSIONS / "0611_strategic_resident_control_plane.py",
            "migration_0611_before_cutover_repair",
        ),
        _load(
            _VERSIONS / "0623_fix_checkpoint_clear_fk_cascade.py",
            "migration_0623_checkpoint_clear",
        ),
        _load(
            _VERSIONS / "0627_resident_cutover_checkpoint_authority.py",
            "migration_0627_cutover_repair",
        ),
    )


def _load_0611_support():
    return _load(
        Path(__file__).with_name("test_migration_0611_strategic_resident.py"),
        "migration_0611_test_support",
    )


def _seed_projects(connection, project, count: int = 3) -> list[uuid.UUID]:
    project_ids = [uuid.uuid4() for _ in range(count)]
    connection.execute(
        project.insert(),
        [
            {
                "id": project_id,
                "vision": {},
                "problem_model": {},
                "directions": [],
                "strategic_memory_notes": [],
                "running_update": None,
                "paused": False,
            }
            for project_id in project_ids
        ],
    )
    return project_ids


def _expect_cutover_rejected(support, connection, resident, checkpoint, project_id):
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        support._exercise_staged_cutover_checkpoint(
            connection,
            resident=resident,
            checkpoint=checkpoint,
            project_id=project_id,
        )


def _exercise_upgrade_path(connection) -> None:
    support = _load_0611_support()
    migration_0611, migration_0623, migration_0627 = _load_migrations()
    project, _task, _prefs, _message = support._sqlite_legacy_schema(connection)
    cutover_project, downgrade_project, cascade_project = _seed_projects(
        connection, project
    )
    operations = Operations(MigrationContext.configure(connection))
    modules = (migration_0611, migration_0623, migration_0627)
    original_ops = tuple(module.op for module in modules)
    for module in modules:
        module.op = operations
    try:
        migration_0611.upgrade()
        migration_0623.upgrade()
        resident = sa.Table(
            "strategicresident", sa.MetaData(), autoload_with=connection
        )
        checkpoint = sa.Table(
            "strategicresidentcheckpoint", sa.MetaData(), autoload_with=connection
        )

        # The already-shipped 0611/0623 schema rejects the new boundary.
        _expect_cutover_rejected(
            support, connection, resident, checkpoint, cutover_project
        )

        migration_0627.upgrade()
        # The helper first proves a cutover-shaped row without the private marker
        # is rejected, then stages the exact marker and completes the transition.
        support._exercise_staged_cutover_checkpoint(
            connection,
            resident=resident,
            checkpoint=checkpoint,
            project_id=cutover_project,
        )

        # Recreating the SQLite fences and wrapping the PostgreSQL fences must
        # preserve 0623's FK-driven checkpoint-clear escape.
        stored_cascade_project = (
            cascade_project
            if connection.dialect.name == "postgresql"
            else cascade_project.hex
        )
        cascade_checkpoint_id = connection.scalar(
            sa.select(resident.c.current_checkpoint_id).where(
                resident.c.project_id == stored_cascade_project
            )
        )
        connection.execute(
            checkpoint.delete().where(checkpoint.c.id == cascade_checkpoint_id)
        )
        assert connection.scalar(
            sa.select(resident.c.current_checkpoint_id).where(
                resident.c.project_id == stored_cascade_project
            )
        ) is None

        migration_0627.downgrade()
        # Downgrade returns to the 0623 authority behavior, not a permissive
        # hybrid: even an otherwise exact staged marker is rejected again.
        _expect_cutover_rejected(
            support, connection, resident, checkpoint, downgrade_project
        )
        migration_0623.downgrade()
        migration_0611.downgrade()
    finally:
        for module, original_op in zip(modules, original_ops, strict=True):
            module.op = original_op


def test_sqlite_upgrades_applied_0611_to_exact_cutover_authority():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _exercise_upgrade_path(connection)


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_upgrades_applied_0611_to_exact_cutover_authority():
    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _name,
    ), engine.begin() as connection:
        await connection.run_sync(_exercise_upgrade_path)
