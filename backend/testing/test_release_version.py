"""Tests for release_version.py — the ReleaseVersion table helpers
(automain LTS Phase 2).

Focus areas:
  * count_merged_since — the landed-on-integration filter MUST be applied on
    both dialects: a COMPLETED task without a landed signal is never counted;
    a merge-equivalent non-landing is excluded; a non-COMPLETED task with the
    flag is never counted; the ``since`` lower bound is exclusive-of-older rows.
  * open_version / current_open_version — insert + single-open-row read,
    including the multiple-open-rows anomaly path.
  * set_version_phase — valid transitions, idempotent no-op, invalid phase
    raises, reasoning truncation.
  * to_phase_block — compact payload shape.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app import release_version as rv
from app import strategic_spawning as ss
from app.models import Project, QualificationVerdict, ReleaseVersion, Task, TaskStatus
from app.time_utils import _utcnow


# ===========================================================================
# Shared fixture helpers
# ===========================================================================


async def _make_project(db_session, name: str = "RV-Test") -> Project:
    project = Project(name=name, user_id="rv-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_task(
    db_session,
    project: Project,
    *,
    status: TaskStatus,
    context: dict,
    updated_at,
) -> Task:
    """Create a Task with an explicit ``updated_at`` and ``context``.

    ``updated_at`` is set at construction time; ``onupdate`` only fires on a
    subsequent UPDATE so the insert value is preserved — this lets a test
    place a task before/after the ``since`` boundary deterministically.
    """
    task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=status,
        context=context,
        updated_at=updated_at,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ===========================================================================
# count_merged_since — the landed signal filter must be applied
# ===========================================================================


@pytest.mark.asyncio
async def test_count_merged_since_counts_merged_completed_task(db_session):
    """A COMPLETED task with context['pr_merged']=True updated after `since`
    is counted."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_count_merged_since_excludes_completed_without_pr_merged(db_session):
    """REGRESSION GUARD: a COMPLETED task that lacks the pr_merged flag must
    NOT be counted.  The old SQLite/except-fallback path dropped this filter
    and over-counted every COMPLETED task."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    # COMPLETED but never merged — no pr_merged key at all.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"some_other_key": "value"},
        updated_at=_utcnow(),
    )
    # COMPLETED with pr_merged explicitly False.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0, "pr_merged filter must exclude non-merged COMPLETED tasks"


@pytest.mark.asyncio
async def test_count_merged_since_mixed_only_counts_merged(db_session):
    """With a mix of merged and non-merged COMPLETED tasks, only the ones
    flagged pr_merged=True are counted."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    for _ in range(3):
        await _make_task(
            db_session,
            project,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            updated_at=_utcnow(),
        )
    for _ in range(2):
        await _make_task(
            db_session,
            project,
            status=TaskStatus.COMPLETED,
            context={},
            updated_at=_utcnow(),
        )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 3


@pytest.mark.asyncio
async def test_count_merged_since_excludes_non_merge_unblocks(db_session):
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_count_merged_since_excludes_non_completed_status(db_session):
    """A task with context['pr_merged']=True but a non-COMPLETED status is
    not counted — the status filter still applies alongside the flag."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project,
        status=TaskStatus.FAILED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0


@pytest.mark.asyncio
async def test_count_merged_since_respects_since_lower_bound(db_session):
    """A merged task updated BEFORE `since` is excluded; the same task type
    updated after is included."""
    project = await _make_project(db_session)
    since = _utcnow()
    old_completed_at = since - timedelta(hours=2)
    fresh_completed_at = since + timedelta(hours=1)

    old_task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=old_completed_at,
        updated_at=old_completed_at,
    )
    db_session.add(old_task)
    await db_session.commit()
    with patch("app.models._utcnow", return_value=old_completed_at):
        old_task.status = TaskStatus.COMPLETED
        old_task.context = {"pr_merged": True}
        await db_session.commit()

    fresh_task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=fresh_completed_at,
        updated_at=fresh_completed_at,
    )
    db_session.add(fresh_task)
    await db_session.commit()
    with patch("app.models._utcnow", return_value=fresh_completed_at):
        fresh_task.status = TaskStatus.COMPLETED
        fresh_task.context = {"pr_merged": True}
        await db_session.commit()

    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 1


@pytest.mark.asyncio
async def test_merged_window_ignores_post_terminal_updates(db_session):
    """Post-terminal writes must not pull an older merge into the window."""
    project = await _make_project(db_session)
    since = _utcnow()
    completed_at = since - timedelta(days=1)
    updated_later_at = since + timedelta(days=1)

    task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=completed_at,
        updated_at=completed_at,
    )
    db_session.add(task)
    await db_session.commit()

    with patch("app.models._utcnow", return_value=completed_at):
        task.status = TaskStatus.COMPLETED
        task.context = {"pr_merged": True}
        await db_session.commit()

    with patch("app.models._utcnow", return_value=updated_later_at):
        task.latest_status_message = "Post-terminal context update"
        await db_session.commit()

    count = await rv.count_merged_since(db_session, project.id, since)
    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )

    assert count == 0
    assert agg == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 0,
    }


@pytest.mark.asyncio
async def test_count_merged_since_scoped_to_project(db_session):
    """A merged task belonging to a different project is not counted."""
    project_a = await _make_project(db_session, name="RV-A")
    project_b = await _make_project(db_session, name="RV-B")
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project_a,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )
    await _make_task(
        db_session,
        project_b,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=_utcnow(),
    )

    assert await rv.count_merged_since(db_session, project_a.id, since) == 1
    assert await rv.count_merged_since(db_session, project_b.id, since) == 1


@pytest.mark.asyncio
async def test_count_merged_since_zero_when_no_tasks(db_session):
    """An empty project returns 0, not None."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)
    count = await rv.count_merged_since(db_session, project.id, since)
    assert count == 0
    assert isinstance(count, int)


# ===========================================================================
# until upper bound — closed (promoted/abandoned) version windows must NOT
# double-count tasks that merged after the version closed.  Each of the three
# window helpers accepts an optional ``until=`` and must exclude tasks whose
# stable terminal timestamp is strictly after it.
# ===========================================================================


async def _make_merged_task_at(db_session, project, *, completed_at, context=None):
    """Insert a merged COMPLETED task whose stable terminal timestamp is
    ``completed_at``.

    Both ``created_at`` and ``updated_at`` are pinned to ``completed_at``: with
    no status-transition entry in ``status_log``, ``_task_completed_at`` falls
    back to ``created_at``, while the SQL pre-filter keys off ``updated_at``.
    Pinning both keeps the window boundary deterministic.
    """
    task = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x", "pr_number": 42}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, **(context or {})},
        created_at=completed_at,
        updated_at=completed_at,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_count_merged_since_until_excludes_tasks_after_upper_bound(db_session):
    """A merged task completed after ``until`` is excluded; one inside the
    [since, until] window is counted."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_at(
        db_session, project, completed_at=until - timedelta(minutes=10)
    )  # inside window
    await _make_merged_task_at(
        db_session, project, completed_at=until + timedelta(minutes=10)
    )  # after the upper bound

    bounded = await rv.count_merged_since(db_session, project.id, since, until=until)
    unbounded = await rv.count_merged_since(db_session, project.id, since)
    assert bounded == 1, "task merged after until must be excluded"
    assert unbounded == 2, "without until both tasks fall in the window"


@pytest.mark.asyncio
async def test_count_merged_since_until_includes_task_exactly_at_bound(db_session):
    """``until`` is inclusive — a task completed exactly at the bound counts."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_at(db_session, project, completed_at=until)

    count = await rv.count_merged_since(db_session, project.id, since, until=until)
    assert count == 1


@pytest.mark.asyncio
async def test_list_merged_tasks_for_version_until_excludes_later_tasks(db_session):
    """list_merged_tasks_for_version drops tasks completed after ``until``."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_at(
        db_session, project, completed_at=until - timedelta(minutes=10)
    )
    await _make_merged_task_at(
        db_session, project, completed_at=until + timedelta(minutes=10)
    )

    bounded = await rv.list_merged_tasks_for_version(
        db_session, project.id, since, until=until
    )
    unbounded = await rv.list_merged_tasks_for_version(db_session, project.id, since)
    assert len(bounded) == 1, "task merged after until must be excluded from listing"
    assert len(unbounded) == 2


@pytest.mark.asyncio
async def test_acceptance_aggregate_until_excludes_later_tasks(db_session):
    """compute_version_acceptance_aggregate honors the ``until`` upper bound."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_at(
        db_session, project, completed_at=until - timedelta(minutes=10)
    )
    await _make_merged_task_at(
        db_session, project, completed_at=until + timedelta(minutes=10)
    )

    bounded = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since, until=until
    )
    unbounded = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert bounded["total"] == 1, "task merged after until must not be aggregated"
    assert unbounded["total"] == 2


# ===========================================================================
# Post-terminal-write upper-bound: a task that completed before ``until`` but
# received a post-terminal write bumping updated_at past ``until`` must still
# be INCLUDED in the bounded window.  The old buggy `updated_at <= until` SQL
# prefilter would have dropped it before the precise Python re-check ran.
# ===========================================================================


async def _make_merged_task_post_terminal_write(
    db_session, project, *, completed_at, post_write_at
):
    """Insert a merged COMPLETED task with a post-terminal write.

    ``created_at`` is pinned to ``completed_at`` so ``_task_completed_at``
    returns ``completed_at`` (no status_log entry, falls back to created_at).
    ``updated_at`` is set to ``post_write_at`` to simulate a write that
    happened after the task was already in a terminal state.
    """
    task = Task(
        project_id=project.id,
        title="post-terminal-write task",
        payloads=[{"type": "create", "prompt": "x", "pr_number": 99}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_at=completed_at,
        updated_at=post_write_at,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_count_merged_since_until_includes_task_with_post_terminal_write(
    db_session,
):
    """A task that completed before ``until`` but has updated_at > until
    (post-terminal write) must still be counted — the SQL prefilter must not
    drop it before the precise _task_completed_at re-check."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    # completed before until, but updated_at is after until
    await _make_merged_task_post_terminal_write(
        db_session,
        project,
        completed_at=until - timedelta(minutes=10),
        post_write_at=until + timedelta(minutes=30),
    )

    bounded = await rv.count_merged_since(db_session, project.id, since, until=until)
    assert bounded == 1, (
        "task completed before until must be counted even if updated_at > until"
    )


@pytest.mark.asyncio
async def test_list_merged_tasks_for_version_includes_task_with_post_terminal_write(
    db_session,
):
    """list_merged_tasks_for_version must not drop a task completed before
    ``until`` that received a post-terminal write bumping updated_at past it."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_post_terminal_write(
        db_session,
        project,
        completed_at=until - timedelta(minutes=10),
        post_write_at=until + timedelta(minutes=30),
    )

    bounded = await rv.list_merged_tasks_for_version(
        db_session, project.id, since, until=until
    )
    assert len(bounded) == 1, (
        "task completed before until must appear in listing even if updated_at > until"
    )


@pytest.mark.asyncio
async def test_acceptance_aggregate_includes_task_with_post_terminal_write(db_session):
    """compute_version_acceptance_aggregate must count a task completed before
    ``until`` that received a post-terminal write bumping updated_at past it."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=2)
    until = _utcnow() - timedelta(hours=1)

    await _make_merged_task_post_terminal_write(
        db_session,
        project,
        completed_at=until - timedelta(minutes=10),
        post_write_at=until + timedelta(minutes=30),
    )

    bounded = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since, until=until
    )
    assert bounded["total"] == 1, (
        "task completed before until must be aggregated even if updated_at > until"
    )


# ===========================================================================
# compute_version_acceptance_aggregate — canonical acceptance bucket
# classification of version-window merged tasks
# ===========================================================================


def _landed_ctx(extra: dict | None = None) -> dict:
    """A landed-on-integration task context, optionally with extra keys."""
    ctx = {"pr_merged": True, "landed_on_integration": True}
    if extra:
        ctx.update(extra)
    return ctx


@pytest.mark.asyncio
async def test_acceptance_aggregate_classifies_each_bucket(db_session):
    """Every canonical acceptance bucket is counted with shared key names."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    # proven — verdict recorded with strict machine proof.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx(
            {
                "code_acceptance_verdict": {
                    "satisfied": True,
                    "proven": True,
                    "kind": "named_test",
                }
            }
        ),
        updated_at=_utcnow(),
    )
    # satisfied_unproven — soft satisfied verdict, not machine-checked.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx(
            {
                "code_acceptance_verdict": {
                    "satisfied": True,
                    "proven": False,
                    "kind": "observable_behavior",
                }
            }
        ),
        updated_at=_utcnow(),
    )
    # declared_unsatisfied — verdict recorded and explicitly NOT satisfied.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx(
            {"code_acceptance_verdict": {"satisfied": False, "kind": "named_test"}}
        ),
        updated_at=_utcnow(),
    )
    # undeclared — no verdict key (the common/legacy case).
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx(),
        updated_at=_utcnow(),
    )
    # merged_unverified_degraded — degraded verification overrides verdict state.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx(
            {
                "review_loop": {"unverified_merge_reason": "verify_setup_failed"},
                "code_acceptance_verdict": {"satisfied": True, "proven": True},
            }
        ),
        updated_at=_utcnow(),
    )
    # malformed — verdict dict recorded but neither satisfied nor unsatisfied.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx({"code_acceptance_verdict": {"kind": "named_test"}}),
        updated_at=_utcnow(),
    )

    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert agg == {
        "proven": 1,
        "satisfied_unproven": 1,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 1,
        "undeclared": 1,
        "merged_unverified_degraded": 1,
        "malformed": 1,
        "total": 6,
    }


@pytest.mark.asyncio
async def test_acceptance_aggregate_excludes_unlanded_and_non_completed(db_session):
    """Only COMPLETED + landed tasks are classified; an unmerged completion and
    a non-completed task are excluded from every bucket."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    # Landed + declared-unsatisfied — counted.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx({"code_acceptance_verdict": {"satisfied": False}}),
        updated_at=_utcnow(),
    )
    # COMPLETED but never merged — excluded.
    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"code_acceptance_verdict": {"satisfied": False}},
        updated_at=_utcnow(),
    )
    # In-flight with a declared verdict — excluded (not COMPLETED).
    await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        context=_landed_ctx({"code_acceptance_verdict": {"satisfied": False}}),
        updated_at=_utcnow(),
    )

    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert agg == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 1,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_acceptance_aggregate_respects_since_window(db_session):
    """Tasks merged before ``since`` are outside the version window and excluded."""
    project = await _make_project(db_session)
    since = _utcnow()
    old_completed_at = since - timedelta(hours=2)
    fresh_completed_at = since + timedelta(hours=1)

    before_window = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=old_completed_at,
        updated_at=old_completed_at,
    )
    db_session.add(before_window)
    await db_session.commit()
    with patch("app.models._utcnow", return_value=old_completed_at):
        before_window.status = TaskStatus.COMPLETED
        before_window.context = _landed_ctx(
            {"code_acceptance_verdict": {"satisfied": False}}
        )
        await db_session.commit()

    inside_window = Task(
        project_id=project.id,
        title="merge-test-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.QUEUED,
        context={},
        created_at=fresh_completed_at,
        updated_at=fresh_completed_at,
    )
    db_session.add(inside_window)
    await db_session.commit()
    with patch("app.models._utcnow", return_value=fresh_completed_at):
        inside_window.status = TaskStatus.COMPLETED
        inside_window.context = _landed_ctx(
            {"code_acceptance_verdict": {"satisfied": True, "proven": True}}
        )
        await db_session.commit()

    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert agg == {
        "proven": 1,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_acceptance_aggregate_empty_window_zeros(db_session):
    """An empty window returns all-zero counts, not None."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert agg == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 0,
    }


@pytest.mark.asyncio
async def test_acceptance_aggregate_malformed_verdict_is_failsafe(db_session):
    """A recorded verdict that is neither satisfied nor explicitly unsatisfied
    is fail-safe but visible in the malformed bucket."""
    project = await _make_project(db_session)
    since = _utcnow() - timedelta(hours=1)

    await _make_task(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context=_landed_ctx({"code_acceptance_verdict": {"kind": "named_test"}}),
        updated_at=_utcnow(),
    )

    agg = await rv.compute_version_acceptance_aggregate(
        db_session, project.id, since=since
    )
    assert agg == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 1,
        "total": 1,
    }


# ===========================================================================
# count_in_flight_tasks — non-terminal task count for the version-cut disclosure
# ===========================================================================


@pytest.mark.asyncio
async def test_count_in_flight_counts_all_non_terminal_statuses(db_session):
    """Every non-terminal status (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING/
    BLOCKED) is counted as in flight."""
    project = await _make_project(db_session)
    now = _utcnow()
    for status in (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
    ):
        await _make_task(db_session, project, status=status, context={}, updated_at=now)

    count = await rv.count_in_flight_tasks(db_session, project.id)
    assert count == 5


@pytest.mark.asyncio
async def test_count_in_flight_excludes_terminal_statuses(db_session):
    """COMPLETED/FAILED/CANCELLED tasks are terminal and not counted."""
    project = await _make_project(db_session)
    now = _utcnow()
    for status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ):
        await _make_task(db_session, project, status=status, context={}, updated_at=now)
    # One live task to prove the query selects the right rows.
    await _make_task(
        db_session, project, status=TaskStatus.IN_PROGRESS, context={}, updated_at=now
    )

    count = await rv.count_in_flight_tasks(db_session, project.id)
    assert count == 1


@pytest.mark.asyncio
async def test_count_in_flight_scoped_to_project(db_session):
    """In-flight tasks are counted per-project, not globally."""
    project_a = await _make_project(db_session, name="RV-A")
    project_b = await _make_project(db_session, name="RV-B")
    now = _utcnow()
    await _make_task(
        db_session, project_a, status=TaskStatus.IN_PROGRESS, context={}, updated_at=now
    )
    await _make_task(
        db_session, project_b, status=TaskStatus.QUEUED, context={}, updated_at=now
    )
    await _make_task(
        db_session, project_b, status=TaskStatus.BLOCKED, context={}, updated_at=now
    )

    assert await rv.count_in_flight_tasks(db_session, project_a.id) == 1
    assert await rv.count_in_flight_tasks(db_session, project_b.id) == 2


@pytest.mark.asyncio
async def test_count_in_flight_zero_when_no_tasks(db_session):
    """An empty project returns 0, not None."""
    project = await _make_project(db_session)
    count = await rv.count_in_flight_tasks(db_session, project.id)
    assert count == 0
    assert isinstance(count, int)


# ===========================================================================
# open_version / current_open_version
# ===========================================================================


@pytest.mark.asyncio
async def test_open_version_inserts_open_build_row(db_session):
    """open_version inserts a row with status='open', phase='build'."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, base_sha="abc123"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.version_number == 1
    assert version.status == "open"
    assert version.phase == "build"
    assert version.base_sha == "abc123"
    assert version.opened_at is not None


@pytest.mark.asyncio
async def test_open_version_stores_goal(db_session):
    """open_version persists the goal snapshot when provided."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session,
        project.id,
        version_number=1,
        goal="Ship the mobile-responsive admin UI",
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "Ship the mobile-responsive admin UI"


@pytest.mark.asyncio
async def test_open_version_goal_defaults_to_none(db_session):
    """open_version leaves goal as None when not provided."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is None


@pytest.mark.asyncio
async def test_open_version_truncates_long_goal(db_session):
    """A goal longer than _GOAL_MAX is truncated to the cap."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="g" * 5000
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is not None
    assert len(version.goal) == rv._GOAL_MAX


@pytest.mark.asyncio
async def test_current_open_version_returns_none_when_absent(db_session):
    """No open version → None."""
    project = await _make_project(db_session)
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_current_open_version_returns_the_open_row(db_session):
    """current_open_version returns the single status='open' row."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 1


@pytest.mark.asyncio
async def test_current_open_version_ignores_non_open_rows(db_session):
    """A promoted row is not returned; only the open one is."""
    project = await _make_project(db_session)

    promoted = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow(),
    )
    db_session.add(promoted)
    await rv.open_version(db_session, project.id, version_number=2)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 2


@pytest.mark.asyncio
async def test_current_open_version_picks_most_recent_on_anomaly(db_session):
    """Two open rows (data anomaly) → the most recently opened is returned."""
    project = await _make_project(db_session)

    older = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow() - timedelta(hours=2),
    )
    newer = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add(older)
    db_session.add(newer)
    await db_session.commit()

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 2


# ===========================================================================
# set_version_phase
# ===========================================================================


@pytest.mark.asyncio
async def test_set_version_phase_transition(db_session):
    """A build→stabilize transition stamps phase, phase_changed_at, reasoning."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.phase == "build"
    rv.set_version_phase(version, "stabilize", reasoning="ready to stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.phase == "stabilize"
    assert version.phase_changed_at is not None
    assert version.phase_reasoning == "ready to stabilize"


@pytest.mark.asyncio
async def test_set_version_phase_idempotent_noop(db_session):
    """Setting the phase to its current value is a no-op (no timestamp bump)."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    # Version starts in 'build'; set to 'build' again.
    rv.set_version_phase(version, "build", reasoning="should not apply")
    # No-op: reasoning is not stamped, phase_changed_at stays None.
    assert version.phase == "build"
    assert version.phase_changed_at is None
    assert version.phase_reasoning is None


def test_set_version_phase_rejects_invalid_phase():
    """An out-of-set phase value raises ValueError."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    with pytest.raises(ValueError, match="Invalid phase"):
        rv.set_version_phase(version, "not-a-phase")


def test_set_version_phase_truncates_long_reasoning():
    """Reasoning longer than the cap is truncated."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    long_reason = "x" * 5000
    rv.set_version_phase(version, "stabilize", reasoning=long_reason)
    assert version.phase_reasoning is not None
    assert len(version.phase_reasoning) == rv._PHASE_REASONING_MAX


# ===========================================================================
# set_version_goal
# ===========================================================================


@pytest.mark.asyncio
async def test_set_version_goal_operator_edit_stamps_editor_and_timestamp(db_session):
    """An operator edit stamps goal, goal_updated_at, and goal_editor_info."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    rv.set_version_goal(version, "Refine the objective", editor_id="op@example.com")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal == "Refine the objective"
    assert version.goal_updated_at is not None
    assert version.goal_editor_info == "[operator:op@example.com]"


@pytest.mark.asyncio
async def test_open_version_auto_derived_goal_leaves_editor_fields_null(db_session):
    """A goal supplied at open time is NOT an operator edit: editor fields stay null."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="Auto-derived objective"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal == "Auto-derived objective"
    # open_version does not go through set_version_goal, so the operator-edit
    # provenance columns remain null until an operator actually edits.
    assert version.goal_updated_at is None
    assert version.goal_editor_info is None


def test_set_version_goal_idempotent_noop():
    """Setting the goal to its current value is a no-op (no timestamp / editor bump)."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        goal="steady goal",
    )
    rv.set_version_goal(version, "steady goal", editor_id="op@example.com")
    # No-op: editor provenance is not stamped because the goal matched.
    assert version.goal == "steady goal"
    assert version.goal_updated_at is None
    assert version.goal_editor_info is None


def test_set_version_goal_truncates_long_goal():
    """A goal longer than the cap is truncated before storage."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    rv.set_version_goal(version, "g" * 5000, editor_id="op@example.com")
    assert version.goal is not None
    assert len(version.goal) == rv._GOAL_MAX


def test_set_version_goal_strategist_source_stamps_strategist_prefix():
    """set_version_goal with source='strategist' records [strategist:id] not [operator:id]."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    rv.set_version_goal(
        version, "Deliver the design-system token layer", editor_id="abc12345", source="strategist"
    )
    assert version.goal == "Deliver the design-system token layer"
    assert version.goal_editor_info == "[strategist:abc12345]"
    assert version.goal_updated_at is not None


def test_set_version_goal_operator_source_default():
    """set_version_goal default source is 'operator' (backward compatible)."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    rv.set_version_goal(version, "Operator objective", editor_id="user@example.com")
    assert version.goal_editor_info == "[operator:user@example.com]"


@pytest.mark.asyncio
async def test_open_version_opens_with_null_goal(db_session):
    """open_version now opens with goal=None; strategist will author it in the first cycle."""
    project = await _make_project(db_session)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    # Versions open with no goal — the strategist sets it later.
    assert version.goal is None
    assert version.goal_editor_info is None


# ===========================================================================
# to_phase_block
# ===========================================================================


def test_to_phase_block_shape():
    """to_phase_block returns the compact dict for the strategic payload."""
    version = ReleaseVersion(
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        phase_changed_at=_utcnow(),
        phase_reasoning="ready to qualify",
        goal="Ship the mobile-responsive admin UI",
        last_qualification={"status": "pending"},
    )
    block = rv.to_phase_block(version)
    assert block["version_number"] == 3
    assert block["phase"] == "stabilize"
    assert block["goal"] == "Ship the mobile-responsive admin UI"
    assert block["phase_changed_at"] is not None
    assert block["phase_reasoning"] == "ready to qualify"
    assert block["last_qualification"] == {"status": "pending"}


def test_to_phase_block_phase_reasoning_none_when_absent():
    """A version with no phase_reasoning / goal yields None for those keys."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert block["phase_reasoning"] is None
    assert block["goal"] is None


def test_to_phase_block_handles_none_phase_changed_at():
    """A version with no phase_changed_at yields None for that key."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert block["phase_changed_at"] is None


def test_to_phase_block_carries_qualification_state():
    """to_phase_block exposes the disambiguated qualification_state alongside the
    raw last_qualification (incident 2026-06-02: raw status='pending' was
    ambiguous)."""
    version = ReleaseVersion(
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={"status": "pending"},
    )
    block = rv.to_phase_block(version)
    # raw status stays untouched; the derived sub-state is added.
    assert block["last_qualification"] == {"status": "pending"}
    assert block["qualification_state"]["state"] == "qualifying"


def test_to_phase_block_carries_promotion_freshness():
    """The strategic version_phase payload inherits the in-flight freshness keys
    via the shared qualification_state pass-through."""
    enabled_at = (_utcnow() - timedelta(minutes=10)).isoformat()
    version = ReleaseVersion(
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "auto_merge_enabled": True,
                "enabled_at": enabled_at,
            },
        },
    )
    qstate = rv.to_phase_block(version)["qualification_state"]
    assert qstate["state"] == "promotion_in_flight"
    assert qstate["promotion_enabled_at"] == enabled_at
    assert qstate["promotion_age_seconds"] >= 590
    assert "promotion_past_timeout" in qstate


def test_to_phase_block_carries_audit_crashed_state():
    """Given a crashed audit child, to_phase_block surfaces the reconciled
    ``audit_crashed`` qualification_state so the strategic payload never tells the
    agent to wait on a dead audit. Default (no audit_child) stays ``qualifying``."""
    version = ReleaseVersion(
        version_number=5,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={"status": "pending"},
    )
    # No audit-child signal: unchanged behavior.
    assert rv.to_phase_block(version)["qualification_state"]["state"] == "qualifying"
    # Crashed audit child: reconciled to audit_crashed.
    block = rv.to_phase_block(version, {"task_status": "FAILED", "report": None})
    assert block["qualification_state"]["state"] == "audit_crashed"
    assert "wait for the verdict" not in block["qualification_state"]["summary"].lower()


def test_to_phase_block_qualification_state_none_when_unqualified():
    """An unqualified version (no verdict) yields qualification_state=None."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        last_qualification=None,
    )
    assert rv.to_phase_block(version)["qualification_state"] is None


def test_to_phase_block_omits_acceptance_aggregate():
    """to_phase_block no longer surfaces the retired acceptance aggregate."""
    version = ReleaseVersion(
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        goal="Ship proven-vs-merged surfacing",
    )
    aggregate = {
        "total": 6,
        "proven": 3,
        "satisfied_unproven": 1,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 1,
        "undeclared": 0,
        "merged_unverified_degraded": 1,
        "malformed": 0,
        "proven_rate": 0.5,
        "satisfied_rate": 0.667,
    }
    version.last_qualification = {"acceptance_aggregate": aggregate}
    block = rv.to_phase_block(version)
    assert "acceptance_aggregate" not in block
    assert block["version_number"] == 3
    assert block["goal"] == "Ship proven-vs-merged surfacing"


def test_to_phase_block_acceptance_aggregate_absent_on_legacy():
    """Legacy phase blocks also omit acceptance_aggregate."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    block = rv.to_phase_block(version)
    assert "acceptance_aggregate" not in block


# ===========================================================================
# build_version_highlights — shared what's-new headline + at-a-glance metrics
# ===========================================================================


def test_build_version_highlights_open_no_merged_work():
    """An open version with zero merged work yields the phase-context
    'no merged work yet' headline and zeroed metrics."""
    version = ReleaseVersion(
        version_number=1,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    h = rv.build_version_highlights(
        version,
        merged_count=0,
        aggregate_serving_status=None,
    )
    assert h["headline"] == "open in build with no merged work yet"
    assert h["merged_count"] == 0
    assert h["proven"] == 0
    assert h["total_merged_for_acceptance"] == 0
    assert h["qualification_confidence"] is None
    assert h["qualification_threshold"] is None


def test_build_version_highlights_open_with_merged():
    """An open version with merged work names the count in its phase context and
    surfaces qualification conf/threshold."""
    version = ReleaseVersion(
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={"confidence": 0.82, "threshold": 0.9},
    )
    h = rv.build_version_highlights(
        version,
        merged_count=3,
        aggregate_serving_status=None,
    )
    assert h["headline"] == "open in stabilize with 3 merged"
    assert h["merged_count"] == 3
    assert h["proven"] == 0
    assert h["total_merged_for_acceptance"] == 0
    assert h["qualification_confidence"] == 0.82
    assert h["qualification_threshold"] == 0.9


def test_build_version_highlights_promoted_and_serving_when_aggregate_confirmed():
    """A promoted version reads 'promoted & serving' only when the AGGREGATE
    serving status is serving_confirmed."""
    version = ReleaseVersion(
        version_number=4,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow(),
    )
    h = rv.build_version_highlights(
        version,
        merged_count=12,
        aggregate_serving_status="serving_confirmed",
    )
    assert h["headline"] == "promoted & serving"


def test_build_version_highlights_promoted_uses_aggregate_not_scalar():
    """A promoted version whose scalar serving_status would say 'serving_confirmed'
    but whose AGGREGATE serving status is deploy_failed reads 'promoted' — the
    helper drives the 'serving' word off the aggregate argument, never the scalar
    column. This is the truthful-state guarantee the shared helper exists to keep."""
    version = ReleaseVersion(
        version_number=5,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow(),
        # Scalar column lies — a naive read would claim serving.
        serving_status="serving_confirmed",
    )
    h = rv.build_version_highlights(
        version,
        merged_count=8,
        aggregate_serving_status="deploy_failed",
    )
    assert h["headline"] == "promoted"


def test_build_version_highlights_promoted_unconfirmed_aggregate_is_just_promoted():
    """A promoted version with an unconfirmed/absent aggregate reads 'promoted',
    not 'promoted & serving'."""
    version = ReleaseVersion(
        version_number=6,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow(),
    )
    for agg in ("unconfirmed", None):
        h = rv.build_version_highlights(
            version,
            merged_count=1,
            aggregate_serving_status=agg,
        )
        assert h["headline"] == "promoted", f"aggregate={agg!r}"


def test_build_version_highlights_abandoned():
    """An abandoned version reads 'abandoned' regardless of merged count or serving."""
    version = ReleaseVersion(
        version_number=7,
        phase="build",
        status="abandoned",
        opened_at=_utcnow(),
    )
    h = rv.build_version_highlights(
        version,
        merged_count=5,
        aggregate_serving_status="serving_confirmed",
    )
    assert h["headline"] == "abandoned"


def test_build_version_highlights_legacy_none_acceptance_omits_metrics():
    """A legacy version omits retired acceptance metrics without crashing."""
    version = ReleaseVersion(
        version_number=8,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        last_qualification=None,
    )
    h = rv.build_version_highlights(
        version,
        merged_count=2,
        aggregate_serving_status=None,
    )
    assert h["proven"] == 0
    assert h["total_merged_for_acceptance"] == 0
    assert h["qualification_confidence"] is None
    assert h["qualification_threshold"] is None


# ===========================================================================
# build_version_highlights — narrative field
# ===========================================================================


def test_build_version_highlights_open_with_merged_has_narrative():
    """An open version with merged work has a non-empty narrative that mentions
    the merged count and phase, and reflects qualification state."""
    version = ReleaseVersion(
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=_utcnow(),
        last_qualification={"confidence": 0.82, "threshold": 0.9},
    )
    h = rv.build_version_highlights(
        version,
        merged_count=3,
        aggregate_serving_status=None,
    )
    narrative = h["narrative"]
    assert isinstance(narrative, str) and narrative, (
        "narrative must be a non-empty string"
    )
    assert "3" in narrative, "narrative should mention merged count"
    assert "stabilize" in narrative, "narrative should mention phase"
    assert "Acceptance evidence" not in narrative
    assert "82%" in narrative or "below" in narrative, (
        "narrative should reflect qualification state"
    )


def test_build_version_highlights_promoted_and_serving_narrative():
    """A promoted-and-serving version has a narrative that mentions promotion and serving."""
    version = ReleaseVersion(
        version_number=4,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow(),
    )
    h = rv.build_version_highlights(
        version,
        merged_count=12,
        aggregate_serving_status="serving_confirmed",
    )
    narrative = h["narrative"]
    assert isinstance(narrative, str) and narrative
    assert "serving" in narrative.lower(), "narrative must mention serving"
    assert "12" in narrative, "narrative should mention merged count"
    assert "Acceptance evidence" not in narrative


def test_build_version_highlights_abandoned_narrative():
    """An abandoned version has a narrative that mentions abandonment and merged count."""
    version = ReleaseVersion(
        version_number=7,
        phase="build",
        status="abandoned",
        opened_at=_utcnow(),
    )
    h = rv.build_version_highlights(
        version,
        merged_count=5,
        aggregate_serving_status="serving_confirmed",
    )
    narrative = h["narrative"]
    assert isinstance(narrative, str) and narrative
    assert "abandon" in narrative.lower(), "narrative must mention abandonment"
    assert "5" in narrative, "narrative should mention merged count"


def test_build_version_highlights_legacy_none_acceptance_narrative():
    """A legacy version with no acceptance aggregate yields a non-empty narrative."""
    version = ReleaseVersion(
        version_number=8,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        last_qualification=None,
    )
    h = rv.build_version_highlights(
        version,
        merged_count=2,
        aggregate_serving_status=None,
    )
    narrative = h["narrative"]
    assert isinstance(narrative, str) and narrative
    assert "build" in narrative.lower() or "2" in narrative


def test_build_version_highlights_narrative_always_present():
    """narrative key is always in the returned dict regardless of version state."""
    for status, merged in [
        ("open", 0),
        ("open", 5),
        ("promoted", 10),
        ("abandoned", 3),
    ]:
        version = ReleaseVersion(
            version_number=1,
            phase="build",
            status=status,
            opened_at=_utcnow(),
        )
        h = rv.build_version_highlights(
            version,
            merged_count=merged,
            aggregate_serving_status=None,
        )
        assert "narrative" in h, f"narrative key missing for status={status}"
        assert isinstance(h["narrative"], str) and h["narrative"], (
            f"narrative must be non-empty string for status={status}"
        )


# ===========================================================================
# summarize_qualification_state — disambiguates the overloaded status="pending"
# ===========================================================================


def test_qual_state_none_when_no_verdict():
    assert rv.summarize_qualification_state(None) is None
    assert rv.summarize_qualification_state({}) is None
    assert rv.summarize_qualification_state({"confidence": 0.9}) is None


def test_qual_state_pending_audit_is_qualifying():
    """Bare status='pending' (no promotion_pending) = audit still scoring."""
    state = rv.summarize_qualification_state({"status": "pending"})
    assert state["state"] == "qualifying"
    assert "audit" in state["summary"].lower()


def test_qual_state_pending_with_promotion_is_promotion_in_flight():
    """status='pending' WITH a promotion_pending sub-key = audit passed, the
    promotion PR is open — the case the agent misread as 'still auditing'."""
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "auto_merge_enabled": True,
            },
        }
    )
    assert state["state"] == "promotion_in_flight"
    assert state["promotion_pr_number"] == 7357
    assert state["promotion_pr_url"] == "https://example/pr/7357"
    # The summary must NOT tell the agent the audit is still running.
    assert "passed" in state["summary"].lower()
    assert "armed" in state["summary"].lower()


def test_summarize_qualification_state_audit_crashed_overrides_pending():
    """A pending verdict whose audit child crashed (FAILED/CANCELLED, no report)
    must reconcile to ``audit_crashed`` — NOT the misleading ``qualifying``/'wait
    for the verdict' — so the panel badge and the audit-findings section agree.
    """
    for crashed_status in ("FAILED", "CANCELLED"):
        state = rv.summarize_qualification_state(
            {"status": "pending"},
            audit_child={"task_status": crashed_status, "report": None},
        )
        assert state["state"] == "audit_crashed", crashed_status
        # The reconciled summary must NOT tell the reader to wait for the verdict.
        summary = state["summary"].lower()
        assert "wait for the verdict" not in summary, crashed_status
        assert "crashed" in summary, crashed_status

    # An unknown terminal status with no report is also a crash (matches the
    # frontend default branch).
    state = rv.summarize_qualification_state(
        {"status": "pending"},
        audit_child={"task_status": "ABORTED", "report": None},
    )
    assert state["state"] == "audit_crashed"


def test_qual_state_pending_no_audit_signal_stays_qualifying():
    """Without an audit-child signal (default None), behavior is unchanged."""
    state = rv.summarize_qualification_state({"status": "pending"})
    assert state["state"] == "qualifying"
    # A crashed-but-with-report audit is NOT a crashed-no-report case.
    state = rv.summarize_qualification_state(
        {"status": "pending"},
        audit_child={"task_status": "FAILED", "report": {"score": 0.9}},
    )
    assert state["state"] == "qualifying"


def test_summarize_qualification_state_bare_promotion_claim_blocks():
    """A bare promotion CLAIM must be recognized as a promotion-blocking state.

    promote() writes ``promotion_pending = {claiming: True, candidate_sha, ...}``
    under the row lock BEFORE releasing it for its 30-120s of GitHub I/O, so a
    concurrent re-qualify can't clobber the in-flight promotion. The claim has no
    ``pr_number`` yet (the PR isn't open), so summarize must still classify it as
    ``promotion_in_flight`` ∈ ``PROMOTION_BLOCKING_STATES`` — that is what makes
    Guard 2b (``_promotion_in_flight_result``) and the version-cut lever refuse a
    concurrent qualify from the instant the claim commits. If this regressed to
    ``qualifying`` the clobber race (PR #12492) would reopen."""
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "promotion_pending": {
                "claiming": True,
                "candidate_sha": "abcd" * 10,
                "enabled_at": _utcnow().isoformat(),
            },
        }
    )
    assert state is not None
    assert state["state"] == "promotion_in_flight"
    assert state["state"] in rv.PROMOTION_BLOCKING_STATES
    # No PR exists yet — the claim precedes the promotion PR being opened.
    assert state.get("promotion_pr_number") is None


def test_qual_state_pending_running_audit_child_stays_qualifying():
    """A non-terminal audit child (still running) must keep ``qualifying``."""
    for running in ("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"):
        state = rv.summarize_qualification_state(
            {"status": "pending"},
            audit_child={"task_status": running, "report": None},
        )
        assert state["state"] == "qualifying", running


def test_qual_state_pending_blocked_and_completed_not_crashed():
    """BLOCKED (waiting on a precondition) and COMPLETED-without-report (a
    finished-but-empty anomaly) must NOT be misclassified as crashed."""
    for non_crash in ("BLOCKED", "COMPLETED"):
        state = rv.summarize_qualification_state(
            {"status": "pending"},
            audit_child={"task_status": non_crash, "report": None},
        )
        assert state["state"] == "qualifying", non_crash


def test_qual_state_running_smoketest_does_not_mask_crashed_audit():
    """When the audit child has already crashed, the reconciled crash state must
    win even though finalization legitimately still waits on the smoketest — the
    audit-child signal we thread in describes the audit, and a crashed audit is
    reported as crashed regardless of a still-running sibling."""
    state = rv.summarize_qualification_state(
        {"status": "pending"},
        audit_child={"task_status": "FAILED", "report": None},
    )
    assert state["state"] == "audit_crashed"


def test_qual_state_promotion_in_flight_freshness_keys_when_enabled_at_fresh():
    """A fresh enabled_at yields additive age / auto-timeout context without
    disturbing the existing state name or PR keys."""
    enabled_at = (_utcnow() - timedelta(minutes=5)).isoformat()
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "auto_merge_enabled": True,
                "enabled_at": enabled_at,
            },
        }
    )
    assert state["state"] == "promotion_in_flight"
    assert state["promotion_pr_number"] == 7357
    assert state["promotion_enabled_at"] == enabled_at
    assert state["promotion_age_seconds"] >= 290  # ~5 min, allow clock slack
    assert state["promotion_age_seconds"] < 600
    assert state["promotion_timeout_seconds"] > 0
    assert state["promotion_past_timeout"] is False
    assert state["promotion_nearing_timeout"] is False
    # The freshness is reflected in the human summary.
    assert "in flight" in state["summary"].lower()


def test_qual_state_promotion_in_flight_past_timeout():
    """An enabled_at older than the cap flags promotion_past_timeout."""
    from app import release_reconciliation

    cap = release_reconciliation._promotion_pending_timeout_seconds()
    enabled_at = (_utcnow() - timedelta(seconds=cap + 3600)).isoformat()
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "auto_merge_enabled": True,
                "enabled_at": enabled_at,
            },
        }
    )
    assert state["promotion_past_timeout"] is True
    assert state["promotion_nearing_timeout"] is False
    assert "passed the auto-timeout" in state["summary"].lower()


def test_qual_state_promotion_in_flight_nearing_timeout():
    """An enabled_at past 80% of the cap (but under it) flags nearing."""
    from app import release_reconciliation

    cap = release_reconciliation._promotion_pending_timeout_seconds()
    # 90% of the cap — nearing but not past.
    enabled_at = (_utcnow() - timedelta(seconds=cap * 0.9)).isoformat()
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "auto_merge_enabled": True,
                "enabled_at": enabled_at,
            },
        }
    )
    assert state["promotion_past_timeout"] is False
    assert state["promotion_nearing_timeout"] is True
    assert "nearing the auto-timeout" in state["summary"].lower()


def test_qual_state_promotion_in_flight_respects_runtime_cap_override(monkeypatch):
    """The cap is the same configurable one the reconciliation sweep uses."""
    from app.shared_state import runtime_config

    monkeypatch.setitem(runtime_config, "promotion_pending_timeout_seconds", 600.0)
    enabled_at = (_utcnow() - timedelta(seconds=700)).isoformat()
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "auto_merge_enabled": True,
                "enabled_at": enabled_at,
            },
        }
    )
    assert state["promotion_timeout_seconds"] == 600.0
    assert state["promotion_past_timeout"] is True


def test_qual_state_promotion_in_flight_missing_enabled_at_is_timeless():
    """Missing enabled_at falls back to the prior keyless behavior, no error."""
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://example/pr/7357",
                "auto_merge_enabled": True,
            },
        }
    )
    assert state["state"] == "promotion_in_flight"
    assert state["promotion_pr_number"] == 7357
    for key in rv._PROMOTION_FRESHNESS_KEYS:
        assert key not in state


def test_qual_state_promotion_in_flight_invalid_enabled_at_is_timeless():
    """An unparseable enabled_at falls back to keyless behavior, no error."""
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "auto_merge_enabled": True,
                "enabled_at": "not-a-timestamp",
            },
        }
    )
    assert state["state"] == "promotion_in_flight"
    for key in rv._PROMOTION_FRESHNESS_KEYS:
        assert key not in state


def test_format_promotion_freshness_phrasing():
    """The shared 409 suffix formatter renders age + timeout state, '' when bare."""
    assert rv.format_promotion_freshness(None) == ""
    assert rv.format_promotion_freshness({"promotion_pr_number": 1}) == ""
    past = rv.format_promotion_freshness(
        {"promotion_age_seconds": 7200, "promotion_past_timeout": True}
    )
    assert "in flight ~2h" in past
    assert "past auto-timeout" in past
    nearing = rv.format_promotion_freshness(
        {"promotion_age_seconds": 3600, "promotion_nearing_timeout": True}
    )
    assert "nearing auto-timeout" in nearing


def test_qual_state_promotion_failed_surfaces_reason():
    state = rv.summarize_qualification_state(
        {"status": "promotion_failed", "error": "pr_ci_red", "detail": "shard 8"}
    )
    assert state["state"] == "promotion_failed"
    assert state["error"] == "pr_ci_red"
    assert "pr_ci_red" in state["summary"]
    assert "fresh qualify" in state["summary"].lower()


def test_qual_state_managed_check_wait_is_distinct_state():
    """status='pending' WITH a managed_check_wait promotion_pending = a
    migration-carrying PR waiting on required checks; Blueprint retries the merge
    automatically. This is a DISTINCT state from native auto-merge — the copy
    must say it is waiting for checks and does NOT need a fresh qualification."""
    enabled_at = (_utcnow() - timedelta(minutes=10)).isoformat()
    state = rv.summarize_qualification_state(
        {
            "status": "pending",
            "promotion_pending": {
                "pr_number": 9191,
                "pr_url": "https://example/pr/9191",
                "auto_merge_enabled": False,
                "migration_carrying": True,
                "managed_check_wait": True,
                "enabled_at": enabled_at,
                "ci_state": "pending",
                "check_snapshot": {
                    "state": "pending",
                    "classified_at": enabled_at,
                    "head_sha": "cafebabe" * 5,
                    "total_checks": 1,
                    "checks": [
                        {
                            "name": "CI / Backend",
                            "status": "in_progress",
                        }
                    ],
                    "truncated": False,
                },
            },
        }
    )
    assert state["state"] == "promotion_check_wait"
    assert state["promotion_pr_number"] == 9191
    assert state["promotion_pr_url"] == "https://example/pr/9191"
    assert state["migration_carrying"] is True
    # The additive CI context is forwarded for operator surfaces.
    assert state["ci_state"] == "pending"
    assert state["check_snapshot"]["state"] == "pending"
    assert state["check_snapshot"]["head_sha"] == "cafebabe" * 5
    assert state["check_snapshot"]["checks"] == [
        {"name": "CI / Backend", "status": "in_progress"}
    ]
    # Freshness keys are present (same machinery as in-flight).
    assert state["promotion_enabled_at"] == enabled_at
    assert state["promotion_age_seconds"] >= 590
    # Copy must explicitly say waiting for required checks + auto-retry, and must
    # NOT tell the reader a fresh qualification is needed.
    lowered = state["summary"].lower()
    assert "required" in lowered and "check" in lowered
    assert "automatically" in lowered
    assert "does not need a fresh qualification" in lowered


def test_qual_state_required_check_failed_surfaces_failing_names():
    """A promotion_failed with error='required_check_failed' classifies as a
    distinct state and surfaces the failing check names from detail."""
    state = rv.summarize_qualification_state(
        {
            "status": "promotion_failed",
            "error": "required_check_failed",
            "detail": "PR #9191 required checks failed: CI / Backend, CI / Lint",
        }
    )
    assert state["state"] == "required_check_failed"
    assert state["error"] == "required_check_failed"
    assert "CI / Backend" in state["detail"]
    # The failing names are surfaced in the human summary.
    assert "CI / Backend" in state["summary"]
    assert "required" in state["summary"].lower()


def test_qual_state_protection_blocked_review_says_human_action():
    """A promotion_failed with error='protection_blocked_review' classifies as a
    distinct state and tells the reader a human action is required and that
    re-qualifying will not help."""
    state = rv.summarize_qualification_state(
        {
            "status": "promotion_failed",
            "error": "protection_blocked_review",
            "detail": "At least 1 approving review is required",
        }
    )
    assert state["state"] == "protection_blocked_review"
    assert state["error"] == "protection_blocked_review"
    lowered = state["summary"].lower()
    assert "human" in lowered
    assert "re-qualifying will not help" in lowered


def test_qual_state_generic_promotion_failed_unchanged_for_other_errors():
    """An ordinary promotion failure (non check/protection error) stays the
    generic promotion_failed state — the split only carves out the two managed
    check-wait blocker errors."""
    state = rv.summarize_qualification_state(
        {"status": "promotion_failed", "error": "merge_blocked", "detail": "conflict"}
    )
    assert state["state"] == "promotion_failed"
    assert state["error"] == "merge_blocked"
    assert "fresh qualify" in state["summary"].lower()


def test_promotion_blocking_states_set_contents():
    """The exported blocking set names exactly the two mid-promotion holds so
    every operator guard agrees by construction."""
    assert rv.PROMOTION_BLOCKING_STATES == frozenset(
        {"promotion_in_flight", "promotion_check_wait"}
    )


def test_qual_state_not_promoted_includes_scores():
    state = rv.summarize_qualification_state(
        {
            "status": "not_promoted",
            "confidence": 0.62,
            "threshold": 0.9,
            "blocker": "audit",
        }
    )
    assert state["state"] == "not_promoted"
    assert state["blocker"] == "audit"
    assert "0.62" in state["summary"]


def test_qual_state_legacy_smoke_rejection_is_stale_and_archival():
    state = rv.summarize_qualification_state(
        {
            "status": "not_promoted",
            "smoketest_enabled": True,
            "blocker": "smoketest_startup_failed",
            "detail": {"legacy": "timeout"},
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 0.0},
        }
    )
    assert state["state"] == "not_promoted"
    assert state["stale"] is True
    assert state["stale_reason"] == "retired_qualifier_smoketest"
    assert "re-qualify" in state["summary"].lower()
    assert "retired" in state["summary"].lower()
    assert state["blocker"] == "smoketest_startup_failed"
    assert state["detail"] == {"legacy": "timeout"}
    assert state["sub_scores"]["smoketest"] == 0.0


def test_qual_state_dry_run_is_promotion_ready():
    state = rv.summarize_qualification_state({"status": "dry_run"})
    assert state["state"] == "dry_run"
    assert "promotion-ready" in state["summary"].lower()


def test_qual_state_promoted():
    state = rv.summarize_qualification_state({"status": "promoted"})
    assert state["state"] == "promoted"


def test_qual_state_deferred_unsynced():
    """The qualify-on-synced gate's deferral renders as an explicit, no-action
    sub-state (not the bare forward-compat fallback)."""
    state = rv.summarize_qualification_state(
        {"status": "deferred_unsynced", "candidate_sha": "abc"}
    )
    assert state["state"] == "deferred_unsynced"
    assert "deferred" in state["summary"].lower()
    assert "auto-retries" in state["summary"].lower()


def test_qual_state_unknown_status_passes_through():
    """Forward-compat: an unrecognized status is echoed, never crashes."""
    state = rv.summarize_qualification_state({"status": "some_new_status"})
    assert state["state"] == "some_new_status"


def test_qual_state_error_no_credentials_is_in_vocab():
    """status='error' with error='no_credentials' returns state='error' ∈ vocab."""
    state = rv.summarize_qualification_state(
        {"status": "error", "error": "no_credentials"}
    )
    assert state is not None
    assert state["state"] == "error"
    assert state["state"] in rv.QUALIFICATION_STATE_VOCAB
    assert state["error"] == "no_credentials"
    # Summary must mention credentials / config problem, not a passing audit.
    assert "credentials" in state["summary"].lower() or "config" in state["summary"].lower()


def test_qual_state_error_forwards_sub_reason_and_detail():
    """status='error' forwards the error sub-reason and detail into the returned dict."""
    state = rv.summarize_qualification_state(
        {"status": "error", "error": "sha_error", "detail": "ref not found"}
    )
    assert state is not None
    assert state["state"] == "error"
    assert state["error"] == "sha_error"
    assert state["detail"] == "ref not found"
    assert "ref not found" in state["summary"]


def test_qual_state_error_spawn_failed_is_in_vocab():
    """status='error' with error='spawn_failed' returns state='error' ∈ vocab."""
    state = rv.summarize_qualification_state(
        {"status": "error", "error": "spawn_failed"}
    )
    assert state is not None
    assert state["state"] == "error"
    assert state["state"] in rv.QUALIFICATION_STATE_VOCAB


def test_qual_state_infra_failure_is_in_vocab():
    """status='infra_failure' returns state='infra_failure' ∈ vocab."""
    state = rv.summarize_qualification_state({"status": "infra_failure"})
    assert state is not None
    assert state["state"] == "infra_failure"
    assert state["state"] in rv.QUALIFICATION_STATE_VOCAB
    # Summary must make clear this is non-conclusive and re-qualify is needed.
    lowered = state["summary"].lower()
    assert "non-conclusive" in lowered or "conclusive" in lowered
    assert "re-qualify" in lowered


def test_qual_state_infra_failure_forwards_detail():
    """status='infra_failure' forwards detail into the returned dict and summary."""
    state = rv.summarize_qualification_state(
        {"status": "infra_failure", "detail": "executor OOM"}
    )
    assert state is not None
    assert state["state"] == "infra_failure"
    assert state["detail"] == "executor OOM"
    assert "executor OOM" in state["summary"]


def test_qual_state_rubric_changed_is_recoverable_hold():
    """status='rubric_changed' returns typed state ∈ vocab with recoverable hold summary."""
    state = rv.summarize_qualification_state(
        {
            "status": "rubric_changed",
            "blocker": "rubric_changed",
            "rubric_drift": {"added": ["crit-a"], "removed": []},
            "rubric_provenance_error": None,
        }
    )
    assert state is not None
    assert state["state"] == "rubric_changed"
    assert state["state"] in rv.QUALIFICATION_STATE_VOCAB
    # Summary must read as a recoverable hold, not a hard failure.
    lowered = state["summary"].lower()
    assert "recoverable" in lowered or "fresh" in lowered
    assert "rubric" in lowered
    # Context fields must be forwarded when present.
    assert state["blocker"] == "rubric_changed"
    assert state["rubric_drift"] == {"added": ["crit-a"], "removed": []}


def test_qual_state_rubric_changed_absent_context_fields():
    """rubric_drift / rubric_provenance_error absent on input → None in result."""
    state = rv.summarize_qualification_state({"status": "rubric_changed"})
    assert state is not None
    assert state["state"] == "rubric_changed"
    assert state["rubric_drift"] is None
    assert state["rubric_provenance_error"] is None


def test_qual_state_rubric_provenance_unknown_is_recoverable_hold():
    """status='rubric_provenance_unknown' returns typed state ∈ vocab with recoverable hold summary."""
    state = rv.summarize_qualification_state(
        {
            "status": "rubric_provenance_unknown",
            "blocker": "rubric_provenance_unknown",
            "rubric_provenance_error": "criteria snapshot missing from audit record",
            "rubric_drift": None,
        }
    )
    assert state is not None
    assert state["state"] == "rubric_provenance_unknown"
    assert state["state"] in rv.QUALIFICATION_STATE_VOCAB
    # Summary must read as a recoverable hold, not a hard failure.
    lowered = state["summary"].lower()
    assert "recoverable" in lowered or "fresh" in lowered
    assert "provenance" in lowered or "rubric" in lowered
    # Context fields must be forwarded when present.
    assert state["blocker"] == "rubric_provenance_unknown"
    assert state["rubric_provenance_error"] == "criteria snapshot missing from audit record"


def test_qual_state_rubric_provenance_unknown_absent_context_fields():
    """rubric_drift / rubric_provenance_error absent on input → None in result."""
    state = rv.summarize_qualification_state({"status": "rubric_provenance_unknown"})
    assert state is not None
    assert state["state"] == "rubric_provenance_unknown"
    assert state["rubric_drift"] is None
    assert state["rubric_provenance_error"] is None


# ===========================================================================
# TestQualificationStateVocab — pin QUALIFICATION_STATE_VOCAB contract
# ===========================================================================


class TestQualificationStateVocab:
    """Pin the QUALIFICATION_STATE_VOCAB constant and its relationship to
    summarize_qualification_state return values."""

    def test_is_frozenset_of_strings(self):
        vocab = rv.QUALIFICATION_STATE_VOCAB
        assert isinstance(vocab, frozenset)
        assert vocab, "vocab must not be empty"
        assert all(isinstance(s, str) for s in vocab)

    def test_contains_all_states(self):
        fixture_path = (
            Path(__file__).parent / "__fixtures__" / "qualification_state_vocab.json"
        )
        fixture = json.loads(fixture_path.read_text())
        expected = frozenset(fixture["states"])
        assert rv.QUALIFICATION_STATE_VOCAB == expected, (
            f"QUALIFICATION_STATE_VOCAB does not match fixture. "
            f"Extra in vocab: {rv.QUALIFICATION_STATE_VOCAB - expected}. "
            f"Missing from vocab: {expected - rv.QUALIFICATION_STATE_VOCAB}."
        )

    def test_promotion_blocking_states_subset_of_vocab(self):
        assert rv.PROMOTION_BLOCKING_STATES <= rv.QUALIFICATION_STATE_VOCAB

    @pytest.mark.parametrize(
        "verdict,kwargs",
        [
            # qualifying — pending with no promotion_pending
            ({"status": "pending"}, {}),
            # audit_crashed — pending with a crashed audit child
            (
                {"status": "pending"},
                {"audit_child": {"task_status": "FAILED", "report": None}},
            ),
            # promotion_check_wait — pending with managed_check_wait promo
            (
                {
                    "status": "pending",
                    "promotion_pending": {"managed_check_wait": True},
                },
                {},
            ),
            # promotion_in_flight — pending with non-managed promo
            (
                {
                    "status": "pending",
                    "promotion_pending": {"auto_merge_enabled": True},
                },
                {},
            ),
            # required_check_failed
            (
                {"status": "promotion_failed", "error": "required_check_failed"},
                {},
            ),
            # protection_blocked_review
            (
                {"status": "promotion_failed", "error": "protection_blocked_review"},
                {},
            ),
            # promotion_failed (generic)
            ({"status": "promotion_failed", "error": "merge_conflict"}, {}),
            # not_promoted
            ({"status": "not_promoted", "confidence": 0.5, "threshold": 0.9}, {}),
            # dry_run
            ({"status": "dry_run"}, {}),
            # promoted (no serving info)
            ({"status": "promoted"}, {}),
            # promoted_unconfirmed
            ({"status": "promoted"}, {"serving_status": "unconfirmed"}),
            # promotion_deploy_failed
            ({"status": "promoted"}, {"serving_status": "deploy_failed"}),
            # promotion_confirmation_timed_out
            (
                {"status": "promoted"},
                {"serving_status": "confirmation_timed_out"},
            ),
            # deferred_unsynced
            ({"status": "deferred_unsynced"}, {}),
            # error — no_credentials sub-reason
            ({"status": "error", "error": "no_credentials"}, {}),
            # error — sha_error sub-reason
            ({"status": "error", "error": "sha_error"}, {}),
            # error — spawn_failed sub-reason
            ({"status": "error", "error": "spawn_failed"}, {}),
            # error — unknown sub-reason (generic fallback)
            ({"status": "error", "error": "unknown_reason"}, {}),
            # infra_failure
            ({"status": "infra_failure"}, {}),
        ],
    )
    def test_returned_state_is_in_vocab(self, verdict, kwargs):
        """Every distinct code path returns a state that is in QUALIFICATION_STATE_VOCAB."""
        result = rv.summarize_qualification_state(verdict, **kwargs)
        assert result is not None, "expected a non-None result for this verdict"
        state = result.get("state")
        assert state in rv.QUALIFICATION_STATE_VOCAB, (
            f"state={state!r} is not in QUALIFICATION_STATE_VOCAB"
        )

    def test_prompt_suffix_hint_states_subset_of_vocab(self):
        """The 5-state prompt-suffix set in strategic_spawning stays inside vocab."""
        hint_states = {
            "promotion_in_flight",
            "promotion_check_wait",
            "promotion_failed",
            "required_check_failed",
            "protection_blocked_review",
        }
        assert hint_states <= rv.QUALIFICATION_STATE_VOCAB

    def test_status_to_expected_states_map_covers_all_classifier_derivations(self):
        """_QUAL_STATUS_STATE_MAP in strategic_spawning mirrors every state that
        summarize_qualification_state can return for each raw status it covers.

        Drift scenario: a new state is added under an existing status in the
        classifier (e.g. a new "pending" sub-state) without updating the map.
        Every strategic payload then flags a false status_state_mismatch conflict.
        This test exercises all reachable branches for each mapped status and
        asserts:
          (a) each returned state is declared in _QUAL_STATUS_STATE_MAP[status], and
          (b) every declared state in the map is reachable by at least one input
              (no dead entries silently diverging from the classifier).
        """
        # Representative inputs that trigger every branch for each mapped status.
        # Each entry is (raw_status, call_kwargs).
        _pending_inputs = [
            # qualifying — bare pending, no promotion sub-key
            ({"status": "pending"}, {}),
            # audit_crashed — pending with a terminal-failed audit child and no report
            (
                {"status": "pending"},
                {"audit_child": {"task_status": "FAILED", "report": None}},
            ),
            # promotion_check_wait — managed_check_wait promotion
            (
                {"status": "pending", "promotion_pending": {"managed_check_wait": True}},
                {},
            ),
            # promotion_in_flight — normal (non-managed) promotion PR open
            (
                {"status": "pending", "promotion_pending": {"auto_merge_enabled": True}},
                {},
            ),
        ]
        _promotion_failed_inputs = [
            # promotion_failed (generic fallback)
            ({"status": "promotion_failed"}, {}),
            # required_check_failed — specific error sub-reason
            ({"status": "promotion_failed", "error": "required_check_failed"}, {}),
            # protection_blocked_review — specific error sub-reason
            ({"status": "promotion_failed", "error": "protection_blocked_review"}, {}),
        ]
        _promoted_inputs = [
            # promoted — no serving info (bare)
            ({"status": "promoted"}, {}),
            # promoted — serving confirmed
            ({"status": "promoted"}, {"serving_status": "serving_confirmed"}),
            # promoted_unconfirmed
            ({"status": "promoted"}, {"serving_status": "unconfirmed"}),
            # promotion_deploy_failed
            ({"status": "promoted"}, {"serving_status": "deploy_failed"}),
            # promotion_confirmation_timed_out
            ({"status": "promoted"}, {"serving_status": "confirmation_timed_out"}),
        ]
        inputs_by_status = {
            "pending": _pending_inputs,
            "promotion_failed": _promotion_failed_inputs,
            "promoted": _promoted_inputs,
        }

        map_under_test = ss._QUAL_STATUS_STATE_MAP
        # Verify the map exists and covers exactly the statuses we have inputs for.
        assert set(map_under_test.keys()) == set(inputs_by_status.keys()), (
            "_QUAL_STATUS_STATE_MAP keys diverged from the inputs_by_status fixture — "
            "update this test when adding or removing a top-level status from the map"
        )

        for raw_status, inputs in inputs_by_status.items():
            declared = map_under_test[raw_status]
            observed: set[str] = set()
            for verdict, kwargs in inputs:
                result = rv.summarize_qualification_state(verdict, **kwargs)
                assert result is not None, (
                    f"summarize_qualification_state returned None for status={raw_status!r}, "
                    f"verdict={verdict!r}, kwargs={kwargs!r} — expected a non-None result"
                )
                state = result.get("state")
                assert state in declared, (
                    f"summarize_qualification_state returned state={state!r} for "
                    f"status={raw_status!r} which is NOT in _QUAL_STATUS_STATE_MAP[{raw_status!r}]="
                    f"{declared!r}. Add the new state to the map or fix the classifier."
                )
                observed.add(state)

            # Every declared state must be reachable — dead entries in the map
            # hide future drift.
            unreachable = declared - observed
            assert not unreachable, (
                f"_QUAL_STATUS_STATE_MAP[{raw_status!r}] declares states that no input "
                f"reached: {unreachable!r}. Either add inputs that exercise them or "
                f"remove dead entries from the map."
            )

    def test_qual_status_state_map_pinned_to_vocab(self):
        """Every state value declared in _QUAL_STATUS_STATE_MAP is a member of
        QUALIFICATION_STATE_VOCAB.

        Drift scenario: QUALIFICATION_STATE_VOCAB is updated (a state is added,
        renamed, or removed) but _QUAL_STATUS_STATE_MAP is not, causing the map
        to reference invalid/outdated states. This test catches that by requiring
        all declared state values to be valid vocab members.

        Note: This test complements test_status_to_expected_states_map_covers_all_
        classifier_derivations by pinning the vocab-membership direction. The
        existing reachability test ensures the map is complete (no dead entries);
        this test ensures the map's values are valid (no drift).
        """
        map_under_test = ss._QUAL_STATUS_STATE_MAP
        declared_states = set()
        for status, states in map_under_test.items():
            for state in states:
                declared_states.add(state)

        # Every declared state must be in the authoritative vocab
        invalid_states = declared_states - rv.QUALIFICATION_STATE_VOCAB
        assert not invalid_states, (
            f"_QUAL_STATUS_STATE_MAP declares states not in QUALIFICATION_STATE_VOCAB: "
            f"{invalid_states!r}. Either (a) update QUALIFICATION_STATE_VOCAB if these "
            f"are legitimate new states from summarize_qualification_state, or (b) remove "
            f"them from _QUAL_STATUS_STATE_MAP if they are no longer produced."
        )

        # Sanity check: the map should declare some states
        assert declared_states, (
            "_QUAL_STATUS_STATE_MAP is empty — expected at least some states to be "
            "declared for the mapped statuses."
        )


# ===========================================================================
# recent_closed_versions
# ===========================================================================


@pytest.mark.asyncio
async def test_recent_closed_versions_returns_empty_for_no_history(db_session):
    """A project with only an open version (no closed history) returns []."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id)
    assert rows == []


@pytest.mark.asyncio
async def test_recent_closed_versions_returns_closed_rows_newest_first(db_session):
    """Closed versions are returned newest-first, open version is excluded."""
    project = await _make_project(db_session)

    older = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=4),
    )
    newer = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=2),
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        opened_at=_utcnow(),
    )
    for v in (older, newer, open_v):
        db_session.add(v)
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id)
    assert len(rows) == 2
    assert rows[0].version_number == 2
    assert rows[1].version_number == 1


@pytest.mark.asyncio
async def test_recent_closed_versions_respects_limit(db_session):
    """Passing limit=2 returns at most 2 rows even with more history."""
    project = await _make_project(db_session)

    for i in range(1, 5):
        db_session.add(
            ReleaseVersion(
                project_id=project.id,
                version_number=i,
                phase="build",
                status="promoted",
                opened_at=_utcnow() - timedelta(hours=10 - i),
            )
        )
    await db_session.commit()

    rows = await rv.recent_closed_versions(db_session, project.id, limit=2)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_recent_closed_versions_scoped_to_project(db_session):
    """Closed versions belonging to another project are not returned."""
    project_a = await _make_project(db_session, name="RV-A")
    project_b = await _make_project(db_session, name="RV-B")

    db_session.add(
        ReleaseVersion(
            project_id=project_a.id,
            version_number=1,
            phase="build",
            status="promoted",
            opened_at=_utcnow(),
        )
    )
    await db_session.commit()

    assert await rv.recent_closed_versions(db_session, project_a.id) != []
    assert await rv.recent_closed_versions(db_session, project_b.id) == []


# ===========================================================================
# ensure_initial_release_version
# ===========================================================================


@pytest.mark.asyncio
async def test_ensure_initial_release_version_creates_stabilize_version(db_session):
    """When no version rows exist and branch != 'main', version 1 is opened in
    the 'stabilize' phase (ready to qualify immediately, not waiting on build)."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        promotion_target="main",
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version is not None
    assert version.version_number == 1
    assert version.phase == "stabilize"
    assert version.status == "open"
    assert version.base_sha == "deadbeef"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_snapshots_goal(db_session):
    """The goal passed to the bootstrap is persisted on version 1."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        goal="Bootstrap the LTS release line",
        promotion_target="main",
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version is not None
    assert version.goal == "Bootstrap the LTS release line"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_main_branch(db_session):
    """integration_branch='main' is degenerate (main…HEAD diff is empty) — no row
    is created and None is returned."""
    project = await _make_project(db_session)
    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="main",
        base_sha="abc123",
        promotion_target="main",
    )
    await db_session.commit()

    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_idempotent_open_exists(db_session):
    """If an open version already exists the function is a no-op and returns None."""
    project = await _make_project(db_session)
    await rv.open_version(db_session, project.id, version_number=1, base_sha="sha1")
    await db_session.commit()

    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="sha2",
        promotion_target="main",
    )
    assert result is None

    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.version_number == 1
    assert open_ver.base_sha == "sha1"


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_history_exists(db_session):
    """If any version row exists (even promoted, not open), return None — the
    lifecycle has already started and bootstrapping would skip gaps."""
    project = await _make_project(db_session)

    promoted = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="build",
        status="promoted",
        opened_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(promoted)
    await db_session.commit()

    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="newsha",
        promotion_target="main",
    )
    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_requires_promotion_target(db_session):
    """Omitting promotion_target raises TypeError — it is a required keyword arg."""
    project = await _make_project(db_session)
    with pytest.raises(TypeError):
        await rv.ensure_initial_release_version(
            db_session,
            project.id,
            integration_branch="automain",
            base_sha="abc123",
        )


@pytest.mark.asyncio
async def test_ensure_initial_release_version_noop_when_branch_equals_promotion_target(
    db_session,
):
    """When integration_branch equals a non-'main' promotion_target the check is
    still degenerate — no row is created and None is returned."""
    project = await _make_project(db_session)
    result = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="deploy",
        base_sha="abc123",
        promotion_target="deploy",
    )
    await db_session.commit()

    assert result is None
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_ensure_initial_release_version_creates_when_promotion_target_differs(
    db_session,
):
    """When promotion_target is not 'main' but integration_branch differs from it,
    version 1 is bootstrapped normally."""
    project = await _make_project(db_session)
    version = await rv.ensure_initial_release_version(
        db_session,
        project.id,
        integration_branch="automain",
        base_sha="deadbeef",
        promotion_target="deploy",
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version is not None
    assert version.version_number == 1
    assert version.phase == "stabilize"
    assert version.status == "open"


# ===========================================================================
# abandon_current_version — operator-initiated version cut
# ===========================================================================


async def _make_open_version_row(
    db_session,
    project,
    *,
    version_number: int = 1,
    base_sha: str | None = "basesha1",
    phase: str = "stabilize",
) -> ReleaseVersion:
    version = ReleaseVersion(
        project_id=project.id,
        version_number=version_number,
        phase=phase,
        status="open",
        base_sha=base_sha,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


@pytest.mark.asyncio
async def test_abandon_current_version_closes_open_and_opens_next(db_session):
    """Abandon flips the open row to status='abandoned' (recording provenance in
    last_qualification) and opens a fresh next-numbered open row in build."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=3)

    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="pivoted away from this candidate",
        operator_id="op@example.com",
    )
    await db_session.commit()

    assert abandoned is not None and new_version is not None
    await db_session.refresh(abandoned)
    await db_session.refresh(new_version)

    # Old row is terminal with provenance recorded in the JSONB store.
    assert abandoned.status == "abandoned"
    assert abandoned.version_number == 3
    rec = abandoned.last_qualification
    assert rec["status"] == "abandoned"
    assert rec["reason"] == "pivoted away from this candidate"
    assert rec["operator"] == "op@example.com"
    assert rec["abandoned_at"]

    # New row is open in build at the next number.
    assert new_version.version_number == 4
    assert new_version.status == "open"
    assert new_version.phase == "build"
    # base_sha falls back to the abandoned row's (no GitHub creds in tests).
    assert new_version.base_sha == "basesha1"

    # current_open_version now returns the new row (old one is closed).
    current = await rv.current_open_version(db_session, project.id)
    assert current.version_number == 4


@pytest.mark.asyncio
async def test_abandon_current_version_noop_when_no_open_row(db_session):
    """With no open version the cut is a NO-OP returning (None, None)."""
    project = await _make_project(db_session)

    result = await rv.abandon_current_version(
        db_session, project.id, reason="x", operator_id="op@example.com"
    )
    await db_session.commit()

    assert result == (None, None)
    # No version row was created.
    assert await rv.current_open_version(db_session, project.id) is None


@pytest.mark.asyncio
async def test_abandon_current_version_noop_when_already_closed(db_session):
    """A version already promoted/abandoned is untouched and no duplicate vN+1
    is opened (double-cut race guard)."""
    project = await _make_project(db_session)
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="promoted",
        base_sha="sha2",
    )
    db_session.add(closed)
    await db_session.commit()

    abandoned, new_version = await rv.abandon_current_version(
        db_session, project.id, reason="late cut", operator_id="op@example.com"
    )
    await db_session.commit()

    assert abandoned is None and new_version is None
    # The promoted row is untouched and remains the only row.
    await db_session.refresh(closed)
    assert closed.status == "promoted"
    all_rows = await rv.recent_closed_versions(db_session, project.id, limit=10)
    assert [v.version_number for v in all_rows] == [2]


@pytest.mark.asyncio
async def test_abandon_stamps_reason_and_operator(db_session):
    """The human reason is mirrored into phase_reasoning with the operator
    convention, and the reason is truncated to the phase-reasoning cap."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=1)

    long_reason = "z" * 800
    abandoned, _new = await rv.abandon_current_version(
        db_session, project.id, reason=long_reason, operator_id="alice@example.com"
    )
    await db_session.commit()
    await db_session.refresh(abandoned)

    assert abandoned.phase_reasoning.startswith(
        "[operator:alice@example.com] abandoned"
    )
    # Truncated to the phase-reasoning cap.
    assert len(abandoned.phase_reasoning) <= rv._PHASE_REASONING_MAX
    assert len(abandoned.last_qualification["reason"]) <= rv._PHASE_REASONING_MAX


@pytest.mark.asyncio
async def test_abandon_without_reason_still_cuts(db_session):
    """A cut with no reason still abandons the version and opens the next, with
    a null reason recorded."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=5)

    abandoned, new_version = await rv.abandon_current_version(
        db_session, project.id, reason=None, operator_id="op@example.com"
    )
    await db_session.commit()
    await db_session.refresh(abandoned)

    assert abandoned.status == "abandoned"
    assert abandoned.last_qualification["reason"] is None
    assert abandoned.phase_reasoning == "[operator:op@example.com] abandoned"
    assert new_version.version_number == 6


@pytest.mark.asyncio
async def test_abandon_idempotent_replay_same_key_does_not_open_further_version(
    db_session,
):
    """A repeated cut carrying the same idempotency key replays the original
    cut's result (does NOT abandon the freshly-opened next version) so the
    version number is not advanced a second time."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=7)

    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="pivot",
        operator_id="op@example.com",
        idempotency_key="cut-key-1",
    )
    await db_session.commit()
    assert abandoned.version_number == 7
    assert new_version.version_number == 8
    # The key is stamped into the abandoned row's provenance (no schema change).
    assert abandoned.last_qualification["idempotency_key"] == "cut-key-1"

    # Repeat with the SAME key: returns the original pair, no second cut.
    replay_abandoned, replay_new = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="pivot",
        operator_id="op@example.com",
        idempotency_key="cut-key-1",
    )
    await db_session.commit()
    assert replay_abandoned.version_number == 7
    assert replay_new.version_number == 8

    # v8 is still open and was NOT abandoned; no v9 was opened.
    current = await rv.current_open_version(db_session, project.id)
    assert current.version_number == 8
    assert current.status == "open"
    all_rows = await rv.recent_closed_versions(db_session, project.id, limit=10)
    assert [v.version_number for v in all_rows] == [7]


@pytest.mark.asyncio
async def test_abandon_activates_staged_directions(db_session):
    """An abandon counts as the awaited version resolving: staged directions
    gated on the abandoned version are activated, not orphaned."""
    project = Project(
        name="RV-Abandon-Staged",
        user_id="rv-user",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-staged",
                "text": "Staged work",
                "status": "staged",
                "staged_for_version": 2,
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _make_open_version_row(db_session, project, version_number=2)

    # The staged-activation step runs in its OWN AsyncSessionLocal session
    # (mirroring the promotion path). Bind the lazy production engine to the
    # test engine so that session reaches the same DB (the per-test teardown
    # fixture may have nulled it from a prior test).
    import app.database as _app_db  # noqa: PLC0415

    _app_db._LazyEngine._engine = db_session.bind

    abandoned, new_version = await rv.abandon_current_version(
        db_session, project.id, reason="cut", operator_id="op@example.com"
    )
    await db_session.commit()

    assert abandoned is not None and new_version.version_number == 3
    # The staged direction (staged_for_version=2 <= 2) was activated.
    await db_session.refresh(project)
    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-staged"]["status"] == "active"


@pytest.mark.asyncio
async def test_late_qualifier_finalize_inert_after_abandon(db_session):
    """A qualifier finalize that lands AFTER the version was abandoned is inert:
    the existing ``status != 'open'`` guard makes it a no-op, leaving the
    abandoned terminal state untouched."""
    from app.release_qualifier import _finalize_locked_version  # noqa: PLC0415

    project = await _make_project(db_session)
    # Simulate a version that was mid-qualification (pending) when abandoned.
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="abandoned",
        base_sha="sha1",
        last_qualification={"status": "pending", "candidate_sha": "sha1"},
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _finalize_locked_version(db_session, version.id, None)

    # The guard skips finalize entirely — no promotion, no status change.
    assert result is None
    await db_session.refresh(version)
    assert version.status == "abandoned"
    # No duplicate next version was opened.
    assert await rv.current_open_version(db_session, project.id) is None


# ===========================================================================
# Idempotency tests — version cuts with idempotency_key
# ===========================================================================


@pytest.mark.asyncio
async def test_abandon_with_idempotency_key_stores_key(db_session):
    """A cut with an idempotency_key stores it in last_qualification for later
    detection of duplicates."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=1)

    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="test cut",
        operator_id="op@example.com",
        idempotency_key="cut-key-123",
    )
    await db_session.commit()
    await db_session.refresh(abandoned)

    # The key is stored in last_qualification.
    assert abandoned.last_qualification.get("idempotency_key") == "cut-key-123"
    assert new_version.version_number == 2


@pytest.mark.asyncio
async def test_abandon_idempotency_duplicate_returns_original_pair(db_session):
    """A repeat cut with the same idempotency_key returns the original
    abandoned/next pair instead of cutting a second version."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=3)

    # First cut with a key.
    abandoned1, new1 = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="original cut",
        operator_id="op@example.com",
        idempotency_key="cut-key-abc",
    )
    await db_session.commit()

    # Simulate the open version v4 being used (what would happen in production).
    # Now retry the SAME cut with the same key.
    abandoned2, new2 = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="retry cut (should be ignored)",
        operator_id="op@example.com",
        idempotency_key="cut-key-abc",
    )
    await db_session.commit()

    # The second call returns the ORIGINAL abandoned/next pair.
    assert abandoned2.version_number == 3
    assert abandoned2.status == "abandoned"
    assert new2.version_number == 4
    # Only one cut was performed; no v5 was created.
    all_versions = await rv.recent_closed_versions(db_session, project.id, limit=10)
    assert [v.version_number for v in all_versions] == [3]


@pytest.mark.asyncio
async def test_abandon_different_idempotency_keys_produce_different_cuts(db_session):
    """Two cuts with different idempotency_keys each produce their own cut
    (no deduplication across different keys)."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=1)

    # First cut.
    abandoned1, new1 = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="first cut",
        operator_id="op@example.com",
        idempotency_key="key-1",
    )
    await db_session.commit()

    # v2 is now open (created by the first cut)
    assert new1.version_number == 2

    # Second cut with a DIFFERENT key (on the now-open v2).
    abandoned2, new2 = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="second cut",
        operator_id="op@example.com",
        idempotency_key="key-2",
    )
    await db_session.commit()

    # Both cuts succeed independently.
    assert abandoned1.version_number == 1
    assert new1.version_number == 2
    assert abandoned2.version_number == 2
    assert new2.version_number == 3
    # Both keys are stored in their respective abandoned versions.
    await db_session.refresh(abandoned1)
    await db_session.refresh(abandoned2)
    assert abandoned1.last_qualification.get("idempotency_key") == "key-1"
    assert abandoned2.last_qualification.get("idempotency_key") == "key-2"


@pytest.mark.asyncio
async def test_abandon_no_idempotency_key_still_works(db_session):
    """Cuts without an idempotency_key (None) work as before: no deduplication,
    no key storage."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=5)

    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="cut without key",
        operator_id="op@example.com",
        idempotency_key=None,
    )
    await db_session.commit()
    await db_session.refresh(abandoned)

    # No key stored.
    assert "idempotency_key" not in (abandoned.last_qualification or {})
    assert new_version.version_number == 6


@pytest.mark.asyncio
async def test_abandon_concurrent_race_guard_independent_of_idempotency(db_session):
    """The concurrent-race guard (re-lock + re-check) still prevents two
    concurrent operators from opening duplicate vN+1, even when using different
    idempotency keys (the race happens before the key check)."""
    project = await _make_project(db_session)
    await _make_open_version_row(db_session, project, version_number=1)

    # Simulate two concurrent cut attempts with different keys hitting the
    # same open version. In production, the second one would re-lock and find
    # the version already closed (status != 'open'), returning (None, None).
    # Here we just verify the race guard works by manually closing the row.
    open_v = await rv.current_open_version(db_session, project.id)
    assert open_v is not None
    open_v.status = "promoted"  # Simulate it being closed by concurrent op.
    db_session.add(open_v)
    await db_session.commit()

    # Now the second cut attempt should find no open version.
    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="late cut attempt",
        operator_id="op@example.com",
        idempotency_key="different-key",
    )

    assert abandoned is None and new_version is None


@pytest.mark.asyncio
async def test_abandon_current_version_blocked_when_promotion_in_flight(db_session):
    """abandon_current_version raises VersionCutBlockedByPromotion when the
    version's last_qualification carries promotion_pending and no finalized
    verdict row exists (true in-flight promotion must still block)."""
    project = await _make_project(db_session)
    version = await _make_open_version_row(db_session, project, version_number=5)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "dead" * 10,
        "promotion_pending": {
            "pr_number": 55,
            "pr_url": "http://pr/55",
            "enabled_at": "2026-07-01T00:00:00",
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    with pytest.raises(rv.VersionCutBlockedByPromotion) as exc_info:
        await rv.abandon_current_version(
            db_session, project.id, reason="operator cut", operator_id="op@example.com"
        )

    assert exc_info.value.promotion_pr_number == 55


@pytest.mark.asyncio
async def test_abandon_current_version_succeeds_when_verdict_finalized(db_session):
    """Stale-pending window: last_qualification has promotion_pending but the
    newest QualificationVerdict row already carries a finalized terminal_status.
    abandon_current_version must NOT raise VersionCutBlockedByPromotion — the
    promotion already resolved, so cutting is allowed."""
    project = await _make_project(db_session)
    version = await _make_open_version_row(db_session, project, version_number=6)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "babe" * 10,
        "promotion_pending": {
            "pr_number": 66,
            "pr_url": "http://pr/66",
            "enabled_at": "2026-07-01T00:00:00",
        },
    }
    db_session.add(version)

    # Finalized verdict already exists — promotion resolved.
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="babe" * 10,
        terminal_status="promoted",
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(version)

    abandoned, new_version = await rv.abandon_current_version(
        db_session,
        project.id,
        reason="cut after promotion resolved",
        operator_id="op@example.com",
    )
    await db_session.commit()

    assert abandoned is not None and new_version is not None
    await db_session.refresh(abandoned)
    assert abandoned.status == "abandoned"


# ===========================================================================
# refresh_open_version_goal_if_auto_derived
# ===========================================================================


@pytest.mark.asyncio
async def test_refresh_auto_derived_goal_updates_when_focus_changes(db_session):
    """Auto-derived goal (goal_editor_info null) is rewritten to new current_focus."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="old focus"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal_editor_info is None

    updated = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, "new focus for v2"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert updated is not None
    assert version.goal == "new focus for v2"
    assert version.goal_updated_at is not None
    assert version.goal_editor_info is None  # still auto-derived


@pytest.mark.asyncio
async def test_refresh_operator_edited_goal_is_not_clobbered(db_session):
    """An operator-edited goal (goal_editor_info non-null) is never overwritten."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="old auto focus"
    )
    await db_session.commit()

    rv.set_version_goal(version, "operator goal", editor_id="op@example.com")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal_editor_info == "[operator:op@example.com]"

    updated = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, "strategic new focus"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert updated is None
    assert version.goal == "operator goal"


@pytest.mark.asyncio
async def test_refresh_no_op_when_focus_already_matches(db_session):
    """No update when current_focus already matches the stored goal."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="same focus"
    )
    await db_session.commit()
    await db_session.refresh(version)

    original_updated_at = version.goal_updated_at

    updated = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, "same focus"
    )
    await db_session.commit()

    assert updated is None
    await db_session.refresh(version)
    assert version.goal == "same focus"
    assert version.goal_updated_at == original_updated_at


@pytest.mark.asyncio
async def test_refresh_no_op_when_no_open_version(db_session):
    """Returns None without error when no open version exists."""
    project = await _make_project(db_session)
    # No open version — just confirm no error is raised.
    result = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, "some focus"
    )
    assert result is None


@pytest.mark.asyncio
async def test_refresh_no_op_when_focus_is_blank(db_session):
    """Blank or None current_focus is treated as a no-op."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="existing goal"
    )
    await db_session.commit()

    result_none = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, None
    )
    result_blank = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, "   "
    )
    assert result_none is None
    assert result_blank is None
    await db_session.refresh(version)
    assert version.goal == "existing goal"


@pytest.mark.asyncio
async def test_refresh_savepoint_keeps_outer_transaction_usable(db_session):
    """A DB failure during the refresh must not poison the caller's transaction."""
    project = await _make_project(db_session)
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="old focus"
    )
    await db_session.commit()
    await db_session.refresh(version)

    original_execute = db_session.execute
    poisoned = False

    async def _poison_then_fail(statement, *args, **kwargs):
        nonlocal poisoned
        if not poisoned:
            poisoned = True
            dup_id = uuid.uuid4()
            db_session.add(
                Task(
                    id=dup_id,
                    project_id=project.id,
                    title="poison-refresh",
                    payloads=[{"type": "create", "prompt": "x"}],
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
            )
            db_session.add(
                Task(
                    id=dup_id,
                    project_id=project.id,
                    title="poison-refresh",
                    payloads=[{"type": "create", "prompt": "x"}],
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
            )
            with contextlib.suppress(IntegrityError):
                await db_session.flush()
        return await original_execute(statement, *args, **kwargs)

    with patch.object(db_session, "execute", new=_poison_then_fail):
        updated = await rv.refresh_open_version_goal_if_auto_derived(
            db_session, project.id, "new focus"
        )

    assert updated is None
    await db_session.refresh(version)
    assert version.goal == "old focus"

    project.name = "still-usable"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    assert project.name == "still-usable"


# ===========================================================================
# version goal freeze-at-open contract
# ===========================================================================


@pytest.mark.asyncio
async def test_version_goal_frozen_after_open_not_refreshed_mid_version(db_session):
    """version.goal is frozen at open_version() and must not be mutated mid-version.

    Strategic cycles previously called refresh_open_version_goal_if_auto_derived,
    causing the goal to track the strategist's running current_focus and turn into
    an operational status log. That call was removed; this test documents the
    freeze-at-open contract: the goal stays at its initial mission statement.
    """
    project = await _make_project(db_session)
    mission = "Deliver design-system @theme token substrate and ESLint enforcement"
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal=mission
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal == mission
    assert version.goal_updated_at is None

    # Verify the function itself is callable (it's preserved for operator paths
    # like admin_agent_ops). Calling it here simulates the OLD strategic-cycle
    # behavior; the fix is that strategic_completion.py no longer calls it.
    status_log = (
        "v18 re-qualifying at HEAD abc123 after blocked_no_merge_grade failure. "
        "All v18 deliverables confirmed complete: design-system tokens, Badge "
        "primitives, full text-[Npx] migration, ESLint enforcement."
    )
    result = await rv.refresh_open_version_goal_if_auto_derived(
        db_session, project.id, status_log
    )
    assert result is not None, (
        "refresh_open_version_goal_if_auto_derived must still work when called "
        "directly (operator paths rely on it) — the freeze is enforced by NOT "
        "calling it from strategic_completion.py"
    )
    await db_session.rollback()

    # After rollback (simulating strategic_completion NOT calling refresh),
    # the goal must still be the original mission statement.
    await db_session.refresh(version)
    assert version.goal == mission, (
        f"version.goal was mutated mid-version from {mission!r} — "
        "strategic cycles must not call refresh_open_version_goal_if_auto_derived"
    )
    assert version.goal_updated_at is None


# ===========================================================================
# list_release_versions endpoint — paginated full-history browse
# ===========================================================================


async def _make_member(db_session, project, *, user_id="rv-user", role="viewer"):
    """Create a ProjectMember so the access check on the endpoint passes."""
    from app.models import ProjectMember

    member = ProjectMember(project_id=project.id, user_id=user_id, role=role)
    db_session.add(member)
    await db_session.commit()
    return member


async def _make_version(
    db_session, project, *, version_number, status, opened_at, phase="build"
):
    version = ReleaseVersion(
        project_id=project.id,
        version_number=version_number,
        status=status,
        phase=phase,
        opened_at=opened_at,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


async def _seed_history(db_session, project):
    """Seed 3 closed versions + 1 open, opened_at increasing with the number."""
    base = _utcnow()
    await _make_version(
        db_session,
        project,
        version_number=1,
        status="promoted",
        opened_at=base - timedelta(days=4),
    )
    await _make_version(
        db_session,
        project,
        version_number=2,
        status="abandoned",
        opened_at=base - timedelta(days=3),
    )
    await _make_version(
        db_session,
        project,
        version_number=3,
        status="promoted",
        opened_at=base - timedelta(days=2),
    )
    await _make_version(
        db_session,
        project,
        version_number=4,
        status="open",
        opened_at=base - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_list_release_versions_pagination(db_session):
    """The endpoint returns the full history newest-first, with limit/offset/status
    honoured and a pre-pagination ``total``."""
    from app.routers.admin_release_version import list_release_versions

    project = await _make_project(db_session, name="RV-List")
    await _make_member(db_session, project, user_id="rv-user", role="viewer")
    current_user = {"user_id": "rv-user"}

    await _seed_history(db_session, project)

    # All versions, newest-first; envelope shape is exactly the four keys.
    resp = await list_release_versions(
        project_id=project.id,
        limit=20,
        offset=0,
        status=None,
        session=db_session,
        current_user=current_user,
    )
    assert set(resp.keys()) == {"versions", "total", "limit", "offset"}
    assert resp["total"] == 4
    assert resp["limit"] == 20
    assert resp["offset"] == 0
    assert [v["version_number"] for v in resp["versions"]] == [4, 3, 2, 1]
    # Each entry carries the rich serialization fields.
    assert "highlights" in resp["versions"][0]
    assert "merged_count" in resp["versions"][0]
    assert "serving" in resp["versions"][0]

    # limit is respected (only the 2 newest returned, total unchanged).
    resp_limit = await list_release_versions(
        project_id=project.id,
        limit=2,
        offset=0,
        status=None,
        session=db_session,
        current_user=current_user,
    )
    assert resp_limit["total"] == 4
    assert [v["version_number"] for v in resp_limit["versions"]] == [4, 3]

    # offset skips the first rows.
    resp_offset = await list_release_versions(
        project_id=project.id,
        limit=2,
        offset=2,
        status=None,
        session=db_session,
        current_user=current_user,
    )
    assert resp_offset["total"] == 4
    assert [v["version_number"] for v in resp_offset["versions"]] == [2, 1]

    # status filter returns only the matching rows, and total reflects the filter.
    resp_promoted = await list_release_versions(
        project_id=project.id,
        limit=20,
        offset=0,
        status="promoted",
        session=db_session,
        current_user=current_user,
    )
    assert resp_promoted["total"] == 2
    assert {v["version_number"] for v in resp_promoted["versions"]} == {1, 3}
    assert all(v["status"] == "promoted" for v in resp_promoted["versions"])


@pytest.mark.asyncio
async def test_list_release_versions_rejects_invalid_status(db_session):
    """An unknown ``status`` value is rejected with a 400 rather than silently
    returning everything."""
    from fastapi import HTTPException

    from app.routers.admin_release_version import list_release_versions

    project = await _make_project(db_session, name="RV-BadStatus")
    await _make_member(db_session, project, user_id="rv-user", role="viewer")

    with pytest.raises(HTTPException) as exc:
        await list_release_versions(
            project_id=project.id,
            limit=20,
            offset=0,
            status="bogus",
            session=db_session,
            current_user={"user_id": "rv-user"},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_release_versions_requires_membership(db_session):
    """A non-member is denied (404) — the access check runs before any query."""
    from fastapi import HTTPException

    from app.routers.admin_release_version import list_release_versions

    project = await _make_project(db_session, name="RV-NoMember")
    await _seed_history(db_session, project)

    with pytest.raises(HTTPException) as exc:
        await list_release_versions(
            project_id=project.id,
            limit=20,
            offset=0,
            status=None,
            session=db_session,
            current_user={"user_id": "stranger"},
        )
    assert exc.value.status_code == 404
