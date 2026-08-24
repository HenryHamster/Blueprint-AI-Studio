"""Lane-waste diagnostics extracted from ``app.digest_metrics``.

Pure functions for the v10 review/fix lane pilot: a per-iteration waste
taxonomy, a per-task drilldown handle, the chain-complete waste diagnostics, and
the per-metric trend between persisted lane baselines. The chain grouping and
outcome resolution reuse the outer-loop helpers in ``app.digest_outer_loop`` so
the diagnostics match ``compute_outer_loop_convergence`` exactly.

``app.digest_metrics`` re-exports these symbols so existing import paths (and
``mock.patch`` targets) are unchanged.
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from app.digest_outer_loop import (
    _group_outer_loop_chains,
    _outer_loop_chain_observation,
    _outer_loop_pct,
    _outer_loop_terminal_outcome,
    _scan_escalation_alerts,
)
from app.models import Alert, Task, TaskStatus
from app.review_heuristics import (
    LANE_CI,
    LANE_CONFLICT,
    LANE_REVIEW_CONVERGENCE,
)

# ── Lane waste taxonomy (v10 pilot baseline) ──────────────────────────────────
# Pilot-useful classification of WHY a review/fix iteration was spent. Every
# review-loop iteration-task (review / fix), including terminal
# FAILED/CANCELLED ones, maps to exactly one category. Ordering of the resolver
# below is the priority: infra terminal failures dominate, then lost repo state
# (drift), lost reasoning (fix-loop divergence), then the mechanical review-fix
# lanes (conflict / CI / arbitration), with everything else "unknown".
LANE_WASTE_CATEGORIES: tuple[str, ...] = (
    "infra",
    "merge_conflict",
    "ci_churn",
    "review_ambiguity",
    "lost_reasoning",
    "lost_repo_state",
    "unknown",
)


def _lane_waste_completed_at(t: "Task") -> Optional[str]:
    """ISO completion timestamp for a drilldown row (status_log tail → updated_at)."""
    sl = getattr(t, "status_log", None)
    if sl:
        last = sl[-1]
        if isinstance(last, dict) and last.get("t"):
            return str(last["t"])
    for attr in ("updated_at", "created_at"):
        val = getattr(t, attr, None)
        if val is not None:
            return val.isoformat()
    return None


def classify_lane_waste_category(t: "Task") -> str:
    """Classify one review-loop iteration-task into a pilot waste category.

    Pure function reusing the canonical helpers so the diagnostic matches
    operator logic: ``failure_classification.INFRA_ANALYTICS_CLASSES`` for the
    infra-class terminal failures (the analytics superset — it also admits
    UNPLACEABLE_EXECUTION_SITE, a terminal routing-contract failure whose work
    never ran), ``review_loop.drift_signal`` for lost repo state,
    ``fix_loop_telemetry`` divergence reasons for lost reasoning, and
    ``classify_iteration_lane`` for the mechanical review-fix lanes.
    """
    from app.failure_classification import INFRA_ANALYTICS_CLASSES
    from app.fix_loop_telemetry import has_terminal_divergence
    from app.review_heuristics import (
        LANE_CI,
        LANE_CONFLICT,
        LANE_REVIEW_CONVERGENCE,
        classify_iteration_lane,
    )

    ctx = t.context or {}
    if (
        t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
        and ctx.get("failure_category") in INFRA_ANALYTICS_CLASSES
    ):
        return "infra"
    rl = ctx.get("review_loop") or {}
    drift = rl.get("drift_signal")
    if isinstance(drift, dict) and (drift.get("drift_in") or drift.get("drift_out")):
        return "lost_repo_state"
    if has_terminal_divergence(getattr(t, "fix_loop_history", None)) is not None:
        return "lost_reasoning"
    lane = classify_iteration_lane(t)
    if lane == LANE_CONFLICT:
        return "merge_conflict"
    if lane == LANE_CI:
        return "ci_churn"
    if lane == LANE_REVIEW_CONVERGENCE:
        iteration = rl.get("iteration")
        if isinstance(iteration, (int, float)):
            if iteration <= 2:
                return "review_ambiguity"
            else:
                return "lost_reasoning"
    return "unknown"


def _lane_waste_task_row(t: "Task") -> Dict[str, Any]:
    """Drilldown handle for one review-loop iteration-task."""
    from app.review_heuristics import classify_iteration_lane
    from app.review_pipeline import task_counts_toward_review_cap

    ctx = t.context or {}
    rl = ctx.get("review_loop") or {}
    payload = t.first_payload or {}
    iteration = rl.get("iteration")
    status_val = getattr(t.status, "value", t.status)
    return {
        "task_id": str(t.id),
        "status": status_val,
        "type": str(payload.get("type") or "create"),
        "lane": classify_iteration_lane(t),
        "waste_category": classify_lane_waste_category(t),
        "terminal_outcome": _outer_loop_terminal_outcome(t),
        "failure_category": ctx.get("failure_category"),
        "stale_head_discard": bool(ctx.get("_review_stale_head")),
        "retarget_scope": rl.get("retarget_scope")
        if rl.get("retarget_scope") in ("delta", "full")
        else None,
        "counts_toward_cap": task_counts_toward_review_cap(t),
        "iteration": int(iteration) if isinstance(iteration, (int, float)) else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat()
        if getattr(t, "started_at", None)
        else None,
        "completed_at": _lane_waste_completed_at(t),
    }


def compute_lane_waste_diagnostics(
    all_tasks: List[Task],
    window_start: "datetime",
    *,
    chain_limit: int = 100,
    escalation_alerts: Optional[List[Alert]] = None,
) -> Dict[str, Any]:
    """Chain-complete review/fix waste diagnostics for the v10 lane pilot.

    Pure function (no DB). Groups in-window review-loop tasks — INCLUDING
    terminal FAILED/CANCELLED iterations — into PR-chains using the SAME
    ``_outer_loop_chain_key``/``_group_outer_loop_chains`` semantics as
    ``compute_outer_loop_convergence``. Callers may supply the chain-original
    task plus matching escalation Alerts so terminal markers stamped outside
    the review-loop window still resolve the canonical outcome.

    For every chain it reports drilldown handles (task ids, original task id, pr
    number, project id, terminal outcome/status, lane, type, timestamps), the
    per-chain waste taxonomy totals, the cap-counted iteration total (excluding
    infra-class failures and stale-head discards exactly as
    ``task_counts_toward_review_cap`` does), and a cap-exhausted flag derived
    from the resolved terminal outcome. Aggregate ``waste_taxonomy`` rolls the
    per-task categories up across all chains.
    """
    tasks_by_id, chains, _task_id_to_chain = _group_outer_loop_chains(
        all_tasks, window_start
    )

    taxonomy_totals: Dict[str, int] = dict.fromkeys(LANE_WASTE_CATEGORIES, 0)
    waste_task_count = 0
    cap_counted_total = 0
    cap_exhausted_chain_count = 0
    stale_head_discard_count = 0
    retarget_review_scope_counts = {"delta": 0, "full": 0}
    converged_review_counts: List[int] = []
    supersede_affected_chain_count = 0
    supersede_affected_merged_review_counts: List[int] = []
    cap_exhausted_direction_ids: set[str] = set()
    chain_rows: List[Dict[str, Any]] = []

    alert_reasons_by_chain = {}
    if escalation_alerts is not None:
        alert_reasons_by_chain, _by_reason, _escalated_ids = _scan_escalation_alerts(
            escalation_alerts, _task_id_to_chain
        )

    for key, chain_tasks in chains.items():
        obs = _outer_loop_chain_observation(
            key,
            chain_tasks,
            tasks_by_id,
            alert_reasons_by_chain.get(key, set()),
        )
        per_chain_taxonomy: Dict[str, int] = dict.fromkeys(LANE_WASTE_CATEGORIES, 0)
        cap_counted = 0
        task_rows: List[Dict[str, Any]] = []
        latest_created = None
        for t in chain_tasks:
            row = _lane_waste_task_row(t)
            task_rows.append(row)
            per_chain_taxonomy[row["waste_category"]] += 1
            if row["counts_toward_cap"]:
                cap_counted += 1
            if row["stale_head_discard"]:
                stale_head_discard_count += 1
            scope = row["retarget_scope"]
            if scope in retarget_review_scope_counts:
                retarget_review_scope_counts[scope] += 1
            created = t.created_at
            if created is not None and (
                latest_created is None or created > latest_created
            ):
                latest_created = created

        for cat, n in per_chain_taxonomy.items():
            taxonomy_totals[cat] += n
        waste_task_count += len(task_rows)
        cap_counted_total += cap_counted

        outcome = obs["outcome"]
        cap_exhausted = outcome == "cap_exhausted"
        if cap_exhausted:
            cap_exhausted_chain_count += 1
            if obs["direction_id"]:
                cap_exhausted_direction_ids.add(obs["direction_id"])
        if obs["reviews_to_converge"] is not None:
            converged_review_counts.append(obs["reviews_to_converge"])
        if obs["supersede_affected"]:
            supersede_affected_chain_count += 1
            if outcome == "merged" and obs["reviews_to_converge"] is not None:
                supersede_affected_merged_review_counts.append(
                    obs["reviews_to_converge"]
                )

        # Newest-first drilldown rows within the chain.
        task_rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
        chain_rows.append(
            {
                "chain_key": key,
                "original_task_id": obs["original_task_id"],
                "pr_number": obs["pr_number"],
                "review_loop_pr_numbers": obs["review_loop_pr_numbers"],
                "supersede_affected": obs["supersede_affected"],
                "project_id": obs["project_id"],
                "direction_id": obs["direction_id"],
                "terminal_outcome": outcome,
                "cap_exhausted": cap_exhausted,
                "task_count": len(task_rows),
                "cap_counted_iterations": cap_counted,
                "cap_excluded_iterations": len(task_rows) - cap_counted,
                "max_iteration": obs["max_iteration"],
                "review_count": obs["review_count"],
                "reviews_to_converge": obs["reviews_to_converge"],
                "churn_count": obs["churn_count"],
                "has_drift": obs["has_drift"],
                "waste_totals": per_chain_taxonomy,
                "tasks": task_rows,
                "_latest_created": latest_created,
            }
        )

    chain_rows.sort(
        key=lambda r: r["_latest_created"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for r in chain_rows:
        r.pop("_latest_created", None)

    return {
        "waste_taxonomy": taxonomy_totals,
        "waste_task_count": waste_task_count,
        "cap_counted_task_count": cap_counted_total,
        "cap_excluded_task_count": waste_task_count - cap_counted_total,
        "chain_count": len(chains),
        "cap_exhausted_chain_count": cap_exhausted_chain_count,
        "cap_exhausted_direction_count": len(cap_exhausted_direction_ids),
        "supersede_affected_chain_count": supersede_affected_chain_count,
        "supersede_affected_reviews_to_converge": {
            "count": len(supersede_affected_merged_review_counts),
            "sample_count": len(supersede_affected_merged_review_counts),
            "median": _outer_loop_pct(supersede_affected_merged_review_counts, 50),
            "p95": _outer_loop_pct(supersede_affected_merged_review_counts, 95),
        },
        "stale_head_discard_count": stale_head_discard_count,
        "retarget_review_scope_counts": retarget_review_scope_counts,
        "converged_review_counts": sorted(converged_review_counts),
        "reviews_to_converge": {
            "count": len(converged_review_counts),
            "median": _outer_loop_pct(converged_review_counts, 50),
            "p95": _outer_loop_pct(converged_review_counts, 95),
        },
        "chain_limit": chain_limit,
        "chains_truncated": len(chain_rows) > chain_limit,
        "chains": chain_rows[:chain_limit],
    }


def compute_lane_baseline_trend(
    baselines: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    """Compute per-metric trend between the two most recent persisted lane baselines.

    ``baselines`` must be sorted newest-first (as returned by
    ``get_lane_baselines``).  Returns ``None`` when fewer than 2 baselines
    exist.  For each tracked metric the returned block contains ``latest``,
    ``prior``, ``delta``, and ``classification``
    (``"improving"`` / ``"declining"`` / ``"stable"`` / ``None``).

    Thresholds:
    - Relay medians (wall_clock, active, gap seconds): relative ±10%.
    - Relay medians task_count and lane p50/p90 iterations: absolute ±0.5.
    - Waste-taxonomy shares (category count / waste_task_count): absolute ±0.05.
    - Outer-loop cycles and cap counts: absolute ±0.5;
      convergence rate: absolute ±0.05.

    All timing and iteration metrics treat lower as better; waste shares also
    treat lower as better (less waste = improving).
    """
    if len(baselines) < 2:
        return None

    latest = baselines[0]
    prior = baselines[1]

    def _block(
        curr: float | None,
        prev: float | None,
        *,
        threshold_rel: float | None = None,
        threshold_abs: float | None = None,
        lower_is_better: bool = True,
        classify: bool = True,
    ) -> Dict[str, Any]:
        delta = (curr - prev) if (curr is not None and prev is not None) else None
        if delta is None or not classify:
            classification: str | None = None
        elif threshold_rel is not None:
            if prev == 0:
                classification = None
            else:
                rel = delta / abs(prev)
                if lower_is_better:
                    classification = (
                        "improving"
                        if rel < -threshold_rel
                        else "declining"
                        if rel > threshold_rel
                        else "stable"
                    )
                else:
                    classification = (
                        "improving"
                        if rel > threshold_rel
                        else "declining"
                        if rel < -threshold_rel
                        else "stable"
                    )
        else:
            assert threshold_abs is not None
            if lower_is_better:
                classification = (
                    "improving"
                    if delta < -threshold_abs
                    else "declining"
                    if delta > threshold_abs
                    else "stable"
                )
            else:
                classification = (
                    "improving"
                    if delta > threshold_abs
                    else "declining"
                    if delta < -threshold_abs
                    else "stable"
                )
        return {
            "latest": curr,
            "prior": prev,
            "delta": delta,
            "classification": classification,
        }

    relay_curr = latest.get("relay_medians") or {}
    relay_prev = prior.get("relay_medians") or {}
    relay_trend: Dict[str, Any] = {}
    for key in ("wall_clock_seconds", "active_seconds", "gap_seconds"):
        relay_trend[key] = _block(
            relay_curr.get(key),
            relay_prev.get(key),
            threshold_rel=0.10,
            lower_is_better=True,
        )
    relay_trend["task_count"] = _block(
        relay_curr.get("task_count"),
        relay_prev.get("task_count"),
        threshold_abs=0.5,
        lower_is_better=True,
    )

    diag_curr = latest.get("lane_iteration_distribution") or {}
    diag_prev = prior.get("lane_iteration_distribution") or {}
    lane_iter_trend: Dict[str, Any] = {}
    for lane in (LANE_CONFLICT, LANE_CI, LANE_REVIEW_CONVERGENCE):
        lc = diag_curr.get(lane) or {}
        lp = diag_prev.get(lane) or {}
        lane_iter_trend[lane] = {
            "p50": _block(
                lc.get("p50_iterations"),
                lp.get("p50_iterations"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
            "p90": _block(
                lc.get("p90_iterations"),
                lp.get("p90_iterations"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
        }

    waste_curr = (latest.get("lane_waste") or {}).get("waste_taxonomy") or {}
    waste_prev = (prior.get("lane_waste") or {}).get("waste_taxonomy") or {}
    total_curr = (latest.get("lane_waste") or {}).get("waste_task_count") or 0
    total_prev = (prior.get("lane_waste") or {}).get("waste_task_count") or 0
    waste_trend: Dict[str, Any] = {}
    for cat in LANE_WASTE_CATEGORIES:
        share_curr = (waste_curr.get(cat, 0) / total_curr) if total_curr > 0 else None
        share_prev = (waste_prev.get(cat, 0) / total_prev) if total_prev > 0 else None
        waste_trend[cat] = _block(
            share_curr,
            share_prev,
            threshold_abs=0.05,
            lower_is_better=True,
        )

    crw_curr = latest.get("checkpoint_recovery_waste") or {}
    crw_prev = prior.get("checkpoint_recovery_waste") or {}
    crw_trend = _block(
        float(crw_curr.get("total", 0)),
        float(crw_prev.get("total", 0)),
        threshold_abs=1.0,
        lower_is_better=True,
    )

    convergence_curr = latest.get("outer_loop_convergence") or {}
    convergence_prev = prior.get("outer_loop_convergence") or {}
    convergence_trend = {
        "iteration_median": _block(
            convergence_curr.get("iteration_median"),
            convergence_prev.get("iteration_median"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
        "iteration_p95": _block(
            convergence_curr.get("iteration_p95"),
            convergence_prev.get("iteration_p95"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
        "convergence_rate": _block(
            convergence_curr.get("convergence_rate"),
            convergence_prev.get("convergence_rate"),
            threshold_abs=0.05,
            lower_is_better=False,
        ),
        "cap_exhausted_chain_count": _block(
            convergence_curr.get("cap_exhausted_chain_count"),
            convergence_prev.get("cap_exhausted_chain_count"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
        "cap_exhausted_direction_count": _block(
            convergence_curr.get("cap_exhausted_direction_count"),
            convergence_prev.get("cap_exhausted_direction_count"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
        "supersede_affected_reviews_to_converge_median": _block(
            convergence_curr.get("supersede_affected_reviews_to_converge_median"),
            convergence_prev.get("supersede_affected_reviews_to_converge_median"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
        "supersede_affected_reviews_to_converge_p95": _block(
            convergence_curr.get("supersede_affected_reviews_to_converge_p95"),
            convergence_prev.get("supersede_affected_reviews_to_converge_p95"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
    }

    lane_waste_curr = latest.get("lane_waste") or {}
    lane_waste_prev = prior.get("lane_waste") or {}
    reviews_curr = lane_waste_curr.get("reviews_to_converge") or {}
    reviews_prev = lane_waste_prev.get("reviews_to_converge") or {}
    scopes_curr = lane_waste_curr.get("retarget_review_scope_counts") or {}
    scopes_prev = lane_waste_prev.get("retarget_review_scope_counts") or {}
    retarget_convergence_trend = {
        "reviews_to_converge": {
            # Sample size, not cost: this is how many chains CONVERGED
            # (merged) in the window. Fewer converged chains is a throughput
            # drop, not an improvement, and more is not a regression, so this
            # row carries the delta without a direction verdict.
            "count": _block(
                reviews_curr.get("count"),
                reviews_prev.get("count"),
                threshold_abs=0.5,
                classify=False,
            ),
            "median": _block(
                reviews_curr.get("median"),
                reviews_prev.get("median"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
            "p95": _block(
                reviews_curr.get("p95"),
                reviews_prev.get("p95"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
        },
        "retarget_review_scope_counts": {
            "delta": _block(
                scopes_curr.get("delta"),
                scopes_prev.get("delta"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
            "full": _block(
                scopes_curr.get("full"),
                scopes_prev.get("full"),
                threshold_abs=0.5,
                lower_is_better=True,
            ),
        },
        "stale_head_discard_count": _block(
            lane_waste_curr.get("stale_head_discard_count"),
            lane_waste_prev.get("stale_head_discard_count"),
            threshold_abs=0.5,
            lower_is_better=True,
        ),
    }

    return {
        "latest_version": latest.get("version_number"),
        "prior_version": prior.get("version_number"),
        "relay_medians": relay_trend,
        "lane_iterations": lane_iter_trend,
        "waste_taxonomy_shares": waste_trend,
        "checkpoint_recovery_waste_total": crw_trend,
        "outer_loop_convergence": convergence_trend,
        "retarget_convergence": retarget_convergence_trend,
    }
