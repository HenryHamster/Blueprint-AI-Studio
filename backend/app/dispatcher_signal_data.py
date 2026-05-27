"""Pure data-collection helpers for dispatcher signal evaluation.

This module owns the signal-value lookup (``_current_signal_value_for_class``)
and the cross-case / fingerprint / failure-collection helpers used by both
the detection path inside ``pipeline_regression_dispatcher`` AND by the
recovery evaluators in ``regression_recovery_evaluators`` AND by the
watchdog in ``regression_recovery_watchdog``. Extracting them here lets
recovery and watchdog import the canonical implementation directly without
the late ``from app import pipeline_regression_dispatcher as dispatcher``
indirection that previously avoided the import cycle.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dispatcher_formatters import (
    _SR_FP_RATE_FLOOR,
    _parse_iso_timestamp,
    _parse_signal_timestamp,
)
from app.models import (
    RegressionRemediationCase,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


# Per-fingerprint fix-loop divergence threshold. Same noisy-code-path signal,
# but grouped by normalized error fingerprint so a poisoned area gets a
# targeted investigation instead of only the aggregate rate signal.
_FIX_LOOP_DIVERGENCE_FINGERPRINT_MIN_TASKS = 3
_FIX_LOOP_DIVERGENCE_FINGERPRINT_WINDOW_DAYS = 7

# Viewport-regression signal thresholds. Viewport tests run less often than fix-loop
# cycles, so the minimum failure count is lower. Same 7-day window as fingerprints.
_VIEWPORT_REGRESSION_MIN_FAILURES = 2
_VIEWPORT_REGRESSION_WINDOW_DAYS = 7

# Agent VM resource exhaustion signal thresholds.
# VM-layer failures (OOM, spot preemption, provisioning failure, heartbeat loss)
# must reach this count in the 24h window before an investigation is spawned.
# Informed by the playbook's e2-standard-2 memory-pressure entry: 3 occurrences
# is noise suppression matching _ANOMALY_DISPATCH_MIN_OCCURRENCES.
_AGENT_VM_RESOURCE_MIN_FAILURES = 3
_AGENT_VM_RESOURCE_WINDOW_HOURS = 24


# Meta-investigation regression_class prefixes that are NOT themselves grouped
# by the recurring-class evaluator (shared by detection + recovery module).
_RECURRING_CLASS_META_PREFIXES = (
    "layer_concentration:",
    "cross_case_fingerprint:",
    "recurring_class:",
    "anomaly:",
)


def _signal_higher_is_better(regression_class: str) -> bool:
    """Return True when a HIGHER ``current_rate`` is the recovery direction.

    Mirrors the ``tripped`` formulas in ``_current_signal_value_for_class``:
    sr_catch_rate trips on ``prior - curr`` (a hit-rate dropping is regression),
    and pipeline_success_rate is a success-percentage. All other classes
    (divergence, blind-spot miss-rate, anomaly counts, telemetry-stale age,
    crash recency) are lower-is-better.
    """
    if regression_class == "sr_catch_rate" or regression_class.startswith(
        "sr_catch_rate:"
    ):
        return True
    if regression_class in (
        "pipeline_success_rate",
        "sr_catch_rate_below_floor",
        "sr_pass_engagement_thinning",
    ):
        return True
    return False


async def _current_signal_value_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None = None,
) -> dict[str, Any] | None:
    # Lazy imports break the dispatcher ↔ evaluators ↔ this-module cycle.
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD
    from app.models import Task as TaskModel
    from app.pipeline_health import compute_pipeline_health_summary
    from app.pipeline_regression_dispatcher import (
        _ANOMALY_DISPATCH_MIN_OCCURRENCES,
        _ANOMALY_DISPATCH_RECENCY_HOURS,
        _PIPELINE_SUCCESS_MIN_SAMPLE,
        _SR_BLIND_SPOT_THEME_FLOOR,
        _SR_FP_MIN_SAMPLE,
        _get_staleness_raw_ts,
    )
    from app.regression_recovery_evaluators import (
        SPAWNABLE_REGRESSION_CLASS_SHAPES,
        get_telemetry_signal_spec,
    )

    # Bucket-level checks: tripped=True if any constituent signal is still tripped.
    if regression_class == "sr_catch_rate":
        categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
            "categories"
        ) or {}
        for cat in sorted(categories):
            rc = f"sr_catch_rate:{cat}"
            state = await _current_signal_value_for_class(
                session, project_id, rc, project_design_doc
            )
            if state is not None and state["tripped"]:
                return state
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": False,
            "extras": [],
        }

    if regression_class == "sr_false_positive":
        categories = ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
            "categories"
        ) or {}
        for cat in sorted(categories):
            rc = f"sr_false_positive:{cat}"
            state = await _current_signal_value_for_class(
                session, project_id, rc, project_design_doc
            )
            if state is not None and state["tripped"]:
                return state
        return {
            "signal_name": "sr_false_positive",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": False,
            "extras": [],
        }

    if regression_class == "fix_loop_divergence":
        for loop in ("lint", "test", "check"):
            rc = f"fix_loop_divergence:{loop}"
            state = await _current_signal_value_for_class(session, project_id, rc)
            if state is not None and state["tripped"]:
                return state
        return {
            "signal_name": "fix_loop_divergence",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": False,
            "extras": [],
        }

    _sr_parts = regression_class.split(":", 2)
    if len(_sr_parts) == 3 and _sr_parts[0] == "sr_catch_rate":
        # Per-direction form: sr_catch_rate:{cat}:{direction_id}
        from app.playbook_learning import _MIN_DIRECTION_SR_SAMPLE

        _, cat, direction_id = _sr_parts
        trends = (project_design_doc or {}).get("sr_catch_rate_trends") or {}
        dir_entry = (trends.get("by_direction") or {}).get(direction_id) or {}
        dir_cat = (dir_entry.get("categories") or {}).get(cat) or {}
        direction_hit_rate = dir_cat.get("hit_rate")
        sample_count = dir_cat.get("sample_count", 0)
        if direction_hit_rate is None or sample_count < _MIN_DIRECTION_SR_SAMPLE:
            return None
        pw_cat = (trends.get("categories") or {}).get(cat) or {}
        baseline = pw_cat.get("previous_hit_rate") or pw_cat.get("current_hit_rate")
        if baseline is None:
            return None
        magnitude = round(baseline - direction_hit_rate, 3)
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": baseline,
            "current_rate": direction_hit_rate,
            "sample_size": sample_count,
            "magnitude": magnitude,
            "tripped": magnitude >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"Self-review catch-rate for category '{cat}' in direction '{direction_id}' is regressing."
            ],
        }

    if regression_class.startswith("sr_catch_rate:"):
        cat = regression_class.split(":", 1)[1]
        data = (
            ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
                "categories"
            )
            or {}
        ).get(cat) or {}
        prior = data.get("previous_hit_rate")
        curr = data.get("current_hit_rate")
        raw_sample = data.get("current_human_count")
        if prior is None or curr is None:
            return None
        sample_size = int(raw_sample) if raw_sample is not None else _MIN_SR_SAMPLE
        if raw_sample is not None and sample_size < _MIN_SR_SAMPLE:
            return None
        trend_sig = data.get("trend_significant")
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": prior,
            "current_rate": curr,
            "sample_size": sample_size,
            "magnitude": round(prior - curr, 3),
            "tripped": (prior - curr) >= _TREND_DELTA_THRESHOLD
            and trend_sig is not False,
            "extras": [f"Self-review catch-rate for category '{cat}' is regressing."],
        }

    if regression_class.startswith("sr_false_positive:"):
        cat = regression_class.split(":", 1)[1]
        data = (
            ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get(
                "categories"
            )
            or {}
        ).get(cat) or {}
        curr_fp_rate = data.get("false_positive_rate")
        prev_fp_rate = data.get("previous_fp_rate")
        sample_size = int(data.get("current_human_count") or 0)
        if curr_fp_rate is None or prev_fp_rate is None:
            return None
        if sample_size < _SR_FP_MIN_SAMPLE:
            return None
        tripped = (
            curr_fp_rate >= _SR_FP_RATE_FLOOR and prev_fp_rate >= _SR_FP_RATE_FLOOR
        )
        return {
            "signal_name": "sr_false_positive",
            "prior_rate": prev_fp_rate,
            "current_rate": curr_fp_rate,
            "sample_size": sample_size,
            "magnitude": round(curr_fp_rate - prev_fp_rate, 3),
            "tripped": tripped,
            "extras": [
                f"SR false-positive rate for category '{cat}' is {curr_fp_rate:.1%} "
                f"(prior window: {prev_fp_rate:.1%}) — exceeds floor of {_SR_FP_RATE_FLOOR:.0%} "
                "in both windows."
            ],
        }

    if regression_class.startswith("fix_loop_divergence:"):
        loop = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                        TaskModel.created_at >= now - timedelta(days=14),
                    )
                )
            )
            .scalars()
            .all()
        )

        def _naive(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        c7 = _naive(now - timedelta(days=7))
        c14 = _naive(now - timedelta(days=14))
        stats = {"curr": {"n": 0, "d": 0}, "prev": {"n": 0, "d": 0}}
        for start, end, bucket in (
            (c7, datetime(9999, 12, 31), "curr"),
            (c14, c7, "prev"),
        ):
            for t in tasks:
                ts = _naive(t.created_at) if t.created_at else None
                if ts is None or not (start <= ts < end):
                    continue
                bucket_hist = (t.fix_loop_history or {}).get(loop) or []
                if not bucket_hist:
                    continue
                last_exit: str | None = None
                for ev in reversed(bucket_hist):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None and isinstance(bucket_hist[-1], dict):
                    last_exit = bucket_hist[-1].get("exit_reason")
                stats[bucket]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    stats[bucket]["d"] += 1
        cn, cd = stats["curr"]["n"], stats["curr"]["d"]
        pn, pd = stats["prev"]["n"], stats["prev"]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            return None
        curr_rate = cd / cn
        prior_rate = pd / pn
        return {
            "signal_name": "fix_loop_divergence",
            "prior_rate": round(prior_rate, 3),
            "current_rate": round(curr_rate, 3),
            "sample_size": cn,
            "magnitude": round(curr_rate - prior_rate, 3),
            "tripped": (curr_rate - prior_rate) >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"Fix-loop '{loop}' divergence rate is rising: {pd}/{pn} → {cd}/{cn} divergent."
            ],
        }

    if regression_class.startswith("sr_blind_spot:theme:"):
        from app.sr_blind_spots import aggregate_sr_blind_spot_themes

        theme = regression_class.split(":", 2)[2]
        themes = await aggregate_sr_blind_spot_themes(
            session, project_id, days=14, limit=100
        )
        count = next((e["count"] for e in themes if e["theme"] == theme), 0)
        return {
            "signal_name": "sr_blind_spot",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": count,
            "magnitude": float(count),
            "tripped": count >= _SR_BLIND_SPOT_THEME_FLOOR,
            "extras": [
                f"SR blind-spot theme '{theme}' appeared {count}x in the last 14 days."
            ],
        }

    if regression_class.startswith("sr_blind_spot:"):
        from app.sr_blind_spots import _count_reviewer_disagreement_directions
        from app.models import Task as TaskModel

        cat = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.status == TaskStatus.COMPLETED,
                        TaskModel.created_at >= now - timedelta(days=7),
                    )
                )
            )
            .scalars()
            .all()
        )

        def _naive(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        dirs = _count_reviewer_disagreement_directions(tasks)
        c_rate, c_denom = None, 0
        for (k_cat, _k_type), bucket in dirs.items():
            if k_cat != cat:
                continue
            denom = bucket["miss"] + bucket["tp"] + bucket["fp"]
            if denom < _MIN_SR_SAMPLE:
                continue
            rate = bucket["miss"] / denom
            if c_denom == 0 or denom > c_denom:
                c_rate = round(rate, 3)
                c_denom = denom
        if c_denom < _MIN_SR_SAMPLE:
            return None
        return {
            "signal_name": "sr_blind_spot",
            "prior_rate": None,
            "current_rate": c_rate,
            "sample_size": c_denom,
            "magnitude": c_rate,
            "tripped": (c_rate or 0.0) >= _TREND_DELTA_THRESHOLD,
            "extras": [
                f"SR blind-spot for category '{cat}' (reviewer-disagreement rate in current 7d window)."
            ],
        }

    if regression_class == "pipeline_success_rate":
        from app.models import Task as TaskModel

        summary = await compute_pipeline_health_summary(
            session, project_id, lookback_hours=24
        )
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        rows = (
            await session.execute(
                select(TaskModel.id, TaskModel.status, TaskModel.updated_at).where(
                    TaskModel.project_id == project_id,
                    TaskModel.updated_at >= window_start,
                    TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                )
            )
        ).all()
        sample_size = len(rows)
        if sample_size < _PIPELINE_SUCCESS_MIN_SAMPLE:
            return None
        failure_distribution = summary.get("failure_distribution") or {}
        extras = ["24h pipeline success-rate is declining."]
        if failure_distribution:
            top = ", ".join(
                f"{k}={v}" for k, v in list(failure_distribution.items())[:3]
            )
            extras.append(f"Top failure classes: {top}")
        return {
            "signal_name": "pipeline_success_rate",
            "prior_rate": None,
            "current_rate": summary.get("success_rate"),
            "sample_size": sample_size,
            "magnitude": None,
            "tripped": summary.get("trend") == "declining",
            "extras": extras,
        }

    if regression_class.startswith("anomaly:"):
        anomaly_key = regression_class[len("anomaly:") :]
        import app.shared_state as _ss
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

        entry = (_ss.persistent_anomalies.get(str(project_id)) or {}).get(anomaly_key)
        if entry is None:
            # Durable fallback: project.design_doc survives pod restarts.
            entry = (
                (project_design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY) or {}
            ).get(anomaly_key)
        if not isinstance(entry, dict):
            return {
                "signal_name": "persistent_anomaly",
                "prior_rate": None,
                "current_rate": None,
                "sample_size": 0,
                "magnitude": None,
                "tripped": False,
                "extras": [],
            }
        n = int(entry.get("occurrence_count") or 0)
        last_seen = str(entry.get("last_seen") or "")
        cutoff = (
            _utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
        ).isoformat()
        tripped = n >= _ANOMALY_DISPATCH_MIN_OCCURRENCES and last_seen >= cutoff
        return {
            "signal_name": "persistent_anomaly",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": n,
            "magnitude": float(n),
            "tripped": tripped,
            "extras": [f"Persistent anomaly '{anomaly_key}' seen {n}x."],
        }

    if regression_class.startswith("telemetry_stale:"):
        signal = regression_class[len("telemetry_stale:") :]
        doc = project_design_doc or {}
        now = _utcnow()
        raw_ts, threshold = _get_staleness_raw_ts(signal, doc, project_id)
        computed_at = _parse_iso_timestamp(raw_ts)
        if computed_at is None:
            return None
        age = now - computed_at
        return {
            "signal_name": "telemetry_stale",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": round(age.total_seconds() / 3600, 2),
            "tripped": age > threshold,
            "extras": [
                f"Signal '{signal}' last computed {round(age.total_seconds() / 3600, 1)}h ago."
            ],
        }

    if regression_class.startswith("telemetry_never_sufficient:"):
        signal = regression_class[len("telemetry_never_sufficient:") :]
        spec = get_telemetry_signal_spec(signal)
        if spec is None or spec.never_sufficient_regression_class != regression_class:
            return None
        if spec.signal_heartbeat_at is None or spec.last_sufficient_at is None:
            return None
        doc = project_design_doc or {}
        signal_ts = spec.signal_heartbeat_at(doc, project_id)
        if not signal_ts:
            return None
        has_sufficient = bool(spec.last_sufficient_at(doc, project_id))
        suffix = "" if not has_sufficient else "now has"
        return {
            "signal_name": "telemetry_never_sufficient",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": 0.0,
            "tripped": not has_sufficient,
            "extras": [
                f"Signal '{signal}' has {suffix if suffix else 'not'} reached data sufficiency."
            ],
        }

    if regression_class.startswith("telemetry_sufficiency_stale:"):
        signal = regression_class[len("telemetry_sufficiency_stale:") :]
        spec = get_telemetry_signal_spec(signal)
        if spec is None or spec.sufficiency_stale_regression_class != regression_class:
            return None
        if spec.signal_heartbeat_at is None or spec.last_sufficient_at is None:
            return None
        doc = project_design_doc or {}
        signal_ts = spec.signal_heartbeat_at(doc, project_id)
        sufficient_ts = spec.last_sufficient_at(doc, project_id)
        if not signal_ts or not sufficient_ts:
            return None
        signal_at = _parse_iso_timestamp(signal_ts)
        sufficient_at = _parse_iso_timestamp(sufficient_ts)
        now = _utcnow()
        if (
            signal_at is None
            or sufficient_at is None
            or spec.frozen_signal_fresh_window is None
            or spec.sufficiency_stale_window is None
        ):
            return None
        is_fresh = (now - signal_at) <= spec.frozen_signal_fresh_window
        is_frozen = (now - sufficient_at) > spec.sufficiency_stale_window
        age = now - sufficient_at
        return {
            "signal_name": "telemetry_sufficiency_stale",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": round(age.total_seconds() / 3600, 2),
            "tripped": is_fresh and is_frozen,
            "extras": [
                f"Signal '{signal}' frozen: real data stale for "
                f"{round(age.total_seconds() / 3600, 1)}h but signal computing."
            ],
        }

    if regression_class.startswith("background_service_crash:"):
        service = regression_class[len("background_service_crash:") :]
        from app.models import Task as TaskModel

        now = _utcnow()
        # Considered still crashing if a crash investigation task was spawned
        # for this service within the last 2 hours (lifecycle._spawn_crash_investigations
        # writes spawned_by="crash_investigation" and service_name on each task).
        cutoff = now - timedelta(hours=2)
        recent_tasks = (
            (
                await session.execute(
                    select(TaskModel).where(
                        TaskModel.project_id == project_id,
                        TaskModel.created_at >= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        tripped = any(
            (t.context or {}).get("spawned_by") == "crash_investigation"
            and (t.context or {}).get("service_name") == service
            for t in recent_tasks
        )
        status_str = "still crashing" if tripped else "appears stable"
        return {
            "signal_name": "background_service_crash",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": 0,
            "magnitude": None,
            "tripped": tripped,
            "extras": [f"Background service '{service}' {status_str}."],
        }

    # Dynamic / activity-derived classes are dispatched through the canonical
    # registry below. Routing recovery through the same declaration the test
    # enumerates means a class cannot become spawnable without also gaining a
    # recovery branch — the gap that previously stranded ~two-thirds of classes
    # in MERGED until the stale watchdog escalated the loop's own merged fix.
    for shape in SPAWNABLE_REGRESSION_CLASS_SHAPES:
        if shape.evaluator is None:
            continue
        matched = (shape.match == "exact" and regression_class == shape.token) or (
            shape.match == "prefix" and regression_class.startswith(shape.token)
        )
        if matched:
            return await shape.evaluator(
                session, project_id, regression_class, project_design_doc
            )

    return None


def _index_cross_case_fingerprints(
    all_cases: list[RegressionRemediationCase],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    """Group symptom fingerprints across cases (shared by detection + recovery)."""
    by_fp: dict[str, dict[str, Any]] = {}
    for case in all_cases:
        case_class = case.regression_class or ""
        case_id = str(case.id)
        symptoms = case.symptoms_by_layer or {}
        if not isinstance(symptoms, dict):
            continue
        for entries in symptoms.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fp_raw = entry.get("fingerprint")
                if not isinstance(fp_raw, str):
                    continue
                fp = fp_raw.strip()
                if not fp:
                    continue
                if fp == case_class:
                    continue
                obs_dt = _parse_iso_timestamp(entry.get("observed_at"))
                if obs_dt is None or obs_dt < cutoff:
                    continue
                bucket = by_fp.setdefault(
                    fp,
                    {
                        "occurrences": 0,
                        "classes": set(),
                        "case_ids": [],
                        "samples": [],
                    },
                )
                bucket["occurrences"] += 1
                bucket["classes"].add(case_class)
                if case_id not in bucket["case_ids"]:
                    bucket["case_ids"].append(case_id)
                sample = entry.get("sample")
                if isinstance(sample, str) and sample:
                    short = sample[:500]
                    if short not in bucket["samples"] and len(bucket["samples"]) < 5:
                        bucket["samples"].append(short)
    return by_fp


def _group_recurring_class_entries(
    all_cases: list[RegressionRemediationCase],
) -> dict[str, list[dict[str, Any]]]:
    """Group layer-tagged fix-log entries by (non-meta) regression_class."""
    by_class: dict[str, list[dict[str, Any]]] = {}
    for case in all_cases:
        rc = case.regression_class or ""
        if any(rc.startswith(p) for p in _RECURRING_CLASS_META_PREFIXES):
            continue
        fix_log = case.fingerprint_to_fix_log or []
        if not isinstance(fix_log, list):
            continue
        entries = [e for e in fix_log if isinstance(e, dict) and "layers_targeted" in e]
        if entries:
            by_class.setdefault(rc, []).extend(entries)
    return by_class


async def _collect_fix_loop_divergence_fingerprints(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from app.failure_classification import _fingerprint_message
    from app.fix_loop_telemetry import DIVERGENCE_EXIT_REASONS
    from app.models import Task as TaskModel

    now = _utcnow()
    cutoff = now - timedelta(days=_FIX_LOOP_DIVERGENCE_FINGERPRINT_WINDOW_DAYS)
    rows = (
        (
            await session.execute(
                select(TaskModel)
                .where(
                    TaskModel.project_id == project_id,
                    TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    TaskModel.updated_at >= cutoff,
                )
                .order_by(TaskModel.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[str, dict[str, Any]] = {}
    for task in rows:
        ctx = task.context or {}
        files = [
            str(f) for f in (ctx.get("related_files") or []) if isinstance(f, str) and f
        ][:10]
        scope = str(ctx.get("scope") or "unknown")
        for bucket in (task.fix_loop_history or {}).values():
            for ev in bucket or []:
                if (
                    not isinstance(ev, dict)
                    or ev.get("exit_reason") not in DIVERGENCE_EXIT_REASONS
                ):
                    continue
                ts = (
                    _parse_signal_timestamp(ev.get("ended_at") or ev.get("started_at"))
                    or task.updated_at
                )
                if ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts < cutoff:
                    continue
                fingerprint = _fingerprint_message(str(ev.get("error_tail") or ""))
                if not fingerprint:
                    continue
                item = grouped.setdefault(
                    fingerprint,
                    {
                        "tasks": set(),
                        "samples": [],
                        "files": set(),
                        "scopes": set(),
                    },
                )
                item["tasks"].add(str(task.id))
                item["files"].update(files)
                item["scopes"].add(scope)
                if ev.get("error_tail") and len(item["samples"]) < 2:
                    item["samples"].append(str(ev.get("error_tail"))[:240])
    trips: list[dict[str, Any]] = []
    for fingerprint, item in grouped.items():
        count = len(item["tasks"])
        if count < _FIX_LOOP_DIVERGENCE_FINGERPRINT_MIN_TASKS:
            continue
        short_fingerprint = (
            "".join(
                ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in fingerprint
            )[:80].strip("-")
            or "unknown"
        )
        trips.append(
            {
                "fingerprint": fingerprint,
                "short_fingerprint": short_fingerprint,
                "count": count,
                "samples": item["samples"],
                "files": sorted(item["files"]),
                "scopes": sorted(item["scopes"]),
            }
        )
    return sorted(trips, key=lambda item: (-item["count"], item["fingerprint"]))


def _extract_failure_type(msg: str) -> str:
    """Extract failure type from viewport test error message.

    Returns 'overflow' for expectNoOverflowAt, 'text_truncation' for
    expectNoTextTruncationAt, 'touch_target' for expectMinTouchTargetAt,
    or 'unknown' if none is found.
    """
    if "expectNoOverflowAt" in msg:
        return "overflow"
    if "expectNoTextTruncationAt" in msg:
        return "text_truncation"
    if "expectMinTouchTargetAt" in msg:
        return "touch_target"
    return "unknown"


async def _collect_viewport_failures(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Collect recent VIEWPORT_REGRESSION task failures grouped by (component, width).

    Calls classify_failure() to filter for viewport-test errors, then extracts
    the component name (from .viewport.test.tsx file references in the message
    or task title) and the viewport width (from expectNoOverflowAt/expectMinTouchTargetAt
    error strings). Falls back to "unknown" / "0" when extraction fails.
    Also classifies each failure as 'overflow', 'touch_target', or 'mixed'.
    """
    from app.failure_classification import VIEWPORT_REGRESSION, classify_failure
    from app.models import Task as TaskModel

    now = _utcnow()
    cutoff = now - timedelta(days=_VIEWPORT_REGRESSION_WINDOW_DAYS)
    rows = (
        (
            await session.execute(
                select(TaskModel)
                .where(
                    TaskModel.project_id == project_id,
                    TaskModel.status == TaskStatus.FAILED,
                    TaskModel.updated_at >= cutoff,
                )
                .order_by(TaskModel.updated_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    _width_re = re.compile(
        r"expect(?:NoOverflow|NoTextTruncation|MinTouchTarget)At\((\d+)\)",
        re.IGNORECASE,
    )
    _component_re = re.compile(r"([\w-]+)\.viewport\.test\.tsx", re.IGNORECASE)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for task in rows:
        if classify_failure(task) != VIEWPORT_REGRESSION:
            continue
        msg = task.latest_status_message or ""
        combined = msg + " " + (task.title or "")

        width_m = _width_re.search(msg)
        width = width_m.group(1) if width_m else "0"

        comp_m = _component_re.search(combined)
        component = comp_m.group(1) if comp_m else "unknown"

        key = (component, width)
        item = grouped.setdefault(
            key,
            {
                "tasks": set(),
                "samples": [],
                "failure_types": set(),
                "earliest_task": None,
            },
        )
        item["tasks"].add(str(task.id))
        item["failure_types"].add(_extract_failure_type(msg))
        if msg and len(item["samples"]) < 2:
            item["samples"].append(msg[:240])
        # Track the earliest failing task for introducing-PR attribution.
        if (
            item["earliest_task"] is None
            or task.updated_at < item["earliest_task"].updated_at
        ):
            item["earliest_task"] = task

    trips: list[dict[str, Any]] = []
    for (component, width), item in grouped.items():
        count = len(item["tasks"])
        if count < _VIEWPORT_REGRESSION_MIN_FAILURES:
            continue
        # Determine failure_type: 'overflow', 'touch_target', or 'mixed'
        types = item["failure_types"] - {"unknown"}
        if not types:
            failure_type = "unknown"
        elif len(types) == 1:
            failure_type = types.pop()
        else:
            failure_type = "mixed"
        # Derive introducing-PR / introducing-commit from the earliest failing task.
        earliest_task = item.get("earliest_task")
        introducing_pr_number: int | None = None
        introducing_commit_sha: str | None = None
        if earliest_task is not None:
            pr_num = earliest_task.first_payload.get("pr_number")
            if pr_num is not None:
                try:
                    introducing_pr_number = int(pr_num)
                except (TypeError, ValueError):
                    pass
            if introducing_pr_number is None:
                head_sha = (earliest_task.context or {}).get(
                    "head_sha"
                ) or earliest_task.first_payload.get("head_sha")
                if head_sha:
                    introducing_commit_sha = str(head_sha)
        trips.append(
            {
                "component": component,
                "width": width,
                "count": count,
                "samples": item["samples"],
                "failure_type": failure_type,
                "introducing_pr_number": introducing_pr_number,
                "introducing_commit_sha": introducing_commit_sha,
            }
        )
    return sorted(trips, key=lambda x: (-x["count"], x["component"], x["width"]))


async def _collect_agent_vm_failures(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict]:
    """Return recent FAILED tasks classified by VM-resource exit reason.

    Inspects Task.context for failure_category, timeout_subtype, and
    spot_preemption_count. Only tasks whose failure is attributable to the
    AGENT_VM infrastructure layer are included; code-level timeouts (cascade,
    iteration_limit) are excluded so this signal does not double-fire with
    layer_concentration on the same events.

    Exit-reason labels: 'oom', 'timeout', 'spot_preempt', 'vm_provisioning_failure'.
    """
    cutoff = _utcnow() - timedelta(hours=_AGENT_VM_RESOURCE_WINDOW_HOURS)
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.FAILED,
                    Task.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    from app.failure_classification import classify_vm_exit_reason

    results = []
    for task in rows:
        exit_reason = classify_vm_exit_reason(task)
        if exit_reason is None:
            continue
        ctx = task.context or {}
        payload = task.first_payload or {}
        results.append(
            {
                "task_id": str(task.id),
                "task_type": payload.get("type", "unknown"),
                "exit_reason": exit_reason,
                "scope": ctx.get("scope", "unknown"),
            }
        )
    return results
