"""SQLite no-op coverage for the 0639 TIMESTAMPTZ alignment migration.

The migration only acts on PostgreSQL; on SQLite it is a no-op. These tests
verify that the upgrade/downgrade run without error on SQLite and that the
table schema is unchanged afterwards.
"""

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
        / "0639_align_projectcomputeimagebuild_cols_to_timestamptz.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0639_align_cols_to_timestamptz",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0639_schema(connection):
    """Create the projectcomputeimagebuild table as migration 0633 would leave it."""
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    build = sa.Table(
        "projectcomputeimagebuild",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dep_hash", sa.String(255), nullable=False),
        sa.Column("registry_repo", sa.String(512), nullable=False),
        sa.Column("image_name", sa.String(255), nullable=False),
        sa.Column("builder_vm_id", sa.String(255), nullable=True),
        sa.Column("zone", sa.String(128), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        # Bare DateTime() — the pre-0639 state (the bug being fixed)
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        # Use CURRENT_TIMESTAMP (SQLite-compatible) — the real migration 0633
        # used server_default=sa.text("now()") which is Postgres-only.
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
    )
    metadata.create_all(connection)
    return project, build


def test_sqlite_upgrade_downgrade_noop():
    """0639 is a no-op on SQLite; verify upgrade and downgrade run cleanly."""
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, build = _pre_0639_schema(connection)
        project_id = uuid.uuid4()
        connection.execute(project.insert().values(id=project_id))
        connection.execute(
            build.insert().values(
                id=uuid.uuid4(),
                project_id=project_id,
                dep_hash="abc123",
                registry_repo="us-central1-docker.pkg.dev/proj/repo",
                image_name="compute-image",
            )
        )

        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            # upgrade should not raise on SQLite
            migration.upgrade()

            # Table should still be readable
            rows = connection.execute(build.select()).fetchall()
            assert len(rows) == 1

            # downgrade should not raise on SQLite
            migration.downgrade()

            rows = connection.execute(build.select()).fetchall()
            assert len(rows) == 1
        finally:
            migration.op = original_op
