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
on a fuzzy heuristic. When the signal is weak (no candidate qualifies) it returns
``None`` so good independent work is never falsely serialized. When MORE THAN ONE
candidate qualifies (genuine duplicate foundations), it deterministically returns
the CANONICAL provider — earliest by ``created_at``, tie-broken by ``str(id)`` —
matching the spawn-time resolver and the convergence path so every dependent
attaches to the SAME foundation rather than an arbitrary duplicate.

This is a pure matcher over a pre-fetched candidate list — it does no IO, which
keeps it cheap to call inside the sweep loop and trivial to unit-test.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.finding_deduplication import (
    prerequisite_matches_task,
    select_canonical_provider,
)
from app.landed_signal import has_merge_unblock_signal
from app.models import Task
from app.task_blocked import canonicalize_exact_task_ref, is_exact_task_ref

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
    """kind='task': the single candidate whose id, finding-hash, OR lineage matches ``ref``.

    Reuses the spawn-time resolver's explicit task-signal predicate
    (:func:`finding_deduplication.prerequisite_matches_task`) so a need that
    names its foundation by a non-UUID ``finding_hash`` — exactly how the
    producer records sibling/prerequisite needs — resolves here too, not only a
    raw task id. That predicate is LINEAGE-AWARE: it matches the provider's
    canonical ``finding_hash`` OR any hash in its recorded
    ``finding_lineage`` (:func:`finding_deduplication.task_finding_lineage`), so
    a need keyed by an ORIGINAL finding hash still resolves onto a retitled /
    split provider whose recomputed ``finding_hash`` has drifted (identity-drift;
    audit case 2). Matching stays EXPLICIT — recorded lineage hashes only, never
    fuzzy title overlap — and degrades gracefully to the bare exact-hash match on
    older providers that carry no ``finding_lineage`` entry. When more than one
    candidate shares the signal, the CANONICAL provider (earliest by
    ``created_at``, id-tiebroken) is returned so the resume-time matcher attaches
    the dependent to the SAME foundation the spawn-time resolver would pick.

    A non-exact ``ref`` (a title-like string rather than a task UUID / finding
    hash, per :func:`app.task_blocked.is_exact_task_ref`) can never identify a
    real task, so it short-circuits to ``None`` — the shared exact-task-ref
    policy applied at the resolver, never a fuzzy title match.
    """
    ref_text = str(ref).strip()
    if not is_exact_task_ref(ref_text):
        return None
    normalized = {
        "kind": "task",
        "ref": canonicalize_exact_task_ref(ref_text) or ref_text,
    }
    matches = [c for c in candidates if prerequisite_matches_task(normalized, c)]
    return select_canonical_provider(matches)


def _match_pr(ref: str, candidates: List[Task]) -> Optional[Task]:
    """kind='pr': the canonical candidate whose open PR equals ``ref``.

    When more than one candidate owns the number, the CANONICAL provider
    (earliest by ``created_at``, id-tiebroken) is returned so dependents
    deterministically serialize behind the same foundation rather than an
    arbitrary one.
    """
    try:
        pr_num = int(str(ref).strip().lstrip("#"))
    except (ValueError, AttributeError, TypeError):
        return None
    matches = [c for c in candidates if _candidate_pr_number(c) == pr_num]
    return select_canonical_provider(matches)


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

    When more than one candidate qualifies, the CANONICAL provider (earliest by
    ``created_at``, id-tiebroken) is returned so every dependent of duplicate
    capability providers deterministically attaches to the SAME foundation; no
    qualifying signal returns ``None`` so independent work is never falsely
    serialized.
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
    return select_canonical_provider(matches)


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


def need_is_structurally_unobservable(need: Dict[str, Any]) -> bool:
    """True iff NO provider class can EVER satisfy ``need``.

    A need is structurally unobservable when no future foundation, knowledge
    document, or registry capability could ever bind it — so the resume sweep
    re-arming the recheck on it would loop forever. This is the BOUNDED-OUTCOME
    liveness guard: a residual need matching nothing live/completed/lineage/
    knowledge/registry/dead AND proven unobservable here must reach a terminal
    disposition immediately rather than re-arm indefinitely. Two provable cases:

      * a malformed (non-dict) need — nothing can resolve it; and
      * an observation-class / explicitly structurally-dead need — an ``obs:``
        ref/label, or a ``structurally_dead`` flag (shared construction-time
        detector :func:`app.task_blocked._raw_need_is_structurally_dead`):
        observation findings never spawn a provider task, so no foundation can
        ever exist.

    Deliberately CONSERVATIVE: ANY other well-formed need is NEVER unobservable
    here. A ``capability`` need may be advertised by a future provider or the
    registry; an exact ``task`` / ``pr`` need may resolve to a foundation that
    has not appeared yet; and even a NON-exact ``kind=task`` ref can be satisfied
    by a durable KnowledgeDocument whose ``topic`` equals the ref (see
    :func:`knowledge_substance_satisfies_need`). Only the construction-time
    structurally-dead signal — never mere ref shape — proves unobservability, so
    independent work and knowledge-satisfiable waiters are never falsely
    terminated.
    """
    if not isinstance(need, dict):
        return True
    from app.task_blocked import _raw_need_is_structurally_dead

    return _raw_need_is_structurally_dead(need)


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


# Reasons a COMPLETED foundation that MATCHED a need nevertheless did not SHIP.
# These are the two (and only two) sub-states :func:`_foundation_shipped`
# excludes, named so a give-up can attribute the strand to what actually
# happened instead of to "nothing ever provided it".
UNSHIPPED_PR_NEVER_MERGED = "pr_never_merged"
UNSHIPPED_GHOST_MERGE = "ghost_merge_abandoned_rollup"


def unshipped_completed_foundation_for_need(
    task: Task,
    need: Dict[str, Any],
    completed_candidates: List[Task],
    failed_parent_ids: set | None = None,
) -> tuple | None:
    """Return ``(provider, unshipped_reason)`` for a need a terminal task DID
    produce but whose work never LANDED — or ``None``.

    This is the EXACT complement of :func:`completed_foundation_for_need`: same
    conservative exact-match signals (task id / finding hash / PR number /
    explicit capability marker), same candidate set, but matched against the
    foundations :func:`_foundation_shipped` EXCLUDES rather than the ones it
    admits. A need that matches here is not "unprovided" — a task ran, reached
    COMPLETED, and produced the artifact; the artifact simply never reached the
    dependent's base.

    Diagnostic only: it is deliberately NOT consulted by the resolution path
    (an unshipped foundation must keep failing to satisfy the need — that is the
    invariant the shipped gate exists to protect). It exists so a give-up can
    ATTRIBUTE the strand accurately, naming the provider that did the work.

    ``unshipped_reason`` is one of :data:`UNSHIPPED_PR_NEVER_MERGED` (the
    provider carries no merge-unblock signal — its PR is open or was closed
    unmerged) or :data:`UNSHIPPED_GHOST_MERGE` (it merged, but only into a
    rollup staging branch whose parent then FAILED/CANCELLED, so the code never
    reached the integration branch).
    """
    if not isinstance(need, dict):
        return None
    unshipped = [
        c
        for c in completed_candidates
        if c.id != task.id and not _foundation_shipped(c, failed_parent_ids)
    ]
    match = _match_need(need, unshipped)
    if match is None:
        return None
    if not has_merge_unblock_signal(match):
        return match, UNSHIPPED_PR_NEVER_MERGED
    return match, UNSHIPPED_GHOST_MERGE


def foundation_pr_number(candidate: Task) -> int | None:
    """Best-effort PR number a foundation task owns, or ``None``.

    Reads the two canonical homes and nothing else: the leading payload's
    ``pr_number`` (what :func:`_match_pr` matches on, written by
    :func:`task_pr_linkage.persist_task_pr_linkage`) and, failing that, the
    review-loop / rollup identity resolved by
    :func:`ci_merge_helpers.effective_pr_number`. Returns ``None`` rather than
    guessing when neither is recorded, so a caller must say the PR number is
    unrecorded instead of inventing one.
    """
    direct = _candidate_pr_number(candidate)
    if direct is not None:
        return direct
    from app.ci_merge_helpers import effective_pr_number

    raw = effective_pr_number(candidate)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None


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

    A non-death cancellation is NOT a genuine death and is excluded so it never
    triggers a termination: a requeue-replaced clone (``_replaced_by_task_id``),
    a clean "scope already covered" no-op (``failure_category == SCOPE_EMPTY``),
    or a co-deferred shelving (``_co_deferred_shelved``) whose work re-surfaces in
    a later cycle. The shared predicate
    :func:`app.failure_classification.is_non_death_cancellation` is the single
    source of this policy, so it cannot drift from the dispatch-time classifier.
    """
    if not isinstance(need, dict):
        return False
    from app.failure_classification import is_non_death_cancellation

    eligible = []
    for c in dead_candidates:
        if c.id == task.id:
            continue
        if is_non_death_cancellation(c):
            continue
        eligible.append(c)
    return _match_need(need, eligible) is not None


# Pipeline task types whose completion the EVALUATING original waits on. An
# ``original -> review/fix child`` edge is a real dependency even though it
# never appears in ``depends_on``: the original cannot leave EVALUATING until
# its pipeline reaches a verdict.
_PIPELINE_TASK_TYPES = frozenset({"review", "fix"})


def _pipeline_edges_by_original(
    task: Task, candidates: List[Task]
) -> Dict[str, List[str]]:
    """Map original-task id -> ids of its review/fix pipeline tasks.

    ``task`` itself is included because the BLOCKED dependent is not part of
    the active candidate set. This is exactly the incident shape (2026-08-19,
    fix task dd0e8588 / original c45d9719): a capability need on a pipeline
    fix task matched the fix task's own EVALUATING original as the provider.
    ``depends_on`` traversal alone cannot see that the original's completion
    is causally downstream of the fix task, so the wired edge deadlocked both
    rows until an operator cut it.
    """
    edges: Dict[str, List[str]] = {}
    for c in (task, *candidates):
        payload = c.first_payload or {}
        if str(payload.get("type") or "").lower() not in _PIPELINE_TASK_TYPES:
            continue
        orig_text = str(payload.get("original_task_id") or "").strip()
        if orig_text and orig_text != str(c.id):
            edges.setdefault(orig_text, []).append(str(c.id))
    return edges


def _would_create_cycle(task: Task, match: Task, candidates: List[Task]) -> bool:
    """True if adding ``task -> match`` would close a dependency cycle.

    A cycle is closed iff ``task`` is already reachable from ``match`` along
    existing ``depends_on`` edges OR implicit pipeline edges (an EVALUATING
    original depends on its spawned review/fix tasks — see
    :func:`_pipeline_edges_by_original`). We traverse the candidate set's edges
    (the in-flight foundations); a dep id outside the set is terminal for
    ``depends_on`` traversal (it cannot reach back into the active graph
    through an unloaded node, and the real dispatch-time cycle breaker —
    ``orchestrator_health._find_cycle_members`` — is the authoritative
    backstop). Pipeline edges are keyed by id, so they still apply to nodes
    outside the loaded candidate set.
    """
    by_id = {str(c.id): c for c in candidates}
    pipeline_edges = _pipeline_edges_by_original(task, candidates)
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
        frontier.extend(pipeline_edges.get(node, ()))
        node_task = by_id.get(node)
        if node_task is None:
            continue
        for dep in node_task.depends_on or []:
            frontier.append(str(dep))
    return False


def knowledge_substance_satisfies_need(
    need: Dict[str, Any],
    substance_index,
) -> bool:
    """True iff a durable KnowledgeDocument already holds the substance of this need.

    Only fires for ``kind=='task'`` needs — the incident failure class (a finding-hash
    or task-id ref whose originating task is CANCELLED). Matching is conservative and
    exact: the need's ref must equal a record's ``finding_hash`` OR a record's
    ``topic`` (no fuzzy/substring/keyword heuristics), so independent work is never
    falsely satisfied. ``substance_index`` is a pre-fetched list of
    ``KnowledgeSubstance`` records (or any objects with ``.finding_hash`` and
    ``.topic`` attributes); pass ``[]`` to short-circuit with ``False``.

    For ``compute_experiment`` records the gate is the durable-grounding predicate
    (durable citation present, no grounding miss, grounded > 0 when the probe ran) —
    NOT the stricter acceptance-evidence gate (``classify_knowledge_acceptance``,
    which further requires ``analysis_mode=='llm'``). Using the acceptance gate would
    permanently starve dependents whenever LLM analysis is unavailable (no
    ANTHROPIC_API_KEY or LLM disabled), since every compute document becomes a
    fallback in that environment. A grounded measured-only (``analysis_mode=fallback``)
    document therefore still satisfies a prerequisite need; it only fails the
    task-success scoring gate, not the dependency-unblock gate.
    """
    if not isinstance(need, dict):
        return False
    if need.get("kind") != "task":
        return False
    ref = need.get("ref")
    if not ref:
        return False
    ref = str(ref).strip()
    from app.knowledge_query import knowledge_substance_has_durable_compute_grounding

    for record in substance_index:
        source_kind = getattr(record, "source_kind", None)
        if source_kind == "compute_experiment" and (
            not knowledge_substance_has_durable_compute_grounding(record)
        ):
            continue
        if record.finding_hash and record.finding_hash == ref:
            return True
        if record.topic and record.topic == ref:
            return True
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
