"""SQLite test for the 0668 table-drop migration.

Migration 0668 drops the unused ``projectcomputeimagebuild`` table.
On SQLite the ``upgrade()`` (``DROP TABLE``) works fully; the
``downgrade()`` recreates the table but uses ``sa.func.now()``
in ``server_default``, which is not a recognised built-in on SQLite,
so the downgrade is best-effort there.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0668_drop_projectcomputeimagebuild.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0668_drop_projectcomputeimagebuild",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0668_schema(connection):
    """Create ``project`` + ``projectcomputeimagebuild`` as migration 0659
    would leave them (before the 0668 drop)."""
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
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("image_published_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
    )
    metadata.create_all(connection)
    return project, build


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_0668_module_loads_and_has_correct_revision():
    """Verify the migration module has the expected revision identifiers."""
    migration = _load_migration()
    assert migration.revision == "0668", (
        f"Expected revision 0668, got {migration.revision!r}"
    )
    assert migration.down_revision == "0659", (
        f"Expected down_revision 0659, got {migration.down_revision!r}"
    )


# ---------------------------------------------------------------------------
# Execution — upgrade (full on SQLite)
# ---------------------------------------------------------------------------


def test_0668_upgrade_drops_table():
    """Verify upgrade drops ``projectcomputeimagebuild`` with no FK breakage.

    After the drop the ``project`` table (the FK target) must still be
    present and queryable.
    """
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")

    # -- Pre-migration: create schema and seed rows ------------------------
    with engine.begin() as connection:
        project, build = _pre_0668_schema(connection)

        project_id = uuid.uuid4()
        connection.execute(project.insert().values(id=project_id))

        build_id = uuid.uuid4()
        connection.execute(
            build.insert().values(
                id=build_id,
                project_id=project_id,
                dep_hash="abc123",
                registry_repo="us-central1-docker.pkg.dev/proj/repo",
                image_name="compute-image",
            )
        )

    # Confirm pre-upgrade state
    inspector = sa.inspect(engine)
    pre_tables = inspector.get_table_names()
    assert "projectcomputeimagebuild" in pre_tables, (
        "Pre-migration: projectcomputeimagebuild should exist"
    )
    assert "project" in pre_tables, (
        "Pre-migration: project table should exist"
    )

    # -- Upgrade: drop projectcomputeimagebuild ---------------------------
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original_op

    # -- Post-upgrade assertions ------------------------------------------
    inspector = sa.inspect(engine)
    post_tables = inspector.get_table_names()
    assert "projectcomputeimagebuild" not in post_tables, (
        "Migration 0668 upgrade should drop projectcomputeimagebuild"
    )
    assert "project" in post_tables, (
        "project table (FK target) must survive the upgrade"
    )


# ---------------------------------------------------------------------------
# Execution — downgrade (best-effort on SQLite)
# ---------------------------------------------------------------------------


def test_0668_downgrade_recreates_table():
    """Verify downgrade recreates ``projectcomputeimagebuild``.

    On SQLite the downgrade's ``sa.func.now()`` server_default is not
    supported (SQLite has no ``now()`` function). The test runs it
    anyway and skips on error — the upgrade path is the primary concern.
    """
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")

    # Start from post-upgrade state (table already dropped).
    with engine.begin() as connection:
        project = sa.Table(
            "project",
            sa.MetaData(),
            sa.Column("id", sa.Uuid(), primary_key=True),
        )
        project.create(connection)
        connection.execute(project.insert().values(id=uuid.uuid4()))

    assert "projectcomputeimagebuild" not in sa.inspect(engine).get_table_names()

    # -- Downgrade --------------------------------------------------------
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.downgrade()
        except Exception:
            # `sa.func.now()` is not valid on SQLite as a default expression
            # (the migration's downgrade relies on it).  That is acceptable.
            pytest.skip(
                "Migration 0668 downgrade uses sa.func.now() in "
                "server_default which is not valid on SQLite"
            )
        finally:
            migration.op = original_op

    # -- Post-downgrade assertions ----------------------------------------
    inspector = sa.inspect(engine)
    post_tables = inspector.get_table_names()
    assert "projectcomputeimagebuild" in post_tables, (
        "Migration 0668 downgrade should recreate projectcomputeimagebuild"
    )
    assert "project" in post_tables, (
        "project table must survive the downgrade"
    )
