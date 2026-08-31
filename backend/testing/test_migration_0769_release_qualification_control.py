"""Runtime coverage for the 0769 append-only qualification control ledger."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0769_release_qualification_control.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0769_runtime", _MIGRATION)
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


def test_0769_is_append_only_and_allows_project_cascade_delete():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
    with engine.begin() as connection:
        project = sa.Table(
            "project",
            sa.MetaData(),
            sa.Column("id", sa.Uuid(), primary_key=True),
        )
        project.create(connection)
        _run(migration, connection, "upgrade")

        inspector = sa.inspect(connection)
        assert "releasequalificationcontrolrevision" in inspector.get_table_names()
        assert {
            "trg_releasequalificationcontrolrevision_no_update",
            "trg_releasequalificationcontrolrevision_no_delete",
        } <= {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        unique_columns = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(
                "releasequalificationcontrolrevision"
            )
        }
        assert ("project_id", "revision") in unique_columns
        assert ("project_id", "idempotency_key") in unique_columns

        project_id = uuid.uuid4()
        receipt_id = uuid.uuid4()
        connection.execute(sa.insert(project).values(id=project_id))
        connection.execute(
            sa.text(
                "INSERT INTO releasequalificationcontrolrevision "
                "(id, project_id, revision, automatic_starts_enabled, "
                "idempotency_key, specification_hash, actor_user_id, reason) "
                "VALUES (:id, :project_id, 1, false, :key, :hash, :actor, :reason)"
            ),
            {
                "id": receipt_id.hex,
                "project_id": project_id.hex,
                "key": "hold-v57",
                "hash": "a" * 64,
                "actor": "owner",
                "reason": "Bound v57 canary",
            },
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "UPDATE releasequalificationcontrolrevision "
                    "SET reason = 'changed' WHERE id = :id"
                ),
                {"id": receipt_id.hex},
            )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "DELETE FROM releasequalificationcontrolrevision WHERE id = :id"
                ),
                {"id": receipt_id.hex},
            )

        connection.execute(sa.delete(project).where(project.c.id == project_id))
        remaining = connection.execute(
            sa.text("SELECT count(*) FROM releasequalificationcontrolrevision")
        ).scalar_one()
        assert remaining == 0
        _run(migration, connection, "downgrade")
        assert "releasequalificationcontrolrevision" not in sa.inspect(
            connection
        ).get_table_names()
