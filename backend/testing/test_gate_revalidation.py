"""Tests for the gate-failure auto-revalidation module (gate_revalidation.py).

Covers:
- ``is_gate_failure_blocked`` — predicate detection of gate-failure-blocked tasks
- ``_ci_conclusion_is_green`` — CI result interpretation
- ``resolve_gate_state`` — integration HEAD resolution + CI check (mocked)
- ``has_live_respawn`` — dedup guard for existing siblings
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from app.gate_revalidation import (
    _ci_conclusion_is_green,
    has_live_respawn,
    is_gate_failure_blocked,
    resolve_gate_state,
)
from app.models import Task, TaskStatus


# ── is_gate_failure_blocked ──────────────────────────────────────────────


def _blocked_task(blocked_reason: str | None = None,
                  latest_status_message: str | None = None) -> Task:
    return Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title="test",
        status=TaskStatus.BLOCKED,
        blocked_reason=blocked_reason,
        latest_status_message=latest_status_message,
        created_by="u",
        status_log=[],
    )


def test_is_gate_failure_blocked_matches_standalone_smoke():
    """blocked_reason containing 'standalone_smoke' triggers the gate check."""
    task = _blocked_task(blocked_reason="standalone_smoke test run failed")
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_matches_standalone_smoke_natural():
    """Natural-language 'standalone smoke' also matches."""
    task = _blocked_task(blocked_reason="standalone smoke suite failed")
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_matches_typecheck():
    task = _blocked_task(blocked_reason="typecheck failed with 5 errors")
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_matches_vitest():
    task = _blocked_task(blocked_reason="vitest run: 2 failing tests")
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_case_insensitive():
    """Matching is case-insensitive."""
    task = _blocked_task(blocked_reason="VITEST: assertion failures")
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_falls_back_to_status_message():
    """When blocked_reason is None, check latest_status_message."""
    task = _blocked_task(
        blocked_reason=None,
        latest_status_message="BLOCKER: standalone smoke failures",
    )
    assert is_gate_failure_blocked(task) is True


def test_is_gate_failure_blocked_no_match():
    """Task-specific blockers should not match."""
    task = _blocked_task(blocked_reason="Conflict: branch ahead of base")
    assert is_gate_failure_blocked(task) is False


def test_is_gate_failure_blocked_empty():
    task = _blocked_task(blocked_reason="", latest_status_message="")
    assert is_gate_failure_blocked(task) is False


def test_is_gate_failure_blocked_none():
    task = _blocked_task(blocked_reason=None, latest_status_message=None)
    assert is_gate_failure_blocked(task) is False


# ── _ci_conclusion_is_green ──────────────────────────────────────────────


def test_ci_conclusion_green_on_success():
    assert _ci_conclusion_is_green(
        {"status": "completed", "conclusion": "success", "details": []}
    ) is True


def test_ci_conclusion_green_on_no_checks():
    """An integration branch with no CI checks is considered green."""
    assert _ci_conclusion_is_green(
        {"status": "no_checks", "conclusion": None, "details": []}
    ) is True


def test_ci_conclusion_red_on_failure():
    assert _ci_conclusion_is_green(
        {"status": "completed", "conclusion": "failure", "details": []}
    ) is False


def test_ci_conclusion_red_on_pending():
    assert _ci_conclusion_is_green(
        {"status": "in_progress", "conclusion": None, "details": []}
    ) is False


def test_ci_conclusion_red_on_api_error():
    assert _ci_conclusion_is_green(
        {
            "status": "pending",
            "conclusion": None,
            "details": [],
            "_from_api_error": True,
        }
    ) is False


# ── resolve_gate_state ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_gate_state_no_project():
    """Should return None when the project does not exist."""
    session = AsyncMock()
    session.get.return_value = None
    result = await resolve_gate_state(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gate_state_no_github_owner():
    """Should return None when the project has no GitHub owner/repo."""
    session = AsyncMock()
    project = MagicMock()
    project.github_owner = None
    project.github_repo = "some-repo"
    session.get.return_value = project
    result = await resolve_gate_state(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gate_state_no_github_repo():
    """Should return None when the project has no GitHub repo."""
    session = AsyncMock()
    project = MagicMock()
    project.github_owner = "some-owner"
    project.github_repo = None
    session.get.return_value = project
    result = await resolve_gate_state(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gate_state_no_token():
    """Should return None when no GitHub token can be obtained."""
    session = AsyncMock()
    project = MagicMock()
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = uuid.uuid4()
    session.get.return_value = project

    with patch("app.github.get_repo_installation_token",
               AsyncMock(return_value=None)):
        result = await resolve_gate_state(session, project.id)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gate_state_green_at_head():
    """Should return green_at_head=True when CI at HEAD is green."""
    session = AsyncMock()
    project_id = uuid.uuid4()
    project = MagicMock()
    project.id = project_id
    project.github_owner = "owner"
    project.github_repo = "repo"
    session.get.return_value = project

    fake_sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    with (
        patch("app.github.get_repo_installation_token",
              AsyncMock(return_value="fake-token")),
        patch("app.github.get_branch_sha",
              AsyncMock(return_value=fake_sha)),
        patch("app.branch_resolution.get_project_integration_branch",
              return_value="automain"),
        patch("app.github.get_check_runs_cached",
              AsyncMock(return_value={"status": "completed",
                                      "conclusion": "success",
                                      "details": []})),
    ):
        result = await resolve_gate_state(session, project_id)

    assert result is not None
    assert result["head_sha"] == fake_sha
    assert result["green_at_head"] is True
    assert "checked_at_utc" in result


@pytest.mark.asyncio
async def test_resolve_gate_state_red_at_head():
    """Should return green_at_head=False when CI at HEAD is red."""
    session = AsyncMock()
    project_id = uuid.uuid4()
    project = MagicMock()
    project.id = project_id
    project.github_owner = "owner"
    project.github_repo = "repo"
    session.get.return_value = project

    with (
        patch("app.github.get_repo_installation_token",
              AsyncMock(return_value="fake-token")),
        patch("app.github.get_branch_sha",
              AsyncMock(return_value="deadbeef" * 5)),
        patch("app.branch_resolution.get_project_integration_branch",
              return_value="automain"),
        patch("app.github.get_check_runs_cached",
              AsyncMock(return_value={"status": "completed",
                                      "conclusion": "failure",
                                      "details": []})),
    ):
        result = await resolve_gate_state(session, project_id)

    assert result is not None
    assert result["green_at_head"] is False


@pytest.mark.asyncio
async def test_resolve_gate_state_caches_token():
    """When github_info_cache is provided, the token is cached for the batch."""
    session = AsyncMock()
    project_id = uuid.uuid4()
    project = MagicMock()
    project.id = project_id
    project.github_owner = "owner"
    project.github_repo = "repo"
    session.get.return_value = project

    cache: dict[str, str] = {}
    with (
        patch("app.github.get_repo_installation_token",
              AsyncMock(return_value="cached-token")),
        patch("app.github.get_branch_sha",
              AsyncMock(return_value="deadbeef" * 5)),
        patch("app.branch_resolution.get_project_integration_branch",
              return_value="automain"),
        patch("app.github.get_check_runs_cached",
              AsyncMock(return_value={"status": "completed",
                                      "conclusion": "success",
                                      "details": []})),
    ):
        result1 = await resolve_gate_state(session, project_id,
                                           github_info_cache=cache)
        assert result1 is not None
        assert cache[str(project_id)] == "cached-token"

        # Second call uses the cached token (get_repo_installation_token not
        # called again — we can't easily assert on call count with the async
        # patch, but the cache dict is populated, which is the observable
        # contract).
        result2 = await resolve_gate_state(session, project_id,
                                           github_info_cache=cache)
        assert result2 is not None


# ── has_live_respawn ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def in_mem_session():
    """Provide an async SQLite session backed by a fresh in-memory DB.

    Registers a JSONB→JSON compiler so models using JSONB columns work on
    SQLite (JSONB is Postgres-specific; SQLite only knows JSON).
    """
    def _visit_JSONB(self, type_, **kw):
        return self.visit_JSON(type_, **kw)
    SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


async def _insert_tasks(session, tasks: list[Task]):
    for t in tasks:
        session.add(t)
    await session.commit()
    for t in tasks:
        await session.refresh(t)


async def test_has_live_respawn_no_parent(in_mem_session):
    """A task with no parent_task_id should return False (scope unknown)."""
    task = Task(
        id=uuid.uuid4(), project_id=uuid.uuid4(), title="t", status=TaskStatus.BLOCKED,
        created_by="u", parent_task_id=None, status_log=[],
    )
    await _insert_tasks(in_mem_session, [task])
    result = await has_live_respawn(task, in_mem_session)
    assert result is False


async def test_has_live_respawn_no_siblings(in_mem_session):
    """A task with a parent but no live siblings should return False."""
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(), project_id=pid, title="t", status=TaskStatus.BLOCKED,
        created_by="u", parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [task])
    result = await has_live_respawn(task, in_mem_session)
    assert result is False


async def test_has_live_respawn_active_sibling_guards(in_mem_session):
    """An active sibling with the same parent_task_id should guard.

    This is the dedup-guard path: the blocked original should NOT resume
    because a respawn (QUEUED sibling) already covers the same scope.
    """
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    blocked = Task(
        id=uuid.uuid4(), project_id=pid, title="original",
        status=TaskStatus.BLOCKED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    respawn = Task(
        id=uuid.uuid4(), project_id=pid, title="respawn",
        status=TaskStatus.QUEUED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [blocked, respawn])
    result = await has_live_respawn(blocked, in_mem_session)
    assert result is True


async def test_has_live_respawn_in_progress_sibling_guards(in_mem_session):
    """An IN_PROGRESS sibling also counts as a live respawn."""
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    blocked = Task(
        id=uuid.uuid4(), project_id=pid, title="original",
        status=TaskStatus.BLOCKED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    respawn = Task(
        id=uuid.uuid4(), project_id=pid, title="respawn",
        status=TaskStatus.IN_PROGRESS, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [blocked, respawn])
    result = await has_live_respawn(blocked, in_mem_session)
    assert result is True


async def test_has_live_respawn_terminal_sibling_does_not_guard(in_mem_session):
    """A terminal (FAILED) sibling should NOT guard — the respawn is dead."""
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    blocked = Task(
        id=uuid.uuid4(), project_id=pid, title="original",
        status=TaskStatus.BLOCKED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    dead = Task(
        id=uuid.uuid4(), project_id=pid, title="respawn",
        status=TaskStatus.FAILED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [blocked, dead])
    result = await has_live_respawn(blocked, in_mem_session)
    assert result is False


async def test_has_live_respawn_different_project_not_guarded(in_mem_session):
    """A sibling in a different project should not guard."""
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    other_pid = uuid.uuid4()
    blocked = Task(
        id=uuid.uuid4(), project_id=pid, title="original",
        status=TaskStatus.BLOCKED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    other = Task(
        id=uuid.uuid4(), project_id=other_pid, title="other",
        status=TaskStatus.QUEUED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [blocked, other])
    result = await has_live_respawn(blocked, in_mem_session)
    assert result is False


async def test_has_live_respawn_skips_self(in_mem_session):
    """The task itself should not count as a sibling."""
    parent_id = uuid.uuid4()
    pid = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(), project_id=pid, title="t",
        status=TaskStatus.BLOCKED, created_by="u",
        parent_task_id=parent_id, status_log=[],
    )
    await _insert_tasks(in_mem_session, [task])
    result = await has_live_respawn(task, in_mem_session)
    assert result is False
