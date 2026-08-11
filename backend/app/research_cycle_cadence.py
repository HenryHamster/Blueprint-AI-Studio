"""Accepted research-cycle cadence accounting.

The legacy ``_research_spawn_count`` name is retained in project vision, but
its meaning is the monotonic total of completed, authoritative research
cycles.  A separate last-fired receipt measures how many accepted cycles have
accrued since the most recent Strategic refresh.  Keep the terminal-status and
finalized-artifact checks in one leaf helper so grader parking and child-rollup
completion cannot drift.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import Project, Task, TaskStatus
from app.task_locking import lock_project_then_task, with_vision_locked


RESEARCH_SPAWN_COUNTED_IDS_MAX = 200
STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY = (
    "_strategic_last_accepted_research_count"
)
STRATEGIC_CADENCE_ACCEPTED_COUNT_CONTEXT_KEY = (
    "_strategic_cadence_accepted_count"
)


def _nonnegative_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_strategic_cadence_accepted_count(value: object) -> int:
    """Return the canonical integer stored on a durable Strategic spawn."""

    return _nonnegative_count(value)


def strategic_cadence_due(
    accepted_count: object,
    last_fired_count: object,
    frequency: object,
) -> bool:
    """Return whether ``frequency`` new accepted cycles accrued since fire."""

    accepted = _nonnegative_count(accepted_count)
    last_fired = min(accepted, _nonnegative_count(last_fired_count))
    period = _nonnegative_count(frequency)
    return period > 0 and accepted > 0 and accepted - last_fired >= period


def record_strategic_cadence_fire(
    vision: dict,
    *,
    accepted_count: object | None = None,
) -> int:
    """Stamp the monotonic decision receipt without mutating accepted cycles.

    ``accepted_count`` is the scheduler snapshot that made the cycle due. A
    research completion may advance the shared counter while the Strategic task
    is being assembled; consuming that later completion in this receipt could
    skip work the just-spawned strategist never observed. The project lock still
    makes the write atomic, while the snapshot keeps the cadence conservative.
    """

    current_accepted = _nonnegative_count(vision.get("_research_spawn_count"))
    observed_accepted = (
        current_accepted
        if accepted_count is None
        else min(current_accepted, _nonnegative_count(accepted_count))
    )
    previous = _nonnegative_count(
        vision.get(STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY)
    )
    fired_at = max(previous, observed_accepted)
    vision[STRATEGIC_LAST_ACCEPTED_RESEARCH_COUNT_KEY] = fired_at
    return fired_at


async def stamp_strategic_cadence_fire(
    session,
    project_id,
    *,
    accepted_count: object,
) -> int:
    """Stage a cadence receipt in the caller's already-open transaction.

    Strategic spawners call this before the same commit that makes their Task
    and StrategicCycle durable. A crash after that commit but before the
    autonomous loop's outer bookkeeping commit therefore cannot replay the
    same accepted-count boundary.
    """

    stamped = 0

    def _stamp(vision: dict) -> None:
        nonlocal stamped
        stamped = record_strategic_cadence_fire(
            vision,
            accepted_count=accepted_count,
        )

    locked = await with_vision_locked(session, project_id, _stamp)
    if not locked:
        raise RuntimeError(
            f"project {project_id} disappeared before Strategic cadence receipt"
        )
    return stamped


@asynccontextmanager
async def lock_accepted_research_boundary(session, task: Task):
    """Re-lock the authoritative boundary in canonical Project -> Task order.

    Research completion intentionally commits intermediate visibility and
    bookkeeping phases. Those commits release entrypoint locks, so the final
    artifact/marker/cadence transaction must refresh both rows before dirtying
    either ORM instance.
    """

    # Pending pre-boundary diagnostics may have dirtied the identity-mapped Task.
    # Suppress autoflush so the lock SELECTs — Project first, Task second — remain
    # the first writes/locks in this terminal transaction. Callers reapply their
    # locally computed final context to the refreshed Task before committing.
    with session.no_autoflush:
        async with lock_project_then_task(
            session,
            task.project_id,
            task.id,
            skip_locked=False,
        ) as (project, locked_task):
            if project is None or locked_task is None:
                raise RuntimeError(
                    f"research boundary rows disappeared for task {task.id}"
                )
            if str(locked_task.project_id) != str(project.id):
                raise RuntimeError(
                    f"research task {locked_task.id} no longer belongs to project {project.id}"
                )
            yield project, locked_task


async def count_accepted_research_cycle(
    session,
    task: Task,
    *,
    project: Project,
) -> bool:
    """Count ``task`` exactly once at its authoritative acceptance boundary.

    Returns ``True`` only when this call added the task id.  The caller owns the
    transaction and must commit the finalized marker and vision receipt
    together. ``EVALUATING`` is accepted here only because a research parent
    with routed implementation children remains in delivery rollup after its
    analysis result is already final; grader/challenge parking has no finalized
    marker and therefore cannot advance cadence.

    ``project`` must be the exact Project row already locked before ``task`` by
    :func:`lock_accepted_research_boundary`. Requiring it fail-closed prevents a
    future caller from falling back to a Task -> Project lock inversion.
    """

    if (
        (task.first_payload or {}).get("type") != "research"
        or task.status not in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
        or (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
    ):
        return False

    task_id = str(task.id)
    counted_now = False

    def _increment(vision: dict) -> None:
        nonlocal counted_now
        raw_counted = vision.get("_research_spawn_counted_ids") or []
        counted = list(raw_counted) if isinstance(raw_counted, list) else []
        if task_id in counted:
            return
        try:
            current = int(vision.get("_research_spawn_count") or 0)
        except (TypeError, ValueError):
            current = 0
        vision["_research_spawn_count"] = current + 1
        vision["_research_spawn_counted_ids"] = [*counted, task_id][
            -RESEARCH_SPAWN_COUNTED_IDS_MAX:
        ]
        counted_now = True

    await with_vision_locked(
        session,
        task.project_id,
        _increment,
        _locked_project=project,
    )
    return counted_now
