"""Focused PostgreSQL retry-safety tests for migration 0713."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0713_hot_sweep_partial_indexes.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0713_hot_sweep_partial_indexes",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScalarResult:
    def __init__(self, value: bool | None):
        self.value = value

    def scalar_one_or_none(self) -> bool | None:
        return self.value


class _FakeBind:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, validity: bool | None):
        self.validity = validity
        self.catalog_queries: list[tuple[object, dict[str, str]]] = []

    def execute(self, statement, params):
        self.catalog_queries.append((statement, params))
        return _ScalarResult(self.validity)


class _FakeContext:
    def __init__(self):
        self.autocommit_entries = 0

    @contextmanager
    def autocommit_block(self):
        self.autocommit_entries += 1
        yield


class _FakeOperations:
    def __init__(self, validity: bool | None):
        self.bind = _FakeBind(validity)
        self.context = _FakeContext()
        self.statements: list[str] = []

    def get_bind(self):
        return self.bind

    def get_context(self):
        return self.context

    def execute(self, statement: str):
        self.statements.append(statement)


@pytest.mark.parametrize(
    ("validity", "expected_drop_count", "expected_create_count"),
    [
        (False, 3, 3),
        (True, 0, 0),
        (None, 0, 3),
    ],
)
def test_postgres_upgrade_recovers_index_state(
    validity: bool | None,
    expected_drop_count: int,
    expected_create_count: int,
):
    migration = _load_migration()
    fake_op = _FakeOperations(validity)
    migration.op = fake_op

    migration.upgrade()

    assert len(fake_op.bind.catalog_queries) == 3
    assert fake_op.context.autocommit_entries == 3
    assert (
        sum(
            statement.startswith("DROP INDEX CONCURRENTLY ")
            for statement in fake_op.statements
        )
        == expected_drop_count
    )
    assert (
        sum(
            statement.startswith("CREATE INDEX CONCURRENTLY ")
            for statement in fake_op.statements
        )
        == expected_create_count
    )


def test_postgres_downgrade_drops_every_index_in_one_autocommit_block():
    migration = _load_migration()
    fake_op = _FakeOperations(True)
    migration.op = fake_op

    migration.downgrade()

    assert fake_op.context.autocommit_entries == 1
    assert fake_op.statements == [
        f"DROP INDEX CONCURRENTLY IF EXISTS {name}"
        for name, _table, _columns, _predicate in reversed(migration._INDEXES)
    ]
