"""Pipeline-regression dispatcher.

Watches existing pipeline-wide regression signals (SR catch-rate, fix-loop
divergence, pipeline success-rate trend) and spawns ONE ``investigate`` task per
detected regression class so the existing investigate→fix chain
(``_handle_investigation_completion`` → ``_spawn_investigation_remediation``)
can autonomously diagnose and ship fixes for the autonomous loop's own
pipeline regressions.

Background-service-crash spawns are wired separately from the watchdog path
(``lifecycle._spawn_crash_investigations``). That path keys its own dedup off
``Task.context.service_name`` rather than ``regression_class``, so the two
paths do not share an in-flight dedup key — each enforces its own.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.deploy_health import (
    _DEPLOY_MONITORING_DEFER_WINDOW,
    get_deploy_monitoring_defer_until,
    record_deploy_monitoring_deferral,
)
from app.models import (
    Alert, AlertSeverity, DeploymentState,
    RegressionCaseStatus, RegressionRemediationCase,
    Task, TaskHistory, TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Cooldown between consecutive spawns for the SAME regression_class. Mirrors
# the self_audit_dispatch cooldown pattern — keeps a noisy regression from
# generating an investigate every cycle once the prior task terminates.
_REGRESSION_COOLDOWN = timedelta(hours=6)

# Per-cycle system-wide concurrency budget. A noisy regression cannot starve
# the rest of the pipeline by occupying every research slot.
_REGRESSION_DISPATCHER_SYSTEM_CAP = 2

# Conservative floor on the 24h pipeline-success-rate sample. Below this the
# trend label is too noisy to trigger an investigation. 10 completed/failed
# tasks in 24h is the same threshold operators use to mute "low signal" pages.
_PIPELINE_SUCCESS_MIN_SAMPLE = 10

# Give merged signals time to settle before the recovery scan re-evaluates them.
_REGRESSION_RECOVERY_COOLDOWN = timedelta(minutes=15)

# Watchdog staleness thresholds.  An ACTIVE case that hasn't been updated in
# 24h has silently stalled (investigation finished with low confidence and no
# remediation was spawned, or the close-loop step dropped it).  A MERGED case
# that hasn't been updated in 6h means the recovery scan cannot evaluate it
# (e.g. signal_snapshot_at_merge is missing).  Both escalate to
# NEEDS_MANUAL_INTERVENTION and open an Alert so operators are notified.
_WATCHDOG_ACTIVE_STALE_THRESHOLD = timedelta(hours=24)
_WATCHDOG_MERGED_STALE_THRESHOLD = timedelta(hours=6)

# Persistent-anomaly dispatch thresholds. An anomaly must recur at least this
# many times within the recency window before an investigation is spawned — the
# same philosophy as the 5pp SR delta floor: one bad cycle is noise, repeated
# occurrences are signal.
_ANOMALY_DISPATCH_MIN_OCCURRENCES = 3
_ANOMALY_DISPATCH_RECENCY_HOURS = 48

# SR blind-spot theme occurrence floor. A blind_spot theme must appear at least
# this many times across the 14d window before it is considered a structural gap
# in the self-review prompt rather than a one-off observation from a single task.
_SR_BLIND_SPOT_THEME_FLOOR = 5

_ACTIVE_INVESTIGATE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)


async def _last_spawn_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Optional[Task]:
    """Return the most recent dispatcher-spawned task for this class, if any.

    Filters by ``context['spawned_by']`` and ``context['regression_class']`` in
    the WHERE clause so the cooldown guard cannot be silently bypassed by a
    busy project generating >N unrelated tasks within the cooldown window.
    """
    return (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["spawned_by"].as_string()
            == "pipeline_regression_dispatcher",
            Task.context["regression_class"].as_string() == regression_class,
        ).order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()


async def _in_flight_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Optional[Task]:
    """Return the in-flight investigation for this regression class, if any.

    Only catches dispatcher-spawned tasks: any task with
    ``context.regression_class == <class>`` in non-terminal status. The
    crash-investigation path in ``lifecycle._spawn_crash_investigations``
    does not write ``regression_class`` and is deduped separately on
    ``service_name``.
    """
    rows = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(_ACTIVE_INVESTIGATE_STATUSES),
        )
    )).scalars().all()
    for t in rows:
        if (t.context or {}).get("regression_class") == regression_class:
            payload = t.first_payload or {}
            if payload.get("type") == "investigate":
                return t
    return None


async def _count_active_research_for_project(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Count research-shape tasks currently consuming a research slot.

    Mirrors the ``max_total_research_per_project`` accounting in
    ``autonomous_research.py``: research + investigate share the same budget so
    a regression flood cannot starve direction research.
    """
    rows = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([
                TaskStatus.QUEUED, TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
            ]),
        )
    )).scalars().all()
    n = 0
    for t in rows:
        ptype = (t.first_payload or {}).get("type")
        if ptype in ("research", "investigate"):
            n += 1
    return n


def _format_signal_lines(
    signal_name: str,
    regression_class: str,
    prior_rate: Optional[float],
    current_rate: Optional[float],
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
        "\nDiagnose the underlying root cause of this regression and produce "
        "the structured diagnosis JSON report ({summary, root_cause, "
        "recommended_fix, severity, confidence, affected_files}). "
        "Do NOT prescribe a fix — let the investigate→fix chain produce the "
        "remediation."
    )
    return "\n".join(lines)


def _format_prior_attempt_context(case: "RegressionRemediationCase") -> str:
    """Build a prior-attempt notice to append when re-spawning after >= 2 failures.

    Only called when existing_case.attempts >= 2 so the first re-arm (attempts==1)
    keeps the original prompt unchanged — this avoids noise on the second attempt
    and reserves the enrichment for cases that have already failed twice.
    """
    lines = [
        "",
        "PRIOR ATTEMPT CONTEXT",
        f"This regression has been investigated {case.attempts} time(s) without recovering the signal.",
    ]
    pr = case.last_remediation_pr_number
    if pr is not None:
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


def _format_bundle_signal_lines(signals: list[dict]) -> str:
    """Format a single investigate prompt covering a bundle of related signals."""
    lines = [
        f"Pipeline regression bundle: {len(signals)} related signal(s) tripped this cycle.",
        "Investigate the shared root cause across all signals below.\n",
    ]
    for s in signals:
        rc = s.get("regression_class", s["signal_name"])
        prior_str = f"{s['prior_rate']:.1%}" if s.get("prior_rate") is not None else "N/A"
        curr_str = f"{s['current_rate']:.1%}" if s.get("current_rate") is not None else "N/A"
        lines += [
            f"Signal: {s['signal_name']} / class={rc}",
            f"  Prior: {prior_str}  Current: {curr_str}  n={s.get('sample_size', 0)}",
        ]
        for extra in (s.get("extras") or []):
            lines.append(f"  {extra}")
    lines.append(
        "\nDiagnose the shared root cause and produce the structured diagnosis "
        "JSON ({summary, root_cause, recommended_fix, severity, confidence, "
        "affected_files}). Do NOT prescribe a fix — let the investigate→fix "
        "chain produce the remediation."
    )
    return "\n".join(lines)


async def _current_signal_value_for_class(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
    project_design_doc: dict | None = None,
) -> Optional[dict[str, Any]]:
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD  # noqa: PLC0415
    from app.models import Task as TaskModel  # noqa: PLC0415

    # Bucket-level checks: tripped=True if any constituent signal is still tripped.
    if regression_class == "sr_catch_rate":
        categories = (
            ((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get("categories") or {}
        )
        for cat in sorted(categories):
            rc = f"sr_catch_rate:{cat}"
            state = await _current_signal_value_for_class(session, project_id, rc, project_design_doc)
            if state is not None and state["tripped"]:
                return state
        return {"signal_name": "sr_catch_rate", "prior_rate": None, "current_rate": None,
                "sample_size": 0, "magnitude": None, "tripped": False, "extras": []}

    if regression_class == "fix_loop_divergence":
        for loop in ("lint", "test", "check"):
            rc = f"fix_loop_divergence:{loop}"
            state = await _current_signal_value_for_class(session, project_id, rc)
            if state is not None and state["tripped"]:
                return state
        return {"signal_name": "fix_loop_divergence", "prior_rate": None, "current_rate": None,
                "sample_size": 0, "magnitude": None, "tripped": False, "extras": []}

    if regression_class.startswith("sr_catch_rate:"):
        cat = regression_class.split(":", 1)[1]
        data = (((project_design_doc or {}).get("sr_catch_rate_trends") or {}).get("categories") or {}).get(cat) or {}
        prior = data.get("previous_hit_rate")
        curr = data.get("current_hit_rate")
        raw_sample = data.get("current_human_count")
        if prior is None or curr is None:
            return None
        sample_size = int(raw_sample) if raw_sample is not None else _MIN_SR_SAMPLE
        if raw_sample is not None and sample_size < _MIN_SR_SAMPLE:
            return None
        return {
            "signal_name": "sr_catch_rate",
            "prior_rate": prior,
            "current_rate": curr,
            "sample_size": sample_size,
            "magnitude": round(prior - curr, 3),
            "tripped": (prior - curr) >= _TREND_DELTA_THRESHOLD,
            "extras": [f"Self-review catch-rate for category '{cat}' is regressing."],
        }

    if regression_class.startswith("fix_loop_divergence:"):
        loop = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (await session.execute(
            select(TaskModel).where(
                TaskModel.project_id == project_id,
                TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                TaskModel.created_at >= now - timedelta(days=14),
            )
        )).scalars().all()
        def _naive(dt: datetime) -> datetime:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        c7 = _naive(now - timedelta(days=7))
        c14 = _naive(now - timedelta(days=14))
        stats = {"curr": {"n": 0, "d": 0}, "prev": {"n": 0, "d": 0}}
        for start, end, bucket in ((c7, datetime(9999, 12, 31), "curr"), (c14, c7, "prev")):
            for t in tasks:
                ts = _naive(t.created_at) if t.created_at else None
                if ts is None or not (start <= ts < end):
                    continue
                bucket_hist = (t.fix_loop_history or {}).get(loop) or []
                if not bucket_hist:
                    continue
                last_exit: Optional[str] = None
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
            "extras": [f"Fix-loop '{loop}' divergence rate is rising: {pd}/{pn} → {cd}/{cn} divergent."],
        }

    if regression_class.startswith("sr_blind_spot:theme:"):
        from app.sr_blind_spots import aggregate_sr_blind_spot_themes  # noqa: PLC0415
        theme = regression_class.split(":", 2)[2]
        themes = await aggregate_sr_blind_spot_themes(session, project_id, days=14, limit=100)
        count = next((e["count"] for e in themes if e["theme"] == theme), 0)
        return {
            "signal_name": "sr_blind_spot",
            "prior_rate": None,
            "current_rate": None,
            "sample_size": count,
            "magnitude": float(count),
            "tripped": count >= _SR_BLIND_SPOT_THEME_FLOOR,
            "extras": [f"SR blind-spot theme '{theme}' appeared {count}x in the last 14 days."],
        }

    if regression_class.startswith("sr_blind_spot:"):
        from app.sr_blind_spots import _count_reviewer_disagreement_directions  # noqa: PLC0415
        from app.models import Task as TaskModel  # noqa: PLC0415

        cat = regression_class.split(":", 1)[1]
        now = _utcnow()
        tasks = (await session.execute(
            select(TaskModel).where(
                TaskModel.project_id == project_id,
                TaskModel.status == TaskStatus.COMPLETED,
                TaskModel.created_at >= now - timedelta(days=7),
            )
        )).scalars().all()

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
            "extras": [f"SR blind-spot for category '{cat}' (reviewer-disagreement rate in current 7d window)."],
        }

    if regression_class == "pipeline_success_rate":
        from app.models import Task as TaskModel  # noqa: PLC0415
        from app.routers.admin_digest import compute_pipeline_health_summary  # noqa: PLC0415

        summary = await compute_pipeline_health_summary(session, project_id, lookback_hours=24)
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        rows = (await session.execute(
            select(TaskModel.id, TaskModel.status, TaskModel.updated_at).where(
                TaskModel.project_id == project_id,
                TaskModel.updated_at >= window_start,
                TaskModel.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            )
        )).all()
        sample_size = len(rows)
        if sample_size < _PIPELINE_SUCCESS_MIN_SAMPLE:
            return None
        failure_distribution = summary.get("failure_distribution") or {}
        extras = ["24h pipeline success-rate is declining."]
        if failure_distribution:
            top = ", ".join(f"{k}={v}" for k, v in list(failure_distribution.items())[:3])
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
        anomaly_key = regression_class[len("anomaly:"):]
        import app.shared_state as _ss  # noqa: PLC0415
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY  # noqa: PLC0415
        entry = (_ss.persistent_anomalies.get(str(project_id)) or {}).get(anomaly_key)
        if entry is None:
            # Durable fallback: project.design_doc survives pod restarts.
            entry = ((project_design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY) or {}).get(anomaly_key)
        if not isinstance(entry, dict):
            return {"signal_name": "persistent_anomaly", "prior_rate": None, "current_rate": None,
                    "sample_size": 0, "magnitude": None, "tripped": False, "extras": []}
        n = int(entry.get("occurrence_count") or 0)
        last_seen = str(entry.get("last_seen") or "")
        cutoff = (_utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)).isoformat()
        tripped = n >= _ANOMALY_DISPATCH_MIN_OCCURRENCES and last_seen >= cutoff
        return {"signal_name": "persistent_anomaly", "prior_rate": None, "current_rate": None,
                "sample_size": n, "magnitude": float(n), "tripped": tripped,
                "extras": [f"Persistent anomaly '{anomaly_key}' seen {n}x."]}

    return None


async def _get_remediation_case(
    session: AsyncSession,
    project_id: uuid.UUID,
    regression_class: str,
) -> Optional[RegressionRemediationCase]:
    return (await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )).scalars().first()


async def _maybe_spawn(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    signal_name: str,
    regression_class: str,
    prior_rate: Optional[float],
    current_rate: Optional[float],
    sample_size: int,
    magnitude: Optional[float],
    extras: list[str] | None = None,
    title_hint: str,
    extra_snapshot_fields: dict | None = None,
    prompt_override: str | None = None,
    extra_context_fields: dict | None = None,
) -> Optional[Task]:
    """Apply dedup + cooldown gates and spawn an investigate task on green light."""
    logger.info(
        "[REGRESSION_DISPATCHER] signal-tripped project=%s class=%s prior=%s "
        "current=%s sample=%d",
        project_id, regression_class, prior_rate, current_rate, sample_size,
    )

    in_flight = await _in_flight_for_class(session, project_id, regression_class)
    if in_flight is not None:
        logger.info(
            "[REGRESSION_DISPATCHER] dedup-hit project=%s class=%s in_flight_task=%s",
            project_id, regression_class, in_flight.id,
        )
        return None

    existing_case = await _get_remediation_case(session, project_id, regression_class)
    if existing_case is not None and existing_case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
        logger.info(
            "[REGRESSION_DISPATCHER] manual-intervention-suppressed project=%s "
            "class=%s case_id=%s",
            project_id, regression_class, existing_case.id,
        )
        return None
    if existing_case is not None and existing_case.status == RegressionCaseStatus.MERGED:
        # Recovery scan owns MERGED cases — let it decide CLOSED vs re_arm()
        # before any new investigation can fire, otherwise a same-class
        # spawn within the dispatcher cooldown window would race the
        # recovery loop this PR adds.
        logger.info(
            "[REGRESSION_DISPATCHER] merged-awaiting-recovery-scan project=%s "
            "class=%s case_id=%s",
            project_id, regression_class, existing_case.id,
        )
        return None

    last_spawn = await _last_spawn_for_class(session, project_id, regression_class)
    now = _utcnow()
    if last_spawn is not None and last_spawn.created_at:
        last_at = last_spawn.created_at
        if last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
        if now < last_at + _REGRESSION_COOLDOWN:
            logger.info(
                "[REGRESSION_DISPATCHER] cooldown-hit project=%s class=%s "
                "last_spawn=%s cooldown_until=%s",
                project_id, regression_class, last_at.isoformat(),
                (last_at + _REGRESSION_COOLDOWN).isoformat(),
            )
            return None

    cooldown_until = (now + _REGRESSION_COOLDOWN).isoformat()
    prompt = prompt_override if prompt_override is not None else _format_signal_lines(
        signal_name=signal_name,
        regression_class=regression_class,
        prior_rate=prior_rate,
        current_rate=current_rate,
        sample_size=sample_size,
        extras=extras,
    )
    if existing_case is not None and (existing_case.attempts or 0) >= 2:
        prompt += _format_prior_attempt_context(existing_case)
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": "main",
            "effort": "low",
        },
        "pipeline_regression_dispatcher:investigate",
    )
    context: dict[str, Any] = {
        "spawned_by": "pipeline_regression_dispatcher",
        "regression_class": regression_class,
        "signal_name": signal_name,
        "magnitude": magnitude,
        "prior_rate": prior_rate,
        "current_rate": current_rate,
        "sample_size": sample_size,
        "dispatcher_cooldown_until_iso": cooldown_until,
    }
    if extra_context_fields:
        context.update(extra_context_fields)
    new_task = Task(
        project_id=project_id,
        title=f"Investigate pipeline regression: {title_hint}"[:200],
        description=(
            f"Auto-spawned from pipeline-regression dispatcher "
            f"({signal_name})"
        )[:500],
        payloads=[payload],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.flush()
    session.add(TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "PIPELINE_REGRESSION_DISPATCHER",
            "regression_class": regression_class,
            "signal_name": signal_name,
        },
    ))
    session.add(Alert(
        project_id=project_id,
        severity=AlertSeverity.INFO,
        title=f"Pipeline regression investigation spawned: {regression_class}"[:200],
        message=(
            f"Signal {signal_name} tripped (prior={prior_rate}, "
            f"current={current_rate}, n={sample_size}). Investigation "
            f"task {new_task.id} queued."
        )[:1000],
    ))

    signal_snapshot: dict[str, Any] = {
        "prior_rate": prior_rate,
        "current_rate": current_rate,
        "sample_size": sample_size,
        "signal_name": signal_name,
        "magnitude": magnitude,
        "captured_at": now.isoformat(),
    }
    if extra_snapshot_fields:
        signal_snapshot.update(extra_snapshot_fields)
    if existing_case is None:
        session.add(RegressionRemediationCase(
            project_id=project_id,
            regression_class=regression_class,
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn=signal_snapshot,
            last_remediation_task_id=new_task.id,
            last_seen_at=now,
            hit_count=1,
        ))
    else:
        existing_case.signal_snapshot_at_spawn = signal_snapshot
        existing_case.hit_count = (existing_case.hit_count or 0) + 1
        existing_case.last_seen_at = now
        existing_case.last_remediation_task_id = new_task.id
        # Reactivate so admin state matches the in-flight remediation. A
        # CLOSED case re-tripping is a fresh regression cycle; without this
        # the case would stay CLOSED while a new investigation runs.
        existing_case.status = RegressionCaseStatus.ACTIVE
        session.add(existing_case)

    logger.info(
        "[REGRESSION_DISPATCHER] spawn-success project=%s class=%s task_id=%s",
        project_id, regression_class, new_task.id,
    )
    return new_task


async def _evaluate_sr_catch_rate_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped SR catch-rate categories and coalesce into one spawn."""
    if spawn_budget_left <= 0:
        return 0
    categories = (
        (project_design_doc or {}).get("sr_catch_rate_trends") or {}
    ).get("categories") or {}
    if not isinstance(categories, dict):
        return 0

    tripped: list[dict] = []
    for cat in sorted(categories):
        if not isinstance(categories[cat], dict):
            continue
        rc = f"sr_catch_rate:{cat}"
        state = await _current_signal_value_for_class(session, project_id, rc, project_design_doc)
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
    spawn = await _maybe_spawn(
        session, project_id,
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


async def _evaluate_fix_loop_divergence_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Collect all tripped fix-loop divergence loops and coalesce into one spawn."""
    if spawn_budget_left <= 0:
        return 0

    tripped: list[dict] = []
    for loop in ("lint", "test", "check"):
        rc = f"fix_loop_divergence:{loop}"
        state = await _current_signal_value_for_class(session, project_id, rc)
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
    spawn = await _maybe_spawn(
        session, project_id,
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


async def _evaluate_pipeline_success_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip pipeline_success_rate when 24h trend declines on a non-trivial sample."""
    if spawn_budget_left <= 0:
        return 0
    state = await _current_signal_value_for_class(session, project_id, "pipeline_success_rate")
    if state is None or not state["tripped"]:
        return 0
    spawn = await _maybe_spawn(
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


async def _probe_sr_blind_spot_any_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> bool:
    """Side-effect-free probe: would any sr_blind_spot signal trip right now?"""
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD  # noqa: PLC0415
    from app.sr_blind_spots import (  # noqa: PLC0415
        _count_reviewer_disagreement_directions,
        aggregate_sr_blind_spot_themes,
    )
    from app.models import Task as TaskModel  # noqa: PLC0415

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    tasks = (await session.execute(
        select(TaskModel).where(
            TaskModel.project_id == project_id,
            TaskModel.status == TaskStatus.COMPLETED,
            TaskModel.created_at >= now - timedelta(days=14),
        )
    )).scalars().all()

    c7 = _naive(now - timedelta(days=7))
    curr_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) >= c7]
    prev_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) < c7]
    curr_dirs = _count_reviewer_disagreement_directions(curr_tasks)
    prev_dirs = _count_reviewer_disagreement_directions(prev_tasks)

    for (cat, task_type), c_bucket in curr_dirs.items():
        p_bucket = prev_dirs.get((cat, task_type), {"miss": 0, "tp": 0, "fp": 0})
        c_denom = c_bucket["miss"] + c_bucket["tp"] + c_bucket["fp"]
        p_denom = p_bucket["miss"] + p_bucket["tp"] + p_bucket["fp"]
        if c_denom < _MIN_SR_SAMPLE or p_denom < _MIN_SR_SAMPLE:
            continue
        c_rate = c_bucket["miss"] / c_denom
        p_rate = p_bucket["miss"] / p_denom
        if c_rate >= _TREND_DELTA_THRESHOLD and p_rate >= _TREND_DELTA_THRESHOLD:
            return True

    themes = await aggregate_sr_blind_spot_themes(session, project_id, days=14, limit=10)
    for theme_entry in themes:
        if theme_entry["count"] >= _SR_BLIND_SPOT_THEME_FLOOR:
            return True
    return False


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
    from app.playbook_learning import _MIN_SR_SAMPLE, _TREND_DELTA_THRESHOLD  # noqa: PLC0415
    from app.sr_blind_spots import (  # noqa: PLC0415
        _count_reviewer_disagreement_directions,
        aggregate_sr_blind_spot_themes,
    )
    from app.models import Task as TaskModel  # noqa: PLC0415

    if spawn_budget_left <= 0:
        return 0

    spawned = 0
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # --- Disagreement-rate signal ---
    tasks = (await session.execute(
        select(TaskModel).where(
            TaskModel.project_id == project_id,
            TaskModel.status == TaskStatus.COMPLETED,
            TaskModel.created_at >= now - timedelta(days=14),
        )
    )).scalars().all()

    c7 = _naive(now - timedelta(days=7))
    curr_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) >= c7]
    prev_tasks = [t for t in tasks if t.created_at and _naive(t.created_at) < c7]
    curr_dirs = _count_reviewer_disagreement_directions(curr_tasks)
    prev_dirs = _count_reviewer_disagreement_directions(prev_tasks)

    # Dedupe by category: one spawn per category regardless of task_type.
    # Iterate sorted pairs; for each category, try each (cat, task_type)
    # until one trips (or none do for that category).
    from collections import defaultdict  # noqa: PLC0415
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
                for finding in (sro.get("reviewer_findings") or []):
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
            spawn = await _maybe_spawn(
                session, project_id,
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
    themes = await aggregate_sr_blind_spot_themes(session, project_id, days=14, limit=100)
    for theme_entry in themes:
        if spawned >= spawn_budget_left:
            break
        count = theme_entry["count"]
        if count < _SR_BLIND_SPOT_THEME_FLOOR:
            break  # sorted descending — subsequent entries also below floor
        theme = theme_entry["theme"]
        regression_class = f"sr_blind_spot:theme:{theme}"
        spawn = await _maybe_spawn(
            session, project_id,
            signal_name="sr_blind_spot",
            regression_class=regression_class,
            prior_rate=None,
            current_rate=None,
            sample_size=count,
            magnitude=float(count),
            extras=[
                f"Recurring SR blind-spot theme: '{theme}' appeared {count}x in the last 14 days.",
            ],
            title_hint=f"SR blind-spot theme: {theme}"[:80],
        )
        if spawn is not None:
            spawned += 1

    return spawned


async def run_recovery_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Re-evaluate merged regression cases and close or re-arm them."""
    from app.models import Project  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        return 0

    now = _utcnow()
    rows = (await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.status == RegressionCaseStatus.MERGED,
        )
    )).scalars().all()
    changed = 0
    for case in rows:
        snapshot = case.signal_snapshot_at_merge or {}
        merged_at_raw = snapshot.get("captured_at") or case.updated_at.isoformat()
        try:
            merged_at = datetime.fromisoformat(str(merged_at_raw).replace("Z", "+00:00"))
        except ValueError:
            merged_at = case.updated_at
        if merged_at.tzinfo is not None:
            merged_at = merged_at.replace(tzinfo=None)
        if now < merged_at + _REGRESSION_RECOVERY_COOLDOWN:
            continue
        state = await _current_signal_value_for_class(
            session, project_id, case.regression_class, project.design_doc,
        )
        if state is None:
            continue
        if not state["tripped"]:
            case.mark_closed()
            case.fingerprint_to_fix_log = list(case.fingerprint_to_fix_log or []) + [{
                "fingerprint": case.regression_class,
                "recovered_at": now.isoformat(),
                "post_merge_value": state["current_rate"],
            }]
        else:
            case.re_arm()
        session.add(case)
        changed += 1
    if changed:
        await session.commit()
    return changed


async def _evaluate_anomaly_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
    spawn_budget_left: int,
) -> int:
    """Trip on persistent anomalies that have exceeded the occurrence threshold."""
    import app.shared_state as _ss  # noqa: PLC0415

    if spawn_budget_left <= 0:
        return 0

    proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
    if not proj_anomalies:
        from app.models import Project as _Proj  # noqa: PLC0415
        from app.shared_state import _ANOMALY_FINGERPRINTS_KEY  # noqa: PLC0415
        _proj = await session.get(_Proj, project_id)
        if _proj is not None:
            _stored = ((_proj.design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY)) or {}
            if _stored:
                _ss.persistent_anomalies[str(project_id)] = _stored
                proj_anomalies = _stored
    if not proj_anomalies:
        return 0

    now = _utcnow()
    recency_cutoff = (now - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)).isoformat()
    spawned = 0
    for anomaly_key, entry in list(proj_anomalies.items()):
        if spawned >= spawn_budget_left:
            break
        if not isinstance(entry, dict):
            continue
        occurrence_count = int(entry.get("occurrence_count") or 0)
        if occurrence_count < _ANOMALY_DISPATCH_MIN_OCCURRENCES:
            continue
        last_seen = str(entry.get("last_seen") or "")
        if last_seen < recency_cutoff:
            continue
        regression_class = f"anomaly:{anomaly_key}"
        sample_msg = entry.get("example_message") or entry.get("latest_details") or ""
        spawn = await _maybe_spawn(
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
        )
        if spawn is not None:
            spawned += 1
    return spawned


async def run_watchdog_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Escalate stale ACTIVE/MERGED regression cases to NEEDS_MANUAL_INTERVENTION.

    Catches two silent failure modes:
    - ACTIVE cases not updated in 24h: investigation finished with low/no
      confidence and no remediation task was spawned.
    - MERGED cases not updated in 6h: recovery scan could not evaluate them
      (e.g. signal_snapshot_at_merge missing or signal unresolvable).

    Returns the number of cases escalated.
    """
    now = _utcnow()
    rows = (await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.status.in_([
                RegressionCaseStatus.ACTIVE,
                RegressionCaseStatus.MERGED,
            ]),
        )
    )).scalars().all()

    escalated = 0
    for case in rows:
        updated = case.updated_at
        if updated.tzinfo is not None:
            updated = updated.replace(tzinfo=None)

        if case.status == RegressionCaseStatus.ACTIVE:
            threshold = _WATCHDOG_ACTIVE_STALE_THRESHOLD
            reason = "ACTIVE case stale — investigation may have finished with low confidence and no remediation was spawned"
        else:  # MERGED
            threshold = _WATCHDOG_MERGED_STALE_THRESHOLD
            reason = "MERGED case stale — recovery scan could not evaluate it (signal_snapshot_at_merge may be empty)"

        if now < updated + threshold:
            continue

        logger.warning(
            "[REGRESSION_WATCHDOG] stale-%s project=%s class=%s updated_at=%s "
            "threshold_hours=%s — escalating to NEEDS_MANUAL_INTERVENTION",
            case.status, project_id, case.regression_class,
            updated.isoformat(), threshold.total_seconds() / 3600,
        )
        case.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
        case.needs_manual_intervention_at = now
        session.add(case)
        session.add(Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=f"Regression case stalled: {case.regression_class}"[:200],
            message=(
                f"{reason}. Case last updated {updated.isoformat()}; "
                f"staleness threshold {threshold}. Manual review required."
            )[:1000],
        ))
        escalated += 1

    if escalated:
        await session.commit()
    return escalated


async def _any_signal_would_trip(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict | None,
) -> bool:
    """Side-effect-free probe: would any subscribed signal trip right now?

    Used to gate the deploy-monitoring defer alert so we don't claim a signal
    tripped when none actually would. Mirrors the classes scanned in
    ``run_once``: SR catch-rate, fix-loop divergence, pipeline success-rate,
    persistent anomalies.
    """
    sr_state = await _current_signal_value_for_class(
        session, project_id, "sr_catch_rate", project_design_doc,
    )
    if sr_state is not None and sr_state["tripped"]:
        return True
    fl_state = await _current_signal_value_for_class(
        session, project_id, "fix_loop_divergence", project_design_doc,
    )
    if fl_state is not None and fl_state["tripped"]:
        return True
    ps_state = await _current_signal_value_for_class(
        session, project_id, "pipeline_success_rate", project_design_doc,
    )
    if ps_state is not None and ps_state["tripped"]:
        return True
    import app.shared_state as _ss  # noqa: PLC0415
    proj_anomalies = _ss.persistent_anomalies.get(str(project_id)) or {}
    recency_cutoff = (
        _utcnow() - timedelta(hours=_ANOMALY_DISPATCH_RECENCY_HOURS)
    ).isoformat()
    for entry in proj_anomalies.values():
        if not isinstance(entry, dict):
            continue
        if int(entry.get("occurrence_count") or 0) < _ANOMALY_DISPATCH_MIN_OCCURRENCES:
            continue
        if str(entry.get("last_seen") or "") < recency_cutoff:
            continue
        return True
    if await _probe_sr_blind_spot_any_trip(session, project_id):
        return True
    return False


async def run_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Evaluate all subscribed pipeline-regression signals for ``project_id``.

    Returns the number of investigate tasks spawned this call. Caller is
    responsible for committing the session — this matches the
    ``_spawn_proactive_fingerprint_investigations`` pattern in
    ``anomaly_detection`` and lets the orchestrator coalesce dispatcher work
    with its existing 60s tick.
    """
    from app.models import Project  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        return 0

    # Deploy-monitoring guard: if a fresh deploy is in its observation window,
    # defer spawns to avoid chasing deploy-induced signal noise.  PAUSED/HALTED
    # are operator-controlled states the dispatcher already skips via other
    # coordination; only MONITORING needs a time-bounded defer here. Use the
    # canonical helpers in deploy_health so the defer rule has one home.
    deploy_state = await session.get(DeploymentState, 1)
    defer_until = get_deploy_monitoring_defer_until(deploy_state)
    if defer_until is not None and _utcnow() < defer_until:
        # Only emit the operator-visible Alert when a signal actually would
        # have tripped this cycle — otherwise the run_once tick (every 60s)
        # would spam ~30 misleading "signal tripped" alerts across the 30-min
        # window. record_deploy_monitoring_deferral additionally dedupes by
        # title so a single alert covers the whole monitoring window.
        if await _any_signal_would_trip(session, project_id, project.design_doc):
            logger.info(
                "[REGRESSION_DISPATCHER] deploy-monitoring-defer project=%s "
                "defer_until=%s — skipping spawn",
                project_id, defer_until.isoformat(),
            )
            await record_deploy_monitoring_deferral(
                session, project_id, deploy_state, "pipeline_regression_dispatcher",
            )
            await session.commit()
        return 0

    # Cap 1: research slot budget shared with autonomous research.
    max_total = int(_runtime_config.get("max_total_research_per_project", 5) or 5)
    active_research = await _count_active_research_for_project(session, project_id)
    project_remaining = max_total - active_research
    if project_remaining <= 0:
        logger.info(
            "[REGRESSION_DISPATCHER] cap-hit project=%s active_research=%d/%d "
            "(max_total_research_per_project) — skipping",
            project_id, active_research, max_total,
        )
        return 0

    # Cap 2: per-cycle system-wide concurrency budget.
    spawn_budget = min(project_remaining, _REGRESSION_DISPATCHER_SYSTEM_CAP)

    spawned = 0
    spawned += await _evaluate_sr_catch_rate_signal(
        session, project_id, project.design_doc, spawn_budget - spawned,
    )
    spawned += await _evaluate_fix_loop_divergence_signal(
        session, project_id, spawn_budget - spawned,
    )
    spawned += await _evaluate_pipeline_success_signal(
        session, project_id, spawn_budget - spawned,
    )
    spawned += await _evaluate_anomaly_signal(
        session, project_id, spawn_budget - spawned,
    )
    spawned += await _evaluate_sr_blind_spot_signal(
        session, project_id, spawn_budget - spawned,
    )

    if spawned:
        await session.commit()
    return spawned
