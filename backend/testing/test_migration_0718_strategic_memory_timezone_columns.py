"""SQLite parity coverage for Strategic Memory timestamp correction 0718."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


_VERSIONS = Path(__file__).resolve().parents[1] / "alembic/versions"


def _migration(revision: str, filename: str):
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(migration, connection, name: str) -> None:
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        getattr(migration, name)()
    finally:
        migration.op = original_op


def _sqlite_trigger_names(connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        )
    }


def test_0718_converts_every_strategic_memory_timestamp_and_reverses_cleanly():
    migration_0715 = _migration(
        "migration_0715_timezone", "0715_strategic_context_envelope_snapshots.py"
    )
    migration_0716 = _migration(
        "migration_0716_timezone", "0716_strategic_memory_source_control_plane.py"
    )
    migration_0717 = _migration(
        "migration_0717_timezone", "0717_strategic_memory_semantic_ledger.py"
    )
    migration = _migration(
        "migration_0718_timezone", "0718_strategic_memory_timezone_columns.py"
    )
    assert migration.revision == "0718"
    assert migration.down_revision == "0717"
    assert len(migration._COLUMNS) == 28
    assert len({(table, column) for table, column, _ in migration._COLUMNS}) == 28

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        sa.Table(
            "project", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True)
        ).create(connection)
        _run(migration_0715, connection, "upgrade")
        _run(migration_0716, connection, "upgrade")
        _run(migration_0717, connection, "upgrade")
        triggers_before = _sqlite_trigger_names(connection)
        _run(migration, connection, "upgrade")
        assert _sqlite_trigger_names(connection) == triggers_before

        inspector = sa.inspect(connection)
        for table, column, _has_default in migration._COLUMNS:
            assert column in {
                column_info["name"] for column_info in inspector.get_columns(table)
            }

        _run(migration, connection, "downgrade")
        assert _sqlite_trigger_names(connection) == triggers_before
        inspector = sa.inspect(connection)
        for table, column, _has_default in migration._COLUMNS:
            assert column in {
                column_info["name"] for column_info in inspector.get_columns(table)
            }
