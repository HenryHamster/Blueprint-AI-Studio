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
    CODE_ERROR,
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
from app.landed_signal import is_merged_completion, merge_verification_status
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.review_pipeline import _is_test_file
from app.task_capabilities import PRODUCT_TASK_TYPES
from app.task_locking import with_design_doc_locked, with_directions_locked


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
    }
)


def _is_non_genuine_cancelled(task: Task) -> bool:
    """True when a CANCELLED task represents a non-genuine outcome that must not
    count against direction health.

    Covers (a) cascade kills (see ``_is_cascade_cancelled``), (b) the new
    review/rollup non-genuine markers (``pr_closed_non_rejection``,
    ``cap_exhausted_after_approve``), and (c) REASSIGNED / SCOPE_EMPTY handoffs
    that land CANCELLED rather than FAILED. A genuine code/review failure never
    carries any of these signals, so this never hides a real regression.
    """
    if task.status != TaskStatus.CANCELLED:
        return False
    if _is_cascade_cancelled(task):
        return True
    ctx = task.context or {}
    if ctx.get("failure_detail") in _NON_GENUINE_CANCELLED_DETAILS:
        return True
    return ctx.get("failure_category") in (REASSIGNED, SCOPE_EMPTY)


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


_AUTONOMY_RETIREMENT_THRESHOLD = 10


def compute_direction_autonomy_state(direction: dict) -> dict:
    """Return lifetime autonomy metrics and retirement eligibility for a direction.

    Reads research_history and history_aggregate; applies a legacy fallback so
    pre-attribution entries (no autonomous_prs_merged key) treat prs_merged as
    autonomous — consistent with _fold_evicted_into_aggregate and admin_digest.

    A direction is retirement-eligible when lifetime_autonomous_prs_merged >= 10
    AND lifetime_operator_actions == 0 (zero-tolerance for operator intervention).
    """
    history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    lifetime_autonomous = sum(
        h.get("autonomous_prs_merged", h.get("prs_merged", 0)) for h in history
    ) + (agg.get("total_autonomous_prs_merged") or 0)
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


def _apply_cochange_to_doc(doc: dict, merged_files_lists: list[list[str]]) -> None:
    """Apply co-change affinity index updates to a design_doc dict in place.

    Core logic extracted from _update_cochange_index so it can be called
    inside with_design_doc_locked (which passes the doc dict directly).
    """
    if not merged_files_lists:
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
    for files in merged_files_lists:
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
    if entry_snap is None:
        return
    # Work on a mutable copy; all stat writes accumulate here and are then
    # applied to the freshly-locked directions row inside with_directions_locked.
    entry = dict(entry_snap)
    try:
        research_task_uuid = uuid.UUID(entry.get("task_id", ""))
    except (ValueError, AttributeError):
        return
    child_tasks_stmt = select(Task).where(Task.parent_task_id == research_task_uuid)
    child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
    if not child_tasks_rows:
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
        """True when *ct* opened a PR (via any reliable signal)."""
        if ct.id in _pr_artifact_task_ids:
            return True
        ctx_ = ct.context or {}
        if ctx_.get("pr_number") or ctx_.get("rollup_pr_number"):
            return True
        return bool((ct.first_payload or {}).get("pr_number"))
    # Per-(provider, model) cost rollup: keyed by f"{provider}|{model}", values
    # in USD. Uses VM-runtime × spot/on-demand rate (matching admin_digest math)
    # so values are consistent even when Task.estimated_cost_usd is unbackfilled
    # for older rows. Tasks missing both provider and model bucket under
    # "unknown|unknown" so legacy entries degrade without dropping cost.
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE  # noqa: PLC0415

    cost_by_provider_model: dict[str, float] = {}
    cycle_cost_total = 0.0
    for ct in child_tasks_rows:
        if not (ct.vm_provisioned_at and ct.vm_stopped_at):
            continue
        rate = SPOT_RATE if ct.vm_spot else ON_DEMAND_RATE
        t_cost = rate * (ct.vm_stopped_at - ct.vm_provisioned_at).total_seconds()
        if t_cost <= 0:
            continue
        provider_key = ct.provider or "unknown"
        model_key = ct.model or "unknown"
        bucket_key = f"{provider_key}|{model_key}"
        cost_by_provider_model[bucket_key] = (
            cost_by_provider_model.get(bucket_key, 0.0) + t_cost
        )
        cycle_cost_total += t_cost
    if cost_by_provider_model:
        entry["cost_by_provider_model"] = {
            k: round(v, 4) for k, v in cost_by_provider_model.items()
        }
        entry["estimated_cost_usd"] = round(cycle_cost_total, 4)

    entry["tasks_completed"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED
    )
    entry["tasks_failed"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED
    )
    entry["tasks_cancelled"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED
    )
    # BLOCKED tasks are counted separately from FAILED — they are parked on an
    # absent precondition, not failures, and the auto-unblock sweep is expected
    # to return them to QUEUED. Surfacing the count keeps them out of the
    # failure tally while still being visible to operators/analytics.
    entry["tasks_blocked"] = sum(
        1 for ct in child_tasks_rows if ct.status == TaskStatus.BLOCKED
    )
    entry["prs_merged"] = sum(
        1
        for ct in child_tasks_rows
        if is_merged_completion(ct)
    )
    entry["prs_rejected"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_rejected")
    )
    # tasks_completed_no_pr: children that COMPLETED correctly without ever
    # opening a PR — "no changes needed / work already exists" outcomes. These
    # are a healthy result, NOT churn, so a downstream zero-merge/churn guard
    # must be able to subtract them from the completed-but-unmerged population.
    # Excludes merged completions (is_merged_completion) and pr_rejected ones so
    # only genuine no-PR completions are counted; ``_opened_pr`` gates on the
    # canonical PULL_REQUEST artifact (plus pr_number fallbacks).
    entry["tasks_completed_no_pr"] = sum(
        1
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and not is_merged_completion(ct)
        and not (ct.context or {}).get("pr_rejected")
        and not _opened_pr(ct)
    )
    _merged_rows = [
        ct
        for ct in child_tasks_rows
        if is_merged_completion(ct)
    ]
    entry["autonomous_prs_merged"] = sum(
        1 for ct in _merged_rows if is_autonomous_merge(ct)
    )
    entry["assisted_prs_merged"] = sum(
        1 for ct in _merged_rows if not is_autonomous_merge(ct)
    )
    # verified_prs_merged: merged completions whose PR was actually verified by
    # a real test suite (not a fall-open for a degraded environment/setup reason).
    # Additive alongside prs_merged for back-compat; consumers that need "proven
    # fix" counts should prefer this field over the raw prs_merged.
    entry["verified_prs_merged"] = sum(
        1
        for ct in _merged_rows
        if merge_verification_status(ct) != "unverified_degraded"
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
        1
        for ct in _prev_rows
        if is_merged_completion(ct)
    )
    entry["preventive_context_tasks_failed"] = sum(
        1 for ct in _prev_rows if _counts_as_failure(ct)
    )
    # Remediation tasks: spawned from a diagnosis or auto-remediation. Tracked
    # separately so research agents can see the remediation burden — a
    # direction merging emergency fixes is not the same as one merging features.
    _remediation_rows = [
        ct
        for ct in child_tasks_rows
        if (ct.context or {}).get("diagnosis_task_id")
        or (ct.context or {}).get("auto_remediation") is True
    ]
    entry["remediation_tasks_created"] = len(_remediation_rows)
    entry["remediation_tasks_merged"] = sum(
        1
        for ct in _remediation_rows
        if is_merged_completion(ct)
    )
    entry["remediation_tasks_failed"] = sum(
        1 for ct in _remediation_rows if _counts_as_failure(ct)
    )
    # Collect changed files from merged PRs for co-change index update
    merged_files_lists = [
        (ct.context or {}).get("_pr_changed_files", [])
        for ct in child_tasks_rows
        if is_merged_completion(ct)
        and (ct.context or {}).get("_pr_changed_files")
    ]
    _tasks_created = entry.get("tasks_created", 0)
    entry["conversion_rate"] = (
        round(entry["prs_merged"] / _tasks_created, 3) if _tasks_created > 0 else None
    )
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
    # avg_implementation_hours: time the agent spent producing a PR
    # (vm_stopped_at - created_at) across ALL completed children, not
    # just merged ones. Pairs with avg_review_hours below to split
    # avg_time_to_merge_hours into implementation vs review-pipeline time.
    impl_durations = [
        (_to_naive_utc(ct.vm_stopped_at) - _to_naive_utc(ct.created_at)).total_seconds()
        / 3600
        for ct in child_tasks_rows
        if ct.status == TaskStatus.COMPLETED
        and ct.vm_stopped_at is not None
        and ct.created_at is not None
    ]
    entry["avg_implementation_hours"] = (
        round(sum(impl_durations) / len(impl_durations), 2) if impl_durations else None
    )
    # avg_review_hours: time from implementation finish to PR merge.
    # PR merge timestamp isn't stored; approximate via the matching
    # review child's vm_stopped_at (latest if multiple iterations),
    # falling back to the merged task's updated_at.
    review_end_by_pr: dict = {}
    for ct in child_tasks_rows:
        if (ct.first_payload or {}).get("type") != "review":
            continue
        if ct.vm_stopped_at is None:
            continue
        pr_num = (ct.context or {}).get("pr_number")
        if pr_num is None:
            continue
        prev = review_end_by_pr.get(pr_num)
        if prev is None or ct.vm_stopped_at > prev:
            review_end_by_pr[pr_num] = ct.vm_stopped_at
    review_durations = []
    for ct in child_tasks_rows:
        if not (
            is_merged_completion(ct)
            and ct.vm_stopped_at is not None
        ):
            continue
        pr_num = (ct.context or {}).get("pr_number")
        end_ts = review_end_by_pr.get(pr_num) if pr_num is not None else None
        if end_ts is None:
            end_ts = ct.updated_at
        end_ts_n = _to_naive_utc(end_ts)
        vm_stop_n = _to_naive_utc(ct.vm_stopped_at)
        if end_ts_n is not None and vm_stop_n is not None and end_ts_n >= vm_stop_n:
            review_durations.append((end_ts_n - vm_stop_n).total_seconds() / 3600)
    entry["avg_review_hours"] = (
        round(sum(review_durations) / len(review_durations), 2)
        if review_durations
        else None
    )
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
            if category in INFRA_CLASSES and not _is_review_cap_exhausted(ct):
                infra_failures += 1
    entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat
    entry["tasks_failed_infra"] = infra_failures
    entry["taxonomy_summary"] = taxonomy_summary

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
        if is_merged_completion(ct)
        and rollup_failed
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
    entry["structural_count"] = structural_count

    # Track per-scope merge rates so research agents can calibrate scope assignments.
    # calibrated_in: tasks calibrated INTO this scope from a larger one (counted at the
    # effective scope). calibrated_out: tasks originally proposed at this scope but
    # calibrated to a smaller one (counted at the original scope).
    scope_breakdown: dict = {}
    escalation_outcomes: dict = {}
    SCOPE_RANK = {"small": 0, "medium": 1, "large": 2}
    expansion_sums: dict = {}
    expansion_counts: dict = {}
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
        prod_changed = [f for f in changed if not _is_test_file(f)]
        if related and prod_changed:
            ratio = len(prod_changed) / len(related)
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
            avg = round(expansion_sums[scope_key] / count, 2)
            scope_breakdown[scope_key]["avg_expansion_ratio"] = avg
            scope_breakdown[scope_key]["avg_expansion_ratio_merged"] = avg
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
            merged_ct = sum(
                1
                for st in sts
                if is_merged_completion(st)
            )
            failed_nc = sum(
                1
                for st in sts
                if _counts_as_failure(st)
                and (
                    _classify_failure(st) not in INFRA_CLASSES
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
        _sub_merged = sum(
            1
            for ct in _decomposed
            if is_merged_completion(ct)
        )
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
                # Promote (proposed) finding to (confirmed)
                new_established = []
                for item in parsed["established"]:
                    if title.lower() in item.lower() and "(proposed)" in item:
                        new_established.append(
                            item.replace("(proposed)", "(confirmed)")
                        )
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
        session.expire(project, ["design_doc"])
        await with_design_doc_locked(
            session,
            project_id,
            lambda doc: _apply_cochange_to_doc(doc, merged_files_lists),
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

    No-op unless *task* carries both ``direction_id`` and ``research_task_id``.
    Best-effort: a failure here must never break merge finalization, so errors
    are logged and the session is rolled back to keep the caller's scope clean.
    """
    import contextlib
    import logging

    ctx = task.context or {}
    direction_id = ctx.get("direction_id")
    research_task_id = ctx.get("research_task_id")
    if not (direction_id and research_task_id) or task.project_id is None:
        return
    try:
        await _update_direction_research_outcomes(
            task.project_id, str(direction_id), str(research_task_id), session
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; must not break merge finalize
        logging.getLogger(__name__).warning(
            "[DIRECTION] post-merge outcome refresh failed for task %s: %r",
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
