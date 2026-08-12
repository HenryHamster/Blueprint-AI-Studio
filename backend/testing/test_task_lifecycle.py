"""
Tests for backend/app/task_lifecycle.py.

Covers the critical functions:
  1. _has_active_strategic_task — active strategic task guard
  2. _increment_research_spawn_count — vision spawn counter management
  3. _requeue_parent_task — failed child retry with context injection
  4. _process_post_commit — task not found guard and rollup triggering
"""

import ast
import contextlib
import importlib
import inspect
import json
import os
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from sqlalchemy import text as _sa_text
from sqlalchemy.exc import (
    IntegrityError as _IntegrityError,
    PendingRollbackError as _PendingRollbackError,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.migration_reservations import allocate_migration_reservation
from app.models import (
    Project,
    StrategicCycle,
    Task,
    TaskStatus,
    TASK_ORIGINATION_RESEARCH,
    TASK_ORIGINATION_STRATEGIST,
    PoolVm,
    PoolVmState,
    ArtifactType,
    Alert,
    AlertSeverity,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
    MigrationReservationStatus,
)
from app.schemas import AgentStatusPayload
from app.task_lifecycle import (
    _has_active_strategic_task,
    _increment_research_spawn_count,
    _mark_dependents_warm_claimable,
    _requeue_parent_task,
    _comment_with_retry,
    _process_post_commit,
    _record_parent_failure,
    _is_transient_scan_error,
    _resolve_fallback_verdict,
    park_task,
    fire_task_wakes,
    deliver_wake,
)
from app.reaper_sweeps import sweep_deliver_task_wakes
from app.time_utils import _utcnow
from testing._post_commit_test_helpers import _post_commit_sources
from app.retry_prompt import (
    _extract_failing_tests,
    _extract_error_files,
    _build_scope_overflow_details,
    _build_retry_prompt_suffix,
    _detect_ci_review_disagreement,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    async with _Session() as session:
        yield session, _Session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the inline pre-merge Alembic dual-head guard to a no-op.

    Both inline merge paths in ``_process_post_commit`` (review-APPROVE and the
    non-staging small-scope auto-approve) now run
    ``enforce_premerge_migration_guard`` before merging, which calls
    ``github.get_pr_files``. Existing merge-reaching tests only mock
    ``merge_pull_request``; without this default an
    unmocked ``get_pr_files`` would make the guard DEFER and falsely hold the
    merge. A PR that touches only a non-migration file passes the guard cleanly.
    Tests that exercise the guard re-patch ``app.github.get_pr_files`` to
    override this default.
    """
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(return_value=[{"filename": "README.md"}]),
    )


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
    origination: str | None = None,
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
        origination=origination or "SYSTEM",
        **extra,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _open_pull_request(
    *,
    head_sha: str = "abc123",
    head_ref: str = "blueprint/task-abc123",
    base_sha: str = "base123",
    base_ref: str = "main",
    mergeable_state: str = "clean",
) -> dict:
    """Exact open PR snapshot accepted by the policy-bound merge authority."""
    return {
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": mergeable_state,
        "head": {"sha": head_sha, "ref": head_ref},
        "base": {"sha": base_sha, "ref": base_ref},
    }


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
        vm_instance_id="agent-vm-stale-blocked",
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        started_at=_utcnow() - timedelta(minutes=12),
        parked_at=_utcnow() - timedelta(minutes=8),
        park_reason="stale park marker",
        retry_after_until=_utcnow() + timedelta(minutes=15),
        context={"_retry_after_resets": 2, "_blocked_optimistic_retries": 3},
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    assert task.status == TaskStatus.BLOCKED
    # In-transition wrote the BLOCKED_PRECONDITION compat tags.
    assert (task.context or {}).get("failure_class") == "BLOCKED_PRECONDITION"
    assert (task.context or {}).get("failure_category") == "BLOCKED_PRECONDITION"

    unblock_to_queued(task)
    assert task.status == TaskStatus.QUEUED
    assert "blocker" not in (task.context or {})
    # The out-transition must also drop the stale BLOCKED_PRECONDITION tags so a
    # recovered task does not carry them into its next run.
    assert "failure_class" not in (task.context or {})
    assert "failure_category" not in (task.context or {})
    assert task.retry_after_until is None
    assert "_retry_after_resets" not in (task.context or {})
    assert "_blocked_optimistic_retries" not in (task.context or {})
    assert task.vm_instance_id is None
    assert task.last_heartbeat is None
    assert task.started_at is None
    assert task.parked_at is None
    assert task.park_reason is None
    assert task.blocker_kind is None
    assert task.blocked_reason is None
    assert task.blocked_at is None
    assert task.recheck_after is None


def test_clear_blocker_metadata_preserves_authoritative_verdict():
    """An authoritative non-blocked verdict written after the block must survive
    clearing — only the stale BLOCKED_PRECONDITION tags are dropped."""
    from app.task_blocked import clear_blocker_metadata, enter_blocked_state

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.IN_PROGRESS,
        retry_after_until=_utcnow() + timedelta(minutes=20),
        context={"_retry_after_resets": 1},
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    # A later authoritative classifier overwrites the failure class.
    ctx = dict(task.context or {})
    ctx["failure_class"] = "RATE_LIMIT"
    ctx["failure_category"] = "RATE_LIMIT"
    task.context = ctx

    clear_blocker_metadata(task)
    assert "blocker" not in (task.context or {})
    assert (task.context or {}).get("failure_class") == "RATE_LIMIT"
    assert (task.context or {}).get("failure_category") == "RATE_LIMIT"


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


def test_fresh_attempt_context_strip_preserves_retry_guidance_history():
    from app.sidecar_authority import SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS
    from app.task_blocked import strip_fresh_attempt_context

    ctx = strip_fresh_attempt_context(
        {
            "failure_class": "CODE_ERROR",
            "failure_category": "CODE_ERROR",
            "blocker": {"source": "agent"},
            "_blocked_escalated": True,
            "_precondition_escalated_key": "dep",
            "_blocked_optimistic_retries": 2,
            "_retry_after_resets": 4,
            "_dispatch_prep_failures": 3,
            "provision_failures": 2,
            "llm_account_id": "acct-1",
            "agent_error_report": {"exit_code": 1},
            "_requested_payload_model": "gpt-5.6-sol",
            "_resolved_payload_model": "claude-sonnet-4-6",
            "research_output_contract_version": 2,
            "research_findings_finalized": True,
            "_research_spawn_pending": True,
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_failure_class_history": ["SCOPE_OVERFLOW"],
            **{key: {"stale": True} for key in SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS},
            # Source-link and sidecar-interpretation markers are not themselves
            # attempt results. The task-aware reset helper below decides whether
            # the overlapping challenged-hashes key is source state or immutable
            # sidecar state.
            "quality_grader_for_task_id": "source-task-id",
            "finding_challenge_for_task_id": "source-task-id",
            "adversarial_grader": True,
            "challenged_boundary": "reproduce_absent",
            "challenged_first_verdict": "absent",
            "challenged_finding_hash": "finding-hash",
        }
    )

    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    assert "blocker" not in ctx
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert "_blocked_optimistic_retries" not in ctx
    assert "_retry_after_resets" not in ctx
    assert "_dispatch_prep_failures" not in ctx
    assert "provision_failures" not in ctx
    assert "llm_account_id" not in ctx
    assert "agent_error_report" not in ctx
    assert "_requested_payload_model" not in ctx
    assert "_resolved_payload_model" not in ctx
    assert "research_output_contract_version" not in ctx
    assert "research_findings_finalized" not in ctx
    assert "_research_spawn_pending" not in ctx
    assert SIDECAR_FRESH_ATTEMPT_CONTEXT_KEYS.isdisjoint(ctx)
    assert ctx["_previous_failure_class"] == "SCOPE_OVERFLOW"
    assert ctx["_failure_class_history"] == ["SCOPE_OVERFLOW"]
    assert ctx["quality_grader_for_task_id"] == "source-task-id"
    assert ctx["finding_challenge_for_task_id"] == "source-task-id"
    assert ctx["adversarial_grader"] is True
    assert ctx["challenged_boundary"] == "reproduce_absent"
    assert ctx["challenged_first_verdict"] == "absent"
    assert ctx["challenged_finding_hash"] == "finding-hash"


def test_fresh_attempt_reset_strips_source_gate_state_but_preserves_sidecar_shape():
    from app.task_blocked import reset_task_for_fresh_attempt

    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source = Task(
        project_id=project_id,
        payloads=[{"type": "research"}],
        created_by="ORCHESTRATOR",
        plan_output=[{"title": "stale attempt output"}],
        context={
            "quality_grader": {"passed": True},
            "awaiting_quality_grader": str(uuid.uuid4()),
            "_adversarial_challenged_hashes": ["stale-source-hash"],
            "research_findings_finalized": True,
            "_research_spawn_pending": True,
            "prior_deferred_findings": [{"title": "durable retry input"}],
            "deferred_findings": [{"title": "durable cycle handoff"}],
        },
        vm_instance_id="agent-vm-source-attempt",
        status=TaskStatus.FAILED,
    )
    reset_task_for_fresh_attempt(source)

    assert "quality_grader" not in (source.context or {})
    assert "awaiting_quality_grader" not in (source.context or {})
    assert "_adversarial_challenged_hashes" not in (source.context or {})
    assert "research_findings_finalized" not in (source.context or {})
    assert "_research_spawn_pending" not in (source.context or {})
    assert source.plan_output is None
    assert (source.context or {})["prior_deferred_findings"] == [
        {"title": "durable retry input"}
    ]
    assert (source.context or {})["deferred_findings"] == [
        {"title": "durable cycle handoff"}
    ]

    adversarial = Task(
        project_id=project_id,
        payloads=[{"type": "investigate"}],
        created_by="ORCHESTRATOR",
        parent_task_id=None,
        context={
            "quality_grader_for_task_id": str(source_id),
            "graded_task_type": "research",
            "adversarial_grader": True,
            "_adversarial_challenged_hashes": ["immutable-sidecar-hash"],
            "adversarial_gate": {"forged": "stale-result"},
        },
        vm_instance_id="agent-vm-sidecar-attempt",
        status=TaskStatus.FAILED,
    )
    reset_task_for_fresh_attempt(adversarial)

    sidecar_ctx = adversarial.context or {}
    assert sidecar_ctx["quality_grader_for_task_id"] == str(source_id)
    assert sidecar_ctx["graded_task_type"] == "research"
    assert sidecar_ctx["adversarial_grader"] is True
    assert sidecar_ctx["_adversarial_challenged_hashes"] == [
        "immutable-sidecar-hash"
    ]
    assert "adversarial_gate" not in sidecar_ctx


def test_fresh_attempt_reset_preserves_authenticated_challenge_markers_only():
    from app.task_blocked import reset_task_for_fresh_attempt

    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    immutable = {
        "finding_challenge_for_task_id": str(source_id),
        "challenged_boundary": "reproduce_absent",
        "challenged_first_verdict": "absent",
        "challenged_finding_hash": "immutable-finding-hash",
    }
    challenge = Task(
        project_id=project_id,
        payloads=[{"type": "investigate"}],
        created_by="ORCHESTRATOR",
        parent_task_id=None,
        context={**immutable, "finding_challenge": {"agreement": "stale"}},
        vm_instance_id="agent-vm-challenge-attempt",
        status=TaskStatus.FAILED,
    )
    reset_task_for_fresh_attempt(challenge)

    assert all(
        (challenge.context or {}).get(key) == value
        for key, value in immutable.items()
    )
    assert "finding_challenge" not in (challenge.context or {})

    forged = Task(
        project_id=project_id,
        payloads=[{"type": "investigate"}],
        created_by="AGENT",
        parent_task_id=None,
        context={
            **immutable,
            "_adversarial_challenged_hashes": ["forged"],
        },
        vm_instance_id="agent-vm-forged-sidecar",
        status=TaskStatus.FAILED,
    )
    reset_task_for_fresh_attempt(forged)

    # Non-overlapping markers are harmless without an authenticated source
    # shape, but attempt-local challenged hashes must never survive by key alone.
    assert "_adversarial_challenged_hashes" not in (forged.context or {})


@pytest.mark.asyncio
async def test_fresh_attempt_payload_model_restore_survives_flush_and_expire(tl_db):
    """Nested JSONB restoration must be durable, not only visible in memory."""
    from app.task_blocked import reset_task_for_fresh_attempt

    session, _factory = tl_db
    project = await _make_project(session)
    task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "model": "claude-sonnet-4-6",
                "execution_mode": "default",
            }
        ],
        status=TaskStatus.IN_PROGRESS,
        llm_account_id="aaaaaaaa-0001-4000-a000-000000000099",
        context={
            "llm_account_id": "aaaaaaaa-0001-4000-a000-000000000099",
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "agent_error_report": {"exit_code": 1},
            "_requested_payload_model": "gpt-5.6-sol",
            "_resolved_payload_model": "claude-sonnet-4-6",
            "_previous_failure_class": "RATE_LIMIT",
        },
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    reset_task_for_fresh_attempt(task)
    session.add(task)
    await session.flush()
    session.expire(task, ["payloads", "context"])
    await session.refresh(task, attribute_names=["payloads", "context"])

    assert task.payloads[0]["model"] == "gpt-5.6-sol"
    assert task.payloads[0]["execution_mode"] == "default"
    assert task.context.get("_previous_failure_class") == "RATE_LIMIT"
    assert "agent_error_report" not in task.context
    assert "llm_model" not in task.context


def test_fresh_attempt_preserves_resident_dispatch_identity():
    """Rate/dead-auth retries must remain the same persistent Workday session."""
    from app.task_blocked import reset_task_for_fresh_attempt

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review", "research_mode": "strategic"}],
        status=TaskStatus.IN_PROGRESS,
        context={
            "_resident_session": True,
            "strategic_session_id": "session-123",
            "_resident_worker_generation": 7,
            "preferred_runner_target_id": "runner-preferred",
            "strategic_local_runner_required": True,
            "strategic_local_runner_reason": "workbench_continuous",
            "llm_provider": "anthropic",
            "llm_account_id": "acct-exhausted",
            "agent_error_report": {"exit_code": 1},
            "_previous_failure_class": "RATE_LIMIT",
        },
    )

    reset_task_for_fresh_attempt(task)

    assert task.context["_resident_session"] is True
    assert task.context["strategic_session_id"] == "session-123"
    assert task.context["_resident_worker_generation"] == 7
    assert task.context["preferred_runner_target_id"] == "runner-preferred"
    assert task.context["strategic_local_runner_required"] is True
    assert task.context["strategic_local_runner_reason"] == "workbench_continuous"
    assert "llm_provider" not in task.context
    assert "llm_account_id" not in task.context
    assert "agent_error_report" not in task.context

def test_clear_blocker_metadata_allows_clean_reescalation():
    from app.task_blocked import (
        clear_blocker_metadata,
        enter_blocked_state,
        mark_blocker_escalated,
    )

    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.IN_PROGRESS,
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    mark_blocker_escalated(task, detail="first deadline")

    clear_blocker_metadata(task)
    ctx = task.context or {}
    assert "blocker" not in ctx
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert task.retry_after_until is None
    assert "_retry_after_resets" not in ctx

    enter_blocked_state(task, waiting_on="dep", reason="blocked again")
    mark_blocker_escalated(task, detail="second deadline")

    assert (task.context or {})["blocker"]["escalation_detail"] == "second deadline"
    assert (task.context or {})["failure_class"] == "BLOCKED_PRECONDITION"


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
async def test_requeue_parent_clone_strips_dispatch_prep_failures(tl_db, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    child = await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "failure_category": "CODE_ERROR",
            "_dispatch_prep_failures": 3,
            "_blocked_escalated": True,
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_failure_class_history": ["SCOPE_OVERFLOW"],
        },
        latest_status_message="tests failed",
    )

    retried = await _requeue_parent_task(parent, session)
    assert retried == 1

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.id != child.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert "_dispatch_prep_failures" not in clone_ctx
    assert "_blocked_escalated" not in clone_ctx
    assert "failure_category" not in clone_ctx
    assert clone_ctx["_previous_failure_class"] == "CODE_ERROR"
    history_classes = [
        entry.get("class") if isinstance(entry, dict) else entry
        for entry in clone_ctx["_failure_class_history"]
    ]
    assert history_classes == ["SCOPE_OVERFLOW", "CODE_ERROR"]


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
async def test_requeue_parent_blocked_ignores_superseded_cancelled_child(tl_db, mocker):
    """A blocked + superseded-cancelled mix re-evaluates for rollup instead of BLOCKED."""
    precondition_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_precondition_blocker_alert",
        new_callable=AsyncMock,
    )
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
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
        context={"_replaced_by_task_id": str(uuid.uuid4())},
        latest_status_message="Parent requeued — replaced by retry",
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context.get("_requeue_count") == 0
    assert "awaiting rollup re-evaluation" in (parent.latest_status_message or "")
    assert "blocked on upstream precondition" not in (
        parent.latest_status_message or ""
    )
    assert precondition_alert.await_count == 0
    precondition_alert.assert_not_awaited()
    alerts = (
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
    assert alerts == []


@pytest.mark.asyncio
async def test_requeue_parent_waits_when_child_is_parked(tl_db, mocker):
    """A parent whose only non-terminal child is PARKED must not be resolved as
    failed.  The shared terminal-verdict helper treats PARKED as active so the
    parent stays EVALUATING until the wake delivery advances the parked child."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    # PARKED child — non-terminal, waiting on an external signal.
    await _make_task(
        session,
        project.id,
        status=TaskStatus.PARKED,
        parent_task_id=parent.id,
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    # PARKED is active — the parent must not be marked FAILED or COMPLETED.
    assert parent.status not in (TaskStatus.FAILED, TaskStatus.COMPLETED)


@pytest.mark.asyncio
async def test_requeue_parent_waits_when_completed_sibling_has_parked_sibling(
    tl_db, mocker
):
    """A completed child alongside a PARKED sibling must not prematurely collapse
    the parent.  has_active_children must return True for the PARKED child so the
    parent remains in EVALUATING waiting for the wake delivery."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.PARKED,
        parent_task_id=parent.id,
    )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status not in (TaskStatus.FAILED, TaskStatus.COMPLETED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_specs", "expected_status", "expected_alert_calls", "expected_message"),
    [
        (
            [
                (
                    TaskStatus.BLOCKED,
                    None,
                    "BLOCKER: dep not landed",
                )
            ],
            TaskStatus.BLOCKED,
            1,
            "blocked on upstream precondition",
        ),
        (
            [
                (TaskStatus.BLOCKED, None, "BLOCKER: dep not landed"),
                (
                    TaskStatus.CANCELLED,
                    {"_replaced_by_task_id": str(uuid.uuid4())},
                    "Parent requeued — replaced by retry",
                ),
            ],
            TaskStatus.EVALUATING,
            0,
            "awaiting rollup re-evaluation",
        ),
        (
            [
                (TaskStatus.BLOCKED, None, "BLOCKER: dep not landed"),
                (
                    TaskStatus.CANCELLED,
                    {"failure_detail": "review_cap_exhausted"},
                    "Cancelled after review cap exhausted",
                ),
            ],
            TaskStatus.FAILED,
            0,
            "Re-evaluation limit reached",
        ),
        (
            [
                (TaskStatus.BLOCKED, None, "BLOCKER: dep not landed"),
                (
                    TaskStatus.FAILED,
                    {"failure_category": "CODE_ERROR"},
                    "tests failed",
                ),
            ],
            TaskStatus.FAILED,
            0,
            "Re-evaluation limit reached",
        ),
        (
            [
                (TaskStatus.BLOCKED, None, "BLOCKER: dep not landed"),
                (TaskStatus.COMPLETED, {"pr_merged": True}, None),
            ],
            TaskStatus.EVALUATING,
            0,
            "awaiting rollup re-evaluation",
        ),
    ],
)
async def test_requeue_limit_respects_mixed_terminal_child_sets(
    tl_db,
    mocker,
    child_specs,
    expected_status,
    expected_alert_calls,
    expected_message,
):
    """The limit path should distinguish pure blocked, mixed, and rollup cases."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    precondition_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_precondition_blocker_alert",
        new_callable=AsyncMock,
    )
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
    )

    for status, context, latest_status_message in child_specs:
        await _make_task(
            session,
            project.id,
            status=status,
            parent_task_id=parent.id,
            context=context,
            latest_status_message=latest_status_message,
        )

    await _requeue_parent_task(parent, session)

    await session.refresh(parent)
    assert parent.status == expected_status
    assert expected_message in (parent.latest_status_message or "")
    assert precondition_alert.await_count == expected_alert_calls
    if expected_status == TaskStatus.BLOCKED:
        assert (parent.context or {}).get("blocker", {}).get("source") == "parent"
    else:
        assert (parent.context or {}).get("blocker") is None
        assert (parent.context or {}).get("_requeue_count") == 3


@pytest.mark.asyncio
async def test_requeue_terminal_all_non_death_cancelled_single(tl_db, mocker):
    """All-children-terminal path: a parent whose ONLY unsucceeded child is a
    non-death cancel (_replaced_by_task_id — work continues under the clone)
    finalizes CANCELLED immediately — not parked EVALUATING forever — with a
    clear message and NO direction-health penalty / failure alert.

    _replaced_by_task_id is recognized by is_non_death_cancellation; the
    all_non_death_cancelled verdict fires before genuine_failures can count it,
    preventing a false FAILED + AGENT_TIMEOUT."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    failure_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    record_failure = mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0, "direction_id": "d-non-genuine"},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
        latest_status_message="Parent requeued — replaced by retry",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    # Non-death cancel → CANCELLED, not FAILED.
    assert parent.status == TaskStatus.CANCELLED
    assert "deliberately shelved or superseded" in (parent.latest_status_message or "")
    assert "not a failure" in (parent.latest_status_message or "")
    # No failure category stored — this is not a failure.
    assert parent.context.get("failure_category") is None
    # Budget untouched — nothing was retried.
    assert parent.context.get("_requeue_count") == 0
    # Non-death cancels → no direction-health ding and no failure alert.
    record_failure.assert_not_called()
    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_terminal_all_non_genuine_cancelled_multiple_kinds(tl_db, mocker):
    """All non-genuine-cancel taxonomy classes together (cascade, superseded,
    pr_closed_non_rejection, cap_exhausted_after_approve, REASSIGNED, SCOPE_EMPTY)
    with nothing else → parent FAILED, no direction-health ding."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    record_failure = mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )
    non_genuine_specs = [
        (
            {"_replaced_by_task_id": str(uuid.uuid4())},
            "Parent requeued — replaced by retry",
        ),
        ({}, "Superseded by task abc123"),
        ({"failure_detail": "pr_closed_non_rejection"}, "PR closed without rejection"),
        ({"failure_detail": "cap_exhausted_after_approve"}, "Capped after approval"),
        ({"failure_category": "REASSIGNED"}, "Reassigned to another agent"),
        ({"failure_category": "SCOPE_EMPTY"}, "SCOPE_EMPTY: nothing to do"),
    ]
    for ctx, msg in non_genuine_specs:
        await _make_task(
            session,
            project.id,
            status=TaskStatus.CANCELLED,
            parent_task_id=parent.id,
            context=ctx,
            latest_status_message=msg,
        )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "All 6 subtask(s) superseded/cancelled" in (
        parent.latest_status_message or ""
    )
    assert parent.context.get("failure_category") == "SCOPE_EMPTY"
    record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_requeue_limit_all_non_death_cancelled_terminates_cancelled(
    tl_db, mocker
):
    """Requeue-LIMIT path: parent at the cap whose only unsucceeded child is a
    non-death cancel terminates CANCELLED (kept in lockstep with the
    all-children-terminal path) without a direction-health ding.

    _replaced_by_task_id satisfies is_non_death_cancellation so the
    all_non_death_cancelled verdict fires and the parent is CANCELLED, not FAILED."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    record_failure = mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
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
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
        latest_status_message="Parent requeued — replaced by retry",
    )

    result = await _requeue_parent_task(parent, session)

    assert result == 0
    await session.refresh(parent)
    # Non-death cancel → CANCELLED, not FAILED.
    assert parent.status == TaskStatus.CANCELLED
    assert "deliberately shelved or superseded" in (parent.latest_status_message or "")
    assert parent.context.get("failure_category") is None
    record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_requeue_non_genuine_cancel_with_active_replacement_stays_evaluating(
    tl_db, mocker
):
    """A non-genuine cancel alongside a still-active replacement child must STAY
    EVALUATING — the live replacement may yet roll up; do NOT terminate."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )
    await _make_task(
        session,
        project.id,
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
        latest_status_message="Parent requeued — replaced by retry",
    )
    # Still-active replacement clone.
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
    assert "awaiting rollup re-evaluation" in (parent.latest_status_message or "")
    assert parent.context.get("_requeue_count") == 0


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
@pytest.mark.parametrize(
    "origination",
    [TASK_ORIGINATION_RESEARCH, TASK_ORIGINATION_STRATEGIST],
)
async def test_requeue_clone_inherits_origination(tl_db, mocker, origination):
    """Requeue clones retain the source task's durable origination class."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(
        session,
        project.id,
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        origination=origination,
        latest_status_message="Build failed: tests failing",
    )

    assert await _requeue_parent_task(parent, session) == 1

    clone = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .one()
    )
    assert clone.origination == origination


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


# ── _detect_ci_review_disagreement ─────────────────────────────────────────


def test_detect_ci_review_disagreement_frontend_uses_typecheck_not_build():
    """When reviewer flags frontend build issues but CI is green, the
    disagreement block instructs `npm run typecheck` (agent-VM safe)
    instead of `npm run build` (which OOMs on agent VMs).

    Key requirement: CLAUDE.md forbids `npm run build` on agent VMs and
    mandates `npm run typecheck` as the substitute. The retry prompt must
    reflect this — agents must not be told to run the OOM command.
    """
    ci_check = {
        "details": [
            {
                "name": "CI / Frontend",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }
    # Reviewer complaint includes "npm run build" (the problematic command).
    feedback = "npm run build failed: vite bundle error"
    issues = []
    pr_number = 123

    result = _detect_ci_review_disagreement(issues, feedback, ci_check, pr_number)

    # Must include the disagreement block (CI is green but reviewer flagged).
    assert "## CI vs reviewer build-gate disagreement" in result
    assert "CI / Frontend" in result

    # Must include npm run typecheck (agent-VM safe).
    assert "npm run typecheck" in result

    # Must NOT tell agents to run npm run build (the OOM-prone command).
    # Note: The pattern regex still detects "npm run build" in complaints,
    # but the example steps should use typecheck instead.
    lines = result.split("\n")
    # Find the lines with example commands (marked with "Run `" or similar).
    command_lines = [line for line in lines if "npm run" in line]
    # All command lines should reference typecheck, not build.
    for line in command_lines:
        if "npm run" in line and "example" not in line.lower():
            # This is a command to run, verify it's typecheck
            assert "npm run typecheck" in line, f"Command line should use typecheck: {line}"


def test_detect_ci_review_disagreement_references_ci_as_authoritative():
    """The disagreement block references the green CI run as the
    authoritative build proof, so reviewers don't need to reproduce the
    full bundle locally."""
    ci_check = {
        "details": [
            {
                "name": "CI / Frontend",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }
    feedback = "admin-ui npm ci && npm run build failed"
    issues = []
    pr_number = 456

    result = _detect_ci_review_disagreement(issues, feedback, ci_check, pr_number)

    assert "## CI vs reviewer build-gate disagreement" in result
    # Must reference CI as authoritative proof (not asking reviewer to reproduce).
    assert "authoritative" in result.lower()
    # Text should mention "green" and "CI run URL" to show the CI is the proof.
    assert "green" in result.lower()
    assert "run url" in result.lower()


def test_detect_ci_review_disagreement_pattern_still_detects_build_complaints():
    """The _BUILD_GATE_PATTERN regex used for complaint detection must
    still match 'npm run build' to identify build-gate disputes, even
    though the guidance will recommend typecheck instead of build."""
    ci_check = {
        "details": [
            {
                "name": "CI / Frontend",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }
    # Complaint contains "npm run build" — the pattern must detect it.
    feedback = "error: npm run build failed on admin-ui"
    issues = []
    pr_number = 789

    result = _detect_ci_review_disagreement(issues, feedback, ci_check, pr_number)

    # Pattern must have detected the complaint and generated the block.
    assert "## CI vs reviewer build-gate disagreement" in result


def test_detect_ci_review_disagreement_empty_when_no_ci_check():
    """Return empty string when there's no CI check data."""
    result = _detect_ci_review_disagreement([], "some complaint", None, None)
    assert result == ""


def test_detect_ci_review_disagreement_empty_when_ci_not_green():
    """Return empty string when CI jobs are not all green."""
    ci_check = {
        "details": [
            {
                "name": "CI / Frontend",
                "conclusion": "failure",  # Not green
                "status": "completed",
            }
        ]
    }
    feedback = "npm run build failed"
    result = _detect_ci_review_disagreement([], feedback, ci_check, None)
    assert result == ""


def test_detect_ci_review_disagreement_empty_when_no_matching_complaint():
    """Return empty string when complaint doesn't match the gate pattern."""
    ci_check = {
        "details": [
            {
                "name": "CI / Frontend",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }
    feedback = "Code is slow and needs optimization"  # No build complaint
    result = _detect_ci_review_disagreement([], feedback, ci_check, None)
    assert result == ""


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
async def test_requeue_preserves_operator_guidance_across_automatic_retry(
    tl_db, mocker
):
    """A later automatic retry must not discard manual operator correction."""
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.classify_failure", return_value="AGENT_TIMEOUT")
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    guidance = "Do not create 0626. Remove the duplicate migration instead."
    failed_child = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 2,
            "_original_prompt_suffix": "ORIGINAL_NOTE: keep this.",
        },
        latest_status_message="Agent timed out",
    )
    failed_child.payloads = [
        {
            "type": "create",
            "prompt": "Renumber the migration to 0626.",
            "operator_retry_guidance": guidance,
            "prompt_suffix": (
                "IMPORTANT: The previous attempt TIMED OUT.\n"
                "ORIGINAL_NOTE: keep this.\n\n"
                f"## Operator retry guidance\n{guidance}"
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
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .first()
    )
    assert clone is not None
    prompt_suffix = clone.first_payload.get("prompt_suffix", "")
    assert prompt_suffix.count("## Operator retry guidance") == 1
    assert prompt_suffix.endswith(guidance)
    assert "IMPORTANT: The previous attempt TIMED OUT" in prompt_suffix
    assert "ORIGINAL_NOTE: keep this." in prompt_suffix
    assert clone.first_payload["operator_retry_guidance"] == guidance


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
# GitHub-helper extraction seam (task_lifecycle_github_helpers.py)
# ══════════════════════════════════════════════════════════════════════════════


def test_github_helpers_seam_no_circular_import():
    """The extracted GitHub helpers live in their own sibling module and
    are re-exported by task_lifecycle, so existing import paths are unchanged
    and the new module imports cleanly without a cycle."""
    import importlib

    helpers = importlib.import_module("app.task_lifecycle_github_helpers")
    lifecycle = importlib.import_module("app.task_lifecycle")

    for name in (
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
@pytest.mark.parametrize("task_type", ["fix", "review", "research", "create"])
async def test_process_post_commit_locks_project_before_task_for_nonstrategic(
    tl_db, mocker, task_type
):
    """WI-6: _process_post_commit acquires the Project lock BEFORE the Task
    lock for NON-strategic task types too — enforcing the canonical
    Project-before-Task ordering for the whole post-commit unit.

    Prior code only used the canonical ``lock_project_then_task``
    (Project-before-Task) pair-locker for strategic types (design_doc_update,
    strategic_review) and locked the Task first (task-only FOR UPDATE) for
    every other type, reaching the Project lock only downstream. Two
    concurrent non-strategic post-commits in the same project therefore
    acquired Task→Project while a strategic / dispatch / reaper path acquired
    Project→Task — the opposite orders deadlocked (Postgres 40P01
    DeadlockDetected, ~6/hr on bursty concurrent agent-status callbacks).

    This asserts the canonical pair-locker is invoked with the task's
    (project_id, task_id) under skip_locked for plain non-strategic types.
    A true concurrency race is non-deterministic to unit-test, so we assert
    the code path takes the Project lock first via the canonical helper rather
    than racing threads (see test_lock_ordering.py for the helper-level
    contention regression on Postgres).
    """
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session, project.id, task_type=task_type, status=TaskStatus.COMPLETED
    )

    calls: dict = {}

    class _StopAfterLock(Exception):
        pass

    @contextlib.asynccontextmanager
    async def _spy_lock_project_then_task(session_, project_id, task_id, **kw):
        calls["project_id"] = project_id
        calls["task_id"] = task_id
        calls["skip_locked"] = kw.get("skip_locked")
        # Short-circuit before the heavy downstream post-commit machinery — we
        # only need to prove the canonical pair-locker was invoked first.
        raise _StopAfterLock()
        yield (None, None)  # pragma: no cover — makes this an async generator

    mocker.patch("app.task_locking.lock_project_then_task", _spy_lock_project_then_task)

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    with pytest.raises(_StopAfterLock):
        await _process_post_commit(task.id, payload, _session_factory=factory)

    assert calls["project_id"] == project.id, (
        "post-commit must lock the task's OWNING project first"
    )
    assert calls["task_id"] == task.id
    assert calls["skip_locked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lock_result", "expected_scope"),
    [("project_busy", "project"), ("task_busy", "task")],
)
async def test_process_post_commit_attributes_pair_lock_contention(
    tl_db, mocker, lock_result, expected_scope
):
    """The recovery ledger gets the actual contended half of the lock pair."""
    from app.task_lifecycle import PostCommitLocked

    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session, project.id, task_type="plan", status=TaskStatus.EVALUATING
    )

    @contextlib.asynccontextmanager
    async def _contended_pair(*args, **kwargs):
        if lock_result == "project_busy":
            yield (None, None)
        else:
            yield (project, None)

    mocker.patch("app.task_locking.lock_project_then_task", _contended_pair)
    mocker.patch("asyncio.sleep", new=AsyncMock())
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)

    with pytest.raises(PostCommitLocked) as exc_info:
        await _process_post_commit(task.id, payload, _session_factory=factory)

    assert exc_info.value.lock_scope == expected_scope
    expected_id = project.id if expected_scope == "project" else task.id
    assert exc_info.value.resource_id == expected_id


@pytest.mark.asyncio
async def test_childless_recovery_claim_cannot_revive_cancelled_plan(tl_db, mocker):
    """Cancellation between claim and processing wins under the lifecycle lock."""
    session, factory = tl_db
    project = await _make_project(session)
    claim_token = "claim-before-cancel"
    task = await _make_task(
        session,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={
            "_childless_post_commit_recovery": {
                "attempts": 1,
                "claim_token": claim_token,
            }
        },
    )
    task.plan_output = {"subtasks": [{"type": "create", "prompt": "do x"}]}
    await session.commit()

    # This is the state change that raced after the recovery claim committed.
    task.status = TaskStatus.CANCELLED
    await session.commit()
    handle_plan = mocker.patch(
        "app.task_completion._handle_plan_completion", new=AsyncMock()
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs=json.dumps(task.plan_output),
    )

    result = await _process_post_commit(
        task.id,
        payload,
        _session_factory=factory,
        _expected_childless_recovery_token=claim_token,
    )

    assert result == {"status": "accepted", "recovery_skipped": True}
    handle_plan.assert_not_awaited()
    await session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    child_ids = (
        await session.execute(select(Task.id).where(Task.parent_task_id == task.id))
    ).scalars().all()
    assert child_ids == []


@pytest.mark.asyncio
async def test_childless_recovery_preserves_cancel_during_handler(tl_db, mocker):
    """Cancellation during the handler's unlocked window wins over recovery."""
    session, factory = tl_db
    project = await _make_project(session)
    claim_token = "claim-before-handler"
    task = await _make_task(
        session,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={
            "_childless_post_commit_recovery": {
                "attempts": 1,
                "claim_token": claim_token,
            }
        },
    )
    task.plan_output = {"subtasks": [{"type": "create", "prompt": "do x"}]}
    await session.commit()

    async def _cancel_in_unlocked_window(task_arg, logs, handler_session):
        # Mirrors _ensure_branch's intentional mid-handler commit/release.
        await handler_session.commit()
        async with factory() as cancel_session:
            cancelled = await cancel_session.get(Task, task.id)
            cancelled.status = TaskStatus.CANCELLED
            cancel_session.add(cancelled)
            await cancel_session.commit()
        raise RuntimeError("snapshot changed after cancellation")

    handle_plan = mocker.patch(
        "app.task_completion._handle_plan_completion",
        new=AsyncMock(side_effect=_cancel_in_unlocked_window),
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs=json.dumps(task.plan_output),
    )

    result = await _process_post_commit(
        task.id,
        payload,
        _session_factory=factory,
        _expected_childless_recovery_token=claim_token,
    )

    assert result == {"status": "accepted", "recovery_skipped": True}
    handle_plan.assert_awaited_once()
    await session.refresh(task)
    assert task.status == TaskStatus.CANCELLED
    child_ids = (
        await session.execute(select(Task.id).where(Task.parent_task_id == task.id))
    ).scalars().all()
    assert child_ids == []


@pytest.mark.asyncio
async def test_childless_recovery_current_handler_failure_propagates(tl_db, mocker):
    """The recovery ledger, not generic handler recovery, owns its retries."""
    session, factory = tl_db
    project = await _make_project(session)
    claim_token = "current-claim"
    task = await _make_task(
        session,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={
            "_childless_post_commit_recovery": {
                "attempts": 1,
                "claim_token": claim_token,
            }
        },
    )
    task.plan_output = {"subtasks": [{"type": "create", "prompt": "do x"}]}
    await session.commit()
    mocker.patch(
        "app.task_completion._handle_plan_completion",
        new=AsyncMock(side_effect=RuntimeError("branch creation failed")),
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs=json.dumps(task.plan_output),
    )

    with pytest.raises(RuntimeError, match="branch creation failed"):
        await _process_post_commit(
            task.id,
            payload,
            _session_factory=factory,
            _expected_childless_recovery_token=claim_token,
        )

    await session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {})["_childless_post_commit_recovery"][
        "claim_token"
    ] == claim_token


def test_strategic_completion_reserves_message_block_before_authority_locks():
    source = _post_commit_sources()
    reserve_at = source.index("await reserve_project_message_sequences(")
    reservation_commit_at = source.index("await session.commit()", reserve_at)
    authority_lock_at = source.index("async with lock_project_then_task(")

    assert reserve_at < reservation_commit_at < authority_lock_at
    assert '_peek_task_type == "strategic_review"' not in source
    assert '_peek_completion_handler == "strategic"' in source
    assert "message_sequences=preallocated_strategic_message_sequences" in source


@pytest.mark.asyncio
async def test_design_doc_capability_does_not_reserve_strategic_message_sequences(
    tl_db, mocker
):
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="design_doc_update",
        status=TaskStatus.COMPLETED,
    )
    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        logs=json.dumps({"messages": [{"type": "NOTE", "content": "Bounded output"}]}),
    )
    events: list[str] = []

    class _StopAfterAuthorityLock(Exception):
        pass

    @contextlib.asynccontextmanager
    async def _stop_at_authority_lock(*_args, **_kwargs):
        events.append("authority_lock")
        raise _StopAfterAuthorityLock()
        yield (None, None)  # pragma: no cover

    reserve = mocker.patch(
        "app.project_message_creation.reserve_project_message_sequences",
        new_callable=AsyncMock,
        side_effect=AssertionError("design-doc completion must not reserve messages"),
    )
    mocker.patch(
        "app.task_locking.lock_project_then_task",
        new=_stop_at_authority_lock,
    )

    with pytest.raises(_StopAfterAuthorityLock):
        await _process_post_commit(task.id, payload, _session_factory=factory)

    assert events == ["authority_lock"]
    reserve.assert_not_awaited()


def test_strategic_completion_message_count_covers_direct_and_reply_emissions():
    from app.strategic_completion import (
        _handle_strategic_completion,
        strategic_completion_message_count,
    )

    logs = json.dumps(
        {
            "messages": [
                {"type": "NOTE", "content": "First"},
                {"type": "NOTE", "content": "  "},
                {"type": "QUESTION", "content": "Second"},
            ],
            "resolved_messages": [
                {"id": str(uuid.uuid4())},
                {"id": str(uuid.uuid4())},
                {"id": "invalid-but-still-possible"},
            ],
        }
    )

    assert strategic_completion_message_count(logs) == 5
    source = inspect.getsource(_handle_strategic_completion)
    for helper_name in ("post_feedback_acknowledgment", "post_question_answer"):
        helper_at = source.index(f"await {helper_name}")
        assert (
            "sequence=next(reserved_message_sequences, None)"
            in source[helper_at : helper_at + 1_200]
        )


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
@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_process_post_commit_emits_cycle_completed_on_terminal_strategic(
    tl_db, mocker, task_type, expected_phase, status
):
    """A strategic task that terminates FAILED/CANCELLED never reaches the
    COMPLETED completion handlers, so the lifecycle backstop must emit
    strategic_cycle_completed to clear the frontend "Strategist working…"
    indicator (review iter-1 MAJOR, task_lifecycle.py:4006)."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(session, project.id, task_type=task_type, status=status)
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    payload = AgentStatusPayload(task_id=task.id, status=status)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    emit.assert_awaited_once()
    assert emit.await_args.args == (task.project_id, task.id, expected_phase)
    assert emit.await_args.kwargs["phase_state"] == "failed"
    assert "status" in emit.await_args.kwargs["completion"]


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
    task.vm_provisioned_at = _utcnow() - timedelta(minutes=5)
    task.vm_stopped_at = _utcnow()
    task.api_call_summary = {
        "total_input_tokens": 1200,
        "total_output_tokens": 300,
        "llm_call_count": 2,
    }
    await session.commit()

    async def _failing_handler(t, logs, sess, **_kwargs):
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
            (await verify_session.execute(select(Task).where(Task.id == task.id)))
            .scalars()
            .first()
        )
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
    assert refetched.llm_input_tokens_banked == 1200
    assert refetched.llm_output_tokens_banked == 300
    assert refetched.llm_call_count_banked == 2
    assert refetched.api_call_summary is None
    assert len(refetched.attempt_history or []) == 1
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

    async def _failing_handler(t, logs, sess, **_kwargs):
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
            (await verify_session.execute(select(Task).where(Task.id == task.id)))
            .scalars()
            .first()
        )

    assert refetched is not None
    assert refetched.status == TaskStatus.FAILED
    assert "handler failed" in (refetched.latest_status_message or "").lower()


def _poison_session_factory(project_id):
    """Build a coroutine that GENUINELY aborts a session's transaction.

    Inserts a duplicate-PK pair and flushes (suppressing the IntegrityError)
    so the underlying DB transaction is really dead — the way a lock_timeout /
    statement_timeout-cancelled statement leaves it in production. A fake
    raise without the aborted flush would leave the transaction active and
    hide ordering bugs in the recovery path (rollback-before-read, re-fetch
    after rollback). Mirrors the real-poison pattern in test_reaper's per-row
    collision test.
    """

    async def _poison(sess):
        dup = uuid.uuid4()
        for _ in range(2):
            sess.add(
                Task(
                    id=dup,
                    project_id=project_id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                )
            )
        from sqlalchemy.exc import IntegrityError as _IntegrityError

        with contextlib.suppress(_IntegrityError):
            await sess.flush()

    return _poison


@pytest.mark.asyncio
async def test_process_post_commit_rollup_poison_recovers_and_completes(tl_db, mocker):
    """Burst-2 regression (prod 2026-06-07 01:05): a lock-timeout inside the
    rollup trigger (_spawn_pipeline_task's spawn commit) left the post-commit
    session in pending-rollback; the rollup except block then read ``task.id``
    on the poisoned session and re-raised PendingRollbackError out of
    _process_post_commit entirely (500 to the live agent), and the sections
    after it (direction outcomes, terminal-record helpers, final commit) never
    ran. The hardened except must roll back FIRST, re-fetch the task, and
    complete the request cleanly without corrupting the task."""
    from sqlalchemy.exc import OperationalError as _OperationalError

    session, factory = tl_db
    project = await _make_project(session)
    parent = await _make_task(
        session, project.id, task_type="plan", status=TaskStatus.EVALUATING
    )
    child = await _make_task(
        session,
        project.id,
        # "create" with no pr_url in the payload skips the review-pipeline
        # section and flows straight to the rollup gate (same shape as
        # test_process_post_commit_completed_child_triggers_rollup_check).
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    poison = _poison_session_factory(project.id)

    async def _poison_and_raise(t, sess):
        await poison(sess)
        # Expire the caller's instance the way rollup-internal relock/rollback
        # machinery can in production, then surface the asyncpg lock-timeout
        # shape SQLAlchemy wraps. (The root-conftest no-op expire shim keeps
        # the attributes readable in this harness; the genuine aborted
        # transaction is what exercises the recovery ordering.)
        sess.expire(t)
        raise _OperationalError(
            "INSERT INTO task ...",
            {},
            Exception("canceling statement due to lock timeout"),
        )

    mocker.patch(
        "app.task_lifecycle._check_and_trigger_rollup",
        side_effect=_poison_and_raise,
    )

    payload = AgentStatusPayload(task_id=child.id, status=TaskStatus.COMPLETED)
    result = await _process_post_commit(child.id, payload, _session_factory=factory)

    # The request path completed — no PendingRollbackError cascade.
    assert result.get("status") == "received"

    async with factory() as verify_session:
        refetched = (
            (await verify_session.execute(select(Task).where(Task.id == child.id)))
            .scalars()
            .first()
        )
    assert refetched is not None
    # The task is NOT corrupted by the rollup failure: it keeps the
    # agent-reported terminal status (the rollup retries via its own sweep).
    assert refetched.status == TaskStatus.COMPLETED
    # The poison rows were rolled back, not committed by a later section.
    async with factory() as verify_session:
        stray = (
            (
                await verify_session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.id.notin_([parent.id, child.id]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert stray == []


@pytest.mark.asyncio
async def test_process_post_commit_handler_genuine_poison_still_requeues(tl_db, mocker):
    """Burst-1 cascade class (prod 2026-06-07 00:56): a completion handler
    that dies with the transaction GENUINELY aborted (failed commit inside the
    handler) must still reach the transient-requeue recovery — rollback first,
    re-fetch, persist the requeue — instead of poisoning the recovery writes."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    poison = _poison_session_factory(project.id)

    async def _failing_handler(t, logs, sess, **_kwargs):
        await poison(sess)
        sess.expire(t)
        raise Exception("canceling statement due to statement timeout")

    mocker.patch(
        "app.research_completion._handle_research_completion",
        side_effect=_failing_handler,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result.get("status") == "received"

    async with factory() as verify_session:
        refetched = (
            (await verify_session.execute(select(Task).where(Task.id == task.id)))
            .scalars()
            .first()
        )
    assert refetched is not None
    assert refetched.status == TaskStatus.QUEUED
    assert refetched.attempt_count == 1
    assert (refetched.latest_status_message or "").startswith(
        "Handler hit transient error, requeuing"
    )


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
        return_value=_open_pull_request(head_ref="feat/x"),
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
    mocker.patch("app.github.submit_pr_review", new_callable=AsyncMock)

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
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 5050,
                    "base_sha": "base123",
                    "head_sha": "abc123",
                },
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
        verdict_json=(
            '{"verdict": "APPROVE", "issues": [], '
            '"reviewed_head_sha": "abc123", '
            '"reviewed_base_sha": "base123"}'
        ),
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


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST_CHANGES verification-only downgrade (CI-green spurious-rejection guard)
# ══════════════════════════════════════════════════════════════════════════════


async def _run_request_changes_branch(
    tl_db,
    mocker,
    *,
    verdict_json,
    ci_check_runs,
    original_context=None,
    review_history=None,
    pr_files=None,
):
    """Drive _process_post_commit through the review-verdict handling.

    ``ci_check_runs`` is the value (or side_effect callable) for the
    treat_missing_as_success=False get_check_runs_cached call used by the
    verification-only verdict-flip gate.

    A verification-only REQUEST_CHANGES on authoritatively-green CI no longer
    halts the loop — it FLIPS the verdict to APPROVE before the verdict branch,
    so the PR merges through the normal approve path. The APPROVE merge cluster
    is therefore mocked here (``merge_pull_request`` etc.) so callers can assert
    the merge was attempted. The still-blocks scenarios never reach that path.

    Returns ``(captured_spawn_kwargs, merge_mock, review_task)`` so callers can
    assert whether a fix was spawned, whether the PR merged, and the final task
    state.
    """
    captured_spawn_kwargs = []

    async def _capture_spawn(*args, **kwargs):
        captured_spawn_kwargs.append(kwargs)
        return (None, False)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    # APPROVE-path merge cluster (only exercised when the verdict flips to
    # APPROVE on a verification-only + green-CI PR). Harmless for the
    # still-blocks scenarios, which take the REQUEST_CHANGES fix-spawn branch
    # and never call these.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.task_lifecycle._comment_with_retry", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    # PR is open, not merged; head sha present so the downgrade gate can query CI.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "head": {"sha": "deadbeef", "ref": "feat/x"},
            "base": {"sha": "base-sha", "ref": "main"},
            "state": "open",
            "merged": False,
        },
    )
    # The review-posting step (submit_pr_review) + fallbacks.
    mocker.patch(
        "app.github.submit_pr_review", new_callable=AsyncMock, return_value={"id": 1}
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    # Empty-PR check. Default: a non-empty file list so the empty-PR
    # short-circuit is NOT taken in the still-blocks scenarios. Callers
    # exercising the empty-PR cleanup path pass pr_files=[].
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=(
            [{"filename": "app/x.py"}] if pr_files is None else pr_files
        ),
    )
    # get_check_runs_cached is called twice in the branch with different args:
    #   1) the downgrade gate (treat_missing_as_success=False)
    #   2) the CI-errors-for-fix-context fetch (default treat_missing_as_success)
    # Route by the treat_missing_as_success kwarg so we can control the gate
    # independently while leaving the fix-context fetch benign.
    if callable(ci_check_runs):
        _gate_value = ci_check_runs
    else:

        async def _gate_value(*a, **kw):
            return ci_check_runs

    async def _check_runs_router(*args, **kwargs):
        if kwargs.get("treat_missing_as_success") is False:
            return await _gate_value(*args, **kwargs)
        # fix-context fetch: report no failure so no extra CI errors are appended
        return {
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        }

    mocker.patch("app.github.get_check_runs_cached", side_effect=_check_runs_router)

    session, factory = tl_db
    project = await _make_project(session)
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "X", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context=original_context or {},
    )
    session.add(original)
    await session.commit()
    await session.refresh(original)

    review_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 6060,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "review_loop": {
                "pr_number": 6060,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
                "iteration": 1,
                "review_history": list(review_history or []),
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 6060,
                    "base_sha": "base-sha",
                    "head_sha": "deadbeef",
                },
            },
        },
    )
    session.add(review_task)
    await session.commit()
    await session.refresh(review_task)

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/o/r/pull/6060",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=verdict_json,
    )

    await _process_post_commit(review_task.id, payload, _session_factory=factory)
    await session.refresh(review_task)
    return captured_spawn_kwargs, merge_mock, review_task


_VERIFICATION_ONLY_VERDICT = json.dumps(
    {
        "verdict": "REQUEST_CHANGES",
        "feedback": "Could not run the verify gate in my sandbox.",
        "issues": [
            {
                "description": "Could not run ./check — command not found in sandbox",
                "severity": "major",
            },
            {
                "description": "CI probe returned 401 Unauthorized; unable to verify",
                "severity": "major",
            },
        ],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
)

_CONCRETE_DEFECT_VERDICT = json.dumps(
    {
        "verdict": "REQUEST_CHANGES",
        "feedback": "Found a real bug.",
        "issues": [
            {
                "description": "Could not run ./check in sandbox",
                "severity": "minor",
            },
            {
                "description": "SQL injection: user input is interpolated into the query",
                "severity": "critical",
            },
        ],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
)


@pytest.mark.asyncio
async def test_request_changes_verification_only_ci_green_is_approved_and_merges(
    tl_db, mocker
):
    """Verification-only REQUEST_CHANGES + authoritatively-green CI → treated as
    APPROVE and the PR merges through the normal approve path.

    This is the correctness fix: a verification-only verdict on green CI with no
    concrete defect means "the reviewer had no real objection and CI passed", so
    the PR must land — NOT be left open-but-unmerged (loop stopped without
    review_approved, leaving the ci_passed AND review_approved merge gate forever
    unsatisfied). No fix is spawned; the verdict-flip marker is recorded.
    """
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_VERIFICATION_ONLY_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured == [], "Verification-only + green CI must NOT spawn a fix"
    # The flip routes through the APPROVE branch, which merges the PR.
    assert merge_mock.await_count >= 1, (
        "Verification-only + green CI must merge the PR via the approve path"
    )
    assert merge_mock.await_args.kwargs["expected_head_sha"] == "deadbeef"
    assert (review_task.context or {}).get("verification_only_downgrade") is True


@pytest.mark.asyncio
async def test_request_changes_concrete_defect_ci_green_still_blocks(tl_db, mocker):
    """A concrete code-defect issue is respected even when CI is green → fix spawned."""
    captured, merge_mock, _review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_CONCRETE_DEFECT_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured, "Concrete-defect REQUEST_CHANGES must still spawn a fix"
    merge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_head_request_changes_revokes_ci_wait_parent_before_fix(
    tl_db, mocker
):
    """A parked parent's older approval/bypass cannot outlive a new blocker."""
    old_approval = {
        "iteration": 0,
        "verdict": "APPROVE",
        "feedback": "Earlier reviewer approved this exact head.",
        "issues": [],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
    prior_child_blocker = {
        "iteration": 0,
        "verdict": "REQUEST_CHANGES",
        "feedback": "Earlier child review found an authorization defect.",
        "issues": [
            {
                "description": "Authorization bypass in the admin endpoint",
                "severity": "major",
                "file": "app/admin.py",
                "line": 40,
            }
        ],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_CONCRETE_DEFECT_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
        review_history=[prior_child_blocker],
        original_context={
            "review_loop": {
                "pr_number": 6060,
                "branch": "feat/x",
                "base_branch": "main",
                "ci_waiting": True,
                "ci_passed": True,
                "review_approved": True,
                "review_not_required": True,
                "review_policy_reason": "small diff",
                "verdict": "APPROVE",
                "review_history": [old_approval],
                "review_target": {
                    "project_id": "stale-project",
                    "pr_number": 6060,
                    "base_sha": "base-sha",
                    "head_sha": "deadbeef",
                },
                "head_sha": "deadbeef",
                "base_sha": "base-sha",
            }
        },
    )

    assert captured, "The fix may spawn only after durable parent revocation"
    merge_mock.assert_not_awaited()
    session, _factory = tl_db
    original_id = uuid.UUID(review_task.first_payload["original_task_id"])
    original = await session.get(Task, original_id)
    await session.refresh(original)
    parent_loop = (original.context or {})["review_loop"]
    assert parent_loop["review_approved"] is False
    assert "review_not_required" not in parent_loop
    assert "review_policy_reason" not in parent_loop
    assert "verdict" not in parent_loop
    assert parent_loop["ci_passed"] is True
    assert parent_loop["ci_waiting"] is True
    assert parent_loop["review_history"][-1]["verdict"] == "REQUEST_CHANGES"
    assert parent_loop["review_history"][-1]["review_task_id"] == str(
        review_task.id
    )
    assert parent_loop["issue_ledger"]["open"]
    assert any(
        "SQL injection" in issue["description"]
        for issue in parent_loop["issue_ledger"]["open"]
    )
    assert any(
        "Authorization bypass" in issue["description"]
        for issue in parent_loop["issue_ledger"]["open"]
    )
    assert any(
        "Authorization bypass" in issue["description"]
        for issue in parent_loop["review_history"][-1]["issues"]
    )
    assert parent_loop["review_target"] == {
        "project_id": str(original.project_id),
        "pr_number": 6060,
        "base_sha": "base-sha",
        "head_sha": "deadbeef",
    }


@pytest.mark.asyncio
async def test_request_changes_verification_only_ci_pending_still_blocks(tl_db, mocker):
    """Verification-only issues but CI NOT green (pending) → fix still spawned."""
    captured, merge_mock, _review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_VERIFICATION_ONLY_VERDICT,
        ci_check_runs={
            "status": "in_progress",
            "conclusion": None,
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured, "CI not green (pending) must keep REQUEST_CHANGES blocking"
    merge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_changes_verification_only_no_checks_still_blocks(tl_db, mocker):
    """treat_missing_as_success=False → 'no_checks' is NOT a pass → fix spawned."""
    captured, merge_mock, _review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_VERIFICATION_ONLY_VERDICT,
        ci_check_runs={
            "status": "no_checks",
            "conclusion": None,
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured, "Absence of CI checks must keep REQUEST_CHANGES blocking"
    merge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_changes_verification_only_check_runs_raises_still_blocks(
    tl_db, mocker
):
    """get_check_runs raising → fail-safe: keep REQUEST_CHANGES blocking → fix spawned."""

    async def _raise(*args, **kwargs):
        raise RuntimeError("github 503")

    captured, merge_mock, _review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_VERIFICATION_ONLY_VERDICT,
        ci_check_runs=_raise,
    )
    assert captured, "A check-runs error must keep REQUEST_CHANGES blocking (fail-safe)"
    merge_mock.assert_not_awaited()


# REQUEST_CHANGES whose decision field is DECOUPLED from its own structured
# evidence — the PR #9678 non-convergence root cause. The reviewer emits the
# REQUEST_CHANGES token but lists no blocking (CRITICAL/MAJOR) issue, so the fix
# loop has nothing actionable to fix and churns to the iteration cap on a green,
# mergeable PR. The reconciliation records APPROVE instead.

_EMPTY_ISSUES_VERDICT = json.dumps(
    {
        "verdict": "REQUEST_CHANGES",
        "feedback": (
            "Verified the iteration-5 delta against the prior blockers. No "
            "remaining blocking correctness, security, or coverage issue found "
            "in this pass."
        ),
        "issues": [],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
)

_MINOR_ONLY_VERDICT = json.dumps(
    {
        "verdict": "REQUEST_CHANGES",
        "feedback": "A couple of optional polish notes.",
        "issues": [
            {"description": "Rename this variable for clarity", "severity": "minor"},
            {"description": "Add a docstring to the helper", "severity": "minor"},
        ],
        "reviewed_head_sha": "deadbeef",
        "reviewed_base_sha": "base-sha",
    }
)


@pytest.mark.asyncio
async def test_request_changes_empty_issues_ci_green_is_approved_and_merges(
    tl_db, mocker
):
    """REQUEST_CHANGES with an EMPTY issues array + green CI → APPROVE + merge.

    The exact PR #9678 shape: the reviewer's prose CLEARS the PR ("no remaining
    blocking … issue") while it stamps the REQUEST_CHANGES token, and the issues
    array is empty. Recording that token verbatim spawned a fix with nothing to
    fix → loop churned to the global iteration cap and the task FAILED on a
    green, mergeable PR. The decision must follow the structured evidence: no
    blocking issue on authoritative-green CI → APPROVE through the normal path.
    """
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_EMPTY_ISSUES_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured == [], (
        "Empty-issues REQUEST_CHANGES + green CI must NOT spawn a fix"
    )
    assert merge_mock.await_count >= 1, "must merge via the approve path"
    assert (review_task.context or {}).get(
        "review_verdict_reconciled_to_approve"
    ) is True
    # NOT a verification-only case — the legacy sub-case marker must stay unset.
    assert (review_task.context or {}).get("verification_only_downgrade") is None


@pytest.mark.asyncio
async def test_request_changes_minor_only_ci_green_is_approved_and_merges(
    tl_db, mocker
):
    """REQUEST_CHANGES carrying ONLY MINOR issues + green CI → APPROVE + merge.

    Per the review contract MINOR findings are non-blocking ("a PR with only
    MINOR findings should APPROVE"); they must not drive a fix iteration.
    """
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_MINOR_ONLY_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured == [], "MINOR-only REQUEST_CHANGES + green CI must NOT spawn a fix"
    assert merge_mock.await_count >= 1, "must merge via the approve path"
    assert (review_task.context or {}).get(
        "review_verdict_reconciled_to_approve"
    ) is True
    assert (review_task.context or {}).get("verification_only_downgrade") is None


@pytest.mark.asyncio
async def test_request_changes_no_blocking_issue_ci_pending_still_blocks(tl_db, mocker):
    """No blocking issue but CI NOT green → fail-safe: keep REQUEST_CHANGES.

    The reconciliation only ever flips toward APPROVE under authoritative-green
    CI — a non-green PR keeps blocking and spawns a fix exactly as before.
    """
    captured, merge_mock, _review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_MINOR_ONLY_VERDICT,
        ci_check_runs={
            "status": "in_progress",
            "conclusion": None,
            "failed_logs": "",
            "details": [],
        },
    )
    assert captured, (
        "No-blocking-issue + non-green CI must keep REQUEST_CHANGES blocking"
    )
    merge_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST_CHANGES empty-PR short-circuit (X2 of the fdd07b88 plan) — extracted
# into app/review_request_changes.run_request_changes
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_changes_empty_pr_closes_and_marks_parent_non_terminal(
    tl_db, mocker
):
    """Empty-PR short-circuit: REQUEST_CHANGES on a PR with no file changes
    closes the PR instead of spawning a futile fix, and classifies the rollup
    parent's landing so intermediate staged targets keep waiting downstream.

    With ``classify_rollup_merge_outcome`` reporting a NON-terminal landing
    (branch equivalent to ``main`` but not an integration landing), the
    original task stays EVALUATING with ``pr_closed_empty`` set and the
    broadcast carries that status. No fix is spawned.
    """
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock
    )
    classify_mock = mocker.patch(
        "app.review_request_changes.classify_rollup_merge_outcome",
        new_callable=AsyncMock,
        return_value=(False, False, "main"),
    )
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_CONCRETE_DEFECT_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
        pr_files=[],
    )
    assert captured == [], "Empty PR must NOT spawn a futile fix"
    merge_mock.assert_not_awaited()
    close_mock.assert_awaited_once()
    classify_mock.assert_awaited_once()
    assert review_task.status == TaskStatus.COMPLETED
    assert "closed as empty" in review_task.latest_status_message

    session, _factory = tl_db
    original_id = uuid.UUID(review_task.first_payload["original_task_id"])
    original = await session.get(Task, original_id)
    await session.refresh(original)
    assert (original.context or {}).get("pr_closed_empty") is True
    assert original.status == TaskStatus.EVALUATING

    import app.task_lifecycle as _tl_mod

    broadcast_mock = _tl_mod.broadcast_to_project
    broadcast_mock.assert_awaited()
    _call = broadcast_mock.await_args
    assert _call.args[1] == "task_updated"
    assert _call.args[2]["task_id"] == str(original.id)
    assert _call.args[2]["status"] == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_request_changes_empty_pr_terminal_merge_unblocks_dependents(
    tl_db, mocker
):
    """Terminal variant of the empty-PR cleanup: when the closed PR's branch is
    an integration landing, the original task completes, its direction merge
    counts heal, and its dependents are unblocked
    (``event_driven_unblock_dependents``).
    """
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock
    )
    classify_mock = mocker.patch(
        "app.review_request_changes.classify_rollup_merge_outcome",
        new_callable=AsyncMock,
        return_value=(True, True, "main"),
    )
    landed_mock = mocker.patch(
        "app.review_request_changes.corroborate_and_apply_merged_landed_signal",
        new_callable=AsyncMock,
    )
    refresh_mock = mocker.patch(
        "app.direction_outcomes.refresh_direction_outcomes_after_merge",
        new_callable=AsyncMock,
    )
    unblock_mock = mocker.patch(
        "app.reaper_sweeps.event_driven_unblock_dependents",
        new_callable=AsyncMock,
    )
    captured, merge_mock, review_task = await _run_request_changes_branch(
        tl_db,
        mocker,
        verdict_json=_CONCRETE_DEFECT_VERDICT,
        ci_check_runs={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
        pr_files=[],
    )
    assert captured == [], "Empty PR must NOT spawn a futile fix"
    merge_mock.assert_not_awaited()
    close_mock.assert_awaited_once()
    classify_mock.assert_awaited_once()
    landed_mock.assert_awaited_once()
    refresh_mock.assert_awaited_once()
    unblock_mock.assert_awaited_once()

    session, _factory = tl_db
    original_id = uuid.UUID(review_task.first_payload["original_task_id"])
    original = await session.get(Task, original_id)
    await session.refresh(original)
    assert (original.context or {}).get("pr_closed_empty") is True
    assert original.status == TaskStatus.COMPLETED


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
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(base_ref="research/cycle-1"),
    )
    mocker.patch("app.security.scan_pr_for_injection", side_effect=_scan_side_effect)
    # Merge succeeds
    mocker.patch(
        "app.github.merge_pull_request",
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
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": "abc123", "ref": "blueprint/task-abc123"},
            "base": {"sha": "base123", "ref": "research/cycle-1"},
        },
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


@pytest.mark.asyncio
async def test_process_post_commit_stamps_first_pr_opened(tl_db, mocker):
    """_process_post_commit stamps first_pr_opened_at in project.build_config when
    a create task completes with a PULL_REQUEST artifact.

    The stamp happens before the security scan; the scan is made to fail
    transiently (requires_approval=True path) so the session commits quickly
    while still including the project build_config update.
    """
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

    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/99",
        branch="blueprint/task-abc123",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    await _process_post_commit(task.id, payload, _session_factory=factory)

    async with factory() as verify_session:
        fetched_project = (
            (
                await verify_session.execute(
                    select(Project).where(Project.id == project.id)
                )
            )
            .scalars()
            .first()
        )
    assert fetched_project is not None
    assert (fetched_project.build_config or {}).get("first_pr_opened_at") is not None, (
        "first_pr_opened_at must be stamped in build_config when a create task "
        "completes with a PULL_REQUEST artifact"
    )


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
                "iteration": 1,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 99,
                    "base_sha": "base123",
                    "head_sha": "abc123",
                },
            },
        },
    )

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    # Every approval and merge is bound to the same immutable PR target.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(head_ref="blueprint/task-remfp"),
    )
    mocker.patch(
        "app.github.submit_pr_review", new_callable=AsyncMock, return_value={"id": 1}
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new_callable=AsyncMock,
        return_value={"status": "completed", "conclusion": "success"},
    )
    mocker.patch(
        "app.github.merge_pull_request",
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
        verdict_json=(
            '{"verdict": "APPROVE", "feedback": "LGTM", '
            '"reviewed_head_sha": "abc123", '
            '"reviewed_base_sha": "base123"}'
        ),
        pr_url="https://github.com/owner/repo/pull/99",
        branch="blueprint/task-remfp",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(review_task.id, payload, _session_factory=factory)

    await session.refresh(case)
    assert case.status == ResidualCaseStatus.MERGED
    assert case.updated_at >= now


# ══════════════════════════════════════════════════════════════════════════════
# Inline pre-merge Alembic dual-head guard (parity with the CI-completion path)
# ══════════════════════════════════════════════════════════════════════════════

# A diff that adds a migration whose revision id ("0002") collides with a
# revision already on the target branch — the dual-head vector.
_TL_DUAL_HEAD_DIFF = (
    "diff --git a/backend/alembic/versions/0002_dup.py "
    "b/backend/alembic/versions/0002_dup.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/backend/alembic/versions/0002_dup.py\n"
    '+"""dup migration"""\n'
    '+revision = "0002"\n'
    '+down_revision = "0001"\n'
)


def _patch_dual_head_collision(mocker):
    """Make the inline guard see a same-revision dual-head collision."""
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            return_value=[
                {
                    "filename": "backend/alembic/versions/0002_dup.py",
                    "status": "added",
                    "patch": '+"""dup migration"""\n+revision = "0002"\n+down_revision = "0001"',
                }
            ]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        AsyncMock(return_value=_TL_DUAL_HEAD_DIFF),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        AsyncMock(return_value=["0001_init.py", "0002_existing.py"]),
    )


async def _setup_approve_merge(session, project, *, base_branch="main"):
    """Create original + review tasks wired for the APPROVE inline-merge path."""
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "X", "base_branch": base_branch}],
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
                "pr_number": 5151,
                "branch": "feat/x",
                "base_branch": base_branch,
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        context={
            "review_loop": {
                "pr_number": 5151,
                "branch": "feat/x",
                "base_branch": base_branch,
                "original_task_id": str(original.id),
                "iteration": 1,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 5151,
                    "base_sha": "base123",
                    "head_sha": "abc123",
                },
            },
        },
    )
    session.add(review_task)
    await session.commit()
    await session.refresh(review_task)
    return original, review_task


def payload_for(review_task):
    """Build the APPROVE completion payload for a review task."""
    return AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/5151",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=(
            '{"verdict": "APPROVE", "issues": [], '
            '"reviewed_head_sha": "abc123", '
            '"reviewed_base_sha": "base123"}'
        ),
    )


def _patch_approve_merge_env(mocker):
    """Mocks shared by the APPROVE inline-merge tests (CI green, PR open)."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "head": {"sha": "abc123", "ref": "feat/x"},
            "base": {"sha": "base123", "ref": "main"},
            "state": "open",
            "merged": False,
        },
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new_callable=AsyncMock,
        return_value={"status": "completed", "conclusion": "success"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.github.submit_pr_review", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._comment_with_retry", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)


@pytest.mark.asyncio
async def test_rollup_approve_handoff_copies_exact_authoritative_proof(tl_db, mocker):
    """The parked parent receives history, ledger, and the exact approved target."""
    session, factory = tl_db
    project = await _make_project(session)
    original, review_task = await _setup_approve_merge(session, project)
    original.context = {
        "rollup_pr_number": 5151,
        "review_loop": {
            "ci_waiting": True,
            "ci_passed": False,
            "review_approved": False,
            "review_not_required": True,
            "review_policy_reason": "legacy bypass must be superseded",
            "pr_number": 5151,
            "branch": "feat/x",
            "base_branch": "main",
            "original_task_id": str(original.id),
        },
    }
    session.add(original)
    await session.commit()

    _patch_approve_merge_env(mocker)
    merge_gate = mocker.patch(
        "app.ci_completion._attempt_merge_with_retry_and_escalation",
        new_callable=AsyncMock,
    )

    result = await _process_post_commit(
        review_task.id,
        payload_for(review_task),
        _session_factory=factory,
    )

    assert result == {"status": "accepted"}
    merge_gate.assert_not_awaited()
    await session.refresh(original)
    loop = (original.context or {})["review_loop"]
    assert loop["review_approved"] is True
    assert loop["review_target"] == {
        "project_id": str(project.id),
        "pr_number": 5151,
        "base_sha": "base123",
        "head_sha": "abc123",
    }
    assert loop["head_sha"] == "abc123"
    assert loop["base_sha"] == "base123"
    assert loop["review_history"][-1]["verdict"] == "APPROVE"
    assert loop["review_history"][-1]["reviewed_head_sha"] == "abc123"
    assert loop["review_history"][-1]["reviewed_base_sha"] == "base123"
    ledger = loop["issue_ledger"]
    assert ledger["open"] == []
    assert ledger["resolved"] == []
    assert ledger["schema_version"] == 1
    assert ledger["complete"] is True
    assert ledger["through_entry_key"]
    assert "review_not_required" not in loop
    assert "review_policy_reason" not in loop


@pytest.mark.asyncio
async def test_process_post_commit_review_approve_merge_held_on_dual_head(
    tl_db, mocker
):
    """APPROVE + CI green: a same-revision dual head spawns a migration fix."""
    session, factory = tl_db
    project = await _make_project(session)
    _, review_task = await _setup_approve_merge(session, project)

    _patch_approve_merge_env(mocker)
    _patch_dual_head_collision(mocker)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(fake_fix, True),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    result = await _process_post_commit(
        review_task.id, payload_for(review_task), _session_factory=factory
    )

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs.get("fix_reason") == "migration_guard"
    await session.refresh(review_task)
    # The source PR remains the delivery owner until the exact replacement
    # head lands; dispatching a fix must not make an unlanded PR look complete.
    assert review_task.status == TaskStatus.EVALUATING
    rl = (review_task.context or {}).get("review_loop", {})
    assert rl.get("ci_waiting") is False
    assert rl.get("ci_passed") is False
    # Any code-changing fix invalidates the approval that preceded it.  The
    # replacement head must complete a new exact-target review before merge.
    assert rl.get("review_approved") is False
    assert rl.get("migration_guard_state") == "fix_spawned"
    assert rl.get("migration_guard_fix_spawned") == str(fake_fix.id)
    assert rl.get("ci_fix_spawned") == str(fake_fix.id)
    assert "spawned migration fix" in (review_task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_process_post_commit_review_approve_guard_defer_preserves_ci_wait(
    tl_db, mocker
):
    """A transient inline Alembic guard defer does not spawn a fix."""
    from app.rollup_migration_validation import PremergeMigrationGuardResult

    session, factory = tl_db
    project = await _make_project(session)
    _, review_task = await _setup_approve_merge(session, project)

    _patch_approve_merge_env(mocker)
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=PremergeMigrationGuardResult(
            "defer", "GitHub 503 while fetching PR files"
        ),
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, False),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    result = await _process_post_commit(
        review_task.id, payload_for(review_task), _session_factory=factory
    )

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    spawn_mock.assert_not_called()
    await session.refresh(review_task)
    assert review_task.status == TaskStatus.EVALUATING
    rl = (review_task.context or {}).get("review_loop", {})
    assert rl.get("ci_waiting") is True
    assert rl.get("ci_passed") is True
    assert rl.get("migration_guard_state") == "deferred"
    assert "merge deferred" in (review_task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_process_post_commit_review_approve_merge_proceeds_when_guard_ok(
    tl_db, mocker
):
    """APPROVE + CI green + clean (non-migration) PR: the inline merge fires."""
    session, factory = tl_db
    project = await _make_project(session)
    _, review_task = await _setup_approve_merge(session, project)

    _patch_approve_merge_env(mocker)
    # autouse _default_no_migration_pr fixture already returns a non-migration
    # file from get_pr_files, so the guard returns ok.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    await _process_post_commit(
        review_task.id, payload_for(review_task), _session_factory=factory
    )

    merge_mock.assert_awaited_once()


def test_rollup_handoff_log_uses_precommit_parent_id_snapshot():
    """The post-commit log must not dereference an expired rollup parent."""
    source = _post_commit_sources()
    assert "(parent {_orig_id_str})" in source
    assert "(parent {_orig.id})" not in source


@pytest.mark.asyncio
async def test_small_scope_auto_approve_merge_held_on_dual_head(tl_db, mocker):
    """Non-staging auto_approve_small spawns a migration fix on a dual head."""
    session, factory = tl_db
    project = await _make_project(session)

    direction_id = "dir-auto-approve"
    project.directions = [{"id": direction_id, "title": "d"}]
    session.add(project)
    await session.commit()

    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small", "direction_id": direction_id},
    )
    task.payloads = [{"type": "create", "prompt": "x", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-aa", base_ref="main"
        ),
    )
    # Force the auto_approve_small gate open.
    mocker.patch("app.direction_metrics._compute_acceptance_rate", return_value=0.9)
    mocker.patch(
        "app.review_policy.should_auto_approve_diff",
        return_value=(True, "small clean diff"),
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
    _patch_dual_head_collision(mocker)
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(fake_fix, True),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-aa",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs.get("fix_reason") == "migration_guard"
    await session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is False
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("migration_guard_state") == "fix_spawned"
    assert review_loop.get("migration_guard_fix_spawned") == str(fake_fix.id)
    assert "spawned migration fix" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_small_scope_auto_approve_merge_blocked_by_guard_skips_auto_merge(
    tl_db, mocker
):
    """A deterministic pre-merge guard block spawns a migration fix."""
    session, factory = tl_db
    project = await _make_project(session)

    direction_id = "dir-auto-approve"
    project.directions = [{"id": direction_id, "title": "d"}]
    session.add(project)
    await session.commit()

    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={
            "scope": "small",
            "direction_id": direction_id,
            "review_loop": {
                "head_sha": "deadbeef",
                "polls_since_pr_fetch": 1,
                "mergeable_state": "clean",
                "last_merge_msg": "old merge result",
            },
        },
    )
    task.payloads = [{"type": "create", "prompt": "x", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-aa", base_ref="main"
        ),
    )
    mocker.patch("app.direction_metrics._compute_acceptance_rate", return_value=0.9)
    mocker.patch(
        "app.review_policy.should_auto_approve_diff",
        return_value=(True, "small clean diff"),
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
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=(False, "dual-head: duplicate migration revision"),
    )
    fake_fix = type("FT", (), {"id": uuid.uuid4()})()
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(fake_fix, True),
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-aa",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs.get("fix_reason") == "migration_guard"
    await session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is False
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("migration_guard_state") == "fix_spawned"
    assert review_loop.get("migration_guard_fix_spawned") == str(fake_fix.id)
    assert "spawned migration fix" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_small_scope_auto_approve_guard_defer_preserves_ci_wait(
    tl_db, mocker
):
    """A transient guard defer in the small-scope path keeps the task pollable."""
    from app.rollup_migration_validation import PremergeMigrationGuardResult

    session, factory = tl_db
    project = await _make_project(session)

    direction_id = "dir-auto-approve"
    project.directions = [{"id": direction_id, "title": "d"}]
    session.add(project)
    await session.commit()

    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small", "direction_id": direction_id},
    )
    task.payloads = [{"type": "create", "prompt": "x", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-aa", base_ref="main"
        ),
    )
    mocker.patch("app.direction_metrics._compute_acceptance_rate", return_value=0.9)
    mocker.patch(
        "app.review_policy.should_auto_approve_diff",
        return_value=(True, "small clean diff"),
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
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=PremergeMigrationGuardResult(
            "defer", "GitHub 503 while fetching PR files"
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, False),
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-aa",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    spawn_mock.assert_not_called()
    await session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_passed") is True
    assert review_loop.get("migration_guard_state") == "deferred"
    assert "merge deferred" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_small_scope_auto_approve_auto_finalizes_reserved_migration(
    tl_db, mocker
):
    """Small-scope auto-approve rewrites a reserved provisional migration and
    requeues CI instead of attempting the stale merge."""
    session, factory = tl_db
    project = await _make_project(session)

    direction_id = "dir-auto-finalize"
    project.directions = [{"id": direction_id, "title": "d"}]
    session.add(project)
    await session.commit()

    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small", "direction_id": direction_id},
    )
    task.payloads = [{"type": "create", "prompt": "x", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await allocate_migration_reservation(session, task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-aa", base_ref="main"
        ),
    )
    mocker.patch("app.direction_metrics._compute_acceptance_rate", return_value=0.9)
    mocker.patch(
        "app.review_policy.should_auto_approve_diff",
        return_value=(True, "small clean diff"),
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
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch(
        "app.rollup_migration_validation.enforce_premerge_migration_guard",
        new_callable=AsyncMock,
        return_value=(False, "dual-head: duplicate migration revision"),
    )
    finalization_mock = mocker.patch(
        "app.rollup_management.attempt_pr_migration_finalization",
        new_callable=AsyncMock,
        return_value={
            "pr_number": 77,
            "branch": "blueprint/task-aa",
            "old_revision": "0153",
            "finalized_revision": "0152",
            "finalized_down_revision": "0151",
            "expected_schema_revision": "0152",
        },
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-aa",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result == {"status": "accepted"}
    merge_mock.assert_not_called()
    await session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    review_loop = (task.context or {}).get("review_loop", {})
    # Finalization rewrites the PR head, so the pre-rewrite small-diff bypass
    # cannot remain merge authority for the new bytes.
    assert review_loop.get("review_approved") is False
    assert review_loop.get("authoritative_review_required") is True
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("head_sha") == ""
    assert review_loop.get("polls_since_pr_fetch") == 999
    assert "mergeable_state" not in review_loop
    assert "last_merge_msg" not in review_loop
    assert review_loop.get("pr_number") == 77
    finalization = (task.context or {}).get("_migration_finalization")
    assert finalization is not None
    assert finalization["old_revision"] == "0153"
    assert finalization["finalized_revision"] == "0152"
    finalization_mock.assert_awaited_once()
    assert finalization_mock.await_args.kwargs["promotion_target"] == "main"
    assert (
        "auto-finalized provisional alembic migration"
        in (task.latest_status_message or "").lower()
    )


@pytest.mark.asyncio
async def test_small_scope_intermediate_merge_skips_guard(tl_db, mocker):
    """A staging-branch (intermediate) small-scope merge does NOT invoke the guard."""
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
        {"type": "create", "prompt": "x", "base_branch": "research/cycle-1"}
    ]
    session.add(task)
    await session.commit()
    await session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-bb", base_ref="research/cycle-1"
        ),
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
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    # If the guard ran it would call get_pr_files first; assert it never does.
    guard_files = mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "README.md"}],
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/88",
        branch="blueprint/task-bb",
        artifact_type=ArtifactType.PULL_REQUEST,
    )
    await _process_post_commit(task.id, payload, _session_factory=factory)

    merge_mock.assert_awaited_once()
    guard_files.assert_not_called()


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
# Review-wait respawn reset on fix completion
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fix_reason",
    ["code_review", "ci_failure", "merge_conflict", "migration_guard"],
)
async def test_fix_completion_all_reasons_spawn_exact_head_delta_rereview(
    tl_db, mocker, fix_reason
):
    """Every pushed fix gets a bounded review of its exact pre-fix delta."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "merged": False,
            "state": "open",
            "base": {"sha": "live-base"},
            "head": {"sha": "live-head"},
        },
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
    spawned_review = mocker.Mock(id=uuid.uuid4())
    spawn_mock = mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(spawned_review, True),
    )

    session, factory = tl_db
    project = await _make_project(session)

    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": {
                "pr_number": 42,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
                "fix_reason": fix_reason,
                "iteration": 2,
                "reviewed_head_sha": "pre-fix-head",
            },
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)
    await session.refresh(task)

    spawn_mock.assert_awaited_once()
    spawn_args = spawn_mock.await_args.args
    spawn_kwargs = spawn_mock.await_args.kwargs
    assert spawn_args[1] == "review"
    assert spawn_kwargs["delta_base_sha"] == "pre-fix-head"
    assert spawn_kwargs["review_target"] == {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "live-base",
        "head_sha": "live-head",
    }


@pytest.mark.asyncio
async def test_fix_completion_spawn_failure_creates_alert(tl_db, mocker):
    """When a fix task completes and the review spawn returns (None, False)
    with a terminal spawn reason, the fix task must have requires_approval=True
    and a WARNING Alert must be created with the accurate reason label."""
    from app.models import Alert, AlertSeverity

    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
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

    async def _spawn_fails(*args, **kwargs):
        source_task = args[0]
        # Simulate the real _spawn_pipeline_task behavior of writing the
        # spawn reason to the source task's context before returning None.
        from app.review_pipeline import SPAWN_REASON_KEY, SPAWN_REASON_CAP

        source_task.context = {
            **(source_task.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_CAP,
        }
        return (None, False)

    mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task", side_effect=_spawn_fails
    )

    session, factory = tl_db
    project = await _make_project(session)

    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": {
                "pr_number": 43,
                "branch": "blueprint/test-branch",
                "base_branch": "main",
                "fix_reason": "code_review",
                "iteration": 2,
            },
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)
    await session.refresh(task)

    assert task.requires_approval is True, (
        "fix task must require approval when review spawn fails"
    )
    assert "Cap Exhausted" in (task.latest_status_message or ""), (
        f"status message must mention the spawn reason, "
        f"got: {task.latest_status_message!r}"
    )

    # Verify a WARNING Alert was created for the spawn failure.
    alert = (
        await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.WARNING,
                Alert.dismissed_at.is_(None),
            )
        )
    ).scalars().first()
    assert alert is not None, "must create a WARNING Alert for spawn failure"
    assert "Cap Exhausted" in (alert.title or ""), (
        f"alert title must mention the reason, got: {alert.title!r}"
    )


# ── Fix-task base_branch fallback chain + original_task_id recovery ──────────


async def _make_fix_task_with_payload(
    session,
    project,
    *,
    base_branch: str | None = None,
    original_task_id: str | None = None,
    review_loop: dict | None = None,
) -> Task:
    """Create a COMPLETED fix task with a custom first_payload (optionally
    carrying ``base_branch`` / ``original_task_id``) and review_loop context."""
    task = await _make_task(
        session,
        project.id,
        task_type="fix",
        status=TaskStatus.COMPLETED,
        context={"review_loop": review_loop or {}},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    payload: dict = {"type": "fix", "prompt": "do fix"}
    if base_branch is not None:
        payload["base_branch"] = base_branch
    if original_task_id is not None:
        payload["original_task_id"] = original_task_id
    task.payloads = [payload]
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _patch_fix_completion_github(mocker, spawned_review):
    """Patch the GitHub/security/spawn surface used by the fix-completion branch."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "merged": False,
            "state": "open",
            "base": {"sha": "live-base"},
            "head": {"sha": "live-head"},
        },
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
    spawn_mock = mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(spawned_review, True),
    )
    return spawn_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rl_base_branch,payload_base_branch,project_integration,expected_base_branch",
    [
        # Branch 1: review_loop.base_branch wins over everything.
        ("rl-main", "payload-main", None, "rl-main"),
        # Branch 2: task_payload.base_branch fallback when review_loop lacks it.
        (None, "payload-main", None, "payload-main"),
        # Branch 3 (terminal/default arm): falls through to
        # _shared_state.get_project_integration_branch(_fix_project).
        (None, None, "project-integration", "project-integration"),
    ],
    ids=["review_loop", "task_payload", "project_integration_default"],
)
async def test_fix_completion_base_branch_fallback_chain(
    tl_db,
    mocker,
    rl_base_branch,
    payload_base_branch,
    project_integration,
    expected_base_branch,
):
    """Every arm of the base_branch fallback chain is exercised, including the
    terminal/default arm that resolves from the project's integration branch."""
    spawned_review = mocker.Mock(id=uuid.uuid4())
    spawn_mock = _patch_fix_completion_github(mocker, spawned_review)

    session, factory = tl_db
    project = await _make_project(session)
    if project_integration is not None:
        project.integration_branch = project_integration
        session.add(project)
        await session.commit()

    review_loop = {"pr_number": 50, "branch": "blueprint/test-branch"}
    if rl_base_branch is not None:
        review_loop["base_branch"] = rl_base_branch
    task = await _make_fix_task_with_payload(
        session,
        project,
        base_branch=payload_base_branch,
        review_loop=review_loop,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    spawn_mock.assert_awaited_once()
    # _spawn_pipeline_task(task, "review", session, pr_number, branch, base_branch, ...)
    spawn_args = spawn_mock.await_args.args
    assert spawn_args[1] == "review"
    assert spawn_args[5] == expected_base_branch, (
        f"expected base_branch {expected_base_branch!r}, got {spawn_args[5]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rl_original_task_id,payload_original_task_id,expected_original_task_id",
    [
        # Found: review_loop.original_task_id is passed through to the spawn.
        ("ci-task-rl", None, "ci-task-rl"),
        # Found: task_payload.original_task_id fallback when review_loop lacks it.
        (None, "ci-task-payload", "ci-task-payload"),
        # Not found: neither review_loop nor task_payload carries it — the
        # spawn receives an empty string (no stale value leaking through).
        (None, None, ""),
    ],
    ids=["review_loop", "task_payload", "not_found"],
)
async def test_fix_completion_original_task_id_recovery(
    tl_db,
    mocker,
    rl_original_task_id,
    payload_original_task_id,
    expected_original_task_id,
):
    """The original CI task id is recovered from review_loop / first_payload and
    forwarded to the re-review spawn; when absent it resolves to an empty string."""
    spawned_review = mocker.Mock(id=uuid.uuid4())
    spawn_mock = _patch_fix_completion_github(mocker, spawned_review)

    session, factory = tl_db
    project = await _make_project(session)

    review_loop = {
        "pr_number": 51,
        "branch": "blueprint/test-branch",
        "base_branch": "main",
    }
    if rl_original_task_id is not None:
        review_loop["original_task_id"] = rl_original_task_id
    task = await _make_fix_task_with_payload(
        session,
        project,
        original_task_id=payload_original_task_id,
        review_loop=review_loop,
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    spawn_mock.assert_awaited_once()
    spawn_kwargs = spawn_mock.await_args.kwargs
    assert spawn_kwargs["original_task_id"] == expected_original_task_id, (
        f"expected original_task_id {expected_original_task_id!r}, "
        f"got {spawn_kwargs['original_task_id']!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Exact-target merge authority
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dirty_target_is_held_before_merge_submission(tl_db, mocker):
    """A live dirty target is rejected before the expected-head merge PUT."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._spawn_pipeline_task", new_callable=AsyncMock)
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
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-deadbeef",
            base_ref="research/cycle-1",
            mergeable_state="dirty",
        ),
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

    merge_mock.assert_not_called()
    await session.refresh(task)
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("authoritative_review_required") is True


@pytest.mark.asyncio
async def test_is_intermediate_honors_custom_staging_prefix(tl_db, mocker):
    """A small-scope create task whose base branch matches a NON-default
    configured staging prefix must be classified as intermediate (is_intermediate
    =True), routing through the policy-bound merge path. Regression guard for the bug
    where the small-diff path hardcoded 'research/'/'plan/' instead of the
    per-project staging_branch_prefixes.
    """
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

    spawn_mock = mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, False),
    )
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
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=_open_pull_request(
            head_ref="blueprint/task-deadbeef", base_ref="feature/cycle-1"
        ),
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

    merge_mock.assert_awaited_once()
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_auto_merge_clean_state_defers_under_blueprint_not_review(tl_db, mocker):
    """A clean-but-unmerged PR parks for Blueprint's next exact-target retry."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.review_merge_authority.asyncio.sleep", new_callable=AsyncMock)

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
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "not mergeable"},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "mergeable_state": "clean",
            "base": {"sha": "base123", "ref": "research/cycle-1"},
            "head": {"sha": "abc123", "ref": "blueprint/task-deadbeef"},
        },
    )
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "backend/app/foo.py"}],
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
    @pytest.mark.parametrize("payload_type", ["review"])
    async def test_review_cancel_does_not_close_reviewed_pr(
        self, tl_db, mocker, payload_type
    ):
        """A reviewer references the implementation PR but never owns it."""
        from app import task_lifecycle as tl

        session, _ = tl_db
        project = Project(name="p", user_id="u")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        task = Task(
            project_id=project.id,
            payloads=[{"type": payload_type, "pr_number": 15138}],
            status=TaskStatus.CANCELLED,
            context={"review_loop": {"pr_number": 15138}},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        token_mock = AsyncMock(return_value=("tok", "Owner", "Repo"))
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new=token_mock,
        )
        close_mock = AsyncMock(return_value=True)
        mocker.patch("app.github.close_pull_request", new=close_mock)

        closed = await tl.close_orphan_pr_if_any(
            task, session, reason="duplicate_review"
        )

        assert closed is False
        token_mock.assert_not_called()
        close_mock.assert_not_called()

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
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
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
    # Use a non-strategic task type so the post-commit task acquisition takes
    # the plain task-only lock path rather than the strategic Project→Task
    # pair-lock (which relies on FOR UPDATE skip_locked that the SQLite test
    # harness does not honor). The real linked cycle below exercises the
    # current reconciliation lookup contract.
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.FAILED,
        context={"strategic_cycle_id": str(uuid.uuid4())},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    cycle = StrategicCycle(
        id=task.context["strategic_cycle_id"],
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow(),
    )
    session.add(cycle)
    await session.commit()
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)

    # Must return cleanly, not raise PendingRollbackError on the final commit.
    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"
    assert captured["session"] is not session


@pytest.mark.asyncio
async def test_post_commit_terminal_expires_migration_reservation_on_failure(
    tl_db, mocker
):
    """T4 (fdd07b88 §6.2 gap): a FAILED task holding an ACTIVE migration
    reservation must have it expired by the terminal-tail stage
    (run_post_commit_terminal -> _expire_migration_reservation_if_terminal),
    freeing the provisional revision id for re-allocation. Exercises the
    extracted app.task_lifecycle_post_commit_terminal module end-to-end via
    _process_post_commit rather than mocking the helper out."""
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    task.payloads = [{"type": "create", "prompt": "x", "base_branch": "main"}]
    session.add(task)
    await session.commit()
    await session.refresh(task)
    reservation = await allocate_migration_reservation(session, task)
    assert reservation.status == MigrationReservationStatus.ACTIVE

    task.status = TaskStatus.FAILED
    session.add(task)
    await session.commit()

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)
    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"

    await session.refresh(reservation)
    assert reservation.status == MigrationReservationStatus.EXPIRED


@pytest.mark.asyncio
async def test_completion_dispatch_requeues_on_transient_handler_error(
    tl_db, mocker
):
    """S3 (fdd07b88): a completion handler raising a transient error (matches
    the keyword allowlist) must requeue the task rather than fail it.
    Exercises the extracted app.task_lifecycle_completion_dispatch module
    end-to-end via _process_post_commit rather than mocking the stage out."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="memo",
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    mocker.patch(
        "app.task_completion._handle_memo_completion",
        new_callable=AsyncMock,
        side_effect=RuntimeError("connection refused while posting result"),
    )
    payload = AgentStatusPayload(
        task_id=task.id, status=TaskStatus.COMPLETED, logs="done"
    )

    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"

    await session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 1


@pytest.mark.asyncio
async def test_completion_dispatch_fails_task_on_non_transient_handler_error(
    tl_db, mocker
):
    """S3 (fdd07b88): a completion handler raising a non-transient error must
    mark the task FAILED with a classified failure category rather than
    requeue. Exercises the extracted app.task_lifecycle_completion_dispatch
    module end-to-end via _process_post_commit rather than mocking the stage
    out."""
    session, factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="memo",
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    mocker.patch(
        "app.task_completion._handle_memo_completion",
        new_callable=AsyncMock,
        side_effect=ValueError("malformed memo payload"),
    )
    payload = AgentStatusPayload(
        task_id=task.id, status=TaskStatus.COMPLETED, logs="done"
    )

    result = await _process_post_commit(task.id, payload, _session_factory=factory)
    assert result["status"] == "received"

    await session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "Completed but handler failed" in (task.latest_status_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cycle_outcome,expect_failure",
    [
        # Phase-1 chained phase-2 successfully → cycle advanced → NOT a stall.
        ("phase2_running", False),
        # Phase-1 genuinely stuck in its own running outcome → still caught.
        ("phase1_running", True),
    ],
)
async def test_strategic_backstop_skips_advanced_cycle(
    tl_db, mocker, cycle_outcome, expect_failure
):
    """Regression: the lifecycle backstop must only mark a strategic phase failed
    when the cycle is still stuck in THAT phase. A phase-1 (design_doc_update)
    task whose cycle has advanced to phase2_running succeeded (it chained phase-2)
    and must NOT be marked failed_phase1 — that false failure inflated
    _strategic_failures toward the circuit breaker and produced the bogus
    'strategic review complete … but FAILED' reports. A cycle still in
    phase1_running IS a genuine stall and must still be caught."""
    session, factory = tl_db
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)

    class _Cycle:
        outcome = cycle_outcome

    mocker.patch(
        "app.strategic_cycle.get", new_callable=AsyncMock, return_value=_Cycle()
    )
    mark = mocker.patch("app.strategic_cycle.mark_failure", new_callable=AsyncMock)

    project = await _make_project(session)
    cycle_id = str(uuid.uuid4())
    task = await _make_task(
        session,
        project.id,
        task_type="research",
        status=TaskStatus.FAILED,
        context={"strategic_cycle_id": cycle_id},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    cycle = StrategicCycle(
        id=cycle_id,
        project_id=project.id,
        outcome=cycle_outcome,
        phase1_task_id=task.id,
        phase1_started_at=_utcnow(),
    )
    session.add(cycle)
    await session.commit()
    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)
    await _process_post_commit(task.id, payload, _session_factory=factory)

    if expect_failure:
        mark.assert_awaited_once()
        assert mark.await_args.kwargs.get("phase") == "phase1"
    else:
        mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_strategic_handler_failure_reconciles_cycle_immediately(
    tl_db, mocker
):
    """A failed COMPLETED handler must close its still-running strategic phase.

    Handler recovery changes the current task to FAILED after the agent payload
    has already been committed as COMPLETED. The lifecycle backstop must use
    that current status rather than the stale payload status.
    """
    session, factory = tl_db
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._handle_strategic_completion",
        new_callable=AsyncMock,
        side_effect=RuntimeError("strategic completion failed"),
    )

    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="strategic_review",
        status=TaskStatus.COMPLETED,
        context={"strategic_cycle_id": "sc-handler-failure"},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    cycle = StrategicCycle(
        id="sc-handler-failure",
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow(),
        phase2_started_at=_utcnow(),
    )
    session.add(cycle)
    await session.commit()

    emit = mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed", new_callable=AsyncMock
    )

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.COMPLETED)
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result["status"] == "received"
    await session.refresh(task)
    await session.refresh(cycle)
    assert task.status == TaskStatus.FAILED
    assert cycle.outcome == "failed_phase2"
    assert cycle.phase2_completed_at is not None
    # Durable terminal state and the connected UI must not diverge: the emit is
    # driven by the reconciled cycle, not by the (COMPLETED) payload status.
    emit.assert_awaited_once()
    emit_kwargs = emit.await_args.kwargs
    assert emit.await_args.args[2] == 2
    assert emit_kwargs["cycle_id"] == "sc-handler-failure"
    assert emit_kwargs["phase_state"] == "failed_phase2"
    assert emit_kwargs["completion"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_pending_marker_late_failed_payload_cannot_regress_completed_phase1(
    tl_db, mocker
):
    """A late FAILED payload must not run the legacy strategic cycle writer.

    The persisted task is COMPLETED and its continuation is pending while the
    cycle has already advanced to phase 2. This uses a real strategic phase-1
    task type so the old task-type-derived writer is exercised rather than
    bypassed by a research fixture.
    """
    session, factory = tl_db
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    emit = mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed", new_callable=AsyncMock
    )
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="design_doc_update",
        status=TaskStatus.COMPLETED,
        context={
            "_post_commit_pending_meta": {"generation_token": "continuation-1"}
        },
    )
    task.post_commit_pending = True
    session.add(task)
    cycle = StrategicCycle(
        id="sc-late-failed-payload",
        project_id=project.id,
        outcome="phase2_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow(),
        phase1_completed_at=_utcnow(),
        phase2_started_at=_utcnow(),
    )
    session.add(cycle)
    await session.commit()

    payload = AgentStatusPayload(task_id=task.id, status=TaskStatus.FAILED)
    result = await _process_post_commit(task.id, payload, _session_factory=factory)

    assert result["status"] == "received"
    await session.refresh(cycle)
    await session.refresh(task)
    await session.refresh(project)
    assert cycle.outcome == "phase2_running"
    assert task.status == TaskStatus.COMPLETED
    assert "_strategic_failures" not in (project.vision or {})
    emit.assert_not_awaited()


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
# the docs PR via the direct merge helper instead of spawning review).


@pytest.mark.asyncio
async def test_main_branch_non_rollup_skips_review_auto_merges(
    tl_db, mocker
):
    """A non-rollup single task landing directly into automain (base_branch=main,
    not intermediate) skips the model review and routes through the auto-merge
    path. CI + the promotion gate serve as the backstop, per the v24 cost plan.
    """
    session, factory = tl_db
    project = await _make_project(session)

    # docs task with any scope on main branch — all non-intermediate tasks now
    # skip review regardless of auto-approve eligibility.
    task = await _make_task(
        session,
        project.id,
        task_type="docs",
        status=TaskStatus.IN_PROGRESS,
        context={
            "scope": "small",
        },
    )
    # base_branch=main → not a staging branch → is_intermediate is False
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
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            **_open_pull_request(head_ref="blueprint/task-docs", base_ref="main"),
            "additions": 5,
            "deletions": 1,
            "changed_files": 1,
        },
    )
    merge_spy = mocker.patch(
        "app.github.merge_pull_request",
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
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "docs/readme.md", "status": "modified"}],
    )
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    review_calls = []

    async def _capture_spawn(src_task, ptype, *a, **kw):
        review_calls.append(ptype)
        return (None, False)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/77",
        branch="blueprint/task-docs",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    # Non-intermediate task must take the auto-merge path, not spawn a review.
    merge_spy.assert_awaited()
    assert "review" not in review_calls, (
        "non-rollup main-branch task must skip review and auto-merge "
        "(v24 cost plan: CI + promotion gate backstop)"
    )


@pytest.mark.asyncio
async def test_large_scope_intermediate_still_spawns_review(tl_db, mocker):
    """A large-scope intermediate (staging-branch) task that feeds into a
    rollup PR still gets the full model review dispatch. Only non-rollup single
    tasks landing directly into automain skip review per the v24 cost plan.
    """
    session, factory = tl_db
    project = await _make_project(session)

    task = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "large"},
    )
    # base_branch matching staging prefix → is_intermediate=True
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
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "README.md", "status": "modified"}],
    )
    review_spawns = []

    async def _capture_spawn(src_task, ptype, *a, **kw):
        review_spawns.append(ptype)
        mock_task = mocker.MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.first_payload = {"type": "review"}
        mock_task.status = TaskStatus.QUEUED
        return (mock_task, True)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/owner/repo/pull/88",
        branch="blueprint/task-abc",
        artifact_type=ArtifactType.PULL_REQUEST,
    )

    await _process_post_commit(task.id, payload, _session_factory=factory)

    assert "review" in review_spawns, (
        "large-scope intermediate (rollup-chain) task must still spawn a review"
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
            "VERDICT: REQUEST_CHANGES\n"
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
        assert _resolve_fallback_verdict('"REQUEST_CHANGES"') == "REQUEST_CHANGES"

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
            "app.dep_lifecycle._mark_unblocked_dependents",
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
            "app.dep_lifecycle._mark_unblocked_dependents",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        )

        # Must not raise.
        assert await _mark_dependents_warm_claimable(landed) is None


# ══════════════════════════════════════════════════════════════════════════════
# Forward-quality grader resume is registry-driven for security_audit.
#
# A security_audit source parks EVALUATING behind an independent grader; when the
# grader's verdict returns, _resolve_quality_grader_completion must re-dispatch
# the source to its REGISTRY completion handler (not a hardwired research-only
# map). The source stores plan_output as {"findings": [...]}, so the resume
# reconstruction must round-trip that dict shape back to the findings JSON array
# the handler's parser expects.
# ══════════════════════════════════════════════════════════════════════════════


async def _make_audit_source_and_grader(session, *, verdict_passed: bool):
    """A parked security_audit source + its independent grader, mid-grade."""
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY

    project = await _make_project(session)
    findings = [
        {
            "severity": "high",
            "title": "Missing auth check",
            "description": "Endpoint lacks @require_role.",
            "fix_prompt": "Add @require_role('admin').",
            "file_path": "app/routers/admin.py",
            "line_number": 7,
        },
    ]
    source = Task(
        project_id=project.id,
        payloads=[{"type": "security_audit", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        plan_output={"findings": findings},  # dict shape the audit handler writes
        context={},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    grader = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id)},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    source.context = {"awaiting_quality_grader": str(grader.id)}
    session.add(grader)
    session.add(source)
    await session.commit()
    await session.refresh(grader)
    return project, source, grader


def _quiet_audit_completion_io(mocker):
    """Silence the GitHub and broadcast IO the audit handler performs."""
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )
    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.task_completion.get_open_pull_requests", new=AsyncMock(return_value=[])
    )
    mocker.patch(
        "app.task_completion.create_pr_comment", new=AsyncMock(return_value=None)
    )


@pytest.mark.asyncio
async def test_grader_resume_security_audit_pass_spawns_from_dict_plan_output(
    tl_db, mocker
):
    """A PASS grade re-dispatches the security_audit source through the registry
    and spawns fix children, reconstructed from the dict-shaped plan_output."""
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, _ = tl_db
    _quiet_audit_completion_io(mocker)
    _project, source, grader = await _make_audit_source_and_grader(
        session, verdict_passed=True
    )

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='{"verdict": "pass", "reasoning": "audit is well-grounded"}',
    )
    await _resolve_quality_grader_completion(grader, payload, session)

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == source.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        "PASS grade must spawn the fix child from stored findings"
    )
    await session.refresh(source)
    assert source.status == TaskStatus.EVALUATING
    assert (source.context or {}).get("quality_grader", {}).get("passed") is True


@pytest.mark.asyncio
async def test_grader_resume_security_audit_fail_spawns_nothing_and_fails(
    tl_db, mocker
):
    """A FAIL grade re-dispatches the source, which marks itself FAILED and
    spawns no fix children (fail closed for an ungrounded audit)."""
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, _ = tl_db
    _quiet_audit_completion_io(mocker)
    _project, source, grader = await _make_audit_source_and_grader(
        session, verdict_passed=False
    )

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='{"verdict": "fail", "reasoning": "cited vulnerability absent at HEAD"}',
    )
    await _resolve_quality_grader_completion(grader, payload, session)

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == source.id)))
        .scalars()
        .all()
    )
    assert children == [], "FAIL grade must spawn no fix tasks"
    await session.refresh(source)
    assert source.status == TaskStatus.FAILED
    assert (source.context or {}).get("evaluation_gate", {}).get("passed") is False


@pytest.mark.parametrize("lane", ["quality", "challenge"])
@pytest.mark.parametrize(
    "violation",
    [
        "wrong_awaited_id",
        "boolean_awaited",
        "cross_project",
        "agent_created",
        "wrong_type",
        "parented",
    ],
)
@pytest.mark.asyncio
async def test_sidecar_resolver_rejects_unauthorized_or_mismatched_source(
    tl_db, mocker, lane, violation
):
    """Only the exact backend-owned sidecar may write a parked source verdict."""
    from app.task_evaluators import (
        FINDING_CHALLENGE_SOURCE_KEY,
        FINDING_CHALLENGE_VERDICT_KEY,
        QUALITY_GRADER_SOURCE_KEY,
        QUALITY_GRADER_VERDICT_KEY,
    )
    from app.task_lifecycle import (
        _resolve_finding_challenge_completion,
        _resolve_quality_grader_completion,
    )

    session, _ = tl_db
    source_project = await _make_project(session)
    sidecar_project = (
        await _make_project(session) if violation == "cross_project" else source_project
    )
    source = Task(
        project_id=source_project.id,
        payloads=[{"type": "research", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        plan_output=[],
        context={},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    if lane == "quality":
        source_key = QUALITY_GRADER_SOURCE_KEY
        awaiting_key = "awaiting_quality_grader"
        verdict_key = QUALITY_GRADER_VERDICT_KEY
        sidecar_context = {source_key: str(source.id)}
        resolver = _resolve_quality_grader_completion
        logs = '{"verdict": "pass", "reasoning": "forged"}'
    else:
        source_key = FINDING_CHALLENGE_SOURCE_KEY
        awaiting_key = "awaiting_finding_challenge"
        verdict_key = FINDING_CHALLENGE_VERDICT_KEY
        sidecar_context = {
            source_key: str(source.id),
            "challenged_boundary": "reproduce_absent",
            "challenged_first_verdict": "absent",
        }
        resolver = _resolve_finding_challenge_completion
        logs = '{"agreement": "disagrees", "reason": "forged"}'

    sidecar = Task(
        project_id=sidecar_project.id,
        payloads=[
            {
                "type": "create" if violation == "wrong_type" else "investigate",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="AGENT" if violation == "agent_created" else "ORCHESTRATOR",
        parent_task_id=source.id if violation == "parented" else None,
        context=sidecar_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    source.context = {
        awaiting_key: (
            str(uuid.uuid4())
            if violation == "wrong_awaited_id"
            else (True if violation == "boolean_awaited" else str(sidecar.id))
        )
    }
    session.add(source)
    session.add(sidecar)
    await session.commit()
    await session.refresh(source)
    await session.refresh(sidecar)

    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )
    payload = AgentStatusPayload(
        task_id=sidecar.id,
        status=TaskStatus.COMPLETED,
        logs=logs,
    )
    await resolver(sidecar, payload, session)

    await session.refresh(source)
    assert source.status == TaskStatus.EVALUATING
    assert (source.context or {}).get(awaiting_key) is not None
    assert verdict_key not in (source.context or {})
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_park_wake_delivery_is_exactly_once(tl_db):
    """Park a task on an external wake source, fire it, then run the leader-owned
    wake-delivery worker TWICE — the parked task must advance EXACTLY ONCE.

    Exercises the full park-and-wake seam end to end against real production
    code: ``park_task`` (release slot/VM + durable registration), the firing side
    ``fire_task_wakes`` (PENDING -> FIRED), and the worker
    ``sweep_deliver_task_wakes`` (claim FIRED rows -> ``deliver_wake`` advances
    the task once). A second worker pass must be a no-op: the task stays QUEUED,
    the registration stays DELIVERED, its ``delivered_at`` is unchanged, and it
    was delivered exactly once (``attempt_count == 1``).
    """
    session, _factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        context={
            "failure_class": "CODE_ERROR",
            "failure_category": "CODE_ERROR",
            "blocker": {"source": "agent"},
            "_blocked_escalated": True,
            "_precondition_escalated_key": "dep",
            "_blocked_optimistic_retries": 2,
            "_retry_after_resets": 3,
            "_dispatch_prep_failures": 3,
            "provision_failures": 1,
            "llm_account_id": "acct-1",
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_failure_class_history": ["SCOPE_OVERFLOW"],
        },
    )
    task.last_heartbeat = _utcnow() - timedelta(minutes=10)
    task.started_at = _utcnow() - timedelta(minutes=11)
    task.retry_after_until = datetime(2099, 1, 1)

    # --- Park the task on the compute-job-terminal source. -------------------
    reg = await park_task(
        session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref="job-abc",
        source_metadata={"compute_job_id": "job-abc"},
        wake_action=WakeAction.RESUME,
        reason="waiting on compute job job-abc",
    )
    await session.commit()
    await session.refresh(task)
    await session.refresh(reg)

    # Parking releases the slot/VM and stamps operator-visible metadata, and is
    # distinguishable from BLOCKED / EVALUATING by the PARKED status itself.
    assert task.status == TaskStatus.PARKED
    assert task.parked_at is not None
    assert task.park_reason == "waiting on compute job job-abc"
    assert task.vm_stopped_at is not None
    assert reg.delivery_state == WakeDeliveryState.PENDING
    assert reg.wake_action == WakeAction.RESUME

    # --- Worker before the source fires: nothing to deliver. ----------------
    delivered_pre = await sweep_deliver_task_wakes(session, _utcnow())
    await session.refresh(task)
    await session.refresh(reg)
    assert delivered_pre == 0
    assert task.status == TaskStatus.PARKED
    assert reg.delivery_state == WakeDeliveryState.PENDING

    # --- Source fires: PENDING -> FIRED. ------------------------------------
    fired = await fire_task_wakes(
        session,
        wake_source_kind="compute_job_terminal",
        wake_source_ref="job-abc",
    )
    await session.commit()
    await session.refresh(reg)
    assert fired == 1
    assert reg.delivery_state == WakeDeliveryState.FIRED
    assert reg.fired_at is not None

    # --- Worker pass #1: delivers exactly once. -----------------------------
    delivered_1 = await sweep_deliver_task_wakes(session, _utcnow())
    await session.refresh(task)
    await session.refresh(reg)
    assert delivered_1 == 1
    assert task.status == TaskStatus.QUEUED  # RESUME re-queues for dispatch
    assert task.parked_at is None
    assert task.park_reason is None
    assert task.vm_instance_id is None
    assert task.last_heartbeat is None
    assert task.started_at is None
    ctx = task.context or {}
    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    assert "blocker" not in ctx
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert "_blocked_optimistic_retries" not in ctx
    assert "_retry_after_resets" not in ctx
    assert "_dispatch_prep_failures" not in ctx
    assert "provision_failures" not in ctx
    assert "llm_account_id" not in ctx
    assert task.retry_after_until is None
    assert ctx["_previous_failure_class"] == "SCOPE_OVERFLOW"
    assert ctx["_failure_class_history"] == ["SCOPE_OVERFLOW"]
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "resumed"
    assert reg.attempt_count == 1
    first_delivered_at = reg.delivered_at
    assert first_delivered_at is not None

    # --- Worker pass #2: strict no-op (exactly-once). -----------------------
    delivered_2 = await sweep_deliver_task_wakes(session, _utcnow())
    await session.refresh(task)
    await session.refresh(reg)
    assert delivered_2 == 0
    assert task.status == TaskStatus.QUEUED  # NOT re-advanced
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.delivered_at == first_delivered_at  # unchanged — delivered once
    assert reg.attempt_count == 1  # the FIRED filter never re-selected it

    # Exactly one delivered registration exists for the task.
    delivered_rows = (
        (
            await session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.task_id == task.id,
                    TaskWakeRegistration.delivery_state == WakeDeliveryState.DELIVERED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(delivered_rows) == 1


@pytest.mark.asyncio
async def test_park_task_vm_instance_id_invariant(tl_db):
    """park_task must clear vm_instance_id=None on the task row.

    Regression test for the PARKED vm_instance_id invariant:
    - After park_task returns, task.vm_instance_id must be None.
    - The pre-park vm_instance_id is still available to the caller for VM teardown
      if captured before calling park_task (simulates agent_status capture pattern).
    """
    session, _factory = tl_db
    project = await _make_project(session)
    original_vm_id = f"agent-vm-{uuid.uuid4().hex}"
    task = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=original_vm_id,
    )

    # Capture before parking — mirrors the agent_status endpoint's pattern.
    captured_vm_id = task.vm_instance_id
    assert captured_vm_id == original_vm_id, (
        "precondition: vm_instance_id set before park"
    )

    await park_task(
        session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref="job-xyz",
        source_metadata={"compute_job_id": "job-xyz"},
        wake_action=WakeAction.RESUME,
        reason="waiting on compute job",
    )

    # The invariant: park_task clears vm_instance_id so parked tasks are
    # excluded from the executable_statuses projection in aggregation.
    assert task.vm_instance_id is None, (
        "park_task must clear vm_instance_id; parked task still had "
        f"vm_instance_id={task.vm_instance_id!r}"
    )
    # The caller's captured copy still holds the original id for teardown.
    assert captured_vm_id == original_vm_id, (
        "caller-captured vm_id must be unaffected by park_task clearing the field"
    )
    assert task.status == TaskStatus.PARKED
    assert task.vm_stopped_at is not None


@pytest.mark.asyncio
async def test_park_task_retires_leased_pool_member_before_clearing_vm_id(
    tl_db, mocker
):
    """park_task must release a LEASED warm-pool member before dropping linkage."""
    session, _factory = tl_db
    mocker.patch("app.warm_pool.is_warm_pool_enabled", return_value=True)

    project = await _make_project(session)
    original_vm_id = f"agent-vm-{uuid.uuid4().hex}"
    task = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=original_vm_id,
    )
    pool_member = PoolVm(
        vm_instance_id=original_vm_id,
        project_id=project.id,
        state=PoolVmState.LEASED,
        leased_task_id=task.id,
    )
    idle_member = PoolVm(
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        project_id=project.id,
        state=PoolVmState.IDLE,
    )
    session.add(pool_member)
    session.add(idle_member)
    await session.commit()

    await park_task(
        session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref="job-xyz",
        source_metadata={"compute_job_id": "job-xyz"},
        wake_action=WakeAction.RESUME,
        reason="waiting on compute job",
    )
    await session.commit()
    await session.refresh(task)
    await session.refresh(pool_member)
    await session.refresh(idle_member)

    assert task.vm_instance_id is None
    assert pool_member.state == PoolVmState.RETIRING
    assert pool_member.leased_task_id is None
    assert idle_member.state == PoolVmState.IDLE


@pytest.mark.asyncio
async def test_compute_wake_delivery_commit_contract_is_truthful(tl_db, mocker):
    """deliver_wake on a compute-FINALIZE registration uses the caller's single
    commit to atomically cover the FIRED→DELIVERED flip, park-marker clearing, and
    task finalization.  deliver_wake itself does NOT commit — the caller owns the
    commit.  Repeated calls must return False without re-advancing the task.

    The contract: for wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND with
    wake_action == FINALIZE, deliver_wake mutates session state but does not commit.
    The caller (sweep or test) commits exactly once after deliver_wake returns True,
    making the DELIVERED state and task advance durable atomically.
    """
    from app.models import WakeAction, WakeDeliveryState, ComputeJob, ComputeJobStatus
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

    session, _factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    # Create a real ComputeJob row so deliver_wake can resolve it (a missing or
    # non-UUID wake_source_ref takes the "job_record_missing" early-exit path).
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=task.id,
        backend="local",
        machine_type="e2-standard-2",
        command="echo done",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.SUCCEEDED,
    )
    session.add(job)
    await session.commit()

    # Park the task on the compute-job-terminal wake source with FINALIZE action.
    reg = await park_task(
        session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        source_metadata={"compute_job_id": str(job.id)},
        wake_action=WakeAction.FINALIZE,
        reason="waiting on compute job",
    )
    await session.commit()
    await session.refresh(task)
    await session.refresh(reg)

    assert task.status == TaskStatus.PARKED
    # park_task's catch-up path fires the registration immediately when the
    # ComputeJob is already terminal at park time — no separate fire_task_wakes call.
    assert reg.delivery_state == WakeDeliveryState.FIRED

    # Stub out the completion handler (owns_commit=False — pure mutation, no
    # internal commit).  The caller's commit below is what makes state durable.
    async def _fake_completion(task_arg, logs_arg, session_arg, *, owns_commit=True):
        pass  # pure mutation; caller owns the commit

    mocker.patch(
        "app.task_completion._handle_compute_experiment_completion",
        side_effect=_fake_completion,
    )

    # --- First deliver_wake call: mutates session but does NOT commit. ---------
    result = await deliver_wake(session, reg, now=_utcnow())
    assert result is True

    # deliver_wake does not commit — state is not yet durable.  The caller
    # (here: the test, mirroring the sweep) owns the single commit.
    await session.commit()

    # Verify durability: reload the registration from the DB in a fresh query.
    await session.refresh(reg)
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.delivered_at is not None
    assert reg.result_state == "finalized"

    # Park markers cleared atomically with DELIVERED flip.
    await session.refresh(task)
    assert task.parked_at is None
    assert task.park_reason is None

    # --- Second deliver_wake call: strict no-op (idempotency). ----------------
    first_delivered_at = reg.delivered_at
    result2 = await deliver_wake(session, reg, now=_utcnow())
    assert result2 is False

    await session.refresh(reg)
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.delivered_at == first_delivered_at  # unchanged


@pytest.mark.asyncio
async def test_compute_wake_delivery_handler_failure_leaves_task_recoverable(
    tl_db, mocker
):
    """When _handle_compute_experiment_completion raises after deliver_wake sets
    FIRED→DELIVERED in memory, a rollback restores the registration to FIRED and
    the task to PARKED with parked_at intact — the task is NOT permanently wedged.

    This guards the critical atomicity invariant: because deliver_wake does NOT
    pre-commit the DELIVERED flip, a handler failure rolls back everything and the
    registration remains re-deliverable (or the poison handler can mark it FAILED,
    leaving the task PARKED with parked_at set so the strand-guard can recover it).
    """
    from app.models import WakeAction, WakeDeliveryState, ComputeJob, ComputeJobStatus
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

    session, _factory = tl_db
    project = await _make_project(session)
    task = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )

    # Create a real ComputeJob row so deliver_wake can resolve it (a missing or
    # non-UUID wake_source_ref takes the "job_record_missing" early-exit path).
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=task.id,
        backend="local",
        machine_type="e2-standard-2",
        command="echo done",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.SUCCEEDED,
    )
    session.add(job)
    await session.commit()

    reg = await park_task(
        session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        source_metadata={"compute_job_id": str(job.id)},
        wake_action=WakeAction.FINALIZE,
        reason="waiting on compute job",
    )
    await session.commit()
    await session.refresh(task)
    await session.refresh(reg)

    parked_at_before = task.parked_at
    assert parked_at_before is not None
    assert task.status == TaskStatus.PARKED
    # park_task's catch-up path fires the registration immediately when the
    # ComputeJob is already terminal at park time — no separate fire_task_wakes call.
    assert reg.delivery_state == WakeDeliveryState.FIRED

    # Stub the completion handler to raise — simulating a finalization failure.
    async def _raising_completion(task_arg, logs_arg, session_arg, *, owns_commit=True):
        raise RuntimeError("simulated finalization failure")

    mocker.patch(
        "app.task_completion._handle_compute_experiment_completion",
        side_effect=_raising_completion,
    )

    # deliver_wake must propagate the handler's exception (no silent swallow).
    with pytest.raises(RuntimeError, match="simulated finalization failure"):
        await deliver_wake(session, reg, now=_utcnow())

    # Simulate what the sweep does on exception: rollback.
    await session.rollback()

    # After rollback the registration must still be FIRED (not DELIVERED) so the
    # poison handler or a future sweep iteration can handle it.
    await session.refresh(reg)
    assert reg.delivery_state == WakeDeliveryState.FIRED, (
        "registration must remain FIRED after rollback so the task stays recoverable"
    )

    # The task must still be PARKED with parked_at intact — NOT NULL — so the
    # strand-guard can eventually recover it.
    await session.refresh(task)
    assert task.status == TaskStatus.PARKED, "task must remain PARKED after rollback"
    assert task.parked_at is not None, (
        "parked_at must NOT be NULL after rollback — a NULL parked_at with "
        "DELIVERED registration permanently wedges the task"
    )


@pytest.mark.asyncio
async def test_park_task_catches_up_already_terminal_compute_job(tl_db):
    """park_task creates a FIRED (not PENDING) registration when its compute job
    is already terminal at park time, and stays PENDING when the job is active.

    Covers the terminal-before-park catch-up path in park_task:
    - A ComputeJob in a terminal status → registration born FIRED, fired_at set.
    - A ComputeJob in a non-terminal status → registration stays PENDING.
    - A non-compute-job wake source → registration stays PENDING (no catch-up).
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.models import ComputeJob, ComputeJobStatus

    session, _factory = tl_db
    project = await _make_project(session)

    # --- Terminal job: registration must be born FIRED. -----------------------
    task_terminal = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    terminal_job = ComputeJob(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=task_terminal.id,
        backend="local",
        machine_type="e2-standard-2",
        command="echo done",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.SUCCEEDED,
    )
    session.add(terminal_job)
    await session.commit()

    reg_terminal = await park_task(
        session,
        task_terminal,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(terminal_job.id),
        wake_action=WakeAction.FINALIZE,
        reason="parked on terminal job",
    )
    await session.commit()
    await session.refresh(reg_terminal)

    assert reg_terminal.delivery_state == WakeDeliveryState.FIRED, (
        "park_task must create FIRED registration for already-terminal compute job"
    )
    assert reg_terminal.fired_at is not None

    # --- Non-terminal job: registration must stay PENDING. --------------------
    task_active = await _make_task(
        session,
        project.id,
        task_type="compute_experiment",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    active_job = ComputeJob(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=task_active.id,
        backend="local",
        machine_type="e2-standard-2",
        command="echo running",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.RUNNING,
    )
    session.add(active_job)
    await session.commit()

    reg_active = await park_task(
        session,
        task_active,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(active_job.id),
        wake_action=WakeAction.FINALIZE,
        reason="parked on active job",
    )
    await session.commit()
    await session.refresh(reg_active)

    assert reg_active.delivery_state == WakeDeliveryState.PENDING, (
        "park_task must create PENDING registration for a non-terminal compute job"
    )
    assert reg_active.fired_at is None

    # --- Non-compute-job wake: registration must stay PENDING. ----------------
    task_other = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    reg_other = await park_task(
        session,
        task_other,
        wake_source_kind="knowledge_document_landed",
        wake_source_ref="doc-abc",
        wake_action=WakeAction.RESUME,
        reason="waiting on knowledge doc",
    )
    await session.commit()
    await session.refresh(reg_other)

    assert reg_other.delivery_state == WakeDeliveryState.PENDING, (
        "park_task must not touch PENDING state for non-compute-job wake kinds"
    )
    assert reg_other.fired_at is None



# ══════════════════════════════════════════════════════════════════════════════
# _sweep_stranded_sidecar_sources / trigger_stranded_sidecar_resolution
#
# A side-car that dies without posting status (reaper kill, VM exhaustion,
# cancellation) must still unpark its source task via the lane resolver's
# existing fail-closed semantics, rather than leaving it EVALUATING forever.
# ══════════════════════════════════════════════════════════════════════════════


async def _make_research_source_and_grader(
    session, *, terminal_status=TaskStatus.FAILED
):
    """EVALUATING research source + adversarial grader in a terminal state (no status POST).

    Mirrors the real spawn path: source carries ``awaiting_adversarial_grader`` (the
    adversarial-mode awaiting key), grader carries ``QUALITY_GRADER_SOURCE_KEY``.
    """
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    project = await _make_project(session)
    source = Task(
        project_id=project.id,
        payloads=[
            {"type": "research", "prompt": "investigate X", "base_branch": "main"}
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        plan_output=[
            {
                "hash": "abc123",
                "severity": "high",
                "title": "Vuln",
                "description": "desc",
                "fix_prompt": "fix it",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    grader = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "prompt": "grade it", "base_branch": "main"}],
        status=terminal_status,
        created_by="ORCHESTRATOR",
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id), "adversarial_grader": True},
        latest_status_message="VM died",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(grader)
    await session.commit()
    await session.refresh(grader)

    # Source carries the adversarial-mode awaiting key (not awaiting_quality_grader)
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return project, source, grader


@pytest.mark.asyncio
async def test_grader_verdict_handler_crash_is_recoverable(tl_db, mocker):
    """A crash after verdict-record commit must leave a marker the sweep can recover."""
    from app.schemas import AgentStatusPayload
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import (
        ADVERSARIAL_GATE_VERDICT_KEY,
        SIDECAR_VERDICT_APPLICATION_PENDING_KEY,
    )
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(side_effect=[RuntimeError("handler crashed"), None]),
    )

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        **(grader.context or {}),
        "_adversarial_challenged_hashes": ["finding-hash-recoverable"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-recoverable", "verdict": "pass", "reason": "valid"}]',
    )
    with pytest.raises(RuntimeError, match="handler crashed"):
        await _resolve_quality_grader_completion(grader, payload, session)

    await session.refresh(source)
    ctx = source.context or {}
    assert source.status == TaskStatus.COMPLETED
    assert ctx.get(ADVERSARIAL_GATE_VERDICT_KEY, {}).get("parse_outcome") == "ok"
    assert ctx.get(SIDECAR_VERDICT_APPLICATION_PENDING_KEY) == {
        "lane": "adversarial_grader",
        "sidecar_task_id": str(grader.id),
        "verdict_key": ADVERSARIAL_GATE_VERDICT_KEY,
    }

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    assert handler.await_count == 2
    assert SIDECAR_VERDICT_APPLICATION_PENDING_KEY not in (source.context or {})
    assert (source.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY, {}).get(
        "finding_verdicts", {}
    ).get("finding-hash-recoverable", {}).get("passed") is True


@pytest.mark.asyncio
async def test_duplicate_grader_resolution_after_application_is_noop(tl_db, mocker):
    """A duplicate terminal status post after application must not re-dispatch."""
    from app.schemas import AgentStatusPayload
    from app.task_evaluators import ADVERSARIAL_GATE_VERDICT_KEY
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, _ = tl_db
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        **(grader.context or {}),
        "_adversarial_challenged_hashes": ["finding-hash-once"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-once", "verdict": "pass", "reason": "valid"}]',
    )
    await _resolve_quality_grader_completion(grader, payload, session)
    await session.refresh(source)
    first_record = (source.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY)

    replay_payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-once", "verdict": "fail", "reason": "stale replay"}]',
    )
    await _resolve_quality_grader_completion(grader, replay_payload, session)
    await session.refresh(source)

    assert handler.await_count == 1
    assert (source.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY) == first_record


@pytest.mark.asyncio
async def test_reaper_terminal_grader_resolves_parked_source_fail_closed(tl_db, mocker):
    """A reaper-killed adversarial grader (FAILED applied directly, no status POST)
    must cause the parked research source to resolve fail-closed — findings deferred,
    source unparked and completed — rather than stranding EVALUATING.

    This is the named acceptance criterion for this task.
    """
    from app.services import (
        _sweep_stranded_sidecar_sources,
    )  # triggers services→recovery_sweeps load order

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    # Short-circuit the research completion handler so only the resolver path is exercised.
    # The handler is imported lazily inside the resolver, so we patch its origin module.
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )

    _project, source, grader = await _make_research_source_and_grader(session)

    assert source.status == TaskStatus.EVALUATING, (
        "source must be EVALUATING before sweep"
    )
    assert grader.status == TaskStatus.FAILED, "grader must be FAILED (reaper-killed)"

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    # Resolver sets source status to COMPLETED before re-dispatching, so it must
    # not be EVALUATING after the sweep runs.
    assert source.status != TaskStatus.EVALUATING, (
        "source must not remain EVALUATING after sweep resolves the terminal grader"
    )
    # Adversarial gate verdict must be written fail-closed
    assert (source.context or {}).get("adversarial_gate") is not None, (
        "adversarial_gate verdict must be written on the source after resolution"
    )
    # Adversarial awaiting key must be cleared
    assert "awaiting_adversarial_grader" not in (source.context or {}), (
        "awaiting_adversarial_grader must be cleared after resolution"
    )
    assert handler.await_args.kwargs["project"].id == source.project_id


@pytest.mark.asyncio
async def test_sweep_rebinds_legacy_boolean_to_completed_backend_sidecar(
    tl_db, mocker
):
    """Recover the old True-before-UUID crash window without losing a verdict."""
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.services import _sweep_stranded_sidecar_sources

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )
    _project, source, _grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": True}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    await _sweep_stranded_sidecar_sources()
    await session.refresh(source)

    assert handler.await_count == 1
    assert handler.await_args.kwargs["project"].id == source.project_id
    assert source.status != TaskStatus.EVALUATING
    assert "awaiting_adversarial_grader" not in (source.context or {})
    assert (source.context or {}).get("adversarial_gate") is not None


@pytest.mark.asyncio
async def test_legacy_sidecar_rebind_preserves_peer_committed_source_context(tl_db):
    """The canonical re-lock merges the UUID onto the peer's fresh JSON state."""

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.sidecar_recovery_binding import rebind_legacy_boolean_sidecars

    session, Session = tl_db
    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.context = {"awaiting_adversarial_grader": True}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    async with Session() as peer:
        peer_source = await peer.get(Task, source.id)
        assert peer_source is not None
        peer_source.context = {
            "awaiting_adversarial_grader": True,
            "peer_committed_evidence": {"generation": 7},
        }
        _flag_modified(peer_source, "context")
        peer.add(peer_source)
        await peer.commit()

    assert "peer_committed_evidence" not in (source.context or {})
    rebound = await rebind_legacy_boolean_sidecars(session, [source], limit=50)

    assert [row.id for row in rebound] == [source.id]
    async with Session() as verify:
        persisted = await verify.get(Task, source.id)
        assert persisted is not None
        assert (persisted.context or {})["awaiting_adversarial_grader"] == str(
            grader.id
        )
        assert (persisted.context or {})["peer_committed_evidence"] == {
            "generation": 7
        }


@pytest.mark.asyncio
async def test_legacy_sidecar_rebind_does_not_overwrite_peer_cancellation(tl_db):
    """A terminal transition that wins Project -> Task remains authoritative."""

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.sidecar_recovery_binding import rebind_legacy_boolean_sidecars

    session, Session = tl_db
    _project, source, _grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.context = {"awaiting_adversarial_grader": True}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    async with Session() as peer:
        peer_source = await peer.get(Task, source.id)
        assert peer_source is not None
        peer_source.status = TaskStatus.CANCELLED
        peer_source.context = {
            "awaiting_adversarial_grader": True,
            "admin_cancellation": {"source": "peer-session"},
        }
        _flag_modified(peer_source, "context")
        peer.add(peer_source)
        await peer.commit()

    assert source.status == TaskStatus.EVALUATING
    rebound = await rebind_legacy_boolean_sidecars(session, [source], limit=50)

    assert rebound == []
    async with Session() as verify:
        persisted = await verify.get(Task, source.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.CANCELLED
        assert persisted.context == {
            "awaiting_adversarial_grader": True,
            "admin_cancellation": {"source": "peer-session"},
        }


@pytest.mark.asyncio
async def test_sweep_rechecks_rebound_sidecar_after_peer_requeue(tl_db, mocker):
    """A cached terminal sidecar cannot resolve after its requeue wins."""

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.services import _sweep_stranded_sidecar_sources
    from app.sidecar_recovery_binding import (
        rebind_legacy_boolean_sidecars as _real_rebind,
    )

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )
    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.FAILED
    )
    source.context = {"awaiting_adversarial_grader": True}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    async def _rebind_then_peer_requeues(bound_session, evaluating, *, limit):
        rebound = await _real_rebind(bound_session, evaluating, limit=limit)
        async with Session() as peer:
            peer_grader = await peer.get(Task, grader.id)
            assert peer_grader is not None
            peer_grader.status = TaskStatus.QUEUED
            peer.add(peer_grader)
            await peer.commit()
        return rebound

    mocker.patch(
        "app.recovery_sidecar.rebind_legacy_boolean_sidecars",
        new=_rebind_then_peer_requeues,
    )

    await _sweep_stranded_sidecar_sources()

    assert handler.await_count == 0
    async with Session() as verify:
        persisted_source = await verify.get(Task, source.id)
        persisted_grader = await verify.get(Task, grader.id)
        assert persisted_source is not None
        assert persisted_grader is not None
        assert persisted_source.status == TaskStatus.EVALUATING
        assert (persisted_source.context or {})[
            "awaiting_adversarial_grader"
        ] == str(grader.id)
        assert persisted_grader.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_sweep_does_not_bind_agent_created_legacy_sidecar(tl_db, mocker):
    """A matching source key alone cannot grant an agent task verdict authority."""
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.services import _sweep_stranded_sidecar_sources

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )
    _project, source, impostor = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.context = {"awaiting_adversarial_grader": True}
    impostor.created_by = "AGENT"
    _flag_modified(source, "context")
    session.add(source)
    session.add(impostor)
    await session.commit()

    await _sweep_stranded_sidecar_sources()
    await session.refresh(source)

    assert handler.await_count == 0
    assert source.status == TaskStatus.EVALUATING
    assert (source.context or {}).get("awaiting_adversarial_grader") is True


@pytest.mark.asyncio
async def test_resolver_uses_grader_hashes_when_source_hashes_are_missing(
    tl_db, mocker
):
    """If source-side challenge hashes are lost, grader-side hashes still resolve."""
    from app.schemas import AgentStatusPayload
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, _ = tl_db
    mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
        return_value=None,
    )

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        QUALITY_GRADER_SOURCE_KEY: str(source.id),
        "adversarial_grader": True,
        "_adversarial_challenged_hashes": ["grader-only-hash-001"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs=(
            '[{"hash": "grader-only-hash-001", "verdict": "pass", '
            '"reason": "well-grounded"}]'
        ),
    )
    await _resolve_quality_grader_completion(grader, payload, session)
    await session.refresh(source)

    verdict = (source.context or {}).get("adversarial_gate") or {}
    assert verdict.get("parse_outcome") == "ok"
    assert (
        verdict.get("finding_verdicts", {})
        .get("grader-only-hash-001", {})
        .get("passed")
        is True
    )
    assert "awaiting_adversarial_grader" not in (source.context or {})




@pytest.mark.asyncio
async def test_sweep_stranded_finding_challenge_failed(tl_db, mocker):
    """A FAILED finding-challenge side-car with no status POST must resolve the parked source."""
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import FINDING_CHALLENGE_SOURCE_KEY

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    # Short-circuit the research handler; we only care that the resolver ran.
    handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )

    project = await _make_project(session)

    source = Task(
        project_id=project.id,
        payloads=[
            {"type": "research", "prompt": "investigate Y", "base_branch": "main"}
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        plan_output=[],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    challenge = Task(
        project_id=project.id,
        payloads=[
            {"type": "investigate", "prompt": "challenge", "base_branch": "main"}
        ],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={
            FINDING_CHALLENGE_SOURCE_KEY: str(source.id),
            "challenged_boundary": "reproduce_absent",
            "challenged_first_verdict": "absent",
        },
        latest_status_message="Reaper killed",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(challenge)
    await session.commit()
    await session.refresh(challenge)

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    source.context = {"awaiting_finding_challenge": str(challenge.id)}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    assert source.status != TaskStatus.EVALUATING, (
        "source must not remain EVALUATING after sweep resolves the failed finding challenge"
    )
    assert "awaiting_finding_challenge" not in (source.context or {}), (
        "awaiting_finding_challenge must be cleared after resolution"
    )
    assert handler.await_args.kwargs["project"].id == project.id


@pytest.mark.asyncio
async def test_sweep_stranded_skips_non_terminal_sidecar(tl_db, mocker):
    """A sidecar that is still IN_PROGRESS (not terminal) must not be resolved by the sweep."""
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)

    project = await _make_project(session)

    source = Task(
        project_id=project.id,
        payloads=[
            {"type": "research", "prompt": "investigate Z", "base_branch": "main"}
        ],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    grader = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "prompt": "grade", "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id)},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(grader)
    await session.commit()
    await session.refresh(grader)

    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    source.context = {"awaiting_quality_grader": str(grader.id)}
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    assert source.status == TaskStatus.EVALUATING, (
        "source must remain EVALUATING when sidecar is still live"
    )


@pytest.mark.asyncio
async def test_sweep_stranded_multi_lane_non_first_sidecar(tl_db, mocker):
    """Recovery defers a terminal non-first lane until a live lane completes."""
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import (
        FINDING_CHALLENGE_SOURCE_KEY,
        FINDING_CHALLENGE_VERDICT_KEY,
        QUALITY_GRADER_SOURCE_KEY,
        QUALITY_GRADER_VERDICT_KEY,
    )
    from app.task_lifecycle import trigger_stranded_sidecar_resolution

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    resume_handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    project = await _make_project(session)

    # Create a live (IN_PROGRESS) sidecar for the FIRST awaiting key (quality_grader).
    # Its UUID will be collected; since it's not terminal the source stays EVALUATING
    # for that lane — this is by design.
    live_grader = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "prompt": "grade", "base_branch": "main"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(live_grader)
    await session.commit()
    await session.refresh(live_grader)

    # Source task — EVALUATING with TWO awaiting keys.
    source = Task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    # Make the live lane an exact backend-authored sidecar for this source.
    live_grader.context = {QUALITY_GRADER_SOURCE_KEY: str(source.id)}
    session.add(live_grader)
    await session.commit()
    await session.refresh(live_grader)

    # Create a FAILED sidecar for the SECOND awaiting key (finding_challenge).
    dead_challenge = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "prompt": "assess", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={FINDING_CHALLENGE_SOURCE_KEY: str(source.id)},
        latest_status_message="Reaper killed",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(dead_challenge)
    await session.commit()
    await session.refresh(dead_challenge)

    # Wire source context: first key has a live sidecar, second has the dead one.
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    source.context = {
        "awaiting_quality_grader": str(live_grader.id),
        "awaiting_finding_challenge": str(dead_challenge.id),
    }
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    assert source.status == TaskStatus.EVALUATING
    assert "awaiting_finding_challenge" not in (source.context or {})
    assert (source.context or {}).get("awaiting_quality_grader") == str(
        live_grader.id
    )
    assert FINDING_CHALLENGE_VERDICT_KEY in (source.context or {})
    assert resume_handler.await_count == 0

    # The initially-live lane can still deliver normally. It is now the last
    # exact awaited lane, so its one completion resumes with both verdicts.
    live_grader.status = TaskStatus.COMPLETED
    live_grader.latest_status_message = "PASS"
    session.add(live_grader)
    await session.commit()
    await session.refresh(live_grader)
    await trigger_stranded_sidecar_resolution(live_grader, session)

    await session.refresh(source)
    assert source.status == TaskStatus.COMPLETED
    assert "awaiting_quality_grader" not in (source.context or {})
    assert QUALITY_GRADER_VERDICT_KEY in (source.context or {})
    assert FINDING_CHALLENGE_VERDICT_KEY in (source.context or {})
    assert resume_handler.await_count == 1

    # Replayed terminal delivery remains a no-op.
    await trigger_stranded_sidecar_resolution(live_grader, session)
    assert resume_handler.await_count == 1


@pytest.mark.asyncio
async def test_sweep_stranded_multi_terminal_lanes_both_verdicts_applied(tl_db, mocker):
    """A source with two terminal sidecars in different lanes has both verdicts applied exactly once.

    The stranded-sidecar recovery sweep must fire lane resolution once per terminal
    awaiting_* key. This test verifies the invariant: when a source carries two
    terminal sidecars in two different lanes (e.g., quality_grader and
    finding_challenge), the sweep resolves both, and both lane verdicts are
    recorded in the source's context exactly once.
    """
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import (
        QUALITY_GRADER_SOURCE_KEY,
        QUALITY_GRADER_VERDICT_KEY,
        FINDING_CHALLENGE_SOURCE_KEY,
        FINDING_CHALLENGE_VERDICT_KEY,
    )

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    resume_handler = mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "app.task_completion.broadcast_to_project", new=AsyncMock(return_value=None)
    )

    project = await _make_project(session)

    # Source task — EVALUATING with two awaiting keys for different lanes.
    source = Task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    # Create first terminal sidecar for quality_grader lane.
    grader = Task(
        project_id=project.id,
        payloads=[{"type": "investigate", "prompt": "grade", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id)},
        latest_status_message="Grader crashed",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(grader)
    await session.commit()
    await session.refresh(grader)

    # Create second terminal sidecar for finding_challenge lane.
    challenge = Task(
        project_id=project.id,
        payloads=[
            {"type": "investigate", "prompt": "challenge", "base_branch": "main"}
        ],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={FINDING_CHALLENGE_SOURCE_KEY: str(source.id)},
        latest_status_message="Challenge timed out",
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    session.add(challenge)
    await session.commit()
    await session.refresh(challenge)

    # Wire both sidecars into the source's context, one for each lane.
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    source.context = {
        "awaiting_quality_grader": str(grader.id),
        "awaiting_finding_challenge": str(challenge.id),
    }
    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    # Run the sweep.
    await _sweep_stranded_sidecar_sources()

    # Verify the source has left EVALUATING.
    await session.refresh(source)
    assert source.status != TaskStatus.EVALUATING, (
        "source must not remain EVALUATING after sweep resolves both terminal sidecars"
    )

    # Verify both verdicts are present in the source's context.
    ctx = source.context or {}
    assert QUALITY_GRADER_VERDICT_KEY in ctx, (
        "quality_grader verdict must be written on the source after resolution"
    )
    assert FINDING_CHALLENGE_VERDICT_KEY in ctx, (
        "finding_challenge verdict must be written on the source after resolution"
    )

    # Verify both awaiting keys have been cleared.
    assert "awaiting_quality_grader" not in ctx, (
        "awaiting_quality_grader must be cleared after resolution"
    )
    assert "awaiting_finding_challenge" not in ctx, (
        "awaiting_finding_challenge must be cleared after resolution"
    )
    assert resume_handler.await_count == 1


@pytest.mark.asyncio
async def test_sweep_filters_awaiting_sources_before_per_cycle_limit(tl_db, mocker):
    """Ordinary EVALUATING backlog cannot starve an exact stranded source."""
    from app.services import _sweep_stranded_sidecar_sources
    from app.task_evaluators import (
        QUALITY_GRADER_SOURCE_KEY,
        QUALITY_GRADER_VERDICT_KEY,
    )

    session, Session = tl_db
    mocker.patch("app.recovery_sweeps.services.AsyncSessionLocal", Session)
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.research_completion._handle_research_completion",
        new_callable=AsyncMock,
        return_value=None,
    )

    project = await _make_project(session)
    for index in range(50):
        session.add(
            Task(
                project_id=project.id,
                title=f"Ordinary evaluating task {index}",
                payloads=[{"type": "create", "prompt": "ordinary"}],
                status=TaskStatus.EVALUATING,
                created_by="ORCHESTRATOR",
            )
        )
    await session.flush()

    source = Task(
        project_id=project.id,
        title="Stranded source after cap",
        payloads=[{"type": "research", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
    )
    session.add(source)
    await session.flush()
    grader = Task(
        project_id=project.id,
        title="Terminal exact grader",
        payloads=[{"type": "investigate", "prompt": "grade", "base_branch": "main"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        context={QUALITY_GRADER_SOURCE_KEY: str(source.id)},
        latest_status_message="grader died",
    )
    session.add(grader)
    await session.flush()
    source.context = {"awaiting_quality_grader": str(grader.id)}
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    _flag_modified(source, "context")
    session.add(source)
    await session.commit()

    await _sweep_stranded_sidecar_sources()

    await session.refresh(source)
    assert source.status != TaskStatus.EVALUATING
    assert QUALITY_GRADER_VERDICT_KEY in (source.context or {})


# ══════════════════════════════════════════════════════════════════════════════
# Adversarial Gate Challenger Provenance
#
# The adversarial gate verdict record should carry challenger runtime provenance
# (grader VM instance id, agent source SHA, runtime fingerprint, image name, build
# timestamp) to establish an evidence chain proving whether a verdict was produced
# by a post-fix agent image. Provenance is best-effort: lookup failures do not
# prevent the verdict from being written.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_adversarial_gate_includes_grader_provenance_from_pool_vm(tl_db, mocker):
    """Adversarial gate record includes provenance fields from PoolVm when vm_instance_id exists."""
    from app.schemas import AgentStatusPayload
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY
    from app.task_lifecycle import _resolve_quality_grader_completion
    from app.models import PoolVm

    session, _ = tl_db
    mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )

    # Mock the in-memory cache lookups to return None (forces fallback to PoolVm row).
    mocker.patch(
        "app.compute.get_vm_source_sha",
        return_value=None,
    )
    mocker.patch(
        "app.compute.get_vm_runtime_fingerprint",
        return_value=None,
    )

    # Create a PoolVm row with provenance data.
    pool_vm = PoolVm(
        vm_instance_id=grader.vm_instance_id,
        project_id=_project.id,
        agent_source_sha="abc123def456789abcdef",
        agent_runtime_fingerprint="runtime-fp-xyz",
        agent_image_name="agent-image-v1.0.0",
        agent_image_build_ts=1234567890,
    )
    session.add(pool_vm)
    await session.commit()

    # Set up the challenged hashes explicitly so parsing works.
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        QUALITY_GRADER_SOURCE_KEY: str(source.id),
        "adversarial_grader": True,
        "_adversarial_challenged_hashes": ["finding-hash-1"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-1", "verdict": "pass", "reason": "valid"}]',
    )
    await _resolve_quality_grader_completion(grader, payload, session)
    await session.refresh(source)

    # Verify provenance fields are present in the adversarial gate record.
    gate_record = (source.context or {}).get("adversarial_gate") or {}
    assert gate_record.get("grader_vm_instance_id") == grader.vm_instance_id
    assert gate_record.get("grader_agent_source_sha") == "abc123def456789abcdef"
    assert gate_record.get("grader_agent_runtime_fingerprint") == "runtime-fp-xyz"
    assert gate_record.get("grader_agent_image_name") == "agent-image-v1.0.0"
    assert gate_record.get("grader_agent_image_build_ts") == 1234567890
    # Verdict and parse_outcome should also be present.
    assert "finding_verdicts" in gate_record
    assert gate_record.get("parse_outcome") == "ok"


@pytest.mark.asyncio
async def test_adversarial_gate_fallback_provenance_from_cache(tl_db, mocker):
    """Adversarial gate uses in-memory cache when available, falls back to PoolVm for fields cache lacks."""
    from app.schemas import AgentStatusPayload
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY
    from app.task_lifecycle import _resolve_quality_grader_completion
    from app.models import PoolVm

    session, _ = tl_db
    mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )

    # Mock: cache has source_sha but not runtime_fingerprint.
    mocker.patch(
        "app.compute.get_vm_source_sha",
        return_value="cached-source-sha-abc123",
    )
    mocker.patch(
        "app.compute.get_vm_runtime_fingerprint",
        return_value=None,
    )

    # PoolVm row provides fallback for runtime_fingerprint and image metadata.
    pool_vm = PoolVm(
        vm_instance_id=grader.vm_instance_id,
        project_id=_project.id,
        agent_source_sha="poolvm-source-sha-ignored",  # Cache takes precedence
        agent_runtime_fingerprint="poolvm-runtime-fp",  # Falls back here
        agent_image_name="poolvm-agent-image",
        agent_image_build_ts=9999,
    )
    session.add(pool_vm)
    await session.commit()

    # Set up the challenged hashes explicitly.
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        QUALITY_GRADER_SOURCE_KEY: str(source.id),
        "adversarial_grader": True,
        "_adversarial_challenged_hashes": ["finding-hash-2"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-2", "verdict": "fail", "reason": "issue"}]',
    )
    await _resolve_quality_grader_completion(grader, payload, session)
    await session.refresh(source)

    gate_record = (source.context or {}).get("adversarial_gate") or {}
    # Cache value takes precedence.
    assert gate_record.get("grader_agent_source_sha") == "cached-source-sha-abc123"
    # Fallback to PoolVm for missing cache value.
    assert gate_record.get("grader_agent_runtime_fingerprint") == "poolvm-runtime-fp"
    # Image metadata comes from PoolVm.
    assert gate_record.get("grader_agent_image_name") == "poolvm-agent-image"
    assert gate_record.get("grader_agent_image_build_ts") == 9999


@pytest.mark.asyncio
async def test_adversarial_gate_provenance_lookup_failure_doesnt_prevent_verdict(
    tl_db, mocker
):
    """Provenance lookup failure (exception) doesn't prevent verdict from being written."""
    from app.schemas import AgentStatusPayload
    from app.task_evaluators import QUALITY_GRADER_SOURCE_KEY
    from app.task_lifecycle import _resolve_quality_grader_completion

    session, _ = tl_db
    mocker.patch(
        "app.research_completion._handle_research_completion",
        new=AsyncMock(return_value=None),
    )

    _project, source, grader = await _make_research_source_and_grader(
        session, terminal_status=TaskStatus.COMPLETED
    )

    # Mock provenance lookups to raise an exception (best-effort should catch it).
    mocker.patch(
        "app.compute.get_vm_source_sha",
        side_effect=RuntimeError("Cache lookup failed"),
    )

    # Set up the challenged hashes explicitly.
    source.plan_output = []
    source.context = {"awaiting_adversarial_grader": str(grader.id)}
    grader.context = {
        QUALITY_GRADER_SOURCE_KEY: str(source.id),
        "adversarial_grader": True,
        "_adversarial_challenged_hashes": ["finding-hash-3"],
    }
    session.add(source)
    session.add(grader)
    await session.commit()

    payload = AgentStatusPayload(
        task_id=grader.id,
        status=TaskStatus.COMPLETED,
        logs='[{"hash": "finding-hash-3", "verdict": "pass", "reason": "valid"}]',
    )
    # This should not raise; provenance is best-effort.
    await _resolve_quality_grader_completion(grader, payload, session)
    await session.refresh(source)

    gate_record = (source.context or {}).get("adversarial_gate") or {}
    # Verdict and parse_outcome must be present even if provenance lookup failed.
    assert "finding_verdicts" in gate_record, (
        "verdict must be written even when provenance lookup fails"
    )
    assert gate_record.get("parse_outcome") == "ok"
    assert (
        gate_record.get("finding_verdicts", {}).get("finding-hash-3", {}).get("passed")
        is True
    )
    # Provenance fields may or may not be present, but absence is OK.
    # (The exception is caught and logged at debug level)

# ══════════════════════════════════════════════════════════════════════════════
# Stale-head guard (verdict-time head pin)
# ══════════════════════════════════════════════════════════════════════════════


async def _run_stale_head_branch(
    tl_db,
    mocker,
    *,
    reviewed_head_sha: str,
    live_head_sha: str,
    reviewed_base_sha: str = "base-sha-1",
    live_base_sha: str = "base-sha-1",
    review_history: list[dict] | None = None,
    current_issues: list[dict] | None = None,
):
    """Drive _process_post_commit through a REQUEST_CHANGES verdict that carries
    ``reviewed_head_sha``.  Returns (captured_spawn_calls, review_task) where
    ``captured_spawn_calls`` is a list of (args, kwargs) tuples recorded when
    ``_spawn_pipeline_task`` was invoked.

    ``reviewed_head_sha`` is the SHA baked into the verdict JSON.
    ``live_head_sha``     is the SHA that get_pull_request returns as the live PR head.
    """
    captured_spawn_calls: list[tuple] = []

    async def _capture_spawn(*args, **kwargs):
        captured_spawn_calls.append((args, kwargs))
        return (None, False)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.task_lifecycle._comment_with_retry", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "head": {"sha": live_head_sha, "ref": "feat/x"},
            "base": {"sha": live_base_sha, "ref": "main"},
            "state": "open",
            "merged": False,
        },
    )
    mocker.patch(
        "app.github.submit_pr_review", new_callable=AsyncMock, return_value={"id": 1}
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "app/x.py"}],
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )

    session, factory = tl_db
    project = await _make_project(session)
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "X", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context={},
    )
    session.add(original)
    await session.commit()
    await session.refresh(original)

    ctx: dict = {
        "review_loop": {
            "pr_number": 7070,
            "branch": "feat/x",
            "base_branch": "main",
            "original_task_id": str(original.id),
            "iteration": 1,
            "review_target": {
                "project_id": str(project.id),
                "pr_number": 7070,
                "base_sha": reviewed_base_sha,
                "head_sha": reviewed_head_sha,
            },
            **(
                {"review_history": review_history}
                if review_history is not None
                else {}
            ),
        },
    }
    review_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 7070,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=ctx,
    )
    session.add(review_task)
    await session.commit()
    await session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Found a real bug.",
            "issues": current_issues
            or [{"description": "Real bug", "severity": "major"}],
            "reviewed_head_sha": reviewed_head_sha,
            "reviewed_base_sha": reviewed_base_sha,
        }
    )

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/o/r/pull/7070",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=verdict_json,
    )

    await _process_post_commit(review_task.id, payload, _session_factory=factory)
    await session.refresh(review_task)
    return captured_spawn_calls, review_task


@pytest.mark.asyncio
async def test_stale_head_verdict_discards_fix_and_respawns_review(tl_db, mocker):
    """When the verdict carries a reviewed_head_sha that differs from the live PR head,
    the pipeline must NOT spawn a fix (stale guidance), must spawn a fresh REVIEW,
    and must set _review_stale_head=True without any discard-count escape hatch."""
    calls, review_task = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha="oldsha0000000",
        live_head_sha="newsha1111111",
    )

    # Exactly one spawn call, and it must be for a "review" (not a "fix").
    assert len(calls) == 1, f"Expected 1 spawn call, got {len(calls)}"
    spawned_task_type = calls[0][0][1]  # positional arg[1] = task_type
    assert spawned_task_type == "review", (
        f"Stale-head discard must respawn a review, got {spawned_task_type!r}"
    )
    assert calls[0][1]["delta_base_sha"] == "oldsha0000000"

    # Context flags.
    ctx = review_task.context or {}
    assert ctx.get("_review_stale_head") is True, "_review_stale_head must be set"
    assert not any(call[0][1] == "fix" for call in calls)


@pytest.mark.asyncio
async def test_base_only_request_changes_keeps_review_admitted(tl_db, mocker):
    """Base-only drift keeps the head-bound REQUEST_CHANGES result usable."""
    calls, review_task = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha="samehead00000",
        live_head_sha="samehead00000",
        reviewed_base_sha="oldbase000000",
        live_base_sha="newbase111111",
    )

    assert not any(c[0][1] == "review" for c in calls)
    fix_calls = [c for c in calls if c[0][1] == "fix"]
    assert len(fix_calls) == 1
    assert (review_task.context or {}).get("_review_stale_head") is not True


@pytest.mark.asyncio
async def test_base_and_head_drift_request_changes_respawns_full_review(
    tl_db, mocker
):
    """When the base moved too, no head-relative delta can cover the change.

    The prior review proved ``old base..old head``. A delta from the old head
    would show the head motion but never the effect of the new base, so the
    respawn must fall back to a full review.
    """
    calls, _ = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha="oldhead000000",
        live_head_sha="newhead111111",
        reviewed_base_sha="oldbase000000",
        live_base_sha="newbase111111",
    )

    review_calls = [c for c in calls if c[0][1] == "review"]
    assert len(review_calls) == 1
    assert review_calls[0][1]["delta_base_sha"] == ""


@pytest.mark.asyncio
async def test_same_head_fix_receives_prior_and_current_authoritative_blockers(
    tl_db, mocker
):
    """Omitting A while raising B on the same head must hand both to the fixer."""
    head = "a" * 40
    calls, _review_task = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha=head,
        live_head_sha=head,
        review_history=[
            {
                "iteration": 0,
                "verdict": "REQUEST_CHANGES",
                "reviewed_head_sha": head,
                "reviewed_base_sha": "base-sha-1",
                "target_verified_by_backend": True,
                "review_task_id": "prior-same-head-review",
                "issues": [
                    {
                        "issue_id": "BP-prior",
                        "description": "prior null-check blocker",
                        "severity": "major",
                    }
                ],
            }
        ],
        current_issues=[
            {
                "issue_id": "BP-current",
                "description": "new lost-update race",
                "severity": "major",
            }
        ],
    )

    fix_calls = [call for call in calls if call[0][1] == "fix"]
    assert len(fix_calls) == 1
    assert [
        issue["description"] for issue in fix_calls[0][1]["current_issues"]
    ] == ["prior null-check blocker", "new lost-update race"]


@pytest.mark.asyncio
async def test_changed_head_fix_receives_only_still_open_blockers(tl_db, mocker):
    """A later exact-head review may close omitted A while newly raised B stays open."""
    old_head = "a" * 40
    new_head = "b" * 40
    calls, _review_task = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha=new_head,
        live_head_sha=new_head,
        review_history=[
            {
                "iteration": 0,
                "verdict": "REQUEST_CHANGES",
                "reviewed_head_sha": old_head,
                "reviewed_base_sha": "base-sha-1",
                "target_verified_by_backend": True,
                "review_task_id": "prior-old-head-review",
                "issues": [
                    {
                        "issue_id": "BP-prior",
                        "description": "prior null-check blocker",
                        "severity": "major",
                    }
                ],
            }
        ],
        current_issues=[
            {
                "issue_id": "BP-current",
                "description": "new lost-update race",
                "severity": "major",
            }
        ],
    )

    fix_calls = [call for call in calls if call[0][1] == "fix"]
    assert len(fix_calls) == 1
    assert [
        issue["description"] for issue in fix_calls[0][1]["current_issues"]
    ] == ["new lost-update race"]


@pytest.mark.asyncio
async def test_stale_head_respawn_idempotent_on_replay(tl_db, mocker):
    """A replay keeps the stale verdict discarded without spawning duplicate work.

    The task-local stale marker and review spawn idempotency bound retries; no
    counter is allowed to make an old-head verdict authoritative.
    """
    import json as _json
    from app.task_lifecycle import _process_post_commit

    session, factory = tl_db

    # First, run a clean first-discard to get the committed post-discard state.
    calls1, review_task1 = await _run_stale_head_branch(
        tl_db,
        mocker,
        reviewed_head_sha="stalehd000000",
        live_head_sha="livehd1111111",
    )
    # Confirm the first run marked the stale verdict and spawned its replacement.
    ctx1 = review_task1.context or {}
    assert ctx1.get("_review_stale_head") is True, "first run must set _review_stale_head"
    assert len(calls1) == 1 and calls1[0][0][1] == "review", (
        "first run must spawn exactly one fresh review"
    )

    # Now replay: call _process_post_commit again with the same task (context
    # already has _review_stale_head=True and counter=1).
    spawn_calls_replay: list[tuple] = []

    async def _capture_spawn_replay(*args, **kwargs):
        spawn_calls_replay.append((args, kwargs))
        return (None, False)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn_replay)

    verdict_json = _json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Found a real bug.",
            "issues": [{"description": "Real bug", "severity": "major"}],
            "reviewed_head_sha": "stalehd000000",
            "reviewed_base_sha": "base-sha-1",
        }
    )
    payload = AgentStatusPayload(
        task_id=review_task1.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/o/r/pull/7070",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=verdict_json,
    )
    await _process_post_commit(review_task1.id, payload, _session_factory=factory)
    await session.refresh(review_task1)

    ctx_replay = review_task1.context or {}
    assert ctx_replay.get("_review_stale_head") is True
    assert not spawn_calls_replay, (
        f"replay must NOT spawn a second fresh review (got {len(spawn_calls_replay)} calls)"
    )


@pytest.mark.asyncio
async def test_missing_review_provenance_respawns_exact_target_review(tl_db, mocker):
    """A REQUEST_CHANGES verdict without both SHAs cannot authorize a fix."""
    captured_spawn_calls: list[tuple] = []

    async def _capture_spawn(*args, **kwargs):
        captured_spawn_calls.append((args, kwargs))
        return (None, False)

    mocker.patch("app.task_lifecycle._spawn_pipeline_task", side_effect=_capture_spawn)
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.task_lifecycle._comment_with_retry", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.task_lifecycle.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "head": {"sha": "anylivesha", "ref": "feat/x"},
            "base": {"sha": "livebasesha", "ref": "main"},
            "state": "open",
            "merged": False,
        },
    )
    mocker.patch(
        "app.github.submit_pr_review", new_callable=AsyncMock, return_value={"id": 1}
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "app/x.py"}],
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
            "details": [],
        },
    )

    session, factory = tl_db
    project = await _make_project(session)
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "X", "base_branch": "main"}],
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
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
                "pr_number": 7071,
                "branch": "feat/x",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "review_loop": {
                "pr_number": 7071,
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

    # Old verdict without reviewed_head_sha.
    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Found a bug.",
            "issues": [{"description": "Real bug", "severity": "major"}],
        }
    )

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        pr_url="https://github.com/o/r/pull/7071",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=verdict_json,
    )

    await _process_post_commit(review_task.id, payload, _session_factory=factory)
    await session.refresh(review_task)

    # Missing provenance is discarded just like a mismatched target. The
    # replacement review is pinned to the exact live base/head; no fix runs.
    fix_calls = [c for c in captured_spawn_calls if c[0][1] == "fix"]
    review_calls = [c for c in captured_spawn_calls if c[0][1] == "review"]
    assert not fix_calls
    assert len(review_calls) == 1
    assert review_calls[0][1]["review_target"] == {
        "project_id": str(project.id),
        "pr_number": 7071,
        "base_sha": "livebasesha",
        "head_sha": "anylivesha",
    }
    assert review_calls[0][1]["delta_base_sha"] == ""
    assert (review_task.context or {}).get("_review_stale_head") is True


@pytest.mark.asyncio
async def test_requeue_clone_strips_lane_transient_context(tl_db, mocker):
    """Requeued clone must not inherit transient lane or warm handoff directives.

    A clone carrying improvement_lane_id would silently claim the lane-reserved
    warm VM and expose stale lane_resume context. Warm handoff directives are
    per-attempt routing that the clone must not inherit. Durable chain keys
    (_review_fix_lane_arm / _review_fix_lane_id) must survive so measurement
    continuity is preserved.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    transient_keys = {
        "improvement_lane_id": "lane-abc-123",
        "is_lane_phase": True,
        "lane_checkpoint_sha": "deadbeef",
        "lane_resume": {"prior_phases": ["phase1"], "critical_files": ["foo.py"]},
        "warm_target_vm_id": "vm-warm-xyz",
        "warm_target_pr": "https://github.com/o/r/pull/99",
        "warm_handoff_deadline": "2099-01-01T00:00:00Z",
    }
    durable_keys = {
        "_review_fix_lane_arm": "lane",
        "_review_fix_lane_id": "chain-uuid-456",
    }

    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            "_attempt_number": 1,
            **transient_keys,
            **durable_keys,
        },
        latest_status_message="Build failed",
    )

    result = await _requeue_parent_task(parent, session)
    assert result == 1

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
    clone_ctx = clones[0].context or {}

    # All transient lane + warm handoff keys must be absent from the clone
    for key in transient_keys:
        assert key not in clone_ctx, (
            f"Transient key {key!r} must not be inherited by requeue clone"
        )

    # Durable chain keys must survive for measurement continuity
    assert clone_ctx.get("_review_fix_lane_arm") == "lane", (
        "_review_fix_lane_arm must be preserved in requeue clone"
    )
    assert clone_ctx.get("_review_fix_lane_id") == "chain-uuid-456", (
        "_review_fix_lane_id must be preserved in requeue clone"
    )


@pytest.mark.asyncio
async def test_requeue_clone_strips_failure_verdict_keys(tl_db, mocker):
    """Requeue clone must not inherit the failed child's authoritative failure verdict
    or blocker-escalation markers.

    A clone carrying failure_class would short-circuit classify_failure on its
    first death, reporting the predecessor's class rather than its own evidence.
    A clone carrying _blocked_escalated would route its very first BLOCKED episode
    into the already-escalated branch, skipping the fresh-episode alert/cadence.
    """
    mocker.patch("app.playbook_learning._maybe_auto_playbook", new_callable=AsyncMock)
    session, _ = tl_db
    project = await _make_project(session)
    parent = await _make_task(session, project.id, status=TaskStatus.EVALUATING)

    verdict_keys = {
        "failure_class": "CODE_ERROR",
        "failure_category": "CODE_ERROR",
        "blocker": {"kind": "test_failure", "tests": ["test_bar"]},
        "_blocked_escalated": True,
        "_precondition_escalated_key": "some_precondition",
        "_blocked_optimistic_retries": 3,
    }
    durable_keys = {
        "_previous_failure_class": "CODE_ERROR",
        "_attempt_number": 1,
    }

    await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        context={
            **verdict_keys,
            **durable_keys,
        },
        latest_status_message="Build failed",
    )

    result = await _requeue_parent_task(parent, session)
    assert result == 1

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # All stale verdict and blocker-escalation keys must be absent from the clone.
    for key in verdict_keys:
        assert key not in clone_ctx, (
            f"Stale verdict/blocker key {key!r} must not be inherited by requeue clone"
        )

    # Durable diagnostic keys must survive so retry guidance renders correctly.
    assert clone_ctx.get("_previous_failure_class") == "CODE_ERROR", (
        "_previous_failure_class must be preserved in requeue clone"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _requeue_parent_task — LLM cost accumulation
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_requeue_clone_carries_prior_attempts_llm_cost(tl_db, mocker):
    """Clone of a failed task with live LLM tokens carries accumulated
    LLM rework dollars in _prior_attempts_llm_cost.

    Exercises the symmetric LLM accumulator added alongside _prior_attempts_cost.
    Uses anthropic/claude-sonnet-4-6 (input: $3/MTok, output: $15/MTok) so the
    expected cost is deterministic from the pricing table.
    """
    from app.efficiency_snapshot import LLM_TOKEN_PRICING

    session, _ = tl_db

    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    input_tokens = 500_000
    output_tokens = 100_000
    expected_cost = round(input_tokens * input_rate + output_tokens * output_rate, 6)

    # Use api_call_summary (a real SQLModel Field) for live token data rather than
    # the property-backed llm_input_tokens_banked which is not a Pydantic Field and
    # therefore is not set by SQLModel's generated __init__.
    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="Process exited with code 1",
        context={"failure_category": "claude_code_failed"},
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_call_summary={
            "llm_call_count": 10,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
        },
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    await _requeue_parent_task(parent, session)
    await session.commit()

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1, f"Expected 1 QUEUED clone, got {len(clones)}"
    clone_ctx = clones[0].context or {}
    assert "_prior_attempts_llm_cost" in clone_ctx, (
        "_prior_attempts_llm_cost must be present on clone when child has LLM tokens"
    )
    assert clone_ctx["_prior_attempts_llm_cost"] == pytest.approx(
        expected_cost, rel=1e-6
    ), f"Expected LLM cost {expected_cost}, got {clone_ctx['_prior_attempts_llm_cost']}"


@pytest.mark.asyncio
async def test_requeue_llm_cost_accumulates_across_two_requeueues(tl_db, mocker):
    """Second requeue accumulates LLM cost across both prior failed attempts."""
    from app.efficiency_snapshot import LLM_TOKEN_PRICING

    session, _ = tl_db

    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    in1, out1 = 200_000, 50_000
    in2, out2 = 300_000, 80_000
    cost1 = round(in1 * input_rate + out1 * output_rate, 6)
    cost2 = round(in2 * input_rate + out2 * output_rate, 6)

    # First failed child — use api_call_summary (a real SQLModel Field) for tokens.
    child1 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="fail 1",
        context={"failure_category": "claude_code_failed"},
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_call_summary={
            "llm_call_count": 5,
            "total_input_tokens": in1,
            "total_output_tokens": out1,
        },
    )
    session.add(child1)
    await session.commit()
    await session.refresh(child1)

    # First requeue -> produces clone1 (QUEUED)
    await _requeue_parent_task(parent, session)
    await session.commit()

    clones1 = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones1) == 1
    clone1 = clones1[0]
    assert (clone1.context or {}).get("_prior_attempts_llm_cost") == pytest.approx(
        cost1, rel=1e-6
    )

    # Now fail clone1 with more LLM spend, and requeue again
    clone1.status = TaskStatus.FAILED
    clone1.latest_status_message = "fail 2"
    clone1.provider = "anthropic"
    clone1.model = "claude-sonnet-4-6"
    clone1.api_call_summary = {
        "llm_call_count": 8,
        "total_input_tokens": in2,
        "total_output_tokens": out2,
    }
    session.add(clone1)
    await session.commit()
    await session.refresh(clone1)

    await session.refresh(parent)
    await _requeue_parent_task(parent, session)
    await session.commit()

    clones2 = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones2) == 1
    clone2_ctx = clones2[0].context or {}
    expected_total = round(cost1 + cost2, 6)
    assert clone2_ctx.get("_prior_attempts_llm_cost") == pytest.approx(
        expected_total, rel=1e-6
    ), (
        f"Expected accumulated cost {expected_total}, got {clone2_ctx.get('_prior_attempts_llm_cost')}"
    )


@pytest.mark.asyncio
async def test_requeue_llm_cost_absent_when_no_llm_data(tl_db, mocker):
    """Clone of a failed task with no LLM token data omits _prior_attempts_llm_cost."""
    session, _ = tl_db

    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="fail no llm",
        context={"failure_category": "INFRA"},
        # No provider/model/token fields set
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    await _requeue_parent_task(parent, session)
    await session.commit()

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    # Key must be absent (or zero) when there are no LLM tokens to price
    assert clone_ctx.get("_prior_attempts_llm_cost", 0.0) == 0.0, (
        "_prior_attempts_llm_cost must be absent or 0 when child has no LLM data"
    )


@pytest.mark.asyncio
async def test_requeue_llm_cost_zero_for_unpriced_provider_model(tl_db, mocker):
    """Tokens from an unknown provider/model contribute $0 and never block requeue."""
    session, _ = tl_db

    project = await _make_project(session)
    parent = await _make_task(
        session,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0},
    )

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="fail unpriced",
        context={"failure_category": "claude_code_failed"},
        provider="unknown-provider",
        model="unknown-model-xyz",
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 999_000,
            "total_output_tokens": 200_000,
        },
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    # Must not raise
    await _requeue_parent_task(parent, session)
    await session.commit()

    clones = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1, "Requeue must succeed even with an unpriced provider/model"
    clone_ctx = clones[0].context or {}
    # Unpriced combo yields $0 so key is absent or 0
    assert clone_ctx.get("_prior_attempts_llm_cost", 0.0) == 0.0, (
        "Unpriced provider/model must contribute $0, not fail the requeue"
    )


def test_wake_cluster_reexported_from_extracted_module():
    """The park-and-wake cluster was extracted to ``app.task_lifecycle_wakes``;
    ``app.task_lifecycle`` must re-export the SAME function objects so every
    existing caller (``from app.task_lifecycle import park_task`` and the
    ``app.task_lifecycle.park_task`` patch targets in tests) keeps working.

    Guards the refactor contract: if a future edit drops a re-export, the
    ``hasattr`` check fails; if it re-implements a function instead of
    forwarding it, the identity check fails.
    """
    import app.task_lifecycle as tl
    import app.task_lifecycle_wakes as tl_wakes

    moved = [
        "close_orphan_pr_if_any",
        "park_task",
        "fire_task_wakes",
        "deliver_wake",
        "expire_wake",
        "_maybe_fire_terminal_compute_wake",
    ]
    for name in moved:
        assert hasattr(tl, name), (
            f"task_lifecycle no longer re-exports {name} from task_lifecycle_wakes"
        )
        assert getattr(tl, name) is getattr(tl_wakes, name), (
            f"{name} must be the SAME function object in task_lifecycle and "
            "task_lifecycle_wakes (re-export, not a re-implementation)"
        )

    # Constants moved with the cluster are re-exported too.
    for const in ("_PARKABLE_STATUSES", "_PARK_STRAND_SECONDS"):
        assert getattr(tl, const) is getattr(tl_wakes, const), (
            f"{const} must be re-exported from task_lifecycle"
        )
