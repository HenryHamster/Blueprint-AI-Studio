import os
import uuid
import pytest
import pytest_asyncio
import sqlalchemy
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

# Set required environment variables before any app modules are imported.
# These are dummy values used only for tests — real credentials are not needed
# because auth dependencies are overridden below.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-key-for-pytest-min32")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-ticket-secret-min-32byts")

# SQLite does not support PostgreSQL's JSONB type.  Add a visit_JSONB method
# to the SQLite type compiler so it renders JSONB as plain JSON when the
# in-memory SQLite engine creates tables for testing.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler  # noqa: E402


def _sqlite_visit_JSONB(self, type_, **kw):
    return self.visit_JSON(type_, **kw)


_SQLiteTypeCompiler.visit_JSONB = _sqlite_visit_JSONB

from app.main import app  # noqa: E402
from app.database import get_session  # noqa: E402
from app.auth import get_current_user, get_current_user_optional, require_admin  # noqa: E402

# --- Database engine selection ---
# CI explicitly sets USE_TEST_POSTGRES=true to use real PostgreSQL.
# Local dev (including docker-compose) defaults to in-memory SQLite for speed.
_db_url = os.getenv("DATABASE_URL", "")
USE_POSTGRES = os.getenv(
    "USE_TEST_POSTGRES", ""
).lower() == "true" and _db_url.startswith("postgresql")

if USE_POSTGRES:
    # CI mode: real PostgreSQL (tables created by alembic migration before tests)
    # Use NullPool to avoid connection state leaking between tests
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_db_url, echo=False, future=True, poolclass=NullPool)
    TestingSessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
else:
    # Local mode: in-memory SQLite (fast, no setup needed)
    # SQLite doesn't support PostgreSQL's JSONB type — patch the type compiler.
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    def _visit_JSONB(self, type_, **kw):
        return "TEXT"

    SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

# Point the production lazy engine (used by `AsyncSessionLocal()` in background-task
# and handler code paths that don't go through the overridden get_session dependency,
# e.g. max_plan_pool._persist_rate_limit_state) at the test engine. The production
# engine pools connections, so a pooled asyncpg connection created in one test's
# event loop gets reused in a later test's loop and raises "got Future attached to a
# different loop". Both test engines use a non-reusing pool (NullPool for Postgres,
# StaticPool for SQLite), so binding here keeps every session on the same DB without
# cross-loop connection reuse.
import app.database as _app_database  # noqa: E402

_app_database._LazyEngine._engine = engine


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """
    Provides a database session for each test.
    - PostgreSQL (CI): tests commit normally; isolation is achieved by
      ``DELETE``-ing every app table after each test in a FK-order-INDEPENDENT
      way — the order is computed from SQLModel metadata (children before
      parents), so there is no hand-maintained list to drift out of sync.
    - SQLite (local): creates/drops all tables per test.
    """
    if USE_POSTGRES:
        # Use a fresh session per test. Tests commit normally.
        # Isolation is achieved by deleting all table rows after each test.
        async with TestingSessionLocal() as session:
            yield session

        # Clean up: wipe every app table, FK-order-INDEPENDENTLY. The table
        # order is derived from SQLModel's metadata (the same source the schema
        # is built from), so adding a new model table is picked up
        # automatically — there is no hand-maintained dependency-ordered DELETE
        # loop to silently drift out of sync with the schema (design-audit P6).
        #
        # We DELETE rather than TRUNCATE deliberately. TRUNCATE (even
        # TRUNCATE ... CASCADE) takes an ``AccessExclusiveLock`` on every named
        # table *and* every cascade-reachable FK child, which conflicts with
        # ANY other lock — including the plain reads/writes still held by
        # endpoint request-sessions and background tasks that a test's
        # ``/api/...`` call left in flight (those run on separate connections
        # of the same NullPool engine / the production lazy engine). Two such
        # connections grabbing the cascade locks in different orders deadlock,
        # which is exactly the ``DeadlockDetectedError`` the CASCADE variant hit
        # on CI shards 1 & 2. ``DELETE`` only takes a ``RowExclusiveLock``,
        # which coexists with concurrent reads and never escalates to a
        # whole-table exclusive lock, so it is deadlock-free in this harness.
        #
        # ``sorted_tables`` is ordered parents-before-children (FK-dependency
        # order); reversing it gives children-before-parents, so each DELETE
        # runs after its dependents are already empty and no FK constraint is
        # violated. ``alembic_version`` is NOT a SQLModel table, so it is
        # intentionally left untouched (the migration head must persist).
        ordered_tables = list(reversed(SQLModel.metadata.sorted_tables))
        if ordered_tables:
            async with engine.begin() as conn:
                for table in ordered_tables:
                    await conn.execute(sqlalchemy.text(f'DELETE FROM "{table.name}"'))
    else:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with TestingSessionLocal() as session:
            yield session
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_production_engine_per_test():
    """Dispose the lazy production engine after every test so its pooled
    connections never outlive the test's event loop.

    ``app.database.engine`` is a *pooled* engine (``_make_engine``: pool_size=10,
    pool_recycle=540) created on first use and bound to whatever event loop
    first touches it.  Code paths that use the production ``AsyncSessionLocal``
    rather than this module's ``TestingSessionLocal`` — notably the release
    qualifier's dedicated-session promote chain
    (``_finalize_in_dedicated_session``), plus assorted background-task helpers
    in ``agent.py`` / ``admin_pool_accounts.py`` — bind that engine's pool to
    the loop of whichever test runs first.  Because pytest-asyncio uses a fresh
    function-scoped event loop per test, a *later* test that touches the same
    engine on a different loop raises ``got Future <...> attached to a different
    loop`` / ``Event loop is closed`` — an order-dependent flake (it is exactly
    why ``test_promote_does_not_commit_caller_unrelated_transaction`` failed in
    the full suite but passed in isolation).

    In PostgreSQL mode, nulling ``_LazyEngine._engine`` forces
    ``AsyncSessionLocal`` (bound to the lazy proxy) to rebuild a fresh engine on
    the *next* test's own loop.  In local SQLite mode, production
    ``_make_engine`` cannot rebuild a valid SQLite engine because it carries
    Postgres pool arguments, so the lazy engine is rebound to the shared test
    engine after disposal.  ``dispose()`` is best-effort cleanup — a pool bound
    to an already-dead loop may raise, which is harmless once the reference is
    dropped.  Reaching into ``_LazyEngine._engine`` directly (not via the proxy)
    avoids re-creating the engine on the teardown loop just to dispose it.
    """
    yield
    import contextlib

    from app import database

    eng = database._LazyEngine._engine
    if eng is not None:
        # dispose() may raise if the pool is bound to an already-dead loop —
        # intentionally ignorable; dropping the reference below is what matters.
        with contextlib.suppress(Exception):
            await eng.dispose()
        database._LazyEngine._engine = None if USE_POSTGRES else engine


@pytest.fixture(scope="function")
def override_get_session(db_session):
    """
    Overrides the FastAPI dependency so the API uses our test database.
    """
    if USE_POSTGRES:
        # PostgreSQL: each request gets its own session (same as production behavior)
        async def _override():
            async with TestingSessionLocal() as session:
                yield session
    else:
        # SQLite: each request gets its own session on the shared StaticPool connection
        async def _override():
            async with TestingSessionLocal() as session:
                yield session

    app.dependency_overrides[get_session] = _override
    yield
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(scope="function")
def override_get_current_user():
    """
    Overrides the authentication dependency so we don't need real tokens.
    Returns an admin user so the /api/admin/* endpoints are accessible.
    """

    async def _override_get_current_user():
        return {
            "user_id": "test-user-123",
            "email": "test@example.com",
            "role": "admin",
        }

    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_current_user_optional] = _override_get_current_user
    # Also bypass the require_admin router-level dependency directly
    app.dependency_overrides[require_admin] = lambda: {
        "user_id": "test-user-123",
        "email": "test@example.com",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_user_optional, None)
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture(autouse=True)
def reset_digest_cache():
    """Clear the in-memory digest TTL cache and the precompute caches between
    tests so a stale response from one test's DB state isn't served to the
    next. The precompute caches (digest + health-overview) are module-level
    singletons primed by the leader's background tick, so they must be reset
    alongside the TTL cache."""
    from app.digest_cache import _default_cache
    from app.digest_precompute import get_health_precompute_cache, get_precompute_cache

    _default_cache.clear()
    get_precompute_cache().clear()
    get_health_precompute_cache().clear()
    yield
    _default_cache.clear()
    get_precompute_cache().clear()
    get_health_precompute_cache().clear()


@pytest.fixture(autouse=True)
def reset_runtime_config():
    """Snapshot and restore runtime_config around every test.

    Tests that call admin-settings PATCH endpoints mutate the global
    runtime_config dict in-place (e.g. residual_audit_defer_escalation_threshold).
    Without a reset those mutations bleed into unrelated tests and break
    assertions that rely on hardcoded defaults.
    """
    import copy
    from app import shared_state

    snapshot = copy.deepcopy(shared_state.runtime_config)
    yield
    shared_state.runtime_config.clear()
    shared_state.runtime_config.update(snapshot)


@pytest.fixture(autouse=True)
def reset_github_module_caches():
    """Clear module-level GitHub API caches between tests.

    ``_list_prs_for_commit_cache`` (github_refs) and ``_check_runs_cache``
    (github_checks) are module-level dicts that persist across tests.  Without
    this fixture a successful call in test A populates the cache, and test B
    with the same SHA receives the stale cached result instead of making its own
    mocked HTTP call — breaking non-200 and non-list-body error-path tests.
    """
    from app import github_refs, github_checks

    github_refs._list_prs_for_commit_cache.clear()
    github_checks._check_runs_cache.clear()
    yield
    github_refs._list_prs_for_commit_cache.clear()
    github_checks._check_runs_cache.clear()


@pytest.fixture(autouse=True)
def reset_detect_local_gpu_cache():
    """Reset the module-level detect_local_gpu cache before and after every test.

    detect_local_gpu() caches its result in a process-lifetime global.  Without
    this fixture a value populated (or monkeypatched) by one test leaks into the
    next, causing order-dependent failures in parallel or xdist runs.
    """
    from app import compute_backends as cb

    cb._detect_local_gpu_cache = cb._DETECT_LOCAL_GPU_UNSET
    yield
    cb._detect_local_gpu_cache = cb._DETECT_LOCAL_GPU_UNSET


@pytest.fixture(autouse=True)
def reset_runner_rate_limiter():
    """Reset the in-memory runner rate limiter buckets before every test.

    Prevents rate-limit state from leaking across tests.
    """
    from app.runner_rate_limiter import reset_runner_rate_limiter

    reset_runner_rate_limiter()
    yield
    reset_runner_rate_limiter()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """
    Reset the slowapi in-memory rate-limit counters before every test so that
    repeated calls to the same endpoint don't trigger 429 errors.

    slowapi's Limiter stores rate-limit data via limiter._limiter.storage
    (a limits.MemoryStorage instance), not at limiter._storage directly.
    """
    from app.main import limiter

    # slowapi wraps a limits strategy object; the storage is one level deeper
    underlying = getattr(limiter, "_limiter", None)
    storage = getattr(underlying, "storage", None) if underlying is not None else None
    if storage is not None:
        if hasattr(storage, "reset"):
            storage.reset()
        else:
            # Fallback: clear whichever internal dict the storage uses
            for attr in ("_storage", "storage", "_data", "data"):
                bucket = getattr(storage, attr, None)
                if isinstance(bucket, dict):
                    bucket.clear()
                    break
    yield


_TEST_CSRF_TOKEN = "test-csrf-token-for-client-fixture"


@pytest.fixture(autouse=True)
def _allow_plaintext_credentials_for_tests(monkeypatch):
    """Allow encryption pass-through for tests without GITHUB_PAT_ENCRYPTION_KEY.

    Most tests do not configure an encryption key and should not need to.
    This fixture sets ``ALLOW_PLAINTEXT_CREDENTIALS=true`` so that
    :func:`~app.field_encryption.encrypt_field` and
    :func:`~app.field_encryption.decrypt_field` pass through values unchanged
    instead of raising ``RuntimeError``.

    Tests that specifically verify the error path when neither the key nor the
    opt-in is set can remove the variable with their own ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")


@pytest_asyncio.fixture(scope="function")
async def client(override_get_session, override_get_current_user):
    """
    Provides an async HTTP client for testing the FastAPI endpoints.

    Uses matching CSRF cookie + header so the double-submit cookie check in
    CSRFMiddleware passes without needing a Bearer token.  This keeps
    agent-endpoint tests that supply X-Agent-Token honest: those requests
    are exempted by the X-Agent-Token bypass path, not by Bearer.  The
    actual auth dependency is overridden by override_get_current_user, which
    ignores the token value.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-CSRF-Token": _TEST_CSRF_TOKEN},
        cookies={"csrf_token": _TEST_CSRF_TOKEN},
    ) as ac:
        yield ac


@pytest_asyncio.fixture(scope="function")
async def unauthenticated_client(override_get_session):
    """HTTP client with no Firebase auth — get_current_user_optional returns None.

    Use this to test endpoints that should return a 302 redirect for browser
    users who have no credentials (e.g. operator clicking a PR-body link).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
def missing_greenlet_proxy():
    """Factory: wrap an object so that accessing ``poison_attr`` after
    ``.poison()`` is called raises ``MissingGreenlet`` — mimicking asyncpg's
    behaviour when production code lazy-reads an expired ORM attribute in an
    async context after a session rollback.

    Usage::

        proxy = missing_greenlet_proxy(project, poison_attr="id")
        # ... trigger rollback in production code ...
        proxy.poison()   # arm the proxy
        # any read of proxy.id now raises MissingGreenlet

    This fixture generalises the ad-hoc ``_ProjectIdPoisonAfterRollback`` proxy
    used in ``test_sync_resident_snapshots_project_id_before_rollback`` so that
    future MissingGreenlet regression tests can reach for a shared, reusable
    helper instead of reinventing the proxy each time.
    """
    from sqlalchemy.exc import MissingGreenlet as _MG

    class _Proxy:
        def __init__(self, real_obj: object, poison_attr: str) -> None:
            object.__setattr__(self, "_real", real_obj)
            object.__setattr__(self, "_attr", poison_attr)
            object.__setattr__(self, "_poisoned", False)

        def poison(self) -> None:
            object.__setattr__(self, "_poisoned", True)

        def __getattr__(self, name: str) -> object:
            if object.__getattribute__(self, "_poisoned") and name == object.__getattribute__(self, "_attr"):
                raise _MG(
                    "simulated post-rollback lazy-load of expired attribute "
                    "(missing_greenlet_proxy fixture)"
                )
            return getattr(object.__getattribute__(self, "_real"), name)

    def _factory(real_obj: object, *, poison_attr: str) -> _Proxy:
        return _Proxy(real_obj, poison_attr)

    return _factory


@pytest.fixture
def mock_provision_vm(mocker):
    return mocker.patch("app.main.provision_agent_vm", return_value=None)


@pytest.fixture
def mock_delete_vm(mocker):
    return mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)


@pytest.fixture
def mock_broadcast(mocker):
    from unittest.mock import AsyncMock

    return mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)


async def assert_alert_exists(
    session: AsyncSession,
    task_id: uuid.UUID,
    severity: str,
    contains_text: str | None = None,
):
    """Assert at least one undismissed alert exists for task_id+severity, return it."""
    from app.models import Alert

    result = await session.execute(
        select(Alert).where(
            Alert.task_id == task_id,
            Alert.severity == severity,
            Alert.dismissed_at.is_(None),
        )
    )
    alerts = result.scalars().all()
    assert alerts, (
        f"Expected an alert with severity={severity!r} for task {task_id}, but none found"
    )
    if contains_text is not None:
        assert any(contains_text in a.message for a in alerts), (
            f"No alert message contains {contains_text!r}; messages: {[a.message for a in alerts]}"
        )
    return alerts[0]


async def assert_no_alert(
    session: AsyncSession,
    task_id: uuid.UUID,
    severity: str,
) -> None:
    """Assert no undismissed alert exists for task_id+severity."""
    from app.models import Alert

    result = await session.execute(
        select(Alert).where(
            Alert.task_id == task_id,
            Alert.severity == severity,
            Alert.dismissed_at.is_(None),
        )
    )
    alerts = result.scalars().all()
    assert not alerts, (
        f"Expected no alert with severity={severity!r} for task {task_id}, but found {len(alerts)}"
    )
