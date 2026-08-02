"""Focused retry-safety tests for migration 0720."""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
        / "0720_repair_direction_status_staged_index.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0720_direction_status_staged_index",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(migration, connection):
    original_op = migration.op
    context = MigrationContext.configure(connection)
    migration.op = Operations(context)
    try:
        with context.begin_transaction():
            migration.upgrade()
    finally:
        migration.op = original_op


def _healthy_state(**changes):
    value = {
        "index_schema": "public",
        "table_schema": "public",
        "table_name": "direction",
        "access_method": "btree",
        "is_unique": False,
        "is_primary": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "key_attribute_count": 1,
        "total_attribute_count": 1,
        "key_column": "status",
        "definition": (
            "CREATE INDEX ix_direction_status_staged ON public.direction "
            "USING btree (status) WHERE ((status)::text = 'staged'::text)"
        ),
        "predicate": "((status)::text = 'staged'::text)",
    }
    value.update(changes)
    return value


class _MappingResult:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value


class _FakeBind:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, states):
        self.states = list(states)
        self.queries = []

    def execute(self, statement, params):
        self.queries.append((statement, params))
        return _MappingResult(self.states.pop(0))


class _FakeContext:
    def __init__(self):
        self.autocommit_entries = 0

    @contextmanager
    def autocommit_block(self):
        self.autocommit_entries += 1
        yield


class _FakeOperations:
    def __init__(self, states, dialect="postgresql"):
        self.bind = _FakeBind(states)
        self.bind.dialect = SimpleNamespace(name=dialect)
        self.context = _FakeContext()
        self.statements = []

    def get_bind(self):
        return self.bind

    def get_context(self):
        return self.context

    def execute(self, statement):
        self.statements.append(statement)


@pytest.mark.parametrize(
    ("initial", "expected_drop"),
    [(None, False), (_healthy_state(is_valid=False), True)],
)
def test_upgrade_creates_or_repairs_and_verifies(initial, expected_drop):
    migration = _load_migration()
    states = [initial]
    if expected_drop:
        states.append(initial)
    states.append(_healthy_state())
    fake_op = _FakeOperations(states)
    migration.op = fake_op

    migration.upgrade()

    assert fake_op.context.autocommit_entries == 1
    assert any(
        statement.startswith("DROP INDEX CONCURRENTLY ")
        for statement in fake_op.statements
    ) is expected_drop
    assert any(
        statement.startswith("CREATE INDEX CONCURRENTLY ")
        for statement in fake_op.statements
    )
    assert len(fake_op.bind.queries) == (3 if expected_drop else 2)


def test_upgrade_accepts_only_an_exact_healthy_existing_index():
    migration = _load_migration()
    fake_op = _FakeOperations([_healthy_state()])
    migration.op = fake_op

    migration.upgrade()

    assert fake_op.context.autocommit_entries == 0
    assert fake_op.statements == []


def test_upgrade_rejects_wrong_valid_same_name_index():
    migration = _load_migration()
    fake_op = _FakeOperations([_healthy_state(key_column="priority")])
    migration.op = fake_op

    with pytest.raises(RuntimeError, match="index verification failed"):
        migration.upgrade()

    assert fake_op.context.autocommit_entries == 0
    assert fake_op.statements == []


def test_upgrade_rejects_wrong_invalid_same_name_index_without_ddl():
    migration = _load_migration()
    fake_op = _FakeOperations(
        [_healthy_state(is_valid=False, key_column="priority")]
    )
    migration.op = fake_op

    with pytest.raises(RuntimeError, match="unexpected invalid index"):
        migration.upgrade()

    assert fake_op.context.autocommit_entries == 0
    assert fake_op.statements == []


def test_upgrade_rechecks_invalid_index_before_destructive_ddl():
    migration = _load_migration()
    fake_op = _FakeOperations([_healthy_state(is_valid=False), None])
    migration.op = fake_op

    with pytest.raises(RuntimeError, match="unexpected invalid index"):
        migration.upgrade()

    assert fake_op.context.autocommit_entries == 1
    assert fake_op.statements == []


def test_upgrade_rejects_unhealthy_post_create_state():
    migration = _load_migration()
    fake_op = _FakeOperations([None, _healthy_state(is_ready=False)])
    migration.op = fake_op

    with pytest.raises(RuntimeError, match="index verification failed"):
        migration.upgrade()


def test_sqlite_upgrade_is_a_noop():
    migration = _load_migration()
    fake_op = _FakeOperations([], dialect="sqlite")
    migration.op = fake_op

    migration.upgrade()

    assert fake_op.statements == []
    assert fake_op.bind.queries == []


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_missing_index_is_created_concurrently_and_verified():
    migration = _load_migration()
    async with ephemeral_postgres_database(
        make_url(os.environ["DATABASE_URL"])
    ) as (engine, _database_name):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "CREATE TABLE public.direction ("
                    "id uuid PRIMARY KEY, status varchar NOT NULL)"
                )
            )

        async with engine.connect() as connection:
            await connection.run_sync(
                lambda sync_connection: _run_upgrade(migration, sync_connection)
            )

        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    migration._INDEX_STATE_SQL,
                    {"index_name": migration._INDEX_NAME},
                )
            ).mappings().one()

        migration._assert_expected_healthy(state)
