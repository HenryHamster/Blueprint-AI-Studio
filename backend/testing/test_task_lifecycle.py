"""
Tests for backend/app/task_lifecycle.py.

Covers the critical functions:
  1. _has_active_strategic_task — active strategic task guard
  2. _increment_research_spawn_count — vision spawn counter management
  3. _requeue_parent_task — failed child retry with context injection
  4. _merge_with_retry — PR merge with exponential backoff
  5. _process_post_commit — task not found guard and rollup triggering
"""
import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import Project, Task, TaskStatus, ArtifactType
from app.schemas import AgentStatusPayload
from app.task_lifecycle import (
    _has_active_strategic_task,
    _increment_research_spawn_count,
    _requeue_parent_task,
    _merge_with_retry,
    _comment_with_retry,
    _process_post_commit,
    _record_parent_failure,
    _is_transient_scan_error,
    _extract_failing_tests,
    _extract_error_files,
    _build_scope_overflow_details,
    _build_retry_prompt_suffix,
)

# SQLite JSONB compatibility — defensive, conftest.py also sets this
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler
if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def tl_db(mocker):
    """Isolated in-memory SQLite DB for task_lifecycle tests.

    Yields (session, session_factory) so tests can also pass the factory
    as _session_factory to _process_post_commit.
    """
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )

    # Patch AsyncSessionLocal used by the inner dep-unblocking session
    mocker.patch("app.task_lifecycle.AsyncSessionLocal", _Session)
    # Suppress broadcast calls
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    # Suppress failure alert creation (deferred import from app.main)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _make_project(session) -> Project:
    project = Project(name="Test Project", user_id="user-1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(
    session,
    project_id,
    *,
    task_type: str = "create",
    status: TaskStatus = TaskStatus.QUEUED,
    parent_task_id=None,
    context: dict | None = None,
    depends_on: list | None = None,
    latest_status_message: str | None = None,
    vm_instance_id: str | None = None,
) -> Task:
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type, "prompt": f"do {task_type}"}],
        status=status,
        created_by="ORCHESTRATOR",
        parent_task_id=parent_task_id,
        context=context or {},
        depends_on=depends_on or [],
        latest_status_message=latest_status_message,
        vm_instance_id=vm_instance_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ══════════════════════════════════════════════════════════════════════════════
# _has_active_strategic_task
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_has_active_strategic_task_true_for_design_doc_update(tl_db):
    """Returns True when a QUEUED design_doc_update task exists."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="design_doc_update", status=TaskStatus.QUEUED)
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_true_for_strategic_review_in_progress(tl_db):
    """Returns True when a strategic_review task is IN_PROGRESS."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="strategic_review", status=TaskStatus.IN_PROGRESS)
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_true_for_provisioning(tl_db):
    """Returns True for a PROVISIONING strategic task."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="design_doc_update", status=TaskStatus.PROVISIONING)
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_false_for_non_strategic(tl_db):
    """Returns False when only non-strategic tasks are active."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="create", status=TaskStatus.QUEUED)
    await _make_task(session, project.id, task_type="research", status=TaskStatus.IN_PROGRESS)
    assert await _has_active_strategic_task(project.id, session) is False


@pytest.mark.asyncio
async def test_has_active_strategic_task_false_for_completed_strategic(tl_db):
    """Returns False when the strategic task is already COMPLETED (terminal)."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="design_doc_update", status=TaskStatus.COMPLETED)
    assert await _has_active_strategic_task(project.id, session) is False


@pytest.mark.asyncio
async def test_has_active_strategic_task_false_with_no_tasks(tl_db):
    """Returns False for an empty project."""
    session, _ = tl_db
    project = await _make_project(session)
    assert await _has_active_strategic_task(project.id, session) is False


# ══════════════════════════════════════════════════════════════════════════════
# _increment_research_spawn_count
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_increment_research_spawn_count_starts_at_one(tl_db):
    """First increment on a project with no vision returns 1 and persists."""
    session, _ = tl_db
    project = await _make_project(session)
    # vision defaults to {} — no existing spawn count
    assert not project.vision.get("_research_spawn_count")

    count = await _increment_research_spawn_count(project, session)

    assert count == 1
    assert project.vision["_research_spawn_count"] == 1


@pytest.mark.asyncio
async def test_increment_research_spawn_count_accumulates_and_preserves_other_keys(tl_db):
    """Successive increments accumulate and other vision keys are preserved."""
    session, _ = tl_db
    project = await _make_project(session)
    project.vision = {"_research_spawn_count": 4, "purpose": "keep-me"}
    session.add(project)
    await session.commit()

    count = await _increment_research_spawn_count(project, session)

    assert count == 5
    assert project.vision["_research_spawn_count"] == 5
    assert project.vision["purpose"] == "keep-me"


# ══════════════════════════════════════════════════════════════════════════════
# _requeue_parent_task
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_requeue_skips_cancelled_parent(tl_db):
    """Cancelled parent is left unchanged and 0 is returned."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.CANCELLED)

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_requeue_fails_parent_permanently_at_limit(tl_db):
    """Parent at MAX_PARENT_REQUEUES=3 is set to FAILED and creates an alert."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_limit_persists_failure_category(tl_db):
    """Parent at requeue limit gets failure_category derived from worst failed child."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    # Child with CODE_ERROR persisted
    await _make_task(
        session, project.id, status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"failure_category": "CODE_ERROR"},
        latest_status_message="tests failed",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context.get("failure_category") == "CODE_ERROR"


@pytest.mark.asyncio
async def test_requeue_limit_records_deploy_monitoring_result(tl_db):
    """Parent at requeue limit appends a failure entry to deploy_monitoring.task_results.

    Regression guard: previously _requeue_parent_task set FAILED + persisted
    failure_category but never called record_task_result, so parent-task failures
    were invisible to deploy health / per-type failure gates.
    """
    import app.shared_state as _ss
    from app.routers.agent_status import _recorded_task_ids

    _ss.deploy_monitoring["task_results"].clear()
    _recorded_task_ids.clear()

    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    await _make_task(
        session, project.id, status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"failure_category": "CODE_ERROR"},
        latest_status_message="tests failed",
    )

    await _requeue_parent_task(parent, session)

    entries = list(_ss.deploy_monitoring["task_results"])
    matching = [e for e in entries if e[1] == "failure" and e[2] == "CODE_ERROR"]
    assert matching, f"expected a CODE_ERROR failure entry, got {entries}"


@pytest.mark.asyncio
async def test_requeue_limit_records_direction_result_on_non_infra_failure(tl_db):
    """Parent at requeue limit with direction_id + non-infra failure increments direction streak."""
    import app.shared_state as _ss
    from app.routers.agent_status import _recorded_task_ids

    _ss.direction_failure_streaks.clear()
    _recorded_task_ids.clear()

    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3, "direction_id": "d-test-42"},
    )
    await _make_task(
        session, project.id, status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"failure_category": "CODE_ERROR"},
        latest_status_message="tests failed",
    )

    await _requeue_parent_task(parent, session)

    streak = _ss.direction_failure_streaks.get("d-test-42")
    assert streak is not None, "expected direction streak to be recorded"
    assert streak["consecutive_failures"] == 1
    assert streak["last_failure_class"] == "CODE_ERROR"


@pytest.mark.asyncio
async def test_requeue_no_children_to_retry_records_deploy_monitoring_result(tl_db):
    """Parent with only CANCELLED children (no failed, no active) records AGENT_TIMEOUT failure.

    Covers the second parent-failure path in _requeue_parent_task: retried_count == 0
    and no successful/active children — parent is marked FAILED with AGENT_TIMEOUT.
    """
    import app.shared_state as _ss
    from app.routers.agent_status import _recorded_task_ids

    _ss.deploy_monitoring["task_results"].clear()
    _recorded_task_ids.clear()

    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    # Only a CANCELLED child → retried_count=0, no completed, no active.
    await _make_task(
        session, project.id, status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    entries = list(_ss.deploy_monitoring["task_results"])
    matching = [e for e in entries if e[1] == "failure" and e[2] == "AGENT_TIMEOUT"]
    assert matching, f"expected an AGENT_TIMEOUT failure entry, got {entries}"


@pytest.mark.asyncio
async def test_requeue_cancels_failed_children_and_creates_clones(tl_db, mocker):
    """Failed children are cancelled and replaced with QUEUED clones."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="Build failed: tests failing",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 1

    await session.refresh(child)
    assert child.status == TaskStatus.CANCELLED
    assert child.latest_status_message == "Parent requeued — replaced by retry"

    clones = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone = clones[0]
    assert clone.context["_attempt_number"] == 2
    assert clone.context["_max_attempts"] == 4
    assert clone.context["_previous_error"] == "Build failed: tests failing"
    assert clone.context.get("_previous_failure_class") is not None


@pytest.mark.asyncio
async def test_requeue_sets_parent_to_evaluating_and_increments_requeue_count(tl_db, mocker):
    """Parent status becomes EVALUATING and _requeue_count is incremented."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 1},
    )
    await _make_task(session, project.id, status=TaskStatus.FAILED, parent_task_id=parent.id)

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context["_requeue_count"] == 2
    assert parent.attempt_count == 1


@pytest.mark.asyncio
async def test_requeue_noop_with_completed_child_does_not_burn_budget(tl_db):
    """No FAILED children + has COMPLETED child → EVALUATING, budget untouched.

    Regression: manual retry of a COMPLETED research parent (whose children
    all merged but rollup PR is still open) used to burn a _requeue_count
    slot per call, eventually failing the parent at MAX_PARENT_REQUEUES=3.
    """
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 1},
    )
    await _make_task(
        session, project.id, status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    # Budget must NOT be consumed when nothing was retried
    assert parent.context["_requeue_count"] == 1
    assert parent.attempt_count == 0
    assert "No failed subtasks to retry" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_noop_with_active_child_does_not_burn_budget(tl_db):
    """No FAILED children + active child → EVALUATING, budget untouched."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session, project.id, status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent.id,
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context["_requeue_count"] == 2
    assert parent.attempt_count == 0


@pytest.mark.asyncio
async def test_requeue_all_cancelled_children_marks_parent_failed(tl_db):
    """All children CANCELLED (none FAILED/COMPLETED/active) → parent FAILED.

    Prevents reaper Step 4 from looping on a parent whose children are all
    cancelled — there is nothing to retry and nothing to roll up.
    """
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )
    await _make_task(
        session, project.id, status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "No failed subtasks to retry" in (parent.latest_status_message or "")
    # Must be a valid _SEVERITY key so worst_child_failure_category ranks it
    # correctly; the prior "UNKNOWN" literal silently fell back to CODE_ERROR.
    assert parent.context.get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_requeue_strips_max_plan_account_id_from_clone(tl_db, mocker):
    """max_plan_account_id (blacklisted account) is excluded from clone context."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"max_plan_account_id": "acct-blacklisted", "_attempt_number": 1},
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert "max_plan_account_id" not in clone.context


@pytest.mark.asyncio
async def test_requeue_agent_timeout_injects_scope_guidance(tl_db, mocker):
    """AGENT_TIMEOUT failure inserts timeout scope-reduction guidance into prompt_suffix."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="AGENT_TIMEOUT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="Agent timed out",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "TIMED OUT" in prompt_suffix


@pytest.mark.asyncio
async def test_requeue_scope_overflow_medium_escalates_to_large(tl_db, mocker):
    """SCOPE_OVERFLOW on a medium task escalates retry scope to 'large' (plan decomposition)."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="SCOPE_OVERFLOW")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1, "scope": "medium", "failure_category": "SCOPE_OVERFLOW"},
        latest_status_message="Scope overflow: 11 files changed",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert clone.context.get("scope") == "large"
    assert clone.context.get("_scope_reduced") is True
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "scope" in prompt_suffix.lower()
    assert "file-count budget" in prompt_suffix.lower()


@pytest.mark.asyncio
async def test_requeue_scope_overflow_small_does_not_escalate(tl_db, mocker):
    """SCOPE_OVERFLOW on a small task keeps the same scope — only medium escalates to large."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="SCOPE_OVERFLOW")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1, "scope": "small", "failure_category": "SCOPE_OVERFLOW"},
        latest_status_message="Scope overflow: 7 files changed for a small-scope task",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    # Scope stays small; guidance is still added
    assert clone.context.get("scope") == "small"
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "file-count budget" in prompt_suffix.lower()


@pytest.mark.asyncio
async def test_requeue_scope_overflow_raw_lowercase_label_escalates(tl_db, mocker):
    """Raw lowercase 'scope_overflow' in ctx (pre-normalization) also triggers escalation."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    # Simulate a case where classify_failure returned a different class but the raw
    # agent label is still present in ctx (defensive path).
    mocker.patch("app.task_lifecycle.classify_failure", return_value="CODE_ERROR")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1, "scope": "medium", "failure_category": "scope_overflow"},
        latest_status_message="Scope overflow: too many files",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert clone.context.get("scope") == "large"


@pytest.mark.asyncio
async def test_requeue_scope_overflow_injects_module_distribution(tl_db, mocker):
    """SCOPE_OVERFLOW retry surfaces module distribution and expansion ratio to the plan agent."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="SCOPE_OVERFLOW")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
            "_pr_changed_files": [
                "backend/app/orchestrator.py",
                "backend/app/review_pipeline.py",
                "backend/app/task_lifecycle.py",
                "agent/src/handlers/standard.ts",
                "agent/src/prompt.ts",
                "admin-ui/src/components/DashboardHome.tsx",
            ],
        },
        latest_status_message="Scope overflow: 6 non-test files changed",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "OVERFLOW DETAILS FROM PREVIOUS ATTEMPT" in prompt_suffix
    assert "backend/app=3" in prompt_suffix
    assert "agent/src=2" in prompt_suffix
    assert "admin-ui/src=1" in prompt_suffix
    assert "module boundaries" in prompt_suffix


def test_build_scope_overflow_details_returns_empty_without_changed_files():
    """No _previous_changed_files → no overflow block (caller keeps the generic guidance)."""
    assert _build_scope_overflow_details({}, "medium") == ""
    assert _build_scope_overflow_details({"_previous_changed_files": []}, "medium") == ""


def test_build_scope_overflow_details_reports_expansion_ratio():
    """Expansion ratio is computed against SCOPE_OVERFLOW_THRESHOLDS for the failed scope."""
    ctx = {
        "_previous_changed_files": [f"backend/app/f{i}.py" for i in range(15)],
    }
    block = _build_scope_overflow_details(ctx, "medium")
    assert "Files touched: 15" in block
    # medium threshold is 10 -> over by 5, 1.5x budget
    assert "medium=10" in block
    assert "over by 5" in block
    assert "1.5x" in block


def test_build_scope_overflow_details_single_module_omits_split_hint():
    """When all files live in one module, the split-along-boundaries hint is skipped."""
    ctx = {
        "_previous_changed_files": [
            "backend/app/a.py", "backend/app/b.py", "backend/app/c.py",
        ],
    }
    block = _build_scope_overflow_details(ctx, "small")
    assert "backend/app=3" in block
    assert "module boundaries" not in block


@pytest.mark.asyncio
async def test_requeue_external_api_injects_api_guidance(tl_db, mocker):
    """EXTERNAL_API failure inserts API-specific retry guidance into prompt_suffix."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="EXTERNAL_API")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="GitHub API returned 503 Service Unavailable",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "GitHub API" in prompt_suffix
    assert "503" in prompt_suffix
    assert "backoff" in prompt_suffix.lower()


@pytest.mark.asyncio
async def test_requeue_rate_limit_injects_rate_limit_guidance(tl_db, mocker):
    """RATE_LIMIT failure inserts rate-limit retry guidance into prompt_suffix."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="RATE_LIMIT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="429 Too Many Requests",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "rate limit" in prompt_suffix.lower()
    assert "backoff" in prompt_suffix.lower()


@pytest.mark.asyncio
async def test_requeue_remaps_depends_on_to_new_clone_ids(tl_db, mocker):
    """depends_on entries pointing to cancelled children are remapped to their clones."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    child_a = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task A"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        created_by="ORCHESTRATOR",
    )
    session.add(child_a)
    await session.flush()

    child_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "task B depends on A"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        depends_on=[str(child_a.id)],
        created_by="ORCHESTRATOR",
    )
    session.add(child_b)
    await session.commit()

    await _requeue_parent_task(parent, session)

    clones = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 2

    # The clone of child_b should depend on the clone of child_a, not on child_a itself
    clone_b = next((c for c in clones if c.depends_on), None)
    clone_a = next((c for c in clones if not c.depends_on), None)
    assert clone_b is not None
    assert clone_a is not None
    assert str(child_a.id) not in clone_b.depends_on
    assert str(clone_a.id) in clone_b.depends_on


@pytest.mark.asyncio
async def test_requeue_skips_non_failed_children(tl_db, mocker):
    """COMPLETED and QUEUED children are left untouched; only FAILED ones are retried."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    completed_child = await _make_task(
        session, project.id, status=TaskStatus.COMPLETED, parent_task_id=parent.id
    )
    queued_child = await _make_task(
        session, project.id, status=TaskStatus.QUEUED, parent_task_id=parent.id
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(completed_child)
    await session.refresh(queued_child)
    assert completed_child.status == TaskStatus.COMPLETED
    assert queued_child.status == TaskStatus.QUEUED


# ══════════════════════════════════════════════════════════════════════════════
# _requeue_parent_task — enriched failure diagnosis
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_requeue_enriches_clone_context_with_fingerprint_and_stage(tl_db, mocker):
    """Clone context includes error fingerprint and failure stage for CODE_ERROR at test stage."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest failed: assertion error on line 42",
    )

    await _requeue_parent_task(parent, session)

    clones = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone = clones[0]

    # Fingerprint should be a 12-char hex string
    fingerprint = clone.context.get("_previous_error_fingerprint")
    assert fingerprint is not None
    assert len(fingerprint) == 12
    assert all(c in "0123456789abcdef" for c in fingerprint)

    # Stage should be derived as "test" because status message contains "pytest"
    assert clone.context.get("_previous_failure_stage") == "test"

    # Error summary should contain first 500 chars (CODE_ERROR)
    summary = clone.context.get("_previous_error_summary")
    assert summary is not None
    assert "pytest failed" in summary


@pytest.mark.asyncio
async def test_requeue_enriches_clone_context_provision_stage(tl_db, mocker):
    """Clone context has _previous_failure_stage=provision for PROVISION_FAILURE."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="PROVISION_FAILURE")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="vm failed to start",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert clone.context.get("_previous_failure_stage") == "provision"


@pytest.mark.asyncio
async def test_requeue_sets_recurring_flag_when_anomaly_exceeds_threshold(tl_db, mocker):
    """_previous_is_recurring=True when persistent_anomalies has CODE_ERROR with count >= 3."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest: test_foo FAILED",
    )

    # Inject a persistent anomaly for CODE_ERROR with high occurrence count
    import app.shared_state as _shared_state
    _shared_state.persistent_anomalies[str(project.id)] = {
        "CODE_ERROR": {"occurrence_count": 5, "severity": "high", "latest_details": "test"}
    }
    try:
        await _requeue_parent_task(parent, session)
    finally:
        _shared_state.persistent_anomalies.pop(str(project.id), None)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert clone.context.get("_previous_is_recurring") is True


@pytest.mark.asyncio
async def test_requeue_does_not_set_recurring_flag_below_threshold(tl_db, mocker):
    """_previous_is_recurring is absent when occurrence_count < 3."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest: test_bar FAILED",
    )

    import app.shared_state as _shared_state
    _shared_state.persistent_anomalies[str(project.id)] = {
        "CODE_ERROR": {"occurrence_count": 2, "severity": "medium", "latest_details": "test"}
    }
    try:
        await _requeue_parent_task(parent, session)
    finally:
        _shared_state.persistent_anomalies.pop(str(project.id), None)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert "_previous_is_recurring" not in clone.context


@pytest.mark.asyncio
async def test_requeue_no_error_summary_for_non_code_error(tl_db, mocker):
    """_previous_error_summary is not injected for AGENT_TIMEOUT failures."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="AGENT_TIMEOUT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="Agent timed out",
    )

    await _requeue_parent_task(parent, session)

    clone = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()
    assert clone is not None
    assert "_previous_error_summary" not in clone.context


# ══════════════════════════════════════════════════════════════════════════════
# _derive_failure_stage
# ══════════════════════════════════════════════════════════════════════════════

def test_derive_failure_stage_provision():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("PROVISION_FAILURE", "vm failed to start") == "provision"


def test_derive_failure_stage_test_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "pytest: FAILED assert x == y") == "test"


def test_derive_failure_stage_lint_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "ruff: E501 line too long") == "lint"


def test_derive_failure_stage_build_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "build failed: cannot compile module") == "build"


def test_derive_failure_stage_clone_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "git clone failed: repository not found") == "clone"


def test_derive_failure_stage_pr_creation():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "failed to create pr: branch not found") == "pr_creation"


def test_derive_failure_stage_merge_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "merge conflict in app.py") == "merge"
    assert _derive_failure_stage("CODE_ERROR", "PR not mergeable") == "merge"
    assert _derive_failure_stage("CODE_ERROR", "branch is behind main") == "merge"


def test_derive_failure_stage_review_keyword():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("CODE_ERROR", "reviewer rejected changes") == "review"
    assert _derive_failure_stage("CODE_ERROR", "changes requested on PR") == "review"
    assert _derive_failure_stage("CODE_ERROR", "review dismissed by author") == "review"


def test_derive_failure_stage_unknown():
    from app.task_lifecycle import _derive_failure_stage
    assert _derive_failure_stage("AGENT_TIMEOUT", "No heartbeat for 300s") is None


# ══════════════════════════════════════════════════════════════════════════════
# _merge_with_retry
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_merge_with_retry_succeeds_on_first_attempt():
    """Returns the merge result immediately when the first call succeeds."""
    mock_merge = AsyncMock(return_value={"merged": True, "sha": "abc123"})
    result = await _merge_with_retry(mock_merge, "gh_token", "owner", "repo", 42, "proj-id")
    assert result == {"merged": True, "sha": "abc123"}
    assert mock_merge.await_count == 1


@pytest.mark.asyncio
async def test_merge_with_retry_retries_on_exception_and_returns_success(mocker):
    """Retries after a transient exception and returns the successful result."""
    mocker.patch("asyncio.sleep", new=AsyncMock())
    call_count = 0

    async def _flaky_merge(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient network error")
        return {"merged": True}

    result = await _merge_with_retry(_flaky_merge, "token", "owner", "repo", 10, "proj")
    assert result == {"merged": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_merge_with_retry_raises_last_exception_after_max_attempts(mocker):
    """Raises the last exception after exhausting all retry attempts."""
    mocker.patch("asyncio.sleep", new=AsyncMock())

    async def _always_fails(*args, **kwargs):
        raise RuntimeError("persistent error")

    with pytest.raises(RuntimeError, match="persistent error"):
        await _merge_with_retry(
            _always_fails, "token", "owner", "repo", 99, "proj", max_attempts=3
        )


@pytest.mark.asyncio
async def test_merge_with_retry_returns_non_merged_without_retry():
    """A non-exception non-merged result (e.g., conflict) is returned on the first call."""
    call_count = 0

    async def _conflict_merge(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"merged": False, "message": "Merge conflict detected"}

    result = await _merge_with_retry(_conflict_merge, "token", "owner", "repo", 5, "proj")
    assert result == {"merged": False, "message": "Merge conflict detected"}
    assert call_count == 1


@pytest.mark.asyncio
async def test_merge_with_retry_respects_max_attempts_parameter(mocker):
    """custom max_attempts controls how many times the merge is attempted."""
    mocker.patch("asyncio.sleep", new=AsyncMock())
    call_count = 0

    async def _always_fails(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ValueError("fail each time")

    with pytest.raises(ValueError):
        await _merge_with_retry(
            _always_fails, "token", "owner", "repo", 1, "proj", max_attempts=2
        )
    assert call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# _comment_with_retry
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_comment_with_retry_succeeds_on_first_attempt():
    """Returns immediately after a successful first call, passing args through."""
    mock_comment = AsyncMock(return_value=None)
    result = await _comment_with_retry(
        mock_comment, "gh_token", "owner", "repo", 42, "hello body"
    )
    assert result is None
    assert mock_comment.await_count == 1
    mock_comment.assert_awaited_once_with("gh_token", "owner", "repo", 42, "hello body")


@pytest.mark.asyncio
async def test_comment_with_retry_retries_on_exception_then_succeeds(mocker):
    """Retries after a transient exception and returns on the subsequent success."""
    mocker.patch("asyncio.sleep", new=AsyncMock())
    call_count = 0

    async def _flaky_comment(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("transient network error")
        return None

    result = await _comment_with_retry(
        _flaky_comment, "token", "owner", "repo", 10, "body"
    )
    assert result is None
    assert call_count == 2


@pytest.mark.asyncio
async def test_comment_with_retry_swallows_error_after_max_attempts(mocker):
    """After exhausting retries, the final exception is swallowed (returns None)."""
    mocker.patch("asyncio.sleep", new=AsyncMock())
    call_count = 0

    async def _always_fails(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("persistent error")

    result = await _comment_with_retry(
        _always_fails, "token", "owner", "repo", 99, "body", max_attempts=3
    )
    assert result is None
    assert call_count == 3


@pytest.mark.asyncio
async def test_comment_with_retry_respects_max_attempts_parameter(mocker):
    """Custom max_attempts controls how many times the comment is attempted."""
    mocker.patch("asyncio.sleep", new=AsyncMock())
    call_count = 0

    async def _always_fails(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ValueError("fail each time")

    result = await _comment_with_retry(
        _always_fails, "token", "owner", "repo", 1, "body", max_attempts=2
    )
    assert result is None
    assert call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# _process_post_commit
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_process_post_commit_task_not_found_returns_accepted(tl_db):
    """Returns {"status": "accepted"} when the task_id does not exist in the DB."""
    _, factory = tl_db
    nonexistent_id = uuid.uuid4()
    payload = AgentStatusPayload(task_id=nonexistent_id, status=TaskStatus.COMPLETED)

    result = await _process_post_commit(nonexistent_id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}


@pytest.mark.asyncio
async def test_process_post_commit_completed_child_triggers_rollup_check(tl_db, mocker):
    """A COMPLETED child task with a parent causes _check_and_trigger_rollup to be called."""
    mock_rollup = mocker.patch(
        "app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock
    )
    session, factory = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(
        task_id=child.id,
        status=TaskStatus.COMPLETED,
        # No pr_url → skip review pipeline
    )

    result = await _process_post_commit(child.id, payload, _session_factory=factory)

    assert result["status"] == "received"
    mock_rollup.assert_awaited_once()
    # The task passed to rollup is the child task
    rollup_call_task = mock_rollup.call_args[0][0]
    assert str(rollup_call_task.id) == str(child.id)


# ══════════════════════════════════════════════════════════════════════════════
# _is_transient_scan_error
# ══════════════════════════════════════════════════════════════════════════════

def test_is_transient_scan_error_timeout():
    assert _is_transient_scan_error(TimeoutError("connection timed out")) is True


def test_is_transient_scan_error_connection():
    assert _is_transient_scan_error(ConnectionError("failed to connect")) is True


def test_is_transient_scan_error_503():
    assert _is_transient_scan_error(Exception("503 service unavailable")) is True


def test_is_transient_scan_error_non_transient():
    assert _is_transient_scan_error(ValueError("invalid token")) is False


def test_is_transient_scan_error_permission():
    assert _is_transient_scan_error(PermissionError("access denied")) is False


# ══════════════════════════════════════════════════════════════════════════════
# Security scan retry logic (via _process_post_commit)
# ══════════════════════════════════════════════════════════════════════════════

async def _make_task_with_pr(session, project_id, *, scope="small"):
    """Helper: create a task that will trigger the small-scope security scan path."""
    task = Task(
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "do create", "effort": scope}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        context={"scope": scope},
        depends_on=[],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_scan_transient_retry_success(tl_db, mocker):
    """Transient scan error on first attempt, success on retry — no requires_approval."""
    session, factory = tl_db
    project = await _make_project(session)
    # Scope must be "small" in context; base_branch must start with "research/" for is_intermediate path
    task = await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    # Patch the payloads to include base_branch so branch routing picks intermediate path
    task.payloads = [{"type": "create", "prompt": "do create", "base_branch": "research/cycle-1"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    clean_result = {"blocked": False, "findings": [], "summary": "clean", "report": ""}

    call_count = 0
    async def _scan_side_effect(*_args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("connection timed out")
        return clean_result

    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.routers.agent.get_github_token_for_task", new_callable=AsyncMock,
                 return_value=("gh-token", "owner", "repo"))
    mocker.patch("app.security.scan_pr_for_injection", side_effect=_scan_side_effect)
    # Merge succeeds
    mocker.patch("app.task_lifecycle._merge_with_retry", new_callable=AsyncMock,
                 return_value={"merged": True})
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/42",
        branch="blueprint/task-abc123",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result["status"] in ("accepted", "received")
    assert call_count == 2  # first attempt failed, retry succeeded

    await session.refresh(task)
    assert not task.requires_approval
    assert (task.context or {}).get("scan_transient_failure") is not True


@pytest.mark.asyncio
async def test_scan_transient_retry_exhausted(tl_db, mocker):
    """Both scan attempts fail transiently — sets requires_approval and scan_transient_failure flag."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session, project.id, task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    task.payloads = [{"type": "create", "prompt": "do create", "base_branch": "research/cycle-1"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("app.routers.agent.get_github_token_for_task", new_callable=AsyncMock,
                 return_value=("gh-token", "owner", "repo"))
    mocker.patch("app.security.scan_pr_for_injection",
                 side_effect=TimeoutError("connection timed out"))

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/42",
        branch="blueprint/task-abc123",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}

    await session.refresh(task)
    assert task.requires_approval is True
    assert (task.context or {}).get("scan_transient_failure") is True
    assert "infrastructure error" in (task.latest_status_message or "")


# ══════════════════════════════════════════════════════════════════════════════
# _record_parent_failure
# ══════════════════════════════════════════════════════════════════════════════

def test_record_parent_failure_with_direction_id_calls_both(mocker):
    """direction_id in context → both record_task_result and record_direction_result."""
    mock_task_result = mocker.patch("app.routers.agent_status.record_task_result")
    mock_dir_result = mocker.patch("app.shared_state.record_direction_result")

    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    parent = Task(
        id=task_id,
        project_id=project_id,
        payloads=[{"type": "create", "prompt": "do it"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={"direction_id": "d-abc-123"},
    )

    _record_parent_failure(parent, "CODE_ERROR")

    mock_task_result.assert_called_once_with(
        success=False,
        failure_class="CODE_ERROR",
        task_id=str(task_id),
        task_type="create",
    )
    mock_dir_result.assert_called_once_with(
        "d-abc-123",
        success=False,
        failure_class="CODE_ERROR",
        project_id=str(project_id),
    )


def test_record_parent_failure_without_direction_id_skips_direction_result(mocker):
    """No direction_id → only record_task_result is called."""
    mock_task_result = mocker.patch("app.routers.agent_status.record_task_result")
    mock_dir_result = mocker.patch("app.shared_state.record_direction_result")

    parent = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "research"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={},
    )

    _record_parent_failure(parent, "AGENT_TIMEOUT")

    mock_task_result.assert_called_once()
    mock_dir_result.assert_not_called()


def test_record_parent_failure_empty_direction_id_skips_direction_result(mocker):
    """Empty-string direction_id is falsy → record_direction_result is skipped."""
    mock_task_result = mocker.patch("app.routers.agent_status.record_task_result")
    mock_dir_result = mocker.patch("app.shared_state.record_direction_result")

    parent = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[{"type": "plan"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={"direction_id": ""},
    )

    _record_parent_failure(parent, "CODE_ERROR")

    mock_task_result.assert_called_once()
    mock_dir_result.assert_not_called()


def test_record_parent_failure_defaults_task_type_when_payload_missing(mocker):
    """Empty payloads → task_type forwarded as empty string (not raised)."""
    mock_task_result = mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")

    parent = Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        payloads=[],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={},
    )

    _record_parent_failure(parent, "CODE_ERROR")

    _, kwargs = mock_task_result.call_args
    assert kwargs["task_type"] == ""


# ── _extract_failing_tests / _extract_error_files ──────────────────────────────

class TestExtractFailingTests:
    def test_pytest_failed_line(self):
        assert _extract_failing_tests("FAILED tests/test_foo.py::test_bar") == [
            "tests/test_foo.py::test_bar"
        ]

    def test_jest_vitest_fail_line(self):
        assert _extract_failing_tests("FAIL src/components/App.test.tsx") == [
            "src/components/App.test.tsx"
        ]

    def test_generic_error_line_truncated(self):
        long = "Error: " + "x" * 200
        result = _extract_failing_tests(long)
        assert len(result) == 1
        assert len(result[0]) == 120
        assert result[0].startswith("Error: x")

    def test_deduplicates_repeats(self):
        text = "FAILED tests/test_a.py::test_x\nFAILED tests/test_a.py::test_x\n"
        assert _extract_failing_tests(text) == ["tests/test_a.py::test_x"]

    def test_caps_at_ten(self):
        lines = "\n".join(f"FAILED tests/test_{i}.py::test_x" for i in range(15))
        assert len(_extract_failing_tests(lines)) == 10

    def test_empty_input(self):
        assert _extract_failing_tests("") == []

    def test_mixed_formats(self):
        text = (
            "FAILED tests/test_a.py::test_x\n"
            "FAIL src/b.test.ts\n"
            "Error: boom\n"
        )
        result = _extract_failing_tests(text)
        assert "tests/test_a.py::test_x" in result
        assert "src/b.test.ts" in result
        assert any(r.startswith("Error: boom") for r in result)


class TestExtractErrorFiles:
    def test_python_traceback(self):
        assert _extract_error_files('File "backend/app/main.py", line 42') == [
            "backend/app/main.py"
        ]

    def test_typescript_error(self):
        assert _extract_error_files("src/index.ts:15:3") == ["src/index.ts"]

    def test_path_without_separator_skipped(self):
        assert _extract_error_files("file.ts:1:1") == []

    def test_empty_input(self):
        assert _extract_error_files("") == []

    def test_deduplicates_repeats(self):
        text = 'File "a/b.py", line 1\nFile "a/b.py", line 2\n'
        assert _extract_error_files(text) == ["a/b.py"]

    def test_caps_at_five(self):
        text = "\n".join(f'File "a/f{i}.py", line 1' for i in range(8))
        assert len(_extract_error_files(text)) == 5

    def test_mixed_patterns(self):
        text = (
            'File "backend/app/main.py", line 42\n'
            "src/index.ts:15:3 error\n"
        )
        result = _extract_error_files(text)
        assert "backend/app/main.py" in result
        assert "src/index.ts" in result


# ── _build_retry_prompt_suffix ────────────────────────────────────────────────

class TestBuildRetryPromptSuffix:
    """Pure-function tests for the retry prompt-suffix builder.

    Covers each failure_class branch, the scope override conditions for
    AGENT_TIMEOUT/SCOPE_OVERFLOW, keyword-based hints and context extraction
    for CODE_ERROR, service/status detection for EXTERNAL_API, and the
    fallback path for None/unknown failure classes.
    """

    def test_agent_timeout_attempt_one_no_scope_override(self):
        guidance, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", "boom", attempt=1, task_scope="large", ctx={}
        )
        assert "TIMED OUT" in guidance
        assert "reduce scope" in guidance
        assert override is None

    def test_agent_timeout_large_downgrades_to_medium_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", None, attempt=2, task_scope="large", ctx={}
        )
        assert override == "medium"

    def test_agent_timeout_medium_downgrades_to_small_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", None, attempt=3, task_scope="medium", ctx={}
        )
        assert override == "small"

    def test_agent_timeout_small_no_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", None, attempt=2, task_scope="small", ctx={}
        )
        assert override is None

    def test_scope_overflow_medium_escalates_to_large(self):
        guidance, override = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW", None, attempt=1, task_scope="medium", ctx={}
        )
        assert "scope's file-count budget" in guidance
        assert override == "large"

    def test_scope_overflow_small_does_not_escalate(self):
        _, override = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW", None, attempt=1, task_scope="small", ctx={}
        )
        assert override is None

    def test_scope_overflow_includes_overflow_details_when_changed_files_present(self):
        ctx = {
            "_previous_changed_files": [
                "backend/app/a.py", "backend/app/b.py", "agent/src/c.ts",
            ],
        }
        guidance, _ = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW", None, attempt=1, task_scope="medium", ctx=ctx
        )
        assert "OVERFLOW DETAILS FROM PREVIOUS ATTEMPT" in guidance
        assert "backend/app=2" in guidance
        assert "agent/src=1" in guidance

    def test_scope_overflow_raw_lowercase_label_via_failure_category(self):
        """Path matches when failure_class is missing but ctx has lowercase label."""
        _, override = _build_retry_prompt_suffix(
            None, None, attempt=1, task_scope="medium",
            ctx={"failure_category": "scope_overflow"},
        )
        assert override == "large"

    def test_code_error_test_keyword_hint(self):
        guidance, override = _build_retry_prompt_suffix(
            "CODE_ERROR", "pytest assertion failed in test_foo",
            attempt=1, task_scope="small", ctx={},
        )
        assert "failing tests" in guidance.lower()
        assert override is None

    def test_code_error_lint_keyword_hint(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "ruff E501 line too long; eslint warning",
            attempt=1, task_scope="small", ctx={},
        )
        assert "lint/type errors" in guidance

    def test_code_error_no_hint_when_no_keywords(self):
        """No test/lint keywords → no RETRY GUIDANCE prefix from hints."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "something unexpected happened",
            attempt=1, task_scope="small", ctx={},
        )
        assert "RETRY GUIDANCE" not in guidance

    def test_code_error_attempt_one_no_scope_override(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR", "boom", attempt=1, task_scope="large", ctx={},
        )
        assert override is None

    def test_code_error_large_downgrades_to_medium_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR", "boom", attempt=2, task_scope="large", ctx={},
        )
        assert override == "medium"

    def test_code_error_medium_downgrades_to_small_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR", "boom", attempt=2, task_scope="medium", ctx={},
        )
        assert override == "small"

    def test_code_error_small_no_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR", "boom", attempt=3, task_scope="small", ctx={},
        )
        assert override is None

    def test_code_error_extracts_previous_failing_tests_and_error_files(self):
        ctx = {
            "_previous_failing_tests": ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
            "_previous_error_files": ["backend/app/main.py", "agent/src/index.ts"],
        }
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "test failure: assert", attempt=1, task_scope="small", ctx=ctx,
        )
        assert "Previously failing tests:" in guidance
        assert "tests/test_a.py::test_x" in guidance
        assert "tests/test_b.py::test_y" in guidance
        assert "Error file locations:" in guidance
        assert "backend/app/main.py" in guidance
        assert "agent/src/index.ts" in guidance

    def test_code_error_stage_test_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "something unexpected", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "test"},
        )
        assert "test stage" in guidance
        assert "different approach" in guidance

    def test_code_error_stage_lint_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "something unexpected", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "lint"},
        )
        assert "lint stage" in guidance
        assert "type errors" in guidance

    def test_code_error_stage_build_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "something unexpected", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "build"},
        )
        assert "build stage" in guidance
        assert "import paths" in guidance

    def test_code_error_stage_merge_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "something unexpected", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "merge"},
        )
        assert "merge stage" in guidance
        assert "merge conflicts" in guidance

    def test_code_error_stage_takes_precedence_over_keyword(self):
        """When stage is set, use stage guidance instead of keyword-matched hints."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "pytest assertion failed", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "lint"},
        )
        assert "lint stage" in guidance
        assert "failing tests" not in guidance.lower()

    def test_code_error_unknown_stage_falls_back_to_keyword(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "pytest assertion failed", attempt=1, task_scope="small",
            ctx={"_previous_failure_stage": "provision"},
        )
        assert "failing tests" in guidance.lower()

    def test_agent_timeout_oom_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", "exit code 137 out of memory",
            attempt=1, task_scope="medium", ctx={},
        )
        assert "out-of-memory" in guidance
        assert "memory" in guidance.lower()

    def test_agent_timeout_heartbeat_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", "No heartbeat received after 300s",
            attempt=1, task_scope="medium", ctx={},
        )
        assert "heartbeat" in guidance.lower()
        assert "smaller" in guidance.lower()

    def test_agent_timeout_iteration_limit_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", "review-fix cycle limit reached",
            attempt=1, task_scope="medium", ctx={},
        )
        assert "iteration" in guidance.lower()
        assert "narrow scope" in guidance.lower()

    def test_agent_timeout_cascade_no_scope_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT", "Cancelled: parent evaluation timeout",
            attempt=3, task_scope="medium", ctx={},
        )
        assert override is None

    def test_code_error_same_tests_warning_on_overlap(self):
        ctx = {"_previous_failing_tests": ["tests/test_a.py::test_x"]}
        status = "pytest FAILED tests/test_a.py::test_x\nassert 1 == 2"
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", status, attempt=2, task_scope="small", ctx=ctx,
        )
        assert "same tests are failing" in guidance.lower()
        assert "tests/test_a.py::test_x" in guidance
        assert "Previously failing tests:" in guidance

    def test_previous_is_recurring_surfaces_in_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "pytest failed", attempt=2, task_scope="small",
            ctx={"_previous_is_recurring": True},
        )
        assert "CROSS-ATTEMPT CONTEXT" in guidance
        assert "recurred" in guidance.lower()

    def test_previous_self_review_outcome_surfaces(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR", "err", attempt=2, task_scope="small",
            ctx={"_previous_self_review_outcome": "missed edge case in parser"},
        )
        assert "self-review" in guidance.lower()
        assert "missed edge case" in guidance

    def test_external_api_github_with_status_code(self):
        guidance, override = _build_retry_prompt_suffix(
            "EXTERNAL_API", "GitHub API returned 503 Service Unavailable",
            attempt=1, task_scope="small", ctx={},
        )
        assert "GitHub API error" in guidance
        assert "(status 503)" in guidance
        assert "exponential backoff" in guidance
        assert override is None

    def test_external_api_anthropic_detected_from_claude_keyword(self):
        guidance, _ = _build_retry_prompt_suffix(
            "EXTERNAL_API", "Claude returned 429", attempt=1, task_scope="small", ctx={},
        )
        assert "Claude API error" in guidance
        assert "(status 429)" in guidance

    def test_external_api_generic_without_status_code(self):
        guidance, _ = _build_retry_prompt_suffix(
            "EXTERNAL_API", "network unreachable", attempt=1, task_scope="small", ctx={},
        )
        assert "external API error" in guidance
        assert "(status" not in guidance

    def test_rate_limit_static_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            "RATE_LIMIT", None, attempt=1, task_scope="small", ctx={},
        )
        assert "rate limit" in guidance.lower()
        assert "Retry-After" in guidance
        assert override is None

    def test_unknown_failure_class_returns_empty_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            "MYSTERY_FAILURE", "anything", attempt=1, task_scope="small", ctx={},
        )
        assert guidance == ""
        assert override is None

    def test_none_failure_class_returns_empty_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            None, None, attempt=1, task_scope="small", ctx={},
        )
        assert guidance == ""
        assert override is None
