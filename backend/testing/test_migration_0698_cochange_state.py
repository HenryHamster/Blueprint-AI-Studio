"""Real PostgreSQL data-safety proof for migration 0698."""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database

_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not _USE_POSTGRES,
    reason="requires the real PostgreSQL migration harness",
)


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0698_backfill_one_sided_cochange_state.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0698", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(migration, connection):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        migration.upgrade()
    finally:
        migration.op = original_op


@requires_postgres
@pytest.mark.asyncio
async def test_cochange_backfill_is_lossless_and_authority_safe():
    migration = _migration()
    project_ids = {
        name: uuid.uuid4()
        for name in (
            "index_only",
            "applied_only",
            "old_index_only",
            "old_applied_only",
            "old_index_wins",
            "old_applied_wins",
            "authoritative",
            "unrelated",
        )
    }
    index_value = {"a.py": {"b.py": 0.8}}
    applied_value = ["owner/repo#1", "owner/repo#2"]
    old_index_value = {"old-index.py": {"dedicated.py": 0.9}}
    old_applied_value = ["old-telemetry#1"]
    authoritative_value = {
        "index": {"dedicated.py": {"wins.py": 1.0}},
        "applied_prs": ["dedicated#1"],
    }
    authoritative_id = uuid.uuid4()
    authoritative_at = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "CREATE TABLE project ("
                    "id uuid PRIMARY KEY, design_doc jsonb NOT NULL)"
                )
            )
            await connection.execute(
                sa.text(
                    "CREATE TABLE project_telemetry ("
                    "id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                    "key varchar(100) NOT NULL, value jsonb, "
                    "updated_at timestamptz NOT NULL, "
                    "CONSTRAINT uq_project_telemetry_project_key "
                    "UNIQUE (project_id, key))"
                )
            )
            project_docs = {
                "index_only": {"cochange_index": index_value},
                "applied_only": {"cochange_applied_prs": applied_value},
                "old_index_only": {"notes": "old telemetry only"},
                "old_applied_only": {"notes": "old telemetry only"},
                "old_index_wins": {
                    "cochange_index": {"stale-doc.py": {"must_not_win.py": 0.1}}
                },
                "old_applied_wins": {
                    "cochange_applied_prs": ["stale-doc#1"]
                },
                "authoritative": {
                    "cochange_index": {"legacy": {"must_not_win": 0.1}},
                },
                "unrelated": {"notes": "no co-change state"},
            }
            for name, project_id in project_ids.items():
                await connection.execute(
                    sa.text(
                        "INSERT INTO project (id, design_doc) VALUES "
                        "(CAST(:id AS uuid), CAST(:design_doc AS jsonb))"
                    ),
                    {
                        "id": project_id,
                        "design_doc": json.dumps(project_docs[name]),
                    },
                )

            old_rows = [
                ("old_index_only", "cochange_index", old_index_value),
                ("old_applied_only", "cochange_applied_prs", old_applied_value),
                ("old_index_wins", "cochange_index", old_index_value),
                ("old_applied_wins", "cochange_applied_prs", old_applied_value),
            ]
            for name, key, value in old_rows:
                await connection.execute(
                    sa.text(
                        "INSERT INTO project_telemetry "
                        "(id, project_id, key, value, updated_at) VALUES "
                        "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                        ":key, CAST(:value AS jsonb), :updated_at)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids[name],
                        "key": key,
                        "value": json.dumps(value),
                        "updated_at": authoritative_at,
                    },
                )

            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'cochange_state', CAST(:value AS jsonb), :updated_at)"
                ),
                {
                    "id": authoritative_id,
                    "project_id": project_ids["authoritative"],
                    "value": json.dumps(authoritative_value),
                    "updated_at": authoritative_at,
                },
            )

        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _run_upgrade(migration, sync_connection)
            )
            await connection.run_sync(
                lambda sync_connection: _run_upgrade(migration, sync_connection)
            )

        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT id, project_id, value, updated_at "
                            "FROM project_telemetry "
                            "WHERE key = 'cochange_state' ORDER BY project_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            by_project = {str(row["project_id"]): row for row in rows}
            assert len(rows) == 7
            expected = {
                "index_only": {"index": index_value, "applied_prs": []},
                "applied_only": {"index": {}, "applied_prs": applied_value},
                "old_index_only": {"index": old_index_value, "applied_prs": []},
                "old_applied_only": {
                    "index": {},
                    "applied_prs": old_applied_value,
                },
                "old_index_wins": {"index": old_index_value, "applied_prs": []},
                "old_applied_wins": {
                    "index": {},
                    "applied_prs": old_applied_value,
                },
            }
            for name, value in expected.items():
                assert by_project[str(project_ids[name])]["value"] == value

            authoritative = by_project[str(project_ids["authoritative"])]
            assert authoritative["id"] == authoritative_id
            assert authoritative["value"] == authoritative_value
            assert authoritative["updated_at"] == authoritative_at
            assert str(project_ids["unrelated"]) not in by_project


def test_downgrade_is_non_destructive():
    migration = _migration()
    assert migration.downgrade() is None
