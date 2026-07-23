"""Executable coverage for migration 0696's preserving SR backfill."""

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
        / "0696_backfill_project_sr_analytics.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0696", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("design_doc", sa.JSON(), nullable=False),
    )
    sr = sa.Table(
        "project_sr_analytics",
        metadata,
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    metadata.create_all(connection)
    return project, sr


def _run_upgrade(migration, connection):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        migration.upgrade()
    finally:
        migration.op = original_op


def test_upgrade_backfills_only_sr_keys_and_dedicated_values_win():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, sr = _schema(connection)
        connection.execute(
            project.insert(),
            [
                {
                    "id": "legacy-only",
                    "design_doc": {
                        "sr_catch_rate_trends": {"source": "legacy"},
                        "sr_engagement_threshold": 0.8,
                        "unrelated": {"must_not_move": True},
                    },
                },
                {
                    "id": "partial-dedicated",
                    "design_doc": {
                        "sr_catch_rate_trends": {"source": "legacy"},
                        "sr_floor_threshold": 0.6,
                    },
                },
                {"id": "no-sr", "design_doc": {"notes": "leave me"}},
            ],
        )
        connection.execute(
            sr.insert(),
            {
                "project_id": "partial-dedicated",
                "data": {"sr_catch_rate_trends": {"source": "dedicated"}},
                "updated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
            },
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        rows = {
            row.project_id: row.data
            for row in connection.execute(sa.select(sr)).all()
        }
        assert rows["legacy-only"] == {
            "sr_catch_rate_trends": {"source": "legacy"},
            "sr_engagement_threshold": 0.8,
        }
        assert rows["partial-dedicated"] == {
            "sr_catch_rate_trends": {"source": "dedicated"},
            "sr_floor_threshold": 0.6,
        }
        assert "no-sr" not in rows
        assert "unrelated" not in rows["legacy-only"]


def test_legacy_payload_accepts_json_text_and_preserves_falsey_values():
    migration = _migration()

    payload = migration._legacy_sr_payload(
        json.dumps(
            {
                "sr_floor_threshold": 0,
                "sr_engagement_min_passes": False,
                "sr_catch_rate_trends": {},
                "unrelated": "ignored",
            }
        )
    )

    assert payload == {
        "sr_floor_threshold": 0,
        "sr_engagement_min_passes": False,
        "sr_catch_rate_trends": {},
    }


def test_downgrade_is_non_destructive():
    migration = _migration()
    assert migration.downgrade() is None


def test_migration_key_inventory_matches_runtime_inventory():
    migration = _migration()
    from app.sr_analytics_store import LEGACY_SR_ANALYTICS_KEYS

    assert migration.LEGACY_SR_ANALYTICS_KEYS == LEGACY_SR_ANALYTICS_KEYS


def test_upgrade_backfills_every_documented_legacy_key():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    all_legacy_values = {
        key: {"inventory_key": key}
        for key in sorted(migration.LEGACY_SR_ANALYTICS_KEYS)
    }
    with engine.begin() as connection:
        project, sr = _schema(connection)
        connection.execute(
            project.insert(),
            {
                "id": "all-legacy-keys",
                "design_doc": {
                    **all_legacy_values,
                    "unrelated": {"must_not_move": True},
                },
            },
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        rows = connection.execute(sa.select(sr)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["data"] == all_legacy_values
        assert set(rows[0]["data"]) == migration.LEGACY_SR_ANALYTICS_KEYS
        assert "unrelated" not in rows[0]["data"]


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_preserves_authoritative_non_mapping_jsonb_rows():
    migration = _migration()
    authoritative_at = datetime(2026, 7, 23, 17, 0, tzinfo=timezone.utc)
    raw_values = {
        "list": ["dedicated", {"shape": "list"}],
        "string": "dedicated-string",
        "number": 7,
        "boolean": False,
        "null": None,
    }
    project_ids = {name: uuid.uuid4() for name in raw_values}

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
                    "CREATE TABLE project_sr_analytics ("
                    "project_id uuid PRIMARY KEY, data jsonb NOT NULL, "
                    "updated_at timestamptz NOT NULL)"
                )
            )
            for name, project_id in project_ids.items():
                await connection.execute(
                    sa.text(
                        "INSERT INTO project (id, design_doc) VALUES "
                        "(CAST(:id AS uuid), CAST(:design_doc AS jsonb))"
                    ),
                    {
                        "id": project_id,
                        "design_doc": json.dumps(
                            {"sr_floor_threshold": {"legacy": name}}
                        ),
                    },
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO project_sr_analytics "
                        "(project_id, data, updated_at) VALUES "
                        "(CAST(:project_id AS uuid), CAST(:data AS jsonb), "
                        ":updated_at)"
                    ),
                    {
                        "project_id": project_id,
                        "data": json.dumps(raw_values[name]),
                        "updated_at": authoritative_at,
                    },
                )

        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _run_upgrade(migration, sync_connection)
            )

        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, data, updated_at "
                            "FROM project_sr_analytics ORDER BY project_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
            by_project = {str(row["project_id"]): row for row in rows}
            for name, project_id in project_ids.items():
                row = by_project[str(project_id)]
                assert row["data"] == raw_values[name]
                assert row["updated_at"] == authoritative_at
