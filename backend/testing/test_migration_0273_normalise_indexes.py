"""Tests for migration 0273: normalise duplicate task index definitions.

Migration 0273 idempotently re-asserts two indexes whose names were each
created by two separate migrations, causing SQLite chain replay to fail on
the second unguarded create:

  ix_task_project_status_updated — created by both 0236 and 0237 (SQLite unguarded)
  ix_task_post_commit_pending    — created by both 0045 and 0117 (SQLite unguarded)

Each test builds a throwaway in-memory SQLite DB that simulates one of the
reachable intermediate states (index present / index absent) and verifies that
upgrade() completes cleanly and leaves both indexes in place.  Running upgrade()
twice must also be a clean no-op.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "0273_normalise_duplicate_task_indexes.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0273", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine, module):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()


def _index_names(engine) -> set[str]:
    with engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task'"
            )
        )
        return {row[0] for row in result}


def _make_task_table(engine):
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE task ("
                "id INTEGER PRIMARY KEY, "
                "project_id VARCHAR, "
                "status VARCHAR, "
                "updated_at DATETIME, "
                "post_commit_pending BOOLEAN NOT NULL DEFAULT 0"
                ")"
            )
        )


_EXPECTED_INDEXES = {
    "ix_task_project_status_updated",
    "ix_task_post_commit_pending",
}


def test_0273_upgrade_when_neither_index_exists():
    """Fresh task table with no pre-existing indexes: upgrade creates both."""
    engine = sa.create_engine("sqlite://")
    _make_task_table(engine)
    module = _load_migration()

    assert not (_EXPECTED_INDEXES & _index_names(engine))

    _run_upgrade(engine, module)

    assert _EXPECTED_INDEXES <= _index_names(engine)


def test_0273_upgrade_when_both_indexes_already_exist():
    """Both indexes already present (0236+0117 already ran): upgrade is a no-op."""
    engine = sa.create_engine("sqlite://")
    _make_task_table(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE INDEX ix_task_project_status_updated"
                " ON task (project_id, status, updated_at)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE INDEX ix_task_post_commit_pending"
                " ON task (project_id, status) WHERE post_commit_pending = true"
            )
        )
    module = _load_migration()

    _run_upgrade(engine, module)

    assert _EXPECTED_INDEXES <= _index_names(engine)


def test_0273_upgrade_when_only_status_updated_exists():
    """Only ix_task_project_status_updated present: upgrade adds the missing one."""
    engine = sa.create_engine("sqlite://")
    _make_task_table(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE INDEX ix_task_project_status_updated"
                " ON task (project_id, status, updated_at)"
            )
        )
    module = _load_migration()

    _run_upgrade(engine, module)

    assert _EXPECTED_INDEXES <= _index_names(engine)


def test_0273_upgrade_when_only_post_commit_pending_exists():
    """Only ix_task_post_commit_pending present: upgrade adds the missing one."""
    engine = sa.create_engine("sqlite://")
    _make_task_table(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE INDEX ix_task_post_commit_pending"
                " ON task (project_id, status) WHERE post_commit_pending = true"
            )
        )
    module = _load_migration()

    _run_upgrade(engine, module)

    assert _EXPECTED_INDEXES <= _index_names(engine)


def test_0273_is_idempotent():
    """Running upgrade() twice is a clean no-op (both indexes survive)."""
    engine = sa.create_engine("sqlite://")
    _make_task_table(engine)
    module = _load_migration()

    _run_upgrade(engine, module)
    _run_upgrade(engine, module)

    assert _EXPECTED_INDEXES <= _index_names(engine)
