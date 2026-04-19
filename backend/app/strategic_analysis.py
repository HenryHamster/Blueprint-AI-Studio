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

    result: dict = {
        "window": {"from": window_from or "all_time", "to": now_iso, "vision_revision": vision_rev},
        "overall": overall_summary,
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    return result
