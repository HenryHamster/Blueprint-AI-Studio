"""Pure metric / formatter helpers extracted from pipeline_regression_dispatcher.

These functions are synchronous, take no AsyncSession, and do no I/O. They are
the compute-and-format family from the dispatcher (layer set math, MTTI
percentile, etc.) that admin_digest already consumed via deferred imports.

The dispatcher re-imports these names at module load so existing
``from app.pipeline_regression_dispatcher import <name>`` callers and
internal references continue to work unchanged — this module is a sibling,
not a replacement.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.dispatcher_formatters import (
    _extract_recurrence_cycles,
    _parse_dispatcher_dt,
)
from app.models import (
    ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
    ESCALATION_REASON_ESCALATED_CROSS_LAYER,
    ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
    ESCALATION_REASON_ESCALATED_RECURRENCE,
    RegressionRemediationCase,
)
from app.regression_layer_classifier import (
    LAYER_DISPLAY_ORDER,
    aggregate_targeted_layers,
)
from app.time_utils import _utcnow


# Lookback window for the project-wide layer-defense ledger injected into meta-investigation
# prompts. Covers ~3 months of defense activity across all regression classes for the project.
_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS = 90


_META_ESCALATION_REASONS: frozenset[str] = frozenset(
    [
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
        ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
        ESCALATION_REASON_ESCALATED_RECURRENCE,
    ]
)

_ESCALATION_BUCKET_ALL_OTHER = "all_other"
_ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP = "compliance_followup"

_ESCALATION_BUCKET_ORDER: list[str] = [
    ESCALATION_REASON_ESCALATED_CROSS_LAYER,
    ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
    ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
    ESCALATION_REASON_ESCALATED_RECURRENCE,
    _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP,
    _ESCALATION_BUCKET_ALL_OTHER,
]


def compute_mtti_p50_sec(cases: list) -> float | None:
    """Return median seconds from escalation to operator acknowledgement.

    Filters to cases where both ``needs_manual_intervention_at`` and
    ``operator_acknowledged_at`` are set.  Returns ``None`` when no acked
    cases are present in the input.  Uses ``statistics.median`` — no numpy.
    """
    import statistics

    deltas = [
        (c.operator_acknowledged_at - c.needs_manual_intervention_at).total_seconds()
        for c in cases
        if c.needs_manual_intervention_at is not None
        and c.operator_acknowledged_at is not None
    ]
    return statistics.median(deltas) if deltas else None


def _layers_targeted_union(entries: list[dict[str, Any]] | None) -> set[str]:
    """Union of canonical ``layers_targeted`` across fix-log / cycle entries.

    Backwards-compatible name for callers (tests, admin_digest); the actual
    aggregation is owned by ``regression_layer_classifier.aggregate_targeted_layers``.
    Entries from ``_extract_recurrence_cycles`` carry an empty ``layers_targeted``
    list when ``layers_targeted_present`` is False, so the classifier's
    canonical-layer filter yields the same result as the prior dispatcher
    implementation.
    """
    return aggregate_targeted_layers(entries or [])


def compute_untargeted_layers(
    fingerprint_to_fix_log: list[dict[str, Any]] | None,
) -> list[str]:
    """Canonical layers never targeted by any prior fix in this case's history.

    Returns layers from ``LAYER_DISPLAY_ORDER`` (in canonical order)
    that do not appear in any entry's ``layers_targeted`` list. Unrecognized
    layer names in the fix-log are silently dropped so a typo or future-added
    layer never crashes the read path. Empty / missing / non-list fix-log
    yields the full canonical tuple — everything is untargeted by default.
    """
    if not isinstance(fingerprint_to_fix_log, list):
        return list(LAYER_DISPLAY_ORDER)
    targeted = _layers_targeted_union(fingerprint_to_fix_log)
    canonical = set(LAYER_DISPLAY_ORDER)
    targeted &= canonical
    return [layer for layer in LAYER_DISPLAY_ORDER if layer not in targeted]


def compute_class_layer_defense_counts(
    fix_logs: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Per-canonical-layer fix counts across a list of fix-log entries.

    Caller should pass the aggregated fix-log entries for all cases that share
    the same regression_class.  Returns one entry per canonical layer in
    canonical order (including layers with zero fixes) so the UI renders a
    stable six-row table.
    """
    counts: dict[str, int] = dict.fromkeys(LAYER_DISPLAY_ORDER, 0)
    for entry in fix_logs or []:
        if not isinstance(entry, dict):
            continue
        for layer in entry.get("layers_targeted") or []:
            if layer in counts:
                counts[layer] += 1
    return counts


def compute_layer_defense_density(
    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Return per-canonical-layer defense activity over a lookback window.

    Walks ``(case_id, fix_log_entry)`` pairs from
    ``RegressionRemediationCase.fingerprint_to_fix_log``, applies the same
    ``merged_at`` cutoff used by ``_build_layer_defense_ledger``, and returns
    one entry per canonical layer in canonical order — including layers with
    zero activity. Each entry has ``layer``, ``fix_count``, ``last_fix_at``
    (ISO timestamp of the most recent qualifying ``merged_at`` for that layer,
    or ``None``), and ``sample_case_ids`` (up to 5 distinct case ids whose fix
    logs contributed an entry, ordered most-recent-first).
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    layer_state: dict[str, dict[str, Any]] = {
        layer: {
            "fix_count": 0,
            "last_fix_at": None,  # datetime
            "case_contribs": [],  # list[(merged_dt, case_id)] for sampling
        }
        for layer in LAYER_DISPLAY_ORDER
    }

    for case_id, entry in all_fix_logs_with_case_ids:
        if not isinstance(entry, dict):
            continue
        merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
        if merged_dt is None or merged_dt < cutoff:
            continue
        layers_targeted = entry.get("layers_targeted") or []
        if not layers_targeted:
            continue
        for layer in layers_targeted:
            if layer not in layer_state:
                continue
            s = layer_state[layer]
            s["fix_count"] += 1
            if s["last_fix_at"] is None or merged_dt > s["last_fix_at"]:
                s["last_fix_at"] = merged_dt
            s["case_contribs"].append((merged_dt, str(case_id)))

    result: list[dict[str, Any]] = []
    for layer in LAYER_DISPLAY_ORDER:
        s = layer_state[layer]
        contribs = sorted(s["case_contribs"], key=lambda p: p[0], reverse=True)
        seen: set[str] = set()
        sample_ids: list[str] = []
        for _dt, cid in contribs:
            if cid in seen:
                continue
            seen.add(cid)
            sample_ids.append(cid)
            if len(sample_ids) >= 5:
                break
        result.append(
            {
                "layer": layer,
                "fix_count": s["fix_count"],
                "last_fix_at": s["last_fix_at"].isoformat()
                if s["last_fix_at"] is not None
                else None,
                "sample_case_ids": sample_ids,
            }
        )
    return result


def _compute_layer_fragility_stats(
    all_fix_logs: list[dict[str, Any]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """Return per-layer fragility stats from fix-log entries within the lookback window.

    Each canonical layer entry contains: fix_count, avg_hold_days (float|None),
    still_holding_count, held_gt_7d_count, retripped_within_48h_count.
    avg_hold_days is None when no re-tripped entries exist (i.e. all defenses
    are still holding or no fixes recorded).
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    layer_stats: dict[str, dict[str, Any]] = {
        layer: {
            "fix_count": 0,
            "hold_days": [],
            "still_holding_ages": [],
            "retripped_within_48h": 0,
            "held_gt_7d": 0,
            "single_layer_fix_count": 0,
            "single_layer_hold_days": [],
            "multi_layer_fix_count": 0,
            "multi_layer_hold_days": [],
        }
        for layer in LAYER_DISPLAY_ORDER
    }

    for entry in all_fix_logs:
        if not isinstance(entry, dict):
            continue
        merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
        if merged_dt is None or merged_dt < cutoff:
            continue
        layers_targeted = entry.get("layers_targeted") or []
        if not layers_targeted:
            continue
        recovered_dt = _parse_dispatcher_dt(entry.get("recovered_at"))
        age_days = (now - merged_dt).total_seconds() / 86400.0
        is_multi_layer = len(layers_targeted) >= 2

        for layer in layers_targeted:
            if layer not in layer_stats:
                continue
            s = layer_stats[layer]
            s["fix_count"] += 1
            if is_multi_layer:
                s["multi_layer_fix_count"] += 1
            else:
                s["single_layer_fix_count"] += 1
            if recovered_dt is not None:
                hold = (recovered_dt - merged_dt).total_seconds() / 86400.0
                s["hold_days"].append(hold)
                if is_multi_layer:
                    s["multi_layer_hold_days"].append(hold)
                else:
                    s["single_layer_hold_days"].append(hold)
                if hold < 2.0:
                    s["retripped_within_48h"] += 1
                if hold > 7.0:
                    s["held_gt_7d"] += 1
            else:
                s["still_holding_ages"].append(age_days)
                if age_days > 7.0:
                    s["held_gt_7d"] += 1

    def _avg_or_none(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    result: dict[str, dict[str, Any]] = {}
    for layer in LAYER_DISPLAY_ORDER:
        s = layer_stats[layer]
        hold_days_list = s["hold_days"]
        result[layer] = {
            "fix_count": s["fix_count"],
            "avg_hold_days": _avg_or_none(hold_days_list),
            "still_holding_count": len(s["still_holding_ages"]),
            "held_gt_7d_count": s["held_gt_7d"],
            "retripped_within_48h_count": s["retripped_within_48h"],
            "single_layer_fix_count": s["single_layer_fix_count"],
            "single_layer_avg_hold_days": _avg_or_none(s["single_layer_hold_days"]),
            "multi_layer_fix_count": s["multi_layer_fix_count"],
            "multi_layer_avg_hold_days": _avg_or_none(s["multi_layer_hold_days"]),
        }
    return result


def _compliance_followup_fix_idx(raw_log: list) -> int:
    """Return the 0-based fix-cycle index of the first fix entry after a compliance_followup_spawned marker.

    Returns -1 if no such entry exists (no followup spawned or the spawned task has not yet merged).
    """
    saw_spawn = False
    fix_idx = 0
    for entry in raw_log:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "compliance_followup_spawned":
            saw_spawn = True
        elif entry.get("fix_pr_number") is not None:
            if saw_spawn:
                return fix_idx
            fix_idx += 1
    return -1


def compute_escalation_outcome_breakdown(
    cases: list[RegressionRemediationCase],
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate case outcomes by escalation_reason bucket.

    Groups cases into five buckets — one per meta-escalation reason plus
    'all_other' for None or any non-meta value — and computes per-bucket:
    - merged_count: number of cases whose first cycle merged within the window
    - retripped_within_30d_count: of those, how many had a subsequent cycle
      within 30 days of the first cycle's merged_at
    - durable_resolution_rate: 1 - retripped / merged, or None when merged_count == 0
    - median_hold_days: median of (recovered_at - merged_at) in days for cases
      whose first cycle has both timestamps
    - in_flight_excluded_count: cases whose every fix entry has merged_at == None

    Uses _extract_recurrence_cycles to walk fingerprint_to_fix_log. Cases whose
    all fix-log entries have merged_at == None are counted as in-flight and excluded
    from merged_count and all rate denominators.
    """
    if now is None:
        now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)
    retrip_window = timedelta(days=30)

    def _bucket_for(reason: str | None, raw_log: list) -> str:
        if (
            reason == ESCALATION_REASON_ESCALATED_CROSS_LAYER
            and _compliance_followup_fix_idx(raw_log) >= 0
        ):
            return _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP
        if reason in _META_ESCALATION_REASONS:
            return str(reason)
        return _ESCALATION_BUCKET_ALL_OTHER

    bucket_state: dict[str, dict[str, Any]] = {
        b: {
            "merged_count": 0,
            "retripped_within_30d_count": 0,
            "hold_days": [],
            "in_flight_excluded_count": 0,
            "prevention_prevented": 0,
            "prevention_recurred": 0,
        }
        for b in _ESCALATION_BUCKET_ORDER
    }

    for case in cases:
        raw_log = case.fingerprint_to_fix_log or []
        bucket = _bucket_for(getattr(case, "escalation_reason", None), raw_log)
        st = bucket_state[bucket]

        fix_entries = [
            e
            for e in raw_log
            if isinstance(e, dict) and e.get("fix_pr_number") is not None
        ]
        if fix_entries and all(e.get("merged_at") is None for e in fix_entries):
            st["in_flight_excluded_count"] += 1
            continue

        cycles = _extract_recurrence_cycles(case)
        # For compliance_followup cases, start from the cycle after the followup was spawned
        # so metrics reflect the compliance followup fix's durability, not the original non-compliant fix.
        if bucket == _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP:
            cf_idx = _compliance_followup_fix_idx(raw_log)
            effective_cycles = cycles[cf_idx:] if 0 <= cf_idx < len(cycles) else []
        else:
            effective_cycles = cycles
        if not effective_cycles:
            continue

        first = effective_cycles[0]
        merged_at = first["merged_at"]

        # Prevention rate: observe defenses with a complete 30d window (merged >= 30d ago).
        # This is independent of the merged_count lookback filter.
        if merged_at is not None and merged_at + retrip_window <= now:
            _retripped = any(
                c["merged_at"] is not None
                and c["merged_at"] <= merged_at + retrip_window
                for c in effective_cycles[1:]
            )
            if _retripped:
                st["prevention_recurred"] += 1
            else:
                st["prevention_prevented"] += 1

        if merged_at is None or not (cutoff <= merged_at <= now):
            continue

        st["merged_count"] += 1

        recovered_at = first.get("recovered_at")
        if isinstance(recovered_at, datetime):
            hold = (recovered_at - merged_at).total_seconds() / 86400.0
            if hold >= 0:
                st["hold_days"].append(hold)

        for later in effective_cycles[1:]:
            next_merged = later["merged_at"]
            if next_merged is not None and next_merged <= merged_at + retrip_window:
                st["retripped_within_30d_count"] += 1
                break

    result: dict[str, Any] = {}
    for bucket in _ESCALATION_BUCKET_ORDER:
        s = bucket_state[bucket]
        mc = s["merged_count"]
        rc = s["retripped_within_30d_count"]
        hold_list: list[float] = s["hold_days"]
        median_hold: float | None = None
        if hold_list:
            sorted_hold = sorted(hold_list)
            n = len(sorted_hold)
            mid = n // 2
            median_hold = (
                sorted_hold[mid]
                if n % 2 == 1
                else (sorted_hold[mid - 1] + sorted_hold[mid]) / 2.0
            )
        _prev = s["prevention_prevented"]
        _prec = s["prevention_recurred"]
        _pdenom = _prev + _prec
        result[bucket] = {
            "merged_count": mc,
            "retripped_within_30d_count": rc,
            "durable_resolution_rate": (1.0 - rc / mc) if mc > 0 else None,
            "median_hold_days": median_hold,
            "in_flight_excluded_count": s["in_flight_excluded_count"],
            "prevention_rate_30d": (_prev / _pdenom) if _pdenom > 0 else None,
            "recurrences_observed": _prec,
        }

    return {
        "by_reason": result,
        "lookback_days": lookback_days,
        "generated_at": now.isoformat(),
    }


def compute_first_attempt_compliance_trend(
    cases: list[RegressionRemediationCase],
    *,
    num_windows: int = 6,
    window_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-window first-attempt cross-layer compliance rate.

    For each cross-layer-mandated case (first fix-log entry with
    spawn_was_cross_layer=True), classifies the outcome of that first attempt:

    - ``first_attempt_compliant``: layers_targeted present and >=2 distinct
      canonical layers (including recommended_unmodifiable_layers).
    - ``followup_rescued``: first fix had <2 canonical layers AND a
      compliance_followup_spawned marker with a subsequent merged fix exists.
    - ``awaiting_backfill``: layers_targeted absent, or non-compliant without a
      merged followup yet; excluded from the rate denominator.

    Cases are bucketed by merged_at of the first cross-layer fix entry into
    ``num_windows`` rolling ``window_days``-wide windows ending at ``now``
    (oldest window first). ``rate`` = first_attempt_compliant /
    (first_attempt_compliant + followup_rescued), or None when denominator is 0.
    """
    if now is None:
        now = _utcnow()
    canonical = set(LAYER_DISPLAY_ORDER)

    # Build window boundaries oldest-first
    windows: list[dict[str, Any]] = []
    for i in range(num_windows - 1, -1, -1):
        w_end = now - timedelta(days=i * window_days)
        w_start = w_end - timedelta(days=window_days)
        windows.append(
            {
                "_w_start": w_start,
                "_w_end": w_end,
                "window_start": w_start.isoformat(),
                "window_end": w_end.isoformat(),
                "first_attempt_compliant": 0,
                "followup_rescued": 0,
                "awaiting_backfill": 0,
            }
        )

    for case in cases:
        raw_log = case.fingerprint_to_fix_log or []
        if not isinstance(raw_log, list):
            continue

        # Find the first fix entry carrying the cross-layer mandate
        first_entry: dict[str, Any] | None = None
        for entry in raw_log:
            if (
                isinstance(entry, dict)
                and entry.get("fix_pr_number") is not None
                and entry.get("spawn_was_cross_layer")
            ):
                first_entry = entry
                break
        if first_entry is None:
            continue

        merged_at = _parse_dispatcher_dt(first_entry.get("merged_at"))
        if merged_at is None:
            continue

        bucket: dict[str, Any] | None = None
        for w in windows:
            if w["_w_start"] <= merged_at < w["_w_end"]:
                bucket = w
                break
        if bucket is None:
            continue

        if "layers_targeted" not in first_entry:
            bucket["awaiting_backfill"] += 1
            continue

        layers = set(first_entry.get("layers_targeted") or []) & canonical
        unmodifiable = (
            set(first_entry.get("recommended_unmodifiable_layers") or []) & canonical
        )
        if len(layers | unmodifiable) >= 2:
            bucket["first_attempt_compliant"] += 1
        elif _compliance_followup_fix_idx(raw_log) >= 0:
            bucket["followup_rescued"] += 1
        else:
            bucket["awaiting_backfill"] += 1

    result_windows = []
    for w in windows:
        denom = w["first_attempt_compliant"] + w["followup_rescued"]
        result_windows.append(
            {
                "window_start": w["window_start"],
                "window_end": w["window_end"],
                "first_attempt_compliant": w["first_attempt_compliant"],
                "followup_rescued": w["followup_rescued"],
                "awaiting_backfill": w["awaiting_backfill"],
                "rate": w["first_attempt_compliant"] / denom if denom > 0 else None,
            }
        )

    return {
        "windows": result_windows,
        "num_windows": num_windows,
        "window_days": window_days,
        "generated_at": now.isoformat(),
    }


def compute_avg_prevention_rate_7d(
    cases: list[RegressionRemediationCase],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Unweighted average prevention rate for defenses whose 30d window closed in the last 7d.

    A defense is eligible when its first cycle's merged_at falls in [now-37d, now-30d]
    (the 30d observation window opened and closed within the past 7 days).
    A defense "prevented" when no subsequent cycle occurred within 30d of that merge.

    Returns a dict with keys: avg_rate (float|None), eligible_count, prevented_count,
    recurred_count.
    """
    if now is None:
        now = _utcnow()
    window_open = now - timedelta(days=37)
    window_close = now - timedelta(days=30)
    retrip_window = timedelta(days=30)

    prevented = 0
    recurred = 0

    for case in cases:
        raw_log = case.fingerprint_to_fix_log or []
        fix_entries = [
            e
            for e in raw_log
            if isinstance(e, dict) and e.get("fix_pr_number") is not None
        ]
        if fix_entries and all(e.get("merged_at") is None for e in fix_entries):
            continue
        cycles = _extract_recurrence_cycles(case)
        if not cycles:
            continue
        first = cycles[0]
        merged_at = first["merged_at"]
        if merged_at is None:
            continue
        if not (window_open <= merged_at <= window_close):
            continue
        retripped = any(
            c["merged_at"] is not None and c["merged_at"] <= merged_at + retrip_window
            for c in cycles[1:]
        )
        if retripped:
            recurred += 1
        else:
            prevented += 1

    eligible = prevented + recurred
    return {
        "avg_rate": prevented / eligible if eligible > 0 else None,
        "eligible_count": eligible,
        "prevented_count": prevented,
        "recurred_count": recurred,
    }


def _build_layer_defense_ledger(
    all_fix_logs: list[dict[str, Any]],
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> str:
    """Build a RECENT DEFENSE ACTIVITY BY LAYER block for meta-investigation prompts.

    Aggregates fingerprint_to_fix_log entries from all RegressionRemediationCase rows
    for a project, filters to merged_at within lookback_days, and computes per-layer
    statistics. All six canonical layers always appear. Returns empty string when
    all_fix_logs is empty.
    """
    if not all_fix_logs:
        return ""

    stats = _compute_layer_fragility_stats(all_fix_logs, lookback_days)

    lines: list[str] = [
        "",
        f"RECENT DEFENSE ACTIVITY BY LAYER (last {lookback_days} days)",
    ]
    for layer in LAYER_DISPLAY_ORDER:
        s = stats[layer]
        if s["fix_count"] == 0:
            lines.append(
                f"  {layer}: no defenses added here in the last {lookback_days} days"
            )
        else:
            parts = [f"  {layer}: {s['fix_count']} defense(s) added"]
            avg_hold = s["avg_hold_days"]
            if avg_hold is not None:
                parts.append(f"avg hold {avg_hold:.1f}d before re-trip")
            if s["still_holding_count"] > 0:
                parts.append(f"{s['still_holding_count']} still holding")
            if s["held_gt_7d_count"] > 0:
                parts.append(f"{s['held_gt_7d_count']} held >7d")
            if s["retripped_within_48h_count"] > 0:
                parts.append(
                    f"{s['retripped_within_48h_count']} retripped <48h (fragile)"
                )
            lines.append("; ".join(parts))
    return "\n".join(lines)


def _compute_recovery_layer_mismatch(
    fix_log: list[dict[str, Any]] | None,
    symptoms_by_layer: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Return a recovery_layer_mismatch entry when fix didn't cover all symptom layers.

    Compares the union of layers_targeted across fix-log entries against the
    layers that fired symptoms during the case lifecycle.  Returns None when
    there is no data to compare or when all symptom layers were addressed.
    """
    fix_layers = _layers_targeted_union(fix_log)
    symptom_layers = set((symptoms_by_layer or {}).keys())
    if not fix_layers or not symptom_layers:
        return None
    unaddressed = symptom_layers - fix_layers
    if not unaddressed:
        return None
    return {
        "type": "recovery_layer_mismatch",
        "evaluated_at": now.isoformat(),
        "fix_layers": sorted(fix_layers),
        "symptom_layers": sorted(symptom_layers),
        "unaddressed_layers": sorted(unaddressed),
    }
