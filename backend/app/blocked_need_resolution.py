"""Resolve a blocked task's machine-resolvable NEED into a real depends_on edge.

A BLOCKED task can record *what* it is waiting on as a normalized
``{kind, ref}`` need (see :mod:`app.task_blocked`), but recording the need does
nothing on its own: without a ``depends_on`` edge to the in-flight foundation
that will satisfy it, the blocked-resume sweep can only re-dispatch a full agent
VM a bounded number of times before escalating-and-parking the task.

This leaf module turns that need into a structural dependency. Given a blocked
task and the set of currently active/queued/in-flight tasks in the same project,
:func:`resolve_blocked_need` returns the SINGLE task that satisfies the need (or
``None``). The caller (``reaper_sweeps.sweep_unblock_blocked_tasks``) then adds
that task's id to ``depends_on`` and requeues, so the EXISTING dispatch-time
dependency gate (``orchestrator_health._check_candidate_task_deps``) and
blocked-resume machinery carry the work to completion — no parallel waiting logic.

Matching is deliberately CONSERVATIVE: an explicit reference (task id, PR number)
is matched directly; a capability need is matched only on an exact/explicit
signal (payload type, exact title, or an exact related-file reference) and never
on a fuzzy heuristic. When the signal is ambiguous (more than one candidate) or
weak, it returns ``None`` so good independent work is never falsely serialized.

This is a pure matcher over a pre-fetched candidate list — it does no IO, which
keeps it cheap to call inside the sweep loop and trivial to unit-test.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.models import Task

# Candidate statuses an in-flight foundation can be in. Kept here (not imported
# from the sweep) so the candidate-enumeration contract lives next to the matcher
# that depends on it.
ACTIVE_CANDIDATE_STATUSES = ("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING")


def _candidate_pr_number(candidate: Task) -> Optional[int]:
    """Return the candidate's owned PR number from its leading payload, if any."""
    raw = (candidate.first_payload or {}).get("pr_number")
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _match_task(ref: str, candidates: List[Task]) -> Optional[Task]:
    """kind='task': the candidate whose id equals ``ref`` (exact id match)."""
    try:
        ref_id = uuid.UUID(str(ref).strip())
    except (ValueError, AttributeError, TypeError):
        return None
    for c in candidates:
        if c.id == ref_id:
            return c
    return None


def _match_pr(ref: str, candidates: List[Task]) -> Optional[Task]:
    """kind='pr': the single candidate whose open PR equals ``ref``.

    Ambiguity (more than one candidate owning the number) returns ``None`` — we
    never guess which in-flight task to serialize behind.
    """
    try:
        pr_num = int(str(ref).strip().lstrip("#"))
    except (ValueError, AttributeError, TypeError):
        return None
    matches = [c for c in candidates if _candidate_pr_number(c) == pr_num]
    return matches[0] if len(matches) == 1 else None


def _match_capability(
    ref: str, need: Dict[str, Any], candidates: List[Task]
) -> Optional[Task]:
    """kind='capability': match ONLY on an explicit/conservative signal.

    A capability need names a foundation by description rather than by id, so we
    refuse anything fuzzy. A candidate qualifies only when one of these EXACT
    signals holds:

      - its leading-payload ``type`` equals the capability ref, OR
      - its title exactly equals the ref (case-insensitive, trimmed), OR
      - the ref appears as an exact entry in its ``related_files``.

    Returns the match only when EXACTLY ONE candidate qualifies; ambiguity or no
    signal returns ``None`` so independent work is never falsely serialized.
    """
    ref_text = str(ref).strip()
    if not ref_text:
        return None
    ref_lower = ref_text.lower()

    matches: List[Task] = []
    for c in candidates:
        payload_type = (c.first_payload or {}).get("type")
        if isinstance(payload_type, str) and payload_type.strip().lower() == ref_lower:
            matches.append(c)
            continue
        if (c.title or "").strip().lower() == ref_lower:
            matches.append(c)
            continue
        related = (c.context or {}).get("related_files") or []
        if isinstance(related, list) and any(
            isinstance(f, str) and f.strip() == ref_text for f in related
        ):
            matches.append(c)
            continue
    return matches[0] if len(matches) == 1 else None


def _would_create_cycle(task: Task, match: Task, candidates: List[Task]) -> bool:
    """True if adding ``task -> match`` would close a ``depends_on`` cycle.

    A cycle is closed iff ``task`` is already reachable from ``match`` along
    existing ``depends_on`` edges. We traverse the candidate set's edges (the
    in-flight foundations); a dep id outside the set is terminal for traversal
    (it cannot reach back into the active graph through an unloaded node, and the
    real dispatch-time cycle breaker — ``orchestrator_health._find_cycle_members``
    — is the authoritative backstop).
    """
    by_id = {str(c.id): c for c in candidates}
    target = str(task.id)
    seen: set[str] = set()
    frontier = [str(match.id)]
    while frontier:
        node = frontier.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        node_task = by_id.get(node)
        if node_task is None:
            continue
        for dep in node_task.depends_on or []:
            frontier.append(str(dep))
    return False


def resolve_blocked_need(
    task: Task, need: Dict[str, Any], candidates: List[Task]
) -> Optional[Task]:
    """Return the single active candidate that satisfies ``need``, or ``None``.

    ``candidates`` is the pre-fetched set of same-project tasks in an active
    status (see :data:`ACTIVE_CANDIDATE_STATUSES`). Matching is conservative:
    explicit task-id / PR-number references resolve directly; a capability need
    resolves only on an exact signal. A self-edge or a candidate that would close
    a ``depends_on`` cycle is refused. When uncertain, returns ``None`` — a false
    edge that needlessly serializes independent work is worse than no edge.
    """
    if not isinstance(need, dict):
        return None
    kind = need.get("kind")
    ref = need.get("ref")
    if not kind or not ref:
        return None

    # Never wire a self-edge: a task can never be its own prerequisite.
    eligible = [c for c in candidates if c.id != task.id]

    if kind == "task":
        match = _match_task(ref, eligible)
    elif kind == "pr":
        match = _match_pr(ref, eligible)
    elif kind == "capability":
        match = _match_capability(ref, need, eligible)
    else:
        match = None

    if match is None:
        return None
    if match.id == task.id:  # defensive — also guarded above
        return None
    if _would_create_cycle(task, match, candidates):
        return None
    return match
