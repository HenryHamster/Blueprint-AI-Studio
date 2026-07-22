"""SQLite/Postgres test for the 0656 expression-index migration.

Migration 0656 creates ``CREATE INDEX CONCURRENTLY IF NOT EXISTS
ix_projectmessage_context_audience ON projectmessage ((context->>'audience'))``,
a PostgreSQL-specific expression index that accelerates the
``operator_visible_audience_filter()`` predicate.

On SQLite the raw SQL uses PostgreSQL-only syntax (->>, CONCURRENTLY), so
execution tests skip on non-Postgres dialects.  Module-level metadata
verification runs everywhere.
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
        / "0656_projectmessage_context_audience_index.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0656_projectmessage_context_audience_index",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0656_schema(connection):
    """Create ``project`` + ``projectmessage`` as migration 0655 would leave them."""
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    msg = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Use type JSON() over JSONB() for dialect portability.
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("surface", sa.String(32), nullable=False, server_default="activity"),
        sa.Column("handling_state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("message_type", sa.String(), nullable=False, server_default="NOTE"),
        sa.Column("from_type", sa.String(), nullable=False, server_default="user"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
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
    return project, msg


# ---------------------------------------------------------------------------
# Metadata tests (run everywhere)
# ---------------------------------------------------------------------------


def test_0656_module_loads_and_has_correct_revision():
    """Verify the migration module has the expected revision identifiers."""
    migration = _load_migration()
    assert migration.revision == "0656", (
        f"Expected revision 0656, got {migration.revision!r}"
    )
    assert migration.down_revision == "0655", (
        f"Expected down_revision 0655, got {migration.down_revision!r}"
    )


# ---------------------------------------------------------------------------
# Execution tests (Postgres only; skip on SQLite)
# ---------------------------------------------------------------------------


def test_0656_upgrade_creates_expression_index():
    """Verify the expression index on ``context->>'audience'`` is created.

    This test requires PostgreSQL because the migration emits raw DDL
    with ``->>`` (JSONB field-access operator) and ``CONCURRENTLY``,
    neither of which SQLite supports.  On SQLite the test skips
    gracefully after verifying the pre-migration schema is set up.
    """
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")

    # -- Pre-migration schema and seed data --------------------------------
    with engine.begin() as connection:
        project, msg = _pre_0656_schema(connection)

        project_id = uuid.uuid4()
        connection.execute(project.insert().values(id=project_id))

        for audience in ("operator", "user", "operator", None):
            ctx = {"audience": audience} if audience else None
            connection.execute(
                msg.insert().values(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    surface="activity",
                    handling_state="pending",
                    message_type="NOTE",
                    from_type="user",
                    content="Test",
                    status="open",
                    context=ctx,
                )
            )

    # -- Detect dialect and either run or skip ----------------------------
    with engine.connect() as connection:
        dialect = connection.dialect.name
    if dialect != "postgresql":
        pytest.skip(
            "Migration 0656 uses PostgreSQL-specific DDL "
            "(->> operator, CONCURRENTLY) — skipping execution on "
            f"dialect {dialect!r}"
        )

    # -- Upgrade -----------------------------------------------------------
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
        finally:
            migration.op = original_op

    # -- Verify index exists via inspector ---------------------------------
    inspector = sa.inspect(engine)
    indexes = inspector.get_indexes("projectmessage")
    index_names = {idx["name"] for idx in indexes}
    assert "ix_projectmessage_context_audience" in index_names, (
        "Migration 0656 should create "
        "ix_projectmessage_context_audience on projectmessage"
    )

    # -- Verify the index accelerates queries (avoid seq scan) -------------
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT id FROM projectmessage "
                "WHERE context->>'audience' = 'operator'"
            )
        ).fetchall()
    assert len(rows) == 2, (
        f"Expected 2 rows matching audience='operator', got {len(rows)}"
    )

    # -- Downgrade ---------------------------------------------------------
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op2 = migration.op
        migration.op = operations
        try:
            migration.downgrade()
        finally:
            migration.op = original_op2

    # -- Verify index is gone ----------------------------------------------
    inspector = sa.inspect(engine)
    indexes = inspector.get_indexes("projectmessage")
    index_names = {idx["name"] for idx in indexes}
    assert "ix_projectmessage_context_audience" not in index_names, (
        "Migration 0656 downgrade should drop "
        "ix_projectmessage_context_audience"
    )
