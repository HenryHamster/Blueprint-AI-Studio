"""Tests for ``app.database.resolve_dialect_name``.

The helper fixes a latent footgun (triggering incident: PR #7099). The naive
idiom

    session.bind.dialect.name if session.bind else "postgresql"

silently defaults to ``"postgresql"`` whenever ``session.bind`` is None — which
an ``AsyncSession`` can be when it resolves its engine lazily via
``get_bind()`` rather than via an eagerly-stored ``.bind``. On a SQLite-bound
session the idiom then emits Postgres-only SQL (e.g. the ``@>`` JSONB operator)
→ SQLite blows up with ``unrecognized token: "@"``. A ``--postgres``-only test
run never trips it because the dialect is genuinely Postgres there.

``resolve_dialect_name`` instead falls through to ``session.get_bind()`` on the
None-bind path, which resolves the ACTUAL engine the query runs against (SQLite
vs Postgres), and only defaults to ``"postgresql"`` if ``get_bind()`` itself
raises.

The tests are deliberately dialect-independent — the None-bind cases assert the
correct ``"sqlite"`` resolution regardless of whether the suite runs against
SQLite or ``--postgres``, because they pin the bind explicitly.
"""

from unittest.mock import MagicMock

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import resolve_dialect_name


def _sqlite_engine():
    """A fresh in-memory SQLite async engine (StaticPool keeps one conn)."""
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


async def test_real_bound_sqlite_session_resolves_sqlite():
    """End-to-end with a real engine: a session explicitly bound to a SQLite
    engine has a non-None ``.bind`` and the helper returns "sqlite" — i.e. the
    common (already-working) path keeps working, against a live engine.
    """
    engine = _sqlite_engine()
    try:
        async with AsyncSession(bind=engine, expire_on_commit=False) as session:
            assert session.bind is not None
            assert session.bind.dialect.name == "sqlite"
            assert resolve_dialect_name(session) == "sqlite"
    finally:
        await engine.dispose()


def test_explicit_bind_returns_its_dialect_and_skips_get_bind():
    """When ``.bind`` is set the helper reads it directly and never calls
    ``get_bind()`` — preserving today's behaviour for every session that has an
    explicit bind (i.e. all currently-passing tests).
    """
    session = MagicMock()
    session.bind = MagicMock()
    session.bind.dialect.name = "postgresql"
    # If the helper ever fell through to get_bind() this would explode.
    session.get_bind.side_effect = AssertionError(
        "get_bind() must not be called when .bind is set"
    )
    assert resolve_dialect_name(session) == "postgresql"


def test_none_bind_resolves_via_get_bind_sqlite():
    """THE regression: ``.bind`` is None (the footgun condition) but the session
    still resolves a SQLite engine via ``get_bind()``. The helper MUST return
    "sqlite" — NOT silently default to "postgresql" the way the old idiom did.

    ``get_bind()`` is backed by a real SQLite engine so the dialect name is
    produced by SQLAlchemy itself, not hand-asserted.
    """
    engine = _sqlite_engine()
    try:
        session = MagicMock()
        session.bind = None
        session.get_bind.return_value = engine
        assert session.get_bind().dialect.name == "sqlite"
        assert resolve_dialect_name(session) == "sqlite"
        # And critically: it did NOT fall back to the Postgres default.
        assert resolve_dialect_name(session) != "postgresql"
    finally:
        engine.sync_engine.dispose()


def test_none_bind_resolves_via_get_bind_postgresql():
    """Symmetry: when ``.bind`` is None but ``get_bind()`` resolves a Postgres
    engine, the helper returns "postgresql" (so the real prod path keeps
    emitting JSONB SQL). Mocked so no live Postgres is required.
    """
    session = MagicMock()
    session.bind = None
    session.get_bind.return_value.dialect.name = "postgresql"
    assert resolve_dialect_name(session) == "postgresql"


def test_get_bind_failure_falls_back_to_postgresql():
    """When ``.bind`` is None AND ``get_bind()`` raises (no engine configured at
    all), the helper degrades to the production default "postgresql" rather than
    crashing the query path — and only then.
    """
    session = MagicMock()
    session.bind = None
    session.get_bind.side_effect = RuntimeError("no engine bound")
    assert resolve_dialect_name(session) == "postgresql"
