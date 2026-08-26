"""Pipeline ACTION verbs for the strategist / Strategic Resident.

The strategic op surface (``POST /api/agent/strategic``) could describe the
pipeline in detail but could not ACT on it: there was no verb for promote,
requeue/retry, unpark, reprioritise, or iteration-cap reset. Every one of
those was an operator-only HTTP lever, so an operator instruction like
"requeue that task" or "promote it" had no op to call and bottlenecked on a
human driving the admin API by hand.

This module holds the argument parsing and the post-commit appliers for those
verbs. It exists as its own module for two reasons:

* **Lock discipline.** ``strategic_operation`` runs its whole body while
  holding the Project row ``FOR UPDATE`` (``_lock_project_or_429``) and the
  strategic Task row ``FOR UPDATE``. Force-promote resolves a candidate SHA, a
  diff hash, and CI state against GitHub; requeueing a review/fix task fetches
  its PR. Minutes of external I/O under those locks is the exact failure that
  ``strategic_completion._apply_qualify_request_post_commit`` documents (idled
  connection killed mid-flush by Cloud SQL, poisoned session, whole agent job
  requeued). So the slow half of each verb runs AFTER the caller has committed
  and released its locks, on a dedicated fresh session -- the same shape
  ``request_qualification`` already uses.
* **One implementation, not a parallel one.** Each applier re-enters the SAME
  server-side function the operator lever calls, rather than reimplementing
  its semantics:

  ======================= ==================================================
  op                      server-side implementation it re-enters
  ======================= ==================================================
  ``force_promote``       ``release_qualifier.force_promote_current_version``
                          (also behind ``POST /projects/{id}/promote/force``)
  ``requeue_task``        ``admin_task_mutations.modify_task_apply`` (also
                          behind ``POST /operator-access/v1/tasks/{id}/requeue``
                          and ``PATCH /api/admin/tasks/{id}``)
  ``reset_iteration_cap`` ``iteration_cap_reset.apply_iteration_cap_reset``
                          (also behind
                          ``POST /api/admin/tasks/{id}/reset-iteration-cap``)
  ``unhold_direction``    ``direction_streaks.clear_direction_hold`` (also
                          behind ``POST /projects/{id}/directions/{d}/unhold``)
  ``set_task_priority``   direct ``Task.priority`` write -- see
                          :func:`parse_priority`; this is the one verb with no
                          pre-existing operator lever to re-enter.
  ======================= ==================================================

WHAT THESE VERBS DELIBERATELY DO NOT DO
    They never invent an outcome. ``force_promote`` skips the release AUDIT
    gate and ONLY that -- the promotion PR still has to go green on the
    promotion target's required checks before anything merges -- so this module
    reports the raw ``promote()`` status back rather than claiming success, and
    marks ``promoted=False`` for every pre-promote no-op guard. Surfacing the
    REASON is the point: a Resident that reports "promoted" when the real
    answer was ``promotion_check_wait`` or ``ci_not_green`` has misinformed the
    operator about production.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)

_HEX = frozenset("0123456789abcdefABCDEF")


class PipelineActionError(Exception):
    """A refusal carrying the HTTP status the dispatch should answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# Source statuses a task may be re-driven from. Identical to the
# project-operator lever's ``_RETRYABLE_TASK_STATUSES`` -- requeue and retry are
# the same operation, so they must accept the same set.
RETRYABLE_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.EVALUATING,
        TaskStatus.PARKED,
    }
)

# Mirrors app.plan_completion.TASK_PRIORITY_MAP, the labels every spawner uses.
PRIORITY_LABELS: dict[str, int] = {"high": 100, "medium": 50, "low": 0}

_REASON_MAX = 500
_GUIDANCE_MAX = 5_000


# ---------------------------------------------------------------------------
# Argument parsing -- cheap, no I/O, safe to run under the caller's locks.
# ---------------------------------------------------------------------------


def _require_object(data: Any, op: str) -> dict:
    if not isinstance(data, dict):
        raise PipelineActionError(400, f"{op} data must be an object")
    return data


def parse_reason(data: dict, op: str, *, max_len: int = _REASON_MAX) -> str:
    """Require a non-empty operator-facing ``reason``.

    Every verb here is a privileged pipeline action; an unexplained one is
    unreviewable after the fact, so the reason is required rather than optional
    and is recorded on whatever the action touches.
    """
    reason = str(data.get("reason", "") or "").strip()
    if not reason:
        raise PipelineActionError(400, f"{op} requires a non-empty 'reason' string")
    return reason[:max_len]


def parse_target_task_id(data: dict, op: str) -> uuid.UUID:
    raw = data.get("target_task_id")
    if not raw:
        raise PipelineActionError(400, f"{op} requires a 'target_task_id'")
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise PipelineActionError(
            400, f"{op} target_task_id must be a UUID"
        ) from exc


def parse_priority(value: Any) -> int:
    """Resolve ``high|medium|low`` or an explicit 0-100 integer.

    ``Task.priority`` is the integer the dispatch loop's candidate query orders
    by (``ix_task_status_priority``). Accepting the labels keeps this op's
    vocabulary identical to ``update_direction``'s ``priority``, while the
    integer form exists because the column IS an integer and pinning an exact
    value is sometimes what the operator means.
    """
    if isinstance(value, bool):  # bool subclasses int; reject it explicitly
        raise PipelineActionError(
            400,
            "set_task_priority priority must be high|medium|low or an integer 0-100",
        )
    if isinstance(value, str):
        label = value.strip().lower()
        if label in PRIORITY_LABELS:
            return PRIORITY_LABELS[label]
        if not label.isdigit():
            raise PipelineActionError(
                400,
                "set_task_priority priority must be high|medium|low or an "
                "integer 0-100",
            )
        value = int(label)
    if not isinstance(value, int):
        raise PipelineActionError(
            400,
            "set_task_priority priority must be high|medium|low or an integer 0-100",
        )
    if not 0 <= value <= 100:
        raise PipelineActionError(
            400, f"set_task_priority priority {value} is out of range (0-100)"
        )
    return int(value)


def parse_force_promote(data: Any) -> tuple[str, str | None]:
    """Return ``(reason, candidate_sha or None)``."""
    obj = _require_object(data, "force_promote")
    reason = parse_reason(obj, "force_promote")
    raw_sha = obj.get("candidate_sha")
    if raw_sha is None or (isinstance(raw_sha, str) and not raw_sha.strip()):
        return reason, None
    sha = str(raw_sha).strip()
    # force_promote_current_version RESOLVES an abbreviated ref against GitHub,
    # so a short sha is legitimate; only reject what cannot be a commit ref at
    # all. Rejecting it here beats a bare ``sha_error`` four layers deeper.
    if not (7 <= len(sha) <= 40) or any(c not in _HEX for c in sha):
        raise PipelineActionError(
            400,
            "force_promote candidate_sha must be a 7-40 character hex commit sha",
        )
    return reason, sha.lower()


def parse_requeue_task(data: Any) -> tuple[uuid.UUID, str, str]:
    """Return ``(target_task_id, reason, guidance)``.

    ``guidance`` is the one-shot retry instruction appended to the requeued
    prompt (``requeue_prompt_suffix``). It defaults to the reason because the
    underlying mutation requires a non-empty suffix, and requeueing with no
    instruction at all is how an agent repeats the attempt that just failed.
    """
    obj = _require_object(data, "requeue_task")
    if "_original_prompt_suffix" in obj:
        raise PipelineActionError(
            400,
            "requeue_task cannot set '_original_prompt_suffix'; it is internal "
            "retry-dedup metadata, not operator guidance",
        )
    target_task_id = parse_target_task_id(obj, "requeue_task")
    reason = parse_reason(obj, "requeue_task")
    guidance = str(obj.get("guidance") or "").strip() or reason
    return target_task_id, reason, guidance[:_GUIDANCE_MAX]


def parse_reset_iteration_cap(data: Any) -> tuple[uuid.UUID, str]:
    obj = _require_object(data, "reset_iteration_cap")
    return (
        parse_target_task_id(obj, "reset_iteration_cap"),
        parse_reason(obj, "reset_iteration_cap"),
    )


def parse_set_task_priority(data: Any) -> tuple[uuid.UUID, int, str]:
    obj = _require_object(data, "set_task_priority")
    if "priority" not in obj:
        raise PipelineActionError(400, "set_task_priority requires a 'priority'")
    return (
        parse_target_task_id(obj, "set_task_priority"),
        parse_priority(obj.get("priority")),
        parse_reason(obj, "set_task_priority"),
    )


def parse_unhold_direction(data: Any) -> tuple[str, str]:
    obj = _require_object(data, "unhold_direction")
    direction_id = str(obj.get("direction_id") or "").strip()
    if not direction_id:
        raise PipelineActionError(
            400, "unhold_direction requires a non-empty 'direction_id'"
        )
    return direction_id, parse_reason(obj, "unhold_direction")


# ---------------------------------------------------------------------------
# Shared target resolution
# ---------------------------------------------------------------------------


async def load_same_project_task(
    session: AsyncSession,
    *,
    target_task_id: uuid.UUID,
    project_id: uuid.UUID,
    op: str,
) -> Task:
    """Load a task and refuse a cross-project target.

    The strategic session's project IS the authorization boundary for these
    verbs -- unlike ``cancel_task``, which additionally requires the target to
    have been spawned by this very session. That narrower rule is right for
    cancel (it destroys work someone else queued) but wrong here: an operator
    saying "requeue that task" means any task in the project they are talking
    about, and a task this session did not spawn is precisely the platform-kill
    case that needs re-driving.
    """
    target = await session.get(Task, target_task_id)
    if target is None:
        raise PipelineActionError(404, f"{op}: task {target_task_id} not found")
    if target.project_id != project_id:
        raise PipelineActionError(
            403, f"{op}: task {target_task_id} belongs to a different project"
        )
    return target


# ---------------------------------------------------------------------------
# Post-commit appliers -- slow I/O, run on a FRESH session with no lock held.
# ---------------------------------------------------------------------------


async def apply_force_promote_post_commit(
    *,
    project_id: uuid.UUID,
    actor: str,
    reason: str,
    candidate_sha: str | None,
    session_factory=None,
) -> dict[str, Any]:
    """Force-promote on a dedicated session, AFTER the caller released its locks.

    Returns the raw ``force_promote_current_version`` result plus a ``promoted``
    boolean derived from ``_FORCE_PROMOTE_NOOP_STATUSES`` -- the set of statuses
    that mean ``promote()`` never ran. The status is NOT translated into an HTTP
    error the way the operator route does, because for a conversational caller
    "a promotion is already in flight" is an answer to relay, not a failure.
    """
    from app import release_qualifier
    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    async with factory() as fresh:
        result = await release_qualifier.force_promote_current_version(
            fresh,
            project_id,
            candidate_sha=candidate_sha,
            actor=actor,
            reason=reason,
        )
        await fresh.commit()
    result = result or {}
    status = str(result.get("status") or "")
    promoted = bool(status) and status not in (
        release_qualifier._FORCE_PROMOTE_NOOP_STATUSES
    )
    logger.warning(
        "[STRATEGIC API] force_promote by %s on project %s (candidate=%s reason=%r): "
        "status=%s promoted=%s -- the AUDIT gate was skipped, CI was NOT",
        actor,
        project_id,
        candidate_sha or "integration HEAD",
        reason,
        status or "<none>",
        promoted,
    )
    return {"promoted": promoted, "promotion": result}


def summarize_promotion(result: dict[str, Any]) -> dict[str, Any]:
    """Bound the ``promote()`` result to the scalars an operator needs told.

    The raw result can carry nested audit/CI structures; a conversational
    surface that echoes all of it truncates somewhere unpredictable. These keys
    are chosen so the REASON always survives: ``status`` plus whatever the
    no-op guard set as its explanation, and the PR coordinates when one exists.
    """
    result = result or {}
    out: dict[str, Any] = {"status": result.get("status")}
    for key in (
        "detail",
        "promotion_pr_number",
        "promotion_pr_url",
        "pr_number",
        "pr_url",
        "version_number",
        "candidate_sha",
        "merged",
    ):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
    ci_resolution = result.get("ci_resolution")
    if isinstance(ci_resolution, dict):
        ci_state = ci_resolution.get("state")
        if isinstance(ci_state, (str, int, bool)):
            out["ci_state"] = ci_state
    return out


async def apply_requeue_post_commit(
    *,
    target_task_id: uuid.UUID,
    project_id: uuid.UUID,
    guidance: str,
    background_tasks,
    session_factory=None,
) -> dict[str, Any]:
    """Re-drive one task to QUEUED on a dedicated session.

    Re-enters ``modify_task_apply`` -- the same function the operator PATCH and
    the project-operator requeue lever call -- so attempt/runtime reset, blocker
    clearing, cascade rules, and review/fix PR admission behave identically. A
    review/fix requeue fetches the PR from GitHub inside that call, which is
    the reason this runs post-commit rather than under the dispatch's locks.
    """
    from app.admin_task_mutations import modify_task_apply
    from app.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    async with factory() as fresh:
        target = await load_same_project_task(
            fresh,
            target_task_id=target_task_id,
            project_id=project_id,
            op="requeue_task",
        )
        if target.status not in RETRYABLE_TASK_STATUSES:
            raise PipelineActionError(
                409,
                f"requeue_task: task {target_task_id} is in status "
                f"{getattr(target.status, 'value', target.status)}, which is not "
                "retryable (allowed: "
                f"{sorted(s.value for s in RETRYABLE_TASK_STATUSES)})",
            )
        previous_status = getattr(target.status, "value", str(target.status))
        result = await modify_task_apply(
            target,
            {"status": TaskStatus.QUEUED, "requeue_prompt_suffix": guidance},
            background_tasks,
            fresh,
            force_kill=False,
        )
    return {
        "target_task_id": str(target_task_id),
        "previous_status": previous_status,
        "status": (result or {}).get("status") or TaskStatus.QUEUED.value,
    }


async def clear_direction_hold_for_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    direction_id: str,
) -> bool:
    """Clear a direction's consecutive-failure hold; return whether one was set.

    Mirrors ``POST /projects/{id}/directions/{direction_id}/unhold`` exactly,
    including its 404 when the id is not one of the project's directions -- the
    hold registry is keyed by bare direction id, so skipping that existence
    check would let a typo silently "succeed" against another project's id.
    """
    import app.shared_state as _ss

    project = await session.get(Project, project_id)
    directions = (project.directions if project is not None else None) or []
    if not any(
        isinstance(d, dict) and d.get("id") == direction_id for d in directions
    ):
        raise PipelineActionError(
            404, f"unhold_direction: direction {direction_id} not found"
        )
    was_held, _hold_reason = _ss.is_direction_held(direction_id)
    _ss.clear_direction_hold(direction_id)
    return bool(was_held)


__all__ = [
    "PRIORITY_LABELS",
    "RETRYABLE_TASK_STATUSES",
    "PipelineActionError",
    "apply_force_promote_post_commit",
    "apply_requeue_post_commit",
    "clear_direction_hold_for_project",
    "load_same_project_task",
    "parse_force_promote",
    "parse_priority",
    "parse_reason",
    "parse_requeue_task",
    "parse_reset_iteration_cap",
    "parse_set_task_priority",
    "parse_target_task_id",
    "parse_unhold_direction",
    "summarize_promotion",
]
