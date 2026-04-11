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
os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-key-for-pytest")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-ticket-secret")

# SQLite does not support PostgreSQL's JSONB type.  Add a visit_JSONB method
# to the SQLite type compiler so it renders JSONB as plain JSON when the
# in-memory SQLite engine creates tables for testing.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

def _sqlite_visit_JSONB(self, type_, **kw):
    return self.visit_JSON(type_, **kw)

_SQLiteTypeCompiler.visit_JSONB = _sqlite_visit_JSONB

from app.main import app  # noqa: E402
from app.database import get_session  # noqa: E402
from app.auth import get_current_user, require_admin  # noqa: E402

# --- Database engine selection ---
# CI explicitly sets USE_TEST_POSTGRES=true to use real PostgreSQL.
# Local dev (including docker-compose) defaults to in-memory SQLite for speed.
_db_url = os.getenv("DATABASE_URL", "")
USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and _db_url.startswith("postgresql")

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
        autocommit=False, autoflush=False, bind=engine, class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """
    Provides a database session for each test.
    - PostgreSQL (CI): nested savepoint pattern — test commits go to savepoint,
      outer transaction rolled back after test for full isolation.
    - SQLite (local): creates/drops all tables per test.
    """
    if USE_POSTGRES:
        # Use a fresh session per test. Tests commit normally.
        # Isolation is achieved by truncating all tables after each test.
        async with TestingSessionLocal() as session:
            yield session

        # Clean up: delete test data in dependency order (avoids FK violations and ORM event triggers)
        async with engine.begin() as conn:
            for table in ("artifact", "taskhistory", "alert", "task", "projectmember", "project", "maxplanaccount", "userintegration", "alloweduser"):
                await conn.execute(sqlalchemy.text(f"DELETE FROM {table}"))
            # Reset deploymentstate to empty (singleton, don't delete)
            await conn.execute(sqlalchemy.text("DELETE FROM deploymentstate"))
    else:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with TestingSessionLocal() as session:
            yield session
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)


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
        return {"user_id": "test-user-123", "email": "test@example.com", "role": "admin"}
    app.dependency_overrides[get_current_user] = _override_get_current_user
    # Also bypass the require_admin router-level dependency directly
    app.dependency_overrides[require_admin] = lambda: {"user_id": "test-user-123", "email": "test@example.com", "role": "admin"}
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_admin, None)

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
    underlying = getattr(limiter, '_limiter', None)
    storage = getattr(underlying, 'storage', None) if underlying is not None else None
    if storage is not None:
        if hasattr(storage, 'reset'):
            storage.reset()
        else:
            # Fallback: clear whichever internal dict the storage uses
            for attr in ('_storage', 'storage', '_data', 'data'):
                bucket = getattr(storage, attr, None)
                if isinstance(bucket, dict):
                    bucket.clear()
                    break
    yield


@pytest_asyncio.fixture(scope="function")
async def client(override_get_session, override_get_current_user):
    """
    Provides an async HTTP client for testing the FastAPI endpoints.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_provision_vm(mocker):
    return mocker.patch('app.main.provision_agent_vm', return_value=None)


@pytest.fixture
def mock_delete_vm(mocker):
    return mocker.patch('app.main.delete_agent_vm', return_value=None)


@pytest.fixture
def mock_broadcast(mocker):
    from unittest.mock import AsyncMock
    return mocker.patch('app.broadcast.broadcast_to_project', new_callable=AsyncMock)


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
    assert alerts, f"Expected an alert with severity={severity!r} for task {task_id}, but none found"
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
