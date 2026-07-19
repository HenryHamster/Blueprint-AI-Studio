"""Convergence helpers for bug-class ProjectMessage linkage.

When the strategist decides to fix a BUG_REPORT or BLOCKER it should record a
durable forward link pointing to the direction or task that owns the fix, rather
than immediately stamping the message 'resolved'. The actual convergence to
'resolved' happens later when the owning outcome lands (a separate reaper sweep).

Status vocabulary (stored in ProjectMessage.status — a plain str column, no migration):
  'open'    — unowned, needs action from the strategist.
  'linked'  — owned by an outcome (direction or task), fix in-flight.
  'resolved'— terminal, fix landed or explicitly dismissed.

The forward link is stored in ProjectMessage.context[_CONVERGENCE_LINK_KEY] (JSONB).
No new DB columns or migrations are required.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlmodel import select

from app.message_audience import OPERATOR_ACTION_NEEDED
from app.models import MessageType, ProjectMessage
from app.project_message_projection import enum_value
from app.time_utils import _utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

# Minimum hex characters required for a short message reference. The admin UI
# surfaces the first 8 hex chars (``id.slice(0, 8)``); anything shorter is too
# collision-prone to resolve unambiguously, so we reject it loudly rather than
# match an arbitrary row.
MIN_MESSAGE_REF_HEXLEN = 8

# Status string for a message that has a durable forward link but is not yet resolved.
LINKED_STATUS = "linked"

# Message types that qualify as bug-class (can be linked to an owned outcome).
BUG_CLASS_TYPES: frozenset[str] = frozenset(
    {MessageType.BUG_REPORT.value, MessageType.BLOCKER.value}
)

# Status string for an unowned, action-needed message.
OPEN_STATUS = "open"

# Author types whose bug-class messages are pipeline-generated (system events or
# agent-surfaced findings) and therefore carry machine-readable ownership context
# (a direction id, or a pr_number + source_task_id). Operator-authored ("user")
# bug reports are excluded — they are never auto-resolved by ownership signals.
# Single source of truth for "system/agent-authored bug/blocker", shared by the
# open-owned auto-resolve sweep so the eligibility set is defined exactly once.
SYSTEM_AUTHORED_FROM_TYPES: frozenset[str] = frozenset({"system", "agent"})

# JSONB context key under which the convergence link is stored.
_CONVERGENCE_LINK_KEY = "_convergence_link"

# Allowed outcome kinds for a convergence link.
_VALID_LINK_KINDS: frozenset[str] = frozenset({"direction", "task"})

# Structured context fields used to conservatively cluster duplicate bug-class
# messages. A cluster only forms when at least one anchor field is present.
_CLUSTER_CONTEXT_KEYS: tuple[str, ...] = (
    "dedupe_key",
    "fingerprint",
    "branch",
    "direction_id",
    "source",
    "event_type",
)
_CLUSTER_ANCHOR_KEYS: frozenset[str] = frozenset(
    {"dedupe_key", "fingerprint", "branch", "direction_id"}
)


def is_bug_class(msg: "ProjectMessage") -> bool:
    """Return True if msg is a BUG_REPORT or BLOCKER."""
    msg_type = getattr(msg, "message_type", None)
    if msg_type is None:
        return False
    # message_type is a MessageType enum; .value gives the string.
    raw = msg_type.value if hasattr(msg_type, "value") else str(msg_type)
    return raw in BUG_CLASS_TYPES


def bug_message_cluster_signature(
    msg: "ProjectMessage",
) -> tuple[tuple[str, str], ...] | None:
    """Return a conservative structured signature for duplicate bug messages.

    The signature only includes stable context fields that already carry
    machine-generated identity. If no anchor field is present, the message is
    treated as unclusterable so ambiguous or low-signal messages stay separate.
    """
    if not is_bug_class(msg):
        return None
    ctx = msg.context if isinstance(msg.context, dict) else {}
    signature: list[tuple[str, str]] = []
    has_anchor = False
    for key in _CLUSTER_CONTEXT_KEYS:
        raw = ctx.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        signature.append((key, value))
        if key in _CLUSTER_ANCHOR_KEYS:
            has_anchor = True
    if not signature or not has_anchor:
        return None
    return tuple(signature)


def bug_messages_share_cluster(lhs: "ProjectMessage", rhs: "ProjectMessage") -> bool:
    """Return True when two bug-class messages have the same structured signature."""
    lhs_sig = bug_message_cluster_signature(lhs)
    if lhs_sig is None:
        return False
    return lhs_sig == bug_message_cluster_signature(rhs)


def link_message_to_outcome(
    msg: "ProjectMessage",
    kind: str,
    identifier: str,
    *,
    linked_at: "datetime | None" = None,
) -> None:
    """Record a durable forward link on a bug-class message and set its status to LINKED_STATUS.

    Mutates msg.context (JSONB) and msg.status in place.
    Does NOT call session.add() or commit — the caller owns those.

    Args:
        msg: A ProjectMessage ORM instance (BUG_REPORT or BLOCKER).
        kind: The outcome kind — 'direction' or 'task'.
        identifier: The direction id or task UUID string that owns the fix.
        linked_at: Optional timestamp (defaults to utcnow).

    Raises:
        ValueError: if kind is not in _VALID_LINK_KINDS.
    """
    if kind not in _VALID_LINK_KINDS:
        raise ValueError(
            f"link_message_to_outcome: invalid kind {kind!r}; "
            f"must be one of {sorted(_VALID_LINK_KINDS)}"
        )
    ctx = dict(msg.context or {})
    ctx[_CONVERGENCE_LINK_KEY] = {
        "kind": kind,
        "id": str(identifier),
        "linked_at": (linked_at or _utcnow()).isoformat(),
    }
    msg.context = ctx
    msg.status = LINKED_STATUS


def get_message_link(msg: "ProjectMessage") -> dict | None:
    """Return the convergence link from a message's context, or None if absent."""
    ctx = msg.context if isinstance(msg.context, dict) else {}
    link = ctx.get(_CONVERGENCE_LINK_KEY)
    if not isinstance(link, dict):
        return None
    return link


# ── Generalized message lifecycle (Stage 2, design d-7c851b3f) ────────────────
#
# The bug-class-only convergence machinery above (link_message_to_outcome /
# LINKED_STATUS / the Step-15 sweep) is generalized here so that ANY 'linked'
# message — not only BUG_REPORT/BLOCKER — auto-resolves once its recorded
# _convergence_link target genuinely LANDS, and so the operator-facing
# needs_action flag is DERIVED (never a schema column). These helpers are the
# shared decision layer both the generalized linked-resolve sweep and the
# stale-open decay sweep in reaper_sweeps.py consult, so the two stamp their
# resolutions and derive needs-action identically.

# Terminal status string for a resolved message.
RESOLVED_STATUS = "resolved"

# Message types that constitute an operator "needs action" item — mirror of
# operator_attention._NEEDS_OPERATOR_MESSAGE_TYPES, kept as string values here so
# this IO-light module stays import-cheap. needs_action is DERIVED, not stored.
NEEDS_ACTION_MESSAGE_TYPES: frozenset[str] = frozenset(
    {MessageType.QUESTION.value, MessageType.BLOCKER.value}
)

# Conversational-era message types that age out under stale-open decay. NOTE
# and FEEDBACK decay after STALE_OPEN_DECAY_DAYS (14d, 'auto-decayed (stale)').
# Unanswered QUESTION items use a longer window (STALE_QUESTION_DECAY_DAYS, 30d)
# and receive the resolution 'stale_unanswered' so operators can distinguish
# auto-resolved from genuinely-answered questions. BLOCKER is excluded (requires
# human resolution), and BUG_REPORT is owned by the bug-class convergence sweeps.
CONVERSATIONAL_DECAY_MESSAGE_TYPES: frozenset[str] = frozenset(
    {MessageType.NOTE.value, MessageType.FEEDBACK.value, MessageType.QUESTION.value}
)


def _message_type_value(msg: "ProjectMessage") -> str:
    raw = getattr(msg, "message_type", None)
    if raw is None:
        return ""
    return raw.value if hasattr(raw, "value") else str(raw)


def _message_status_value(msg: "ProjectMessage") -> str:
    raw = getattr(msg, "status", None)
    if raw is None:
        return ""
    return raw.value if hasattr(raw, "value") else str(raw)


def derive_needs_action(msg: "ProjectMessage") -> bool:
    """Return True when *msg* is an unresolved operator needs-action item.

    ``needs_action`` is DERIVED (never persisted): a message needs operator
    action when either (a) its ``message_type`` is QUESTION or BLOCKER, or
    (b) its audience context is ``operator-action-needed``; AND it has not been
    terminally resolved. This is the single source of truth consumed by both
    the in-memory attention model and the audience-routing layer.
    """
    if _message_status_value(msg) == RESOLVED_STATUS:
        return False
    if _message_type_value(msg) in NEEDS_ACTION_MESSAGE_TYPES:
        return True
    context = msg.context if isinstance(msg.context, dict) else {}
    return context.get("audience") == OPERATOR_ACTION_NEEDED


def mark_message_resolved(
    msg: "ProjectMessage", now: "datetime", resolution: str
) -> None:
    """Terminally resolve a message and stamp its audit trail in place.

    Mutates ``msg.status``/``resolution``/``resolved_at``/``context``; does NOT
    ``session.add()`` or commit — the caller owns those. Shared by the
    generalized linked-resolve and stale-decay sweeps so every auto-resolution
    records the same ``_convergence_converged_at`` audit key.
    """
    msg.status = RESOLVED_STATUS
    msg.resolution = resolution
    msg.resolved_at = now
    ctx = dict(msg.context or {})
    ctx["_convergence_converged_at"] = now.isoformat()
    msg.context = ctx


async def linked_outcome_landed_resolution(
    session: "AsyncSession", msg: "ProjectMessage", *, now: "datetime"
) -> str | None:
    """Audit string when *msg*'s ``_convergence_link`` target genuinely LANDED, else None.

    Generalizes the positive (convergence) half of
    ``sweep_converge_linked_bug_messages`` to ANY linked message: a linked
    outcome counts as resolved only when it actually *lands* —

      - Direction: ``status == 'completed'``
      - Task:      ``landed_signal.is_merged_completion(task)`` (COMPLETED + merged)

    An outcome that ended WITHOUT landing (failed/cancelled/retired/…), is still
    in-flight, missing, or malformed returns ``None`` — fail-safe, leaving the
    message untouched so manual resolve and stale-decay remain the fallback.
    ``now`` is accepted for signature parity with the sweep helpers and future
    time-bounded checks; the landing decision itself is time-independent.
    """
    from app.landed_signal import is_merged_completion
    from app.models import Direction, Task

    link = get_message_link(msg)
    if not link:
        return None
    kind = link.get("kind")
    outcome_id = link.get("id")
    if not kind or not outcome_id:
        return None

    if kind == "direction":
        dir_row = (
            (
                await session.execute(
                    select(Direction).where(
                        Direction.project_id == msg.project_id,
                        Direction.direction_key == str(outcome_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        if dir_row is not None and (dir_row.status or "active") == "completed":
            return f"Owning direction '{outcome_id}' completed"
        return None

    if kind == "task":
        try:
            task_uuid = uuid.UUID(str(outcome_id))
        except (ValueError, AttributeError, TypeError):
            return None
        task = await session.get(Task, task_uuid)
        if task is None:
            return None
        if is_merged_completion(task):
            return f"Owning task '{outcome_id}' completed and PR merged"
        return None

    return None


_HEX_RE = re.compile(r"^[0-9a-f]*$")


def _normalize_message_ref(ref: str) -> str:
    """Normalize a short ref or full UUID to a lowercase, dash-free hex prefix."""
    return str(ref or "").strip().lower().replace("-", "")


def _message_ref_candidates(
    matches: list["ProjectMessage"],
) -> list[dict[str, str | None]]:
    """Return deterministic disambiguators for ambiguous message refs.

    Each candidate includes a collision-free hex prefix (at least
    ``MIN_MESSAGE_REF_HEXLEN`` chars, extended only as far as needed) plus the
    full UUID so callers can retry resolution without recovering ids out of
    band.
    """
    ordered = sorted(matches, key=lambda msg: msg.id.hex)
    prefix_len = MIN_MESSAGE_REF_HEXLEN
    while prefix_len < 32:
        prefixes = {msg.id.hex[:prefix_len] for msg in ordered}
        if len(prefixes) == len(ordered):
            break
        prefix_len += 1

    candidates: list[dict[str, str | None]] = []
    for msg in ordered:
        candidates.append(
            {
                "id": str(msg.id),
                "ref": msg.id.hex[:prefix_len],
                "type": enum_value(getattr(msg, "message_type", "")),
                "from_type": str(getattr(msg, "from_type", "") or ""),
                "status": enum_value(getattr(msg, "status", "")),
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
        )
    return candidates


async def resolve_message_ref(
    session: "AsyncSession",
    project_id: uuid.UUID,
    ref: str,
) -> "ProjectMessage":
    """Resolve a short ref or full UUID to the one matching ProjectMessage.

    The reference may be a full UUID or a short hex prefix (case-insensitive,
    dashes ignored). Resolution is strictly project-scoped: the query filters
    on ``project_id`` FIRST so a ref from another project can never match (the
    caller surfaces that as a 404, never a 403, to avoid project enumeration).
    Matching spans ALL statuses (open, resolved, linked) — not just open.

    Raises:
        HTTPException(400): the normalized ref is empty or shorter than
            ``MIN_MESSAGE_REF_HEXLEN`` (too collision-prone to resolve).
        HTTPException(404): no message in the project starts with the prefix.
        HTTPException(409): more than one message matches (ambiguous prefix);
            the detail includes retryable candidate refs/full ids and NO row is
            touched.
    """
    prefix = _normalize_message_ref(ref)
    if not prefix:
        raise HTTPException(status_code=400, detail="message reference required")
    if len(prefix) < MIN_MESSAGE_REF_HEXLEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"message reference too short: need at least "
                f"{MIN_MESSAGE_REF_HEXLEN} hex characters"
            ),
        )
    if not _HEX_RE.match(prefix):
        raise HTTPException(
            status_code=400,
            detail="message reference must contain only hex characters (0-9, a-f)",
        )
    # Two-phase lookup: first fetch only IDs (cheap, no limit) scoped to the
    # project, match the prefix in Python, then load the full row(s) for any
    # hit. This avoids both the 500-row recency cap and loading full JSONB for
    # every candidate row.
    id_result = await session.execute(
        select(ProjectMessage.id).where(ProjectMessage.project_id == project_id)
    )
    all_ids: list[uuid.UUID] = list(id_result.scalars().all())
    matched_ids = [mid for mid in all_ids if mid.hex.startswith(prefix)]
    if not matched_ids:
        raise HTTPException(status_code=404, detail="Message not found")
    full_result = await session.execute(
        select(ProjectMessage).where(ProjectMessage.id.in_(matched_ids))
    )
    matches = list(full_result.scalars().all())
    if not matches:
        raise HTTPException(status_code=404, detail="Message not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Ambiguous message reference {prefix!r} matches multiple messages"
                ),
                "prefix": prefix,
                "min_hex_chars": MIN_MESSAGE_REF_HEXLEN,
                "candidates": _message_ref_candidates(matches),
            },
        )
    return matches[0]
