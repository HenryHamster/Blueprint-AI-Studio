"""Strategic analysis helpers — pure, synchronous, no DB access.

Extracted from app.strategic_completion to keep that module under its line ceiling.
"""

from datetime import timedelta

from app.direction_metrics import (
    _entry_generalized_successes,
    _entry_generalized_terminal_outcomes,
    canonical_cost_per_merged_pr,
)
from app.direction_outcomes import verified_merged_count, PR_INELIGIBLE_TASK_TYPES
from app.time_utils import _utcnow

# Directions whose cost_per_merged_pr exceeds median * multiplier are flagged
# for retirement consideration by the strategic agent.
_HIGH_COST_PER_MERGE_MULTIPLIER = 2.0

# Caps for _build_retired_directions_summary. 90d keeps context relevant;
# 800 chars/entry lets the agent recognize overlap without inflating the prompt.
_RETIRED_WINDOW_DAYS = 90
_RETIRED_MAX_COUNT = 20
_RETIRED_PER_ENTRY_CHAR_CAP = 800


def _entry_cost_usd(entry: dict, on_demand_rate: float) -> float:
    """Return the recorded spend for a history entry, falling back to the
    tasks_created * avg_time_to_merge_hours proxy for legacy entries that
    predate exact attribution (i.e. entries without a positive estimated_cost_usd).
    """
    recorded = entry.get("estimated_cost_usd")
    try:
        if recorded is not None and float(recorded) > 0:
            return float(recorded)
    except (TypeError, ValueError):
        pass
    tcount = entry.get("tasks_created", 0) or 0
    hours = entry.get("avg_time_to_merge_hours")
    if tcount and hours:
        return tcount * float(hours) * 3600 * on_demand_rate
    return 0.0


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

        # Time-window filter: use the timestamp each writer already produces.
        # Writers: retire op -> retired_at, saturation/code-failure/scaffolding -> completed_at,
        # proposal expiry -> expired_at. Fall back to last history entry timestamp.
        retired_at = d.get("retired_at") or d.get("completed_at") or d.get("expired_at")
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

        # Estimated cost per merge — prefer recorded spend; fall back to proxy for
        # legacy entries that predate exact attribution. Include evicted cycles
        # folded into history_aggregate so lifetime cost is not truncated at the
        # 10-cycle eviction window.
        try:
            agg_cost = max(
                0.0, float(agg.get("total_estimated_cost_usd") or 0.0)
            )
        except (TypeError, ValueError):
            agg_cost = 0.0
        estimated_cost_usd = round(
            sum(_entry_cost_usd(h, ON_DEMAND_RATE) for h in history) + agg_cost, 2
        )
        # Use canonical_cost_per_merged_pr to enforce population consistency:
        # denominator must be ALL merged PRs (raw total_merged), not verified-only.
        cost_per_merge = canonical_cost_per_merged_pr(
            estimated_cost_usd, total_merged
        ) if estimated_cost_usd > 0 else None

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
        retirement_outcome = d.get("retirement_outcome") or d.get("auto_completed_reason")
        if retirement_outcome:
            entry["retirement_outcome"] = str(retirement_outcome)[:200]
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

    # Augment with durable retired-direction memory for directions pruned from
    # the live list. The lifecycle pass removes terminal directions from
    # project.directions, so directions retired a cycle ago are invisible to the
    # loop above. vision['_recently_retired_directions'] survives pruning and
    # stores compact entries (id, text_tokens, retired_at, reason, gap_id,
    # cycles, prs_merged). Adding them here ensures the agent sees cross-cycle
    # retirements and can avoid re-proposing work for the same gaps. The outcome
    # metrics (cycles, prs_merged) surface the actual result so re-proposals of
    # landed or known-dead work can be detected.
    vision = project.vision or {}
    recently_retired_memory = vision.get("_recently_retired_directions") or []
    if recently_retired_memory and len(results) < max_count:
        existing_ids = {r.get("id") for r in results if r.get("id")}
        for mem_entry in reversed(recently_retired_memory):  # newest first
            if len(results) >= max_count:
                break
            entry_id = mem_entry.get("id", "")
            if not entry_id or entry_id in existing_ids:
                continue
            retired_at = mem_entry.get("retired_at", "")
            if retired_at and retired_at < cutoff_iso:
                continue
            text = mem_entry.get("text") or " ".join(mem_entry.get("text_tokens") or [])
            mem_result: dict = {
                "id": entry_id,
                "text": text[:200],
                "status": "retired",
                "cycles": mem_entry.get("cycles", 0),
                "prs_merged": mem_entry.get("prs_merged", 0),
            }
            if mem_entry.get("reason"):
                mem_result["retirement_reason"] = str(mem_entry["reason"])[:200]
            # Surface the compact retirement outcome ("why it was retired / what
            # landed" — rationale or a superseded-by successor pointer) so the
            # agent sees what a retired direction actually shipped, not just its
            # coarse reason category.
            if mem_entry.get("outcome"):
                mem_result["retirement_outcome"] = str(mem_entry["outcome"])[:200]
            else:
                # The freetext ``outcome`` is optional and the lifecycle retire
                # path never writes it, but every memory entry carries a
                # structured ``retired_outcome`` ({classification, metrics}). Fall
                # back to it so the strategist always sees how a retired direction
                # ended (classification + outcome metrics), not just its coarse
                # reason category. Keep the same per-entry char cap.
                retired_outcome = mem_entry.get("retired_outcome")
                if isinstance(retired_outcome, dict):
                    classification = str(
                        retired_outcome.get("classification") or ""
                    ).strip()
                    if classification:
                        outcome_text = classification
                        metrics = retired_outcome.get("metrics")
                        if isinstance(metrics, dict):
                            metric_bits = [
                                f"{k}={v}"
                                for k, v in metrics.items()
                                if isinstance(v, (int, float, bool))
                            ]
                            if metric_bits:
                                outcome_text = (
                                    f"{classification} ({', '.join(metric_bits)})"
                                )
                        mem_result["retirement_outcome"] = outcome_text[:200]
            results.append(mem_result)
            existing_ids.add(entry_id)

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
    knowledge_by_direction: dict | None = None,
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
        # raw_merged stays RAW (operator-judgment metrics acceptance_rate and
        # operator_rejection_rate below count every operator-accepted merge,
        # verified or not). verified_merged is the shipped-work signal that feeds
        # outcome["merged"], difficulty, and the downstream selection trims.
        raw_merged = sum(h.get("prs_merged", 0) for h in window_history)
        verified_merged = sum(verified_merged_count(h) for h in window_history)
        rejected = sum(h.get("prs_rejected", 0) for h in window_history)
        failed = sum(h.get("tasks_failed", 0) for h in window_history)
        failed_infra = sum(h.get("tasks_failed_infra", 0) for h in window_history)
        # Generalized successes: code merges PLUS non-code evaluation successes
        # (research quality, audit findings, memo artifacts, verify verdicts).
        # Legacy entries without the field fall back to prs_merged so code-only
        # directions remain unchanged; non-code contributions appear on entries
        # written after the generalized-outcome rollout.
        generalized_successes = sum(
            _entry_generalized_successes(h) for h in window_history
        )
        # Terminal-outcome denominator: ALL terminal outcomes (success + neutral
        # + failure), matching the canonical dashboard definition in
        # direction_metrics / digest_direction_progress. Reusing the shared
        # helper keeps exactly one definition of generalized_health_rate so the
        # strategic engine and the operator dashboard never diverge.
        generalized_terminal_outcomes = sum(
            _entry_generalized_terminal_outcomes(h) for h in window_history
        )
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

        total_prs = raw_merged + rejected
        acceptance_rate = round(raw_merged / total_prs, 3) if total_prs > 0 else None
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
            "merged": verified_merged,
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
        # Generalized health — ADDITIVE. Present when non-code successes exist
        # so mixed-portfolio directions show positive health even with zero PRs.
        if generalized_terminal_outcomes > 0:
            outcome["generalized_terminal_outcomes"] = generalized_terminal_outcomes
        if generalized_successes > 0:
            outcome["generalized_successes"] = generalized_successes
            if generalized_terminal_outcomes > 0:
                outcome["generalized_health_rate"] = round(
                    generalized_successes / generalized_terminal_outcomes, 3
                )
        # Estimated cost — prefer recorded spend per entry; fall back to the
        # tasks_created * avg_time_to_merge_hours proxy for legacy entries.
        estimated_cost_usd = round(
            sum(_entry_cost_usd(h, ON_DEMAND_RATE) for h in window_history), 2
        )
        cost_budget_usd = d.get("cost_budget_usd")
        if estimated_cost_usd > 0:
            outcome["estimated_cost_usd"] = estimated_cost_usd
            cpm = canonical_cost_per_merged_pr(estimated_cost_usd, raw_merged)
            if cpm is not None:
                outcome["cost_per_merged_pr"] = cpm
        if isinstance(cost_budget_usd, (int, float)) and cost_budget_usd > 0:
            outcome["cost_budget_usd"] = cost_budget_usd
            if estimated_cost_usd > cost_budget_usd:
                outcome["budget_exceeded"] = True
        if rejected > 0:
            total_prs = raw_merged + rejected
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
        # Surface cumulative confirmed-finding count and trend from
        # outcome_progress_history (eviction-robust signal used by scaffolding
        # detector). Shared helper keeps thresholds in sync with strategic_spawning.
        from app.strategic_completion import outcome_progress_summary_fields

        outcome.update(outcome_progress_summary_fields(d))
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
        if knowledge_by_direction is not None:
            dir_knowledge = knowledge_by_direction.get(d.get("id") or "", [])
            if dir_knowledge:
                outcome["direction_knowledge"] = dir_knowledge[:5]
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
    # Count only PR-eligible failed tasks in the success_rate denominator.
    # PR-ineligible tasks (read-only audits, knowledge docs, etc.) can't produce
    # merges, so they must not deflate success_rate when they fail.
    total_failed_pr_ineligible = 0
    for d in directions:
        if d.get("status", "active") not in ("active", "expired", "paused"):
            continue
        history = d.get("research_history", [])
        if window_from:
            window_history = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            window_history = list(history)
        for entry in window_history:
            type_breakdown = entry.get("type_breakdown") or {}
            for ttype in PR_INELIGIBLE_TASK_TYPES:
                if ttype in type_breakdown:
                    total_failed_pr_ineligible += (type_breakdown[ttype] or {}).get("failed", 0) or 0
    # total_failed derives from each cycle's ``tasks_failed`` field, which counts
    # only TaskStatus.FAILED (direction_outcomes.py). type_breakdown[*]['failed']
    # is written via _counts_as_failure, which counts FAILED *plus* genuinely-
    # cancelled tasks. The two counts therefore use DIFFERENT failure predicates
    # over the same tasks, so total_failed_pr_ineligible can exceed the PR-
    # ineligible contribution to total_failed by the number of genuinely-cancelled
    # PR-ineligible tasks. Clamp the eligible failure count at zero so the
    # subtraction can never go negative (which would otherwise drive success_rate
    # above 1.0). Also clamp the emitted rate at 1.0 as belt-and-braces so an
    # over-subtraction can never publish an impossible >1.0 success_rate into
    # strategic outcome evaluation.
    total_failed_pr_eligible = max(total_failed - total_failed_pr_ineligible, 0)
    success_rate = round(
        min(total_merged / max(total_merged + total_failed_pr_eligible, 1), 1.0), 2
    )
    # Generalized success rate — code merges PLUS non-code evaluation successes.
    # Falls back to prs_merged for directions without the new field so legacy
    # data produces the same rate as the PR-based metric.
    total_generalized_successes = sum(
        d.get("generalized_successes", d["merged"]) for d in direction_outcomes
    )
    # Denominator is ALL terminal outcomes (success + neutral + failure), summed
    # from the per-direction generalized_terminal_outcomes computed above via the
    # shared direction_metrics helper. Directions with no terminal history fall
    # back to merged+failed (their terminal count is otherwise zero). This keeps
    # the portfolio rate consistent with the canonical dashboard definition
    # rather than the old successes+failed denominator that dropped neutrals.
    total_generalized_terminal = sum(
        d.get("generalized_terminal_outcomes", d["merged"] + d["failed"])
        for d in direction_outcomes
    )
    generalized_success_rate = round(
        total_generalized_successes / max(total_generalized_terminal, 1),
        2,
    )

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
    # generalized_success_rate is the mixed-portfolio comparator — includes code
    # merges AND non-code evaluation successes. Differs from success_rate only
    # when directions include research/audit/memo/verify work.
    if total_generalized_successes != total_merged:
        overall_summary["generalized_successes"] = total_generalized_successes
        overall_summary["generalized_success_rate"] = generalized_success_rate
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
        return sum(int(h.get("prs_merged") or 0) for h in history) + int(
            agg.get("total_prs_merged") or 0
        )

    def _lifetime_verified_merged(d: dict) -> int:
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        verified_history_total = 0
        for h in history:
            verified_history_total += int(verified_merged_count(h))
        return verified_history_total + int(
            agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
        )

    def _lifetime_generalized_successes(d: dict) -> int:
        """Lifetime generalized successes — code merges + non-code eval successes.

        Falls back to prs_merged for legacy entries that predate the
        generalized_successes field so code-only directions remain unchanged.
        """
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        return sum(
            int(h.get("generalized_successes", h.get("prs_merged", 0)) or 0)
            for h in history
        ) + int(
            agg.get("total_generalized_successes") or agg.get("total_prs_merged") or 0
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
    generalized_successes_count = 0
    for cid in target.get("created_direction_ids") or []:
        d = dir_by_id.get(cid)
        if d is None:
            created_breakdown.append(
                {
                    "direction_id": cid,
                    "status": "missing",
                    "success": False,
                    "generalized_success": False,
                }
            )
            continue
        merged = _lifetime_merged(d)
        verified_merged = _lifetime_verified_merged(d)
        gen_successes = _lifetime_generalized_successes(d)
        auto_paused = bool(d.get("auto_paused"))
        qscore = round(_compute_direction_quality_score(d), 3)
        above_median = qscore > median_quality
        # Code-based success: at least one VERIFIED merge (raw prs_merged stays exposed).
        success = verified_merged >= 1 and not auto_paused
        # Generalized success: any terminal evaluation success, not just PR merges.
        generalized_success = gen_successes >= 1 and not auto_paused
        if success:
            successes += 1
        if generalized_success:
            generalized_successes_count += 1
        created_entry: dict = {
            "direction_id": cid,
            "prs_merged": merged,
            "auto_paused": auto_paused,
            "quality_score": qscore,
            "above_median_quality": above_median,
            "success": success,
            "generalized_success": generalized_success,
        }
        if gen_successes != merged:
            created_entry["generalized_successes"] = gen_successes
        created_breakdown.append(created_entry)

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
    generalized_decision_accuracy_rate = (
        round(generalized_successes_count / created_total, 2) if created_total else None
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
    # generalized_decision_accuracy_rate is additive — only present when it
    # differs from decision_accuracy_rate (i.e. non-code successes exist).
    if generalized_successes_count != successes:
        result["created_generalized_successful"] = generalized_successes_count
        result["generalized_decision_accuracy_rate"] = (
            generalized_decision_accuracy_rate
        )
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
        return sum(int(h.get("prs_merged") or 0) for h in history) + int(
            agg.get("total_prs_merged") or 0
        )

    def _direction_lifetime_generalized(d: dict) -> int:
        """Lifetime generalized successes — falls back to prs_merged for legacy."""
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        return sum(
            int(h.get("generalized_successes", h.get("prs_merged", 0)) or 0)
            for h in history
        ) + int(
            agg.get("total_generalized_successes") or agg.get("total_prs_merged") or 0
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
        # Generalized: a direction is a success when it produced ANY evaluation
        # success — not just a code merge (includes research quality, audits, memos).
        created_with_successes = sum(
            1
            for cid in created_ids
            if cid in dir_by_id and _direction_lifetime_generalized(dir_by_id[cid]) > 0
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
        gen_successful = created_with_successes + retired_still_retired
        gen_conversion_rate = (
            round(gen_successful / decisions_total, 2) if decisions_total else None
        )
        cycle_entry: dict = {
            "cycle_id": entry.get("cycle_id"),
            "completed_at": entry.get("phase2_completed_at"),
            "directions_created": len(created_ids),
            "directions_retired": len(retired_ids),
            "created_with_merges": created_with_merges,
            "retired_still_retired": retired_still_retired,
            "vision_updated": bool(entry.get("vision_updated")),
            "decision_conversion_rate": conversion_rate,
        }
        # generalized_decision_conversion_rate is additive — only when it differs.
        if created_with_successes != created_with_merges:
            cycle_entry["created_with_successes"] = created_with_successes
            cycle_entry["generalized_decision_conversion_rate"] = gen_conversion_rate
        cycle_metrics.append(cycle_entry)

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

    ratios: list[float] = []
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
            predicted = float(prediction.get("similar_past_merges") or 0)
            if predicted <= 0:
                continue
            d = directions_by_id.get(direction_id)
            if d is None:
                continue
            history = d.get("research_history") or []
            agg = d.get("history_aggregate") or {}
            # Actual merge total is the VERIFIED lifetime total: calibrating the
            # prediction against degraded/unverified merges would teach the
            # predictor that work shipped when it did not. Legacy fallback handled
            # by verified_merged_count / the aggregate .get chain.
            actual = float(
                sum(verified_merged_count(h) for h in history)
                + int(
                    agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0))
                    or 0
                )
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
