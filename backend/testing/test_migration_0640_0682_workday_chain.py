"""Real PostgreSQL proof for the amended 0640-to-head Workday chain."""

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
_TELEMETRY_KEY = "direction_first_attempt_feedback"
_FORBIDDEN_SHADOW_TABLES = (
    "strategicworkermemoryproposal",
    "strategicoperationapproval",
    "strategicresidentordinarytaskterminalevent",
    "strategicresidenttaskterminalevent",
    "strategicoperationreceipt",
    "strategicresident",
    "strategicresidentcheckpoint",
    "strategicresidentsweepstate",
)
_USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not _USE_POSTGRES,
    reason="requires the real PostgreSQL migration harness",
)


def _upgrade_to(sync_connection: sa.Connection, destination: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    script = ScriptDirectory.from_config(config)

    def migrations_from_current(current_revision, _context):
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


async def _upgrade(engine, destination: str) -> None:
    async with engine.connect() as connection:
        await connection.run_sync(
            lambda sync_connection: _upgrade_to(sync_connection, destination)
        )


def _head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


async def _index_exists(connection) -> bool:
    return bool(
        await connection.scalar(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND tablename = 'projectmessage' "
                "AND indexname = 'ix_projectmessage_project_session_created'"
                ")"
            )
        )
    )


@requires_postgres
@pytest.mark.asyncio
async def test_amended_chain_from_0640_preserves_workday_and_telemetry():
    project_ids = {name: uuid.uuid4() for name in ("object", "string", "existing")}
    message_id = uuid.uuid4()
    authoritative_id = uuid.uuid4()
    authoritative_time = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        await _upgrade(engine, "0640")

        project_rows = [
            {
                "id": project_ids["object"],
                "name": "object",
                "design_doc": json.dumps(
                    {_TELEMETRY_KEY: {"score": 1, "tags": ["amended"]}}
                ),
            },
            {
                "id": project_ids["string"],
                "name": "string",
                "design_doc": json.dumps({_TELEMETRY_KEY: "[1]"}),
            },
            {
                "id": project_ids["existing"],
                "name": "existing",
                "design_doc": json.dumps(
                    {_TELEMETRY_KEY: {"version": "stale-design-doc"}}
                ),
            },
        ]
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO project "
                    "(id, name, user_id, status, created_at, design_doc) "
                    "VALUES (CAST(:id AS uuid), :name, 'test-user', 'active', "
                    "CURRENT_TIMESTAMP, CAST(:design_doc AS jsonb))"
                ),
                project_rows,
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO projectmessage "
                    "(id, project_id, from_type, content, created_at, "
                    "resident_session_id) "
                    "VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    "'user', 'sentinel', CURRENT_TIMESTAMP, :session_id)"
                ),
                {
                    "id": message_id,
                    "project_id": project_ids["object"],
                    "session_id": "session-before-0657",
                },
            )

        await _upgrade(engine, "0659")

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT resident_session_id FROM projectmessage "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": message_id},
                )
                == "session-before-0657"
            )
            assert await _index_exists(connection)
            for table_name in _FORBIDDEN_SHADOW_TABLES:
                assert (
                    await connection.scalar(
                        sa.text("SELECT to_regclass(:table_name)"),
                        {"table_name": table_name},
                    )
                    is None
                ), f"{table_name} must be removed by 0657"

        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO project_telemetry "
                    "(id, project_id, key, value, updated_at) "
                    "VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), "
                    ":key, CAST(:value AS jsonb), :updated_at)"
                ),
                {
                    "id": authoritative_id,
                    "project_id": project_ids["existing"],
                    "key": _TELEMETRY_KEY,
                    "value": json.dumps({"version": "live-writer"}),
                    "updated_at": authoritative_time,
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO project_efficiency_snapshot "
                    "(project_id, data, updated_at) VALUES "
                    "(CAST(:project_id AS uuid), CAST(:data AS jsonb), :updated_at)"
                ),
                {
                    "project_id": project_ids["object"],
                    "data": json.dumps(
                        {"metric_def_version": "v-legacy", "score": 7}
                    ),
                    "updated_at": authoritative_time,
                },
            )

        await _upgrade(engine, "head")

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                == _head_revision()
            )
            assert (
                await connection.scalar(
                    sa.text(
                        "SELECT resident_session_id FROM projectmessage "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": message_id},
                )
                == "session-before-0657"
            )
            assert await _index_exists(connection)
            efficiency_row = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT project_id, captured_at, metric_def_version, blob "
                            "FROM project_efficiency_snapshot"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert efficiency_row["project_id"] == project_ids["object"]
            assert efficiency_row["captured_at"] == authoritative_time
            assert efficiency_row["metric_def_version"] == "v-legacy"
            assert efficiency_row["blob"] == {
                "metric_def_version": "v-legacy",
                "score": 7,
            }

            rows = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT id, project_id, value, updated_at, "
                            "jsonb_typeof(value) AS value_type "
                            "FROM project_telemetry WHERE key = :key"
                        ),
                        {"key": _TELEMETRY_KEY},
                    )
                )
                .mappings()
                .all()
            )
            by_project = {row["project_id"]: row for row in rows}
            assert by_project[project_ids["object"]]["value"] == {
                "score": 1,
                "tags": ["amended"],
            }
            assert by_project[project_ids["string"]]["value"] == "[1]"
            assert by_project[project_ids["string"]]["value_type"] == "string"

            existing = by_project[project_ids["existing"]]
            assert (
                existing["id"],
                existing["value"],
                existing["updated_at"],
            ) == (
                authoritative_id,
                {"version": "live-writer"},
                authoritative_time,
            )
            for table_name in _FORBIDDEN_SHADOW_TABLES:
                assert (
                    await connection.scalar(
                        sa.text("SELECT to_regclass(:table_name)"),
                        {"table_name": table_name},
                    )
                    is None
                )
