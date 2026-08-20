"""``local_only`` execution-placement waits — the durable "held for capacity" record.

Extracted from :mod:`app.orchestrator_runner_dispatch`, which is a chronic
size-limit offender (decomposition required; cap bumps are forbidden by
``app.size_limit_policy``). This is the smallest self-contained seam in that
module: one task.context key plus the three helpers that stage, clear, and
commit it.

A ``local_only`` project promises local execution, so the absence of an
eligible runner is BACKPRESSURE, not a failure — these helpers keep the task
QUEUED with a truthful, operator-legible wait record instead of falling back
to managed cloud or failing the task. They deliberately have no timeout.

Re-exported from ``orchestrator_runner_dispatch`` (and thence from
``app.orchestrator``) so existing import paths keep working unchanged.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task
from app.resource_envelope import resolve_project_execution_placement_policy
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# One durable, task-wide wait record for the project placement contract.  Unlike
# the older capability-specific timestamps below, this is not a failure timer:
# ``local_only`` explicitly means that lack of capacity is backpressure, not a
# reason to spend money elsewhere or to fail useful work.  Keep the value a
# structured record so operators can distinguish a policy wait from a runner
# capability fault without adding another top-level timestamp for each task type.
EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY = "execution_placement_wait"


def _stage_local_only_placement_wait(task: Task, *, reason: str) -> bool:
    """Stage a durable ``local_only`` wait and return whether it changed ``task``.

    This helper intentionally does not commit.  Dispatch callers can commit the
    wait immediately, while timeout/recovery code can stage it together with its
    other updates.  The original ``since`` is retained while the policy and
    reason are stable so queue age is truthful across dispatcher cycles.

    When ``_try_runner_dispatch`` detected that ZERO active runners advertise a
    capability this task requires, it stamps that finding into
    ``task.context["capability_dispatch_reason"]`` (in-memory, same cycle, same
    transaction) before falling through to this hold. When present, it wins
    over the generic ``reason`` so both the structured wait record and the
    operator-facing message name the missing capability instead of reading as
    generic backpressure.
    """
    ctx = dict(task.context or {})
    capability_reason = ctx.pop("capability_dispatch_reason", None)
    has_capability_reason = bool(
        isinstance(capability_reason, str) and capability_reason.strip()
    )
    effective_reason = capability_reason if has_capability_reason else reason
    current = ctx.get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY)
    current_since = (
        current.get("since")
        if isinstance(current, dict) and isinstance(current.get("since"), str)
        else None
    )
    wait_record = {
        "policy": "local_only",
        "reason": effective_reason,
        "since": current_since or _utcnow().isoformat(),
    }
    changed = ctx.get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY) != wait_record
    if has_capability_reason:
        changed = True
    if ctx.pop("runner_target_id", None) is not None:
        changed = True
    if ctx.pop("runner_hostname", None) is not None:
        changed = True
    ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY] = wait_record
    message = "Waiting for local execution capacity (project policy: local_only)."
    if has_capability_reason:
        message = f"{message} ({capability_reason})"
    if task.latest_status_message != message:
        task.latest_status_message = message
        changed = True
    if changed:
        task.context = ctx
    return changed


def _clear_execution_placement_wait(ctx: dict) -> None:
    """Clear a policy wait once a task has durably acquired local execution."""
    ctx.pop(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY, None)


async def _hold_task_for_local_only_placement(
    session: AsyncSession,
    task: Task,
    *,
    reason: str = "no_eligible_local_runner",
) -> bool:
    """Keep a ``local_only`` task queued when no runner can take it.

    Returns ``True`` only when the project contract is strict and the task was
    handled.  It deliberately has no timeout: the policy promises local
    execution, so absence of a runner must never become an implicit managed
    fallback or a terminal failure.
    """
    project = await session.get(Project, task.project_id)
    if (
        project is None
        or resolve_project_execution_placement_policy(project) != "local_only"
    ):
        return False
    task_id = task.id
    changed = _stage_local_only_placement_wait(task, reason=reason)
    if changed:
        session.add(task)
        await session.commit()
    else:
        # Keep the orchestrator's transaction boundary small.  Do not touch
        # ``task`` after this rollback: SQLAlchemy expires ORM attributes.
        await session.rollback()
    logger.info(
        "[RUNNERS] Holding task %s for local_only placement: %s",
        task_id,
        reason,
    )
    return True
