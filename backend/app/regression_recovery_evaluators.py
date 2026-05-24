"""Recovery evaluators extracted from pipeline_regression_dispatcher.

This module owns the dynamic / activity-derived recovery evaluators plus the
canonical spawnable regression-class registry. A few shared helpers and
threshold constants still live in ``pipeline_regression_dispatcher`` because the
detection path also consumes them; the evaluators import that module lazily at
call time to avoid a top-level circular import while preserving existing
behavior.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any, Callable, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dispatcher_formatters import (
    _CROSS_CASE_FINGERPRINT_LOOKBACK,
    _extract_evaluating_since,
)
from app.models import (
    Alert,
    AlertSeverity,
    RegressionRemediationCase,
    Task,
    TaskStatus,
)
from app.regression_dispatcher_metrics import (
    _compute_layer_fragility_stats,
    compute_avg_prevention_rate_7d,
    compute_layer_defense_density,
)
from app.regression_layer_classifier import (
    LAYER_DISPLAY_ORDER,
    aggregate_targeted_layers,
    is_recurring_cross_layer_class,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow


def _dispatcher_module():
    from app import pipeline_regression_dispatcher as dispatcher

    return dispatcher


async def _recover_viewport_regression(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    body = regression_class[len("viewport_regression:") :]
    component, _, rest = body.partition(":")
    failure_type, _, width = rest.partition("@")
    trips = await _dispatcher_module()._collect_viewport_failures(session, project_id)
    match = next(
        (
            t
            for t in trips
            if t["component"] == component
            and t.get("failure_type", "unknown") == failure_type
            and t["width"] == width
        ),
        None,
    )
    count = match["count"] if match else 0
    return {
        "signal_name": "viewport_regression",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": count,
        "magnitude": float(count),
        "tripped": match is not None,
        "extras": (
            [
                f"Viewport regression {component} {failure_type}@{width}px: "
                f"{count} recent failure(s)."
            ]
            if match is not None
            else []
        ),
    }


async def _recover_viewport_coverage_drop(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    tripped = _dispatcher_module()._probe_viewport_coverage_drop(project_design_doc)
    snap = (project_design_doc or {}).get("viewport_coverage_snapshot") or {}
    before = snap.get("assertion_count_before")
    after = snap.get("assertion_count_after")
    escape_hatches = int(snap.get("new_escape_hatch_count") or 0)
    drop = 0
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        drop = max(int(before) - int(after), 0)
    return {
        "signal_name": "viewport_coverage_drop",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": max(drop, escape_hatches),
        "magnitude": float(drop + escape_hatches),
        "tripped": tripped,
        "extras": (
            [
                f"Viewport coverage snapshot: {drop} assertion(s) dropped, "
                f"{escape_hatches} new escape hatch(es)."
            ]
            if tripped
            else []
        ),
    }


async def _recover_reviewer_sla_stall(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    case_id_str = regression_class.split(":", 1)[1] if ":" in regression_class else ""
    threshold_hours = float(_runtime_config.get("reviewer_sla_stall_hours", 6))
    now = _utcnow()
    tripped = False
    elapsed_hours = 0.0
    target_case = None
    try:
        target_case = await session.get(
            RegressionRemediationCase, uuid.UUID(case_id_str)
        )
    except (ValueError, TypeError):
        target_case = None
    if target_case is not None and target_case.last_remediation_task_id is not None:
        task = await session.get(Task, target_case.last_remediation_task_id)
        if task is not None and task.status == TaskStatus.EVALUATING:
            evaluating_since = _extract_evaluating_since(task)
            elapsed_hours = (now - evaluating_since).total_seconds() / 3600
            tripped = elapsed_hours >= threshold_hours
    return {
        "signal_name": "reviewer_sla_stall",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": int(elapsed_hours),
        "magnitude": elapsed_hours,
        "tripped": tripped,
        "extras": (
            [
                f"Remediation case {case_id_str} still stalled in EVALUATING "
                f"for {elapsed_hours:.1f}h."
            ]
            if tripped
            else []
        ),
    }


async def _recover_layer_concentration(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    layer = regression_class.split(":", 1)[1] if ":" in regression_class else ""
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
    all_fix_logs_with_case_ids = [
        (str(c.id), entry)
        for c in all_cases
        for entry in (c.fingerprint_to_fix_log or [])
        if isinstance(entry, dict)
    ]
    all_fix_logs = [entry for _, entry in all_fix_logs_with_case_ids]
    density = compute_layer_defense_density(all_fix_logs_with_case_ids)
    fragility = _compute_layer_fragility_stats(all_fix_logs)
    row = next((d for d in density if d["layer"] == layer), None)
    tripped = False
    fix_count = 0
    distinct = 0
    if row is not None:
        distinct = len(row["sample_case_ids"])
        fix_count = row["fix_count"]
        frag = fragility.get(layer) or {}
        avg_hold = frag.get("avg_hold_days")
        retrips = frag.get("retripped_within_48h_count", 0)
        is_fragile = (
            avg_hold is not None
            and avg_hold < dispatcher._LAYER_CONCENTRATION_FRAGILE_AVG_HOLD_DAYS
        ) or retrips >= dispatcher._LAYER_CONCENTRATION_FRAGILE_RETRIPS
        tripped = (
            distinct >= dispatcher._LAYER_CONCENTRATION_MIN_DISTINCT_CASES
            and is_fragile
        )
    return {
        "signal_name": "layer_concentration",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": fix_count,
        "magnitude": float(fix_count),
        "tripped": tripped,
        "extras": (
            [f"Layer '{layer}' still fragile across {distinct} distinct case(s)."]
            if tripped
            else []
        ),
    }


async def _recover_cross_case_fingerprint(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    short_hash = regression_class.split(":", 1)[1] if ":" in regression_class else ""
    now = _utcnow()
    cutoff = now - _CROSS_CASE_FINGERPRINT_LOOKBACK
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
    by_fp = dispatcher._index_cross_case_fingerprints(all_cases, cutoff)
    occurrence_count = 0
    distinct_classes = 0
    for fingerprint, bucket in by_fp.items():
        if hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8] != short_hash:
            continue
        occurrence_count = bucket["occurrences"]
        distinct_classes = len(bucket["classes"])
        break
    tripped = (
        distinct_classes >= dispatcher._CROSS_CASE_FINGERPRINT_MIN_DISTINCT_CLASSES
        and occurrence_count >= dispatcher._CROSS_CASE_FINGERPRINT_MIN_OCCURRENCES
    )
    return {
        "signal_name": "cross_case_fingerprint",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": occurrence_count,
        "magnitude": float(occurrence_count),
        "tripped": tripped,
        "extras": (
            [
                f"Fingerprint {short_hash} still spans {distinct_classes} class(es), "
                f"{occurrence_count} occurrence(s)."
            ]
            if tripped
            else []
        ),
    }


async def _recover_recurring_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    original_class = (
        regression_class.split(":", 1)[1] if ":" in regression_class else ""
    )
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
    by_class = dispatcher._group_recurring_class_entries(all_cases)
    entries = by_class.get(original_class, [])
    tripped = bool(entries) and is_recurring_cross_layer_class(
        entries,
        min_cycles=dispatcher._RECURRING_CLASS_MIN_CYCLES,
        min_layers=dispatcher._RECURRING_CLASS_MIN_LAYERS,
    )
    targeted = aggregate_targeted_layers(entries) if entries else set()
    return {
        "signal_name": "recurring_class",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": len(entries),
        "magnitude": float(len(entries)),
        "tripped": tripped,
        "extras": (
            [
                f"Class '{original_class}' still recurring across "
                f"{len(entries)} cycle(s), {len(targeted)} layer(s)."
            ]
            if tripped
            else []
        ),
    }


async def _recover_strategic_cycle_stall(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    now = _utcnow()
    stall_cutoff = now - timedelta(hours=dispatcher._STRATEGIC_CYCLE_STALL_HOURS)
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
    tripped = True
    if any(
        (t.first_payload or {}).get("type") in dispatcher._STRATEGIC_TASK_TYPES
        and t.status == TaskStatus.COMPLETED
        for t in recent_tasks
    ) or not any(
        (t.first_payload or {}).get("type") not in dispatcher._STRATEGIC_TASK_TYPES
        for t in recent_tasks
    ):
        tripped = False
    else:
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
            tripped = False
    return {
        "signal_name": "strategic_cycle_stall",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": len(recent_tasks),
        "magnitude": float(dispatcher._STRATEGIC_CYCLE_STALL_HOURS) if tripped else 0.0,
        "tripped": tripped,
        "extras": (
            [
                f"No strategic cycle completed in {dispatcher._STRATEGIC_CYCLE_STALL_HOURS}h."
            ]
            if tripped
            else []
        ),
    }


async def _recover_agent_vm_resource(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    failures = await dispatcher._collect_agent_vm_failures(session, project_id)
    count = len(failures)
    tripped = count >= dispatcher._AGENT_VM_RESOURCE_MIN_FAILURES
    return {
        "signal_name": "agent_vm_resource",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": count,
        "magnitude": float(count),
        "tripped": tripped,
        "extras": (
            [
                f"{count} VM-infrastructure failures in the last "
                f"{dispatcher._AGENT_VM_RESOURCE_WINDOW_HOURS}h."
            ]
            if tripped
            else []
        ),
    }


async def _recover_prevention_low(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
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
    tripped = (
        avg_rate is not None
        and avg_rate < dispatcher._PREVENTION_RATE_THRESHOLD
        and eligible_count >= dispatcher._PREVENTION_RATE_MIN_SAMPLE
    )
    return {
        "signal_name": "low_prevention_rate_7d",
        "prior_rate": dispatcher._PREVENTION_RATE_THRESHOLD,
        "current_rate": avg_rate,
        "sample_size": eligible_count,
        "magnitude": (
            round(dispatcher._PREVENTION_RATE_THRESHOLD - avg_rate, 3)
            if avg_rate is not None
            else None
        ),
        "tripped": tripped,
        "extras": (
            [f"7d cross-layer prevention rate {avg_rate:.0%} below floor."]
            if tripped and avg_rate is not None
            else []
        ),
    }


async def _recover_sr_below_floor(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    from app.playbook_learning import (
        _FLOOR_MIN_CONSECUTIVE_WINDOWS,
        _SR_FLOOR_THRESHOLD,
    )

    doc = project_design_doc or {}
    categories = (doc.get("sr_catch_rate_trends") or {}).get("categories") or {}
    floor_threshold = float(doc.get("sr_floor_threshold") or _SR_FLOOR_THRESHOLD)
    floor_min_windows = int(
        doc.get("sr_floor_min_windows") or _FLOOR_MIN_CONSECUTIVE_WINDOWS
    )
    tripped_cats: list[tuple[str, dict]] = []
    if isinstance(categories, dict):
        for cat, data in sorted(categories.items()):
            if not isinstance(data, dict) or not data.get("below_floor"):
                continue
            rate = data.get("current_hit_rate")
            history = data.get("rate_history") or []
            if rate is None or len(history) < floor_min_windows:
                continue
            tripped_cats.append((cat, data))
    rep = tripped_cats[0][1] if tripped_cats else {}
    rep_rate = rep.get("current_hit_rate")
    return {
        "signal_name": "sr_catch_rate_below_floor",
        "prior_rate": floor_threshold,
        "current_rate": rep_rate,
        "sample_size": int(rep.get("current_human_count") or 0),
        "magnitude": (
            round(floor_threshold - (rep_rate or 0), 3) if tripped_cats else None
        ),
        "tripped": bool(tripped_cats),
        "extras": (
            [
                "SR catch-rate still below floor for: "
                + ", ".join(cat for cat, _ in tripped_cats)
            ]
            if tripped_cats
            else []
        ),
    }


async def _recover_sr_pass_engagement_thinning(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    from app.playbook_learning import (
        _SR_ENGAGEMENT_MIN_PASSES,
        _SR_ENGAGEMENT_THRESHOLD,
    )

    doc = project_design_doc or {}
    categories = (doc.get("sr_catch_rate_trends") or {}).get("categories") or {}
    eng_threshold = float(
        doc.get("sr_engagement_threshold") or _SR_ENGAGEMENT_THRESHOLD
    )
    eng_min_passes = int(
        doc.get("sr_engagement_min_passes") or _SR_ENGAGEMENT_MIN_PASSES
    )
    thinning: list[tuple[str, dict]] = []
    if isinstance(categories, dict):
        for cat, data in sorted(categories.items()):
            if not isinstance(data, dict):
                continue
            rate = data.get("reasoning_engagement_rate")
            total = data.get("reasoning_engagement_total") or 0
            if rate is None or total < eng_min_passes:
                continue
            if rate < eng_threshold:
                thinning.append((cat, data))
    rep = thinning[0][1] if thinning else {}
    rep_rate = rep.get("reasoning_engagement_rate")
    return {
        "signal_name": "sr_pass_engagement_thinning",
        "prior_rate": eng_threshold,
        "current_rate": rep_rate,
        "sample_size": int(rep.get("reasoning_engagement_total") or 0),
        "magnitude": (
            round(eng_threshold - (rep_rate or 0.0), 3) if thinning else None
        ),
        "tripped": bool(thinning),
        "extras": (
            [
                "SR PASS engagement still thin for: "
                + ", ".join(cat for cat, _ in thinning)
            ]
            if thinning
            else []
        ),
    }


async def _recover_sr_false_attestation(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    from app.playbook_learning import _FALSE_ATTESTATION_RATE_THRESHOLD

    per_category = (
        (project_design_doc or {}).get("sr_false_attestation_trends") or {}
    ).get("per_category") or {}
    elevated: list[tuple[str, dict]] = []
    if isinstance(per_category, dict):
        for cat, data in sorted(per_category.items()):
            if not isinstance(data, dict):
                continue
            rate = data.get("false_attestation_rate")
            if rate is not None and rate > _FALSE_ATTESTATION_RATE_THRESHOLD:
                elevated.append((cat, data))
    rep = elevated[0][1] if elevated else {}
    rep_rate = rep.get("false_attestation_rate")
    prev_rate = rep.get("previous_false_attestation_rate")
    return {
        "signal_name": "sr_false_attestation",
        "prior_rate": prev_rate,
        "current_rate": rep_rate,
        "sample_size": int(
            (rep.get("false_attested") or 0) + (rep.get("ci_confirmed") or 0)
        ),
        "magnitude": (
            round((rep_rate or 0.0) - (prev_rate or 0.0), 3) if elevated else None
        ),
        "tripped": bool(elevated),
        "extras": (
            [
                "SR false-attestation still elevated for: "
                + ", ".join(cat for cat, _ in elevated)
            ]
            if elevated
            else []
        ),
    }


async def _recover_fix_loop_divergence_fingerprint(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    short_fingerprint = (
        regression_class.split(":", 1)[1] if ":" in regression_class else ""
    )
    trips = await _dispatcher_module()._collect_fix_loop_divergence_fingerprints(
        session,
        project_id,
    )
    match = next(
        (t for t in trips if t["short_fingerprint"] == short_fingerprint), None
    )
    count = match["count"] if match else 0
    return {
        "signal_name": "fix_loop_divergence_fingerprint",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": count,
        "magnitude": float(count),
        "tripped": match is not None,
        "extras": (
            [
                f"Fix-loop divergence fingerprint {short_fingerprint} still "
                f"recurring across {count} task(s)."
            ]
            if match is not None
            else []
        ),
    }


async def _recover_observation_alert_sla(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None,
) -> dict[str, Any]:
    dispatcher = _dispatcher_module()
    title = regression_class.split(":", 1)[1] if ":" in regression_class else ""
    now = _utcnow()
    sla_hours = dispatcher._OBSERVATION_ALERT_SLA_HOURS.get(
        title, dispatcher._OBSERVATION_ALERT_SLA_HOURS_DEFAULT
    )
    open_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        )
        .scalars()
        .all()
    )
    elapsed_hours = 0.0
    for alert in open_alerts:
        created = alert.created_at
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        hours = (now - created).total_seconds() / 3600
        elapsed_hours = max(elapsed_hours, hours)
    tripped = elapsed_hours >= sla_hours and bool(open_alerts)
    return {
        "signal_name": "observation_alert_sla",
        "prior_rate": None,
        "current_rate": None,
        "sample_size": len(open_alerts),
        "magnitude": elapsed_hours,
        "tripped": tripped,
        "extras": (
            [
                f"Observation alert '{title}' still open {elapsed_hours:.1f}h "
                f"(SLA {sla_hours}h)."
            ]
            if tripped
            else []
        ),
    }


class RegressionClassShape(NamedTuple):
    """One spawnable regression_class shape."""

    signal_name: str
    match: str
    token: str
    example: str
    evaluator: (
        Callable[
            [AsyncSession, uuid.UUID, str, dict | None],
            Any,
        ]
        | None
    ) = None


SPAWNABLE_REGRESSION_CLASS_SHAPES: tuple[RegressionClassShape, ...] = (
    RegressionClassShape("sr_catch_rate", "exact", "sr_catch_rate", "sr_catch_rate"),
    RegressionClassShape(
        "sr_catch_rate",
        "prefix",
        "sr_catch_rate:",
        "sr_catch_rate:correctness:dir-coverage-test",
    ),
    RegressionClassShape(
        "sr_catch_rate", "prefix", "sr_catch_rate:", "sr_catch_rate:correctness"
    ),
    RegressionClassShape(
        "sr_false_positive", "exact", "sr_false_positive", "sr_false_positive"
    ),
    RegressionClassShape(
        "sr_false_positive",
        "prefix",
        "sr_false_positive:",
        "sr_false_positive:correctness",
    ),
    RegressionClassShape(
        "fix_loop_divergence", "exact", "fix_loop_divergence", "fix_loop_divergence"
    ),
    RegressionClassShape(
        "fix_loop_divergence",
        "prefix",
        "fix_loop_divergence:",
        "fix_loop_divergence:lint",
    ),
    RegressionClassShape(
        "sr_blind_spot",
        "prefix",
        "sr_blind_spot:theme:",
        "sr_blind_spot:theme:flaky-async",
    ),
    RegressionClassShape(
        "sr_blind_spot", "prefix", "sr_blind_spot:", "sr_blind_spot:correctness"
    ),
    RegressionClassShape(
        "pipeline_success_rate",
        "exact",
        "pipeline_success_rate",
        "pipeline_success_rate",
    ),
    RegressionClassShape(
        "persistent_anomaly", "prefix", "anomaly:", "anomaly:cov-test"
    ),
    RegressionClassShape(
        "telemetry_stale",
        "prefix",
        "telemetry_stale:",
        "telemetry_stale:sr_catch_rate",
    ),
    RegressionClassShape(
        "telemetry_never_sufficient",
        "prefix",
        "telemetry_never_sufficient:",
        "telemetry_never_sufficient:sr_catch_rate",
    ),
    RegressionClassShape(
        "telemetry_sufficiency_stale",
        "prefix",
        "telemetry_sufficiency_stale:",
        "telemetry_sufficiency_stale:sr_catch_rate",
    ),
    RegressionClassShape(
        "background_service_crash",
        "prefix",
        "background_service_crash:",
        "background_service_crash:reaper",
    ),
    RegressionClassShape(
        "viewport_regression",
        "prefix",
        "viewport_regression:",
        "viewport_regression:DashboardHome:overflow@375",
        _recover_viewport_regression,
    ),
    RegressionClassShape(
        "viewport_coverage_drop",
        "exact",
        "viewport_coverage_drop",
        "viewport_coverage_drop",
        _recover_viewport_coverage_drop,
    ),
    RegressionClassShape(
        "reviewer_sla_stall",
        "prefix",
        "reviewer_sla_stall:",
        "reviewer_sla_stall:00000000-0000-0000-0000-000000000000",
        _recover_reviewer_sla_stall,
    ),
    RegressionClassShape(
        "layer_concentration",
        "prefix",
        "layer_concentration:",
        f"layer_concentration:{LAYER_DISPLAY_ORDER[0]}",
        _recover_layer_concentration,
    ),
    RegressionClassShape(
        "cross_case_fingerprint",
        "prefix",
        "cross_case_fingerprint:",
        "cross_case_fingerprint:deadbeef",
        _recover_cross_case_fingerprint,
    ),
    RegressionClassShape(
        "recurring_class",
        "prefix",
        "recurring_class:",
        "recurring_class:sr_catch_rate:correctness",
        _recover_recurring_class,
    ),
    RegressionClassShape(
        "strategic_cycle_stall",
        "exact",
        "strategic_cycle_stall",
        "strategic_cycle_stall",
        _recover_strategic_cycle_stall,
    ),
    RegressionClassShape(
        "agent_vm_resource",
        "exact",
        "agent_vm_resource_exhaustion",
        "agent_vm_resource_exhaustion",
        _recover_agent_vm_resource,
    ),
    RegressionClassShape(
        "low_prevention_rate_7d",
        "exact",
        "cross_layer_defense_prevention_low",
        "cross_layer_defense_prevention_low",
        _recover_prevention_low,
    ),
    RegressionClassShape(
        "sr_catch_rate_below_floor",
        "exact",
        "sr_catch_rate_below_floor",
        "sr_catch_rate_below_floor",
        _recover_sr_below_floor,
    ),
    RegressionClassShape(
        "sr_pass_engagement_thinning",
        "exact",
        "sr_pass_engagement_thinning",
        "sr_pass_engagement_thinning",
        _recover_sr_pass_engagement_thinning,
    ),
    RegressionClassShape(
        "sr_false_attestation",
        "exact",
        "sr_false_attestation",
        "sr_false_attestation",
        _recover_sr_false_attestation,
    ),
    RegressionClassShape(
        "fix_loop_divergence_fingerprint",
        "prefix",
        "fix_loop_divergence_fingerprint:",
        "fix_loop_divergence_fingerprint:cov-test-fp",
        _recover_fix_loop_divergence_fingerprint,
    ),
    RegressionClassShape(
        "observation_alert_sla",
        "prefix",
        "observation_alert_sla:",
        "observation_alert_sla:Cross-layer backfill lag: stalled",
        _recover_observation_alert_sla,
    ),
)


def match_spawnable_shape(regression_class: str) -> RegressionClassShape | None:
    """Return the first canonical shape matching ``regression_class`` (or None)."""
    for shape in SPAWNABLE_REGRESSION_CLASS_SHAPES:
        if shape.match == "exact" and regression_class == shape.token:
            return shape
        if shape.match == "prefix" and regression_class.startswith(shape.token):
            return shape
    return None
