"""SQLite parity coverage for the additive 0715 Strategic Memory ledger."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

import pytest
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


def test_0717_adds_project_scoped_semantic_ledger_and_reverses_cleanly():
    migration_0715 = _migration("migration_0715", "0715_strategic_context_envelope_snapshots.py")
    migration_0716 = _migration("migration_0716", "0716_strategic_memory_source_control_plane.py")
    migration = _migration("migration_0717", "0717_strategic_memory_semantic_ledger.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        sa.Table("project", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True)).create(connection)
        _run(migration_0715, connection, "upgrade")
        _run(migration_0716, connection, "upgrade")
        _run(migration, connection, "upgrade")

        inspector = sa.inspect(connection)
        assert {
            "strategicmemoryitem",
            "strategicmemoryitemversion",
            "strategicmemoryitemsource",
            "strategicmemorytransition",
            "strategicmemorycorrectionbarrier",
            "strategicmemorycorrectionbarrierevent",
            "strategicmemoryedge",
            "strategicmemoryedgesource",
            "strategicmemoryprojection",
        } <= set(inspector.get_table_names())
        state_columns = {
            column["name"]
            for column in inspector.get_columns("strategicmemoryprojectstate")
        }
        assert "ledger_revision" in state_columns
        job_columns = {
            column["name"]
            for column in inspector.get_columns("strategicmemoryconsolidationjob")
        }
        assert {
            "accepted_proposal_idempotency_key",
            "accepted_proposal_hash",
            "proposal_schema_version",
            "resulting_ledger_revision",
        } <= job_columns
        job_indexes = {
            index["name"]
            for index in inspector.get_indexes("strategicmemoryconsolidationjob")
        }
        assert "uq_smem_job_project_accepted_proposal" in job_indexes
        version_fks = inspector.get_foreign_keys("strategicmemoryitemversion")
        assert any(
            fk["constrained_columns"] == ["project_id", "item_id"]
            and fk["referred_table"] == "strategicmemoryitem"
            for fk in version_fks
        )
        edge_fks = inspector.get_foreign_keys("strategicmemoryedge")
        assert sum(
            fk["referred_table"] == "strategicmemoryitemversion"
            for fk in edge_fks
        ) == 2
        source_fks = inspector.get_foreign_keys("strategicmemoryitemsource")
        assert any(
            fk["constrained_columns"] == ["project_id", "source_id"]
            and fk["referred_table"] == "strategicmemorysource"
            for fk in source_fks
        )

        trigger_names = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert "trg_strategicmemoryitemversion_no_update" in trigger_names
        assert "trg_strategicmemorycorrectionbarrierevent_no_delete" in trigger_names

        _run(migration, connection, "downgrade")
        assert not ({
            "strategicmemoryitem",
            "strategicmemoryitemversion",
            "strategicmemoryitemsource",
            "strategicmemorytransition",
            "strategicmemorycorrectionbarrier",
            "strategicmemorycorrectionbarrierevent",
            "strategicmemoryedge",
            "strategicmemoryedgesource",
            "strategicmemoryprojection",
        } & set(sa.inspect(connection).get_table_names()))


def test_0717_array_and_json_columns_have_postgresql_sqlite_parity():
    migration = _migration("migration_0717_types", "0717_strategic_memory_semantic_ledger.py")
    assert isinstance(
        migration._JSON.dialect_impl(postgresql.dialect()), postgresql.JSONB
    )
    assert isinstance(
        migration._TEXT_ARRAY.dialect_impl(postgresql.dialect()), postgresql.ARRAY
    )
    sqlite_dialect = sa.create_engine("sqlite://").dialect
    assert "JSON" in str(migration._TEXT_ARRAY.compile(dialect=sqlite_dialect)).upper()


def test_0717_rejects_duplicate_project_scope_and_cascades_project_delete():
    migration_0715 = _migration("migration_0715_scope", "0715_strategic_context_envelope_snapshots.py")
    migration_0716 = _migration("migration_0716_scope", "0716_strategic_memory_source_control_plane.py")
    migration = _migration("migration_0717_scope", "0717_strategic_memory_semantic_ledger.py")
    engine = sa.create_engine("sqlite://")
    with engine.connect() as setup_connection:
        setup_connection.execute(sa.text("PRAGMA foreign_keys = ON"))
    project_id = uuid.uuid4().hex
    item_id = uuid.uuid4().hex
    version_id = {"item_id": item_id, "version": 1}
    with engine.begin() as connection:
        sa.Table("project", sa.MetaData(), sa.Column("id", sa.Uuid(), primary_key=True)).create(connection)
        _run(migration_0715, connection, "upgrade")
        _run(migration_0716, connection, "upgrade")
        _run(migration, connection, "upgrade")
        project = sa.table("project", sa.column("id"))
        item = sa.table(
            "strategicmemoryitem",
            sa.column("id"), sa.column("project_id"), sa.column("scope_type"),
            sa.column("scope_key"), sa.column("kind"), sa.column("logical_key"),
            sa.column("current_version"), sa.column("current_status"),
        )
        version = sa.table(
            "strategicmemoryitemversion",
            sa.column("item_id"), sa.column("version"), sa.column("project_id"),
            sa.column("canonical_text"), sa.column("structured_value"),
            sa.column("epistemic_status"), sa.column("confidence"),
            sa.column("recorded_source_sequence"), sa.column("exact_terms"),
            sa.column("formula_fingerprints"), sa.column("content_hash"),
        )
        projection = sa.table(
            "strategicmemoryprojection",
            sa.column("project_id"), sa.column("ledger_revision"),
            sa.column("through_source_sequence"), sa.column("structured_state"),
            sa.column("canonical_text"), sa.column("canonical_sha256"),
        )
        connection.execute(sa.insert(project).values(id=project_id))
        item_values = {
            "id": item_id,
            "project_id": project_id,
            "scope_type": "project",
            "scope_key": "",
            "kind": "claim",
            "logical_key": "eta",
            "current_version": 1,
            "current_status": "active",
        }
        connection.execute(sa.insert(item).values(**item_values))
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.insert(item).values(**{**item_values, "id": uuid.uuid4().hex}))
        connection.execute(
            sa.insert(version).values(
                **version_id,
                project_id=project_id,
                canonical_text="eta = 0.2",
                structured_value=json.dumps({}),
                epistemic_status="operator_explicit",
                confidence=1,
                recorded_source_sequence=1,
                exact_terms=json.dumps(["eta"]),
                formula_fingerprints=json.dumps([]),
                content_hash="a" * 64,
            )
        )
        connection.execute(
            sa.insert(projection).values(
                project_id=project_id,
                ledger_revision=1,
                through_source_sequence=1,
                structured_state=json.dumps({}),
                canonical_text="eta = 0.2",
                canonical_sha256="b" * 64,
            )
        )
        # Immutable version rows cannot be updated in place.
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.update(version)
                .where(version.c.item_id == item_id, version.c.version == 1)
                .values(canonical_text="eta = 0.3")
            )
        connection.execute(sa.delete(project).where(project.c.id == project_id))
        assert connection.execute(sa.select(sa.func.count()).select_from(item)).scalar_one() == 0
        assert connection.execute(sa.select(sa.func.count()).select_from(version)).scalar_one() == 0
        assert connection.execute(sa.select(sa.func.count()).select_from(projection)).scalar_one() == 0
