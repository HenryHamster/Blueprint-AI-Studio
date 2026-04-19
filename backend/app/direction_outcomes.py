"""Direction research outcome tracking.

Extracted from task_lifecycle.py to relieve its line-count ceiling.
Contains _update_direction_research_outcomes, called whenever a child task
of a research cycle reaches a terminal status so that research_history
entries accumulate live outcome statistics.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.failure_stage import _derive_failure_stage
from app.failure_classification import (
    CODE_ERROR,
    INFRA_CLASSES,
    SCOPE_OVERFLOW,
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
    classify_failure as _classify_failure,
    classify_failure_taxonomy as _classify_taxonomy,
)
from app.models import Project, Task, TaskStatus


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
    )


def _is_review_cap_exhausted(task: Task) -> bool:
    """Review-fix pipeline hit its iteration cap — a code/review-quality failure,
    not infrastructure, even though failure_class is AGENT_TIMEOUT (in INFRA_CLASSES)."""
    return (task.context or {}).get("failure_detail") == "review_cap_exhausted"


def _counts_as_failure(task: Task) -> bool:
    """Terminal status that represents a genuine failure for direction metrics."""
    if task.status == TaskStatus.FAILED:
        return True
    return task.status == TaskStatus.CANCELLED and not _is_cascade_cancelled(task)


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


def _update_cochange_index(project: Project, merged_files_lists: list[list[str]]) -> None:
    """Update the co-change affinity index in project.design_doc.

    For each merged PR's file list, records which files changed together.
    Scores decay on each call so stale associations fade over time.
    Keeps top-5 affinities per file, capped at 200 files total.
    """
    if not merged_files_lists:
        return

    DECAY = 0.90
    TOP_N = 5
    MAX_FILES = 200
    MIN_SCORE = 0.3

    design_doc = dict(project.design_doc or {})
    raw_index = design_doc.get("cochange_index") or {}
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
            for file_b in unique_files[i + 1:]:
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
        top_files = sorted(file_scores.keys(), key=lambda f: file_scores[f], reverse=True)[:MAX_FILES]
        index = {f: index[f] for f in top_files}

    design_doc["cochange_index"] = index
    project.design_doc = design_doc
    flag_modified(project, "design_doc")


async def _update_direction_research_outcomes(
    project_id: uuid.UUID, direction_id: str, research_task_id: str, session: AsyncSession
) -> None:
    """Update a direction's research_history entry with child task outcome counts.

    Called whenever a child task of a research cycle reaches a terminal status so
    that research_history entries accumulate live outcome statistics.
    """
    from app.research_context import _parse_research_context  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if not project:
        return
    directions = list(project.directions or [])
    updated = False
    merged_files_lists: list[list[str]] = []
    for d in directions:
        if d.get("id") != direction_id:
            continue
        history = d.get("research_history", [])
        for entry in history:
            if entry.get("task_id") != research_task_id:
                continue
            research_task_uuid = None
            try:
                research_task_uuid = uuid.UUID(entry.get("task_id", ""))
            except (ValueError, AttributeError):
                pass
            if not research_task_uuid:
                break
            child_tasks_stmt = select(Task).where(Task.parent_task_id == research_task_uuid)
            child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
            if not child_tasks_rows:
                break
            entry["tasks_completed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED)
            entry["tasks_failed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED)
            entry["tasks_cancelled"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED)
            entry["prs_merged"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged")
            )
            entry["prs_rejected"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_rejected")
            )
            # Collect changed files from merged PRs for co-change index update
            merged_files_lists = [
                (ct.context or {}).get("_pr_changed_files", [])
                for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED
                and (ct.context or {}).get("pr_merged")
                and (ct.context or {}).get("_pr_changed_files")
            ]
            _tasks_created = entry.get("tasks_created", 0)
            entry["conversion_rate"] = round(entry["prs_merged"] / _tasks_created, 3) if _tasks_created > 0 else None
            # Review quality signals: track how users respond to Blueprint's reviews
            entry["reviews_dismissed"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("review_dismissed")
            )
            entry["reviews_overridden"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("user_overrode_review")
            )
            entry["user_issues_caught"] = sum(
                1 for ct in child_tasks_rows
                if (ct.context or {}).get("user_caught_issues")
            )
            # avg_time_to_merge_hours: mean of (vm_stopped_at - created_at) for merged tasks
            merge_durations = [
                (ct.vm_stopped_at - ct.created_at).total_seconds() / 3600
                for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED
                and (ct.context or {}).get("pr_merged")
                and ct.vm_stopped_at is not None
                and ct.created_at is not None
            ]
            entry["avg_time_to_merge_hours"] = round(sum(merge_durations) / len(merge_durations), 2) if merge_durations else None
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
                    failed_details.append({
                        "title": ct.title or "",
                        "reason": reason,
                        "category": category,
                        "taxonomy": taxonomy,
                        "effort_attempted": effort_attempted,
                        "scope_calibrated_from": scope_calibrated_from,
                    })
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
                1 for ct in child_tasks_rows
                if ct.requires_approval
                and ct.status not in (TaskStatus.COMPLETED,)
                and ct.id not in sd_code_error_ids
            )
            research_task_obj = await session.get(Task, research_task_uuid)
            rollup_failed = (
                research_task_obj is not None
                and research_task_obj.status == TaskStatus.FAILED
            )
            sd_staging_only = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED
                and (ct.context or {}).get("pr_merged")
                and rollup_failed
            )
            entry["stage_dropout"] = {
                "tasks_failed_code_error": sd_code_error,
                "tasks_failed_infra": sd_infra,
                "tasks_required_approval": sd_required_approval,
                "tasks_merged_staging_only": sd_staging_only,
            }

            # Classify child tasks as product vs structural
            PRODUCT_TYPES = {"create", "feature", "improvement"}
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
                ctx_t = ct.context or {}
                if ct.status == TaskStatus.COMPLETED and ctx_t.get("pr_merged"):
                    type_breakdown[task_type]["merged"] += 1
                if _counts_as_failure(ct):
                    type_breakdown[task_type]["failed"] += 1
                if task_type in PRODUCT_TYPES:
                    product_count += 1
                else:
                    structural_count += 1
            entry["type_distribution"] = type_counts
            entry["type_breakdown"] = type_breakdown
            total_typed = product_count + structural_count
            entry["product_ratio"] = round(product_count / total_typed, 2) if total_typed > 0 else 0.0
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
                        "created": 0, "merged": 0, "failed": 0, "failed_infra": 0,
                        "failed_code_error": 0, "failed_scope_overflow": 0,
                        "failed_analysis_overflow": 0,
                        "calibrated_in": 0, "calibrated_out": 0,
                        "exploration_created": 0, "exploration_merged": 0,
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
                    if ct.status == TaskStatus.COMPLETED and ctx_.get("pr_merged"):
                        effort_bucket["merged"] += 1
                    if _counts_as_failure(ct):
                        effort_bucket["failed"] += 1
                if ct.status == TaskStatus.COMPLETED and ctx_.get("pr_merged"):
                    scope_breakdown[scope]["merged"] += 1
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
                if ctx_.get("medium_exploration"):
                    scope_breakdown[scope]["exploration_created"] += 1
                    if ct.status == TaskStatus.COMPLETED and ctx_.get("pr_merged"):
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
                        if ct.status == TaskStatus.COMPLETED and ctx_.get("pr_merged"):
                            bucket["merged"] += 1
                        if _counts_as_failure(ct):
                            bucket["failed"] += 1
                # File expansion ratio: actual files touched / expected files.
                # Leading indicator of scope overflow risk.
                related = ctx_.get("related_files") or []
                changed = ctx_.get("_pr_changed_files") or []
                if related and changed:
                    ratio = len(changed) / len(related)
                    if ct.status == TaskStatus.COMPLETED:
                        expansion_sums[scope] = expansion_sums.get(scope, 0.0) + ratio
                        expansion_counts[scope] = expansion_counts.get(scope, 0) + 1
                    elif _counts_as_failure(ct):
                        # Failed tasks (especially SCOPE_OVERFLOW) are the exact scenario
                        # the expansion predictor tries to prevent — track separately so
                        # scope calibration has a conservative signal.
                        expansion_failed_sums[scope] = expansion_failed_sums.get(scope, 0.0) + ratio
                        expansion_failed_counts[scope] = expansion_failed_counts.get(scope, 0) + 1
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
                if ct.status == TaskStatus.COMPLETED and ctx2.get("pr_merged"):
                    cplx_merged_sum[scope2] = cplx_merged_sum.get(scope2, 0.0) + float(cs)
                    cplx_merged_n[scope2] = cplx_merged_n.get(scope2, 0) + 1
                elif _counts_as_failure(ct):
                    cplx_failed_sum[scope2] = cplx_failed_sum.get(scope2, 0.0) + float(cs)
                    cplx_failed_n[scope2] = cplx_failed_n.get(scope2, 0) + 1
            for sk, n in cplx_merged_n.items():
                if n > 0 and sk in scope_breakdown:
                    scope_breakdown[sk]["avg_complexity_merged"] = round(cplx_merged_sum[sk] / n, 2)
            for sk, n in cplx_failed_n.items():
                if n > 0 and sk in scope_breakdown:
                    scope_breakdown[sk]["avg_complexity_failed"] = round(cplx_failed_sum[sk] / n, 2)
            skip_counts = (
                (research_task_obj.context or {}).get("skipped_low_confidence_by_scope", {})
                if research_task_obj else {}
            )
            for sk, cnt in (skip_counts or {}).items():
                _ensure_scope(sk)
                scope_breakdown[sk]["skipped_low_confidence"] = (
                    scope_breakdown[sk].get("skipped_low_confidence", 0) + int(cnt)
                )
            entry["scope_breakdown"] = scope_breakdown
            entry["escalation_outcomes"] = escalation_outcomes

            # Decomposition quality: per plan-parent metrics so future calibration
            # can correlate decomposition shape (breadth, file overlap, cross-module
            # coupling, dep depth) with subtask merge success.
            plan_child_ids = [
                ct.id for ct in child_tasks_rows
                if (ct.first_payload or {}).get("type") == "plan"
                and ct.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            ]
            decomposition_quality: list = []
            if plan_child_ids:
                subtask_rows = (await session.execute(
                    select(Task).where(Task.parent_task_id.in_(plan_child_ids))
                )).scalars().all()
                by_parent: dict = {}
                for st in subtask_rows:
                    by_parent.setdefault(st.parent_task_id, []).append(st)
                for plan_id, sts in by_parent.items():
                    merged_ct = sum(
                        1 for st in sts
                        if st.status == TaskStatus.COMPLETED and (st.context or {}).get("pr_merged")
                    )
                    failed_nc = sum(
                        1 for st in sts
                        if _counts_as_failure(st)
                        and (_classify_failure(st) not in INFRA_CLASSES
                             or _is_review_cap_exhausted(st))
                    )
                    file_counts = [
                        len((st.context or {}).get("_pr_changed_files") or [])
                        for st in sts
                        if (st.context or {}).get("_pr_changed_files")
                    ]
                    cross_mod = sum(
                        1 for st in sts
                        if len({
                            f.split("/", 1)[0]
                            for f in ((st.context or {}).get("_pr_changed_files") or [])
                            if "/" in f
                        }) >= 2
                    )
                    deps_map = {str(st.id): list(st.depends_on or []) for st in sts}
                    denom = merged_ct + failed_nc
                    decomposition_quality.append({
                        "plan_task_id": str(plan_id),
                        "subtask_count": len(sts),
                        "subtask_merge_rate": round(merged_ct / denom, 3) if denom > 0 else None,
                        "avg_files_per_subtask": round(sum(file_counts) / len(file_counts), 2) if file_counts else 0.0,
                        "max_files_in_subtask": max(file_counts) if file_counts else 0,
                        "dependency_depth": _longest_chain_length(deps_map),
                        "cross_module_count": cross_mod,
                    })
                # Sentinel entries for plans that reached terminal state without producing
                # any subtasks — otherwise these decomposition failures leave no data in
                # decomposition_quality and _poor_decomp_history stays blind to them.
                for plan_id in plan_child_ids:
                    if plan_id in by_parent:
                        continue
                    decomposition_quality.append({
                        "plan_task_id": str(plan_id),
                        "subtask_count": 0,
                        "subtask_merge_rate": 0.0,
                        "avg_files_per_subtask": 0.0,
                        "max_files_in_subtask": 0,
                        "dependency_depth": 0,
                        "cross_module_count": 0,
                    })
            entry["decomposition_quality"] = decomposition_quality

            # Co-change auto-decomposition outcomes: track merge rate of
            # sub-findings produced by auto_decompose_findings_by_cochange so
            # spawn_calibration can factor in whether splitting medium findings
            # into smaller ones actually improves conversion.
            _decomp_stats = (
                ((research_task_obj.context or {}) if research_task_obj else {})
                .get("cochange_decomposition_stats") or {}
            ).get(direction_id)
            if _decomp_stats:
                _decomposed = [
                    ct for ct in child_tasks_rows
                    if (ct.context or {}).get("auto_decomposed")
                ]
                _sub_merged = sum(
                    1 for ct in _decomposed
                    if ct.status == TaskStatus.COMPLETED
                    and (ct.context or {}).get("pr_merged")
                )
                _sub_total = len(_decomposed)
                entry["cochange_decomposition"] = {
                    "medium_findings_split": _decomp_stats.get("medium_findings_split", 0),
                    "sub_findings_produced": _decomp_stats.get("sub_findings_produced", 0),
                    "sub_findings_merged": _sub_merged,
                    "sub_findings_total": _sub_total,
                    "merge_rate": round(_sub_merged / _sub_total, 3) if _sub_total > 0 else None,
                }

            # Maintain research_context: promote proposed findings based on outcomes
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
                        category = (ct.context or {}).get("failure_category") or _classify_failure(ct)
                        ts = ct.vm_stopped_at or ct.updated_at
                        failed_at_iso = ts.date().isoformat() if ts else ""
                        stage = (ct.context or {}).get("_previous_failure_stage") or _derive_failure_stage(
                            category, ct.latest_status_message or ""
                        ) or ""
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
                    elif ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged"):
                        # Promote (proposed) finding to (confirmed)
                        new_established = []
                        for item in parsed["established"]:
                            if title.lower() in item.lower() and "(proposed)" in item:
                                new_established.append(item.replace("(proposed)", "(confirmed)"))
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
                d["research_context"] = updated_ctx[:8000]

            updated = True
            break
        if updated:
            break
    if updated:
        if merged_files_lists:
            _update_cochange_index(project, merged_files_lists)
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
        _refresh_pipeline_scope_merge_rates(project)


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
