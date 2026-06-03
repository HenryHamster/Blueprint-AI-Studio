"""
Tests for backend/app/task_lifecycle.py.

Covers the critical functions:
  1. _has_active_strategic_task — active strategic task guard
  2. _increment_research_spawn_count — vision spawn counter management
  3. _requeue_parent_task — failed child retry with context injection
  4. _merge_with_retry — PR merge with exponential backoff
  5. _process_post_commit — task not found guard and rollup triggering
"""

import ast
import contextlib
import importlib
import os
import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import Project, Task, TaskStatus, ArtifactType, Alert, AlertSeverity
from app.schemas import AgentStatusPayload
from app.task_lifecycle import (
    _has_active_strategic_task,
    _increment_research_spawn_count,
    _mark_dependents_warm_claimable,
    _requeue_parent_task,
    _merge_with_retry,
    _comment_with_retry,
    _process_post_commit,
    _record_parent_failure,
    _is_transient_scan_error,
    _resolve_fallback_verdict,
)
from app.retry_prompt import (
    _extract_failing_tests,
    _extract_error_files,
    _build_scope_overflow_details,
    _build_retry_prompt_suffix,
    merge_retry_prompt_suffix,
)

# SQLite JSONB compatibility — defensive, conftest.py also sets this
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler

if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
    _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
        type_, **kw
    )


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
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    # Patch AsyncSessionLocal used by the inner dep-unblocking session
    mocker.patch("app.task_lifecycle.AsyncSessionLocal", _Session)
    # Suppress broadcast calls
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    # Suppress failure alert creation (deferred import from app.main)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_project(session, *, build_config: dict | None = None) -> Project:
    project = Project(name="Test Project", user_id="user-1", build_config=build_config)
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
    # Audit W1: extract promoted columns from context for legacy fixtures.
    ctx = dict(context or {})
    extra: dict = {}
    if "_previous_error_fingerprint" in ctx:
        extra["previous_error_fingerprint"] = ctx.pop("_previous_error_fingerprint")
    if "auto_remediation" in ctx:
        extra["auto_remediation"] = bool(ctx.pop("auto_remediation"))
    task = Task(
        project_id=project_id,
        payloads=[{"type": task_type, "prompt": f"do {task_type}"}],
        status=status,
        created_by="ORCHESTRATOR",
        parent_task_id=parent_task_id,
        context=ctx,
        depends_on=depends_on or [],
        latest_status_message=latest_status_message,
        vm_instance_id=vm_instance_id,
        **extra,
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
    await _make_task(
        session, project.id, task_type="design_doc_update", status=TaskStatus.QUEUED
    )
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_true_for_strategic_review_in_progress(tl_db):
    """Returns True when a strategic_review task is IN_PROGRESS."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(
        session, project.id, task_type="strategic_review", status=TaskStatus.IN_PROGRESS
    )
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_true_for_provisioning(tl_db):
    """Returns True for a PROVISIONING strategic task."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(
        session,
        project.id,
        task_type="design_doc_update",
        status=TaskStatus.PROVISIONING,
    )
    assert await _has_active_strategic_task(project.id, session) is True


@pytest.mark.asyncio
async def test_has_active_strategic_task_false_for_non_strategic(tl_db):
    """Returns False when only non-strategic tasks are active."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(session, project.id, task_type="create", status=TaskStatus.QUEUED)
    await _make_task(
        session, project.id, task_type="research", status=TaskStatus.IN_PROGRESS
    )
    assert await _has_active_strategic_task(project.id, session) is False


@pytest.mark.asyncio
async def test_has_active_strategic_task_false_for_completed_strategic(tl_db):
    """Returns False when the strategic task is already COMPLETED (terminal)."""
    session, _ = tl_db
    project = await _make_project(session)
    await _make_task(
        session, project.id, task_type="design_doc_update", status=TaskStatus.COMPLETED
    )
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
async def test_increment_research_spawn_count_accumulates_and_preserves_other_keys(
    tl_db,
):
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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    # Child with CODE_ERROR persisted
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3, "direction_id": "d-test-42"},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
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
async def test_requeue_limit_blocks_when_all_failed_children_blocked_precondition(
    tl_db, mocker
):
    """Parent at the requeue limit whose only failed child is BLOCKED_PRECONDITION
    transitions to the recoverable BLOCKED state — not force-FAILED with a
    confabulated CODE_ERROR category — and raises a precondition-blocker alert.
    Mirrors the all-children-terminal sweep treatment in the limit path.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    # FAILED child whose status message routes to BLOCKED_PRECONDITION.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: upstream PR #42 not yet merged",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert parent.status != TaskStatus.FAILED
    # Budget-exempt precondition class retained for historical consumers.
    assert parent.context.get("failure_category") == "BLOCKED_PRECONDITION"
    assert parent.blocker_kind == "upstream_dependency"
    # A precondition-blocker alert is surfaced.
    precondition_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.task_id == parent.id,
                    Alert.title.contains("Precondition blocker"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(precondition_alerts) == 1
    assert precondition_alerts[0].severity == AlertSeverity.WARNING


@pytest.mark.asyncio
async def test_requeue_limit_blocks_when_child_in_first_class_blocked_state(
    tl_db, mocker
):
    """Parent at the requeue limit with a first-class BLOCKED child (invisible to
    the old FAILED-only query) transitions to BLOCKED, not FAILED."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert (parent.context or {}).get("blocker", {}).get("source") == "parent"


@pytest.mark.asyncio
async def test_requeue_limit_fails_when_genuine_and_blocked_children_mixed(
    tl_db, mocker
):
    """Parent at the requeue limit with a genuine CODE_ERROR child alongside a
    blocked sibling still FAILS — with the worst GENUINE category, not the
    CODE_ERROR fallback derived from blocked/empty input."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    # Genuine failure with a known category.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"failure_category": "CODE_ERROR"},
        latest_status_message="tests failed",
    )
    # Blocked-on-precondition sibling — must not force the parent to BLOCKED.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context.get("failure_category") == "CODE_ERROR"


@pytest.mark.asyncio
async def test_requeue_limit_fails_when_genuine_cancelled_child_present(tl_db, mocker):
    """Parent at the requeue limit with a genuine (non-superseded) CANCELLED child
    still FAILS — a genuine cancellation prevents parking as BLOCKED."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"failure_detail": "review_cap_exhausted"},
        latest_status_message="Cancelled after review cap exhausted",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_requeue_limit_records_no_direction_failure_when_blocked(tl_db, mocker):
    """A parent that hits the requeue limit but parks as BLOCKED must NOT record a
    direction-health failure (the blocked verdict is budget-exempt)."""
    import app.shared_state as _ss
    from app.routers.agent_status import _recorded_task_ids

    _ss.direction_failure_streaks.clear()
    _recorded_task_ids.clear()
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3, "direction_id": "d-blocked-7"},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: upstream PR not yet merged",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert _ss.direction_failure_streaks.get("d-blocked-7") is None


@pytest.mark.asyncio
async def test_requeue_same_class_consecutive_failures_reduces_budget(tl_db):
    """Two consecutive CODE_ERROR failures reduce effective budget to 2."""
    session, _ = tl_db
    project = await _make_project(session)
    # requeue_count=2 is below base max (3) but at the reduced max — should fail.
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_previous_failure_class": "CODE_ERROR",
        },
        latest_status_message="tests failed",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_different_class_failures_preserve_full_budget(tl_db, mocker):
    """A class change between attempts preserves the full MAX_PARENT_REQUEUES=3 budget."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    # Current failure is CODE_ERROR, previous was SCOPE_OVERFLOW → different class.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_attempt_number": 2,
        },
        latest_status_message="tests failed",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # Budget intact: parent was requeued (EVALUATING), not failed.
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context["_requeue_count"] == 3


@pytest.mark.asyncio
async def test_requeue_alternating_pattern_in_history_reduces_budget(tl_db, mocker):
    """Alternating non-infra failure history (e.g. CODE_ERROR/AGENT_TIMEOUT/CODE_ERROR)
    shrinks the parent budget even when the immediately previous class differs.

    Covers compute_effective_max_requeues's history-based branch: counts repeats in
    _failure_class_history and trims max requeues by 1 once any non-infra class
    appears 2+ times. Without this branch, oscillating failures would silently
    consume the full budget despite being just as recurring as same-class repeats.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    # Current failure: CODE_ERROR. Immediately previous: AGENT_TIMEOUT (different
    # class — would normally preserve budget). But _failure_class_history shows
    # CODE_ERROR has already occurred twice in recent attempts → trim by 1.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_previous_failure_class": "AGENT_TIMEOUT",
            "_failure_class_history": ["CODE_ERROR", "AGENT_TIMEOUT", "CODE_ERROR"],
            "_attempt_number": 3,
        },
        latest_status_message="tests failed again",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # Reduced budget (2) reached → parent FAILED instead of requeued.
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_recurring_failing_tests_reduces_budget(tl_db, mocker):
    """Same specific tests failing across attempts triggers an additional budget
    reduction on top of any class-based reduction, so deterministic test failures
    exit retry sooner rather than consuming the full CODE_ERROR budget.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    # Same test ID shows up in prior-attempt ctx and in the current status message.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_previous_failing_tests": ["tests/test_foo.py::test_bar"],
            "_attempt_number": 2,
        },
        latest_status_message="FAILED tests/test_foo.py::test_bar - AssertionError",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # With MAX_PARENT_REQUEUES=3 and _requeue_count=2, normally requeued (2<3).
    # The recurring-tests reduction brings effective max down to 2 → parent FAILED.
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_same_class_infra_exempt_from_budget_reduction(tl_db, mocker):
    """Same-class INFRA failures (e.g., PROVISION_FAILURE) are exempt from budget shrink."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "PROVISION_FAILURE",
            "_previous_failure_class": "PROVISION_FAILURE",
            "_attempt_number": 2,
        },
        latest_status_message="provisioning failed",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # INFRA class exempt → full budget preserved, parent requeued not failed.
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context["_requeue_count"] == 3


@pytest.mark.asyncio
async def test_requeue_history_count_three_reduces_budget_by_two(tl_db, mocker):
    """A non-infra class appearing 3+ times in _failure_class_history reduces
    the parent retry budget by 2 (not 1): pattern strongly suggests retries
    are not addressing the root cause.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    # _requeue_count=1 → with MAX=3 reduced by 2, effective=1, hit cap → FAILED.
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 1},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_previous_failure_class": "AGENT_TIMEOUT",
            "_failure_class_history": ["CODE_ERROR", "CODE_ERROR", "CODE_ERROR"],
            "_attempt_number": 4,
        },
        latest_status_message="tests failed yet again",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_repeated_scope_overflow_reduces_budget(tl_db, mocker):
    """SCOPE_OVERFLOW is in INFRA_CLASSES but repeated occurrences (2+) signal
    a persistent scope mismatch, not transient infra — reduce budget by 1.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    # _requeue_count=2 → with MAX=3 reduced by 1, effective=2, hit cap → FAILED.
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "SCOPE_OVERFLOW",
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_failure_class_history": [
                "SCOPE_OVERFLOW",
                "CODE_ERROR",
                "SCOPE_OVERFLOW",
            ],
            "_attempt_number": 3,
        },
        latest_status_message="scope overflow again",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_requeue_repeated_agent_timeout_reduces_budget(tl_db, mocker):
    """AGENT_TIMEOUT is in INFRA_CLASSES but repeated occurrences (2+) signal
    a scope/complexity mismatch with the runtime budget — reduce budget by 1.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "AGENT_TIMEOUT",
            "_previous_failure_class": "AGENT_TIMEOUT",
            "_failure_class_history": ["AGENT_TIMEOUT", "CODE_ERROR", "AGENT_TIMEOUT"],
            "_attempt_number": 3,
        },
        latest_status_message="agent timeout again",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit reached" in (parent.latest_status_message or "")


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
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    entries = list(_ss.deploy_monitoring["task_results"])
    matching = [e for e in entries if e[1] == "failure" and e[2] == "AGENT_TIMEOUT"]
    assert matching, f"expected an AGENT_TIMEOUT failure entry, got {entries}"


@pytest.mark.asyncio
async def test_requeue_all_children_blocked_escalates_precondition_alert(tl_db, mocker):
    """All-children-blocked parent raises ONE deduplicated precondition alert.

    When every FAILED child is BLOCKED_PRECONDITION (upstream dependency unmet),
    the parent transitions into the first-class BLOCKED state rather than a code
    failure, and a single operator-visible WARNING alert describing the upstream
    blocker is raised — not a silent generic FAILED. Re-running the requeue must
    NOT spam a second alert (deduplicated by task + blocker identity).
    """
    import app.shared_state as _ss
    from app.routers.agent_status import _recorded_task_ids

    _ss.deploy_monitoring["task_results"].clear()
    _recorded_task_ids.clear()
    # The precondition-blocker helper broadcasts via app.alert_dispatch; suppress
    # the socket call but keep the real (dedup) helper so we exercise idempotence.
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    # Single FAILED child whose status message routes to BLOCKED_PRECONDITION.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: upstream PR #42 not yet merged",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    # Parent retains the budget-exempt precondition class for compatibility with
    # historical consumers (digest metrics, analytics), even though it is no
    # longer a FAILED task.
    assert parent.context.get("failure_category") == "BLOCKED_PRECONDITION"
    # Durable blocker metadata is persisted on the task itself.
    blocker = parent.context.get("blocker") or {}
    assert blocker.get("source") == "parent"
    assert blocker.get("failure_class") == "BLOCKED_PRECONDITION"
    assert parent.blocker_kind == "upstream_dependency"
    assert parent.blocked_reason == parent.latest_status_message
    assert parent.blocked_at is not None
    assert parent.recheck_after is not None and parent.recheck_after > parent.blocked_at

    precondition_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.task_id == parent.id,
                    Alert.title.contains("Precondition blocker"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(precondition_alerts) == 1
    alert = precondition_alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "upstream dependency" in alert.title

    # Re-run: dedup must hold — still exactly one precondition alert.
    await _requeue_parent_task(parent, session)
    precondition_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.task_id == parent.id,
                    Alert.title.contains("Precondition blocker"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(precondition_alerts) == 1


def test_enter_blocked_state_persists_metadata_no_budget_burn():
    """enter_blocked_state moves a task to BLOCKED with durable metadata and
    preserves the BLOCKED_PRECONDITION class — without touching attempt_count."""
    from app.task_blocked import enter_blocked_state, get_blocker_metadata

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    enter_blocked_state(
        task,
        waiting_on="upstream dependency",
        reason="BLOCKER: dep not landed",
    )
    assert task.status == TaskStatus.BLOCKED
    # Retry budget untouched.
    assert task.attempt_count == 0
    # Classification preserved for historical/analytics consumers.
    assert task.context["failure_category"] == "BLOCKED_PRECONDITION"
    assert task.context["failure_class"] == "BLOCKED_PRECONDITION"
    meta = get_blocker_metadata(task)
    assert meta["waiting_on"] == "upstream dependency"
    assert meta["reason"] == "BLOCKER: dep not landed"
    assert meta["source"] == "agent"
    assert meta["blocker_kind"] == "upstream_dependency"
    assert meta["escalated"] is False
    assert meta["recheck_at"]  # recheck timing persisted
    assert task.blocker_kind == "upstream_dependency"
    assert task.blocked_reason == "BLOCKER: dep not landed"
    assert task.blocked_at is not None
    assert task.recheck_after is not None and task.recheck_after > task.blocked_at


def test_unblock_to_queued_clears_metadata_and_requeues():
    """unblock_to_queued is the BLOCKED -> QUEUED out-transition: it clears the
    blocker metadata and re-queues the task for dispatch."""
    from app.task_blocked import enter_blocked_state, unblock_to_queued

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.IN_PROGRESS,
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    assert task.status == TaskStatus.BLOCKED

    unblock_to_queued(task)
    assert task.status == TaskStatus.QUEUED
    assert "blocker" not in (task.context or {})
    assert task.blocker_kind is None
    assert task.blocked_reason is None
    assert task.blocked_at is None
    assert task.recheck_after is None


def test_mark_blocker_escalated_sets_marker():
    """mark_blocker_escalated flips the escalation marker and stamps a time."""
    from app.task_blocked import (
        enter_blocked_state,
        get_blocker_metadata,
        mark_blocker_escalated,
    )

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.IN_PROGRESS,
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    mark_blocker_escalated(task, detail="deadline elapsed")
    meta = get_blocker_metadata(task)
    assert meta["escalated"] is True
    assert meta["escalated_at"]
    assert meta["escalation_detail"] == "deadline elapsed"


@pytest.mark.asyncio
async def test_requeue_parent_blocked_when_child_in_blocked_state(tl_db, mocker):
    """A parent whose only unsucceeded child is in the first-class BLOCKED state
    transitions to BLOCKED (not FAILED) without burning its requeue budget."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert (parent.context or {}).get("blocker", {}).get("source") == "parent"
    # Requeue budget untouched — this was not a retry.
    assert (parent.context or {}).get("_requeue_count", 0) == 0


@pytest.mark.asyncio
async def test_requeue_parent_failed_when_blocked_and_genuine_cancelled_child_mixed(
    tl_db, mocker
):
    """A genuine CANCELLED child prevents the parent from parking as BLOCKED."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"failure_detail": "review_cap_exhausted"},
        latest_status_message="Cancelled after review cap exhausted",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.status != TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_requeue_parent_mixed_blocked_and_superseded_cancelled_not_parked(
    tl_db, mocker
):
    """A mix of genuinely-BLOCKED + non-genuine-cancelled children (no genuine
    failure) must NOT be coerced to fully-BLOCKED. Some children were superseded
    by a replacement clone, so "all subtasks blocked" is factually wrong — the
    parent is routed to EVALUATING for rollup re-evaluation instead, with no
    precondition-blocker alert and no requeue-budget burn."""
    alert = mocker.patch(
        "app.alert_dispatch._maybe_create_precondition_blocker_alert",
        new_callable=AsyncMock,
    )
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        status=TaskStatus.BLOCKED,
        parent_task_id=parent.id,
        latest_status_message="BLOCKER: dep not landed",
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
        latest_status_message="Parent requeued — replaced by retry",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # Not coerced to fully-BLOCKED — re-evaluated by rollup instead.
    assert parent.status == TaskStatus.EVALUATING
    assert parent.status != TaskStatus.BLOCKED
    assert "awaiting rollup re-evaluation" in (parent.latest_status_message or "")
    # Message must NOT claim all subtasks are blocked.
    assert "All 1 subtask" not in (parent.latest_status_message or "")
    # No precondition-blocker alert for a mixed set, and budget untouched.
    alert.assert_not_called()
    assert (parent.context or {}).get("_requeue_count", 0) == 0


@pytest.mark.asyncio
async def test_requeue_parent_blocked_only_when_purely_blocked(tl_db, mocker):
    """When EVERY unsucceeded child is genuinely BLOCKED (no cancellations), the
    parent still parks BLOCKED with an accurate count message."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    for _ in range(2):
        await _make_task(
            session,
            project.id,
            status=TaskStatus.BLOCKED,
            parent_task_id=parent.id,
            latest_status_message="BLOCKER: dep not landed",
        )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert (parent.context or {}).get("blocker", {}).get("source") == "parent"
    # Count message is factually accurate: 2 blocked subtasks.
    assert "All 2 subtask(s) blocked" in (parent.latest_status_message or "")
    assert (parent.context or {}).get("_requeue_count", 0) == 0


@pytest.mark.asyncio
async def test_requeue_cancels_failed_children_and_creates_clones(tl_db, mocker):
    """Failed children are cancelled and replaced with QUEUED clones."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        session,
        project.id,
        task_type="create",
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

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone = clones[0]
    assert clone.context["_attempt_number"] == 2
    assert clone.context["_max_attempts"] == 4
    assert clone.context["_previous_error"] == "Build failed: tests failing"
    assert clone.context.get("_previous_failure_class") is not None


@pytest.mark.asyncio
async def test_requeue_sets_parent_to_evaluating_and_increments_requeue_count(
    tl_db, mocker
):
    """Parent status becomes EVALUATING and _requeue_count is incremented."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 1},
    )
    await _make_task(
        session, project.id, status=TaskStatus.FAILED, parent_task_id=parent.id
    )

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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 1},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.COMPLETED,
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
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 2},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
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
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
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
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"max_plan_account_id": "acct-blacklisted", "_attempt_number": 1},
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert "max_plan_account_id" not in clone.context


@pytest.mark.asyncio
async def test_requeue_rate_limit_propagates_blacklist_to_clone(tl_db, mocker):
    """RATE_LIMIT failure: clone carries the failed account in blacklisted_accounts.

    Regression guard: _ctx_keys_to_strip previously cleared blacklisted_accounts on
    every clone, so the retry could re-pick the same account that just returned 429.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="RATE_LIMIT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "llm_account_id": "acct-rl-1234",
            "blacklisted_accounts": ["acct-rl-1234"],
        },
        latest_status_message="Usage limit reached",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    bl = clone.context.get("blacklisted_accounts") or []
    assert "acct-rl-1234" in bl, f"failed account missing from clone blacklist: {bl}"
    # llm_account_id must still be stripped (clone gets a fresh account selection)
    assert "llm_account_id" not in clone.context


@pytest.mark.asyncio
async def test_requeue_rate_limit_adds_account_to_existing_blacklist(tl_db, mocker):
    """RATE_LIMIT: failed account is appended if not already in prior blacklist."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="RATE_LIMIT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 2,
            "llm_account_id": "acct-new-fail",
            # prior blacklist from previous attempt
            "blacklisted_accounts": ["acct-old-fail"],
        },
        latest_status_message="Usage limit reached",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    bl = clone.context.get("blacklisted_accounts") or []
    assert "acct-old-fail" in bl
    assert "acct-new-fail" in bl


@pytest.mark.asyncio
async def test_requeue_non_rate_limit_does_not_preserve_blacklist(tl_db, mocker):
    """CODE_ERROR failures do not carry forward blacklisted_accounts — account was not at fault."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="CODE_ERROR")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "llm_account_id": "acct-xyz",
            "blacklisted_accounts": ["acct-xyz"],
        },
        latest_status_message="Tests failed",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert not clone.context.get("blacklisted_accounts"), (
        "blacklisted_accounts should be absent for CODE_ERROR (account was not at fault)"
    )


@pytest.mark.asyncio
async def test_requeue_agent_timeout_injects_scope_guidance(tl_db, mocker):
    """AGENT_TIMEOUT failure inserts timeout scope-reduction guidance into prompt_suffix."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="AGENT_TIMEOUT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="Agent timed out",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
        },
        latest_status_message="Scope overflow: 11 files changed",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert clone.context.get("scope") == "large"
    assert clone.context.get("_scope_reduced") is True
    assert (
        clone.context.get("scope_calibrated_from") == "medium:scope_overflow_escalation"
    )
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "scope" in prompt_suffix.lower()
    assert "file-count budget" in prompt_suffix.lower()


@pytest.mark.asyncio
async def test_requeue_scope_overflow_small_escalates_to_medium(tl_db, mocker):
    """SCOPE_OVERFLOW on a small task escalates to medium for a 10-file budget."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="SCOPE_OVERFLOW")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "scope": "small",
            "failure_category": "SCOPE_OVERFLOW",
        },
        latest_status_message="Scope overflow: 7 files changed for a small-scope task",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    # Scope escalates small→medium; guidance is still added
    assert clone.context.get("scope") == "medium"
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            "scope": "medium",
            "failure_category": "scope_overflow",
        },
        latest_status_message="Scope overflow: too many files",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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
        session,
        project.id,
        task_type="create",
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

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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
    assert (
        _build_scope_overflow_details({"_previous_changed_files": []}, "medium") == ""
    )


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
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="GitHub API returned 503 Service Unavailable",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""
    assert "GitHub API" in prompt_suffix
    assert "503" in prompt_suffix
    assert "backoff" in prompt_suffix.lower()


def test_merge_retry_prompt_suffix_strips_all_known_auto_prefixes_on_first_call():
    """First call snapshots `existing_suffix` with all known auto-blocks
    stripped; the snapshot is what carries forward verbatim across retries.

    Critical: covers the reaper-driven within-task retry path that was
    blindly prepending — observed on 911b3ed3, two `IMPORTANT: …TIMED OUT`
    blocks plus an old RETRY GUIDANCE block stacked from successive reaps.
    """
    ctx: dict = {}
    polluted = (
        "IMPORTANT: The previous attempt TIMED OUT — the agent stopped sending heartbeats. Keep operations smaller:\n"
        "- step\n"
        "\n"
        "RETRY GUIDANCE: prior block\n"
        "- detail\n"
        "\n"
        "ORIGINAL_NOTE: spawn-time note preserved.\n"
    )
    new_guidance = "RETRY GUIDANCE: fresh block\n- new detail\n"
    out = merge_retry_prompt_suffix(new_guidance, polluted, ctx)
    assert out.count("IMPORTANT: The previous attempt TIMED OUT") == 0
    # New block appears exactly once.
    assert out.count("RETRY GUIDANCE:") == 1
    assert "ORIGINAL_NOTE: spawn-time note preserved" in out
    # Snapshot persisted in ctx without any auto-block.
    snap = ctx["_original_prompt_suffix"]
    assert "IMPORTANT: The previous attempt TIMED OUT" not in snap
    assert "RETRY GUIDANCE:" not in snap
    assert "ORIGINAL_NOTE: spawn-time note preserved" in snap


def test_merge_retry_prompt_suffix_carries_snapshot_forward_on_repeat():
    """Subsequent calls reuse the snapshotted original; never re-strip."""
    ctx = {"_original_prompt_suffix": "ORIGINAL_NOTE: kept verbatim."}
    new_guidance = (
        "IMPORTANT: The previous attempt TIMED OUT — heartbeats stopped.\n- step\n"
    )
    out = merge_retry_prompt_suffix(
        new_guidance,
        # An "existing" suffix that already has the prior block — should be
        # ignored entirely because ctx already has the snapshot.
        new_guidance + "\nORIGINAL_NOTE: kept verbatim.\n",
        ctx,
    )
    assert out.count("IMPORTANT: The previous attempt TIMED OUT") == 1
    assert "ORIGINAL_NOTE: kept verbatim" in out


@pytest.mark.asyncio
async def test_requeue_agent_timeout_does_not_stack_suffix_on_repeat_retry(
    tl_db, mocker
):
    """Repeat AGENT_TIMEOUT retry should not duplicate the timeout suffix block.

    Regression for 291914e6 / f5c8a36f / 897a742e (2026-05-07): the original
    dedup regex only matched 'RETRY GUIDANCE:' headers and missed the
    'IMPORTANT: The previous attempt TIMED OUT' header used by AGENT_TIMEOUT.
    Each retry stacked another copy. Fix tracks _original_prompt_suffix in
    ctx and replaces the auto-generated portion entirely on each retry.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="AGENT_TIMEOUT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    failed_child = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 2},
        latest_status_message="Agent timed out (heartbeat lost)",
    )
    # Simulate a payload that already carries the timeout suffix from the
    # previous retry (legacy stacking regression). The original spawn-time
    # suffix is a small note we want preserved.
    failed_child.payloads = [
        {
            "type": "create",
            "prompt": "do create",
            "prompt_suffix": (
                "IMPORTANT: The previous attempt TIMED OUT — the agent stopped sending heartbeats. Keep operations smaller:\n"
                "- Break long-running steps into shorter units that emit progress frequently\n"
                "- Avoid single commands that block for minutes; prefer batched/iterative work\n"
                "- Commit partial progress early so a later heartbeat loss doesn't erase it\n"
                "- Reduce scope to fit comfortably within the heartbeat window\n"
                "\n"
                "ORIGINAL_NOTE: spawn-time guidance preserved across retries.\n"
            ),
        }
    ]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(failed_child, "payloads")
    session.add(failed_child)
    await session.commit()

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    prompt_suffix = clone.payloads[0].get("prompt_suffix", "") or ""

    # Timeout block must appear EXACTLY ONCE (not stacked).
    occurrences = prompt_suffix.count("IMPORTANT: The previous attempt TIMED OUT")
    assert occurrences == 1, (
        f"Timeout suffix stacked {occurrences} times — dedup regression. "
        f"Suffix:\n{prompt_suffix}"
    )
    # Original spawn-time note must be preserved.
    assert "ORIGINAL_NOTE: spawn-time guidance preserved" in prompt_suffix
    # And the dedup snapshot is now stamped in clone ctx for future retries.
    assert "_original_prompt_suffix" in (clone.context or {})
    assert (
        "IMPORTANT: The previous attempt TIMED OUT"
        not in clone.context["_original_prompt_suffix"]
    )


@pytest.mark.asyncio
async def test_requeue_rate_limit_injects_rate_limit_guidance(tl_db, mocker):
    """RATE_LIMIT failure inserts rate-limit retry guidance into prompt_suffix."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="RATE_LIMIT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="429 Too Many Requests",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest failed: assertion error on line 42",
    )

    await _requeue_parent_task(parent, session)

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
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
async def test_requeue_skips_child_when_error_fingerprint_matches_prior(tl_db, mocker):
    """Children with matching _previous_error_fingerprint are not cloned, and
    the parent's _requeue_count is not incremented when all failed children
    are skipped via fingerprint match."""
    from app.anomaly_detection import _error_fingerprint

    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    # COMPLETED sibling keeps the no-failed-to-retry branch from marking parent FAILED
    await _make_task(
        session,
        project.id,
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    error_msg = "pytest failed: assertion error on line 42"
    prior_fp = _error_fingerprint(error_msg)
    failed_child = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 2, "_previous_error_fingerprint": prior_fp},
        latest_status_message=error_msg,
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    # No new QUEUED clone was created for the fingerprint-matching child
    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert clones == []

    # Original failed child is untouched (not cancelled)
    await session.refresh(failed_child)
    assert failed_child.status == TaskStatus.FAILED

    # Parent requeue count was not incremented
    await session.refresh(parent)
    assert (parent.context or {}).get("_requeue_count", 0) == 0


@pytest.mark.asyncio
async def test_requeue_enriches_clone_context_provision_stage(tl_db, mocker):
    """Clone context has _previous_failure_stage=provision for PROVISION_FAILURE."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch(
        "app.task_lifecycle.classify_failure", return_value="PROVISION_FAILURE"
    )
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="vm failed to start",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert clone.context.get("_previous_failure_stage") == "provision"


@pytest.mark.asyncio
async def test_requeue_sets_recurring_flag_when_anomaly_exceeds_threshold(
    tl_db, mocker
):
    """_previous_is_recurring=True when persistent_anomalies has CODE_ERROR with count >= 3."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest: test_foo FAILED",
    )

    # Inject a persistent anomaly for CODE_ERROR with high occurrence count
    import app.shared_state as _shared_state

    _shared_state.persistent_anomalies[str(project.id)] = {
        "CODE_ERROR": {
            "occurrence_count": 5,
            "severity": "high",
            "latest_details": "test",
        }
    }
    try:
        await _requeue_parent_task(parent, session)
    finally:
        _shared_state.persistent_anomalies.pop(str(project.id), None)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="pytest: test_bar FAILED",
    )

    import app.shared_state as _shared_state

    _shared_state.persistent_anomalies[str(project.id)] = {
        "CODE_ERROR": {
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "test",
        }
    }
    try:
        await _requeue_parent_task(parent, session)
    finally:
        _shared_state.persistent_anomalies.pop(str(project.id), None)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_attempt_number": 1},
        latest_status_message="Agent timed out",
    )

    await _requeue_parent_task(parent, session)

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    assert "_previous_error_summary" not in clone.context


# ══════════════════════════════════════════════════════════════════════════════
# _derive_failure_stage
# ══════════════════════════════════════════════════════════════════════════════


def test_derive_failure_stage_provision():
    from app.task_lifecycle import _derive_failure_stage

    assert (
        _derive_failure_stage("PROVISION_FAILURE", "vm failed to start") == "provision"
    )


def test_derive_failure_stage_test_keyword():
    from app.task_lifecycle import _derive_failure_stage

    assert _derive_failure_stage("CODE_ERROR", "pytest: FAILED assert x == y") == "test"


def test_derive_failure_stage_lint_keyword():
    from app.task_lifecycle import _derive_failure_stage

    assert _derive_failure_stage("CODE_ERROR", "ruff: E501 line too long") == "lint"


def test_derive_failure_stage_build_keyword():
    from app.task_lifecycle import _derive_failure_stage

    assert (
        _derive_failure_stage("CODE_ERROR", "build failed: cannot compile module")
        == "build"
    )


def test_derive_failure_stage_clone_keyword():
    from app.task_lifecycle import _derive_failure_stage

    assert (
        _derive_failure_stage("CODE_ERROR", "git clone failed: repository not found")
        == "clone"
    )


def test_derive_failure_stage_pr_creation():
    from app.task_lifecycle import _derive_failure_stage

    assert (
        _derive_failure_stage("CODE_ERROR", "failed to create pr: branch not found")
        == "pr_creation"
    )


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


def test_derive_failure_stage_specific_before_test_keyword():
    """Specific stage keywords must be checked before the generic 'test' keyword."""
    from app.task_lifecycle import _derive_failure_stage

    # Lint errors reported against test file paths → lint, not test
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "ruff check failed: backend/testing/test_api.py"
        )
        == "lint"
    )
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "eslint error in src/components/Foo.test.tsx"
        )
        == "lint"
    )
    # Merge conflict in a test file → merge, not test
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "merge conflict in backend/testing/test_api.py"
        )
        == "merge"
    )
    # Review rejection mentioning test coverage → review, not test
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "review rejected: test coverage insufficient"
        )
        == "review"
    )
    # Build error importing test utilities → build, not test
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "build failed: cannot compile testing/helpers.ts"
        )
        == "build"
    )


def test_derive_failure_stage_integration():
    """Cross-file coordination failures map to 'integration', ahead of test/build."""
    from app.task_lifecycle import _derive_failure_stage

    assert (
        _derive_failure_stage(
            "CODE_ERROR", "ImportError: cannot import name 'foo' from 'app.bar'"
        )
        == "integration"
    )
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "AttributeError: module 'app.x' has no attribute 'y'"
        )
        == "integration"
    )
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "TS2305: Module './x' has no exported member 'Foo'"
        )
        == "integration"
    )
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "Type 'string' is not assignable to type 'number'"
        )
        == "integration"
    )
    # Should win over generic test/build even when those keywords also appear
    assert (
        _derive_failure_stage(
            "CODE_ERROR", "pytest failed: ImportError: no module named 'app.newmod'"
        )
        == "integration"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _merge_with_retry
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_merge_with_retry_succeeds_on_first_attempt():
    """Returns the merge result immediately when the first call succeeds."""
    mock_merge = AsyncMock(return_value={"merged": True, "sha": "abc123"})
    result = await _merge_with_retry(
        mock_merge, "gh_token", "owner", "repo", 42, "proj-id"
    )
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

    result = await _merge_with_retry(
        _conflict_merge, "token", "owner", "repo", 5, "proj"
    )
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
# GitHub-helper extraction seam (task_lifecycle_github_helpers.py)
# ══════════════════════════════════════════════════════════════════════════════


def test_github_helpers_seam_no_circular_import():
    """The extracted GitHub-retry helpers live in their own sibling module and
    are re-exported by task_lifecycle, so existing import paths are unchanged
    and the new module imports cleanly without a cycle."""
    import importlib

    helpers = importlib.import_module("app.task_lifecycle_github_helpers")
    lifecycle = importlib.import_module("app.task_lifecycle")

    for name in (
        "_merge_with_retry",
        "_comment_with_retry",
        "_pick_merge_method",
        "_get_approval_gate_file_lists",
    ):
        # Defined in the new module...
        assert hasattr(helpers, name), f"{name} missing from helpers module"
        # ...and re-exported by task_lifecycle as the SAME object (so call sites
        # and test mocks of app.task_lifecycle.<name> remain valid).
        assert getattr(lifecycle, name) is getattr(helpers, name)


def test_github_helpers_pick_merge_method_unchanged():
    """_pick_merge_method behavior is preserved post-extraction: squash by
    default, merge for back-merge conflict-resolution tasks."""
    from app.task_lifecycle_github_helpers import _pick_merge_method

    plain = Task(project_id=uuid.uuid4(), context={})
    backmerge = Task(project_id=uuid.uuid4(), context={"backmerge_conflict": True})
    assert _pick_merge_method(plain) == "squash"
    assert _pick_merge_method(backmerge) == "merge"


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

    result = await _process_post_commit(
        nonexistent_id, payload, _session_factory=factory
    )

    assert result == {"status": "accepted"}


@pytest.mark.asyncio
async def test_process_post_commit_persists_timing_fields(tl_db, mocker):
    """Phase 0 telemetry: setup_secs, warm_start, prior_role_vm_id round-trip
    into task.context["_timing"] (mirrors _shown_playbook_keys storage pattern)."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={"existing": "value"},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        setup_secs=12.5,
        warm_start=True,
        prior_role_vm_id="agent-vm-prior",
    )
    await _process_post_commit(task.id, payload, _session_factory=factory)

    await session.refresh(task)
    timing = (task.context or {}).get("_timing") or {}
    assert timing.get("setup_secs") == 12.5
    assert timing.get("warm_start") is True
    assert timing.get("prior_role_vm_id") == "agent-vm-prior"
    # Existing context must be preserved.
    assert (task.context or {}).get("existing") == "value"


@pytest.mark.asyncio
async def test_process_post_commit_omits_unsent_timing_fields(tl_db, mocker):
    """When the agent doesn't send timing fields, _process_post_commit must
    not write a _timing dict — older agents that pre-date Phase 0 should not
    have an empty/garbage record imposed on them."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    await session.refresh(task)
    assert "_timing" not in (task.context or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type,expected_phase",
    [("design_doc_update", 1), ("strategic_review", 2)],
)
@pytest.mark.parametrize(
    "status", [TaskStatus.FAILED, TaskStatus.CANCELLED]
)
async def test_process_post_commit_emits_cycle_completed_on_terminal_strategic(
    tl_db, mocker, task_type, expected_phase, status
):
    """A strategic task that terminates FAILED/CANCELLED never reaches the
    COMPLETED completion handlers, so the lifecycle backstop must emit
    strategic_cycle_completed to clear the frontend "Strategist working…"
    indicator (review iter-1 MAJOR, task_lifecycle.py:4006)."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session, project.id, task_type=task_type, status=status
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    payload = AgentStatusPayload(task_id=task.id, status=status)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    emit.assert_awaited_once_with(task.project_id, task.id, expected_phase)


@pytest.mark.asyncio
async def test_process_post_commit_no_cycle_completed_for_nonstrategic_failure(
    tl_db, mocker
):
    """The terminal-failure backstop is gated on strategic task types — a
    non-strategic FAILED task must NOT emit strategic_cycle_completed."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session, project.id, task_type="fix", status=TaskStatus.FAILED
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_post_commit_transient_handler_failure_requeues_after_rollback(
    tl_db, mocker
):
    """A completion handler that raises a transient DB error must leave the task
    REQUEUED and persisted — not silently stranded in its agent-reported status.

    Regression for the 2026-05-29 silent-loss: ``_handle_research_completion``
    hit a 120s statement_timeout, the requeue commit then died on the poisoned
    (un-rolled-back) session, and the research task stuck COMPLETED with 0
    children and its findings undispatched. ``_process_post_commit`` must roll
    the aborted transaction back, re-fetch the task, and persist the requeue —
    discarding the handler's partial (uncommitted) work rather than letting it
    leak into the recovery commit.
    """
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        latest_status_message="[18.6MB] Using tool: Bash",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    async def _failing_handler(t, logs, sess):
        # Mirror the real handler: stage partial uncommitted work (a child task,
        # like a spawned finding) and then hit a transient DB error (statement
        # timeout). The partial work must NOT survive recovery, and the requeue
        # must still persist.
        sess.add(
            Task(
                project_id=t.project_id,
                payloads=[{"type": "create", "prompt": "partial-handler-child"}],
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                parent_task_id=t.id,
            )
        )
        raise Exception("canceling statement due to statement timeout")

    # _handle_research_completion is imported lazily inside _process_post_commit
    # (from app.research_completion), so patch it at its source module.
    mocker.patch(
        "app.research_completion._handle_research_completion",
        side_effect=_failing_handler,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    # Re-fetch from a brand-new session to assert what actually persisted.
    async with factory() as verify_session:
        refetched = (
            await verify_session.execute(select(Task).where(Task.id == task.id))
        ).scalars().first()
        children = (
            (
                await verify_session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )
            )
            .scalars()
            .all()
        )

    assert refetched is not None
    # Recovery persisted: the task was requeued for a fresh attempt.
    assert refetched.status == TaskStatus.QUEUED
    assert refetched.attempt_count == 1
    assert (refetched.latest_status_message or "").startswith(
        "Handler hit transient error, requeuing"
    )
    # The handler's partial, uncommitted work was rolled back, not persisted.
    assert children == []


@pytest.mark.asyncio
async def test_process_post_commit_nontransient_handler_failure_fails_persisted(
    tl_db, mocker
):
    """A non-transient handler error must persist a FAILED status (not stay stuck
    at the agent-reported terminal status). Complements the transient-requeue
    path: both recovery branches run after the poisoned-session rollback."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    async def _failing_handler(t, logs, sess):
        raise ValueError("structured findings parse blew up")

    # _handle_research_completion is imported lazily inside _process_post_commit
    # (from app.research_completion), so patch it at its source module.
    mocker.patch(
        "app.research_completion._handle_research_completion",
        side_effect=_failing_handler,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    async with factory() as verify_session:
        refetched = (
            await verify_session.execute(select(Task).where(Task.id == task.id))
        ).scalars().first()

    assert refetched is not None
    assert refetched.status == TaskStatus.FAILED
    assert "handler failed" in (refetched.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_approve_branch_does_not_propagate_warm_target(tl_db, mocker):
    """Phase 2d: when the APPROVE branch hits CI failure and spawns a fix,
    the spawn must pass suppress_warm_handoff=True so no warm-target pin
    lands on the new task. We verify by patching _spawn_pipeline_task and
    asserting on the call kwargs."""
    # Skip rollup; we don't actually need it for this test.
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)

    # Capture the suppress_warm_handoff kwarg from _spawn_pipeline_task calls.
    captured_kwargs: list[dict] = []

    async def _capture(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return (None, False)

    # _spawn_pipeline_task is referenced in task_lifecycle's globals via
    # module-level import; patch at the call site's module.
    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture)
    # get_github_token_for_task is lazily imported inside _process_post_commit
    # — patch at its definition site so the deferred import picks up the mock.
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    # get_pull_request needs to return both the head ref equal to "feat/x"
    # and a sha so the CI-status check has a head_sha to query against.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "abc123", "ref": "feat/x"}},
    )
    # Simulate CI failure so the APPROVE-CI-failure spawn fires.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "failure",
            "failed_logs": "build failed",
        },
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    # _process_post_commit's APPROVE branch is reached when:
    #   - task.status == COMPLETED
    #   - first_payload.type == "review"
    #   - verdict == APPROVE in payload (parsed from verdict_json)
    session, factory = tl_db
    project = await _make_project(session)

    # The original create task whose PR is being reviewed. Must be present
    # for the merge-blocked / APPROVE path. We give it a vm_instance_id so
    # later code referencing it doesn't NPE on legacy expectations.
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "X", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        context={},
    )
    session.add(original)
    await session.commit()
    await session.refresh(original)

    review_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 5050,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        context={
            "review_loop": {
                "pr_number": 5050,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
                "iteration": 1,
            },
        },
    )
    session.add(review_task)
    await session.commit()
    await session.refresh(review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/o/r/pull/5050",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json='{"verdict": "APPROVE", "issues": []}',
    )

    try:  # noqa: SIM105
        await _process_post_commit(review_task.id, payload, _session_factory=factory)
    except Exception:  # noqa: S110
        # The harness may fail later in the APPROVE path on environment-specific
        # mocks (e.g. broadcasting); we just need _spawn_pipeline_task to have
        # been invoked with suppress_warm_handoff=True before that.
        pass

    ci_failure_calls = [
        kw for kw in captured_kwargs if kw.get("fix_reason") == "ci_failure"
    ]
    assert ci_failure_calls, "Expected at least one ci_failure spawn from APPROVE path"
    for kw in ci_failure_calls:
        assert kw.get("suppress_warm_handoff") is True, (
            "APPROVE-path CI-failure spawn must pass suppress_warm_handoff=True"
        )


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
        session,
        project.id,
        task_type="create",
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    # Patch the payloads to include base_branch so branch routing picks intermediate path
    task.payloads = [
        {"type": "create", "prompt": "do create", "base_branch": "research/cycle-1"}
    ]
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
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch("app.security.scan_pr_for_injection", side_effect=_scan_side_effect)
    # Merge succeeds
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
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
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    task.payloads = [
        {"type": "create", "prompt": "do create", "base_branch": "research/cycle-1"}
    ]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        side_effect=TimeoutError("connection timed out"),
    )

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
# Residual remediation MERGED transition on merge
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_residual_remediation_case_merged_on_merge(tl_db, mocker):
    """When a residual_fingerprint_remediation task's PR merges (via review pipeline), its REMEDIATING case is set MERGED."""
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from datetime import datetime, timezone

    session, factory = tl_db
    project = await _make_project(session)

    fp = "fp_lifecycle_resolve_xyz"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fp,
        status=ResidualCaseStatus.REMEDIATING,
        hit_count=5,
        sample_message="some recurring error",
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(case)

    # The original create task spawned by residual fingerprint remediation.
    orig_task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={
            "auto_remediation": True,
            "spawned_by": "residual_fingerprint_remediation",
            "error_fingerprint": fp,
        },
    )

    # A review task linked to the original via review_loop (this is the task
    # whose completion triggers the merge path in _process_post_commit).
    review_task = await _make_task(
        session,
        project.id,
        task_type="review",
        status=TaskStatus.IN_PROGRESS,
        context={
            "review_loop": {
                "original_task_id": str(orig_task.id),
                "pr_number": 99,
                "branch": "blueprint/task-remfp",
                "base_branch": "main",
            },
        },
    )

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    # get_pull_request: used for PR state check (early-exit guard) and head-branch validation.
    # Return empty head.sha so CI gate is skipped; ref matches branch so no branch-mismatch abort.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "head": {"ref": "blueprint/task-remfp", "sha": ""},
            "state": "open",
            "merged": False,
        },
    )
    mocker.patch(
        "app.github.submit_pr_review", new_callable=AsyncMock, return_value={"id": 1}
    )
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.task_lifecycle._comment_with_retry", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)

    # Submit APPROVED verdict for the review task; this triggers the merge path in
    # _process_post_commit → post_remediation_result_message → post_residual_fix_result_message
    # which transitions the case to MERGED.
    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        verdict_json='{"verdict": "APPROVE", "feedback": "LGTM"}',
        pr_url="https://github.com/owner/repo/pull/99",
        branch="blueprint/task-remfp",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(review_task.id, payload, _session_factory=factory)

    await session.refresh(case)
    assert case.status == ResidualCaseStatus.MERGED
    assert case.updated_at >= now


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
        text = "FAILED tests/test_a.py::test_x\nFAIL src/b.test.ts\nError: boom\n"
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
        text = 'File "backend/app/main.py", line 42\nsrc/index.ts:15:3 error\n'
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

    def test_agent_timeout_iteration_limit_no_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "Review-fix cycle limit reached",
            attempt=3,
            task_scope="medium",
            ctx={},
        )
        assert override is None

    def test_agent_timeout_iteration_limit_large_no_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "review-fix cap reached",
            attempt=2,
            task_scope="large",
            ctx={},
        )
        assert override is None

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

    def test_scope_overflow_small_escalates_to_medium(self):
        guidance, override = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW", None, attempt=1, task_scope="small", ctx={}
        )
        assert "scope's file-count budget" in guidance
        assert override == "medium"

    def test_scope_overflow_includes_overflow_details_when_changed_files_present(self):
        ctx = {
            "_previous_changed_files": [
                "backend/app/a.py",
                "backend/app/b.py",
                "agent/src/c.ts",
            ],
        }
        guidance, _ = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW", None, attempt=1, task_scope="medium", ctx=ctx
        )
        assert "OVERFLOW DETAILS FROM PREVIOUS ATTEMPT" in guidance
        assert "backend/app=2" in guidance
        assert "agent/src=1" in guidance

    def test_scope_overflow_analysis_gate_uses_analysis_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            "SCOPE_OVERFLOW",
            None,
            attempt=1,
            task_scope="medium",
            ctx={"scope_gate_triggered": "analysis_overflow"},
        )
        assert "analysis plan" in guidance.lower()
        assert "file-count budget" not in guidance.lower()
        assert override == "large"

    def test_scope_overflow_raw_lowercase_label_via_failure_category(self):
        """Path matches when failure_class is missing but ctx has lowercase label."""
        _, override = _build_retry_prompt_suffix(
            None,
            None,
            attempt=1,
            task_scope="medium",
            ctx={"failure_category": "scope_overflow"},
        )
        assert override == "large"

    def test_code_error_test_keyword_hint(self):
        guidance, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "pytest assertion failed in test_foo",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "failing tests" in guidance.lower()
        assert override is None

    def test_code_error_lint_keyword_hint(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "ruff E501 line too long; eslint warning",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "lint/type errors" in guidance

    def test_code_error_no_keywords_uses_default_fallback(self):
        """No test/lint keywords → default fallback guidance is emitted."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something unexpected happened",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "RETRY GUIDANCE" in guidance
        assert "unclassified" in guidance.lower()

    def test_code_error_incomplete_emits_no_ship_guidance(self):
        """Wrapper-fallback 'incomplete' (agent printed diff, never shipped) gets
        bespoke retry guidance — bypasses keyword-based stage routing that would
        otherwise mis-match 'build' on a diff body containing BuildConfig/etc."""
        # Status message simulates the diff-fragment tail that agent_status.py
        # captures. Without the special-case, _previous_failure_stage="build"
        # would route to the irrelevant build-stage hint.
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "interface ProjectBuildConfigSectionProps {\n  githubOwner: string;",
            attempt=2,
            task_scope="small",
            ctx={
                "agent_failure_category_raw": "incomplete",
                "_previous_failure_stage": "build",  # mis-derived; should be ignored
            },
        )
        assert "printed a diff to stdout" in guidance
        assert "./pr" in guidance and "./done" in guidance
        # The build-stage guidance text must NOT have leaked through.
        assert "Verify import paths" not in guidance

    def test_code_error_incomplete_overrides_keyword_hints(self):
        """'incomplete' raw label takes precedence over status-message keyword
        hints — the agent didn't actually run tests/lint, so those hints would
        be misleading."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "diff text that mentions test_foo and eslint",
            attempt=1,
            task_scope="small",
            ctx={"agent_failure_category_raw": "incomplete"},
        )
        assert "printed a diff to stdout" in guidance
        # Generic test/lint hint should not appear.
        assert "Fix the specific failing tests" not in guidance
        assert "Fix lint/type errors first" not in guidance

    def test_code_error_incomplete_codex_gets_apply_patch_guidance(self):
        """When the previous attempt ran on Codex (openai), the retry guidance
        names apply_patch — not the Claude-specific Edit/Write tools.
        Regression guard for incident 2026-05-07 task 3e1a042b."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "+ some diff body",
            attempt=2,
            task_scope="medium",
            ctx={
                "agent_failure_category_raw": "incomplete",
                "_previous_provider": "openai",
            },
        )
        assert "apply_patch" in guidance
        assert "Codex" in guidance
        # Claude-only tool names must NOT leak into Codex guidance.
        assert "Edit/Write/apply_patch" not in guidance

    def test_code_error_incomplete_claude_default_keeps_existing_wording(self):
        """No previous-provider context (or anthropic) keeps the original
        Edit/Write/apply_patch wording intact."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "+ some diff body",
            attempt=2,
            task_scope="medium",
            ctx={
                "agent_failure_category_raw": "incomplete",
                "_previous_provider": "anthropic",
            },
        )
        assert "Edit/Write/apply_patch" in guidance
        assert "Codex" not in guidance

    def test_code_error_incomplete_repeat_provider_appends_escape_hatch(self):
        """Two prior incomplete failures from the same provider trigger an
        explicit ./fail escape-hatch hint so the orchestrator can escalate
        instead of burning the full retry budget on a tool-layer mismatch."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "+ some diff body",
            attempt=3,
            task_scope="medium",
            ctx={
                "agent_failure_category_raw": "incomplete",
                "_previous_provider": "openai",
                "_failure_class_history": [
                    {"at": "2026-05-07T14:00:00", "class": "CODE_ERROR"},
                    {"at": "2026-05-07T14:30:00", "class": "CODE_ERROR"},
                ],
            },
        )
        assert "needs provider switch" in guidance
        assert "./fail" in guidance

    def test_code_error_attempt_one_no_scope_override(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=1,
            task_scope="large",
            ctx={},
        )
        assert override is None

    def test_code_error_large_downgrades_to_medium_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="large",
            ctx={},
        )
        assert override == "medium"

    def test_code_error_medium_downgrades_to_small_on_attempt_two(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={},
        )
        assert override == "small"

    def test_code_error_test_stage_preserves_scope(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={"_previous_failure_stage": "test"},
        )
        assert override is None

    def test_code_error_lint_stage_preserves_scope(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=3,
            task_scope="large",
            ctx={"_previous_failure_stage": "lint"},
        )
        assert override is None

    def test_code_error_build_stage_preserves_scope(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={"_previous_failure_stage": "build"},
        )
        assert override is None

    def test_code_error_review_stage_preserves_scope(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={"_previous_failure_stage": "review"},
        )
        assert override is None

    def test_code_error_downgrades_when_expansion_ratio_indicates_scope_overflow(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={"_previous_failure_stage": "test", "expansion_ratio": 2.0},
        )
        assert override == "small"

    def test_code_error_expansion_ratio_at_threshold_does_not_force_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=2,
            task_scope="medium",
            ctx={"_previous_failure_stage": "test", "expansion_ratio": 1.5},
        )
        assert override is None

    def test_code_error_small_no_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "boom",
            attempt=3,
            task_scope="small",
            ctx={},
        )
        assert override is None

    def test_code_error_extracts_previous_failing_tests_and_error_files(self):
        ctx = {
            "_previous_failing_tests": [
                "tests/test_a.py::test_x",
                "tests/test_b.py::test_y",
            ],
            "_previous_error_files": ["backend/app/main.py", "agent/src/index.ts"],
        }
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "test failure: assert",
            attempt=1,
            task_scope="small",
            ctx=ctx,
        )
        assert "Previously failing tests:" in guidance
        assert "tests/test_a.py::test_x" in guidance
        assert "tests/test_b.py::test_y" in guidance
        assert "Error file locations:" in guidance
        assert "backend/app/main.py" in guidance
        assert "agent/src/index.ts" in guidance

    def test_code_error_includes_previous_review_issues(self):
        ctx = {
            "_previous_review_issues": [
                "Missing null check in foo()",
                "Error handling swallows exceptions",
            ],
        }
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "test failure",
            attempt=1,
            task_scope="small",
            ctx=ctx,
        )
        assert "Unresolved review issues" in guidance
        assert "Missing null check in foo()" in guidance
        assert "Error handling swallows exceptions" in guidance

    def test_code_error_generic_message_falls_back_to_default_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something went wrong",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert guidance
        assert "RETRY GUIDANCE" in guidance
        assert "compile" in guidance.lower() or "lint" in guidance.lower()

    def test_code_error_stage_test_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something unexpected",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "test"},
        )
        assert "test stage" in guidance
        assert "different approach" in guidance

    def test_code_error_stage_lint_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something unexpected",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "lint"},
        )
        assert "lint stage" in guidance
        assert "type errors" in guidance

    def test_code_error_stage_build_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something unexpected",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "build"},
        )
        assert "build stage" in guidance
        assert "import paths" in guidance

    def test_code_error_stage_merge_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "something unexpected",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "merge"},
        )
        assert "merge stage" in guidance
        assert "merge conflicts" in guidance

    def test_code_error_stage_takes_precedence_over_keyword(self):
        """When stage is set, use stage guidance instead of keyword-matched hints."""
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "pytest assertion failed",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "lint"},
        )
        assert "lint stage" in guidance
        assert "failing tests" not in guidance.lower()

    def test_code_error_unknown_stage_falls_back_to_keyword(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "pytest assertion failed",
            attempt=1,
            task_scope="small",
            ctx={"_previous_failure_stage": "provision"},
        )
        assert "failing tests" in guidance.lower()

    def test_agent_timeout_oom_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "exit code 137 out of memory",
            attempt=1,
            task_scope="medium",
            ctx={},
        )
        assert "out-of-memory" in guidance
        assert "memory" in guidance.lower()

    def test_agent_timeout_heartbeat_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "No heartbeat received after 300s",
            attempt=1,
            task_scope="medium",
            ctx={},
        )
        assert "heartbeat" in guidance.lower()
        assert "smaller" in guidance.lower()

    def test_agent_timeout_iteration_limit_subtype_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "review-fix cycle limit reached",
            attempt=1,
            task_scope="medium",
            ctx={},
        )
        assert "iteration" in guidance.lower()
        assert "narrow scope" in guidance.lower()

    def test_agent_timeout_cascade_no_scope_downgrade(self):
        _, override = _build_retry_prompt_suffix(
            "AGENT_TIMEOUT",
            "Cancelled: parent evaluation timeout",
            attempt=3,
            task_scope="medium",
            ctx={},
        )
        assert override is None

    def test_code_error_same_tests_warning_on_overlap(self):
        ctx = {"_previous_failing_tests": ["tests/test_a.py::test_x"]}
        status = "pytest FAILED tests/test_a.py::test_x\nassert 1 == 2"
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            status,
            attempt=2,
            task_scope="small",
            ctx=ctx,
        )
        assert "same tests are failing" in guidance.lower()
        assert "tests/test_a.py::test_x" in guidance
        assert "Previously failing tests:" in guidance

    def test_previous_is_recurring_surfaces_in_guidance(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "pytest failed",
            attempt=2,
            task_scope="small",
            ctx={"_previous_is_recurring": True},
        )
        assert "CROSS-ATTEMPT CONTEXT" in guidance
        assert "recurred" in guidance.lower()

    def test_previous_self_review_outcome_surfaces(self):
        guidance, _ = _build_retry_prompt_suffix(
            "CODE_ERROR",
            "err",
            attempt=2,
            task_scope="small",
            ctx={"_previous_self_review_outcome": "missed edge case in parser"},
        )
        assert "self-review" in guidance.lower()
        assert "missed edge case" in guidance

    def test_external_api_github_with_status_code(self):
        guidance, override = _build_retry_prompt_suffix(
            "EXTERNAL_API",
            "GitHub API returned 503 Service Unavailable",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "GitHub API error" in guidance
        assert "(status 503)" in guidance
        assert "exponential backoff" in guidance
        assert override is None

    def test_external_api_anthropic_detected_from_claude_keyword(self):
        guidance, _ = _build_retry_prompt_suffix(
            "EXTERNAL_API",
            "Claude returned 429",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "Claude API error" in guidance
        assert "(status 429)" in guidance

    def test_external_api_generic_without_status_code(self):
        guidance, _ = _build_retry_prompt_suffix(
            "EXTERNAL_API",
            "network unreachable",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "external API error" in guidance
        assert "(status" not in guidance

    def test_rate_limit_static_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            "RATE_LIMIT",
            None,
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert "rate limit" in guidance.lower()
        assert "Retry-After" in guidance
        assert override is None

    def test_unknown_failure_class_returns_empty_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            "MYSTERY_FAILURE",
            "anything",
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert guidance == ""
        assert override is None

    def test_none_failure_class_returns_empty_guidance(self):
        guidance, override = _build_retry_prompt_suffix(
            None,
            None,
            attempt=1,
            task_scope="small",
            ctx={},
        )
        assert guidance == ""
        assert override is None


# ══════════════════════════════════════════════════════════════════════════════
# Double-spawn dedup: _requeue_parent_task skips cloning when debug-fix is active
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_requeue_skips_clone_when_debug_fix_active(tl_db):
    """_requeue_parent_task does NOT clone a failed child when its context has
    _debug_fix_spawned_task_id pointing to an active (QUEUED) Fix: task.

    This prevents the duplicate-PR race: auto-remediation (debug → Fix: path)
    and _requeue_parent_task both firing for the same failure, each opening a
    separate PR on the same base branch."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    # The active Fix: task spawned by the debug path
    active_fix = await _make_task(
        session,
        project.id,
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
    )

    # Failed child that references the active fix task
    child = await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_debug_fix_spawned_task_id": str(active_fix.id)},
        latest_status_message="tests failed",
    )

    retried = await _requeue_parent_task(parent, session)

    # The failed child should NOT have been cloned — debug-fix is already active
    assert retried == 0, f"Expected 0 retried (debug-fix active), got {retried}"

    # The failed child should still be FAILED (not cancelled), since we skipped it
    await session.refresh(child)
    assert child.status == TaskStatus.FAILED, (
        f"Failed child should remain FAILED when debug-fix is active, got {child.status}"
    )

    # No additional tasks should have been spawned
    result = await session.execute(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.id.notin_([child.id, active_fix.id]),
        )
    )
    extra = result.scalars().all()
    assert extra == [], f"Expected no new tasks spawned, got {[t.id for t in extra]}"


@pytest.mark.asyncio
async def test_requeue_proceeds_when_debug_fix_failed(tl_db):
    """_requeue_parent_task DOES clone a failed child when its _debug_fix_spawned_task_id
    points to a FAILED fix task.

    The debug-fix path tried and failed, so requeue should take over and give
    the task another attempt through the normal retry channel."""
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    # The debug-spawned Fix: task has parent_task_id = child.id (not parent.id),
    # so it is NOT a direct child of parent and won't be cloned by this requeue call.
    # We use a separate project to make that clear.
    failed_fix_id = uuid.uuid4()
    failed_fix = Task(
        id=failed_fix_id,
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "fix it"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        vm_instance_id="agent-vm-failed-fix",
    )
    session.add(failed_fix)
    await session.commit()
    await session.refresh(failed_fix)

    # Failed child (direct child of parent) references the now-failed fix task
    child = await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={"_debug_fix_spawned_task_id": str(failed_fix.id)},
        latest_status_message="code error: tests failed",
    )

    retried = await _requeue_parent_task(parent, session)

    # The failed child should have been cloned (debug-fix itself failed, requeue takes over)
    assert retried == 1, f"Expected 1 retried (debug-fix itself failed), got {retried}"

    await session.refresh(child)
    assert child.status == TaskStatus.CANCELLED


# ── Fix-task SR feedback loop: fix_iteration stamp ───────────────────────────


@pytest.mark.asyncio
async def test_fix_completion_stamps_fix_iteration_on_sr_outcome(tl_db, mocker):
    """When a fix task completes with _self_review_outcome lacking fix_iteration,
    the backend stamps it using review_loop.iteration as source of truth."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    session, factory = tl_db
    project = await _make_project(session)
    # review_loop.iteration=4 → second fix iteration (review1→fix1→review2→fix2)
    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "check_results": {"regression_risk": {"passed": True}},
            },
            "review_loop": {"iteration": 4},
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)
    await session.refresh(task)
    sro = (task.context or {}).get("_self_review_outcome") or {}
    assert sro.get("fix_iteration") == 2  # 4 // 2 = 2
    assert sro.get("verdict") == "pass"  # existing fields preserved


@pytest.mark.asyncio
async def test_fix_completion_preserves_agent_stamped_fix_iteration(tl_db, mocker):
    """When the agent already stamped fix_iteration, the backend does not overwrite it."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "fix_iteration": 3,  # already stamped by agent
            },
            "review_loop": {"iteration": 4},
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)
    await session.refresh(task)
    sro = (task.context or {}).get("_self_review_outcome") or {}
    assert sro.get("fix_iteration") == 3  # agent stamp wins, not overwritten by backend


# ══════════════════════════════════════════════════════════════════════════════
# Auto-merge failure → mergeable_state='dirty' → fix task gets a prompt
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_auto_merge_dirty_spawns_fix_with_prompt(tl_db, mocker):
    """When auto-merge fails and mergeable_state='dirty', the spawned fix task
    must carry a non-empty fix_prompt with rebase guidance.

    Regression guard for the bug where spawn_kwargs lacked 'fix_prompt',
    causing the agent to throw 'Fatal error: Fix task missing prompt'.
    """
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

    captured_kwargs: list[dict] = []

    async def _capture_spawn(*args, **kwargs):
        captured_kwargs.append(kwargs)
        mock_task = mocker.MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.first_payload = {"type": "fix"}
        mock_task.status = TaskStatus.QUEUED
        return (mock_task, True)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    # Merge fails
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "merge conflict"},
    )
    # GitHub reports mergeable_state='dirty'
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "mergeable_state": "dirty",
            "head": {"sha": "abc123", "ref": "blueprint/task-deadbeef"},
        },
    )

    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    # intermediate branch so is_intermediate=True and the auto-merge path fires
    task.payloads = [
        {
            "type": "create",
            "prompt": "implement feature X",
            "base_branch": "research/cycle-1",
        }
    ]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    fix_spawns = [
        kw for kw in captured_kwargs if kw.get("fix_reason") == "merge_conflict"
    ]
    assert fix_spawns, "Expected a merge_conflict fix task to be spawned"
    fix_kw = fix_spawns[0]
    fix_prompt = fix_kw.get("fix_prompt", "")
    assert fix_prompt, (
        "fix_prompt must be non-empty (was missing, causing Fatal error in agent)"
    )
    assert "rebase" in fix_prompt.lower(), "fix_prompt must contain rebase guidance"
    assert "77" in fix_prompt, "fix_prompt must reference the PR number"
    assert "research/cycle-1" in fix_prompt, "fix_prompt must reference the base branch"


@pytest.mark.asyncio
async def test_is_intermediate_honors_custom_staging_prefix(tl_db, mocker):
    """A small-scope create task whose base branch matches a NON-default
    configured staging prefix must be classified as intermediate (is_intermediate
    =True), routing through the auto-merge path. Regression guard for the bug
    where the small-diff path hardcoded 'research/'/'plan/' instead of the
    per-project staging_branch_prefixes.
    """
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

    captured_kwargs: list[dict] = []

    async def _capture_spawn(*args, **kwargs):
        captured_kwargs.append(kwargs)
        mock_task = mocker.MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.first_payload = {"type": "fix"}
        mock_task.status = TaskStatus.QUEUED
        return (mock_task, True)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    # Merge fails with a dirty state so the intermediate path spawns a
    # merge_conflict fix — observable proof the intermediate branch was taken.
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "merge conflict"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "mergeable_state": "dirty",
            "head": {"sha": "abc123", "ref": "blueprint/task-deadbeef"},
        },
    )

    session, factory = tl_db
    project = await _make_project(
        session, build_config={"staging_branch_prefixes": ["feature/", "wip/"]}
    )
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    # Custom-prefix base branch — only recognized as intermediate when the site
    # routes through the per-project staging prefixes.
    task.payloads = [
        {
            "type": "create",
            "prompt": "implement feature X",
            "base_branch": "feature/cycle-1",
        }
    ]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/88",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    fix_spawns = [
        kw for kw in captured_kwargs if kw.get("fix_reason") == "merge_conflict"
    ]
    assert fix_spawns, (
        "Expected the intermediate auto-merge path to fire for a custom-prefix "
        "base branch (is_intermediate must honor staging_branch_prefixes)"
    )
    assert "feature/cycle-1" in fix_spawns[0].get("fix_prompt", "")


@pytest.mark.asyncio
async def test_auto_merge_clean_state_enables_auto_merge_not_review(tl_db, mocker):
    """When auto-merge fails but mergeable_state='clean', no recovery task should be
    spawned. The PR is in good shape — re-enable auto-merge and park in CI-wait.
    """
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

    spawn_calls: list = []

    async def _capture_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        mock_task = mocker.MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.first_payload = {"type": "review"}
        mock_task.status = TaskStatus.QUEUED
        return (mock_task, True)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "not mergeable"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "mergeable_state": "clean",
            "head": {"sha": "abc123", "ref": "blueprint/task-deadbeef"},
        },
    )
    enable_auto_merge_mock = mocker.patch(
        "app.github.enable_auto_merge",
        new_callable=AsyncMock,
        return_value={"enabled": True},
    )

    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    task.payloads = [
        {
            "type": "create",
            "prompt": "implement feature Y",
            "base_branch": "research/cycle-1",
        }
    ]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/88",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    assert not spawn_calls, (
        "No review or fix task should be spawned when mergeable_state='clean'"
    )
    enable_auto_merge_mock.assert_called_once()

    # Parked task must be able to satisfy the merge gate on the next CI signal.
    # The small/medium-intermediate path explicitly skips review, so no review
    # task will ever flip review_approved. If the parked review_loop doesn't
    # carry review_approved=True (and ci_passed=True given mergeable_state=clean
    # implies green CI), handle_ci_completion's _review_approved gate at
    # ci_completion.py:1236 strands the task in EVALUATING forever.
    from app.ci_completion import _review_approved

    await session.refresh(task)
    parked_loop = (task.context or {}).get("review_loop", {})
    assert parked_loop.get("ci_waiting") is True
    assert parked_loop.get("review_approved") is True, (
        "parked review_loop must carry review_approved=True so the skip-review "
        "path can satisfy the merge gate"
    )
    assert parked_loop.get("ci_passed") is True, (
        "mergeable_state='clean' implies CI is green; ci_passed=True lets the "
        "next CI-completion poll merge immediately without re-waiting on checks"
    )
    assert _review_approved(parked_loop), (
        "_review_approved must return True so handle_ci_completion can merge"
    )


# ---------------------------------------------------------------------------
# Orphan-PR cleanup on cancellation
# ---------------------------------------------------------------------------
#
# Regression for c56fa118 / PR #5983 (2026-05-26): a duplicate task was
# cancelled (twin shipped the same finding via rollup) but its open PR
# stayed orphaned and blocked the parent's rollup gate.
#
# An earlier rev wired this through the Task.status terminal-transition
# event listener as a fire-and-forget; that broke ~20 requeue tests by
# racing a second session on the test fixture's StaticPool SQLite engine.
# The current design exposes `close_orphan_pr_if_any` as an explicit
# helper called from each cancel site with the site's own session.


class TestCloseOrphanPrIfAny:
    """close_orphan_pr_if_any: best-effort GitHub PR close given a task and
    a session. Cancel-site callers invoke it after setting CANCELLED to
    keep the originating site's session in scope."""

    @pytest.mark.asyncio
    async def test_closes_open_pr(self, tl_db, mocker):
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "refactor"}],
            status=TaskStatus.CANCELLED,
            context={"_pr_url": "https://github.com/Owner/Repo/pull/5983"},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new=AsyncMock(return_value=("tok", "Owner", "Repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"state": "open", "merged": False}),
        )
        close_mock = AsyncMock(return_value=True)
        mocker.patch("app.github.close_pull_request", new=close_mock)

        closed = await tl.close_orphan_pr_if_any(task, session, reason="dedup")

        assert closed is True
        close_mock.assert_awaited_once()
        # pr_number is the 4th positional arg (token, owner, repo, pr_number)
        assert close_mock.await_args.args[3] == 5983

    @pytest.mark.asyncio
    async def test_noop_when_no_pr_url(self, tl_db, mocker):
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "research"}],
            status=TaskStatus.CANCELLED,
            context={},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        close_mock = AsyncMock()
        mocker.patch("app.github.close_pull_request", new=close_mock)

        closed = await tl.close_orphan_pr_if_any(task, session, reason="dedup")

        assert closed is False
        close_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_pr_already_merged(self, tl_db, mocker):
        """If the PR somehow merged after the task was cancelled (race),
        don't try to re-close it."""
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "refactor"}],
            status=TaskStatus.CANCELLED,
            context={"_pr_url": "https://github.com/Owner/Repo/pull/42"},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new=AsyncMock(return_value=("tok", "Owner", "Repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"state": "closed", "merged": True}),
        )
        close_mock = AsyncMock()
        mocker.patch("app.github.close_pull_request", new=close_mock)

        closed = await tl.close_orphan_pr_if_any(task, session, reason="dedup")

        assert closed is False
        close_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_pr_already_closed(self, tl_db, mocker):
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "refactor"}],
            status=TaskStatus.CANCELLED,
            context={"_pr_url": "https://github.com/Owner/Repo/pull/77"},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new=AsyncMock(return_value=("tok", "Owner", "Repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"state": "closed", "merged": False}),
        )
        close_mock = AsyncMock()
        mocker.patch("app.github.close_pull_request", new=close_mock)

        closed = await tl.close_orphan_pr_if_any(task, session, reason="dedup")

        assert closed is False
        close_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_github_errors(self, tl_db, mocker):
        """GitHub 5xx / network errors during the close PATCH must be logged
        and swallowed so the cancel itself completes."""
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "refactor"}],
            status=TaskStatus.CANCELLED,
            context={"_pr_url": "https://github.com/Owner/Repo/pull/99"},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new=AsyncMock(return_value=("tok", "Owner", "Repo")),
        )
        mocker.patch(
            "app.github.get_pull_request",
            new=AsyncMock(return_value={"state": "open", "merged": False}),
        )
        mocker.patch(
            "app.github.close_pull_request",
            new=AsyncMock(side_effect=RuntimeError("github down")),
        )

        closed = await tl.close_orphan_pr_if_any(task, session, reason="dedup")
        assert closed is False  # swallowed, not raised


# ══════════════════════════════════════════════════════════════════════════════
# Modularity tripwire — task_lifecycle_requeue extraction
# ══════════════════════════════════════════════════════════════════════════════


_TASK_LIFECYCLE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "task_lifecycle.py")
)

_EXTRACTED_TO_REQUEUE = (
    "_record_parent_failure",
    "_requeue_parent_task",
)


def _top_level_defs_in_file(path: str) -> dict:
    """Return {name: kind} for module-level function/class/assignment defs."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, "function")
        elif isinstance(node, ast.AsyncFunctionDef):
            found.setdefault(node.name, "async function")
        elif isinstance(node, ast.ClassDef):
            found.setdefault(node.name, "class")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, "assignment")
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            found.setdefault(node.target.id, "assignment")
    return found


class TestRequeueExtractionTripwire:
    """Ensures _record_parent_failure / _requeue_parent_task stay in
    task_lifecycle_requeue.py and are not silently re-defined in task_lifecycle.py.
    """

    def test_symbols_not_redefined_in_task_lifecycle(self):
        defs = _top_level_defs_in_file(_TASK_LIFECYCLE_PATH)
        violations = [
            f"  {name} ({defs[name]})" for name in _EXTRACTED_TO_REQUEUE if name in defs
        ]
        assert not violations, (
            "task_lifecycle.py re-defines symbols that were extracted to "
            "task_lifecycle_requeue.py. Drop the local definition and rely on "
            "the re-export.\n\nViolations:\n" + "\n".join(violations)
        )

    @pytest.mark.parametrize("symbol", _EXTRACTED_TO_REQUEUE)
    def test_symbol_importable_from_task_lifecycle(self, symbol: str):
        facade = importlib.import_module("app.task_lifecycle")
        canonical = importlib.import_module("app.task_lifecycle_requeue")

        assert hasattr(facade, symbol), (
            f"app.task_lifecycle.{symbol} is no longer importable. "
            f"Add: from app.task_lifecycle_requeue import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"app.task_lifecycle_requeue.{symbol} is missing — restore the "
            f"definition or remove it from the tripwire."
        )
        assert getattr(facade, symbol) is getattr(canonical, symbol), (
            f"app.task_lifecycle.{symbol} is not the same object as "
            f"app.task_lifecycle_requeue.{symbol}. The facade is shadowing "
            f"the canonical implementation."
        )


# ══════════════════════════════════════════════════════════════════════════════
# C1 — shared-session poison: a sub-helper that hits a REAL DB-level error must
# not leave _process_post_commit's INTERNAL session in PendingRollback for the
# subsequent helpers / final commit.
#
# Two things every test below gets right that the original (vacuous) versions
# got wrong:
#
#   1. REAL driver-level poison, not an application RuntimeError. A bare
#      ``side_effect=RuntimeError`` does NOT poison a SQLAlchemy session — the
#      session only enters PendingRollback after a *DBAPI* error (a failed
#      ``execute`` / flush). So each injected helper runs a genuinely-failing
#      statement on the session it is handed (``SELECT … FROM <missing table>``
#      → OperationalError) before raising. Only then is the session actually in
#      the aborted state the C1 rollbacks exist to recover from.
#
#   2. We poison + probe the SAME session production uses — the INTERNAL one
#      ``_process_post_commit`` opens via ``async with factory() as session``
#      (task_lifecycle.py:568) — NOT this test's fixture ``session`` (a distinct
#      AsyncSession; poisoning one does not poison the other even though they
#      share the StaticPool connection). The injected helper receives that exact
#      internal session as an argument; ``_capture_and_poison`` records it and
#      asserts it really is poisoned, then ``_process_post_commit`` returning
#      ``{"status": "received"}`` proves its FINAL COMMIT succeeded on that same
#      session — i.e. the C1 rollback recovered it.
#
# Why each fails WITHOUT the C1 fix: on pre-C1 ``main`` none of the sub-helper
# excepts roll back and the final commit is an unguarded ``await
# session.commit()``. A real poison injected mid-chain therefore propagates to
# the next unguarded DB op (a later terminal helper, which on main is NOT
# wrapped in a per-helper try) or to the final commit, raising
# PendingRollbackError straight out of _process_post_commit — the call raises
# instead of returning ``{"status": "received"}``. WITH the C1 fix the rollback
# clears the poison and the chain finishes cleanly.
# ══════════════════════════════════════════════════════════════════════════════

from sqlalchemy import text as _sa_text  # noqa: E402
from sqlalchemy.exc import (  # noqa: E402
    IntegrityError as _IntegrityError,
    PendingRollbackError as _PendingRollbackError,
)


async def _poison_session(session) -> None:
    """Put ``session`` into a real aborted / PendingRollback transaction via a
    genuine DRIVER-level error — an ORM flush that hits a PK UNIQUE violation
    (IntegrityError).

    IMPORTANT: a failing *read* (``SELECT … FROM <missing table>`` →
    OperationalError) does NOT poison the session — empirically the next
    ``execute`` on it still succeeds. Only a failed flush/commit (an
    IntegrityError on a duplicate PK — the production shape of two racing
    writers / a serialization failure) actually dooms the transaction, so the
    next ``execute``/``commit`` raises PendingRollbackError until ``rollback()``
    is called. That is exactly the state the C1 rollbacks exist to recover from,
    and what a plain ``raise RuntimeError`` (which leaves the session perfectly
    usable) cannot reproduce.
    """
    import uuid as _uuid

    project_id = (
        await session.execute(_sa_text("SELECT id FROM project LIMIT 1"))
    ).scalar()
    # Two Task rows with the SAME primary key flushed together: the second
    # INSERT violates the PK UNIQUE constraint → IntegrityError → transaction
    # aborted.
    dup_id = _uuid.uuid4()
    for _ in range(2):
        session.add(
            Task(
                id=dup_id,
                project_id=project_id,
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
            )
        )
    # Expected to fail — the duplicate-PK flush aborts the transaction. Swallow
    # so the *caller* (the patched helper) controls what it ultimately raises.
    with contextlib.suppress(_IntegrityError):
        await session.flush()
    # A failed flush CLEARS session.new/dirty. Re-add a fresh pending row (no
    # flush) so the handler's final-commit guard (``if session.dirty or
    # session.new``) is True on the poisoned session — mirroring production,
    # where legitimate deferred work (terminal status, spawned tasks) is still
    # pending when the poison occurs. Without this, the unguarded pre-C1 final
    # commit would be SKIPPED and the poison would go undetected.
    session.add(
        Task(
            id=_uuid.uuid4(),
            project_id=project_id,
            payloads=[{"type": "create", "prompt": "post-poison deferred work"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
    )


def _capture_and_poison(captured: dict, session_argno: int):
    """Build an AsyncMock side_effect that captures the internal session at
    ``args[session_argno]``, poisons it with a real IntegrityError-level fault,
    asserts the poison actually took (so the test proves it injected a genuine
    driver-level fault — not a no-op that leaves the session usable), then
    raises to exercise the helper's except + C1 rollback."""

    async def _side_effect(*args, **kwargs):
        session = args[session_argno]
        captured["session"] = session
        await _poison_session(session)
        # Prove the poison is real: a follow-up op on this session now raises
        # PendingRollbackError. If it did NOT, the injected failure was not a
        # genuine session poison and the whole test would be vacuous — exactly
        # the bug this rework fixes.
        with pytest.raises(_PendingRollbackError):
            await session.execute(_sa_text("SELECT 1"))
        raise RuntimeError("simulated handler failure after real session poison")

    return _side_effect


@pytest.mark.asyncio
async def test_post_commit_rollup_raise_does_not_poison_session(tl_db, mocker):
    """C1: _check_and_trigger_rollup hitting a real DB error must roll back the
    INTERNAL post-commit session so the rest of the chain + final commit run
    clean. Without the fix the poison propagates to the final commit and
    _process_post_commit raises PendingRollbackError."""
    captured: dict = {}
    # _check_and_trigger_rollup(task, session) — session is positional arg 1.
    mocker.patch(
        "app.task_lifecycle._check_and_trigger_rollup",
        new_callable=AsyncMock,
        side_effect=_capture_and_poison(captured, session_argno=1),
    )
    session, factory = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload = AgentStatusPayload(task_id=child.id, status=TaskStatus.COMPLETED)

    # Must NOT raise PendingRollbackError — the C1 rollback after the rollup
    # except clears the poison so the final commit on the internal session
    # succeeds and the handler returns normally.
    result = await _process_post_commit(child.id, payload, _session_factory=factory)
    assert result["status"] == "received"
    # We poisoned the INTERNAL session production uses, not the fixture session.
    assert captured["session"] is not session


@pytest.mark.asyncio
async def test_post_commit_direction_outcome_raise_does_not_poison_session(
    tl_db, mocker
):
    """C1: _update_direction_research_outcomes hitting a real DB error must roll
    back the INTERNAL session so the terminal-record chain + final commit are
    not run poisoned. Without the fix the poison reaches a later helper / the
    final commit and _process_post_commit raises."""
    captured: dict = {}
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    # _update_direction_research_outcomes(project_id, direction_id,
    # research_task_id, session) — session is positional arg 3. Imported lazily
    # from app.direction_outcomes.
    mocker.patch(
        "app.direction_outcomes._update_direction_research_outcomes",
        new_callable=AsyncMock,
        side_effect=_capture_and_poison(captured, session_argno=3),
    )
    later_remediation = mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )
    session, factory = tl_db
    project = await _make_project(session)
    # FAILED (not COMPLETED) so the COMPLETED-only research-completion handler is
    # skipped, keeping the test focused on the 6b direction-outcomes block — which
    # fires on COMPLETED/FAILED/CANCELLED when direction_id + research_task_id are
    # present.
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.FAILED,
        context={"direction_id": "d-1", "research_task_id": str(uuid.uuid4())},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)

    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"

    # Chain continued past the raising direction-outcomes call on a clean session.
    later_remediation.assert_awaited_once()
    assert captured["session"] is not session


@pytest.mark.asyncio
async def test_post_commit_strategic_backstop_raise_does_not_500(tl_db, mocker):
    """C1: a real DB error in the strategic backstop must roll back the INTERNAL
    session so the guarded final commit does not surface PendingRollbackError as
    a 500 to the agent. Without the backstop rollback the unguarded final commit
    raises PendingRollbackError straight out of the handler."""
    captured: dict = {}
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    # strategic_cycle.get(session, cycle_id) — session is positional arg 0.
    mocker.patch(
        "app.strategic_cycle.get",
        new_callable=AsyncMock,
        side_effect=_capture_and_poison(captured, session_argno=0),
    )
    session, factory = tl_db
    project = await _make_project(session)
    # The backstop fires for ANY task carrying a strategic_cycle_id in context
    # (it reads payload type only to pick the phase label). Use a non-strategic
    # task type so the post-commit task acquisition takes the plain task-only
    # lock path rather than the strategic Project→Task pair-lock (which relies
    # on FOR UPDATE skip_locked that the SQLite test harness does not honor).
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.FAILED,
        context={"strategic_cycle_id": str(uuid.uuid4())},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)

    # Must return cleanly, not raise PendingRollbackError on the final commit.
    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"
    assert captured["session"] is not session


# ══════════════════════════════════════════════════════════════════════════════
# Cluster C5 — FAILED safeguard releases the cross-cutting merge gate.
# Pre-fix: the gate was released ONLY on the security_audit COMPLETED branch
# (_handle_security_audit_completion), so a FAILED/reaped safeguard left
# orig.requires_approval=True forever — a permanent strand that the services.py
# 2h approval-gate timeout then turns into a FAILED parent (discarding merged
# siblings). _process_post_commit's FAILED branch must now release the gate.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_post_commit_failed_safeguard_releases_merge_gate(tl_db, mocker):
    """A safeguard (security_audit) task that FAILS must clear the merge gate on
    its orig — proving the FAILED post-commit path wires in
    _maybe_release_safeguard_gate. This test FAILS against the pre-fix code,
    where the orig stayed requires_approval=True forever."""
    session, factory = tl_db

    project = await _make_project(session)
    # The gated original task — EVALUATING, merge held on the safeguard.
    orig = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"review_approved": True}},
    )
    orig.requires_approval = True
    session.add(orig)
    await session.commit()
    await session.refresh(orig)

    # The safeguard task, reported FAILED by the agent.
    safeguard = await _make_task(
        session,
        project.id,
        task_type="security_audit",
        status=TaskStatus.FAILED,
        parent_task_id=orig.id,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 4242,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(task_id=safeguard.id, status=TaskStatus.FAILED)
    await _process_post_commit(safeguard.id, payload, _session_factory=factory)

    # Re-fetch the orig from a fresh session to assert the gate was released.
    async with factory() as verify_session:
        refetched_orig = (
            await verify_session.execute(select(Task).where(Task.id == orig.id))
        ).scalars().first()

    assert refetched_orig is not None
    assert refetched_orig.requires_approval is False, (
        "FAILED safeguard must release the merge gate, not strand it forever"
    )
    # EVALUATING orig is re-armed for the CI loop (ci_waiting handoff).
    assert (
        (refetched_orig.context or {}).get("review_loop", {}).get("ci_waiting") is True
    )


@pytest.mark.asyncio
async def test_post_commit_failed_non_safeguard_does_not_touch_gate(tl_db, mocker):
    """Control: a FAILED non-safeguard task must not invoke the gate release
    (no orig context → no-op). Guards against over-broad wiring."""
    session, factory = tl_db

    project = await _make_project(session)
    # A normal create task that fails — carries no safeguard_for_task_id.
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    # If the release helper were called for a non-safeguard task it would no-op,
    # but assert it's never even invoked for a non-security_audit type.
    spy = mocker.patch(
        "app.task_completion._maybe_release_safeguard_gate",
        new_callable=AsyncMock,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    spy.assert_not_awaited()


# ── QP6: main-branch auto-approve gate uses the strict tested helper ──────────
#
# Before QP6 the live main-branch auto-approve path in _process_post_commit was
# a WEAKER inline duplicate of should_auto_approve_diff: it checked only the
# GitHub aggregate diff size + file count + scope=="small" + direction
# acceptance, and SKIPPED the strict checks the tested helper applies (source-
# type eligibility, test-file exclusion, scope-overflow). The fix routes the
# live gate through should_auto_approve_diff (single source of truth). This test
# pins the gap: a `docs`-type small PR with favorable direction history that the
# weak copy auto-merged (skipping review) must now be REJECTED by the gate and
# routed to the review pipeline. It fails against pre-fix code (which auto-merged
# the docs PR via _merge_with_retry instead of spawning review).


@pytest.mark.asyncio
async def test_main_branch_auto_approve_rejects_ineligible_type_via_strict_helper(
    tl_db, mocker
):
    session, factory = tl_db
    project = await _make_project(session)
    # Favorable direction history so the acceptance gate would PASS (isolating
    # the source-type gap as the thing that rejects).
    project.directions = [
        {
            "id": "d-docs-1",
            "research_history": [{"prs_merged": 8, "prs_rejected": 0}],
        }
    ]
    session.add(project)
    await session.commit()

    # docs task: allowed past the outer routing guard AND auto-approved by the
    # weak inline copy, but rejected by should_auto_approve_diff (source type not
    # in create/refactor/fix/migrate).
    task = await _make_task(
        session,
        project.id,
        task_type="docs",
        status=TaskStatus.IN_PROGRESS,
        context={
            "scope": "small",
            "direction_id": "d-docs-1",
            "_pr_changed_files": ["docs/readme.md"],
            "_pr_diff_summary": [
                {"file": "docs/readme.md", "added": 5, "removed": 1}
            ],
        },
    )
    # base_branch=main → not a staging branch → is_intermediate is False, so the
    # main-branch auto-approve gate (the QP6 path) is the one under test.
    task.payloads = [{"type": "docs", "prompt": "update docs", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    # PR-state guard fetch + (pre-fix) weak-copy diff source. The additions/
    # deletions/changed_files make the PR small enough that the PRE-FIX weak
    # inline copy WOULD auto-approve this docs PR (proving the test fails against
    # pre-fix code); the post-fix strict helper rejects it on source type.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "merged": False,
            "state": "open",
            "additions": 5,
            "deletions": 1,
            "changed_files": 1,
        },
    )
    # _merge_with_retry is the discriminator: only the auto-approve path merges.
    # The review path runs a security scan too, so the scan is NOT a discriminator.
    merge_spy = mocker.patch(
        "app.task_lifecycle._merge_with_retry",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    # Review-spawn path dependencies.
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "docs/readme.md", "status": "modified"}],
    )
    review_calls = []

    async def _capture_spawn(src_task, ptype, *a, **kw):
        review_calls.append(ptype)
        return (None, False)

    mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-docs",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    # Post-fix: the strict helper rejects docs → no auto-merge, review spawned.
    merge_spy.assert_not_awaited()
    assert "review" in review_calls, (
        "ineligible-type small PR must be routed to review, not auto-merged "
        "(weak-copy gap closed)"
    )


# ── _resolve_fallback_verdict — structured/anchored verdict parsing ────────────
#
# latent-#1: the prior fallback used `verdict_text.upper().rfind("APPROVE")`, a
# bare prose substring match. Free text like "do not APPROVE this yet" therefore
# yielded a FALSE approval that merges unreviewed code. The fix requires a
# structured/anchored approval signal (quoted token, JSON verdict field, or a
# standalone verdict line). These tests pin both halves: prose must NOT approve,
# and genuine structured approvals MUST still approve.


class TestResolveFallbackVerdict:
    # ---- false-positive prose that must NOT approve (the latent-#1 bug) -------

    @pytest.mark.parametrize(
        "prose",
        [
            "do not APPROVE this yet",
            "Do not approve this until the tests are fixed.",
            "needs changes before I can approve",
            "I would approve if the migration were reversible, but it is not.",
            "The reviewer is still deciding whether to approve.",
            "Cannot approve — see the unhandled None case in services.py.",
            "This is close to approval but not there.",
            "approved-by header is missing from the commit",
        ],
    )
    def test_bare_prose_approve_does_not_approve(self, prose):
        # Pre-fix (rfind substring match) these all returned "APPROVE".
        assert _resolve_fallback_verdict(prose) != "APPROVE"

    def test_bare_prose_with_no_request_changes_token_falls_through_to_none(self):
        # Pure prose with no anchored verdict at all → None (caller defaults to
        # REQUEST_CHANGES + manual review). Pre-fix this returned "APPROVE".
        assert _resolve_fallback_verdict("do not APPROVE this yet") is None

    def test_prose_discussing_approval_then_requesting_changes(self):
        text = (
            "We discussed whether to approve. Final call below.\n"
            'VERDICT: REQUEST_CHANGES\n'
        )
        assert _resolve_fallback_verdict(text) == "REQUEST_CHANGES"

    # ---- genuine structured approvals that MUST still approve ----------------

    def test_quoted_token_approves(self):
        assert _resolve_fallback_verdict('the verdict is "APPROVE"') == "APPROVE"

    def test_json_verdict_field_approves(self):
        # A truncated/invalid JSON blob that Methods 1 & 2 could not parse but
        # still carries the structured field.
        text = '{"verdict": "APPROVE", "feedback": "looks good" '  # missing close
        assert _resolve_fallback_verdict(text) == "APPROVE"

    def test_standalone_verdict_line_approves(self):
        text = "Reviewed all changed files.\nAPPROVE\n"
        assert _resolve_fallback_verdict(text) == "APPROVE"

    def test_verdict_prefixed_line_approves(self):
        assert _resolve_fallback_verdict("VERDICT: APPROVE") == "APPROVE"

    def test_approved_alias_quoted_approves(self):
        assert _resolve_fallback_verdict('result: "APPROVED"') == "APPROVE"

    # ---- structured REQUEST_CHANGES and precedence ---------------------------

    def test_quoted_request_changes(self):
        assert (
            _resolve_fallback_verdict('"REQUEST_CHANGES"') == "REQUEST_CHANGES"
        )

    def test_standalone_request_changes_with_space(self):
        assert _resolve_fallback_verdict("REQUEST CHANGES") == "REQUEST_CHANGES"

    def test_rightmost_anchored_token_wins(self):
        # An early structured APPROVE overridden by a later structured
        # REQUEST_CHANGES → REQUEST_CHANGES (the safe, later decision).
        text = '"APPROVE" was considered, but final:\nVERDICT: REQUEST_CHANGES'
        assert _resolve_fallback_verdict(text) == "REQUEST_CHANGES"

    def test_no_verdict_returns_none(self):
        assert _resolve_fallback_verdict("just some unrelated log output") is None
        assert _resolve_fallback_verdict("") is None


# ══════════════════════════════════════════════════════════════════════════════
# _mark_dependents_warm_claimable — completion-side warm-reuse trigger
# ══════════════════════════════════════════════════════════════════════════════


class TestMarkDependentsWarmClaimable:
    """The in-process merge paths (small-scope auto-merge, review-approved merge)
    delegate to the single authoritative orchestrator implementation to stamp the
    sticky ``_deps_satisfied`` readiness marker on dependents a landing unblocks,
    so the just-completed task's warm VM can claim follow-on work inside its
    teardown-defer grace window. The wrapper must (1) delegate with the landed
    task + the GitHub-aware PR-merge verifier, and (2) never let a failure abort
    post-commit processing."""

    @pytest.mark.asyncio
    async def test_delegates_to_orchestrator_with_landed_task(self, tl_db, mocker):
        session, _ = tl_db
        project = await _make_project(session)
        landed = await _make_task(
            session, project.id, task_type="research", status=TaskStatus.COMPLETED
        )

        spy = mocker.patch(
            "app.orchestrator._mark_unblocked_dependents",
            new_callable=AsyncMock,
            return_value=2,
        )
        verifier = object()
        mocker.patch("app.rollup_management._verify_pr_merged", verifier)

        await _mark_dependents_warm_claimable(landed)

        spy.assert_awaited_once()
        # The landed task and the authoritative PR-merge verifier are forwarded.
        assert spy.await_args.args[0] is landed
        assert spy.await_args.args[1] is verifier

    @pytest.mark.asyncio
    async def test_swallows_delegate_failure(self, tl_db, mocker):
        """A failure in the marking pass must never abort post-commit work."""
        session, _ = tl_db
        project = await _make_project(session)
        landed = await _make_task(
            session, project.id, task_type="research", status=TaskStatus.COMPLETED
        )

        mocker.patch(
            "app.orchestrator._mark_unblocked_dependents",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        )

        # Must not raise.
        assert await _mark_dependents_warm_claimable(landed) is None
