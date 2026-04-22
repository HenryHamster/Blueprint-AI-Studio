"""Strategic analysis helpers — pure, synchronous, no DB access.

Extracted from app.strategic_completion to keep that module under its line ceiling.
"""
from datetime import datetime, timezone
from app.time_utils import _utcnow


def _build_outcome_evaluation(project) -> dict:
    """Build a planned-vs-actual assessment for the strategic agent.

    Compares what the vision predicted against what directions actually produced.
    All data lives on project.directions and project.vision — no DB queries needed.
    """
    from app.research_context import _parse_research_context  # noqa: PLC0415
    from app.direction_metrics import _compute_difficulty_index  # noqa: PLC0415
    from app.routers.admin_analytics import ON_DEMAND_RATE  # noqa: PLC0415
    vision = project.vision or {}
    directions = project.directions or []
    now_iso = _utcnow().isoformat()

    # Determine evaluation window: since last vision update, or all time
    window_from = vision.get("last_updated")
    vision_rev = vision.get("revision", 0)

    # Per-direction outcomes
    direction_outcomes = []
    for d in directions:
        status = d.get("status", "active")
        if status not in ("active", "expired", "paused"):
            continue
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})

        # Filter history to evaluation window
        if window_from:
            window_history = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            window_history = list(history)

        cycles = len(window_history)
        merged = sum(h.get("prs_merged", 0) for h in window_history)
        rejected = sum(h.get("prs_rejected", 0) for h in window_history)
        failed = sum(h.get("tasks_failed", 0) for h in window_history)
        failed_infra = sum(h.get("tasks_failed_infra", 0) for h in window_history)
        taxonomy_summary: dict[str, int] = {}
        for h in window_history:
            for tkey, tval in (h.get("taxonomy_summary") or {}).items():
                if tval:
                    taxonomy_summary[tkey] = taxonomy_summary.get(tkey, 0) + tval

        # Extract confirmed vs proposed findings from research_context
        confirmed = []
        proposed = []
        failed_approaches = []
        ctx = d.get("research_context", "")
        if ctx:
            parsed = _parse_research_context(ctx)
            for item in parsed.get("established", []):
                if "(confirmed)" in item:
                    confirmed.append(item.replace("(confirmed)", "").strip())
                elif "(proposed)" in item:
                    proposed.append(item.replace("(proposed)", "").strip())
                else:
                    confirmed.append(item)  # Unmarked items are treated as confirmed
            failed_approaches = parsed.get("failed", [])

        total_prs = merged + rejected
        acceptance_rate = round(merged / total_prs, 3) if total_prs > 0 else None
        # avg_time_to_merge_hours: average of per-cycle values where present
        merge_time_values = [
            h["avg_time_to_merge_hours"]
            for h in window_history
            if h.get("avg_time_to_merge_hours") is not None
        ]
        avg_time_to_merge_hours = round(sum(merge_time_values) / len(merge_time_values), 2) if merge_time_values else None

        outcome: dict = {
            "id": d.get("id"),
            "text": (d.get("text") or "")[:200],
            "system_id": d.get("system_id"),
            "status": status,
            "cycles": cycles,
            "merged": merged,
            "failed": failed,
            "prs_rejected": rejected,
        }
        if failed_infra > 0:
            outcome["tasks_failed_infra"] = failed_infra
        if taxonomy_summary:
            outcome["taxonomy_summary"] = taxonomy_summary
        if acceptance_rate is not None:
            outcome["acceptance_rate"] = acceptance_rate
        if avg_time_to_merge_hours is not None:
            outcome["avg_time_to_merge_hours"] = avg_time_to_merge_hours
        outcome["difficulty_index"] = round(_compute_difficulty_index(d), 2)
        # Estimated cost: sum(tasks_created * avg_time_to_merge_hours) * 3600 * ON_DEMAND_RATE
        # avg_time_to_merge_hours is a per-cycle proxy for typical task VM runtime.
        cost_seconds = 0.0
        for h in window_history:
            tcount = h.get("tasks_created", 0) or 0
            hours = h.get("avg_time_to_merge_hours")
            if tcount and hours:
                cost_seconds += tcount * hours * 3600
        estimated_cost_usd = round(cost_seconds * ON_DEMAND_RATE, 2)
        if estimated_cost_usd > 0:
            outcome["estimated_cost_usd"] = estimated_cost_usd
            if merged > 0:
                outcome["cost_per_merged_pr"] = round(estimated_cost_usd / merged, 2)
        if rejected > 0:
            total_prs = merged + rejected
            outcome["operator_rejection_rate"] = round(rejected / total_prs, 2) if total_prs > 0 else None
        if confirmed:
            outcome["confirmed"] = confirmed[:15]
        if proposed:
            outcome["proposed"] = proposed[:10]
        if failed_approaches:
            outcome["failed_approaches"] = failed_approaches[:10]
        # Include lifetime totals for context
        lifetime_cycles = len(history) + (agg.get("total_cycles") or 0)
        lifetime_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
        lifetime_rejected = sum(h.get("prs_rejected", 0) for h in history) + (agg.get("total_prs_rejected") or 0)
        if lifetime_cycles > cycles:
            lifetime_entry: dict = {"cycles": lifetime_cycles, "merged": lifetime_merged}
            if lifetime_rejected > 0:
                lifetime_entry["rejected"] = lifetime_rejected
            outcome["lifetime"] = lifetime_entry

        # Aggregate type distribution across window cycles
        type_dist: dict = {}
        product_weighted = 0.0
        for cycle_entry in window_history:
            for t, count in cycle_entry.get("type_distribution", {}).items():
                type_dist[t] = type_dist.get(t, 0) + count
            tasks_in_cycle = cycle_entry.get("tasks_created", 0)
            product_weighted += cycle_entry.get("product_ratio", 0.0) * tasks_in_cycle
        total_typed = sum(type_dist.values())
        if type_dist:
            outcome["type_distribution"] = type_dist
            outcome["product_ratio"] = round(product_weighted / total_typed, 2) if total_typed > 0 else None

        # Aggregate scope_breakdown + escalation_outcomes across window cycles.
        # Compact form: per-scope {created, merged, failed}; escalation_outcomes
        # merged across cycles. Skip scopes with no data so the prompt stays small.
        scope_perf: dict = {}
        escalations_agg: dict = {}
        for cycle_entry in window_history:
            for scope_name, stats in (cycle_entry.get("scope_breakdown") or {}).items():
                if not isinstance(stats, dict):
                    continue
                created_n = stats.get("created", 0) or 0
                merged_n = stats.get("merged", 0) or 0
                failed_n = stats.get("failed", 0) or 0
                if not (created_n or merged_n or failed_n):
                    continue
                bucket = scope_perf.setdefault(
                    scope_name, {"created": 0, "merged": 0, "failed": 0}
                )
                bucket["created"] += created_n
                bucket["merged"] += merged_n
                bucket["failed"] += failed_n
            for esc_key, esc_stats in (cycle_entry.get("escalation_outcomes") or {}).items():
                if not isinstance(esc_stats, dict):
                    continue
                eb = escalations_agg.setdefault(
                    esc_key, {"attempted": 0, "merged": 0, "failed": 0}
                )
                eb["attempted"] += esc_stats.get("attempted", 0) or 0
                eb["merged"] += esc_stats.get("merged", 0) or 0
                eb["failed"] += esc_stats.get("failed", 0) or 0
        if scope_perf:
            outcome["scope_performance"] = scope_perf
        if escalations_agg:
            outcome["escalation_outcomes"] = escalations_agg

        # Per-direction trend (first-half vs second-half merge rate over window history)
        if cycles >= 4:
            sorted_window = sorted(window_history, key=lambda h: h.get("timestamp") or "")
            mid_d = len(sorted_window) // 2
            first_d = sorted_window[:mid_d]
            second_d = sorted_window[mid_d:]
            first_rate_d = sum(h.get("prs_merged", 0) for h in first_d) / max(len(first_d), 1)
            second_rate_d = sum(h.get("prs_merged", 0) for h in second_d) / max(len(second_d), 1)
            if second_rate_d > first_rate_d * 1.3:
                outcome["trend"] = "improving"
            elif second_rate_d < first_rate_d * 0.7:
                outcome["trend"] = "declining"
            else:
                outcome["trend"] = "stable"

        # Per-direction velocity: recent-2-cycle merge rate vs lifetime average
        if lifetime_cycles >= 2 and len(window_history) >= 2:
            sorted_full = sorted(history, key=lambda h: h.get("timestamp") or "")
            recent = sorted_full[-2:] if len(sorted_full) >= 2 else sorted_full
            recent_rate = sum(h.get("prs_merged", 0) for h in recent) / max(len(recent), 1)
            lifetime_rate = lifetime_merged / lifetime_cycles if lifetime_cycles > 0 else 0
            if lifetime_rate > 0:
                outcome["velocity"] = round(recent_rate / lifetime_rate, 2)

        direction_outcomes.append(outcome)

    # Overall health
    total_cycles = sum(d["cycles"] for d in direction_outcomes)
    total_merged = sum(d["merged"] for d in direction_outcomes)
    total_failed = sum(d["failed"] for d in direction_outcomes)
    success_rate = round(total_merged / max(total_merged + total_failed, 1), 2)

    # Trend: compare first half vs second half of all window history entries
    all_window_entries = []
    for d in directions:
        history = d.get("research_history", [])
        if window_from:
            entries = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            entries = list(history)
        all_window_entries.extend(entries)
    all_window_entries.sort(key=lambda h: h.get("timestamp") or "")
    trend = "stable"
    if len(all_window_entries) >= 4:
        mid = len(all_window_entries) // 2
        first_half = all_window_entries[:mid]
        second_half = all_window_entries[mid:]
        first_rate = sum(h.get("prs_merged", 0) for h in first_half) / max(len(first_half), 1)
        second_rate = sum(h.get("prs_merged", 0) for h in second_half) / max(len(second_half), 1)
        if second_rate > first_rate * 1.3:
            trend = "improving"
        elif second_rate < first_rate * 0.7:
            trend = "declining"

    # System-level status — cross-reference vision systems with direction outcomes
    systems = []
    vision_systems = vision.get("systems", [])
    if isinstance(vision_systems, list):
        for sys in vision_systems:
            if not isinstance(sys, dict):
                continue
            sys_id = sys.get("id")
            if not sys_id:
                continue
            # Find directions mapped to this system
            mapped = [d for d in direction_outcomes if d.get("system_id") == sys_id]
            sys_merged = sum(d["merged"] for d in mapped)
            sys_failed = sum(d["failed"] for d in mapped)
            systems.append({
                "id": sys_id,
                "name": sys.get("name", ""),
                "planned_status": sys.get("status", "unknown"),
                "directions_count": len(mapped),
                "merged": sys_merged,
                "failed": sys_failed,
            })

    # Overall type distribution across all directions
    overall_type_dist: dict = {}
    overall_product_weighted = 0.0
    for d_out in direction_outcomes:
        for t, count in d_out.get("type_distribution", {}).items():
            overall_type_dist[t] = overall_type_dist.get(t, 0) + count
        tasks_count = sum(d_out.get("type_distribution", {}).values())
        if d_out.get("product_ratio") is not None:
            overall_product_weighted += d_out["product_ratio"] * tasks_count
    overall_total_typed = sum(overall_type_dist.values())
    overall_product_ratio = round(overall_product_weighted / overall_total_typed, 2) if overall_total_typed > 0 else None

    overall_summary: dict = {
        "cycles": total_cycles, "merged": total_merged, "failed": total_failed,
        "success_rate": success_rate, "trend": trend,
    }
    if overall_type_dist:
        overall_summary["type_distribution"] = overall_type_dist
    if overall_product_ratio is not None:
        overall_summary["product_ratio"] = overall_product_ratio

    # System-level cost summary and efficiency ranking
    total_cost_usd = round(sum(d.get("estimated_cost_usd", 0.0) for d in direction_outcomes), 2)
    if total_cost_usd > 0:
        overall_summary["estimated_cost_usd"] = total_cost_usd
        ranked = [
            {"id": d["id"], "cost_per_merged_pr": d["cost_per_merged_pr"]}
            for d in direction_outcomes if d.get("cost_per_merged_pr") is not None
        ]
        ranked.sort(key=lambda x: x["cost_per_merged_pr"])
        if ranked:
            overall_summary["cost_efficiency_ranking"] = ranked

    result: dict = {
        "window": {"from": window_from or "all_time", "to": now_iso, "vision_revision": vision_rev},
        "overall": overall_summary,
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    strategic_effectiveness = _build_strategic_effectiveness(directions, vision)
    if strategic_effectiveness:
        result["strategic_effectiveness"] = strategic_effectiveness
    decision_accuracy = _build_decision_accuracy(directions, vision)
    if decision_accuracy:
        result["decision_accuracy"] = decision_accuracy
    return result


def _build_decision_accuracy(directions: list, vision: dict) -> dict:
    """Score the most recent completed strategic cycle's create/retire decisions.

    Looks up each created direction's actual merge outcome and quality score,
    and each retired direction's current status, then summarizes how accurate
    the strategic agent's last set of decisions turned out to be.
    """
    from app.direction_metrics import _compute_direction_quality_score  # noqa: PLC0415
    cycle_history = list((vision or {}).get("_strategic_cycle_history") or [])
    target = None
    for entry in reversed(cycle_history):
        if entry.get("outcome") != "completed":
            continue
        if entry.get("created_direction_ids") or entry.get("retired_direction_ids"):
            target = entry
            break
    if target is None:
        return {}

    dir_by_id = {d.get("id"): d for d in directions if d.get("id")}

    def _lifetime_merged(d: dict) -> int:
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        return sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)

    quality_scores = [
        round(_compute_direction_quality_score(d), 3)
        for d in directions if d.get("id")
    ]
    quality_scores_sorted = sorted(quality_scores)
    median_quality = 0.0
    if quality_scores_sorted:
        n = len(quality_scores_sorted)
        if n % 2 == 1:
            median_quality = quality_scores_sorted[n // 2]
        else:
            median_quality = (quality_scores_sorted[n // 2 - 1] + quality_scores_sorted[n // 2]) / 2

    created_breakdown: list = []
    successes = 0
    for cid in (target.get("created_direction_ids") or []):
        d = dir_by_id.get(cid)
        if d is None:
            created_breakdown.append({"direction_id": cid, "status": "missing", "success": False})
            continue
        merged = _lifetime_merged(d)
        auto_paused = bool(d.get("auto_paused"))
        qscore = round(_compute_direction_quality_score(d), 3)
        above_median = qscore > median_quality
        success = merged >= 1 and not auto_paused
        if success:
            successes += 1
        created_breakdown.append({
            "direction_id": cid,
            "prs_merged": merged,
            "auto_paused": auto_paused,
            "quality_score": qscore,
            "above_median_quality": above_median,
            "success": success,
        })

    retired_breakdown: list = []
    retired_good = 0
    retired_total = 0
    for rid in (target.get("retired_direction_ids") or []):
        retired_total += 1
        d = dir_by_id.get(rid)
        if d is None:
            retired_breakdown.append({"direction_id": rid, "stayed_retired": True, "status": "missing"})
            retired_good += 1
            continue
        status = d.get("status")
        stayed = status in ("retired", "expired")
        if stayed:
            retired_good += 1
        retired_breakdown.append({
            "direction_id": rid,
            "status": status,
            "stayed_retired": stayed,
        })

    created_total = len(created_breakdown)
    decision_accuracy_rate = (
        round(successes / created_total, 2) if created_total else None
    )
    retired_accuracy_rate = (
        round(retired_good / retired_total, 2) if retired_total else None
    )

    result: dict = {
        "cycle_id": target.get("cycle_id"),
        "completed_at": target.get("phase2_completed_at"),
        "created_count": created_total,
        "created_successful": successes,
        "decision_accuracy_rate": decision_accuracy_rate,
        "median_quality_score": round(median_quality, 3),
        "created": created_breakdown,
    }
    if retired_breakdown:
        result["retired"] = retired_breakdown
        result["retired_accuracy_rate"] = retired_accuracy_rate
    return result


def _build_strategic_effectiveness(directions: list, vision: dict) -> dict:
    """Aggregate per-cycle strategic decision quality from _strategic_cycle_history.

    Measures whether the strategic agent's own choices (which directions to
    create/retire) are producing better outcomes across recent cycles. Distinct
    from per-direction trends, which measure direction performance.
    """
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    completed = [
        c for c in cycle_history
        if c.get("outcome") not in (None, "in_progress")
    ]
    if not completed:
        return {}

    # Index directions by id for quick lookup of current status / merge counts.
    dir_by_id: dict = {d.get("id"): d for d in directions if d.get("id")}

    def _direction_lifetime_merged(d: dict) -> int:
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        return sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)

    recent = completed[-5:]
    cycle_metrics: list = []
    for entry in recent:
        created_ids = list(entry.get("created_direction_ids") or [])
        retired_ids = list(entry.get("retired_direction_ids") or [])
        created_with_merges = sum(
            1 for cid in created_ids
            if cid in dir_by_id and _direction_lifetime_merged(dir_by_id[cid]) > 0
        )
        retired_still_retired = sum(
            1 for rid in retired_ids
            if rid in dir_by_id and dir_by_id[rid].get("status") in ("retired", "expired")
        )
        decisions_total = len(created_ids) + len(retired_ids)
        successful = created_with_merges + retired_still_retired
        conversion_rate = round(successful / decisions_total, 2) if decisions_total else None
        cycle_metrics.append({
            "cycle_id": entry.get("cycle_id"),
            "completed_at": entry.get("phase2_completed_at"),
            "directions_created": len(created_ids),
            "directions_retired": len(retired_ids),
            "created_with_merges": created_with_merges,
            "retired_still_retired": retired_still_retired,
            "vision_updated": bool(entry.get("vision_updated")),
            "decision_conversion_rate": conversion_rate,
        })

    # Trend across cycles: average decision_conversion_rate, first half vs second half.
    rates = [c["decision_conversion_rate"] for c in cycle_metrics if c["decision_conversion_rate"] is not None]
    trend = "insufficient_data"
    if len(rates) >= 4:
        mid = len(rates) // 2
        first_avg = sum(rates[:mid]) / max(mid, 1)
        second_avg = sum(rates[mid:]) / max(len(rates) - mid, 1)
        if second_avg > first_avg * 1.3:
            trend = "improving"
        elif second_avg < first_avg * 0.7:
            trend = "declining"
        else:
            trend = "stable"
    elif rates:
        trend = "stable"

    avg_rate = round(sum(rates) / len(rates), 2) if rates else None

    return {
        "cycles_evaluated": len(cycle_metrics),
        "avg_decision_conversion_rate": avg_rate,
        "trend": trend,
        "strategic_failures": vision.get("_strategic_failures", 0),
        "cycles": cycle_metrics,
    }


def _compute_prediction_calibration_factor(
    project, lookback_cycles: int = 5, min_samples: int = 3
) -> float:
    """Compute a scaling factor to correct systematic bias in merge predictions.

    Walks the last ``lookback_cycles`` completed strategic cycles that have
    ``created_predictions`` stored. For each direction whose prediction included
    a non-zero ``similar_past_merges`` value, computes the ratio of actual
    current merges to that predicted value. The mean ratio across all such
    data points is the raw factor.

    When fewer than ``min_samples`` data points are available, the result is
    blended toward 1.0 proportionally so that sparse history does not produce
    wild swings. The final value is clipped to [0.5, 1.5].

    Returns:
        float < 1.0  — predictions have been optimistic (over-predicting merges)
        float == 1.0 — no history or predictions are well-calibrated
        float > 1.0  — predictions have been pessimistic (under-predicting merges)
    """
    cycle_history = (project.vision or {}).get("_strategic_cycle_history") or []
    directions_by_id = {
        d.get("id"): d for d in (project.directions or []) if d.get("id")
    }

    ratios: list = []
    cycles_seen = 0

    for entry in reversed(cycle_history):
        if entry.get("outcome") != "completed":
            continue
        created_predictions = entry.get("created_predictions") or {}
        if not created_predictions:
            continue
        cycles_seen += 1
        if cycles_seen > lookback_cycles:
            break
        for direction_id, prediction in created_predictions.items():
            predicted = prediction.get("similar_past_merges") or 0
            if predicted <= 0:
                continue
            d = directions_by_id.get(direction_id)
            if d is None:
                continue
            history = d.get("research_history") or []
            agg = d.get("history_aggregate") or {}
            actual = (
                sum(h.get("prs_merged", 0) for h in history)
                + (agg.get("total_prs_merged") or 0)
            )
            ratios.append(actual / predicted)

    if not ratios:
        return 1.0

    raw = sum(ratios) / len(ratios)
    n = len(ratios)
    if n < min_samples:
        weight = n / min_samples
        raw = weight * raw + (1.0 - weight) * 1.0

    return max(0.5, min(1.5, round(raw, 4)))
