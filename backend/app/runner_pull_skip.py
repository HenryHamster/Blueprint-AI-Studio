"""Runner pull-skip bookkeeping — the escalation that keeps a skip from becoming a stall.

``/api/runner/next-task`` scans the tasks pinned to a runner and SKIPS any whose
LLM credentials cannot be resolved (pool exhausted for that task's model, or a
payload that crashes credential resolution). Skipping is what stopped ONE
unroutable task at the head of the queue from starving a healthy runner forever
— but a skip that is only ever *counted* re-creates the same P0 one row further
down:

    Nothing unpins, fails, or reaps a permanently-unroutable pinned task. The
    head is reaper-exempt (``_is_runner_pinned_awaiting_pull``),
    ``_fail_stale_local_runner_holds`` sweeps only QUEUED, and
    ``_deactivate_stale_runners`` fires only on runner inactivity. So with a
    bounded candidate scan of N, **N+1 permanently-poisoned pinned tasks
    reproduce the original starvation exactly** — silently, because the skip
    counter escalated to nobody.

So a skip escalates, mirroring :mod:`app.runner_decline` (whose ``/decline-task``
counterpart already alerts at 3):

  * :func:`maybe_escalate_pull_skip` raises a deduplicated operator Alert once a
    task has been skipped :data:`SKIP_ALERT_THRESHOLD` times, carrying the skip
    code and the runner's own reason.
  * Past :data:`SKIP_UNPIN_THRESHOLD` it UNPINS the task — back to QUEUED, runner
    pin cleared — so the orchestrator re-dispatches it (other hardware, or cloud)
    and, critically, it stops occupying a slot in every future candidate scan.
    That is what makes the poisoned set self-draining instead of permanent.

Counting is throttled, not per-poll: ``count`` advances at most once per
:data:`SKIP_RESTAMP_SECONDS` (300s), so it is a proxy for elapsed time
(count ~= minutes/5), not for poll frequency. The thresholds are chosen against
that: alert at ~10 min, unpin at ~20 min — long enough that an ordinary
rate-limit cooldown (seconds to a few minutes) recovers on its own and never
escalates, short enough that a genuinely poisoned task clears the runner's queue
while an operator still cares.

:func:`skip_marker_is_fresh` is the read side of the same marker: ``next-task``
uses it to skip RE-RESOLVING a candidate it just proved unroutable, which is what
keeps a bounded candidate scan from turning into N credential resolutions —
each acquiring the global ``_max_plan_pool_lock`` — on every poll of every runner
during a pool-exhaustion storm.

The two clocks are SEPARATE fields (``seen_at`` for freshness, ``at`` for the
count) and must stay that way. Collapsing them into one silently disabled the
short-circuit entirely: the count clock only ticks every 300s, so a marker judged
"fresh" against a 30s TTL from that same field was stale at essentially every
poll — and the runner's own 503 backoff ceiling is 30s, landing the comparison
exactly on the boundary. Under sustained exhaustion the short-circuit fired ZERO
times and the resolve budget was burned re-proving the same head candidates, so
the head-of-line P0 was merely re-bounded from 1 to ``resolve_attempts`` rather
than removed. See the field docs on :data:`RUNNER_PULL_SKIP_CONTEXT_KEY`.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, Task, TaskStatus, _utcnow
from app.time_utils import _coerce_naive_utc

logger = logging.getLogger(__name__)

# task.context key: {"code", "reason", "at", "seen_at", "first_at", "count"}
#
# TWO timestamps, deliberately. They answer different questions and MUST NOT be
# collapsed back into one:
#
#   ``seen_at``  — when a runner pull last RESOLVED this task and failed. Written
#                  on EVERY stamp. This is the freshness clock (SKIP_RESOLVE_TTL).
#   ``at``       — when ``count`` was last incremented. Written only once per
#                  SKIP_RESTAMP_SECONDS. This is the escalation clock.
#
# Sharing one field silently DISABLED the short-circuit: the count clock only
# advances every 300s, so a marker judged "fresh" against a 30s TTL using that
# same field was stale at essentially every poll — and the runner's own backoff
# ceiling is 30s, landing the comparison exactly on the boundary. The result was
# zero short-circuits during a sustained storm, i.e. precisely when it mattered.
RUNNER_PULL_SKIP_CONTEXT_KEY = "runner_pull_skip"

# Lifetime tally of unpins, kept OUTSIDE the marker so the audit trail survives
# the marker being dropped (and never makes a drained task look "examined").
RUNNER_PULL_UNPINS_CONTEXT_KEY = "runner_pull_unpins"

# Machine-readable skip codes (returned in the 503 body and stamped on the task).
SKIP_CODE_NO_ACCOUNT = "no_eligible_account"
SKIP_CODE_CREDENTIAL_ERROR = "credential_resolution_failed"

# How often ``count`` may advance (the ESCALATION clock — see ``at`` above).
# Throttled so a live countdown in the reason ("240s left") cannot rewrite the
# row on every poll, and so ``count`` stays a proxy for elapsed time
# (count ~= minutes/5) rather than for poll frequency. Keyed on the TASK, not on
# the skip code: keying it on the code let a task flapping between
# ``no_eligible_account`` and ``credential_resolution_failed`` bypass the
# throttle entirely and escalate in seconds instead of minutes.
SKIP_RESTAMP_SECONDS = 300

# How long a skip marker suppresses RE-RESOLUTION of the same candidate (the
# FRESHNESS clock — see ``seen_at`` above).
#
# MUST EXCEED the runner's poll-backoff ceiling (scripts/local_runner.py backs
# off to 30.0s on every 503). At TTL == ceiling the freshness comparison sits
# exactly on the boundary and loses, so the short-circuit never fires under
# sustained exhaustion — the one condition it exists for. 90s clears that ceiling
# with margin while still bounding recovery latency when the pool heals.
SKIP_RESOLVE_TTL_SECONDS = 90

# Skips of ONE task before the operator is alerted / the task is unpinned.
# Against the 300s restamp throttle these are ~10 min and ~20 min of continuous
# unroutability. See the module docstring.
SKIP_ALERT_THRESHOLD = 3
SKIP_UNPIN_THRESHOLD = 5

SKIP_ALERT_TITLE = "Task repeatedly skipped by runner pull"


def _marker(task: Any, runner_id: str | None = None) -> dict:
    """The skip marker for ``task``, or {} — scoped to ``runner_id`` when given.

    Routability is RUNNER-dependent (``runner_available_clis`` feeds
    ``select_llm_runtime_for_task``), so a marker written by runner A says nothing
    about whether runner B can route the task. A task unpinned from A and
    re-pinned to B carries A's marker, and B would otherwise short-circuit — and
    refuse to run — a task it is perfectly capable of running. So a marker owned
    by a different runner reads as ABSENT: B re-resolves for itself.

    A marker with no ``runner_id`` is treated as owned (no such markers exist in
    the field yet; this only keeps the accessors total).
    """
    ctx = getattr(task, "context", None) or {}
    rec = ctx.get(RUNNER_PULL_SKIP_CONTEXT_KEY)
    if not isinstance(rec, dict):
        return {}
    if runner_id is not None:
        owner = rec.get("runner_id")
        if owner is not None and str(owner) != str(runner_id):
            return {}
    return rec


def clear_skip_marker(task: Any) -> None:
    """Drop the skip marker — the current episode is over.

    Called when the task DISPATCHES, and when it is unpinned. Without this the
    marker is immortal: ``count`` and ``first_at`` persist in ``task.context``
    across episodes, so a task that hits a transient cooldown on five separate
    occasions — possibly days apart, each recovering on its own — accumulates
    ``count=5`` and is eventually unpinned for no reason at all. ``has_skip_marker``
    would also stay True forever, permanently deprioritising a healthy task in the
    resolve order. The counters only mean anything within ONE continuous episode of
    unroutability.
    """
    ctx = getattr(task, "context", None) or {}
    if RUNNER_PULL_SKIP_CONTEXT_KEY not in ctx:
        return
    ctx = dict(ctx)
    ctx.pop(RUNNER_PULL_SKIP_CONTEXT_KEY, None)
    task.context = ctx
    flag_modified(task, "context")


def skip_count(task: Any, runner_id: str | None = None) -> int:
    """How many times ``task`` has been skipped by a runner pull."""
    try:
        return int(_marker(task, runner_id).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def has_skip_marker(task: Any, runner_id: str | None = None) -> bool:
    """Has THIS runner ever examined ``task`` and skipped it?

    Distinguishes NEVER-EXAMINED from examined-but-stale. The resolve budget goes
    to never-examined candidates first: without that, an expired marker outranks a
    never-seen candidate, the poll re-proves T1..T3 every TTL, and the scan window
    never slides far enough to reach a routable task deeper in the list — the
    head-of-line starvation again, just at a larger bound.
    """
    return bool(_marker(task, runner_id))


# Sorts BEFORE any real timestamp, so a marker we cannot date is re-probed first
# rather than being starved by markers we can.
#
# NAIVE, deliberately. This codebase stores TIMESTAMP WITHOUT TIME ZONE and
# ``_utcnow()`` returns a naive datetime, so every marker ``stamp_skip_reason``
# writes parses back naive. An AWARE sentinel compares fine against itself but
# blows up the moment one undatable marker shares the stale group with one datable
# one: ``sorted()`` in ``next_task`` raises "can't compare offset-naive and
# offset-aware datetimes" — an unhandled 500 on the pull fleet's dispatch
# endpoint, and a 500 is NOT the 503 a runner knows how to back off from.
_EPOCH = datetime.min  # noqa: DTZ901 - naive BY DESIGN; must match _utcnow().
# DTZ901's suggested remedy is `.replace(tzinfo=...)`, which would reintroduce the
# aware-vs-naive TypeError that 500s POST /api/runner/next-task. Do not 'fix' this.


def skip_seen_at(task: Any, runner_id: str | None = None) -> datetime:
    """When this runner last RESOLVED ``task`` and failed — the round-robin key.

    The stale group is drained OLDEST-EXAMINED FIRST, not in priority order.
    Priority order resonates with the TTL and is a starvation bug: in steady state
    every candidate carries a marker, so the stale group is the whole list, and
    exactly ``attempts`` markers come due per poll. Draining them by priority makes
    the rotation lock onto the first ``attempts * TTL / poll_interval`` candidates
    FOREVER (9, with the defaults). Everything past that is examined once and then
    frozen: never re-probed, so it never dispatches even when it becomes routable;
    and its ``count`` never advances, so it never alerts, never unpins, and never
    drains — it holds a candidate slot permanently. The effective scan bound
    collapses from 25 to 9.

    Oldest-first breaks the resonance: the rotation is fair, every candidate is
    re-probed, every counter advances, and the poisoned set actually drains.

    Priority within the stale group is close to meaningless anyway — every member
    was already proven unroutable, and priority still governs the ORDER OF THE SCAN
    and the never-examined group, which is where it decides who dispatches first.
    """
    seen = _marker(task, runner_id).get("seen_at")
    if not isinstance(seen, str):
        return _EPOCH
    try:
        # Coerced to naive UTC: a stored ISO timestamp may carry an arbitrary
        # offset (an operator hand-PATCHing task.context during an incident, a
        # 'Z'-suffix writer), and mixing an aware value into this sort key raises
        # TypeError and 500s the endpoint. Never trust the tz-ness of stored text.
        return _coerce_naive_utc(datetime.fromisoformat(seen))
    except (ValueError, TypeError):
        return _EPOCH


def skip_marker_is_fresh(
    task: Any, runner_id: str | None = None, now: datetime | None = None
) -> bool:
    """Was ``task`` proved unroutable by THIS runner within :data:`SKIP_RESOLVE_TTL_SECONDS`?

    When True the caller may reuse the recorded reason instead of re-running
    credential resolution — which is the expensive part (a ``Project`` roundtrip
    plus the global ``_max_plan_pool_lock``). Fail-open: an unparseable or
    missing marker returns False, so a bad marker can only cost a re-resolution,
    never make a task undispatchable.

    Reads ``seen_at`` (written on EVERY stamp), NOT ``at`` (the throttled
    escalation clock). Reading ``at`` here is what made this function return False
    at essentially every poll during a sustained storm — see the field docs above.
    ``at`` is the fallback purely so markers written before ``seen_at`` existed
    still parse.
    """
    marker = _marker(task, runner_id)
    seen = marker.get("seen_at") or marker.get("at")
    if not isinstance(seen, str):
        return False
    try:
        # Coerce INSIDE the try, and to naive UTC: the subtraction below is against
        # naive ``_utcnow()``, so an aware stored timestamp raises TypeError —
        # which a ValueError-only except never caught, so it escaped as a 500.
        seen_at = _coerce_naive_utc(datetime.fromisoformat(seen))
    except (ValueError, TypeError):
        return False
    return ((now or _utcnow()) - seen_at).total_seconds() < SKIP_RESOLVE_TTL_SECONDS


def cached_skip(task: Any, runner_id: str | None = None) -> tuple[str, str]:
    """``(code, reason)`` from the marker — the WHY for a fresh-skipped candidate."""
    rec = _marker(task, runner_id)
    return str(rec.get("code") or ""), str(rec.get("reason") or "")


def stamp_skip_reason(
    task: Task, code: str, reason: str, runner_id: str | None = None
) -> None:
    """Record WHY this pinned task was passed over — on the task itself.

    Without this, an operator sees a task sitting in PROVISIONING and a runner
    503-ing, with the reason computed and thrown away. ``latest_status_message``
    is the established operator-facing reason channel (it auto-appends to
    ``status_log``), so the WHY shows up in the admin UI next to the task.

    ``seen_at`` is written on EVERY call — it is the freshness clock that lets the
    next poll short-circuit this candidate instead of paying the global pool lock
    to re-derive a reason it already has.

    ``count`` (and its clock ``at``) advance at most once per
    :data:`SKIP_RESTAMP_SECONDS`, so ``count`` measures ELAPSED TIME rather than
    poll frequency, which is what makes the escalation thresholds mean ~10 min and
    ~20 min. The throttle is keyed on the TASK, not on the skip code: keying it on
    the code let a task flapping between ``no_eligible_account`` and
    ``credential_resolution_failed`` re-stamp on every poll and reach the alert in
    ~15s and the unpin in ~25s. The operator-facing message still refreshes
    immediately on a code change, so a task that flips from "rate-limited" to
    "poisoned payload" still surfaces that at once — only the COUNTER is throttled.

    NOTE (unsynchronized RMW): this rewrites the whole ``task.context`` dict on a
    row we deliberately do NOT hold a lock on (see ``next_task``). The realistic
    concurrent writer is this runner's own overlapping poll. The worst case is
    NOT merely a redundant resolution: if poll A skips task X while poll B WINS
    it, A's stale-context UPDATE can clobber B's ``apply_task_llm_runtime_context``
    stamp and lose the ``context["llm_runtime"]`` cost-attribution blob.
    ``llm_account_id`` is a separate column and survives, so attribution degrades
    rather than vanishing — and a dispatch is never lost, never double-issued, and
    never stuck, because the winner is decided by the row lock on a column
    (``started_at``) that this function does not touch. The unpin path, which DOES
    change task state, takes the row lock instead.
    """
    ctx = dict(task.context or {})
    prev = ctx.get(RUNNER_PULL_SKIP_CONTEXT_KEY)
    prev = prev if isinstance(prev, dict) else {}
    # A marker owned by a DIFFERENT runner describes different hardware's routing
    # (CLI inventory differs), so it must not seed this runner's counters — the
    # episode starts fresh here.
    if runner_id is not None:
        owner = prev.get("runner_id")
        if owner is not None and str(owner) != str(runner_id):
            prev = {}
    now = _utcnow()
    now_iso = now.isoformat()

    # Escalation clock: advance `count` only once per SKIP_RESTAMP_SECONDS.
    count = int(prev.get("count") or 0)
    count_at = prev.get("at")
    advance_count = True
    if isinstance(count_at, str):
        try:
            prev_at = _coerce_naive_utc(datetime.fromisoformat(count_at))
            if (now - prev_at).total_seconds() < SKIP_RESTAMP_SECONDS:
                advance_count = False
        except (ValueError, TypeError):
            pass  # unparseable/aware marker — fall through and re-stamp

    marker = {
        "code": code,
        "reason": reason[:500],
        # Whose verdict this is. Routability is runner-dependent, so another
        # runner must re-resolve rather than trust it (see _marker).
        "runner_id": str(runner_id) if runner_id is not None else None,
        # Freshness clock — ALWAYS advanced.
        "seen_at": now_iso,
        # Escalation clock — advanced with `count`.
        "at": now_iso if advance_count else (count_at or now_iso),
        # Preserved across re-stamps so an operator can see how long this task
        # has been unroutable, not just how many times it was counted.
        "first_at": prev.get("first_at") or now_iso,
        "count": count + 1 if advance_count else count,
    }
    ctx[RUNNER_PULL_SKIP_CONTEXT_KEY] = marker
    task.context = ctx
    flag_modified(task, "context")
    if advance_count or prev.get("code") != code:
        task.latest_status_message = f"Runner pull skipped ({code}): {reason}"[:500]


async def _maybe_create_skip_alert(
    session: AsyncSession, task: Task, count: int, code: str, reason: str
) -> bool:
    """Deduplicated WARNING alert for a task the runner pull keeps skipping.

    Deduplicated on ``(task_id, title, dismissed_at IS NULL)`` — the same shape
    ``maybe_create_decline_alert`` and every other task-scoped alert uses — so a
    task that stays unroutable raises exactly ONE alert per episode. The CALLER
    commits. Returns False on a dedup hit.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == SKIP_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return False

    msg = (
        f"This task is pinned to a local runner but has been skipped {count} "
        f"times by /next-task because its LLM credentials cannot be resolved "
        f"({code}): {reason}. It is occupying a slot in the runner's candidate "
        f"scan without ever dispatching. It will be unpinned back to QUEUED "
        f"after {SKIP_UNPIN_THRESHOLD} skips so it can re-dispatch elsewhere, "
        f"but the underlying cause — an exhausted account pool for this task's "
        f"model, or a payload that crashes credential resolution — needs fixing."
    )[:1000]
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=SKIP_ALERT_TITLE,
        message=msg,
        task_id=task.id,
        event_type="runner.degraded",
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    logger.warning(
        "[RUNNERS] Task %s skipped %d times by runner pull — raised operator "
        "alert. %s",
        task.id,
        count,
        msg,
    )
    return True


async def _unpin_skipped_task(session: AsyncSession, task: Task) -> bool:
    """Return a hopelessly-unroutable pinned task to QUEUED for re-dispatch.

    Re-selects the row ``FOR UPDATE SKIP LOCKED`` under the SAME claim guard the
    dispatch path uses (``PROVISIONING`` + ``started_at IS NULL``): a concurrent
    poll may have resolved and claimed this task since we read it unlocked, and
    unpinning a task that is booting an agent would strand it. Losing that race
    is a no-op, not an error.

    The local-runner REQUIREMENT (``strategic_local_runner_required`` and the
    compute require-local stamps) is deliberately left alone — only the pin to
    THIS runner is cleared. Clearing the requirement would force a cloud fallback,
    which is the eviction the hold-timeout sweep owns, not this one.
    """
    locked = (
        await session.execute(
            select(Task)
            .where(
                Task.id == task.id,
                Task.status == TaskStatus.PROVISIONING,
                Task.started_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().first()
    if locked is None:
        return False

    ctx = dict(locked.context or {})
    ctx.pop("runner_target_id", None)
    ctx.pop("runner_hostname", None)
    # DROP the marker outright: this episode is over. Keeping a zeroed marker would
    # leave `seen_at` recent, so a re-pin to the SAME runner within the TTL would
    # short-circuit a task we specifically want re-resolved on arrival — and
    # `has_skip_marker` would keep deprioritising it in the resolve order forever.
    # The unpin tally survives in its own key so the audit trail is intact without
    # a live marker (and without polluting the never-examined/stale partition).
    ctx.pop(RUNNER_PULL_SKIP_CONTEXT_KEY, None)
    ctx[RUNNER_PULL_UNPINS_CONTEXT_KEY] = (
        int(ctx.get(RUNNER_PULL_UNPINS_CONTEXT_KEY) or 0) + 1
    )
    ctx["runner_pull_unpinned_at"] = _utcnow().isoformat()
    locked.context = ctx
    flag_modified(locked, "context")
    locked.status = TaskStatus.QUEUED
    locked.vm_instance_id = None
    locked.vm_provisioned_at = None
    locked.latest_status_message = (
        "Unpinned from local runner after repeated pull skips "
        "(credentials unresolvable); returned to QUEUED for re-dispatch."
    )[:500]
    session.add(locked)
    logger.warning(
        "[RUNNERS] Task %s unpinned back to QUEUED after %d runner-pull skips "
        "— it was permanently unroutable and blocking the runner's candidate scan",
        locked.id,
        SKIP_UNPIN_THRESHOLD,
    )
    return True


async def maybe_escalate_pull_skip(
    session: AsyncSession,
    task: Task,
    code: str,
    reason: str,
    runner_id: str | None = None,
) -> str | None:
    """Alert and/or unpin a task the runner pull keeps skipping.

    Returns ``"unpinned"``, ``"alerted"``, or None. The CALLER commits — this is
    invoked from ``/next-task``, which already commits the skip stamps, so the
    escalation rides the same transaction.

    Escalation is what makes the bounded candidate scan safe. Without it a skip
    counter increments forever with no consumer, and enough permanently-poisoned
    pinned tasks silently reproduce the head-of-line starvation the scan exists
    to prevent.
    """
    count = skip_count(task, runner_id)
    if count < SKIP_ALERT_THRESHOLD:
        return None

    outcome: str | None = None
    try:
        if await _maybe_create_skip_alert(session, task, count, code, reason):
            outcome = "alerted"
    except Exception:
        logger.exception(
            "[RUNNERS] skip alert failed for task %s (non-fatal)", task.id
        )

    if count >= SKIP_UNPIN_THRESHOLD:
        try:
            if await _unpin_skipped_task(session, task):
                outcome = "unpinned"
        except Exception:
            logger.exception(
                "[RUNNERS] skip unpin failed for task %s (non-fatal)", task.id
            )
    return outcome
