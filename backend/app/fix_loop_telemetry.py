"""Per-task fix-loop telemetry ingest helpers.

The agent emits one ``FixLoopEventIn`` per fix-loop iteration (lint, test,
or Claude-driven ``check``). Events ride the existing heartbeat / status
endpoints; this module is the boundary that validates and folds them
into ``Task.fix_loop_history`` JSONB.

Shape on the column:
    {"lint":  [FixLoopAttempt, ...],
     "test":  [FixLoopAttempt, ...],
     "check": [FixLoopAttempt, ...]}

Append-or-replace by ``(loop, iteration)``: agents emit ``in_progress``
first, then a final terminal event for the same iteration. Per-loop
lists are capped so a runaway agent can't blow the column up.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task


# Recognized per-task fix loops. Anything outside this set is rejected at
# the Pydantic boundary so a typo can't silently create a new bucket.
FixLoopName = Literal["lint", "test", "check"]

# Recognized terminal exit reasons. Loose-typed (``Optional[str]``) on the
# ingest model so an unknown reason from a forward-rolled agent doesn't
# 422 the whole heartbeat — the mainstream values are documented here for
# downstream consumers.
EXIT_REASONS = (
    "passed",
    "early_unchanged",
    "max_attempts",
    "divergence_detected",
    "runaway_detected",
    "in_progress",
)

# Maximum attempts kept per loop in a single task's history. The agent's
# attempt cap is single-digits in normal operation; the runaway cap below
# protects the column against a buggy / divergent loop spamming events.
MAX_ATTEMPTS_PER_LOOP = 50

# Exit reasons that indicate the agent concluded the fix path is not converging.
# Importable by retry_budget.py and admin_digest.py so suppression logic and
# dashboard consumers share a single authoritative set.
DIVERGENCE_EXIT_REASONS: frozenset[str] = frozenset({
    "divergence_detected",
    "divergence_oscillation",
    "divergence_similar_error",
    "divergence_diff_revert",
    "runaway_detected",
    "same_wrong_fix",
})


def normalize_fix_loop_error_fingerprint(message: str | None) -> str:
    """Normalize fix-loop error text into a stable grouping fingerprint.

    Delegates to the existing failure-classification normalizer so UUIDs,
    numbers, and size tokens cluster identically across telemetry consumers.
    """
    from app.failure_classification import _fingerprint_message  # noqa: PLC0415

    return _fingerprint_message(message or "")


class FixLoopEventIn(BaseModel):
    """Ingest payload for one fix-loop attempt.

    Validated at the heartbeat / status boundary. The persisted shape on
    ``Task.fix_loop_history`` is the ``model_dump()`` of this object —
    keep field names stable across versions.
    """
    model_config = ConfigDict(extra="ignore")

    loop: FixLoopName
    iteration: int = Field(..., ge=1, le=1000)
    fingerprint_hash: Optional[str] = Field(None, max_length=64)
    error_tail: Optional[str] = Field(None, max_length=800)
    change_summary: Optional[str] = Field(None, max_length=2000)
    exit_reason: Optional[str] = Field(None, max_length=64)
    source: Literal["agent_emitted", "trace_synthesized"] = "agent_emitted"
    started_at: Optional[str] = Field(None, max_length=64)
    ended_at: Optional[str] = Field(None, max_length=64)


def apply_fix_loop_events(task: Task, events: List[FixLoopEventIn]) -> None:
    """Fold a list of ``FixLoopEventIn`` into ``task.fix_loop_history``.

    Behavior:
      - Append-or-replace by ``(loop, iteration)``. Later events for the
        same key overwrite earlier ones (agents emit ``in_progress``
        first, then a terminal event for the same iteration).
      - Per-loop lists capped at ``MAX_ATTEMPTS_PER_LOOP`` (oldest
        dropped) so a runaway loop can't grow the column unbounded.
      - Marks the column dirty for SQLAlchemy so JSONB mutation persists.

    Caller is expected to be inside an existing ``SELECT ... FOR UPDATE``
    window — this helper only touches the in-memory Task object.
    """
    if not events:
        return

    history: Dict[str, List[Dict[str, Any]]] = dict(task.fix_loop_history or {})
    for ev in events:
        loop = ev.loop
        bucket: List[Dict[str, Any]] = list(history.get(loop, []))
        new_entry = ev.model_dump(exclude_none=False)

        # Replace by (loop, iteration) so a terminal event overwrites the
        # prior in_progress emission for the same iteration.
        replaced = False
        for i, existing in enumerate(bucket):
            if existing.get("iteration") == ev.iteration:
                bucket[i] = new_entry
                replaced = True
                break
        if not replaced:
            bucket.append(new_entry)

        # Cap per-loop list. Drop oldest so the latest divergence trace is
        # always retained for diagnosis.
        if len(bucket) > MAX_ATTEMPTS_PER_LOOP:
            bucket = bucket[-MAX_ATTEMPTS_PER_LOOP:]

        history[loop] = bucket

    task.fix_loop_history = history
    flag_modified(task, "fix_loop_history")


def has_terminal_divergence(
    fix_loop_history: Dict[str, Any] | None,
) -> "tuple[str, str] | None":
    """Return ``(loop_name, exit_reason)`` when the most-recently-touched loop
    ended with a divergence-class reason, else ``None``.

    Conservative: only fires when the last non-``in_progress`` entry of the
    most-recently-touched loop (by ``ended_at``/``started_at`` timestamp) is in
    ``DIVERGENCE_EXIT_REASONS``.  Old divergence followed by a recent unrelated
    failure will NOT trigger abandonment because the newer loop sorts later.
    """
    if not fix_loop_history:
        return None

    # Collect (loop, last_terminal_reason, last_timestamp_str) for each loop.
    candidates: list[tuple[str, str | None, str]] = []
    for loop in ("lint", "test", "check"):
        bucket = fix_loop_history.get(loop) or []
        if not bucket:
            continue
        last_terminal: str | None = None
        for ev in reversed(bucket):
            if not isinstance(ev, dict):
                continue
            reason = ev.get("exit_reason")
            if reason and reason != "in_progress":
                last_terminal = reason
                break
        last_entry = bucket[-1] if isinstance(bucket[-1], dict) else {}
        ts = last_entry.get("ended_at") or last_entry.get("started_at") or ""
        candidates.append((loop, last_terminal, ts))

    if not candidates:
        return None

    # Most-recently-touched loop first (lexicographic ISO timestamp compare).
    candidates.sort(key=lambda x: x[2], reverse=True)
    most_recent_loop, most_recent_reason, _ = candidates[0]

    if most_recent_reason in DIVERGENCE_EXIT_REASONS:
        return (most_recent_loop, most_recent_reason)
    return None


def summarize_fix_loop_history(history: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    """Build a per-loop summary suitable for the admin read endpoint.

    For each known loop returns ``{"attempts": int, "last_exit_reason": str | None}``.
    Loops not present in the history are still included with zero attempts
    so the dashboard can render a stable shape.
    """
    summary: Dict[str, Dict[str, Any]] = {}
    history = history or {}
    for loop in ("lint", "test", "check"):
        bucket = history.get(loop) or []
        last_exit_reason: Optional[str] = None
        if bucket:
            last_exit_reason = bucket[-1].get("exit_reason") if isinstance(bucket[-1], dict) else None
        summary[loop] = {
            "attempts": len(bucket),
            "last_exit_reason": last_exit_reason,
        }
    return summary
