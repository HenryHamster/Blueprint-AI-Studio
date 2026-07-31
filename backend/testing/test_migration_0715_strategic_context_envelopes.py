"""Executable schema coverage for migration 0715."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path(__file__).resolve().parents[1] / (
        "alembic/versions/0715_strategic_context_envelope_snapshots.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0715", path)
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


def test_upgrade_and_downgrade_create_only_additive_envelope_schema():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        sa.Table("project", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True)).create(connection)
        _run(migration, connection, "upgrade")

        inspector = sa.inspect(connection)
        state_columns = {column["name"] for column in inspector.get_columns("strategicmemoryprojectstate")}
        snapshot_columns = {column["name"] for column in inspector.get_columns("strategiccontextenvelopesnapshot")}
        assert {"project_id", "context_authority_generation", "privacy_generation"} <= state_columns
        assert {
            "snapshot_id", "canonical_envelope", "canonical_sha256",
            "worker_incarnation", "retrieval_trace", "expires_at",
        } <= snapshot_columns
        index_names = {index["name"] for index in inspector.get_indexes("strategiccontextenvelopesnapshot")}
        assert "ix_sctx_project_expires" in index_names

        _run(migration, connection, "downgrade")
        assert "strategicmemoryprojectstate" not in sa.inspect(connection).get_table_names()
        assert "strategiccontextenvelopesnapshot" not in sa.inspect(connection).get_table_names()
