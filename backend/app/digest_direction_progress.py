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

from app.direction_outcomes import (
    PR_INELIGIBLE_TASK_TYPES,
    _SPAWN_GATE_REASON_MAX_LEN,
    _SPAWN_GATE_REASONS_SAMPLE_CAP,
    compute_direction_autonomy_state,
    declared_merged_count,
    proven_merged_count,
    reachable_merged_count,
    verified_merged_count,
)
from app.models import Project, Task, TaskStatus, get_task_cost_by_provider_model
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
from app.time_utils import _to_naive_utc, _coerce_naive_utc

_CHALLENGE_BOUNDARY_BUCKETS = (
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
)
_CHALLENGE_AGREEMENT_BUCKETS = (
    CHALLENGE_AGREES,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)


def _empty_finding_challenge_summary() -> dict[str, Any]:
    return {
        "challenged_findings": 0,
        "agreement_distribution": {k: 0 for k in _CHALLENGE_AGREEMENT_BUCKETS},
        "boundary_distribution": {k: 0 for k in _CHALLENGE_BOUNDARY_BUCKETS},
        "fail_closed_unparseable": 0,
    }


def _add_count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _merge_finding_challenge_summary(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    target["challenged_findings"] += _add_count(source.get("challenged_findings"))
    target["fail_closed_unparseable"] += _add_count(
        source.get("fail_closed_unparseable")
    )
    source_agreements = source.get("agreement_distribution") or {}
    if isinstance(source_agreements, dict):
        for key in _CHALLENGE_AGREEMENT_BUCKETS:
            target["agreement_distribution"][key] += _add_count(
                source_agreements.get(key)
            )
    source_boundaries = source.get("boundary_distribution") or {}
    if isinstance(source_boundaries, dict):
        for key in _CHALLENGE_BOUNDARY_BUCKETS:
            target["boundary_distribution"][key] += _add_count(
                source_boundaries.get(key)
            )


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


def pr_ineligible_created_in_aggregate(aggregate: Dict[str, Any]) -> int:
    """Number of folded PR-ineligible tasks in a direction history aggregate."""
    return aggregate.get("total_pr_ineligible_tasks_created") or 0


def compute_spawn_gate_aggregate(
    history: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """Aggregate research spawn-gate counters from a direction's research_history.

    Returns a compact dict or None when all counters are zero (no spawn-gate
    activity recorded). Beyond the legacy challenged/refuted/survived counts and
    merge rates, the dict carries the decision-quality breakdown
    (explicit_pass/fail_open/unresolved), the preserved spawned/merged sample
    sizes (so each merge rate always travels with its denominator), and a
    compact, deduped, capped sample of refuted/unresolved challenger reasons.
    """
    challenged = 0
    refuted = 0
    survived = 0
    explicit_pass = 0
    fail_open = 0
    unresolved = 0
    manifest_excluded_fail_closed = 0
    adversarial_give_up = 0
    challenged_spawned = 0
    challenged_merged = 0
    unchallenged_spawned = 0
    unchallenged_merged = 0
    exhaustion_count = 0
    refuted_reasons: List[str] = []
    _seen_reasons: set[str] = set()
    manifest_excluded_samples: List[Dict[str, str]] = []
    _seen_manifest_excluded: set[tuple[str, str]] = set()
    adversarial_give_up_samples: List[Dict[str, str]] = []
    _seen_adversarial_give_up: set[str] = set()
    parse_outcome_distribution: dict[str, int] = {}
    cause_distribution: dict[str, int] = {}
    for entry in history or []:
        challenged += entry.get("research_spawn_gate_challenged_count") or 0
        refuted += entry.get("research_spawn_gate_refuted_count") or 0
        survived += entry.get("research_spawn_gate_survived_count") or 0
        explicit_pass += entry.get("research_spawn_gate_explicit_pass_count") or 0
        fail_open += entry.get("research_spawn_gate_fail_open_count") or 0
        unresolved += entry.get("research_spawn_gate_unresolved_count") or 0
        manifest_excluded_fail_closed += (
            entry.get("research_spawn_gate_manifest_excluded_fail_closed_count") or 0
        )
        adversarial_give_up += (
            entry.get("research_spawn_gate_adversarial_give_up_count") or 0
        )
        challenged_spawned += entry.get("research_spawn_gate_challenged_spawned") or 0
        challenged_merged += entry.get("research_spawn_gate_challenged_merged") or 0
        unchallenged_spawned += (
            entry.get("research_spawn_gate_unchallenged_spawned") or 0
        )
        unchallenged_merged += entry.get("research_spawn_gate_unchallenged_merged") or 0
        exhaustion_count += (
            1 if entry.get("research_spawn_gate_wholesale_retry_exhausted") else 0
        )
        for _r in entry.get("research_spawn_gate_refuted_reasons") or []:
            _r = str(_r).strip()[:_SPAWN_GATE_REASON_MAX_LEN]
            if (
                _r
                and _r not in _seen_reasons
                and len(refuted_reasons) < _SPAWN_GATE_REASONS_SAMPLE_CAP
            ):
                _seen_reasons.add(_r)
                refuted_reasons.append(_r)
        for _sample in (
            entry.get("research_spawn_gate_manifest_excluded_fail_closed_samples") or []
        ):
            if not isinstance(_sample, dict):
                continue
            _compact = {
                "hash": str(_sample.get("hash") or "")[:80],
                "title": str(_sample.get("title") or "")[:120],
                "reason": str(_sample.get("reason") or "")[:_SPAWN_GATE_REASON_MAX_LEN],
            }
            _key = (_compact["hash"], _compact["title"])
            if (
                (_compact["hash"] or _compact["title"])
                and _key not in _seen_manifest_excluded
                and len(manifest_excluded_samples) < _SPAWN_GATE_REASONS_SAMPLE_CAP
            ):
                _seen_manifest_excluded.add(_key)
                manifest_excluded_samples.append(_compact)
        for _gu_sample in (
            entry.get("research_spawn_gate_adversarial_give_up_samples") or []
        ):
            if not isinstance(_gu_sample, dict):
                continue
            _gu_title = str(_gu_sample.get("title") or "")[:120]
            if (
                _gu_title
                and _gu_title not in _seen_adversarial_give_up
                and len(adversarial_give_up_samples) < _SPAWN_GATE_REASONS_SAMPLE_CAP
            ):
                _seen_adversarial_give_up.add(_gu_title)
                _gu_compact: Dict[str, str] = {"title": _gu_title}
                if _gu_sample.get("direction_id"):
                    _gu_compact["direction_id"] = str(_gu_sample["direction_id"])
                adversarial_give_up_samples.append(_gu_compact)
        _parse_outcome = str(
            entry.get("research_spawn_gate_parse_outcome") or ""
        ).strip()
        if _parse_outcome:
            parse_outcome_distribution[_parse_outcome] = (
                parse_outcome_distribution.get(_parse_outcome, 0) + 1
            )
        for _cause_cat, _cause_cnt in (
            entry.get("research_spawn_gate_cause_counts") or {}
        ).items():
            _cause_cat = str(_cause_cat).strip()
            _cnt = int(_cause_cnt or 0)
            if _cause_cat and _cnt > 0:
                cause_distribution[_cause_cat] = (
                    cause_distribution.get(_cause_cat, 0) + _cnt
                )
    if not any(
        [
            challenged,
            refuted,
            survived,
            manifest_excluded_fail_closed,
            adversarial_give_up,
            challenged_spawned,
            unchallenged_spawned,
            exhaustion_count,
        ]
    ):
        return None
    challenged_merge_rate = (
        round(challenged_merged / challenged_spawned, 3)
        if challenged_spawned > 0
        else None
    )
    unchallenged_merge_rate = (
        round(unchallenged_merged / unchallenged_spawned, 3)
        if unchallenged_spawned > 0
        else None
    )
    return {
        "challenged": challenged,
        "refuted": refuted,
        "survived": survived,
        "explicit_pass_count": explicit_pass,
        "fail_open_count": fail_open,
        "unresolved_count": unresolved,
        "manifest_excluded_fail_closed_count": manifest_excluded_fail_closed,
        "manifest_excluded_fail_closed_samples": manifest_excluded_samples,
        "adversarial_give_up_count": adversarial_give_up,
        "adversarial_give_up_samples": adversarial_give_up_samples,
        # Sample sizes preserved so each merge rate travels with its denominator.
        "challenged_spawned": challenged_spawned,
        "challenged_merged": challenged_merged,
        "unchallenged_spawned": unchallenged_spawned,
        "unchallenged_merged": unchallenged_merged,
        "challenged_merge_rate": challenged_merge_rate,
        "unchallenged_merge_rate": unchallenged_merge_rate,
        "exhaustion_count": exhaustion_count,
        "refuted_reasons": refuted_reasons,
        "parse_outcome_distribution": parse_outcome_distribution or None,
        "cause_distribution": cause_distribution or None,
    }


def _compute_auto_pause_epidemic_summary(
    active_directions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate auto-pause cycle counts across active directions.

    Returns total_cycles, affected_directions count, and top-5 offenders
    sorted by churn_family_pause_count (zombie signal) then
    auto_pause_count (lifetime) descending. All inputs are in-memory dicts
    already loaded from project.directions — no DB access needed.
    """
    pause_counts = [
        (
            str(d.get("id") or d.get("direction_id") or ""),
            d.get("text") or d.get("description") or "",
            int(d.get("auto_pause_count") or 0),
            int(d.get("churn_family_pause_count") or 0),
        )
        for d in active_directions
        if d.get("id") or d.get("direction_id")
    ]
    total_cycles = sum(c for _, _, c, _ in pause_counts)
    affected_directions = sum(1 for _, _, c, _ in pause_counts if c > 0)
    top_offenders = sorted(
        [
            {
                "direction_id": did,
                "direction_text": text[:80],
                "auto_pause_count": count,
                "churn_family_pause_count": consec,
            }
            for did, text, count, consec in pause_counts
            if count > 0
        ],
        key=lambda x: (
            x["churn_family_pause_count"],
            x["auto_pause_count"],
        ),
        reverse=True,
    )[:5]
    return {
        "total_cycles": total_cycles,
        "affected_directions": affected_directions,
        "top_offenders": top_offenders,
    }


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
        canonical_cost_per_merged_pr,
    )
    from app.digest_metrics import compute_format_compliance_metrics  # noqa: PLC0415
    from app.direction_outcomes import synthesize_prompt_quality_patterns
    from app.notification_dispatch import dispatch_external_notification
    from app.routers.admin_analytics import _derive_completed_at  # noqa: PLC0415
    from app.vm_cost_rates import get_vm_rate_per_second  # noqa: PLC0415

    direction_progress_list: List[Dict[str, Any]] = []
    direction_portfolio_composition: List[Dict[str, Any]] = []
    dir_id_to_text: Dict[str, str] = {}
    scope_conversion_rates: Dict[str, Dict[str, Any]] = {}
    prev_scope_conversion_rates: Dict[str, Dict[str, Any]] = {}
    budget_alert_fired = False
    _budget_alert_direction_ids: List[str] = []

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
        reachable_prs_merged = 0
        reachable_prs_recorded = False
        # Proven merge count + declaration-coverage numerator/denominator —
        # STRICTER than verified_prs_merged: proven counts machine-checked
        # named-test proof OR recorded downstream-artifact consumption; declared
        # counts merges that committed to forward evidence at plan time;
        # merged_write_total is the coverage denominator.
        proven_prs_merged = 0
        consumed_prs_merged = 0
        declared_prs_merged = 0
        merged_write_total = 0
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
        dir_challenge_summary = _empty_finding_challenge_summary()
        dq_plans = 0
        dq_merge_rate_sum = 0.0
        dq_merge_rate_count = 0
        dq_subtask_count_sum = 0
        dq_cross_module_sum = 0
        dir_reviews_dismissed = 0
        dir_reviews_overridden = 0
        dir_user_issues_caught = 0
        spawn_gate_challenged_count = 0
        spawn_gate_refuted_count = 0
        spawn_gate_survived_count = 0
        spawn_gate_explicit_pass_count = 0
        spawn_gate_fail_open_count = 0
        spawn_gate_unresolved_count = 0
        spawn_gate_manifest_excluded_fail_closed_count = 0
        spawn_gate_adversarial_give_up_count = 0
        spawn_gate_challenged_spawned = 0
        spawn_gate_challenged_merged = 0
        spawn_gate_unchallenged_spawned = 0
        spawn_gate_unchallenged_merged = 0
        spawn_gate_exhaustion_count = 0
        spawn_gate_refuted_reasons: List[str] = []
        _spawn_gate_seen_reasons: set[str] = set()
        spawn_gate_manifest_excluded_samples: List[Dict[str, str]] = []
        _spawn_gate_seen_manifest_excluded: set[tuple[str, str]] = set()
        spawn_gate_adversarial_give_up_samples: List[Dict[str, str]] = []
        _spawn_gate_seen_adversarial_give_up: set[str] = set()
        spawn_gate_parse_outcome_distribution: dict[str, int] = {}
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
                reachable_count = reachable_merged_count(entry)
                if reachable_count is not None:
                    reachable_prs_merged += reachable_count
                    reachable_prs_recorded = True
                proven_prs_merged += proven_merged_count(entry)
                consumed_prs_merged += entry.get("acceptance_consumed_merged", 0) or 0
                declared_prs_merged += declared_merged_count(entry)
                merged_write_total += entry.get("acceptance_merged_write_total", 0) or 0
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
                _merge_finding_challenge_summary(
                    dir_challenge_summary, entry.get("finding_challenge_summary")
                )
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
                spawn_gate_challenged_count += (
                    entry.get("research_spawn_gate_challenged_count") or 0
                )
                spawn_gate_refuted_count += (
                    entry.get("research_spawn_gate_refuted_count") or 0
                )
                spawn_gate_survived_count += (
                    entry.get("research_spawn_gate_survived_count") or 0
                )
                spawn_gate_explicit_pass_count += (
                    entry.get("research_spawn_gate_explicit_pass_count") or 0
                )
                spawn_gate_fail_open_count += (
                    entry.get("research_spawn_gate_fail_open_count") or 0
                )
                spawn_gate_unresolved_count += (
                    entry.get("research_spawn_gate_unresolved_count") or 0
                )
                spawn_gate_manifest_excluded_fail_closed_count += (
                    entry.get("research_spawn_gate_manifest_excluded_fail_closed_count")
                    or 0
                )
                spawn_gate_adversarial_give_up_count += (
                    entry.get("research_spawn_gate_adversarial_give_up_count") or 0
                )
                for _sg_reason in (
                    entry.get("research_spawn_gate_refuted_reasons") or []
                ):
                    _sg_reason = str(_sg_reason).strip()[:_SPAWN_GATE_REASON_MAX_LEN]
                    if (
                        _sg_reason
                        and _sg_reason not in _spawn_gate_seen_reasons
                        and len(spawn_gate_refuted_reasons)
                        < _SPAWN_GATE_REASONS_SAMPLE_CAP
                    ):
                        _spawn_gate_seen_reasons.add(_sg_reason)
                        spawn_gate_refuted_reasons.append(_sg_reason)
                for _sg_sample in (
                    entry.get(
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
                        and _sample_key not in _spawn_gate_seen_manifest_excluded
                        and len(spawn_gate_manifest_excluded_samples)
                        < _SPAWN_GATE_REASONS_SAMPLE_CAP
                    ):
                        _spawn_gate_seen_manifest_excluded.add(_sample_key)
                        spawn_gate_manifest_excluded_samples.append(_sample)
                for _gu_sample in (
                    entry.get("research_spawn_gate_adversarial_give_up_samples") or []
                ):
                    if not isinstance(_gu_sample, dict):
                        continue
                    _gu_title = str(_gu_sample.get("title") or "")[:120]
                    if (
                        _gu_title
                        and _gu_title not in _spawn_gate_seen_adversarial_give_up
                        and len(spawn_gate_adversarial_give_up_samples)
                        < _SPAWN_GATE_REASONS_SAMPLE_CAP
                    ):
                        _spawn_gate_seen_adversarial_give_up.add(_gu_title)
                        _gu_compact: Dict[str, str] = {"title": _gu_title}
                        if _gu_sample.get("direction_id"):
                            _gu_compact["direction_id"] = str(
                                _gu_sample["direction_id"]
                            )
                        spawn_gate_adversarial_give_up_samples.append(_gu_compact)
                _spawn_gate_parse_outcome = str(
                    entry.get("research_spawn_gate_parse_outcome") or ""
                ).strip()
                if _spawn_gate_parse_outcome:
                    spawn_gate_parse_outcome_distribution[_spawn_gate_parse_outcome] = (
                        spawn_gate_parse_outcome_distribution.get(
                            _spawn_gate_parse_outcome, 0
                        )
                        + 1
                    )
                spawn_gate_challenged_spawned += (
                    entry.get("research_spawn_gate_challenged_spawned") or 0
                )
                spawn_gate_challenged_merged += (
                    entry.get("research_spawn_gate_challenged_merged") or 0
                )
                spawn_gate_unchallenged_spawned += (
                    entry.get("research_spawn_gate_unchallenged_spawned") or 0
                )
                spawn_gate_unchallenged_merged += (
                    entry.get("research_spawn_gate_unchallenged_merged") or 0
                )
                spawn_gate_exhaustion_count += (
                    1
                    if entry.get("research_spawn_gate_wholesale_retry_exhausted")
                    else 0
                )
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
            t_cost = (
                get_vm_rate_per_second(
                    machine_type=t.__dict__.get("vm_machine_type"),
                    spot=bool(t.vm_spot),
                )
                * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            )
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
            # Use pair-atomic helper for provider/model breakdown so chimera keys
            # (e.g. openai|claude-opus-4-8 from independently-resolved columns)
            # cannot appear. Banked attempt_history costs are included via window_start.
            pm_costs = get_task_cost_by_provider_model(t, window_start=window_start)
            for pm_key, pm_cost in pm_costs.items():
                cost_by_provider_model[pm_key] = (
                    cost_by_provider_model.get(pm_key, 0.0) + pm_cost
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
        # Proven/declared lifetime totals — STRICTER than verified; legacy entries
        # and pre-aggregate folds contribute 0 (proven_merged_count /
        # declared_merged_count / 0-fallback), matching the strategic stat surface.
        lifetime_proven_prs_merged = sum(
            proven_merged_count(h) for h in _history_all
        ) + (_agg.get("total_acceptance_proven_merged", 0) or 0)
        lifetime_consumed_prs_merged = sum(
            h.get("acceptance_consumed_merged", 0) for h in _history_all
        ) + (_agg.get("total_acceptance_consumed_merged", 0) or 0)
        lifetime_declared_prs_merged = sum(
            declared_merged_count(h) for h in _history_all
        ) + (_agg.get("total_acceptance_declared_merged", 0) or 0)
        lifetime_merged_write_total = sum(
            h.get("acceptance_merged_write_total", 0) for h in _history_all
        ) + (_agg.get("total_acceptance_merged_write_total", 0) or 0)
        lifetime_acceptance_declaration_coverage = (
            round(lifetime_declared_prs_merged / lifetime_merged_write_total, 3)
            if lifetime_merged_write_total > 0
            else None
        )
        lifetime_prs_rejected = sum(h.get("prs_rejected", 0) for h in _history_all) + (
            _agg.get("total_prs_rejected") or 0
        )
        lifetime_tasks_created = sum(
            h.get("tasks_created", 0) for h in _history_all
        ) + (_agg.get("total_tasks_created") or 0)
        lifetime_pr_ineligible_tasks_created = sum(
            pr_ineligible_created_in_entry(h) for h in _history_all
        ) + pr_ineligible_created_in_aggregate(_agg)
        lifetime_pr_eligible_tasks_created = (
            lifetime_tasks_created - lifetime_pr_ineligible_tasks_created
        )
        lifetime_tasks_failed = sum(h.get("tasks_failed", 0) for h in _history_all) + (
            _agg.get("total_tasks_failed") or 0
        )
        lifetime_conversion_rate = (
            round(lifetime_verified_prs_merged / lifetime_pr_eligible_tasks_created, 3)
            if lifetime_pr_eligible_tasks_created > 0
            else None
        )
        # Autonomy lifetime totals + retirement eligibility — derived by the
        # single source of truth (compute_direction_autonomy_state) rather than
        # re-computed inline, so this digest surface can never silently disagree
        # with the strategist payload or the helper. It credits only verifiably-
        # landed autonomous merges toward the 10-merge milestone.
        _autonomy_state = compute_direction_autonomy_state(d)
        lifetime_autonomous_prs_merged = _autonomy_state[
            "lifetime_autonomous_prs_merged"
        ]
        lifetime_operator_actions = _autonomy_state["lifetime_operator_actions"]
        lifetime_generalized_state = _compute_lifetime_generalized_outcome_state(d)
        retirement_eligible = _autonomy_state["retirement_eligible"]
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
        # Window declaration coverage = declared / merged-write, recomputed from
        # summed counts (not averaged per-cycle fractions). None when no merged
        # write task exists in the window — never retro-failing legacy rows.
        acceptance_declaration_coverage = (
            round(declared_prs_merged / merged_write_total, 3)
            if merged_write_total > 0
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

        finding_challenge_summary = (
            dir_challenge_summary
            if dir_challenge_summary["challenged_findings"] > 0
            else None
        )

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

        _dir_fc = compute_format_compliance_metrics(tasks_for_dir)["overall"]
        dir_format_compliance_rate = _dir_fc["format_compliance_rate"]
        dir_self_repair_success_rate = _dir_fc["self_repair_success_rate"]
        spawn_gate_challenged_merge_rate = (
            round(spawn_gate_challenged_merged / spawn_gate_challenged_spawned, 3)
            if spawn_gate_challenged_spawned > 0
            else None
        )
        spawn_gate_unchallenged_merge_rate = (
            round(spawn_gate_unchallenged_merged / spawn_gate_unchallenged_spawned, 3)
            if spawn_gate_unchallenged_spawned > 0
            else None
        )
        research_spawn_gate = {
            "challenged_count": spawn_gate_challenged_count,
            "refuted_count": spawn_gate_refuted_count,
            "survived_count": spawn_gate_survived_count,
            # Decision-quality breakdown — additive over the legacy 9 fields.
            "explicit_pass_count": spawn_gate_explicit_pass_count,
            "fail_open_count": spawn_gate_fail_open_count,
            "unresolved_count": spawn_gate_unresolved_count,
            "manifest_excluded_fail_closed_count": (
                spawn_gate_manifest_excluded_fail_closed_count
            ),
            "manifest_excluded_fail_closed_samples": (
                spawn_gate_manifest_excluded_samples
            ),
            "adversarial_give_up_count": spawn_gate_adversarial_give_up_count,
            "adversarial_give_up_samples": spawn_gate_adversarial_give_up_samples,
            "challenged_spawned": spawn_gate_challenged_spawned,
            "challenged_merged": spawn_gate_challenged_merged,
            "challenged_merge_rate": spawn_gate_challenged_merge_rate,
            "unchallenged_spawned": spawn_gate_unchallenged_spawned,
            "unchallenged_merged": spawn_gate_unchallenged_merged,
            "unchallenged_merge_rate": spawn_gate_unchallenged_merge_rate,
            "exhaustion_count": spawn_gate_exhaustion_count,
            "refuted_reasons": spawn_gate_refuted_reasons,
            "parse_outcome_distribution": (
                spawn_gate_parse_outcome_distribution or None
            ),
        }
        # Drop the reasons sample from the all-zero test so a cycle with only an
        # (empty) sample still collapses to None — gate emptiness is decided by
        # the numeric counters, not the always-present sample list.
        if not any(
            v
            for k, v in research_spawn_gate.items()
            if k
            not in (
                "refuted_reasons",
                "manifest_excluded_fail_closed_samples",
                "adversarial_give_up_samples",
            )
        ):
            research_spawn_gate = None

        cost_budget_usd = d.get("cost_budget_usd")
        budget_utilization_pct = None
        budget_exceeded = False
        if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
            budget_utilization_pct = round((dir_cost / cost_budget_usd) * 100, 1)
            budget_exceeded = dir_cost > cost_budget_usd
            if budget_utilization_pct >= 80 and not d.get("_budget_alerted"):
                _budget_alert_direction_ids.append(did)
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
                    **(
                        {"reachable_prs_merged": reachable_prs_merged}
                        if reachable_prs_recorded
                        else {}
                    ),
                    # Proven count + declaration coverage: machine-checked subset
                    # of verified merges (proven <= verified) and how much merged
                    # code committed to forward evidence. Lifetime counterparts
                    # surfaced below alongside lifetime_verified_prs_merged.
                    "proven_prs_merged": proven_prs_merged,
                    "consumed_prs_merged": consumed_prs_merged,
                    "acceptance_declaration_coverage": acceptance_declaration_coverage,
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
                    "auto_pause_count": int(d.get("auto_pause_count") or 0),
                    "churn_family_pause_count": int(d.get("churn_family_pause_count") or 0),
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
                    "cost_per_merged_pr": canonical_cost_per_merged_pr(
                        dir_cost, prs_merged
                    ),
                    "scope_breakdown": dir_scope_agg or None,
                    "taxonomy_summary": dir_taxonomy_summary or None,
                    "stage_dropout": dir_stage_dropout or None,
                    "recent_failure_titles": recent_failed_titles[-3:] or None,
                    "filter_funnel": filter_funnel,
                    "finding_challenge_summary": finding_challenge_summary,
                    "decomposition_summary": decomposition_summary,
                    "first_attempt_success_rate": dir_fa_rate,
                    "first_attempt_sample_size": dir_fa_sample_size,
                    "format_compliance_rate": dir_format_compliance_rate,
                    "self_repair_success_rate": dir_self_repair_success_rate,
                    "research_spawn_gate": research_spawn_gate,
                    "quality_score": round(_compute_direction_quality_score(d), 3),
                    "lifetime_cycles": lifetime_cycles,
                    "lifetime_prs_merged": lifetime_prs_merged,
                    "lifetime_proven_prs_merged": lifetime_proven_prs_merged,
                    "lifetime_consumed_prs_merged": lifetime_consumed_prs_merged,
                    "lifetime_acceptance_declaration_coverage": (
                        lifetime_acceptance_declaration_coverage
                    ),
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

    if budget_alert_fired and _budget_alert_direction_ids:
        # Re-fetch the project row with FOR UPDATE (PostgreSQL) to avoid clobbering
        # concurrent locked writes. The digest reads an unlocked snapshot; writing
        # the whole directions column back from that snapshot would silently revert
        # any concurrent update that touched the same column.
        # On SQLite (tests), no concurrent writes exist so we write the already-
        # loaded snapshot directly — a re-fetch via select() causes StaticPool
        # connection conflicts when dedicated-session helpers are also in flight.
        try:
            from sqlmodel import select as _select
            from app.database import resolve_dialect_name

            _alert_set = set(_budget_alert_direction_ids)
            if resolve_dialect_name(session) == "postgresql":
                _write_target = (
                    await session.execute(
                        _select(Project)
                        .where(Project.id == project_id)
                        .with_for_update()
                    )
                ).scalars().first()
            else:
                _write_target = project
            if _write_target is not None:
                _any_stamped = False
                for _ld in _write_target.directions or []:
                    _lid = str(_ld.get("id") or _ld.get("direction_id") or "")
                    if _lid in _alert_set and not _ld.get("_budget_alerted"):
                        _ld["_budget_alerted"] = True
                        _any_stamped = True
                if _any_stamped:
                    flag_modified(_write_target, "directions")
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
            "auto_pause_epidemic_summary": _compute_auto_pause_epidemic_summary(
                active_directions
            ),
        },
    )


def compute_spawn_gate_census(
    tasks: List[Task],
    window_start: datetime,  # noqa: ARG001 — retained for caller symmetry
) -> Dict[str, Any]:
    """Project-wide windowed census of spawn-gate activity from research task contexts.

    Reads spawn-gate telemetry directly from each research task's ``context``
    rather than from direction ``research_history``, so undirected cycles are
    covered and multi-direction cycles are naturally deduplicated (one row per
    ``task.id``).

    Returns a census dict with per-cycle ``rows`` (one per research task) and
    project-level aggregate counters.
    """
    from app.failure_classification import GRADER_DIAGNOSTIC_TRAIL_KEY  # noqa: PLC0415

    rows: List[Dict[str, Any]] = []
    seen_task_ids: set[str] = set()

    gate_invoked_count = 0
    graders_attempted_total = 0
    parse_outcome_distribution: Dict[str, int] = {}
    cause_distribution: Dict[str, int] = {}
    exhaustion_count = 0
    give_up_count = 0

    for task in tasks:
        task_id = str(task.id)
        if task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)

        payload_type = (task.first_payload or {}).get("type")
        if payload_type != "research":
            continue

        ctx = task.context or {}
        if not isinstance(ctx, dict):
            ctx = {}

        challenged_count = int(ctx.get("research_spawn_gate_challenged_count") or 0)
        trail = ctx.get(GRADER_DIAGNOSTIC_TRAIL_KEY)
        if not isinstance(trail, list):
            trail = []

        gate_invoked = challenged_count > 0 or len(trail) > 0
        graders_attempted = challenged_count if challenged_count > 0 else len(trail)

        parse_outcome = (
            str(ctx.get("research_spawn_gate_parse_outcome") or "").strip() or None
        )

        raw_cause_counts = ctx.get("research_spawn_gate_cause_counts")
        task_cause_counts: Dict[str, int] = {}
        if isinstance(raw_cause_counts, dict):
            for cat, cnt in raw_cause_counts.items():
                cat = str(cat).strip()
                if cat:
                    task_cause_counts[cat] = int(cnt or 0)

        exhausted = bool(ctx.get("research_spawn_gate_wholesale_retry_exhausted"))

        _give_up_ctx = ctx.get("finding_structural_give_up") or {}
        task_give_up = int(
            (_give_up_ctx.get("by_reason") or {}).get("adversarial_gate", 0)
        )

        rows.append(
            {
                "task_id": task_id,
                "direction_id": ctx.get("direction_id") or None,
                "gate_invoked": gate_invoked,
                "graders_attempted": graders_attempted,
                "parse_outcome": parse_outcome,
                "cause_counts": task_cause_counts or None,
                "exhausted": exhausted,
                "give_up_count": task_give_up,
            }
        )

        if gate_invoked:
            gate_invoked_count += 1
        graders_attempted_total += graders_attempted
        if parse_outcome:
            parse_outcome_distribution[parse_outcome] = (
                parse_outcome_distribution.get(parse_outcome, 0) + 1
            )
        for cat, cnt in task_cause_counts.items():
            cause_distribution[cat] = cause_distribution.get(cat, 0) + cnt
        if exhausted:
            exhaustion_count += 1
        give_up_count += task_give_up

    return {
        "cycles_scanned": len(rows),
        "gate_invoked_count": gate_invoked_count,
        "graders_attempted_total": graders_attempted_total,
        "exhaustion_count": exhaustion_count,
        "give_up_count": give_up_count,
        "parse_outcome_distribution": parse_outcome_distribution or None,
        "cause_distribution": cause_distribution or None,
        "rows": rows,
    }
