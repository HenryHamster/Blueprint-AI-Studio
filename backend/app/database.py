import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")

def _make_engine():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")
    return create_async_engine(
        DATABASE_URL,
        echo=os.getenv("SQL_ECHO", "false").lower() == "true",
        future=True,
        pool_size=40,
        max_overflow=60,
        pool_timeout=30,
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "lock_timeout": "30000",  # 30s — prevents indefinite lock waits on FOR UPDATE
            }
        },
    )

# Lazy engine: created on first access so test environments can import without asyncpg
class _LazyEngine:
    _engine = None

    def __getattr__(self, name):
        if self._engine is None:
            self.__class__._engine = _make_engine()
        return getattr(self._engine, name)

    def __call__(self, *args, **kwargs):
        if self._engine is None:
            self.__class__._engine = _make_engine()
        return self._engine(*args, **kwargs)

engine = _LazyEngine()

# Session factory for background tasks and dependency injection
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    """Schema management is handled by Alembic migrations."""
    pass

async def get_session() -> AsyncSession: # type: ignore
    """Dependency to provide a database session to FastAPI routes."""
    async with AsyncSessionLocal() as session:
        yield session
