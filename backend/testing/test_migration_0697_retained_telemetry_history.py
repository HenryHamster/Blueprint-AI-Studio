"""Executable coverage for migration 0697's preserving history backfill."""

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
        / "0697_backfill_retained_telemetry_history.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0697", path)
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
    telemetry = sa.Table(
        "project_telemetry",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "key"),
    )
    efficiency = sa.Table(
        "project_efficiency_snapshot",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_def_version", sa.String(), nullable=False),
        sa.Column("blob", sa.JSON(), nullable=False),
        sa.UniqueConstraint("project_id", "captured_at"),
    )
    lane = sa.Table(
        "project_lane_baseline",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_def_version", sa.String(), nullable=False),
        sa.Column("blob", sa.JSON(), nullable=False),
        sa.UniqueConstraint("project_id", "version_number"),
    )
    metadata.create_all(connection)
    return project, telemetry, efficiency, lane


def _run_upgrade(migration, connection):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        migration.upgrade()
    finally:
        migration.op = original_op


def _rows(connection, table, *order_by):
    statement = sa.select(table)
    if order_by:
        statement = statement.order_by(*order_by)
    return connection.execute(statement).mappings().all()


def test_upgrade_recovers_retained_history_and_is_idempotent():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, telemetry, efficiency, lane = _schema(connection)
        legacy_doc = {
            "efficiency_baseline": {
                "baseline": {"cost": 3},
                "snapshots": [
                    {
                        "captured_at": "2026-07-20T00:00:00+00:00",
                        "metric_def_version": "v15",
                        "cost": 4,
                    },
                    {
                        "captured_at": "2026-07-21T00:00:00+00:00",
                        "cost": 5,
                    },
                ],
            },
            "lane_baselines": {
                "1": {
                    "captured_at": "2026-07-20T01:00:00+00:00",
                    "baseline_def_version": "v1",
                    "pr_count": 2,
                },
                "2": {
                    "captured_at": "2026-07-21T01:00:00+00:00",
                    "pr_count": 3,
                },
            },
            "unrelated": {"must_stay_only_in_design_doc": True},
        }
        connection.execute(
            project.insert(),
            {"id": "legacy", "design_doc": legacy_doc},
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        telemetry_rows = _rows(
            connection, telemetry, telemetry.c.project_id, telemetry.c.key
        )
        assert len(telemetry_rows) == 1
        assert telemetry_rows[0]["key"] == "efficiency_baseline"
        assert telemetry_rows[0]["value"] == legacy_doc["efficiency_baseline"]

        efficiency_rows = _rows(
            connection, efficiency, efficiency.c.captured_at
        )
        assert len(efficiency_rows) == 2
        assert [row["metric_def_version"] for row in efficiency_rows] == [
            "v15",
            "legacy",
        ]
        assert [row["blob"]["cost"] for row in efficiency_rows] == [4, 5]

        lane_rows = _rows(connection, lane, lane.c.version_number)
        assert len(lane_rows) == 2
        assert [row["version_number"] for row in lane_rows] == [1, 2]
        assert [row["baseline_def_version"] for row in lane_rows] == [
            "v1",
            "legacy",
        ]
        assert [row["blob"]["pr_count"] for row in lane_rows] == [2, 3]

        stored_doc = connection.execute(
            sa.select(project.c.design_doc).where(project.c.id == "legacy")
        ).scalar_one()
        assert stored_doc == legacy_doc


def test_upgrade_preserves_existing_dedicated_values():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, telemetry, efficiency, lane = _schema(connection)
        captured = datetime(2026, 7, 20, tzinfo=timezone.utc)
        connection.execute(
            project.insert(),
            {
                "id": "dedicated",
                "design_doc": {
                    "efficiency_baseline": {
                        "baseline": {"source": "legacy"},
                        "snapshots": [
                            {
                                "captured_at": captured.isoformat(),
                                "metric_def_version": "legacy-version",
                                "source": "legacy",
                            }
                        ],
                    },
                    "lane_baselines": {
                        "7": {
                            "captured_at": captured.isoformat(),
                            "baseline_def_version": "legacy-version",
                            "source": "legacy",
                        }
                    },
                },
            },
        )
        connection.execute(
            telemetry.insert(),
            {
                "id": "telemetry-existing",
                "project_id": "dedicated",
                "key": "efficiency_baseline",
                "value": {"source": "dedicated"},
                "updated_at": captured,
            },
        )
        connection.execute(
            efficiency.insert(),
            {
                "id": "efficiency-existing",
                "project_id": "dedicated",
                "captured_at": captured,
                "metric_def_version": "dedicated-version",
                "blob": {"source": "dedicated"},
            },
        )
        connection.execute(
            lane.insert(),
            {
                "id": "lane-existing",
                "project_id": "dedicated",
                "version_number": 7,
                "captured_at": captured,
                "baseline_def_version": "dedicated-version",
                "blob": {"source": "dedicated"},
            },
        )

        _run_upgrade(migration, connection)
        _run_upgrade(migration, connection)

        assert _rows(connection, telemetry)[0]["value"] == {
            "source": "dedicated"
        }
        assert _rows(connection, efficiency)[0]["blob"] == {
            "source": "dedicated"
        }
        assert _rows(connection, lane)[0]["blob"] == {"source": "dedicated"}


def test_upgrade_skips_malformed_entries_without_stripping_fallback():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, telemetry, efficiency, lane = _schema(connection)
        malformed_doc = {
            "efficiency_baseline": {
                "baseline": {"still": "recoverable"},
                "snapshots": [
                    {"captured_at": "not-a-date", "metric_def_version": "v1"},
                    "not-an-object",
                ],
            },
            "lane_baselines": {
                "not-a-version": {"captured_at": "2026-07-20T00:00:00+00:00"},
                "3": {"captured_at": "not-a-date"},
                "04": {
                    "captured_at": "2026-07-20T00:00:00+00:00",
                    "baseline_def_version": "v1",
                },
            },
        }
        connection.execute(
            project.insert(),
            {"id": "malformed", "design_doc": malformed_doc},
        )

        _run_upgrade(migration, connection)

        assert _rows(connection, telemetry)[0]["value"] == {
            "baseline": {"still": "recoverable"},
            "snapshots": [
                {"captured_at": "not-a-date", "metric_def_version": "v1"},
                "not-an-object",
            ],
        }
        assert _rows(connection, efficiency) == []
        assert _rows(connection, lane) == []
        assert connection.execute(
            sa.select(project.c.design_doc).where(project.c.id == "malformed")
        ).scalar_one() == malformed_doc


def test_downgrade_is_non_destructive():
    migration = _migration()
    assert migration.downgrade() is None


def test_schema_bound_normalizers_match_rebuilt_table_limits():
    migration = _migration()

    assert migration._version_number("2147483647") == 2_147_483_647
    assert migration._version_number("2147483648") is None
    assert migration._definition_version("v1") == "v1"
    assert migration._definition_version("x" * 65) == "legacy"


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_skips_integer_overflow_and_normalizes_long_def_versions():
    migration = _migration()
    project_id = uuid.uuid4()
    captured_at = "2026-07-23T18:00:00+00:00"
    long_version = "x" * 65
    design_doc = {
        "efficiency_baseline": {
            "snapshots": [
                {
                    "captured_at": captured_at,
                    "metric_def_version": long_version,
                    "score": 1,
                }
            ]
        },
        "lane_baselines": {
            "2147483647": {
                "captured_at": captured_at,
                "baseline_def_version": long_version,
                "score": 2,
            },
            "2147483648": {
                "captured_at": captured_at,
                "baseline_def_version": "overflow",
                "score": 3,
            },
        },
    }

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
                    "UNIQUE (project_id, key))"
                )
            )
            await connection.execute(
                sa.text(
                    "CREATE TABLE project_efficiency_snapshot ("
                    "id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                    "captured_at timestamptz NOT NULL, "
                    "metric_def_version varchar(64) NOT NULL, blob jsonb NOT NULL, "
                    "UNIQUE (project_id, captured_at))"
                )
            )
            await connection.execute(
                sa.text(
                    "CREATE TABLE project_lane_baseline ("
                    "id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                    "version_number integer NOT NULL, "
                    "captured_at timestamptz NOT NULL, "
                    "baseline_def_version varchar(64) NOT NULL, "
                    "blob jsonb NOT NULL, UNIQUE (project_id, version_number))"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project (id, design_doc) VALUES "
                    "(CAST(:id AS uuid), CAST(:design_doc AS jsonb))"
                ),
                {"id": project_id, "design_doc": json.dumps(design_doc)},
            )

        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _run_upgrade(migration, sync_connection)
            )

        async with engine.connect() as connection:
            efficiency_row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT metric_def_version, blob "
                            "FROM project_efficiency_snapshot"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert efficiency_row["metric_def_version"] == "legacy"
            assert efficiency_row["blob"]["metric_def_version"] == long_version

            lane_rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT version_number, baseline_def_version, blob "
                            "FROM project_lane_baseline ORDER BY version_number"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert len(lane_rows) == 1
            assert lane_rows[0]["version_number"] == 2_147_483_647
            assert lane_rows[0]["baseline_def_version"] == "legacy"
            assert lane_rows[0]["blob"]["baseline_def_version"] == long_version

            telemetry_value = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key = 'efficiency_baseline'"
                ),
                {"project_id": project_id},
            )
            assert telemetry_value == design_doc["efficiency_baseline"]
