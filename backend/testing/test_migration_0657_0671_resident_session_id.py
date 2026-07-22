"""Tests for migrations 0657 + 0671 — resident_session_id survives the pair.

Migration 0657 (drop_resident_schema) previously dropped
``projectmessage.resident_session_id`` along with the rest of the shadow-turn
schema. Migration 0671 (restore_workday_message_session_id) re-added it empty,
silently wiping Workday conversation session linkage for any production DB that
ran both in a single ``alembic upgrade head``.

The fix: 0657 now skips the resident_session_id drop; 0671 is made idempotent
(adds column/index only when absent), so:

  (a) ``resident_session_id`` values set before 0657 survive through 0671
      on a normal upgrade path — the column was never dropped.
  (b) Running 0671 alone on a DB where the column already exists is a safe
      no-op (idempotency; covers fresh-install and the "column kept" path).
  (c) Running 0671 on a DB where the column was dropped (old 0657, CI envs)
      correctly re-creates the column — no error on either path.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(filename: str):
    path = _VERSIONS / filename
    spec = importlib.util.spec_from_file_location(filename, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ops(conn: sa.Connection):
    return Operations(MigrationContext.configure(conn))


def _create_projectmessage_with_session_id(conn: sa.Connection) -> None:
    """Minimal pre-0657 projectmessage schema (SQLite in-memory)."""
    conn.execute(
        sa.text(
            "CREATE TABLE projectmessage ("
            "  id TEXT PRIMARY KEY,"
            "  project_id TEXT NOT NULL,"
            "  created_at TEXT NOT NULL,"
            "  resident_session_id TEXT"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX ix_projectmessage_project_session_created "
            "ON projectmessage (project_id, resident_session_id, created_at)"
        )
    )


def _create_projectmessage_without_session_id(conn: sa.Connection) -> None:
    """Schema as left by old-0657 (column already dropped)."""
    conn.execute(
        sa.text(
            "CREATE TABLE projectmessage ("
            "  id TEXT PRIMARY KEY,"
            "  project_id TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        )
    )


def _seed(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO projectmessage (id, project_id, created_at, resident_session_id) "
            "VALUES ('msg-1', 'proj-a', '2026-01-01', 'sess-abc'), "
            "       ('msg-2', 'proj-a', '2026-01-02', 'sess-abc'), "
            "       ('msg-3', 'proj-b', '2026-01-03', NULL)"
        )
    )


def _session_ids(conn: sa.Connection) -> dict:
    rows = conn.execute(
        sa.text("SELECT id, resident_session_id FROM projectmessage ORDER BY id")
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_resident_session_id_survives_0657_then_0671():
    """Values present before 0657 survive through 0671 — no data loss."""
    mig0657 = _load("0657_drop_resident_schema.py")
    mig0671 = _load("0671_restore_workday_message_session_id.py")

    # Also need task table since 0657 drops task columns too.
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_projectmessage_with_session_id(conn)
        # Minimal task table (0657 tries to drop a column from it).
        conn.execute(
            sa.text(
                "CREATE TABLE task ("
                "  id TEXT PRIMARY KEY,"
                "  strategic_operation_receipt_id TEXT"
                ")"
            )
        )
        conn.execute(
            sa.text(
                "CREATE INDEX ix_task_strategic_operation_receipt_id "
                "ON task (strategic_operation_receipt_id)"
            )
        )
        _seed(conn)

        mig0657.op = _ops(conn)
        mig0657.upgrade()

        # Column must still exist and values must be intact after 0657.
        by_id = _session_ids(conn)
        assert by_id["msg-1"] == "sess-abc", (
            "resident_session_id for msg-1 was wiped by 0657 — data-loss bug"
        )
        assert by_id["msg-2"] == "sess-abc", (
            "resident_session_id for msg-2 was wiped by 0657 — data-loss bug"
        )
        assert by_id["msg-3"] is None, "NULL resident_session_id must remain NULL"

        # Run 0671 — must be a no-op (column/index already present).
        mig0671.op = _ops(conn)
        mig0671.upgrade()

        by_id_after = _session_ids(conn)
        assert by_id_after["msg-1"] == "sess-abc", (
            "resident_session_id for msg-1 lost after 0671"
        )
        assert by_id_after["msg-2"] == "sess-abc", (
            "resident_session_id for msg-2 lost after 0671"
        )
        assert by_id_after["msg-3"] is None
        index_names = {
            index["name"] for index in sa.inspect(conn).get_indexes("projectmessage")
        }
        assert "ix_projectmessage_project_session_created" in index_names, (
            "the canonical revision-0396 composite index must survive"
        )


def test_0671_idempotent_when_column_already_exists():
    """Running 0671 on a DB where the column is already present must not error."""
    mig0671 = _load("0671_restore_workday_message_session_id.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_projectmessage_with_session_id(conn)
        _seed(conn)

        mig0671.op = _ops(conn)
        # Column exists — upgrade must succeed silently (no AlreadyExistsError).
        mig0671.upgrade()

        by_id = _session_ids(conn)
        assert by_id["msg-1"] == "sess-abc", (
            "resident_session_id must be preserved when 0671 is a no-op"
        )


def test_0671_adds_column_when_absent():
    """Running 0671 on a DB where column was previously dropped must re-add it."""
    mig0671 = _load("0671_restore_workday_message_session_id.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_projectmessage_without_session_id(conn)
        conn.execute(
            sa.text(
                "INSERT INTO projectmessage (id, project_id, created_at) "
                "VALUES ('msg-x', 'proj-c', '2026-01-05')"
            )
        )

        mig0671.op = _ops(conn)
        # Must add the column without error.
        mig0671.upgrade()

        # Column now exists and existing row has NULL.
        val = conn.execute(
            sa.text("SELECT resident_session_id FROM projectmessage WHERE id='msg-x'")
        ).scalar_one()
        assert val is None, "re-added column must default to NULL for existing rows"


def test_0671_downgrade_preserves_canonical_column_and_index():
    """Downgrade cannot delete revision-0396 schema or Workday history."""
    mig0671 = _load("0671_restore_workday_message_session_id.py")

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_projectmessage_without_session_id(conn)
        conn.execute(
            sa.text(
                "INSERT INTO projectmessage (id, project_id, created_at) "
                "VALUES ('msg-x', 'proj-c', '2026-01-05')"
            )
        )

        mig0671.op = _ops(conn)
        mig0671.upgrade()
        conn.execute(
            sa.text(
                "UPDATE projectmessage SET resident_session_id = 'sentinel' "
                "WHERE id = 'msg-x'"
            )
        )

        mig0671.downgrade()
        inspector = sa.inspect(conn)
        columns = {column["name"] for column in inspector.get_columns("projectmessage")}
        indexes = {index["name"] for index in inspector.get_indexes("projectmessage")}
        assert "resident_session_id" in columns
        assert "ix_projectmessage_project_session_created" in indexes
        assert _session_ids(conn)["msg-x"] == "sentinel"
