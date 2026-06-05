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

from typing import TYPE_CHECKING

from app.models import MessageType
from app.time_utils import _utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from app.models import ProjectMessage

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


def is_open_system_bug_message(msg: "ProjectMessage") -> bool:
    """Return True if msg is an OPEN, system/agent-authored BUG_REPORT/BLOCKER.

    This is the eligibility predicate for the open-owned auto-resolve sweep. It
    is deliberately DISJOINT from the linked-convergence sweep, which selects
    ``status == LINKED_STATUS`` — this one selects ``status == OPEN_STATUS`` — so
    the two never overlap or double-process a message.
    """
    if not is_bug_class(msg):
        return False
    if getattr(msg, "status", None) != OPEN_STATUS:
        return False
    return getattr(msg, "from_type", None) in SYSTEM_AUTHORED_FROM_TYPES


def bug_message_cluster_signature(msg: "ProjectMessage") -> tuple[tuple[str, str], ...] | None:
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


def bug_messages_share_cluster(
    lhs: "ProjectMessage", rhs: "ProjectMessage"
) -> bool:
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
