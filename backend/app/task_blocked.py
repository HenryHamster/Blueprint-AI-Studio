"""First-class BLOCKED task state — durable blocker metadata + transitions.

A task is BLOCKED when the work is wanted but not currently runnable because a
precondition is absent: an upstream dependency/rollup hasn't landed, a base
branch or referenced file doesn't exist yet, or the agent surfaced a
``BLOCKER:`` message. BLOCKED is distinct from FAILED — it does not count as a
code/infra failure, does not burn the retry budget, and is recoverable. The
in-transition (FAILED/agent-blocker -> BLOCKED) lives here alongside the
out-transition (BLOCKED -> QUEUED) so the pair is defined in one place; the
recurring recheck/escalation sweep that decides *when* to call the
out-transition is owned by the auto-unblock work.

Durable blocker metadata is stored under ``Task.context['blocker']`` so the
state is additive and backward-compatible (no migration required). The accessor
surface here is intentionally column-shaped (``waiting_on``, ``reason``,
``recheck_at``, ``deadline_at``, ``escalated``) so a later migration can promote
these keys to dedicated columns without changing call sites.

Backward compatibility: ``enter_blocked_state`` also writes the canonical
``BLOCKED_PRECONDITION`` failure class into ``context['failure_category']`` /
``context['failure_class']`` so existing consumers (digest metrics, analytics,
failure-taxonomy) keep classifying blocked work exactly as before, and so
historical rows that terminated FAILED with that class remain comparable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.models import Task, TaskStatus
from app.time_utils import _utcnow

# Canonical failure class for precondition blockers (mirrors
# app.failure_classification.BLOCKED_PRECONDITION; duplicated as a literal here
# to avoid importing the heavy classification module into this leaf helper).
BLOCKED_PRECONDITION = "BLOCKED_PRECONDITION"

# JSONB key under Task.context that holds the structured blocker metadata.
BLOCKER_CONTEXT_KEY = "blocker"

# Default interval before a blocked task is rechecked by the auto-unblock sweep.
DEFAULT_BLOCKER_RECHECK_SECONDS = 600
_UPSTREAM_REASON_MARKERS = (
    "upstream ",
    " rollup",
    "pr #",
    "pull request",
    "not yet merged",
    "dependency",
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _default_blocker_kind(*, waiting_on: str, reason: str, source: str) -> str:
    waiting_norm = (waiting_on or "").lower()
    reason_norm = (reason or "").lower()
    if (
        source == "parent"
        or "subtask" in waiting_norm
        or "dependency" in waiting_norm
        or "upstream" in waiting_norm
        or any(marker in reason_norm for marker in _UPSTREAM_REASON_MARKERS)
    ):
        return "upstream_dependency"
    return "precondition"


def get_blocker_metadata(task: Task) -> Dict[str, Any]:
    """Return a copy of the task's structured blocker metadata (``{}`` if none)."""
    meta = (task.context or {}).get(BLOCKER_CONTEXT_KEY)
    return dict(meta) if isinstance(meta, dict) else {}


def is_blocked(task: Task) -> bool:
    """True if the task is in the first-class BLOCKED state."""
    return task.status == TaskStatus.BLOCKED


def enter_blocked_state(
    task: Task,
    *,
    waiting_on: str,
    reason: str,
    source: str = "agent",
    blocker_kind: Optional[str] = None,
    failure_class: str = BLOCKED_PRECONDITION,
    recheck_in_seconds: Optional[int] = DEFAULT_BLOCKER_RECHECK_SECONDS,
    deadline_at: Optional[datetime] = None,
    escalated: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Transition ``task`` into the BLOCKED state and persist blocker metadata.

    Sets ``task.status = TaskStatus.BLOCKED`` and records what the task is
    waiting on, a human-readable reason, recheck timing, an optional escalation
    deadline, and an escalation marker into ``task.context['blocker']``. Returns
    the metadata dict written. Does NOT increment ``attempt_count`` and does NOT
    record a task/direction failure — BLOCKED is not a failure.

    The caller owns the session (``session.add`` / ``commit``) and any VM
    teardown / account-slot release.
    """
    now = now or _utcnow()
    blocker_code = (
        blocker_kind
        or _default_blocker_kind(waiting_on=waiting_on, reason=reason, source=source)
    )
    recheck_at = (
        now + timedelta(seconds=recheck_in_seconds) if recheck_in_seconds else None
    )
    reason_text = (reason or "")[:2000]
    meta: Dict[str, Any] = {
        "waiting_on": waiting_on,
        "reason": reason_text,
        "source": source,
        "blocker_kind": blocker_code,
        "failure_class": failure_class,
        "blocked_at": now.isoformat(),
        "recheck_at": _iso(recheck_at),
        "deadline_at": _iso(deadline_at),
        "escalated": bool(escalated),
    }
    ctx = dict(task.context or {})
    ctx[BLOCKER_CONTEXT_KEY] = meta
    # Preserve BLOCKED_PRECONDITION classification compatibility so historical
    # consumers (digest metrics, analytics, failure taxonomy) keep reading the
    # same failure class even though the task no longer terminates FAILED.
    ctx["failure_category"] = failure_class
    ctx["failure_class"] = failure_class
    task.context = ctx
    task.status = TaskStatus.BLOCKED
    task.blocker_kind = blocker_code[:64]
    task.blocked_reason = reason_text
    task.blocked_at = now
    task.recheck_after = recheck_at
    return meta


def mark_blocker_escalated(
    task: Task, *, detail: Optional[str] = None, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Flag a blocked task's metadata as escalated (e.g. deadline elapsed).

    No-op-safe on tasks that carry no blocker metadata yet — it seeds a minimal
    record so the escalation marker is never lost.
    """
    now = now or _utcnow()
    meta = get_blocker_metadata(task)
    meta["escalated"] = True
    meta["escalated_at"] = now.isoformat()
    if detail:
        meta["escalation_detail"] = detail[:2000]
    ctx = dict(task.context or {})
    ctx[BLOCKER_CONTEXT_KEY] = meta
    task.context = ctx
    return meta


def clear_blocker_metadata(task: Task) -> None:
    """Drop the structured blocker metadata from the task context."""
    task.blocker_kind = None
    task.blocked_reason = None
    task.blocked_at = None
    task.recheck_after = None
    if BLOCKER_CONTEXT_KEY not in (task.context or {}):
        return
    ctx = dict(task.context or {})
    ctx.pop(BLOCKER_CONTEXT_KEY, None)
    task.context = ctx


def unblock_to_queued(
    task: Task, *, message: Optional[str] = None
) -> None:
    """Out-transition: return a BLOCKED task to QUEUED once its precondition
    resolves.

    Clears the blocker metadata and re-queues the task for dispatch. The status
    change fires the model's revive callbacks (see models._on_status_set), which
    clear any JWT deny-list entry left by the BLOCKED transition. The sweep that
    decides *when* a precondition has resolved is owned by the auto-unblock work.
    """
    clear_blocker_metadata(task)
    task.status = TaskStatus.QUEUED
    task.latest_status_message = (
        message or "Unblocked — precondition resolved, requeued"
    )
