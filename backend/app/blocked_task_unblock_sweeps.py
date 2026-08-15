"""Blocked-task auto-unblock and escalation sweep helpers.

Extracted from reaper_sweeps.py (Step 12 and the related recovery sweep).
All public sweep entry points and markers are re-exported from
app.reaper_sweeps so existing import sites require no changes.
"""

import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlmodel import select

from app import pause_clock
from app.gate_revalidation import GATE_REVALIDATION_QUERY_MARKERS
from app.models import DeploymentState, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# ── Step 12: BLOCKED-task auto-unblock / escalation sweep ──────────────────
#
# A task enters the first-class BLOCKED state when a durable precondition is
# missing — an orchestrator-discovered conflict that persisted past the defer
# threshold (orchestrator_filters._record_defer_and_maybe_escalate) or an
# agent/lifecycle "BLOCKER:" with an upstream dependency. BLOCKED tasks are
# undispatchable on the normal queue path (the candidate query is QUEUED-only)
# and are excluded from stale-task reaping. This sweep is the recovery path:
# it cheaply rechecks each BLOCKED task at/after its recheck_after and either
# moves it back to QUEUED (precondition cleared) or re-arms a later recheck.
# A blocker that is permanently unsatisfiable (its dependency cascade-cancelled)
# or that outlives the escalation deadline gets ONE deduplicated operator alert
# and stops being rechecked.

# Base recheck interval, doubled per consecutive still-blocked recheck up to the
# cap. Keeps the sweep cheap (no per-cycle re-judging) while staying responsive.
_BLOCKED_RECHECK_BASE = timedelta(minutes=5)
_BLOCKED_RECHECK_MAX = timedelta(hours=1)
# A blocker that never clears within this window is escalated to the operator
# as permanently blocked (one ERROR alert) and parked.
_BLOCKED_ESCALATE_AFTER = timedelta(hours=6)


async def _blocked_escalation_due(task, session) -> bool:
    """True iff a BLOCKED task has exceeded the 6h escalate-and-park deadline in
    ACTIVE (pause-excluded) time.

    While the pipeline is PAUSED a BLOCKED task cannot be dispatched and its
    prerequisite work cannot merge, so it makes no progress toward clearing.
    Counting that pause time toward the deadline would escalate the task to the
    operator and PARK it (``_BLOCKED_ESCALATED_PARK`` ≈ 3650 days) purely because
    the operator paused — a false positive that strands a task that was only
    blocked by the pause. ``blocked_at`` is set at scattered sites with no
    cleanly-stampable baseline, so the shared pause_clock subtracts the currently-
    open pause window (the established open-window fallback): this fully prevents
    escalation WHILE paused; ``deploy_state=None`` → raw wall-clock (unchanged
    legacy/test behavior). An onset-stamped ``pause_baseline`` would upgrade this
    to exact across already-closed windows (see the qualification/promotion
    backstops for the same trade-off)."""
    if task.blocked_at is None:
        return False
    deploy_state = await session.get(DeploymentState, 1)
    active_seconds = pause_clock.active_elapsed_seconds(
        deploy_state, task.blocked_at, None
    )
    return active_seconds >= _BLOCKED_ESCALATE_AFTER.total_seconds()
# Parking horizon for terminally-escalated blockers. On escalation we push
# ``recheck_after`` far into the future (rather than leaving it NULL) so the row
# drops out of the due query (``recheck_after IS NULL OR <= now``) and is never
# re-SELECTed / FOR UPDATE-locked on the reaper hot path again. Leaving it NULL
# kept escalated rows permanently "due" (sorted first by nullsfirst), where they
# were re-locked every cycle and could crowd genuinely-due rechecks out of the
# batch-size window. Recovery has given up on these; an operator re-queues them.
_BLOCKED_ESCALATED_PARK = timedelta(days=3650)
# A parked (escalated) blocker that STILL carries a machine-resolvable need is
# re-examined at this LOW, bounded cadence instead of the multi-year park
# horizon, so that when the foundation it waited on finally lands — a sibling
# task, a merged PR, or shipped code now reachable on integration — the task
# resumes automatically rather than waiting for a manual operator requeue. The
# re-evaluation is cheap (one conservative exact-match pass) and never
# re-escalates or re-alerts, so it does not reintroduce per-cycle churn. Parked
# blockers with NO need keep the multi-year horizon and stay for the operator.
_BLOCKED_PARKED_REEVAL_INTERVAL = timedelta(hours=6)
# Bounded number of parked re-evaluations a still-unsatisfiable need is given
# before the dependent is terminated. A need that never becomes satisfiable —
# e.g. a capability no task ever advertises — would otherwise re-arm the parked
# re-evaluation at the low cadence forever. After this many parked re-evals with
# no resolution and no provably-dead foundation, the dependent reaches a bounded
# TERMINAL (CANCELLED) outcome instead of parking forever. The legitimate
# "foundation might still appear" case keeps its resume behavior within the
# bound — only an exhausted, never-satisfiable need terminates.
_BLOCKED_PARKED_REEVAL_CAP = 4
# A no-dependency agent/lifecycle blocker is often a TRANSIENT infra failure
# (orchestrator ./pr open 5xx, git push stale-lease) that clears on its own, but
# can also be a durable precondition (missing base branch/file). We can't tell
# them apart, so we optimistically requeue such a blocker up to this many times —
# a transient blip recovers on retry; a durable blocker re-blocks and, once the
# budget is spent, parks for the normal backoff + 6h operator escalation. The cap
# is deliberately small: re-dispatch costs a full agent VM run, so this trades a
# few wasted runs for auto-recovery instead of a multi-hour operator wedge.
_BLOCKED_OPTIMISTIC_RETRY_CAP = 3
# Durable context marker stamped on a blocked dependent that the resume system
# terminally CANCELS because it can provably never resume. The terminal
# transition clears all structured blocker metadata, so without this marker the
# give-up is invisible in aggregate (only a free-text status message + one
# deduplicated alert survive). The marker is queryable JSONB — no migration — and
# feeds the fleet-wide unresumable-cancellation health signal
# (``digest_metrics.compute_unresumable_cancellation_summary``). Observability
# only: stamping it changes nothing about WHEN or WHETHER a dependent is
# cancelled.
UNRESUMABLE_CANCEL_CONTEXT_KEY = "_unresumable_cancellation"
# Durable marker for the GOOD outcome, symmetric to the give-up marker above: a
# blocked dependent that successfully RESUMED (status → QUEUED) when its foundation
# landed. The transient latest_status_message and the in-memory ``requeued`` counter
# do not survive, so a regression where resumes stall looks identical to 'all
# healthy'. This queryable JSONB marker (no migration) feeds the dependency-health
# digest's positive resume signal (``digest_metrics.compute_blocked_resume_summary``).
# Observability only: stamping it changes nothing about WHEN or WHETHER a dependent
# resumes.
RESUME_MARKER_CONTEXT_KEY = "_blocked_resume"
# Give-up classes — the two reasons a blocked dependent can provably never resume.
GIVE_UP_FOUNDATION_DIED = (
    "foundation_died"  # needed foundation reached terminal FAILED/CANCELLED
)
GIVE_UP_NEVER_SATISFIABLE = (
    "never_satisfiable"  # need outlived its bounded parked re-evals
)

# Need kinds that name a FOUNDATION (a sibling task / finding-hash, or an explicit
# capability) rather than an external ref like a PR number. When one of these is
# cancelled as never_satisfiable, no task ever provided it — the dependent was
# stranded because the foundation was never created, which is first-class
# missing-foundation demand (see
# ``capability_demand.collect_missing_capability_demand``).
_FOUNDATION_NEED_KINDS = ("capability", "task")
# Additive sub-key stamped onto the UNRESUMABLE_CANCEL_CONTEXT_KEY marker when a
# never-satisfiable give-up cancelled a foundation-kind need: it flags an EXPLICIT
# missing-foundation retirement (stranded because nothing was ever built to satisfy
# the prerequisite), distinct from a give-up whose foundation existed and died.
# Purely additive — it never removes or renames an existing marker key, and the
# demand collector reads it to fold post-cancel demand back into the aggregate.
MISSING_FOUNDATION_MARKER_KEY = "missing_foundation"

# A recheck_after more than this far in the future is considered "hard-parked"
# by the old +10y sentinel path; any legitimate bounded recheck will be shorter.
_TRANSIENT_BLOCKER_PARK_SENTINEL = timedelta(days=365)


def _next_recheck_delay(attempts: int) -> timedelta:
    """Exponential backoff for the next recheck, capped at ``_BLOCKED_RECHECK_MAX``."""
    secs = _BLOCKED_RECHECK_BASE.total_seconds() * (2 ** max(0, attempts))
    return timedelta(seconds=min(secs, _BLOCKED_RECHECK_MAX.total_seconds()))


async def _blocked_precondition_state(task, session, verify_pr_merged) -> str:
    """Classify whether a BLOCKED task's recorded precondition is now satisfied.

    Returns one of:
      - ``"clear"``   — the precondition is satisfied (or is a transient conflict
                        with no dependency edge); requeue and let dispatch run/
                        re-judge it.
      - ``"retry"``   — a no-dependency agent/lifecycle blocker that may be a
                        transient infra failure; the driver optimistically
                        requeues it a bounded number of times before parking.
      - ``"waiting"`` — an upstream dependency is still in flight; stay BLOCKED.
      - ``"dead"``    — an upstream dependency genuinely failed / was cancelled,
                        so this work is impossible; escalate terminally.

    Reuses the single source of truth for dependency resolution
    (``orchestrator_health._check_candidate_task_deps``) rather than
    reimplementing merge/cancel/scope-empty semantics here.
    """
    deps = task.depends_on or []
    blocker_meta = (task.context or {}).get("blocker") or {}
    blocker_source = (
        blocker_meta.get("source") if isinstance(blocker_meta, dict) else None
    )
    if not deps:
        if task.blocker_kind in {"file_conflict", "pending_rollup"}:
            # Orchestrator-owned blockers with no dependency edges are still
            # safe to requeue optimistically: dispatch re-judges and re-blocks
            # them immediately if the conflict is still live.
            return "clear"
        if blocker_source == "parent":
            children = (
                (
                    await session.execute(
                        select(Task).where(Task.parent_task_id == task.id)
                    )
                )
                .scalars()
                .all()
            )
            for child in children:
                child_ctx = child.context or {}
                if child.status == TaskStatus.BLOCKED:
                    return "waiting"
                if child.status == TaskStatus.FAILED and (
                    child_ctx.get("failure_category") == "BLOCKED_PRECONDITION"
                    or child_ctx.get("failure_class") == "BLOCKED_PRECONDITION"
                    or (child.latest_status_message or "")
                    .lstrip()
                    .lower()
                    .startswith("blocker:")
                ):
                    return "waiting"
            # Child blockers have cleared; move the parent back into the
            # rollup/re-evaluation path instead of normal dispatch.
            return "re_evaluate"
        if verify_pr_merged is None:
            return "waiting"
        # AGENT-surfaced "BLOCKER:" with no explicit dependency edge (source set
        # by agent_status.py): these are often TRANSIENT infra failures
        # (orchestrator ./pr open 5xx, git push stale-lease) that clear on their
        # own, but can also be durable preconditions (missing base branch/file).
        # We can't reliably tell them apart from here, so signal a BOUNDED
        # optimistic requeue: the driver retries up to
        # _BLOCKED_OPTIMISTIC_RETRY_CAP times before parking + escalating. A
        # transient blip recovers on retry; a durable blocker re-blocks and is
        # parked once the retry budget is spent. (Previously this returned
        # "waiting" unconditionally, which left a transient infra blip wedging
        # its EVALUATING rollup parent for hours until an operator manually
        # requeued — incident 2026-06-04, tasks 87ddb2a4/18c8c195.) Scoped to
        # source=="agent" so parent-source blockers and metadata-less blocked
        # subtasks keep their existing "wait for explicit recovery" semantics.
        if blocker_source == "agent":
            from app.execution_capability_requirements import (
                CAPABILITY_BACKEND_VERIFICATION,
                learned_execution_capabilities,
            )

            learned_capabilities = learned_execution_capabilities(task.context)
            if learned_capabilities:
                from app.orchestrator_runner_dispatch import (
                    _load_active_runners_cache,
                    _select_eligible_runner,
                )
                from app.runner_decline import recently_declined_runner_ids

                active_runners = await _load_active_runners_cache(session)
                declined_ids = recently_declined_runner_ids(task)
                task_type = str((task.first_payload or {}).get("type") or "")
                if _select_eligible_runner(
                    active_runners,
                    task.project_id,
                    task_type,
                    None,
                    {},
                    declined_runner_ids=declined_ids,
                    requires_project_verification=(
                        CAPABILITY_BACKEND_VERIFICATION in learned_capabilities
                    ),
                ):
                    return "retry"
                # A stale positive heartbeat from the contradicted runner is
                # not recovery evidence. Stay parked until another currently
                # capable runner appears.
                return "waiting"
            blocker_text = " ".join(
                str(value or "")
                for value in (
                    task.blocked_reason,
                    task.latest_status_message,
                    blocker_meta.get("reason")
                    if isinstance(blocker_meta, dict)
                    else None,
                )
            )
            if "VIEWPORT_GATE_INFRA_UNAVAILABLE" in blocker_text:
                # The VM viewport gate is retired: browser capture is now an
                # advisory CI-owned check. Clear legacy rows immediately rather
                # than preserving their obsolete runner-capability park.
                return "clear"
            return "retry"
        return "waiting"
    if verify_pr_merged is None:
        return "waiting"

    from app.orchestrator_health import _check_candidate_task_deps

    dep_uuids = []
    for d in deps:
        try:
            dep_uuids.append(uuid.UUID(str(d)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not dep_uuids:
        return "clear"
    dep_tasks = (
        (await session.execute(select(Task).where(Task.id.in_(dep_uuids))))
        .scalars()
        .all()
    )
    result, _ = await _check_candidate_task_deps(
        task, dep_tasks, dep_uuids, session, verify_pr_merged
    )
    if result == "eligible":
        return "clear"
    if result == "cascade_cancel":
        return "dead"
    return "waiting"


def _clear_blocked_fields(task, *, preserve_first_blocked: bool = False) -> None:
    """Reset a task's durable blocker metadata and defer bookkeeping on unblock.

    Delegates the structured ``blocker`` record and the BLOCKED_PRECONDITION
    compat-tag clearing to ``task_blocked.clear_blocker_metadata`` — the single
    source of truth for "what a clean out-transition drops" — so the reaper's
    recovery path and the canonical out-transition cannot drift. This sweep
    then clears its own recheck/escalation bookkeeping keys on top.

    **Keys preserved vs. cleared by ``preserve_first_blocked``:**

    When ``preserve_first_blocked=True`` (optimistic requeue — a conflict/rollup
    blocker with no resolved dependency edge, which dispatch may immediately
    re-block), four context keys survive the transition:

    - ``_blocked_first_at`` — the original onset timestamp; preserved so the
      escalation deadline is measured from the true start of the block episode
      rather than restarting on every requeue.
    - ``_blocked_optimistic_retries`` — the count of optimistic requeue attempts;
      preserved so a transient agent/lifecycle blocker keeps accumulating toward
      its cap across requeue cycles.
    - ``_precondition_defer`` — the per-blocker-kind defer counter; preserved so
      a persistent conflict/rollup blocker escalates to FAILED instead of
      cycling forever.
    - ``_precondition_escalated_key`` — the blocker kind that triggered
      escalation; **requires special handling** because
      ``task_blocked.clear_blocker_metadata`` calls
      ``strip_fresh_attempt_context`` internally, which unconditionally strips
      this key (it is in ``FRESH_ATTEMPT_CONTEXT_STRIP_KEYS``). This function
      therefore snapshots the value *before* calling ``clear_blocker_metadata``
      and restores it afterward when ``preserve_first_blocked=True``.

    Note that ``clear_blocker_metadata`` itself always calls
    ``strip_fresh_attempt_context`` with ``preserve_blocked_optimistic_retries=True``,
    which means ``_blocked_optimistic_retries`` is never removed by
    ``clear_blocker_metadata`` alone — it survives every unblock transition and
    is only wiped here when ``preserve_first_blocked=False`` (a genuine
    dependency clear), so a later unrelated block starts with a fresh budget.

    When ``preserve_first_blocked=False`` (genuine dependency clear), all four
    keys above are removed, giving the next block episode a clean slate.
    """
    from app.task_blocked import clear_blocker_metadata

    # Explicitly snapshot _precondition_escalated_key BEFORE clear_blocker_metadata
    # runs: strip_fresh_attempt_context (called inside clear_blocker_metadata)
    # unconditionally removes it because it is in FRESH_ATTEMPT_CONTEXT_STRIP_KEYS.
    # We save it here and restore it below (line 288) when preserve_first_blocked=True.
    # The defer counter (_precondition_defer) survives passively: it is NOT in
    # FRESH_ATTEMPT_CONTEXT_STRIP_KEYS and is only deleted by the keys-to-pop list
    # when preserve_first_blocked=False, so it persists when requeue-optimizing.
    ctx_before = task.context or {}
    saved_escalated_key = (
        ctx_before.get("_precondition_escalated_key")
        if preserve_first_blocked
        else None
    )

    # Drops blocker columns, the structured blocker record, and the stale
    # BLOCKED_PRECONDITION failure_class/failure_category tags.
    clear_blocker_metadata(task)
    ctx = task.context or {}
    keys = [
        "_blocked_escalated",
        "_blocked_recheck_attempts",
    ]
    if not preserve_first_blocked:
        keys.append("_blocked_first_at")
        # The optimistic-retry budget and evidence timestamp are preserved across
        # an onset-preserving requeue (so a transient agent/lifecycle blocker keeps
        # counting toward its cap), but wiped on a genuine dependency clear so a
        # later, unrelated block starts with a fresh budget.
        keys.append("_blocked_optimistic_retries")
        keys.append("_blocked_last_retry_at")
        # The defer counter and escalation stamp must also survive an
        # onset-preserving (optimistic) requeue: if dispatch re-blocks on the
        # same blocker identity, _record_defer_and_maybe_escalate increments
        # from the preserved count rather than restarting at 1, so a persistent
        # conflict/rollup blocker escalates to FAILED instead of cycling forever.
        keys.append("_precondition_defer")
        keys.append("_precondition_escalated_key")
    for k in keys:
        ctx.pop(k, None)
    # Restore the escalated-key for optimistic requeues: it was stripped above by
    # clear_blocker_metadata but must survive so dispatch can accumulate the defer
    # counter toward the escalation threshold on repeated conflict re-blocks.
    if preserve_first_blocked and saved_escalated_key is not None:
        ctx["_precondition_escalated_key"] = saved_escalated_key
    task.context = ctx


async def _fetch_active_candidates(session, project_id) -> list:
    """Active/in-flight tasks in a project — the foundations a need can resolve to.

    Statuses mirror ``blocked_need_resolution.ACTIVE_CANDIDATE_STATUSES``: a need
    can only resolve onto work that is still going to land (QUEUED/PROVISIONING/
    IN_PROGRESS/EVALUATING), never onto already-terminal rows.
    """
    from app.blocked_need_resolution import ACTIVE_CANDIDATE_STATUSES

    active = [TaskStatus(s) for s in ACTIVE_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(active),
                )
            )
        )
        .scalars()
        .all()
    )


async def _fetch_knowledge_substance_index(session, project_id) -> list:
    """Substance index for durable KnowledgeDocuments in a project.

    Delegates to :func:`knowledge_query.fetch_knowledge_substance_index` so the
    SQL join lives in one place and the result is cached per project across the
    sweep batch (exactly as the active/completed/dead candidate caches are).
    """
    from app.knowledge_query import fetch_knowledge_substance_index

    return await fetch_knowledge_substance_index(session, project_id)


async def _fetch_working_capability_names(session, project_id) -> set:
    """Canonical names of registry capabilities the project declares ``working``.

    Drawn from the strategist-curated ``project.problem_model.capabilities``
    registry — the same vocabulary agents declare prerequisite refs from. A
    ``working`` capability already exists in the codebase, so a capability need
    naming one is satisfied by the repo itself (no provider task required).
    Cached per project across the sweep batch like the candidate sets.
    """
    from app.capability_registry import working_capability_names
    from app.models import Project

    project = await session.get(Project, project_id)
    return working_capability_names(
        getattr(project, "problem_model", None),
        proof_records=getattr(project, "capability_proof_records", None),
    )


def _fold_proof_records_tier2(
    status_by_name: dict, proof_records: object, now: object = None
) -> dict:
    """Upgrade an evidence_refs-derived proof map with durable proof_records.

    ``record_capability_proof`` stamps a landed provider's proof into BOTH the
    capability's ``evidence_refs`` (tier-1, read by
    :func:`working_capability_proof_status`) AND the project-level
    ``capability_proof_records`` column (tier-2). The evidence_refs append is
    fail-open — if the problem_model shape blocks it, the proof survives ONLY in
    ``capability_proof_records``. The release-qualifier and badge seams already
    consult that tier-2 fallback (:func:`enrich_problem_model_capability_proofs`),
    so the blocked-resume enforce gate must too — otherwise an enforce-mode
    dependent is stranded BLOCKED on a capability the other two seams treat as
    proven.

    Only ``working`` capabilities already present in ``status_by_name`` are
    considered (the tier-1 map is authoritative on WHICH capabilities exist and
    are working), and a tier-2 status is applied only when it is strictly stronger
    than the tier-1 status — so a landed proof never regresses a capability the
    evidence_refs already classify more highly.

    Freshness parity with the tier-1 gate: each proof record's ``proved_at`` is
    carried into the synthetic ref as ``recorded_at`` and ``now`` is threaded into
    the tiered classifier, so a stale tier-2 proof-record (older than
    :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL`) can no longer resurrect an
    expired capability at the enforce gate after the fail-open evidence_refs append
    is missing. ``now`` defaults to the current UTC time when not supplied.

    The rule itself now lives in
    :func:`app.capability_registry.fold_ledger_proof_upgrades`: the spawn-time
    resolver needs the IDENTICAL upgrade, and the two consumer seams must not grade
    a capability differently. This thin wrapper stays as the blocked-sweep-local
    name for it.
    """
    from app.capability_registry import fold_ledger_proof_upgrades

    return fold_ledger_proof_upgrades(status_by_name, proof_records, now=now)


async def _fetch_working_capability_proof_status(session, project_id) -> dict:
    """``{canonical_name: proof_status}`` for the project's ``working`` capabilities.

    The proof-aware companion of :func:`_fetch_working_capability_names`, drawn
    from the SAME ``project.problem_model.capabilities`` registry. Its keys are
    exactly the working-capability name set (both readers select the identical
    alias-aware working set), so the boolean registry-satisfies check derives its
    name set from these keys — one project fetch serves both the resolution and
    the OBSERVE-mode proof surfacing. Cached per project across the sweep batch.

    The evidence_refs-derived tier-1 map is then reconciled against the project's
    durable ``capability_proof_records`` ledger by
    :func:`~app.capability_registry.consumer_gate_proof_status`, which the
    spawn-time resolver calls too, so both consumer seams (and the release
    qualifier) grade a capability identically. That reconciliation runs BOTH ways:

    * the ledger RAISES a capability whose tier-1 ``evidence_refs`` append was lost
      to the fail-open path (see :func:`_fold_proof_records_tier2`), and
    * SECURITY (audit finding #4) the ledger REFUSES a ``proven`` claim it does not
      corroborate. ``problem_model.capabilities[].evidence_refs`` is AGENT-WRITABLE
      JSONB: without this, an agent that wrote itself a
      ``{"kind": "named_test", "proven": true}`` ref would clear this gate and resume
      every dependent blocked on that capability with nothing machine-verified behind
      it. Only a provenance-authorized ``proof_exempt`` clears without a ledger entry.
    """
    from app.capability_registry import consumer_gate_proof_status
    from app.models import Project
    from app.time_utils import _utcnow

    # One clock for both tiers so a proof on the freshness boundary is graded
    # identically whether it lands in evidence_refs (tier-1) or the durable
    # capability_proof_records column (tier-2).
    now = _utcnow()
    project = await session.get(Project, project_id)
    return consumer_gate_proof_status(
        getattr(project, "problem_model", None),
        getattr(project, "capability_proof_records", None),
        now=now,
    )


async def _classify_blocker_needs(
    task,
    session,
    *,
    candidate_cache: dict,
    completed_cache: dict,
    dead_cache: dict,
    knowledge_cache: dict,
    registry_cache: dict,
    proof_gate_mode: str = "off",
) -> dict:
    """Classify EVERY recorded need of a blocked task against the live foundations.

    A blocked dependent can wait on MORE THAN ONE unresolved prerequisite (see
    :func:`task_blocked.get_blocker_needs`). This evaluates each one against the
    active / completed / dead candidate sets and returns a disposition dict:

      ``had_needs``       — the task carried ≥1 machine-resolvable need
      ``edges``           — foundation ids to wire (need → active in-flight task)
      ``completed_needs`` — needs satisfied by already-completed/merged work
                            (includes knowledge-document-satisfied needs)
      ``residual``        — needs not yet accounted for (no foundation matches)
      ``dead``            — ≥1 need names a terminally FAILED/CANCELLED foundation

    Resolution per need is active → completed → knowledge → registry → dead →
    residual. Knowledge-satisfied needs (a dangling kind='task' ref whose
    substance persists as a durable KnowledgeDocument) are routed into
    ``completed_needs`` so the existing resume path handles them without wiring
    a depends_on edge. The registry step does the same for a capability need the
    project's problem_model already declares ``working`` — the capability exists
    in the codebase itself, so no provider task will ever advertise it; without
    this step such a need sits residual to the escalation deadline (or worse, a
    long-dead historical provider routes it to the dead-foundation cancel). It
    deliberately runs BEFORE the dead check for exactly that reason. This
    prevents the dead-foundation path from cancelling a dependent whose need IS
    durably satisfied. Reuses the shared conservative predicates in
    :mod:`app.blocked_need_resolution` (no parallel matcher); candidate sets are
    fetched lazily and cached per project across the sweep batch. A foundation
    already present in ``task.depends_on`` is treated as already-wired (it is not
    re-added to ``edges``), so a partially-resolved blocker keeps converging.
    """
    from app.task_blocked import get_blocker_needs
    from app.blocked_need_resolution import (
        resolve_blocked_need,
        completed_foundation_for_need,
        need_matched_dead_foundation,
        knowledge_substance_satisfies_need,
    )
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        capability_need_proof_status,
        registry_satisfies_capability_need,
        task_backed_capability_need_blocked_by_proof_gate,
    )

    needs = get_blocker_needs(task)
    disp = {
        "had_needs": bool(needs),
        "edges": [],
        "completed_needs": [],
        "residual": [],
        "dead": False,
        # The first need that matched a terminally-dead foundation, recorded so the
        # foundation-died cancel can stamp it into the unresumable marker.
        "dead_need": None,
        # Needs that DID match a ``working`` registry capability but were held back
        # from ``completed_needs`` by the ENFORCE proof gate (the capability is only
        # claimed, not proven). These are a subset of ``residual``. Recorded
        # separately so the disposition can HOLD the dependent BLOCKED
        # deterministically rather than letting it fall through to the legacy
        # precondition machine — where a no-dep agent-source blocker would be
        # optimistically requeued, silently defeating the enforce gate. A gate-held
        # need is always observable (it resumes the instant proof is recorded), so
        # holding it never trips the never-satisfiable termination bound.
        "proof_gate_held": [],
    }
    if not needs:
        return disp

    active = candidate_cache.get(task.project_id)
    if active is None:
        active = await _fetch_active_candidates(session, task.project_id)
        candidate_cache[task.project_id] = active
    completed = completed_cache.get(task.project_id)
    if completed is None:
        completed = await _fetch_completed_candidates(session, task.project_id)
        completed_cache[task.project_id] = completed
    dead = dead_cache.get(task.project_id)
    if dead is None:
        dead = await _fetch_dead_candidates(session, task.project_id)
        dead_cache[task.project_id] = dead
    knowledge = knowledge_cache.get(task.project_id)
    if knowledge is None:
        knowledge = await _fetch_knowledge_substance_index(session, task.project_id)
        knowledge_cache[task.project_id] = knowledge
    # Registry working-capability proof map, fetched only when a capability need
    # is actually present (the common task-kind path pays no extra query). The map
    # keys ARE the working-capability name set (both readers select the identical
    # alias-aware working set), so the boolean registry-satisfies check derives its
    # name set from the keys — one fetch serves both the resolution and the
    # OBSERVE-mode proof surfacing below.
    working_proof = registry_cache.get(task.project_id)
    if working_proof is None and any(n.get("kind") == "capability" for n in needs):
        working_proof = await _fetch_working_capability_proof_status(
            session, task.project_id
        )
        registry_cache[task.project_id] = working_proof
    if working_proof is None:
        working_proof = {}
    working_caps = set(working_proof)
    project = None

    async def _task_backed_capability_is_held(need):
        nonlocal project
        if need.get("kind") != "capability":
            return False
        if project is None:
            from app.models import Project
            project = await session.get(Project, task.project_id)
        return task_backed_capability_need_blocked_by_proof_gate(
            need,
            getattr(project, "capability_proof_records", None),
            getattr(project, "problem_model", None),
            mode=proof_gate_mode,
        )

    # Stranded-parent set so a GENUINE ghost merge does NOT satisfy a completed
    # need. LANDED-IN-BASE-LINEAGE semantics: the ghost-merge exclusion fires ONLY
    # for a rollup parent that has terminally FAILED/CANCELLED — its staging branch
    # is abandoned, so a child PR that merged into it never reached the dependent's
    # base lineage. A parent that has merely NOT-YET-ROLLED-UP — still in flight,
    # or COMPLETED while its rollup PR to integration is pending — is NOT stranded:
    # the child's PR has already merged into the plan/staging branch that IS in the
    # dependent's base lineage, so the dependent cuts from it and sees the code
    # regardless of when the parent rollup integrates (audit case 3: a completed,
    # exact-title provider whose PR merged into the plan branch must resume its
    # dependent even before the rollup lands). Keying the strand on definitively
    # FAILED/CANCELLED parents only preserves the genuine ghost-merge exclusion
    # while no longer parking a dependent on a provider that genuinely landed in
    # its base lineage.
    failed_parents = {c.id for c in dead}

    existing_edges = {str(d) for d in (task.depends_on or [])}
    for need in needs:
        match = resolve_blocked_need(task, need, active)
        if match is not None:
            if await _task_backed_capability_is_held(need):
                disp["residual"].append(need)
                disp["proof_gate_held"].append(need)
                continue
            match_id = str(match.id)
            if match_id not in existing_edges and match_id not in disp["edges"]:
                disp["edges"].append(match_id)
            continue
        if (
            completed_foundation_for_need(task, need, completed, failed_parents)
            is not None
        ):
            if await _task_backed_capability_is_held(need):
                disp["residual"].append(need)
                disp["proof_gate_held"].append(need)
                continue
            disp["completed_needs"].append(need)
            continue
        # Durable-knowledge fallback: a dangling kind='task' need whose originating
        # task is CANCELLED but whose substance persists as a KnowledgeDocument is
        # satisfied — do NOT route it to the dead-foundation path.
        if knowledge_substance_satisfies_need(need, knowledge):
            disp["completed_needs"].append(need)
            continue
        # Registry-satisfied capability: the problem_model already declares this
        # capability ``working``, so the codebase itself is the foundation —
        # resume with no edge. Checked BEFORE the dead-foundation path so a
        # stale terminal provider of a still-working capability can never
        # cancel the dependent.
        if registry_satisfies_capability_need(need, working_caps):
            _proof_status = capability_need_proof_status(need, working_proof)
            if capability_need_blocked_by_proof_gate(
                need, working_proof, mode=proof_gate_mode
            ):
                # ENFORCE mode: an unproven/partial 'working' capability does NOT
                # satisfy the need. Leave it in ``residual`` (do NOT route to
                # completed_needs) so the dependent stays BLOCKED — the codebase
                # foundation is only claimed, not proven, so no edge is wired and
                # the task does not resume.
                logger.warning(
                    "[BLOCKED-RESUME][capability-proof-enforce] task=%s need "
                    "capability:%s NOT satisfied — the registry capability is "
                    "'working' with proof_status=%s (unproven); enforce mode holds "
                    "the dependent BLOCKED instead of resuming it",
                    task.id,
                    need.get("ref"),
                    _proof_status,
                )
                disp["residual"].append(need)
                # Also record it as gate-held (subset of residual) so the
                # disposition holds the dependent BLOCKED deterministically
                # instead of falling through to the optimistic-retry path.
                disp["proof_gate_held"].append(need)
                continue
            # OBSERVE mode: surface (never gate) a blocked-resume resolution backed
            # by a capability declared 'working' with no proven evidence. The need
            # still routes to completed_needs below; this only makes the unproven
            # claim visible while enforce mode is off.
            if _proof_status is not None and _proof_status != "proven":
                logger.warning(
                    "[BLOCKED-RESUME][capability-proof-observe] task=%s need "
                    "capability:%s resolved as satisfied-by-the-codebase, but the "
                    "registry capability is 'working' with proof_status=%s — "
                    "unproven resolution (observe mode, not gated)",
                    task.id,
                    need.get("ref"),
                    _proof_status,
                )
            disp["completed_needs"].append(need)
            continue
        if need_matched_dead_foundation(task, need, dead):
            disp["dead"] = True
            if disp["dead_need"] is None:
                disp["dead_need"] = need
            continue
        disp["residual"].append(need)
    return disp


def _wire_edges(task, edge_ids: list) -> bool:
    """Idempotently append ``edge_ids`` to ``task.depends_on``. Returns True if any
    edge was newly added. Full-list reassignment so SQLAlchemy observes the JSONB
    change (``flag_modified`` is applied by callers)."""
    from app.depends_on_provenance import remember_structural_depends_on

    existing = [str(d) for d in (task.depends_on or [])]
    added = False
    for match_id in edge_ids:
        if match_id not in existing:
            existing.append(match_id)
            remember_structural_depends_on(task, match_id)
            added = True
    if added:
        task.depends_on = existing
    return added


def _persist_residual_needs(task, residual: list) -> None:
    """Re-persist only the still-unsatisfied needs back into the blocker metadata.

    Drops needs resolved this round (wired into an edge or satisfied by completed
    work) and keeps only ``residual``, recomputing the legacy single ``need``
    mirror = ``residual[0]``. The blocker record is NOT cleared — the task stays
    BLOCKED on the remaining needs so the next tick re-evaluates only those.
    ``residual`` MUST be non-empty (the all-accounted path clears the blocker
    instead).
    """
    from app.task_blocked import BLOCKER_CONTEXT_KEY

    ctx = dict(task.context or {})
    meta = ctx.get(BLOCKER_CONTEXT_KEY)
    if not isinstance(meta, dict):
        return
    meta = dict(meta)
    meta["needs"] = list(residual)
    meta["need"] = residual[0]
    ctx[BLOCKER_CONTEXT_KEY] = meta
    task.context = ctx


async def _fetch_completed_candidates(session, project_id) -> list:
    """Completed/merged tasks in a project — foundations that have already LANDED.

    Statuses mirror ``blocked_need_resolution.COMPLETED_CANDIDATE_STATUSES``: a
    parked blocker's need may now be satisfied by work that has since completed
    (a sibling task done, the PR it named merged), which the active-candidate
    matcher never sees.
    """
    from app.blocked_need_resolution import COMPLETED_CANDIDATE_STATUSES

    completed = [TaskStatus(s) for s in COMPLETED_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(completed),
                )
            )
        )
        .scalars()
        .all()
    )


async def _partition_completed_needs_base_safe(
    task, completed_needs, completed, session
) -> tuple[list, list]:
    """Re-cut a resuming dependent's base over ALL its completed needs at ONCE.

    A blocked dependent that resumes because its needs are satisfied by ALREADY-
    completed work carries NO ``depends_on`` edge, so the dispatch-time re-cut
    (which only inspects edged candidates) can never fire for it. If a completed
    foundation that satisfied a need is a GENUINE cross-base code dependency that
    has landed on integration, the dependent would otherwise dispatch against a
    staging base forked before the foundation landed — exactly the failure the
    re-cut machinery exists to prevent.

    The re-cut MUST run as a SINGLE call over every completed foundation, exactly
    as the dispatch path does with the full ``dep_tasks`` set. Re-cutting one
    foundation at a time is unsafe when a dependent has MULTIPLE completed needs in
    the same tick: the ``_recut_*`` accounting (conflict counter, recheck timer,
    escalated flag) lives in a SINGLE per-dependent namespace, so a base-safe /
    successful re-cut for one foundation calls ``_clear_recut_accounting`` and
    WIPES a conflicting sibling foundation's accumulating conflict counter —
    defeating the consecutive-conflict escalation bound and parking the dependent
    invisibly forever. One shared re-cut keeps the accounting coherent:
    ``_has_landed_cross_base_code_dep`` is ``any(...)`` over the whole set, so a
    single conflicting foundation holds the dependent and accumulates the conflict
    counter, and the no-op clear only fires when NO foundation needs a re-cut.

    Returns ``(base_safe, recut_pending)``:

      * ``base_safe``     — needs whose foundation needs no re-cut (same-base /
        ordering-only / unresolved) or whose shared re-cut succeeded; safe to drop.
      * ``recut_pending`` — needs whose landed cross-base foundation still requires
        a (not-yet-clean) re-cut; retained so the dependent stays BLOCKED.
    """
    from app.blocked_need_resolution import completed_foundation_for_need
    from app.orchestrator_health import (
        _has_landed_cross_base_code_dep,
        _shared_staging_dependency_source,
        ensure_dependent_base_recut,
    )

    resolved = [
        (need, completed_foundation_for_need(task, need, completed))
        for need in completed_needs
    ]
    foundations = [f for _, f in resolved if f is not None]
    # No foundation resolved (defensive) or a single shared re-cut succeeded / was
    # a no-op → every completed need is base-safe.
    if not foundations or await ensure_dependent_base_recut(task, foundations, session):
        return list(completed_needs), []
    # Re-cut required but not yet clean (conflict / transient error): hold back
    # ONLY the needs whose foundation is the unsafe landed cross-base code dep; the
    # rest (same-base / ordering-only / unresolved) need no re-cut, stay base-safe.
    base_safe: list = []
    recut_pending: list = []
    for need, foundation in resolved:
        if foundation is not None and (
            _has_landed_cross_base_code_dep(task, [foundation])
            or await _shared_staging_dependency_source(task, foundation, session)
        ):
            recut_pending.append(need)
        else:
            base_safe.append(need)
    return base_safe, recut_pending


async def _fetch_dead_candidates(session, project_id) -> list:
    """Terminally failed/cancelled tasks — foundations that have definitively DIED.

    Statuses mirror ``blocked_need_resolution.TERMINAL_CANDIDATE_STATUSES``: a
    blocked dependent whose named foundation reached one of these states will
    never be satisfied by it, so the dependent is terminated (CANCELLED) rather
    than parked to re-arm forever.
    """
    from app.blocked_need_resolution import TERMINAL_CANDIDATE_STATUSES

    dead = [TaskStatus(s) for s in TERMINAL_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(dead),
                )
            )
        )
        .scalars()
        .all()
    )


async def _cancel_blocker_as_dead(
    task,
    session,
    *,
    detail: str,
    message: str,
    pending_broadcasts: list,
    give_up_class: str,
    need=None,
) -> None:
    """Terminally CANCEL a blocked dependent that can provably never resume.

    Mirrors the dispatch-time cascade-cancel of an edged dead dependency
    (``orchestrator._cascade_cancel_dependent`` sets CANCELLED): a needed-but-
    never-edged foundation that has definitively FAILED/CANCELLED — or a need that
    has outlived its bounded parked re-evaluations without ever becoming
    satisfiable — makes this work impossible. Raises ONE deduplicated operator
    alert (so the auto-cancel is visible) and clears the blocker metadata.

    Stamps a durable ``UNRESUMABLE_CANCEL_CONTEXT_KEY`` marker recording the
    ``give_up_class`` (foundation-died vs never-satisfiable), the unresolved
    ``need``, and the originating direction, so this terminal give-up is queryable
    and countable fleet-wide (the structured blocker metadata is otherwise cleared
    by ``_clear_blocked_fields`` on the same transition). Observability only.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _maybe_create_blocked_escalation_alert

    await _maybe_create_blocked_escalation_alert(
        task,
        session,
        blocker_kind=task.blocker_kind or "upstream dependency",
        detail=detail,
    )
    # Capture the originating direction before _clear_blocked_fields runs (it
    # preserves direction_id, but read defensively in case that changes).
    direction_id = (task.context or {}).get("direction_id")
    _clear_blocked_fields(task, preserve_first_blocked=False)
    ctx = task.context or {}
    marker = {
        "give_up_class": give_up_class,
        "need": need if isinstance(need, dict) else None,
        "direction_id": direction_id,
        "at": _utcnow().isoformat(),
    }
    # Additively flag a never-satisfiable give-up of a FOUNDATION-kind need
    # (capability / sibling-task / finding-hash) as an explicit missing-foundation
    # retirement: nothing was ever built to satisfy the prerequisite, so the
    # stranded dependent is first-class demand to COMMISSION that foundation. A PR
    # need, a foundation-died give-up, or a malformed title-like ``task`` ref (not
    # a real task id / finding hash, per the shared exact-task-ref policy) does NOT
    # get the flag — a title-like ref names no commissionable foundation. Purely
    # additive.
    from app.task_blocked import is_foundation_demand_need

    if give_up_class == GIVE_UP_NEVER_SATISFIABLE and is_foundation_demand_need(need):
        marker[MISSING_FOUNDATION_MARKER_KEY] = True
    ctx[UNRESUMABLE_CANCEL_CONTEXT_KEY] = marker
    task.context = ctx
    task.status = TaskStatus.CANCELLED
    task.latest_status_message = message
    flag_modified(task, "context")
    session.add(task)
    pending_broadcasts.append(
        (
            str(task.project_id),
            {"task_id": str(task.id), "status": "CANCELLED", "message": message},
        )
    )


async def _apply_needs_disposition(
    task,
    ctx,
    session,
    now,
    disp,
    *,
    completed_cache: dict,
    pending_broadcasts: list,
    parked: bool,
) -> str:
    """Apply a multi-need classification (:func:`_classify_blocker_needs`).

    A dependent is resumable to QUEUED ONLY when EVERY recorded need is accounted
    for: an edge for each in-flight foundation, and no edge for a need already
    satisfied by completed/merged work (verified base-safe). When only some needs
    resolve, the resolvable edges are wired, the residual still-unsatisfied needs
    are re-persisted, and the task stays BLOCKED so the next tick re-evaluates only
    the remainder. If ANY need names a terminally dead foundation the dependent is
    cancelled; the parked re-evaluation cap applies per the parked path.

    Returns one of:
      ``"resumed"``   — every need accounted for; task → QUEUED (caller counts it)
      ``"cancelled"`` — a dead foundation, or the parked re-eval cap was exceeded
      ``"held"``      — needs remain (partial / unsatisfiable / re-cut conflict /
                        enforce-gate hold); task stays BLOCKED, residual
                        re-persisted, recheck re-armed
      ``"escalate"``  — non-parked, residual needs remain, an edge is already wired
                        and the escalation deadline has passed; caller escalates
      ``"none"``      — non-parked, residual remains, no edge yet: caller falls
                        through to the legacy precondition state machine
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _resolve_blocked_escalation_alert
    from app.blocked_need_resolution import need_is_structurally_unobservable

    # 1) ANY need names a terminally-dead foundation → cancel (both paths). A
    #    single provably-dead foundation makes the dependent impossible regardless
    #    of how the other needs resolve, mirroring the dispatch-time cascade.
    if disp["dead"]:
        await _cancel_blocker_as_dead(
            task,
            session,
            detail=(
                "A needed foundation has terminally failed or was cancelled — this "
                "task can never run as-is. Auto-cancelled (re-scope, re-point the "
                "need, or recreate the foundation to retry)."
            ),
            message="Cancelled — needed foundation terminally failed/cancelled",
            pending_broadcasts=pending_broadcasts,
            give_up_class=GIVE_UP_FOUNDATION_DIED,
            need=disp.get("dead_need"),
        )
        return "cancelled"

    # 1b) BOUNDED-OUTCOME LIVENESS INVARIANT. A residual need the sweep can prove
    #     is STRUCTURALLY UNOBSERVABLE — an observation-class ("obs:") ref/label,
    #     an explicit ``structurally_dead`` flag, or a malformed (non-dict) need —
    #     matched NOTHING live/completed/lineage/knowledge/registry (it is in
    #     ``residual``) and can never be matched by any future foundation. Re-arming
    #     the recheck on it
    #     would loop forever, so the dependent must reach a terminal disposition
    #     NOW. Mirroring the dead-foundation cascade above, a SINGLE such residual
    #     makes the task impossible (every recorded need must resolve to resume),
    #     so escalate-and-cancel immediately regardless of how the other needs
    #     resolved. This makes an indefinite recheck loop on an unobservable need
    #     impossible by construction (the parked re-eval cap is the bound for
    #     observable-but-unprovided needs; this is the bound for unobservable ones).
    unobservable = [n for n in disp["residual"] if need_is_structurally_unobservable(n)]
    if unobservable:
        await _cancel_blocker_as_dead(
            task,
            session,
            detail=(
                "A recorded prerequisite can never be satisfied by any foundation "
                "— it names an observation-class or structurally-dead reference no "
                "task will ever provide. Auto-cancelled (re-scope or re-point the "
                "need to retry)."
            ),
            message="Cancelled — prerequisite is structurally unobservable",
            pending_broadcasts=pending_broadcasts,
            give_up_class=GIVE_UP_NEVER_SATISFIABLE,
            need=unobservable[0],
        )
        return "cancelled"

    residual = list(disp["residual"])
    edges = disp["edges"]
    completed_needs = disp["completed_needs"]
    # Needs the ENFORCE proof gate held back (a subset of ``residual``). Their
    # presence forces a deterministic HOLD below: a gate-held need matched a
    # ``working`` registry capability, so it is NOT a genuinely-unprovided
    # residual that may fall through to the legacy precondition machine (where a
    # no-dep agent-source blocker would be optimistically requeued, defeating the
    # gate). It resumes the instant proof is recorded, so holding it is bounded by
    # that event — not the never-satisfiable termination cap.
    gate_held = list(disp.get("proof_gate_held") or [])

    # 2) Verify the completed foundations are base-safe BEFORE treating them as
    #    accounted-for — on BOTH the partial- and full-resolution paths. A need
    #    satisfied by ALREADY-completed work carries no edge, so the dispatch-time
    #    re-cut (which only inspects edged candidates) can never fire for it; a
    #    cross-base GENUINE code foundation must have the dependent's base re-cut
    #    from integration first. _partition_completed_needs_base_safe runs ALL the
    #    completed foundations through ONE shared re-cut this tick (per-foundation
    #    re-cut would let a base-safe sibling's _clear_recut_accounting wipe a
    #    conflicting sibling's accumulating conflict counter — parking the dependent
    #    invisibly forever). A need whose re-cut is not yet safe is held back in
    #    ``recut_pending`` so it is re-checked next tick rather than dropped
    #    unverified — closing the partial-resolution gap where a cross-base
    #    completed need silently vanished and the dependent later resumed against a
    #    stale base. Such a need DID match a foundation (it is mid-recut, not
    #    genuinely unprovided), so it must always HOLD and must NOT fall through to
    #    the legacy precondition machine.
    recut_pending: list = []
    if completed_needs:
        completed = completed_cache.get(task.project_id)
        if completed is None:
            completed = await _fetch_completed_candidates(session, task.project_id)
            completed_cache[task.project_id] = completed
        completed_needs, recut_pending = await _partition_completed_needs_base_safe(
            task, completed_needs, completed, session
        )
        # The shared re-cut REPLACES and COMMITS task.context with a fresh dict
        # carrying the _recut_* escalation accounting on its failure path; re-read
        # the local ctx so the disposition branches below don't clobber it.
        ctx = task.context or {}

    progressed = bool(edges) or bool(completed_needs)
    # A still-unsatisfied need is either genuinely unprovided (``residual``) or a
    # completed foundation mid-recut (``recut_pending``). Both are re-persisted as
    # needs so the next tick re-evaluates them; only the all-accounted-for path
    # (both empty) leaves BLOCKED.
    unresolved = residual + recut_pending

    # 3) Unresolved needs remain → cannot fully resume this tick.
    if unresolved:
        if progressed or recut_pending or gate_held:
            # Partial resolution (or a need mid-recut, or an enforce-gate hold):
            # wire the resolvable edges, drop the resolved (and base-safe-verified
            # completed) needs, keep ONLY the unresolved set, and stay BLOCKED so
            # the next tick re-evaluates the remainder. A recut_pending need ALWAYS
            # holds here — it matched a foundation, so it must never fall through to
            # the precondition machine. A ``gate_held`` need holds for the same
            # reason: it matched a ``working`` registry capability but the enforce
            # proof gate held it, so it must stay BLOCKED here rather than fall
            # through ("none") where a no-dep agent-source blocker would be
            # optimistically requeued — silently defeating the gate.
            # Progress resets the parked re-eval cap — but ONLY when no sibling need
            # is still stuck mid-recut. A base-safe need's progress must not erase
            # the give-up accounting while a conflicting sibling remains unresolved.
            edged = _wire_edges(task, edges)
            _persist_residual_needs(task, unresolved)
            ctx = task.context or {}
            if progressed and not recut_pending:
                ctx.pop("_blocked_parked_reevals", None)
            attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
            ctx["_blocked_recheck_attempts"] = attempts
            task.context = ctx
            flag_modified(task, "context")
            if edged:
                flag_modified(task, "depends_on")
            task.recheck_after = now + (
                _BLOCKED_PARKED_REEVAL_INTERVAL
                if parked
                else _next_recheck_delay(attempts)
            )
            session.add(task)
            return "held"

        # No need resolved this round (and none mid-recut).
        if parked:
            # Apply the per-need parked re-evaluation cap: cancel rather than
            # re-arm a never-satisfiable parked blocker forever.
            reevals = int(ctx.get("_blocked_parked_reevals", 0)) + 1
            if reevals >= _BLOCKED_PARKED_REEVAL_CAP:
                await _cancel_blocker_as_dead(
                    task,
                    session,
                    detail=(
                        f"Blocked need was never satisfiable after {reevals} parked "
                        "re-evaluations — no task ever provided it. Auto-cancelled "
                        "(re-scope or re-point the need to retry)."
                    ),
                    message="Cancelled — blocked need never became satisfiable",
                    pending_broadcasts=pending_broadcasts,
                    give_up_class=GIVE_UP_NEVER_SATISFIABLE,
                    need=residual[0] if residual else None,
                )
                return "cancelled"
            ctx["_blocked_parked_reevals"] = reevals
            task.context = ctx
            flag_modified(task, "context")
            task.recheck_after = now + _BLOCKED_PARKED_REEVAL_INTERVAL
            session.add(task)
            return "held"

        # Non-parked, no progress. With NO edge wired the task can safely fall
        # through to the legacy precondition machine (optimistic retry / waiting /
        # deadline escalation) exactly as a single unresolved need did before. But
        # if an edge is ALREADY wired (a prerequisite resolved on an earlier tick)
        # the task must NOT be resumed by that edge clearing while other needs are
        # still missing — keep it BLOCKED, escalating only at the deadline.
        if not (task.depends_on or []):
            return "none"
        if await _blocked_escalation_due(task, session):
            return "escalate"
        attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
        ctx["_blocked_recheck_attempts"] = attempts
        task.context = ctx
        flag_modified(task, "context")
        task.recheck_after = now + _next_recheck_delay(attempts)
        session.add(task)
        return "held"

    # 4) Nothing unresolved → every need is accounted for AND every completed
    #    foundation was already verified base-safe in step 2 (a base-unsafe
    #    completed need would have been captured in recut_pending above and held).
    #    Wire any in-flight edges and resume.
    # Capture the originating direction before _clear_blocked_fields runs (it
    # preserves direction_id, but read defensively in case that changes), mirroring
    # _cancel_blocker_as_dead.
    direction_id = (task.context or {}).get("direction_id")
    edged = _wire_edges(task, edges)
    _clear_blocked_fields(task, preserve_first_blocked=False)
    task.status = TaskStatus.QUEUED
    if edged:
        message = "Unblocked — all needs resolved into dependency edges, requeued"
        resolved_by = "edge"
        foundation = list(edges)
    elif completed_needs:
        message = "Unblocked — all needs satisfied by completed work, requeued"
        resolved_by = "completed"
        foundation = list(completed_needs)
    else:
        message = "Unblocked — all needs resolved, requeued"
        resolved_by = "completed"
        foundation = list(completed_needs)
    task.latest_status_message = message
    # Stamp the durable resume marker (symmetric to UNRESUMABLE_CANCEL_CONTEXT_KEY).
    # Re-read ctx after _clear_blocked_fields cleared the structured blocker metadata.
    ctx = task.context or {}
    ctx[RESUME_MARKER_CONTEXT_KEY] = {
        "resolved_by": resolved_by,
        "foundation": foundation,
        "direction_id": direction_id,
        "at": _utcnow().isoformat(),
    }
    task.context = ctx
    flag_modified(task, "context")
    if edged:
        flag_modified(task, "depends_on")
    session.add(task)
    pending_broadcasts.append(
        (
            str(task.project_id),
            {"task_id": str(task.id), "status": "QUEUED", "message": message},
        )
    )
    # Reconcile the now-stale 'permanently blocked' operator alert when resuming a
    # parked blocker. Best-effort: a reconcile failure must never abort the resume.
    if parked:
        with contextlib.suppress(Exception):
            await _resolve_blocked_escalation_alert(task, session)
    return "resumed"


async def sweep_unblock_blocked_tasks(
    session,
    now: datetime,
    *,
    verify_pr_merged=None,
    batch_size: int = 200,
) -> int:
    """Step 12: re-evaluate due BLOCKED tasks; requeue or escalate them.

    Idempotent and concurrency-safe alongside the orchestrator/reaper: due rows
    are claimed with ``FOR UPDATE SKIP LOCKED`` so two sweeps (or a sweep racing
    the orchestrator) never double-process the same task.

    Returns the number of tasks moved back to QUEUED.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _maybe_create_blocked_escalation_alert
    from app.broadcast import broadcast_to_project as _bcast

    if verify_pr_merged is None:
        from app.rollup_pr_verification import _verify_pr_merged

        verify_pr_merged = _verify_pr_merged

    # Lock-free pre-scan: collect IDs without holding a batch of row locks across
    # the per-task GitHub calls (_blocked_precondition_state → verify_pr_merged,
    # up to 120s each). A batch FOR UPDATE on 200 rows held that entire window —
    # a lock-across-network anti-pattern that starved agent heartbeats and drove
    # idle_in_transaction_session_timeout kills. Instead: scan IDs advisory-only,
    # re-acquire a single-row FOR UPDATE SKIP LOCKED at the top of each iteration,
    # and commit (releasing that lock) before moving to the next task's I/O.
    due_id_stmt = (
        select(Task.id)
        .where(
            Task.status == TaskStatus.BLOCKED,
            (Task.recheck_after.is_(None)) | (Task.recheck_after <= now),
        )
        .order_by(Task.recheck_after.asc().nullsfirst())
        .limit(batch_size)
    )
    regular_due_ids = (await session.execute(due_id_stmt)).scalars().all()

    # Repository-wide gate failures are event-sensitive: when integration HEAD
    # changes, their old exponential backoff should not delay recovery for up to
    # an hour. Pull a bounded priority set regardless of recheck_after; the
    # in-loop SHA equality check and per-project GitHub cache keep this cheap and
    # prevent repeated requeues on the same HEAD. Projects that intentionally
    # skip GitHub CI on their integration branch may requeue to execute their
    # real task gate on a fresh base; pending/red/API-error CI remains closed.
    gate_text_predicates = []
    for marker in GATE_REVALIDATION_QUERY_MARKERS:
        pattern = f"%{marker}%"
        gate_text_predicates.extend(
            [
                Task.blocked_reason.ilike(pattern),
                Task.latest_status_message.ilike(pattern),
            ]
        )
    gate_id_stmt = (
        select(Task.id)
        .where(
            Task.status == TaskStatus.BLOCKED,
            or_(*gate_text_predicates),
        )
        .order_by(Task.blocked_at.asc().nullsfirst())
        .limit(batch_size)
    )
    gate_ids = (await session.execute(gate_id_stmt)).scalars().all()
    gate_id_set = set(gate_ids)
    due_ids = list(dict.fromkeys([*gate_ids, *regular_due_ids]))[:batch_size]
    if not due_ids:
        return 0

    requeued = 0
    # task_updated broadcasts are deferred until after the per-row commit so the
    # admin UI (which refetches task/digest data on these events) never observes
    # a stale BLOCKED row — or, on rollback, a transition that never persisted.
    pending_broadcasts: list[tuple[str, dict]] = []
    # Active-candidate lists are fetched at most once per project across the batch
    # (only when a task actually has a resolvable need with no edge). The
    # completed-candidate cache is the parked-resume counterpart: a parked
    # blocker's need may now be satisfied by already-merged/shipped work.
    candidate_cache: dict = {}
    completed_cache: dict = {}
    # Terminally-dead foundations (FAILED/CANCELLED): a needed-but-never-edged
    # foundation that has definitively died makes its dependent impossible — it is
    # cancelled rather than parked to re-arm forever.
    dead_cache: dict = {}
    # Durable knowledge-document substance index: a dangling kind='task' need whose
    # originating task is CANCELLED but whose substance persists as a durable
    # KnowledgeDocument is satisfied rather than routed to the dead-foundation path.
    knowledge_cache: dict = {}
    # Per-project registry working-capability sets (problem_model.capabilities):
    # a capability need the registry already declares ``working`` is satisfied by
    # the codebase itself and resumes with no edge.
    registry_cache: dict = {}
    # ── Gate revalidation caches (repo-wide gate failures) ──────────────
    # Per-project gate state cache: project_id → {head_sha, green_at_head,
    # checked_at_utc}.  Populated on first gate-failure-blocked task per
    # project and reused across siblings in the same sweep batch.
    _gate_cache: dict[str, dict] = {}
    # Per-project GitHub token cache: project_id → installation token string.
    # Populated by resolve_gate_state and reused across sibling tasks.
    _github_cache: dict[str, str] = {}

    async def _escalate_blocked_task(
        task,
        ctx,
        *,
        blocker_kind: str,
        detail: str,
    ) -> None:
        from app.task_blocked import get_blocker_metadata, mark_blocker_escalated

        await _maybe_create_blocked_escalation_alert(
            task,
            session,
            blocker_kind=blocker_kind,
            detail=detail,
        )
        ctx["_blocked_escalated"] = True
        task.context = ctx
        # Terminal escalation keeps the task parked in BLOCKED for the operator,
        # so reflect that in the structured record (escalated -> True) rather
        # than leaving the in-transition's escalated=False stale.
        mark_blocker_escalated(task, detail=detail, now=now)
        flag_modified(task, "context")
        # A parked blocker that still carries a machine-resolvable need is parked
        # at the LOW re-evaluation cadence (so it comes back due and resumes when
        # its foundation lands), not the multi-year horizon.  Transient blockers
        # (file_conflict, pending_rollup) are also parked at the LOW cadence so
        # they self-heal when the overlapping in-flight task merges — hard-parking
        # them at +10y caused operator-requeue whack-a-mole (incident 2026-06-07).
        # A need-less non-transient blocker keeps the far-future park.
        has_need = isinstance(get_blocker_metadata(task).get("need"), dict)
        is_transient = task.blocker_kind in {"file_conflict", "pending_rollup"}
        task.recheck_after = now + (
            _BLOCKED_PARKED_REEVAL_INTERVAL
            if (has_need or is_transient)
            else _BLOCKED_ESCALATED_PARK
        )
        session.add(task)

    async def _commit_task_row(bc_start: int) -> None:
        """Commit this task's mutations and emit its deferred broadcasts.

        Called at every loop-exit point (explicit ``continue`` or fall-through)
        so the per-row FOR UPDATE lock is released before the NEXT task's
        GitHub call starts.  Broadcast emission is deferred until after commit
        so the UI never refetches a stale row.
        """
        task_bc = pending_broadcasts[bc_start:]
        del pending_broadcasts[bc_start:]
        await session.commit()
        for _pid, _payload in task_bc:
            try:
                await _bcast(_pid, "task_updated", _payload)
            except Exception:
                logger.debug(
                    "[REAPER] Failed to broadcast %s for %s",
                    _payload.get("status"),
                    _payload.get("task_id"),
                    exc_info=True,
                )

    for task_id in due_ids:
        # Re-acquire a single-row FOR UPDATE SKIP LOCKED so the row lock is
        # held ONLY during this task's processing — never across the per-task
        # GitHub calls (_blocked_precondition_state → verify_pr_merged, up to
        # 120s).  The per-row commit then releases it before the next task's
        # I/O starts, keeping the locked window narrow enough that agent
        # heartbeats are never starved.
        task_filters = [
            Task.id == task_id,
            Task.status == TaskStatus.BLOCKED,
        ]
        if task_id not in gate_id_set:
            task_filters.append(
                (Task.recheck_after.is_(None)) | (Task.recheck_after <= now)
            )
        task = (
            await session.scalars(
                select(Task)
                .where(*task_filters)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).first()
        if task is None:
            continue

        _bc_start = len(pending_broadcasts)

        # ── Gate revalidation for repo-wide gate failures ──────────────
        # When integration HEAD has advanced past the task's branch point,
        # cheaply re-check the gate condition against current HEAD instead
        # of spawning a suite run.  One GitHub check-runs call per project
        # per sweep cycle (cached), not per blocked task.
        from app.gate_revalidation import is_gate_failure_blocked as _gfb

        if _gfb(task):
            from app.gate_revalidation import (
                has_live_respawn,
                head_advanced_since_gate_failure,
                resolve_gate_state,
            )

            _pid_key = str(task.project_id)
            _gs = _gate_cache.get(_pid_key)
            if _gs is None:
                _gs = await resolve_gate_state(
                    session,
                    task.project_id,
                    github_info_cache=_github_cache,
                )
                _gate_cache[_pid_key] = _gs or {}
            if (
                _gs
                and _gs.get(
                    "revalidation_ready",
                    _gs.get("green_at_head", False),
                )
                and head_advanced_since_gate_failure(
                    task,
                    _gs.get("head_sha"),
                    head_committed_at=_gs.get("head_committed_at"),
                )
                and not await has_live_respawn(task, session)
            ):
                from sqlalchemy.orm.attributes import flag_modified

                _clear_blocked_fields(task, preserve_first_blocked=False)
                task.status = TaskStatus.QUEUED
                if _gs.get("green_at_head"):
                    task.latest_status_message = (
                        "Unblocked — repo-wide gate resolved at integration HEAD "
                        f"({_gs['head_sha'][:12]}), requeued on fresh base"
                    )
                else:
                    task.latest_status_message = (
                        "Unblocked — integration HEAD advanced "
                        f"({_gs['head_sha'][:12]}); requeued to rerun the "
                        "repository gate on a fresh base"
                    )
                ctx = task.context or {}
                ctx["_gate_revalidated_at"] = _gs["checked_at_utc"]
                ctx["_gate_revalidated_head"] = _gs["head_sha"]
                task.context = ctx
                flag_modified(task, "context")
                session.add(task)
                requeued += 1
                pending_broadcasts.append(
                    (
                        str(task.project_id),
                        {
                            "task_id": str(task.id),
                            "status": "QUEUED",
                            "message": task.latest_status_message,
                        },
                    )
                )
                await _commit_task_row(_bc_start)
                continue
            # A live same-scope respawn means DON'T unblock; fall through to the
            # existing blocker machinery so the respawn completes instead of
            # racing the recovered original.

        ctx = task.context or {}
        # Multi-need classification (shared by the parked and live paths): a
        # blocked dependent can wait on MORE THAN ONE prerequisite. Classify EVERY
        # recorded need once against the live active / completed / dead foundation
        # sets, then apply the disposition — resume only when ALL needs resolve.
        from app.shared_state import runtime_config as _blocker_rt_config
        _proof_gate_mode = str(
            _blocker_rt_config.get("capability_proof_gate_mode", "off") or "off"
        )
        disp = await _classify_blocker_needs(
            task,
            session,
            candidate_cache=candidate_cache,
            completed_cache=completed_cache,
            dead_cache=dead_cache,
            knowledge_cache=knowledge_cache,
            registry_cache=registry_cache,
            proof_gate_mode=_proof_gate_mode,
        )

        # Already escalated terminally — leave parked for the operator. On
        # escalation we push recheck_after to a far-future sentinel so these rows
        # drop out of the due set entirely. Backstop: if a legacy/None
        # recheck_after let one resurface here, evict it the same way so it stops
        # being re-SELECTed and FOR UPDATE-locked every reaper cycle.
        if ctx.get("_blocked_escalated"):
            # Parked-blocker auto-resume / cancel / hold: a parked blocker that
            # carries still-satisfiable needs is resumed (edges wired + requeue, or
            # proceed when satisfied by completed work) and its stale alert is
            # reconciled; one whose needs are partially resolved keeps converging;
            # one whose needs are provably dead or have outlived the bounded parked
            # re-evaluations is terminated. Runs at the low parked cadence — never
            # re-escalates.
            if disp["had_needs"]:
                outcome = await _apply_needs_disposition(
                    task,
                    ctx,
                    session,
                    now,
                    disp,
                    completed_cache=completed_cache,
                    pending_broadcasts=pending_broadcasts,
                    parked=True,
                )
                if outcome == "resumed":
                    requeued += 1
                    await _commit_task_row(_bc_start)
                    continue
                if outcome in ("cancelled", "held"):
                    await _commit_task_row(_bc_start)
                    continue
            # Need-less parked blocker — handle transient vs durable separately.
            # Transient blockers (file_conflict, pending_rollup) clear when the
            # overlapping in-flight task merges; re-evaluate their precondition
            # state so they requeue automatically when the conflict lifts, or
            # re-arm with bounded backoff while it persists.  A need-less
            # NON-transient blocker keeps the far-future park and stays for
            # the operator.
            is_transient = task.blocker_kind in {"file_conflict", "pending_rollup"}
            if is_transient:
                state = await _blocked_precondition_state(
                    task, session, verify_pr_merged
                )
                if state == "clear":
                    _clear_blocked_fields(task, preserve_first_blocked=False)
                    task.status = TaskStatus.QUEUED
                    task.latest_status_message = (
                        "Unblocked — transient conflict cleared, requeued"
                    )
                    flag_modified(task, "context")
                    session.add(task)
                    requeued += 1
                    pending_broadcasts.append(
                        (
                            str(task.project_id),
                            {
                                "task_id": str(task.id),
                                "status": "QUEUED",
                                "message": task.latest_status_message,
                            },
                        )
                    )
                else:
                    task.recheck_after = now + _BLOCKED_PARKED_REEVAL_INTERVAL
                    session.add(task)
                await _commit_task_row(_bc_start)
                continue
            if task.recheck_after is None or task.recheck_after <= now:
                task.recheck_after = now + _BLOCKED_ESCALATED_PARK
                session.add(task)
            await _commit_task_row(_bc_start)
            continue

        # Cross-task RESOLUTION (runs BEFORE the optimistic-retry / escalate path):
        # wire a real depends_on edge for each need that names an in-flight
        # foundation, resume with no edge for any need already satisfied by
        # completed/merged work, and stay BLOCKED on the residual until EVERY need
        # is accounted for. A dead foundation cancels; a fully-resolved set
        # SHORT-CIRCUITS the bounded optimistic retry below. A task that still has
        # an unresolved need with no edge falls through ("none") to the legacy
        # precondition machine unchanged.
        if disp["had_needs"]:
            outcome = await _apply_needs_disposition(
                task,
                ctx,
                session,
                now,
                disp,
                completed_cache=completed_cache,
                pending_broadcasts=pending_broadcasts,
                parked=False,
            )
            if outcome == "resumed":
                requeued += 1
                await _commit_task_row(_bc_start)
                continue
            if outcome in ("cancelled", "held"):
                await _commit_task_row(_bc_start)
                continue
            if outcome == "escalate":
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "upstream dependency",
                    detail=(
                        "Task has unresolved prerequisites that never appeared "
                        "within the deadline — needs operator intervention "
                        "(re-scope, re-point the need, or recreate the foundation)."
                    ),
                )
                await _commit_task_row(_bc_start)
                continue
            # outcome == "none": fall through to the precondition state machine.

        state = await _blocked_precondition_state(task, session, verify_pr_merged)
        if state == "clear":
            blocker_meta = (task.context or {}).get("blocker") or {}
            blocker_source = (
                blocker_meta.get("source") if isinstance(blocker_meta, dict) else None
            )
            # Conflict/rollup blockers are transient-but-self-re-blocking:
            # dispatch requeues them every recheck interval and re-blocks if
            # the precondition still holds. Their deadline must measure from
            # the TRUE first-blocked onset, so they are escalation-eligible and
            # onset-preserving even when they carry stale, already-satisfied
            # dependency edges (a successfully-completed dep id is never
            # stripped from depends_on). Key the discriminator off the actual
            # blocker identity, NOT the mere presence of dependency edges —
            # otherwise a conflict-blocked task with a satisfied dep would skip
            # escalation and reset its onset every cycle, looping forever.
            is_optimistic_conflict = task.blocker_kind in {
                "file_conflict",
                "pending_rollup",
            }
            is_genuine_clear = (not is_optimistic_conflict) and (
                bool(task.depends_on) or blocker_source == "parent"
            )
            if not is_genuine_clear and await _blocked_escalation_due(task, session):
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "unknown",
                    detail=(
                        f"Task has been blocked since {task.blocked_at:%Y-%m-%d %H:%M} "
                        f"without clearing: {task.blocked_reason or task.latest_status_message or ''}"
                    ),
                )
                await _commit_task_row(_bc_start)
                continue
            # A conflict/rollup blocker is requeued optimistically and must
            # preserve its first-blocked onset across the requeue (so the next
            # block episode keeps counting toward the deadline) — unless it has
            # already crossed the deadline and was escalated above. A genuine
            # dependency/parent clear resets all, including the onset.
            _clear_blocked_fields(task, preserve_first_blocked=is_optimistic_conflict)
            task.status = TaskStatus.QUEUED
            task.latest_status_message = "Unblocked — requeued for dispatch"
            flag_modified(task, "context")
            session.add(task)
            requeued += 1
            pending_broadcasts.append(
                (
                    str(task.project_id),
                    {
                        "task_id": str(task.id),
                        "status": "QUEUED",
                        "message": task.latest_status_message,
                    },
                )
            )
        elif state == "re_evaluate":
            _clear_blocked_fields(task, preserve_first_blocked=False)
            task.status = TaskStatus.EVALUATING
            task.latest_status_message = "Unblocked — awaiting rollup re-evaluation"
            flag_modified(task, "context")
            session.add(task)
            pending_broadcasts.append(
                (
                    str(task.project_id),
                    {
                        "task_id": str(task.id),
                        "status": "EVALUATING",
                        "message": task.latest_status_message,
                    },
                )
            )
        elif state == "dead":
            await _escalate_blocked_task(
                task,
                ctx,
                blocker_kind=task.blocker_kind or "upstream dependency",
                detail=(
                    "Upstream dependency failed or was cancelled — this task can "
                    "never run as-is and needs operator intervention "
                    "(re-scope, re-point dependency, or cancel)."
                ),
            )
        else:  # "retry" or "waiting"
            past_deadline = await _blocked_escalation_due(task, session)
            optimistic_used = int(ctx.get("_blocked_optimistic_retries", 0))
            # "retry": a no-dep agent/lifecycle blocker that may be a transient
            # infra failure — optimistically requeue (bounded) so a passing blip
            # auto-recovers instead of wedging its rollup parent until an operator
            # intervenes. Preserve the first-blocked onset so the 6h deadline
            # still measures from true onset across the requeue episodes.
            if (
                state == "retry"
                and not past_deadline
                and optimistic_used < _BLOCKED_OPTIMISTIC_RETRY_CAP
            ):
                # Evidence gate for retries 2+: require that task.blocked_at is
                # more recent than the last retry timestamp, proving the prior
                # retry dispatched a real VM run that ended by re-blocking.
                # Retry 1 (optimistic_used == 0) needs no prior evidence.
                if optimistic_used > 0:
                    last_retry_at_str = ctx.get("_blocked_last_retry_at")
                    has_retry_evidence = False
                    try:
                        last_retry_at = datetime.fromisoformat(last_retry_at_str)
                        has_retry_evidence = (
                            task.blocked_at is not None
                            and task.blocked_at > last_retry_at
                        )
                    except (ValueError, TypeError):
                        # Missing, malformed, or timezone-incomparable evidence
                        # is not proof that the previous retry ran.
                        pass
                    if not has_retry_evidence:
                        # No evidence of a VM run since last retry; hold in
                        # backoff instead of consuming the next retry slot.
                        attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
                        ctx["_blocked_recheck_attempts"] = attempts
                        task.context = ctx
                        flag_modified(task, "context")
                        task.recheck_after = now + _next_recheck_delay(attempts)
                        session.add(task)
                        await _commit_task_row(_bc_start)
                        continue
                ctx["_blocked_optimistic_retries"] = optimistic_used + 1
                ctx["_blocked_last_retry_at"] = now.isoformat()
                task.context = ctx
                # _clear_blocked_fields preserves _blocked_optimistic_retries and
                # _blocked_last_retry_at on an onset-preserving requeue (only wiped
                # on a genuine dependency clear), so the budget and evidence
                # timestamp keep accumulating across re-blocks.
                _clear_blocked_fields(task, preserve_first_blocked=True)
                task.status = TaskStatus.QUEUED
                task.latest_status_message = (
                    f"Unblocked — optimistic infra-precondition retry "
                    f"{optimistic_used + 1}/{_BLOCKED_OPTIMISTIC_RETRY_CAP}"
                )
                flag_modified(task, "context")
                session.add(task)
                requeued += 1
                pending_broadcasts.append(
                    (
                        str(task.project_id),
                        {
                            "task_id": str(task.id),
                            "status": "QUEUED",
                            "message": task.latest_status_message,
                        },
                    )
                )
                await _commit_task_row(_bc_start)
                continue
            # "waiting" (or a retry that exhausted its budget): re-arm a later
            # recheck with backoff. Deadline escalation stays active for blockers
            # still genuinely unresolved at the 6h mark.
            if past_deadline:
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "unknown",
                    detail=(
                        f"Task has been blocked since {task.blocked_at:%Y-%m-%d %H:%M} "
                        f"without clearing: {task.blocked_reason or task.latest_status_message or ''}"
                    ),
                )
                await _commit_task_row(_bc_start)
                continue
            attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
            ctx["_blocked_recheck_attempts"] = attempts
            task.context = ctx
            flag_modified(task, "context")
            task.recheck_after = now + _next_recheck_delay(attempts)
            session.add(task)

        # Per-row commit for paths that fall through (re_evaluate, dead,
        # state==clear-requeue, waiting→recheck-armed).
        await _commit_task_row(_bc_start)

    if requeued:
        logger.info(f"[REAPER] Step 12: unblocked {requeued} task(s) → QUEUED")
    return requeued


async def _event_driven_unblock_project(
    project_id,
    session,
    *,
    batch_size: int,
) -> int:
    """Shared engine for the event-driven BLOCKED re-resolution fast paths.

    Scans same-project BLOCKED tasks that carry a machine-resolvable need and runs
    the SAME classification (:func:`_classify_blocker_needs`, which consults the
    active / completed / dead candidate sets AND the durable knowledge substance
    index) + disposition (:func:`_apply_needs_disposition`) the periodic Step-12
    sweep uses. A task is resumed to QUEUED only when EVERY recorded need is
    accounted for — the disposition's all-needs-resolved gate is preserved, so a
    task with a still-unsatisfied sibling need stays BLOCKED.

    Fail-open: any per-task error is swallowed and logged; never propagated.
    Bounded: scans at most ``batch_size`` BLOCKED rows per call. Idempotent
    alongside the periodic sweep and the sibling fast paths — ``FOR UPDATE SKIP
    LOCKED`` means a row being evaluated elsewhere is skipped here, and vice versa,
    so a task can never be double-resumed.

    Returns the number of tasks resumed (0 on error or no match). Callers own the
    contextual log line; this helper only mutates + commits the resolved rows.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.task_blocked import get_blocker_needs

    now = _utcnow()

    # Fetch BLOCKED dependents in the same project; skip-lock for sweep parity.
    due_stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.BLOCKED,
            Task.project_id == project_id,
        )
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    candidates = (await session.execute(due_stmt)).scalars().all()
    if not candidates:
        return 0

    requeued = 0
    changed = False
    pending_broadcasts: list[tuple[str, dict]] = []
    candidate_cache: dict = {}
    completed_cache: dict = {}
    dead_cache: dict = {}
    knowledge_cache: dict = {}
    registry_cache: dict = {}

    for task in candidates:
        # Skip tasks with no machine-resolvable needs; the sweep handles those.
        if not get_blocker_needs(task):
            continue
        ctx = task.context or {}
        parked = bool(ctx.get("_blocked_escalated"))
        try:
            from app.shared_state import runtime_config as _blocker_rt_config
            _proof_gate_mode = str(
                _blocker_rt_config.get("capability_proof_gate_mode", "off") or "off"
            )
            disp = await _classify_blocker_needs(
                task,
                session,
                candidate_cache=candidate_cache,
                completed_cache=completed_cache,
                dead_cache=dead_cache,
                knowledge_cache=knowledge_cache,
                registry_cache=registry_cache,
                proof_gate_mode=_proof_gate_mode,
            )
            if not disp["had_needs"]:
                continue
            outcome = await _apply_needs_disposition(
                task,
                ctx,
                session,
                now,
                disp,
                completed_cache=completed_cache,
                pending_broadcasts=pending_broadcasts,
                parked=parked,
            )
            if outcome == "resumed":
                requeued += 1
                changed = True
            elif outcome in ("cancelled", "held"):
                changed = True
        except Exception:
            logger.exception(
                "[REAPER] event-driven unblock failed for task %s — skipping",
                task.id,
            )

    if changed:
        await session.commit()
    # Loop var renamed off ``project_id`` so it can't shadow the engine's
    # ``project_id`` parameter (the broadcast tuple already carries its own).
    for bcast_pid, payload in pending_broadcasts:
        try:
            await _bcast(bcast_pid, "task_updated", payload)
        except Exception:
            logger.debug(
                "[REAPER] event-driven unblock broadcast failed for %s",
                payload.get("task_id"),
                exc_info=True,
            )
    return requeued


async def event_driven_unblock_dependents(
    landed_task: "Task",
    session,
    *,
    batch_size: int = 50,
) -> int:
    """Fast-path event-driven re-resolution of BLOCKED dependents when a foundation lands.

    Called immediately after a task commits to COMPLETED + merge-unblock signal.
    Scans same-project BLOCKED dependents with machine-resolvable needs and
    resumes any whose needs are now satisfied by the just-landed foundation.

    Fail-open: any per-task error is swallowed and logged; the function never
    propagates an exception to its caller. Bounded: scans at most ``batch_size``
    BLOCKED rows per call. Idempotent alongside the periodic sweep: uses
    ``FOR UPDATE SKIP LOCKED`` so a row being evaluated by the sweep is skipped
    here, and vice versa.

    Returns the number of tasks resumed (0 on error or no-match).
    """
    from app.landed_signal import has_merge_unblock_signal

    if not has_merge_unblock_signal(landed_task):
        return 0

    requeued = await _event_driven_unblock_project(
        landed_task.project_id, session, batch_size=batch_size
    )
    if requeued:
        logger.info(
            "[REAPER] event_driven_unblock: landing of %s unblocked %d dependent(s) → QUEUED",
            landed_task.id,
            requeued,
        )
    return requeued


async def event_driven_unblock_on_knowledge(
    project_id,
    session,
    *,
    batch_size: int = 50,
) -> int:
    """Fast-path event-driven re-resolution of BLOCKED tasks when a KnowledgeDocument lands.

    A knowledge document making a BLOCKED task's ``kind='task'`` / finding-hash
    prerequisite durably satisfiable (matching ``finding_hash`` or exact ``topic``)
    used to leave waiters BLOCKED until the slow periodic sweep re-checked them at
    ``recheck_after`` (or, once escalated, only every 6h). This fires immediately
    after the producing task commits the document, so waiters resume at once.

    Unlike :func:`event_driven_unblock_dependents` there is no single landed task —
    a knowledge doc satisfies a need by substance, not by a depends_on edge — so it
    is keyed on ``project_id`` and re-evaluates the whole project's BLOCKED set
    through the SAME shared engine (:func:`_event_driven_unblock_project`), which in
    turn consults the durable knowledge substance index via
    :func:`_classify_blocker_needs`. The conservative finding-hash/exact-topic
    matching and the all-needs-resolved resume gate are preserved unchanged.

    Fail-open + bounded + idempotent (``FOR UPDATE SKIP LOCKED``) alongside the
    periodic sweep and the landed-task path; must be called best-effort by the
    knowledge-completion chokepoint so it can never break document persistence.

    Returns the number of tasks resumed (0 on error or no match).
    """
    requeued = await _event_driven_unblock_project(
        project_id, session, batch_size=batch_size
    )
    if requeued:
        logger.info(
            "[REAPER] event_driven_unblock_on_knowledge: new knowledge in project %s "
            "unblocked %d task(s) → QUEUED",
            project_id,
            requeued,
        )
    return requeued


async def event_driven_unblock_on_capability_proof(
    project_id,
    session,
    *,
    batch_size: int = 50,
) -> int:
    """Re-resolve proof-gated BLOCKED tasks after a capability earns proof.

    A newly committed executed-test proof changes the registry answer for every
    same-project ``kind='capability'`` need, but those tasks may have a future
    ``recheck_after``. Reuse the shared locked, bounded resolver so only tasks
    whose *all* needs are now satisfied resume. This event seam is deliberately
    fail-open: proof persistence remains authoritative if wake-up work fails.
    """
    try:
        requeued = await _event_driven_unblock_project(
            project_id, session, batch_size=batch_size
        )
    except Exception:
        logger.exception(
            "[REAPER] event_driven_unblock_on_capability_proof failed for project %s",
            project_id,
        )
        return 0
    if requeued:
        logger.info(
            "[REAPER] event_driven_unblock_on_capability_proof: new proof in project %s "
            "unblocked %d task(s) → QUEUED",
            project_id,
            requeued,
        )
    return requeued
