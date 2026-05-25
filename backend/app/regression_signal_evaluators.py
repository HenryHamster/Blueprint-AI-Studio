"""Pipeline-regression signal evaluators extracted from the dispatcher.

This module owns the ``_evaluate_*_signal`` async helpers — the detection-side
counterparts to the ``_recover_*`` evaluators in
``regression_recovery_evaluators``. Each evaluator inspects a slice of pipeline
state and, when its signal trips, asks the dispatcher to ``_maybe_spawn`` an
investigation.

The dispatcher re-exports every evaluator name so existing
``from app.pipeline_regression_dispatcher import _evaluate_X`` callers keep
working unchanged. Helpers, constants, and the in-flight cooldown/dedup
machinery still live on the dispatcher and are reached here through
``_dispatcher_module()`` lazy lookups, mirroring the pattern already used by
``regression_recovery_evaluators``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dispatcher_formatters import (
    _CROSS_CASE_FINGERPRINT_LOOKBACK,
    _extract_evaluating_since,
    _format_bundle_signal_lines,
    _format_cross_case_fingerprint_prompt,
    _format_layer_concentration_prompt,
    _format_recurring_class_prompt,
    _format_reviewer_sla_stall_prompt,
    _format_signal_lines,
    _format_sr_fp_signal_lines,
    _format_strategic_stall_prompt,
    _parse_iso_timestamp,
)
from app.models import (
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskStatus,
)
from app.regression_dispatcher_metrics import (
    _build_layer_defense_ledger,
    _compute_layer_fragility_stats,
    compute_avg_prevention_rate_7d,
    compute_layer_defense_density,
)
from app.regression_layer_classifier import (
    aggregate_targeted_layers,
    is_recurring_cross_layer_class,
)
from app.regression_recovery_evaluators import (
    TELEMETRY_SIGNAL_SPECS,
    get_telemetry_signal_spec,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow


def _dispatcher_module():
    from app import pipeline_regression_dispatcher as dispatcher

    return dispatcher


async def _evaluate_low_prevention_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Fire a research signal when the 7d average defense prevention rate drops below threshold."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    cases = list(
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        ).scalars()
    )

    result = compute_avg_prevention_rate_7d(cases)
    avg_rate = result["avg_rate"]
    eligible_count = result["eligible_count"]

    if avg_rate is None or avg_rate >= dispatcher._PREVENTION_RATE_THRESHOLD:
        return 0
    if eligible_count < dispatcher._PREVENTION_RATE_MIN_SAMPLE:
        return 0

    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="low_prevention_rate_7d",
        regression_class="cross_layer_defense_prevention_low",
        prior_rate=dispatcher._PREVENTION_RATE_THRESHOLD,
        current_rate=avg_rate,
        sample_size=eligible_count,
        magnitude=dispatcher._PREVENTION_RATE_THRESHOLD - avg_rate,
        title_hint=f"Low cross-layer defense prevention rate ({avg_rate:.0%})",
        extra_context_fields={
            "prevention_rate_7d": avg_rate,
            "eligible_defenses": eligible_count,
            "recurred_count": result["recurred_count"],
        },
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_catch_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped SR catch-rate categories and coalesce into one spawn."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    tripped: list[dict] = []
    for cat in sorted(categories):
        if not isinstance(categories[cat], dict):
            continue
        rc = f"sr_catch_rate:{cat}"
        state = await dispatcher._current_signal_value_for_class(
            session, project_id, rc, project_design_doc
        )
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    cats_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = (
        _format_bundle_signal_lines(tripped)
        if len(tripped) > 1
        else _format_signal_lines(
            signal_name=rep["signal_name"],
            regression_class=rep["regression_class"],
            prior_rate=rep["prior_rate"],
            current_rate=rep["current_rate"],
            sample_size=rep["sample_size"],
            extras=rep["extras"],
        )
    )
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name=rep["signal_name"],
        regression_class="sr_catch_rate",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"SR catch-rate ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_false_positive_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when any SR category's false-positive rate exceeds the floor in both windows."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    tripped: list[dict] = []
    for cat in sorted(categories):
        if not isinstance(categories[cat], dict):
            continue
        rc = f"sr_false_positive:{cat}"
        state = await dispatcher._current_signal_value_for_class(
            session, project_id, rc, project_design_doc
        )
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    cats_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = _format_sr_fp_signal_lines(
        regression_class=rep["regression_class"],
        current_fp_rate=rep["current_rate"],
        prior_fp_rate=rep["prior_rate"],
        sample_size=rep["sample_size"],
    )
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="sr_false_positive",
        regression_class="sr_false_positive",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"SR false-positive ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_catch_rate_direction_signals(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Spawn per-direction investigations when a direction regresses but project-wide is healthy."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    by_direction = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "by_direction"
    ) or {}
    if not isinstance(by_direction, dict):
        return 0
    _spawned = 0
    dir_spawns = 0
    for direction_id in sorted(by_direction):
        if (
            _spawned >= spawn_budget_left
            or dir_spawns >= dispatcher._MAX_DIRECTION_SR_SPAWNS_PER_CYCLE
        ):
            break
        dir_cats = sorted(
            (by_direction[direction_id].get("categories") or {}).items(),
            key=lambda kv: kv[1].get("miss_rate", 0),
            reverse=True,
        )
        for cat, _ in dir_cats:
            rc = f"sr_catch_rate:{cat}:{direction_id}"
            state = await dispatcher._current_signal_value_for_class(
                session, project_id, rc, project_design_doc
            )
            if state is None or not state["tripped"]:
                continue
            # Skip if project-wide aggregate for this category is also tripping.
            pw_state = await dispatcher._current_signal_value_for_class(
                session, project_id, f"sr_catch_rate:{cat}", project_design_doc
            )
            if pw_state is not None and pw_state["tripped"]:
                continue
            prompt = _format_signal_lines(
                signal_name=state["signal_name"],
                regression_class=rc,
                prior_rate=state["prior_rate"],
                current_rate=state["current_rate"],
                sample_size=state["sample_size"],
                extras=state["extras"],
            )
            spawn = await dispatcher._maybe_spawn(
                session,
                project_id,
                signal_name=state["signal_name"],
                regression_class=rc,
                prior_rate=state["prior_rate"],
                current_rate=state["current_rate"],
                sample_size=state["sample_size"],
                magnitude=state["magnitude"],
                title_hint=f"SR catch-rate ({cat}) direction {direction_id[:8]}",
                prompt_override=prompt,
                extra_context_fields={"direction_id": direction_id},
            )
            if spawn is not None:
                _spawned += 1
                dir_spawns += 1
            break  # one attempt per direction (worst tripped category first)
    return _spawned


async def _evaluate_sr_below_floor_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when any SR category sits below the floor for min-windows consecutively."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import (
        _SR_FLOOR_THRESHOLD,
        _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    )

    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    floor_threshold = float(
        (project_design_doc or {}).get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD
    )
    floor_min_windows = int(
        (project_design_doc or {}).get("sr_floor_min_windows")
        or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )

    tripped: list[tuple[str, dict]] = []
    for cat, data in sorted(categories.items()):
        if not isinstance(data, dict):
            continue
        if not data.get("below_floor"):
            continue
        rate = data.get("current_hit_rate")
        history = data.get("rate_history") or []
        if rate is None or len(history) < floor_min_windows:
            continue
        tripped.append((cat, data))

    if not tripped:
        return 0

    cats_label = ", ".join(cat for cat, _ in tripped)
    lines = [
        "Pipeline quality signal: SR catch-rate sustained below floor "
        "(sub-signal=below_floor / class=sr_catch_rate_below_floor).",
        f"Categories stuck below {floor_threshold:.0%} floor for "
        f"{floor_min_windows}+ consecutive windows: {cats_label}.",
        "",
        "Sub-signal: below_floor — the hit-rate is chronically low (not just declining).",
        "The category has been stuck at a low catch-rate without getting worse, so the "
        "delta-trend signal has not fired. Investigate why the baseline is low and what "
        "structural changes to the self-review prompt or process could lift it.",
        "",
    ]
    for cat, data in tripped:
        rate = data.get("current_hit_rate")
        history = data.get("rate_history") or []
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        hist_str = ", ".join(f"{r:.0%}" for r in history[-floor_min_windows:])
        lines.append(
            f"  • {cat}: current={rate_str}, last {floor_min_windows} windows: [{hist_str}]"
        )
    lines.append("")
    lines.append(
        "FIRST: triage whether this signal actually represents a regression. "
        "Set `verdict` to 'not_a_regression' when the low rate is intentional or "
        "the sample is too small. Otherwise produce the structured diagnosis JSON."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = tripped[0]
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="sr_catch_rate_below_floor",
        regression_class="sr_catch_rate_below_floor",
        prior_rate=floor_threshold,
        current_rate=rep_data.get("current_hit_rate"),
        sample_size=int(rep_data.get("current_human_count") or 0),
        magnitude=round(floor_threshold - (rep_data.get("current_hit_rate") or 0), 3),
        title_hint=f"SR catch-rate below floor ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_pass_engagement_thinning_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when PASS reasoning engagement drops below threshold for a category."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import (
        _SR_ENGAGEMENT_THRESHOLD,
        _SR_ENGAGEMENT_MIN_PASSES,
    )

    categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
        "categories"
    ) or {}
    if not isinstance(categories, dict):
        return 0

    eng_threshold = float(
        (project_design_doc or {}).get("sr_engagement_threshold")
        or _SR_ENGAGEMENT_THRESHOLD
    )
    eng_min_passes = int(
        (project_design_doc or {}).get("sr_engagement_min_passes")
        or _SR_ENGAGEMENT_MIN_PASSES
    )

    thinning: list[tuple[str, dict]] = []
    for cat, data in sorted(categories.items()):
        if not isinstance(data, dict):
            continue
        rate = data.get("reasoning_engagement_rate")
        total = data.get("reasoning_engagement_total") or 0
        if rate is None or total < eng_min_passes:
            continue
        if rate < eng_threshold:
            thinning.append((cat, data))

    if not thinning:
        return 0

    cats_label = ", ".join(cat for cat, _ in thinning)
    lines = [
        "Pipeline quality signal: SR PASS reasoning engagement thinning "
        "(sub-signal=pass_engagement_thinning / class=sr_pass_engagement_thinning).",
        f"Categories with PASS engagement below {eng_threshold:.0%}: {cats_label}.",
        "",
        "Sub-signal: pass_engagement_thinning — the agent is rubber-stamping PASS "
        "verdicts without substantive reasoning. This is an early indicator that "
        "precedes catch-rate regressions.",
        "",
    ]
    for cat, data in thinning:
        rate = data.get("reasoning_engagement_rate")
        total = data.get("reasoning_engagement_total") or 0
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        lines.append(f"  • {cat}: engagement={rate_str} (n={total} PASS verdicts)")
    lines.append("")
    lines.append(
        "Investigate why PASS reasoning is thin for these categories. "
        "Check the self-review prompt wording — does it make it too easy to "
        "skip substantive reasoning for PASS verdicts? Produce the structured "
        "diagnosis JSON ({summary, root_cause, recommended_fix, severity, "
        "confidence, affected_files, verdict, verdict_reason})."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = thinning[0]
    rep_rate = rep_data.get("reasoning_engagement_rate") or 0.0
    rep_total = int(rep_data.get("reasoning_engagement_total") or 0)
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="sr_pass_engagement_thinning",
        regression_class="sr_pass_engagement_thinning",
        prior_rate=eng_threshold,
        current_rate=rep_rate,
        sample_size=rep_total,
        magnitude=round(eng_threshold - rep_rate, 3),
        title_hint=f"SR PASS engagement thinning ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in thinning]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_false_attestation_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip when SR false-attestation rate exceeds threshold for any category."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    from app.sr_analytics import _FALSE_ATTESTATION_RATE_THRESHOLD

    per_category = (
        (project_design_doc or {}).get("sr_false_attestation_trends") or {}
    ).get("per_category") or {}
    if not isinstance(per_category, dict):
        return 0

    elevated: list[tuple[str, dict]] = []
    for cat, data in sorted(per_category.items()):
        if not isinstance(data, dict):
            continue
        rate = data.get("false_attestation_rate")
        if rate is not None and rate > _FALSE_ATTESTATION_RATE_THRESHOLD:
            elevated.append((cat, data))

    if not elevated:
        return 0

    cats_label = ", ".join(cat for cat, _ in elevated)
    lines = [
        "Pipeline quality signal: SR false attestation elevated "
        "(sub-signal=sr_false_attestation / class=sr_false_attestation).",
        f"Categories where SR claimed passed but CI recorded failure: {cats_label}.",
        "",
        "Sub-signal: sr_false_attestation — SR claimed tests_executed_cleanly, "
        "lint_executed_cleanly, or typecheck_executed_cleanly passed but CI "
        "recorded ci_passed=False for the same task. This indicates the agent "
        "is attesting to clean execution without actually running the checks.",
        "",
    ]
    for cat, data in elevated:
        rate = data.get("false_attestation_rate")
        n = data.get("false_attested", 0)
        total = (data.get("ci_confirmed") or 0) + n
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        prev_rate = data.get("previous_false_attestation_rate")
        prev_str = f"{prev_rate:.0%}" if prev_rate is not None else "N/A"
        lines.append(
            f"  • {cat}: false_attestation_rate={rate_str} ({n}/{total}), "
            f"prior window={prev_str}"
        )
    lines.append("")
    lines.append(
        "Investigate why the agent is attesting to clean execution without "
        "running the checks. Check whether the self-review prompt adequately "
        "enforces that execution evidence must come from an actual run on the "
        "current VM. Produce the structured diagnosis JSON ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files, verdict, verdict_reason})."
    )
    prompt = "\n".join(lines)

    _rep_cat, rep_data = elevated[0]
    rep_rate = rep_data.get("false_attestation_rate") or 0.0
    prev_rate = rep_data.get("previous_false_attestation_rate") or 0.0
    rep_total = int(
        (rep_data.get("false_attested") or 0) + (rep_data.get("ci_confirmed") or 0)
    )
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="sr_false_attestation",
        regression_class="sr_false_attestation",
        prior_rate=prev_rate,
        current_rate=rep_rate,
        sample_size=rep_total,
        magnitude=round(rep_rate - prev_rate, 3),
        title_hint=f"SR false attestation elevated ({cats_label})",
        prompt_override=prompt,
        extra_context_fields={"categories": [cat for cat, _ in elevated]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_fix_loop_divergence_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped fix-loop divergence loops and coalesce into one spawn."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    tripped: list[dict] = []
    for loop in ("lint", "test", "check"):
        rc = f"fix_loop_divergence:{loop}"
        state = await dispatcher._current_signal_value_for_class(
            session, project_id, rc
        )
        if state is not None and state["tripped"]:
            state["regression_class"] = rc
            tripped.append(state)

    if not tripped:
        return 0

    rep = tripped[0]
    loops_label = ", ".join(s["regression_class"].split(":")[1] for s in tripped)
    prompt = (
        _format_bundle_signal_lines(tripped)
        if len(tripped) > 1
        else _format_signal_lines(
            signal_name=rep["signal_name"],
            regression_class=rep["regression_class"],
            prior_rate=rep["prior_rate"],
            current_rate=rep["current_rate"],
            sample_size=rep["sample_size"],
            extras=rep["extras"],
        )
    )
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name=rep["signal_name"],
        regression_class="fix_loop_divergence",
        prior_rate=rep["prior_rate"],
        current_rate=rep["current_rate"],
        sample_size=rep["sample_size"],
        magnitude=rep["magnitude"],
        title_hint=f"fix-loop divergence ({loops_label})",
        prompt_override=prompt,
        extra_context_fields={"signals": [s["regression_class"] for s in tripped]},
    )
    return 1 if spawn is not None else 0


async def _evaluate_fix_loop_divergence_fingerprint_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0 or not _runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    ):
        return 0
    trips = await dispatcher._collect_fix_loop_divergence_fingerprints(
        session, project_id
    )
    spawned = 0
    for trip in trips:
        if spawned >= spawn_budget_left:
            break
        prompt = _format_signal_lines(
            signal_name="fix_loop_divergence_fingerprint",
            regression_class=f"fix_loop_divergence_fingerprint:{trip['short_fingerprint']}",
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            extras=[
                f"Normalized fingerprint: {trip['fingerprint']}",
                f"Affected scope(s): {', '.join(trip['scopes']) or 'unknown'}",
                f"Affected files: {', '.join(trip['files']) or '(none recorded)'}",
                *[f"Sample failure tail: {tail}" for tail in trip["samples"]],
            ],
        )
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="fix_loop_divergence_fingerprint",
            regression_class=f"fix_loop_divergence_fingerprint:{trip['short_fingerprint']}",
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            magnitude=float(trip["count"]),
            title_hint=f"fix-loop divergence fingerprint ({trip['short_fingerprint']})"[
                :80
            ],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_viewport_regression_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip viewport_regression:<Component>:<AssertionType>@<width> when ≥2 viewport-test failures recur."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    trips = await dispatcher._collect_viewport_failures(session, project_id)
    spawned = 0
    for trip in trips:
        if spawned >= spawn_budget_left:
            break
        component = trip["component"]
        width = trip["width"]
        failure_type = trip.get("failure_type", "unknown")
        regression_class = f"viewport_regression:{component}:{failure_type}@{width}"
        prompt = _format_signal_lines(
            signal_name="viewport_regression",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            extras=[
                f"Component: {component}",
                f"Viewport width: {width}px",
                f"Failure type: {failure_type}",
                *[f"Sample failure: {s}" for s in trip["samples"]],
            ],
        )
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="viewport_regression",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=trip["count"],
            magnitude=float(trip["count"]),
            title_hint=f"viewport regression: {component} at {width}px"[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1
            if hasattr(RegressionRemediationCase, "introducing_pr_number"):
                case = await dispatcher._get_remediation_case(
                    session, project_id, regression_class
                )
                if case is not None:
                    case.introducing_pr_number = trip.get("introducing_pr_number")
                    case.introducing_commit_sha = trip.get("introducing_commit_sha")
                    session.add(case)
    return spawned


async def _evaluate_viewport_coverage_drop_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip viewport_coverage_drop when a merged PR silences viewport assertions.

    Reads the pre-computed snapshot from project.design_doc[
    "viewport_coverage_snapshot"] and fires when:
      - post-merge assertion count is lower than pre-merge count (test deleted /
        skipped), OR
      - new data-viewport-allow-overflow / data-viewport-allow-small-target
        escape-hatch attributes were added (bypass without restoring the test),
    AND no component file was legitimately removed in the same PR.
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    if not dispatcher._probe_viewport_coverage_drop(project_design_doc):
        return 0
    doc = project_design_doc or {}
    snap = doc["viewport_coverage_snapshot"]
    before = int(snap["assertion_count_before"])
    after = int(snap["assertion_count_after"])
    escape_hatches = int(snap.get("new_escape_hatch_count") or 0)
    drop = before - after
    extras = [
        f"Assertion count before merge: {before}",
        f"Assertion count after merge: {after}",
    ]
    if drop > 0:
        extras.append(f"Net assertion drop: {drop}")
    if escape_hatches > 0:
        extras.append(f"New escape-hatch attributes added: {escape_hatches}")
    pr = snap.get("merged_pr")
    if pr:
        extras.append(f"Merged PR: #{pr}")
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="viewport_coverage_drop",
        regression_class="viewport_coverage_drop",
        prior_rate=None,
        current_rate=None,
        sample_size=max(drop, escape_hatches),
        magnitude=float(drop + escape_hatches),
        title_hint="viewport coverage drop: assertions removed or silenced",
        extras=extras,
    )
    return 1 if spawn is not None else 0


async def _evaluate_reviewer_sla_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip reviewer_sla_stall:<case_id> when a remediation task sits in EVALUATING too long."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    if not _runtime_config.get("reviewer_sla_stall_enabled", True):
        return 0

    threshold_hours = float(_runtime_config.get("reviewer_sla_stall_hours", 6))
    now = _utcnow()

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.ACTIVE,
                    RegressionRemediationCase.last_remediation_task_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    task_ids = [c.last_remediation_task_id for c in cases if c.last_remediation_task_id]
    tasks_by_id: dict = {}
    if task_ids:
        tasks_by_id = {
            t.id: t
            for t in (await session.execute(select(Task).where(Task.id.in_(task_ids))))
            .scalars()
            .all()
        }

    spawned = 0
    for case in cases:
        if spawned >= spawn_budget_left:
            break

        task = tasks_by_id.get(case.last_remediation_task_id)
        if task is None:
            continue

        # Terminal tasks have already resolved; non-EVALUATING tasks are not stalled yet.
        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        if task.status != TaskStatus.EVALUATING:
            continue

        evaluating_since = _extract_evaluating_since(task)
        elapsed_hours = (now - evaluating_since).total_seconds() / 3600
        if elapsed_hours < threshold_hours:
            continue

        regression_class = f"reviewer_sla_stall:{case.id}"
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="reviewer_sla_stall",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=int(elapsed_hours),
            magnitude=elapsed_hours,
            title_hint=f"reviewer SLA stall ({elapsed_hours:.1f}h)"[:80],
            prompt_override=_format_reviewer_sla_stall_prompt(
                case_id=case.id,
                pr_number=case.last_remediation_pr_number,
                elapsed_hours=elapsed_hours,
                task_id=task.id,
                task_status=str(task.status),
                latest_status_message=task.latest_status_message,
            ),
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_pipeline_success_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip pipeline_success_rate when 24h trend declines on a non-trivial sample."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    state = await dispatcher._current_signal_value_for_class(
        session, project_id, "pipeline_success_rate"
    )
    if state is None or not state["tripped"]:
        return 0
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name=state["signal_name"],
        regression_class="pipeline_success_rate",
        prior_rate=state["prior_rate"],
        current_rate=state["current_rate"],
        sample_size=state["sample_size"],
        magnitude=state["magnitude"],
        extras=state["extras"],
        title_hint="24h pipeline success-rate declining",
    )
    return 1 if spawn is not None else 0


async def _evaluate_sr_blind_spot_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Evaluate SR blind-spot drift: reviewer-disagreement rate and recurring themes.

    Trips when either:
    (a) a (category, task_type)'s miss rate exceeds _TREND_DELTA_THRESHOLD in
        BOTH the current 7d window AND the prior 7-14d window (sustained drift,
        not a one-off), with at least _MIN_SR_SAMPLE denominator in each window;
    (b) a recurring blind_spot theme reaches _SR_BLIND_SPOT_THEME_FLOOR
        occurrences in the 14d window.
    """
    dispatcher = _dispatcher_module()
    from app.sr_analytics import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
    from app.sr_blind_spots import (
        _count_reviewer_disagreement_directions,
        aggregate_sr_blind_spot_themes,
        is_disallowed_blind_spot,
    )
    from app.models import Task as TaskModel

    if spawn_budget_left <= 0:
        return 0

    spawned = 0
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # --- Disagreement-rate signal ---
    tasks = (
        (
            await session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project_id,
                    TaskModel.status == TaskStatus.COMPLETED,
                    TaskModel.created_at >= now - timedelta(days=14),
                )
            )
        )
        .scalars()
        .all()
    )

    c7 = _naive(now - timedelta(days=7))
    curr_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) >= c7]
    prev_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) < c7]
    curr_dirs = _count_reviewer_disagreement_directions(curr_tasks)
    prev_dirs = _count_reviewer_disagreement_directions(prev_tasks)

    # Dedupe by category: one spawn per category regardless of task_type.
    # Iterate sorted pairs; for each category, try each (cat, task_type)
    # until one trips (or none do for that category).
    cat_to_pairs: dict[str, list[str]] = defaultdict(list)
    for cat, task_type in sorted(curr_dirs):
        cat_to_pairs[cat].append(task_type)

    for cat, task_types in sorted(cat_to_pairs.items()):
        if spawned >= spawn_budget_left:
            break
        for task_type in task_types:
            c_bucket = curr_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
            p_bucket = prev_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
            c_denom = c_bucket["miss"] + c_bucket["tp"] + c_bucket["fp"]
            p_denom = p_bucket["miss"] + p_bucket["tp"] + p_bucket["fp"]
            if c_denom < _MIN_SR_SAMPLE or p_denom < _MIN_SR_SAMPLE:
                continue
            c_rate = c_bucket["miss"] / c_denom
            p_rate = p_bucket["miss"] / p_denom
            if c_rate < _TREND_DELTA_THRESHOLD or p_rate < _TREND_DELTA_THRESHOLD:
                continue

            # Both windows trip — collect prompt evidence then spawn.
            reviewer_examples: list[str] = []
            for t in curr_tasks:
                sro = (t.context or {}).get("_self_review_outcome") or {}
                for finding in sro.get("reviewer_findings") or []:
                    if not isinstance(finding, dict):
                        continue
                    if finding.get("category") == cat:
                        desc = str(finding.get("description", ""))[:120]
                        if desc:
                            reviewer_examples.append(desc)
                    if len(reviewer_examples) >= 2:
                        break
                if len(reviewer_examples) >= 2:
                    break

            extras = [
                f"SR blind-spot: category '{cat}' consistently caught by reviewers "
                f"but missed by SR ({task_type} tasks).",
                f"Current 7d miss rate: {c_rate:.1%} ({c_bucket['miss']}/{c_denom})",
                f"Prior 7-14d miss rate: {p_rate:.1%} ({p_bucket['miss']}/{p_denom})",
            ]
            for ex in reviewer_examples:
                extras.append(f"Example reviewer finding: {ex}")

            regression_class = f"sr_blind_spot:{cat}"
            spawn = await dispatcher._maybe_spawn(
                session,
                project_id,
                signal_name="sr_blind_spot",
                regression_class=regression_class,
                prior_rate=round(p_rate, 3),
                current_rate=round(c_rate, 3),
                sample_size=c_denom,
                magnitude=round(c_rate - p_rate, 3),
                extras=extras,
                title_hint=f"SR blind-spot: {cat} ({task_type})"[:80],
            )
            if spawn is not None:
                spawned += 1
            break  # one spawn per category

    # --- Theme signal ---
    themes = await aggregate_sr_blind_spot_themes(
        session, project_id, days=14, limit=100
    )
    for theme_entry in themes:
        if spawned >= spawn_budget_left:
            break
        count = theme_entry["count"]
        if count < dispatcher._SR_BLIND_SPOT_THEME_FLOOR:
            break  # sorted descending — subsequent entries also below floor
        theme = theme_entry["theme"]
        if is_disallowed_blind_spot(theme):
            _rj_inner = dispatcher._cycle_rejections_var.get()
            if _rj_inner is not None:
                _rj_inner["disallowed_theme"] = _rj_inner.get("disallowed_theme", 0) + 1
            dispatcher.logger.info(
                "[REGRESSION_DISPATCHER] disallowed-theme skip project=%s theme=%r",
                project_id,
                theme,
            )
            continue
        regression_class = f"sr_blind_spot:theme:{theme}"
        theme_extras: list[str] = [
            f"Recurring SR blind-spot theme: '{theme}' appeared {count}x in the last 14 days.",
        ]
        for ex in theme_entry.get("examples") or []:
            theme_extras.append(
                f"Example (task {ex['task_id']} / {ex['task_type']}): {ex['text']}"
            )
        triage = theme_entry.get("triage") or {}
        triage_context = triage.get("triage_context") or ""
        if triage_context:
            theme_extras.append(f"Triage: {triage_context}")
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="sr_blind_spot",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=count,
            magnitude=float(count),
            extras=theme_extras,
            title_hint=f"SR blind-spot theme: {theme}"[:80],
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_layer_concentration_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Escalate when a single canonical layer absorbs fragile fixes across many cases.

    Trips when, for a canonical layer:
    - At least _LAYER_CONCENTRATION_MIN_DISTINCT_CASES distinct regression cases
      contributed a fix targeting that layer in the lookback window, AND
    - The layer is fragile: avg hold days < _LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
      OR re-trip count >= _LAYER_CONCENTRATION_FRAGILE_RETRIPS.

    Uses regression_class="layer_concentration:<LAYER>" so each layer has its own
    dedup/cooldown slot via the existing _maybe_spawn gates.
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    all_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not all_cases:
        return 0

    all_fix_logs_with_case_ids: list[tuple[str, dict[str, Any]]] = [
        (str(c.id), entry)
        for c in all_cases
        for entry in (c.fingerprint_to_fix_log or [])
        if isinstance(entry, dict)
    ]
    if not all_fix_logs_with_case_ids:
        return 0

    all_fix_logs = [entry for _, entry in all_fix_logs_with_case_ids]

    density = compute_layer_defense_density(all_fix_logs_with_case_ids)
    fragility = _compute_layer_fragility_stats(all_fix_logs)
    layer_defense_block = _build_layer_defense_ledger(all_fix_logs)

    spawned = 0
    for density_row in density:
        if spawned >= spawn_budget_left:
            break
        layer = density_row["layer"]
        sample_case_ids = density_row["sample_case_ids"]
        if len(sample_case_ids) < dispatcher._LAYER_CONCENTRATION_MIN_DISTINCT_CASES:
            continue

        frag = fragility[layer]
        avg_hold = frag["avg_hold_days"]
        retrips = frag["retripped_within_48h_count"]
        is_fragile = (
            avg_hold is not None
            and avg_hold < dispatcher._LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
        ) or retrips >= dispatcher._LAYER_CONCENTRATION_FRAGILE_RETRIPS
        if not is_fragile:
            continue

        fix_count = density_row["fix_count"]
        prompt = _format_layer_concentration_prompt(
            layer=layer,
            sample_case_ids=sample_case_ids,
            all_fix_logs_with_case_ids=all_fix_logs_with_case_ids,
            layer_defense_block=layer_defense_block,
        )
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="layer_concentration",
            regression_class=f"layer_concentration:{layer}",
            prior_rate=None,
            current_rate=None,
            sample_size=fix_count,
            magnitude=float(fix_count),
            title_hint=f"Layer concentration: {layer} ({len(sample_case_ids)} cases)",
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_cross_case_fingerprint_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Escalate when the SAME error fingerprint surfaces across multiple regression classes.

    Trips when, for a normalized fingerprint string:
    - Entries observed within ``_CROSS_CASE_FINGERPRINT_LOOKBACK`` span at least
      ``_CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES`` distinct
      ``RegressionRemediationCase.regression_class`` values, AND
    - Total in-window occurrence count >= ``_CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES``.

    Uses regression_class="cross_case_fingerprint:<short-hash>" so each
    fingerprint has its own dedup/cooldown slot via the existing _maybe_spawn
    gates. ``<short-hash>`` is the first 8 hex chars of sha256(fingerprint).

    Skips empty fingerprints and fingerprints whose value equals the case's
    own ``regression_class`` (the fallback ``_record_layer_observation`` writes
    when no real classify_layers message was available — clustering those is
    pure noise).
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    all_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not all_cases:
        return 0

    now = _utcnow()
    cutoff = now - _CROSS_CASE_FINGERPRINT_LOOKBACK

    # fingerprint -> aggregated state (shared with the recovery path so both
    # sides classify identically).
    by_fp = dispatcher._index_cross_case_fingerprints(all_cases, cutoff)

    spawned = 0
    # Iterate in deterministic order (highest occurrence first, then fingerprint
    # text) so multiple eligible fingerprints in one cycle resolve consistently.
    candidates = sorted(
        by_fp.items(),
        key=lambda kv: (-kv[1]["occurrences"], kv[0]),
    )
    for fingerprint, bucket in candidates:
        if spawned >= spawn_budget_left:
            break
        occurrence_count = bucket["occurrences"]
        contributing_classes = sorted(bucket["classes"])
        if (
            len(contributing_classes)
            < dispatcher._CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES
        ):
            continue
        if occurrence_count < dispatcher._CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES:
            continue

        short_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]
        regression_class = f"cross_case_fingerprint:{short_hash}"
        prompt = _format_cross_case_fingerprint_prompt(
            fingerprint=fingerprint,
            short_hash=short_hash,
            contributing_classes=contributing_classes,
            contributing_case_ids=bucket["case_ids"],
            sample_messages=bucket["samples"],
            occurrence_count=occurrence_count,
        )
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="cross_case_fingerprint",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=occurrence_count,
            magnitude=float(occurrence_count),
            title_hint=(
                f"cross-case fingerprint {short_hash} "
                f"({len(contributing_classes)} classes, {occurrence_count} hits)"
            )[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_recurring_class_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Proactively escalate when a regression class spans 2+ layers across 3+ cycles.

    Unlike the inline cross-layer detection in _maybe_spawn (which only fires when
    the current signal also trips), this evaluator scans historical case records so
    a class that accumulated cross-layer history without concurrent signals is caught.

    Uses regression_class="recurring_class:<original_class>" for dedup/cooldown.
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    cutoff = _utcnow() - dispatcher._RECURRING_CLASS_LOOKBACK
    all_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.first_seen_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if not all_cases:
        return 0

    # Group fix-log entries by regression_class, skipping meta-investigation
    # classes (shared with the recovery path so both sides group identically).
    by_class = dispatcher._group_recurring_class_entries(all_cases)

    if not by_class:
        return 0

    # Compute layer defense block once (only if at least one class will trip).
    _layer_defense_block: str | None = None

    spawned = 0
    for regression_class, all_entries in sorted(by_class.items()):
        if spawned >= spawn_budget_left:
            break
        if not is_recurring_cross_layer_class(
            all_entries,
            min_cycles=dispatcher._RECURRING_CLASS_MIN_CYCLES,
            min_layers=dispatcher._RECURRING_CLASS_MIN_LAYERS,
        ):
            continue
        targeted = aggregate_targeted_layers(all_entries)

        if _layer_defense_block is None:
            all_fix_logs_with_case_ids: list[tuple[str, dict]] = [
                (str(c.id), entry)
                for c in all_cases
                for entry in (c.fingerprint_to_fix_log or [])
                if isinstance(entry, dict)
            ]
            _layer_defense_block = _build_layer_defense_ledger(
                all_fix_logs_with_case_ids
            )

        cycle_rows = [
            {
                "cycle": i + 1,
                "layers_targeted": e.get("layers_targeted") or [],
                "recommended_unmodifiable_layers": e.get(
                    "recommended_unmodifiable_layers"
                )
                or [],
                "merged_at": e.get("merged_at") or "unknown",
                "spawn_was_cross_layer": bool(e.get("spawn_was_cross_layer")),
                "layers_targeted_present": "layers_targeted" in e,
            }
            for i, e in enumerate(all_entries)
        ]
        prompt = _format_recurring_class_prompt(
            regression_class=regression_class,
            targeted_layers=sorted(targeted),
            cycle_rows=cycle_rows,
            layer_defense_block=_layer_defense_block or "",
        )
        meta_class = f"recurring_class:{regression_class}"
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="recurring_class",
            regression_class=meta_class,
            prior_rate=None,
            current_rate=None,
            sample_size=len(all_entries),
            magnitude=float(len(all_entries)),
            title_hint=(
                f"Recurring cross-layer class: {regression_class} "
                f"({len(all_entries)} cycles, {len(targeted)} layers)"
            )[:80],
            prompt_override=prompt,
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def _evaluate_telemetry_staleness_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Spawn investigate tasks for pipeline signals that have stopped computing.

    Treats each stale signal as its own regression_class so dedup, cooldown,
    and MTTD/MTTR dashboards work identically to other regression classes.
    The existing WARNING Alerts from _run_signal_staleness_watchdog still fire
    — this adds an autonomous investigation, not a replacement.
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    doc = project_design_doc or {}
    now = _utcnow()
    spawned = 0
    for spec in TELEMETRY_SIGNAL_SPECS:
        if spawned >= spawn_budget_left:
            break
        raw_ts, threshold = dispatcher._get_staleness_raw_ts(
            spec.signal_name, doc, project_id
        )
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None or (now - computed_at) <= threshold:
            continue
        age = now - computed_at
        regression_class = f"telemetry_stale:{spec.signal_name}"
        prompt = "\n".join(
            [
                f"Telemetry staleness: signal '{spec.signal_name}' has not refreshed in {round(age.total_seconds() / 3600, 1)}h.",
                f"Last computed: {computed_at.isoformat()} (freshness window: {round(threshold.total_seconds() / 3600, 1)}h).",
                f"This signal is computed by: {spec.source_module}.",
                "",
                "Diagnose why this signal stopped computing and ship a fix.",
                "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
            ]
        )
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="telemetry_stale",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=0,
            magnitude=round(age.total_seconds() / 3600, 2),
            extras=[
                f"Signal '{spec.signal_name}' last computed {round(age.total_seconds() / 3600, 1)}h ago."
            ],
            title_hint=f"telemetry staleness: {spec.signal_name}",
            prompt_override=prompt,
            extra_snapshot_fields={
                "signal_last_computed_at": computed_at.isoformat(),
                "age_hours": round(age.total_seconds() / 3600, 1),
                "source_module": spec.source_module,
            },
        )
        if spawn is not None:
            spawned += 1

    # Detect frozen-but-fresh: signal is computing (fresh heartbeat) but real data
    # has not refreshed in the sufficiency-stale window. The metric is frozen.
    if spawned < spawn_budget_left:
        sr_spec = get_telemetry_signal_spec("sr_catch_rate")
        signal_ts = sr_spec.signal_heartbeat_at(doc, project_id) if sr_spec else None
        sufficient_ts = sr_spec.last_sufficient_at(doc, project_id) if sr_spec else None
        if (
            sr_spec is not None
            and sr_spec.signal_heartbeat_at is not None
            and sr_spec.last_sufficient_at is not None
            and signal_ts
            and sufficient_ts
        ):
            signal_at = _parse_iso_timestamp(signal_ts)
            sufficient_at = _parse_iso_timestamp(sufficient_ts)
            if (
                signal_at is not None
                and sufficient_at is not None
                and sr_spec.frozen_signal_fresh_window is not None
                and sr_spec.sufficiency_stale_window is not None
                and (now - signal_at) <= sr_spec.frozen_signal_fresh_window
                and (now - sufficient_at) > sr_spec.sufficiency_stale_window
            ):
                regression_class = (
                    sr_spec.sufficiency_stale_regression_class
                    or "telemetry_sufficiency_stale:sr_catch_rate"
                )
                age = now - sufficient_at
                prompt = "\n".join(
                    [
                        "Telemetry frozen: signal 'sr_catch_rate' is computing (fresh heartbeat)"
                        f" but real data has not refreshed in {round(age.total_seconds() / 3600, 1)}h.",
                        f"signal_last_computed_at: {signal_at.isoformat()} (fresh)",
                        f"last_sufficient_computed_at: {sufficient_at.isoformat()} "
                        f"({round(age.total_seconds() / 3600, 1)}h stale)",
                        f"This signal is computed by: {sr_spec.source_module}.",
                        "",
                        "The metric is running its computation cycle but failing to accumulate"
                        " sufficient data. Diagnose why new data is not flowing into the measurement.",
                        "Possible causes: insufficient reviewer rejections, bucket starvation,"
                        " changed sampling rules, or blocked data pipeline.",
                        "Produce the structured diagnosis JSON"
                        " ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                        "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
                    ]
                )
                spawn = await dispatcher._maybe_spawn(
                    session,
                    project_id,
                    signal_name="telemetry_sufficiency_stale",
                    regression_class=regression_class,
                    prior_rate=None,
                    current_rate=None,
                    sample_size=0,
                    magnitude=round(age.total_seconds() / 3600, 2),
                    extras=[
                        f"Signal 'sr_catch_rate' frozen: real data stale for "
                        f"{round(age.total_seconds() / 3600, 1)}h but signal computing."
                    ],
                    title_hint="telemetry frozen: sr_catch_rate",
                    prompt_override=prompt,
                    extra_snapshot_fields={
                        "signal_last_computed_at": signal_at.isoformat(),
                        "last_sufficient_computed_at": sufficient_at.isoformat(),
                        "sufficiency_stale_hours": round(age.total_seconds() / 3600, 1),
                        "source_module": sr_spec.source_module,
                    },
                )
                if spawn is not None:
                    spawned += 1

    # Detect never-sufficient: sr_catch_rate has been running (heartbeat present)
    # but has never produced a real measurement (last_sufficient_computed_at absent).
    if spawned < spawn_budget_left:
        sr_spec = get_telemetry_signal_spec("sr_catch_rate")
        signal_ts = sr_spec.signal_heartbeat_at(doc, project_id) if sr_spec else None
        sufficient_ts = sr_spec.last_sufficient_at(doc, project_id) if sr_spec else None
        if (
            sr_spec is not None
            and sr_spec.signal_heartbeat_at is not None
            and sr_spec.last_sufficient_at is not None
            and signal_ts
            and not sufficient_ts
        ):
            regression_class = (
                sr_spec.never_sufficient_regression_class
                or "telemetry_never_sufficient:sr_catch_rate"
            )
            prompt = "\n".join(
                [
                    "Telemetry degenerate: signal 'sr_catch_rate' has been computed repeatedly"
                    " but has never reached data sufficiency.",
                    "signal_last_computed_at is set but last_sufficient_computed_at is absent.",
                    f"This signal is computed by: {sr_spec.source_module}.",
                    "",
                    "Diagnose why this signal never reaches data sufficiency and ship a fix.",
                    "Possible causes: sample size too small, no reviewer rejections in task"
                    " history, bucket thresholds too high.",
                    "Produce the structured diagnosis JSON"
                    " ({summary, root_cause, recommended_fix, severity, confidence, affected_files}).",
                    "Do NOT prescribe a fix — let the investigate→fix chain produce the remediation.",
                ]
            )
            spawn = await dispatcher._maybe_spawn(
                session,
                project_id,
                signal_name="telemetry_never_sufficient",
                regression_class=regression_class,
                prior_rate=None,
                current_rate=None,
                sample_size=0,
                magnitude=0.0,
                extras=["Signal 'sr_catch_rate' has never reached data sufficiency."],
                title_hint="telemetry never-sufficient: sr_catch_rate",
                prompt_override=prompt,
                extra_snapshot_fields={
                    "signal_last_computed_at": signal_ts,
                    "last_sufficient_computed_at": None,
                    "source_module": sr_spec.source_module,
                },
            )
            if spawn is not None:
                spawned += 1

    return spawned


async def _evaluate_strategic_cycle_stall_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_vision: dict | None,
    spawn_budget_left: int,
) -> int:
    """Trip strategic_cycle_stall when no strategic task completed in stall window.

    Only fires for projects that have previously completed at least one strategic
    cycle (so new projects without any history are excluded) AND have non-strategic
    activity in the current window (so idle/paused projects are excluded).
    """
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    stall_cutoff = _utcnow() - timedelta(hours=dispatcher._STRATEGIC_CYCLE_STALL_HOURS)

    recent_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.created_at >= stall_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    # If any strategic task completed recently, no stall.
    for t in recent_tasks:
        if (t.first_payload or {}).get(
            "type"
        ) in dispatcher._STRATEGIC_TASK_TYPES and t.status == TaskStatus.COMPLETED:
            return 0

    # If no non-strategic tasks exist in the window, the project is idle — skip.
    if not any(
        (t.first_payload or {}).get("type") not in dispatcher._STRATEGIC_TASK_TYPES
        for t in recent_tasks
    ):
        return 0

    # Guard: only fire for projects that have previously had at least one
    # completed strategic cycle. New projects (no prior strategic history)
    # are excluded — they have not yet established a cadence to stall against.
    prior_strategic = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at < stall_cutoff,
                )
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    if not any(
        (t.first_payload or {}).get("type") in dispatcher._STRATEGIC_TASK_TYPES
        for t in prior_strategic
    ):
        return 0

    failed_strategic = [
        t
        for t in recent_tasks
        if (t.first_payload or {}).get("type") in dispatcher._STRATEGIC_TASK_TYPES
        and t.status == TaskStatus.FAILED
    ]
    payload_bloat_skip = (project_vision or {}).get("_strategic_payload_bloat_skip")
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="strategic_cycle_stall",
        regression_class="strategic_cycle_stall",
        prior_rate=None,
        current_rate=None,
        sample_size=len(recent_tasks),
        magnitude=float(dispatcher._STRATEGIC_CYCLE_STALL_HOURS),
        title_hint=(
            f"strategic engine stalled (>{dispatcher._STRATEGIC_CYCLE_STALL_HOURS}h)"
        ),
        prompt_override=_format_strategic_stall_prompt(
            stall_hours=dispatcher._STRATEGIC_CYCLE_STALL_HOURS,
            recent_task_count=len(recent_tasks),
            failed_strategic=failed_strategic,
            payload_bloat_skip=payload_bloat_skip,
        ),
    )
    return 1 if spawn is not None else 0


async def _evaluate_anomaly_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip on persistent anomalies that have exceeded the occurrence threshold."""
    import app.shared_state as _ss

    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0

    proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
    if not proj_anomalies:
        from app.models import Project as _Proj
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

        _proj = await session.get(_Proj, project_id)
        if _proj is not None:
            _stored = ((_proj.design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY)) or {}
            if _stored:
                _ss.persistent_anomalies[str(project_id)] = _stored
                proj_anomalies = _stored
    if not proj_anomalies:
        return 0

    now = _utcnow()
    recency_cutoff = (
        now - timedelta(hours=dispatcher._ANOMALY_DISPATCH_RECENCY_HOURS)
    ).isoformat()
    spawned = 0
    for anomaly_key, entry in list(proj_anomalies.items()):
        if spawned >= spawn_budget_left:
            break
        if not isinstance(entry, dict):
            continue
        occurrence_count = int(entry.get("occurrence_count") or 0)
        if occurrence_count < dispatcher._ANOMALY_DISPATCH_MIN_OCCURRENCES:
            continue
        last_seen = str(entry.get("last_seen") or "")
        if last_seen < recency_cutoff:
            continue
        regression_class = f"anomaly:{anomaly_key}"
        sample_msg = entry.get("example_message") or entry.get("latest_details") or ""
        _first_seen_raw = entry.get("first_seen")
        _anomaly_first_seen: datetime | None = None
        if _first_seen_raw:
            try:
                _anomaly_first_seen = datetime.fromisoformat(
                    str(_first_seen_raw).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                pass
        spawn = await dispatcher._maybe_spawn(
            session,
            project_id,
            signal_name="persistent_anomaly",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=occurrence_count,
            magnitude=float(occurrence_count),
            extras=[
                f"Persistent anomaly '{anomaly_key}' detected {occurrence_count}x "
                f"(severity: {entry.get('severity', 'unknown')}).",
                (entry.get("latest_details") or "")[:300],
            ],
            title_hint=f"persistent anomaly {anomaly_key}"[:80],
            extra_snapshot_fields={
                "hit_count": occurrence_count,
                "occurrence_count": occurrence_count,
                "first_seen": str(entry.get("first_seen") or ""),
                "last_seen": last_seen,
                "sample_message": sample_msg[:500],
            },
            signal_first_seen_at=_anomaly_first_seen,
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def _evaluate_agent_vm_resource_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip agent_vm_resource_exhaustion when VM-layer failures spike in the 24h window."""
    dispatcher = _dispatcher_module()
    if spawn_budget_left <= 0:
        return 0
    failures = await dispatcher._collect_agent_vm_failures(session, project_id)
    if len(failures) < dispatcher._AGENT_VM_RESOURCE_MIN_FAILURES:
        return 0

    reason_counts: dict[str, int] = {}
    task_id_samples: list[str] = []
    task_types: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    for f in failures:
        reason = f["exit_reason"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(task_id_samples) < 5:
            task_id_samples.append(f["task_id"])
        ttype = f["task_type"]
        task_types[ttype] = task_types.get(ttype, 0) + 1
        scope = f["scope"]
        scope_counts[scope] = scope_counts.get(scope, 0) + 1

    count = len(failures)
    reason_summary = ", ".join(f"{r}:{c}" for r, c in sorted(reason_counts.items()))
    type_summary = ", ".join(f"{t}:{c}" for t, c in sorted(task_types.items()))
    scope_summary = ", ".join(f"{s}:{c}" for s, c in sorted(scope_counts.items()))

    prompt = "\n".join(
        [
            "Pipeline regression detected (agent_vm_resource / class=agent_vm_resource_exhaustion).",
            "Canonical layer: AGENT_VM",
            f"{count} tasks failed with VM-infrastructure exit reasons in the last "
            f"{dispatcher._AGENT_VM_RESOURCE_WINDOW_HOURS}h.",
            f"Exit reasons: {reason_summary}",
            f"Task types affected: {type_summary}",
            f"Task scopes affected: {scope_summary}",
            f"Sample task IDs: {', '.join(task_id_samples)}",
            "",
            "Investigate: is this code-budget creep (work outgrew the VM tier), spot "
            "capacity churn, OS image regression, or something else?",
            "Specifically:",
            "1. Is exhaustion concentrated on a particular scope (small/medium/large)?",
            "2. Is it concentrated on a particular task type?",
            "3. Are OOM kills indicating memory pressure vs. heartbeat losses indicating "
            "spot preemption?",
            "4. Does the timing correlate with an OS image or VM-type change?",
            "",
            "Produce the structured diagnosis JSON ({summary, root_cause, recommended_fix, "
            "severity, confidence, affected_files}).",
        ]
    )
    spawn = await dispatcher._maybe_spawn(
        session,
        project_id,
        signal_name="agent_vm_resource",
        regression_class="agent_vm_resource_exhaustion",
        prior_rate=None,
        current_rate=None,
        sample_size=count,
        magnitude=float(count),
        title_hint=f"agent VM resource exhaustion ({reason_summary})"[:80],
        prompt_override=prompt,
        extra_context_fields={
            "reason_counts": reason_counts,
            "canonical_layer": "AGENT_VM",
        },
    )
    return 1 if spawn is not None else 0
