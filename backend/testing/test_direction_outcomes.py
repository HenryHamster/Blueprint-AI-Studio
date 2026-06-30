"""Tests for _update_direction_research_outcomes in direction_outcomes.py.

Covers:
1. Guard conditions (missing project, direction not found, invalid UUID, no children, task_id not in history)
2. Status aggregation (tasks_completed, tasks_failed, tasks_cancelled)
3. PR metrics (prs_merged, prs_rejected, conversion_rate)
4. Time-to-merge calculation (avg_time_to_merge_hours)
5. Failure categorization (infra vs non-infra, failed_task_details cap, reason truncation)
6. Scope breakdown per-scope created/merged/failed counters
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.direction_outcomes import (
    _counts_as_failure,
    _is_cascade_cancelled,
    _is_non_genuine_cancelled,
    _longest_chain_length,
    _refresh_pipeline_scope_merge_rates,
    _update_cochange_index,
    _update_direction_research_outcomes,
    acceptance_metric_invariant_violations,
    acceptance_metrics_consistent,
    refresh_direction_outcomes_after_merge,
)
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.shared_state import runtime_config


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _create_project(
    session: AsyncSession, directions: list | None = None
) -> Project:
    """Create a minimal project with optional directions list."""
    project = Project(
        name="Test Project",
        user_id="test-user-123",
        directions=directions or [],
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return project


def _make_direction(
    direction_id: str, research_task_id: str, tasks_created: int = 3
) -> dict:
    """Build a minimal direction with one research_history entry."""
    return {
        "id": direction_id,
        "text": "Improve test coverage",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {
                "task_id": research_task_id,
                "tasks_created": tasks_created,
                "cycle": 1,
            }
        ],
    }


async def _create_child_task(
    session: AsyncSession,
    parent_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    context: dict | None = None,
    payloads: list | None = None,
    title: str = "Child task",
    latest_status_message: str | None = None,
    created_at: datetime | None = None,
    vm_stopped_at: datetime | None = None,
    created_by: str = "ORCHESTRATOR",
) -> Task:
    task = Task(
        project_id=project_id,
        parent_task_id=parent_id,
        title=title,
        payloads=payloads or [{"type": "create", "scope": "small"}],
        context=context or {},
        status=status,
        created_by=created_by,
        latest_status_message=latest_status_message,
    )
    if created_at is not None:
        task.created_at = created_at
    if vm_stopped_at is not None:
        task.vm_stopped_at = vm_stopped_at
    session.add(task)
    await session.flush()
    return task


async def _make_research_task(session: AsyncSession, project_id: uuid.UUID) -> Task:
    """Create a placeholder research task row (parent)."""
    task = Task(
        project_id=project_id,
        title="Research: improve things",
        payloads=[{"type": "research"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    session.add(task)
    await session.flush()
    return task


# ─────────────────────────────────────────────────────────────────────────────
# 1. Guard conditions — early return / break without committing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_missing_project(db_session: AsyncSession):
    """Returns early when project_id does not exist."""
    # Should not raise; just returns None
    await _update_direction_research_outcomes(
        project_id=uuid.uuid4(),
        direction_id="d-missing",
        research_task_id=str(uuid.uuid4()),
        session=db_session,
    )


@pytest.mark.asyncio
async def test_guard_direction_not_found(db_session: AsyncSession):
    """Returns early when no direction matches direction_id."""
    direction_id = "d-exists"
    research_task_id = str(uuid.uuid4())
    project = await _create_project(
        db_session, directions=[_make_direction(direction_id, research_task_id)]
    )

    # Call with a non-existent direction_id
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id="d-does-not-exist",
        research_task_id=research_task_id,
        session=db_session,
    )
    # Project should be unchanged (no commit happened) — we just verify no error
    await db_session.refresh(project)
    assert project.directions[0]["id"] == direction_id


@pytest.mark.asyncio
async def test_guard_research_task_id_not_in_history(db_session: AsyncSession):
    """Breaks without updating when task_id not in research_history."""
    direction_id = "d-history"
    history_task_id = str(uuid.uuid4())
    project = await _create_project(
        db_session, directions=[_make_direction(direction_id, history_task_id)]
    )

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=str(uuid.uuid4()),  # different from history entry
        session=db_session,
    )
    await db_session.refresh(project)
    # No tasks_completed key should have been added
    assert "tasks_completed" not in project.directions[0]["research_history"][0]


@pytest.mark.asyncio
async def test_guard_invalid_uuid_in_history(db_session: AsyncSession):
    """Breaks when task_id in history entry is not a valid UUID."""
    direction_id = "d-baduuid"
    bad_task_id = "not-a-uuid"
    project = await _create_project(
        db_session,
        directions=[
            {
                "id": direction_id,
                "text": "Test",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "research_history": [{"task_id": bad_task_id, "tasks_created": 1}],
            }
        ],
    )

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=bad_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    assert "tasks_completed" not in project.directions[0]["research_history"][0]


@pytest.mark.asyncio
async def test_guard_no_child_tasks(db_session: AsyncSession):
    """Zero-child cycles still persist parent spawn-gate telemetry."""
    direction_id = "d-nochildren"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 3,
        "research_spawn_gate_refuted_count": 2,
        "research_spawn_gate_survived_count": 1,
        "research_spawn_gate_explicit_pass_count": 1,
        "research_spawn_gate_fail_open_count": 0,
        "research_spawn_gate_unresolved_count": 1,
        "research_spawn_gate_wholesale_retry_exhausted": True,
        "research_spawn_gate_refuted_reasons": ["fail-closed: no verdict"],
    }
    project.directions = [_make_direction(direction_id, research_task_id)]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert "tasks_completed" not in entry
    assert entry["research_spawn_gate_challenged_count"] == 3
    assert entry["research_spawn_gate_refuted_count"] == 2
    assert entry["research_spawn_gate_survived_count"] == 1
    assert entry["research_spawn_gate_explicit_pass_count"] == 1
    assert entry["research_spawn_gate_fail_open_count"] == 0
    assert entry["research_spawn_gate_unresolved_count"] == 1
    assert entry["research_spawn_gate_wholesale_retry_exhausted"] is True
    assert entry["research_spawn_gate_refuted_reasons"] == ["fail-closed: no verdict"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Status aggregation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_aggregation_mixed(db_session: AsyncSession):
    """tasks_completed/failed/cancelled are counted from mixed-status children."""
    direction_id = "d-mixed"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="c1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="c2",
    )
    await _create_child_task(
        db_session, research_task.id, project.id, status=TaskStatus.FAILED, title="c3"
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        title="c4",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        title="c5",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.BLOCKED,
        title="c6",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["tasks_completed"] == 2
    assert entry["tasks_failed"] == 1
    assert entry["tasks_cancelled"] == 1
    # BLOCKED tasks are non-terminal and not folded into the failure tally.


def test_blocked_status_does_not_count_as_failure():
    """A BLOCKED task is non-terminal — it must never count as a direction failure
    (which would inflate failure rates and falsely consume retry budget)."""
    blocked = Task(
        project_id=uuid.uuid4(),
        title="t",
        description="d",
        status=TaskStatus.BLOCKED,
        created_by="u",
        payloads=[{"type": "fix"}],
        context={},
    )
    assert _counts_as_failure(blocked) is False
    failed = Task(
        project_id=uuid.uuid4(),
        title="t",
        description="d",
        status=TaskStatus.FAILED,
        created_by="u",
        payloads=[{"type": "fix"}],
        context={},
    )
    assert _counts_as_failure(failed) is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. PR metrics
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_metrics_basic(db_session: AsyncSession):
    """prs_merged and prs_rejected counted from context flags."""
    direction_id = "d-prs"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged2",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_rejected": True},
        title="rejected1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="no-pr",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 2
    assert entry["prs_rejected"] == 1


@pytest.mark.asyncio
async def test_generalized_success_gates_on_reproduce_verdict(db_session: AsyncSession):
    """A merged fix still counts as a shipped merge, but only a *reproduced*
    defect scores as a generalized (proven-fix) success; an already_resolved
    merge ships code without inflating generalized_successes."""
    direction_id = "d-reproduce"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "reproduce_verdict": {"status": "reproduced"},
        },
        payloads=[{"type": "fix", "scope": "small"}],
        title="reproduced-fix",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "reproduce_verdict": {"status": "already_resolved"},
        },
        payloads=[{"type": "fix", "scope": "small"}],
        title="already-resolved-merge",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Both merged PRs still count as shipped code (merge-rate metrics unchanged).
    assert entry["prs_merged"] == 2
    assert entry["verified_prs_merged"] == 2
    # ...but only the reproduced defect is a proven-fix generalized success.
    assert entry["generalized_successes"] == 1
    code_merge = entry["outcome_kind_breakdown"]["code_merge"]
    assert code_merge["success"] == 1
    assert code_merge["neutral"] == 1


@pytest.mark.asyncio
async def test_pr_metrics_exclude_non_merge_unblocks(db_session: AsyncSession):
    """Staging/no-op success paths should not inflate prs_merged analytics."""
    direction_id = "d-prs-non-merge"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        title="landed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": False},
        title="staging-only-success",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 1


@pytest.mark.asyncio
async def test_confirmed_non_landing_discounts_credit_gate(db_session: AsyncSession):
    """A badge-counted task confirmed non-landing via ancestry reduces credit_gate_merged_count.

    Regression: stranded_prs_merged was never written, so confirmed non-landings
    remained visible to steering decisions via the stale prs_merged count.
    """
    from app.direction_outcomes import (
        credit_gate_merged_count,
        discount_stranded_pr_from_direction_credit,
    )
    from app.landed_signal import mark_stranded_non_landing

    direction_id = "d-stranded-credit"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=2)]
    await db_session.flush()

    # Task badge-counted (pr_merged=True) before stranding is discovered.
    stranded_task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="stranded",
    )
    # A second task that landed cleanly on integration.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="landed",
    )
    await db_session.flush()

    # Initial recompute: both tasks count before stranding is known.
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 2
    assert credit_gate_merged_count(entry) == 2

    # Ancestry check later confirms the first PR never reached integration.
    # Capture badge state before stranding — mark_stranded_non_landing clears pr_merged.
    had_pr_merged = bool((stranded_task.context or {}).get("pr_merged"))
    mark_stranded_non_landing(stranded_task, reason="rollup->integration merge not an ancestor")
    await db_session.flush()

    # discount_stranded_pr_from_direction_credit persists stranded_prs_merged so
    # credit_gate_merged_count subtracts the confirmed non-landing from credit.
    await discount_stranded_pr_from_direction_credit(stranded_task, db_session, had_pr_merged=had_pr_merged)

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry.get("stranded_prs_merged") == 1
    assert credit_gate_merged_count(entry) == 1  # 2 verified - 1 stranded


@pytest.mark.asyncio
async def test_discount_stranded_pr_is_idempotent(db_session: AsyncSession):
    """discount_stranded_pr_from_direction_credit must not over-subtract on repeated calls.

    Regression: rollup-management cycles re-enter the merged-PR classification
    block while the parent remains in EVALUATING, calling discount repeatedly and
    driving credit_gate_merged_count below the legitimate count.
    """
    from app.direction_outcomes import (
        credit_gate_merged_count,
        discount_stranded_pr_from_direction_credit,
    )
    from app.landed_signal import mark_stranded_non_landing

    direction_id = "d-idempotent-discount"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=3)]
    await db_session.flush()

    stranded_task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="stranded",
    )
    # A second task that landed cleanly.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="landed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    # Capture badge state before stranding — mark_stranded_non_landing clears pr_merged.
    had_pr_merged = bool((stranded_task.context or {}).get("pr_merged"))
    mark_stranded_non_landing(stranded_task, reason="not reachable from integration")
    await db_session.flush()

    # Simulate N rollup-management cycles all calling discount on the same task.
    for _ in range(3):
        await discount_stranded_pr_from_direction_credit(stranded_task, db_session, had_pr_merged=had_pr_merged)

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # stranded_prs_merged must be exactly 1, not 3.
    assert entry.get("stranded_prs_merged") == 1
    assert credit_gate_merged_count(entry) == 1  # 2 prs_merged - 1 stranded
    # Idempotency flag must be set on the task context.
    assert stranded_task.context.get("stranded_discount_applied") is True


@pytest.mark.asyncio
async def test_discount_stranded_pr_guards(db_session: AsyncSession):
    """discount_stranded_pr_from_direction_credit early-return guards fire correctly."""
    from app.direction_outcomes import discount_stranded_pr_from_direction_credit

    direction_id = "d-guards"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=1)]
    await db_session.flush()

    # Guard 1: pr_merged=False — task was never badge-counted; must not touch history.
    no_badge_task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": False,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="no-badge",
    )
    await db_session.flush()
    await discount_stranded_pr_from_direction_credit(no_badge_task, db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry.get("stranded_prs_merged") is None  # untouched

    # Guard 2: missing direction_id — should return without error.
    bare_task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},  # no direction_id / research_task_id
        title="bare",
    )
    await db_session.flush()
    await discount_stranded_pr_from_direction_credit(bare_task, db_session)
    # No exception raised and history still untouched.
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry.get("stranded_prs_merged") is None


@pytest.mark.asyncio
async def test_verify_rollup_merge_landed_stranded_invokes_discount(
    db_session: AsyncSession, mocker
):
    """verify_rollup_merge_landed's ancestry-failure path must call discount_stranded_pr.

    Integration test: the wiring from rollup_pr_verification into
    discount_stranded_pr_from_direction_credit is exercised end-to-end by
    stubbing only the GitHub I/O layer (is_ancestor_of -> False).
    """
    from app.direction_outcomes import credit_gate_merged_count
    from app.rollup_pr_verification import verify_rollup_merge_landed

    direction_id = "d-verify-wiring"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [_make_direction(direction_id, research_task_id, tasks_created=2)]
    await db_session.flush()

    child_task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
        title="rollup-task",
    )
    await db_session.flush()

    # Seed prs_merged=1 so there is credit to subtract.
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    # Stub GitHub I/O: is_ancestor_of returns False (definitive non-landing).
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch("app.github.is_ancestor_of", return_value=False)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    result = await verify_rollup_merge_landed(
        child_task,
        project,
        db_session,
        pr_number=1,
        merge_commit_sha="abc1234",
        integration_branch="main",
        source="test",
    )

    assert result == "stranded"

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Discount must have been applied exactly once.
    assert entry.get("stranded_prs_merged") == 1
    assert credit_gate_merged_count(entry) == 0  # 1 prs_merged - 1 stranded
    # Idempotency flag must be set.
    assert child_task.context.get("stranded_discount_applied") is True


@pytest.mark.asyncio
async def test_conversion_rate_division_by_zero(db_session: AsyncSession):
    """verified_prs_merged is computed correctly even when tasks_created is 0."""
    direction_id = "d-zerodiv"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    # tasks_created = 0
    direction = _make_direction(direction_id, research_task_id, tasks_created=0)
    project.directions = [direction]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="merged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Even with tasks_created=0, the merge still counts in verified_prs_merged.
    assert entry["verified_prs_merged"] == 1
    assert entry["tasks_created"] == 0


def test_verified_merged_count_helper():
    """verified_merged_count prefers verified_prs_merged, falls back to prs_merged."""
    from app.direction_outcomes import verified_merged_count

    # Legacy entry (no verified_prs_merged) → falls back to raw prs_merged.
    assert verified_merged_count({"prs_merged": 4}) == 4
    # New entry → uses verified count (degraded merges already excluded).
    assert verified_merged_count({"prs_merged": 4, "verified_prs_merged": 2}) == 2
    # Empty / missing → 0.
    assert verified_merged_count({}) == 0


def test_proven_merged_count_helper():
    """proven_merged_count reads acceptance_proven_merged, 0 for legacy entries."""
    from app.direction_outcomes import proven_merged_count

    assert proven_merged_count({"acceptance_proven_merged": 3}) == 3
    # Legacy entry (no field) degrades to 0 — never claims raw merges as proven.
    assert proven_merged_count({"prs_merged": 4, "verified_prs_merged": 4}) == 0
    assert proven_merged_count({}) == 0


def test_declared_merged_count_helper():
    """declared_merged_count reads acceptance_declared_merged, 0 for legacy."""
    from app.direction_outcomes import declared_merged_count

    assert declared_merged_count({"acceptance_declared_merged": 2}) == 2
    # Legacy entry (no field) degrades to 0 — never claims raw merges as declared.
    assert declared_merged_count({"prs_merged": 4, "verified_prs_merged": 4}) == 0
    assert declared_merged_count({}) == 0


def test_reachable_merged_count_helper():
    """reachable_merged_count reads reachable_prs_merged; returns None for legacy."""
    from app.direction_outcomes import reachable_merged_count

    # New entry with field present → returns int.
    assert reachable_merged_count({"reachable_prs_merged": 3}) == 3
    assert reachable_merged_count({"reachable_prs_merged": 0}) == 0
    # Legacy entry (field absent) → None, never fabricates zero or raw count.
    assert reachable_merged_count({"prs_merged": 4, "verified_prs_merged": 4}) is None
    assert reachable_merged_count({}) is None


@pytest.mark.asyncio
async def test_conversion_rate_excludes_degraded_unverified_merge(
    db_session: AsyncSession,
):
    """Conversion counts verified + benign-unverified merges, NOT degraded ones."""
    direction_id = "d-verif-conv"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Verified merge — no fall-open reason.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="verified",
    )
    # Benign-unverified merge (no runnable suite) — a legitimate positive signal.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "no_detectable_test_command"},
        },
        title="benign",
    )
    # Degraded-unverified merge (verify setup failed) — never actually verified.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
        },
        title="degraded",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 3
    # Verification-aware: 2 (verified + benign) out of 3 created.
    assert entry["verified_prs_merged"] == 2


@pytest.mark.asyncio
async def test_verified_merged_excludes_declared_unsatisfied_acceptance(
    db_session: AsyncSession,
):
    """A merged PR whose declared acceptance evidence is unsatisfied does not
    count toward verified_prs_merged (or conversion_rate); satisfied / no-key
    merges still count, mirroring the per-task outcome layer."""
    direction_id = "d-accept-conv"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Merge with no acceptance verdict (legacy / no declaration) — still counts.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="no-acceptance",
    )
    # Merge with acceptance declared AND satisfied — counts.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {"satisfied": True},
        },
        title="acceptance-satisfied",
    )
    # Merge with acceptance declared but NOT satisfied — excluded.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {"satisfied": False},
        },
        title="acceptance-unsatisfied",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 3
    # Only the no-key + satisfied merges are verified; the unsatisfied one drops.
    assert entry["verified_prs_merged"] == 2


@pytest.mark.asyncio
async def test_verified_merged_excludes_degraded_and_unsatisfied_acceptance(
    db_session: AsyncSession,
):
    """The acceptance exclusion composes with the degraded-unverified one: a
    merge that is both degraded and acceptance-unsatisfied drops once, while an
    undeclared merge stays neutral and still counts toward verified progress."""
    direction_id = "d-accept-degraded"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # Clean verified merge — counts.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="clean",
    )
    # Undeclared merge — neutral, still counts.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="undeclared",
    )
    # Degraded-unverified merge — excluded by the degraded gate.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
        },
        title="degraded",
    )
    # Acceptance-unsatisfied merge — excluded by the acceptance gate.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {"satisfied": False},
        },
        title="acceptance-unsatisfied",
    )
    # Both degraded AND acceptance-unsatisfied — still drops exactly once.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
            "code_acceptance_verdict": {"satisfied": False},
        },
        title="both",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 5
    # Clean + undeclared merges survive; degraded and unsatisfied rows do not.
    assert entry["verified_prs_merged"] == 2


def test_churn_metrics_excludes_degraded_unverified_merges():
    """_compute_churn_metrics counts degraded merges as non-merge finished work."""
    from app.strategic_completion import _compute_churn_metrics

    # Each cycle: 2 completed, 2 merged of which only 1 verified (1 degraded).
    direction = {
        "research_history": [
            {"tasks_completed": 2, "prs_merged": 2, "verified_prs_merged": 1},
            {"tasks_completed": 2, "prs_merged": 2, "verified_prs_merged": 1},
            {"tasks_completed": 2, "prs_merged": 2, "verified_prs_merged": 1},
        ]
    }
    metrics = _compute_churn_metrics(direction)
    # Verified-aware merged = 3 of 6 finished → 0.5 (raw prs_merged would be 1.0).
    assert metrics["merged"] == 3
    assert metrics["merge_rate"] == 0.5


def test_lifetime_burn_excludes_degraded_unverified_merges():
    """_compute_lifetime_burn_metrics uses verified counts (window + aggregate)."""
    from app.strategic_completion import _compute_lifetime_burn_metrics

    direction = {
        "research_history": [
            {"tasks_completed": 4, "prs_merged": 4, "verified_prs_merged": 2},
        ],
        "history_aggregate": {
            "total_tasks_completed": 4,
            "total_prs_merged": 4,
            "total_verified_prs_merged": 2,
        },
    }
    m = _compute_lifetime_burn_metrics(direction)
    # lifetime_merged = 2 (window verified) + 2 (agg verified) = 4, of 8 finished.
    assert m["lifetime_merged"] == 4
    assert m["lifetime_merge_rate"] == 0.5


def test_lifetime_burn_legacy_aggregate_falls_back_to_total_prs_merged():
    """Aggregates predating total_verified_prs_merged use total_prs_merged."""
    from app.strategic_completion import _compute_lifetime_burn_metrics

    direction = {
        "research_history": [],
        "history_aggregate": {
            "total_tasks_completed": 4,
            "total_prs_merged": 4,
        },
    }
    m = _compute_lifetime_burn_metrics(direction)
    assert m["lifetime_merged"] == 4


@pytest.mark.asyncio
async def test_rejected_and_review_counts_require_completed_status(
    db_session: AsyncSession,
):
    """prs_rejected, reviews_dismissed, reviews_overridden exclude non-COMPLETED tasks."""
    direction_id = "d-status-filter"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=6)
    ]
    await db_session.flush()

    # COMPLETED — should count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="ok",
    )
    # FAILED with the same flags — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="failed",
    )
    # CANCELLED with flags — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={
            "pr_rejected": True,
            "review_dismissed": True,
            "user_overrode_review": True,
        },
        title="cancelled",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_rejected"] == 1
    assert entry["reviews_dismissed"] == 1
    assert entry["reviews_overridden"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Time-to-merge calculation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_avg_time_to_merge_hours(db_session: AsyncSession):
    """avg_time_to_merge_hours is mean of (vm_stopped_at - created_at) for merged tasks."""
    direction_id = "d-ttm"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    base = _utcnow()
    # Task 1: 2 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=2),
        title="t1",
    )
    # Task 2: 4 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=4),
        title="t2",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] == 3.0


@pytest.mark.asyncio
async def test_avg_time_to_merge_excludes_null_timestamps(db_session: AsyncSession):
    """Tasks with null vm_stopped_at or created_at are excluded from merge time calc."""
    direction_id = "d-nullts"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    base = _utcnow()
    # One valid merged task: 3 hours
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=base,
        vm_stopped_at=base + timedelta(hours=3),
        title="valid",
    )
    # One merged task with no vm_stopped_at
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="no-stop-time",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] == 3.0


@pytest.mark.asyncio
async def test_avg_time_to_merge_none_when_no_merged(db_session: AsyncSession):
    """avg_time_to_merge_hours is None when no tasks have pr_merged."""
    direction_id = "d-nomerge"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["avg_time_to_merge_hours"] is None


# Retired: tests for avg_implementation_hours and avg_review_hours.
# Fields were retired as they had no named consumers. Removed 2026-06-05.


# ─────────────────────────────────────────────────────────────────────────────
# 5. Failure categorization
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_infra_vs_non_infra_failure_counting(db_session: AsyncSession):
    """tasks_failed_infra counts only INFRA_CATEGORIES; failed_task_details includes all failed/cancelled."""
    direction_id = "d-infra"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
        title="infra1",
        latest_status_message="VM failed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
        title="infra2",
        latest_status_message="Rate limited",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        title="noinfra1",
        latest_status_message="Code error",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"failure_category": "AGENT_TIMEOUT"},
        title="infra3",
        latest_status_message="Timed out",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT are infra; CODE_ERROR is not
    assert entry["tasks_failed_infra"] == 3
    assert len(entry["failed_task_details"]) == 4


@pytest.mark.asyncio
async def test_infra_failure_without_persisted_category_counts_as_infra(
    db_session: AsyncSession,
):
    """A FAILED child with no failure_category but an infra status message must
    be counted in tasks_failed_infra via the classifier fallback.  A FAILED child
    with neither a persisted category nor a classifiable message must NOT be counted.
    """
    direction_id = "d-infra-fallback"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # No failure_category; status message classifies as infra (provision failure)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="no-category-infra",
        latest_status_message="failed to provision vm: quota exceeded",
    )
    # No failure_category; status message is a plain code error — NOT infra
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="no-category-code",
        latest_status_message="AssertionError in test_foo.py",
    )
    # Explicit CODE_ERROR persisted — must still NOT count as infra
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        title="explicit-code-error",
        latest_status_message="Code error",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Only the provision-failure child (classified via fallback) counts as infra
    assert entry["tasks_failed_infra"] == 1
    assert entry["tasks_failed"] == 3
    # Derived metric: code_failed should be 2 (total_failed - infra), not 3
    # (This is what the churn/saturation guardrail consumes via strategic_completion)
    # tasks_failed_infra == 1 so code failures == tasks_failed - tasks_failed_infra == 2
    assert entry["tasks_failed"] - entry["tasks_failed_infra"] == 2


@pytest.mark.asyncio
async def test_failed_task_details_capped_at_5(db_session: AsyncSession):
    """failed_task_details is capped at 5 entries even when more tasks fail."""
    direction_id = "d-cap"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=8)
    ]
    await db_session.flush()

    for i in range(8):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.FAILED,
            context={},
            title=f"failed-{i}",
            latest_status_message=f"Error {i}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["failed_task_details"]) == 5


@pytest.mark.asyncio
async def test_failed_task_reason_truncated_to_120(db_session: AsyncSession):
    """reason in failed_task_details is truncated to 120 characters."""
    direction_id = "d-truncate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    long_message = "X" * 200
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="long-fail",
        latest_status_message=long_message,
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["failed_task_details"][0]["reason"]) == 120


@pytest.mark.asyncio
async def test_taxonomy_fields_in_outcome_entries(db_session: AsyncSession):
    """Each failed_task_details entry has a 'taxonomy' label; taxonomy_summary counts
    all failed tasks across the 4 categories (not just the capped 5)."""
    direction_id = "d-taxonomy"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=8)
    ]
    await db_session.flush()

    # infra_related: PROVISION_FAILURE
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
        title="infra1",
        latest_status_message="VM failed",
    )
    # infra_related: AGENT_TIMEOUT
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"failure_category": "AGENT_TIMEOUT"},
        title="infra2",
        latest_status_message="Timed out",
    )
    # review_quality: CODE_ERROR with pytest signal
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR", "scope": "small"},
        title="rq1",
        latest_status_message="pytest failed in tests",
    )
    # scope_related: CODE_ERROR with positive scope evidence (status-msg keyword)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "failure_category": "CODE_ERROR",
            "scope": "medium",
            "scope_calibrated_from": "small",
        },
        title="sc1",
        latest_status_message="task exceeds scope budget",
    )
    # code_defect: CODE_ERROR small with no classifying signal
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR", "scope": "small"},
        title="cd1",
        latest_status_message="unknown failure",
    )
    # Extra failures to push total above the cap of 5
    for i in range(3):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.FAILED,
            context={"failure_category": "PROVISION_FAILURE"},
            title=f"extra-{i}",
            latest_status_message="vm crashed",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # failed_task_details remains capped at 5 with taxonomy on each
    assert len(entry["failed_task_details"]) == 5
    for d in entry["failed_task_details"]:
        assert "taxonomy" in d
        assert d["taxonomy"] in {
            "scope_related",
            "review_quality",
            "infra_related",
            "code_defect",
        }
    # taxonomy_summary counts ALL 8 failed tasks, not just capped 5
    ts = entry["taxonomy_summary"]
    assert ts["infra_related"] == 5  # 1 provision + 1 agent_timeout + 3 extra provision
    assert ts["review_quality"] == 1
    assert ts["scope_related"] == 1
    assert ts["code_defect"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. Scope breakdown
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_breakdown_per_scope_counters(db_session: AsyncSession):
    """scope_breakdown tracks created/merged/failed per scope."""
    direction_id = "d-scope"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # small: 2 created, 1 merged, 0 failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="s1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="s2",
    )
    # medium: 1 created, 0 merged, 1 failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium"},
        payloads=[{"type": "create"}],
        title="m1",
    )
    # no scope → unknown
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},  # no scope key
        payloads=[{"type": "create"}],
        title="u1",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    sb = entry["scope_breakdown"]

    assert sb["small"]["created"] == 2
    assert sb["small"]["merged"] == 1
    assert sb["small"]["failed"] == 0

    assert sb["medium"]["created"] == 1
    assert sb["medium"]["merged"] == 0
    assert sb["medium"]["failed"] == 1

    assert sb["unknown"]["created"] == 1


@pytest.mark.asyncio
async def test_scope_breakdown_tracks_calibration(db_session: AsyncSession):
    """A task with scope=small + scope_calibrated_from=medium increments small.calibrated_in
    and medium.calibrated_out so consumers see scope ambition vs effective scope."""
    direction_id = "d-calibrate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # Calibrated task: proposed medium, ran as small, merged
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "scope_calibrated_from": "medium",
        },
        payloads=[{"type": "create"}],
        title="calibrated",
    )
    # Non-calibrated small task
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="plain-small",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["created"] == 2
    assert sb["small"]["calibrated_in"] == 1
    assert sb["small"]["calibrated_out"] == 0
    # medium gets a calibrated_out entry even though no task ran AT medium
    assert sb["medium"]["created"] == 0
    assert sb["medium"]["calibrated_in"] == 0
    assert sb["medium"]["calibrated_out"] == 1


@pytest.mark.asyncio
async def test_escalation_outcomes_tracks_attempt_merge_fail(db_session: AsyncSession):
    """escalation_outcomes buckets tasks whose effective scope exceeds the
    original (calibrated_from), keyed ``{from}_to_{to}``, with attempted/
    merged/failed counters. Downsizing (e.g. medium → small) and same-scope
    calibration must NOT show up in escalation_outcomes."""
    direction_id = "d-escalate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # Escalation small → medium, merged
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "medium",
            "scope_calibrated_from": "small",
        },
        payloads=[{"type": "create"}],
        title="esc-sm-md-merged",
    )
    # Escalation small → medium, failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "scope_calibrated_from": "small"},
        payloads=[{"type": "create"}],
        title="esc-sm-md-failed",
    )
    # Escalation medium → large, attempted only (still running)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "large", "scope_calibrated_from": "medium"},
        payloads=[{"type": "create"}],
        title="esc-md-lg-running",
    )
    # Downsize medium → small — must NOT appear in escalation_outcomes
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "scope_calibrated_from": "medium",
        },
        payloads=[{"type": "create"}],
        title="downsize-md-sm",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    eo = entry["escalation_outcomes"]

    assert set(eo.keys()) == {"small_to_medium", "medium_to_large"}
    assert eo["small_to_medium"] == {"attempted": 2, "merged": 1, "failed": 1}
    assert eo["medium_to_large"] == {"attempted": 1, "merged": 0, "failed": 0}


@pytest.mark.asyncio
async def test_escalation_outcomes_empty_when_no_escalations(db_session: AsyncSession):
    """escalation_outcomes is an empty dict when no child is calibrated upward."""
    direction_id = "d-no-escalate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="plain-small",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["escalation_outcomes"] == {}


@pytest.mark.asyncio
async def test_scope_breakdown_avg_expansion_ratio(db_session: AsyncSession):
    """avg_expansion_ratio per scope is mean of len(_pr_changed_files)/len(related_files)
    for completed tasks that have both fields populated."""
    direction_id = "d-expand"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # small #1: 2 expected, 4 changed → ratio 2.0
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["a.py", "b.py"],
            "_pr_changed_files": ["a.py", "b.py", "c.py", "d.py"],
        },
        payloads=[{"type": "create"}],
        title="s1",
    )
    # small #2: 1 expected, 1 changed → ratio 1.0  → avg small = 1.5
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["x.py"],
            "_pr_changed_files": ["x.py"],
        },
        payloads=[{"type": "create"}],
        title="s2",
    )
    # medium: 2 expected, 6 changed → ratio 3.0
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "medium",
            "related_files": ["m1.py", "m2.py"],
            "_pr_changed_files": ["m1.py", "m2.py", "m3.py", "m4.py", "m5.py", "m6.py"],
        },
        payloads=[{"type": "create"}],
        title="m1",
    )
    # small with empty related_files: should be excluded (division by zero guard)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": [],
            "_pr_changed_files": ["y.py"],
        },
        payloads=[{"type": "create"}],
        title="s-no-related",
    )
    # failed task: contributes to avg_expansion_ratio_failed (separate from merged bucket)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "related_files": ["z.py"],
            "_pr_changed_files": ["z.py", "q.py", "r.py"],
        },
        payloads=[{"type": "create"}],
        title="failed-m",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["avg_expansion_ratio"] == 1.5
    assert sb["small"]["avg_expansion_ratio_merged"] == 1.5
    assert sb["medium"]["avg_expansion_ratio"] == 3.0
    assert sb["medium"]["avg_expansion_ratio_merged"] == 3.0
    # Failed medium task with 1 related_file and 3 changed files → ratio 3.0
    assert sb["medium"]["avg_expansion_ratio_failed"] == 3.0
    assert "avg_expansion_ratio_failed" not in sb["small"]


@pytest.mark.asyncio
async def test_expansion_ratio_excludes_test_files(db_session: AsyncSession):
    """Test files in _pr_changed_files must not inflate the expansion ratio.

    Tasks that declare production files in related_files and then ship both
    those production files plus paired test files should have an expansion
    ratio of 1.0 (production-only), not 2.0.
    """
    direction_id = "d-expand-tests"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # 2 production related_files, PR touches the same 2 plus 2 test files →
    # production-only ratio 1.0 (not inflated to 2.0).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "scope": "small",
            "related_files": ["backend/app/foo.py", "backend/app/bar.py"],
            "_pr_changed_files": [
                "backend/app/foo.py",
                "backend/app/bar.py",
                "backend/testing/test_foo.py",
                "backend/testing/test_bar.py",
            ],
        },
        payloads=[{"type": "create"}],
        title="s-with-tests",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["avg_expansion_ratio"] == 1.0
    assert sb["small"]["avg_expansion_ratio_merged"] == 1.0


@pytest.mark.asyncio
async def test_scope_breakdown_failed_scope_overflow_counter(db_session: AsyncSession):
    """SCOPE_OVERFLOW failures increment both failed_infra and failed_scope_overflow.

    _effective_scope_denominator subtracts (failed_infra - failed_scope_overflow)
    from created to exclude true-infra failures while keeping scope-overflow
    signal in the denominator.
    """
    direction_id = "d-scope-overflow"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # medium task that hit scope overflow
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "SCOPE_OVERFLOW"},
        payloads=[{"type": "create"}],
        title="m-overflow",
        latest_status_message="scope overflow",
    )
    # medium task with a pure infra failure (no scope overflow)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "RATE_LIMIT"},
        payloads=[{"type": "create"}],
        title="m-rate",
        latest_status_message="rate limited",
    )
    # small task with CODE_ERROR — should not touch either infra counter
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-code",
        latest_status_message="test failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    # SCOPE_OVERFLOW is in INFRA_CLASSES, so it bumps both counters
    assert sb["medium"]["failed"] == 2
    assert sb["medium"]["failed_infra"] == 2
    assert sb["medium"]["failed_scope_overflow"] == 1
    # CODE_ERROR does not touch either infra counter
    assert sb["small"]["failed"] == 1
    assert sb["small"]["failed_infra"] == 0
    assert sb["small"]["failed_scope_overflow"] == 0


@pytest.mark.asyncio
async def test_scope_breakdown_failed_analysis_overflow_counter(
    db_session: AsyncSession,
):
    """scope_gate_triggered='analysis_overflow' increments failed_analysis_overflow.

    Other scope_gate values (post_review_overflow) and non-analysis failures
    leave the counter untouched — a stronger mismatch signal than generic
    SCOPE_OVERFLOW, used by spawn_calibration to detect systemic scope
    misestimation.
    """
    direction_id = "d-analysis-overflow"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # medium + analysis_overflow
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
            "scope_gate_triggered": "analysis_overflow",
        },
        payloads=[{"type": "create"}],
        title="m-ao",
        latest_status_message="analysis overflow",
    )
    # medium + post_review_overflow — must NOT increment
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "SCOPE_OVERFLOW",
            "scope_gate_triggered": "post_review_overflow",
        },
        payloads=[{"type": "create"}],
        title="m-pro",
        latest_status_message="post review overflow",
    )
    # small + no scope_gate
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-code",
        latest_status_message="test failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["failed_analysis_overflow"] == 1
    assert sb["small"]["failed_analysis_overflow"] == 0


@pytest.mark.asyncio
async def test_scope_breakdown_failure_stages_per_scope(db_session: AsyncSession):
    """failure_stages counts per-stage failures within each scope bucket."""
    direction_id = "d-stages"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # small — test failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-test",
        latest_status_message="pytest assert failed",
    )
    # small — lint failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s-lint",
        latest_status_message="ruff E501",
    )
    # medium — merge failure via _previous_failure_stage
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={
            "scope": "medium",
            "failure_category": "CODE_ERROR",
            "_previous_failure_stage": "merge",
        },
        payloads=[{"type": "create"}],
        title="m-merge",
        latest_status_message="something else",
    )
    # medium — unknown stage (AGENT_TIMEOUT, generic message)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "medium", "failure_category": "AGENT_TIMEOUT"},
        payloads=[{"type": "create"}],
        title="m-unknown",
        latest_status_message="No heartbeat for 300s",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["small"]["failure_stages"] == {"test": 1, "lint": 1}
    assert sb["medium"]["failure_stages"] == {"merge": 1, "unknown": 1}


@pytest.mark.asyncio
async def test_scope_breakdown_merged_cross_module(db_session: AsyncSession):
    """merged_cross_module counts merged tasks whose PR touches 2+ top-level dirs."""
    direction_id = "d-xmod"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # merged, cross-module (backend + admin-ui)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "scope": "medium",
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/a.py", "admin-ui/src/b.ts"],
        },
        payloads=[{"type": "create"}],
        title="x1",
    )
    # merged, single-module
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "scope": "medium",
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/x.py", "backend/app/y.py"],
        },
        payloads=[{"type": "create"}],
        title="x2",
    )
    # merged, no changed files recorded
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small", "pr_merged": True},
        payloads=[{"type": "create"}],
        title="x3",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert sb["medium"]["merged"] == 2
    assert sb["medium"]["merged_cross_module"] == 1
    assert sb["small"]["merged"] == 1
    assert sb["small"]["merged_cross_module"] == 0


def test_aggregate_scope_breakdown_sums_failure_stages():
    """_aggregate_scope_breakdown sums failure_stages counts across history entries."""
    from app.spawn_calibration import _aggregate_scope_breakdown

    entries = [
        {
            "scope_breakdown": {
                "small": {
                    "created": 2,
                    "merged": 1,
                    "failed": 1,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"test": 1},
                },
            }
        },
        {
            "scope_breakdown": {
                "small": {
                    "created": 3,
                    "merged": 1,
                    "failed": 2,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"test": 1, "lint": 1},
                },
                "medium": {
                    "created": 1,
                    "merged": 0,
                    "failed": 1,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failure_stages": {"merge": 1},
                },
            }
        },
    ]
    agg = _aggregate_scope_breakdown(entries)
    assert agg["small"]["failure_stages"] == {"test": 2, "lint": 1}
    assert agg["medium"]["failure_stages"] == {"merge": 1}


@pytest.mark.asyncio
async def test_scope_breakdown_no_expansion_ratio_when_no_data(
    db_session: AsyncSession,
):
    """avg_expansion_ratio is omitted when no qualifying tasks exist for a scope."""
    direction_id = "d-no-expand"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    # Task missing _pr_changed_files entirely
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small", "related_files": ["a.py"]},
        payloads=[{"type": "create"}],
        title="no-changed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    sb = project.directions[0]["research_history"][0]["scope_breakdown"]
    assert "avg_expansion_ratio" not in sb["small"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Co-change affinity index
# ─────────────────────────────────────────────────────────────────────────────


def _make_project_with_design_doc(design_doc: dict | None = None) -> Project:
    """Create an in-memory Project with the given design_doc (no DB flush needed)."""
    return Project(
        name="Co-change Test",
        user_id="test-user",
        design_doc=design_doc or {},
    )


def test_cochange_index_basic_pairs():
    """Co-changed files are recorded bidirectionally with score 1."""
    project = _make_project_with_design_doc()
    _update_cochange_index(project, [["a.py", "b.py", "c.py"]])
    idx = project.design_doc["cochange_index"]
    assert idx["a.py"]["b.py"] == 1.0
    assert idx["a.py"]["c.py"] == 1.0
    assert idx["b.py"]["a.py"] == 1.0
    assert idx["c.py"]["a.py"] == 1.0


def test_cochange_index_accumulates_scores():
    """Scores increment across separate calls (decay applied once per call)."""
    project = _make_project_with_design_doc()
    # First call: a↔b = 1.0
    _update_cochange_index(project, [["a.py", "b.py"]])
    # Second call: existing 1.0 decays to 0.9, then +1.0 = 1.9
    _update_cochange_index(project, [["a.py", "b.py"]])
    idx = project.design_doc["cochange_index"]
    assert idx["a.py"]["b.py"] == pytest.approx(1.9, abs=0.01)


def test_cochange_index_decay_fades_old_scores():
    """Repeated updates decay old scores toward zero."""
    project = _make_project_with_design_doc({"cochange_index": {"a.py": {"b.py": 1.0}}})
    # Apply 10 rounds of decay without adding new data (empty list → early return)
    for _ in range(10):
        # Use a single-file list (no pairs generated) to trigger decay only
        _update_cochange_index(project, [["a.py"]])
    idx = project.design_doc.get("cochange_index", {})
    # After enough decay cycles, score should either be pruned or very small
    score = (idx.get("a.py") or {}).get("b.py", 0.0)
    assert score < 0.35  # decayed below MIN_SCORE threshold (0.3)


def test_cochange_index_prunes_to_top_5():
    """Only the top-5 co-changed files are kept per file."""
    project = _make_project_with_design_doc()
    # Create 7 files that all co-change with a.py
    files = ["a.py"] + [f"file{i}.py" for i in range(7)]
    _update_cochange_index(project, [files])
    idx = project.design_doc["cochange_index"]
    assert len(idx["a.py"]) == 5


def test_cochange_index_skips_single_file_prs():
    """PRs with only one file produce no pairs."""
    project = _make_project_with_design_doc()
    _update_cochange_index(project, [["solo.py"]])
    idx = project.design_doc.get("cochange_index", {})
    assert idx == {}


def test_cochange_index_no_op_on_empty_input():
    """Empty merged_files_lists returns immediately with no design_doc change."""
    project = _make_project_with_design_doc({"cochange_index": {"a.py": {"b.py": 2.0}}})
    _update_cochange_index(project, [])
    # design_doc must be unchanged
    assert project.design_doc["cochange_index"]["a.py"]["b.py"] == 2.0


def test_cochange_index_caps_total_files():
    """Index is capped at MAX_FILES (200) tracked files."""
    # Build a large index with 210 files
    large_index: dict = {
        f"file{i}.py": {f"file{i + 1}.py": float(i)} for i in range(210)
    }
    project = _make_project_with_design_doc({"cochange_index": large_index})
    # Trigger an update with a small new PR
    _update_cochange_index(project, [["x.py", "y.py"]])
    idx = project.design_doc["cochange_index"]
    assert len(idx) <= 200


@pytest.mark.asyncio
async def test_cochange_index_updated_on_merged_pr(db_session: AsyncSession):
    """_update_direction_research_outcomes populates cochange_index from _pr_changed_files."""
    direction_id = "d-cochange"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "_pr_changed_files": [
                "app/models.py",
                "app/schemas.py",
                "tests/test_models.py",
            ],
        },
        title="merged-with-files",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    idx = (project.design_doc or {}).get("cochange_index", {})
    assert "app/models.py" in idx
    assert "app/schemas.py" in idx["app/models.py"]
    assert "tests/test_models.py" in idx["app/models.py"]


@pytest.mark.asyncio
async def test_cochange_index_not_updated_when_no_changed_files(
    db_session: AsyncSession,
):
    """No cochange_index entry when merged PR has no _pr_changed_files."""
    direction_id = "d-nofiles"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},  # no _pr_changed_files
        title="merged-no-files",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    # cochange_index should not exist or be empty
    idx = (project.design_doc or {}).get("cochange_index", {})
    assert idx == {}


@pytest.mark.asyncio
async def test_scope_breakdown_unknown_default(db_session: AsyncSession):
    """Tasks with scope in context are categorized correctly; tasks missing scope default to 'unknown'."""
    direction_id = "d-unknown-scope"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # Task with scope in context — should be categorized as "small"
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
        payloads=[{"type": "fix"}],
        title="with-scope",
    )
    # Task with no scope in context — should default to "unknown"
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "fix"}],
        title="no-scope",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert "small" in entry["scope_breakdown"]
    assert entry["scope_breakdown"]["small"]["created"] == 1
    assert "unknown" in entry["scope_breakdown"]
    assert entry["scope_breakdown"]["unknown"]["created"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 8. Decomposition quality (plan parent metrics)
# ─────────────────────────────────────────────────────────────────────────────


def test_longest_chain_length_empty_and_linear():
    assert _longest_chain_length({}) == 0
    # A -> B -> C (depth 3)
    assert _longest_chain_length({"a": [], "b": ["a"], "c": ["b"]}) == 3


def test_longest_chain_length_breaks_cycles_and_drops_external():
    # Cycle a→b→a is treated as depth 0 for the cycling node; external IDs ignored.
    chain = _longest_chain_length({"a": ["b"], "b": ["a", "zzz-external"]})
    assert chain >= 1


@pytest.mark.asyncio
async def test_decomposition_quality_basic(db_session: AsyncSession):
    """For a terminal plan child with subtasks, metrics are captured."""
    direction_id = "d-decomp"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "plan"}],
        title="plan-a",
    )
    # Subtask 1: merged, 2 files, cross-module (backend + tests)
    s1 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "_pr_changed_files": ["backend/app/x.py", "tests/test_x.py"],
        },
        title="s1",
    )
    # Subtask 2: merged, 1 file (single module)
    s2 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["backend/app/y.py"]},
        title="s2",
    )
    # Subtask 3: failed with non-infra error; depends on s1 and s2
    s3 = await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        title="s3",
        latest_status_message="unit test failed",
    )
    s3.depends_on = [str(s1.id), str(s2.id)]
    db_session.add(s3)
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert len(dq) == 1
    q = dq[0]
    assert q["plan_task_id"] == str(plan.id)
    assert q["subtask_count"] == 3
    # merged=2, failed non-infra=1 → 2/3
    assert q["subtask_merge_rate"] == round(2 / 3, 3)
    # file counts: 2, 1 → avg 1.5, max 2
    assert q["avg_files_per_subtask"] == 1.5
    assert q["max_files_in_subtask"] == 2
    # dependency depth: s3 -> (s1|s2) = 2
    assert q["dependency_depth"] == 2
    # cross-module: only s1 touches >=2 top-level dirs
    assert q["cross_module_count"] == 1


@pytest.mark.asyncio
async def test_decomposition_quality_ignores_infra_failures_in_merge_rate(
    db_session: AsyncSession,
):
    """Infra-class failures are excluded from the merge rate denominator."""
    direction_id = "d-decomp-infra"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "plan"}],
        title="plan-b",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["a.py"]},
        title="m",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
        title="infra",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    # merged=1, failed_non_infra=0 → 1/1
    assert dq[0]["subtask_merge_rate"] == 1.0


@pytest.mark.asyncio
async def test_decomposition_quality_records_failed_plan_with_zero_subtasks(
    db_session: AsyncSession,
):
    """A terminal plan that produced no subtasks gets a sentinel entry with
    subtask_merge_rate=0.0 so _poor_decomp_history can count the failure."""
    direction_id = "d-decomp-zero"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
        payloads=[{"type": "plan"}],
        title="plan-zero-subtasks",
        latest_status_message="plan handler crashed before spawning",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert len(dq) == 1
    entry = dq[0]
    assert entry["plan_task_id"] == str(plan.id)
    assert entry["subtask_count"] == 0
    assert entry["subtask_merge_rate"] == 0.0


@pytest.mark.asyncio
async def test_decomposition_quality_skips_non_terminal_plans(db_session: AsyncSession):
    """Plan tasks still EVALUATING/running are not included."""
    direction_id = "d-decomp-skip"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    plan = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "plan"}],
        title="plan-open",
    )
    await _create_child_task(
        db_session,
        plan.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "_pr_changed_files": ["a.py"]},
        title="s",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    dq = project.directions[0]["research_history"][0]["decomposition_quality"]
    assert dq == []


# ─────────────────────────────────────────────────────────────────────────────
# 9. Pipeline scope merge rate auto-population
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _reset_pipeline_scope_rates():
    """Snapshot/restore runtime_config['pipeline_scope_merge_rates'] per test."""
    original = dict(runtime_config.get("pipeline_scope_merge_rates") or {})
    runtime_config["pipeline_scope_merge_rates"] = {}
    yield
    runtime_config["pipeline_scope_merge_rates"] = original


def _direction_with_scope_breakdown(direction_id: str, scope_breakdown: dict) -> dict:
    """Build a direction with one research_history entry carrying scope_breakdown."""
    return {
        "id": direction_id,
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"task_id": str(uuid.uuid4()), "scope_breakdown": scope_breakdown}
        ],
    }


def test_refresh_pipeline_scope_merge_rates_aggregates_and_publishes(
    _reset_pipeline_scope_rates,
):
    """Rates are aggregated across all directions and written to runtime_config."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "small": {
                        "created": 4,
                        "merged": 3,
                        "failed": 1,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
            _direction_with_scope_breakdown(
                "d2",
                {
                    "small": {
                        "created": 3,
                        "merged": 2,
                        "failed": 1,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    # small effective = (3+2) merged + (1+1) failed - 0 infra = 7 >= 5
    # rate = 5 / 7 = 0.714
    assert runtime_config["pipeline_scope_merge_rates"] == {"small": round(5 / 7, 3)}


def test_refresh_pipeline_scope_merge_rates_omits_sparse_scopes(
    _reset_pipeline_scope_rates,
):
    """Scopes with < 5 effective data points are excluded from the published dict."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "small": {
                        "created": 6,
                        "merged": 4,
                        "failed": 2,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                    "medium": {
                        "created": 3,
                        "merged": 1,
                        "failed": 2,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    rates = runtime_config["pipeline_scope_merge_rates"]
    assert "small" in rates  # effective = 6, included
    assert "medium" not in rates  # effective = 3, excluded


def test_refresh_pipeline_scope_merge_rates_excludes_infra_from_denominator(
    _reset_pipeline_scope_rates,
):
    """Non-overflow infra failures are excluded from the effective denominator."""
    project = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "medium": {
                        "created": 10,
                        "merged": 3,
                        "failed": 7,
                        "failed_infra": 5,
                        "failed_scope_overflow": 1,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project)
    # effective = 3 merged + 7 failed - (5 infra - 1 overflow) = 3 + 7 - 4 = 6
    # rate = 3 / 6 = 0.5
    assert runtime_config["pipeline_scope_merge_rates"] == {"medium": 0.5}


def test_refresh_pipeline_scope_merge_rates_no_directions(_reset_pipeline_scope_rates):
    """Empty directions produces an empty rates dict."""
    project = Project(name="p", user_id="u", directions=[])
    _refresh_pipeline_scope_merge_rates(project)
    assert runtime_config["pipeline_scope_merge_rates"] == {}


def test_pipeline_scope_expansion_ratios_fallback(_reset_pipeline_scope_rates):
    """Pipeline-wide expansion ratio is published when samples >= 5; fallback chain
    uses the pipeline signal only when direction and project history lack data.
    """
    from app.research_completion import _avg_medium_expansion_ratio

    # Pipeline signal: 5 medium history entries with avg_expansion_ratio.
    entries = [
        {
            "task_id": str(uuid.uuid4()),
            "scope_breakdown": {
                "medium": {
                    "created": 1,
                    "merged": 1,
                    "failed": 0,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "avg_expansion_ratio": 3.0,
                    "avg_expansion_ratio_merged": 3.0,
                },
            },
        }
        for _ in range(5)
    ]
    seeded = {
        "id": "d-seed",
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": entries,
    }
    # A brand-new direction with no research_history — only benefits from the
    # pipeline-wide fallback.
    fresh = {
        "id": "d-new",
        "text": "t",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [],
    }
    project = Project(name="p", user_id="u", directions=[seeded, fresh])

    _refresh_pipeline_scope_merge_rates(project)
    assert runtime_config["pipeline_scope_expansion_ratios"] == {"medium": 3.0}

    # Direction/project helper returns None for the fresh direction alone —
    # confirms the fallback chain would reach the pipeline tier.
    assert _avg_medium_expansion_ratio([fresh]) is None

    # Sparse signal (< 5 samples) → no publication, fallback falls through to 2.0.
    project_sparse = Project(
        name="p",
        user_id="u",
        directions=[
            _direction_with_scope_breakdown(
                "d1",
                {
                    "medium": {
                        "created": 1,
                        "merged": 1,
                        "failed": 0,
                        "failed_infra": 0,
                        "failed_scope_overflow": 0,
                        "avg_expansion_ratio": 3.0,
                        "avg_expansion_ratio_merged": 3.0,
                    },
                },
            ),
        ],
    )
    _refresh_pipeline_scope_merge_rates(project_sparse)
    assert runtime_config["pipeline_scope_expansion_ratios"] == {}


@pytest.mark.asyncio
async def test_update_outcomes_publishes_pipeline_scope_rates(
    db_session: AsyncSession, _reset_pipeline_scope_rates
):
    """_update_direction_research_outcomes writes rates after updating a direction."""
    direction_id = "d-rates"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    # Seed another direction with history so the cross-direction aggregation reaches
    # the 5-effective threshold after this cycle's 2 small tasks land.
    seeded_direction = _direction_with_scope_breakdown(
        "d-seed",
        {
            "small": {
                "created": 3,
                "merged": 3,
                "failed": 0,
                "failed_infra": 0,
                "failed_scope_overflow": 0,
            },
        },
    )
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2),
        seeded_direction,
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "small"},
        payloads=[{"type": "create"}],
        title="s1",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="s2",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    # Combined small: 1 merged + 3 seeded merged = 4; 1 failed; infra 0.
    # With seeded 3 merged + this cycle 1 merged + 1 failed = effective 5 → 4/5 = 0.8
    rates = runtime_config["pipeline_scope_merge_rates"]
    assert rates == {"small": 0.8}


@pytest.mark.asyncio
async def test_update_outcomes_writes_cost_by_provider_model(db_session: AsyncSession):
    """_update_direction_research_outcomes records per-(provider, model) cost on
    the research_history entry, keyed by f"{provider}|{model}". Tasks lacking
    provider/model fall under "unknown|unknown" so legacy rows still contribute."""
    direction_id = "d-pm-rollup"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    now = _utcnow()
    # Two child tasks across distinct (provider, model) pairs; one legacy without
    # provider/model. Each runs for a known VM duration so cost is deterministic.
    t_anth = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="anth",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="anthropic",
        model="claude-opus-4-7",
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )
    t_oai = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="oai",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="openai",
        model="gpt-5.4",
        vm_provisioned_at=now - timedelta(minutes=30),
        vm_stopped_at=now,
        vm_spot=True,
    )
    t_legacy = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="legacy",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # provider/model omitted — legacy row.
        vm_provisioned_at=now - timedelta(minutes=10),
        vm_stopped_at=now,
        vm_spot=True,
    )
    db_session.add_all([t_anth, t_oai, t_legacy])
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    entry = project.directions[0]["research_history"][0]
    by_pm = entry.get("cost_by_provider_model")
    assert isinstance(by_pm, dict) and by_pm, "cost_by_provider_model must be populated"
    assert "anthropic|claude-opus-4-7" in by_pm
    assert "openai|gpt-5.4" in by_pm
    assert "unknown|unknown" in by_pm
    for v in by_pm.values():
        assert v > 0
    # The aggregate matches the entry-level estimated_cost_usd within rounding.
    assert abs(sum(by_pm.values()) - entry["estimated_cost_usd"]) < 1e-3


@pytest.mark.asyncio
async def test_update_outcomes_cost_provider_switch_retry_no_chimera(
    db_session: AsyncSession,
):
    """Banked retry attempts must contribute to cost under their OWN recorded
    (provider, model) pair, not a chimera mix of attempt-1 provider + attempt-2
    model or vice versa. Specifically:

    (a) No cross-provider chimera bucket (providerA|modelB or providerB|modelA).
    (b) Each attempt's cost lands under its own recorded pair.
    (c) The banked earlier-attempt cost is NOT lost from the rollup.
    (d) The breakdown bucket sum equals estimated_cost_usd.
    (e) An unresolvable half degrades to unknown|unknown wholesale.
    """
    direction_id = "d-chimera-test"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    now = _utcnow()

    # Task that retried: attempt 1 on anthropic/claude-opus-4-7, attempt 2 on
    # openai/gpt-5.4. Both attempts are banked via attempt_history snapshots
    # (banking clears vm_provisioned_at/vm_stopped_at).
    attempt1_cost = 0.012
    attempt2_cost = 0.007
    retried_task = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="retried",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="openai",
        model="gpt-5.4",
        # No live span (both attempts banked)
        vm_provisioned_at=None,
        vm_stopped_at=None,
        vm_spot=False,
    )
    retried_task.attempt_history = [
        {
            "vm_provisioned_at": (now - timedelta(hours=2)).isoformat(),
            "vm_stopped_at": (now - timedelta(hours=1)).isoformat(),
            "vm_spot": False,
            "vm_machine_type": "e2-standard-2",
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "vm_runtime_cost_usd": attempt1_cost,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_call_count": 0,
        },
        {
            "vm_provisioned_at": (now - timedelta(minutes=60)).isoformat(),
            "vm_stopped_at": (now - timedelta(minutes=30)).isoformat(),
            "vm_spot": False,
            "vm_machine_type": "e2-standard-2",
            "provider": "openai",
            "model": "gpt-5.4",
            "vm_runtime_cost_usd": attempt2_cost,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_call_count": 0,
        },
    ]

    # Task with only provider set (no model) — must degrade to unknown|unknown
    partial_task = Task(
        project_id=project.id,
        parent_task_id=research_task.id,
        title="partial",
        payloads=[{"type": "create", "scope": "small"}],
        context={},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        provider="anthropic",
        model=None,
    )
    partial_cost = 0.005
    partial_task.attempt_history = [
        {
            "vm_provisioned_at": (now - timedelta(minutes=20)).isoformat(),
            "vm_stopped_at": (now - timedelta(minutes=10)).isoformat(),
            "vm_spot": True,
            "vm_machine_type": "e2-standard-2",
            "provider": "anthropic",
            "model": None,
            "vm_runtime_cost_usd": partial_cost,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "llm_call_count": 0,
        },
    ]

    db_session.add_all([retried_task, partial_task])
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)

    entry = project.directions[0]["research_history"][0]
    by_pm = entry.get("cost_by_provider_model")
    assert isinstance(by_pm, dict) and by_pm, "cost_by_provider_model must be populated"

    # (a) No cross-provider chimera keys
    assert "anthropic|gpt-5.4" not in by_pm, (
        "chimera key anthropic|gpt-5.4 must not appear"
    )
    assert "openai|claude-opus-4-7" not in by_pm, (
        "chimera key openai|claude-opus-4-7 must not appear"
    )

    # (b) Each attempt's own pair present
    assert "anthropic|claude-opus-4-7" in by_pm, "attempt-1 pair must appear"
    assert "openai|gpt-5.4" in by_pm, "attempt-2 pair must appear"

    # (c) Banked earlier-attempt cost not lost
    assert by_pm["anthropic|claude-opus-4-7"] == pytest.approx(attempt1_cost, abs=1e-4)
    assert by_pm["openai|gpt-5.4"] == pytest.approx(attempt2_cost, abs=1e-4)

    # (e) Unresolvable half degrades to unknown|unknown
    assert "unknown|unknown" in by_pm, (
        "partial provider/model must degrade to unknown|unknown"
    )
    assert "anthropic|unknown" not in by_pm, "half-resolved key must not appear"

    # (d) Bucket sum == estimated_cost_usd
    assert abs(sum(by_pm.values()) - entry["estimated_cost_usd"]) < 1e-3


# ─────────────────────────────────────────────────────────────────────────────
# 10. Cascade-cancellation exclusion from failure metrics
# ─────────────────────────────────────────────────────────────────────────────


def test_is_cascade_cancelled_detects_signals():
    """All four cascade signals are detected; genuine CANCELLED is not."""
    # Requeue replacement via _replaced_by_task_id in context
    t = Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": "x"},
        created_by="ORCHESTRATOR",
    )
    assert _is_cascade_cancelled(t)
    # Requeue message
    t.context = {}
    t.latest_status_message = "Parent requeued — replaced by retry"
    assert _is_cascade_cancelled(t)
    # Parent cascade
    t.latest_status_message = "Cancelled: parent evaluation timeout after 6h"
    assert _is_cascade_cancelled(t)
    # Dependency cascade
    t.latest_status_message = "Cancelled: dependency task failed"
    assert _is_cascade_cancelled(t)
    # Review-pipeline supersession ("Superseded by fix on PR #N: ...")
    t.latest_status_message = (
        "Superseded by fix on PR #5317: review was scoped to a pre-conflict snapshot"
    )
    assert _is_cascade_cancelled(t)
    # Stale-heartbeat supersession ("Superseded by new review for PR #N: ...")
    t.latest_status_message = (
        "Superseded by new review for PR #42: no heartbeat for >30min"
    )
    assert _is_cascade_cancelled(t)
    # Genuine cancellation (operator action with no cascade marker)
    t.latest_status_message = "Cancelled by admin"
    assert not _is_cascade_cancelled(t)
    # FAILED is never a cascade cancellation
    t.status = TaskStatus.FAILED
    t.latest_status_message = "Parent requeued — replaced by retry"
    assert not _is_cascade_cancelled(t)


def test_counts_as_failure_excludes_cascade_cancelled():
    t = Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": "x"},
        created_by="ORCHESTRATOR",
    )
    assert not _counts_as_failure(t)
    t.context = {}
    t.latest_status_message = "Cancelled by admin"
    assert _counts_as_failure(t)
    t.status = TaskStatus.FAILED
    assert _counts_as_failure(t)


def _compute_experiment_task(
    outcome, *, status=TaskStatus.COMPLETED, acceptance_grade=None
):
    plan_output = (
        {"compute_result": {"outcome": outcome, "final_cost_usd": 1.0}}
        if outcome is not None
        else None
    )
    if isinstance(plan_output, dict) and acceptance_grade is not None:
        plan_output["knowledge_document"] = {
            "source_kind": "compute_experiment",
            "knowledge_acceptance_evidence": acceptance_grade == "acceptance",
            "compute_acceptance_grade": acceptance_grade,
        }
    return Task(
        project_id=uuid.uuid4(),
        title="compute",
        payloads=[{"type": "compute_experiment"}],
        status=status,
        context={},
        plan_output=plan_output,
        created_by="ORCHESTRATOR",
    )


def test_compute_experiment_outcome_feeds_success_predicate():
    """Only acceptance-grade SUCCESS compute knowledge is a successful outcome;
    budget/deadline terminations and measured-only results are neutral."""
    from app.task_outcomes import evaluate_task_outcome, is_successful_outcome

    success = _compute_experiment_task("SUCCESS", acceptance_grade="acceptance")
    assert is_successful_outcome(success) is True
    assert evaluate_task_outcome(success).status == "success"

    measured_only = _compute_experiment_task(
        "SUCCESS", acceptance_grade="measured_only"
    )
    assert is_successful_outcome(measured_only) is False
    assert evaluate_task_outcome(measured_only).status == "neutral"

    spend_cap = _compute_experiment_task("SPEND_CAP_EXCEEDED")
    assert is_successful_outcome(spend_cap) is False
    assert evaluate_task_outcome(spend_cap).status == "neutral"

    failure = _compute_experiment_task("FAILURE")
    assert evaluate_task_outcome(failure).status == "failure"


def _cancelled_task(context):
    return Task(
        project_id=uuid.uuid4(),
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context=context,
        created_by="ORCHESTRATOR",
    )


def test_non_genuine_cancelled_excludes_new_markers():
    """CROSS-FILE CONSISTENCY: the FIX A / FIX D / FIX H non-genuine CANCELLED
    outcomes must NOT count toward direction failure metrics."""
    # FIX A: pr_closed_non_rejection
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    )
    # FIX D: cap_exhausted_after_approve
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_detail": "cap_exhausted_after_approve"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_detail": "cap_exhausted_after_approve"})
    )
    # Supersession churn cancellations are non-genuine too, even though they
    # keep failure_category=AGENT_TIMEOUT for the infra bucket.
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_detail": "superseded_by_review_pipeline"})
    )
    assert not _counts_as_failure(
        _cancelled_task({"failure_detail": "superseded_by_review_pipeline"})
    )
    # FIX H: REASSIGNED handoff (and the SCOPE_EMPTY no-op) lands CANCELLED.
    assert _is_non_genuine_cancelled(
        _cancelled_task({"failure_category": "REASSIGNED"})
    )
    assert not _counts_as_failure(_cancelled_task({"failure_category": "REASSIGNED"}))
    assert not _counts_as_failure(_cancelled_task({"failure_category": "SCOPE_EMPTY"}))
    # prerequisite_escalated_to_parent — research cycle cancels a child whose
    # prerequisite has no live/deferred provider this cycle. Recoverable: work
    # re-surfaces in the next cycle. Must NOT count as a genuine death.
    assert _is_non_genuine_cancelled(
        _cancelled_task({"prerequisite_escalated_to_parent": True})
    )
    assert not _counts_as_failure(
        _cancelled_task({"prerequisite_escalated_to_parent": True})
    )
    # CO_DEFERRED_SHELVED — co-deferred dependent shelved alongside its same-cycle-
    # deferred prerequisite. Recoverable shelving, not a failure.
    from app.failure_classification import CO_DEFERRED_SHELVED

    assert _is_non_genuine_cancelled(
        _cancelled_task({CO_DEFERRED_SHELVED: True})
    )
    assert not _counts_as_failure(
        _cancelled_task({CO_DEFERRED_SHELVED: True})
    )


def test_genuine_cancelled_and_failed_still_count():
    """GUARDRAIL: a genuine CANCELLED (operator-cancelled with no non-genuine
    marker) and a genuine FAILED still count toward failure metrics."""
    genuine = _cancelled_task({})
    genuine.latest_status_message = "Cancelled by admin"
    assert not _is_non_genuine_cancelled(genuine)
    assert _counts_as_failure(genuine)
    # A real code failure that landed CANCELLED with a CODE_ERROR category is
    # NOT excluded (only REASSIGNED/SCOPE_EMPTY categories are non-genuine).
    code_err = _cancelled_task({"failure_category": "CODE_ERROR"})
    code_err.latest_status_message = "Cancelled by admin"
    assert not _is_non_genuine_cancelled(code_err)
    assert _counts_as_failure(code_err)
    # FAILED is always a genuine failure.
    failed = _cancelled_task({"failure_detail": "pr_closed_non_rejection"})
    failed.status = TaskStatus.FAILED
    assert _counts_as_failure(failed)


@pytest.mark.asyncio
async def test_cascade_cancelled_excluded_from_failure_buckets(
    db_session: AsyncSession,
):
    """A cascade-cancelled task stays in tasks_cancelled but is excluded from
    taxonomy_summary, scope_breakdown.failed, type_breakdown.failed,
    stage_dropout, and failed_task_details."""
    direction_id = "d-cascade"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Genuine FAILED — should count everywhere
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "failure_category": "CODE_ERROR"},
        payloads=[{"type": "create"}],
        title="real-failure",
        latest_status_message="pytest failed",
    )
    # Cascade-cancelled via requeue clone — should NOT count as failure
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small", "_replaced_by_task_id": str(uuid.uuid4())},
        payloads=[{"type": "create"}],
        title="requeued",
        latest_status_message="Parent requeued — replaced by retry",
    )
    # Cascade-cancelled via parent dependency failure — should NOT count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small"},
        payloads=[{"type": "create"}],
        title="dep-cascade",
        latest_status_message="Cancelled: parent dependency failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # tasks_cancelled still reflects BOTH cancelled tasks — raw status count is unchanged
    assert entry["tasks_cancelled"] == 2
    assert entry["tasks_failed"] == 1

    # Only the genuine CODE_ERROR should appear in taxonomy / failed_task_details
    assert len(entry["failed_task_details"]) == 1
    assert entry["failed_task_details"][0]["title"] == "real-failure"
    ts = entry["taxonomy_summary"]
    assert sum(ts.values()) == 1  # cascade entries not counted

    # scope_breakdown.small.failed should only include the real failure
    sb = entry["scope_breakdown"]
    assert sb["small"]["failed"] == 1
    assert sb["small"]["failed_code_error"] == 1

    # type_breakdown.create.failed excludes cascade cancellations
    assert entry["type_breakdown"]["create"]["failed"] == 1

    # stage_dropout.code_error only counts the real failure
    assert entry["stage_dropout"]["tasks_failed_code_error"] == 1


@pytest.mark.asyncio
async def test_genuine_cancelled_still_counts_as_failure(db_session: AsyncSession):
    """A CANCELLED task without cascade markers still counts toward failure metrics."""
    direction_id = "d-genuine-cancel"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        context={"scope": "small", "failure_category": "AGENT_TIMEOUT"},
        payloads=[{"type": "create"}],
        title="genuine-cancel",
        latest_status_message="Task timed out on VM",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["scope_breakdown"]["small"]["failed"] == 1
    assert len(entry["failed_task_details"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Preventive context tracking
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preventive_context_counts(db_session: AsyncSession):
    """preventive_context_tasks_{created,merged,failed} counted from child context."""
    direction_id = "d-prev"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # 3 preventive-context: 1 merged, 1 failed, 1 in-progress/other
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"has_preventive_context": True, "pr_merged": True},
        title="prev-merged",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"has_preventive_context": True},
        title="prev-failed",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"has_preventive_context": True},  # completed, not merged
        title="prev-completed-no-pr",
    )
    # Non-preventive: shouldn't count
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="no-prev-merged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["preventive_context_tasks_created"] == 3
    assert entry["preventive_context_tasks_merged"] == 1
    assert entry["preventive_context_tasks_failed"] == 1


# Retired: test_remediation_task_counts. Tested the retired fields remediation_tasks_created,
# remediation_tasks_merged, remediation_tasks_failed which were never consumed. Removed 2026-06-05.


def test_synthesize_prompt_quality_patterns_emits_when_signal_meaningful():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 1200,
                    "avg_related_file_count": 4.0,
                    "frac_mentions_tests": 0.8,
                    "frac_mentions_symbols": 0.7,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 800,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.3,
                    "frac_mentions_symbols": 0.2,
                },
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1300,
                    "avg_related_file_count": 5.0,
                    "frac_mentions_tests": 0.9,
                    "frac_mentions_symbols": 0.8,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 700,
                    "avg_related_file_count": 1.5,
                    "frac_mentions_tests": 0.2,
                    "frac_mentions_symbols": 0.1,
                },
            }
        },
    ]
    notes = synthesize_prompt_quality_patterns(history)
    assert len(notes) >= 3
    joined = " ".join(notes)
    assert "chars" in joined
    assert "tests" in joined


def test_synthesize_prompt_quality_patterns_skips_when_too_few_cycles():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1200,
                    "frac_mentions_tests": 0.8,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "frac_mentions_tests": 0.2,
                },
            }
        },
    ]
    assert synthesize_prompt_quality_patterns(history) == []


def test_synthesize_prompt_quality_patterns_skips_when_differences_are_noise():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 1000,
                    "avg_related_file_count": 3.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 1010,
                    "avg_related_file_count": 3.1,
                    "frac_mentions_tests": 0.55,
                    "frac_mentions_symbols": 0.5,
                },
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 2,
                    "avg_char_count": 990,
                    "avg_related_file_count": 2.9,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 1000,
                    "avg_related_file_count": 3.0,
                    "frac_mentions_tests": 0.55,
                    "frac_mentions_symbols": 0.5,
                },
            }
        },
    ]
    assert synthesize_prompt_quality_patterns(history) == []


def test_synthesize_prompt_quality_patterns_emits_scope_segmented_note():
    from app.direction_outcomes import synthesize_prompt_quality_patterns

    history = [
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "merged_small": {"count": 2, "avg_char_count": 300},
                "failed_small": {"count": 2, "avg_char_count": 900},
            }
        },
        {
            "prompt_telemetry": {
                "merged": {
                    "count": 3,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "failed": {
                    "count": 2,
                    "avg_char_count": 600,
                    "avg_related_file_count": 2.0,
                    "frac_mentions_tests": 0.5,
                    "frac_mentions_symbols": 0.5,
                },
                "merged_small": {"count": 2, "avg_char_count": 320},
                "failed_small": {"count": 2, "avg_char_count": 880},
            }
        },
    ]
    notes = synthesize_prompt_quality_patterns(history)
    assert any("Small-scope" in n for n in notes)


def test_summarize_review_quality_feedback_includes_top_categories():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 1, "tasks_completed": 1}]
    dfa = {
        "missing_tests": 4,
        "style": 2,
        "naming": 1,
        "obscure": 1,
        "total": 8,
        "last_updated": "2026-04-20T00:00:00",
    }
    result = summarize_review_quality_feedback(history, dfa)
    assert result is not None
    assert result["top_categories"] == {"missing_tests": 4, "style": 2, "naming": 1}


def test_summarize_review_quality_feedback_skips_top_categories_when_sparse():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 1, "tasks_completed": 1}]
    dfa = {"missing_tests": 1, "style": 1, "total": 2}
    result = summarize_review_quality_feedback(history, dfa)
    assert result is not None
    assert "top_categories" not in result


def test_summarize_review_quality_feedback_no_dfa_unchanged():
    from app.direction_outcomes import summarize_review_quality_feedback

    history = [{"reviews_dismissed": 2, "tasks_completed": 1}]
    result = summarize_review_quality_feedback(history)
    assert result is not None
    assert "top_categories" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Autonomous / operator-assisted classification
# ─────────────────────────────────────────────────────────────────────────────


class TestIsAutonomousMerge:
    """Unit tests for is_autonomous_merge — pure function, no DB required."""

    def _make_task(self, created_by="ORCHESTRATOR", context=None) -> Task:
        t = Task(
            project_id=uuid.uuid4(),
            title="test",
            payloads=[{"type": "create"}],
            context=context or {},
            status=TaskStatus.COMPLETED,
            created_by=created_by,
        )
        return t

    def test_orchestrator_created_no_override_is_autonomous(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="ORCHESTRATOR")
        assert is_autonomous_merge(task) is True

    def test_user_created_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="USER")
        assert is_autonomous_merge(task) is False

    def test_review_dismissed_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"review_dismissed": True})
        assert is_autonomous_merge(task) is False

    def test_user_overrode_review_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"user_overrode_review": True})
        assert is_autonomous_merge(task) is False

    def test_orchestrator_with_pr_merged_and_no_override_is_autonomous(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(context={"pr_merged": True})
        assert is_autonomous_merge(task) is True

    def test_user_created_even_with_pr_merged_is_operator_assisted(self):
        from app.direction_outcomes import is_autonomous_merge

        task = self._make_task(created_by="USER", context={"pr_merged": True})
        assert is_autonomous_merge(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation: autonomous_prs_merged + assisted_prs_merged sum invariant
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_assisted_sum_equals_prs_merged(db_session: AsyncSession):
    """autonomous_prs_merged + assisted_prs_merged == prs_merged for any mix."""
    direction_id = "d-attribution"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # 2 autonomous merges, 1 operator-assisted merge, 1 rejected
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="ORCHESTRATOR",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        context={"pr_merged": True},
        created_by="USER",
    )
    await _create_child_task(
        db_session, research_task.id, project.id, context={"pr_rejected": True}
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 3
    assert entry["autonomous_prs_merged"] == 2
    assert entry["assisted_prs_merged"] == 1
    assert (
        entry["autonomous_prs_merged"] + entry["assisted_prs_merged"]
        == entry["prs_merged"]
    )


@pytest.mark.asyncio
async def test_all_autonomous_direction_counts(db_session: AsyncSession):
    """All merges autonomous → assisted==0 and autonomous==prs_merged."""
    direction_id = "d-all-auto"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    for _ in range(2):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            context={"pr_merged": True},
            created_by="ORCHESTRATOR",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["autonomous_prs_merged"] == 2
    assert entry["assisted_prs_merged"] == 0
    assert entry["operator_action_count"] == 0


@pytest.mark.asyncio
async def test_zero_autonomous_all_assisted(db_session: AsyncSession):
    """All merges operator-assisted → autonomous==0."""
    direction_id = "d-all-assisted"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    for _ in range(2):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            context={"pr_merged": True, "review_dismissed": True},
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["autonomous_prs_merged"] == 0
    assert entry["assisted_prs_merged"] == 2
    assert entry["prs_merged"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# _fold_evicted_into_aggregate: attribution rollup
# ─────────────────────────────────────────────────────────────────────────────


def test_fold_evicted_attribution_fields():
    """total_autonomous/assisted/operator aggregate correctly; legacy entries fall back to prs_merged."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    # One pre-attribution entry (no autonomous_prs_merged → fallback to prs_merged)
    # One post-attribution entry with split counts
    evicted = [
        {"timestamp": "2025-01-01T00:00:00", "tasks_created": 3, "prs_merged": 3},
        {
            "timestamp": "2025-02-01T00:00:00",
            "tasks_created": 5,
            "prs_merged": 3,
            "autonomous_prs_merged": 2,
            "assisted_prs_merged": 1,
            "operator_action_count": 2,
        },
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    # Legacy entry contributes prs_merged=3 to autonomous (conservative fallback)
    # Post-attribution entry contributes autonomous=2
    assert agg["total_autonomous_prs_merged"] == 5  # 3 + 2
    assert agg["total_assisted_prs_merged"] == 1
    assert agg["total_operator_actions"] == 2


def test_fold_evicted_attribution_accumulates_on_prior_agg():
    """New evictions accumulate on top of an existing aggregate with attribution fields."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    prior = {
        "total_cycles": 2,
        "total_autonomous_prs_merged": 4,
        "total_assisted_prs_merged": 1,
        "total_operator_actions": 1,
    }
    evicted = [
        {
            "timestamp": "2025-03-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 2,
            "autonomous_prs_merged": 1,
            "assisted_prs_merged": 1,
            "operator_action_count": 1,
        }
    ]
    agg = _fold_evicted_into_aggregate(prior, evicted)

    assert agg["total_autonomous_prs_merged"] == 5  # 4 + 1
    assert agg["total_assisted_prs_merged"] == 2  # 1 + 1
    assert agg["total_operator_actions"] == 2  # 1 + 1


def test_fold_evicted_acceptance_aggregates_survive_eviction():
    """proven/declared/merged-write totals roll into history_aggregate so a
    direction past the 10-cycle window keeps an honest proof signal; legacy
    entries (no acceptance fields) contribute 0 to every aggregate."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    evicted = [
        # Legacy entry — no acceptance_* fields → contributes 0 (not raw merges).
        {"timestamp": "2025-01-01T00:00:00", "tasks_created": 3, "prs_merged": 3},
        {
            "timestamp": "2025-02-01T00:00:00",
            "tasks_created": 5,
            "prs_merged": 4,
            "acceptance_proven_merged": 1,
            "acceptance_declared_merged": 3,
            "acceptance_merged_write_total": 4,
        },
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    assert agg["total_acceptance_proven_merged"] == 1
    assert agg["total_acceptance_declared_merged"] == 3
    assert agg["total_acceptance_merged_write_total"] == 4


def test_fold_evicted_acceptance_aggregates_accumulate_on_prior_agg():
    """New acceptance evictions accumulate on an existing aggregate; a prior agg
    written before the keys existed backfills from the defaults (legacy 0)."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    prior = {
        "total_cycles": 2,
        # Pre-acceptance aggregate: no total_acceptance_* keys at all.
        "total_verified_prs_merged": 6,
    }
    evicted = [
        {
            "timestamp": "2025-03-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 2,
            "acceptance_proven_merged": 2,
            "acceptance_declared_merged": 2,
            "acceptance_merged_write_total": 2,
        }
    ]
    agg = _fold_evicted_into_aggregate(prior, evicted)

    assert agg["total_acceptance_proven_merged"] == 2
    assert agg["total_acceptance_declared_merged"] == 2
    assert agg["total_acceptance_merged_write_total"] == 2


def test_fold_evicted_generalized_fields_survive_eviction():
    """Generalized successes and outcome-kind counts persist in history_aggregate."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    evicted = [
        {
            "timestamp": "2025-01-01T00:00:00",
            "prs_merged": 2,
        },
        {
            "timestamp": "2025-02-01T00:00:00",
            "generalized_successes": 3,
            "tasks_completed": 3,
            "tasks_failed": 1,
            "outcome_kind_breakdown": {
                "research_quality": {"success": 3, "neutral": 0, "failure": 1}
            },
        },
    ]

    agg = _fold_evicted_into_aggregate(None, evicted)

    assert agg["total_generalized_successes"] == 5
    assert agg["total_generalized_terminal_outcomes"] == 6
    assert agg["outcome_kind_breakdown"] == {
        "code_merge": {"success": 2, "neutral": 0, "failure": 0},
        "research_quality": {"success": 3, "neutral": 0, "failure": 1},
    }


def test_fold_evicted_generalized_fields_backfill_legacy_aggregate():
    """Legacy aggregates are upgraded before new evictions are added."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    prior = {
        "total_cycles": 3,
        "total_prs_merged": 4,
        "total_tasks_completed": 5,
        "total_tasks_failed": 1,
    }
    evicted = [
        {
            "timestamp": "2025-03-01T00:00:00",
            "generalized_successes": 2,
            "tasks_completed": 2,
            "outcome_kind_breakdown": {
                "research_quality": {"success": 2, "neutral": 0, "failure": 0}
            },
        }
    ]

    agg = _fold_evicted_into_aggregate(prior, evicted)

    assert agg["total_generalized_successes"] == 6
    assert agg["total_generalized_terminal_outcomes"] == 8
    # The legacy prior records 6 terminal task outcomes (5 completed + 1 failed)
    # but only 4 are attributable to a kind (code_merge, from prs_merged). The
    # backfill reconciles the breakdown with the terminal total by parking the 2
    # unattributable outcomes under read_only_unclassified, so the breakdown sums
    # to the asserted total_generalized_terminal_outcomes (4 + 2 + 2 == 8).
    assert agg["outcome_kind_breakdown"] == {
        "code_merge": {"success": 4, "neutral": 0, "failure": 0},
        "research_quality": {"success": 2, "neutral": 0, "failure": 0},
        "read_only_unclassified": {"success": 0, "neutral": 2, "failure": 0},
    }


def test_fold_evicted_reachable_recorded_cycles_tracks_presence_not_truthiness():
    """total_reachable_recorded_cycles increments on key presence, not value truthiness.

    An evicted cycle with reachable_prs_merged=0 (known-zero) must increment the
    counter so the aggregate can later signal 'reachability was measured' even when
    the total_reachable_prs_merged sum is 0.  A legacy cycle (key absent) must NOT
    increment the counter.
    """
    from app.direction_metrics import _fold_evicted_into_aggregate

    # One zero-reachable cycle (key present, value 0) and one legacy cycle (key absent).
    evicted = [
        {
            "timestamp": "2025-06-01T00:00:00",
            "prs_merged": 2,
            "verified_prs_merged": 2,
            "reachable_prs_merged": 0,  # present, zero — reachability measured, nothing confirmed
        },
        {
            "timestamp": "2025-05-01T00:00:00",
            "prs_merged": 1,
            "verified_prs_merged": 1,
            # reachable_prs_merged absent — legacy, unknown reachability
        },
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    # Only the cycle with the key present increments the counter.
    assert agg["total_reachable_recorded_cycles"] == 1
    # Total reachable is still 0 (not inflated by legacy fallback).
    assert agg["total_reachable_prs_merged"] == 0

    # Accumulate a second call with another zero-reachable cycle.
    evicted2 = [
        {
            "timestamp": "2025-07-01T00:00:00",
            "prs_merged": 3,
            "reachable_prs_merged": 0,
        }
    ]
    agg2 = _fold_evicted_into_aggregate(agg, evicted2)

    assert agg2["total_reachable_recorded_cycles"] == 2  # 1 + 1
    assert agg2["total_reachable_prs_merged"] == 0


def test_fold_evicted_stranded_prs_merged_accumulates():
    """stranded_prs_merged rolls into total_stranded_prs_merged and accumulates on a prior aggregate."""
    from app.direction_metrics import _fold_evicted_into_aggregate

    # One evicted entry with 2 verified merges, 1 confirmed-stranded
    evicted = [
        {
            "timestamp": "2025-04-01T00:00:00",
            "tasks_created": 3,
            "prs_merged": 2,
            "verified_prs_merged": 2,
            "stranded_prs_merged": 1,
        }
    ]
    agg = _fold_evicted_into_aggregate(None, evicted)

    assert agg["total_stranded_prs_merged"] == 1

    # Second call: accumulate on top of existing aggregate that already has stranded count
    evicted2 = [
        {
            "timestamp": "2025-05-01T00:00:00",
            "tasks_created": 2,
            "prs_merged": 3,
            "verified_prs_merged": 3,
            "stranded_prs_merged": 2,
        }
    ]
    agg2 = _fold_evicted_into_aggregate(agg, evicted2)

    assert agg2["total_stranded_prs_merged"] == 3  # 1 + 2


# ─────────────────────────────────────────────────────────────────────────────
# compute_direction_autonomy_state
# ─────────────────────────────────────────────────────────────────────────────


def test_autonomy_state_below_threshold():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [
            {"autonomous_prs_merged": 3, "operator_action_count": 0},
            {"autonomous_prs_merged": 5, "operator_action_count": 0},
        ],
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 8
    assert result["lifetime_operator_actions"] == 0
    assert result["retirement_eligible"] is False


def test_autonomy_state_at_threshold():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 10, "operator_action_count": 0}]
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 10
    assert result["lifetime_operator_actions"] == 0
    assert result["retirement_eligible"] is True


def test_autonomy_state_above_threshold_from_aggregate():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [],
        "history_aggregate": {
            "total_autonomous_prs_merged": 12,
            "total_operator_actions": 0,
        },
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 12
    assert result["retirement_eligible"] is True


def test_autonomy_state_operator_action_blocks_eligibility():
    """12 autonomous merges but 1 operator action — must NOT be retirement-eligible."""
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 12, "operator_action_count": 1}]
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 12
    assert result["lifetime_operator_actions"] == 1
    assert result["retirement_eligible"] is False


def test_autonomy_state_legacy_fallback_treats_prs_merged_as_autonomous():
    """Pre-attribution entries without autonomous_prs_merged fall back to prs_merged."""
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {"research_history": [{"prs_merged": 5}]}  # no autonomous_prs_merged key
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 5


def test_autonomy_state_combines_history_and_aggregate():
    from app.direction_outcomes import compute_direction_autonomy_state

    d = {
        "research_history": [{"autonomous_prs_merged": 3, "operator_action_count": 0}],
        "history_aggregate": {
            "total_autonomous_prs_merged": 7,
            "total_operator_actions": 0,
        },
    }
    result = compute_direction_autonomy_state(d)
    assert result["lifetime_autonomous_prs_merged"] == 10
    assert result["retirement_eligible"] is True


# ─────────────────────────────────────────────────────────────────────────────
# refresh_direction_outcomes_after_merge — heal stale prs_merged after a late merge
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_after_merge_heals_stale_prs_merged(db_session: AsyncSession):
    """Reproduces the churn-guard false positive and its fix.

    prs_merged is snapshotted when each child reaches COMPLETED — at which point
    its PR has only just opened, so the count is 0. The PR lands asynchronously
    later; refreshing after the landed signal is stamped recomputes the real
    count instead of leaving a stale 0 that trips the churn guardrail.
    """
    direction_id = "d-merge-refresh"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    db_session.add(project)
    await db_session.flush()

    child_ctx = {"direction_id": direction_id, "research_task_id": rtid}
    children = [
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context=dict(child_ctx),
            title=f"create {i}",
        )
        for i in range(2)
    ]
    await db_session.flush()

    # Phase 1 — children COMPLETED but PRs only just opened (no landed signal).
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["tasks_completed"] == 2
    assert entry["prs_merged"] == 0  # the stale snapshot that trips churn

    # Phase 2 — both PRs land on the integration branch (landed signal stamped),
    # then a single child's merge fires the refresh, healing the whole cycle.
    for c in children:
        c.context = {**c.context, "pr_merged": True, "landed_on_integration": True}
        db_session.add(c)
    await db_session.flush()

    await refresh_direction_outcomes_after_merge(children[0], db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 2  # both siblings counted, not just the trigger


@pytest.mark.asyncio
async def test_refresh_after_merge_noop_without_direction_context(
    db_session: AsyncSession,
):
    """A merged task that isn't a research-cycle child is a no-op (no crash)."""
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    task = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},  # no direction_id
    )
    await db_session.flush()
    # Must not raise and must not touch any direction state.
    await refresh_direction_outcomes_after_merge(task, db_session)


@pytest.mark.asyncio
async def test_refresh_after_out_of_band_merge_path_heals_prs_merged(
    db_session: AsyncSession,
):
    """A child whose PR lands via a newly-wired out-of-band path heals prs_merged.

    Models the gap fixed in Task 1: a child reported terminal (here FAILED, as in
    the agent_status / reaper out-of-band recovery) while its PR merged later. The
    recovery path stamps the landed signal and COMPLETED, then calls
    ``refresh_direction_outcomes_after_merge``. Before the refresh the cycle's
    prs_merged is the stale 0 that trips the zero-merge/churn guard; after it the
    landed child is counted (alongside a sibling that landed in the same window),
    proving the whole-cycle recompute the wired paths rely on.
    """
    direction_id = "d-oob-merge"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    db_session.add(project)
    await db_session.flush()

    child_ctx = {"direction_id": direction_id, "research_task_id": rtid}
    # The trigger child reported FAILED with an open PR (agent died after merge);
    # the sibling already COMPLETED. Neither yet carries the landed signal.
    trigger = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context=dict(child_ctx),
        title="out-of-band create",
    )
    sibling = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context=dict(child_ctx),
        title="sibling create",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 0  # stale: PRs not yet recorded as landed

    # The out-of-band recovery path lands BOTH PRs on the integration branch and
    # flips the trigger COMPLETED, then fires the refresh on the trigger child.
    trigger.status = TaskStatus.COMPLETED
    trigger.context = {
        **trigger.context,
        "pr_merged": True,
        "landed_on_integration": True,
    }
    sibling.context = {
        **sibling.context,
        "pr_merged": True,
        "landed_on_integration": True,
    }
    db_session.add(trigger)
    db_session.add(sibling)
    await db_session.flush()

    await refresh_direction_outcomes_after_merge(trigger, db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Both the trigger AND the sibling are counted — the recompute re-counts every
    # child of the cycle, not just the one that fired the refresh.
    assert entry["prs_merged"] == 2


@pytest.mark.asyncio
async def test_rollup_merge_triggers_direction_outcome_refresh(
    db_session: AsyncSession,
):
    """Rollup-squash merge heals stale prs_merged via the refresh call.

    Models the FALSE-AUTO-PAUSE root cause: children complete with PRs still
    open, so prs_merged=0 at snapshot time. The rollup later squash-merges all
    PRs, stamps landed_on_integration=True, then calls
    refresh_direction_outcomes_after_merge on the first direction-lineage child.
    The recompute re-counts every sibling in the cycle, so a single refresh
    heals the whole entry — verified_prs_merged rises from 0 to the real count.
    """
    direction_id = "d-rollup-refresh"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    db_session.add(project)
    await db_session.flush()

    child_ctx = {"direction_id": direction_id, "research_task_id": rtid}
    children = [
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context=dict(child_ctx),
            title=f"rollup child {i}",
        )
        for i in range(2)
    ]
    await db_session.flush()

    # Snapshot before rollup lands: PRs open but not yet merged.
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 0  # stale snapshot before rollup merge

    # Rollup squash-merges: stamp landed signal on all children.
    for c in children:
        c.context = {
            **c.context,
            "pr_merged": True,
            "landed_on_integration": True,
        }
        db_session.add(c)
    await db_session.flush()

    # The rollup merge path fires refresh on the first direction-lineage child.
    _dir_child = next(
        (
            c
            for c in children
            if (c.context or {}).get("direction_id")
            and (c.context or {}).get("research_task_id")
        ),
        None,
    )
    assert _dir_child is not None
    await refresh_direction_outcomes_after_merge(_dir_child, db_session)
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Both siblings counted — the recompute heals the whole cycle entry.
    assert entry["prs_merged"] == 2


@pytest.mark.asyncio
async def test_tasks_completed_no_pr_counts_only_genuine_no_pr(
    db_session: AsyncSession,
):
    """tasks_completed_no_pr counts a COMPLETED-no-PR child, excluding others.

    "No changes needed — work already exists" completions are a healthy outcome,
    not churn. The count must include a COMPLETED child that opened no PR and
    exclude: a merged completion, a pr_rejected completion, a COMPLETED child that
    DID open a PR (PULL_REQUEST artifact / pr_number signal), and any in-flight /
    failed child.
    """
    direction_id = "d-no-pr"
    project = await _create_project(db_session)
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=6)]
    db_session.add(project)
    await db_session.flush()

    # (1) COMPLETED, never opened a PR — the genuine no-PR completion to count.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="no-changes-needed",
    )
    # (2) Merged completion — excluded (it landed a PR).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        title="merged",
    )
    # (3) pr_rejected completion — excluded.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_rejected": True},
        title="rejected",
    )
    # (4) COMPLETED but DID open a PR (artifact present, no landed/rejected
    #     signal) — excluded: it produced a PR, so it is not a no-PR completion.
    opened_pr = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_number": 99},
        title="opened-pr-not-yet-landed",
    )
    db_session.add(
        Artifact(
            task_id=opened_pr.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/99",
        )
    )
    # (5) In-flight (IN_PROGRESS) — excluded (not COMPLETED).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={},
        title="in-flight",
    )
    # (6) FAILED — excluded (not COMPLETED).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={},
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Only child (1) qualifies.
    assert entry["tasks_completed_no_pr"] == 1
    # Sanity: the other completed children are still reflected in their buckets.
    assert entry["prs_merged"] == 1
    assert entry["prs_rejected"] == 1
    assert entry["tasks_completed"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# Generalized (mixed-portfolio) outcome aggregates — ADDITIVE to PR metrics.
# A direction whose cycle merged 0 PRs but produced successful research +
# audit + memo outcomes must show positive generalized health, while code-only
# portfolios keep identical merge metrics.
# ─────────────────────────────────────────────────────────────────────────────


async def _set_plan_output(session: AsyncSession, task: Task, plan_output) -> None:
    """Attach a plan_output payload to an already-flushed child task."""
    task.plan_output = plan_output
    session.add(task)
    await session.flush()


@pytest.mark.asyncio
async def test_generalized_health_non_code_portfolio(db_session: AsyncSession):
    """A 0-PR cycle of research/audit/memo successes shows positive health."""
    direction_id = "d-noncode"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=3)]
    await db_session.flush()

    # Successful research: a titled, graded finding.
    research_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        title="research",
    )
    await _set_plan_output(
        db_session,
        research_child,
        [{"title": "Investigate X", "actionability_grade": "HIGH"}],
    )
    # Successful audit: structured findings present.
    audit_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "security_audit"}],
        title="audit",
    )
    await _set_plan_output(
        db_session, audit_child, {"findings": [{"severity": "high"}]}
    )
    # Successful memo: a reasoning artifact mirrored onto plan_output.
    memo_child = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "memo"}],
        title="memo",
    )
    await _set_plan_output(db_session, memo_child, {"memo": "Reasoning text"})
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # Existing merge metrics are intact and honestly zero (no PR-merging task).
    assert entry["prs_merged"] == 0
    # Generalized signal: all three read-only successes counted.
    assert entry["generalized_successes"] == 3
    assert entry["generalized_health_rate"] == 1.0
    breakdown = entry["outcome_kind_breakdown"]
    assert breakdown["research_quality"]["success"] == 1
    assert breakdown["audit_findings"]["success"] == 1
    assert breakdown["memo_artifact"]["success"] == 1


@pytest.mark.asyncio
async def test_generalized_health_code_only_unchanged(db_session: AsyncSession):
    """Code-only portfolio: merge metrics AND generalized successes both equal
    the merged count (a code merge is a generalized success of kind code_merge)."""
    direction_id = "d-codeonly"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=2)]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "scope": "small"}],
        title="merged",
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "scope": "small"}],
        title="failed",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    assert entry["prs_merged"] == 1
    # One code merge → one generalized success of kind code_merge.
    assert entry["generalized_successes"] == 1
    assert entry["outcome_kind_breakdown"]["code_merge"]["success"] == 1
    assert entry["outcome_kind_breakdown"]["code_merge"]["failure"] == 1
    # 1 success / 2 terminal children.
    assert entry["generalized_health_rate"] == 0.5


@pytest.mark.asyncio
async def test_product_vs_structural_classification_registry_driven(
    db_session: AsyncSession,
):
    """product_ratio classifies child tasks via the task-type registry (PRODUCT_TASK_TYPES).
    create + fix + migrate count as product; refactor + research count as structural;
    an unknown/future type defaults to structural."""
    direction_id = "d-product-split"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    rtid = str(research_task.id)
    project.directions = [_make_direction(direction_id, rtid, tasks_created=6)]
    await db_session.flush()

    # Product types (3): create, fix, migrate.
    for ttype in ("create", "fix", "migrate"):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            payloads=[{"type": ttype, "scope": "small"}],
            title=f"product-{ttype}",
        )
    # Structural types (3): refactor (write but overhead), research (read-only),
    # and an unknown/future type that defaults to structural.
    for ttype in ("refactor", "research", "future_unknown_type"):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            payloads=[{"type": ttype, "scope": "small"}],
            title=f"structural-{ttype}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=rtid,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # 3 product / 6 total = 0.5
    assert entry["product_ratio"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# verified_prs_merged
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verified_prs_merged_excludes_degraded(db_session: AsyncSession):
    """verified_prs_merged counts only merges NOT falling open for a degraded reason."""
    direction_id = "d-verified-prs"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # 1. Verified merge (no unverified_merge_reason).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        title="verified-merge",
    )
    # 2. Benign unverified merge (project has no test suite — not penalised).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "review_loop": {"unverified_merge_reason": "no_detectable_test_command"},
        },
        title="benign-unverified",
    )
    # 3. Degraded unverified merge (env/setup failure blocked verification).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
        },
        title="degraded-unverified",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # All three PR merges count in the raw metric (back-compat).
    assert entry["prs_merged"] == 3
    # Only verified + benign count as verified merges (degraded excluded).
    assert entry["verified_prs_merged"] == 2


@pytest.mark.asyncio
async def test_verified_write_merged_excludes_malformed_acceptance(
    db_session: AsyncSession,
):
    """A malformed recorded verdict stays out of both verified slices.

    This keeps acceptance_verified_write_merged from outrunning verified_prs_merged
    and preserves the invariant guard for write merges with broken verdict shapes.
    """
    direction_id = "d-malformed-accept"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {"kind": "named_test"},
        },
        title="malformed-acceptance",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["prs_merged"] == 1
    assert entry["verified_prs_merged"] == 0
    assert entry["acceptance_verified_write_merged"] == 0
    assert acceptance_metric_invariant_violations(entry) == []


# ─────────────────────────────────────────────────────────────────────────────
# no_op_task_details — affirmative no-op (already-resolved / non-reproducing)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_task_details_populated(db_session: AsyncSession):
    """A COMPLETED child with a no_op_reason populates no_op_task_details."""
    direction_id = "d-noop"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"no_op_reason": "defect already resolved on HEAD"},
        title="stale-finding",
    )
    # A plain COMPLETED child with no no_op_reason must NOT appear.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="plain-complete",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["no_op_task_details"] == [
        {"title": "stale-finding", "no_op_reason": "defect already resolved on HEAD"}
    ]


@pytest.mark.asyncio
async def test_no_op_task_details_excludes_merged_and_failed(
    db_session: AsyncSession,
):
    """A merged-and-no-op'd child keeps its merge (no stale); failed is excluded."""
    direction_id = "d-noop-excl"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    # Merged completion that also carries a no_op_reason must NOT be tallied as
    # a no-op (a verified merge keeps its merged tag).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "no_op_reason": "should be ignored"},
        title="merged-finding",
    )
    # A FAILED child with a no_op_reason is not COMPLETED — excluded.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"no_op_reason": "irrelevant"},
        title="failed-finding",
        latest_status_message="boom",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["no_op_task_details"] == []


@pytest.mark.asyncio
async def test_no_op_task_details_capped_and_truncated(db_session: AsyncSession):
    """no_op_task_details caps at 5 entries and truncates reason to 120 chars."""
    direction_id = "d-noop-cap"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=7)
    ]
    await db_session.flush()

    long_reason = "Y" * 200
    for i in range(7):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context={"no_op_reason": long_reason},
            title=f"noop-{i}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["no_op_task_details"]) == 5
    assert all(len(d["no_op_reason"]) == 120 for d in entry["no_op_task_details"])


@pytest.mark.asyncio
async def test_proven_by_evidence_excludes_undeclared_and_soft_merges(
    db_session: AsyncSession,
):
    """The honest proven-by-evidence + declaration-coverage aggregates.

    Exercises the additive aggregation in ``_update_direction_research_outcomes``:
    only a machine-proven merge counts toward ``acceptance_proven_merged`` — either
    a satisfied ``named_test`` (machine-checked) or a satisfied ``downstream_artifact``
    that was consumed (recorded downstream-artifact consumption); an undeclared merge
    and a soft ``observable_behavior`` merge (satisfied by a shipped diff only) are
    EXCLUDED from proven. ``acceptance_declaration_coverage`` counts declared vs
    undeclared merged WRITE tasks. The existing ``verified_prs_merged`` values must
    stay unchanged (they still credit soft and undeclared merges) — proven is strictly
    additive, not a redefinition.
    """
    direction_id = "d-proven"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # (1) Undeclared merge: no acceptance verdict at all → declared=False,
    # proven=False. Still counts toward verified_prs_merged (back-compat).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="undeclared",
    )
    # (2) Soft merge: declared observable_behavior satisfied by a shipped diff
    # only → declared=True, proven=False. Counts as verified, NOT proven.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": False,
                "kind": "observable_behavior",
            },
        },
        title="soft-behavior",
    )
    # (3) Proven merge: machine-checked named_test → declared=True, proven=True.
    # One of two paths to proven; the other is a consumed downstream_artifact.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
            },
        },
        title="proven-named-test",
    )
    # (4) Declared but unsatisfied named_test → declared=True, proven=False;
    # also excluded from verified_prs_merged by the satisfied=False gate.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
            },
        },
        title="unsatisfied-named-test",
    )
    # (5) Non-WRITE merged task (review) carrying a proven verdict — must NOT
    # dilute or inflate the WRITE-only coverage/proven aggregates.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "scope": "small"}],
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
            },
        },
        title="review-not-write",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # Existing counts are UNCHANGED: all 5 merged; verified excludes only the
    # unsatisfied named_test (4).
    assert entry["prs_merged"] == 5
    assert entry["verified_prs_merged"] == 4

    # New additive proven-by-evidence signal: only WRITE proven merges count
    # (named_test or consumed downstream_artifact) — undeclared, soft behavior,
    # unsatisfied, and the non-WRITE review row are all excluded.
    assert entry["acceptance_proven_merged"] == 1
    # Declaration coverage over the 4 merged WRITE tasks: 3 declared
    # (behavior, proven named_test, unsatisfied named_test), 1 undeclared.
    assert entry["acceptance_declared_merged"] == 3
    assert entry["acceptance_declaration_coverage"] == round(3 / 4, 3)
    # The coverage denominator is persisted so lifetime/window coverage can be
    # recomputed from summed counts rather than averaging per-cycle fractions.
    assert entry["acceptance_merged_write_total"] == 4


@pytest.mark.asyncio
async def test_declaration_coverage_none_when_no_merged_write_tasks(
    db_session: AsyncSession,
):
    """Declaration coverage is None (no denominator) when no merged WRITE task
    exists, and proven/declared counts are 0 — never retro-failing legacy rows."""
    direction_id = "d-proven-empty"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=1)
    ]
    await db_session.flush()

    # A completed-but-unmerged write task: no landed signal, so not a merged row.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={},
        title="unmerged",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["acceptance_proven_merged"] == 0
    assert entry["acceptance_declared_merged"] == 0
    assert entry["acceptance_declaration_coverage"] is None
    assert entry["acceptance_merged_write_total"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Acceptance-evidence metric invariant guard
#
# A durable, non-gameable guard over the SEMANTIC relationships between the
# acceptance-evidence aggregates (acceptance_proven_merged /
# acceptance_declared_merged / acceptance_declaration_coverage /
# acceptance_merged_write_total alongside verified_prs_merged / prs_merged). It
# asserts the ordering + identity guarantees ALWAYS hold across a wide range of
# constructed merge mixes, so a future refactor of any definition cannot silently
# let proven drift above verified, or let a soft / undeclared / test-only /
# degraded merge be counted as proven, with nothing failing.
# ─────────────────────────────────────────────────────────────────────────────


# Each scenario is one merged child task. The flags record what the scenario
# SHOULD contribute to each aggregate, derived from the REAL gate semantics
# (acceptance_is_proven / acceptance_was_declared / merge_verification_status /
# WRITE_TASK_TYPES) — never a contradictory shape the real gate cannot emit
# (e.g. proven=True on a degraded merge, which the gate makes impossible because
# a degraded fall-open is not test-verified).
_ACCEPT_SCENARIOS: dict[str, dict] = {
    # Machine-checked proven named_test merge.
    "proven": {
        "context": {
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
            },
        },
        "type": "create",
        "write": True,
        "merged": True,
        "proven": True,
        "declared": True,
        "verified": True,
    },
    # Downstream artifact proven by a later consumer: consumed is a strict subset
    # of proven, and records the distinct acceptance_consumed_merged aggregate.
    "consumed": {
        "context": {
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": True,
                "consumed": True,
                "kind": "downstream_artifact",
            },
        },
        "type": "create",
        "write": True,
        "merged": True,
        "proven": True,
        "consumed": True,
        "declared": True,
        "verified": True,
    },
    # Satisfied-but-soft observable_behavior merge: declared + verified, not proven.
    "soft": {
        "context": {
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": True,
                "proven": False,
                "kind": "observable_behavior",
            },
        },
        "type": "fix",
        "write": True,
        "merged": True,
        "proven": False,
        "consumed": False,
        "declared": True,
        "verified": True,
    },
    # Declared but unsatisfied: declared, but blocks success → not verified, not proven.
    "declared_unsat": {
        "context": {
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
            },
        },
        "type": "refactor",
        "write": True,
        "merged": True,
        "proven": False,
        "consumed": False,
        "declared": True,
        "verified": False,
    },
    # Undeclared / legacy write merge: verified, never declared, never proven.
    "undeclared": {
        "context": {"pr_merged": True},
        "type": "create",
        "write": True,
        "merged": True,
        "proven": False,
        "consumed": False,
        "declared": False,
        "verified": True,
    },
    # Degraded-verification write merge: not verified, not declared, not proven.
    "degraded": {
        "context": {
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
        },
        "type": "create",
        "write": True,
        "merged": True,
        "proven": False,
        "consumed": False,
        "declared": False,
        "verified": False,
    },
    # Read-only (non-write) merge: counts toward prs_merged/verified but is OUTSIDE
    # the merged-write denominator, so it can never raise declared/proven.
    "readonly": {
        "context": {"pr_merged": True},
        "type": "research",
        "write": False,
        "merged": True,
        "proven": False,
        "consumed": False,
        "declared": False,
        "verified": True,
    },
}


def _expected_acceptance_aggregates(mix: list[str]) -> dict:
    """Compute the aggregate values a mix of scenarios SHOULD produce."""
    rows = [_ACCEPT_SCENARIOS[name] for name in mix]
    merged = [r for r in rows if r["merged"]]
    merged_write = [r for r in merged if r["write"]]
    declared = sum(1 for r in merged_write if r["declared"])
    proven = sum(1 for r in merged_write if r["proven"])
    consumed = sum(1 for r in merged_write if r.get("consumed"))
    verified_write = sum(1 for r in merged_write if r["verified"])
    verified = sum(1 for r in merged if r["verified"])
    prs_merged = len(merged)
    mw = len(merged_write)
    return {
        "acceptance_proven_merged": proven,
        "acceptance_consumed_merged": consumed,
        "acceptance_declared_merged": declared,
        "acceptance_verified_write_merged": verified_write,
        "acceptance_merged_write_total": mw,
        "verified_prs_merged": verified,
        "prs_merged": prs_merged,
        "acceptance_declaration_coverage": (
            round(declared / mw, 3) if mw > 0 else None
        ),
    }


async def _run_mix(db_session: AsyncSession, direction_id: str, mix: list[str]) -> dict:
    """Create one merged child per scenario, run the aggregator, return the entry."""
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=max(len(mix), 1))
    ]
    await db_session.flush()

    for i, name in enumerate(mix):
        scenario = _ACCEPT_SCENARIOS[name]
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context=dict(scenario["context"]),
            payloads=[{"type": scenario["type"], "scope": "small"}],
            title=f"{name}-{i}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    return project.directions[0]["research_history"][0]


@pytest.mark.parametrize(
    "mix",
    [
        # All distinct scenario kinds together.
        [
            "proven",
            "consumed",
            "soft",
            "declared_unsat",
            "undeclared",
            "degraded",
            "readonly",
        ],
        # Proven-heavy.
        ["proven", "consumed", "proven", "soft"],
        # No proven at all (soft + undeclared + degraded).
        ["soft", "undeclared", "degraded"],
        # Only read-only merges → no merged-write denominator.
        ["readonly", "readonly"],
        # Only declared-unsatisfied write merges.
        ["declared_unsat", "declared_unsat"],
        # Duplicated full mix to exercise larger counts.
        [
            "proven",
            "proven",
            "soft",
            "soft",
            "declared_unsat",
            "undeclared",
            "degraded",
            "readonly",
            "readonly",
        ],
    ],
)
@pytest.mark.asyncio
async def test_acceptance_metric_invariants_hold_across_mixes(
    db_session: AsyncSession, mix: list[str]
):
    """The acceptance-evidence metric invariants ALWAYS hold, and the aggregates
    match the values derived independently from the real gate semantics.

    Drives the production code under test end-to-end: builds the child-task mix,
    runs ``_update_direction_research_outcomes`` to produce the real aggregates,
    and asserts the production guard ``acceptance_metric_invariant_violations``
    finds NO violation — proven ⊆ declared ⊆ merged-write, proven ⊆ verified ⊆
    merged, and the declaration-coverage identity. Also pins each aggregate to its
    independently-computed expected value so the guard cannot pass on
    always-true / degenerate metrics.
    """
    entry = await _run_mix(db_session, "d-accept-inv", mix)

    # The production invariant guard finds no violation on real aggregates.
    violations = acceptance_metric_invariant_violations(entry)
    assert violations == [], violations
    assert acceptance_metrics_consistent(entry) is True

    # And the aggregates equal the values derived from the real gate semantics —
    # the ordering holds because the values are genuinely what the mix implies,
    # not because the metrics collapsed to zero.
    expected = _expected_acceptance_aggregates(mix)
    for key, value in expected.items():
        assert entry[key] == value, (key, entry.get(key), value)

    # Spell out the ordering chain explicitly on the real values.
    assert (
        0
        <= entry["acceptance_proven_merged"]
        <= entry["acceptance_declared_merged"]
        <= entry["acceptance_merged_write_total"]
    )
    assert (
        0
        <= entry["acceptance_verified_write_merged"]
        <= entry["acceptance_merged_write_total"]
    )
    assert (
        0
        <= entry["acceptance_proven_merged"]
        <= entry["verified_prs_merged"]
        <= entry["prs_merged"]
    )


@pytest.mark.asyncio
async def test_declaration_coverage_identity_and_readonly_excluded(
    db_session: AsyncSession,
):
    """Coverage == declared / merged-write, and read-only children are excluded
    from the merged-write denominator entirely (they never raise declared or
    proven, even though they DO count toward prs_merged/verified)."""
    # 2 declared write merges (1 proven, 1 soft) + 2 read-only merges.
    entry = await _run_mix(
        db_session,
        "d-accept-cov",
        ["proven", "soft", "readonly", "readonly"],
    )
    # Read-only merges do NOT enter the merged-write denominator.
    assert entry["acceptance_merged_write_total"] == 2
    assert entry["acceptance_verified_write_merged"] == 2
    assert entry["acceptance_declared_merged"] == 2
    assert entry["acceptance_proven_merged"] == 1
    # ...but they DO count as merged PRs.
    assert entry["prs_merged"] == 4
    assert entry["verified_prs_merged"] == 4
    # Coverage is taken over merged-write only: 2/2 == 1.0, not 2/4.
    assert entry["acceptance_declaration_coverage"] == 1.0
    assert acceptance_metric_invariant_violations(entry) == []
    from app.direction_metrics import proof_weighted_merged

    assert proof_weighted_merged(entry) == 3.7


def test_acceptance_metric_invariant_violations_detects_drift():
    """The guard is non-vacuous: it flags every relationship it promises to lock.

    Pure-dict unit test (no DB) that feeds the production guard deliberately
    inconsistent entries — exactly the regressions a future refactor could
    introduce — and asserts each is caught. A guard that returned ``[]`` here
    would be an always-true assertion; this is what makes it a real invariant.
    """
    # A clean, consistent entry has no violations.
    clean = {
        "acceptance_proven_merged": 1,
        "acceptance_consumed_merged": 1,
        "acceptance_declared_merged": 2,
        "acceptance_verified_write_merged": 2,
        "acceptance_merged_write_total": 3,
        "verified_prs_merged": 2,
        "prs_merged": 4,
        "acceptance_declaration_coverage": round(2 / 3, 3),
    }
    assert acceptance_metric_invariant_violations(clean) == []
    assert acceptance_metrics_consistent(clean) is True

    # proven drifts above declared.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_proven_merged": 3}
    )
    # consumed drifts above proven.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_consumed_merged": 2, "acceptance_proven_merged": 1}
    )
    # proven drifts above verified.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_proven_merged": 1, "verified_prs_merged": 0}
    )
    # verified drifts above prs_merged.
    assert acceptance_metric_invariant_violations({**clean, "verified_prs_merged": 5})
    # declared drifts above merged-write.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_declared_merged": 4}
    )
    # verified-write drifts above merged-write.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_verified_write_merged": 4}
    )
    # merged-write drifts above prs_merged.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_merged_write_total": 5}
    )
    # coverage no longer equals declared / merged-write.
    assert acceptance_metric_invariant_violations(
        {**clean, "acceptance_declaration_coverage": 0.999}
    )
    # coverage is non-None despite a zero merged-write denominator.
    assert acceptance_metric_invariant_violations(
        {
            "acceptance_proven_merged": 0,
            "acceptance_declared_merged": 0,
            "acceptance_merged_write_total": 0,
            "verified_prs_merged": 0,
            "prs_merged": 0,
            "acceptance_declaration_coverage": 0.5,
        }
    )


def test_acceptance_metric_invariants_fail_safe_on_legacy_entries():
    """Legacy entries written before a field existed never spuriously violate."""
    # No acceptance fields at all (pre-acceptance history).
    assert acceptance_metric_invariant_violations({"prs_merged": 5}) == []
    # Pre-denominator entry: coverage present but no merged-write count → the
    # identity check is skipped; only the [0,1] sanity bound applies.
    assert (
        acceptance_metric_invariant_violations(
            {
                "acceptance_proven_merged": 1,
                "acceptance_declared_merged": 2,
                "verified_prs_merged": 3,
                "prs_merged": 4,
                "acceptance_declaration_coverage": 0.5,
            }
        )
        == []
    )
    # A pre-denominator entry with an out-of-range coverage is still caught.
    assert acceptance_metric_invariant_violations(
        {"prs_merged": 4, "acceptance_declaration_coverage": 1.5}
    )
    # Completely empty entry is vacuously consistent.
    assert acceptance_metric_invariant_violations({}) == []


def test_stranded_reachable_metrics_allow_persisted_discounts():
    """Stranded credit survives later recomputes without tripping the guard.

    A direction can legitimately end up with stranded_prs_merged still present
    after a later refresh has recomputed prs_merged to exclude the stranded task.
    """
    valid = {
        "prs_merged": 1,
        "reachable_prs_merged": 1,
        "stranded_prs_merged": 1,
    }
    assert acceptance_metric_invariant_violations(valid) == []

    legacy = {"prs_merged": 4}
    assert acceptance_metric_invariant_violations(legacy) == []

    stranded_negative = {**valid, "stranded_prs_merged": -1}
    assert any(
        "stranded (-1) is negative" == violation
        for violation in acceptance_metric_invariant_violations(stranded_negative)
    )

    reachable_too_high = {**valid, "reachable_prs_merged": 3}
    assert any(
        "reachable (3) exceeds prs_merged (1)" == violation
        for violation in acceptance_metric_invariant_violations(reachable_too_high)
    )


@pytest.mark.asyncio
async def test_unproven_merge_surfaced_to_research_feedback(
    db_session: AsyncSession,
):
    """A merged WRITE task whose declared acceptance evidence was UNSATISFIED is
    captured in ``unproven_merge_details`` (title + truncated verdict reason) so
    the next research cycle learns the fix shipped but its claim did not hold.

    Such a delivered-but-unproven merge is neither a failure nor a no-op, so it
    must NOT appear in ``failed_task_details`` or ``no_op_task_details``. A
    satisfied merge and an undeclared (legacy) merge are excluded from the new
    list, and the list is capped at 5 like its siblings.
    """
    direction_id = "d-unproven-feedback"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=9)
    ]
    await db_session.flush()

    # The delivered-but-unproven merge: declared acceptance evidence, NOT
    # satisfied. Carries a verdict reason that should be surfaced verbatim
    # (truncated to 120 chars).
    long_reason = "named_test did not pass under the real merge verdict; " + ("x" * 200)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {
                "satisfied": False,
                "reason": long_reason,
            },
        },
        title="unproven-merge",
    )
    # A satisfied merge — proven, must NOT be in unproven list.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "code_acceptance_verdict": {"satisfied": True},
        },
        title="proven-merge",
    )
    # An undeclared (legacy) merge — no verdict key, must NOT be in unproven list.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        title="undeclared-merge",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    details = entry["unproven_merge_details"]
    assert len(details) == 1
    only = details[0]
    assert only["title"] == "unproven-merge"
    # Reason is captured and truncated to 120 chars.
    assert only["reason"] == long_reason[:120]
    assert len(only["reason"]) == 120

    # It is neither a failure nor a no-op.
    unproven_titles = {d["title"] for d in details}
    failed_titles = {d["title"] for d in entry.get("failed_task_details", [])}
    no_op_titles = {d["title"] for d in entry.get("no_op_task_details", [])}
    assert unproven_titles.isdisjoint(failed_titles)
    assert unproven_titles.isdisjoint(no_op_titles)
    assert "unproven-merge" not in failed_titles
    assert "unproven-merge" not in no_op_titles


@pytest.mark.asyncio
async def test_unproven_merge_details_capped_at_five(
    db_session: AsyncSession,
):
    """The unproven_merge_details list is capped at 5 entries like its siblings."""
    direction_id = "d-unproven-cap"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=7)
    ]
    await db_session.flush()

    for i in range(7):
        await _create_child_task(
            db_session,
            research_task.id,
            project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                "code_acceptance_verdict": {
                    "satisfied": False,
                    "reason": f"evidence {i} unsatisfied",
                },
            },
            title=f"unproven-{i}",
        )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert len(entry["unproven_merge_details"]) == 5


@pytest.mark.asyncio
async def test_per_direction_format_metrics_have_a_consumer_or_are_retired(
    db_session: AsyncSession,
):
    """format_compliance_rate and self_repair_success_rate are emitted with named consumers.

    Exercises _update_direction_research_outcomes to verify these fields are
    computed from child-task format_compliance_summary telemetry and stored in
    the research_history entry. The manifest entries must have a named consumer
    (not pending), confirming d-e0800012's requirement that every decision-critical
    metric has a named reader.
    """
    from app.direction_outcomes import RESEARCH_HISTORY_METRIC_MANIFEST

    # Verify manifest entries have been promoted from pending to consumer.
    fc_entry = RESEARCH_HISTORY_METRIC_MANIFEST["format_compliance_rate"]
    sr_entry = RESEARCH_HISTORY_METRIC_MANIFEST["self_repair_success_rate"]
    sg_entry = RESEARCH_HISTORY_METRIC_MANIFEST[
        "research_spawn_gate_wholesale_retry_exhausted"
    ]
    assert "consumer" in fc_entry, (
        "format_compliance_rate must have a named consumer in RESEARCH_HISTORY_METRIC_MANIFEST"
    )
    assert "consumer" in sr_entry, (
        "self_repair_success_rate must have a named consumer in RESEARCH_HISTORY_METRIC_MANIFEST"
    )
    assert "consumer" in sg_entry, (
        "research_spawn_gate_wholesale_retry_exhausted must have a named consumer in RESEARCH_HISTORY_METRIC_MANIFEST"
    )

    # Functional check: verify the fields are correctly computed and stored.
    direction_id = "d-format-consumer-check"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Well-formed task: format_error_total == 0.
    well_formed = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="well-formed",
    )
    well_formed.format_compliance_summary = {
        "format_error_total": 0,
        "format_repair_attempts": 0,
        "format_repair_successes": 0,
    }

    # Malformed task: had one error, attempted repair (succeeded).
    malformed = await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="malformed-repaired",
    )
    malformed.format_compliance_summary = {
        "format_error_total": 1,
        "format_repair_attempts": 1,
        "format_repair_successes": 1,
    }

    # Task without telemetry — must be excluded from measurement.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="no-telemetry",
    )

    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # 2 measured tasks: 1 well-formed → 50% compliance rate.
    assert entry["format_compliance_rate"] == 0.5
    # 1 repair attempt, 1 success → 100% repair success rate.
    assert entry["self_repair_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_spawn_gate_counts_and_rates_are_recorded(db_session: AsyncSession):
    """research_history records challenged/refuted/survived counts and merge rates."""
    direction_id = "d-spawn-gate"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 2,
        "research_spawn_gate_refuted_count": 1,
        "research_spawn_gate_survived_count": 1,
    }

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="challenged-merged",
        context={
            "direction_id": direction_id,
            "research_spawn_gate_challenged": True,
            "research_spawn_gate_verdict": "pass",
            "pr_merged": True,
            "landed_on_integration": True,
        },
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="unchallenged-open",
        context={
            "direction_id": direction_id,
            "research_spawn_gate_challenged": False,
            "research_spawn_gate_verdict": None,
        },
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["research_spawn_gate_challenged_count"] == 2
    assert entry["research_spawn_gate_refuted_count"] == 1
    assert entry["research_spawn_gate_survived_count"] == 1
    assert entry["research_spawn_gate_challenged_spawned"] == 1
    assert entry["research_spawn_gate_challenged_merged"] == 1
    assert entry["research_spawn_gate_challenged_merge_rate"] == 1.0
    assert entry["research_spawn_gate_unchallenged_spawned"] == 1
    assert entry["research_spawn_gate_unchallenged_merged"] == 0
    assert entry["research_spawn_gate_unchallenged_merge_rate"] == 0.0


@pytest.mark.asyncio
async def test_spawn_gate_breakdown_and_reasons_roll_up(db_session: AsyncSession):
    """Decision-quality breakdown counters and reasons sample roll into history.

    The new explicit_pass/fail_open/unresolved counters and the compact
    refuted/unresolved reasons sample produced on the research task context are
    copied into the per-direction research_history entry, alongside (not
    replacing) the existing challenged/refuted/survived counters and the
    challenged/unchallenged spawned+merged denominators.
    """
    direction_id = "d-spawn-gate-breakdown"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 4,
        "research_spawn_gate_refuted_count": 2,
        "research_spawn_gate_survived_count": 2,
        "research_spawn_gate_explicit_pass_count": 1,
        "research_spawn_gate_fail_open_count": 1,
        "research_spawn_gate_unresolved_count": 1,
        "research_spawn_gate_parse_outcome": "no_hash_matches",
        "research_spawn_gate_manifest_excluded_fail_closed_count": 1,
        "research_spawn_gate_manifest_excluded_fail_closed_samples": [
            {
                "hash": "excluded-hash",
                "title": "Budget-excluded finding",
                "reason": "fail-closed: high-stakes finding was not admitted to the adversarial manifest",
            }
        ],
        "research_spawn_gate_refuted_reasons": [
            "stale premise",
            "no_hash_matches",
            # Duplicate is deduped by the producer; assert resilience anyway.
            "stale premise",
        ],
    }

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="challenged-merged",
        context={
            "direction_id": direction_id,
            "research_spawn_gate_challenged": True,
            "research_spawn_gate_verdict": "pass",
            "pr_merged": True,
            "landed_on_integration": True,
        },
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="unchallenged-open",
        context={
            "direction_id": direction_id,
            "research_spawn_gate_challenged": False,
            "research_spawn_gate_verdict": None,
        },
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Legacy inclusive counters preserved.
    assert entry["research_spawn_gate_challenged_count"] == 4
    assert entry["research_spawn_gate_refuted_count"] == 2
    assert entry["research_spawn_gate_survived_count"] == 2
    # New decision-quality breakdown rolled up.
    assert entry["research_spawn_gate_explicit_pass_count"] == 1
    assert entry["research_spawn_gate_fail_open_count"] == 1
    assert entry["research_spawn_gate_unresolved_count"] == 1
    assert entry["research_spawn_gate_parse_outcome"] == "no_hash_matches"
    assert entry["research_spawn_gate_manifest_excluded_fail_closed_count"] == 1
    assert entry["research_spawn_gate_manifest_excluded_fail_closed_samples"] == [
        {
            "hash": "excluded-hash",
            "title": "Budget-excluded finding",
            "reason": "fail-closed: high-stakes finding was not admitted to the adversarial manifest",
        }
    ]
    # Denominators / sample sizes preserved (merge rates travel with them).
    assert entry["research_spawn_gate_challenged_spawned"] == 1
    assert entry["research_spawn_gate_challenged_merged"] == 1
    assert entry["research_spawn_gate_challenged_merge_rate"] == 1.0
    assert entry["research_spawn_gate_unchallenged_spawned"] == 1
    assert entry["research_spawn_gate_unchallenged_merged"] == 0
    assert entry["research_spawn_gate_unchallenged_merge_rate"] == 0.0
    # Compact reasons sample rolled up, deduped, and capped.
    reasons = entry["research_spawn_gate_refuted_reasons"]
    assert "stale premise" in reasons
    assert "no_hash_matches" in reasons
    assert len(reasons) == len(set(reasons)), "reasons sample must be deduped"
    assert len(reasons) <= 5


@pytest.mark.asyncio
async def test_spawn_gate_cause_flows_from_research_ctx_to_history(
    db_session: AsyncSession,
):
    """research_spawn_gate_cause and cause_counts are copied from research task ctx into history.

    When the research task context carries research_spawn_gate_cause and
    research_spawn_gate_cause_counts, both must appear in the research_history entry.
    When they are absent, the keys must not be present in the entry.
    """
    direction_id = "d-spawn-gate-cause"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 2,
        "research_spawn_gate_unresolved_count": 2,
        "research_spawn_gate_cause": {
            "category": "AGENT_TIMEOUT",
            "detail": "timed out",
        },
        "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 2},
    }

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=0)
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["research_spawn_gate_cause"] == {
        "category": "AGENT_TIMEOUT",
        "detail": "timed out",
    }
    assert entry["research_spawn_gate_cause_counts"] == {"AGENT_TIMEOUT": 2}

    # Verify that absent cause keys do not appear in the entry.
    direction_id2 = "d-spawn-gate-cause-absent"
    research_task2 = await _make_research_task(db_session, project.id)
    research_task2_id = str(research_task2.id)
    research_task2.context = {
        "direction_id": direction_id2,
        "research_spawn_gate_challenged_count": 1,
    }
    project.directions = project.directions + [
        _make_direction(direction_id2, research_task2_id, tasks_created=0)
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id2,
        research_task_id=research_task2_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry2 = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_id2
    )
    assert "research_spawn_gate_cause" not in entry2
    assert "research_spawn_gate_cause_counts" not in entry2


@pytest.mark.asyncio
async def test_adversarial_give_up_count_rolls_up_to_spawn_gate_metrics(
    db_session: AsyncSession,
):
    """Adversarial give-up events from finding_structural_give_up roll into history.

    When a research task's context carries a finding_structural_give_up dict with
    by_reason.adversarial_gate > 0, the count must appear in the research_history
    entry as research_spawn_gate_adversarial_give_up_count and the adversarial-gate
    filtered sample must appear as research_spawn_gate_adversarial_give_up_samples.
    Non-adversarial-gate samples must NOT be included in the samples list.
    """
    direction_id = "d-adversarial-give-up"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 0,
        "research_spawn_gate_refuted_count": 0,
        "research_spawn_gate_survived_count": 0,
        "research_spawn_gate_explicit_pass_count": 0,
        "research_spawn_gate_fail_open_count": 0,
        "research_spawn_gate_unresolved_count": 0,
        "finding_structural_give_up": {
            "at": "2026-06-10T00:00:00",
            "total": 3,
            "by_reason": {
                "adversarial_gate": 2,
                "manifest_excluded": 1,
            },
            "by_direction": {direction_id: 3},
            "sample": [
                {
                    "title": "Adversarial-killed finding A",
                    "reason": "adversarial_gate",
                    "direction_id": direction_id,
                },
                {
                    "title": "Adversarial-killed finding B",
                    "reason": "adversarial_gate",
                    "direction_id": direction_id,
                },
                {
                    "title": "Manifest-excluded finding C",
                    "reason": "manifest_excluded",
                    "direction_id": direction_id,
                },
            ],
        },
    }

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=2)
    ]
    await db_session.flush()

    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="surviving-task",
        context={"direction_id": direction_id},
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # Adversarial give-up count reflects only the adversarial_gate bucket.
    assert entry["research_spawn_gate_adversarial_give_up_count"] == 2

    # Samples contain only adversarial-gate entries (manifest_excluded is filtered out).
    samples = entry.get("research_spawn_gate_adversarial_give_up_samples", [])
    assert len(samples) == 2
    titles = [s["title"] for s in samples]
    assert "Adversarial-killed finding A" in titles
    assert "Adversarial-killed finding B" in titles
    assert "Manifest-excluded finding C" not in titles

    # Give-up count is a separate bucket — legacy refuted/survived counters unchanged.
    assert entry["research_spawn_gate_refuted_count"] == 0
    assert entry["research_spawn_gate_survived_count"] == 0


@pytest.mark.asyncio
async def test_spawn_gate_scoped_to_owning_direction(db_session: AsyncSession):
    """Per-direction gate data is not smeared across sibling directions.

    When research_spawn_gate_by_direction is present, direction B's history entry
    must only carry gate counters from B's own per-direction entry, not the totals
    that belong to direction A.
    """
    direction_a = "d-sg-dir-a"
    direction_b = "d-sg-dir-b"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    # Direction A had 3 challenges; direction B had none.
    research_task.context = {
        "direction_id": direction_a,
        "research_spawn_gate_challenged_count": 3,
        "research_spawn_gate_refuted_count": 2,
        "research_spawn_gate_survived_count": 1,
        "research_spawn_gate_wholesale_retry_exhausted": True,
        "research_spawn_gate_by_direction": {
            direction_a: {
                "challenged_count": 3,
                "refuted_count": 2,
                "survived_count": 1,
                "explicit_pass_count": 0,
                "fail_open_count": 1,
                "unresolved_count": 1,
                "wholesale_retry_exhausted": True,
            },
            direction_b: {},
        },
    }
    project.directions = [
        _make_direction(direction_a, research_task_id, tasks_created=1),
        _make_direction(direction_b, research_task_id, tasks_created=0),
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_b,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry_b = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_b
    )
    # Direction B's entry must reflect B's own (zero) gate activity, not A's.
    assert entry_b["research_spawn_gate_challenged_count"] == 0
    assert entry_b["research_spawn_gate_refuted_count"] == 0
    assert entry_b["research_spawn_gate_survived_count"] == 0
    assert entry_b["research_spawn_gate_wholesale_retry_exhausted"] is False


@pytest.mark.asyncio
async def test_spawn_gate_missing_direction_entry_stays_zero(
    db_session: AsyncSession,
):
    """A missing per-direction entry must not fall back to sibling globals."""
    direction_a = "d-sg-dir-a"
    direction_b = "d-sg-dir-b"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_a,
        "research_spawn_gate_challenged_count": 3,
        "research_spawn_gate_refuted_count": 2,
        "research_spawn_gate_survived_count": 1,
        "research_spawn_gate_wholesale_retry_exhausted": True,
        "research_spawn_gate_by_direction": {
            direction_a: {
                "challenged_count": 3,
                "refuted_count": 2,
                "survived_count": 1,
                "explicit_pass_count": 0,
                "fail_open_count": 1,
                "unresolved_count": 1,
                "wholesale_retry_exhausted": True,
            }
        },
    }
    project.directions = [
        _make_direction(direction_a, research_task_id, tasks_created=1),
        _make_direction(direction_b, research_task_id, tasks_created=0),
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_b,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry_b = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_b
    )
    assert entry_b["research_spawn_gate_challenged_count"] == 0
    assert entry_b["research_spawn_gate_refuted_count"] == 0
    assert entry_b["research_spawn_gate_survived_count"] == 0
    assert entry_b["research_spawn_gate_explicit_pass_count"] == 0
    assert entry_b["research_spawn_gate_fail_open_count"] == 0
    assert entry_b["research_spawn_gate_unresolved_count"] == 0
    assert entry_b["research_spawn_gate_wholesale_retry_exhausted"] is False


@pytest.mark.asyncio
async def test_spawn_gate_exhausted_flag_cleared_on_healthy_pass(
    db_session: AsyncSession,
):
    """Per-direction wholesale_retry_exhausted overrides the sticky global flag.

    The global context flag is set on exhaustion and never cleared, so a later
    healthy-gate cycle still reports exhausted=True globally.  When the per-direction
    entry says False, the direction's history entry must record False.
    """
    direction_id = "d-sg-exhausted-cleared"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    # Global flag is still True (from an earlier cycle), but this direction's
    # per-direction entry reflects a healthy pass.
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_wholesale_retry_exhausted": True,
        "research_spawn_gate_parse_outcome": "ok",
        "research_spawn_gate_by_direction": {
            direction_id: {
                "challenged_count": 2,
                "refuted_count": 0,
                "survived_count": 2,
                "explicit_pass_count": 2,
                "fail_open_count": 0,
                "unresolved_count": 0,
                "wholesale_retry_exhausted": False,
                "parse_outcome": "ok",
            },
        },
    }
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=0)
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    assert entry["research_spawn_gate_wholesale_retry_exhausted"] is False
    assert entry["research_spawn_gate_challenged_count"] == 2
    assert entry["research_spawn_gate_survived_count"] == 2


@pytest.mark.asyncio
async def test_spawn_gate_challenged_spawned_filtered_by_direction(
    db_session: AsyncSession,
):
    """challenged_spawned / unchallenged_spawned are scoped to the owning direction.

    Two directions share a research task; each has child tasks tagged with their own
    direction_id. The challenged_spawned and unchallenged_spawned counts for each
    direction must only count that direction's children, not the combined total.
    """
    direction_a = "d-sg-spawn-a"
    direction_b = "d-sg-spawn-b"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    research_task.context = {
        "direction_id": direction_a,
        "research_spawn_gate_by_direction": {
            direction_a: {
                "challenged_count": 2,
                "refuted_count": 0,
                "survived_count": 2,
                "explicit_pass_count": 2,
                "fail_open_count": 0,
                "unresolved_count": 0,
                "wholesale_retry_exhausted": False,
            },
            direction_b: {
                "challenged_count": 1,
                "refuted_count": 1,
                "survived_count": 0,
                "explicit_pass_count": 0,
                "fail_open_count": 0,
                "unresolved_count": 1,
                "wholesale_retry_exhausted": False,
            },
        },
    }
    project.directions = [
        _make_direction(direction_a, research_task_id, tasks_created=3),
        _make_direction(direction_b, research_task_id, tasks_created=2),
    ]
    await db_session.flush()

    # Direction A: 2 challenged children, 1 unchallenged child.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="a-challenged-1",
        context={
            "direction_id": direction_a,
            "research_spawn_gate_challenged": True,
            "pr_merged": True,
            "landed_on_integration": True,
        },
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="a-challenged-2",
        context={"direction_id": direction_a, "research_spawn_gate_challenged": True},
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="a-unchallenged",
        context={"direction_id": direction_a, "research_spawn_gate_challenged": False},
    )
    # Direction B: 1 challenged child, 1 unchallenged child.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="b-challenged",
        context={"direction_id": direction_b, "research_spawn_gate_challenged": True},
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="b-unchallenged",
        context={"direction_id": direction_b, "research_spawn_gate_challenged": False},
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_a,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry_a = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_a
    )
    # Direction A should only count its own children.
    assert entry_a["research_spawn_gate_challenged_spawned"] == 2
    assert entry_a["research_spawn_gate_unchallenged_spawned"] == 1

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_b,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry_b = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_b
    )
    # Direction B should only count its own children.
    assert entry_b["research_spawn_gate_challenged_spawned"] == 1
    assert entry_b["research_spawn_gate_unchallenged_spawned"] == 1


@pytest.mark.asyncio
async def test_spawn_gate_fallback_when_no_by_direction_key(db_session: AsyncSession):
    """Global spawn-gate counters are used when research_spawn_gate_by_direction is absent.

    This covers pre-fix cycles (where the per-direction dict was never written) and
    standalone research tasks that serve only one direction.  The global fallback must
    preserve the existing behavior so these older history entries remain valid.
    """
    direction_id = "d-sg-fallback"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    # No research_spawn_gate_by_direction key — only the global counters.
    research_task.context = {
        "direction_id": direction_id,
        "research_spawn_gate_challenged_count": 4,
        "research_spawn_gate_refuted_count": 1,
        "research_spawn_gate_survived_count": 3,
        "research_spawn_gate_explicit_pass_count": 2,
        "research_spawn_gate_fail_open_count": 1,
        "research_spawn_gate_unresolved_count": 0,
        "research_spawn_gate_wholesale_retry_exhausted": False,
        "research_spawn_gate_refuted_reasons": ["plausible but unverified"],
    }
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=0)
    ]
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    # Global counters must be forwarded unchanged via the fallback path.
    assert entry["research_spawn_gate_challenged_count"] == 4
    assert entry["research_spawn_gate_refuted_count"] == 1
    assert entry["research_spawn_gate_survived_count"] == 3
    assert entry["research_spawn_gate_explicit_pass_count"] == 2
    assert entry["research_spawn_gate_fail_open_count"] == 1
    assert entry["research_spawn_gate_unresolved_count"] == 0
    assert entry["research_spawn_gate_wholesale_retry_exhausted"] is False
    assert entry["research_spawn_gate_refuted_reasons"] == ["plausible but unverified"]


# ─────────────────────────────────────────────────────────────────────────────
# Multi-direction cycle outcome scoping
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_direction_cycle_outcomes_scoped_to_own_direction(
    db_session: AsyncSession,
):
    """Per-direction outcome stats must not smear across sibling directions.

    Two directions share a single research task. Direction A has 2 merged children
    and 1 failed child; direction B has 1 merged child and 1 cancelled child. After
    running the updater for each direction, each entry must reflect only its own
    children for prs_merged, tasks_completed, tasks_failed, tasks_cancelled,
    type_breakdown, and scope_breakdown.
    """
    direction_a = "d-scope-a"
    direction_b = "d-scope-b"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_a, research_task_id, tasks_created=3),
        _make_direction(direction_b, research_task_id, tasks_created=2),
    ]
    await db_session.flush()

    # Direction A children: 2 merged, 1 failed
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="a-merged-1",
        context={
            "direction_id": direction_a,
            "pr_merged": True,
            "landed_on_integration": True,
            "scope": "small",
        },
        payloads=[{"type": "fix", "scope": "small"}],
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="a-merged-2",
        context={
            "direction_id": direction_a,
            "pr_merged": True,
            "landed_on_integration": True,
            "scope": "medium",
        },
        payloads=[{"type": "create", "scope": "medium"}],
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        title="a-failed-1",
        context={"direction_id": direction_a, "scope": "small"},
        payloads=[{"type": "fix", "scope": "small"}],
    )
    # Direction B children: 1 merged, 1 cancelled
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="b-merged-1",
        context={
            "direction_id": direction_b,
            "pr_merged": True,
            "landed_on_integration": True,
            "scope": "large",
        },
        payloads=[{"type": "fix", "scope": "large"}],
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.CANCELLED,
        title="b-cancelled-1",
        context={"direction_id": direction_b, "scope": "large"},
        payloads=[{"type": "fix", "scope": "large"}],
    )
    await db_session.flush()

    # Run updater for direction A
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_a,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    entry_a = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_a
    )

    # Direction A: 3 children (2 completed, 1 failed), 2 merged
    assert entry_a["prs_merged"] == 2, (
        f"direction A prs_merged: {entry_a['prs_merged']}"
    )
    assert entry_a["tasks_completed"] == 2
    assert entry_a["tasks_failed"] == 1
    assert entry_a["tasks_cancelled"] == 0

    # type_breakdown must only reflect direction A task types (fix x2, create x1)
    tb_a = entry_a.get("type_breakdown", {})
    assert "fix" in tb_a
    assert tb_a["fix"]["created"] == 2
    assert "create" in tb_a
    assert tb_a["create"]["created"] == 1
    # Direction B's "large" scope children must NOT appear in direction A
    sb_a = entry_a.get("scope_breakdown", {})
    assert sb_a.get("large", {}).get("created", 0) == 0

    # Run updater for direction B
    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_b,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    entry_b = next(
        d["research_history"][0] for d in project.directions if d["id"] == direction_b
    )

    # Direction B: 2 children (1 completed, 0 failed, 1 cancelled), 1 merged
    assert entry_b["prs_merged"] == 1, (
        f"direction B prs_merged: {entry_b['prs_merged']}"
    )
    assert entry_b["tasks_completed"] == 1
    assert entry_b["tasks_failed"] == 0
    assert entry_b["tasks_cancelled"] == 1

    # Direction A's "small"/"medium" scope children must NOT appear in direction B
    sb_b = entry_b.get("scope_breakdown", {})
    assert sb_b.get("small", {}).get("created", 0) == 0
    assert sb_b.get("medium", {}).get("created", 0) == 0
    assert sb_b.get("large", {}).get("created", 0) == 2


@pytest.mark.asyncio
async def test_multi_direction_cycle_legacy_fallback(db_session: AsyncSession):
    """When no child has direction_id set, all children are attributed to the direction.

    This covers legacy single-direction cycles where children were spawned before
    the direction_id context key was introduced. Both directions see the same combined
    totals (the pre-fix behavior), ensuring existing history entries remain valid.
    """
    direction_id = "d-legacy"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)
    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=3)
    ]
    await db_session.flush()

    # Children with NO direction_id in context (legacy)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="legacy-merged",
        context={"pr_merged": True, "landed_on_integration": True},
        payloads=[{"type": "fix", "scope": "small"}],
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        title="legacy-no-pr",
        context={},
        payloads=[{"type": "fix", "scope": "small"}],
    )
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        title="legacy-failed",
        context={},
        payloads=[{"type": "fix", "scope": "small"}],
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )
    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # All 3 children must be counted (legacy fallback: no direction_id filtering)
    assert entry["prs_merged"] == 1
    assert entry["tasks_completed"] == 2
    assert entry["tasks_failed"] == 1
    assert entry["tasks_cancelled"] == 0


@pytest.mark.asyncio
async def test_scope_breakdown_skips_ineligible_task_types(db_session: AsyncSession):
    """scope_breakdown excludes web_research, design, and compute_experiment children.

    These types can never open a PR, so they must not inflate the scope
    conversion denominator.  type_breakdown (used by pr_ineligible_created_in_entry)
    still records them correctly — only the scope buckets are excluded.
    """
    direction_id = "d-ineligible"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=5)
    ]
    await db_session.flush()

    # One merged create task at scope=medium (PR-eligible)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "scope": "medium", "direction_id": direction_id},
        payloads=[{"type": "create"}],
        title="eligible-merged",
    )
    # One web_research child at scope=medium (never opens a PR)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "medium", "direction_id": direction_id},
        payloads=[{"type": "web_research"}],
        title="knowledge-web",
    )
    # One design child at scope=small (never opens a PR)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"scope": "small", "direction_id": direction_id},
        payloads=[{"type": "design"}],
        title="knowledge-design",
    )
    # One compute_experiment child at scope=medium (never opens a PR)
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"scope": "medium", "direction_id": direction_id},
        payloads=[{"type": "compute_experiment"}],
        title="compute-exp",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]
    sb = entry["scope_breakdown"]
    tb = entry["type_breakdown"]

    # scope_breakdown must only reflect the PR-eligible create task
    assert sb["medium"]["created"] == 1, (
        "only the create task counts in scope_breakdown"
    )
    assert sb["medium"]["merged"] == 1
    assert "small" not in sb, "design (ineligible) must not create a small bucket"

    # type_breakdown still tracks all types (used by pr_ineligible_created_in_entry)
    assert tb["create"]["created"] == 1
    assert tb["web_research"]["created"] == 1
    assert tb["design"]["created"] == 1
    assert tb["compute_experiment"]["created"] == 1


# ---------------------------------------------------------------------------
# reachable_prs_merged tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reachable_prs_merged_excludes_confirmed_non_landing(
    db_session: AsyncSession,
):
    """reachable_prs_merged counts only explicit landed_on_integration=True.

    Acceptance requirement (named_test gate): confirmed non-landings
    (landed_on_integration=False) must NOT inflate prs_merged OR
    reachable_prs_merged; indeterminate/badge-only merges (key absent) must
    still count in prs_merged but NOT in reachable_prs_merged.
    """
    direction_id = "d-reachable-core"
    project = await _create_project(db_session, directions=[])
    research_task = await _make_research_task(db_session, project.id)
    research_task_id = str(research_task.id)

    project.directions = [
        _make_direction(direction_id, research_task_id, tasks_created=4)
    ]
    await db_session.flush()

    # (a) Reachable: explicit landed_on_integration=True → counts in both.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True, "direction_id": direction_id},
        title="reachable-merge",
    )
    # (b) Badge-only / indeterminate: pr_merged=True, key absent → counts in
    #     prs_merged only (indeterminate ancestry still gets credit).
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "direction_id": direction_id},
        title="badge-only-merge",
    )
    # (c) Confirmed non-landing: explicit False → excluded from BOTH.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": False, "direction_id": direction_id},
        title="stranded-merge",
    )
    # (d) FAILED task: never merged, never counts.
    await _create_child_task(
        db_session,
        research_task.id,
        project.id,
        status=TaskStatus.FAILED,
        context={"direction_id": direction_id},
        title="failed-task",
    )
    await db_session.flush()

    await _update_direction_research_outcomes(
        project_id=project.id,
        direction_id=direction_id,
        research_task_id=research_task_id,
        session=db_session,
    )

    await db_session.refresh(project)
    entry = project.directions[0]["research_history"][0]

    # (a) + (b) count in prs_merged; (c) does NOT (is_merged_completion=False).
    assert entry["prs_merged"] == 2, (
        "confirmed non-landing must NOT inflate prs_merged"
    )
    # Only (a) is ancestry-confirmed.
    assert entry["reachable_prs_merged"] == 1, (
        "only explicit landed_on_integration=True counts as reachable"
    )
    # Invariant: reachable ≤ prs_merged.
    assert entry["reachable_prs_merged"] <= entry["prs_merged"]
    assert acceptance_metric_invariant_violations(entry) == []


@pytest.mark.asyncio
async def test_reachable_prs_merged_invariant_guard(db_session: AsyncSession):
    """acceptance_metric_invariant_violations flags reachable > prs_merged."""
    # Pure-dict check: the guard must catch the reachable > prs_merged drift.
    bad = {"prs_merged": 1, "reachable_prs_merged": 2}
    violations = acceptance_metric_invariant_violations(bad)
    assert any("reachable" in v for v in violations), (
        f"expected reachable-exceeds-prs_merged violation, got: {violations}"
    )

    # Clean entry: field present, within bounds.
    ok = {"prs_merged": 3, "reachable_prs_merged": 2}
    assert acceptance_metric_invariant_violations(ok) == []

    # Legacy entry (field absent): guard skips reachable check, no spurious violation.
    legacy = {"prs_merged": 5}
    assert acceptance_metric_invariant_violations(legacy) == []
