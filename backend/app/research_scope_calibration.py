"""Finding-scope calibration and preventive-context helpers.

Extracted from app.research_completion to keep that module under its
size guardrail. Hosts the pre-spawn scope-upgrade heuristics
(``_calibrate_finding_scope`` and its supporting threshold/ratio helpers),
the prompt-telemetry builder, the compute-spend-threshold wrapper, and the
preventive-context builder.

These helpers are re-exported from app.research_completion so existing
import paths (``from app.research_completion import _calibrate_finding_scope``)
and the in-module ``_handle_research_completion`` name lookups continue to
resolve unchanged.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.compute_experiment_contract import resolve_compute_spend_threshold
from app.scope_complexity import _composite_complexity_score
from app.shared_state import runtime_config as _runtime_config
from app.spawn_calibration import SCOPE_FILE_BUDGETS
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Minimum medium-scope exploration fraction. Prevents the adaptive formula from
# collapsing to 0 for a direction with no successful medium merges, which would
# permanently shut down medium exploration and prevent data collection.
MEDIUM_EXPLORATION_FLOOR = 0.05


def _avg_medium_expansion_ratio(
    directions: List[Dict[str, Any]],
) -> float | None:
    """Aggregate medium-scope expansion ratios across ``directions``'s last 3
    research_history entries. Prefers max(mean_merged, mean_failed) so the
    failed-task overflow signal biases conservatively. Returns None when no
    directions contribute any ratio data.
    """
    _merged: list[float] = []
    _failed: list[float] = []
    for _d in directions:
        for entry in (_d.get("research_history") or [])[-3:]:
            _m = (entry.get("scope_breakdown") or {}).get("medium", {})
            _r = _m.get("avg_expansion_ratio")
            if isinstance(_r, (int, float)):
                _merged.append(float(_r))
            _rf = _m.get("avg_expansion_ratio_failed")
            if isinstance(_rf, (int, float)):
                _failed.append(float(_rf))
    _candidates = []
    if _merged:
        _candidates.append(sum(_merged) / len(_merged))
    if _failed:
        _candidates.append(sum(_failed) / len(_failed))
    return max(_candidates) if _candidates else None


def _compute_prompt_telemetry(
    full_prompt: str,
    related_files: List[str],
    scope: str,
) -> Dict[str, Any]:
    """Lightweight prompt characteristics for outcome correlation.

    Stored in task.context['prompt_telemetry'] at spawn time and aggregated by
    direction_outcomes to surface patterns like "prompts mentioning tests merge
    at higher rates than those that don't".
    """
    import re

    text = full_prompt or ""
    budget = SCOPE_FILE_BUDGETS.get(scope, {}).get("total_files_max") or 0
    n_related = len(related_files or [])
    return {
        "char_count": len(text),
        "related_file_count": n_related,
        "mentions_tests": bool(re.search(r"\btest", text, re.IGNORECASE)),
        "mentions_symbols": bool(
            re.search(
                r"\b[a-z][a-zA-Z0-9_]*[a-z][A-Z]\w*\b|\b[A-Z][a-zA-Z0-9_]*[a-z]\w*\b\(|\b[a-z_][a-z0-9_]+\(",
                text,
            )
        ),
        "files_to_budget_ratio": round(n_related / budget, 3) if budget > 0 else None,
    }


def _resolve_compute_spend_threshold(
    runtime_config: Dict[str, Any],
    direction: Dict[str, Any] | None,
) -> float:
    """Backward-compatible wrapper for tests/callers importing the old helper."""
    return resolve_compute_spend_threshold(runtime_config, direction)


def _outcome_complexity_threshold(
    direction: Dict[str, Any] | None,
) -> float | None:
    """Derive the escalation threshold for medium-scope findings from the
    direction's recent avg_complexity_merged / avg_complexity_failed observations
    (last 3 research_history entries, 'medium' scope_breakdown).

    Returns midpoint(merged, failed) when both exist, avg_failed when only failed
    exists, or None when no data is available.
    """
    if not direction:
        return None
    merged_vals: list[float] = []
    failed_vals: list[float] = []
    for entry in (direction.get("research_history") or [])[-3:]:
        med = (entry.get("scope_breakdown") or {}).get("medium", {}) or {}
        m = med.get("avg_complexity_merged")
        f = med.get("avg_complexity_failed")
        if isinstance(m, (int, float)):
            merged_vals.append(float(m))
        if isinstance(f, (int, float)):
            failed_vals.append(float(f))
    avg_merged = sum(merged_vals) / len(merged_vals) if merged_vals else None
    avg_failed = sum(failed_vals) / len(failed_vals) if failed_vals else None
    if avg_merged is not None and avg_failed is not None:
        return (avg_merged + avg_failed) / 2.0
    if avg_failed is not None:
        return avg_failed
    return None


def _cochange_decomposition_suppressed(
    direction: Dict[str, Any] | None,
    min_sub_findings: int = 3,
    merge_rate_threshold: float = 0.4,
) -> bool:
    """Return True when cochange auto-decomposition should be skipped for this
    direction because historical sub-finding merge rate is poor.

    Aggregates ``cochange_decomposition`` entries across the direction's
    ``research_history``. When cumulative sub_findings_total >= min_sub_findings
    and merge rate < merge_rate_threshold, callers should skip auto-splitting
    to avoid fragmenting findings that empirically don't benefit from it.
    """
    if not direction:
        return False
    total = 0
    merged = 0
    for entry in direction.get("research_history") or []:
        cd = entry.get("cochange_decomposition") or {}
        total += cd.get("sub_findings_total", 0) or 0
        merged += cd.get("sub_findings_merged", 0) or 0
    if total < min_sub_findings:
        return False
    return (merged / total) < merge_rate_threshold


def _calibrate_finding_scope(
    finding: Dict[str, Any],
    scope: str,
    direction: Dict[str, Any] | None,
    cochange_index: Dict[str, Any] | None,
    all_directions: List[Dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    """Apply pre-spawn scope-upgrade heuristics (file count, cross-module span,
    expansion-ratio prediction, composite complexity).

    Returns (new_scope, scope_calibrated_from). scope_calibrated_from is None
    when no heuristic fired, so callers can preserve any prior calibration tag.
    Mutates ``finding`` to record ``_complexity_score`` / ``_complexity_signals``
    when the composite-complexity check runs.
    """
    title = finding.get("title", "")
    scope_calibrated_from: str | None = None

    if scope == "small":
        _heuristic_files = finding.get("related_files") or []
        if len(_heuristic_files) >= 4:
            logger.info(
                "[RESEARCH] Upgrading scope small→medium for '%s' "
                "(file-count heuristic: %d related_files >= 4)",
                title,
                len(_heuristic_files),
            )
            scope = "medium"
            scope_calibrated_from = "small"

    if scope == "medium":
        _heuristic_files = finding.get("related_files") or []
        _medium_file_budget = (
            SCOPE_FILE_BUDGETS.get("medium", {}).get("total_files_max") or 16
        )
        # There is deliberately no cross-module (top-level directory span)
        # escalation here: directory span is not evidence that work decomposes
        # — a cross-stack contract change always spans top-level dirs, and
        # routing it to plan decomposition produces per-module fragments that
        # merge individually while breaking the shared contract.
        if len(_heuristic_files) >= _medium_file_budget:
            logger.info(
                "[RESEARCH] Upgrading scope medium→large for '%s' "
                "(file-count heuristic: %d related_files >= medium budget %d)",
                title,
                len(_heuristic_files),
                _medium_file_budget,
            )
            scope = "large"
            scope_calibrated_from = "medium"
        elif _heuristic_files:
            # Predictive expansion-ratio check: historical avg_expansion_ratio
            # (actual files touched / expected related_files) multiplied by this finding's
            # related_files predicts how many files the task will actually touch. If that
            # exceeds the medium file budget (SCOPE_FILE_BUDGETS), route
            # to large for plan decomposition before burning 5-10 minutes on a task that
            # will almost certainly scope-overflow during implementation.
            _avg_ratio = _avg_medium_expansion_ratio([direction]) if direction else None
            _ratio_source = "direction"
            if _avg_ratio is None and all_directions:
                _avg_ratio = _avg_medium_expansion_ratio(all_directions)
                _ratio_source = "project"
            if _avg_ratio is None:
                _pipeline_ratio = (
                    _runtime_config.get("pipeline_scope_expansion_ratios") or {}
                ).get("medium")
                if isinstance(_pipeline_ratio, (int, float)):
                    _avg_ratio = float(_pipeline_ratio)
                    _ratio_source = "pipeline"
            if _avg_ratio is None:
                # Conservative prior for brand-new projects with no expansion-ratio
                # data anywhere — still applies the safety net rather than skipping.
                _avg_ratio = 2.0
                _ratio_source = "default"
            _predicted_files = len(_heuristic_files) * _avg_ratio
            _medium_budget = (
                SCOPE_FILE_BUDGETS.get("medium", {}).get("total_files_max") or 16
            )
            if _predicted_files > _medium_budget:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(expansion-ratio prediction [%s]: %d related_files × %.2f avg_ratio "
                    "= %.1f predicted files > %d threshold)",
                    title,
                    _ratio_source,
                    len(_heuristic_files),
                    _avg_ratio,
                    _predicted_files,
                    _medium_budget,
                )
                scope = "large"
                scope_calibrated_from = "medium"
        # Composite complexity score: combines file count, historical expansion,
        # cochange cluster density, and cross-module span into a single merge-
        # probability estimate. Upgrades when score indicates low P(merge) even if
        # individual binary checks above let the task through.
        if scope == "medium":
            # Unconditional escalation: if pipeline-wide medium merge rate has
            # collapsed to at-or-below the exploration floor, medium scope is
            # proven unviable system-wide. Skip the composite-complexity check
            # entirely and route to large for plan decomposition.
            _pipeline_medium_rate = (
                _runtime_config.get("pipeline_scope_merge_rates") or {}
            ).get("medium")
            if (
                isinstance(_pipeline_medium_rate, (int, float))
                and float(_pipeline_medium_rate) <= MEDIUM_EXPLORATION_FLOOR
            ):
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(pipeline medium merge rate %.3f <= exploration floor %.3f — "
                    "unconditional escalation)",
                    title,
                    float(_pipeline_medium_rate),
                    MEDIUM_EXPLORATION_FLOOR,
                )
                return "large", "medium:pipeline_floor"
        if scope == "medium":
            _cc_score, _cc_signals = _composite_complexity_score(
                finding.get("related_files") or [],
                direction,
                cochange_index,
            )
            finding["_complexity_score"] = round(_cc_score, 3)
            finding["_complexity_signals"] = _cc_signals
            # Adaptive escalation threshold. Base preference order:
            #   1. Direction outcome-derived midpoint of avg_complexity_merged /
            #      avg_complexity_failed (last 3 cycles) — empirically calibrated.
            #   2. Fixed 0.45 baseline when no outcome data exists yet.
            # The pipeline-wide medium merge-rate tightening is layered on top as
            # a secondary adjustment so poor pipeline-wide performance still
            # raises the bar during degradation regardless of the base source.
            _outcome_base = _outcome_complexity_threshold(direction)
            _cc_threshold = _outcome_base if _outcome_base is not None else 0.45
            _pipeline_medium = (
                _runtime_config.get("pipeline_scope_merge_rates") or {}
            ).get("medium")
            if isinstance(_pipeline_medium, (int, float)) and _pipeline_medium < 0.4:
                _cc_threshold = min(
                    0.75, _cc_threshold + (0.4 - float(_pipeline_medium)) * 0.5
                )
            if _cc_score < _cc_threshold:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(composite complexity %.2f < %.2f, pipeline_medium=%s, signals=%s)",
                    title,
                    _cc_score,
                    _cc_threshold,
                    f"{_pipeline_medium:.2f}"
                    if isinstance(_pipeline_medium, (int, float))
                    else "n/a",
                    _cc_signals,
                )
                scope = "large"
                scope_calibrated_from = "medium"
    return scope, scope_calibrated_from


def _build_preventive_context(
    finding: Dict[str, Any],
    directions_by_id: Dict[str, Dict[str, Any]],
    project_anomalies: Dict[str, Dict[str, Any]] | None,
) -> Dict[str, Any] | None:
    """Produce a concise preventive warning when a finding's related_files
    overlap with recently-diagnosed failure hotspots or active anomaly patterns.

    Returns None when no overlap exists, so callers can skip injection.
    """
    related_files = [
        f for f in (finding.get("related_files") or []) if isinstance(f, str)
    ]
    if not related_files:
        return None
    related_set = set(related_files)
    cutoff = _utcnow() - timedelta(days=14)

    hotspot: Dict[str, Any] | None = None
    for d in directions_by_id.values():
        affected = [
            a for a in (d.get("diagnosed_affected_files") or []) if isinstance(a, str)
        ]
        overlap = sorted(related_set.intersection(affected))
        if not overlap:
            continue
        completed_at = d.get("diagnosis_completed_at")
        if not isinstance(completed_at, str):
            continue
        try:
            ts = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        hotspot = {
            "overlap_files": overlap,
            "summary": (d.get("diagnosis_summary") or "").strip()[:220],
            "fingerprint": d.get("last_remediation_fingerprint"),
        }
        break

    anomaly_match: Dict[str, Any] | None = None
    if project_anomalies:
        modules = {f.split("/", 1)[0].lower() for f in related_files if "/" in f}
        for key, info in project_anomalies.items():
            # Project anomaly entries are sometimes stored as raw strings rather
            # than the {"latest_details": ...} dict shape, which previously
            # AttributeError'd here and crashed the entire research dispatch
            # loop (every research task spawned 0 children, no log, no status).
            if not isinstance(info, dict):
                continue
            details = (info.get("latest_details") or "").lower()
            if not details:
                continue
            if any(m and m in details for m in modules):
                anomaly_match = {
                    "key": key,
                    "details": info.get("latest_details") or "",
                }
                break

    if not hotspot and not anomaly_match:
        return None

    parts: list[str] = []
    if hotspot:
        files_str = ", ".join(hotspot["overlap_files"][:3])
        fp_str = (
            f" (fingerprint {hotspot['fingerprint']})" if hotspot["fingerprint"] else ""
        )
        root = hotspot["summary"] or "see direction diagnosis"
        parts.append(
            f"Files {files_str} were recently involved in a recurring failure{fp_str}. "
            f"Root cause: {root}."
        )
    if anomaly_match:
        parts.append(
            f"Active cross-direction anomaly {anomaly_match['key']}: "
            f"{anomaly_match['details'][:140]}"
        )
    parts.append("Ensure your changes don't reintroduce this pattern.")
    warning = " ".join(parts)[:500]
    return {
        "warning": warning,
        "fingerprint": hotspot["fingerprint"] if hotspot else None,
        "overlap_files": hotspot["overlap_files"] if hotspot else [],
        "anomaly_key": anomaly_match["key"] if anomaly_match else None,
    }
