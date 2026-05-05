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
