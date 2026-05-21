"""Strategic analysis helpers — pure, synchronous, no DB access.

Extracted from app.strategic_completion to keep that module under its line ceiling.
"""

from datetime import timedelta

from app.time_utils import _utcnow

# Directions whose cost_per_merged_pr exceeds median * multiplier are flagged
# for retirement consideration by the strategic agent.
_HIGH_COST_PER_MERGE_MULTIPLIER = 2.0

# Caps for _build_retired_directions_summary. 90d keeps context relevant;
# 800 chars/entry lets the agent recognize overlap without inflating the prompt.
_RETIRED_WINDOW_DAYS = 90
_RETIRED_MAX_COUNT = 20
_RETIRED_PER_ENTRY_CHAR_CAP = 800


def _build_retired_directions_summary(
    project,
    window_days: int = _RETIRED_WINDOW_DAYS,
    max_count: int = _RETIRED_MAX_COUNT,
    per_entry_char_cap: int = _RETIRED_PER_ENTRY_CHAR_CAP,
) -> list[dict]:
    """Summarize recently retired directions so the strategic agent avoids
    re-proposing semantically duplicate work.

    Includes directions with status in {retired, completed, superseded} or
    expired directions that have a final-disposition marker
    (auto_completed_reason). Caps at max_count entries within window_days,
    with per_entry_char_cap chars per JSON-serialized entry so the summary
    never blows the prompt budget.

    Cap rationale: 90d window is long enough to cover recent strategic cycles
    while excluding ancient history; 800 chars/entry fits id + text + metrics
    + top failed_approaches; 20 entries covers active projects without
    exceeding a ~16 KB total footprint.
    """
    import json as _json

    from app.research_context import _parse_research_context
    from app.routers.admin_analytics import ON_DEMAND_RATE

    directions = project.directions or []
    cutoff_iso = (_utcnow() - timedelta(days=window_days)).isoformat()

    retired_statuses = {"retired", "completed", "superseded"}
    results: list[dict] = []

    for d in directions:
        status = d.get("status", "")
        is_expired_with_disposition = status == "expired" and bool(
            d.get("auto_completed_reason")
        )
        if status not in retired_statuses and not is_expired_with_disposition:
            continue

        # Time-window filter: use retired_at, auto_completed_at, or last history entry
        retired_at = d.get("retired_at") or d.get("auto_completed_at")
        if not retired_at:
            history_list = d.get("research_history") or []
            retired_at = history_list[-1].get("timestamp") if history_list else None
        if retired_at and retired_at < cutoff_iso:
            continue

        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        total_cycles = len(history) + (agg.get("total_cycles") or 0)
        total_merged = sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )
        total_rejected = sum(h.get("prs_rejected", 0) for h in history) + (
            agg.get("total_prs_rejected") or 0
        )

        # Dominant taxonomy across lifetime
        taxonomy_totals: dict[str, int] = {}
        for h in history:
            for tkey, tval in (h.get("taxonomy_summary") or {}).items():
                if tval:
                    taxonomy_totals[tkey] = taxonomy_totals.get(tkey, 0) + tval
        dominant_taxonomy = (
            max(taxonomy_totals, key=lambda k: taxonomy_totals[k])
            if taxonomy_totals
            else None
        )

        # Estimated cost per merge
        cost_seconds = 0.0
        for h in history:
            tcount = h.get("tasks_created", 0) or 0
            hours = h.get("avg_time_to_merge_hours")
            if tcount and hours:
                cost_seconds += tcount * hours * 3600
        estimated_cost_usd = round(cost_seconds * ON_DEMAND_RATE, 2)
        cost_per_merge = (
            round(estimated_cost_usd / total_merged, 2)
            if total_merged > 0 and estimated_cost_usd > 0
            else None
        )

        # Top failed approaches from research_context
        ctx = d.get("research_context", "")
        failed_approaches: list[str] = []
        if ctx:
            parsed = _parse_research_context(ctx)
            failed_approaches = parsed.get("failed", [])[:5]

        # Most recent 1-2 history entries (compact)
        recent_history = [
            {
                "timestamp": h.get("timestamp"),
                "prs_merged": h.get("prs_merged", 0),
                "prs_rejected": h.get("prs_rejected", 0),
                "tasks_failed": h.get("tasks_failed", 0),
            }
            for h in history[-2:]
        ]

        entry: dict = {
            "id": d.get("id"),
            "text": (d.get("text") or "")[:200],
            "status": status,
            "cycles": total_cycles,
            "prs_merged": total_merged,
        }
        if total_rejected > 0:
            entry["prs_rejected"] = total_rejected
        if d.get("retirement_reason"):
            entry["retirement_reason"] = str(d["retirement_reason"])[:200]
        if d.get("auto_completed_reason"):
            entry["auto_completed_reason"] = str(d["auto_completed_reason"])[:200]
        if cost_per_merge is not None:
            entry["cost_per_merge"] = cost_per_merge
        if dominant_taxonomy:
            entry["dominant_taxonomy"] = dominant_taxonomy
        if failed_approaches:
            entry["failed_approaches"] = failed_approaches
        if recent_history:
            entry["recent_history"] = recent_history

        # Apply per-entry char cap: trim optional bulky fields first
        if len(_json.dumps(entry)) > per_entry_char_cap:
            entry.pop("failed_approaches", None)
            entry.pop("recent_history", None)
        if len(_json.dumps(entry)) > per_entry_char_cap:
            entry["text"] = entry["text"][:100]

        results.append(entry)
        if len(results) >= max_count:
            break

    # Newest retired first (best proxy: sort by id descending is unreliable;
    # use retired_at / last history timestamp via original iteration order, or
    # sort by prs_merged descending as a stable tiebreak — not time-ordered
    # but good enough for the agent's dedup use case).
    return results


def _index_findings_by_direction(findings_rows: list) -> dict:
    """Group query_findings_index rows by direction_id for O(1) per-direction lookup.

    Returns {direction_id: [row, ...]} suitable for passing to
    _build_outcome_evaluation as findings_by_direction.  Rows with no
    direction_id are keyed under "_unlinked" and are never injected into a
    direction outcome.
    """
    by_dir: dict = {}
    for row in findings_rows:
        did = row.get("direction_id") or "_unlinked"
        by_dir.setdefault(did, []).append(row)
    return by_dir


def _build_outcome_evaluation(
    project,
    cycle_history: list | None = None,
    findings_by_direction: dict | None = None,
) -> dict:
    """Build a planned-vs-actual assessment for the strategic agent.

    Compares what the vision predicted against what directions actually produced.
    All data lives on project.directions and project.vision — no DB queries needed.

    Audit W2: ``cycle_history`` is now passed in (was JSONB on vision); the
    async caller pre-fetches via ``strategic_cycle.recent_for_agent_context``.
    Falls back to an empty list if the caller didn't provide one — the
    sub-helpers handle that gracefully.
    """
    from app.research_context import _parse_research_context
    from app.direction_metrics import _compute_difficulty_index
    from app.routers.admin_analytics import ON_DEMAND_RATE

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
            window_history = [
                h for h in history if (h.get("timestamp") or "") >= window_from
            ]
        else:
            window_history = list(history)

        cycles = len(window_history)
        merged = sum(h.get("prs_merged", 0) for h in window_history)
        # For CPM, count only autonomous merges; legacy entries without the field
        # are treated as all-autonomous (conservative fallback for old data).
        autonomous_merged = sum(
            h.get("autonomous_prs_merged", h.get("prs_merged", 0))
            for h in window_history
        )
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
        avg_time_to_merge_hours = (
            round(sum(merge_time_values) / len(merge_time_values), 2)
            if merge_time_values
            else None
        )

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
        cost_budget_usd = d.get("cost_budget_usd")
        if estimated_cost_usd > 0:
            outcome["estimated_cost_usd"] = estimated_cost_usd
            if autonomous_merged > 0:
                outcome["cost_per_merged_pr"] = round(
                    estimated_cost_usd / autonomous_merged, 2
                )
        if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
            outcome["cost_budget_usd"] = cost_budget_usd
            if estimated_cost_usd > cost_budget_usd:
                outcome["budget_exceeded"] = True
        if rejected > 0:
            total_prs = merged + rejected
            outcome["operator_rejection_rate"] = (
                round(rejected / total_prs, 2) if total_prs > 0 else None
            )
        # Surface the stated outcome goal alongside the planned-vs-actual metrics
        # so the strategic agent can judge progress against success_evidence /
        # retirement_condition. Mirror the write-side cap from main.py.
        _DIR_FIELD_MAX = 500
        dir_context = d.get("context") or {}
        _success_evidence = str(dir_context.get("success_evidence") or "").strip()
        if _success_evidence:
            outcome["success_evidence"] = _success_evidence[:_DIR_FIELD_MAX]
        _retirement_condition = str(
            dir_context.get("retirement_condition") or ""
        ).strip()
        if _retirement_condition:
            outcome["retirement_condition"] = _retirement_condition[:_DIR_FIELD_MAX]
        # Advisory outcome-less-scaffolding flag (see _check_outcome_scaffolding):
        # surface it in the planned-vs-actual output so the strategic cycle treats
        # it as a first-class signal alongside success_evidence / retirement_condition.
        if d.get("outcome_scaffolding_flagged"):
            outcome["outcome_scaffolding_flagged"] = True
            _osc_ev = d.get("outcome_scaffolding_evidence")
            if _osc_ev:
                outcome["outcome_scaffolding_evidence"] = _osc_ev
        if confirmed:
            outcome["confirmed"] = confirmed[:15]
        if proposed:
            outcome["proposed"] = proposed[:10]
        if failed_approaches:
            outcome["failed_approaches"] = failed_approaches[:10]
        # Include recent diagnosis if available
        diagnosis_summary = (d.get("diagnosis_summary") or "").strip()
        if diagnosis_summary:
            diagnosis_at = d.get("diagnosis_completed_at")
            recent_diagnosis = {"summary": diagnosis_summary[:500]}
            if diagnosis_at:
                recent_diagnosis["diagnosed_at"] = diagnosis_at
            if d.get("diagnosed_affected_files"):
                recent_diagnosis["affected_files"] = d.get("diagnosed_affected_files")[
                    :5
                ]
            outcome["recent_diagnosis"] = recent_diagnosis
        raw_evidence = d.get("recent_findings_evidence") or []
        if raw_evidence:
            from app.research_context import filter_direction_evidence

            filtered = filter_direction_evidence(ctx, raw_evidence)
            if filtered:
                outcome["evidence"] = filtered
        if findings_by_direction is not None:
            live_rows = findings_by_direction.get(d.get("id") or "", [])
            if live_rows:
                outcome["recent_findings"] = live_rows[:5]
        # Include lifetime totals for context
        lifetime_cycles = len(history) + (agg.get("total_cycles") or 0)
        lifetime_merged = sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )
        lifetime_rejected = sum(h.get("prs_rejected", 0) for h in history) + (
            agg.get("total_prs_rejected") or 0
        )
        if lifetime_cycles > cycles:
            lifetime_entry: dict = {
                "cycles": lifetime_cycles,
                "merged": lifetime_merged,
            }
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
            outcome["product_ratio"] = (
                round(product_weighted / total_typed, 2) if total_typed > 0 else None
            )

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
            for esc_key, esc_stats in (
                cycle_entry.get("escalation_outcomes") or {}
            ).items():
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
            sorted_window = sorted(
                window_history, key=lambda h: h.get("timestamp") or ""
            )
            mid_d = len(sorted_window) // 2
            first_d = sorted_window[:mid_d]
            second_d = sorted_window[mid_d:]
            first_rate_d = sum(h.get("prs_merged", 0) for h in first_d) / max(
                len(first_d), 1
            )
            second_rate_d = sum(h.get("prs_merged", 0) for h in second_d) / max(
                len(second_d), 1
            )
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
            recent_rate = sum(h.get("prs_merged", 0) for h in recent) / max(
                len(recent), 1
            )
            lifetime_rate = (
                lifetime_merged / lifetime_cycles if lifetime_cycles > 0 else 0
            )
            if lifetime_rate > 0:
                outcome["velocity"] = round(recent_rate / lifetime_rate, 2)

        direction_outcomes.append(outcome)

    # Flag directions with cost_per_merged_pr well above project median.
    # Requires at least 2 data points so the median is meaningful.
    _cpm_values = sorted(
        d["cost_per_merged_pr"]
        for d in direction_outcomes
        if d.get("cost_per_merged_pr") is not None
    )
    if len(_cpm_values) >= 2:
        n = len(_cpm_values)
        _cpm_median = (
            _cpm_values[n // 2]
            if n % 2 == 1
            else (_cpm_values[n // 2 - 1] + _cpm_values[n // 2]) / 2
        )
        _cpm_threshold = _cpm_median * _HIGH_COST_PER_MERGE_MULTIPLIER
        for _d_out in direction_outcomes:
            cpm = _d_out.get("cost_per_merged_pr")
            if cpm is not None and cpm > _cpm_threshold:
                _d_out["high_cost_per_merge"] = True

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
        first_rate = sum(h.get("prs_merged", 0) for h in first_half) / max(
            len(first_half), 1
        )
        second_rate = sum(h.get("prs_merged", 0) for h in second_half) / max(
            len(second_half), 1
        )
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
            systems.append(
                {
                    "id": sys_id,
                    "name": sys.get("name", ""),
                    "planned_status": sys.get("status", "unknown"),
                    "directions_count": len(mapped),
                    "merged": sys_merged,
                    "failed": sys_failed,
                }
            )

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
    overall_product_ratio = (
        round(overall_product_weighted / overall_total_typed, 2)
        if overall_total_typed > 0
        else None
    )

    overall_summary: dict = {
        "cycles": total_cycles,
        "merged": total_merged,
        "failed": total_failed,
        "success_rate": success_rate,
        "trend": trend,
    }
    if overall_type_dist:
        overall_summary["type_distribution"] = overall_type_dist
    if overall_product_ratio is not None:
        overall_summary["product_ratio"] = overall_product_ratio

    # System-level cost summary and efficiency ranking
    total_cost_usd = round(
        sum(d.get("estimated_cost_usd", 0.0) for d in direction_outcomes), 2
    )
    if total_cost_usd > 0:
        overall_summary["estimated_cost_usd"] = total_cost_usd
        ranked = [
            {"id": d["id"], "cost_per_merged_pr": d["cost_per_merged_pr"]}
            for d in direction_outcomes
            if d.get("cost_per_merged_pr") is not None
        ]
        ranked.sort(key=lambda x: x["cost_per_merged_pr"])
        if ranked:
            overall_summary["cost_efficiency_ranking"] = ranked

    result: dict = {
        "window": {
            "from": window_from or "all_time",
            "to": now_iso,
            "vision_revision": vision_rev,
        },
        "overall": overall_summary,
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    strategic_effectiveness = _build_strategic_effectiveness(
        directions, cycle_history or [], vision
    )
    if strategic_effectiveness:
        result["strategic_effectiveness"] = strategic_effectiveness
    decision_accuracy = _build_decision_accuracy(directions, cycle_history or [])
    if decision_accuracy:
        result["decision_accuracy"] = decision_accuracy
    return result


def _build_decision_accuracy(directions: list, cycle_history: list) -> dict:
    """Score the most recent completed strategic cycle's create/retire decisions.

    Looks up each created direction's actual merge outcome and quality score,
    and each retired direction's current status, then summarizes how accurate
    the strategic agent's last set of decisions turned out to be.

    Audit W2: ``cycle_history`` is now passed in (was read from
    ``vision._strategic_cycle_history``); the async caller pre-fetches via
    ``strategic_cycle.recent_for_agent_context``.
    """
    from app.direction_metrics import _compute_direction_quality_score

    cycle_history = list(cycle_history or [])
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
        return sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )

    quality_scores = [
        round(_compute_direction_quality_score(d), 3) for d in directions if d.get("id")
    ]
    quality_scores_sorted = sorted(quality_scores)
    median_quality = 0.0
    if quality_scores_sorted:
        n = len(quality_scores_sorted)
        if n % 2 == 1:
            median_quality = quality_scores_sorted[n // 2]
        else:
            median_quality = (
                quality_scores_sorted[n // 2 - 1] + quality_scores_sorted[n // 2]
            ) / 2

    created_breakdown: list = []
    successes = 0
    for cid in target.get("created_direction_ids") or []:
        d = dir_by_id.get(cid)
        if d is None:
            created_breakdown.append(
                {"direction_id": cid, "status": "missing", "success": False}
            )
            continue
        merged = _lifetime_merged(d)
        auto_paused = bool(d.get("auto_paused"))
        qscore = round(_compute_direction_quality_score(d), 3)
        above_median = qscore > median_quality
        success = merged >= 1 and not auto_paused
        if success:
            successes += 1
        created_breakdown.append(
            {
                "direction_id": cid,
                "prs_merged": merged,
                "auto_paused": auto_paused,
                "quality_score": qscore,
                "above_median_quality": above_median,
                "success": success,
            }
        )

    retired_breakdown: list = []
    retired_good = 0
    retired_total = 0
    for rid in target.get("retired_direction_ids") or []:
        retired_total += 1
        d = dir_by_id.get(rid)
        if d is None:
            retired_breakdown.append(
                {"direction_id": rid, "stayed_retired": True, "status": "missing"}
            )
            retired_good += 1
            continue
        status = d.get("status")
        stayed = status in ("retired", "expired")
        if stayed:
            retired_good += 1
        retired_breakdown.append(
            {
                "direction_id": rid,
                "status": status,
                "stayed_retired": stayed,
            }
        )

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


def _build_strategic_effectiveness(
    directions: list, cycle_history: list, vision: dict | None = None
) -> dict:
    """Aggregate per-cycle strategic decision quality from cycle history.

    Measures whether the strategic agent's own choices (which directions to
    create/retire) are producing better outcomes across recent cycles. Distinct
    from per-direction trends, which measure direction performance.

    Audit W2: ``cycle_history`` is passed in (was read from
    ``vision._strategic_cycle_history``); the async caller pre-fetches via
    ``strategic_cycle.recent_for_agent_context``. ``vision`` is still needed
    for the ``_strategic_failures`` counter (a circuit-breaker state that
    intentionally stays in JSONB, not promoted to the cycle table).
    """
    vision = vision or {}
    cycle_history = list(cycle_history or [])
    completed = [
        c for c in cycle_history if c.get("outcome") not in (None, "in_progress")
    ]
    if not completed:
        return {}

    # Index directions by id for quick lookup of current status / merge counts.
    dir_by_id: dict = {d.get("id"): d for d in directions if d.get("id")}

    def _direction_lifetime_merged(d: dict) -> int:
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        return sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )

    recent = completed[-5:]
    cycle_metrics: list = []
    for entry in recent:
        created_ids = list(entry.get("created_direction_ids") or [])
        retired_ids = list(entry.get("retired_direction_ids") or [])
        created_with_merges = sum(
            1
            for cid in created_ids
            if cid in dir_by_id and _direction_lifetime_merged(dir_by_id[cid]) > 0
        )
        retired_still_retired = sum(
            1
            for rid in retired_ids
            if rid in dir_by_id
            and dir_by_id[rid].get("status") in ("retired", "expired")
        )
        decisions_total = len(created_ids) + len(retired_ids)
        successful = created_with_merges + retired_still_retired
        conversion_rate = (
            round(successful / decisions_total, 2) if decisions_total else None
        )
        cycle_metrics.append(
            {
                "cycle_id": entry.get("cycle_id"),
                "completed_at": entry.get("phase2_completed_at"),
                "directions_created": len(created_ids),
                "directions_retired": len(retired_ids),
                "created_with_merges": created_with_merges,
                "retired_still_retired": retired_still_retired,
                "vision_updated": bool(entry.get("vision_updated")),
                "decision_conversion_rate": conversion_rate,
            }
        )

    # Trend across cycles: average decision_conversion_rate, first half vs second half.
    rates = [
        c["decision_conversion_rate"]
        for c in cycle_metrics
        if c["decision_conversion_rate"] is not None
    ]
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
    project,
    cycle_history: list | None = None,
    lookback_cycles: int = 5,
    min_samples: int = 3,
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
    # Audit W2: cycle_history is now passed in (was read from JSONB).
    cycle_history = list(cycle_history or [])
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
            actual = sum(h.get("prs_merged", 0) for h in history) + (
                agg.get("total_prs_merged") or 0
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


def _compute_prediction_calibration(
    project,
    cycle_history: list | None = None,
    lookback_cycles: int = 5,
    min_samples: int = 3,
) -> dict:
    """Return calibration factor, human-readable note, and raw sample count.

    Wraps ``_compute_prediction_calibration_factor`` and adds the canonical
    note string and the number of raw data points so callers share wording.

    Returns a dict with keys: ``factor`` (float), ``note`` (str),
    ``sample_count`` (int — raw points before blending).
    """
    cycle_history = list(cycle_history or [])
    directions_by_id = {
        d.get("id"): d for d in (project.directions or []) if d.get("id")
    }
    sample_count = 0
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
            if directions_by_id.get(direction_id) is None:
                continue
            sample_count += 1

    factor = _compute_prediction_calibration_factor(
        project,
        cycle_history,
        lookback_cycles=lookback_cycles,
        min_samples=min_samples,
    )
    if abs(factor - 1.0) < 0.05:
        note = f"Predictions are well-calibrated (factor {factor})."
    elif factor < 1.0:
        pct = round((1.0 - factor) * 100)
        note = f"Predictions have been running ~{pct}% optimistic — factor {factor} applied to future merge estimates."
    else:
        pct = round((factor - 1.0) * 100)
        note = f"Predictions have been running ~{pct}% conservative — factor {factor} applied to future merge estimates."

    return {"factor": factor, "note": note, "sample_count": sample_count}


def _build_retired_approaches_block(
    retired_directions_summary: list | None = None,
    max_entries: int = 30,
    per_entry_char_cap: int = 80,
) -> str | None:
    """Build a prompt block listing failed approaches from retired directions.

    Extracts failed_approaches from each retired direction and formats them
    with direction IDs as a "Do not re-propose" block for the strategic prompt.
    Caps total entries at max_entries, with each entry truncated to
    per_entry_char_cap characters.

    Returns the formatted prompt block as a string, or None if no approaches
    exist. The block includes a header and is ready to append to prompt_suffix.
    """
    if not retired_directions_summary:
        return None

    entries: list[str] = []
    for direction in retired_directions_summary:
        direction_id = direction.get("id", "unknown")
        direction_text = direction.get("text", "")[:40]
        failed_approaches = direction.get("failed_approaches") or []

        for approach in failed_approaches:
            # Truncate approach text and tag with direction
            approach_text = str(approach)[:per_entry_char_cap]
            entry = f"- {approach_text} (from {direction_id}: {direction_text})"
            entries.append(entry)

            if len(entries) >= max_entries:
                break

        if len(entries) >= max_entries:
            break

    if not entries:
        return None

    block = (
        "## Do not re-propose these — already attempted under retired directions\n"
        + "\n".join(entries)
    )
    return block
