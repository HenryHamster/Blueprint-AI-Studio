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
  * And past :data:`MAX_PULL_UNPIN_CYCLES` of that recycling it stops recycling
    and BLOCKS the task with a machine-readable reason. Unpinning only helps when
    the cause is TRANSIENT; when no enabled account can serve the task's routed
    model, re-dispatch just re-pins it and the whole clock runs again. The
    lifetime unpin tally existed but was WRITE-ONLY, so nothing ever noticed the
    loop was not converging — which is how a task can sit in PROVISIONING for
    hours behind an accurate status message, in no terminal state and in no
    failed/blocked view. See :func:`_block_unroutable_task`.

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
# A DETERMINISTIC runner/task capability mismatch — e.g. a browser-required task
# pinned to a runner with no usable Chromium. Unlike the two codes above, which
# describe a TRANSIENT pool state that may heal on a later poll, a capability
# mismatch never becomes routable by re-polling THIS runner, so the pinned task is
# released to QUEUED immediately (see release_capability_mismatched_task) rather
# than counted toward the ~20-min escalation clock.
SKIP_CODE_CAPABILITY_MISMATCH = "runner_capability_mismatch"

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

# Full skip->unpin->re-dispatch cycles a task may burn before the platform stops
# recycling it and hands it to an operator as BLOCKED.
#
# The unpin at SKIP_UNPIN_THRESHOLD returns the task to QUEUED so the
# orchestrator can re-dispatch it. When the cause is TRANSIENT (a rate-limit
# storm, a credential refresh, one runner missing a CLI) that works: the next
# dispatch resolves and the episode ends. When the cause is STRUCTURAL — no
# enabled account anywhere can serve the task's routed provider/model — the
# re-dispatch pins the task to a runner again, it skips again, and ~25 minutes
# later it is unpinned again. Forever.
#
# That loop had no exit. RUNNER_PULL_UNPINS_CONTEXT_KEY was WRITE-ONLY: nothing
# in the codebase read it, so nothing ever concluded "recycling is not working"
# — which is how a task can hold PROVISIONING for hours behind a perfectly
# accurate status message, never reaching a terminal state and never appearing
# in any failed/blocked view (live: 2026-08-24, a research task whose only
# routed account had been manually disabled).
#
# Three cycles is ~75 minutes of continuous unroutability: long enough that any
# transient pool condition has healed and never reaches here, short enough that
# a structurally-unroutable task stops consuming dispatch slots while an
# operator still cares. It is deliberately counted in CYCLES, not in skips: a
# single episode's five skips is the transient budget, and only a repeat after a
# full re-dispatch is evidence the platform cannot fix this by itself.
MAX_PULL_UNPIN_CYCLES = 3

# Episode-scoped cycle counter feeding MAX_PULL_UNPIN_CYCLES. Distinct from the
# lifetime RUNNER_PULL_UNPINS_CONTEXT_KEY audit tally, which must never be reset
# (it is the audit trail). This one is cleared the moment the task DISPATCHES,
# because a task that actually ran proves the recycle worked and must start any
# future episode with a full budget.
RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY = "runner_pull_unpin_cycles"

# blocker_kind for a task the account pool structurally cannot route.
LLM_ROUTE_BLOCKER_KIND = "llm_route_unroutable"
# task.context key carrying the machine-readable WHY behind that block: the
# skip code, the reason text (which names the model and the offending account),
# the routed model/provider, and how many cycles were burned proving it.
LLM_ROUTE_BLOCK_CONTEXT_KEY = "llm_route_block"

# How long before a blocked-unroutable task is re-examined. The block is not
# terminal-forever: re-enabling the account, or fixing the routing so another
# provider can serve the model, clears it (see
# app.blocked_retry_authority.blocked_precondition_state).
LLM_ROUTE_RECHECK_SECONDS = 900

SKIP_ALERT_TITLE = "Task repeatedly skipped by runner pull"
LLM_ROUTE_ALERT_TITLE = "Task unroutable — no enabled LLM account for its model"


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
    if (
        RUNNER_PULL_SKIP_CONTEXT_KEY not in ctx
        and RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY not in ctx
    ):
        return
    ctx = dict(ctx)
    ctx.pop(RUNNER_PULL_SKIP_CONTEXT_KEY, None)
    # The episode-scoped unpin-cycle budget resets too: the task DISPATCHED, so
    # recycling demonstrably worked and a future unrelated episode must not
    # inherit a spent budget and give up early. The lifetime tally under
    # RUNNER_PULL_UNPINS_CONTEXT_KEY is left alone — it is the audit trail.
    ctx.pop(RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY, None)
    task.context = ctx
    flag_modified(task, "context")


def pull_unpin_cycles(task: Any) -> int:
    """How many skip->unpin->re-dispatch cycles this episode has burned.

    The READER that RUNNER_PULL_UNPINS_CONTEXT_KEY never had. A counter nobody
    reads cannot bound anything, which is exactly how the recycle loop became
    unbounded.
    """
    ctx = getattr(task, "context", None) or {}
    try:
        return int(ctx.get(RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY) or 0)
    except (TypeError, ValueError):
        return 0


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
        f"model, or a payload that crashes credential resolution — needs fixing. "
        f"After {MAX_PULL_UNPIN_CYCLES} unpin/re-dispatch cycles without ever "
        f"resolving, the task is BLOCKED rather than recycled again."
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


async def _relock_pinned_under_claim_guard(
    session: AsyncSession, task: Task
) -> Task | None:
    """Re-select a pinned task ``FOR UPDATE SKIP LOCKED`` under the claim guard.

    ``PROVISIONING`` + ``started_at IS NULL`` is the SAME guard the dispatch path
    uses. A concurrent poll may have resolved and claimed this task since the
    caller read it unlocked, and mutating a task that is booting an agent would
    strand it. Losing that race returns None — a no-op, not an error.
    """
    return (
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


async def _return_pin_to_queue(
    session: AsyncSession, task: Task, *, status_message: str, count_cycle: bool = False
) -> bool:
    """Re-lock a pinned task under the dispatch claim guard and return it to QUEUED.

    The shared body behind both the repeated-skip unpin and the deterministic
    capability-mismatch release. Re-selects the row ``FOR UPDATE SKIP LOCKED``
    under the SAME claim guard the dispatch path uses (``PROVISIONING`` +
    ``started_at IS NULL``): a concurrent poll may have resolved and claimed this
    task since the caller read it unlocked, and unpinning a task that is booting an
    agent would strand it. Losing that race is a no-op, not an error.

    The local-runner REQUIREMENT (``strategic_local_runner_required`` and the
    compute require-local stamps) is deliberately left alone — only the pin to
    THIS runner is cleared. Clearing the requirement would force a cloud fallback,
    which is the eviction the hold-timeout sweep owns, not this one.
    """
    locked = await _relock_pinned_under_claim_guard(session, task)
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
    if count_cycle:
        # Episode-scoped twin of the tally above, and the one that is actually
        # READ (see MAX_PULL_UNPIN_CYCLES). Cleared on dispatch; the lifetime
        # tally is not.
        #
        # Bumped ONLY by the repeated-skip unpin. A deterministic capability
        # release is a different loop with a different fix (dispatch routes
        # browser work to Chromium-capable runners), and letting it spend this
        # budget would BLOCK a task that is perfectly routable — turning a bound
        # on one broken loop into a false positive on another.
        try:
            _cycles = int(ctx.get(RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY) or 0)
        except (TypeError, ValueError):
            _cycles = 0
        ctx[RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY] = _cycles + 1
    ctx["runner_pull_unpinned_at"] = _utcnow().isoformat()
    locked.context = ctx
    flag_modified(locked, "context")
    locked.status = TaskStatus.QUEUED
    locked.vm_instance_id = None
    locked.vm_provisioned_at = None
    locked.latest_status_message = status_message[:500]
    session.add(locked)
    return True


async def _unpin_skipped_task(session: AsyncSession, task: Task) -> bool:
    """Return a hopelessly-unroutable pinned task to QUEUED for re-dispatch."""
    released = await _return_pin_to_queue(
        session,
        task,
        status_message=(
            "Unpinned from local runner after repeated pull skips "
            "(credentials unresolvable); returned to QUEUED for re-dispatch."
        ),
        count_cycle=True,
    )
    if released:
        logger.warning(
            "[RUNNERS] Task %s unpinned back to QUEUED after %d runner-pull skips "
            "— it was permanently unroutable and blocking the runner's candidate "
            "scan",
            task.id,
            SKIP_UNPIN_THRESHOLD,
        )
    return released


async def release_capability_mismatched_task(
    session: AsyncSession, task: Task, reason: str
) -> bool:
    """Release a pinned task the polling runner can NEVER satisfy back to QUEUED.

    A capability mismatch — e.g. a browser-required task pinned to a runner with no
    usable Chromium — is DETERMINISTIC: re-polling THIS runner will never make the
    task routable, so unlike the repeated-skip escalation there is no clock to run
    down. The pin is cleared on the first poll that detects the mismatch and normal
    dispatch — which already filters browser work to Chromium-capable runners —
    re-routes it (or holds it QUEUED until a capable runner has a free slot).
    Losing the winner-lock race (the task was claimed concurrently) is a no-op.
    """
    released = await _return_pin_to_queue(
        session,
        task,
        status_message=(
            f"Released from local runner (capability mismatch): {reason} "
            "Returned to QUEUED for re-dispatch to a capable runner."
        ),
    )
    if released:
        logger.warning(
            "[RUNNERS] Task %s released back to QUEUED — the pinned runner cannot "
            "satisfy a required capability: %s",
            task.id,
            reason,
        )
    return released


def _routed_pair_from_reason(reason: str) -> tuple[str | None, str | None]:
    """Pull ``(provider, model)`` out of the first attempt line of a skip reason.

    Attempt lines are built by ``max_plan_pool._note_attempt`` as
    ``"{provider}/{model}: {detail}"`` and joined with ``" | "``. Best-effort:
    the structured block record degrades to None rather than guessing.
    """
    head = (reason or "").split(" | ", 1)[0]
    prefix = head.split(":", 1)[0].strip()
    if "/" not in prefix:
        return None, None
    provider, _, model = prefix.partition("/")
    provider = provider.strip() or None
    model = model.strip() or None
    if model == "any":
        model = None
    return provider, model


def _unroutable_message(
    code: str, reason: str, cycles: int, provider: str | None, model: str | None
) -> str:
    """Operator-facing WHY — names the model, the provider, and the account."""
    target = (
        f"{provider}/{model}"
        if provider and model
        else (model or provider or "its routed provider/model")
    )
    return (
        f"Unroutable ({code}): no enabled LLM account can serve {target}. "
        f"Returned to the queue and re-dispatched {cycles} times without ever "
        f"resolving credentials, so it is BLOCKED instead of holding a "
        f"provisioning slot indefinitely. Runner reason: {reason}. "
        f"Fix by re-enabling the named account, or by routing this task type to "
        f"a provider that has one — the task unblocks automatically once the "
        f"pool can serve it."
    )


async def _create_unroutable_alert(
    session: AsyncSession, task: Task, message: str
) -> bool:
    """Deduplicated ERROR alert for a structurally unroutable task.

    ERROR, not WARNING: the repeated-skip alert says "this will be unpinned and
    retried". This one says "the platform has given up and a human must act",
    which is a different severity of claim and must not be filtered out with the
    routine degradation noise.
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == LLM_ROUTE_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return False
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.ERROR,
        title=LLM_ROUTE_ALERT_TITLE,
        message=message[:1000],
        task_id=task.id,
        event_type="runner.degraded",
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
    )
    return True


async def _block_unroutable_task(
    session: AsyncSession, task: Task, code: str, reason: str
) -> bool:
    """Stop recycling a structurally-unroutable task; BLOCK it with the WHY.

    BLOCKED, not FAILED: nothing is wrong with the task, the pool cannot serve
    it. BLOCKED is the durable holding state — it does not burn retry budget,
    it is visible to operators, and it self-heals when the precondition clears
    (``blocked_retry_authority.blocked_precondition_state``). Failing the task
    would destroy work over an account toggle.
    """
    # Imported lazily: task_blocked pulls in a wide slice of the model layer and
    # this module is imported by the runner router on a hot path.
    from app.task_blocked import enter_blocked_state

    locked = await _relock_pinned_under_claim_guard(session, task)
    if locked is None:
        return False

    ctx = dict(locked.context or {})
    cycles = int(ctx.get(RUNNER_PULL_UNPIN_CYCLES_CONTEXT_KEY) or 0)
    provider, model = _routed_pair_from_reason(reason)
    ctx.pop("runner_target_id", None)
    ctx.pop("runner_hostname", None)
    ctx.pop(RUNNER_PULL_SKIP_CONTEXT_KEY, None)
    ctx[LLM_ROUTE_BLOCK_CONTEXT_KEY] = {
        "code": code or SKIP_CODE_NO_ACCOUNT,
        "reason": (reason or "")[:1000],
        "provider": provider,
        "model": model,
        "unpin_cycles": cycles,
        "at": _utcnow().isoformat(),
    }
    locked.context = ctx
    flag_modified(locked, "context")

    message = _unroutable_message(
        code or SKIP_CODE_NO_ACCOUNT, (reason or "")[:400], cycles, provider, model
    )
    enter_blocked_state(
        locked,
        waiting_on="an enabled LLM account that can serve this task's routed model",
        reason=message,
        source="platform",
        blocker_kind=LLM_ROUTE_BLOCKER_KIND,
        recheck_in_seconds=LLM_ROUTE_RECHECK_SECONDS,
    )
    flag_modified(locked, "context")
    locked.vm_instance_id = None
    locked.vm_provisioned_at = None
    locked.latest_status_message = message[:500]
    session.add(locked)
    await _create_unroutable_alert(session, locked, message)
    logger.error(
        "[RUNNERS] Task %s BLOCKED as unroutable after %d unpin cycles "
        "(code=%s provider=%s model=%s): %s",
        locked.id,
        cycles,
        code,
        provider,
        model,
        (reason or "")[:500],
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
        # The unpin tally is now READ, not merely written. Past
        # MAX_PULL_UNPIN_CYCLES the recycle loop has provably failed to help, so
        # stop recycling and hand the task to an operator in a terminal-for-now
        # state with a machine-readable reason, instead of orbiting
        # PROVISIONING -> QUEUED -> PROVISIONING forever.
        if pull_unpin_cycles(task) >= MAX_PULL_UNPIN_CYCLES:
            try:
                if await _block_unroutable_task(session, task, code, reason):
                    return "blocked"
            except Exception:
                # Never strand: fall through to the ordinary unpin, which at
                # least keeps the runner's candidate scan clear.
                logger.exception(
                    "[RUNNERS] unroutable block failed for task %s (non-fatal)",
                    task.id,
                )
        try:
            if await _unpin_skipped_task(session, task):
                outcome = "unpinned"
        except Exception:
            logger.exception(
                "[RUNNERS] skip unpin failed for task %s (non-fatal)", task.id
            )
    return outcome
