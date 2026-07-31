"""Executable SQLite/PostgreSQL-parity schema coverage for migration 0713."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql


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


def test_0716_adds_project_scoped_receipt_control_plane_and_reverses_cleanly():
    migration_0715 = _migration("migration_0715", "0715_strategic_context_envelope_snapshots.py")
    migration = _migration("migration_0716", "0716_strategic_memory_source_control_plane.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        sa.Table("project", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True)).create(connection)
        _run(migration_0715, connection, "upgrade")
        _run(migration, connection, "upgrade")

        inspector = sa.inspect(connection)
        table_names = set(inspector.get_table_names())
        assert {
            "strategicmemorysource",
            "strategicmemorysourcedisposition",
            "strategicmemorybackfillcursor",
            "strategicmemoryconsolidationjob",
        } <= table_names
        state_columns = {
            column["name"]
            for column in inspector.get_columns("strategicmemoryprojectstate")
        }
        assert {
            "source_high_watermark",
            "incorporated_source_sequence",
            "backfill_status",
            "extraction_status",
            "last_error",
            "attempt",
        } <= state_columns
        source_columns = {
            column["name"] for column in inspector.get_columns("strategicmemorysource")
        }
        assert {"metadata", "canonical_payload", "native_sequence", "content_hash"} <= source_columns
        source_indexes = {
            index["name"] for index in inspector.get_indexes("strategicmemorysource")
        }
        assert {
            "ix_smem_source_project_sequence",
            "ix_smem_source_project_type_key",
            "ix_smem_source_project_hash",
            "ix_smem_source_project_occurred",
        } <= source_indexes
        job_indexes = {
            index["name"]
            for index in inspector.get_indexes("strategicmemoryconsolidationjob")
        }
        assert "uq_smem_job_active_project" in job_indexes
        job_columns = {
            column["name"]
            for column in inspector.get_columns("strategicmemoryconsolidationjob")
        }
        assert {"context_authority_generation", "privacy_generation"} <= job_columns
        foreign_keys = inspector.get_foreign_keys("strategicmemorysourcedisposition")
        assert any(
            fk["referred_table"] == "strategicmemorysource"
            and fk["options"].get("ondelete") == "CASCADE"
            for fk in foreign_keys
        )

        _run(migration, connection, "downgrade")
        assert not ({
            "strategicmemorysource",
            "strategicmemorysourcedisposition",
            "strategicmemorybackfillcursor",
            "strategicmemoryconsolidationjob",
        } & set(sa.inspect(connection).get_table_names()))
        state_columns_after = {
            column["name"]
            for column in sa.inspect(connection).get_columns("strategicmemoryprojectstate")
        }
        assert "source_high_watermark" not in state_columns_after


def test_0716_json_columns_use_jsonb_on_postgresql_but_remain_sqlite_json():
    migration = _migration("migration_0716_json", "0716_strategic_memory_source_control_plane.py")
    assert isinstance(
        migration._JSON.dialect_impl(postgresql.dialect()), postgresql.JSONB
    )
    assert "JSON" in str(migration._JSON.compile(dialect=sa.create_engine("sqlite://").dialect)).upper()
