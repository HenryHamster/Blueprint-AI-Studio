"""Exact catalog and planner acceptance for migration 0767."""

from __future__ import annotations

import importlib.util
import os
import uuid
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


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0767_workday_recovery_indexes.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0767_workday_recovery_indexes",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(migration, connection) -> None:
    original_op = migration.op
    context = MigrationContext.configure(connection)
    migration.op = Operations(context)
    try:
        with context.begin_transaction():
            migration.upgrade()
    finally:
        migration.op = original_op


def _state(name: str, *, key_count: int, total_count: int) -> dict[str, object]:
    recovery = name == "ix_projectmessage_workday_recovery_candidates"
    return {
        "index_schema": "public",
        "table_schema": "public",
        "table_name": "projectmessage",
        "access_method": "btree",
        "is_unique": False,
        "is_primary": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "key_attribute_count": key_count,
        "total_attribute_count": total_count,
        "definition": (
            f"CREATE INDEX {name} ON public.projectmessage USING btree "
            "(project_id, sequence DESC, id) INCLUDE (context)"
            if recovery
            else f"CREATE INDEX {name} ON public.projectmessage USING btree "
            "(project_id, ((context ->> "
            "'_workday_consultation_atomic_reply_id'::text))) INCLUDE (sequence)"
        ),
        "predicate": (
            "((sequence IS NOT NULL) AND (from_type = 'strategic'::text) AND "
            "(surface = 'conversation'::text) AND (context @> "
            "'{\"_workday_consultation_requested\": true}'::jsonb))"
            if recovery
            else "(context ? '_workday_consultation_atomic_reply_id'::text)"
        ),
    }


@pytest.mark.parametrize(
    ("name", "key_count", "total_count"),
    [
        ("ix_projectmessage_workday_recovery_candidates", 3, 4),
        ("ix_projectmessage_workday_atomic_reply_id", 2, 3),
    ],
)
def test_definition_rejects_include_columns(name, key_count, total_count):
    migration = _load_migration()
    assert not migration._definition_is_expected(
        name,
        _state(name, key_count=key_count, total_count=total_count),
    )


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_indexes_are_exact_idempotent_and_used_by_runtime_queries():
    migration = _load_migration()
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with ephemeral_postgres_database(
        make_url(os.environ["DATABASE_URL"])
    ) as (engine, _database_name):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "CREATE TABLE public.projectmessage ("
                    "id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                    "sequence bigint, from_type text, surface text, "
                    "context jsonb NOT NULL)"
                )
            )

        for _attempt in range(2):
            async with engine.connect() as connection:
                await connection.run_sync(
                    lambda sync_connection: _run_upgrade(
                        migration,
                        sync_connection,
                    )
                )

        async with engine.begin() as connection:
            for name, counts in (
                (migration._RECOVERY_INDEX, (3, 3)),
                (migration._ATOMIC_REPLY_INDEX, (2, 2)),
            ):
                state = (
                    await connection.execute(
                        migration._INDEX_STATE_SQL,
                        {"index_name": name},
                    )
                ).mappings().one()
                migration._assert_expected_state(name, state, healthy=True)
                assert state["key_attribute_count"] == counts[0]
                assert state["total_attribute_count"] == counts[1]

            await connection.execute(
                sa.text(
                    "INSERT INTO public.projectmessage "
                    "(id, project_id, sequence, from_type, surface, context) "
                    "SELECT md5(value::text)::uuid, :project_id, value, "
                    "'strategic', 'conversation', "
                    "jsonb_build_object("
                    "'_workday_consultation_requested', true, "
                    "'_workday_consultation_atomic_reply_id', "
                    "CAST(:source_id AS text)) "
                    "FROM generate_series(1, 1000) AS value"
                ),
                {"project_id": project_id, "source_id": str(source_id)},
            )
            await connection.execute(sa.text("ANALYZE public.projectmessage"))
            await connection.execute(sa.text("SET LOCAL enable_seqscan = off"))
            await connection.execute(sa.text("SET LOCAL enable_bitmapscan = off"))
            candidate_plan = "\n".join(
                (
                    await connection.execute(
                        sa.text(
                            "EXPLAIN SELECT id FROM public.projectmessage "
                            "WHERE project_id = :project_id "
                            "AND sequence IS NOT NULL "
                            "AND from_type = 'strategic' "
                            "AND surface = 'conversation' "
                            "AND context @> "
                            "'{\"_workday_consultation_requested\": true}'::jsonb "
                            "AND sequence > 0 AND sequence <= 1000 "
                            "ORDER BY sequence DESC, id LIMIT 32"
                        ),
                        {"project_id": project_id},
                    )
                ).scalars().all()
            )
            assert migration._RECOVERY_INDEX in candidate_plan
            linked_plan = "\n".join(
                (
                    await connection.execute(
                        sa.text(
                            "EXPLAIN SELECT id FROM public.projectmessage "
                            "WHERE project_id = :project_id "
                            "AND context ? "
                            "'_workday_consultation_atomic_reply_id' "
                            "AND (context ->> "
                            "'_workday_consultation_atomic_reply_id') "
                            "IN (:source_id)"
                        ),
                        {"project_id": project_id, "source_id": str(source_id)},
                    )
                ).scalars().all()
            )
            assert migration._ATOMIC_REPLY_INDEX in linked_plan
