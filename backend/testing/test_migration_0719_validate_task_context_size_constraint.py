from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/0719_validate_task_context_size_constraint.py"
)
_SPEC = importlib.util.spec_from_file_location("migration_0719", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


def _bind(dialect: str) -> SimpleNamespace:
    return SimpleNamespace(dialect=SimpleNamespace(name=dialect))


def test_postgresql_validation_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration.op, "get_bind", lambda: _bind("postgresql"))
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", calls.append)

    migration.upgrade()

    assert calls == [
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '5min'",
        "ALTER TABLE public.task VALIDATE CONSTRAINT chk_task_context_size",
    ]


def test_sqlite_has_no_constraint_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration.op, "get_bind", lambda: _bind("sqlite"))
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", calls.append)

    migration.upgrade()

    assert calls == []


def test_unknown_dialect_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration.op, "get_bind", lambda: _bind("mysql"))

    with pytest.raises(RuntimeError, match="PostgreSQL and SQLite only"):
        migration.upgrade()


def test_downgrade_refuses_to_drop_write_protection() -> None:
    with pytest.raises(NotImplementedError, match="cannot safely restore"):
        migration.downgrade()
