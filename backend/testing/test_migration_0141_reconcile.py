"""Isolated tests for the 0141 reconciliation migration.

0141 re-applies the BLOCKED task fields (migration 0138) + project.merge_gate_runtime
(migration 0139) that the main->automain back-merge caused the live LTS DB to skip
(its version counter had already passed those revision numbers). These tests build a
throwaway in-memory SQLite DB that simulates the prod state — ``task`` / ``project``
WITHOUT those columns — so they never touch the shared harness schema, then run the
migration and assert it (a) adds the missing columns and (b) is a clean no-op on a
second run. The column-reconciliation guards are dialect-independent; the Postgres
branch differs only in CONCURRENTLY index syntax (copied verbatim from the proven
0138) and is exercised live by the deploy's migrate job.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

# JSONB has no SQLite type compiler by default; map it to JSON for the in-memory
# DB (mirrors the harness's SQLite fixture handling).
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )

_BLOCKED_COLS = {"blocker_kind", "blocked_reason", "blocked_at", "recheck_after"}


def _load_migration():
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0141_reconcile_blocked_mergegate.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0141", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine, module):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _make_prod_like_engine():
    """In-memory DB simulating the live state: task/project WITHOUT the
    0138/0139 columns the back-merge caused alembic to skip."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR, "
                "idempotency_key VARCHAR)"
            )
        )
        conn.execute(sa.text("CREATE TABLE project (id INTEGER PRIMARY KEY)"))
    return engine


def test_0141_adds_missing_blocked_and_mergegate_columns():
    engine = _make_prod_like_engine()
    module = _load_migration()

    # Precondition: the prod-like DB is missing all of them.
    assert not (_BLOCKED_COLS & _columns(engine, "task"))
    assert "merge_gate_runtime" not in _columns(engine, "project")

    _run_upgrade(engine, module)

    # The reconciliation added every missing column.
    assert _columns(engine, "task") >= _BLOCKED_COLS
    assert "merge_gate_runtime" in _columns(engine, "project")


def test_0141_is_idempotent_on_second_run():
    engine = _make_prod_like_engine()
    module = _load_migration()

    _run_upgrade(engine, module)  # first run adds the columns
    _run_upgrade(engine, module)  # second run must be a clean no-op

    assert _columns(engine, "task") >= _BLOCKED_COLS
    assert "merge_gate_runtime" in _columns(engine, "project")


def test_0141_noop_when_columns_already_present():
    """On a fresh DB (0138/0139 already created the columns) every step is
    skipped — mirrors what the harness `alembic upgrade head` exercises."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE task (id INTEGER PRIMARY KEY, status VARCHAR, "
                "idempotency_key VARCHAR, blocker_kind VARCHAR, blocked_reason TEXT, "
                "blocked_at DATETIME, recheck_after DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE project (id INTEGER PRIMARY KEY, merge_gate_runtime JSON)"
            )
        )
    module = _load_migration()

    _run_upgrade(engine, module)  # must not error and must not duplicate columns

    assert _columns(engine, "task") >= _BLOCKED_COLS
    assert "merge_gate_runtime" in _columns(engine, "project")
