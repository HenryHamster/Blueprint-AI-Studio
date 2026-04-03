import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

# SQLite does not support PostgreSQL's JSONB type.  Add a visit_JSONB method
# to the SQLite type compiler so it renders JSONB as plain JSON when the
# in-memory SQLite engine creates tables for testing.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

def _sqlite_visit_JSONB(self, type_, **kw):
    return self.visit_JSON(type_, **kw)

_SQLiteTypeCompiler.visit_JSONB = _sqlite_visit_JSONB

from app.main import app
from app.database import get_session
from app.auth import get_current_user

# SQLite doesn't support PostgreSQL's JSONB type. Patch the SQLite type compiler
# so that JSONB columns are treated as TEXT, allowing the in-memory test DB to work.
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

def _visit_JSONB(self, type_, **kw):
    return "TEXT"

SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

# Use an in-memory SQLite database for testing.
# StaticPool ensures all sessions (including per-request ones created by the
# override) share the same single connection, so test data is visible across them.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, class_=AsyncSession,
    # Prevent async "lazy-load on expired attribute" errors with aiosqlite:
    # after a commit the objects retain their in-memory state rather than being
    # marked expired (which would force a synchronous reload incompatible with
    # the aiosqlite async driver used in tests).
    expire_on_commit=False,
)

@pytest_asyncio.fixture(scope="function")
async def db_session():
    """
    Creates a fresh in-memory database for every single test.
    """
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with TestingSessionLocal() as session:
        yield session

    # Drop tables after the test finishes
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)

@pytest.fixture(scope="function")
def override_get_session(db_session):
    """
    Overrides the FastAPI dependency so the API uses our test database.
    Each request gets its own session instance (matching production behaviour),
    but all sessions share the same StaticPool connection so test data committed
    by db_session is immediately visible to the API routes.
    """
    async def _override_get_session():
        async with TestingSessionLocal() as session:
            yield session
    app.dependency_overrides[get_session] = _override_get_session
    yield
    app.dependency_overrides.pop(get_session, None)

@pytest.fixture(scope="function")
def override_get_current_user():
    """
    Overrides the authentication dependency so we don't need real tokens.
    """
    async def _override_get_current_user():
        return {"user_id": "test-user-123", "email": "test@example.com"}
    app.dependency_overrides[get_current_user] = _override_get_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)

@pytest_asyncio.fixture(scope="function")
async def client(override_get_session, override_get_current_user):
    """
    Provides an async HTTP client for testing the FastAPI endpoints.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
