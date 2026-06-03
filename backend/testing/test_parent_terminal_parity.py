"""Behavioral parity test for the two parent-terminal finalizers.

A parent task whose children are all terminal is finalized by TWO independent
code paths:

  1. the reaper requeue path — ``task_lifecycle_requeue._requeue_parent_task``
  2. the rollup / recovery sweep — ``rollup_management._check_and_trigger_rollup``

Both are supposed to obey the SAME centralized terminal-state verdict
(``_resolve_terminal_parent_verdict`` / ``_terminate_all_non_genuine_cancelled``).
Only the reaper path was historically tested against it, and the rollup path
drifted once: its all-cancelled branch hard-coded ``CANCELLED`` while the reaper
path produced ``FAILED`` (``SCOPE_EMPTY`` for an all-non-genuine-cancel set,
``AGENT_TIMEOUT`` when a genuine cancel is present).

These tests drive BOTH finalizers with the same representative
all-terminal-unsucceeded child sets and assert they reach the SAME terminal
status AND the SAME failure classification. They assert *observable parent
state* after actually calling the finalizers — deliberately NOT a static / AST
"is this function registered" scan, since a prior registration-style invariant
in this repo was found vacuous (it sliced source by column offsets, not byte
offsets, and protected nothing). Each case also asserts the canonical status is
``FAILED`` so the test still fails if BOTH finalizers drift together to
``CANCELLED``.

The DB fixture (``cleanup_db``) and task builders are reused from
``test_rollup_cleanup``; the reaper finalizer operates on the same in-memory
session, so a single DB backs both parents.
"""

import uuid

import pytest
from unittest.mock import AsyncMock

from app.models import TaskStatus

# Reuse the in-memory SQLite DB fixture + task builders.
from testing.test_rollup_cleanup import _make_project, _make_task

pytest_plugins = ["testing.test_rollup_cleanup"]


def _mock_finalizer_deps(mocker):
    """Patch the side-effecting deps shared by BOTH finalizers so the tests
    observe only DB state.

    Returns ``(record_failure, failure_alert)`` so a case can assert the
    direction-health ding / failure alert fired (genuine cancel) or did not
    (all non-genuine cancels). ``_record_parent_failure`` is referenced by both
    paths via ``app.task_lifecycle_requeue`` (the rollup branch imports it from
    there), so a single patch covers both finalizers.
    """
    # Reaper path imports broadcast from app.task_lifecycle; rollup path imports
    # it from app.broadcast — patch both so neither makes a real broadcast call.
    mocker.patch("app.task_lifecycle.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Rollup path cleans up staging branches via GitHub — stub it out.
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)
    failure_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    record_failure = mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
    return record_failure, failure_alert


async def _drive_reaper(session, parent):
    from app.task_lifecycle_requeue import _requeue_parent_task

    await _requeue_parent_task(parent, session)
    await session.refresh(parent)
    return parent.status, (parent.context or {}).get("failure_category")


async def _drive_rollup(session, child):
    from app.rollup_management import _check_and_trigger_rollup

    await _check_and_trigger_rollup(child, session)
    parent = await session.get(type(child), child.parent_task_id)
    await session.refresh(parent)
    return parent.status, (parent.context or {}).get("failure_category")


async def _make_parent_with_cancelled_children(session, project_id, child_contexts):
    """Build an EVALUATING parent with one CANCELLED child per entry in
    ``child_contexts`` (each entry is the child's ``context`` dict). Returns
    ``(parent, first_child)`` — the first child is the trigger passed to the
    rollup finalizer."""
    parent = await _make_task(
        session,
        project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 0, "direction_id": "d-parity"},
        plan_output="some plan",
    )
    children = []
    for ctx in child_contexts:
        child = await _make_task(
            session,
            project_id,
            task_type="create",
            status=TaskStatus.CANCELLED,
            parent_task_id=parent.id,
            context=ctx,
        )
        children.append(child)
    return parent, children[0]


@pytest.mark.asyncio
async def test_parent_terminal_parity_all_non_genuine_cancelled(cleanup_db, mocker):
    """Every child a non-genuine cancel (replaced-by-retry / cascade): BOTH
    finalizers must reach FAILED + SCOPE_EMPTY with NO direction-health ding and
    NO failure alert. The rollup path drifting back to CANCELLED (or skipping
    the shared verdict) breaks this."""
    record_failure, failure_alert = _mock_finalizer_deps(mocker)
    session = cleanup_db
    project = await _make_project(session)

    # Non-genuine cancels: ``_replaced_by_task_id`` marks a cascade/requeue
    # replacement (see direction_outcomes._is_cascade_cancelled).
    non_genuine = [
        {"_replaced_by_task_id": str(uuid.uuid4())},
        {"_replaced_by_task_id": str(uuid.uuid4())},
    ]

    reaper_parent, _ = await _make_parent_with_cancelled_children(
        session, project.id, non_genuine
    )
    _, rollup_child = await _make_parent_with_cancelled_children(
        session, project.id, non_genuine
    )

    status_reaper, category_reaper = await _drive_reaper(session, reaper_parent)
    status_rollup, category_rollup = await _drive_rollup(session, rollup_child)

    # Parity: identical terminal status and identical failure classification.
    assert status_reaper == status_rollup, (
        f"finalizers diverged on status: reaper={status_reaper} "
        f"rollup={status_rollup}"
    )
    assert category_reaper == category_rollup, (
        f"finalizers diverged on failure category: reaper={category_reaper} "
        f"rollup={category_rollup}"
    )
    # Canonical verdict for an all-non-genuine-cancel set.
    assert status_reaper == TaskStatus.FAILED
    assert category_reaper == "SCOPE_EMPTY"
    # Non-genuine cancels never ding direction health or raise a failure alert,
    # on EITHER path.
    record_failure.assert_not_called()
    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_terminal_parity_all_genuine_cancelled(cleanup_db, mocker):
    """Every child a genuine cancel (bare CANCELLED, no non-genuine markers):
    BOTH finalizers must reach FAILED + AGENT_TIMEOUT. The shared verdict treats
    a genuine cancellation as a genuine failure, so direction health IS dinged on
    both paths."""
    record_failure, _ = _mock_finalizer_deps(mocker)
    session = cleanup_db
    project = await _make_project(session)

    # Genuine cancels carry no non-genuine markers, so _is_non_genuine_cancelled
    # is False and they count toward genuine_failures.
    genuine = [{}, {}]

    reaper_parent, _ = await _make_parent_with_cancelled_children(
        session, project.id, genuine
    )
    _, rollup_child = await _make_parent_with_cancelled_children(
        session, project.id, genuine
    )

    status_reaper, category_reaper = await _drive_reaper(session, reaper_parent)
    status_rollup, category_rollup = await _drive_rollup(session, rollup_child)

    assert status_reaper == status_rollup, (
        f"finalizers diverged on status: reaper={status_reaper} "
        f"rollup={status_rollup}"
    )
    assert category_reaper == category_rollup, (
        f"finalizers diverged on failure category: reaper={category_reaper} "
        f"rollup={category_rollup}"
    )
    # Canonical verdict for an all-genuine-cancel set.
    assert status_reaper == TaskStatus.FAILED
    assert category_reaper == "AGENT_TIMEOUT"
    # A genuine cancellation IS a genuine failure — direction health is dinged on
    # both paths (the patched _record_parent_failure records the attempt).
    assert record_failure.call_count >= 2
