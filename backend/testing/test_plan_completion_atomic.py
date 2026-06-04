"""Regression tests for C15: plan-completion atomicity.

Before the fix, _handle_plan_completion committed child tasks with
``depends_on=[]`` in a first commit, then resolved depends_on UUIDs and
committed again.  The window between those two commits let the dispatcher
pick up QUEUED children before their serial ordering was established,
causing parallel execution instead of the plan-specified order.

The fix collapses the three commits into one: flush() to assign PKs,
resolve depends_on in-memory, then a single commit() so children and
their dependency ordering are always durable together.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models import Task, TaskHistory, TaskStatus
from app.plan_completion import _handle_plan_completion
from app.task_capabilities import PLAN_SUBTASK_TYPES
from testing.factories import make_project, make_task


@pytest.fixture
def _quiet_mocks(mocker):
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)


async def _setup_plan_task(db_session, mocker, subtasks_json, base_branch="main"):
    """Create a project + plan task, patch _ensure_branch to skip GitHub, and
    return the plan task ready for _handle_plan_completion."""
    project = make_project(name="AtomicPlanTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Plan: test ordering",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "plan", "prompt": "do things", "base_branch": base_branch}
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch _ensure_branch (imported lazily from app.task_completion) to
    # return the task without actually calling GitHub.
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )

    return task


@pytest.mark.asyncio
async def test_depends_on_set_atomically_no_empty_window(
    db_session, mocker, _quiet_mocks
):
    """Children persisted with depends_on already populated (C15 regression).

    A plan with subtask B depending on subtask A (via depends_on_indices: [0]).
    After _handle_plan_completion:
    - child A has depends_on == []
    - child B has depends_on == [str(child_A.id)]

    Before the fix, child B was committed with depends_on=[] first, then
    updated in a second commit — creating a window where the dispatcher could
    read B as having no dependencies and dispatch it in parallel with A.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Step A", "prompt": "do step A", "scope": "small"},
                {
                    "title": "Step B",
                    "prompt": "do step B after A",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 2, f"Expected 2 child tasks, got {len(children)}"

    by_title = {c.title: c for c in children}
    child_a = by_title["Step A"]
    child_b = by_title["Step B"]

    # A has no dependencies.
    assert (child_a.depends_on or []) == [], (
        f"Step A should have no deps, got {child_a.depends_on}"
    )

    # B must depend on A — critically, this must be true as-persisted, not
    # only after a hypothetical second commit.
    assert child_b.depends_on == [str(child_a.id)], (
        f"Step B.depends_on should be [{child_a.id!r}], "
        f"got {child_b.depends_on!r} — atomicity broken"
    )


@pytest.mark.asyncio
async def test_three_step_chain_ordering_is_atomic(db_session, mocker, _quiet_mocks):
    """Three-step serial chain: C depends on B depends on A.

    All three dependency assignments must be durable in the same transaction
    that creates the tasks — no intermediate committed state with empty deps.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Step 1", "prompt": "first", "scope": "small"},
                {
                    "title": "Step 2",
                    "prompt": "second",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
                {
                    "title": "Step 3",
                    "prompt": "third",
                    "scope": "small",
                    "depends_on_indices": [1],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 3

    by_title = {c.title: c for c in children}
    s1 = by_title["Step 1"]
    s2 = by_title["Step 2"]
    s3 = by_title["Step 3"]

    # Verify chain: s1 has no deps, s2 depends on s1, s3 depends on s2.
    assert (s1.depends_on or []) == []
    assert s2.depends_on == [str(s1.id)], (
        f"Step 2 should depend on Step 1, got {s2.depends_on!r}"
    )
    assert s3.depends_on == [str(s2.id)], (
        f"Step 3 should depend on Step 2, got {s3.depends_on!r}"
    )


@pytest.mark.asyncio
async def test_history_records_created_in_same_transaction(
    db_session, mocker, _quiet_mocks
):
    """TaskHistory SPAWNED records must be present immediately after commit.

    Before the fix there were three commits; history was added in the third.
    With the fix, history lands in the single final commit alongside children
    and their depends_on.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Alpha", "prompt": "alpha work", "scope": "small"},
                {
                    "title": "Beta",
                    "prompt": "beta work",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    child_ids = {c.id for c in children}
    assert len(child_ids) == 2

    # Both children must have a SPAWNED history record.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id.in_(child_ids),
                    TaskHistory.action == "SPAWNED",
                )
            )
        )
        .scalars()
        .all()
    )
    spawned_task_ids = {h.task_id for h in history}
    assert spawned_task_ids == child_ids, (
        f"Expected SPAWNED history for all children {child_ids}, "
        f"but only found records for {spawned_task_ids}"
    )


@pytest.mark.asyncio
async def test_no_dep_subtasks_all_have_empty_depends_on(
    db_session, mocker, _quiet_mocks
):
    """Subtasks without depends_on_indices get empty depends_on (not affected by fix)."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "T1", "prompt": "independent task 1", "scope": "small"},
                {"title": "T2", "prompt": "independent task 2", "scope": "small"},
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 2
    for child in children:
        assert (child.depends_on or []) == [], (
            f"Independent child {child.title!r} should have empty depends_on, "
            f"got {child.depends_on!r}"
        )


@pytest.mark.asyncio
async def test_file_overlap_deps_injected_atomically(db_session, mocker, _quiet_mocks):
    """Auto-injected file-overlap deps are also committed atomically.

    Two subtasks sharing a related_file — _inject_file_overlap_deps adds a
    dep from the second to the first.  This must also be atomically committed
    (same transaction as child creation), not left empty in an intermediate
    committed state.
    """
    shared_file = "backend/app/models.py"
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Edit models first",
                    "prompt": "add column",
                    "scope": "small",
                    "related_files": [shared_file],
                },
                {
                    "title": "Edit models second",
                    "prompt": "add another column",
                    "scope": "small",
                    "related_files": [shared_file],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 2

    by_title = {c.title: c for c in children}
    first = by_title["Edit models first"]
    second = by_title["Edit models second"]

    # File-overlap injection should have added first as a dependency of second.
    assert (first.depends_on or []) == [], (
        f"First child should have no deps, got {first.depends_on!r}"
    )
    assert second.depends_on == [str(first.id)], (
        f"Second child should depend on first (file overlap), "
        f"got {second.depends_on!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible_type", sorted(PLAN_SUBTASK_TYPES))
async def test_registry_eligible_subtype_is_honored_not_downgraded(
    db_session, mocker, _quiet_mocks, eligible_type
):
    """Every registry plan-eligible write type is honored as a plan child.

    Previously the eligible set was the hardcoded literal
    {create, refactor, migrate, qa}; a plan asking for e.g. ``docs`` or
    ``deps`` was silently rewritten to ``create``. The gate now derives from
    PLAN_SUBTASK_TYPES, so each declared eligible type is spawned AS ITSELF.
    """
    plan_logs = json.dumps(
        {"subtasks": [{"title": f"Step {eligible_type}", "prompt": "do it", "type": eligible_type}]}
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"Expected 1 child for {eligible_type}, got {len(children)}"
    assert children[0].first_payload.get("type") == eligible_type, (
        f"Child should keep its declared eligible type {eligible_type!r}, "
        f"got {children[0].first_payload.get('type')!r}"
    )


@pytest.mark.asyncio
async def test_non_eligible_subtask_type_is_skipped_not_rewritten(
    db_session, mocker, _quiet_mocks
):
    """A non-eligible requested child type is SKIPPED with a log, never silently
    rewritten to ``create``.

    Two non-eligible kinds are exercised: a read-only type (``research``) and a
    write type that needs a pre-existing PR context (``fix``). Neither may
    masquerade as a successfully-spawned ``create``, and ``fix`` must not crash
    the plan (it can't build a valid fix payload). The eligible sibling in the
    same plan still spawns, proving the skip is per-subtask and observable.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Valid", "prompt": "implement", "type": "create"},
                {"title": "Bogus", "prompt": "go research", "type": "research"},
                {"title": "NeedsPR", "prompt": "patch it", "type": "fix"},
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    # Only the eligible subtask spawned; the non-eligible ones were skipped.
    assert len(children) == 1, f"Expected 1 spawned child, got {len(children)}"
    assert children[0].title == "Valid"
    assert children[0].first_payload.get("type") == "create"
    # The non-eligible types never masqueraded as a create.
    assert {c.title for c in children} == {"Valid"}
    assert not any(
        c.first_payload.get("type") in {"research", "fix"} for c in children
    )
