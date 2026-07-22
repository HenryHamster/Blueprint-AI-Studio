"""Direction research outcome tracking.

Extracted from task_lifecycle.py to relieve its line-count ceiling.
Contains _update_direction_research_outcomes, called whenever a child task
of a research cycle reaches a terminal status so that research_history
entries accumulate live outcome statistics.
"""

import statistics
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.failure_stage import _derive_failure_stage
from app.failure_classification import (
    ADMIN_CANCEL,
    CODE_ERROR,
    CO_DEFERRED_SHELVED,
    INFRA_CLASSES,
    REASSIGNED,
    SCOPE_EMPTY,
    SCOPE_OVERFLOW,
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
    classify_failure as _classify_failure,
    classify_failure_taxonomy as _classify_taxonomy,
)
from app.acceptance_gate import (
    CODE_ACCEPTANCE_VERDICT_KEY,
    acceptance_counts_as_verified_context,
    acceptance_blocks_success,
    acceptance_is_consumed,
    acceptance_is_proven,
    acceptance_was_declared,
    classify_merged_acceptance,
    classify_merged_acceptance_context,
)
from app.landed_signal import (
    LANDED_ON_INTEGRATION_KEY,
    has_merge_unblock_signal,
    is_merged_completion,
)
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
    get_task_cost_by_provider_model,
)
from app.task_capabilities import PRODUCT_TASK_TYPES, READ_ONLY_TASK_TYPES, WRITE_TASK_TYPES
from app.task_locking import with_design_doc_locked, with_directions_locked

# Task types that never open a PR. Derived directly from the registry so that
# newly-registered read_only types are automatically excluded from the
# conversion-rate denominator without requiring a manual edit here.  Used as
# the shared source of truth across backend surfaces (digest_direction_progress,
# pipeline_health) and the agent-side stats renderer so that
# knowledge/compute outputs don't deflate the merged-PR rate.
PR_INELIGIBLE_TASK_TYPES: frozenset[str] = READ_ONLY_TASK_TYPES


def _is_cascade_cancelled(task: Task) -> bool:
    """True when a CANCELLED task was cascade-killed by external factors.

    Signals: context._replaced_by_task_id (requeue clone took over) or a
    latest_status_message prefix that identifies an orchestrator cascade path
    ("Parent requeued", "Cancelled: parent …", "Cancelled: dependency …").
    These tasks did not fail under their own code; excluding them from failure
    metrics prevents false auto-pause and diagnosis triggers.
    """
    if task.status != TaskStatus.CANCELLED:
        return False
    if (task.context or {}).get("_replaced_by_task_id"):
        return True
    msg = task.latest_status_message or ""
    return (
        msg.startswith("Parent requeued")
        or msg.startswith("Cancelled: parent ")
        or msg.startswith("Cancelled: dependency ")
        or msg.startswith("Superseded by")
    )


def _is_review_cap_exhausted(task: Task) -> bool:
    """Review-fix pipeline hit its iteration cap — a code/review-quality failure,
    not infrastructure, even though failure_class is AGENT_TIMEOUT (in INFRA_CLASSES)."""
    return (task.context or {}).get("failure_detail") == "review_cap_exhausted"


# CANCELLED failure_detail markers that represent a non-genuine outcome — work
# that landed elsewhere, was approved-then-capped, or was superseded — NOT a
# code/review failure the direction is responsible for. Counting these dings
# direction health and can trip false auto-pause / diagnosis. See FIX A
# (pr_closed_non_rejection) and FIX D (cap_exhausted_after_approve).
_NON_GENUINE_CANCELLED_DETAILS = frozenset(
    {
        "pr_closed_non_rejection",
        "cap_exhausted_after_approve",
        "superseded_by_review_pipeline",
    }
)


def _is_non_genuine_cancelled(task: Task) -> bool:
    """True when a CANCELLED task represents a non-genuine outcome that must not
    count against direction health.

    Covers (a) cascade kills (see ``_is_cascade_cancelled``), (b) the new
    review/rollup non-genuine markers (``pr_closed_non_rejection``,
    ``cap_exhausted_after_approve``), (c) ADMIN_CANCEL operator actions, and (d)
    REASSIGNED / SCOPE_EMPTY handoffs that land CANCELLED rather than FAILED. A genuine code/review failure never
    carries any of these signals, so this never hides a real regression.
    """
    if task.status != TaskStatus.CANCELLED:
        return False
    if _is_cascade_cancelled(task):
        return True
    ctx = task.context or {}
    if ctx.get("failure_detail") in _NON_GENUINE_CANCELLED_DETAILS:
        return True
    if ctx.get("prerequisite_escalated_to_parent"):
        return True
    if ctx.get(CO_DEFERRED_SHELVED):
        return True
    return ctx.get("failure_category") in (ADMIN_CANCEL, REASSIGNED, SCOPE_EMPTY)


def _counts_as_failure(task: Task) -> bool:
    """Terminal status that represents a genuine failure for direction metrics.

    BLOCKED is deliberately excluded: it is a non-terminal holding state for a
    task whose precondition is absent, not a failure. Counting it here would
    inflate direction failure rates and let parked work consume retry budget it
    never spent. Blocked tasks are tracked separately (see ``tasks_blocked``).
    """
    if task.status == TaskStatus.FAILED:
        return True
    return task.status == TaskStatus.CANCELLED and not _is_non_genuine_cancelled(task)


def is_autonomous_merge(task: Task) -> bool:
    """Return True if the task merged without any operator intervention.

    Precedence (first match wins):
    1. created_by == "USER" → operator-assisted (user created it directly)
    2. review_dismissed or user_overrode_review in context → operator stepped in
    3. Otherwise → autonomous (ORCHESTRATOR-spawned, merged without override)
    """
    if (task.created_by or "").upper() == "USER":
        return False
    ctx = task.context or {}
    if ctx.get("review_dismissed") or ctx.get("user_overrode_review"):
        return False
    return True


def verified_merged_count(entry: dict) -> int:
    """Verification-aware merged-PR count for a research_history entry.

    Returns the merges whose verification was NOT degraded — i.e. verified
    merges PLUS benign-unverified merges (no runnable suite, or the no-CI gate
    deliberately off), but EXCLUDING merges whose test/CI verification was
    blocked by an environment/setup failure (``unverified_degraded``). This is
    exactly the ``verified_prs_merged`` field computed in
    ``_update_direction_research_outcomes``.

    Direction- and pipeline-level rates (conversion, merge, churn, lifetime
    burn) call this so a degraded-unverified merge does not count as a clean
    success — matching the per-task outcome layer, which already scores such a
    merge as neutral. Legacy entries written before ``verified_prs_merged`` was
    recorded fall back to the raw ``prs_merged`` count, so historical data
    degrades to prior behavior rather than dropping to zero.
    """
    if "verified_prs_merged" in entry:
        return int(entry.get("verified_prs_merged") or 0)
    return int(entry.get("prs_merged") or 0)


def verified_autonomous_merged_count(entry: dict) -> int:
    """Verification-aware AUTONOMOUS merged-PR count for a research_history entry.

    The honest counterpart to the raw ``autonomous_prs_merged`` field: counts
    only autonomous merges (no operator intervention) that ALSO landed in a
    verified acceptance bucket — i.e. the intersection recorded as
    ``verified_autonomous_prs_merged`` by ``_update_direction_research_outcomes``.
    Degraded-unverified and acceptance-unsatisfied autonomous merges are excluded
    (they count in ``autonomous_prs_merged`` but not here), and confirmed-stranded
    merges are already absent because a full recompute drops them from the merged
    rows entirely. By construction ``verified_autonomous_prs_merged`` is never
    larger than either ``autonomous_prs_merged`` or ``verified_prs_merged``.

    This is the honest basis for the 10-merge retirement-eligibility milestone in
    :func:`compute_direction_autonomy_state`. A two-step legacy fallback keeps
    historical directions whole: entries written before this field existed fall
    back to the raw ``autonomous_prs_merged`` count, and pre-attribution entries
    lacking even that fall back to ``prs_merged`` — degrading to the prior
    (raw-autonomous / all-merges-autonomous) behavior rather than dropping to zero.
    """
    if "verified_autonomous_prs_merged" in entry:
        return int(entry.get("verified_autonomous_prs_merged") or 0)
    if "autonomous_prs_merged" in entry:
        return int(entry.get("autonomous_prs_merged") or 0)
    return int(entry.get("prs_merged") or 0)


def credit_gate_merged_count(entry: dict) -> int:
    """Credit-eligible merge count for direction decision gates (auto-completion, churn, lifetime burn).

    Confirmed-reachable (landed) and ancestry-indeterminate merges both count.
    Only confirmed-unreachable (stranded) merges are excluded — flag-trust is
    preserved for ancestry-indeterminate outcomes per the merge-trust policy.

    Built on :func:`verified_merged_count` as the base; subtracts
    ``stranded_prs_merged`` when present so a reconciliation pass that discovers
    confirmed-stranded merges can persist them on the entry and have them excluded
    here without touching any credit-gate caller. Legacy entries without the field
    are unchanged (stranded defaults to 0).

    ``stranded_prs_merged`` is reset to 0 by every full recompute in
    :func:`_update_direction_research_outcomes` because the recompute already
    excludes stranded tasks from ``verified_prs_merged`` (``is_merged_completion``
    returns False when ``landed_on_integration`` is explicitly False). The
    subtraction here only applies in the window between stranding and the next
    recompute; after a recompute both ``stranded_prs_merged`` and
    ``verified_prs_merged`` are consistent and the subtraction is a no-op.
    """
    verified = verified_merged_count(entry)
    stranded = int(entry.get("stranded_prs_merged") or 0)
    return max(0, verified - stranded)


def proven_merged_count(entry: dict) -> int:
    """Machine-checked PROVEN merged-PR count for a research_history entry.

    The honest counterpart to :func:`verified_merged_count`: counts only merges
    proven by machine-checked evidence — either a satisfied ``named_test`` that
    ran green, or a satisfied ``downstream_artifact`` that was subsequently
    consumed (recorded downstream-artifact consumption) — as recorded in the
    additive ``acceptance_proven_merged`` field by
    ``_update_direction_research_outcomes``. Strictly distinct from — and never
    larger than — ``verified_prs_merged``: a soft (behavior or unconsumed
    artifact) or undeclared merge counts as verified but NOT proven. Legacy
    entries written before the field existed degrade to ``0`` (nothing is
    retroactively claimed as proven), never to the raw merged count.
    """
    return int(entry.get("acceptance_proven_merged") or 0)


def consumed_merged_count(entry: dict) -> int:
    """Merged downstream_artifact count proven by downstream consumption."""
    return int(entry.get("acceptance_consumed_merged") or 0)


def reachable_merged_count(entry: dict) -> int | None:
    """Ancestry-reachable merged-PR count for a research_history entry.

    Returns the subset of ``prs_merged`` whose persisted task context carries
    ``landed_on_integration=True`` — confirming the merge commit was an ancestor
    of the integration branch at the time it was stamped. Distinct from
    :func:`verified_merged_count`: reachability is about ancestry confirmation,
    not verification quality.

    Returns ``None`` for legacy entries written before ``reachable_prs_merged``
    was recorded — the reachability of those merges is genuinely unknown, and
    fabricating zero would misrepresent the historical data. Callers that render
    a reachable-vs-badge split must omit it when this returns ``None``.
    """
    if "reachable_prs_merged" not in entry:
        return None
    return int(entry.get("reachable_prs_merged") or 0)


def declared_merged_count(entry: dict) -> int:
    """Plan-time-declared merged-PR count for a research_history entry.

    Counts merged WRITE tasks that DECLARED forward acceptance evidence at plan
    time (a verdict was stamped), as recorded in the additive
    ``acceptance_declared_merged`` field by ``_update_direction_research_outcomes``.
    The numerator of declaration coverage; pair it with
    ``acceptance_merged_write_total`` (the coverage denominator) to recompute
    ``declared / merged_write_total`` across cycles without lossy per-cycle
    fraction-averaging. Legacy entries written before the field existed degrade to
    ``0`` (nothing retroactively claimed as declared).
    """
    return int(entry.get("acceptance_declared_merged") or 0)


def acceptance_metric_invariant_violations(entry: dict) -> list[str]:
    """Return the acceptance-evidence metric invariants VIOLATED by ``entry``.

    Empty list ⇒ every guarantee holds. Pure and read-only over a single
    research_history entry's already-computed aggregates — it never mutates,
    recomputes, or changes any metric value. It is the durable guard that locks
    the SEMANTIC relationships between the acceptance-evidence metrics so a future
    refactor of their definitions cannot silently let, say, ``proven`` drift above
    ``verified``, or a soft / undeclared / degraded merge be retro-counted as
    proven, with nothing failing:

      * proven ⊆ declared ⊆ merged-write
        (0 ≤ acceptance_proven_merged ≤ acceptance_declared_merged
         ≤ acceptance_merged_write_total)
      * proven ⊆ verified ⊆ merged
        (0 ≤ acceptance_proven_merged ≤ verified_prs_merged ≤ prs_merged)
      * declared ⊆ merged          (acceptance_declared_merged ≤ prs_merged)
      * inferred ⊆ merged-write, disjoint from declared
        (0 ≤ acceptance_inferred_merged ≤ acceptance_merged_write_total and
         acceptance_inferred_merged + acceptance_declared_merged
         ≤ acceptance_merged_write_total)
      * coverage identity          (acceptance_declaration_coverage ==
        round(declared / merged-write, 3), or None ⇔ merged-write == 0)

    Fail-safe over legacy rows: a field written before it existed reads as 0 via
    :func:`proven_merged_count` / :func:`verified_merged_count` and ``.get``
    defaults, and the merged-write denominator / coverage identity checks are
    skipped when the denominator was never recorded — so historical entries never
    spuriously "violate".
    """
    violations: list[str] = []
    proven = proven_merged_count(entry)
    consumed = consumed_merged_count(entry)
    declared = int(entry.get("acceptance_declared_merged") or 0)
    inferred = int(entry.get("acceptance_inferred_merged") or 0)
    verified = verified_merged_count(entry)
    prs_merged = int(entry.get("prs_merged") or 0)

    if proven < 0:
        violations.append(f"proven ({proven}) is negative")
    if inferred < 0:
        violations.append(f"inferred ({inferred}) is negative")
    if consumed < 0:
        violations.append(f"consumed ({consumed}) is negative")
    if consumed > proven:
        violations.append(f"consumed ({consumed}) exceeds proven ({proven})")
    if proven > declared:
        violations.append(f"proven ({proven}) exceeds declared ({declared})")
    if proven > verified:
        violations.append(f"proven ({proven}) exceeds verified ({verified})")
    if verified > prs_merged:
        violations.append(f"verified ({verified}) exceeds prs_merged ({prs_merged})")
    if declared > prs_merged:
        violations.append(f"declared ({declared}) exceeds prs_merged ({prs_merged})")

    # reachable_prs_merged must be a non-negative subset of prs_merged. Only
    # checked when the field is present — legacy entries without it are skipped.
    if "reachable_prs_merged" in entry:
        reachable = int(entry.get("reachable_prs_merged") or 0)
        if reachable < 0:
            violations.append(f"reachable ({reachable}) is negative")
        if reachable > prs_merged:
            violations.append(
                f"reachable ({reachable}) exceeds prs_merged ({prs_merged})"
            )
    # verified_autonomous_prs_merged is the honest, verification-aware subset of
    # autonomous_prs_merged used for the retirement milestone. It must never
    # outrun either the raw autonomous count or the verified count. Only checked
    # when present — legacy entries without it are skipped.
    if "verified_autonomous_prs_merged" in entry:
        verified_autonomous = int(entry.get("verified_autonomous_prs_merged") or 0)
        autonomous = int(entry.get("autonomous_prs_merged") or 0)
        if verified_autonomous < 0:
            violations.append(
                f"verified-autonomous ({verified_autonomous}) is negative"
            )
        if verified_autonomous > autonomous:
            violations.append(
                f"verified-autonomous ({verified_autonomous}) exceeds "
                f"autonomous ({autonomous})"
            )
        if verified_autonomous > verified:
            violations.append(
                f"verified-autonomous ({verified_autonomous}) exceeds "
                f"verified ({verified})"
            )
    # stranded_prs_merged is a delta-patch applied between stranding and the next
    # full recompute; _update_direction_research_outcomes resets it to 0 once a
    # recompute runs (stranded tasks are already absent from prs_merged at that
    # point), so it is not partitioned against prs_merged here.
    if "stranded_prs_merged" in entry:
        stranded = int(entry.get("stranded_prs_merged") or 0)
        if stranded < 0:
            violations.append(f"stranded ({stranded}) is negative")

    coverage = entry.get("acceptance_declaration_coverage")
    if "acceptance_merged_write_total" in entry:
        merged_write = int(entry.get("acceptance_merged_write_total") or 0)
        verified_write = int(entry.get("acceptance_verified_write_merged") or 0)
        if declared > merged_write:
            violations.append(
                f"declared ({declared}) exceeds merged-write ({merged_write})"
            )
        if merged_write > prs_merged:
            violations.append(
                f"merged-write ({merged_write}) exceeds prs_merged ({prs_merged})"
            )
        if verified_write > merged_write:
            violations.append(
                "verified-write "
                f"({verified_write}) exceeds merged-write ({merged_write})"
            )
        if verified_write > verified:
            violations.append(
                f"verified-write ({verified_write}) exceeds verified ({verified})"
            )
        # inferred_test_cochange merges are a subset of merged-write, and are
        # disjoint from declared merges (the classifier only infers when no
        # verdict dict is present), so declared + inferred also cannot exceed the
        # merged-write population.
        if inferred > merged_write:
            violations.append(
                f"inferred ({inferred}) exceeds merged-write ({merged_write})"
            )
        if inferred + declared > merged_write:
            violations.append(
                f"inferred + declared ({inferred + declared}) exceeds "
                f"merged-write ({merged_write})"
            )
        if merged_write <= 0:
            if coverage is not None:
                violations.append(
                    f"coverage ({coverage}) is not None with zero merged-write"
                )
        else:
            expected = round(declared / merged_write, 3)
            if coverage != expected:
                violations.append(
                    f"coverage ({coverage}) != declared/merged-write "
                    f"({declared}/{merged_write} = {expected})"
                )
    elif coverage is not None and not (0.0 <= coverage <= 1.0):
        # Legacy entry without the explicit denominator — at minimum coverage
        # must be a fraction.
        violations.append(f"coverage ({coverage}) outside [0, 1]")

    return violations


def acceptance_metrics_consistent(entry: dict) -> bool:
    """Whether ``entry``'s acceptance-evidence metrics satisfy every invariant.

    Thin boolean wrapper over :func:`acceptance_metric_invariant_violations`
    (True ⇔ no violations) for call sites that only need the verdict.
    """
    return not acceptance_metric_invariant_violations(entry)


_AUTONOMY_RETIREMENT_THRESHOLD = 10

# Bounds for the compact refuted/unresolved spawn-gate reasons sample rolled into
# each research_history entry — kept in lockstep with the producer caps in
# research_completion.py so the sample stays compact across many cycles.
_SPAWN_GATE_REASONS_SAMPLE_CAP = 5
_SPAWN_GATE_REASON_MAX_LEN = 200


def compute_direction_autonomy_state(direction: dict) -> dict:
    """Return lifetime autonomy metrics and retirement eligibility for a direction.

    The SINGLE source of truth for the retirement-eligibility milestone —
    strategic_spawning.py's strategist stat payload calls this rather than
    re-deriving the rule inline, so the two surfaces can never disagree.

    Retirement eligibility credits only VERIFIABLY-LANDED autonomous merges:
    ``lifetime_autonomous_prs_merged`` sums the verification-aware
    ``verified_autonomous_prs_merged`` field (see
    :func:`verified_autonomous_merged_count`) across research_history plus the
    ``total_verified_autonomous_prs_merged`` aggregate. Degraded-unverified and
    acceptance-unsatisfied autonomous merges — merges that never verifiably
    landed — are therefore NOT counted, mirroring how ``verified_prs_merged``
    relates to the raw ``prs_merged``. A two-step legacy fallback keeps
    historical directions whole: entries without the verified field fall back to
    the raw ``autonomous_prs_merged`` count, and pre-attribution entries fall
    back to ``prs_merged``, so no direction silently loses accumulated credit.

    A direction is retirement-eligible when lifetime_autonomous_prs_merged >= 10
    AND lifetime_operator_actions == 0 (zero-tolerance for operator intervention).
    """
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    lifetime_autonomous = sum(
        verified_autonomous_merged_count(h) for h in history
    ) + (
        agg.get(
            "total_verified_autonomous_prs_merged",
            agg.get("total_autonomous_prs_merged", 0),
        )
        or 0
    )
    lifetime_op_actions = sum(h.get("operator_action_count", 0) for h in history) + (
        agg.get("total_operator_actions") or 0
    )
    return {
        "lifetime_autonomous_prs_merged": lifetime_autonomous,
        "lifetime_operator_actions": lifetime_op_actions,
        "retirement_eligible": (
            lifetime_autonomous >= _AUTONOMY_RETIREMENT_THRESHOLD
            and lifetime_op_actions == 0
        ),
    }


def _longest_chain_length(deps_map: dict[str, list[str]]) -> int:
    """Longest path length in a subtask dependency DAG.

    deps_map keys are subtask UUID strings; values are lists of UUID strings
    that the key depends on. Returns 0 when the map is empty. Self-references
    and external IDs are filtered out; cycles are broken (counted as 0).
    """
    memo: dict[str, int] = {}
    nodes = set(deps_map.keys())

    def depth(n: str, stack: frozenset) -> int:
        if n in memo:
            return memo[n]
        if n in stack:
            return 0
        inner = stack | {n}
        ds = [d for d in deps_map.get(n, []) if d in nodes and d != n]
        memo[n] = 1 + max((depth(d, inner) for d in ds), default=0)
        return memo[n]

    return max((depth(n, frozenset()) for n in nodes), default=0)


def _apply_cochange_to_doc(
    doc: dict,
    merged_files_lists: list[list[str]],
    pr_keys: list[str] | None = None,
) -> None:
    """Apply co-change affinity index updates to a design_doc dict in place.

    Core logic extracted from _update_cochange_index so it can be called
    inside with_design_doc_locked (which passes the doc dict directly).

    When pr_keys is provided (a list aligned with merged_files_lists), each
    entry is only folded in once — already-applied keys are skipped.  If all
    keys are already recorded the function returns without touching the index
    (no spurious decay).  The applied-key set is capped at 1000 entries; an
    evicted key may be re-folded on the next refresh at most once, which is
    safe (it does not corrupt scores the way saturation does).
    """
    APPLIED_CAP = 1000

    if pr_keys is not None:
        # Idempotent path: filter to only genuinely-new PRs.
        applied_list: list[str] = list(doc.get("cochange_applied_prs") or [])
        applied_set = set(applied_list)
        new_pairs = [
            (key, files)
            for key, files in zip(pr_keys, merged_files_lists)
            if key not in applied_set
        ]
        if not new_pairs:
            return
        new_keys = [key for key, _ in new_pairs]
        effective_files_lists = [files for _, files in new_pairs]
    else:
        # Legacy path: no idempotency tracking; apply all lists as before.
        new_keys = []
        effective_files_lists = merged_files_lists
        applied_list = []

    if not effective_files_lists:
        return

    DECAY = 0.90
    TOP_N = 5
    MAX_FILES = 200
    MIN_SCORE = 0.3

    raw_index = doc.get("cochange_index") or {}
    index: dict[str, dict[str, float]] = {k: dict(v) for k, v in raw_index.items()}

    # Decay all existing scores
    for file_key in list(index.keys()):
        file_affinities = index[file_key]
        for co_file in list(file_affinities.keys()):
            file_affinities[co_file] = round(file_affinities[co_file] * DECAY, 3)
            if file_affinities[co_file] < MIN_SCORE:
                del file_affinities[co_file]
        if not file_affinities:
            del index[file_key]

    # Accumulate new co-change pairs from each merged PR
    for files in effective_files_lists:
        unique_files = list(dict.fromkeys(files))
        if len(unique_files) < 2:
            continue
        for i, file_a in enumerate(unique_files):
            for file_b in unique_files[i + 1 :]:
                if file_a not in index:
                    index[file_a] = {}
                index[file_a][file_b] = round(index[file_a].get(file_b, 0.0) + 1.0, 3)
                if file_b not in index:
                    index[file_b] = {}
                index[file_b][file_a] = round(index[file_b].get(file_a, 0.0) + 1.0, 3)

    # Prune to top-N per file
    for file_key in list(index.keys()):
        affinities = index[file_key]
        if len(affinities) > TOP_N:
            sorted_pairs = sorted(affinities.items(), key=lambda x: x[1], reverse=True)
            index[file_key] = dict(sorted_pairs[:TOP_N])

    # Cap total tracked files
    if len(index) > MAX_FILES:
        file_scores = {f: sum(v.values()) for f, v in index.items()}
        top_files = sorted(
            file_scores.keys(), key=lambda f: file_scores[f], reverse=True
        )[:MAX_FILES]
        index = {f: index[f] for f in top_files}

    doc["cochange_index"] = index

    # Persist applied keys (idempotent path only).
    if pr_keys is not None and new_keys:
        updated_applied = applied_list + new_keys
        if len(updated_applied) > APPLIED_CAP:
            updated_applied = updated_applied[-APPLIED_CAP:]
        doc["cochange_applied_prs"] = updated_applied


def _update_cochange_index(
    project: Project, merged_files_lists: list[list[str]]
) -> None:
    """Update the co-change affinity index in project.design_doc.

    For each merged PR's file list, records which files changed together.
    Scores decay on each call so stale associations fade over time.
    Keeps top-5 affinities per file, capped at 200 files total.
    """
    if not merged_files_lists:
        return
    doc = dict(project.design_doc or {})
    _apply_cochange_to_doc(doc, merged_files_lists)
    project.design_doc = doc
    flag_modified(project, "design_doc")


# ---------------------------------------------------------------------------
# Durable metric-governance manifest.
#
# Single source of truth for every metric field written into a direction's
# research_history entry by ``_update_direction_research_outcomes``. This
# replaces the ad-hoc inline ``Retired: …`` comments that used to be the only
# record of a metric's meaning and disposition (a comment disappears silently
# when the code is touched; this manifest is queryable and test-enforced).
#
# Each key is an emitted (or formerly-emitted) ``entry["<field>"]`` field.
# Each value declares ONE of:
#   * {"consumer": "<module/surface that reads it>"} — an active field with a
#     named reader. The consumer string names where the field is read (NOT the
#     write site here); it is documentation, not test-enforced for accuracy.
#   * {"status": "pending", "reason": "...", "date": "YYYY-MM-DD"} — a field
#     that is computed every cycle but currently has NO named consumer. This is
#     the exact recurrence-class the manifest exists to surface: a new metric
#     cannot land without either naming a consumer or being recorded here, and
#     the guard test fails on any emitted field absent from this manifest.
#   * {"status": "retired", "reason": "...", "date": "YYYY-MM-DD"} — a field
#     the computation NO LONGER writes. Kept as an auditable trail of what the
#     metric meant and why it was dropped (replaces a deleted inline comment).
#
# The guard test ``backend/testing/test_direction_outcome_metric_manifest.py``
# AST-extracts every ``entry["..."]`` Store target from the function below and
# asserts: (a) every emitted field is present here (no undeclared metric), and
# (b) every manifest key carrying a "consumer" is still emitted (no stale
# consumer record). "pending"/"retired" keys are exempt from (b) by design.
RESEARCH_HISTORY_METRIC_MANIFEST: dict[str, dict] = {
    # --- Cost ---
    "cost_by_provider_model": {
        "consumer": "direction_lifecycle.py (cost evidence), digest_direction_progress.py"
    },
    "estimated_cost_usd": {
        "consumer": "direction_lifecycle.py / admin.py (cumulative spend), strategic_analysis.py"
    },
    # --- Terminal task counts ---
    "tasks_created": {
        "consumer": "direction_metrics.py (total_tasks_created + momentum/conversion), anomaly_detection.py, claim_task_helpers.py, digest_direction_progress.py (conversion denominator)"
    },
    "tasks_completed": {
        "consumer": "direction_metrics.py, strategic_completion.py, research_completion.py"
    },
    "tasks_failed": {
        "consumer": "anomaly_detection.py, direction_lifecycle.py, strategic_spawning.py, digest_direction_progress.py"
    },
    "tasks_cancelled": {"consumer": "direction_metrics.py (terminal-outcome tracking)"},
    "scope_empty_noop_count": {
        "consumer": "research_direction_state._detect_already_fixed_deadlock (already-fixed deadlock detection)"
    },
    "tasks_completed_no_pr": {
        "consumer": "direction_metrics.py, strategic_completion.py (no-PR completions)"
    },
    # --- PR / merge counts ---
    "prs_merged": {
        "consumer": "digest_direction_progress.py, direction_lifecycle.py, strategic_spawning.py, DirectionCard.tsx (+ many)"
    },
    "merged_prs_any_base": {
        "consumer": "strategic_completion.py (_compute_churn_metrics raw-merge-volume backstop — never pause a direction with real merges in flight)"
    },
    "prs_rejected": {
        "consumer": "review_pipeline.py, pipeline_health.py, strategic_spawning.py"
    },
    "autonomous_prs_merged": {
        "consumer": "compute_direction_autonomy_state (this module), strategic_spawning.py, DirectionCard.tsx"
    },
    "verified_autonomous_prs_merged": {
        "consumer": "compute_direction_autonomy_state (this module), strategic_spawning.py"
    },
    "assisted_prs_merged": {
        "consumer": "digest_direction_progress.py, direction_metrics.py"
    },
    "verified_prs_merged": {
        "consumer": "verified_merged_count (this module), digest_metrics.py, strategic_spawning.py"
    },
    "stranded_prs_merged": {
        "consumer": (
            "credit_gate_merged_count (this module), direction_metrics.py "
            "(total_stranded_prs_merged), strategic_completion.py, "
            "research_completion.py, research_direction_state.py"
        )
    },
    "reachable_prs_merged": {
        "consumer": "reachable_merged_count (this module), strategic_spawning.py (stat_entry reachable_prs_merged), strategic_payload_compaction.py (_compact_direction_summary_entry)"
    },
    # --- Acceptance aggregates: now wired to decision + reader-facing consumers. ---
    # These expose proven-by-evidence and declaration-coverage truth distinct
    # from "merely merged". Reader-facing surfaces show total proven merges and
    # the downstream-artifact consumed subset in admin direction progress,
    # direction portfolio summaries, strategic direction summaries, and
    # project-context pipeline-health direction summaries.
    "acceptance_proven_merged": {
        "consumer": "digest_direction_progress.py / strategic_spawning.py payloads rendered by admin direction summaries and strategic agent direction summaries"
    },
    "acceptance_consumed_merged": {
        "consumer": "admin-ui DirectionProgressSection.tsx + DirectionPortfolioSection.tsx, agent strategic-sections.ts + project-context.ts (consumed subset beside proven merges)"
    },
    "acceptance_declared_merged": {
        "consumer": "digest_direction_progress.py (declaration coverage), strategic_spawning.py, direction_metrics.py (total_acceptance_declared_merged)"
    },
    "acceptance_inferred_merged": {
        "consumer": "direction_metrics.py (total_acceptance_inferred_merged lifetime fold + build_evicted_cycle_snapshot), acceptance_metric_invariant_violations (this module, inferred-ordering guard)"
    },
    "acceptance_merged_write_total": {
        "consumer": "digest_direction_progress.py + strategic_spawning.py (coverage denominator), direction_metrics.py (total_acceptance_merged_write_total)"
    },
    "acceptance_verified_write_merged": {
        "consumer": "direction_metrics.py (proof_weighted_merged / _agg_proof_weighted_merged write-slice weighting)"
    },
    "acceptance_declaration_coverage": {
        "consumer": "digest_direction_progress.py (per-cycle + lifetime coverage surface), strategic_spawning.py"
    },
    # --- Operator / autonomy ---
    "operator_action_count": {
        "consumer": "compute_direction_autonomy_state (this module), strategic_spawning.py, digest_direction_progress.py"
    },
    # --- Preventive-context cohort ---
    "preventive_context_tasks_created": {
        "consumer": "routers/admin_digest.py (digest reporting)"
    },
    "preventive_context_tasks_merged": {
        "consumer": "routers/admin_digest.py (digest reporting)"
    },
    "preventive_context_tasks_failed": {
        "consumer": "routers/admin_digest.py (digest reporting)"
    },
    # --- Conversion: retired stored copy. ---
    # Every conversion surface recomputes conversion from verified_prs_merged /
    # tasks_created (digest_direction_progress.py:466, pipeline_health.py,
    # strategic_analysis.py) rather than reading the stored entry field. The
    # stored copy has been removed; all conversion semantics survive via the
    # canonical count fields.
    "conversion_rate": {
        "status": "retired",
        "reason": "stored copy was never read; all conversion surfaces (digest_direction_progress.py:466, pipeline_health.py, strategic_analysis.py) recompute from verified_prs_merged / tasks_created",
        "date": "2026-06-07",
    },
    # --- Generalized (all-task-type) outcomes ---
    "generalized_successes": {
        "consumer": "digest_direction_progress.py, direction_metrics.py, strategic_analysis.py, DirectionCard.tsx"
    },
    "generalized_health_rate": {
        "consumer": "digest_direction_progress.py, direction_metrics.py, routers/directions.py, DirectionCard.tsx"
    },
    "outcome_kind_breakdown": {
        "consumer": "digest_direction_progress.py, direction_metrics.py, DirectionCard.tsx"
    },
    # --- Structured-output format-compliance (equipping direction d-46468a41) ---
    # Per-cycle copies of the windowed digest measure
    # (digest_metrics.compute_format_compliance_metrics, surfaced as the
    # admin_digest "format_compliance" key). Both are built straight from the
    # extraction layer's own parse verdict, so they are non-gameable. The live
    # decision/operator surface today is the windowed digest measure; these
    # per-direction copies are emitted for a future strategic/operator reader and
    # carry no consumer yet, so they are declared pending (not consumer).
    "format_compliance_rate": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx (per-direction expanded panel)"
    },
    "self_repair_success_rate": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx (per-direction expanded panel)"
    },
    # --- Review-quality signals ---
    "reviews_dismissed": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx"
    },
    "reviews_overridden": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx"
    },
    "user_issues_caught": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx"
    },
    # --- Timing / iteration ---
    "avg_time_to_merge_hours": {
        "consumer": "digest_direction_progress.py, routers/agent.py, strategic_analysis.py"
    },
    "median_review_iterations": {
        "consumer": "autonomous_research.py, efficiency_snapshot.py, direction_metrics.py, routers/agent.py"
    },
    "avg_review_iterations": {
        "consumer": "autonomous_research.py (rolling review-iteration guidance)"
    },
    # --- Failure detail / taxonomy ---
    "failed_task_details": {
        "consumer": "research_context.py, strategic_spawning.py, agent/research-plan.ts"
    },
    "tasks_failed_infra": {
        "consumer": "direction_lifecycle.py, strategic_spawning.py, strategic_completion.py"
    },
    "taxonomy_summary": {
        "consumer": "digest_direction_progress.py, digest_metrics.py, strategic_spawning.py"
    },
    "no_op_task_details": {"consumer": "research_context.py (stale-finding retagging)"},
    "unproven_merge_details": {
        "consumer": "research_context.py (unproven-merge retagging)"
    },
    "stage_dropout": {
        "consumer": "digest_direction_progress.py, pipeline_health.py, claim_task_helpers.py"
    },
    # --- Type / scope distribution ---
    "type_distribution": {
        "consumer": "digest_direction_progress.py, digest_metrics.py, strategic_analysis.py"
    },
    "type_breakdown": {
        "consumer": "digest_direction_progress.py, DirectionProgressSection.tsx"
    },
    "product_ratio": {
        "consumer": "digest_direction_progress.py, digest_metrics.py, strategic_analysis.py"
    },
    "scope_breakdown": {
        "consumer": "digest_direction_progress.py, pipeline_health.py, spawn_calibration.py, agent/research-plan.ts"
    },
    "escalation_outcomes": {
        "consumer": "research_completion.py, routers/admin_digest.py, strategic_analysis.py"
    },
    # --- Prompt quality / decomposition ---
    "prompt_telemetry": {
        "consumer": "synthesize_prompt_quality_patterns (this module) -> digest_direction_progress.py, routers/agent.py"
    },
    "research_spawn_gate_challenged_count": {
        "consumer": "digest_direction_progress.py, routers/agent.py (research spawn-gate cycle telemetry)"
    },
    "research_spawn_gate_refuted_count": {
        "consumer": "digest_direction_progress.py, routers/agent.py (research spawn-gate cycle telemetry)"
    },
    "research_spawn_gate_survived_count": {
        "consumer": "digest_direction_progress.py, routers/agent.py (research spawn-gate cycle telemetry)"
    },
    "research_spawn_gate_explicit_pass_count": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (decision-quality breakdown: explicit challenger pass slice of survived)"
    },
    "research_spawn_gate_fail_open_count": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (decision-quality breakdown: fail-open/no-explicit-verdict pass slice of survived)"
    },
    "research_spawn_gate_unresolved_count": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (decision-quality breakdown: unresolved/no-verdict slice of refuted)"
    },
    "research_spawn_gate_wholesale_retry_exhausted": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (wholesale grader retry-exhaustion count / infrastructure-failure signal)"
    },
    "research_spawn_gate_refuted_reasons": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (compact deduped sample of refuted/unresolved challenger reasons for a degraded-gate signal)"
    },
    "research_spawn_gate_parse_outcome": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (machine-readable adversarial parse classification for unresolved cycles)"
    },
    "research_spawn_gate_cause": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (machine-readable grader non-response cause object for unresolved cycles)"
    },
    "research_spawn_gate_cause_counts": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (per-cause count of unresolved findings for cause distribution aggregation)"
    },
    "research_spawn_gate_manifest_excluded_fail_closed_count": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (manifest-excluded high-stakes fail-closed deferral count)"
    },
    "research_spawn_gate_manifest_excluded_fail_closed_samples": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (compact samples of high-stakes findings excluded from the bounded adversarial manifest)"
    },
    "research_spawn_gate_adversarial_give_up_count": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (count of findings permanently discarded after 3 consecutive adversarial-gate deferrals)"
    },
    "research_spawn_gate_adversarial_give_up_samples": {
        "consumer": "digest_direction_progress.py, strategic_spawning.py (compact samples of finding titles permanently discarded by adversarial-gate give-up)"
    },
    "research_spawn_gate_challenged_spawned": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "research_spawn_gate_challenged_merged": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "research_spawn_gate_challenged_merge_rate": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "research_spawn_gate_unchallenged_spawned": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "research_spawn_gate_unchallenged_merged": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "research_spawn_gate_unchallenged_merge_rate": {
        "consumer": "digest_direction_progress.py (challenged-vs-unchallenged merge-rate rollup)"
    },
    "decomposition_quality": {
        "consumer": "digest_direction_progress.py, playbook_learning.py, spawn_calibration.py"
    },
    "cochange_decomposition": {
        "consumer": "research_completion.py (co-change split outcomes)"
    },
    # --- Retired: no longer emitted by the computation. ---
    # Auditable trail replacing the deleted inline ``Retired: …`` comments.
    "tasks_blocked": {
        "status": "retired",
        "reason": "claimed operator/analytics visibility but had no named consumer (no backend module, analytics, or UI read it)",
        "date": "2026-06-05",
    },
    "remediation_tasks_created": {
        "status": "retired",
        "reason": "intended to track remediation burden but had no named consumer",
        "date": "2026-06-05",
    },
    "remediation_tasks_merged": {
        "status": "retired",
        "reason": "intended to track remediation burden but had no named consumer",
        "date": "2026-06-05",
    },
    "remediation_tasks_failed": {
        "status": "retired",
        "reason": "intended to track remediation burden but had no named consumer",
        "date": "2026-06-05",
    },
    "avg_implementation_hours": {
        "status": "retired",
        "reason": "split of avg_time_to_merge_hours into implementation vs review time; no named consumer",
        "date": "2026-06-05",
    },
    "avg_review_hours": {
        "status": "retired",
        "reason": "split of avg_time_to_merge_hours into implementation vs review time; no named consumer",
        "date": "2026-06-05",
    },
    "structural_count": {
        "status": "retired",
        "reason": "raw structural-task count was stored but never read; the derived product_ratio is what digest_direction_progress.py consumes",
        "date": "2026-06-05",
    },
}


def _apply_evicted_generalized_and_acceptance_deltas(
    agg: dict, snap: dict, entry: dict
) -> None:
    """Heal the acceptance-verified + generalized aggregate keys on a late recompute.

    Companion to the primitive ``_DELTA_FIELDS`` loop in
    :func:`_apply_direction_outcomes`: when a late recompute runs on an evicted
    cycle, this applies deltas for the aggregate keys that
    :func:`~app.direction_metrics._fold_evicted_into_aggregate` credits but the
    primitive delta list omits — ``total_acceptance_verified_merged``,
    ``total_acceptance_verified_write_merged`` (both gated on
    ``acceptance_merged_write_total`` presence in the fresh ``entry``, mirroring
    the fold's condition so a cycle that lost acceptance-awareness is handled
    consistently), ``total_generalized_successes``,
    ``total_generalized_terminal_outcomes``, and the nested
    ``outcome_kind_breakdown``.

    Reuses the same direction_metrics helpers the fold uses so the delta and the
    fold cannot drift. LEGACY BACKFILL: snapshots persisted before these baselines
    existed lack the new keys; each missing baseline is seeded from the SAME
    fallback the fold used BEFORE the delta is taken (see per-field comments), so
    no legacy cycle is double-counted or dropped to zero.
    """
    from app.direction_metrics import (  # noqa: PLC0415
        _OUTCOME_KIND_STATUSES,
        _copy_outcome_kind_breakdown,
        _entry_generalized_successes,
        _entry_generalized_terminal_outcomes,
        _fallback_outcome_kind_breakdown,
    )

    _fresh_acceptance_aware = "acceptance_merged_write_total" in entry
    # --- Acceptance-verified merged base ---
    # Fresh contribution mirrors the fold: verified_merged_count credited only when
    # the cycle is acceptance-aware. A legacy snapshot without the stored baseline
    # falls back to verified_merged_count(snap) (reconstructed from the snapshot's
    # verified_prs_merged), consistent with the value the fold credited at eviction.
    _fresh_acc_verified = verified_merged_count(entry) if _fresh_acceptance_aware else 0
    _snap_acc_verified = int(
        snap.get("acceptance_verified_merged", verified_merged_count(snap)) or 0
    )
    _delta = _fresh_acc_verified - _snap_acc_verified
    if _delta:
        agg["total_acceptance_verified_merged"] = (
            int(agg.get("total_acceptance_verified_merged") or 0) + _delta
        )
    # --- Verified WRITE slice ---
    # The write-verified count cannot be reconstructed from a legacy snapshot's
    # stored primitives, so when the baseline is absent it is seeded to the fresh
    # value (delta 0): the folded eviction-time value is retained rather than
    # double-counted, and the snapshot rewrite re-syncs it for future recomputes.
    _fresh_acc_verified_write = (
        int(entry.get("acceptance_verified_write_merged") or 0)
        if _fresh_acceptance_aware
        else 0
    )
    _snap_acc_verified_write = int(
        snap.get("acceptance_verified_write_merged", _fresh_acc_verified_write) or 0
    )
    _delta = _fresh_acc_verified_write - _snap_acc_verified_write
    if _delta:
        agg["total_acceptance_verified_write_merged"] = (
            int(agg.get("total_acceptance_verified_write_merged") or 0) + _delta
        )
    # --- Generalized successes / terminal outcomes ---
    # The per-cycle generalized contribution is NOT reconstructable from the sparse
    # snapshot (it includes read-only tasks, failed tasks, etc. — not just merges).
    # For a legacy snapshot (new keys absent), seed to the FRESH value so the delta
    # is 0: the fold already credited the rich eviction-time values into the aggregate,
    # so we retain those rather than re-adding. This mirrors the acceptance_verified_write
    # treatment above and avoids the double-count when terminal > prs_merged.
    _fresh_gen_succ = _entry_generalized_successes(entry)
    _snap_gen_succ = int(snap.get("generalized_successes", _fresh_gen_succ) or 0)
    _delta = _fresh_gen_succ - _snap_gen_succ
    if _delta:
        agg["total_generalized_successes"] = (
            int(agg.get("total_generalized_successes") or 0) + _delta
        )
    _fresh_gen_term = _entry_generalized_terminal_outcomes(entry)
    _snap_gen_term = int(snap.get("generalized_terminal_outcomes", _fresh_gen_term) or 0)
    _delta = _fresh_gen_term - _snap_gen_term
    if _delta:
        agg["total_generalized_terminal_outcomes"] = (
            int(agg.get("total_generalized_terminal_outcomes") or 0) + _delta
        )
    # --- Nested outcome_kind_breakdown ---
    # Delta per kind and per status; clamp each bucket status independently at 0 so
    # a negative delta can never push one bucket negative and corrupt the others.
    _fresh_bd = _copy_outcome_kind_breakdown(
        entry.get("outcome_kind_breakdown") or _fallback_outcome_kind_breakdown(entry)
    )
    _snap_bd_raw = snap.get("outcome_kind_breakdown")
    if isinstance(_snap_bd_raw, dict):
        _snap_bd = _copy_outcome_kind_breakdown(_snap_bd_raw)
    else:
        # Legacy snapshot without the breakdown: seed to the fresh value so the
        # delta is 0 per bucket (retain the already-folded eviction-time credit).
        _snap_bd = _copy_outcome_kind_breakdown(_fresh_bd)
    _agg_bd = agg.get("outcome_kind_breakdown")
    if not isinstance(_agg_bd, dict):
        _agg_bd = {}
    for _kind in set(_fresh_bd) | set(_snap_bd):
        _fresh_counts = _fresh_bd.get(_kind) or {}
        _snap_counts = _snap_bd.get(_kind) or {}
        _bucket = _agg_bd.get(_kind)
        if not isinstance(_bucket, dict):
            _bucket = {"success": 0, "neutral": 0, "failure": 0}
            _agg_bd[_kind] = _bucket
        for _status in _OUTCOME_KIND_STATUSES:
            _status_delta = int(_fresh_counts.get(_status, 0) or 0) - int(
                _snap_counts.get(_status, 0) or 0
            )
            if _status_delta:
                _bucket[_status] = max(
                    0, int(_bucket.get(_status, 0) or 0) + _status_delta
                )
    agg["outcome_kind_breakdown"] = _agg_bd


async def _update_direction_research_outcomes(
    project_id: uuid.UUID,
    direction_id: str,
    research_task_id: str,
    session: AsyncSession,
) -> None:
    """Update a direction's research_history entry with child task outcome counts.

    Called whenever a child task of a research cycle reaches a terminal status so
    that research_history entries accumulate live outcome statistics.
    """
    from app.research_context import _parse_research_context  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if not project:
        return
    merged_files_lists: list[list[str]] = []
    merged_pr_keys: list[str] = []
    # Snapshot read for early-exit guards and computation base; the actual
    # write is serialized via with_directions_locked below.
    d = next(
        (x for x in (project.directions or []) if x.get("id") == direction_id), None
    )
    if d is None:
        return
    entry_snap = next(
        (
            e
            for e in d.get("research_history", [])
            if e.get("task_id") == research_task_id
        ),
        None,
    )
    _evicted_snap: dict | None = None
    if entry_snap is None:
        # Check whether this cycle was evicted into history_aggregate vs. genuinely absent.
        _evicted_snap = (
            (d.get("history_aggregate") or {}).get("_evicted_cycle_snapshots") or {}
        ).get(research_task_id)
        if _evicted_snap is None:
            return
        # Entry was evicted — continue computation so delta logic can apply
        # incremental merge-count updates to history_aggregate via _apply_direction_outcomes.
        entry_snap = {"task_id": research_task_id}
    # Work on a mutable copy; all stat writes accumulate here and are then
    # applied to the freshly-locked directions row inside with_directions_locked.
    entry = dict(entry_snap)
    try:
        research_task_uuid = uuid.UUID(entry.get("task_id", ""))
    except (ValueError, AttributeError):
        return
    research_task = await session.get(Task, research_task_uuid)
    research_task_ctx = research_task.context or {} if research_task is not None else {}
    # Per-direction breakdown: when research_completion.py writes a
    # research_spawn_gate_by_direction dict, use the entry for this direction
    # so a multi-direction cycle does not smear one direction's gate activity
    # across sibling directions. When the per-direction dict is present but
    # this direction has no entry, treat it as a zero-activity new-format
    # direction instead of falling back to a sibling's legacy global counters.
    _per_dir_gate_map = research_task_ctx.get("research_spawn_gate_by_direction")
    if isinstance(_per_dir_gate_map, dict):
        _per_dir_gate = _per_dir_gate_map.get(direction_id)
        if direction_id in _per_dir_gate_map:
            if not isinstance(_per_dir_gate, dict):
                _per_dir_gate = {}
            spawn_gate_challenged_count = int(
                _per_dir_gate.get("challenged_count") or 0
            )
            spawn_gate_refuted_count = int(_per_dir_gate.get("refuted_count") or 0)
            spawn_gate_survived_count = int(_per_dir_gate.get("survived_count") or 0)
            spawn_gate_explicit_pass_count = int(
                _per_dir_gate.get("explicit_pass_count") or 0
            )
            spawn_gate_fail_open_count = int(_per_dir_gate.get("fail_open_count") or 0)
            spawn_gate_unresolved_count = int(
                _per_dir_gate.get("unresolved_count") or 0
            )
            spawn_gate_wholesale_retry_exhausted = bool(
                _per_dir_gate.get("wholesale_retry_exhausted")
            )
            spawn_gate_parse_outcome = _per_dir_gate.get("parse_outcome")
            spawn_gate_cause = _per_dir_gate.get("cause")
            spawn_gate_cause_counts = _per_dir_gate.get("cause_counts")
            spawn_gate_manifest_excluded_count = int(
                _per_dir_gate.get("manifest_excluded_count") or 0
            )
            spawn_gate_manifest_excluded_samples: list[dict] = [
                _s
                for _s in (_per_dir_gate.get("manifest_excluded_samples") or [])
                if isinstance(_s, dict)
            ][:_SPAWN_GATE_REASONS_SAMPLE_CAP]
            spawn_gate_refuted_reasons: list[str] = [
                str(_r).strip()[:_SPAWN_GATE_REASON_MAX_LEN]
                for _r in (_per_dir_gate.get("refuted_reasons") or [])
                if _r
            ][:_SPAWN_GATE_REASONS_SAMPLE_CAP]
        else:
            spawn_gate_challenged_count = 0
            spawn_gate_refuted_count = 0
            spawn_gate_survived_count = 0
            spawn_gate_explicit_pass_count = 0
            spawn_gate_fail_open_count = 0
            spawn_gate_unresolved_count = 0
            spawn_gate_wholesale_retry_exhausted = False
            spawn_gate_parse_outcome = None
            spawn_gate_cause = None
            spawn_gate_cause_counts = None
            spawn_gate_manifest_excluded_count = 0
            spawn_gate_manifest_excluded_samples = []
            spawn_gate_refuted_reasons = []
    else:
        # Global (legacy) fallback for pre-fix cycles and single-direction tasks.
        spawn_gate_challenged_count = int(
            research_task_ctx.get("research_spawn_gate_challenged_count") or 0
        )
        spawn_gate_refuted_count = int(
            research_task_ctx.get("research_spawn_gate_refuted_count") or 0
        )
        spawn_gate_survived_count = int(
            research_task_ctx.get("research_spawn_gate_survived_count") or 0
        )
        # Decision-quality breakdown of the inclusive survived/refuted buckets
        # (additive — the legacy counters above are preserved unchanged).
        spawn_gate_explicit_pass_count = int(
            research_task_ctx.get("research_spawn_gate_explicit_pass_count") or 0
        )
        spawn_gate_fail_open_count = int(
            research_task_ctx.get("research_spawn_gate_fail_open_count") or 0
        )
        spawn_gate_unresolved_count = int(
            research_task_ctx.get("research_spawn_gate_unresolved_count") or 0
        )
        spawn_gate_wholesale_retry_exhausted = bool(
            research_task_ctx.get("research_spawn_gate_wholesale_retry_exhausted")
        )
        spawn_gate_parse_outcome = research_task_ctx.get(
            "research_spawn_gate_parse_outcome"
        )
        spawn_gate_cause = research_task_ctx.get("research_spawn_gate_cause")
        spawn_gate_cause_counts = research_task_ctx.get(
            "research_spawn_gate_cause_counts"
        )
        spawn_gate_manifest_excluded_count = int(
            research_task_ctx.get(
                "research_spawn_gate_manifest_excluded_fail_closed_count"
            )
            or 0
        )
        spawn_gate_manifest_excluded_samples: list[dict] = []
        _seen_spawn_gate_manifest_excluded: set[tuple[str, str]] = set()
        for _sg_sample in (
            research_task_ctx.get(
                "research_spawn_gate_manifest_excluded_fail_closed_samples"
            )
            or []
        ):
            if not isinstance(_sg_sample, dict):
                continue
            _sample = {
                "hash": str(_sg_sample.get("hash") or "")[:80],
                "title": str(_sg_sample.get("title") or "")[:120],
                "reason": str(_sg_sample.get("reason") or "")[
                    :_SPAWN_GATE_REASON_MAX_LEN
                ],
            }
            _sample_key = (_sample["hash"], _sample["title"])
            if (
                (_sample["hash"] or _sample["title"])
                and _sample_key not in _seen_spawn_gate_manifest_excluded
                and len(spawn_gate_manifest_excluded_samples)
                < _SPAWN_GATE_REASONS_SAMPLE_CAP
            ):
                _seen_spawn_gate_manifest_excluded.add(_sample_key)
                spawn_gate_manifest_excluded_samples.append(_sample)
        # Compact, deduped, capped sample of refuted/unresolved challenger reasons.
        spawn_gate_refuted_reasons: list[str] = []
        _seen_spawn_gate_reasons: set[str] = set()
        for _sg_reason in (
            research_task_ctx.get("research_spawn_gate_refuted_reasons") or []
        ):
            _sg_reason = str(_sg_reason).strip()[:_SPAWN_GATE_REASON_MAX_LEN]
            if (
                _sg_reason
                and _sg_reason not in _seen_spawn_gate_reasons
                and len(spawn_gate_refuted_reasons) < _SPAWN_GATE_REASONS_SAMPLE_CAP
            ):
                _seen_spawn_gate_reasons.add(_sg_reason)
                spawn_gate_refuted_reasons.append(_sg_reason)
    # Adversarial give-up: findings permanently discarded after 3 consecutive
    # adversarial-gate deferrals (finding_structural_give_up.by_reason.adversarial_gate).
    _give_up = research_task_ctx.get("finding_structural_give_up") or {}
    spawn_gate_adversarial_give_up_count = int(
        (_give_up.get("by_reason") or {}).get("adversarial_gate", 0)
    )
    spawn_gate_adversarial_give_up_samples: list[dict] = []
    _seen_adversarial_give_up: set[str] = set()
    for _gu_sample in _give_up.get("sample") or []:
        if not isinstance(_gu_sample, dict):
            continue
        if _gu_sample.get("reason") != "adversarial_gate":
            continue
        _title = str(_gu_sample.get("title") or "")[:120]
        if (
            _title
            and _title not in _seen_adversarial_give_up
            and len(spawn_gate_adversarial_give_up_samples)
            < _SPAWN_GATE_REASONS_SAMPLE_CAP
        ):
            _seen_adversarial_give_up.add(_title)
            _gu_entry: dict = {"title": _title}
            _dir_id = _gu_sample.get("direction_id")
            if _dir_id:
                _gu_entry["direction_id"] = str(_dir_id)
            spawn_gate_adversarial_give_up_samples.append(_gu_entry)

    def _apply_spawn_gate_parent_metrics() -> None:
        # Keep these as direct entry[...] writes so the manifest guard can
        # discover them via AST, matching the other direction outcome fields.
        entry["research_spawn_gate_challenged_count"] = spawn_gate_challenged_count
        entry["research_spawn_gate_refuted_count"] = spawn_gate_refuted_count
        entry["research_spawn_gate_survived_count"] = spawn_gate_survived_count
        # Decision-quality breakdown - additive; the inclusive survived/refuted
        # counters above stay intact for back-compat.
        entry["research_spawn_gate_explicit_pass_count"] = (
            spawn_gate_explicit_pass_count
        )
        entry["research_spawn_gate_fail_open_count"] = spawn_gate_fail_open_count
        entry["research_spawn_gate_unresolved_count"] = spawn_gate_unresolved_count
        entry["research_spawn_gate_wholesale_retry_exhausted"] = (
            spawn_gate_wholesale_retry_exhausted
        )
        if spawn_gate_refuted_reasons:
            entry["research_spawn_gate_refuted_reasons"] = spawn_gate_refuted_reasons
        if spawn_gate_parse_outcome:
            entry["research_spawn_gate_parse_outcome"] = spawn_gate_parse_outcome
        if spawn_gate_cause:
            entry["research_spawn_gate_cause"] = spawn_gate_cause
        if spawn_gate_cause_counts:
            entry["research_spawn_gate_cause_counts"] = spawn_gate_cause_counts
        if spawn_gate_manifest_excluded_count:
            entry["research_spawn_gate_manifest_excluded_fail_closed_count"] = (
                spawn_gate_manifest_excluded_count
            )
            entry["research_spawn_gate_manifest_excluded_fail_closed_samples"] = (
                spawn_gate_manifest_excluded_samples
            )
        # Adversarial give-up: permanently discarded findings (separate bucket
        # from regular refuted/survived; additive, does not change legacy counters).
        entry["research_spawn_gate_adversarial_give_up_count"] = (
            spawn_gate_adversarial_give_up_count
        )
        if spawn_gate_adversarial_give_up_samples:
            entry["research_spawn_gate_adversarial_give_up_samples"] = (
                spawn_gate_adversarial_give_up_samples
            )

    child_tasks_stmt = select(Task).where(Task.parent_task_id == research_task_uuid)
    child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
    # Direction-scoped filtering: when any child carries a direction_id context
    # key, restrict to children attributed to this direction. Multi-direction
    # research cycles spawn children tagged with each finding's direction_id; without
    # this filter every direction's entry converges to the combined cycle totals,
    # smearing sibling merges, costs, and counts across unrelated directions. Legacy
    # single-direction cycles (no child has direction_id set) keep all children so
    # existing history entries are unaffected.
    has_tagged_children = any(
        (ct.context or {}).get("direction_id") for ct in child_tasks_rows
    )
    if has_tagged_children:
        child_tasks_rows = [
            ct
            for ct in child_tasks_rows
            if (ct.context or {}).get("direction_id") == direction_id
        ]
    if not child_tasks_rows:
        _apply_spawn_gate_parent_metrics()
        if project is not None:
            session.expire(project, ["directions"])

            _zero_child_evicted: list[bool] = [False]

            def _apply_zero_child_spawn_gate(dirs: list) -> None:
                for fresh_d in dirs:
                    if fresh_d.get("id") != direction_id:
                        continue
                    history = list(fresh_d.get("research_history", []))
                    for idx, history_entry in enumerate(history):
                        if history_entry.get("task_id") == research_task_id:
                            history[idx] = entry
                            fresh_d["research_history"] = history
                            return
                    # Entry not found in live history — check if it was evicted.
                    _agg = fresh_d.get("history_aggregate") or {}
                    if (
                        _agg.get("_evicted_cycle_snapshots") or {}
                    ).get(research_task_id):
                        _zero_child_evicted[0] = True
                    fresh_d["research_history"] = history
                    break

            await with_directions_locked(
                session, project_id, _apply_zero_child_spawn_gate
            )
            if _zero_child_evicted[0]:
                import logging as _log_zero
                _log_zero.getLogger(__name__).warning(
                    "[DIRECTION] zero-child spawn-gate patch skipped for evicted cycle "
                    "(research_task_id=%s, direction_id=%s): "
                    "spawn-gate fields have no aggregate equivalent",
                    research_task_id,
                    direction_id,
                )
            await session.commit()
        return
    # Set of child task IDs that ever opened a PR. The PULL_REQUEST Artifact is
    # the canonical "this task opened a PR" record (partial-unique per task, see
    # models.Artifact ux_artifact_task_pull_request) — it is written when the
    # agent publishes a PR and survives even after the task goes terminal, so it
    # is the most reliable opened-a-PR signal. Fetched as one batched id-only
    # query to avoid an N+1 and to avoid loading full Artifact rows. The
    # ``pr_number`` payload/context keys are layered on below as fallbacks for
    # the (rare) window where the PR exists but the artifact row was not yet
    # persisted.
    _child_ids = [ct.id for ct in child_tasks_rows]
    _pr_artifact_task_ids: set[uuid.UUID] = set(
        (
            await session.execute(
                select(Artifact.task_id).where(
                    Artifact.task_id.in_(_child_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .all()
    )

    def _opened_pr(ct: Task) -> bool:
        """True when *ct* opened a PR (via any reliable signal).

        PULL_REQUEST artifact is the authoritative signal.  The pr_number
        fallbacks below are intentionally skipped for 'review' and 'fix'
        task types: ReviewPayload.pr_number is the PR under review (not one
        opened by the task), and fix tasks
        iterate an existing PR rather than opening a new one (see
        _PR_OPENING_COMPLETION_TYPES in task_lifecycle.py).
        """
        if ct.id in _pr_artifact_task_ids:
            return True
        ct_type = (ct.first_payload or {}).get("type", "")
        if ct_type in ("review", "fix"):
            return False
        ctx_ = ct.context or {}
        if ctx_.get("pr_number") or ctx_.get("rollup_pr_number"):
            return True
        return bool((ct.first_payload or {}).get("pr_number"))

    def _associated_with_pr(ct: Task) -> bool:
        """True when *ct* is associated with a real PR — for the no-op gate ONLY.

        Unlike ``_opened_pr``, this predicate does NOT skip the pr_number
        signals for 'review' and 'fix' types.  For the
        no-CODE-no-op question, ANY association with a real pull request
        disqualifies the child: a fix task that pushed code onto an existing
        open (not-yet-landed) PR carries ``context['review_loop']['pr_number']``
        (and often ``context['pr_number']``) but no PULL_REQUEST artifact of
        its own, so ``_opened_pr`` returns False — yet it emphatically churned
        code and is NOT a "work already exists" no-op.

        Do NOT reuse this for the merge-VOLUME metrics (``merged_prs_any_base``
        and others): there the fix/review pr_number skip in
        ``_opened_pr`` is correct (a review's pr_number is the PR under review,
        not a merge that task produced).
        """
        if _opened_pr(ct):
            return True
        ctx_ = ct.context or {}
        review_loop = ctx_.get("review_loop")
        if isinstance(review_loop, dict) and review_loop.get("pr_number"):
            return True
        if ctx_.get("pr_number") or ctx_.get("rollup_pr_number"):
            return True
        return bool((ct.first_payload or {}).get("pr_number"))

    # Per-(provider, model) cost rollup using the shared pair-atomic helper so
    # both this path and the digest path stay consistent and cannot produce
    # chimera keys (e.g. openai|claude-opus-4-8 from independently-resolved
    # columns). Banked attempt_history costs are included; the live span is
    # included when it exists. Both cost axes (breakdown + total) are derived
    # from the same helper output so estimated_cost_usd == sum(breakdown).
    cost_by_provider_model: dict[str, float] = {}
    cycle_cost_total = 0.0
    for ct in child_tasks_rows:
        ct_costs = get_task_cost_by_provider_model(ct)
        for bucket_key, t_cost in ct_costs.items():
            cost_by_provider_model[bucket_key] = (
                cost_by_provider_model.get(bucket_key, 0.0) + t_cost
            )
            cycle_cost_total += t_cost

    # Include ComputeJob.final_cost_usd for compute_experiment tasks. These tasks
    # run on compute substrate rather than a VM, so their real spend lives on the
    # ComputeJob ledger while the monitoring task's vm_runtime_cost is ~$0. Without
    # this the direction's estimated_cost_usd is silently deflated, causing the
    # cost-budget auto-pause gate to never fire on compute-heavy directions.
    _child_ids = [ct.id for ct in child_tasks_rows]
    if _child_ids:
        _compute_terminal_statuses = {
            ComputeJobStatus.SUCCEEDED,
            ComputeJobStatus.FAILED,
            ComputeJobStatus.CANCELLED,
            ComputeJobStatus.TIMED_OUT,
        }
        _cj_stmt = select(ComputeJob.final_cost_usd).where(
            ComputeJob.task_id.in_(_child_ids),
            ComputeJob.final_cost_usd.is_not(None),
            ComputeJob.status.in_(_compute_terminal_statuses),
        )
        _cj_rows = (await session.execute(_cj_stmt)).scalars().all()
        for _cj_cost in _cj_rows:
            _cost = float(_cj_cost)
            if _cost > 0:
                cost_by_provider_model["compute|substrate"] = (
                    cost_by_provider_model.get("compute|substrate", 0.0) + _cost
                )
                cycle_cost_total += _cost

    if cost_by_provider_model:
        entry["cost_by_provider_model"] = {
            k: round(v, 4) for k, v in cost_by_provider_model.items()
        }
        entry["estimated_cost_usd"] = round(cycle_cost_total, 4)

    # Exclude children cancelled at spawn time by prerequisite wiring
    # (escalated-to-parent or co-deferred-shelved) — they were inserted but
    # never ran, so counting them inflates tasks_created and masks saturation.
    entry["tasks_created"] = sum(
        1
        for ct in child_tasks_rows
        if not (
            ct.status == TaskStatus.CANCELLED
            and (
                (ct.context or {}).get("prerequisite_escalated_to_parent")
                or (ct.context or {}).get(CO_DEFERRED_SHELVED)
            )
        )
    )
    entry["tasks_completed"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED
    )
    entry["tasks_failed"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED
    )
    entry["tasks_cancelled"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED
    )
    # Count CANCELLED children that resolved as SCOPE_EMPTY no-ops (work already done
    # on HEAD). Used by _detect_already_fixed_deadlock in research_direction_state.py
    # to quiesce directions stuck re-discovering already-shipped work.
    entry["scope_empty_noop_count"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.CANCELLED
        and (ct.context or {}).get("failure_category") == SCOPE_EMPTY
    )
    # BLOCKED tasks are counted separately from FAILED — they are parked on an
    # absent precondition, not failures, and the auto-unblock sweep is expected
    # to return them to QUEUED.
    # tasks_blocked retired 2026-06-05 — see RESEARCH_HISTORY_METRIC_MANIFEST.
    entry["prs_merged"] = sum(1 for ct in child_tasks_rows if is_merged_completion(ct))
    # merged_prs_any_base: children that COMPLETED, opened a real PR, and got that
    # PR MERGED — onto the integration branch OR a rollup staging branch. It is the
    # raw merge-*volume* signal: unlike prs_merged (integration-landed only, gated
    # on is_merged_completion), a child whose PR merged to a staging branch still
    # carries has_merge_unblock_signal=True (pr_merged) even before its rollup lands
    # and re-stamps landed_on_integration. No-op / no-PR success closes are excluded
    # via _opened_pr, so this never counts "no changes needed" completions as merges.
    # Consumed by the churn guardrail as a defense-in-depth backstop: a direction
    # with real merges in flight is never paused as "0 real merges" even if a rollup
    # landing's credit propagation has not yet recomputed prs_merged.
    entry["merged_prs_any_base"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and has_merge_unblock_signal(ct)
        and _opened_pr(ct)
    )
    entry["prs_rejected"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_rejected")
    )
    # tasks_completed_no_pr: children that COMPLETED correctly without producing
    # any code toward ANY pull request — genuine "no changes needed / work
    # already exists" no-CODE no-ops. These are a healthy result, NOT churn, so
    # a downstream zero-merge/churn guard must be able to subtract them from the
    # completed-but-unmerged population.
    #
    # Excludes merged completions (is_merged_completion) and pr_rejected ones,
    # AND — via ``_associated_with_pr`` rather than ``_opened_pr`` — any
    # fix/review child attached to an open (unmerged) PR.  A fix
    # task that pushed code onto an existing open PR carries a pr_number in
    # context/review_loop but no PULL_REQUEST artifact of its own, so
    # ``_opened_pr`` returns False for it; counting it here would wrongly treat
    # churned code as a no-op and under-report churn.  ``_associated_with_pr``
    # closes that gap so the count is genuine no-CODE no-ops, not merely "did
    # not open a NEW PR".
    entry["tasks_completed_no_pr"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and not is_merged_completion(ct)
        and not (ct.context or {}).get("pr_rejected")
        and not _associated_with_pr(ct)
    )
    _merged_rows = [ct for ct in child_tasks_rows if is_merged_completion(ct)]
    entry["autonomous_prs_merged"] = sum(
        1 for ct in _merged_rows if is_autonomous_merge(ct)
    )
    entry["assisted_prs_merged"] = sum(
        1 for ct in _merged_rows if not is_autonomous_merge(ct)
    )
    # verified_prs_merged: merged completions whose PR was actually verified by
    # a real test suite (not a fall-open for a degraded environment/setup reason)
    # AND whose declared acceptance evidence, if any, was satisfied. Additive
    # alongside prs_merged for back-compat; consumers that need "proven fix"
    # counts should prefer this field over the raw prs_merged.
    #
    # The shared acceptance classifier keeps the direction window and release
    # window in sync: the verified buckets (strict proven + satisfied_unproven
    # + inferred_test_cochange + undeclared) still count as verified, while
    # degraded, explicitly unsatisfied, and malformed rows stay out.
    aggregate_buckets = {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
    }
    for ct in _merged_rows:
        aggregate_buckets[classify_merged_acceptance(ct)] += 1
    entry["verified_prs_merged"] = (
        aggregate_buckets["proven"]
        + aggregate_buckets["satisfied_unproven"]
        + aggregate_buckets["inferred_test_cochange"]
        + aggregate_buckets["undeclared"]
    )
    # verified_autonomous_prs_merged: the honest basis for the retirement-
    # eligibility milestone (compute_direction_autonomy_state). A merge counts
    # here only when it is BOTH autonomous (no operator intervention) AND lands
    # in one of the same verified acceptance buckets as verified_prs_merged
    # (proven + satisfied_unproven + inferred_test_cochange + undeclared).
    # Degraded-unverified and
    # acceptance-unsatisfied autonomous merges are excluded, so by construction
    # verified_autonomous_prs_merged <= autonomous_prs_merged and
    # <= verified_prs_merged. Confirmed-stranded merges are already absent from
    # _merged_rows (is_merged_completion returns False when landed_on_integration
    # is explicitly False), so they never inflate this count on a full recompute.
    _VERIFIED_ACCEPTANCE_BUCKETS = (
        "proven",
        "satisfied_unproven",
        "inferred_test_cochange",
        "undeclared",
    )
    entry["verified_autonomous_prs_merged"] = sum(
        1
        for ct in _merged_rows
        if is_autonomous_merge(ct)
        and classify_merged_acceptance(ct) in _VERIFIED_ACCEPTANCE_BUCKETS
    )
    # A full recompute already excludes confirmed-stranded tasks from prs_merged
    # and verified_prs_merged (is_merged_completion returns False when
    # landed_on_integration is explicitly False). The delta-patch field
    # stranded_prs_merged written by discount_stranded_pr_from_direction_credit
    # is only needed in the window between stranding and the next recompute.
    # Reset it here so credit_gate_merged_count does not double-discount: once
    # because the stranded task is absent from verified_prs_merged, and again
    # because stranded_prs_merged still carries the stale delta.
    entry["stranded_prs_merged"] = 0
    # reachable_prs_merged: the ancestry-confirmed subset of prs_merged — tasks
    # whose persisted context carries landed_on_integration=True (explicit True,
    # NOT the legacy pr_merged fallback). Badge-only/indeterminate merges (key
    # absent but pr_merged=True) are NOT counted here, but they STILL count in
    # prs_merged because indeterminate ancestry must not silently drop credit.
    entry["reachable_prs_merged"] = sum(
        1
        for ct in _merged_rows
        if (ct.context or {}).get(LANDED_ON_INTEGRATION_KEY) is True
    )
    # Honest proven-by-evidence + declaration-coverage aggregates. These are
    # STRICTLY ADDITIVE: they do not change verified_prs_merged or prs_merged —
    # they expose a separate, stronger truth distinct from "merely delivered".
    # Restricted to merged WRITE tasks (the code-producing types that can carry
    # an acceptance declaration) so read-only children never dilute the coverage
    # denominator.
    #
    #   * acceptance_verified_write_merged — verified merged WRITE tasks only.
    #     This is the write-only slice used by proof_weighted_merged so read-only
    #     merged work keeps full credit instead of being tier-weighted.
    #   * acceptance_proven_merged — merges machine-checked PROVEN
    #     (acceptance_is_proven): either a satisfied named_test that ran green
    #     under a real merge-test verdict, or a satisfied downstream_artifact
    #     that was consumed (recorded downstream-artifact consumption). A soft
    #     observable_behavior merge, an unconsumed downstream_artifact, and every
    #     undeclared merge are EXCLUDED — they prove nothing about the specific
    #     claim.
    #   * acceptance_declared_merged — merges that DECLARED any acceptance
    #     evidence at plan time (acceptance_was_declared, i.e. a verdict was
    #     stamped). Undeclared / legacy merges are excluded.
    #   * acceptance_inferred_merged — undeclared merges upgraded to the
    #     inferred_test_cochange bucket: no verdict was stamped, but the PR
    #     co-changed a test file and a non-test production file under genuine CI.
    #     Disjoint from acceptance_declared_merged by construction (the classifier
    #     only infers when no verdict dict is present), so declared + inferred are
    #     non-overlapping subsets of the merged-write population.
    #   * acceptance_declaration_coverage — declared / merged-write, the fraction
    #     of merged code work that committed to forward evidence. None when no
    #     merged write task exists (no denominator), never retro-failing legacy
    #     rows.
    _merged_write_rows = [
        ct
        for ct in _merged_rows
        if (ct.first_payload or {}).get("type") in WRITE_TASK_TYPES
    ]
    entry["acceptance_verified_write_merged"] = sum(
        1
        for ct in _merged_write_rows
        if acceptance_counts_as_verified_context(ct.context or {})
    )
    entry["acceptance_proven_merged"] = sum(
        1
        for ct in _merged_write_rows
        if acceptance_is_proven((ct.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY))
    )
    entry["acceptance_consumed_merged"] = sum(
        1
        for ct in _merged_write_rows
        if acceptance_is_consumed((ct.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY))
    )
    entry["acceptance_declared_merged"] = sum(
        1
        for ct in _merged_write_rows
        if acceptance_was_declared((ct.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY))
    )
    entry["acceptance_inferred_merged"] = sum(
        1
        for ct in _merged_write_rows
        if classify_merged_acceptance_context(ct.context or {})
        == "inferred_test_cochange"
    )
    _merged_write_count = len(_merged_write_rows)
    # Persist the coverage denominator so lifetime/window coverage can be
    # recomputed as declared / merged_write_total (summing counts) rather than
    # averaging per-cycle fractions — the latter is lossy across cycles. It is
    # also the merged WRITE-task count over which acceptance_declaration_coverage
    # is taken; exposing it makes the coverage identity auditable and lets the
    # acceptance-metric invariant guard (acceptance_metric_invariant_violations)
    # pin proven ≤ declared ≤ merged-write without re-deriving it from a rounded
    # ratio.
    entry["acceptance_merged_write_total"] = _merged_write_count
    entry["acceptance_declaration_coverage"] = (
        round(entry["acceptance_declared_merged"] / _merged_write_count, 3)
        if _merged_write_count > 0
        else None
    )
    # operator_action_count: tasks in this cycle with any detectable operator intervention
    entry["operator_action_count"] = sum(
        1
        for ct in child_tasks_rows
        if (ct.created_by or "").upper() == "USER"
        or (ct.context or {}).get("review_dismissed")
        or (ct.context or {}).get("user_overrode_review")
    )
    _prev_rows = [
        ct
        for ct in child_tasks_rows
        if (ct.context or {}).get("has_preventive_context") is True
    ]
    entry["preventive_context_tasks_created"] = len(_prev_rows)
    entry["preventive_context_tasks_merged"] = sum(
        1 for ct in _prev_rows if is_merged_completion(ct)
    )
    entry["preventive_context_tasks_failed"] = sum(
        1 for ct in _prev_rows if _counts_as_failure(ct)
    )
    # Spawn-gate metrics: filter by direction_id only if children are tagged
    # (new multi-direction mode). In legacy mode (untagged children), all
    # children in child_tasks_rows already match this direction, so skip the
    # redundant filter that would exclude all untagged children.
    # ALSO exclude PR-ineligible task types (read-only tasks that can never open
    # a PR): they cannot satisfy is_merged_completion, so including them in the
    # denominator silently deflates both challenged and unchallenged merge rates
    # with never-merging children. The scope_breakdown section already applies
    # this same filter for scope conversion rates, proving the intended convention.
    if has_tagged_children:
        challenged_children = [
            ct
            for ct in child_tasks_rows
            if (ct.context or {}).get("research_spawn_gate_challenged") is True
            and (ct.context or {}).get("direction_id") == direction_id
            and (ct.first_payload or {}).get("type") not in PR_INELIGIBLE_TASK_TYPES
        ]
        unchallenged_children = [
            ct
            for ct in child_tasks_rows
            if not (ct.context or {}).get("research_spawn_gate_challenged")
            and (ct.context or {}).get("direction_id") == direction_id
            and (ct.first_payload or {}).get("type") not in PR_INELIGIBLE_TASK_TYPES
        ]
    else:
        challenged_children = [
            ct
            for ct in child_tasks_rows
            if (ct.context or {}).get("research_spawn_gate_challenged") is True
            and (ct.first_payload or {}).get("type") not in PR_INELIGIBLE_TASK_TYPES
        ]
        unchallenged_children = [
            ct
            for ct in child_tasks_rows
            if not (ct.context or {}).get("research_spawn_gate_challenged")
            and (ct.first_payload or {}).get("type") not in PR_INELIGIBLE_TASK_TYPES
        ]
    challenged_spawned = len(challenged_children)
    unchallenged_spawned = len(unchallenged_children)
    challenged_merged = sum(1 for ct in challenged_children if is_merged_completion(ct))
    unchallenged_merged = sum(
        1 for ct in unchallenged_children if is_merged_completion(ct)
    )
    _apply_spawn_gate_parent_metrics()
    entry["research_spawn_gate_challenged_spawned"] = challenged_spawned
    entry["research_spawn_gate_challenged_merged"] = challenged_merged
    entry["research_spawn_gate_challenged_merge_rate"] = (
        round(challenged_merged / challenged_spawned, 3)
        if challenged_spawned > 0
        else None
    )
    entry["research_spawn_gate_unchallenged_spawned"] = unchallenged_spawned
    entry["research_spawn_gate_unchallenged_merged"] = unchallenged_merged
    entry["research_spawn_gate_unchallenged_merge_rate"] = (
        round(unchallenged_merged / unchallenged_spawned, 3)
        if unchallenged_spawned > 0
        else None
    )
    # remediation_tasks_{created,merged,failed} retired 2026-06-05 —
    # see RESEARCH_HISTORY_METRIC_MANIFEST.
    # Collect changed files from merged PRs for co-change index update.
    # Build parallel (key, files) pairs so each PR is folded in at most once.
    _merged_cochange_entries = [
        (str(ct.id), (ct.context or {}).get("_pr_changed_files", []))
        for ct in child_tasks_rows
        if is_merged_completion(ct) and (ct.context or {}).get("_pr_changed_files")
    ]
    merged_files_lists = [files for _, files in _merged_cochange_entries]
    merged_pr_keys = [key for key, _ in _merged_cochange_entries]
    # Generalized outcome aggregates — ADDITIVE alongside existing PR metrics.
    # Uses evaluate_task_outcome to count successes across ALL task types (code
    # merges + research quality + audit findings + memo artifacts + verification
    # verdicts), not just PR-merging tasks. This lets directions whose work is
    # primarily read-only (research, audit, memo) appear as positive-health
    # rather than zero-output in analytics and the strategic cycle.
    # Lazy import avoids the direction_outcomes → task_outcomes → direction_outcomes
    # circular dependency at module load time.
    from app.task_outcomes import evaluate_task_outcome  # noqa: PLC0415

    _outcome_kind_counts: dict[str, dict[str, int]] = {}
    _generalized_success_count = 0
    for _ct in child_tasks_rows:
        if _ct.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        # Non-genuine cancellations (cascade kills, superseded, cap-exhausted-after-approve, etc.)
        # must not count against direction health per the _is_non_genuine_cancelled contract.
        if _is_non_genuine_cancelled(_ct):
            continue
        _task_outcome = evaluate_task_outcome(_ct)
        _kind = _task_outcome.kind
        if _kind not in _outcome_kind_counts:
            _outcome_kind_counts[_kind] = {"success": 0, "neutral": 0, "failure": 0}
        _outcome_kind_counts[_kind][_task_outcome.status] += 1
        if _task_outcome.status == "success":
            _generalized_success_count += 1
    entry["generalized_successes"] = _generalized_success_count
    _all_terminal_ct = sum(
        _v for _d in _outcome_kind_counts.values() for _v in _d.values()
    )
    entry["generalized_health_rate"] = (
        round(_generalized_success_count / _all_terminal_ct, 3)
        if _all_terminal_ct > 0
        else None
    )
    if _outcome_kind_counts:
        entry["outcome_kind_breakdown"] = _outcome_kind_counts
    # Structured-output format-compliance for this cycle's child tasks. Reuses
    # the same windowed aggregator the digest measure uses (the extraction
    # layer's own parse verdict is ground truth, so neither rate is gameable).
    # Tasks with a NULL format_compliance_summary are skipped. None when no
    # child task carried the telemetry.
    from app.digest_metrics import (  # noqa: PLC0415
        compute_format_compliance_metrics,
    )

    _fc_overall = compute_format_compliance_metrics(child_tasks_rows)["overall"]
    entry["format_compliance_rate"] = _fc_overall["format_compliance_rate"]
    entry["self_repair_success_rate"] = _fc_overall["self_repair_success_rate"]
    # Review quality signals: track how users respond to Blueprint's reviews
    entry["reviews_dismissed"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and (ct.context or {}).get("review_dismissed")
    )
    entry["reviews_overridden"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and (ct.context or {}).get("user_overrode_review")
    )
    entry["user_issues_caught"] = sum(
        1 for ct in child_tasks_rows if (ct.context or {}).get("user_caught_issues")
    )
    # avg_time_to_merge_hours: mean of (vm_stopped_at - created_at) for merged tasks.
    # created_at is TIMESTAMPTZ since 0061; vm_stopped_at is naive — normalize both.
    from app.time_utils import _to_naive_utc

    merge_durations = [
        (_to_naive_utc(ct.vm_stopped_at) - _to_naive_utc(ct.created_at)).total_seconds()
        / 3600
        for ct in child_tasks_rows
        if is_merged_completion(ct)
        and ct.vm_stopped_at is not None
        and ct.created_at is not None
    ]
    entry["avg_time_to_merge_hours"] = (
        round(sum(merge_durations) / len(merge_durations), 2)
        if merge_durations
        else None
    )
    # avg_implementation_hours, avg_review_hours retired 2026-06-05 —
    # see RESEARCH_HISTORY_METRIC_MANIFEST.
    # Review iteration counts per child task — lets research agents see
    # prompt-quality outcomes (fewer iterations = clearer prompt).
    review_iters = [
        int((ct.context or {}).get("review_loop", {}).get("iteration"))
        for ct in child_tasks_rows
        if ct.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and isinstance((ct.context or {}).get("review_loop"), dict)
        and isinstance((ct.context or {}).get("review_loop", {}).get("iteration"), int)
    ]
    entry["median_review_iterations"] = (
        round(statistics.median(review_iters), 2) if review_iters else None
    )
    entry["avg_review_iterations"] = (
        round(sum(review_iters) / len(review_iters), 2) if review_iters else None
    )
    # Capture failed task details so research agents can see WHY tasks failed
    failed_details = []
    infra_failures = 0
    taxonomy_summary = {
        TAXONOMY_SCOPE_RELATED: 0,
        TAXONOMY_REVIEW_QUALITY: 0,
        TAXONOMY_INFRA_RELATED: 0,
        TAXONOMY_CODE_DEFECT: 0,
    }
    for ct in child_tasks_rows:
        if _counts_as_failure(ct):
            reason = (ct.latest_status_message or "")[:120]
            category = (ct.context or {}).get("failure_category", "")
            taxonomy = _classify_taxonomy(ct)
            taxonomy_summary[taxonomy] = taxonomy_summary.get(taxonomy, 0) + 1
            effort_attempted = (ct.first_payload or {}).get("effort")
            scope_calibrated_from = (ct.context or {}).get("scope_calibrated_from")
            failed_details.append(
                {
                    "title": ct.title or "",
                    "reason": reason,
                    "category": category,
                    "taxonomy": taxonomy,
                    "effort_attempted": effort_attempted,
                    "scope_calibrated_from": scope_calibrated_from,
                }
            )
            effective_cat = category or _classify_failure(ct)
            if effective_cat in INFRA_CLASSES and not _is_review_cap_exhausted(ct):
                infra_failures += 1
    entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat
    entry["tasks_failed_infra"] = infra_failures
    entry["taxonomy_summary"] = taxonomy_summary

    # Capture affirmative-no-op task details (mirror of failed_task_details) so
    # the research layer can tell "we already tried this and the defect was gone"
    # from "this is new and unattempted". A child that COMPLETED with a verified
    # ``no_op_reason`` (the agent ran ``./done --already-done`` because the cited
    # defect was absent / already resolved on HEAD) is scored neutral — neither a
    # merged success nor a failure. We record title + the (truncated) reason so
    # the prior-outcomes retagger can mark the originating finding '(stale)'.
    # Merged completions are excluded so a finding that also shipped a verified
    # fix keeps its merged tag, not a stale one.
    no_op_details = []
    for ct in child_tasks_rows:
        if ct.status != TaskStatus.COMPLETED:
            continue
        no_op_reason = (ct.context or {}).get("no_op_reason")
        if not no_op_reason or is_merged_completion(ct):
            continue
        no_op_details.append(
            {
                "title": ct.title or "",
                "no_op_reason": str(no_op_reason)[:120],
            }
        )
    entry["no_op_task_details"] = no_op_details[:5]  # cap at 5 to avoid bloat

    # Capture delivered-but-unproven merge details (mirror of the two lists
    # above). When a merged WRITE task DECLARED acceptance evidence at plan time
    # but that evidence was UNSATISFIED at merge, the acceptance gate records a
    # ``code_acceptance_verdict`` with satisfied=False and the per-task scorer
    # downgrades the merge to NEUTRAL (see acceptance_gate.acceptance_blocks_success
    # and task_outcomes._evaluate_write). Such a merge is neither a failure
    # (excluded from failed_task_details) nor a no-op (excluded from
    # no_op_task_details), so without this list the next research cycle would see
    # the originating finding as shipped and stop chasing it — even though its own
    # declared evidence did not hold. We record title + the (truncated) verdict
    # ``reason`` so the prior-outcomes retagger can mark the finding '(unproven)'
    # and research re-targets it instead of treating it as resolved.
    unproven_merge_details = []
    for ct in _merged_write_rows:
        verdict = (ct.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
        if not acceptance_blocks_success(verdict):
            continue
        reason = str((verdict or {}).get("reason") or "")[:120]
        unproven_merge_details.append(
            {
                "title": ct.title or "",
                "reason": reason,
            }
        )
    entry["unproven_merge_details"] = unproven_merge_details[:5]  # cap at 5

    # Stage dropout: per-cycle breakdown of why tasks didn't reach main
    sd_code_error = 0
    sd_infra = 0
    sd_code_error_ids: set = set()
    for ct in child_tasks_rows:
        if _counts_as_failure(ct):
            fc = _classify_failure(ct)
            if _is_review_cap_exhausted(ct):
                sd_code_error += 1
                sd_code_error_ids.add(ct.id)
            elif fc == "CODE_ERROR":
                sd_code_error += 1
                sd_code_error_ids.add(ct.id)
            elif fc in INFRA_CLASSES:
                sd_infra += 1
    # tasks_required_approval: non-completed tasks that required manual approval
    # Exclude tasks already counted as CODE_ERROR to avoid double-counting
    sd_required_approval = sum(
        1
        for ct in child_tasks_rows
        if ct.requires_approval
        and ct.status not in (TaskStatus.COMPLETED,)
        and ct.id not in sd_code_error_ids
    )
    research_task_obj = await session.get(Task, research_task_uuid)
    rollup_failed = (
        research_task_obj is not None and research_task_obj.status == TaskStatus.FAILED
    )
    sd_staging_only = sum(
        1
        for ct in child_tasks_rows
        if rollup_failed
        and ct.status == TaskStatus.COMPLETED
        and has_merge_unblock_signal(ct)
        and not is_merged_completion(ct)
    )
    entry["stage_dropout"] = {
        "tasks_failed_code_error": sd_code_error,
        "tasks_failed_infra": sd_infra,
        "tasks_required_approval": sd_required_approval,
        "tasks_merged_staging_only": sd_staging_only,
    }

    # Classify child tasks as product vs structural. Membership is derived from
    # the task-type registry (task_capabilities.PRODUCT_TASK_TYPES) — the single
    # source of truth — so value-shipping types (create, fix, migrate, ...) count
    # as product and overhead/read-only types as structural. Unknown/future types
    # not in the registry fall through to structural by the conservative default.
    type_counts: dict = {}
    type_breakdown: dict = {}
    product_count = 0
    structural_count = 0
    for ct in child_tasks_rows:
        task_type = (ct.first_payload or {}).get("type", "unknown")
        type_counts[task_type] = type_counts.get(task_type, 0) + 1
        if task_type not in type_breakdown:
            type_breakdown[task_type] = {"created": 0, "merged": 0, "failed": 0}
        type_breakdown[task_type]["created"] += 1
        if is_merged_completion(ct):
            type_breakdown[task_type]["merged"] += 1
        if _counts_as_failure(ct):
            type_breakdown[task_type]["failed"] += 1
        if task_type in PRODUCT_TASK_TYPES:
            product_count += 1
        else:
            structural_count += 1
    entry["type_distribution"] = type_counts
    entry["type_breakdown"] = type_breakdown
    total_typed = product_count + structural_count
    entry["product_ratio"] = (
        round(product_count / total_typed, 2) if total_typed > 0 else 0.0
    )
    # structural_count retired 2026-06-05 — see RESEARCH_HISTORY_METRIC_MANIFEST.

    # Track per-scope merge rates so research agents can calibrate scope assignments.
    # calibrated_in: tasks calibrated INTO this scope from a larger one (counted at the
    # effective scope). calibrated_out: tasks originally proposed at this scope but
    # calibrated to a smaller one (counted at the original scope).
    scope_breakdown: dict = {}
    escalation_outcomes: dict = {}
    SCOPE_RANK = {"small": 0, "medium": 1, "large": 2}
    expansion_sums: dict = {}
    expansion_counts: dict = {}
    expansion_merged_sums: dict = {}
    expansion_merged_counts: dict = {}
    expansion_failed_sums: dict = {}
    expansion_failed_counts: dict = {}
    cplx_merged_sum: dict = {}
    cplx_merged_n: dict = {}
    cplx_failed_sum: dict = {}
    cplx_failed_n: dict = {}

    def _ensure_scope(s: str) -> None:
        if s not in scope_breakdown:
            scope_breakdown[s] = {
                "created": 0,
                "merged": 0,
                "merged_cross_module": 0,
                "failed": 0,
                "failed_infra": 0,
                "failed_code_error": 0,
                "failed_scope_overflow": 0,
                "failed_analysis_overflow": 0,
                "scope_trim_count": 0,
                "calibrated_in": 0,
                "calibrated_out": 0,
                "exploration_created": 0,
                "exploration_merged": 0,
                "exploration_failed": 0,
                "failure_stages": {},
            }

    for ct in child_tasks_rows:
        ctx_ = ct.context or {}
        # Skip types that can never open a PR — they have no conversion-rate
        # denominator role in scope_breakdown (they complete with knowledge docs
        # or compute jobs, not merged PRs). type_breakdown is built separately
        # above and is unaffected; the raw tasks_created counter (used for
        # saturation detection) is also unaffected.
        if (ct.first_payload or {}).get("type") in PR_INELIGIBLE_TASK_TYPES:
            continue
        scope = ctx_.get("scope", "unknown")
        _ensure_scope(scope)
        scope_breakdown[scope]["created"] += 1
        effort_level = (ct.first_payload or {}).get("effort")
        if effort_level:
            effort_dist = scope_breakdown[scope].setdefault("effort_distribution", {})
            effort_bucket = effort_dist.setdefault(
                effort_level, {"created": 0, "merged": 0, "failed": 0}
            )
            effort_bucket["created"] += 1
            if is_merged_completion(ct):
                effort_bucket["merged"] += 1
            if _counts_as_failure(ct):
                effort_bucket["failed"] += 1
        if is_merged_completion(ct):
            scope_breakdown[scope]["merged"] += 1
            changed_files_for_span = ctx_.get("_pr_changed_files") or []
            top_dirs = {
                f.split("/", 1)[0]
                for f in changed_files_for_span
                if isinstance(f, str) and "/" in f
            }
            if len(top_dirs) >= 2:
                scope_breakdown[scope]["merged_cross_module"] = (
                    scope_breakdown[scope].get("merged_cross_module", 0) + 1
                )
        if _counts_as_failure(ct):
            scope_breakdown[scope]["failed"] += 1
            fcat = ctx_.get("failure_category") or _classify_failure(ct)
            if fcat in INFRA_CLASSES and not _is_review_cap_exhausted(ct):
                scope_breakdown[scope]["failed_infra"] += 1
            if fcat == CODE_ERROR:
                scope_breakdown[scope]["failed_code_error"] += 1
            elif fcat == SCOPE_OVERFLOW:
                scope_breakdown[scope]["failed_scope_overflow"] += 1
            if ctx_.get("scope_gate_triggered") == "analysis_overflow":
                scope_breakdown[scope]["failed_analysis_overflow"] += 1
            stage_name = (
                ctx_.get("_previous_failure_stage")
                or _derive_failure_stage(fcat, ct.latest_status_message or "")
                or "unknown"
            )
            stages_bucket = scope_breakdown[scope]["failure_stages"]
            stages_bucket[stage_name] = stages_bucket.get(stage_name, 0) + 1
        if ctx_.get("scope_gate_triggered") == "scope_trim" and (
            ct.status == TaskStatus.COMPLETED or _counts_as_failure(ct)
        ):
            scope_breakdown[scope]["scope_trim_count"] = (
                scope_breakdown[scope].get("scope_trim_count", 0) + 1
            )
        if ctx_.get("medium_exploration"):
            scope_breakdown[scope]["exploration_created"] += 1
            if is_merged_completion(ct):
                scope_breakdown[scope]["exploration_merged"] += 1
            if _counts_as_failure(ct):
                scope_breakdown[scope]["exploration_failed"] += 1
        calibrated_from = ctx_.get("scope_calibrated_from")
        if calibrated_from:
            scope_breakdown[scope]["calibrated_in"] += 1
            _ensure_scope(calibrated_from)
            scope_breakdown[calibrated_from]["calibrated_out"] += 1
            # Track escalations (original smaller -> effective larger) so we can
            # measure whether escalation actually resolves the task or just burns
            # compute at a larger scope.
            if (
                calibrated_from in SCOPE_RANK
                and scope in SCOPE_RANK
                and SCOPE_RANK[scope] > SCOPE_RANK[calibrated_from]
            ):
                key = f"{calibrated_from}_to_{scope}"
                bucket = escalation_outcomes.setdefault(
                    key, {"attempted": 0, "merged": 0, "failed": 0}
                )
                bucket["attempted"] += 1
                if is_merged_completion(ct):
                    bucket["merged"] += 1
                if _counts_as_failure(ct):
                    bucket["failed"] += 1
        # File expansion ratio: actual files touched / expected files.
        # Leading indicator of scope overflow risk.
        related = ctx_.get("related_files") or []
        changed = ctx_.get("_pr_changed_files") or []
        # Exclude test files: related_files typically declares production files
        # only, so including test files in the numerator systematically inflates
        # the ratio for tasks that ship proper test coverage.
        # Deferred import: review_pipeline -> spawn_calibration -> direction_metrics
        # imports verified_merged_count back out of this module, so importing
        # review_pipeline at module load creates a circular import.
        from app.review_pipeline import _is_test_file

        prod_changed = [f for f in changed if not _is_test_file(f)]
        if related and prod_changed:
            ratio = len(prod_changed) / len(related)
            if is_merged_completion(ct):
                expansion_merged_sums[scope] = (
                    expansion_merged_sums.get(scope, 0.0) + ratio
                )
                expansion_merged_counts[scope] = (
                    expansion_merged_counts.get(scope, 0) + 1
                )
            if ct.status == TaskStatus.COMPLETED:
                expansion_sums[scope] = expansion_sums.get(scope, 0.0) + ratio
                expansion_counts[scope] = expansion_counts.get(scope, 0) + 1
            elif _counts_as_failure(ct):
                # Failed tasks (especially SCOPE_OVERFLOW) are the exact scenario
                # the expansion predictor tries to prevent — track separately so
                # scope calibration has a conservative signal.
                expansion_failed_sums[scope] = (
                    expansion_failed_sums.get(scope, 0.0) + ratio
                )
                expansion_failed_counts[scope] = (
                    expansion_failed_counts.get(scope, 0) + 1
                )
    for scope_key, count in expansion_counts.items():
        if count > 0:
            scope_breakdown[scope_key]["avg_expansion_ratio"] = round(
                expansion_sums[scope_key] / count, 2
            )
    for scope_key, count in expansion_merged_counts.items():
        if count > 0:
            scope_breakdown[scope_key]["avg_expansion_ratio_merged"] = round(
                expansion_merged_sums[scope_key] / count, 2
            )
    for scope_key, count in expansion_failed_counts.items():
        if count > 0:
            _ensure_scope(scope_key)
            scope_breakdown[scope_key]["avg_expansion_ratio_failed"] = round(
                expansion_failed_sums[scope_key] / count, 2
            )
    # Predictive accuracy: average complexity_score for merged vs failed tasks.
    # If scoring is calibrated, merged > failed at each scope.
    for ct in child_tasks_rows:
        ctx2 = ct.context or {}
        cs = ctx2.get("complexity_score")
        if not isinstance(cs, (int, float)):
            continue
        scope2 = ctx2.get("scope", "unknown")
        if is_merged_completion(ct):
            cplx_merged_sum[scope2] = cplx_merged_sum.get(scope2, 0.0) + float(cs)
            cplx_merged_n[scope2] = cplx_merged_n.get(scope2, 0) + 1
        elif _counts_as_failure(ct):
            cplx_failed_sum[scope2] = cplx_failed_sum.get(scope2, 0.0) + float(cs)
            cplx_failed_n[scope2] = cplx_failed_n.get(scope2, 0) + 1
    for sk, n in cplx_merged_n.items():
        if n > 0 and sk in scope_breakdown:
            scope_breakdown[sk]["avg_complexity_merged"] = round(
                cplx_merged_sum[sk] / n, 2
            )
    for sk, n in cplx_failed_n.items():
        if n > 0 and sk in scope_breakdown:
            scope_breakdown[sk]["avg_complexity_failed"] = round(
                cplx_failed_sum[sk] / n, 2
            )
    skip_counts = (
        (research_task_obj.context or {}).get("skipped_low_confidence_by_scope", {})
        if research_task_obj
        else {}
    )
    for sk, cnt in (skip_counts or {}).items():
        _ensure_scope(sk)
        scope_breakdown[sk]["skipped_low_confidence"] = scope_breakdown[sk].get(
            "skipped_low_confidence", 0
        ) + int(cnt)
    entry["scope_breakdown"] = scope_breakdown
    entry["escalation_outcomes"] = escalation_outcomes

    # Prompt-characteristic aggregation: averages per merged/failed bucket so
    # direction-level analysis can correlate prompt shape with outcomes.
    pt_buckets: dict = {"merged": [], "failed": []}
    for ct in child_tasks_rows:
        pt = (ct.context or {}).get("prompt_telemetry")
        if not isinstance(pt, dict):
            continue
        scope_val = (ct.context or {}).get("scope")
        if is_merged_completion(ct):
            pt_buckets["merged"].append(pt)
            if scope_val in ("small", "medium"):
                pt_buckets.setdefault(f"merged_{scope_val}", []).append(pt)
        elif _counts_as_failure(ct):
            pt_buckets["failed"].append(pt)
            if scope_val in ("small", "medium"):
                pt_buckets.setdefault(f"failed_{scope_val}", []).append(pt)
    prompt_telemetry_summary: dict = {}
    for bucket_name, items in pt_buckets.items():
        if not items:
            continue
        # Scope-segmented sub-buckets require 2+ entries to avoid noise.
        if bucket_name not in ("merged", "failed") and len(items) < 2:
            continue
        n = len(items)

        def _avg(key: str):
            vals = [i.get(key) for i in items if isinstance(i.get(key), (int, float))]
            return round(sum(vals) / len(vals), 2) if vals else None

        def _frac(key: str) -> float:
            return round(sum(1 for i in items if i.get(key)) / n, 3)

        prompt_telemetry_summary[bucket_name] = {
            "count": n,
            "avg_char_count": _avg("char_count"),
            "avg_related_file_count": _avg("related_file_count"),
            "avg_files_to_budget_ratio": _avg("files_to_budget_ratio"),
            "frac_mentions_tests": _frac("mentions_tests"),
            "frac_mentions_symbols": _frac("mentions_symbols"),
        }
    if prompt_telemetry_summary:
        entry["prompt_telemetry"] = prompt_telemetry_summary

    # Decomposition quality: per plan-parent metrics so future calibration
    # can correlate decomposition shape (breadth, file overlap, cross-module
    # coupling, dep depth) with subtask merge success.
    #
    # Affirmative-no-op plans (those carrying a verified ``no_op_reason`` — the
    # #7021 principle applied to plan in plan_completion.py) are excluded: a
    # plan that legitimately had nothing to decompose must NOT be scored as a
    # decomposition failure (subtask_merge_rate=0.0), which would penalize the
    # direction for correctly finding the work already done. These land
    # CANCELLED today (already outside the COMPLETED/FAILED filter), but the
    # no_op_reason guard makes the exclusion explicit and robust to the terminal
    # status changing.
    plan_child_ids = [
        ct.id
        for ct in child_tasks_rows
        if (ct.first_payload or {}).get("type") == "plan"
        and ct.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and not (ct.context or {}).get("no_op_reason")
    ]
    decomposition_quality: list = []
    if plan_child_ids:
        subtask_rows = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id.in_(plan_child_ids))
                )
            )
            .scalars()
            .all()
        )
        by_parent: dict = {}
        for st in subtask_rows:
            by_parent.setdefault(st.parent_task_id, []).append(st)
        for plan_id, sts in by_parent.items():
            merged_ct = sum(1 for st in sts if is_merged_completion(st))
            # SCOPE_OVERFLOW stays in the denominator even though it lives in
            # INFRA_CLASSES: it is a scope-quality signal (subtasks cut too
            # large), which is exactly what subtask_merge_rate exists to detect.
            # Mirrors spawn_calibration._effective_scope_denominator, which keeps
            # SCOPE_OVERFLOW and excludes only non-overflow infra fails.
            failed_nc = sum(
                1
                for st in sts
                if _counts_as_failure(st)
                and (
                    (fc := _classify_failure(st)) not in INFRA_CLASSES
                    or fc == SCOPE_OVERFLOW
                    or _is_review_cap_exhausted(st)
                )
            )
            file_counts = [
                len((st.context or {}).get("_pr_changed_files") or [])
                for st in sts
                if (st.context or {}).get("_pr_changed_files")
            ]
            cross_mod = sum(
                1
                for st in sts
                if len(
                    {
                        f.split("/", 1)[0]
                        for f in ((st.context or {}).get("_pr_changed_files") or [])
                        if "/" in f
                    }
                )
                >= 2
            )
            deps_map = {str(st.id): list(st.depends_on or []) for st in sts}
            denom = merged_ct + failed_nc
            decomposition_quality.append(
                {
                    "plan_task_id": str(plan_id),
                    "subtask_count": len(sts),
                    "subtask_merge_rate": round(merged_ct / denom, 3)
                    if denom > 0
                    else None,
                    "avg_files_per_subtask": round(
                        sum(file_counts) / len(file_counts), 2
                    )
                    if file_counts
                    else 0.0,
                    "max_files_in_subtask": max(file_counts) if file_counts else 0,
                    "dependency_depth": _longest_chain_length(deps_map),
                    "cross_module_count": cross_mod,
                }
            )
        # Sentinel entries for plans that reached terminal state without producing
        # any subtasks — otherwise these decomposition failures leave no data in
        # decomposition_quality and _poor_decomp_history stays blind to them.
        for plan_id in plan_child_ids:
            if plan_id in by_parent:
                continue
            decomposition_quality.append(
                {
                    "plan_task_id": str(plan_id),
                    "subtask_count": 0,
                    "subtask_merge_rate": 0.0,
                    "avg_files_per_subtask": 0.0,
                    "max_files_in_subtask": 0,
                    "dependency_depth": 0,
                    "cross_module_count": 0,
                }
            )
    entry["decomposition_quality"] = decomposition_quality

    # Co-change auto-decomposition outcomes: track merge rate of
    # sub-findings produced by auto_decompose_findings_by_cochange so
    # spawn_calibration can factor in whether splitting medium findings
    # into smaller ones actually improves conversion.
    _decomp_stats = (
        ((research_task_obj.context or {}) if research_task_obj else {}).get(
            "cochange_decomposition_stats"
        )
        or {}
    ).get(direction_id)
    if _decomp_stats:
        _decomposed = [
            ct for ct in child_tasks_rows if (ct.context or {}).get("auto_decomposed")
        ]
        _sub_merged = sum(1 for ct in _decomposed if is_merged_completion(ct))
        _sub_total = len(_decomposed)
        entry["cochange_decomposition"] = {
            "medium_findings_split": _decomp_stats.get("medium_findings_split", 0),
            "sub_findings_produced": _decomp_stats.get("sub_findings_produced", 0),
            "sub_findings_merged": _sub_merged,
            "sub_findings_total": _sub_total,
            "merge_rate": round(_sub_merged / _sub_total, 3)
            if _sub_total > 0
            else None,
        }

    # Maintain research_context: promote proposed findings based on outcomes
    new_context: str | None = None
    ctx = d.get("research_context", "")
    if ctx:
        parsed = _parse_research_context(ctx)
        for ct in child_tasks_rows:
            title = ct.title or ""
            if not title:
                continue
            if _counts_as_failure(ct):
                # Move matching proposed finding to Failed Approaches
                reason = (ct.latest_status_message or "")[:120]
                category = (ct.context or {}).get(
                    "failure_category"
                ) or _classify_failure(ct)
                ts = ct.vm_stopped_at or ct.updated_at
                failed_at_iso = ts.date().isoformat() if ts else ""
                stage = (
                    (ct.context or {}).get("_previous_failure_stage")
                    or _derive_failure_stage(category, ct.latest_status_message or "")
                    or ""
                )
                meta_parts = []
                if failed_at_iso:
                    meta_parts.append(f"at={failed_at_iso}")
                if stage:
                    meta_parts.append(f"stage={stage}")
                head = f"{title} ({category})"
                if meta_parts:
                    head = f"{head} [{' '.join(meta_parts)}]"
                new_established = []
                moved = False
                for item in parsed["established"]:
                    if title.lower() in item.lower():
                        parsed["failed"].append(
                            f"{head} — {reason}" if reason else head
                        )
                        moved = True
                    else:
                        new_established.append(item)
                if moved:
                    parsed["established"] = new_established
            elif is_merged_completion(ct):
                # Promote (proposed) finding. A merge whose DECLARED acceptance
                # evidence was UNSATISFIED (acceptance_blocks_success) shipped a
                # fix whose own claim did not hold — tag it '(unproven)' rather
                # than '(confirmed)' so this cycle's context already reflects that
                # research should re-target it, not treat it as resolved. Verified
                # / undeclared / legacy merges still promote to '(confirmed)'.
                promoted_tag = (
                    "unproven"
                    if acceptance_blocks_success(
                        (ct.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
                    )
                    else "confirmed"
                )
                new_established = []
                for item in parsed["established"]:
                    if title.lower() in item.lower() and "(proposed)" in item:
                        new_established.append(
                            item.replace("(proposed)", f"({promoted_tag})")
                        )
                    else:
                        new_established.append(item)
                parsed["established"] = new_established
            elif (ct.context or {}).get("no_op_reason"):
                # Affirmative no-op (./done --already-done): the cited defect was
                # absent / already resolved on HEAD. Retag the matching proposed
                # finding to (stale) so this cycle's context already reflects that
                # the area was clean and the research agent won't re-chase it.
                new_established = []
                for item in parsed["established"]:
                    if title.lower() in item.lower() and "(proposed)" in item:
                        new_established.append(item.replace("(proposed)", "(stale)"))
                    else:
                        new_established.append(item)
                parsed["established"] = new_established

        # Reassemble the document (preserve eviction summary if present)
        parts: list = []
        if parsed.get("eviction_summary"):
            parts.append("## Eviction Summary")
            parts.append(parsed["eviction_summary"])
            parts.append("")
        for header, key in [
            ("## Established Findings", "established"),
            ("## Failed Approaches", "failed"),
            ("## Open Questions", "open_questions"),
        ]:
            parts.append(header)
            for item in parsed[key]:
                parts.append(f"- {item}")
            parts.append("")
        updated_ctx = "\n".join(parts).strip() + "\n"
        new_context = updated_ctx[:8000]

    # Serialize the directions write via row-lock so two concurrent research
    # completions for different directions of the same project don't clobber
    # each other's research_history entries via stale whole-array overwrites.
    session.expire(project, ["directions"])

    # Aggregate-delta fields: (entry_key, aggregate_key) pairs that
    # _fold_evicted_into_aggregate accumulates. Used by _apply_direction_outcomes
    # to apply incremental deltas when the live entry was evicted.
    _DELTA_FIELDS = (
        ("prs_merged", "total_prs_merged"),
        ("verified_prs_merged", "total_verified_prs_merged"),
        ("autonomous_prs_merged", "total_autonomous_prs_merged"),
        ("verified_autonomous_prs_merged", "total_verified_autonomous_prs_merged"),
        ("assisted_prs_merged", "total_assisted_prs_merged"),
        ("reachable_prs_merged", "total_reachable_prs_merged"),
        ("stranded_prs_merged", "total_stranded_prs_merged"),
        ("acceptance_proven_merged", "total_acceptance_proven_merged"),
        ("acceptance_consumed_merged", "total_acceptance_consumed_merged"),
        ("acceptance_declared_merged", "total_acceptance_declared_merged"),
        ("acceptance_inferred_merged", "total_acceptance_inferred_merged"),
        ("acceptance_merged_write_total", "total_acceptance_merged_write_total"),
    )

    def _apply_direction_outcomes(dirs: list) -> None:
        target_d = next((x for x in dirs if x.get("id") == direction_id), None)
        if target_d is None:
            return
        target_entry = next(
            (
                e
                for e in target_d.get("research_history", [])
                if e.get("task_id") == research_task_id
            ),
            None,
        )
        if target_entry is None:
            if _evicted_snap is None:
                return
            # Cycle was evicted — apply incremental merge-count deltas to history_aggregate
            # so late merges and stranded corrections are not permanently lost.
            _agg = target_d.get("history_aggregate") or {}
            _snaps = _agg.get("_evicted_cycle_snapshots") or {}
            _snap = _snaps.get(research_task_id)
            if _snap is None:
                return
            for _entry_key, _agg_key in _DELTA_FIELDS:
                _new_val = int(entry.get(_entry_key) or 0)
                _snap_val = int(_snap.get(_entry_key) or 0)
                _delta = _new_val - _snap_val
                if _delta != 0:
                    _agg[_agg_key] = int(_agg.get(_agg_key) or 0) + _delta
            # ALSO heal the acceptance-verified + generalized aggregate keys that the
            # fold credits but the primitive _DELTA_FIELDS list omits — otherwise a
            # late merge on an evicted cycle leaves total_acceptance_verified_merged,
            # total_acceptance_verified_write_merged, total_generalized_successes,
            # total_generalized_terminal_outcomes, and outcome_kind_breakdown frozen
            # at their eviction-time values, silently diverging the proof-weighted
            # scoring / digest / strategist surfaces from ground truth.
            _apply_evicted_generalized_and_acceptance_deltas(_agg, _snap, entry)
            # Update snapshot to the new baseline (all keys, via the shared builder so
            # it stays byte-identical to the two snapshot-writing sites) for
            # idempotency on future recomputes.
            from app.direction_metrics import (  # noqa: PLC0415
                build_evicted_cycle_snapshot,
            )

            _snaps[research_task_id] = build_evicted_cycle_snapshot(entry)
            _agg["_evicted_cycle_snapshots"] = _snaps
            target_d["history_aggregate"] = _agg
            return
        target_entry.update(entry)
        if new_context is not None:
            target_d["research_context"] = new_context

    # Both the directions write and the design_doc cochange-index write target
    # the same project row and hold its FOR NO KEY UPDATE lock until the session
    # commits, so we issue them under a SINGLE commit. This preserves the
    # original single-transaction atomicity (a crash can't persist the
    # directions write without the cochange-index write) while keeping the
    # lost-update protection of the row lock.
    await with_directions_locked(session, project_id, _apply_direction_outcomes)

    if merged_files_lists:
        # Flush the directions mutation before reacquiring the same Project row
        # for the design_doc/cochange write. Without this, the second
        # populate_existing refresh can reload a pre-mutation directions blob and
        # clobber the newly-added research_history metrics in the same session.
        await session.flush()
        session.expire(project, ["design_doc"])
        await with_design_doc_locked(
            session,
            project_id,
            lambda doc: _apply_cochange_to_doc(
                doc, merged_files_lists, pr_keys=merged_pr_keys
            ),
        )

    await session.commit()

    await session.refresh(project)
    _refresh_pipeline_scope_merge_rates(project)


async def refresh_direction_outcomes_after_merge(
    task: Task, session: AsyncSession
) -> None:
    """Re-run the research-history outcome recompute after a task's PR merges.

    The terminal-status path (``task_lifecycle``) computes a research cycle's
    ``prs_merged`` when each child task reaches COMPLETED — at which point its
    PR has only just *opened*, not merged. The PR lands asynchronously later via
    the rollup, by which point the child is already terminal, so no status
    transition re-fires the recompute and ``prs_merged`` stays stale-low. Call
    this from merge chokepoints (where the landed signal is stamped) so the
    cycle's merge counts reflect reality — the churn guardrail, lifetime-burn
    backstop, and productivity scoring all read ``prs_merged``/``merge_rate``.

    The recompute re-counts *every* child of the cycle via
    :func:`is_merged_completion`, so a single child's merge heals the whole
    cycle entry (picking up sibling PRs that landed since the last recompute).

    Criterion evidence recording runs for any task that carries ``criterion_ids``
    and a proven verdict, independent of whether the direction recompute ran.
    The direction recompute additionally requires ``research_task_id``.
    Best-effort: a failure here must never break merge finalization, so errors
    are logged and the session is rolled back to keep the caller's scope clean.
    """
    import contextlib
    import logging

    if task.project_id is None:
        return
    ctx = task.context or {}
    direction_id = ctx.get("direction_id")
    research_task_id = ctx.get("research_task_id")
    # Snapshot the task id for the best-effort failure logs below. Both blocks
    # call session.rollback() on failure (expiring `task` regardless of
    # expire_on_commit); a later getattr(task, "id") in the except handlers would
    # fire a sync lazy-load -> MissingGreenlet (getattr's default does NOT swallow
    # it — a lazy-load raises MissingGreenlet, not AttributeError), which would
    # propagate out of merge finalization.
    _task_id_str = str(task.id)
    # Direction outcome recompute requires both IDs; skip if either is absent.
    if direction_id and research_task_id:
        try:
            await _update_direction_research_outcomes(
                task.project_id, str(direction_id), str(research_task_id), session
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; must not break merge finalize
            logging.getLogger(__name__).warning(
                "[DIRECTION] post-merge outcome refresh failed for task %s: %r",
                _task_id_str,
                exc,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
                # rollback expired `task`; re-materialise it so the criterion
                # evidence block below (which passes the live ORM task to
                # record_task_merge_evidence) runs on a live instance instead of
                # firing a sync lazy-load -> MissingGreenlet.
                await session.refresh(task)
    # Criterion evidence is independent of the direction recompute — record for
    # any task that carries criterion_ids and a proven verdict.
    # Best-effort: a failure here must never break merge finalization.
    try:
        from app.acceptance_criteria import record_task_merge_evidence

        await record_task_merge_evidence(session, task, task.project_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[DIRECTION] criterion evidence recording failed for task %s: %r",
            _task_id_str,
            exc,
        )
        with contextlib.suppress(Exception):
            await session.rollback()


async def discount_stranded_pr_from_direction_credit(
    task: Task, session: AsyncSession, *, had_pr_merged: bool | None = None
) -> None:
    """Persist a confirmed non-landing discount to the direction's research-history entry.

    Called after :func:`~app.landed_signal.mark_stranded_non_landing` has set
    ``landed_on_integration=False`` on a task whose PR was previously
    badge-counted (``pr_merged=True``) and may already have been accumulated
    into the cycle's ``prs_merged``/``verified_prs_merged`` count by an earlier
    :func:`_update_direction_research_outcomes` pass.

    Increments ``stranded_prs_merged`` on the matching ``research_history`` entry
    so that :func:`credit_gate_merged_count` subtracts the stale credit without
    requiring a full outcome recompute.  Only increments when the task actually
    carried ``pr_merged=True`` — a task that was never badge-counted needs no
    correction.

    ``had_pr_merged`` lets callers supply the pre-stranding badge state when
    calling this function after :func:`~app.landed_signal.mark_stranded_non_landing`,
    which clears ``pr_merged`` from ``task.context``.  When ``None`` (default),
    the function reads ``pr_merged`` from the current context (safe only when
    called before stranding).

    Idempotent: stamps ``stranded_discount_applied=True`` on ``task.context``
    after the first successful discount so repeated rollup-management cycles
    that re-enter the stranded-PR path are no-ops and cannot over-subtract
    direction credit.

    Best-effort: failures are logged and the session rolled back. NB the
    rollback discards any uncommitted state on that session — including the
    caller's own ``mark_stranded_non_landing`` context mutation when the
    caller shares its session — and expires every ORM instance loaded in it
    (regardless of ``expire_on_commit=False``). Callers that must keep the
    stranding marker and their loaded instances alive across a discount
    failure should invoke this on a private short-lived session (see the
    ``_stranding_queue`` drain in
    ``strategic_spawning._build_recent_interventions``); there the rollback
    is contained and genuinely leaves the caller undisturbed.
    """
    import contextlib
    import logging

    try:
        # All attribute/context reads live INSIDE the try: a caller may pass a
        # Task loaded through a load_only(..., raiseload=True) projection that
        # omits project_id (or context), and the resulting InvalidRequestError
        # must degrade to the best-effort warning below — not escape and abort
        # the caller (2026-07-11: it aborted every autonomous spawn cycle via
        # strategic_spawning._build_recent_interventions).
        ctx = task.context or {}
        direction_id = ctx.get("direction_id")
        research_task_id = ctx.get("research_task_id")
        if not (direction_id and research_task_id) or task.project_id is None:
            return
        # Use caller-supplied snapshot when available — mark_stranded_non_landing
        # pops pr_merged before this function is called, so ctx.get("pr_merged")
        # would be falsy even for previously badge-counted tasks.
        _was_badge_counted = (
            had_pr_merged if had_pr_merged is not None else bool(ctx.get("pr_merged"))
        )
        if not _was_badge_counted:
            # Never badge-counted; no stale credit to retract.
            return
        if ctx.get("stranded_discount_applied"):
            # Idempotency guard: discount already applied on a prior cycle; skip.
            return

        _discount_was_applied: list[bool] = [False]

        def _apply_stranded_discount(dirs: list) -> None:
            for d in dirs:
                if d.get("id") != str(direction_id):
                    continue
                history = list(d.get("research_history", []))
                _found = False
                for idx, h in enumerate(history):
                    if h.get("task_id") == str(research_task_id):
                        current = int(h.get("stranded_prs_merged") or 0)
                        history[idx] = {**h, "stranded_prs_merged": current + 1}
                        _found = True
                        break
                d["research_history"] = history
                if not _found:
                    # Cycle entry was evicted — route discount to history_aggregate directly
                    # so the credit-gate correction is not silently lost.
                    _agg = d.get("history_aggregate") or {}
                    _agg.setdefault("total_stranded_prs_merged", 0)
                    _agg["total_stranded_prs_merged"] += 1
                    # Update the eviction snapshot so a future post-merge recompute
                    # computes the correct delta (stranded→0 undoes this increment).
                    _snaps = _agg.get("_evicted_cycle_snapshots") or {}
                    _rid = str(research_task_id)
                    if _rid in _snaps:
                        _snap = _snaps[_rid]
                        _snap["stranded_prs_merged"] = (
                            int(_snap.get("stranded_prs_merged") or 0) + 1
                        )
                    d["history_aggregate"] = _agg
                _discount_was_applied[0] = True
                break

        await with_directions_locked(session, task.project_id, _apply_stranded_discount)
        if _discount_was_applied[0]:
            # Stamp the idempotency flag so repeated rollup-management cycles are no-ops.
            # Only written when the discount was actually applied (live entry or evicted aggregate),
            # not when the direction itself was not found.
            task.context = {**ctx, "stranded_discount_applied": True}
        # Commit even when no discount was applied (direction not found): the
        # caller ran mark_stranded_non_landing on task.context BEFORE calling
        # this function, and without a commit here that stranding marker is
        # discarded on session close — the task then re-strands every cycle
        # (warning-log churn, repeated ancestry checks). The idempotency stamp
        # above is still only written when a discount was actually applied.
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort; must not undo stranding
        logging.getLogger(__name__).warning(
            "[DIRECTION] stranded-PR credit discount failed for task %s: %r",
            getattr(task, "id", None),
            exc,
        )
        with contextlib.suppress(Exception):
            await session.rollback()


def _refresh_pipeline_scope_merge_rates(project: Project) -> None:
    """Publish project-wide scope merge rates to runtime_config.

    Aggregates scope_breakdown across every research_history entry in the
    project's directions, then writes merged/effective rate for each scope
    with >= 5 effective data points. Keeps the fallback signal in
    predict_spawn_success current without operator intervention; scopes with
    sparse data are omitted to avoid noisy early estimates.
    """
    from app.shared_state import runtime_config  # noqa: PLC0415
    from app.spawn_calibration import (  # noqa: PLC0415
        _aggregate_scope_breakdown,
        _effective_scope_denominator,
    )

    proj_entries = [
        entry
        for d in (project.directions or [])
        for entry in (d.get("research_history") or [])
    ]
    aggregated = _aggregate_scope_breakdown(proj_entries)
    rates: dict[str, float] = {}
    for scope, stats in aggregated.items():
        effective = _effective_scope_denominator(stats)
        if effective >= 5:
            rates[scope] = round(stats["merged"] / effective, 3)
    runtime_config["pipeline_scope_merge_rates"] = rates

    # Pipeline-wide expansion ratios per scope: fallback for brand-new directions
    # with no local history. Uses max(mean_merged, mean_failed) so the failed-task
    # overflow signal biases conservatively (same semantics as the direction-level
    # _avg_medium_expansion_ratio helper in research_completion.py).
    ratio_samples: dict[str, dict[str, list[float]]] = {}
    for entry in proj_entries:
        for scope, stats in (entry.get("scope_breakdown") or {}).items():
            bucket = ratio_samples.setdefault(scope, {"merged": [], "failed": []})
            _m = stats.get("avg_expansion_ratio_merged")
            if _m is None:
                _m = stats.get("avg_expansion_ratio")
            if isinstance(_m, (int, float)):
                bucket["merged"].append(float(_m))
            _f = stats.get("avg_expansion_ratio_failed")
            if isinstance(_f, (int, float)):
                bucket["failed"].append(float(_f))
    ratios: dict[str, float] = {}
    for scope, samples in ratio_samples.items():
        if len(samples["merged"]) + len(samples["failed"]) < 5:
            continue
        candidates: list[float] = []
        if samples["merged"]:
            candidates.append(sum(samples["merged"]) / len(samples["merged"]))
        if samples["failed"]:
            candidates.append(sum(samples["failed"]) / len(samples["failed"]))
        if candidates:
            ratios[scope] = round(max(candidates), 3)
    runtime_config["pipeline_scope_expansion_ratios"] = ratios


def summarize_review_quality_feedback(
    history: list, first_attempt_feedback: dict | None = None
) -> dict | None:
    """Aggregate review quality signals across the last 3 research cycles.

    Returns totals for reviews dismissed, overridden, and user-caught issues,
    plus an optional note when a signal is elevated relative to completed
    tasks — letting agents calibrate self-review thoroughness. Returns None
    when no recent cycle carries any of the signals.

    When ``first_attempt_feedback`` is the direction's tally dict from
    ``design_doc['direction_first_attempt_feedback'][direction_id]`` and the
    total category counts are >= 3, includes a ``top_categories`` field with
    the top 3 rejection categories (sorted by count desc) so agents see WHICH
    categories are driving review pressure.
    """
    if not isinstance(history, list):
        return None
    recent = [h for h in history[-3:] if isinstance(h, dict)]
    if not recent:
        return None
    dismissed = sum(int(h.get("reviews_dismissed") or 0) for h in recent)
    overridden = sum(int(h.get("reviews_overridden") or 0) for h in recent)
    user_caught = sum(int(h.get("user_issues_caught") or 0) for h in recent)
    completed = sum(int(h.get("tasks_completed") or 0) for h in recent)
    if dismissed == 0 and overridden == 0 and user_caught == 0:
        return None
    note = None
    if completed >= 3:
        if dismissed / completed >= 0.3:
            note = (
                f"User dismissed {dismissed}/{completed} reviews in last "
                f"{len(recent)} cycles — self-review may be too strict."
            )
        elif user_caught / completed >= 0.3:
            note = (
                f"Users caught {user_caught} issues self-review missed across "
                f"{completed} completed tasks — be more thorough."
            )
        elif overridden / completed >= 0.3:
            note = (
                f"User overrode {overridden}/{completed} review outcomes — "
                f"self-review judgments may be miscalibrated."
            )
    result: dict = {
        "reviews_dismissed": dismissed,
        "reviews_overridden": overridden,
        "user_issues_caught": user_caught,
        "cycles_considered": len(recent),
    }
    if note:
        result["note"] = note
    if isinstance(first_attempt_feedback, dict):
        cat_counts = {
            k: v
            for k, v in first_attempt_feedback.items()
            if k not in ("total", "last_updated") and isinstance(v, int) and v > 0
        }
        if sum(cat_counts.values()) >= 3:
            top = sorted(cat_counts.items(), key=lambda kv: -kv[1])[:3]
            result["top_categories"] = dict(top)
    return result


def synthesize_prompt_quality_patterns(history: list) -> list[str]:
    """Synthesize merged-vs-failed prompt characteristic differences across
    recent research cycles into actionable guidance strings.

    Returns an empty list unless at least 2 cycles have prompt_telemetry
    with both merged and failed buckets populated, and only emits notes
    where the difference between buckets is meaningful (not noise).
    """
    if not isinstance(history, list):
        return []
    merged_acc: dict[str, list[float]] = {}
    failed_acc: dict[str, list[float]] = {}
    cycles_with_both = 0
    for entry in history:
        if not isinstance(entry, dict):
            continue
        pt = entry.get("prompt_telemetry")
        if not isinstance(pt, dict):
            continue
        m = pt.get("merged") if isinstance(pt.get("merged"), dict) else None
        f = pt.get("failed") if isinstance(pt.get("failed"), dict) else None
        if not (m and f):
            continue
        cycles_with_both += 1
        for key in (
            "avg_char_count",
            "avg_related_file_count",
            "frac_mentions_tests",
            "frac_mentions_symbols",
        ):
            mv, fv = m.get(key), f.get(key)
            if isinstance(mv, (int, float)):
                merged_acc.setdefault(key, []).append(float(mv))
            if isinstance(fv, (int, float)):
                failed_acc.setdefault(key, []).append(float(fv))
    if cycles_with_both < 2:
        return []
    notes: list[str] = []

    def _avg(vals: list[float]) -> float | None:
        return sum(vals) / len(vals) if vals else None

    mc, fc = (
        _avg(merged_acc.get("avg_char_count", [])),
        _avg(failed_acc.get("avg_char_count", [])),
    )
    if mc is not None and fc is not None and max(mc, fc) > 0:
        if abs(mc - fc) / max(mc, fc) >= 0.2:
            notes.append(
                f"Merged prompts averaged {int(mc)} chars vs {int(fc)} for failed — "
                f"{'aim for more detail' if mc > fc else 'keep prompts tighter'}."
            )
    mr, fr = (
        _avg(merged_acc.get("avg_related_file_count", [])),
        _avg(failed_acc.get("avg_related_file_count", [])),
    )
    if mr is not None and fr is not None and max(mr, fr) > 0:
        if abs(mr - fr) >= 1.0:
            notes.append(
                f"Merged prompts referenced {round(mr, 1)} related files on average vs {round(fr, 1)} for failed — "
                f"{'cite more concrete files' if mr > fr else 'avoid over-listing files'}."
            )
    mt, ft = (
        _avg(merged_acc.get("frac_mentions_tests", [])),
        _avg(failed_acc.get("frac_mentions_tests", [])),
    )
    if mt is not None and ft is not None and abs(mt - ft) >= 0.2:
        notes.append(
            f"{int(mt * 100)}% of merged prompts mentioned tests vs {int(ft * 100)}% of failed — "
            f"{'emphasize test expectations' if mt > ft else 'test mentions did not correlate with merge here'}."
        )
    ms, fs = (
        _avg(merged_acc.get("frac_mentions_symbols", [])),
        _avg(failed_acc.get("frac_mentions_symbols", [])),
    )
    if ms is not None and fs is not None and abs(ms - fs) >= 0.2:
        notes.append(
            f"{int(ms * 100)}% of merged prompts named specific symbols vs {int(fs * 100)}% of failed — "
            f"{'name concrete functions/classes' if ms > fs else 'symbol naming did not correlate with merge here'}."
        )

    # Scope-segmented char-count guidance: when both merged_<scope> and
    # failed_<scope> buckets have data across cycles, emit a scope-specific
    # note so research agents can calibrate prompt detail per scope.
    for scope in ("small", "medium"):
        m_chars: list[float] = []
        f_chars: list[float] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            pt = entry.get("prompt_telemetry")
            if not isinstance(pt, dict):
                continue
            m_seg = pt.get(f"merged_{scope}")
            f_seg = pt.get(f"failed_{scope}")
            if isinstance(m_seg, dict) and isinstance(
                m_seg.get("avg_char_count"), (int, float)
            ):
                m_chars.append(float(m_seg["avg_char_count"]))
            if isinstance(f_seg, dict) and isinstance(
                f_seg.get("avg_char_count"), (int, float)
            ):
                f_chars.append(float(f_seg["avg_char_count"]))
        if not m_chars or not f_chars:
            continue
        mv, fv = sum(m_chars) / len(m_chars), sum(f_chars) / len(f_chars)
        if max(mv, fv) > 0 and abs(mv - fv) / max(mv, fv) >= 0.2:
            notes.append(
                f"{scope.capitalize()}-scope merged prompts averaged {int(mv)} chars "
                f"vs {int(fv)} for failed {scope}-scope."
            )
    return notes


def summarize_failure_hotspots(direction: dict, project_id) -> dict | None:
    """Build the failure_hotspots block for research direction_context.

    Surfaces diagnosed affected files, the diagnosis summary, and any active
    anomalies tracked in shared_state so research agents can either address
    or steer away from known failure regions. Returns None when there is
    nothing to surface.
    """
    if not isinstance(direction, dict):
        return None
    hotspot_files = [
        f
        for f in (direction.get("diagnosed_affected_files") or [])
        if isinstance(f, str)
    ]
    failure_hotspots: dict = {}
    if hotspot_files:
        failure_hotspots["diagnosed_affected_files"] = hotspot_files[:15]
        diagnosis_summary = (direction.get("diagnosis_summary") or "").strip()
        if diagnosis_summary:
            failure_hotspots["diagnosis_summary"] = diagnosis_summary[:220]
        last_fp = direction.get("last_remediation_fingerprint")
        if last_fp:
            failure_hotspots["last_remediation_fingerprint"] = last_fp
    try:
        from app import shared_state as _ss_anom

        _anoms = _ss_anom.persistent_anomalies.get(str(project_id)) or {}
        if _anoms:
            active_anomalies = []
            for key, info in list(_anoms.items())[:5]:
                if not isinstance(info, dict):
                    continue
                details = (info.get("latest_details") or "")[:140]
                active_anomalies.append(
                    {
                        "key": key,
                        "occurrence_count": info.get("occurrence_count", 0),
                        "severity": info.get("severity"),
                        "details": details,
                    }
                )
            if active_anomalies:
                failure_hotspots["active_anomalies"] = active_anomalies
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Failed to surface active anomalies (non-critical)"
        )
    return failure_hotspots or None


def summarize_dedup_filter(history: list) -> dict | None:
    """Summarize last research cycle's filter_breakdown for the next agent.

    Without this, the research agent cannot tell whether its last batch was
    rejected because of duplicate phrasing, overlapping files, or wrong
    finding type — and so cannot adjust. Returns a dict with a human-readable
    summary plus optional dominant-cause guidance, or None if nothing was
    filtered last cycle.
    """
    if not isinstance(history, list) or not history:
        return None
    last_entry = history[-1]
    if not isinstance(last_entry, dict):
        return None
    fb = last_entry.get("filter_breakdown") or {}
    hash_n = int(fb.get("hash_dedup", 0) or 0)
    word_n = int(fb.get("word_overlap_dedup", 0) or 0)
    file_n = int(fb.get("file_overlap_dedup", 0) or 0)
    type_n = int(fb.get("type_filtered", 0) or 0)
    total_filtered = hash_n + word_n + file_n + type_n
    if total_filtered <= 0:
        return None
    findings_produced = int(last_entry.get("findings_produced", 0) or 0)
    parts = []
    if word_n:
        parts.append(f"{word_n} word-overlap")
    if file_n:
        parts.append(f"{file_n} file-overlap")
    if hash_n:
        parts.append(f"{hash_n} hash-dedup")
    if type_n:
        parts.append(f"{type_n} type-filtered")
    summary_text = (
        f"Last cycle: {total_filtered}/{findings_produced} findings filtered "
        f"({', '.join(parts)})"
    )
    dominant_n, dominant_kind = max(
        ((word_n, "word"), (file_n, "file"), (hash_n, "hash"), (type_n, "type")),
        key=lambda x: x[0],
    )
    guidance = None
    if dominant_n > 0 and dominant_n * 2 >= total_filtered:
        if dominant_kind == "word":
            guidance = (
                "Word-overlap dominates — use more distinctive titles and "
                "descriptions to differentiate from existing work."
            )
        elif dominant_kind == "file":
            guidance = (
                "File-overlap dominates — findings target files with active "
                "PRs. Pick different files or wait for active work to merge."
            )
        elif dominant_kind == "hash":
            guidance = (
                "Hash-dedup dominates — exact duplicate findings. Vary "
                "phrasing and approach to avoid repeating prior work."
            )
        elif dominant_kind == "type":
            guidance = (
                "Most findings were type-filtered (direction_update / "
                "design_doc_observation). Produce actionable task findings."
            )
    result: dict = {"summary": summary_text}
    if guidance:
        result["guidance"] = guidance
    return result
