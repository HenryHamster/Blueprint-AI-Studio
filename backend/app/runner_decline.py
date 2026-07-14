"""Runner task-decline bookkeeping — the record that breaks the decline livelock.

A runner that pulls a task from ``/api/runner/next-task`` may decline it (no
bwrap, docker-only host handed a write task, agent spawn failure). The decline
releases the task back to QUEUED — but until this module existed it recorded
NOTHING, and that turned a recoverable mismatch into a silent infinite loop:

    ``_select_eligible_runner``'s tiebreak is ``r.id.int ^ dispatch_seed``, and
    ``dispatch_seed`` is the TASK's uuid — so it is STABLE per task. With no
    per-task exclusion, the orchestrator re-picked the exact same runner that
    had just declined, forever: pin → pull → decline → pin → …, with no counter,
    no alert, and no reason anywhere.

So the decline is now persisted on ``task.context[DECLINED_BY_CONTEXT_KEY]`` as
``{runner_id: {count, at, reason, hostname}}``. Two consumers:

  * :func:`recently_declined_runner_ids` — the dispatcher excludes these runners
    when picking hardware for THIS task, so the next pin goes somewhere else (or
    nowhere, which correctly falls through to cloud dispatch).
  * :func:`maybe_create_decline_alert` — after
    :data:`DECLINE_ESCALATION_THRESHOLD` declines the operator gets an alert
    carrying the runner's own reason string, instead of a task quietly cycling.

The exclusion is time-boxed (:data:`DECLINE_EXCLUSION_SECONDS`) rather than
permanent: a decline can be transient (a spawn failure under memory pressure),
and permanently burning a runner for a task would strand it on a single-runner
fleet.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, Task, _utcnow
from app.time_utils import _coerce_naive_utc

logger = logging.getLogger(__name__)

# task.context key: {runner_id: {"count": int, "at": iso, "reason": str, "hostname": str}}
DECLINED_BY_CONTEXT_KEY = "declined_by"

# How long a decline keeps a runner out of the running for THAT task.
#
# This number is bounded ABOVE by the Strategic local hold timeout
# (``strategic_local_hold_timeout_seconds``, default 900s — see
# ``orchestrator_runner_dispatch._strategic_local_hold_timeout_seconds``). On a
# single-runner Strategic fleet the decliner is the ONLY Strategic-capable
# runner, so excluding it empties the candidate set, ``_select_eligible_runner``
# returns None, and ``_hold_strategic_local_runner_task`` parks the task — and
# at 900s ``_fail_stale_local_runner_holds`` clears the local-required pin and
# FORCES CLOUD FALLBACK. An exclusion of 1800s could therefore never expire in
# time: one transient decline (a Popen spawn failure) would deterministically
# evict a Resident Strategic Agent task off local execution, defeating
# continuous-local mode.
#
# 600s sits comfortably under the 900s default: the excluded runner becomes
# eligible again at t=600s, leaving ~300s — many dispatch cycles — for the task
# to be re-pinned and pulled locally BEFORE the cloud-fallback sweep fires. It
# is also ~100x the pin→pull→decline loop period (seconds), so it still breaks
# the livelock decisively.
#
# The timeout is runtime-configurable and could be lowered below 600s, so this
# ordering is NOT relied on alone — see ``decline_exclusion_is_advisory``, which
# stops the exclusion from ever emptying the candidate set for a Strategic task
# whose declines still look transient.
DECLINE_EXCLUSION_SECONDS = 10 * 60

# Declines (across all runners) for one task before the operator is alerted.
DECLINE_ESCALATION_THRESHOLD = 3

DECLINE_ALERT_TITLE = "Task repeatedly declined by runners"


def recently_declined_runner_ids(
    task: Any, now: datetime | None = None
) -> set[str]:
    """Runner ids that declined ``task`` within :data:`DECLINE_EXCLUSION_SECONDS`.

    Returned as strings so callers can compare against ``str(runner.id)`` without
    caring whether the context marker survived a JSON round-trip. Unparseable or
    expired entries are ignored (fail-open: a bad marker must never make a task
    undispatchable).
    """
    ctx = getattr(task, "context", None) or {}
    records = ctx.get(DECLINED_BY_CONTEXT_KEY) or {}
    if not isinstance(records, dict):
        return set()
    now = now or _utcnow()
    excluded: set[str] = set()
    for runner_id, rec in records.items():
        if not isinstance(rec, dict):
            continue
        at = rec.get("at")
        if not isinstance(at, str):
            continue
        try:
            # Coerced to naive UTC: `now` is naive (_utcnow / TIMESTAMP WITHOUT
            # TIME ZONE), so an aware stored timestamp — an operator hand-PATCHing
            # task.context, a 'Z'-suffix writer — would raise TypeError on the
            # subtraction below, which this except would not catch. Fail-open on a
            # bad marker; never 500 the dispatch path over a decline record.
            declined_at = _coerce_naive_utc(datetime.fromisoformat(at))
        except (ValueError, TypeError):
            continue
        if (now - declined_at).total_seconds() < DECLINE_EXCLUSION_SECONDS:
            excluded.add(str(runner_id))
    return excluded


def decline_record_for_runner(
    task: Any, runner_id: str, now: datetime | None = None
) -> dict | None:
    """The live decline record for ``runner_id`` on ``task``, or None.

    "Live" = within :data:`DECLINE_EXCLUSION_SECONDS`, i.e. exactly the records
    that :func:`recently_declined_runner_ids` is excluding. Lets a caller report
    WHY a runner was dropped instead of leaving an unexplained hole (the pin-hold
    diagnostic would otherwise blame a generic ``not_eligible``).
    """
    ctx = getattr(task, "context", None) or {}
    records = ctx.get(DECLINED_BY_CONTEXT_KEY) or {}
    if not isinstance(records, dict):
        return None
    rec = records.get(str(runner_id))
    if not isinstance(rec, dict):
        return None
    at = rec.get("at")
    if not isinstance(at, str):
        return None
    try:
        # See recently_declined_runner_ids: coerce to naive UTC so an aware stored
        # timestamp cannot TypeError on the subtraction below.
        declined_at = _coerce_naive_utc(datetime.fromisoformat(at))
    except (ValueError, TypeError):
        return None
    elapsed = ((now or _utcnow()) - declined_at).total_seconds()
    if elapsed >= DECLINE_EXCLUSION_SECONDS:
        return None
    rec = dict(rec)
    rec["seconds_left"] = int(DECLINE_EXCLUSION_SECONDS - elapsed)
    return rec


def total_decline_count(task: Any) -> int:
    """Total declines recorded on ``task`` across every runner."""
    ctx = getattr(task, "context", None) or {}
    records = ctx.get(DECLINED_BY_CONTEXT_KEY) or {}
    if not isinstance(records, dict):
        return 0
    total = 0
    for rec in records.values():
        if isinstance(rec, dict):
            try:
                total += int(rec.get("count") or 1)
            except (TypeError, ValueError):
                total += 1
    return total


def decline_exclusion_is_advisory(task: Any, task_type: str) -> bool:
    """May the decline exclusion be WAIVED rather than empty the candidate set?

    True only for a Strategic task whose declines still look transient (fewer
    than :data:`DECLINE_ESCALATION_THRESHOLD` in total).

    Why Strategic is special: for a normal task, emptying the candidate set is
    the CORRECT outcome — the task falls through to cloud dispatch and makes
    progress. For a local-REQUIRED Strategic task there is no such graceful
    exit: an empty set parks it in ``_hold_strategic_local_runner_task`` and the
    900s sweep evicts it to cloud, which is precisely what continuous-local mode
    exists to prevent. So while the decline still looks like a one-off, we would
    rather re-pick the decliner (it may well succeed on the retry) than hand the
    task to the cloud.

    Bounded on purpose: once the fleet has declined the task
    :data:`DECLINE_ESCALATION_THRESHOLD` times the decline is clearly NOT
    transient, the operator has already been alerted, and the waiver stops. The
    exclusion then applies for real, the candidate set empties, and the ordinary
    hold → cloud-fallback safety valve runs. Without that bound a persistently
    incapable single-runner fleet would livelock forever.

    Callers apply this only when the exclusion WOULD empty the candidate set;
    whenever other hardware exists, the exclusion is honoured unconditionally.
    """
    from app.task_capabilities import STRATEGIC_TASK_TYPES

    if task_type not in STRATEGIC_TASK_TYPES:
        return False
    return total_decline_count(task) < DECLINE_ESCALATION_THRESHOLD


def decline_summary(task: Any) -> str:
    """One-line "runner X (Nx): reason" summary of every decline on ``task``."""
    ctx = getattr(task, "context", None) or {}
    records = ctx.get(DECLINED_BY_CONTEXT_KEY) or {}
    if not isinstance(records, dict):
        return ""
    parts = []
    for runner_id, rec in records.items():
        if not isinstance(rec, dict):
            continue
        label = rec.get("hostname") or runner_id
        parts.append(
            f"{label} ({rec.get('count') or 1}x): {rec.get('reason') or 'unspecified'}"
        )
    return " | ".join(parts)


async def maybe_create_decline_alert(
    session: AsyncSession,
    task: Task,
    total_declines: int,
    reason: str,
) -> bool:
    """Raise a deduplicated WARNING alert for a task the fleet keeps declining.

    Deduplicated on ``(task_id, title, dismissed_at IS NULL)`` — the same shape
    every other task-scoped alert uses — so a task that keeps bouncing raises
    exactly ONE alert per episode. The CALLER commits. Returns False on a dedup
    hit.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == DECLINE_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return False

    msg = (
        f"This task has been declined {total_declines} times by local runners "
        f"and keeps returning to the queue. Most recent reason: {reason}. "
        f"All declines: {decline_summary(task) or 'n/a'}. The declining runners "
        f"are excluded from re-selection for "
        f"{DECLINE_EXCLUSION_SECONDS // 60} minutes, so the task will retry on "
        f"other hardware or fall through to cloud dispatch — but the underlying "
        f"capability gap (missing sandbox, wrong isolation backend, or a runner "
        f"too old to handle this task type) needs fixing."
    )[:1000]
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=DECLINE_ALERT_TITLE,
        message=msg,
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    logger.warning(
        "[RUNNERS] Task %s declined %d times — raised operator alert. %s",
        task.id,
        total_declines,
        msg,
    )
    return True
