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

from typing import Any, Dict, List, Optional

from app.finding_deduplication import prerequisite_matches_task
from app.landed_signal import has_merge_unblock_signal
from app.models import Task

# Candidate statuses an in-flight foundation can be in. Kept here (not imported
# from the sweep) so the candidate-enumeration contract lives next to the matcher
# that depends on it.
ACTIVE_CANDIDATE_STATUSES = ("QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING")

# Statuses a foundation can be in once it has already LANDED. A parked blocker's
# need may now be satisfied by completed/merged work rather than by an in-flight
# candidate, so the parked re-evaluation path matches against this set too (see
# :func:`need_satisfied_by_completed`). A completed foundation needs no edge —
# the work is already done — so resumption simply lets the task proceed.
COMPLETED_CANDIDATE_STATUSES = ("COMPLETED",)

# Statuses a foundation is in once it has definitively DIED. A blocked dependent
# whose machine-resolvable need names a task now in one of these terminal states
# will NEVER be satisfied by it — it matches neither the active nor the completed
# set, so without this signal the parked re-evaluation re-arms forever (the
# existing dead-dependency cascade only fires when a real depends_on edge already
# exists). :func:`need_matched_dead_foundation` recognizes that provably-dead
# case so the dependent can be terminated like an edged dead dependency.
TERMINAL_CANDIDATE_STATUSES = ("FAILED", "CANCELLED")


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
    """kind='task': the single candidate whose id OR finding-hash equals ``ref``.

    Reuses the spawn-time resolver's explicit task-signal predicate
    (:func:`finding_deduplication.prerequisite_matches_task`) so a need that
    names its foundation by a non-UUID ``finding_hash`` — exactly how the
    producer records sibling/prerequisite needs — resolves here too, not only a
    raw task id. Ambiguity (more than one candidate sharing the signal) returns
    ``None`` so independent work is never falsely serialized.
    """
    normalized = {"kind": "task", "ref": str(ref).strip()}
    matches = [c for c in candidates if prerequisite_matches_task(normalized, c)]
    return matches[0] if len(matches) == 1 else None


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

      - it advertises the ref as an explicit capability marker
        (``provides_capability`` / ``capability`` — the producer-stamped signal,
        matched via the spawn-time resolver predicate), OR
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
    normalized = {"kind": "capability", "ref": ref_text}

    matches: List[Task] = []
    for c in candidates:
        if prerequisite_matches_task(normalized, c):
            matches.append(c)
            continue
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


def _match_need(need: Dict[str, Any], candidates: List[Task]) -> Optional[Task]:
    """Return the single candidate satisfying ``need`` by exact signal, or ``None``.

    Pure kind-dispatch over the per-kind matchers — NO self/cycle guard (callers
    that wire an edge apply those; the completed-work path needs no edge so it
    has nothing to guard). Conservative throughout: ambiguous or weak signals
    yield ``None``.
    """
    kind = need.get("kind")
    ref = need.get("ref")
    if not kind or not ref:
        return None
    if kind == "task":
        return _match_task(ref, candidates)
    if kind == "pr":
        return _match_pr(ref, candidates)
    if kind == "capability":
        return _match_capability(ref, need, candidates)
    return None


def _foundation_shipped(
    foundation: Task, failed_parent_ids: Optional[set] = None
) -> bool:
    """True iff a COMPLETED foundation's work actually SHIPPED.

    Aligns the blocked-resume notion of "done" with the one the respawn-dedup
    layer keys off (:func:`finding_deduplication.build_dedup_state`): a completed
    task only satisfies a dependent's prerequisite when its work is genuinely
    visible to that dependent. Two completed sub-states are NOT shipped and are
    excluded here, exactly as the dedup layer would RE-SPAWN them:

      * a COMPLETED task with NO merge-unblock signal (the PR closed/superseded
        without merging — nothing landed), and
      * a *ghost merge* — a PR that merged into a rollup staging branch whose
        rollup parent later FAILED/CANCELLED (or COMPLETED without merging), so
        the code never reached the integration branch. ``failed_parent_ids`` is
        the stranded-parent set; a foundation whose ``parent_task_id`` is in it is
        a ghost merge.

    Gate on the merge-UNBLOCK / rollup-unblock signal (``pr_merged``-equivalent,
    via :func:`has_merge_unblock_signal`), NOT the stricter integration-LANDING
    signal: a foundation that merged onto the SHARED staging base carries the
    rollup-unblock bit and IS genuinely visible to a same-base dependent, so it
    must still satisfy it. Using ``has_landed_signal`` here would wrongly park
    those same-base staging dependents.
    """
    if not has_merge_unblock_signal(foundation):
        return False
    if failed_parent_ids and foundation.parent_task_id in failed_parent_ids:
        return False
    return True


def completed_foundation_for_need(
    task: Task,
    need: Dict[str, Any],
    completed_candidates: List[Task],
    failed_parent_ids: Optional[set] = None,
) -> Optional[Task]:
    """Return the already-completed, SHIPPED task that satisfies ``need``, or ``None``.

    Same exact-match signals as the live path (task id / PR number / explicit
    capability marker) — never a fuzzy guess — but the candidate must also have
    actually shipped (see :func:`_foundation_shipped`): a COMPLETED-but-unmerged
    task or a ghost merge does NOT satisfy the need, mirroring the respawn-dedup
    layer that would rebuild such a foundation. Exposed (alongside the boolean
    :func:`need_satisfied_by_completed`) so a resume path can inspect the actual
    landed foundation — e.g. to decide whether a cross-base base re-cut is
    required before the dependent leaves BLOCKED.
    """
    if not isinstance(need, dict):
        return None
    eligible = [
        c
        for c in completed_candidates
        if c.id != task.id and _foundation_shipped(c, failed_parent_ids)
    ]
    return _match_need(need, eligible)


def need_satisfied_by_completed(
    task: Task,
    need: Dict[str, Any],
    completed_candidates: List[Task],
    failed_parent_ids: Optional[set] = None,
) -> bool:
    """True if ``need`` is satisfied by already-completed/merged work that SHIPPED.

    The live ``resolve_blocked_need`` path only considers active candidates, but a
    parked blocker may have outlived the foundation it waited on — the sibling
    task has since COMPLETED, or the PR it named has merged (its owning task is
    COMPLETED). In that case there is nothing in flight to depend on; the work is
    simply done, so the parked task should resume and proceed with no edge.

    Uses the SAME exact-match signals as the live path (task id / PR number /
    explicit capability marker) — never a fuzzy guess — AND the SAME "shipped"
    gate as the respawn-dedup layer (:func:`_foundation_shipped`): a
    COMPLETED-but-unmerged task or a ghost merge does NOT satisfy the need.
    ``completed_candidates`` is the pre-fetched set of same-project tasks in
    :data:`COMPLETED_CANDIDATE_STATUSES`; ``failed_parent_ids`` is the
    stranded-parent set used to exclude ghost merges.
    """
    return (
        completed_foundation_for_need(
            task, need, completed_candidates, failed_parent_ids
        )
        is not None
    )


def need_matched_dead_foundation(
    task: Task, need: Dict[str, Any], dead_candidates: List[Task]
) -> bool:
    """True if ``need`` names a foundation that has definitively FAILED/CANCELLED.

    The live (active) and completed (merged) resolution paths only consider work
    that may still land or already has; a parked blocker whose named foundation
    has since reached a terminal failed/cancelled state matches NEITHER and would
    re-arm forever. This recognizes that provably-dead case using the SAME exact
    signals as the other paths (task id / PR number / explicit capability marker)
    — never a fuzzy guess — so the legitimate "foundation might still appear" case
    is untouched. ``dead_candidates`` is the pre-fetched set of same-project tasks
    in :data:`TERMINAL_CANDIDATE_STATUSES`.

    A requeue-replaced cancellation (``_replaced_by_task_id``) or a clean
    "scope already covered" exit (``failure_category == SCOPE_EMPTY``) is NOT a
    genuine death — the work either continues under a replacement clone or was a
    deliberate no-op — so such candidates are excluded and never trigger a
    termination.
    """
    if not isinstance(need, dict):
        return False
    from app.failure_classification import SCOPE_EMPTY

    eligible = []
    for c in dead_candidates:
        if c.id == task.id:
            continue
        ctx = c.context or {}
        if ctx.get("_replaced_by_task_id"):
            continue
        if ctx.get("failure_category") == SCOPE_EMPTY:
            continue
        eligible.append(c)
    return _match_need(need, eligible) is not None


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

    # Never wire a self-edge: a task can never be its own prerequisite.
    eligible = [c for c in candidates if c.id != task.id]
    match = _match_need(need, eligible)

    if match is None:
        return None
    if match.id == task.id:  # defensive — also guarded above
        return None
    if _would_create_cycle(task, match, candidates):
        return None
    return match
