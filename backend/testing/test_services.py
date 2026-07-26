"""
Tests for recovery functions in backend/app/services.py.

Covers:
  1. _recover_orphaned_tasks — COMPLETED tasks with no PR artifact / no review child
  2. _persist_circuit_breakers — serialize in-memory breaker state to DeploymentState
  3. _persist_zone_health — serialize zone health snapshot to DeploymentState
  4. gcp_cleanup_service — orphaned VM detection and deletion
  5. _check_daily_cost_ceiling — daily compute budget enforcement (in agent_status.py)

Uses isolated in-memory SQLite DB (same pattern as test_pipeline_reliability.py).
"""

import asyncio
import uuid as _uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.shared_state as _shared_state
from app.models import (
    Project,
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    Alert,
    AlertSeverity,
    DeploymentState,
    ComputeJob,
    ComputeJobStatus,
    PoolVm,
    PoolVmState,
)
from app.routers.agent_status import _check_daily_cost_ceiling
from app.services import (
    _derive_task_branch,
    _recover_childless_evaluating_without_marker,
    _recover_orphaned_tasks,
    _run_auto_archive_cycle,
    _sweep_post_commit_pending,
    gcp_cleanup_service,
)
from app.state_persistence import (
    _persist_circuit_breakers,
    _persist_zone_health,
)
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

# ── SQLite JSONB compatibility ────────────────────────────────────────────────

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc_ago(**kwargs) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(tzinfo=None)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def svc_db(mocker):
    """Isolated in-memory SQLite DB for services tests.

    Patches app.services.AsyncSessionLocal so functions use this DB.
    """
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
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    # Suppress broadcast calls across all tests in this fixture
    mocker.patch("app.services.broadcast_to_project", new_callable=AsyncMock)

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _make_project(session) -> Project:
    project = Project(name="Test Project", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_completed_task(
    session, project_id, task_type="create", minutes_ago=30
) -> Task:
    """Create a COMPLETED, ORCHESTRATOR-created task of the given type."""
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type, "prompt": f"do {task_type}"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_naive_utc_ago(minutes=minutes_ago),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ══════════════════════════════════════════════════════════════════════════════
# _recover_orphaned_tasks
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_orphan_recovery_attempts_github_lookup(svc_db, mocker):
    """A COMPLETED ORCHESTRATOR create-task with no PR artifact and no review child
    should attempt a GitHub PR lookup."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="create")

    # Mock token fetch
    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    # Mock GitHub PR lookup returning no open PRs (to avoid spawn logic)
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = []
    mock_github_request = AsyncMock(return_value=mock_gh_resp)
    mocker.patch("app.github._github_request", mock_github_request)

    await _recover_orphaned_tasks()

    mock_get_token.assert_called_once()
    mock_github_request.assert_called_once()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_task_with_pr_artifact(svc_db, mocker):
    """A COMPLETED task that already has a PR artifact should be skipped."""
    project = await _make_project(svc_db)
    task = await _make_completed_task(svc_db, project.id, task_type="create")

    # Create a PR artifact
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/42",
    )
    svc_db.add(artifact)
    await svc_db.commit()

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    # Token fetch should never be called — task was skipped
    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_task_with_review_child(svc_db, mocker):
    """A COMPLETED task that already has a review child should be skipped."""
    project = await _make_project(svc_db)
    parent = await _make_completed_task(svc_db, project.id, task_type="create")

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "review", "pr_number": 1, "branch": "blueprint/task-abc"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    svc_db.add(child)
    await svc_db.commit()

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_research_type(svc_db, mocker):
    """Tasks with type='research' are not code-producing and must be skipped."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="research")

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_github_token_failure_skips_gracefully(svc_db, mocker):
    """If GitHub token fetch raises, the task should be logged+skipped without crashing."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        side_effect=Exception("token fetch failed"),
    )

    # Should not raise
    await _recover_orphaned_tasks()


@pytest.mark.asyncio
async def test_orphan_recovery_creates_artifact_and_review_when_pr_found(
    svc_db, mocker
):
    """When GitHub returns an open PR, a PR artifact and review child must be created."""
    project = await _make_project(svc_db)
    task = await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )

    pr_url = "https://github.com/owner/repo/pull/99"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 99}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "blueprint/task-abc"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(mock_review_task, True),
    )

    await _recover_orphaned_tasks()

    # Verify artifact was created
    artifact = (
        (
            await svc_db.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    assert artifact is not None
    assert artifact.pr_url == pr_url


@pytest.mark.asyncio
async def test_orphan_recovery_only_checks_recent_tasks(svc_db, mocker):
    """Tasks older than 1 hour should not be included in the orphan sweep."""
    project = await _make_project(svc_db)
    # Create a task older than 1 hour
    await _make_completed_task(svc_db, project.id, task_type="create", minutes_ago=90)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    # Task is outside the 1h window, so no GitHub call should occur
    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_too_recent_tasks(svc_db, mocker):
    """Tasks created less than 5 minutes ago should not be swept (still completing)."""
    project = await _make_project(svc_db)
    # Create a task that is only 2 minutes old
    await _make_completed_task(svc_db, project.id, task_type="create", minutes_ago=2)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_recovers_post_commit_failed_outside_time_window(
    svc_db, mocker
):
    """post_commit_failed=True tasks are recovered even when created_at is outside
    the normal 1-hour window.

    Regression test for the late-abandon strand: agent runtime + lock-starvation
    + 90-min post-commit backstop ≈ 3.5h, which places created_at permanently
    outside the 1-hour window by the time the task is abandoned. The
    post_commit_failed flag must trigger recovery independently of creation age.
    """
    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_naive_utc_ago(hours=3, minutes=30),
        post_commit_failed=True,
    )
    svc_db.add(task)
    await svc_db.commit()
    await svc_db.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )

    pr_url = "https://github.com/owner/repo/pull/123"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 123}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 123}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(mock_review_task, True),
    )

    await _recover_orphaned_tasks()

    mock_spawn.assert_called_once()

    artifact = (
        (
            await svc_db.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    assert artifact is not None, "PR artifact must be created for late-abandoned task"
    assert artifact.pr_url == pr_url


@pytest.mark.asyncio
async def test_orphan_recovery_still_skips_old_task_without_post_commit_failed(
    svc_db, mocker
):
    """Tasks outside the 1-hour window without post_commit_failed=True are still skipped.

    Ensures the post_commit_failed OR-extension doesn't widen the window for
    normal old tasks (would waste GitHub API quota on entire task history).
    """
    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do create"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_naive_utc_ago(hours=3, minutes=30),
        post_commit_failed=False,
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_uses_project_namespace(svc_db, mocker):
    """review-respawn derives branches under the project's task_branch_namespace, not hardcoded 'blueprint'."""
    project = Project(
        name="AutoProj",
        user_id="user1",
        build_config={"task_branch_namespace": "autoproj"},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)

    await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )

    pr_url = "https://github.com/owner/repo/pull/42"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 42}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(mock_review_task, True),
    )

    await _recover_orphaned_tasks()

    mock_spawn.assert_called_once()
    _spawned_branch = mock_spawn.call_args[0][4]
    assert _spawned_branch.startswith("autoproj/"), (
        f"expected autoproj/* branch, got {_spawned_branch!r}"
    )


# ── Audit P1 session-hygiene seam ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_recovery_cancelled_row_does_not_drop_next_rows_mutation(
    svc_db, mocker
):
    """Audit P1 seam: a per-row op that is CANCELLED / times out mid-DB-op must
    NOT drop a SUBSEQUENT orphan's recovery mutation in the same sweep, and each
    row must run on its OWN session.

    Pre-fix _recover_orphaned_tasks held ONE shared session across the whole
    per-row loop. A _bounded(get_github_token_for_task, session) timeout fired a
    cancellation INTO the coroutine while it was mid-DB-op on that shared
    session, leaving it in PendingRollback for the REST of the tick; the body's
    per-row ``except`` swallowed the TimeoutError and ``continue``d WITHOUT
    rolling back, so the next orphan's ``session.commit()`` raised
    PendingRollbackError → the outer ``except`` aborted the sweep → every later
    orphan's artifact/alert/review-spawn (or _recovery_attempts back-off bump)
    was silently dropped, exactly under the load (many orphans + slow GitHub)
    where recovery matters most.

    The fix gives each orphan its OWN short-lived session. This test asserts
    BOTH halves of the invariant:
      1. each processed row received a DISTINCT session object (so a poison on
         one cannot reach the next) — this is what FAILS on the old shared
         design (same session id reused), and
      2. the second orphan's recovery mutation (the no-PR back-off bump) still
         commits even though the first row's token fetch raised a TimeoutError.
    """
    project = await _make_project(svc_db)
    # Two distinct orphan candidates. created_at ASC services the oldest first,
    # so t_cancel (older) is processed before t_victim.
    t_cancel = await _make_completed_task(
        svc_db, project.id, task_type="create", minutes_ago=40
    )
    t_victim = await _make_completed_task(
        svc_db, project.id, task_type="create", minutes_ago=30
    )

    # Run the wrapped coroutine on this loop (production _bounded wraps it in
    # asyncio.wait_for which detaches into a new task and loses the greenlet
    # context under in-memory SQLite — a harness-only artifact).
    async def _passthrough_bounded(coro, *_a, **_k):
        return await coro

    mocker.patch("app.services._bounded", side_effect=_passthrough_bounded)

    seen_sessions: list = []

    async def _token_fetch(task_id, session, *_a, **_k):
        # Record which session object this row ran on so the test can prove
        # per-row isolation (distinct objects ⇒ a poison can't cross rows).
        seen_sessions.append((task_id, id(session)))
        if task_id == t_cancel.id:
            # The wait_for ceiling fired: _bounded re-raises TimeoutError after
            # cancelling the wrapped coroutine mid-DB-op. The body logs+skips.
            raise asyncio.TimeoutError("simulated _bounded ceiling on token fetch")
        return ("gh_token", "owner", "repo")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        side_effect=_token_fetch,
    )

    # The victim row's GitHub PR lookup finds no open PR → it takes the
    # _recovery_attempts back-off bump + commit path. That commit is the
    # mutation we assert survives.
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = []
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    # Must not raise even though row #1 is cancelled mid-op.
    await _recover_orphaned_tasks()

    # Both orphans were processed (token fetch attempted on each).
    seen_ids = {tid for tid, _ in seen_sessions}
    assert t_cancel.id in seen_ids and t_victim.id in seen_ids, (
        f"expected both orphans to be processed, saw {seen_ids}"
    )
    # Invariant 1: distinct session object per row — a poisoned session on the
    # cancelled row can never reach the victim row. Fails on the old shared
    # design (single session reused → identical id()).
    session_ids = {sid for _, sid in seen_sessions}
    assert len(session_ids) == len(seen_sessions), (
        "each orphan must run on its OWN session; a session object was reused "
        "across rows (the audit-P1 shared-session design)"
    )

    # Invariant 2: the victim's recovery mutation (back-off bump) committed,
    # despite the cancelled row earlier in the same sweep.
    import app.services as _services

    async with _services.AsyncSessionLocal() as verify:
        victim = await verify.get(Task, t_victim.id)
        assert victim is not None
        assert (victim.context or {}).get("_recovery_attempts") == 1, (
            "the second orphan's recovery mutation was dropped — a cancelled "
            "earlier row leaked into the rest of the sweep (the audit-P1 "
            "shared-session bug)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Registry extensibility: WRITE_TASK_TYPES is the single edit point
# ══════════════════════════════════════════════════════════════════════════════
#
# Design-audit must-fix: a new PR-producing task type must NOT be silently
# invisible to the orphan-recovery sweep. The recovery gate membership-checks
# `app.services.WRITE_TASK_TYPES` (the canonical registry in
# app/task_capabilities.py), so adding a type to the registry is the ONLY edit
# required for the recovery path to honor it.


@pytest.mark.asyncio
async def test_orphan_recovery_honors_new_write_type_from_registry(svc_db, mocker):
    """A task type that is NOT recognized today becomes recovery-eligible the
    moment it is added to WRITE_TASK_TYPES — no edit to services.py required.

    This proves the registry is the single source of truth: we simulate adding a
    brand-new write task type by extending the frozenset the recovery gate reads,
    then assert the GitHub PR lookup is attempted for a task of that type.
    """
    from app import services as _services

    new_type = "fundamental_new_write_type"
    # Sanity: the type is genuinely unknown today, so any positive result is
    # attributable to the registry extension and not a pre-existing literal.
    assert new_type not in _services.WRITE_TASK_TYPES

    mocker.patch.object(
        _services, "WRITE_TASK_TYPES", _services.WRITE_TASK_TYPES | {new_type}
    )

    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type=new_type)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = []
    mock_github_request = AsyncMock(return_value=mock_gh_resp)
    mocker.patch("app.github._github_request", mock_github_request)

    await _recover_orphaned_tasks()

    # The recovery gate let the new type through → it attempted a GitHub lookup.
    mock_get_token.assert_called_once()
    mock_github_request.assert_called_once()


@pytest.mark.asyncio
async def test_orphan_recovery_skips_type_absent_from_registry(svc_db, mocker):
    """A type that is NOT in WRITE_TASK_TYPES (and not added) is skipped — the
    complement of the extensibility test above. Confirms the gate actually
    consults the registry rather than letting everything through."""
    from app import services as _services

    absent_type = "fundamental_new_write_type"
    assert absent_type not in _services.WRITE_TASK_TYPES

    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type=absent_type)

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_recovery_recovers_qa_task(svc_db, mocker):
    """`qa` opens a PR (agent_mode=write) so it MUST be recovery-eligible. The
    pre-consolidation hardcoded tuple omitted `qa`, silently abandoning orphaned
    qa tasks; sourcing the gate from WRITE_TASK_TYPES fixes that."""
    from app import services as _services

    assert "qa" in _services.WRITE_TASK_TYPES

    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="qa")

    mock_get_token = AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = []
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    await _recover_orphaned_tasks()

    mock_get_token.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# _spawn_review_for_recovered_task helper routing
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_phase1_routes_through_spawn_review_helper(svc_db, mocker):
    """Phase 1 (COMPLETED orphan with missing review) invokes _spawn_review_for_recovered_task."""
    project = await _make_project(svc_db)
    await _make_completed_task(svc_db, project.id, task_type="create")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh_token", "owner", "repo"),
    )
    pr_url = "https://github.com/owner/repo/pull/11"
    mock_gh_resp = MagicMock()
    mock_gh_resp.status_code = 200
    mock_gh_resp.json.return_value = [{"html_url": pr_url, "number": 11}]
    mocker.patch("app.github._github_request", AsyncMock(return_value=mock_gh_resp))

    mock_helper = mocker.patch(
        "app.recovery_post_commit_orphans._spawn_review_for_recovered_task",
        new_callable=AsyncMock,
    )

    await _recover_orphaned_tasks()

    mock_helper.assert_called_once()
    call_kwargs = mock_helper.call_args.kwargs
    assert call_kwargs["pr_number"] == 11
    assert "Auto-recovered orphaned PR" in call_kwargs["alert_title"]


@pytest.mark.asyncio
async def test_phase2_routes_through_spawn_review_helper(svc_db, mocker):
    """Phase 2 (reaped task with PR artifact) invokes _spawn_review_for_recovered_task."""
    project = await _make_project(svc_db)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do create"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        attempt_count=1,
        created_at=_naive_utc_ago(minutes=30),
    )
    svc_db.add(task)
    await svc_db.commit()
    await svc_db.refresh(task)

    pr_url = "https://github.com/owner/repo/pull/22"
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url=pr_url,
    )
    svc_db.add(artifact)
    await svc_db.commit()

    mock_helper = mocker.patch(
        "app.recovery_post_commit_orphans._spawn_review_for_recovered_task",
        new_callable=AsyncMock,
    )

    await _recover_orphaned_tasks()

    mock_helper.assert_called_once()
    call_kwargs = mock_helper.call_args.kwargs
    assert call_kwargs["pr_number"] == 22
    assert "reaped" in call_kwargs["alert_title"]


# ══════════════════════════════════════════════════════════════════════════════
# _persist_circuit_breakers
# ══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def deploy_db(mocker):
    """Isolated in-memory SQLite DB for _persist_circuit_breakers / _persist_zone_health tests."""
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
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    # Seed the singleton DeploymentState row
    async with _Session() as session:
        state = DeploymentState(id=1, current_revision="r1", current_sha="abc123")
        session.add(state)
        await session.commit()

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.mark.asyncio
async def test_persist_circuit_breakers_saves_open_breaker(deploy_db, mocker):
    """An OPEN circuit breaker (failure_count > 0) must be persisted to DeploymentState."""
    _session, _Session = deploy_db
    opened_at = _naive_utcnow()

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["proj-uuid-1"] = {
        "state": "OPEN",
        "failure_count": 5,
        "success_count": 0,
        "opened_at": opened_at,
        "cooldown_seconds": 60,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    mock_get_state = AsyncMock()
    mock_state = MagicMock()
    mock_state.circuit_breaker_states = None
    mock_get_state.return_value = mock_state
    mocker.patch("app.deploy_health._get_deploy_state_for_update", mock_get_state)

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)

    mock_get_state.assert_called_once()
    persisted = mock_state.circuit_breaker_states
    assert "proj-uuid-1" in persisted
    entry = persisted["proj-uuid-1"]
    assert entry["state"] == "OPEN"
    assert entry["failure_count"] == 5
    # opened_at must be serialized to ISO string
    assert entry["opened_at"] == opened_at.isoformat()


@pytest.mark.asyncio
async def test_persist_circuit_breakers_prunes_closed_with_no_failures(
    deploy_db, mocker
):
    """CLOSED breakers with failure_count=0 are default state and must NOT be persisted."""
    _session, _Session = deploy_db

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["proj-clean"] = {
        "state": "CLOSED",
        "failure_count": 0,
        "success_count": 0,
        "opened_at": None,
        "cooldown_seconds": 30,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    mock_state = MagicMock()
    mock_state.circuit_breaker_states = None
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)

    persisted = mock_state.circuit_breaker_states
    assert "proj-clean" not in persisted


@pytest.mark.asyncio
async def test_persist_circuit_breakers_handles_missing_deploy_state(deploy_db, mocker):
    """If _get_deploy_state_for_update returns None, the function must not crash."""
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None)
    )

    saved_breakers = _shared_state.circuit_breakers.copy()
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers["some-proj"] = {
        "state": "OPEN",
        "failure_count": 1,
        "success_count": 0,
        "opened_at": _naive_utcnow(),
        "cooldown_seconds": 30,
        "base_cooldown": 30,
        "half_open_task_id": None,
    }

    try:
        await _persist_circuit_breakers()
    finally:
        _shared_state.circuit_breakers.clear()
        _shared_state.circuit_breakers.update(saved_breakers)
    # No exception == pass


# ══════════════════════════════════════════════════════════════════════════════
# _persist_zone_health
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_persist_zone_health_serializes_datetimes(deploy_db, mocker):
    """Zone health datetime fields (e.g. cooldown_expires_at) must be ISO-stringified."""
    _session, _Session = deploy_db

    expires_at = _naive_utcnow() + timedelta(minutes=30)
    fake_zone_health = {
        "us-central1-f": {
            "consecutive_failures": 3,
            "cooldown_expires_at": expires_at,
        }
    }

    mocker.patch("app.compute._zone_health", fake_zone_health)
    # Provide a real non-async lock
    import threading

    mocker.patch("app.compute._zone_lock", threading.Lock())

    mocker.patch("app.deploy_health._pause_failure_streak", 2)
    from collections import deque as _deque

    mocker.patch("app.deploy_health._health_recovery_history", _deque([True], maxlen=5))

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _persist_zone_health()

    snapshot = mock_state.zone_health_state
    assert "us-central1-f" in snapshot
    zone = snapshot["us-central1-f"]
    assert zone["consecutive_failures"] == 3
    # datetime must have been converted to ISO string
    assert isinstance(zone["cooldown_expires_at"], str)
    assert zone["cooldown_expires_at"] == expires_at.isoformat()


@pytest.mark.asyncio
async def test_persist_zone_health_includes_streak_counters(deploy_db, mocker):
    """Snapshot must include _pause_failure_streak and _health_recovery_history."""
    mocker.patch("app.compute._zone_health", {})
    import threading

    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 4)
    from collections import deque as _deque

    mocker.patch(
        "app.deploy_health._health_recovery_history",
        _deque([True, False, True], maxlen=5),
    )
    from datetime import datetime as _dt

    resume_at = _dt(2026, 4, 17, 12, 0, 0)
    mocker.patch("app.deploy_health._last_auto_resume_at", resume_at)

    mock_state = MagicMock()
    mock_state.zone_health_state = None
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update",
        AsyncMock(return_value=mock_state),
    )

    await _persist_zone_health()

    snapshot = mock_state.zone_health_state
    assert snapshot["_pause_failure_streak"] == 4
    assert snapshot["_health_recovery_history"] == [True, False, True]
    assert snapshot["_last_auto_resume_at"] == resume_at.isoformat()


@pytest.mark.asyncio
async def test_persist_zone_health_handles_missing_deploy_state(mocker):
    """If _get_deploy_state_for_update returns None, the function must not crash."""
    mocker.patch("app.compute._zone_health", {})
    import threading

    mocker.patch("app.compute._zone_lock", threading.Lock())
    mocker.patch("app.deploy_health._pause_failure_streak", 0)
    from collections import deque as _deque

    mocker.patch("app.deploy_health._health_recovery_history", _deque(maxlen=5))
    mocker.patch(
        "app.deploy_health._get_deploy_state_for_update", AsyncMock(return_value=None)
    )
    mocker.patch("app.services.AsyncSessionLocal", MagicMock())

    await _persist_zone_health()
    # No exception == pass


# ══════════════════════════════════════════════════════════════════════════════
# gcp_cleanup_service
# ══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def gcp_db(mocker):
    """Isolated in-memory SQLite DB for gcp_cleanup_service tests."""
    import app.vm_lifecycle as _vm_lifecycle

    previous_vm_count = _vm_lifecycle._last_gcp_vm_count
    previous_vm_check = _vm_lifecycle._last_gcp_vm_check
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
    mocker.patch("app.services.AsyncSessionLocal", _Session)

    try:
        async with _Session() as session:
            yield session
    finally:
        # gcp_cleanup_service intentionally refreshes these process-wide
        # counters.  Keep that contract observable within each test without
        # leaking a mocked cleanup result into duration-reordered tests.
        _vm_lifecycle._last_gcp_vm_count = previous_vm_count
        _vm_lifecycle._last_gcp_vm_check = previous_vm_check
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await _engine.dispose()


async def _make_task_with_vm(session, project_id, status, vm_instance_id=None) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=status,
        created_by="ORCHESTRATOR",
        vm_instance_id=vm_instance_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_gcp_cleanup_passes_active_vm_ids_to_cleanup(gcp_db, mocker):
    """Active (PROVISIONING/IN_PROGRESS) VM IDs should be passed to cleanup_orphaned_vms.
    Completed/failed task VMs should NOT be included."""
    project = await _make_project(gcp_db)

    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.PROVISIONING, "agent-vm-prov-1"
    )
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-ip-1"
    )
    # These should NOT be included
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.COMPLETED, "agent-vm-done-1"
    )
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.FAILED, "agent-vm-fail-1")
    # Task with no vm_instance_id
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, None)

    mock_cleanup = AsyncMock(
        return_value=MagicMock(
            deleted=[], failed=[], total_gcp_vms=2, agent_vm_count=2
        )
    )
    mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)

    # Exit after first cleanup iteration by raising CancelledError on sleep
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    await gcp_cleanup_service()

    mock_cleanup.assert_called_once()
    called_vm_ids = mock_cleanup.call_args[0][0]
    assert "agent-vm-prov-1" in called_vm_ids
    assert "agent-vm-ip-1" in called_vm_ids
    assert "agent-vm-done-1" not in called_vm_ids
    assert "agent-vm-fail-1" not in called_vm_ids
    assert None not in called_vm_ids


@pytest.mark.asyncio
async def test_gcp_cleanup_spares_live_warm_handoff_target_vm(gcp_db, mocker):
    """A QUEUED warm-handoff successor should keep its source VM active until
    the deadline, so the generic orphan sweep must not pass that VM to cleanup."""
    project = await _make_project(gcp_db)
    live_deadline = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)).isoformat()
    handoff_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "warm_target_vm_id": "agent-vm-warm-1",
            "warm_handoff_deadline": live_deadline,
        },
    )
    gcp_db.add(handoff_task)
    await gcp_db.commit()

    mock_cleanup = AsyncMock(
        return_value=MagicMock(
            deleted=[], failed=[], total_gcp_vms=1, agent_vm_count=1
        )
    )
    mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    await gcp_cleanup_service()

    mock_cleanup.assert_called_once()
    called_vm_ids = mock_cleanup.call_args[0][0]
    assert "agent-vm-warm-1" in called_vm_ids


@pytest.mark.asyncio
async def test_gcp_cleanup_reclaims_expired_warm_handoff_target_vm(gcp_db, mocker):
    """Once a warm-handoff deadline expires, the source VM should be reclaimable
    again if no task has claimed it."""
    project = await _make_project(gcp_db)
    expired_deadline = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)).isoformat()
    handoff_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "warm_target_vm_id": "agent-vm-expired-1",
            "warm_handoff_deadline": expired_deadline,
        },
    )
    gcp_db.add(handoff_task)
    await gcp_db.commit()

    mock_cleanup = AsyncMock(
        return_value=MagicMock(
            deleted=[], failed=[], total_gcp_vms=1, agent_vm_count=1
        )
    )
    mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    await gcp_cleanup_service()

    mock_cleanup.assert_called_once()
    called_vm_ids = mock_cleanup.call_args[0][0]
    assert "agent-vm-expired-1" not in called_vm_ids


@pytest.mark.asyncio
async def test_gcp_cleanup_does_not_exempt_pool_vm_when_pool_disabled(gcp_db, mocker):
    """Pool members stay reclaimable when warm_pool_enabled is off."""
    project = await _make_project(gcp_db)
    gcp_db.add(
        PoolVm(
            project_id=project.id,
            vm_instance_id="agent-vm-pool-disabled",
            state=PoolVmState.IDLE,
            account_id="acct-1",
            provider="anthropic",
        )
    )
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-active-1"
    )
    await gcp_db.commit()

    prev = _shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        _shared_state.runtime_config["warm_pool_enabled"] = False
        mock_cleanup = AsyncMock(
            return_value=MagicMock(
                deleted=[], failed=[], total_gcp_vms=2, agent_vm_count=2
            )
        )
        mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)
        mocker.patch(
            "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
        )

        await gcp_cleanup_service()

        mock_cleanup.assert_called_once()
        called_vm_ids = mock_cleanup.call_args[0][0]
        assert "agent-vm-active-1" in called_vm_ids
        assert "agent-vm-pool-disabled" not in called_vm_ids
    finally:
        _shared_state.runtime_config["warm_pool_enabled"] = prev


@pytest.mark.asyncio
async def test_gcp_cleanup_handles_api_failure_gracefully(gcp_db, mocker):
    """If cleanup_orphaned_vms raises an exception, gcp_cleanup_service should log and continue."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mocker.patch(
        "app.compute.cleanup_orphaned_vms", side_effect=RuntimeError("GCP API down")
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise
    await gcp_cleanup_service()


@pytest.mark.asyncio
async def test_gcp_cleanup_handles_timeout_gracefully(gcp_db, mocker):
    """If cleanup_orphaned_vms times out, the service should log and continue without crashing."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    async def _slow_cleanup(_ids):
        raise asyncio.TimeoutError()

    mocker.patch("app.compute.cleanup_orphaned_vms", side_effect=_slow_cleanup)
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise
    await gcp_cleanup_service()


@pytest.mark.asyncio
async def test_gcp_cleanup_updates_vm_count_cache(gcp_db, mocker):
    """When cleanup returns agent_vm_count, the global VM count cache should be updated."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mock_result = MagicMock()
    mock_result.deleted = ["agent-vm-orphan"]
    mock_result.failed = []
    mock_result.agent_vm_count = 5
    mocker.patch(
        "app.compute.cleanup_orphaned_vms", AsyncMock(return_value=mock_result)
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    import app.vm_lifecycle as _vm_lifecycle

    await gcp_cleanup_service()

    assert _vm_lifecycle._last_gcp_vm_count == 5
    assert hasattr(_vm_lifecycle, "_last_gcp_vm_check")


@pytest.mark.asyncio
async def test_gcp_cleanup_caches_post_delete_agent_vm_count(gcp_db, mocker):
    """After orphan deletion, _last_gcp_vm_count must reflect the remaining inventory
    (pre-deletion count minus deleted agent VMs), not the pre-deletion total."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    # GCP inventory: 2 agent VMs — 1 active (agent-vm-1) + 1 orphan (agent-vm-orphan)
    mocker.patch("app.compute.LOCAL_MODE", False)
    mocker.patch("app.compute.FALLBACK_ZONES", [("us-central1-a", "subnet-a")])
    mocker.patch("app.compute.GPU_FALLBACK_ZONES", {})
    mocker.patch(
        "app.compute._list_vms_in_zone",
        return_value=[
            ("agent-vm-1", "us-central1-a", "RUNNING"),
            ("agent-vm-orphan", "us-central1-a", "RUNNING"),
        ],
    )
    mocker.patch("app.compute._delete_single_vm", return_value=True)
    mocker.patch("app.compute._delete_agent_secret", return_value=None)
    mocker.patch(
        "app.compute._refresh_gcp_cleanup_active_vm_ids",
        AsyncMock(return_value={"agent-vm-1"}),
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    import app.vm_lifecycle as _vm_lifecycle

    await gcp_cleanup_service()

    # Pre-deletion: 2 agent VMs. Deleted: 1 orphan. Expected cache: 1 (not 2).
    assert _vm_lifecycle._last_gcp_vm_count == 1


@pytest.mark.asyncio
async def test_gcp_cleanup_logs_partial_failures(gcp_db, mocker):
    """If cleanup_result.failed is non-empty, the service should still run without crashing."""
    project = await _make_project(gcp_db)
    await _make_task_with_vm(gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-1")

    mock_result = MagicMock()
    mock_result.deleted = []
    mock_result.failed = ["agent-vm-bad-1", "agent-vm-bad-2"]
    mock_result.total_gcp_vms = 3
    mock_result.agent_vm_count = 3
    mocker.patch(
        "app.compute.cleanup_orphaned_vms", AsyncMock(return_value=mock_result)
    )
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    # Should not raise despite partial failures
    await gcp_cleanup_service()


@pytest.mark.asyncio
async def test_gcp_cleanup_does_not_reap_parked_task_vm(gcp_db, mocker):
    """A QUEUED task holding a VM for park-and-retarget must NOT appear in the
    orphan-deletion set — gcp_cleanup_service should exempt it from cleanup."""
    project = await _make_project(gcp_db)

    # Parked task: QUEUED + parked_for_retarget context + live VM
    parked_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id="agent-vm-parked-1",
        context={
            "parked_for_retarget": True,
            "park_deadline": (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)).isoformat(),
        },
    )
    gcp_db.add(parked_task)

    # Normal QUEUED task (no vm, no park context) — should not be exempted
    plain_queued = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id=None,
    )
    gcp_db.add(plain_queued)

    # Active IN_PROGRESS task — must always be exempted
    await _make_task_with_vm(
        gcp_db, project.id, TaskStatus.IN_PROGRESS, "agent-vm-active-1"
    )

    await gcp_db.commit()

    mock_cleanup = AsyncMock(
        return_value=MagicMock(
            deleted=[], failed=[], total_gcp_vms=2, agent_vm_count=2
        )
    )
    mocker.patch("app.compute.cleanup_orphaned_vms", mock_cleanup)
    mocker.patch(
        "app.services.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    await gcp_cleanup_service()

    mock_cleanup.assert_called_once()
    called_vm_ids = mock_cleanup.call_args[0][0]
    assert "agent-vm-parked-1" in called_vm_ids, (
        "Parked VM must be exempted from orphan cleanup"
    )
    assert "agent-vm-active-1" in called_vm_ids
    assert None not in called_vm_ids


# ══════════════════════════════════════════════════════════════════════════════
# _check_daily_cost_ceiling (agent_status.py)
# ══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture()
async def cost_db(mocker):
    """Isolated in-memory SQLite DB for _check_daily_cost_ceiling tests."""
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

    # Suppress broadcasts (patched at cost_ceiling where the call actually happens)
    mocker.patch("app.cost_ceiling.broadcast_to_project", new_callable=AsyncMock)
    # Suppress external notification dispatch (patching where defined so ensure_future gets a real coroutine)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


def _make_cost_task(
    project_id, vm_secs=3600, is_spot=False, stopped_today=True
) -> Task:
    """Build a completed Task with cost data (not yet committed)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stop_at = now if stopped_today else today_start - timedelta(seconds=1)
    prov_at = stop_at - timedelta(seconds=vm_secs)
    return Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=is_spot,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_failure_breakdown_classifies_projected_failed_row(
    cost_db, mocker
):
    """Regression: the BUDGET_EXCEEDED cost_by_failure_class aggregation loads
    sibling cost rows via load_only(..., raiseload=True) (app/cost_ceiling.py)
    and, for FAILED rows, reads ``t.last_heartbeat or t.started_at`` bare plus
    classify_failure → _stuck_llm_call_no_progress reads t.api_call_summary.
    When the projection omitted those columns, the raiseload InvalidRequestError
    was swallowed by the block's broad except and the ENTIRE BUDGET_EXCEEDED
    notification was silently dropped (the dispatch never ran) whenever any
    FAILED task was among the day's rows."""
    project = await _make_project(cost_db)

    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)

    failed = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    failed.status = TaskStatus.FAILED
    failed.latest_status_message = (
        "Fatal error: Claude Code exceeded 45-minute time limit"
    )
    failed.api_call_summary = {"llm_call_count": 1, "total_output_tokens": 0}
    failed.last_heartbeat = datetime.now(timezone.utc).replace(tzinfo=None)
    cost_db.add(failed)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Detach the FAILED sibling so the cost_rows query re-loads it through the
    # production load_only projection — an identity-map hit would bypass
    # raiseload and mask the regression.
    cost_db.expunge(failed)

    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)
    # Let the fire-and-forget ensure_future() run to completion.
    await asyncio.sleep(0)

    assert dispatch_mock.call_count == 1, (
        "BUDGET_EXCEEDED dispatch never happened — the cost aggregation likely "
        "died reading unprojected columns off the FAILED sibling row"
    )
    payload = dispatch_mock.call_args.args[2]
    breakdown = payload["cost_by_failure_class"]
    # The stalled-LLM row remains budget-exempt without blacklisting an account.
    assert breakdown.get("no_progress_timeout", 0) > 0, (
        f"expected no_progress_timeout bucket in cost_by_failure_class, got {breakdown!r}"
    )
    assert breakdown.get("COMPLETED", 0) > 0


@pytest.mark.asyncio
async def test_cost_ceiling_no_alert_when_disabled(cost_db, mocker):
    """When daily_cost_ceiling_usd == 0 (disabled), no alert should be created."""
    project = await _make_project(cost_db)

    task = _make_cost_task(project.id, vm_secs=36000)  # 10 hours, very expensive
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0}):
        await _check_daily_cost_ceiling(task, cost_db)

    alert_count = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alert_count) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_no_alert_when_under_ceiling(cost_db, mocker):
    """When cost < ceiling, no alert should be created."""
    project = await _make_project(cost_db)

    # 1 hour on-demand: cost = 3600 * ON_DEMAND_RATE ≈ $0.067
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Set ceiling above actual cost ($1 >> $0.067)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 1.0}):
        await _check_daily_cost_ceiling(task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_alert_created_when_over_ceiling(cost_db, mocker):
    """When total daily cost > ceiling, a WARNING alert should be created."""
    project = await _make_project(cost_db)

    # 1 hour on-demand: cost ≈ $0.067
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Set ceiling below actual cost ($0.01 < $0.067)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    # Function adds alert with session.add() but doesn't commit; flush to make it visible.
    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].title == "Daily cost ceiling exceeded"
    # task_id must be NULL (project-level alert, not task-specific)
    assert alerts[0].task_id is None


@pytest.mark.asyncio
async def test_cost_ceiling_deduplicates_alert(cost_db, mocker):
    """If an undismissed cost ceiling alert already exists, no second alert should be created."""
    project = await _make_project(cost_db)

    # Pre-existing undismissed alert
    existing_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Daily cost ceiling exceeded",
        message="Pre-existing alert.",
    )
    cost_db.add(existing_alert)
    await cost_db.commit()

    # Task that pushes cost over ceiling
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    # Still only the one pre-existing alert, no duplicate
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_cost_ceiling_excludes_other_day_tasks(cost_db, mocker):
    """Tasks whose vm_stopped_at is before today should not count toward the daily total."""
    project = await _make_project(cost_db)

    # Yesterday's task — very expensive but outside today's window
    yesterday_task = _make_cost_task(project.id, vm_secs=86400, stopped_today=False)
    cost_db.add(yesterday_task)
    await cost_db.commit()

    # Today's task — cheap (10 min on-demand)
    today_task = _make_cost_task(project.id, vm_secs=600, is_spot=False)
    cost_db.add(today_task)
    await cost_db.commit()
    await cost_db.refresh(today_task)

    # Ceiling is $0.05 — above today-only cost ($0.011) but below yesterday+today ($1.67)
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        await _check_daily_cost_ceiling(today_task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_spot_rate_used_for_spot_vms(cost_db, mocker):
    """Spot VMs should be costed at SPOT_RATE (cheaper than ON_DEMAND_RATE)."""
    project = await _make_project(cost_db)

    # 1 hour spot: cost = 3600 * SPOT_RATE ≈ $0.020
    spot_task = _make_cost_task(project.id, vm_secs=3600, is_spot=True)
    cost_db.add(spot_task)
    await cost_db.commit()
    await cost_db.refresh(spot_task)

    # Ceiling between spot and on-demand cost ($0.04 > spot $0.020, < on-demand $0.067)
    # If on-demand rate were used, alert would fire. It should NOT fire here.
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.04}):
        await _check_daily_cost_ceiling(spot_task, cost_db)

    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_includes_current_task_cost(cost_db, mocker):
    """The in-memory current task's own cost must be included even if not yet in DB."""
    project = await _make_project(cost_db)

    # Create a task but do NOT commit it to DB — simulates in-memory state
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # In-memory cost data only (not yet in DB)
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )
    cost_db.add(task)
    # Flush to get an ID but keep out of the query (the function excludes Task.id != task.id)
    await cost_db.flush()

    # Ceiling below 1-hour on-demand cost
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    # Function adds alert with session.add() but doesn't commit; flush to make it visible.
    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "Daily cost ceiling exceeded" in alerts[0].title


@pytest.mark.asyncio
async def test_cost_ceiling_budget_exceeded_webhook_enriched(cost_db, mocker):
    """BUDGET_EXCEEDED webhook payload includes direction_text and cost_by_failure_class."""
    project = await _make_project(cost_db)

    dispatch_mock = AsyncMock()
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        dispatch_mock,
    )

    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    task.context = {
        "direction_text": "Improve pipeline throughput",
        "direction_id": "d-x",
    }
    cost_db.add(task)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.01}):
        await _check_daily_cost_ceiling(task, cost_db)

    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch_mock.call_count >= 1
    args, _kwargs = dispatch_mock.call_args
    assert args[1] == "BUDGET_EXCEEDED"
    payload = args[2]
    assert payload["direction_text"] == "Improve pipeline throughput"
    assert payload["ceiling_usd"] == 0.01
    assert "daily_total_usd" in payload
    assert isinstance(payload["cost_by_failure_class"], dict)
    # Pin the bucket total: 1h on-demand ≈ $0.067. If the DB query included the
    # current task (identity map double-count), this bucket would be ~$0.134.
    assert "COMPLETED" in payload["cost_by_failure_class"]
    completed_cost = payload["cost_by_failure_class"]["COMPLETED"]
    assert 0.06 < completed_cost < 0.08, (
        f"expected ~$0.067 single-count, got {completed_cost} (possible double-count)"
    )
    # Internal consistency: sum of buckets should equal daily_total_usd.
    assert (
        abs(sum(payload["cost_by_failure_class"].values()) - payload["daily_total_usd"])
        < 0.01
    )


def _make_terminal_compute_job(
    project_id, final_cost_usd, *, stopped_today=True
) -> ComputeJob:
    """Build a terminal, costed compute job completed within today (default)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    completed = now if stopped_today else today_start - timedelta(seconds=1)
    return ComputeJob(
        project_id=project_id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        final_cost_usd=final_cost_usd,
        accrued_cost_usd=final_cost_usd,
        completed_at=completed,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_alert_when_compute_spend_pushes_over(cost_db, mocker):
    """Under ceiling on VM alone, but terminal compute-job spend crosses it."""
    project = await _make_project(cost_db)

    # 1 hour on-demand VM ≈ $0.067 — under a $0.50 ceiling on its own.
    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    # Terminal compute-job spend today: $0.60 pushes total over $0.50.
    cost_db.add(_make_terminal_compute_job(project.id, 0.60))
    # A still-running job with zero accrued cost must not affect the total.
    running = ComputeJob(
        project_id=project.id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=0.0,
        final_cost_usd=None,
        completed_at=None,
    )
    cost_db.add(running)
    await cost_db.commit()
    await cost_db.refresh(task)

    dispatch_mock = AsyncMock()
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        dispatch_mock,
    )

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.50}):
        await _check_daily_cost_ceiling(task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].title == "Daily cost ceiling exceeded"

    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch_mock.call_count >= 1
    payload = dispatch_mock.call_args[0][2]
    # Additive split keys present; total = VM + settled compute + inflight + pool idle.
    assert payload["daily_compute_cost_usd"] == pytest.approx(0.60, abs=0.01)
    assert 0.06 < payload["daily_vm_cost_usd"] < 0.08
    assert "daily_inflight_compute_cost_usd" in payload
    assert "daily_pool_idle_cost_usd" in payload
    assert payload["daily_total_usd"] == pytest.approx(
        payload["daily_vm_cost_usd"]
        + payload["daily_compute_cost_usd"]
        + payload["daily_inflight_compute_cost_usd"]
        + payload["daily_pool_idle_cost_usd"],
        abs=0.01,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_disabled_ignores_compute_spend(cost_db, mocker):
    """A zero ceiling disables the guard even with terminal compute spend present."""
    project = await _make_project(cost_db)

    task = _make_cost_task(project.id, vm_secs=3600, is_spot=False)
    cost_db.add(task)
    cost_db.add(_make_terminal_compute_job(project.id, 100.0))
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0}):
        await _check_daily_cost_ceiling(task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_cost_ceiling_alert_when_pool_idle_pushes_over(cost_db, mocker):
    """Pool idle cost alone is enough to breach the ceiling if VM and compute are under."""
    from app.vm_cost_rates import idle_cost_usd

    project = await _make_project(cost_db)

    dispatch_mock = AsyncMock()
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        dispatch_mock,
    )

    # VM cost only: 5 minutes on-demand ≈ $0.006 — under a $0.05 ceiling.
    task = _make_cost_task(project.id, vm_secs=300, is_spot=False)
    cost_db.add(task)

    # Fix the clock to noon of the current UTC day so the test is not sensitive
    # to time-of-day. Running near midnight would clamp idle_since to today_start
    # and shrink idle_seconds to near-zero, making expected_idle < $0.05 ceiling.
    real_now = datetime.now(timezone.utc).replace(tzinfo=None)
    fixed_noon = real_now.replace(hour=12, minute=0, second=0, microsecond=0)
    mocker.patch("app.efficiency_snapshot._utcnow", return_value=fixed_noon)

    # Pool VM that has been IDLE for 1 hour before our fixed noon.
    idle_seconds = 3600.0
    idle_since = fixed_noon - timedelta(seconds=idle_seconds)

    pool_vm = PoolVm(
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=idle_since,
        vm_instance_id="pool-vm-test-idle",
        machine_type="e2-standard-2",
    )
    cost_db.add(pool_vm)
    await cost_db.commit()
    await cost_db.refresh(task)

    # idle_cost_usd for 1h ≈ $0.067 must exceed our chosen ceiling.
    expected_idle = idle_cost_usd(idle_seconds)
    assert expected_idle > 0.05, f"Expected pool idle cost > $0.05, got {expected_idle}"

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        await _check_daily_cost_ceiling(task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].title == "Daily cost ceiling exceeded"

    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch_mock.call_count >= 1
    payload = dispatch_mock.call_args[0][2]
    assert payload["daily_pool_idle_cost_usd"] > 0.0
    assert payload["daily_total_usd"] == pytest.approx(
        payload["daily_vm_cost_usd"]
        + payload["daily_compute_cost_usd"]
        + payload["daily_inflight_compute_cost_usd"]
        + payload["daily_pool_idle_cost_usd"],
        abs=0.01,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_inflight_compute_job_counted(cost_db, mocker):
    """An active (non-terminal) compute job's accrued cost is included in the daily total."""
    project = await _make_project(cost_db)

    dispatch_mock = AsyncMock()
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        dispatch_mock,
    )

    # VM cost only: 5 minutes on-demand ≈ $0.006 — under a $0.50 ceiling.
    task = _make_cost_task(project.id, vm_secs=300, is_spot=False)
    cost_db.add(task)

    # In-flight RUNNING compute job with substantial accrued cost — no final_cost_usd yet.
    running_job = ComputeJob(
        project_id=project.id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=0.60,
        final_cost_usd=None,
        completed_at=None,
    )
    cost_db.add(running_job)
    await cost_db.commit()
    await cost_db.refresh(task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.50}):
        await _check_daily_cost_ceiling(task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].title == "Daily cost ceiling exceeded"

    for _ in range(5):
        await asyncio.sleep(0)
    assert dispatch_mock.call_count >= 1
    payload = dispatch_mock.call_args[0][2]
    assert payload["daily_inflight_compute_cost_usd"] == pytest.approx(0.60, abs=0.01)
    assert payload["daily_compute_cost_usd"] == pytest.approx(0.0, abs=0.01)
    assert payload["daily_total_usd"] == pytest.approx(
        payload["daily_vm_cost_usd"]
        + payload["daily_compute_cost_usd"]
        + payload["daily_inflight_compute_cost_usd"]
        + payload["daily_pool_idle_cost_usd"],
        abs=0.01,
    )


@pytest.mark.asyncio
async def test_cost_ceiling_pool_idle_uses_windowed_not_alltime(cost_db, mocker):
    """Pool idle cost check uses today's window only, not all-time accumulated cost.

    Seeds a PoolVm with large historical idle_cost_banked (simulating weeks of
    accumulated cost) but no current idle activity (state=LEASED, no idle_since).
    The daily ceiling is above today's VM spend but well below the historical banked cost.

    With the (buggy) all-time compute_pool_idle_cost():
      total_idle_cost_usd includes idle_cost_banked → huge total → alert fires (wrong).
    With the correct _compute_windowed_idle_cost():
      windowed total excludes pre-today banked cost → no alert (correct).
    """
    project = await _make_project(cost_db)

    # VM task: 5 minutes on-demand ≈ $0.006, well under a $1.00 ceiling.
    task = _make_cost_task(project.id, vm_secs=300, is_spot=False)
    cost_db.add(task)

    # Pool VM with large historical banked cost but NOT currently idle (LEASED).
    # idle_cost_banked=$50 represents weeks of past idle accumulation — should
    # NOT count toward today's daily ceiling check.
    pool_vm = PoolVm(
        project_id=project.id,
        state=PoolVmState.LEASED,
        idle_since=None,
        idle_cost_banked=50.0,  # large all-time historical banked cost
        vm_instance_id="pool-vm-historical-banked",
        machine_type="e2-standard-2",
    )
    cost_db.add(pool_vm)
    await cost_db.commit()
    await cost_db.refresh(task)

    # Ceiling of $1.00: above today's VM cost (~$0.006) but below historical banked cost ($50).
    # Correct windowed behavior: no alert (today's pool idle = $0, total ≈ $0.006 < $1.00).
    # Buggy all-time behavior: alert fires ($50 banked makes total >> $1.00).
    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 1.0}):
        await _check_daily_cost_ceiling(task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0, (
        "No alert should fire: today's pool idle cost is $0 (VM is LEASED, not IDLE). "
        "If this fails, the ceiling check is using all-time banked cost instead of windowed cost."
    )


@pytest.mark.asyncio
async def test_cost_ceiling_includes_running_vm_cost(cost_db, mocker):
    """An in-flight VM (vm_stopped_at=None) on a larger machine type is estimated to
    now and included in the daily total. Proves both live-VM inclusion and
    machine-type-correct pricing: e2-standard-8 2-hour on-demand cost (~$0.536) crosses
    a $0.20 ceiling that e2-standard-2 2-hour pricing (~$0.134) would miss."""
    project = await _make_project(cost_db)

    # Background in-flight task: e2-standard-8, running for 2+ hours, still running.
    # e2-standard-8 on-demand = $0.268/hr → ~2h ≈ $0.536 >> $0.20 ceiling.
    # e2-standard-2 on-demand = $0.067/hr → ~2h ≈ $0.134 < $0.20 (old code would miss it).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    running_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=None,
        vm_spot=False,
        vm_machine_type="e2-standard-8",
    )
    cost_db.add(running_task)
    await cost_db.commit()

    # Current task (the trigger): minimal cost on its own, just finished.
    current_task = _make_cost_task(project.id, vm_secs=60, is_spot=False)
    cost_db.add(current_task)
    await cost_db.commit()
    await cost_db.refresh(current_task)

    with patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.20}):
        await _check_daily_cost_ceiling(current_task, cost_db)

    await cost_db.flush()
    alerts = (
        (await cost_db.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "Expected alert: e2-standard-8 2-hour in-flight cost should exceed $0.20 ceiling"
    )
    assert alerts[0].title == "Daily cost ceiling exceeded"


# ══════════════════════════════════════════════════════════════════════════════
# _run_auto_archive_cycle — age cap + per-project count cap
# ══════════════════════════════════════════════════════════════════════════════


async def _make_terminal_task(
    session, project_id, status=TaskStatus.COMPLETED, days_ago=0
) -> Task:
    """Create a terminal task with updated_at set days_ago days in the past."""
    when = _naive_utc_ago(days=days_ago)
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create"}],
        status=status,
        created_by="ORCHESTRATOR",
        created_at=when,
        updated_at=when,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_auto_archive_age_pass_archives_old_terminal(svc_db):
    """Terminal tasks older than auto_archive_terminal_after_days get archived."""
    project = await _make_project(svc_db)
    old = await _make_terminal_task(svc_db, project.id, days_ago=30)
    fresh = await _make_terminal_task(svc_db, project.id, days_ago=1)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 7,
            "auto_archive_max_terminal_tasks_per_project": 0,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 1
    assert result["count"] == 0
    await svc_db.refresh(old)
    await svc_db.refresh(fresh)
    assert old.archived is True
    assert fresh.archived is False


@pytest.mark.asyncio
async def test_auto_archive_count_pass_archives_beyond_cap(svc_db):
    """Per-project cap keeps the N most-recent terminal tasks; rest archive."""
    project = await _make_project(svc_db)
    # days_ago=0 is newest; days_ago=4 is oldest
    tasks = [
        await _make_terminal_task(svc_db, project.id, days_ago=i) for i in range(5)
    ]

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 3,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 0
    assert result["count"] == 2
    for t in tasks:
        await svc_db.refresh(t)
    # 3 newest (index 0,1,2 = days_ago 0,1,2) stay; 2 oldest (3,4) archived.
    # Don't re-derive age from updated_at — the UPDATE's onupdate=_utcnow hook
    # resets updated_at on archived rows, so we check by creation order.
    assert [t.archived for t in tasks] == [False, False, False, True, True]


@pytest.mark.asyncio
async def test_auto_archive_count_pass_partitions_by_project(svc_db):
    """Cap is applied per-project independently (projects don't cross-count)."""
    p1 = await _make_project(svc_db)
    p2 = Project(name="P2", user_id="user1")
    svc_db.add(p2)
    await svc_db.commit()
    await svc_db.refresh(p2)

    for i in range(4):
        await _make_terminal_task(svc_db, p1.id, days_ago=i)
    await _make_terminal_task(svc_db, p2.id, days_ago=0)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 2,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["count"] == 2
    p1_archived = (
        (
            await svc_db.execute(
                select(Task).where(Task.project_id == p1.id, Task.archived.is_(True))
            )
        )
        .scalars()
        .all()
    )
    p2_archived = (
        (
            await svc_db.execute(
                select(Task).where(Task.project_id == p2.id, Task.archived.is_(True))
            )
        )
        .scalars()
        .all()
    )
    assert len(p1_archived) == 2
    assert len(p2_archived) == 0


@pytest.mark.asyncio
async def test_auto_archive_count_pass_disabled_when_cap_zero(svc_db):
    """cap=0 disables the count pass entirely."""
    project = await _make_project(svc_db)
    for i in range(10):
        await _make_terminal_task(svc_db, project.id, days_ago=i)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 90,
            "auto_archive_max_terminal_tasks_per_project": 0,
        },
    ):
        result = await _run_auto_archive_cycle()

    assert result["age"] == 0
    assert result["count"] == 0
    archived = (
        (await svc_db.execute(select(Task).where(Task.archived.is_(True))))
        .scalars()
        .all()
    )
    assert len(archived) == 0


@pytest.mark.asyncio
async def test_auto_archive_skips_non_terminal(svc_db):
    """Non-terminal tasks (QUEUED, IN_PROGRESS) are never archived, regardless of age."""
    project = await _make_project(svc_db)
    queued = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_by="USER",
        created_at=_naive_utc_ago(days=60),
        updated_at=_naive_utc_ago(days=60),
    )
    svc_db.add(queued)
    await svc_db.commit()
    await svc_db.refresh(queued)

    with patch.dict(
        _shared_state.runtime_config,
        {
            "auto_archive_terminal_after_days": 7,
            "auto_archive_max_terminal_tasks_per_project": 1,
        },
    ):
        await _run_auto_archive_cycle()

    await svc_db.refresh(queued)
    assert queued.archived is False


# ─────────────────────────────────────────────────────────────────────────────
# cleanup_old_images — image family pruner
# ─────────────────────────────────────────────────────────────────────────────


def _mk_image(name: str, ts: str):
    """Lightweight stand-in for google.cloud.compute_v1.Image — just needs .name/.creation_timestamp."""
    im = MagicMock()
    im.name = name
    im.creation_timestamp = ts
    return im


@pytest.mark.asyncio
async def test_cleanup_old_images_deletes_oldest_beyond_keep(mocker):
    """cleanup_old_images should delete only images past the keep-N boundary, newest-first sort."""
    from app import compute as _compute

    images = [
        _mk_image("img-2026-04-15", "2026-04-15T10:00:00.000-00:00"),
        _mk_image("img-2026-04-10", "2026-04-10T10:00:00.000-00:00"),
        _mk_image("img-2026-04-05", "2026-04-05T10:00:00.000-00:00"),
        _mk_image("img-2026-04-01", "2026-04-01T10:00:00.000-00:00"),
        _mk_image("img-2026-03-25", "2026-03-25T10:00:00.000-00:00"),
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)
    mock_client.delete = MagicMock(return_value=None)
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj"
    )

    assert result["total"] == 5
    assert result["kept"] == 2
    assert result["deleted"] == 3
    assert result["failed"] == []
    deleted_names = sorted(
        [c.kwargs["image"] for c in mock_client.delete.call_args_list]
    )
    assert deleted_names == ["img-2026-03-25", "img-2026-04-01", "img-2026-04-05"]


@pytest.mark.asyncio
async def test_cleanup_old_images_noop_when_at_or_below_keep(mocker):
    """If total images <= keep, no delete calls should fire."""
    from app import compute as _compute

    mock_client = MagicMock()
    mock_client.list.return_value = iter(
        [
            _mk_image("img-1", "2026-04-15T10:00:00.000-00:00"),
            _mk_image("img-2", "2026-04-10T10:00:00.000-00:00"),
        ]
    )
    mock_client.delete = MagicMock()
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=5, family="blueprint-agent", project="my-proj"
    )

    assert result["deleted"] == 0
    assert result["kept"] == 2
    mock_client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_old_images_dry_run_does_not_delete(mocker):
    """dry_run=True must return the planned list but never call delete."""
    from app import compute as _compute

    images = [
        _mk_image(f"img-{i}", f"2026-04-{i:02d}T10:00:00.000-00:00")
        for i in range(1, 6)
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)
    mock_client.delete = MagicMock()
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj", dry_run=True
    )

    assert result["dry_run"] is True
    assert result["deleted"] == 0
    assert len(result["would_delete"]) == 3
    mock_client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_old_images_rejects_keep_less_than_one():
    """keep < 1 must raise — we must retain at least one image for VM provisioning."""
    from app import compute as _compute

    with pytest.raises(ValueError):
        await _compute.cleanup_old_images(
            keep=0, family="blueprint-agent", project="my-proj"
        )


@pytest.mark.asyncio
async def test_cleanup_old_images_collects_per_image_failures(mocker):
    """A delete failure on one image must not abort the sweep; failures are aggregated."""
    from app import compute as _compute

    images = [
        _mk_image(f"img-{i}", f"2026-04-{i:02d}T10:00:00.000-00:00")
        for i in range(1, 6)
    ]
    mock_client = MagicMock()
    mock_client.list.return_value = iter(images)

    def _delete_side_effect(**kwargs):
        if kwargs["image"] == "img-2":  # one of the images we expect to delete
            raise RuntimeError("permissionDenied")
        return None

    mock_client.delete = MagicMock(side_effect=_delete_side_effect)
    mocker.patch("google.cloud.compute_v1.ImagesClient", return_value=mock_client)
    mocker.patch.object(_compute, "LOCAL_MODE", False)

    result = await _compute.cleanup_old_images(
        keep=2, family="blueprint-agent", project="my-proj"
    )

    assert result["deleted"] == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["name"] == "img-2"
    assert "permissionDenied" in result["failed"][0]["error"]


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — admin-cancel must be sticky (regression 2026-05-04)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_skips_cancelled_tasks_and_clears_pending(
    svc_db, mocker
):
    """Regression for incident 2026-05-04: an admin-cancelled plan task
    (b9456e05) was zombied 2 hours later when the post-commit sweep retried
    `_handle_plan_completion`. The handler unconditionally set status back to
    EVALUATING and spawned a child (b403b76a), silently overriding the
    admin's cancel decision.

    The sweep must NOT call `_process_post_commit` on CANCELLED tasks.
    Instead it should clear `post_commit_pending` so the row stops appearing
    in future sweeps."""
    project = await _make_project(svc_db)
    cancelled_task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": "irrelevant",
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add(cancelled_task)
    await svc_db.commit()
    await svc_db.refresh(cancelled_task)

    # If `_process_post_commit` is called, the test fails — it should never run.
    mock_process = AsyncMock(
        side_effect=AssertionError(
            "_process_post_commit must not run on CANCELLED tasks"
        )
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    await svc_db.refresh(cancelled_task)
    assert cancelled_task.status == TaskStatus.CANCELLED, "status must stay CANCELLED"
    assert cancelled_task.post_commit_pending is False, (
        "pending flag must be cleared so the row stops appearing in sweeps"
    )
    assert "_post_commit_pending_meta" not in (cancelled_task.context or {}), (
        "pending meta must be cleared alongside the flag"
    )
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_sweep_still_retries_completed_and_failed(svc_db, mocker):
    """The sweep continues to retry COMPLETED and FAILED tasks — only
    CANCELLED is special-cased."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    completed_id = _uuid.uuid4()
    failed_id = _uuid.uuid4()
    completed_task = Task(
        id=completed_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(completed_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    failed_task = Task(
        id=failed_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(failed_id),
                    "status": "FAILED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add_all([completed_task, failed_task])
    await svc_db.commit()

    mock_process = AsyncMock()  # succeed silently
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    # Both retried.
    assert mock_process.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — childless-EVALUATING recovery (incident 2026-05-27)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_recovers_childless_evaluating_task(svc_db, mocker):
    """Incident 2026-05-27: a plan task (e39bd5a3) flipped COMPLETED→EVALUATING
    and released its lock in _ensure_branch BEFORE spawning children, then its
    post-commit died. It stranded EVALUATING with zero children — the reaper
    Step 3 needs a COMPLETED child and Step 4 skips childless parents, and the
    old sweep filter (COMPLETED/FAILED only) ignored it — so it pinned its
    research grandparent for ~6h.

    The sweep must now retry an EVALUATING task that carries a failure-set
    pending marker AND has zero children (the spawn loop demonstrably never
    ran), re-running the completion handler to create the missing children."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(task_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock()  # succeed silently
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_called_once()
    assert mock_process.call_args.args[0] == task_id


@pytest.mark.asyncio
async def test_post_commit_sweep_skips_childed_evaluating_task(svc_db, mocker):
    """The childless guard is essential: an EVALUATING parent that already
    spawned children is a normally-parked parent whose pending marker is
    stale. Re-running its completion handler would DUPLICATE children (child
    creation is unconditional). The sweep must NOT re-run it — instead it
    clears the stale marker so the row stops reappearing."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    parent_id = _uuid.uuid4()
    parent = Task(
        id=parent_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(parent_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    child = Task(
        project_id=project.id,
        parent_task_id=parent_id,
        payloads=[{"type": "create", "prompt": "do the thing"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    svc_db.add_all([parent, child])
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError(
            "must not re-run a childed EVALUATING parent (would duplicate children)"
        )
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_not_called()
    await svc_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, "parent stays parked"
    assert parent.post_commit_pending is False, "stale marker must be cleared"
    assert "_post_commit_pending_meta" not in (parent.context or {}), (
        "stale pending meta must be cleared alongside the flag"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _recover_childless_evaluating_without_marker — marker-LESS childless recovery
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_markerless_childless_evaluating_recovered(svc_db, mocker):
    """A plan parent stranded childless in EVALUATING with NO pending marker (the
    write-ahead marker was absent/cleared) must still be recovered: it reported
    COMPLETED (plan_output with subtasks is persisted), is EVALUATING with zero
    children, and is past the in-flight-spawn grace → re-run the handler."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,  # NO marker — the gap this guard closes
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        # Past the 600s in-flight-spawn grace so it is provably stranded.
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock()  # succeed silently
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _recover_childless_evaluating_without_marker()

    mock_process.assert_called_once()
    assert mock_process.call_args.args[0] == task_id
    # The synthetic payload re-runs the COMPLETED completion path with the
    # persisted plan_output round-tripped through logs.
    synthetic_payload = mock_process.call_args.args[1]
    assert synthetic_payload.status == TaskStatus.COMPLETED
    assert "subtasks" in (synthetic_payload.logs or "")
    claim_token = mock_process.call_args.kwargs[
        "_expected_childless_recovery_token"
    ]
    assert len(claim_token) == 32


@pytest.mark.asyncio
async def test_markerless_candidate_page_cannot_be_starved(svc_db, mocker):
    """Older ineligible/malformed rows cannot hide or poison an eligible plan."""
    project = await _make_project(svc_db)
    for index in range(50):
        svc_db.add(
            Task(
                id=_uuid.uuid4(),
                project_id=project.id,
                payloads=[{"type": "create", "prompt": f"ineligible {index}"}],
                status=TaskStatus.EVALUATING,
                created_by="ORCHESTRATOR",
                post_commit_pending=False,
                updated_at=_naive_utc_ago(minutes=30),
            )
        )
    svc_db.add(
        Task(
            id=_uuid.uuid4(),
            project_id=project.id,
            payloads=[{"type": "plan", "prompt": "malformed"}],
            status=TaskStatus.EVALUATING,
            created_by="ORCHESTRATOR",
            post_commit_pending=False,
            plan_output={"subtasks": {"not": "an array"}},
            updated_at=_naive_utc_ago(minutes=40),
        )
    )
    eligible = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add(eligible)
    await svc_db.commit()

    mock_process = mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(),
    )
    await _recover_childless_evaluating_without_marker()

    mock_process.assert_awaited_once()
    assert mock_process.await_args.args[0] == eligible.id


@pytest.mark.asyncio
async def test_markerless_childless_skips_childed_parent(svc_db, mocker):
    """ZERO-children guard (load-bearing): a marker-less EVALUATING plan parent
    that ALREADY has a child must NOT be re-run — child creation is atomic, so
    re-running would duplicate children."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    parent_id = _uuid.uuid4()
    parent = Task(
        id=parent_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context={
            "_childless_post_commit_recovery": {
                "attempts": 1,
                "claim_token": "prior-success",
            }
        },
        post_commit_pending=False,
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    child = Task(
        project_id=project.id,
        parent_task_id=parent_id,
        payloads=[{"type": "create", "prompt": "do x"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    svc_db.add_all([parent, child])
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError(
            "must not re-run a childed parent (would duplicate children)"
        )
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _recover_childless_evaluating_without_marker()
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_markerless_childless_skips_within_grace(svc_db, mocker):
    """Age guard: a parent that JUST flipped to EVALUATING (within the in-flight-
    spawn grace) must NOT be re-run — the original spawn may still be running and
    re-running would race it into duplicate children."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(seconds=10),  # fresh — within grace
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError("must not re-run within the in-flight-spawn grace")
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _recover_childless_evaluating_without_marker()
    mock_process.assert_not_called()


@pytest.mark.asyncio
async def test_markerless_childless_records_attempt_and_lock_scope(svc_db, mocker):
    """A failed retry is durably counted and identifies project contention."""
    import uuid as _uuid

    from app.task_lifecycle import PostCommitLocked

    project = await _make_project(svc_db)
    task = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(
            side_effect=PostCommitLocked(
                "project locked",
                lock_scope="project",
                resource_id=project.id,
            )
        ),
    )

    await _recover_childless_evaluating_without_marker()
    # The claim/failure writes refresh updated_at, but retry metadata must make
    # the task eligible again on the very next sweep.
    await _recover_childless_evaluating_without_marker()

    await svc_db.refresh(task)
    meta = (task.context or {})["_childless_post_commit_recovery"]
    assert meta["attempts"] == 2
    assert meta["last_lock_scope"] == "project"
    assert "PostCommitLocked" in meta["last_error"]
    assert "claimed_at" not in meta
    assert mock_process.await_count == 2
    first_token = mock_process.await_args_list[0].kwargs[
        "_expected_childless_recovery_token"
    ]
    second_token = mock_process.await_args_list[1].kwargs[
        "_expected_childless_recovery_token"
    ]
    assert first_token != second_token


@pytest.mark.asyncio
async def test_markerless_failure_record_cannot_dirty_fresh_requeue(svc_db, mocker):
    """A requeue that wins during processing must not regain the old budget."""
    from app import services
    from app.task_blocked import reset_task_for_fresh_attempt

    project = await _make_project(svc_db)
    task = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add(task)
    await svc_db.commit()

    async def _requeue_then_fail(*args, **kwargs):
        async with services.AsyncSessionLocal() as race_session:
            raced = await race_session.get(Task, task.id)
            reset_task_for_fresh_attempt(raced)
            raced.status = TaskStatus.QUEUED
            race_session.add(raced)
            await race_session.commit()
        raise RuntimeError("old recovery failed after requeue")

    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(side_effect=_requeue_then_fail),
    )

    await _recover_childless_evaluating_without_marker()

    await svc_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert "_childless_post_commit_recovery" not in (task.context or {})
    assert "_childless_post_commit_abandoned" not in (task.context or {})


@pytest.mark.asyncio
async def test_markerless_late_failure_cannot_clear_newer_claim(svc_db):
    """A late token-A failure cannot erase the active token-B lease."""
    from app.recovery_post_commit_pending import (
        _record_childless_recovery_failure,
    )

    project = await _make_project(svc_db)
    task = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        context={
            "_childless_post_commit_recovery": {
                "attempts": 2,
                "claim_token": "token-b",
                "claimed_at": _naive_utcnow().isoformat(),
            }
        },
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
    )
    svc_db.add(task)
    await svc_db.commit()

    await _record_childless_recovery_failure(task.id, "token-a", RuntimeError("late"))

    await svc_db.refresh(task)
    meta = (task.context or {})["_childless_post_commit_recovery"]
    assert meta["claim_token"] == "token-b"
    assert "claimed_at" in meta
    assert "last_error" not in meta


@pytest.mark.asyncio
async def test_markerless_childless_terminalizes_after_bounded_attempts(
    svc_db, mocker
):
    """Exhausted marker-less recovery becomes visible FAILED, not endless churn."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        post_commit_failed=False,
        context={
            "_childless_post_commit_recovery": {
                "attempts": 20,
                "last_error": "PostCommitLocked: project locked",
            }
        },
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock()
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _recover_childless_evaluating_without_marker()

    mock_process.assert_not_called()
    await svc_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.post_commit_failed is True
    abandoned = (task.context or {})["_childless_post_commit_abandoned"]
    assert abandoned["after_attempts"] == 20


@pytest.mark.asyncio
async def test_markerless_recovery_budget_resets_for_fresh_attempt(svc_db, mocker):
    """A manual/automatic requeue gets a new recovery budget, not attempt 21."""
    from app.task_blocked import reset_task_for_fresh_attempt

    project = await _make_project(svc_db)
    task = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        context={
            "_childless_post_commit_recovery": {"attempts": 20},
            "_childless_post_commit_abandoned": {"after_attempts": 20},
        },
        plan_output={"subtasks": [{"type": "create", "prompt": "old"}]},
    )
    svc_db.add(task)
    await svc_db.commit()

    reset_task_for_fresh_attempt(task)
    task.status = TaskStatus.QUEUED
    await svc_db.commit()
    assert "_childless_post_commit_recovery" not in (task.context or {})
    assert "_childless_post_commit_abandoned" not in (task.context or {})

    # Simulate the newly dispatched plan reporting its own completion before a
    # hard kill at the spawn boundary.
    task.status = TaskStatus.EVALUATING
    task.plan_output = {"subtasks": [{"type": "create", "prompt": "new"}]}
    task.updated_at = _naive_utc_ago(minutes=20)
    await svc_db.commit()

    mock_process = mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new=AsyncMock(side_effect=RuntimeError("retry me")),
    )
    await _recover_childless_evaluating_without_marker()

    await svc_db.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert "_childless_post_commit_abandoned" not in (task.context or {})
    assert (task.context or {})["_childless_post_commit_recovery"]["attempts"] == 1
    mock_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_markerless_childless_skips_marked_and_no_plan_output(svc_db, mocker):
    """A row WITH a pending marker is owned by the marker-based loop (skip here, no
    double-run); a row with NO plan_output never reported a non-empty plan and is
    skipped (not the COMPLETED-spawn signal)."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    marked = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,  # owned by the marker-based loop
        plan_output={"subtasks": [{"type": "create", "prompt": "do x"}]},
        updated_at=_naive_utc_ago(minutes=20),
    )
    no_plan = Task(
        id=_uuid.uuid4(),
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "decompose"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        post_commit_pending=False,
        plan_output=None,  # never reported a plan → not a COMPLETED-spawn signal
        updated_at=_naive_utc_ago(minutes=20),
    )
    svc_db.add_all([marked, no_plan])
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError("must not re-run marked or plan-less rows")
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _recover_childless_evaluating_without_marker()
    mock_process.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — write-ahead marker age-gate (incident 2026-05-21)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_skips_recent_inline_running_marker(svc_db, mocker):
    """A reason="inline_running" marker is written-ahead BEFORE
    _process_post_commit runs. A *recent* one may still be a live inline pass,
    so the sweep must NOT retry it — retrying would double-process the
    post-commit (double review-spawn / double merge)."""
    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),  # just now — run still live
                "reason": "inline_running",
                "payload": {"task_id": "x", "status": "COMPLETED", "logs": "{}"},
                "attempts": 0,
            }
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock(
        side_effect=AssertionError("must not retry a live inline_running marker")
    )
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_not_called()
    await svc_db.refresh(task)
    assert task.post_commit_pending is True, "marker must persist — run in flight"


@pytest.mark.asyncio
async def test_post_commit_sweep_retries_stale_inline_running_marker(svc_db, mocker):
    """Orphan recovery for incident 2026-05-21: when _process_post_commit is
    killed mid-flight by an instance shutdown, the except handler never runs,
    so the write-ahead marker stays reason="inline_running" forever. Once it
    ages past the grace window the sweep MUST retry it — without this the task
    orphans permanently (cluster PR #4775 / be15c811 stranded eefbed05)."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                # 20 min old — well past the 600s grace; the inline pass that
                # set this is provably dead.
                "at": (_naive_utcnow() - timedelta(minutes=20)).isoformat(),
                "reason": "inline_running",
                "payload": {
                    "task_id": str(task_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 0,
            }
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    mock_process = AsyncMock()  # succeeds
    mocker.patch("app.routers.agent_status._process_post_commit", mock_process)

    await _sweep_post_commit_pending()

    mock_process.assert_called_once()
    await svc_db.refresh(task)
    assert task.post_commit_pending is False, "recovered marker must be cleared"
    assert "_post_commit_pending_meta" not in (task.context or {})


@pytest.mark.asyncio
async def test_clear_post_commit_pending_clears_write_ahead_marker(svc_db):
    """_clear_post_commit_pending is the happy-path counterpart to the sweep:
    after a clean _process_post_commit run, update_agent_status calls it to
    drop the write-ahead marker so the sweep never re-runs a finished
    post-commit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.routers.agent_status import _clear_post_commit_pending

    project = await _make_project(svc_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "keep_me": "untouched",
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "inline_running",
                "payload": {"task_id": "x", "status": "COMPLETED", "logs": "{}"},
                "attempts": 0,
            },
        },
    )
    svc_db.add(task)
    await svc_db.commit()

    factory = async_sessionmaker(
        svc_db.bind, class_=AsyncSession, expire_on_commit=False
    )
    await _clear_post_commit_pending(factory, task.id)

    await svc_db.refresh(task)
    assert task.post_commit_pending is False
    assert "_post_commit_pending_meta" not in (task.context or {})
    assert (task.context or {}).get("keep_me") == "untouched", (
        "unrelated context keys must survive the clear"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _sweep_post_commit_pending — bump-failure isolation (regression 2026-05-07)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_bump_failure_does_not_poison_batch(svc_db, mocker):
    """A claim-time bump failure on one task must not poison the rest of the batch.

    Under the claim-time accounting pattern, _claim_and_bump_total_attempts is
    called BEFORE _process_post_commit. If the claim raises (e.g. row-lock
    contention → LockNotAvailableError on the bump commit), the bad task is
    deferred WITHOUT reaching _process_post_commit, and the sweep continues to
    process the good task. The good task's claim-time bump and subsequent
    attempt-bump must both succeed.

    Regression for 2026-05-07 incident (structurally updated for the claim-time
    pattern): a single stuck task's failure must not abort the loop and leave
    subsequent pending tasks unprocessed.
    """
    import uuid as _uuid

    project = await _make_project(svc_db)
    bad_id = _uuid.uuid4()
    good_id = _uuid.uuid4()
    bad_task = Task(
        id=bad_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "bad"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(bad_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 4,
            }
        },
    )
    good_task = Task(
        id=good_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "good"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(good_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
            }
        },
    )
    svc_db.add_all([bad_task, good_task])
    await svc_db.commit()

    # _process_post_commit raises for the good task (persistent error) so
    # the post-failure attempts-bump path runs for it.
    call_log: list[str] = []

    async def fake_process(task_id, _payload):
        call_log.append(str(task_id))
        raise RuntimeError("simulated _process_post_commit retry exhaustion")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=fake_process
    )

    # Make ONLY the bad task's claim-time bump raise (mimics the production
    # LockNotAvailableError that hits during commit when the row is held).
    # flag_modified is the last write before commit inside _claim_and_bump_total_attempts,
    # so raising there aborts the claim-session without committing.
    from sqlalchemy.orm import attributes as _sa_attrs

    real_flag_modified = _sa_attrs.flag_modified

    def fake_flag_modified(obj, key):
        if isinstance(obj, Task) and str(obj.id) == str(bad_id) and key == "context":
            raise RuntimeError("simulated claim-time bump lock_timeout")
        return real_flag_modified(obj, key)

    mocker.patch("sqlalchemy.orm.attributes.flag_modified", fake_flag_modified)

    await _sweep_post_commit_pending()

    # bad_task: claim raised → deferred without reaching _process_post_commit.
    assert str(bad_id) not in call_log, (
        "bad_task claim-time raise must defer the task, not forward it to _process_post_commit"
    )
    # good_task: claim succeeded → reached _process_post_commit.
    assert str(good_id) in call_log, (
        "bad_task deferral must not poison the batch — good_task must still be processed"
    )

    # Good task: persistent failure → attempts bumped 1 → 2.
    await svc_db.refresh(good_task)
    good_attempts = (
        (good_task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    )
    assert good_attempts == 2, (
        f"good task's bump should have advanced attempts to 2, got {good_attempts}. "
        f"This means the bad task's claim failure poisoned the batch — the regression is back."
    )

    # Bad task: deferred → attempts stays at 4.
    await svc_db.refresh(bad_task)
    bad_attempts = (
        (bad_task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    )
    assert bad_attempts == 4, f"bad task attempts should stay at 4, got {bad_attempts}"


@pytest.mark.asyncio
async def test_post_commit_sweep_abandons_lock_starved_marker_by_age(svc_db, mocker):
    """Root-cause regression for the 2026-06-14 stuck-task lock-churn incident
    (task 73281cea). A marker whose attempts/total_attempts stay LOW because its
    bump UPDATE is perpetually row-lock-starved (LockNotAvailableError on the bump
    → total_attempts frozen) would otherwise churn the sweep forever: the
    MAX_TOTAL_ATTEMPTS ceiling can't fire when total_attempts can't advance. The
    wall-clock backstop must terminalize it by age (first_pending_at older than
    POST_COMMIT_MAX_PENDING_AGE_S) WITHOUT invoking the heavy retry.

    Would FAIL pre-fix: an old marker with low counters is neither abandoned
    (neither ceiling met) nor cleared (the retry keeps failing) → pending forever,
    and _process_post_commit is invoked every sweep."""
    import uuid as _uuid
    from datetime import timedelta as _td
    from app.recovery_sweeps import POST_COMMIT_MAX_PENDING_AGE_S

    project = await _make_project(svc_db)
    stuck_id = _uuid.uuid4()
    old_ts = (
        _naive_utcnow() - _td(seconds=POST_COMMIT_MAX_PENDING_AGE_S + 600)
    ).isoformat()
    stuck = Task(
        id=stuck_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "stuck"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": old_ts,
                "first_pending_at": old_ts,
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(stuck_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                # LOW counters: neither the 5-attempt nor the 20-total ceiling
                # would fire — only the age backstop can terminalize this.
                "attempts": 1,
                "total_attempts": 2,
            }
        },
    )
    svc_db.add(stuck)
    await svc_db.commit()

    # The age backstop must short-circuit BEFORE the heavy retry runs.
    called: list[str] = []

    async def fake_process(task_id, _payload):
        called.append(str(task_id))
        raise RuntimeError("retry must not run for an age-terminalized marker")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=fake_process
    )

    await _sweep_post_commit_pending()

    assert called == [], "heavy retry should be short-circuited by the age backstop"

    await svc_db.refresh(stuck)
    assert stuck.post_commit_pending is False
    assert stuck.post_commit_failed is True
    ctx = stuck.context or {}
    assert "_post_commit_pending_meta" not in ctx, (
        "stale pending marker must be cleared"
    )
    abandoned = ctx.get("_post_commit_abandoned") or {}
    assert abandoned.get("reason") == "max_pending_age_exceeded"
    assert abandoned.get("after_pending_age_s", 0) >= POST_COMMIT_MAX_PENDING_AGE_S


# ══════════════════════════════════════════════════════════════════════════════
# Claim-time attempt accounting: structural root-fix for post-commit lock churn
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_defers_cleanly_when_row_held(svc_db, mocker):
    """When _claim_and_bump_total_attempts returns None (row held by another
    writer), the sweep must defer the task — no _process_post_commit call,
    no total_attempts change, post_commit_pending stays True — and continue
    processing other tasks in the batch (no batch poisoning).

    This is the structural root-fix invariant: a persistently-held row causes
    clean defers, never the lock_timeout churn that triggered the 2026-06-14
    stuck-task incident.
    """
    import uuid as _uuid

    project = await _make_project(svc_db)
    stuck_id = _uuid.uuid4()
    other_id = _uuid.uuid4()
    stuck = Task(
        id=stuck_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "stuck"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(stuck_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 1,
                "total_attempts": 3,
            }
        },
    )
    other = Task(
        id=other_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "other"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={
            "_post_commit_pending_meta": {
                "at": _naive_utcnow().isoformat(),
                "reason": "post_commit_error",
                "payload": {
                    "task_id": str(other_id),
                    "status": "COMPLETED",
                    "logs": "{}",
                },
                "attempts": 0,
                "total_attempts": 0,
            }
        },
    )
    svc_db.add_all([stuck, other])
    await svc_db.commit()

    process_calls: list[str] = []

    async def fake_process(task_id, _payload):
        process_calls.append(str(task_id))

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=fake_process
    )

    # Capture real function BEFORE patching to avoid recursive call via the mock.
    from app.recovery_post_commit_pending import (
        _claim_and_bump_total_attempts as _real_claim,
    )

    # Return None for stuck task (row held), let other task claim normally.
    async def fake_claim(task_id, pending_meta):
        if str(task_id) == str(stuck_id):
            return None  # simulate row held by peer
        return await _real_claim(task_id, pending_meta)

    mocker.patch(
        "app.recovery_post_commit_pending._claim_and_bump_total_attempts",
        side_effect=fake_claim,
    )

    await _sweep_post_commit_pending()

    # stuck task: row held → deferred, _process_post_commit NOT called.
    assert str(stuck_id) not in process_calls, (
        "stuck task must be deferred when claim returns None — "
        "_process_post_commit must not be called"
    )

    # stuck task: total_attempts unchanged (defer, not bump).
    await svc_db.refresh(stuck)
    stuck_meta = (stuck.context or {}).get("_post_commit_pending_meta", {})
    assert stuck_meta.get("total_attempts") == 3, (
        f"deferred task must not have total_attempts changed; got {stuck_meta.get('total_attempts')}"
    )
    assert stuck.post_commit_pending is True, (
        "deferred task must keep post_commit_pending=True"
    )

    # other task: claim succeeded → _process_post_commit called (batch not poisoned).
    assert str(other_id) in process_calls, (
        "other task must still be processed despite stuck task deferral"
    )


@pytest.mark.asyncio
async def test_post_commit_sweep_claim_bumps_total_attempts_before_process(
    svc_db, mocker
):
    """total_attempts must be advanced in the DB BEFORE _process_post_commit runs.

    This is the core write-ahead durability guarantee of the claim-time pattern:
    if the instance is killed after the claim commit but before _process_post_commit
    starts, the next sweep sees total_attempts already incremented and will
    eventually terminalize at MAX_TOTAL_ATTEMPTS — even if no work ever ran.

    Verified by: mock _process_post_commit to raise a transient error, then assert
    that total_attempts advanced despite the failure (claim commit already happened).
    """
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = _make_pending_task(project.id, task_id=task_id, attempts=1, total_attempts=5)
    svc_db.add(task)
    await svc_db.commit()

    # Track the DB value of total_attempts at the moment _process_post_commit is called.
    ta_at_process_time: list[int] = []

    async def probe_process(tid, _payload):
        # Read the live DB value under a fresh session to see what was committed.
        from app.services import AsyncSessionLocal
        from app.models import Task as _Task

        async with AsyncSessionLocal() as probe:
            row = await probe.get(_Task, tid)
            meta = (row.context or {}).get("_post_commit_pending_meta", {})
            ta_at_process_time.append(int(meta.get("total_attempts", 0)))
        raise RuntimeError("secondary rate limit — transient error")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=probe_process
    )

    await _sweep_post_commit_pending()

    # The claim-time bump must have committed BEFORE probe_process ran.
    assert ta_at_process_time, "_process_post_commit was never called"
    assert ta_at_process_time[0] == 6, (
        f"total_attempts must be 6 (5+1 from claim) when _process_post_commit runs; "
        f"got {ta_at_process_time[0]}"
    )

    # After transient failure, total_attempts stays at 6 (already bumped).
    await svc_db.refresh(task)
    meta = (task.context or {}).get("_post_commit_pending_meta", {})
    assert meta.get("total_attempts") == 6
    assert meta.get("attempts") == 1, "transient failure must not advance attempts"
    assert task.post_commit_pending is True


@pytest.mark.asyncio
async def test_post_commit_sweep_ceiling_fires_via_claim_bump_no_process(
    svc_db, mocker
):
    """When total_attempts is one-under the ceiling (MAX_TOTAL_ATTEMPTS - 1),
    the claim-time bump pushes it over, and the sweep must terminalize the task
    WITHOUT calling _process_post_commit.

    This validates the post-claim ceiling re-check: the task reaches a terminal
    state by attempt-count, independent of age.
    """
    import uuid as _uuid
    from app.services import MAX_TOTAL_ATTEMPTS

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    # One-under the ceiling: claim-time bump will push it to MAX_TOTAL_ATTEMPTS.
    task = _make_pending_task(
        project.id,
        task_id=task_id,
        attempts=1,
        total_attempts=MAX_TOTAL_ATTEMPTS - 1,
    )
    svc_db.add(task)
    await svc_db.commit()

    process_calls: list[str] = []

    async def fake_process(tid, _payload):
        process_calls.append(str(tid))

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=fake_process
    )

    await _sweep_post_commit_pending()

    # post-claim ceiling re-check must terminalize WITHOUT doing work.
    assert process_calls == [], (
        "_process_post_commit must NOT be called when post-claim ceiling fires"
    )

    await svc_db.refresh(task)
    assert task.post_commit_pending is False, (
        "marker must be cleared on ceiling termination"
    )
    assert task.post_commit_failed is True, "task must be marked post_commit_failed"
    ctx = task.context or {}
    assert "_post_commit_pending_meta" not in ctx, "retry meta must be cleared"
    abandoned = ctx.get("_post_commit_abandoned") or {}
    assert abandoned.get("after_total_attempts") == MAX_TOTAL_ATTEMPTS, (
        f"abandoned record must show total_attempts={MAX_TOTAL_ATTEMPTS}; "
        f"got {abandoned.get('after_total_attempts')}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# C6: _sweep_post_commit_pending — transient failures must NOT burn the cap
# ══════════════════════════════════════════════════════════════════════════════


def _make_pending_task(project_id, *, task_id, attempts, total_attempts=None):
    """A COMPLETED task carrying a failure-set post_commit_pending marker."""
    meta = {
        "at": _naive_utcnow().isoformat(),
        "reason": "post_commit_error",
        "payload": {
            "task_id": str(task_id),
            "status": "COMPLETED",
            "logs": "{}",
        },
        "attempts": attempts,
    }
    if total_attempts is not None:
        meta["total_attempts"] = total_attempts
    return Task(
        id=task_id,
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        post_commit_pending=True,
        context={"_post_commit_pending_meta": meta},
    )


@pytest.mark.asyncio
async def test_post_commit_sweep_transient_failure_does_not_bump_attempts(
    svc_db, mocker
):
    """C6 seam: a TRANSIENT post-commit retry failure (GitHub rate-limit / quota
    outage, row contention) must NOT count against the 5-attempt cap. Pre-fix the
    except handler bumped the attempt counter for ANY exception, so a sustained
    transient outage burned the whole budget and the sweep abandoned a task
    (post_commit_failed) that would have recovered once the outage cleared —
    silently dropping the deferred review/fix spawn and rollup nudge.

    Would FAIL pre-fix: attempts would advance 1 → 2 on the transient raise."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = _make_pending_task(project.id, task_id=task_id, attempts=1)
    svc_db.add(task)
    await svc_db.commit()

    async def transient_fail(task_id, _payload):
        # Mirrors a GitHub secondary-rate-limit surfacing from a post-commit
        # GitHub call (review-spawn / rollup gate).
        raise RuntimeError("You have exceeded a secondary rate limit (429)")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=transient_fail
    )

    await _sweep_post_commit_pending()

    await svc_db.refresh(task)
    attempts = (task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    assert attempts == 1, (
        f"transient failure must NOT increment the attempt cap; got {attempts}"
    )
    assert task.post_commit_pending is True, "marker must persist for the next sweep"
    assert not (task.context or {}).get("post_commit_abandoned"), (
        "task must not be abandoned on a transient failure"
    )
    assert task.post_commit_failed is not True, (
        "task must not be marked post_commit_failed on a transient failure"
    )


@pytest.mark.asyncio
async def test_post_commit_sweep_postcommitlocked_does_not_bump_attempts(
    svc_db, mocker
):
    """C6 seam: PostCommitLocked (row still held by a peer) is pure contention —
    retrying next sweep is correct, so it must not count toward the cap either.

    Would FAIL pre-fix: the typed exception was caught by the bare `except
    Exception` and bumped attempts like any persistent error."""
    import uuid as _uuid
    from app.task_lifecycle import PostCommitLocked

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = _make_pending_task(project.id, task_id=task_id, attempts=3)
    svc_db.add(task)
    await svc_db.commit()

    async def locked(task_id, _payload):
        raise PostCommitLocked(f"task {task_id} row held by peer after 3 retries")

    mocker.patch("app.routers.agent_status._process_post_commit", side_effect=locked)

    await _sweep_post_commit_pending()

    await svc_db.refresh(task)
    attempts = (task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    assert attempts == 3, (
        f"PostCommitLocked must NOT increment the attempt cap; got {attempts}"
    )
    assert task.post_commit_pending is True


@pytest.mark.asyncio
async def test_post_commit_sweep_persistent_failure_still_bumps_attempts(
    svc_db, mocker
):
    """C6 guard: the cap still works for PERSISTENT failures (a genuine defect
    that recurs every retry, e.g. a bug in a completion handler). The transient
    carve-out must not weaken the give-up bound for non-transient errors."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = _make_pending_task(project.id, task_id=task_id, attempts=1)
    svc_db.add(task)
    await svc_db.commit()

    async def persistent_fail(task_id, _payload):
        raise ValueError("malformed completion payload — handler keeps crashing")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=persistent_fail
    )

    await _sweep_post_commit_pending()

    await svc_db.refresh(task)
    attempts = (task.context or {}).get("_post_commit_pending_meta", {}).get("attempts")
    assert attempts == 2, (
        f"persistent failure must still advance the attempt cap; got {attempts}"
    )


def test_post_commit_retry_is_transient_classifier():
    """Unit coverage for the transient/persistent classifier the sweep uses."""
    from app.services import _post_commit_retry_is_transient
    from app.task_lifecycle import PostCommitLocked

    # Transient
    assert _post_commit_retry_is_transient(PostCommitLocked("held by peer"))
    assert _post_commit_retry_is_transient(
        RuntimeError("You have exceeded a secondary rate limit")
    )
    assert _post_commit_retry_is_transient(RuntimeError("HTTP 429 Too Many Requests"))
    assert _post_commit_retry_is_transient(
        RuntimeError("canceling statement due to lock_timeout")
    )
    # Persistent
    assert not _post_commit_retry_is_transient(ValueError("bad payload"))
    assert not _post_commit_retry_is_transient(KeyError("missing field"))
    import asyncio as _asyncio

    assert not _post_commit_retry_is_transient(_asyncio.TimeoutError())


# ══════════════════════════════════════════════════════════════════════════════
# C6 rework (C): hard outer bound — transient-misclassification cannot loop forever
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_sweep_transient_failure_advances_total_attempts(
    svc_db, mocker
):
    """C6 rework seam (C): a transient failure does NOT bump the 5-attempt cap,
    but it MUST advance the hard ``total_attempts`` outer bound — otherwise a
    PERSISTENT failure whose error string merely CONTAINS a transient substring
    ("429"/"quota"/"lock_timeout") re-runs every sweep forever and the marker
    never clears.

    Would FAIL pre-rework: the transient branch was a bare ``continue`` that
    wrote nothing, so ``total_attempts`` never existed and there was no outer
    bound at all."""
    import uuid as _uuid

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    task = _make_pending_task(project.id, task_id=task_id, attempts=1)
    svc_db.add(task)
    await svc_db.commit()

    async def transient_fail(task_id, _payload):
        raise RuntimeError("You have exceeded a secondary rate limit (429)")

    mocker.patch(
        "app.routers.agent_status._process_post_commit", side_effect=transient_fail
    )

    await _sweep_post_commit_pending()

    await svc_db.refresh(task)
    meta = (task.context or {}).get("_post_commit_pending_meta", {})
    # 5-attempt cap untouched (transient carve-out preserved)...
    assert meta.get("attempts") == 1, (
        f"transient failure must NOT bump the 5-attempt cap; got {meta.get('attempts')}"
    )
    # ...but the hard outer bound advanced (seeded from attempts=1, now 2).
    assert meta.get("total_attempts") == 2, (
        f"transient failure must advance total_attempts so it can't loop "
        f"forever; got {meta.get('total_attempts')}"
    )
    assert task.post_commit_pending is True


@pytest.mark.asyncio
async def test_post_commit_sweep_persistent_with_transient_string_eventually_gives_up(
    svc_db, mocker
):
    """C6 rework seam (C): the unbounded-retry tail. A PERSISTENT failure whose
    error string LOOKS transient ("429") is forever classified transient and
    never bumps the 5-attempt cap — pre-rework it re-ran every 60s sweep forever
    and ``post_commit_pending`` never cleared. The hard ``total_attempts`` ceiling
    (MAX_TOTAL_ATTEMPTS) is the backstop: once total_attempts reaches it the
    sweep terminalizes (marker clears + post_commit_failed) regardless of the
    transient-looking string.

    Seeded right at the ceiling so the very next sweep gives up.
    Would FAIL pre-rework: no ceiling existed; the task would re-run forever."""
    import uuid as _uuid
    from app.services import MAX_TOTAL_ATTEMPTS

    project = await _make_project(svc_db)
    task_id = _uuid.uuid4()
    # attempts well under the 5-cap (so the cap can NEVER fire — proving the
    # hard bound is what gives up), total_attempts at the ceiling.
    task = _make_pending_task(
        project.id,
        task_id=task_id,
        attempts=1,
        total_attempts=MAX_TOTAL_ATTEMPTS,
    )
    svc_db.add(task)
    await svc_db.commit()

    async def persistent_but_transient_looking(task_id, _payload):
        # A genuine persistent defect that happens to mention 429 in its text —
        # the misclassification the ceiling must contain.
        raise RuntimeError("handler crash: downstream returned 429 every time")

    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        side_effect=persistent_but_transient_looking,
    )

    await _sweep_post_commit_pending()

    await svc_db.refresh(task)
    # Terminalized: marker cleared and the task marked failed — NOT looping.
    assert task.post_commit_pending is False, (
        "the hard total_attempts ceiling must terminalize a persistently-failing "
        "task even when its error string looks transient"
    )
    assert task.post_commit_failed is True, (
        "task must be marked post_commit_failed after exhausting the hard bound"
    )
    assert (task.context or {}).get("_post_commit_abandoned"), (
        "abandonment must be recorded in context"
    )
    assert "_post_commit_pending_meta" not in (task.context or {}), (
        "the retry meta must be cleared so the task stops re-appearing in sweeps"
    )


# ══════════════════════════════════════════════════════════════════════════════
# stale_branch_prefixes (project-driven branch cleanup)
# stale_branch_prefixes (project-driven branch cleanup)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_branch_prefixes_default_when_not_configured(svc_db):
    """Project without build_config returns the default Blueprint prefixes."""
    project = await _make_project(svc_db)
    # Project has no build_config set
    assert project.build_config is None
    # Should return the Blueprint defaults
    assert project.stale_branch_prefixes == ("blueprint/", "research/", "plan/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_follows_namespace_override(svc_db):
    """Project with task_branch_namespace override picks up the new prefix."""
    project = Project(
        name="Autoproj Project",
        user_id="user1",
        build_config={"task_branch_namespace": "autoproj"},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.task_branch_namespace == "autoproj"
    # Without an explicit stale_branch_prefixes override, the namespace flows
    # into the default cleanup-prefix tuple so per-project work branches stay
    # eligible for sweep.
    assert project.stale_branch_prefixes == ("autoproj/", "research/", "plan/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_custom_from_build_config(svc_db):
    """Project with custom prefixes in build_config uses them."""
    project = Project(
        name="Custom Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": ["feature/", "hotfix/"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.stale_branch_prefixes == ("feature/", "hotfix/")


@pytest.mark.asyncio
async def test_stale_branch_prefixes_empty_list_disables_cleanup(svc_db):
    """Project with empty prefix list in build_config disables cleanup."""
    project = Project(
        name="No Cleanup Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": []},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    # Empty list should be returned as-is (cleanup disabled for this project)
    assert project.stale_branch_prefixes == ()


@pytest.mark.asyncio
async def test_stale_branch_prefixes_preserves_single_prefix(svc_db):
    """Project with single custom prefix works correctly."""
    project = Project(
        name="Single Prefix Project",
        user_id="user1",
        build_config={"stale_branch_prefixes": ["task/"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert project.stale_branch_prefixes == ("task/",)


# ══════════════════════════════════════════════════════════════════════════════
# protected_branches (project-driven cleanup safety)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_protected_branches_default_when_not_configured(svc_db):
    """Project without build_config returns the default protected branch set."""
    project = await _make_project(svc_db)
    assert project.build_config is None
    protected = project.protected_branches
    assert "main" in protected
    assert "master" in protected
    assert "develop" in protected
    assert "dev" in protected


@pytest.mark.asyncio
async def test_protected_branches_custom_from_build_config(svc_db):
    """Project with custom protected_branches in build_config uses them."""
    project = Project(
        name="Custom Protected Project",
        user_id="user1",
        build_config={"protected_branches": ["trunk", "stable"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    protected = project.protected_branches
    assert "trunk" in protected
    assert "stable" in protected


@pytest.mark.asyncio
async def test_protected_branches_always_includes_integration_branch(svc_db):
    """integration_branch is always in the protected set regardless of config."""
    project = Project(
        name="Integration Branch Project",
        user_id="user1",
        integration_branch="develop-lts",
        build_config={"protected_branches": ["trunk"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert "develop-lts" in project.protected_branches


@pytest.mark.asyncio
async def test_protected_branches_always_includes_promotion_target(svc_db):
    """promotion_target is always in the protected set regardless of config."""
    project = Project(
        name="Promotion Target Project",
        user_id="user1",
        promotion_target="release-lts",
        build_config={"protected_branches": ["trunk"]},
    )
    svc_db.add(project)
    await svc_db.commit()
    await svc_db.refresh(project)
    assert "release-lts" in project.protected_branches


# ── _derive_task_branch: task-type -> branch prefix mapping ──────────────────


def test_derive_task_branch_known_prefixes():
    """Each task type with a non-default prefix maps correctly."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    assert (
        _derive_task_branch(task_id, "refactor", "blueprint")
        == f"blueprint/refactor-{short}"
    )
    assert (
        _derive_task_branch(task_id, "docs", "blueprint") == f"blueprint/docs-{short}"
    )
    assert (
        _derive_task_branch(task_id, "deps", "blueprint") == f"blueprint/deps-{short}"
    )
    assert (
        _derive_task_branch(task_id, "migrate", "blueprint")
        == f"blueprint/migrate-{short}"
    )


def test_derive_task_branch_default_prefix():
    """Task types that map to 'task' all yield blueprint/task-{short_id}."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    for task_type in (
        "create",
        "qa",
        "fix",
        "perf",
        "security_audit",
        "debug",
        "smoketest",
        None,
        "",
    ):
        assert (
            _derive_task_branch(task_id, task_type, "blueprint")
            == f"blueprint/task-{short}"
        ), task_type


def test_derive_task_branch_short_id_derivation():
    """short_id is exactly the first 8 hex chars of the UUID with dashes stripped."""
    task_id = _uuid.UUID("aabbccdd-1122-3344-5566-778899aabbcc")
    branch = _derive_task_branch(task_id, "create", "blueprint")
    assert branch == "blueprint/task-aabbccdd"


# ── derive_task_branch: per-project namespace (now REQUIRED) ─────────────────


def test_derive_task_branch_custom_namespace():
    """The (now required) namespace arg sets the leading branch segment."""
    task_id = _uuid.UUID("12345678-abcd-ef01-2345-678abcdef012")
    short = "12345678"
    assert (
        _derive_task_branch(task_id, "refactor", "autoproj")
        == f"autoproj/refactor-{short}"
    )
    assert _derive_task_branch(task_id, "docs", "bot") == f"bot/docs-{short}"
    # namespace is required and has no silent "blueprint" default -- omitting it
    # is a TypeError, which is what surfaces self-repo-hardcoded call sites.
    with pytest.raises(TypeError):
        _derive_task_branch(task_id, "refactor")


def test_get_project_task_branch_namespace_fallback_chain():
    """Resolver returns project override → runtime_config → 'blueprint'."""
    from app.shared_state import (
        get_project_task_branch_namespace,
        runtime_config,
    )

    class _StubProject:
        def __init__(self, build_config):
            self.build_config = build_config

    # Per-project build_config wins
    assert (
        get_project_task_branch_namespace(
            _StubProject({"task_branch_namespace": "autoproj"})
        )
        == "autoproj"
    )
    # Empty build_config falls through to runtime_config default
    assert get_project_task_branch_namespace(_StubProject({})) == runtime_config.get(
        "task_branch_namespace", "blueprint"
    )
    # No project at all returns runtime_config default
    assert get_project_task_branch_namespace(None) == runtime_config.get(
        "task_branch_namespace", "blueprint"
    )


# NOTE: the ``_build_branch_short_id_re`` / ``_DEFAULT_STAGING_PREFIXES``
# cases were relocated to testing/test_branch_cleanup.py when the stale-branch
# cleanup concern was extracted into ``app.branch_cleanup``.


# ══════════════════════════════════════════════════════════════════════════════
# _ci_backstop_timeout_reason — reason-picker for the CI-wait backstop.
#
# Contract test for Fix 2A: when both pipeline gates are open
# (ci_passed=True, review_approved=True) the parent is still EVALUATING
# because the downstream merge gate is blocked.  The reason MUST surface the
# merge-side state (mergeable_state / merge_attempts / last_merge_msg) and
# MUST NOT fall through to the historical "both gates unmet" message — that
# message is the opposite of the truth and made 5 stuck tasks invisible in
# prod.
# ══════════════════════════════════════════════════════════════════════════════


def test_ci_backstop_reason_both_gates_open_surfaces_merge_block():
    """ci_passed=True, review_approved=True, escalated=True must yield a
    merge-block reason mentioning mergeable_state, NOT the legacy
    'both gates unmet' lie."""
    from app.services import _ci_backstop_timeout_reason

    review_loop = {
        "ci_wait_started": "2026-01-01T00:00:00",
        "ci_passed": True,
        "review_approved": True,
        "escalated": True,
        "mergeable_state": "blocked",
        "merge_in_progress_resets": 3,
        "merge_last_error": "Required status check is in progress",
    }
    reason = _ci_backstop_timeout_reason(
        ci_ws_val="2026-01-01T00:00:00",
        ci_ws_valid=True,
        ci_passed=True,
        review_approved=True,
        was_escalated=True,
        review_summary=None,
        review_loop=review_loop,
    )

    # MUST mention the merge gate's actual state.
    assert "merge blocked" in reason.lower()
    assert "mergeable_state" in reason
    assert "blocked" in reason
    # MUST surface the in-progress reset counter (this is the field the
    # downstream merge-gate failure exposes — the bug was that the reason
    # didn't print it).
    assert "merge_attempts=3" in reason
    # The last-merge-msg snippet rides through too.
    assert "Required status check" in reason

    # Anti-regression: the historical fall-through message must NOT appear.
    assert "both gates unmet" not in reason.lower()


def test_ci_backstop_reason_both_gates_open_no_merge_diagnostics_still_named_correctly():
    """Even when the merge-side fields are absent (older parking task whose
    review_loop predates the merge-state persistence), the (ci_passed AND
    review_approved) branch MUST still report 'merge blocked' rather than
    falling through to the gates-unmet message."""
    from app.services import _ci_backstop_timeout_reason

    review_loop = {
        "ci_wait_started": "2026-01-01T00:00:00",
        "ci_passed": True,
        "review_approved": True,
        "escalated": True,
        # No mergeable_state / merge_in_progress_resets / merge_last_error.
    }
    reason = _ci_backstop_timeout_reason(
        ci_ws_val="2026-01-01T00:00:00",
        ci_ws_valid=True,
        ci_passed=True,
        review_approved=True,
        was_escalated=True,
        review_summary=None,
        review_loop=review_loop,
    )

    assert "merge blocked" in reason.lower()
    assert "both gates unmet" not in reason.lower()


def test_ci_backstop_reason_existing_branches_unaffected():
    """The pre-existing branches (CI-only, review-only, escalated-only,
    neither, corrupt/missing ci_wait_started) must keep their original
    messages — this is a strictly additive fix."""
    from app.services import _ci_backstop_timeout_reason

    # CI passed, review pending, no review task surfaced.
    assert (
        _ci_backstop_timeout_reason(
            ci_ws_val="2026-01-01T00:00:00",
            ci_ws_valid=True,
            ci_passed=True,
            review_approved=False,
            was_escalated=False,
            review_summary=None,
            review_loop={},
        )
        == "CI passed but no review task exists for this chain"
    )

    # Review approved, CI never completed.
    assert "review approved but CI never completed" in _ci_backstop_timeout_reason(
        ci_ws_val="2026-01-01T00:00:00",
        ci_ws_valid=True,
        ci_passed=False,
        review_approved=True,
        was_escalated=True,
        review_summary=None,
        review_loop={},
    )

    # Neither gate, escalated.
    assert (
        _ci_backstop_timeout_reason(
            ci_ws_val="2026-01-01T00:00:00",
            ci_ws_valid=True,
            ci_passed=False,
            review_approved=False,
            was_escalated=True,
            review_summary=None,
            review_loop={},
        )
        == "both gates unmet after 20-min escalation"
    )

    # Missing ci_wait_started.
    assert (
        _ci_backstop_timeout_reason(
            ci_ws_val=None,
            ci_ws_valid=False,
            ci_passed=False,
            review_approved=False,
            was_escalated=False,
            review_summary=None,
            review_loop={},
        )
        == "ci_wait_started missing from review_loop"
    )

    # Corrupt ci_wait_started.
    corrupt = _ci_backstop_timeout_reason(
        ci_ws_val="garbage",
        ci_ws_valid=False,
        ci_passed=False,
        review_approved=False,
        was_escalated=False,
        review_summary=None,
        review_loop={},
    )
    assert corrupt.startswith("ci_wait_started corrupt")


def test_ci_backstop_reason_rollup_signals_surfaced_when_review_loop_empty():
    """When both CI and review gates are open but review_loop has no merge
    diagnostics (rollup parent path), _ci_backstop_timeout_reason should
    surface the rollup-path signals from parent_ctx instead of the misleading
    'merge_attempts=0' message."""
    from app.services import _ci_backstop_timeout_reason

    parent_ctx = {
        "review_loop": {
            "ci_wait_started": "2026-01-01T00:00:00",
            "ci_passed": True,
            "review_approved": True,
        },
        "_merge_deferred_state": "empty",
        "_merge_deferred_count": 7,
        "_merge_total_deferrals": 7,
    }
    review_loop = parent_ctx["review_loop"]

    reason = _ci_backstop_timeout_reason(
        ci_ws_val="2026-01-01T00:00:00",
        ci_ws_valid=True,
        ci_passed=True,
        review_approved=True,
        was_escalated=False,
        review_summary=None,
        review_loop=review_loop,
        parent_ctx=parent_ctx,
    )

    assert "rollup merge deferred" in reason
    assert "mergeable_state=empty" in reason
    assert "deferred_count=7" in reason
    assert "total_deferrals=7" in reason
    # Must NOT report the misleading merge_attempts=0 message.
    assert "merge_attempts=0" not in reason


def test_ci_backstop_reason_rollup_signals_not_used_when_review_loop_has_state():
    """When review_loop already has merge diagnostics (non-rollup path or a
    rollup that wrote mergeable_state), parent_ctx signals must NOT override."""
    from app.services import _ci_backstop_timeout_reason

    review_loop = {
        "ci_wait_started": "2026-01-01T00:00:00",
        "ci_passed": True,
        "review_approved": True,
        "mergeable_state": "blocked",
        "merge_in_progress_resets": 2,
    }
    parent_ctx = {
        "_merge_deferred_state": "empty",
        "_merge_deferred_count": 3,
    }

    reason = _ci_backstop_timeout_reason(
        ci_ws_val="2026-01-01T00:00:00",
        ci_ws_valid=True,
        ci_passed=True,
        review_approved=True,
        was_escalated=False,
        review_summary=None,
        review_loop=review_loop,
        parent_ctx=parent_ctx,
    )

    # review_loop state takes priority.
    assert "mergeable_state=blocked" in reason
    assert "merge_attempts=2" in reason
    assert "rollup merge deferred" not in reason
