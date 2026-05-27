"""Pure formatting and parsing helpers extracted from pipeline_regression_dispatcher.

No async, no SQLAlchemy, no shared_state. All functions are pure (only depend on
stdlib, app.models, app.regression_layer_classifier, and app.time_utils).

Imported wholesale by pipeline_regression_dispatcher so all existing call sites
and external importers continue to resolve the same names.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.models import RegressionRemediationCase, Task
from app.regression_layer_classifier import (
    LAYER_DISPLAY_ORDER,
    aggregate_targeted_layers,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (moved from pipeline_regression_dispatcher to break the dependency)
# ---------------------------------------------------------------------------

# Fragile-recurrence meta-investigation thresholds.
# A case is "fragile" if it has recurred >= this many times OR any prior fix cycle
# held < _FRAGILE_RECURRENCE_SHORT_HOLD_DAYS before the signal re-tripped.
_FRAGILE_RECURRENCE_MIN_COUNT = 3
_FRAGILE_RECURRENCE_SHORT_HOLD_DAYS = 2.0

# SR false-positive signal thresholds. A category's FP rate must exceed this
# floor in BOTH the current and previous window (>=2 consecutive) before
# tripping an investigation.
_SR_FP_RATE_FLOOR = 0.40

# Cross-case error-fingerprint lookback window.
_CROSS_CASE_FINGERPRINT_LOOKBACK = timedelta(days=14)


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _format_signal_lines(
    signal_name: str,
    regression_class: str,
    prior_rate: float | None,
    current_rate: float | None,
    sample_size: int,
    extras: list[str] | None = None,
) -> str:
    prior_str = f"{prior_rate:.1%}" if prior_rate is not None else "N/A"
    curr_str = f"{current_rate:.1%}" if current_rate is not None else "N/A"
    lines = [
        f"Pipeline regression detected ({signal_name} / class={regression_class}).",
        f"Prior rate: {prior_str}",
        f"Current rate: {curr_str}",
        f"Sample size: {sample_size}",
    ]
    if extras:
        lines.extend(extras)
    lines.append(
        "\nFIRST: triage whether this signal actually represents a regression. "
        "Set `verdict` to 'not_a_regression' (not 'real_regression') when ANY "
        "of these apply:\n"
        "  - The behavior the signal observed is intentional / by design "
        "(e.g. a pipeline step that is intentionally skipped under known "
        "conditions, a feature flag deliberately gating a step, a recently "
        "merged change that altered the expected baseline).\n"
        "  - The sample size is too small for the prior/current delta to be "
        "statistically meaningful (random variance, not a real shift).\n"
        "  - The 'regression' is just a known-incomplete pipeline step (e.g. "
        "the agent did not run the full backend test suite — that is the "
        "documented design, not a bug).\n"
        "  - The signal is observing transient infrastructure noise (one-off "
        "VM crash, flaky external dep) with no code-side root cause.\n"
        "When verdict='not_a_regression', populate `verdict_reason` with one "
        "to three sentences citing concrete evidence (file paths, design "
        "doc passages, prior commit SHAs, etc.). Leave recommended_fix "
        "empty in that case — DO NOT fabricate a fix to satisfy the schema.\n"
        "\nOTHERWISE (verdict='real_regression'), diagnose the underlying "
        "root cause and produce the structured diagnosis JSON report "
        "({summary, root_cause, recommended_fix, severity, confidence, "
        "affected_files, verdict, verdict_reason}). Do NOT prescribe a fix "
        "yourself — let the investigate→fix chain produce the remediation."
    )
    return "\n".join(lines)


def _format_sr_fp_signal_lines(
    regression_class: str,
    current_fp_rate: float,
    prior_fp_rate: float,
    sample_size: int,
) -> str:
    cat = (
        regression_class.split(":", 1)[1]
        if ":" in regression_class
        else regression_class
    )
    lines = [
        f"SR over-paranoia detected (sr_false_positive / class={regression_class}).",
        f"Category: {cat}",
        f"Current false-positive rate: {current_fp_rate:.1%}",
        f"Prior window false-positive rate: {prior_fp_rate:.1%}",
        f"Sample size (human-reviewed tasks): {sample_size}",
        "",
        "A false positive means the self-review flagged this category as FAILED but no "
        "fresh reviewer confirmed the issue. Both windows are above the "
        f"{_SR_FP_RATE_FLOOR:.0%} floor, indicating a persistent over-paranoia pattern.",
        "",
        "Investigate whether the SR check criterion for this category is too aggressive. "
        "Specifically:",
        "1. Review the self-review prompt wording for this category — does it invite "
        "false positives by being too broadly worded?",
        "2. Check whether the verdict threshold needs tightening (e.g. the check flags "
        "on minor issues that reviewers consistently dismiss).",
        "3. Look at recent tasks where this category was flagged FAILED but the fresh "
        "reviewer passed it — what pattern do they share?",
        "4. Propose a prompt tweak or criterion adjustment, NOT an application-code change.",
        "",
        "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, "
        "severity, confidence, affected_files}) where recommended_fix describes the "
        "prompt/criterion change, not a code fix.",
    ]
    return "\n".join(lines)


def _format_prior_attempt_context(
    case: RegressionRemediationCase,
    *,
    omit_pr_line: bool = False,
) -> str:
    """Build a prior-attempt notice to append when re-spawning after >= 2 failures.

    Only called when existing_case.attempts >= 2 so the first re-arm (attempts==1)
    keeps the original prompt unchanged — this avoids noise on the second attempt
    and reserves the enrichment for cases that have already failed twice.

    ``omit_pr_line`` lets a sibling block (e.g. PRIOR REMEDIATION FAILURE
    CONTEXT, which already names the PR) suppress the duplicate PR mention.
    """
    lines = [
        "",
        "PRIOR ATTEMPT CONTEXT",
        f"This regression has been investigated {case.attempts} time(s) without recovering the signal.",
    ]
    pr = case.last_remediation_pr_number
    if pr is not None and not omit_pr_line:
        lines.append(
            f"Prior remediation PR: #{pr}. Read its diff to understand what was already tried "
            f"before forming a new hypothesis."
        )
    snap = case.signal_snapshot_at_merge or {}
    post_val = snap.get("current_rate")
    if post_val is not None:
        lines.append(f"Signal value after prior fix merged: {post_val:.1%}.")
    lines.append(
        "Do NOT converge on the same diagnosis as prior attempts. "
        "Approach from a different angle — look for a deeper root cause or a "
        "different contributing factor that the prior fix missed."
    )
    return "\n".join(lines)


def _format_prior_verdict_context(case: RegressionRemediationCase) -> str:
    """Build a PRIOR INVESTIGATION VERDICT block to prepend to re-spawned investigations.

    Fires when the case has either a stored false-positive reason or a prior
    unactionable verdict, so the new investigation can skip already-ruled-out
    paths and focus on whether anything has changed since the prior finding.

    The block is intentionally framed as informational — the investigator must
    re-verify the prior conclusion against current evidence and may escalate if
    new facts warrant it.
    """
    parts: list[str] = []

    fp_reason = case.false_positive_reason
    unactionable_verdict = case.last_unactionable_verdict
    unactionable_summary = case.last_unactionable_summary

    if not fp_reason and not unactionable_verdict:
        return ""

    parts.append("")
    parts.append("PRIOR INVESTIGATION VERDICT")
    parts.append(
        "A previous investigation of this regression class reached the following "
        "conclusion. Treat this as context, not a hard constraint — if current "
        "evidence shows the situation has changed, escalate accordingly."
    )

    if fp_reason:
        parts.append("")
        parts.append("Prior verdict: NOT A REGRESSION (false positive)")
        parts.append(f"Reason: {fp_reason}")

    if unactionable_verdict and unactionable_summary:
        parts.append("")
        parts.append(f"Prior verdict: UNACTIONABLE ({unactionable_verdict})")
        parts.append(f"Summary: {unactionable_summary}")
    elif unactionable_verdict:
        parts.append("")
        parts.append(f"Prior verdict: UNACTIONABLE ({unactionable_verdict})")

    parts.append("")
    parts.append(
        "Verify whether the prior conclusion still holds given current evidence. "
        "If the signal has shifted or new information is available, treat this as "
        "a genuine recurrence requiring fresh investigation."
    )
    return "\n".join(parts)


def _parse_dispatcher_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _format_dispatcher_duration(delta: timedelta | None) -> str:
    if delta is None:
        return "unknown"
    seconds = max(int(delta.total_seconds()), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, rem = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{rem}s"


def _extract_recurrence_cycles(
    case: RegressionRemediationCase,
) -> list[dict[str, Any]]:
    log_entries = case.fingerprint_to_fix_log or []
    snapshot = case.signal_snapshot_at_merge or {}
    merged_fallback = _parse_dispatcher_dt(snapshot.get("captured_at"))
    cycles: list[dict[str, Any]] = []
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        has_fix = entry.get("fix_pr_number") is not None
        has_recovery = entry.get("recovered_at") is not None
        if has_fix:
            layers_present = "layers_targeted" in entry
            raw_layers = entry.get("layers_targeted") if layers_present else None
            layers_list = (
                [str(x) for x in raw_layers] if isinstance(raw_layers, list) else []
            )
            cycles.append(
                {
                    "fix_pr_number": entry.get("fix_pr_number"),
                    "merged_at": _parse_dispatcher_dt(entry.get("merged_at"))
                    or merged_fallback
                    or _parse_dispatcher_dt(entry.get("recovered_at"))
                    or case.updated_at
                    or case.created_at,
                    "recovered_at": None,
                    "layers_targeted_present": layers_present,
                    "layers_targeted": layers_list,
                }
            )
            continue
        if has_recovery:
            recovered_at = _parse_dispatcher_dt(entry.get("recovered_at"))
            if cycles and cycles[-1].get("recovered_at") is None:
                cycles[-1]["recovered_at"] = recovered_at
                continue
            cycles.append(
                {
                    "fix_pr_number": None,
                    "merged_at": merged_fallback or recovered_at or case.updated_at,
                    "recovered_at": recovered_at,
                    "layers_targeted_present": False,
                    "layers_targeted": [],
                }
            )
    if not cycles and (
        case.last_remediation_pr_number is not None
        or merged_fallback is not None
        or snapshot.get("captured_at") is not None
    ):
        cycles.append(
            {
                "fix_pr_number": case.last_remediation_pr_number,
                "merged_at": merged_fallback or case.updated_at or case.created_at,
                "recovered_at": None,
                "layers_targeted_present": False,
                "layers_targeted": [],
            }
        )
    return cycles


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


def _format_recurrence_meta_prompt(
    case: RegressionRemediationCase,
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    attempts = getattr(case, "_recurrence_attempts", []) or []
    recurrence_count = getattr(case, "_recurrence_count", len(attempts)) or len(
        attempts
    )
    held_text = getattr(case, "_recurrence_held_text", "unknown")
    obs_layers = list(getattr(case, "_recurrence_obs_layers", []) or [])

    def _format_attempt_layers(attempt: dict[str, Any]) -> str:
        if not attempt.get("layers_targeted_present"):
            return "layers: classification pending"
        layers = attempt.get("layers_targeted") or []
        if not layers:
            return "layers: none of the canonical layers"
        return f"layers: {', '.join(layers)}"

    prior_layers_union = _layers_targeted_union(attempts)
    obs_layers_set = set(obs_layers)
    mismatch = (
        bool(prior_layers_union)
        and bool(obs_layers_set)
        and prior_layers_union.isdisjoint(obs_layers_set)
    )

    lines: list[str] = []
    if mismatch:
        undefended = sorted(obs_layers_set - prior_layers_union)
        lines.extend(
            [
                "SYMPTOM-MISMATCH RECURRENCE",
                (
                    "Prior fixes targeted layers ["
                    f"{', '.join(sorted(prior_layers_union))}] but the current "
                    f"observation classifies as [{', '.join(sorted(obs_layers_set))}]."
                ),
                (f"Difference (un-defended layers): {', '.join(undefended)}."),
                (
                    "Start your layer reasoning from the un-defended layers above; "
                    "the previously-targeted layers have not prevented this re-trip."
                ),
                "",
            ]
        )
    lines.extend(
        [
            "RECURRENCE META-INVESTIGATION",
            (
                f"This regression class has recurred {recurrence_count} time(s) after "
                "prior remediation."
            ),
            f"Most recent prior fix held for {held_text} before the signal re-tripped.",
        ]
    )
    obs_text = ", ".join(obs_layers) if obs_layers else "classification pending"
    lines.append(f"Current observation layers: {obs_text}")
    lines.append("Prior remediation attempts:")
    if attempts:
        for idx, attempt in enumerate(attempts, start=1):
            task_id = attempt.get("task_id") or "unknown"
            pr_number = attempt.get("pr_number")
            merged_at = attempt.get("merged_at") or "unknown"
            recovered_at = attempt.get("recovered_at")
            held_for = attempt.get("held_for") or "unknown"
            parts = [
                f"- Attempt {idx}: task {task_id}",
                f"PR #{pr_number}" if pr_number is not None else "PR unknown",
                f"merged {merged_at}",
                f"held {held_for} before re-trip",
                _format_attempt_layers(attempt),
            ]
            if recovered_at is not None:
                parts.insert(3, f"recovered {recovered_at}")
            lines.append(" | ".join(parts))
    else:
        lines.append("- No prior remediation task metadata was found.")
    lines.extend(
        [
            "",
            "Enumerate ALL SIX canonical layers explicitly:",
            ", ".join(LAYER_DISPLAY_ORDER),
            (
                "For each layer answer: is it involved? if yes, what defense "
                "should we add? if no, why is it ruled out?"
            ),
        ]
    )
    lines.append("")
    lines.append(base_lines)
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _is_fragile_recurrence(
    case: RegressionRemediationCase,
    recurrence_count: int,
    now: datetime,
) -> bool:
    """Return True if the case is chronically fragile."""
    if recurrence_count >= _FRAGILE_RECURRENCE_MIN_COUNT:
        return True
    # Only completed cycles (recovered_at set) count as "held" — an in-progress
    # cycle has no confirmed hold duration yet.
    for cycle in _extract_recurrence_cycles(case):
        merged_at = cycle.get("merged_at")
        recovered_at = cycle.get("recovered_at")
        if merged_at is None or recovered_at is None:
            continue
        hold_days = (recovered_at - merged_at).total_seconds() / 86400
        if hold_days < _FRAGILE_RECURRENCE_SHORT_HOLD_DAYS:
            return True
    return False


def _format_fragile_recurrence_meta_prompt(
    case: RegressionRemediationCase,
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    """Build the fragile-recurrence cross-layer meta-investigation prompt."""
    attempts = getattr(case, "_recurrence_attempts", []) or []
    recurrence_count = getattr(case, "_recurrence_count", len(attempts)) or len(
        attempts
    )

    all_targeted = _layers_targeted_union(attempts) & set(LAYER_DISPLAY_ORDER)
    untargeted = compute_untargeted_layers(attempts)

    lines: list[str] = [
        "FRAGILE-RECURRENCE META-INVESTIGATION",
        (
            f"This regression class has re-tripped {recurrence_count} time(s) and is "
            "classified as chronically fragile (recurrence_count >= "
            f"{_FRAGILE_RECURRENCE_MIN_COUNT} OR a prior fix held < "
            f"{_FRAGILE_RECURRENCE_SHORT_HOLD_DAYS:.0f} days)."
        ),
        (
            "Single-layer fixes have not held. This meta-investigation must enumerate "
            "ALL canonical layers and propose defenses in AT LEAST TWO distinct layers, "
            "OR provide explicit written justification for why a single-layer defense is sufficient."
        ),
        "",
        f"Canonical defense layers: {', '.join(LAYER_DISPLAY_ORDER)}",
        (
            f"Layers already targeted by prior fixes: "
            f"{', '.join(sorted(all_targeted)) if all_targeted else 'none recorded'}"
        ),
        (
            f"Untargeted layers (start here): "
            f"{', '.join(untargeted) if untargeted else 'all layers have been targeted — look for cross-layer interactions'}"
        ),
        "",
        "For each untargeted layer answer: what defense should be added to prevent re-trip?",
        "For each targeted layer answer: why did the prior fix fail to hold?",
        "",
    ]
    lines.append(base_lines)
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _format_theme_layer_meta_prompt(
    base_lines: str, layer_defense_block: str = ""
) -> str:
    """Prepend the canonical layer-enumeration block for SR blind-spot theme regressions.

    Themes only fire after _SR_BLIND_SPOT_THEME_FLOOR (5+) occurrences, so the
    first spawn is already evidence that single-symptom fixes haven't resolved it.
    Force the full layer enumeration on every first dispatch rather than waiting
    for an explicit recurrence cycle.
    """
    lines = [
        "META-INVESTIGATION — SR BLIND-SPOT THEME",
        (
            "This theme has recurred across multiple tasks; single-symptom fixes "
            "have not extinguished the pattern."
        ),
        "",
        "Enumerate ALL SIX canonical layers explicitly:",
        ", ".join(LAYER_DISPLAY_ORDER),
        (
            "For each layer answer: is it involved? if yes, what defense "
            "should we add? if no, why is it ruled out?"
        ),
        "",
        base_lines,
    ]
    if layer_defense_block:
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _format_bundle_signal_lines(signals: list[dict]) -> str:
    """Format a single investigate prompt covering a bundle of related signals."""
    lines = [
        f"Pipeline regression bundle: {len(signals)} related signal(s) tripped this cycle.",
        "Investigate the shared root cause across all signals below.\n",
    ]
    for s in signals:
        rc = s.get("regression_class", s["signal_name"])
        prior_str = (
            f"{s['prior_rate']:.1%}" if s.get("prior_rate") is not None else "N/A"
        )
        curr_str = (
            f"{s['current_rate']:.1%}" if s.get("current_rate") is not None else "N/A"
        )
        lines += [
            f"Signal: {s['signal_name']} / class={rc}",
            f"  Prior: {prior_str}  Current: {curr_str}  n={s.get('sample_size', 0)}",
        ]
        for extra in s.get("extras") or []:
            lines.append(f"  {extra}")
    lines.append(
        "\nDiagnose the shared root cause and produce the structured diagnosis "
        "JSON ({summary, root_cause, recommended_fix, severity, confidence, "
        "affected_files}). Do NOT prescribe a fix — let the investigate→fix "
        "chain produce the remediation."
    )
    return "\n".join(lines)


def _parse_signal_timestamp(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_cross_layer_prompt(
    *,
    signal_name: str,
    regression_class: str,
    layers_evidence: list[dict[str, Any]],
    base_lines: str,
    layer_defense_block: str = "",
) -> str:
    """Build the cross-layer investigate prompt enumerating per-layer evidence."""
    layer_count = len(layers_evidence)
    layer_names = ", ".join(e["layer"] for e in layers_evidence)
    header = [
        f"CROSS-LAYER REGRESSION DETECTED ({signal_name} / class={regression_class}).",
        (
            f"This regression class has surfaced on {layer_count} distinct "
            f"infrastructure layer(s) within the last 48h: {layer_names}."
        ),
        (
            "Diagnose the SHARED root cause and propose defenses in AT LEAST TWO distinct "
            "infrastructure layers, OR provide explicit written justification for why a "
            "single-layer defense is sufficient. Do NOT bias toward whichever layer "
            "surfaced most recently. Each layer below shows one representative fingerprint "
            "+ sample from this window."
        ),
        "",
        "PER-LAYER EVIDENCE",
    ]
    for ev in layers_evidence:
        header.append(f"\nLayer: {ev['layer']}")
        if ev.get("fingerprint"):
            header.append(f"  Fingerprint: {ev['fingerprint']}")
        sample = ev.get("sample") or ""
        if sample:
            header.append(f"  Sample: {sample[:300]}")
    header.append(
        "\nNote: .github/workflows/ files cannot be modified by the agent. "
        "If the recommended fix would touch workflow YAML, treat it as a "
        "finding/note in the diagnosis report rather than a direct edit."
    )
    header.append("")
    header.append(base_lines)
    if layer_defense_block:
        header.append(layer_defense_block)
    return "\n".join(header)


def _format_layer_concentration_prompt(
    *,
    layer: str,
    sample_case_ids: list[str],
    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]],
    layer_defense_block: str = "",
) -> str:
    """Build the layer-concentration meta-investigation prompt."""
    lines = [
        f"LAYER-CONCENTRATION META-INVESTIGATION (layer_concentration:{layer})",
        (
            f"The {layer} infrastructure layer is the recurrent failure mode across "
            "multiple distinct regression classes. This is a cross-case meta-signal, "
            "not a single-case regression — the pattern spans independent regression "
            f"cases, each receiving fixes that target {layer}."
        ),
        "",
        f"CONTRIBUTING CASES (up to 5 most recent for {layer})",
    ]

    for case_id in sample_case_ids:
        case_entries = [
            e
            for cid, e in all_fix_logs_with_case_ids
            if cid == case_id and layer in (e.get("layers_targeted") or [])
        ]
        lines.append(f"\n  Case {case_id}:")
        if not case_entries:
            lines.append("    (no matching fix-log entries)")
            continue
        for e in case_entries[:3]:
            merged = e.get("merged_at") or "unknown"
            recovered = e.get("recovered_at")
            merged_short = merged[:10] if len(merged) >= 10 else merged
            if recovered:
                recovered_short = recovered[:10] if len(recovered) >= 10 else recovered
                merged_dt = _parse_dispatcher_dt(merged)
                recovered_dt = _parse_dispatcher_dt(recovered)
                if merged_dt and recovered_dt:
                    hold_days = (recovered_dt - merged_dt).total_seconds() / 86400.0
                    lines.append(
                        f"    merged={merged_short} recovered={recovered_short} "
                        f"hold={hold_days:.1f}d (re-tripped)"
                    )
                else:
                    lines.append(
                        f"    merged={merged_short} recovered={recovered_short}"
                    )
            else:
                merged_dt = _parse_dispatcher_dt(merged)
                if merged_dt:
                    age_days = (_utcnow() - merged_dt).total_seconds() / 86400.0
                    lines.append(
                        f"    merged={merged_short} still holding (age={age_days:.1f}d)"
                    )
                else:
                    lines.append(f"    merged={merged_short} still holding")

    lines += [
        "",
        "STRUCTURAL QUESTIONS TO INVESTIGATE",
        (
            f"  1. Is the {layer} layer's defense pattern structurally insufficient "
            "(architectural / categorical), rather than individual fixes being wrong? "
            "If every fix targets this layer but the regression recurs, the layer may "
            "have a systemic gap that point-fixes cannot close."
        ),
        (
            f"  2. Propose defenses in AT LEAST ONE OTHER layer to compensate for "
            f"{layer}'s fragility, OR provide written justification for why {layer}-only "
            "defense is sufficient. Defense-in-depth strategies (pre-flight checks, gate "
            "policies, observation thresholds in adjacent layers) may reduce blast radius "
            "when this layer fails."
        ),
        "",
        "Produce the structured diagnosis JSON "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
        "",
        "Note: .github/workflows/ files cannot be modified by the agent. "
        "If the recommended fix would touch workflow YAML, treat it as a "
        "finding/note in the diagnosis report rather than a direct edit.",
    ]

    if layer_defense_block:
        lines.append("")
        lines.append(layer_defense_block)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path-based layer classification (for fix-log layers_targeted backfill)
# ---------------------------------------------------------------------------

# Bound for how many cases to backfill per dispatcher tick. The recovery scan
# runs every 60s and each call hits the GitHub API once per case; capping
# prevents a multi-hundred-case backlog from saturating the API budget.
_LAYER_BACKFILL_PER_TICK = 5

# Cap for the per-entry ``unmatched_paths`` list written by
# ``_backfill_layers_targeted_once`` when classification yields ``[]``. Bounds
# the JSON column on noisy PRs so a single 200-file refactor cannot bloat the
# row.
_UNMATCHED_PATHS_PER_ENTRY_CAP = 20

# Path-classifier coverage signal thresholds. The signal trips when the share
# of recently-merged fix-log entries that classified to ``[]`` exceeds the
# rate over a minimum sample. Both are module-level so tests can monkeypatch.
_PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE = 5
_PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD = 0.20
# Top-N most-frequent unmatched paths to surface in the alert message.
_PATH_CLASSIFIER_COVERAGE_TOP_PATHS = 10
# Stable Alert title used for dedup so a recurring stale-classifier condition
# does not pile up duplicate alerts each cycle.
_PATH_CLASSIFIER_COVERAGE_ALERT_TITLE = "Path-layer classifier coverage stale"

# Awaiting-backfill backlog alert thresholds. Cross-layer compliance buckets
# fix-log entries with spawn_was_cross_layer=True and merged_at set but no
# layers_targeted into ``awaiting_backfill``. The 24h grace inside
# ``compute_cross_layer_compliance`` already separates transient (recent
# merges, backfill not yet retried) from systemic backlog. The thresholds
# below trip the operator alert when EITHER signal indicates the backlog has
# stopped draining: 5 entries past the 24h grace, OR an oldest entry older
# than 3 days. Both point at the same likely causes — GitHub auth failures in
# ``_backfill_layers_targeted_once`` (which logs and continues silently) or
# path-classifier coverage gaps that yield empty layer assignments.
_AWAITING_BACKFILL_COUNT_THRESHOLD = 5
_AWAITING_BACKFILL_AGE_THRESHOLD = timedelta(days=3)
# Window mirrors the GET /regression-cases/cross-layer-compliance default.
_AWAITING_BACKFILL_WINDOW = timedelta(days=30)
_AWAITING_BACKFILL_ALERT_TITLE = "Cross-layer awaiting_backfill backlog stuck"

# Second-stage escalation for observation-only alerts that stay open past SLA.
# When an alert with one of the monitored titles has been open continuously for
# longer than the threshold, the dispatcher opens a RegressionRemediationCase and
# spawns one investigate task so the autonomous loop can remediate the root cause.
# Reuses case dedup (unique regression_class per project) so a single escalation
# is opened per stuck alert regardless of how many dispatcher cycles fire.
_OBSERVATION_ALERT_SLA_HOURS_DEFAULT = 72
# Per-title overrides — key is the alert title, value is SLA hours. Falls back
# to _OBSERVATION_ALERT_SLA_HOURS_DEFAULT when a title is not listed here.
_OBSERVATION_ALERT_SLA_HOURS: dict[str, int] = {}
# Titles of observation-only alerts that are eligible for second-stage escalation.
# admin_digest alert titles are imported lazily inside the function to avoid cycles.
_OBSERVATION_ONLY_ALERT_TITLES = [
    _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE,
    _AWAITING_BACKFILL_ALERT_TITLE,
]


def _extract_evaluating_since(task: Task) -> datetime:
    """Return when the task most recently entered EVALUATING status.

    Scans status_log (newest-first) for the transition entry; falls back
    to task.updated_at then task.created_at if no entry is found.
    """
    for entry in reversed(task.status_log or []):
        if "→ EVALUATING" in str(entry.get("msg", "")):
            ts = _parse_iso_timestamp(entry.get("t"))
            if ts is not None:
                return ts
    anchor = task.updated_at or task.created_at
    return anchor.replace(tzinfo=None) if getattr(anchor, "tzinfo", None) else anchor


def _format_reviewer_sla_stall_prompt(
    *,
    case_id: uuid.UUID,
    pr_number: int | None,
    elapsed_hours: float,
    task_id: uuid.UUID,
    task_status: str,
    latest_status_message: str | None,
) -> str:
    pr_str = f"#{pr_number}" if pr_number is not None else "(no PR recorded)"
    elapsed_str = f"{elapsed_hours:.1f}h"
    status_excerpt = (latest_status_message or "").strip()[:500] or "(none)"
    lines = [
        f"Pipeline regression detected (reviewer_sla_stall / case={case_id}).",
        f"Remediation PR {pr_str} has been in EVALUATING for {elapsed_str} with no review action.",
        f"Linked task: {task_id} (status={task_status})",
        f"Last status message (truncated to 500 chars):\n  {status_excerpt}",
        "",
        "Investigate: why has the remediation PR stalled in review/CI?  Look for:",
        "  - PR blocked on missing reviewer or review queue backlog",
        "  - CI rebuild stuck or flaking (no retry attempted)",
        "  - Merge conflict preventing CI from running",
        "  - Agent VM failure that left the task in EVALUATING with no forward progress",
        "  - PR already closed/merged but the case was not updated (stale case state)",
        "",
        "Diagnose the root cause and produce the structured diagnosis JSON report "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
    ]
    return "\n".join(lines)


def _format_cross_case_fingerprint_prompt(
    *,
    fingerprint: str,
    short_hash: str,
    contributing_classes: list[str],
    contributing_case_ids: list[str],
    sample_messages: list[str],
    occurrence_count: int,
) -> str:
    """Build the cross-case-fingerprint meta-investigation prompt."""
    lines = [
        f"CROSS-CASE FINGERPRINT META-INVESTIGATION (cross_case_fingerprint:{short_hash})",
        (
            "The same normalized error fingerprint has been observed across MULTIPLE "
            "distinct regression_class values. The dispatcher cannot tell from "
            "fingerprint identity alone whether this is one shared root cause or "
            "several unrelated surfaces that happen to normalize the same way — "
            "your investigation must validate which it is."
        ),
        "",
        f"Normalized fingerprint: {fingerprint}",
        f"Total occurrences in last {int(_CROSS_CASE_FINGERPRINT_LOOKBACK.days)}d: {occurrence_count}",
        "",
        "Contributing regression_class values:",
    ]
    for cls in contributing_classes:
        lines.append(f"  - {cls}")
    lines += ["", "Contributing case_ids:"]
    for cid in contributing_case_ids:
        lines.append(f"  - {cid}")
    if sample_messages:
        lines += ["", "Sample failure messages (deduped, truncated):"]
        for msg in sample_messages:
            lines.append(f"  - {msg}")
    lines += [
        "",
        "FRAMING QUESTION FOR THE INVESTIGATION",
        (
            "Is this one shared root symptom we should fix once, rather than "
            "fixing each surface independently? If yes, identify the shared "
            "root cause and propose a single coordinated remediation. If no, "
            "explain why these surfaces happen to share a fingerprint despite "
            "being independent (e.g. fingerprint normalization is too coarse), "
            "and recommend whether the fingerprint extraction should be tightened."
        ),
        "",
        "Produce the structured diagnosis JSON "
        "({summary, root_cause, recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
    ]
    return "\n".join(lines)


def _classify_cycle_compliance(row: dict) -> str:
    """Return the compliance label for a single PER-CYCLE LAYER HISTORY row.

    Labels:
      - ``compliant``         — mandate True AND >=2 distinct canonical layers targeted
                                (recommended_unmodifiable_layers count toward the total)
      - ``non-compliant``     — mandate True AND layers_targeted recorded but <2 canonical layers
      - ``awaiting-backfill`` — mandate True AND layers_targeted not yet present (do NOT accuse)
      - ``not-mandated``      — mandate False or absent
    """
    if not row.get("spawn_was_cross_layer"):
        return "not-mandated"
    if not row.get("layers_targeted_present"):
        return "awaiting-backfill"
    canonical = set(LAYER_DISPLAY_ORDER)
    distinct_canonical = {
        layer for layer in (row.get("layers_targeted") or []) if layer in canonical
    }
    distinct_unmodifiable = {
        layer
        for layer in (row.get("recommended_unmodifiable_layers") or [])
        if layer in canonical
    }
    if len(distinct_canonical | distinct_unmodifiable) >= 2:
        return "compliant"
    return "non-compliant"


def _format_recurring_class_prompt(
    *,
    regression_class: str,
    targeted_layers: list[str],
    cycle_rows: list[dict],
    layer_defense_block: str = "",
) -> str:
    """Build the recurring-class proactive meta-investigation prompt."""
    cycle_count = len(cycle_rows)
    layer_list = ", ".join(targeted_layers)
    lines = [
        f"RECURRING CROSS-LAYER CLASS META-INVESTIGATION (recurring_class:{regression_class})",
        (
            f"Regression class '{regression_class}' has accumulated {cycle_count} fix cycles "
            f"across {len(targeted_layers)} distinct canonical layers ({layer_list}). "
            "Each cycle applied a single-layer fix that held temporarily, then re-tripped "
            "on a different layer — a pattern indicating an unaddressed shared root cause."
        ),
        "",
        "PER-CYCLE LAYER HISTORY",
    ]
    non_compliant_targeted: set[str] = set()
    canonical_set = set(LAYER_DISPLAY_ORDER)
    for row in cycle_rows:
        layers = (
            ", ".join(row["layers_targeted"]) if row["layers_targeted"] else "unknown"
        )
        compliance = _classify_cycle_compliance(row)
        mandate = "yes" if row.get("spawn_was_cross_layer") else "no"
        lines.append(
            f"  Cycle {row['cycle']}: layers={layers}  merged_at={row['merged_at']}"
            f"  cross_layer_mandate={mandate}  compliance={compliance}"
        )
        if compliance == "non-compliant":
            for layer in row.get("layers_targeted") or []:
                if layer in canonical_set:
                    non_compliant_targeted.add(layer)

    if non_compliant_targeted:
        # Reuse compute_untargeted_layers over the same per-row layers_targeted
        # data the cycle history was built from — equivalent to running it over
        # the project's full aggregated fix-log entries for this class.
        untargeted = compute_untargeted_layers(cycle_rows)
        targeted_str = ", ".join(sorted(non_compliant_targeted))
        untargeted_str = (
            ", ".join(untargeted)
            if untargeted
            else "(none — every layer has a prior fix)"
        )
        lines += [
            "",
            "PRIOR MANDATE NON-COMPLIANCE",
            (
                f"At least one prior cycle was spawned with the cross-layer mandate "
                f"but shipped a single-layer fix targeting only: {targeted_str}. "
                "That fix did NOT comply with the multi-layer requirement."
            ),
            (
                f"Canonical layers still UNTARGETED across this class's full fix history: "
                f"{untargeted_str}. "
                "Steer the new defense toward these untargeted layers."
            ),
        ]

    lines += [
        "",
        "REQUIRED: propose defenses in AT LEAST 2 distinct canonical layers.",
        f"Canonical layers: {', '.join(LAYER_DISPLAY_ORDER)}",
        (
            "A single-layer fix has already been tried multiple times — "
            "a multi-layer defense is mandatory."
        ),
        "",
        (
            ".github/workflows/ files cannot be modified by the agent; "
            "treat workflow findings as notes in the diagnosis report."
        ),
    ]
    if layer_defense_block:
        lines.append("")
        lines.append(layer_defense_block)
    return "\n".join(lines)


def _format_strategic_stall_prompt(
    *,
    stall_hours: int,
    recent_task_count: int,
    failed_strategic: list[Task],
    payload_bloat_skip: dict | None = None,
) -> str:
    lines = [
        "Pipeline regression detected (strategic_cycle_stall).",
        f"No strategic_review or design_doc_update task has completed in the last {stall_hours}h.",
        f"Non-strategic task activity in window: {recent_task_count} task(s) created.",
        "",
        "The strategic engine normally runs design_doc_update (Phase 1) to refresh project",
        "context, then strategic_review (Phase 2) to score findings, evaluate directions,",
        "and queue new research tasks. A prolonged stall leads to stale project context,",
        "missed signal patterns, and direction drift.",
        "",
    ]
    if payload_bloat_skip:
        skipped_at = payload_bloat_skip.get("skipped_at", "unknown")
        size = payload_bloat_skip.get("size", 0)
        top = payload_bloat_skip.get("top_contributors", [])
        top_str = (
            ", ".join(f"{c['key']}={c['size']}B" for c in top[:3]) if top else "unknown"
        )
        lines += [
            "ROOT CAUSE IDENTIFIED: strategic spawn was skipped because the assembled",
            f"strategic payload exceeded the 256 KB size budget (last skip: {skipped_at},",
            f"size: {size} bytes, top contributors: {top_str}).",
            "",
            "No strategic task was ever created — so there is no stuck or failed strategic",
            "task to find. The true cause is an over-budget strategic context (design_doc,",
            "problem_model, or vision) that must be compacted before strategic spawns resume.",
            "",
            "Investigate: which strategic context field(s) are driving the payload over budget?",
            "  - Identify the largest fields listed above in top_contributors",
            "  - Check design_doc and problem_model for redundant, verbose, or stale content",
            "  - Propose targeted compaction: trim or summarize oversized sections",
            "  - Do NOT look for stuck/failed strategic tasks — none exist",
            "",
        ]
    else:
        if failed_strategic:
            lines.append("Recent FAILED strategic tasks in this window:")
            for t in failed_strategic:
                ptype = (t.first_payload or {}).get("type", "unknown")
                lines.append(f"  - {ptype} task {t.id} FAILED")
            lines.append("")
        lines += [
            "Investigate: why has the strategic engine not completed a cycle? Look for:",
            "  - strategic_review or design_doc_update tasks stuck in QUEUED/IN_PROGRESS",
            "  - repeated FAILED outcomes on strategic tasks",
            "  - orchestrator scheduling not spawning strategic tasks",
            "  - research slot exhaustion (max_total_research_per_project cap) blocking spawns",
            "  - recent deploy changes that may correlate with the stall onset",
            "",
        ]
    lines += [
        "\nDiagnose the underlying root cause of this regression and produce "
        "the structured diagnosis JSON report ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the "
        "remediation.",
    ]
    return "\n".join(lines)
