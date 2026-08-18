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

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, LocalRunner, Task, _utcnow
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
# evict a Strategic Agent task off local execution, defeating
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

_CAPABILITY_EPOCH_CONTEXT_KEY = "capability_epoch"
_CAPABILITY_EPOCH_KEYS = (
    "runner_source_sha",
    "runner_agent_source_sha",
    "project_verification_fingerprint",
)


class RunnerDeclineExclusions(set[str]):
    """Set-compatible exclusions carrying their immutable decline records.

    Callers historically passed a plain ``set[str]`` into runner selection.
    Keeping this as a set subclass preserves that public contract while letting
    the shared selector compare a deterministic decline with the runner's
    current capability epoch.  Hand-built/plain sets remain strict exclusions.
    """

    def __init__(self, runner_ids=(), *, records: dict | None = None):
        super().__init__(str(runner_id) for runner_id in runner_ids)
        self.records = records or {}


def _bounded_epoch_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:64] if value else None


def project_verification_proof_fingerprint(report: object) -> str | None:
    """Fingerprint the stable backend-verification proof used by retry receipts."""

    if not isinstance(report, dict):
        return None
    proof = {"backend_verification": report}
    return hashlib.sha256(
        json.dumps(
            proof, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()[:16]


def runner_capability_epoch(runner: Any | None) -> dict[str, str | None] | None:
    """Stable identity for the code/proof epoch that can execute a task.

    Load/heartbeat timestamps are deliberately absent.  A daemon self-update,
    an agent checkout update, or a changed functional project-verification
    proof is an authority change; an otherwise identical heartbeat is not.
    """

    if runner is None:
        return None
    capabilities = getattr(runner, "capabilities", None) or {}
    proof = capabilities.get("project_verification")
    epoch = {
        "runner_source_sha": _bounded_epoch_text(
            getattr(runner, "source_sha", None)
        ),
        "runner_agent_source_sha": _bounded_epoch_text(
            getattr(runner, "agent_source_sha", None)
        ),
        "project_verification_fingerprint": (
            project_verification_proof_fingerprint(proof)
        ),
    }
    # The all-null tuple is still a real, explicit epoch for an existing legacy
    # runner.  Persisting it distinguishes "this runner reported no versioned
    # proof yet" from a historical decline record that predates epoch fencing.
    return epoch


def _normalized_capability_epoch(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    if not any(key in value for key in _CAPABILITY_EPOCH_KEYS):
        return None
    epoch = {
        key: _bounded_epoch_text(value.get(key)) for key in _CAPABILITY_EPOCH_KEYS
    }
    return epoch


def decline_exclusion_applies_to_runner(
    exclusions: set[str], runner: Any
) -> bool:
    """Whether ``runner`` is still fenced by its recorded decline epoch.

    Transient and legacy records retain their historical behavior.  A
    deterministic record with an epoch stops excluding the same stable runner
    id only after its code/proof epoch changes.  The ordinary eligibility gate
    then independently requires the new epoch's functional proof to pass.
    """

    runner_id = str(getattr(runner, "id", ""))
    if runner_id not in exclusions:
        return False
    if not isinstance(exclusions, RunnerDeclineExclusions):
        return True
    record = exclusions.records.get(runner_id)
    if not isinstance(record, dict) or record.get("same_runner_retryable") is not False:
        return True
    declined_epoch = _normalized_capability_epoch(
        record.get(_CAPABILITY_EPOCH_CONTEXT_KEY)
    )
    if declined_epoch is None:
        # Pre-epoch deterministic records stay task-lifetime fenced.  Silently
        # guessing recovery from an unversioned marker would restore the loop.
        return True
    current_epoch = runner_capability_epoch(runner)
    return current_epoch is None or current_epoch == declined_epoch


def decline_exclusion_is_advisory_for_candidates(
    exclusions: set[str], task_type: str, candidates: list[Any]
) -> bool:
    """Epoch-aware Strategic waiver over the selector's current candidates.

    The task-only predicate cannot know that a deterministic record belongs to
    an obsolete runner epoch.  At selection time, ignore those stale/absent
    deterministic records so they cannot prevent a different runner's
    transient single-runner waiver.  A deterministic fence that still matches
    a current candidate remains non-advisory.
    """

    from app.task_capabilities import STRATEGIC_TASK_TYPES

    if (
        task_type not in STRATEGIC_TASK_TYPES
        or not isinstance(exclusions, RunnerDeclineExclusions)
    ):
        return False
    candidates_by_id = {str(runner.id): runner for runner in candidates}
    lifetime_transient_declines = 0
    for runner_id, record in exclusions.records.items():
        if not isinstance(record, dict):
            continue
        if record.get("same_runner_retryable") is False:
            runner = candidates_by_id.get(runner_id)
            if runner is not None and decline_exclusion_applies_to_runner(
                exclusions, runner
            ):
                return False
            continue
        # Preserve the task/fleet-wide lifetime bound: expired transient
        # declines and runners no longer in the candidate cache still count.
        # Only an epoch-obsolete deterministic record is ignored here.
        try:
            lifetime_transient_declines += int(record.get("count") or 1)
        except (TypeError, ValueError):
            lifetime_transient_declines += 1
    return lifetime_transient_declines < DECLINE_ESCALATION_THRESHOLD


def record_runner_decline(
    task: Task,
    runner_id: str,
    *,
    reason: str,
    hostname: str | None = None,
    same_runner_retryable: bool = True,
    runner: Any | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Persist one runner decline and return ``(runner_count, total_count)``.

    Both pre-claim runner declines and post-claim runtime incompatibilities must
    write the exact same context shape.  ``same_runner_retryable`` distinguishes
    a plausibly transient decline from a deterministic task/runner contract
    mismatch: Strategic dispatch may waive a transient exclusion on a
    single-runner fleet, but it must never waive a deterministic one.
    """
    ctx = dict(task.context or {})
    raw_records = ctx.get(DECLINED_BY_CONTEXT_KEY)
    records = dict(raw_records) if isinstance(raw_records, dict) else {}
    runner_key = str(runner_id)
    previous = records.get(runner_key)
    try:
        runner_count = int((previous or {}).get("count") or 0) + 1
    except (AttributeError, TypeError, ValueError):
        runner_count = 1

    clean_reason = (reason or "").strip() or "unspecified"
    record = {
        "count": runner_count,
        "at": (now or _utcnow()).isoformat(),
        "reason": clean_reason[:500],
        "hostname": hostname,
        "same_runner_retryable": bool(same_runner_retryable),
    }
    if not same_runner_retryable:
        capability_epoch = runner_capability_epoch(runner)
        if capability_epoch is not None:
            record[_CAPABILITY_EPOCH_CONTEXT_KEY] = capability_epoch
    records[runner_key] = record
    ctx[DECLINED_BY_CONTEXT_KEY] = records
    task.context = ctx
    flag_modified(task, "context")
    return runner_count, total_decline_count(task)


def recently_declined_runner_ids(
    task: Any, now: datetime | None = None
) -> set[str]:
    """Runner ids currently fenced from ``task`` dispatch.

    Transient declines expire after :data:`DECLINE_EXCLUSION_SECONDS`.
    Deterministic task/runner incompatibilities (``same_runner_retryable=False``)
    remain fenced for the task lifetime so a Strategic hold cannot outlive the
    generic TTL and periodically retry the same incapable runner.

    Returned ids are strings so callers can compare against ``str(runner.id)``
    without caring whether the marker survived a JSON round-trip. Unparseable
    transient entries are ignored (fail-open: a bad marker must never make a
    task undispatchable).
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
        if rec.get("same_runner_retryable") is False:
            excluded.add(str(runner_id))
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
    return RunnerDeclineExclusions(excluded, records=records)


async def record_deterministic_runner_decline(
    session: AsyncSession,
    task: Task,
    runner_id: str,
    *,
    reason: str,
    hostname: str | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Record a deterministic decline against the runner's current epoch.

    A missing/malformed runner id intentionally falls back to an unversioned,
    task-lifetime fence.  That is safer than making an incapable runner
    retryable when the server cannot prove which epoch emitted the failure.
    """

    runner = None
    try:
        runner_uuid = uuid.UUID(str(runner_id))
    except (TypeError, ValueError, AttributeError):
        runner_uuid = None
    if runner_uuid is not None:
        runner = await session.get(LocalRunner, runner_uuid)
    return record_runner_decline(
        task,
        runner_id,
        reason=reason,
        hostname=hostname,
        same_runner_retryable=False,
        runner=runner,
        now=now,
    )


def decline_record_for_runner(
    task: Any,
    runner_id: str,
    now: datetime | None = None,
    *,
    runner: Any | None = None,
) -> dict | None:
    """The active decline fence for ``runner_id`` on ``task``, or None.

    This mirrors :func:`recently_declined_runner_ids`: transient records are
    active within the TTL, while deterministic incompatibilities remain active
    for the task lifetime. Lets a caller report WHY a runner was dropped instead
    of leaving an unexplained hole.
    """
    ctx = getattr(task, "context", None) or {}
    records = ctx.get(DECLINED_BY_CONTEXT_KEY) or {}
    if not isinstance(records, dict):
        return None
    rec = records.get(str(runner_id))
    if not isinstance(rec, dict):
        return None
    if rec.get("same_runner_retryable") is False:
        exclusions = RunnerDeclineExclusions({str(runner_id)}, records=records)
        if runner is not None and not decline_exclusion_applies_to_runner(
            exclusions, runner
        ):
            return None
        active = dict(rec)
        active["seconds_left"] = None
        active["task_lifetime"] = True
        return active
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
    than :data:`DECLINE_ESCALATION_THRESHOLD` in total, with no live decline
    explicitly marking the same runner as deterministically incompatible).

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
    context = getattr(task, "context", None) or {}
    records = context.get(DECLINED_BY_CONTEXT_KEY) or {}
    if isinstance(records, dict):
        for runner_id in recently_declined_runner_ids(task):
            record = records.get(runner_id)
            if (
                isinstance(record, dict)
                and record.get("same_runner_retryable") is False
            ):
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
        event_type="runner.degraded",
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
