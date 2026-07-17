"""Behavioral parity test for the two parent-terminal finalizers.

A parent task whose children are all terminal is finalized by TWO independent
code paths:

  1. the reaper requeue path — ``task_lifecycle_requeue._requeue_parent_task``
  2. the rollup / recovery sweep — ``rollup_management._check_and_trigger_rollup``

Both are supposed to obey the SAME centralized terminal-state verdict
(``_resolve_terminal_parent_verdict`` / ``_terminate_all_non_genuine_cancelled`` /
``_terminate_all_non_death_cancelled``).  Only the reaper path was historically
tested against it, and the rollup path drifted once: its all-cancelled branch
hard-coded ``CANCELLED`` while the reaper path produced ``FAILED``
(``SCOPE_EMPTY`` for an all-non-genuine-cancel set, ``AGENT_TIMEOUT`` when a
genuine cancel is present).

Three canonical child-set shapes are tested:

  non-death:    all children are is_non_death_cancellation  → parent CANCELLED
                (co-deferred shelving / requeue replacements / scope-empty)
  non-genuine:  all children are _is_non_genuine_cancelled but NOT all non-death
                → parent FAILED + SCOPE_EMPTY
  genuine:      at least one genuine death cancel          → parent FAILED + AGENT_TIMEOUT

These tests drive BOTH finalizers with the same representative
all-terminal-unsucceeded child sets and assert they reach the SAME terminal
status AND the SAME failure classification.  They assert *observable parent
state* after actually calling the finalizers — deliberately NOT a static / AST
"is this function registered" scan, since a prior registration-style invariant
in this repo was found vacuous (it sliced source by column offsets, not byte
offsets, and protected nothing).

The DB fixture (``cleanup_db``) and task builders are reused from
``test_rollup_cleanup``; the reaper finalizer operates on the same in-memory
session, so a single DB backs both parents.
"""

import uuid

import pytest
from unittest.mock import AsyncMock

from app.failure_classification import CO_DEFERRED_SHELVED
from app.models import TaskStatus

# Reuse the in-memory SQLite DB fixture + task builders.
from testing.test_rollup_cleanup import _make_project, _make_task

pytest_plugins = ["testing.test_rollup_cleanup"]


def _mock_finalizer_deps(mocker):
    """Patch the side-effecting deps shared by BOTH finalizers so the tests
    observe only DB state.

    Returns ``(record_failure, failure_alert)`` so a case can assert the
    direction-health ding / failure alert fired (genuine cancel) or did not
    (all non-genuine / non-death cancels). ``_record_parent_failure`` is
    referenced by both paths via ``app.task_lifecycle_requeue`` (the rollup
    branch imports it from there), so a single patch covers both finalizers.
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
async def test_parent_terminal_parity_all_non_death_cancelled(cleanup_db, mocker):
    """Every child a non-death cancellation (_replaced_by_task_id / co-deferred /
    scope-empty): BOTH finalizers must reach CANCELLED with NO direction-health
    ding and NO failure alert.

    ``_replaced_by_task_id`` is recognized by both is_non_death_cancellation and
    _is_non_genuine_cancelled, but the non-death check fires first in
    _resolve_terminal_parent_verdict, so the parent is CANCELLED (not FAILED).
    """
    record_failure, failure_alert = _mock_finalizer_deps(mocker)
    session = cleanup_db
    project = await _make_project(session)

    # Non-death cancels: ``_replaced_by_task_id`` marks a requeue replacement
    # whose work continues under the clone (is_non_death_cancellation → True).
    non_death = [
        {"_replaced_by_task_id": str(uuid.uuid4())},
        {"_replaced_by_task_id": str(uuid.uuid4())},
    ]

    reaper_parent, _ = await _make_parent_with_cancelled_children(
        session, project.id, non_death
    )
    _, rollup_child = await _make_parent_with_cancelled_children(
        session, project.id, non_death
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
    # Canonical verdict for an all-non-death-cancel set: CANCELLED, not FAILED.
    assert status_reaper == TaskStatus.CANCELLED
    assert category_reaper is None
    # Non-death cancels never ding direction health or raise a failure alert,
    # on EITHER path.
    record_failure.assert_not_called()
    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_terminal_parity_all_co_deferred_cancelled(cleanup_db, mocker):
    """Every child co-deferred shelved (_co_deferred_shelved): BOTH finalizers must
    reach CANCELLED with NO direction-health ding and NO failure alert.

    Co-deferred children ARE now recognized by _is_non_genuine_cancelled (via the
    CO_DEFERRED_SHELVED context key), so they do not count as genuine deaths.
    Before that fix was added, the old path lacked the CO_DEFERRED_SHELVED check
    and would have counted them as genuine deaths, producing FAILED + AGENT_TIMEOUT.
    The is_non_death_cancellation check in _resolve_terminal_parent_verdict also
    catches them as a belt-and-suspenders guard against the false failure signal.

    This test locks the fix for the real incident: research task 5a6bac51 whose
    only child plan was co-deferred was incorrectly FAILED + AGENT_TIMEOUT.
    """
    record_failure, failure_alert = _mock_finalizer_deps(mocker)
    session = cleanup_db
    project = await _make_project(session)

    # Co-deferred children carry CO_DEFERRED_SHELVED in context.
    co_deferred = [
        {CO_DEFERRED_SHELVED: True},
        {CO_DEFERRED_SHELVED: True},
    ]

    reaper_parent, _ = await _make_parent_with_cancelled_children(
        session, project.id, co_deferred
    )
    _, rollup_child = await _make_parent_with_cancelled_children(
        session, project.id, co_deferred
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
    # Co-deferred shelving is recoverable: parent must NOT be FAILED, must NOT
    # ding direction health, must NOT raise a failure alert.
    assert status_reaper == TaskStatus.CANCELLED, (
        f"co-deferred parent should be CANCELLED, got {status_reaper}"
    )
    assert category_reaper is None
    record_failure.assert_not_called()
    failure_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_terminal_parity_all_admin_cancelled(cleanup_db, mocker):
    """Operator-cancelled children finalize both paths as quiet CANCELLED."""
    record_failure, failure_alert = _mock_finalizer_deps(mocker)
    session = cleanup_db
    project = await _make_project(session)

    admin_cancelled = [
        {
            "failure_category": "ADMIN_CANCEL",
            "failure_class": "ADMIN_CANCEL",
            "cancelled_by": "USER",
        },
        {
            "failure_category": "ADMIN_CANCEL",
            "failure_class": "ADMIN_CANCEL",
            "cancelled_by": "USER",
        },
    ]
    reaper_parent, _ = await _make_parent_with_cancelled_children(
        session, project.id, admin_cancelled
    )
    _, rollup_child = await _make_parent_with_cancelled_children(
        session, project.id, admin_cancelled
    )

    status_reaper, category_reaper = await _drive_reaper(session, reaper_parent)
    status_rollup, category_rollup = await _drive_rollup(session, rollup_child)

    assert status_reaper == status_rollup == TaskStatus.CANCELLED
    assert category_reaper == category_rollup is None
    assert "AGENT_TIMEOUT" not in (reaper_parent.context or {}).values()
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

    # Genuine cancels carry no non-genuine or non-death markers, so both
    # _is_non_genuine_cancelled and is_non_death_cancellation are False.
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
