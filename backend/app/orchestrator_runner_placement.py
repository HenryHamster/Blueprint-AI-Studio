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

That last property splits the wait in two, and conflating the halves is what
this module exists to prevent. A wait behind a BUSY fleet drains by itself and
must stay quiet. A wait behind a fleet with no live runner at all never drains,
so an unbounded wait means the work is parked forever with nothing downstream
to turn it into a visible failure -- the reaper must not force-fail it, which
means this module owns the operator page instead (see
``raise_unsatisfiable_local_placement_alert`` /
``clear_unsatisfiable_local_placement_alert``).

Re-exported from ``orchestrator_runner_dispatch`` (and thence from
``app.orchestrator``) so existing import paths keep working unchanged.
"""

import logging
import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.alert_policy import create_project_alert_if_allowed
from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.resource_envelope import resolve_project_execution_placement_policy
from app.runner_project_binding import (
    NO_ACTIVE_RUNNER_BOUND_REASON,
    resolve_unsatisfiable_local_placement,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


# One durable, task-wide wait record for the project placement contract.  Unlike
# the older capability-specific timestamps below, this is not a failure timer:
# ``local_only`` explicitly means that lack of capacity is backpressure, not a
# reason to spend money elsewhere or to fail useful work.  Keep the value a
# structured record so operators can distinguish a policy wait from a runner
# capability fault without adding another top-level timestamp for each task type.
EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY = "execution_placement_wait"


def _stage_local_only_placement_wait(
    task: Task, *, reason: str, block: dict | None = None
) -> bool:
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

    ``block`` is the resolved unsatisfiable-placement snapshot when the caller
    observed that ZERO live runners can serve this project.  It outranks the
    capability reason and switches the message away from capacity wording; see
    ``_hold_task_for_local_only_placement``.
    """
    ctx = dict(task.context or {})
    capability_reason = ctx.pop("capability_dispatch_reason", None)
    has_capability_reason = bool(
        isinstance(capability_reason, str) and capability_reason.strip()
    )
    block_message = block.get("message") if isinstance(block, dict) else None
    has_block = bool(
        isinstance(block, dict) and isinstance(block_message, str) and block_message
    )
    if has_block:
        # An unsatisfiable placement outranks a capability gap: when the active
        # pool for this project is EMPTY, "no active runner advertises X" is an
        # artifact of that emptiness, not the operative blocker.
        effective_reason = NO_ACTIVE_RUNNER_BOUND_REASON
    elif has_capability_reason:
        effective_reason = capability_reason
    else:
        effective_reason = reason
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
    if has_block:
        prior_block = current.get("block") if isinstance(current, dict) else None
        if isinstance(prior_block, dict) and all(
            prior_block.get(key) == block.get(key)
            for key in ("reason", "message", "bound_runner_count")
        ):
            # Same block, still unresolved: keep the FIRST observation's
            # ``recorded_at`` so "how long has this been broken" stays truthful,
            # and so the record does not rewrite itself every dispatcher cycle.
            block = prior_block
        # Durable so read models, the alert, and the stalled-cycle reaper can
        # recognise a permanent block without parsing prose.
        wait_record["unsatisfiable"] = True
        wait_record["block"] = block
    changed = ctx.get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY) != wait_record
    if has_capability_reason:
        # The transient marker was popped out of ``ctx``; persist that removal.
        changed = True
    if ctx.pop("runner_target_id", None) is not None:
        changed = True
    if ctx.pop("runner_hostname", None) is not None:
        changed = True
    ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY] = wait_record
    if has_block:
        # Deliberately NOT the "waiting for capacity" wording: nothing about
        # this state is a queue that drains.  Name the block and the remedy.
        message = (
            "Blocked (project policy: local_only): "
            f"{block_message}."
        )
    else:
        message = (
            "Waiting for local execution capacity (project policy: local_only)."
        )
        if has_capability_reason:
            message = f"{message} ({capability_reason})"
    if task.latest_status_message != message:
        task.latest_status_message = message
        changed = True
    if changed:
        task.context = ctx
    return changed


def local_only_placement_wait_record(task: Task | None) -> dict | None:
    """Return the durable ``local_only`` wait record on ``task``, if it holds one.

    ``None`` when the task is not parked under this contract at all, so callers
    can treat the truthiness of the result as "is this task in a policy hold".
    """
    if task is None:
        return None
    record = (task.context or {}).get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY)
    if not isinstance(record, dict) or record.get("policy") != "local_only":
        return None
    return record


def task_is_local_only_placement_hold(task: Task | None) -> bool:
    """True when ``task`` is QUEUED under the no-timeout ``local_only`` contract.

    The sibling of ``strategic_runtime_work.task_is_strategic_local_runner_hold``
    for the newer, broader project-level policy.  Both name the same idea: a task
    parked here is INTENTIONALLY waiting under an operator policy, so a watchdog
    that force-fails "stuck" work must skip it.  This module promises the task
    will never be failed for lack of a runner (see the module docstring); a
    watchdog that fails it anyway breaks that promise from the outside.

    ``_fail_stale_local_runner_holds`` MIGRATES strategic local-runner holds into
    this contract and strips the keys the older predicate tests, so a task can
    leave that predicate's cover and land here — the two must be checked
    together.
    """
    if task is None or getattr(task, "archived", False):
        return False
    if task.status != TaskStatus.QUEUED:
        return False
    return local_only_placement_wait_record(task) is not None


def task_placement_hold_is_unsatisfiable(task: Task | None) -> bool:
    """True when the task's ``local_only`` hold cannot be cleared by waiting."""
    record = local_only_placement_wait_record(task)
    return bool(record and record.get("unsatisfiable") is True)


# -- Operator alert for an unsatisfiable local_only hold ---------------------
#
# A hold that CAN be satisfied is backpressure and must stay quiet.  A hold that
# CANNOT be satisfied is an outage of this project's ability to run anything at
# all, and the policy's unbounded wait means nothing downstream will ever turn
# it into a visible failure.  Skipping it in the stalled-cycle reaper without
# raising this alert would trade a noisy livelock for a silent one -- strictly
# worse, because the noise was the only signal the operator had.

UNSATISFIABLE_PLACEMENT_EVENT_TYPE = "placement.unsatisfiable_local_hold"
UNSATISFIABLE_PLACEMENT_ALERT_TITLE = "Local-only work parked: no eligible runner"

# An unsatisfiable hold still open after this long stops being a WARNING.
# Matches the stalled-cycle reaper's own 3h horizon, so the alert escalates at
# the moment the pre-fix pipeline would have force-terminated the first cycle.
_UNSATISFIABLE_PLACEMENT_ESCALATION_SECONDS_DEFAULT = 3 * 3600


def unsatisfiable_placement_dedupe_key(project_id: uuid.UUID | str) -> str:
    """One row per PROJECT -- never per task, per cycle, or per occurrence.

    Occurrence identity in a dedupe key costs twice, and the second cost is the
    one that hid this defect for 16 cycles: it mints an immortal row per
    occurrence AND resets ``created_at`` every time, so an age-based escalation
    can never fire.  A condition that has held for two days then looks like
    sixteen unrelated conditions each three hours old.  Dedupe on the condition;
    escalate on its age.
    """
    return f"{UNSATISFIABLE_PLACEMENT_EVENT_TYPE}:{project_id}"


async def _open_unsatisfiable_placement_alerts(
    session: AsyncSession, project_id: uuid.UUID
) -> list[Alert]:
    rows = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == unsatisfiable_placement_dedupe_key(project_id),
            Alert.dismissed_at.is_(None),
        )
    )
    return list(rows.scalars().all())


async def raise_unsatisfiable_local_placement_alert(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    block: dict,
) -> Alert | None:
    """Stage (do not commit) the deduped operator alert for a permanent hold.

    Routed through :func:`create_project_alert_if_allowed` rather than a raw
    ``Alert(...)`` insert so the ``begin_nested`` + ``IntegrityError`` re-lookup
    closes the read-then-insert race, and so re-fires bump ``occurrence_count``
    and ``last_seen_at`` instead of duplicating the row.  Each re-fire is
    therefore strictly MORE informative than the last, never less.

    Severity is a function of the open row's AGE, which is only meaningful
    because the dedupe key is condition-scoped: the same row survives every
    cycle, so ``created_at`` records when the project actually stopped being
    able to run work.
    """
    existing = await _open_unsatisfiable_placement_alerts(session, project_id)
    severity = AlertSeverity.WARNING
    aged_clause = ""
    threshold = int(
        _runtime_config.get(
            "unsatisfiable_placement_escalation_seconds",
            _UNSATISFIABLE_PLACEMENT_ESCALATION_SECONDS_DEFAULT,
        )
    )
    oldest = min((a.created_at for a in existing if a.created_at), default=None)
    if oldest is not None:
        held_seconds = (_utcnow() - oldest).total_seconds()
        if held_seconds >= threshold:
            severity = AlertSeverity.ERROR
            aged_clause = (
                f" Unresolved for {int(held_seconds // 3600)}h -- every task this "
                f"project queues is accumulating behind it."
            )
    message = (
        f"{block.get('message') or 'no eligible local runner'}. This project's "
        f"execution placement policy is local_only, whose wait is unbounded BY "
        f"DESIGN, so the queued work will neither start nor fail on its own -- "
        f"it stays parked until an operator restores an eligible runner."
        f"{aged_clause}"
    )[:1000]
    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=UNSATISFIABLE_PLACEMENT_EVENT_TYPE,
        severity=severity,
        title=UNSATISFIABLE_PLACEMENT_ALERT_TITLE,
        message=message,
        task_id=task_id,
        source="orchestrator",
        dedupe_key=unsatisfiable_placement_dedupe_key(project_id),
        broadcast=False,
    )
    if alert is not None and created:
        logger.warning(
            "[RUNNERS] Unsatisfiable local_only placement for project %s: %s",
            project_id,
            block.get("message"),
        )
    return alert


async def clear_unsatisfiable_local_placement_alert(
    session: AsyncSession, project_id: uuid.UUID
) -> int:
    """Dismiss the open unsatisfiable-placement row once the block is gone.

    The ``SOURCE_CONDITION`` half of the contract declared in
    ``ALERT_EVENT_POLICIES``: this producer owns retirement, so every path that
    observes an eligible runner serving the project calls this.  Commits its own
    dismissal, mirroring
    ``runner_health_alerts._resolve_fleet_no_healthy_runner_alert``.
    """
    open_alerts = await _open_unsatisfiable_placement_alerts(session, project_id)
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    try:
        for alert in open_alerts:
            await broadcast_to_project(
                str(project_id),
                "alert_dismissed",
                {"alert_id": str(alert.id), "title": alert.title},
            )
    except Exception:
        # The dismissal is already committed; a failed fan-out must not make
        # the caller think the row is still open.
        logger.exception(
            "[RUNNERS] alert_dismissed broadcast failed for project %s",
            project_id,
        )
    return len(open_alerts)


def _clear_execution_placement_wait(ctx: dict) -> None:
    """Clear a policy wait once a task has durably acquired local execution."""
    ctx.pop(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY, None)


async def _hold_task_for_local_only_placement(
    session: AsyncSession,
    task: Task,
    *,
    reason: str = "no_eligible_local_runner",
    active_runners_cache: Mapping[uuid.UUID, list] | None = None,
) -> bool:
    """Keep a ``local_only`` task queued when no runner can take it.

    Returns ``True`` only when the project contract is strict and the task was
    handled.  It deliberately has no timeout: the policy promises local
    execution, so absence of a runner must never become an implicit managed
    fallback or a terminal failure.

    ``active_runners_cache`` is the dispatcher's live pool, the same mapping
    ``_try_runner_dispatch`` selects from: ``{project_id: [runner, ...]}`` built
    from runners that are ``status='active'`` AND heartbeating, fanned out over
    the binding allow-list.  An EMPTY list for this project therefore means no
    live runner can ever pick the task up, which is a structural block rather
    than backpressure -- and because the wait has no timeout, nothing downstream
    would ever turn it into a visible failure.  Passing the pool in (rather than
    stamping the observation into ``task.context`` at dispatch) keeps the
    finding out of the persisted context of every managed-cloud task, which
    takes the same "no runner" dispatch branch on every cycle.

    Omitting it means the caller did not observe the pool; the hold then behaves
    exactly as it did before this distinction existed -- generic capacity
    wording, no page.  Fail-safe in the direction that never invents an outage.
    """
    project = await session.get(Project, task.project_id)
    if (
        project is None
        or resolve_project_execution_placement_policy(project) != "local_only"
    ):
        return False
    project_id = task.project_id
    task_id = task.id
    # Resolving the emptiness into a reason, message, and remedy needs a query,
    # so it happens here -- on the local_only path alone, and only when the
    # caller actually observed an empty pool.
    snapshot: dict | None = None
    if active_runners_cache is not None and not (
        active_runners_cache.get(project_id) or []
    ):
        snapshot = await resolve_unsatisfiable_local_placement(session, project_id)
    changed = _stage_local_only_placement_wait(task, reason=reason, block=snapshot)
    if snapshot is not None:
        # Loudness is the whole point of separating this case from capacity:
        # the wait has no timeout and the stalled-cycle reaper now skips it, so
        # this alert is the only thing that reaches an operator.  Staged inside
        # the same transaction as the wait record, then committed together, so
        # the durable record and the page can never disagree.
        await raise_unsatisfiable_local_placement_alert(
            session, project_id=project_id, task_id=task_id, block=snapshot
        )
        session.add(task)
        await session.commit()
    elif changed:
        session.add(task)
        await session.commit()
    else:
        # Keep the orchestrator's transaction boundary small.  Do not touch
        # ``task`` after this rollback: SQLAlchemy expires ORM attributes.
        await session.rollback()
    if snapshot is None:
        # A live runner exists for this project (it is merely busy or lacks a
        # capability), so any standing "no eligible runner" page is stale.  This
        # producer owns SOURCE_CONDITION retirement; retire it here.
        await clear_unsatisfiable_local_placement_alert(session, project_id)
    logger.info(
        "[RUNNERS] Holding task %s for local_only placement: %s",
        task_id,
        reason,
    )
    return True
