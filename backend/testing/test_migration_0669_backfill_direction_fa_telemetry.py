"""Tests for migration 0669 — direction_first_attempt_feedback backfill.

Migration 0669 copies the ``direction_first_attempt_feedback`` blob from
``project.design_doc`` into ``project_telemetry`` via INSERT ... ON CONFLICT.

The critical correctness property this test pins: the ``value`` bound to the
INSERT must be serialized with ``json.dumps()``, NOT Python's ``str()``.
``str()`` of a dict/list produces single-quoted Python repr
(e.g. "{'key': 'value'}") which is not valid JSON and causes the
``::jsonb`` cast to raise ``psycopg2.errors.InvalidTextRepresentation``,
aborting ``alembic upgrade head`` for any project carrying this key.

Because the migration executes PostgreSQL-specific SQL operators (``->``, ``?``,
``CAST(... AS jsonb)``), these tests use a mock connection that intercepts SQL execution
and captures INSERT parameters — the same pattern used by
``test_migration_0532_message_backlog.py``.

Properties tested:
  (a) dict value → json.dumps() produces valid JSON, not Python repr;
  (b) list value → same guarantee;
  (c) None value in SELECT result → row silently skipped;
  (d) empty SELECT result → upgrade is a clean no-op;
  (e) second upgrade call is idempotent (no exception);
  (f) downgrade preserves rows whose migration-versus-writer origin is unknowable.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path


# ── loader ────────────────────────────────────────────────────────────────────


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0669_backfill_direction_fa_telemetry.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0669_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── mock connection ───────────────────────────────────────────────────────────


class _MockResult:
    """Minimal result proxy returned by execute()."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class _MockConn:
    """Intercepts SELECT / INSERT / DELETE calls; SELECT returns seeded rows."""

    def __init__(self, select_rows=()):
        # Each entry is a tuple (project_id, value) matching the migration SELECT.
        self._select_rows = list(select_rows)
        self.captured_select_sql: list[str] = []
        self.captured_insert_sql: list[str] = []
        self.captured_inserts: list[dict] = []
        self.captured_deletes: list[dict] = []

    def execute(self, stmt, params=None):
        sql = str(stmt).upper()
        if "SELECT" in sql:
            self.captured_select_sql.append(str(stmt))
            return _MockResult(self._select_rows)
        if "INSERT" in sql:
            self.captured_insert_sql.append(str(stmt))
            self.captured_inserts.append(params or {})
            return _MockResult()
        if "DELETE" in sql:
            self.captured_deletes.append(params or {})
            return _MockResult()
        return _MockResult()


# ── helpers ───────────────────────────────────────────────────────────────────

_KEY = "direction_first_attempt_feedback"


def _run_upgrade(mig, conn):
    mig.op.get_bind = lambda: conn
    mig.upgrade()


def _run_downgrade(mig, conn):
    mig.op.get_bind = lambda: conn
    mig.downgrade()


# ── tests ─────────────────────────────────────────────────────────────────────


def test_dict_value_serialized_as_valid_json(monkeypatch):
    """(a) A dict returned from design_doc -> key must bind as valid JSON text.

    The pre-fix code used str(value) which produces Python repr with single
    quotes — not JSON.  json.dumps(value) produces standard JSON.
    """
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    fa_dict = {
        "d-abc123": {"first_attempt": "t-001", "score": 0.8, "tags": ["qa", "ci"]},
        "d-def456": {"first_attempt": None, "score": 0.0},
    }
    conn = _MockConn(select_rows=[(project_id, fa_dict)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert len(conn.captured_inserts) == 1, "expected one INSERT for the seeded row"
    params = conn.captured_inserts[0]

    # The bound value must be a valid JSON string — json.loads() must not raise.
    raw_value = params["value"]
    parsed = json.loads(raw_value)

    # Round-trip equality: parsed JSON must equal the original dict.
    assert parsed == fa_dict, f"round-trip mismatch — bound value was: {raw_value!r}"

    # Specifically reject Python repr artifacts (single quotes are illegal JSON).
    assert "'" not in raw_value, (
        f"value contains single quotes — looks like str() repr, not JSON: {raw_value!r}"
    )


def test_list_value_serialized_as_valid_json(monkeypatch):
    """(a continued) A list value must also produce valid JSON (not Python repr)."""
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    fa_list = [{"direction": "d-aaa", "attempt": "t-001"}, {"direction": "d-bbb"}]
    conn = _MockConn(select_rows=[(project_id, fa_list)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert len(conn.captured_inserts) == 1
    raw_value = conn.captured_inserts[0]["value"]
    parsed = json.loads(raw_value)
    assert parsed == fa_list
    assert "'" not in raw_value


def test_insert_params_contain_correct_project_and_key(monkeypatch):
    """INSERT parameters bind the right project_id and telemetry key."""
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    fa_dict = {"d-xyz": {"first_attempt": "t-999"}}
    conn = _MockConn(select_rows=[(project_id, fa_dict)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    params = conn.captured_inserts[0]
    assert params["project_id"] == str(project_id), "project_id must match seeded row"
    assert params["key"] == _KEY, "key must be direction_first_attempt_feedback"
    assert isinstance(params["id"], uuid.UUID), "INSERT must bind a typed UUID"
    assert "updated_at" not in params, "database timestamp must be authoritative"
    insert_sql = conn.captured_insert_sql[0]
    assert "CAST(:id AS uuid)" in insert_sql
    assert "CAST(:project_id AS uuid)" in insert_sql
    assert "CAST(:value AS jsonb)" in insert_sql
    assert "DO NOTHING" in insert_sql
    assert "DO UPDATE" not in insert_sql
    assert "ORDER BY id" in conn.captured_select_sql[0]


def test_none_value_row_is_skipped(monkeypatch):
    """(c) A row whose design_doc -> key yields NULL must be silently skipped."""
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    # value=None simulates design_doc -> key returning SQL NULL.
    conn = _MockConn(select_rows=[(project_id, None)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert conn.captured_inserts == [], (
        "NULL value rows must be skipped — no INSERT should be issued"
    )


def test_empty_select_is_noop(monkeypatch):
    """(d) When no projects carry the key, upgrade returns without any INSERT."""
    mig = _load_migration()
    conn = _MockConn(select_rows=[])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert conn.captured_inserts == [], "no INSERTs expected for empty SELECT"


def test_upgrade_idempotent_on_second_call(monkeypatch):
    """(e) Calling upgrade twice must not raise — mirrors ON CONFLICT behavior."""
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    fa_dict = {"d-repeat": {"first_attempt": "t-001"}}
    conn = _MockConn(select_rows=[(project_id, fa_dict)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()
    # Second call — ON CONFLICT path in real Postgres; mock just records it again.
    mig.upgrade()

    # Two INSERTs captured (first run + second run), no exception raised.
    assert len(conn.captured_inserts) == 2
    for params in conn.captured_inserts:
        assert json.loads(params["value"]) == fa_dict


def test_multiple_projects_each_get_an_insert(monkeypatch):
    """One INSERT per project row in the SELECT result."""
    mig = _load_migration()
    rows = [
        (str(uuid.uuid4()), {"d-001": {"first_attempt": "t-001"}}),
        (str(uuid.uuid4()), {"d-002": {"first_attempt": "t-002"}}),
        (str(uuid.uuid4()), {"d-003": {"first_attempt": "t-003"}}),
    ]
    conn = _MockConn(select_rows=rows)

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert len(conn.captured_inserts) == 3, "expected one INSERT per project row"
    for i, params in enumerate(conn.captured_inserts):
        parsed = json.loads(params["value"])
        assert parsed == rows[i][1], f"row {i}: round-trip mismatch"


def test_downgrade_preserves_indistinguishable_live_writer_rows(monkeypatch):
    """Downgrade cannot identify which rows came from migration versus writers."""
    mig = _load_migration()
    conn = _MockConn(select_rows=[])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.downgrade()

    assert conn.captured_deletes == []


def test_json_looking_string_value_preserves_string_type(monkeypatch):
    """JSON-looking logical strings must not be decoded into other JSON types."""
    mig = _load_migration()
    project_id = str(uuid.uuid4())
    fa_string = "[1]"
    conn = _MockConn(select_rows=[(project_id, fa_string)])

    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    assert len(conn.captured_inserts) == 1
    raw_value = conn.captured_inserts[0]["value"]
    # json.dumps("foo") → '"foo"' — parseable and round-trips to the original string.
    parsed = json.loads(raw_value)
    assert parsed == fa_string
