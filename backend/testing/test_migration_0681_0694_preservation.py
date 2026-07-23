"""Real PostgreSQL preservation proofs for the 0681 and 0694 rebuilds."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_LOCATION = _BACKEND_ROOT / "alembic"
_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not _USE_POSTGRES,
    reason="requires the real PostgreSQL migration harness",
)


def _migrate_to(
    sync_connection: sa.Connection,
    destination: str,
    *,
    downgrade: bool = False,
) -> None:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    script = ScriptDirectory.from_config(config)

    def migrations_from_current(current_revision, _context):
        if downgrade:
            return script._downgrade_revs(destination, current_revision)
        return script._upgrade_revs(destination, current_revision)

    with EnvironmentContext(
        config,
        script,
        fn=migrations_from_current,
        as_sql=False,
        destination_rev=destination,
    ) as environment:
        environment.configure(
            connection=sync_connection,
            transaction_per_migration=True,
        )
        with environment.begin_transaction():
            environment.run_migrations()


async def _migrate(engine, destination: str, *, downgrade: bool = False) -> None:
    async with engine.connect() as connection:
        await connection.run_sync(
            lambda sync_connection: _migrate_to(
                sync_connection,
                destination,
                downgrade=downgrade,
            )
        )


async def _insert_projects(connection, project_ids: dict[str, uuid.UUID]) -> None:
    await connection.execute(
        sa.text(
            "INSERT INTO project "
            "(id, name, user_id, status, created_at, design_doc) "
            "VALUES (CAST(:id AS uuid), :name, 'test-user', 'active', "
            "CURRENT_TIMESTAMP, '{}'::jsonb)"
        ),
        [{"id": project_id, "name": name} for name, project_id in project_ids.items()],
    )


async def _constraint_names(connection, table_name: str) -> set[str]:
    rows = await connection.execute(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = CAST(:table_name AS regclass)"
        ),
        {"table_name": table_name},
    )
    return set(rows.scalars())


@requires_postgres
@pytest.mark.asyncio
async def test_0681_preserves_and_explodes_every_legacy_lane_shape():
    project_ids = {
        name: uuid.uuid4()
        for name in (
            "map",
            "list",
            "null",
            "conflict",
            "malformed_live",
            "uncapped",
        )
    }
    updated_at = datetime(2026, 7, 23, 18, 0, tzinfo=timezone.utc)
    lane_map = {
        "1": {
            "captured_at": "2026-07-23T10:00:00Z",
            "baseline_def_version": "lane-v1",
            "score": 1,
        },
        "01": {"score": "noncanonical-key"},
        str(2_147_483_648): {"score": "outside-pg-integer"},
        "2": ["not-an-object"],
        "3": {
            "captured_at": "not-a-timestamp",
            "baseline_def_version": "x" * 65,
            "score": 3,
        },
    }
    uncapped_map = {
        str(version): {
            "captured_at": f"2026-07-{(version % 20) + 1:02d}T00:00:00Z",
            "baseline_def_version": "bulk",
            "version": version,
        }
        for version in range(25)
    }
    conflict_raw = {"4": {"score": "old-row"}}
    authoritative = {"authoritative": {"score": "newer-telemetry"}}
    malformed_live_raw = {"5": {"score": "recovered-old-row"}}

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        await _migrate(engine, "0680")
        async with engine.begin() as connection:
            await _insert_projects(connection, project_ids)
            await connection.execute(
                sa.text(
                    "INSERT INTO project_lane_baseline "
                    "(project_id, data, updated_at) VALUES "
                    "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
                ),
                [
                    {
                        "project_id": project_ids["map"],
                        "data": json.dumps(lane_map),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["list"],
                        "data": json.dumps(["raw", {"still": "recoverable"}]),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["null"],
                        "data": json.dumps(None),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["conflict"],
                        "data": json.dumps(conflict_raw),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["malformed_live"],
                        "data": json.dumps(malformed_live_raw),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["uncapped"],
                        "data": json.dumps(uncapped_map),
                        "updated_at": updated_at,
                    },
                ],
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'lane_baselines', CAST(:value AS jsonb), :updated_at)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["conflict"],
                        "value": json.dumps(authoritative),
                        "updated_at": updated_at.replace(hour=19),
                    },
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["malformed_live"],
                        "value": json.dumps(["malformed-live"]),
                        "updated_at": updated_at.replace(hour=20),
                    },
                ],
            )

        await _migrate(engine, "0681")

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT to_regclass("
                        "'project_lane_baseline_legacy_0681')"
                    )
                )
                is None
            )
            constraints = await _constraint_names(connection, "project_lane_baseline")
            assert "project_lane_baseline_pkey" in constraints
            assert "uq_proj_lane_baseline" in constraints

            lane_rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT version_number, captured_at, "
                            "baseline_def_version, blob "
                            "FROM project_lane_baseline "
                            "WHERE project_id = CAST(:project_id AS uuid) "
                            "ORDER BY version_number"
                        ),
                        {"project_id": project_ids["map"]},
                    )
                )
                .mappings()
                .all()
            )
            assert [row["version_number"] for row in lane_rows] == [1, 3]
            assert lane_rows[0]["baseline_def_version"] == "lane-v1"
            assert lane_rows[0]["blob"] == lane_map["1"]
            assert lane_rows[1]["captured_at"] == updated_at
            assert lane_rows[1]["baseline_def_version"] == "legacy"
            assert lane_rows[1]["blob"] == lane_map["3"]
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM project_lane_baseline "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids["uncapped"]},
                )
                == 25
            )

            raw_live = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key = 'lane_baselines'"
                ),
                {"project_id": project_ids["map"]},
            )
            assert raw_live == lane_map
            conflict_live = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key = 'lane_baselines'"
                ),
                {"project_id": project_ids["conflict"]},
            )
            assert conflict_live == {**conflict_raw, **authoritative}
            conflict_archive = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key LIKE 'migration_0681_lane_baseline_raw%'"
                ),
                {"project_id": project_ids["conflict"]},
            )
            assert conflict_archive == conflict_raw
            repaired_live = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key = 'lane_baselines'"
                ),
                {"project_id": project_ids["malformed_live"]},
            )
            assert repaired_live == malformed_live_raw
            repaired_archives = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT value, updated_at FROM project_telemetry "
                            "WHERE project_id = CAST(:project_id AS uuid) "
                            "AND key LIKE "
                            "'migration_0681_lane_baseline_raw%' "
                            "ORDER BY key"
                        ),
                        {"project_id": project_ids["malformed_live"]},
                    )
                )
                .mappings()
                .all()
            )
            assert [row["value"] for row in repaired_archives] == [
                malformed_live_raw,
                ["malformed-live"],
            ]
            assert repaired_archives[1]["updated_at"] == updated_at.replace(hour=20)
            malformed_values = {
                str(row.project_id): row.value
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, value FROM project_telemetry "
                            "WHERE key LIKE "
                            "'migration_0681_lane_baseline_raw%' "
                            "AND project_id IN "
                            "(CAST(:list_id AS uuid), CAST(:null_id AS uuid))"
                        ),
                        {
                            "list_id": project_ids["list"],
                            "null_id": project_ids["null"],
                        },
                    )
                )
            }
            assert malformed_values[str(project_ids["list"])] == [
                "raw",
                {"still": "recoverable"},
            ]
            assert malformed_values[str(project_ids["null"])] is None

        await _migrate(engine, "0680", downgrade=True)
        async with engine.connect() as connection:
            restored_map = await connection.scalar(
                sa.text(
                    "SELECT data FROM project_lane_baseline "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_ids["map"]},
            )
            assert restored_map == lane_map
            restored_uncapped = await connection.scalar(
                sa.text(
                    "SELECT data FROM project_lane_baseline "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_ids["uncapped"]},
            )
            assert len(restored_uncapped) == 25
            for name, expected in (
                ("list", ["raw", {"still": "recoverable"}]),
                ("null", None),
            ):
                assert await connection.scalar(
                    sa.text(
                        "SELECT data "
                        "FROM project_lane_baseline "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids[name]},
                ) == expected

        await _migrate(engine, "0681")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM project_lane_baseline "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids["uncapped"]},
                )
                == 25
            )


@requires_postgres
@pytest.mark.asyncio
async def test_0694_preserves_containers_conflicts_and_timestamp_collisions():
    project_ids = {
        name: uuid.uuid4()
        for name in (
            "container",
            "direct",
            "list",
            "null",
            "conflict",
            "malformed_live",
        )
    }
    updated_at = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
    container = {
        "baseline": {"immutable": True},
        "last_run_at": "2026-07-20T00:00:00Z",
        "marker": {"keep": "exact"},
        "snapshots": [
            {
                "captured_at": "2026-07-23T10:00:00Z",
                "metric_def_version": "first",
                "score": 1,
            },
            {
                "captured_at": "2026-07-23T02:00:00-08:00",
                "metric_def_version": "winner",
                "score": 2,
            },
            "malformed-snapshot",
            {
                "captured_at": "not-a-timestamp",
                "metric_def_version": "x" * 65,
                "score": 3,
            },
        ],
    }
    direct = {
        "captured_at": "2026-07-22T00:00:00Z",
        "metric_def_version": "direct-v1",
        "score": 9,
    }
    conflict_raw = {"metric_def_version": "old", "score": 4}
    authoritative = {"baseline": {"source": "newer"}, "snapshots": []}
    malformed_live_raw = {"metric_def_version": "recovered", "score": 5}

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        await _migrate(engine, "0692")
        async with engine.begin() as connection:
            await _insert_projects(connection, project_ids)
            await connection.execute(
                sa.text(
                    "INSERT INTO project_efficiency_snapshot "
                    "(project_id, data, updated_at) VALUES "
                    "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
                ),
                [
                    {
                        "project_id": project_ids["container"],
                        "data": json.dumps(container),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["direct"],
                        "data": json.dumps(direct),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["list"],
                        "data": json.dumps(["raw", 7]),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["null"],
                        "data": json.dumps(None),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["malformed_live"],
                        "data": json.dumps(malformed_live_raw),
                        "updated_at": updated_at,
                    },
                    {
                        "project_id": project_ids["conflict"],
                        "data": json.dumps(conflict_raw),
                        "updated_at": updated_at,
                    },
                ],
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'efficiency_baseline', CAST(:value AS jsonb), :updated_at)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["conflict"],
                        "value": json.dumps(authoritative),
                        "updated_at": updated_at.replace(hour=19),
                    },
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["malformed_live"],
                        "value": json.dumps("malformed-live"),
                        "updated_at": updated_at.replace(hour=20),
                    },
                ],
            )

        await _migrate(engine, "0694")

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT to_regclass("
                        "'project_efficiency_snapshot_legacy_0694')"
                    )
                )
                is None
            )
            constraints = await _constraint_names(
                connection,
                "project_efficiency_snapshot",
            )
            assert "project_efficiency_snapshot_pkey" in constraints
            assert "uq_proj_efficiency_snap" in constraints

            rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT captured_at, metric_def_version, blob "
                            "FROM project_efficiency_snapshot "
                            "WHERE project_id = CAST(:project_id AS uuid) "
                            "ORDER BY captured_at"
                        ),
                        {"project_id": project_ids["container"]},
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 2
            assert rows[0]["blob"]["score"] == 2
            assert rows[0]["metric_def_version"] == "winner"
            assert rows[1]["captured_at"] == updated_at
            assert rows[1]["blob"]["score"] == 3
            assert rows[1]["metric_def_version"] == "legacy"
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT blob FROM project_efficiency_snapshot "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids["direct"]},
                )
                == direct
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT value FROM project_telemetry "
                        "WHERE project_id = CAST(:project_id AS uuid) "
                        "AND key = 'efficiency_baseline'"
                    ),
                    {"project_id": project_ids["container"]},
                )
                == container
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT value FROM project_telemetry "
                        "WHERE project_id = CAST(:project_id AS uuid) "
                        "AND key = 'efficiency_baseline'"
                    ),
                    {"project_id": project_ids["conflict"]},
                )
                == {**conflict_raw, **authoritative}
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT value FROM project_telemetry "
                        "WHERE project_id = CAST(:project_id AS uuid) "
                        "AND key LIKE "
                        "'migration_0694_efficiency_snapshot_raw%'"
                    ),
                    {"project_id": project_ids["conflict"]},
                )
                == conflict_raw
            )
            repaired_live = await connection.scalar(
                sa.text(
                    "SELECT value FROM project_telemetry "
                    "WHERE project_id = CAST(:project_id AS uuid) "
                    "AND key = 'efficiency_baseline'"
                ),
                {"project_id": project_ids["malformed_live"]},
            )
            assert repaired_live == malformed_live_raw
            repaired_archives = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT value, updated_at FROM project_telemetry "
                            "WHERE project_id = CAST(:project_id AS uuid) "
                            "AND key LIKE "
                            "'migration_0694_efficiency_snapshot_raw%' "
                            "ORDER BY key"
                        ),
                        {"project_id": project_ids["malformed_live"]},
                    )
                )
                .mappings()
                .all()
            )
            assert [row["value"] for row in repaired_archives] == [
                malformed_live_raw,
                "malformed-live",
            ]
            assert repaired_archives[1]["updated_at"] == updated_at.replace(hour=20)

        await _migrate(engine, "0692", downgrade=True)
        async with engine.connect() as connection:
            restored = await connection.scalar(
                sa.text(
                    "SELECT data FROM project_efficiency_snapshot "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_ids["container"]},
            )
            assert restored == container
            restored_direct = await connection.scalar(
                sa.text(
                    "SELECT data FROM project_efficiency_snapshot "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_ids["direct"]},
            )
            assert restored_direct == direct
            for name, expected in (
                ("list", ["raw", 7]),
                ("null", None),
            ):
                assert await connection.scalar(
                    sa.text(
                        "SELECT data FROM project_efficiency_snapshot "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids[name]},
                ) == expected

        await _migrate(engine, "0694")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM project_efficiency_snapshot "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids["container"]},
                )
                == 2
            )


@requires_postgres
@pytest.mark.asyncio
async def test_repeated_rebuilds_rotate_current_raw_and_never_prune():
    project_ids = {
        name: uuid.uuid4()
        for name in (
            "lane_list",
            "lane_null",
            "lane_future_capture",
            "eff_list",
            "eff_null",
            "eff_many",
            "eff_direct",
            "live_newer",
            "live_older",
        )
    }
    original_at = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    mutated_at = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)
    lane_conflict_raw = {
        "1": {"source": "dedicated"},
        "2": {"raw_only": True},
    }
    lane_newer_live = {
        "1": {"source": "live-newer"},
        "3": {"live_only": "newer"},
    }
    lane_older_live = {
        "1": {"source": "live-older"},
        "3": {"live_only": "older"},
    }
    future_lane = {
        "9": {
            "captured_at": "2026-07-30T12:00:00Z",
            "baseline_def_version": "future-capture",
        }
    }
    lane_initial = {
        "lane_list": ["lane-a"],
        "lane_null": None,
        "lane_future_capture": future_lane,
        "live_newer": lane_conflict_raw,
        "live_older": lane_conflict_raw,
    }
    lane_mutated = {"lane_list": ["lane-b"], "lane_null": False}
    many_container = {
        "baseline": {"source": "legacy"},
        "snapshots": [
            {
                "captured_at": f"2026-07-{day:02d}T12:00:00Z",
                "metric_def_version": f"metric-{day}",
                "sequence": day,
            }
            for day in range(1, 10)
        ],
    }
    direct = {
        "captured_at": "2026-07-30T12:00:00Z",
        "metric_def_version": "direct",
        "score": 12,
    }
    live_conflict_raw = {
        "captured_at": "2026-07-09T12:00:00Z",
        "metric_def_version": "conflict",
        "shared": "dedicated",
        "raw_only": True,
    }
    newer_live = {"shared": "live-newer", "live_only": "newer"}
    older_live = {"shared": "live-older", "live_only": "older"}

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        await _migrate(engine, "0680")
        async with engine.begin() as connection:
            await _insert_projects(connection, project_ids)
            await connection.execute(
                sa.text(
                    "INSERT INTO project_lane_baseline "
                    "(project_id, data, updated_at) VALUES "
                    "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
                ),
                [
                    {
                        "project_id": project_ids[name],
                        "data": json.dumps(value),
                        "updated_at": original_at,
                    }
                    for name, value in lane_initial.items()
                ],
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'lane_baselines', CAST(:value AS jsonb), :updated_at)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["live_newer"],
                        "value": json.dumps(lane_newer_live),
                        "updated_at": original_at.replace(day=11),
                    },
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["live_older"],
                        "value": json.dumps(lane_older_live),
                        "updated_at": original_at.replace(day=9),
                    },
                ],
            )

        await _migrate(engine, "0681")
        async with engine.connect() as connection:
            lane_authority = {
                str(row.project_id): (row.value, row.updated_at)
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, value, updated_at "
                            "FROM project_telemetry "
                            "WHERE key = 'lane_baselines' "
                            "AND project_id IN "
                            "(CAST(:newer_id AS uuid), CAST(:older_id AS uuid))"
                        ),
                        {
                            "newer_id": project_ids["live_newer"],
                            "older_id": project_ids["live_older"],
                        },
                    )
                )
            }
            newer_value, newer_at = lane_authority[str(project_ids["live_newer"])]
            assert newer_value == {**lane_conflict_raw, **lane_newer_live}
            assert newer_at == original_at.replace(day=11)
            older_value, older_at = lane_authority[str(project_ids["live_older"])]
            assert older_value == {**lane_older_live, **lane_conflict_raw}
            assert older_at == original_at

        await _migrate(engine, "0680", downgrade=True)
        async with engine.begin() as connection:
            future_row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT data, updated_at FROM project_lane_baseline "
                            "WHERE project_id = CAST(:project_id AS uuid)"
                        ),
                        {"project_id": project_ids["lane_future_capture"]},
                    )
                )
                .mappings()
                .one()
            )
            assert future_row["data"] == future_lane
            assert future_row["updated_at"] == original_at
            await connection.execute(
                sa.text(
                    "UPDATE project_lane_baseline "
                    "SET data = CAST(:data AS jsonb), updated_at = :updated_at "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                [
                    {
                        "project_id": project_ids[name],
                        "data": json.dumps(value),
                        "updated_at": mutated_at,
                    }
                    for name, value in lane_mutated.items()
                ],
            )

        await _migrate(engine, "0681")
        async with engine.connect() as connection:
            current_lane_raw = {
                str(row.project_id): (row.value, row.updated_at)
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, value, updated_at "
                            "FROM project_telemetry "
                            "WHERE key = 'migration_0681_lane_baseline_raw' "
                            "AND project_id IN "
                            "(CAST(:list_id AS uuid), CAST(:null_id AS uuid))"
                        ),
                        {
                            "list_id": project_ids["lane_list"],
                            "null_id": project_ids["lane_null"],
                        },
                    )
                )
            }
            for name, expected in lane_mutated.items():
                value, archived_at = current_lane_raw[str(project_ids[name])]
                assert value == expected
                assert archived_at == mutated_at

        await _migrate(engine, "0680", downgrade=True)
        async with engine.connect() as connection:
            for name, expected in lane_mutated.items():
                row = (
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT data, updated_at "
                                "FROM project_lane_baseline "
                                "WHERE project_id = CAST(:project_id AS uuid)"
                            ),
                            {"project_id": project_ids[name]},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["data"] == expected
                assert row["updated_at"] == mutated_at

        await _migrate(engine, "0692")
        async with engine.begin() as connection:
            efficiency_rows = [
                {
                    "project_id": project_ids["eff_list"],
                    "data": json.dumps(["eff-a"]),
                    "updated_at": original_at,
                },
                {
                    "project_id": project_ids["eff_null"],
                    "data": json.dumps(None),
                    "updated_at": original_at,
                },
                {
                    "project_id": project_ids["eff_many"],
                    "data": json.dumps(many_container),
                    "updated_at": original_at,
                },
                {
                    "project_id": project_ids["eff_direct"],
                    "data": json.dumps(direct),
                    "updated_at": original_at,
                },
                {
                    "project_id": project_ids["live_newer"],
                    "data": json.dumps(live_conflict_raw),
                    "updated_at": original_at,
                },
                {
                    "project_id": project_ids["live_older"],
                    "data": json.dumps(live_conflict_raw),
                    "updated_at": original_at,
                },
            ]
            await connection.execute(
                sa.text(
                    "INSERT INTO project_efficiency_snapshot "
                    "(project_id, data, updated_at) VALUES "
                    "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
                ),
                efficiency_rows,
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) VALUES "
                    "(CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'efficiency_baseline', CAST(:value AS jsonb), :updated_at)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["live_newer"],
                        "value": json.dumps(newer_live),
                        "updated_at": original_at.replace(day=11),
                    },
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_ids["live_older"],
                        "value": json.dumps(older_live),
                        "updated_at": original_at.replace(day=9),
                    },
                ],
            )

        await _migrate(engine, "0694")
        async with engine.connect() as connection:
            many_rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT blob FROM project_efficiency_snapshot "
                            "WHERE project_id = CAST(:project_id AS uuid) "
                            "ORDER BY captured_at"
                        ),
                        {"project_id": project_ids["eff_many"]},
                    )
                )
                .scalars()
                .all()
            )
            assert len(many_rows) == 9
            assert [row["sequence"] for row in many_rows] == list(range(1, 10))
            conflict_rows = {
                str(row.project_id): (row.value, row.updated_at)
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, value, updated_at "
                            "FROM project_telemetry "
                            "WHERE key = 'efficiency_baseline' "
                            "AND project_id IN "
                            "(CAST(:newer_id AS uuid), CAST(:older_id AS uuid))"
                        ),
                        {
                            "newer_id": project_ids["live_newer"],
                            "older_id": project_ids["live_older"],
                        },
                    )
                )
            }
            newer_value, newer_at = conflict_rows[str(project_ids["live_newer"])]
            assert newer_value == {**live_conflict_raw, **newer_live}
            assert newer_at == original_at.replace(day=11)
            older_value, older_at = conflict_rows[str(project_ids["live_older"])]
            assert older_value == {**older_live, **live_conflict_raw}
            assert older_at == original_at

        await _migrate(engine, "0692", downgrade=True)
        async with engine.connect() as connection:
            direct_row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT data, updated_at "
                            "FROM project_efficiency_snapshot "
                            "WHERE project_id = CAST(:project_id AS uuid)"
                        ),
                        {"project_id": project_ids["eff_direct"]},
                    )
                )
                .mappings()
                .one()
            )
            assert direct_row["data"] == direct
            assert direct_row["updated_at"] == original_at
            many_row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT data, updated_at "
                            "FROM project_efficiency_snapshot "
                            "WHERE project_id = CAST(:project_id AS uuid)"
                        ),
                        {"project_id": project_ids["eff_many"]},
                    )
                )
                .mappings()
                .one()
            )
            assert many_row["data"] == many_container
            assert many_row["updated_at"] == original_at

        efficiency_mutated = {"eff_list": ["eff-b"], "eff_null": False}
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE project_efficiency_snapshot "
                    "SET data = CAST(:data AS jsonb), updated_at = :updated_at "
                    "WHERE project_id = CAST(:project_id AS uuid)"
                ),
                [
                    {
                        "project_id": project_ids[name],
                        "data": json.dumps(value),
                        "updated_at": mutated_at,
                    }
                    for name, value in efficiency_mutated.items()
                ],
            )

        await _migrate(engine, "0694")
        async with engine.connect() as connection:
            current_efficiency_raw = {
                str(row.project_id): (row.value, row.updated_at)
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, value, updated_at "
                            "FROM project_telemetry "
                            "WHERE key = "
                            "'migration_0694_efficiency_snapshot_raw' "
                            "AND project_id IN "
                            "(CAST(:list_id AS uuid), CAST(:null_id AS uuid))"
                        ),
                        {
                            "list_id": project_ids["eff_list"],
                            "null_id": project_ids["eff_null"],
                        },
                    )
                )
            }
            for name, expected in efficiency_mutated.items():
                value, archived_at = current_efficiency_raw[str(project_ids[name])]
                assert value == expected
                assert archived_at == mutated_at
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM project_efficiency_snapshot "
                        "WHERE project_id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_ids["eff_many"]},
                )
                == 9
            )

        await _migrate(engine, "0692", downgrade=True)
        async with engine.connect() as connection:
            for name, expected in efficiency_mutated.items():
                row = (
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT data, updated_at "
                                "FROM project_efficiency_snapshot "
                                "WHERE project_id = CAST(:project_id AS uuid)"
                            ),
                            {"project_id": project_ids[name]},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["data"] == expected
                assert row["updated_at"] == mutated_at

        await _migrate(engine, "0694")
