"""Tests for project-configurable approval gate file lists.

Verifies that the approval gate can be configured per-project:
- Without config, self-repo projects use Blueprint defaults
- Without config, non-self-repo projects use empty lists
- With config, projects can define their own sensitive file paths
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.task_lifecycle import _get_approval_gate_file_lists
from app.models import Project


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_self_repo_defaults():
    """Without approval_gate_config, self-repo projects use Blueprint defaults."""
    project = Project(name="Blueprint Self", user_id="u1", is_self_repo=True)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # Verify Blueprint defaults are returned
    assert "backend/app/main.py" in infra_files
    assert "backend/app/models.py" in infra_files
    assert "backend/app/security.py" in infra_files
    assert "backend/app/auth.py" in infra_files
    assert "agent/src/index.ts" in infra_files
    assert "CLAUDE.md" in infra_files
    assert "backend/promotion_policy.yaml" in infra_files

    assert "backend/app/security.py" in critical_files
    assert "backend/app/auth.py" in critical_files
    assert "CLAUDE.md" in critical_files
    assert "backend/promotion_policy.yaml" in critical_files


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_non_self_repo_empty():
    """Without approval_gate_config, non-self-repo projects use empty lists."""
    project = Project(name="Other Project", user_id="u1", is_self_repo=False)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_custom_config():
    """With approval_gate_config, projects use their custom file lists."""
    project = Project(
        name="Custom Project",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={
            "infra_files": ["src/main.py", "src/config.py", "src/db.py"],
            "critical_files": ["src/config.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert infra_files == {"src/main.py", "src/config.py", "src/db.py"}
    assert critical_files == {"src/config.py"}


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_custom_config_overrides_self_repo():
    """With approval_gate_config, self-repo projects use config instead of defaults."""
    project = Project(
        name="Self with Custom Config",
        user_id="u1",
        is_self_repo=True,
        approval_gate_config={
            "infra_files": ["custom/file1.py"],
            "critical_files": ["custom/file1.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # Should use custom config, not Blueprint defaults
    assert infra_files == {"custom/file1.py"}
    assert critical_files == {"custom/file1.py"}
    assert "backend/app/main.py" not in infra_files


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_partial_config():
    """Partial config (only infra_files) still triggers custom mode."""
    project = Project(
        name="Partial Config",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={
            "infra_files": ["src/main.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # Should use custom mode (non-empty config found)
    assert infra_files == {"src/main.py"}
    assert critical_files == set()


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_none_project():
    """Handles None project gracefully."""
    infra_files, critical_files = _get_approval_gate_file_lists(None)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest_asyncio.fixture()
async def approval_gate_db(mocker):
    """Isolated in-memory DB for approval gate integration tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.mark.asyncio
async def test_approval_gate_config_persisted_in_db(approval_gate_db):
    """approval_gate_config is properly persisted and retrieved from DB."""
    config = {
        "infra_files": ["src/main.py", "src/auth.py"],
        "critical_files": ["src/auth.py"],
    }
    project = Project(
        name="Persistent Config",
        user_id="u1",
        approval_gate_config=config,
    )
    approval_gate_db.add(project)
    await approval_gate_db.commit()
    await approval_gate_db.refresh(project)

    # Verify config is retrieved correctly
    assert project.approval_gate_config == config
    infra_files, critical_files = _get_approval_gate_file_lists(project)
    assert infra_files == {"src/main.py", "src/auth.py"}
    assert critical_files == {"src/auth.py"}


@pytest.mark.asyncio
async def test_approval_gate_empty_config_uses_defaults(approval_gate_db):
    """Empty approval_gate_config dict falls back to defaults for self-repo."""
    project = Project(
        name="Self Repo Empty Config",
        user_id="u1",
        is_self_repo=True,
        approval_gate_config={},
    )
    approval_gate_db.add(project)
    await approval_gate_db.commit()
    await approval_gate_db.refresh(project)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # Empty config should fall back to Blueprint defaults for self-repo
    assert "backend/app/main.py" in infra_files
    assert "backend/app/security.py" in critical_files
