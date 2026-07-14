"""Tests for the one-time message-backlog auto-resolve cleanup migration.

The migration (revision 0561, file ``0561_message_backlog_cleanup.py`` — the
reserved id for this task; the historical/task-doc name for this work is
``0532``) is step 4 of Stage 2 of design d-7c851b3f. It clears the open v22-era
``ProjectMessage`` backlog so the operator open/needs-action count reflects only
genuinely-live threads:

  1. open system NOTEs whose ``event_type`` reclassifies to a non-operator
     audience under ``message_audience.route_event`` are auto-resolved;
  2. stale conversational NOTE/FEEDBACK rows (older than the decay window, no
     live ``_convergence_link``) are auto-decayed;
  3. genuinely-live QUESTION/BLOCKER rows are LEFT untouched.

The migration is exercised against in-memory sqlite (same harness as
``test_migration_0531_backfill_encryption.py``): create the ``projectmessage``
table, seed representative rows, monkeypatch ``op.get_bind``, run ``upgrade``.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa

_FILENAME = "0561_message_backlog_cleanup.py"
_NOW = datetime.now(timezone.utc).replace(tzinfo=None)
_OLD = _NOW - timedelta(days=30)
_RECENT = _NOW - timedelta(days=1)


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / _FILENAME
    spec = importlib.util.spec_from_file_location("migration_0561_backlog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_schema(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE projectmessage ("
            "id TEXT PRIMARY KEY, project_id TEXT, message_type TEXT, "
            "from_type TEXT, content TEXT, context TEXT, status TEXT, "
            "resolution TEXT, resolved_at TIMESTAMP, created_at TIMESTAMP)"
        )
    )


def _seed(conn: sa.Connection, *, mid, message_type, from_type, context, created_at):
    conn.execute(
        sa.text(
            "INSERT INTO projectmessage "
            "(id, project_id, message_type, from_type, content, context, "
            " status, resolution, resolved_at, created_at) "
            "VALUES (:id, 'proj', :mt, :ft, 'c', :ctx, 'open', NULL, NULL, :ca)"
        ),
        {
            "id": mid,
            "mt": message_type,
            "ft": from_type,
            "ctx": json.dumps(context),
            "ca": created_at,
        },
    )


def _row(conn: sa.Connection, mid):
    return conn.execute(
        sa.text("SELECT status, resolution FROM projectmessage WHERE id = :id"),
        {"id": mid},
    ).one()


# ── Fixture rows ──────────────────────────────────────────────────────────────
# 'pr_merged' routes strategist-internal; 'failure_alert' routes operator-fyi.
_ROWS = {
    # (1) system NOTE, reclassifies to strategist-internal → resolved (reclassify)
    "sys_note_internal": dict(
        message_type="NOTE", from_type="system",
        context={"event_type": "pr_merged"}, created_at=_RECENT,
    ),
    # system NOTE, unregistered event_type fails closed to strategist-internal → resolved
    "sys_note_unregistered": dict(
        message_type="NOTE", from_type="system",
        context={"event_type": "some_legacy_v22_event"}, created_at=_RECENT,
    ),
    # system NOTE, operator-fyi audience → LEFT open (genuinely operator-visible)
    "sys_note_operator": dict(
        message_type="NOTE", from_type="system",
        context={"event_type": "failure_alert"}, created_at=_RECENT,
    ),
    # (2) stale conversational NOTE, no link → auto-decayed
    "stale_note": dict(
        message_type="NOTE", from_type="user", context={}, created_at=_OLD,
    ),
    # stale conversational FEEDBACK, no link → auto-decayed
    "stale_feedback": dict(
        message_type="FEEDBACK", from_type="user", context={}, created_at=_OLD,
    ),
    # stale conversational NOTE WITH a live convergence link → LEFT open
    "stale_note_linked": dict(
        message_type="NOTE", from_type="user",
        context={"_convergence_link": {"kind": "task", "id": "abc"}},
        created_at=_OLD,
    ),
    # recent conversational NOTE (inside decay window) → LEFT open
    "recent_note": dict(
        message_type="NOTE", from_type="user", context={}, created_at=_RECENT,
    ),
    # (3) live QUESTION, old → LEFT open (needs_action re-derived at read time)
    "live_question": dict(
        message_type="QUESTION", from_type="user", context={}, created_at=_OLD,
    ),
    # live BLOCKER, old → LEFT open
    "live_blocker": dict(
        message_type="BLOCKER", from_type="agent", context={}, created_at=_OLD,
    ),
}


def _seed_all(conn: sa.Connection) -> None:
    for mid, spec in _ROWS.items():
        _seed(conn, mid=mid, **spec)


def test_backlog_cleanup_resolves_reclassified_and_stale_preserves_live(monkeypatch):
    """The named acceptance test — all three behaviors in one run."""
    mig = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_schema(conn)
        _seed_all(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()

        # (1) reclassified system NOTEs (registered + fail-closed) resolved.
        for mid in ("sys_note_internal", "sys_note_unregistered"):
            status, resolution = _row(conn, mid)
            assert status == "resolved", f"{mid} must be auto-resolved"
            assert resolution == "auto-resolved (reclassified strategist-internal)"

        # operator-visible system NOTE is left genuinely-live.
        assert _row(conn, "sys_note_operator") == ("open", None)

        # (2) stale conversational rows auto-decayed with the sweep's reason.
        for mid in ("stale_note", "stale_feedback"):
            status, resolution = _row(conn, mid)
            assert status == "resolved", f"{mid} must be auto-decayed"
            assert resolution == "auto-decayed (stale)"

        # linked / recent conversational rows preserved.
        assert _row(conn, "stale_note_linked") == ("open", None)
        assert _row(conn, "recent_note") == ("open", None)

        # (3) genuinely-live QUESTION/BLOCKER preserved.
        assert _row(conn, "live_question") == ("open", None)
        assert _row(conn, "live_blocker") == ("open", None)


def test_backlog_cleanup_is_idempotent(monkeypatch):
    """Re-running the migration changes nothing (every UPDATE guards on 'open')."""
    mig = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_schema(conn)
        _seed_all(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()
        snapshot = {mid: _row(conn, mid) for mid in _ROWS}
        mig.upgrade()  # second pass must be a no-op
        for mid in _ROWS:
            assert _row(conn, mid) == snapshot[mid], f"{mid} changed on re-run"


def test_downgrade_is_noop_and_does_not_raise(monkeypatch):
    mig = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_schema(conn)
        _seed_all(conn)
        monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
        mig.upgrade()
        before = {mid: _row(conn, mid) for mid in _ROWS}
        mig.downgrade()  # must not raise, must not change anything
        for mid in _ROWS:
            assert _row(conn, mid) == before[mid]
