"""Data-shape regression tests for the 0722 discovery-controller migration."""

from __future__ import annotations

import os
from pathlib import Path
import uuid

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


@requires_postgres
@pytest.mark.asyncio
async def test_0722_does_not_reopen_inconclusive_predecessor_with_successor():
    project_id = uuid.uuid4()
    predecessor_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    latest_id = uuid.uuid4()
    parked_id = uuid.uuid4()
    stopped_id = uuid.uuid4()

    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _database_name,
    ):
        await _upgrade(engine, "0720")
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO project "
                    "(id, name, user_id, status, created_at) "
                    "VALUES (CAST(:id AS uuid), 'migration-0721', 'test-user', "
                    "'active', CURRENT_TIMESTAMP)"
                ),
                {"id": project_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO discoverycycle "
                    "(id, project_id, direction_key, generation, outcome_kind, "
                    "state, verdict) VALUES "
                    "(CAST(:predecessor AS uuid), CAST(:project AS uuid), "
                    "'with-successor', 1, 'empirical', 'reviewed', 'inconclusive'), "
                    "(CAST(:successor AS uuid), CAST(:project AS uuid), "
                    "'with-successor', 2, 'empirical', 'open', NULL), "
                    "(CAST(:latest AS uuid), CAST(:project AS uuid), "
                    "'latest-only', 1, 'empirical', 'reviewed', 'inconclusive'), "
                    "(CAST(:parked AS uuid), CAST(:project AS uuid), "
                    "'legacy-park', 1, 'empirical', 'reviewed', 'park'), "
                    "(CAST(:stopped AS uuid), CAST(:project AS uuid), "
                    "'legacy-stop', 1, 'empirical', 'reviewed', 'stop')"
                ),
                {
                    "predecessor": predecessor_id,
                    "successor": successor_id,
                    "latest": latest_id,
                    "parked": parked_id,
                    "stopped": stopped_id,
                    "project": project_id,
                },
            )

        await _upgrade(engine, "0722")

        async with engine.connect() as connection:
            rows = {
                row.id: row
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT id, state, verdict, review_result "
                            "FROM discoverycycle WHERE project_id = CAST(:project AS uuid)"
                        ),
                        {"project": project_id},
                    )
                )
            }

        assert rows[predecessor_id].state == "reviewed"
        assert rows[predecessor_id].verdict == "advance"
        assert rows[successor_id].state == "open"
        assert rows[latest_id].state == "review_pending"
        assert rows[latest_id].verdict is None
        assert rows[latest_id].review_result == {
            "status": "LEGACY_INCONCLUSIVE_REQUEUED"
        }
        assert rows[parked_id].state == "reviewed"
        assert rows[parked_id].verdict == "ask_operator"
        assert rows[parked_id].review_result == {
            "status": "LEGACY_DISPOSITION_REQUIRES_RESOLUTION",
            "legacy_advisory_verdict": "park",
        }
        assert rows[stopped_id].state == "reviewed"
        assert rows[stopped_id].verdict == "ask_operator"
        assert rows[stopped_id].review_result == {
            "status": "LEGACY_DISPOSITION_REQUIRES_RESOLUTION",
            "legacy_advisory_verdict": "stop",
        }
        await engine.dispose()
