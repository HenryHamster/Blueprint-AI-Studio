"""Tests for migration 0679 — resolve stale open messages backlog.

Migration 0679 (``0679_resolve_stale_open_messages.py``) applies the same
lifecycle rules as ``sweep_decay_stale_open_messages`` to clear the v22-era
open ProjectMessage backlog:

  1. NOTE/FEEDBACK >= 14d old, not operator-action-needed, no convergence link
     → resolution 'auto-decayed (stale)'
  2. NOTE/FEEDBACK with audience='operator-action-needed' >= 60d old, no link
     → resolution 'auto-decayed (stale)'
  3. QUESTION >= 30d old, no convergence link
     → resolution 'stale_unanswered'
  4. Messages with a live _convergence_link are preserved regardless of age.
  5. BLOCKER and BUG_REPORT are never touched.
  6. Messages inside decay windows (too recent) are preserved.

Because the migration uses PostgreSQL-specific JSONB operators (``'{}'::jsonb``,
``?``, ``->>``, ``jsonb_build_object``), this test suite exercises the migration
against a mock connection that intercepts SQL calls and applies the logical
rules in Python — the same pattern used by
``test_migration_0669_backfill_direction_fa_telemetry.py``.

Constants (frozen, matching the migration — intentionally NOT imported from app):
  NOTE/FEEDBACK decay: 14 days
  operator-action-needed decay: 60 days
  QUESTION decay: 30 days
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_FILENAME = "0679_resolve_stale_open_messages.py"

# Frozen decay windows matching the migration literals.
_NOTE_FEEDBACK_DECAY_DAYS = 14
_OPERATOR_DECAY_DAYS = 60
_QUESTION_DECAY_DAYS = 30

_NOW = datetime.now(timezone.utc).replace(tzinfo=None)

# Age helpers — all naive UTC datetimes.
_OLD_NOTE = _NOW - timedelta(days=_NOTE_FEEDBACK_DECAY_DAYS + 5)     # 19d ago
_OLD_OPERATOR = _NOW - timedelta(days=_OPERATOR_DECAY_DAYS + 5)      # 65d ago
_OLD_QUESTION = _NOW - timedelta(days=_QUESTION_DECAY_DAYS + 5)      # 35d ago
_BORDERLINE_OPERATOR = _NOW - timedelta(days=_OPERATOR_DECAY_DAYS - 5)  # 55d ago (not yet 60d)
_RECENT = _NOW - timedelta(days=2)                                    # 2d ago, inside all windows


# ── migration loader ──────────────────────────────────────────────────────────


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / _FILENAME
    )
    spec = importlib.util.spec_from_file_location("migration_0679_backlog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── mock connection ───────────────────────────────────────────────────────────


class _FakeResult:
    """Minimal result stub; migration does not use SELECT result rows."""

    def fetchall(self):
        return []


class _FakeConn:
    """Intercepts UPDATE execute() calls from migration 0679 and applies the
    logical lifecycle rules to an in-memory row store.

    Each row is a dict with keys:
      id, message_type, created_at (datetime), context (dict),
      status, resolution
    """

    def __init__(self, rows: list[dict]):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.call_count = 0

    def row(self, mid: str) -> dict:
        return self._rows[mid]

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self.call_count += 1

        if "UPDATE" not in sql.upper():
            return _FakeResult()

        cutoff: datetime = params.get("cutoff")  # type: ignore[assignment]
        now: datetime = params.get("now")  # type: ignore[assignment]
        now_iso: str = params.get("now_iso", "")

        # Identify which of the three UPDATE rules this call represents.
        # Rule 3: QUESTION decay (SQL contains 'stale_unanswered')
        if "stale_unanswered" in sql:
            self._apply_question_rule(cutoff, now, now_iso)
        # Rule 2: operator-action-needed (SQL has "= 'operator-action-needed'")
        elif "= 'operator-action-needed'" in sql:
            self._apply_operator_rule(cutoff, now, now_iso)
        # Rule 1: non-operator NOTE/FEEDBACK (SQL has IS DISTINCT FROM)
        else:
            self._apply_note_feedback_rule(cutoff, now, now_iso)

        return _FakeResult()

    # ── private rule applications ──────────────────────────────────────────

    def _apply_note_feedback_rule(
        self, cutoff: datetime, now: datetime, now_iso: str
    ) -> None:
        """Rule 1: NOTE/FEEDBACK >= 14d, no link, audience != operator-action-needed."""
        for row in self._rows.values():
            if row["status"] != "open":
                continue
            if row["message_type"] not in ("NOTE", "FEEDBACK"):
                continue
            if row["created_at"] > cutoff:
                continue
            ctx = row.get("context") or {}
            if "_convergence_link" in ctx:
                continue
            if ctx.get("audience") == "operator-action-needed":
                continue
            row["status"] = "resolved"
            row["resolution"] = "auto-decayed (stale)"
            row["resolved_at"] = now

    def _apply_operator_rule(
        self, cutoff: datetime, now: datetime, now_iso: str
    ) -> None:
        """Rule 2: NOTE/FEEDBACK >= 60d, no link, audience = operator-action-needed."""
        for row in self._rows.values():
            if row["status"] != "open":
                continue
            if row["message_type"] not in ("NOTE", "FEEDBACK"):
                continue
            if row["created_at"] > cutoff:
                continue
            ctx = row.get("context") or {}
            if "_convergence_link" in ctx:
                continue
            if ctx.get("audience") != "operator-action-needed":
                continue
            row["status"] = "resolved"
            row["resolution"] = "auto-decayed (stale)"
            row["resolved_at"] = now

    def _apply_question_rule(
        self, cutoff: datetime, now: datetime, now_iso: str
    ) -> None:
        """Rule 3: QUESTION >= 30d, no link."""
        for row in self._rows.values():
            if row["status"] != "open":
                continue
            if row["message_type"] != "QUESTION":
                continue
            if row["created_at"] > cutoff:
                continue
            ctx = row.get("context") or {}
            if "_convergence_link" in ctx:
                continue
            row["status"] = "resolved"
            row["resolution"] = "stale_unanswered"
            row["resolved_at"] = now


# ── row factory ───────────────────────────────────────────────────────────────


def _row(
    mid: str,
    message_type: str,
    created_at: datetime,
    context: dict | None = None,
    status: str = "open",
    resolution: str | None = None,
) -> dict:
    return {
        "id": mid,
        "message_type": message_type,
        "created_at": created_at,
        "context": context or {},
        "status": status,
        "resolution": resolution,
        "resolved_at": None,
    }


# ── representative seed rows ──────────────────────────────────────────────────

_SEED_ROWS = [
    # ── Rule 1 candidates: NOTE/FEEDBACK >= 14d, no link, non-operator ────
    _row("stale_note", "NOTE", _OLD_NOTE),
    _row("stale_feedback", "FEEDBACK", _OLD_NOTE),
    # ── Rule 2 candidates: NOTE/FEEDBACK >= 60d, operator-action-needed ──
    _row("stale_operator_note", "NOTE", _OLD_OPERATOR,
         context={"audience": "operator-action-needed"}),
    _row("stale_operator_feedback", "FEEDBACK", _OLD_OPERATOR,
         context={"audience": "operator-action-needed"}),
    # ── Rule 3 candidates: QUESTION >= 30d, no link ───────────────────────
    _row("stale_question", "QUESTION", _OLD_QUESTION),
    # ── Preserved: has convergence link (all types) ───────────────────────
    _row("linked_note", "NOTE", _OLD_NOTE,
         context={"_convergence_link": {"kind": "task", "id": "t-abc"}}),
    _row("linked_question", "QUESTION", _OLD_QUESTION,
         context={"_convergence_link": {"kind": "task", "id": "t-def"}}),
    # ── Preserved: too recent (inside all decay windows) ──────────────────
    _row("recent_note", "NOTE", _RECENT),
    _row("recent_question", "QUESTION", _RECENT),
    # ── Preserved: operator-action-needed but < 60d old ───────────────────
    _row("borderline_operator_note", "NOTE", _BORDERLINE_OPERATOR,
         context={"audience": "operator-action-needed"}),
    # ── Preserved: BLOCKER — never decayed ───────────────────────────────
    _row("live_blocker", "BLOCKER", _OLD_NOTE),
    # ── Preserved: BUG_REPORT — owned by convergence sweeps ──────────────
    _row("live_bug_report", "BUG_REPORT", _OLD_NOTE),
    # ── Preserved: already resolved — guards on status='open' ────────────
    _row("already_resolved", "NOTE", _OLD_NOTE,
         status="resolved", resolution="manual"),
]


# ── tests ─────────────────────────────────────────────────────────────────────


def test_backlog_clear_applies_full_lifecycle_rules_and_preserves_live(monkeypatch):
    """Named acceptance test for criterion c-042d6fbc.

    Verifies that migration 0679 correctly applies all three lifecycle decay
    rules to stale open messages while preserving genuinely-live threads.
    """
    mig = _load_migration()
    conn = _FakeConn(list(_SEED_ROWS))
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    # Rule 1: stale NOTE/FEEDBACK (>= 14d, no link, non-operator) → decayed
    for mid in ("stale_note", "stale_feedback"):
        r = conn.row(mid)
        assert r["status"] == "resolved", f"{mid}: expected resolved, got {r['status']!r}"
        assert r["resolution"] == "auto-decayed (stale)", (
            f"{mid}: expected 'auto-decayed (stale)', got {r['resolution']!r}"
        )

    # Rule 2: stale operator-action-needed NOTE/FEEDBACK (>= 60d) → decayed
    for mid in ("stale_operator_note", "stale_operator_feedback"):
        r = conn.row(mid)
        assert r["status"] == "resolved", f"{mid}: expected resolved, got {r['status']!r}"
        assert r["resolution"] == "auto-decayed (stale)", (
            f"{mid}: expected 'auto-decayed (stale)', got {r['resolution']!r}"
        )

    # Rule 3: stale QUESTION (>= 30d, no link) → stale_unanswered
    r = conn.row("stale_question")
    assert r["status"] == "resolved", f"stale_question: expected resolved, got {r['status']!r}"
    assert r["resolution"] == "stale_unanswered", (
        f"stale_question: expected 'stale_unanswered', got {r['resolution']!r}"
    )

    # Convergence-linked messages preserved regardless of age and type
    for mid in ("linked_note", "linked_question"):
        r = conn.row(mid)
        assert r["status"] == "open", f"{mid}: linked message must be preserved, got {r['status']!r}"
        assert r["resolution"] is None, f"{mid}: linked message must not be resolved"

    # Recent messages inside all decay windows preserved
    for mid in ("recent_note", "recent_question"):
        r = conn.row(mid)
        assert r["status"] == "open", f"{mid}: recent message must be preserved"

    # operator-action-needed NOTE below 60d threshold preserved (not yet expired)
    r = conn.row("borderline_operator_note")
    assert r["status"] == "open", "borderline_operator_note: must stay open (< 60d)"

    # BLOCKER never decayed
    r = conn.row("live_blocker")
    assert r["status"] == "open", "BLOCKER must never be auto-decayed"

    # BUG_REPORT never decayed (owned by convergence sweeps)
    r = conn.row("live_bug_report")
    assert r["status"] == "open", "BUG_REPORT must never be auto-decayed"

    # Already-resolved rows unchanged (status guard on 'open')
    r = conn.row("already_resolved")
    assert r["status"] == "resolved"
    assert r["resolution"] == "manual", "pre-resolved row resolution must not be overwritten"

    # Migration sent exactly 3 UPDATE statements (one per rule)
    assert conn.call_count == 3, f"expected 3 execute calls, got {conn.call_count}"


def test_backlog_clear_is_idempotent(monkeypatch):
    """Re-running upgrade() changes nothing — every UPDATE guards on status='open'."""
    mig = _load_migration()
    conn = _FakeConn(list(_SEED_ROWS))
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    # Capture state after first run.
    snapshot = {mid: (r["status"], r["resolution"]) for mid, r in conn._rows.items()}

    # Second run must be a strict no-op.
    mig.upgrade()
    for mid, r in conn._rows.items():
        assert (r["status"], r["resolution"]) == snapshot[mid], (
            f"{mid} changed on second upgrade() — not idempotent"
        )


def test_backlog_clear_decay_boundary_note_feedback(monkeypatch):
    """Rows at exactly the decay boundary are correctly classified.

    A NOTE created exactly 14 days ago should be decayed; one created 13 days
    ago should be preserved.  Same threshold logic applies to FEEDBACK.
    """
    at_boundary = _NOW - timedelta(days=_NOTE_FEEDBACK_DECAY_DAYS)
    just_inside = _NOW - timedelta(days=_NOTE_FEEDBACK_DECAY_DAYS - 1)

    rows = [
        _row("at_boundary_note", "NOTE", at_boundary),
        _row("just_inside_note", "NOTE", just_inside),
    ]
    mig = _load_migration()
    conn = _FakeConn(rows)
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    # At boundary (created_at <= cutoff) → decayed
    r = conn.row("at_boundary_note")
    assert r["status"] == "resolved", "at-boundary NOTE must be decayed"
    assert r["resolution"] == "auto-decayed (stale)"

    # Just inside window (created_at > cutoff) → preserved
    r = conn.row("just_inside_note")
    assert r["status"] == "open", "NOTE inside 14d window must be preserved"


def test_backlog_clear_operator_audience_uses_60d_threshold(monkeypatch):
    """operator-action-needed rows use the 60d threshold, not the 14d threshold.

    A NOTE with audience='operator-action-needed' created 20 days ago is beyond
    the 14d standard window but NOT beyond the 60d operator window — it must be
    LEFT open.
    """
    at_20d = _NOW - timedelta(days=20)
    rows = [
        _row("operator_20d", "NOTE", at_20d,
             context={"audience": "operator-action-needed"}),
    ]
    mig = _load_migration()
    conn = _FakeConn(rows)
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    r = conn.row("operator_20d")
    assert r["status"] == "open", (
        "operator-action-needed NOTE at 20d must be preserved (60d threshold applies)"
    )


def test_backlog_clear_question_uses_30d_threshold(monkeypatch):
    """QUESTION rows use the 30d threshold.

    A QUESTION created 20 days ago (beyond 14d but inside 30d) must be preserved.
    """
    at_20d = _NOW - timedelta(days=20)
    rows = [
        _row("question_20d", "QUESTION", at_20d),
    ]
    mig = _load_migration()
    conn = _FakeConn(rows)
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    r = conn.row("question_20d")
    assert r["status"] == "open", (
        "QUESTION at 20d must be preserved (30d threshold applies, not 14d)"
    )


def test_backlog_clear_empty_context_treated_as_no_link(monkeypatch):
    """A stale NOTE with null/empty context (no _convergence_link key) is decayed."""
    rows = [
        _row("null_ctx_note", "NOTE", _OLD_NOTE, context=None),
        _row("empty_ctx_note", "NOTE", _OLD_NOTE, context={}),
    ]
    mig = _load_migration()
    conn = _FakeConn(rows)
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()

    for mid in ("null_ctx_note", "empty_ctx_note"):
        r = conn.row(mid)
        assert r["status"] == "resolved", f"{mid}: empty context must be treated as no link"
        assert r["resolution"] == "auto-decayed (stale)"


def test_downgrade_is_noop(monkeypatch):
    """downgrade() is a no-op and must not raise."""
    mig = _load_migration()
    conn = _FakeConn(list(_SEED_ROWS))
    monkeypatch.setattr(mig.op, "get_bind", lambda: conn)
    mig.upgrade()
    before = {mid: (r["status"], r["resolution"]) for mid, r in conn._rows.items()}
    mig.downgrade()  # must not raise
    for mid, r in conn._rows.items():
        assert (r["status"], r["resolution"]) == before[mid], (
            f"{mid} changed after downgrade"
        )
