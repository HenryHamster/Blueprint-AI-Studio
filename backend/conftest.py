# Root-level conftest: compatibility shims for the in-memory SQLite test DB.
#
# 1. JSONB → TEXT: The models use PostgreSQL-specific JSONB columns.  SQLite
#    has no JSONB type compiler, so `create_all` would fail.  Registering a
#    TEXT fallback lets fixture-level schema creation succeed without touching
#    production model code.
#
# 2. expire_all no-op: SQLAlchemy expires all ORM objects after each
#    session.commit().  When production code accesses an attribute on an
#    expired object, it issues a lazy SELECT.  In the async aiosqlite
#    driver this lazy SELECT requires a greenlet context that is absent at
#    the point where the production code runs (after the route's own await
#    chain), raising MissingGreenlet.  Making expire_all a no-op keeps
#    objects "live" in the identity map so their attributes remain readable
#    without a round-trip.  Tests that need the post-commit DB state still
#    call `await db_session.refresh(obj)` explicitly.

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm.state import InstanceState as _InstanceState


# --- Fix 1: JSONB → TEXT ---
def _sqlite_visit_JSONB(self, type_, **kwargs):
    return "TEXT"


SQLiteTypeCompiler.visit_JSONB = _sqlite_visit_JSONB  # type: ignore[attr-defined]


# --- Fix 2: suppress per-object expiry so lazy reloads don't raise MissingGreenlet ---
# SQLAlchemy marks all ORM objects "expired" after session.commit() via
# InstanceState._expire().  The next attribute access then issues a lazy SELECT.
# In the async aiosqlite test environment that lazy SELECT requires a greenlet
# context that is absent (outside the route's own async chain), raising
# MissingGreenlet.  Suppressing _expire keeps each object's attribute dict
# alive so attributes can be read from the identity map without a round-trip.
_orig_instance_expire = _InstanceState._expire


def _noop_expire(
    self, dict_, modified_set, instance_dict=None, no_autoflush_expiry=False
):
    pass  # Don't expire — keep attributes readable without DB round-trip


_InstanceState._expire = _noop_expire  # type: ignore[assignment]


@pytest.fixture
def expire_fidelity():
    """Restore real SQLAlchemy object expiry for one test (fidelity mode).

    The root conftest globally no-ops InstanceState._expire so the aiosqlite
    test environment does not raise MissingGreenlet on lazy-loads after
    session.rollback(). This fixture temporarily restores the original _expire
    for the duration of one test, making expired attribute access raise — the
    same behaviour asyncpg produces in production. Use it to guard against
    MissingGreenlet-class regressions on the cheap SQLite path.
    """
    _InstanceState._expire = _orig_instance_expire  # type: ignore[assignment]
    yield
    _InstanceState._expire = _noop_expire  # type: ignore[assignment]


# Pytest fixture names that guarantee MissingGreenlet test fidelity on SQLite.
# Recognised by TestMissingGreenletTestsFaithful in test_audit_finding_class_guards.py:
# a test that accepts one of these as a function argument is considered faithful
# because the fixture either restores real expiry (expire_fidelity) or raises
# MissingGreenlet directly on expired-attribute access (missing_greenlet_proxy).
GREENLET_FIDELITY_FIXTURE_NAMES: tuple[str, ...] = (
    "expire_fidelity",       # restores _InstanceState._expire for one test
    "missing_greenlet_proxy",  # proxy factory that raises MissingGreenlet on poison
)
