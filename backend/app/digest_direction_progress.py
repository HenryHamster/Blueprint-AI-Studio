"""Per-direction progress aggregation for the catch-up digest.

Extracted from ``app.digest_metrics`` so direction-progress work — the
single largest analytic in that module — doesn't require editing the
shared metrics module or the router directly. ``app.digest_metrics``
re-exports ``compute_direction_progress`` so existing import paths
(and ``unittest.mock.patch`` targets) are unchanged.
"""

from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.direction_outcomes import verified_merged_count
from app.models import Project, Task, TaskStatus
from app.time_utils import _to_naive_utc, _coerce_naive_utc

# Task types that never open a PR. These are excluded from the
# conversion_rate denominator so the metric means the same thing on every
# surface: merged PRs / PR-eligible tasks created (NOT / all tasks created).
PR_INELIGIBLE_TASK_TYPES = ("research", "investigate", "audit", "memo")


def pr_ineligible_created_in_entry(entry: Dict[str, Any]) -> int:
    """Number of PR-ineligible tasks created in one research_history entry.

    Sourced from the entry's ``type_breakdown`` so every consumer computes the
    conversion-rate denominator (merged PRs / PR-eligible tasks) identically.
    """
    type_breakdown = entry.get("type_breakdown") or {}
    return sum(
        (type_breakdown.get(ttype) or {}).get("created", 0) or 0
        for ttype in PR_INELIGIBLE_TASK_TYPES
    )


async def compute_direction_progress(
    session: AsyncSession,
    *,
    project: Project,
    all_tasks: List[Task],
    window_start: datetime,
    prev_window_start: datetime,
) -> tuple[
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, str],
    bool,
    Dict[str, Any],
]:
    """Per-direction progress aggregation for the catch-up digest.

    Walks ``project.directions`` and each direction's ``research_history`` to
    build the ``direction_progress_list`` (sorted by ``cycles_in_window``
    desc), the project-level ``scope_conversion_rates`` /
    ``prev_scope_conversion_rates`` rollups, and a ``dir_id_to_text`` lookup
    used downstream to label failed tasks with their parent direction.

    Side effect: when a direction crosses 80% of its ``cost_budget_usd`` for
    the first time, marks ``_budget_alerted`` on the direction entry,
    dispatches a fire-and-forget ``BUDGET_EXCEEDED`` notification, and (once
    per call) commits the mutation. The returned bool reports whether a
    commit was performed so the caller can avoid double-commits.
    """
    import asyncio

    from sqlalchemy.orm.attributes import flag_modified

    from app.direction_metrics import (
        _compute_direction_quality_score,
        _compute_lifetime_generalized_outcome_state,
        _entry_generalized_successes,
        _entry_generalized_terminal_outcomes,
        _fallback_outcome_kind_breakdown,
        _merge_outcome_kind_breakdown,
    )
    from app.direction_outcomes import synthesize_prompt_quality_patterns
    from app.notification_dispatch import dispatch_external_notification
    from app.routers.admin_analytics import (
        ON_DEMAND_RATE,
        SPOT_RATE,
        _derive_completed_at,
    )

    direction_progress_list: List[Dict[str, Any]] = []
    direction_portfolio_composition: List[Dict[str, Any]] = []
    dir_id_to_text: Dict[str, str] = {}
    scope_conversion_rates: Dict[str, Dict[str, Any]] = {}
    prev_scope_conversion_rates: Dict[str, Dict[str, Any]] = {}
    budget_alert_fired = False

    active_directions = [
        d
        for d in (project.directions or [])
        if d.get("status") in ("active", "held", "auto_paused")
    ]
    active_count = len(active_directions)
    self_sufficient_count = 0

    # Build direction_id -> text lookup
    for d in active_directions:
        did = d.get("id") or d.get("direction_id")
        if did:
            dir_id_to_text[str(did)] = d.get("text") or d.get("description") or ""

    # Build direction_id -> tasks mapping
    dir_tasks: Dict[str, List[Task]] = {}
    for t in all_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            dir_tasks.setdefault(str(did), []).append(t)

    project_id = project.id

    for d in active_directions:
        did = str(d.get("id") or d.get("direction_id") or "")
        if not did:
            continue

        tasks_for_dir = dir_tasks.get(did, [])
        cycles_in_window = sum(
            1
            for t in tasks_for_dir
            if t.status == TaskStatus.COMPLETED
            and (t.first_payload).get("type", "create") == "research"
            and _derive_completed_at(t) >= window_start
        )
        tasks_created_in_window = sum(
            1 for t in tasks_for_dir if _to_naive_utc(t.created_at) >= window_start
        )
        quiet_this_window = cycles_in_window == 0 and tasks_created_in_window == 0

        # Single pass over research_history: aggregate PR/scope/product/type/taxonomy
        prs_merged = 0
        # Verification-aware merge count (verified + benign-unverified, excluding
        # degraded-unverified) — drives conversion_rate so a merge that never
        # actually verified does not inflate the operator-read rate.
        verified_prs_merged = 0
        autonomous_prs_merged = 0
        assisted_prs_merged = 0
        operator_action_count = 0
        prs_rejected = 0
        generalized_successes = 0
        generalized_terminal_outcomes = 0
        findings_tasks_created = 0
        pr_ineligible_tasks_created = 0
        merge_time_values: List[float] = []
        dir_scope_agg: Dict[str, Dict[str, Any]] = {}
        prev_dir_scope_agg: Dict[str, Dict[str, Any]] = {}
        window_product_ratios: List[float] = []
        agg_type_distribution: Dict[str, int] = {}
        agg_type_breakdown: Dict[str, Dict[str, int]] = {}
        dir_outcome_kind_breakdown: Dict[str, Dict[str, int]] = {}
        dir_taxonomy_summary: Dict[str, int] = {}
        dir_stage_dropout: Dict[str, int] = {}
        recent_failed_titles: List[str] = []
        dir_findings_produced = 0
        dir_batch_cap_applied = 0
        dir_filter_breakdown_agg: Dict[str, int] = {}
        dq_plans = 0
        dq_merge_rate_sum = 0.0
        dq_merge_rate_count = 0
        dq_subtask_count_sum = 0
        dq_cross_module_sum = 0
        dir_reviews_dismissed = 0
        dir_reviews_overridden = 0
        dir_user_issues_caught = 0
        for entry in d.get("research_history") or []:
            try:
                entry_ts = _coerce_naive_utc(
                    datetime.fromisoformat(entry.get("timestamp", ""))
                )
            except (ValueError, TypeError):
                continue
            if entry_ts >= window_start:
                prs_merged += entry.get("prs_merged") or 0
                verified_prs_merged += verified_merged_count(entry)
                # Legacy fallback: pre-attribution entries lack autonomous_prs_merged.
                autonomous_prs_merged += (
                    entry.get("autonomous_prs_merged", entry.get("prs_merged")) or 0
                )
                assisted_prs_merged += entry.get("assisted_prs_merged", 0) or 0
                operator_action_count += entry.get("operator_action_count", 0) or 0
                prs_rejected += entry.get("prs_rejected") or 0
                generalized_successes += _entry_generalized_successes(entry)
                generalized_terminal_outcomes += _entry_generalized_terminal_outcomes(
                    entry
                )
                _merge_outcome_kind_breakdown(
                    dir_outcome_kind_breakdown,
                    entry.get("outcome_kind_breakdown")
                    or _fallback_outcome_kind_breakdown(entry),
                )
                dir_reviews_dismissed += entry.get("reviews_dismissed") or 0
                dir_reviews_overridden += entry.get("reviews_overridden") or 0
                dir_user_issues_caught += entry.get("user_issues_caught") or 0
                findings_tasks_created += entry.get("tasks_created") or 0
                # Count tasks of types that never open PRs (research, investigate, audit, memo)
                # to calculate PR-eligible denominator for conversion_rate
                pr_ineligible_tasks_created += pr_ineligible_created_in_entry(entry)
                dir_findings_produced += entry.get("findings_produced") or 0
                dir_batch_cap_applied += entry.get("batch_cap_applied") or 0
                for _fk, _fv in (entry.get("filter_breakdown") or {}).items():
                    dir_filter_breakdown_agg[_fk] = dir_filter_breakdown_agg.get(
                        _fk, 0
                    ) + (_fv or 0)
                atm = entry.get("avg_time_to_merge_hours")
                if atm is not None:
                    merge_time_values.append(float(atm))
                for scope, stats in (entry.get("scope_breakdown") or {}).items():
                    agg = dir_scope_agg.setdefault(
                        scope,
                        {
                            "created": 0,
                            "merged": 0,
                            "merged_cross_module": 0,
                            "failed": 0,
                            "failed_infra": 0,
                            "failed_code_error": 0,
                            "failed_scope_overflow": 0,
                            "calibrated_in": 0,
                            "calibrated_out": 0,
                            "failure_stages": {},
                        },
                    )
                    for k in (
                        "created",
                        "merged",
                        "merged_cross_module",
                        "failed",
                        "failed_infra",
                        "failed_code_error",
                        "failed_scope_overflow",
                        "calibrated_in",
                        "calibrated_out",
                    ):
                        agg[k] += stats.get(k, 0)
                    stage_agg = agg.setdefault("failure_stages", {})
                    for stage_name, stage_cnt in (
                        stats.get("failure_stages") or {}
                    ).items():
                        stage_agg[stage_name] = stage_agg.get(stage_name, 0) + (
                            stage_cnt or 0
                        )
                    # Weighted predictor accumulators (weight by merged/failed counts
                    # that produced each per-cycle average in direction_outcomes.py).
                    merged_w = stats.get("merged", 0) or 0
                    failed_w = stats.get("failed", 0) or 0
                    for field_name, weight in (
                        ("avg_expansion_ratio_merged", merged_w),
                        ("avg_expansion_ratio_failed", failed_w),
                        ("avg_complexity_merged", merged_w),
                        ("avg_complexity_failed", failed_w),
                    ):
                        v = stats.get(field_name)
                        if isinstance(v, (int, float)) and weight > 0:
                            agg[f"_{field_name}_sum"] = (
                                agg.get(f"_{field_name}_sum", 0.0) + float(v) * weight
                            )
                            agg[f"_{field_name}_w"] = (
                                agg.get(f"_{field_name}_w", 0) + weight
                            )
                    for effort, buckets in (
                        stats.get("effort_distribution") or {}
                    ).items():
                        if not isinstance(buckets, dict):
                            continue
                        ed = agg.setdefault("effort_distribution", {})
                        eb = ed.setdefault(
                            effort, {"created": 0, "merged": 0, "failed": 0}
                        )
                        for bk in ("created", "merged", "failed"):
                            eb[bk] += buckets.get(bk, 0) or 0
                pr = entry.get("product_ratio")
                if pr is not None:
                    window_product_ratios.append(float(pr))
                for ttype, cnt in (entry.get("type_distribution") or {}).items():
                    agg_type_distribution[ttype] = (
                        agg_type_distribution.get(ttype, 0) + cnt
                    )
                for ttype, stats in (entry.get("type_breakdown") or {}).items():
                    tb = agg_type_breakdown.setdefault(
                        ttype, {"created": 0, "merged": 0, "failed": 0}
                    )
                    for k in ("created", "merged", "failed"):
                        tb[k] += (stats or {}).get(k, 0) or 0
                for tax, cnt in (entry.get("taxonomy_summary") or {}).items():
                    dir_taxonomy_summary[tax] = dir_taxonomy_summary.get(tax, 0) + cnt
                for stage, cnt in (entry.get("stage_dropout") or {}).items():
                    dir_stage_dropout[stage] = dir_stage_dropout.get(stage, 0) + (
                        cnt or 0
                    )
                for dq in entry.get("decomposition_quality") or []:
                    if not isinstance(dq, dict):
                        continue
                    dq_plans += 1
                    mr = dq.get("subtask_merge_rate")
                    if mr is not None:
                        dq_merge_rate_sum += float(mr)
                        dq_merge_rate_count += 1
                    dq_subtask_count_sum += dq.get("subtask_count") or 0
                    dq_cross_module_sum += dq.get("cross_module_count") or 0
                for fd in entry.get("failed_task_details") or []:
                    title = (fd or {}).get("title")
                    if title:
                        recent_failed_titles.append(title)
            elif entry_ts >= prev_window_start:
                for scope, stats in (entry.get("scope_breakdown") or {}).items():
                    agg = prev_dir_scope_agg.setdefault(
                        scope,
                        {
                            "created": 0,
                            "merged": 0,
                            "calibrated_in": 0,
                            "calibrated_out": 0,
                        },
                    )
                    for k in (
                        "created",
                        "merged",
                        "calibrated_in",
                        "calibrated_out",
                    ):
                        agg[k] += stats.get(k, 0)

        last_entry = (
            (d.get("research_history") or [])[-1] if d.get("research_history") else None
        )
        latest_insights = last_entry.get("cycle_insights", []) if last_entry else []
        recent_finding_titles = (
            last_entry.get("finding_titles", []) if last_entry else []
        )
        latest_research_at = last_entry.get("timestamp") if last_entry else None
        dir_product_ratio = (
            round(sum(window_product_ratios) / len(window_product_ratios), 3)
            if window_product_ratios
            else None
        )

        dir_cost = 0.0
        dir_spot_cost = 0.0
        dir_on_demand_cost = 0.0
        cost_by_scope: Dict[str, float] = {}
        cost_by_status: Dict[str, float] = {}
        cost_by_type: Dict[str, float] = {}
        cost_by_provider_model: Dict[str, float] = {}
        for t in tasks_for_dir:
            if not (
                t.vm_provisioned_at
                and t.vm_stopped_at
                and t.vm_stopped_at >= window_start
            ):
                continue
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            dir_cost += t_cost
            if t.vm_spot:
                dir_spot_cost += t_cost
            else:
                dir_on_demand_cost += t_cost
            scope_key = (t.context or {}).get("scope", "unknown")
            cost_by_scope[scope_key] = cost_by_scope.get(scope_key, 0.0) + t_cost
            status_key = t.status.value if hasattr(t.status, "value") else str(t.status)
            cost_by_status[status_key] = cost_by_status.get(status_key, 0.0) + t_cost
            type_key = (t.first_payload or {}).get("type", "create")
            cost_by_type[type_key] = cost_by_type.get(type_key, 0.0) + t_cost
            provider_key = t.provider or "unknown"
            model_key = t.model or "unknown"
            pm_key = f"{provider_key}|{model_key}"
            cost_by_provider_model[pm_key] = (
                cost_by_provider_model.get(pm_key, 0.0) + t_cost
            )
        cost_by_scope = {k: round(v, 4) for k, v in cost_by_scope.items()}
        cost_by_status = {k: round(v, 4) for k, v in cost_by_status.items()}
        cost_by_type = {k: round(v, 4) for k, v in cost_by_type.items()}
        cost_by_provider_model = {
            k: round(v, 4) for k, v in cost_by_provider_model.items()
        }

        dir_fa_measured = [
            t
            for t in tasks_for_dir
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and _derive_completed_at(t) >= window_start
            and "_first_attempt_succeeded" in (t.context or {})
        ]
        dir_fa_sample_size = len(dir_fa_measured)
        dir_fa_rate = (
            round(
                sum(
                    1
                    for t in dir_fa_measured
                    if (t.context or {}).get("_first_attempt_succeeded")
                )
                / dir_fa_sample_size,
                3,
            )
            if dir_fa_sample_size > 0
            else None
        )

        _history_all = d.get("research_history") or []
        _agg = d.get("history_aggregate") or {}
        lifetime_cycles = len(_history_all) + (_agg.get("total_cycles") or 0)
        lifetime_prs_merged = sum(h.get("prs_merged", 0) for h in _history_all) + (
            _agg.get("total_prs_merged") or 0
        )
        # Verification-aware lifetime merge count for lifetime_conversion_rate:
        # window entries via verified_merged_count, aggregate via the folded
        # total_verified_prs_merged (legacy fallback to total_prs_merged).
        lifetime_verified_prs_merged = sum(
            verified_merged_count(h) for h in _history_all
        ) + (_agg.get("total_verified_prs_merged", _agg.get("total_prs_merged")) or 0)
        lifetime_prs_rejected = sum(h.get("prs_rejected", 0) for h in _history_all) + (
            _agg.get("total_prs_rejected") or 0
        )
        lifetime_tasks_created = sum(
            h.get("tasks_created", 0) for h in _history_all
        ) + (_agg.get("total_tasks_created") or 0)
        lifetime_tasks_failed = sum(h.get("tasks_failed", 0) for h in _history_all) + (
            _agg.get("total_tasks_failed") or 0
        )
        lifetime_conversion_rate = (
            round(lifetime_verified_prs_merged / lifetime_tasks_created, 3)
            if lifetime_tasks_created > 0
            else None
        )
        lifetime_autonomous_prs_merged = sum(
            h.get("autonomous_prs_merged", h.get("prs_merged", 0)) for h in _history_all
        ) + (_agg.get("total_autonomous_prs_merged") or 0)
        lifetime_operator_actions = sum(
            h.get("operator_action_count", 0) for h in _history_all
        ) + (_agg.get("total_operator_actions") or 0)
        lifetime_generalized_state = _compute_lifetime_generalized_outcome_state(d)
        retirement_eligible = (
            lifetime_autonomous_prs_merged >= 10 and lifetime_operator_actions == 0
        )
        evicted_findings_count = len(_agg.get("evicted_findings") or [])
        direction_text = (d.get("text") or d.get("description") or "")[:80]

        direction_portfolio_composition.append(
            {
                "direction_id": did,
                "direction_text": direction_text,
                "status": d.get("status"),
                "priority": d.get("priority") or "medium",
                "quiet_this_window": quiet_this_window,
            }
        )
        if retirement_eligible:
            self_sufficient_count += 1

        total_prs = prs_merged + prs_rejected
        acceptance_rate = round(prs_merged / total_prs, 3) if total_prs > 0 else None
        avg_time_to_merge_hours = (
            round(sum(merge_time_values) / len(merge_time_values), 2)
            if merge_time_values
            else None
        )
        # Conversion rate: verified merges / PR-eligible tasks (exclude non-PR
        # task types). Verification-aware so degraded-unverified merges don't
        # inflate it, matching the per-task outcome layer.
        pr_eligible_tasks = findings_tasks_created - pr_ineligible_tasks_created
        conversion_rate = (
            round(verified_prs_merged / pr_eligible_tasks, 3)
            if pr_eligible_tasks > 0
            else None
        )
        generalized_health_rate = (
            round(generalized_successes / generalized_terminal_outcomes, 3)
            if generalized_terminal_outcomes > 0
            else None
        )
        # Accumulate into project-level scope rate aggregates
        for _s, _st in dir_scope_agg.items():
            if _s not in scope_conversion_rates:
                scope_conversion_rates[_s] = {
                    "created": 0,
                    "merged": 0,
                    "merged_cross_module": 0,
                    "failed_scope_overflow": 0,
                    "calibrated_in": 0,
                    "calibrated_out": 0,
                    "failure_stages": {},
                }
            scope_conversion_rates[_s]["created"] += _st["created"]
            scope_conversion_rates[_s]["merged"] += _st["merged"]
            scope_conversion_rates[_s]["merged_cross_module"] += _st.get(
                "merged_cross_module", 0
            )
            scope_conversion_rates[_s]["failed_scope_overflow"] += _st.get(
                "failed_scope_overflow", 0
            )
            scope_conversion_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
            scope_conversion_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)
            _stage_acc = scope_conversion_rates[_s].setdefault("failure_stages", {})
            for _stg, _scnt in (_st.get("failure_stages") or {}).items():
                _stage_acc[_stg] = _stage_acc.get(_stg, 0) + (_scnt or 0)
            for _f in (
                "avg_expansion_ratio_merged",
                "avg_expansion_ratio_failed",
            ):
                _sum = _st.get(f"_{_f}_sum", 0.0)
                _w = _st.get(f"_{_f}_w", 0)
                if _w > 0:
                    scope_conversion_rates[_s][f"_{_f}_sum"] = (
                        scope_conversion_rates[_s].get(f"_{_f}_sum", 0.0) + _sum
                    )
                    scope_conversion_rates[_s][f"_{_f}_w"] = (
                        scope_conversion_rates[_s].get(f"_{_f}_w", 0) + _w
                    )
        for _s, _st in prev_dir_scope_agg.items():
            if _s not in prev_scope_conversion_rates:
                prev_scope_conversion_rates[_s] = {
                    "created": 0,
                    "merged": 0,
                    "calibrated_in": 0,
                    "calibrated_out": 0,
                }
            prev_scope_conversion_rates[_s]["created"] += _st["created"]
            prev_scope_conversion_rates[_s]["merged"] += _st["merged"]
            prev_scope_conversion_rates[_s]["calibrated_in"] += _st.get(
                "calibrated_in", 0
            )
            prev_scope_conversion_rates[_s]["calibrated_out"] += _st.get(
                "calibrated_out", 0
            )

        decomposition_summary = None
        if dq_plans > 0:
            decomposition_summary = {
                "plans_assessed": dq_plans,
                "avg_subtask_merge_rate": (
                    round(dq_merge_rate_sum / dq_merge_rate_count, 3)
                    if dq_merge_rate_count > 0
                    else None
                ),
                "avg_subtask_count": round(dq_subtask_count_sum / dq_plans, 2),
                "avg_cross_module_count": round(dq_cross_module_sum / dq_plans, 2),
            }

        filter_funnel = None
        if (
            dir_findings_produced
            or dir_batch_cap_applied
            or dir_filter_breakdown_agg
            or findings_tasks_created
        ):
            filter_funnel = {
                "findings_produced": dir_findings_produced,
                "hash_dedup": dir_filter_breakdown_agg.get("hash_dedup", 0),
                "word_overlap": dir_filter_breakdown_agg.get("word_overlap_dedup", 0),
                "file_overlap": dir_filter_breakdown_agg.get("file_overlap_dedup", 0),
                "type_filtered": dir_filter_breakdown_agg.get("type_filtered", 0),
                "empty_prompt": dir_filter_breakdown_agg.get("empty_prompt", 0),
                "batch_cap": dir_batch_cap_applied,
                "tasks_created": findings_tasks_created,
            }

        for _sk, _agg in dir_scope_agg.items():
            for _field in (
                "avg_expansion_ratio_merged",
                "avg_expansion_ratio_failed",
                "avg_complexity_merged",
                "avg_complexity_failed",
            ):
                w = _agg.pop(f"_{_field}_w", 0)
                s = _agg.pop(f"_{_field}_sum", 0.0)
                if w > 0:
                    _agg[_field] = round(s / w, 2)
            if "avg_expansion_ratio_merged" in _agg:
                _agg["avg_expansion_ratio"] = _agg["avg_expansion_ratio_merged"]

        _dir_rejections = []
        for _t in tasks_for_dir:
            _ctx = _t.context or {}
            if not _ctx.get("pr_rejected"):
                continue
            _ts = _derive_completed_at(_t)
            if _ts < window_start:
                continue
            _dir_rejections.append((_ts, _t))
        _dir_rejections.sort(key=lambda x: x[0], reverse=True)

        _pq_notes = synthesize_prompt_quality_patterns(d.get("research_history") or [])
        prompt_quality_hint = _pq_notes[0] if _pq_notes else None
        rejection_reasons = [
            {
                "task_id": str(_t.id),
                "title": _t.title,
                "rejection_note": ((_t.context or {}).get("pr_rejection_note") or "")[
                    :200
                ],
            }
            for _ts, _t in _dir_rejections[:3]
        ]

        cost_budget_usd = d.get("cost_budget_usd")
        budget_utilization_pct = None
        budget_exceeded = False
        if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
            budget_utilization_pct = round((dir_cost / cost_budget_usd) * 100, 1)
            budget_exceeded = dir_cost > cost_budget_usd
            if budget_utilization_pct >= 80 and not d.get("_budget_alerted"):
                d["_budget_alerted"] = True
                flag_modified(project, "directions")
                budget_alert_fired = True
                asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        "BUDGET_EXCEEDED",
                        {
                            "direction_id": did,
                            "direction_name": direction_text,
                            "current_spend_usd": round(dir_cost, 2),
                            "budget_limit_usd": float(cost_budget_usd),
                            "utilization_pct": budget_utilization_pct,
                        },
                    )
                )

        if not quiet_this_window:
            direction_progress_list.append(
                {
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cost_budget_usd": cost_budget_usd,
                    "budget_utilization_pct": budget_utilization_pct,
                    "budget_exceeded": budget_exceeded,
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "verified_prs_merged": verified_prs_merged,
                    "autonomous_prs_merged": autonomous_prs_merged,
                    "assisted_prs_merged": assisted_prs_merged,
                    "operator_action_count": operator_action_count,
                    "prs_rejected": prs_rejected,
                    "generalized_successes": generalized_successes,
                    "generalized_terminal_outcomes": generalized_terminal_outcomes,
                    "generalized_health_rate": generalized_health_rate,
                    "outcome_kind_breakdown": dir_outcome_kind_breakdown or None,
                    "acceptance_rate": acceptance_rate,
                    "conversion_rate": conversion_rate,
                    "pr_eligible_tasks_created": pr_eligible_tasks,
                    "avg_time_to_merge_hours": avg_time_to_merge_hours,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "possibly_complete": bool(d.get("possibly_complete", False)),
                    "auto_paused": bool(d.get("auto_paused", False)),
                    "paused_reason": d.get("paused_reason"),
                    "needs_manual_intervention": bool(
                        d.get("needs_manual_intervention", False)
                    ),
                    "completion_criteria": d.get("completion_criteria"),
                    "completion_suggestion": d.get("completion_suggestion"),
                    "category": d.get("category"),
                    "status": d.get("status"),
                    "added_at": d.get("added_at"),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                    "product_ratio": dir_product_ratio,
                    "type_distribution": agg_type_distribution or None,
                    "type_breakdown": agg_type_breakdown or None,
                    "estimated_cost_usd": round(dir_cost, 2),
                    "spot_cost_usd": round(dir_spot_cost, 2),
                    "on_demand_cost_usd": round(dir_on_demand_cost, 2),
                    "cost_by_scope": cost_by_scope or None,
                    "cost_by_status": cost_by_status or None,
                    "cost_by_type": cost_by_type or None,
                    "cost_by_provider_model": cost_by_provider_model or None,
                    "cost_per_merged_pr": round(dir_cost / autonomous_prs_merged, 4)
                    if autonomous_prs_merged > 0
                    else None,
                    "scope_breakdown": dir_scope_agg or None,
                    "taxonomy_summary": dir_taxonomy_summary or None,
                    "stage_dropout": dir_stage_dropout or None,
                    "recent_failure_titles": recent_failed_titles[-3:] or None,
                    "filter_funnel": filter_funnel,
                    "decomposition_summary": decomposition_summary,
                    "first_attempt_success_rate": dir_fa_rate,
                    "first_attempt_sample_size": dir_fa_sample_size,
                    "quality_score": round(_compute_direction_quality_score(d), 3),
                    "lifetime_cycles": lifetime_cycles,
                    "lifetime_prs_merged": lifetime_prs_merged,
                    "lifetime_prs_rejected": lifetime_prs_rejected,
                    "lifetime_tasks_failed": lifetime_tasks_failed,
                    "lifetime_conversion_rate": lifetime_conversion_rate,
                    "lifetime_autonomous_prs_merged": lifetime_autonomous_prs_merged,
                    "lifetime_operator_actions": lifetime_operator_actions,
                    "lifetime_generalized_successes": lifetime_generalized_state[
                        "generalized_successes"
                    ],
                    "lifetime_generalized_health_rate": lifetime_generalized_state[
                        "generalized_health_rate"
                    ],
                    "retirement_eligible": retirement_eligible,
                    "evicted_findings_count": evicted_findings_count,
                    "review_quality": {
                        "reviews_dismissed": dir_reviews_dismissed,
                        "reviews_overridden": dir_reviews_overridden,
                        "user_issues_caught": dir_user_issues_caught,
                    },
                    "rejection_reasons": rejection_reasons or None,
                    "prompt_quality_hint": prompt_quality_hint,
                }
            )

    direction_progress_list.sort(key=lambda x: x["cycles_in_window"], reverse=True)

    if budget_alert_fired:
        try:
            await session.commit()
        except Exception:
            await session.rollback()

    for _s, _agg in scope_conversion_rates.items():
        for _f in ("avg_expansion_ratio_merged", "avg_expansion_ratio_failed"):
            _w = _agg.pop(f"_{_f}_w", 0)
            _sum = _agg.pop(f"_{_f}_sum", 0.0)
            if _w > 0:
                _agg[_f] = round(_sum / _w, 2)

    return (
        direction_progress_list,
        scope_conversion_rates,
        prev_scope_conversion_rates,
        dir_id_to_text,
        budget_alert_fired,
        {
            "active_count": active_count,
            "self_sufficient_count": self_sufficient_count,
            "composition": direction_portfolio_composition,
        },
    )
