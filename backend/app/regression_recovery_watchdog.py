"""Recovery and watchdog service for pipeline regression remediation cases."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import app.shared_state as _shared_state
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Alert,
    AlertSeverity,
    CLOSE_REASON_RECOVERED_MARGINAL,
    CLOSE_REASON_RECOVERED_UNATTENDED,
    ESCALATION_REASON_STALE_ACTIVE,
    ESCALATION_REASON_STALE_MERGED,
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.pipeline_health import (
    _evaluate_lone_checker_alert,
    _evaluate_sr_catch_rate_targets,
)
from app.regression_dispatcher_metrics import _compute_recovery_layer_mismatch
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.time_utils import _to_naive_utc, _utcnow

logger = logging.getLogger(__name__)


def _dispatcher():
    from app import pipeline_regression_dispatcher as dispatcher

    return dispatcher


async def _run_signal_staleness_watchdog(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_design_doc: dict[str, Any] | None,
) -> int:
    """Emit warning alerts when a known signal source has stopped updating."""
    dispatcher = _dispatcher()
    doc = project_design_doc or {}
    now = _utcnow()
    windows = dispatcher._SIGNAL_STALENESS_WINDOWS
    specs: list[tuple[str, Any, timedelta]] = [
        (
            "sr_catch_rate",
            (doc.get("sr_catch_rate_trends") or {}).get("last_sufficient_computed_at"),
            windows["sr_catch_rate"],
        ),
        (
            "fix_loop_divergence",
            doc.get("fix_loop_divergence_signal_last_computed_at")
            or doc.get("fix_loop_divergence_detected_at"),
            windows["fix_loop_divergence"],
        ),
        (
            "persistent_anomaly",
            (
                (_shared_state.persistent_anomalies.get(str(project_id)) or {}).get(
                    "signal_last_computed_at"
                )
            ),
            windows["persistent_anomaly"],
        ),
        (
            "deploy_health",
            _shared_state.deploy_monitoring.get("signal_last_computed_at"),
            windows["deploy_health"],
        ),
        (
            "sr_blind_spot",
            doc.get("sr_blind_spot_signal_last_computed_at"),
            windows["sr_blind_spot"],
        ),
    ]
    created = 0
    for signal_name, raw_ts, threshold in specs:
        computed_at = dispatcher._parse_iso_timestamp(raw_ts)
        if computed_at is None or (now - computed_at) <= threshold:
            continue
        age = now - computed_at
        title = f"Signal staleness: {signal_name}"
        message = (
            f"{signal_name} last computed {round(age.total_seconds() / 3600, 1)}h "
            f"ago; freshness window is {round(threshold.total_seconds() / 3600, 1)}h."
        )
        existing = (
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
            .first()
        )
        if existing is None:
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=title,
                    message=message,
                )
            )
            created += 1
    sr_trends = doc.get("sr_catch_rate_trends") or {}
    signal_ts = sr_trends.get("signal_last_computed_at")
    sufficient_ts = sr_trends.get("last_sufficient_computed_at")
    if signal_ts and sufficient_ts:
        signal_at = dispatcher._parse_iso_timestamp(signal_ts)
        sufficient_at = dispatcher._parse_iso_timestamp(sufficient_ts)
        if (
            signal_at is not None
            and sufficient_at is not None
            and (now - signal_at)
            <= dispatcher._SR_CATCH_RATE_FROZEN_SIGNAL_FRESH_WINDOW
            and (now - sufficient_at)
            > dispatcher._SR_CATCH_RATE_SUFFICIENCY_STALE_WINDOW
        ):
            title = "Signal frozen: sr_catch_rate"
            age = now - sufficient_at
            message = (
                "sr_catch_rate signal is computing (fresh heartbeat) but real data "
                f"has not refreshed in {round(age.total_seconds() / 3600, 1)}h. "
                "The trend is frozen."
            )
            existing = (
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
                .first()
            )
            if existing is None:
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=title,
                        message=message,
                    )
                )
                created += 1
    if signal_ts and not sufficient_ts:
        title = "Signal staleness: sr_catch_rate"
        message = (
            "sr_catch_rate has been computed but has never reached data sufficiency. "
            "The metric is running but has not produced a real, well-sampled measurement."
        )
        existing = (
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
            .first()
        )
        if existing is None:
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=title,
                    message=message,
                )
            )
            created += 1
    return created


async def run_recovery_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Re-evaluate merged regression cases and close or re-arm them."""
    from app.models import Project

    dispatcher = _dispatcher()
    project = await session.get(Project, project_id)
    if project is None:
        return 0

    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.MERGED,
                )
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for case in rows:
        snapshot = case.signal_snapshot_at_merge or {}
        merged_at_raw = snapshot.get("captured_at") or case.updated_at.isoformat()
        try:
            merged_at = datetime.fromisoformat(
                str(merged_at_raw).replace("Z", "+00:00")
            )
        except ValueError:
            merged_at = case.updated_at
        if merged_at.tzinfo is not None:
            merged_at = merged_at.replace(tzinfo=None)
        if now < merged_at + dispatcher._REGRESSION_RECOVERY_COOLDOWN:
            continue
        try:
            state = await dispatcher._current_signal_value_for_class(
                session,
                project_id,
                case.regression_class,
                project.design_doc,
            )
            if state is None:
                continue
            if not state["tripped"]:
                fix_pr_num = case.last_remediation_pr_number
                if fix_pr_num is not None:
                    try:
                        from app.github import (
                            get_pull_request as _rr_get_pr,
                            get_repo_installation_token as _rr_get_token,
                            is_ancestor_of as _rr_is_ancestor,
                        )

                        if project.github_owner and project.github_repo:
                            rr_token = await _rr_get_token(
                                project.github_owner, project.github_repo
                            )
                            if rr_token:
                                rr_pr_data = await _rr_get_pr(
                                    rr_token,
                                    project.github_owner,
                                    project.github_repo,
                                    fix_pr_num,
                                    project_id=str(project_id),
                                )
                                rr_merge_sha = (rr_pr_data or {}).get(
                                    "merge_commit_sha"
                                )
                                if rr_merge_sha:
                                    rr_branch = _get_project_integration_branch(project)
                                    rr_is_anc = await _rr_is_ancestor(
                                        rr_token,
                                        project.github_owner,
                                        project.github_repo,
                                        rr_merge_sha,
                                        rr_branch,
                                        project_id=str(project_id),
                                    )
                                    if rr_is_anc is not True:
                                        logger.info(
                                            "[REGRESSION_DISPATCHER] Skipping close for "
                                            "project=%s class=%s: fix PR #%s merge commit "
                                            "%s not confirmed ancestor of %s "
                                            "(result=%s - indeterminate treated as not landed)",
                                            project_id,
                                            case.regression_class,
                                            fix_pr_num,
                                            rr_merge_sha,
                                            rr_branch,
                                            rr_is_anc,
                                        )
                                        continue
                    except Exception as rr_exc:  # noqa: BLE001
                        logger.warning(
                            "[REGRESSION_DISPATCHER] Ancestry check skipped for "
                            "project=%s class=%s PR #%s: %s - proceeding with close",
                            project_id,
                            case.regression_class,
                            fix_pr_num,
                            rr_exc,
                        )
                mismatch_entry = _compute_recovery_layer_mismatch(
                    case.fingerprint_to_fix_log, case.symptoms_by_layer, now
                )
                case.mark_closed()
                case.signal_healthy_since_at = None
                new_entries: list[dict[str, Any]] = [
                    {
                        "fingerprint": case.regression_class,
                        "recovered_at": now.isoformat(),
                        "post_merge_value": state["current_rate"],
                    }
                ]
                if mismatch_entry:
                    new_entries.append(mismatch_entry)
                case.fingerprint_to_fix_log = (
                    list(case.fingerprint_to_fix_log or []) + new_entries
                )
            else:
                last_spawn = await dispatcher._last_spawn_for_class(
                    session, project_id, case.regression_class
                )
                if last_spawn is not None and last_spawn.created_at:
                    last_at = last_spawn.created_at
                    if last_at.tzinfo is not None:
                        last_at = last_at.replace(tzinfo=None)
                    if now < last_at + dispatcher._REGRESSION_COOLDOWN:
                        logger.info(
                            "[REGRESSION_DISPATCHER] recovery-rearm-cooldown-deferred "
                            "project=%s class=%s last_spawn=%s cooldown_until=%s",
                            project_id,
                            case.regression_class,
                            last_at.isoformat(),
                            (last_at + dispatcher._REGRESSION_COOLDOWN).isoformat(),
                        )
                        continue
                case.re_arm(fast_track=False)
                case.signal_healthy_since_at = None
            session.add(case)
            changed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[REGRESSION_DISPATCHER] recovery evaluation raised for "
                "project=%s class=%s - skipping this case: %s",
                project_id,
                case.regression_class,
                exc,
            )
            continue
    if changed:
        await session.commit()

    try:
        await dispatcher._backfill_layers_targeted_once(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] layers_targeted backfill raised for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await dispatcher._evaluate_path_classifier_coverage(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] path-classifier coverage evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await dispatcher._evaluate_awaiting_backfill_alert(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] awaiting_backfill alert evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await _evaluate_lone_checker_alert(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] lone-checker alert evaluation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await _evaluate_sr_catch_rate_targets(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] sr_catch_rate_targets alert evaluation "
            "raised for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await _run_compliance_followup_once(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] compliance-followup raised for project=%s: %s",
            project_id,
            exc,
        )

    try:
        await dispatcher._escalate_stale_observation_alerts(session, project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] observation-alert SLA escalation raised "
            "for project=%s: %s",
            project_id,
            exc,
        )

    return changed


async def _persist_recovery_cycle_record(
    session: AsyncSession,
    project_id: uuid.UUID,
    cycle_started_at: datetime,
    marginal_closed: int,
    confirmed_closed: int,
) -> None:
    """Persist a recovery cycle record with marginal/confirmed recovery counts."""
    from app.models import DispatcherCycleRecord

    try:
        session.add(
            DispatcherCycleRecord(
                project_id=project_id,
                cycle_started_at=cycle_started_at,
                cycle_finished_at=_utcnow(),
                signals_evaluated=0,
                signals_tripped=0,
                spawns_emitted=0,
                rejections_by_reason={},
                marginal_recoveries_count=marginal_closed,
                confirmed_recoveries_count=confirmed_closed,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_WATCHDOG] recovery-cycle-record write failed",
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()


async def _check_marginal_recovery_rate_threshold(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Check if marginal recovery rate exceeds threshold for two consecutive cycles."""
    dispatcher = _dispatcher()
    threshold = float(
        _runtime_config.get("marginal_recovery_rate_threshold", 0.30) or 0.30
    )
    records = await dispatcher.get_dispatcher_cycle_records(
        session,
        project_id=str(project_id),
        limit=2,
    )
    if len(records) < 2:
        return

    rates = []
    for rec in records[-2:]:
        marginal = rec.get("marginal_recoveries_count", 0)
        confirmed = rec.get("confirmed_recoveries_count", 0)
        total = marginal + confirmed
        rates.append(marginal / total if total > 0 else 0.0)

    if len(rates) == 2 and all(rate > threshold for rate in rates):
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title="High marginal recovery rate threshold exceeded"[:200],
                message=(
                    f"Marginal recovery rate exceeded {threshold:.0%} "
                    f"for {len(rates)} consecutive cycles. "
                    f"Rates: {[f'{rate:.1%}' for rate in rates]}. "
                    f"May indicate fixes that do not fully address root causes."
                )[:1000],
            )
        )
        await session.commit()


async def run_watchdog_once(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Escalate stale cases and auto-close recovered manual-intervention cases."""
    dispatcher = _dispatcher()
    cycle_started_at = _utcnow()
    recovery_counts = {"marginal": 0, "confirmed": 0}
    token = dispatcher._recovery_counts_var.set(recovery_counts)
    mutated = 0
    marginal_closed = 0
    confirmed_closed = 0
    try:
        now = _utcnow()
        rows = (
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project_id,
                        RegressionRemediationCase.status.in_(
                            [
                                RegressionCaseStatus.ACTIVE,
                                RegressionCaseStatus.MERGED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        for case in rows:
            try:
                updated = case.updated_at
                if updated.tzinfo is not None:
                    updated = updated.replace(tzinfo=None)

                if case.status == RegressionCaseStatus.ACTIVE:
                    threshold = dispatcher._WATCHDOG_ACTIVE_STALE_THRESHOLD
                    reason = (
                        "ACTIVE case stale - investigation may have finished with "
                        "low confidence and no remediation was spawned"
                    )
                    escalation_reason = ESCALATION_REASON_STALE_ACTIVE
                else:
                    threshold = dispatcher._WATCHDOG_MERGED_STALE_THRESHOLD
                    reason = (
                        "MERGED case stale - recovery scan did not auto-close it "
                        "within the staleness window. The underlying signal is most "
                        "likely still tripped while the case is in re-arm cooldown, "
                        "or a transient evaluation error skipped the case on recent "
                        "recovery ticks."
                    )
                    escalation_reason = ESCALATION_REASON_STALE_MERGED

                if now < updated + threshold:
                    continue

                if (
                    case.status == RegressionCaseStatus.ACTIVE
                    and (case.auto_escalation_count or 0) == 0
                ):
                    child_alive = await dispatcher._has_active_remediation_child(
                        session, case
                    )
                    if child_alive:
                        meta_task = (
                            await dispatcher._spawn_auto_escalation_meta_investigate(
                                session, project_id, case
                            )
                        )
                        case.auto_escalation_count = (
                            case.auto_escalation_count or 0
                        ) + 1
                        case.last_remediation_task_id = meta_task.id
                        case.updated_at = now
                        session.add(case)
                        logger.info(
                            "[REGRESSION_WATCHDOG] auto-escalate project=%s class=%s "
                            "meta_task=%s (one-shot, count=1)",
                            project_id,
                            case.regression_class,
                            meta_task.id,
                        )
                        mutated += 1
                        continue

                logger.warning(
                    "[REGRESSION_WATCHDOG] stale-%s project=%s class=%s updated_at=%s "
                    "threshold_hours=%s - escalating to NEEDS_MANUAL_INTERVENTION",
                    case.status,
                    project_id,
                    case.regression_class,
                    updated.isoformat(),
                    threshold.total_seconds() / 3600,
                )
                case.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
                case.needs_manual_intervention_at = now
                case.escalation_reason = escalation_reason
                session.add(case)
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=f"Regression case stalled: {case.regression_class}"[:200],
                        message=(
                            f"{reason}. Case last updated {updated.isoformat()}; "
                            f"staleness threshold {threshold}. Manual review required."
                        )[:1000],
                    )
                )
                mutated += 1
            except Exception as case_exc:  # noqa: BLE001
                logger.warning(
                    "[REGRESSION_WATCHDOG] error processing case project=%s class=%s: %s",
                    project_id,
                    getattr(case, "regression_class", "<unknown>"),
                    case_exc,
                )
                continue

        if mutated:
            await session.commit()

        (
            marginal_closed,
            confirmed_closed,
        ) = await _auto_close_recovered_manual_intervention(session, project_id)
        recovery_counts["marginal"] = marginal_closed
        recovery_counts["confirmed"] = confirmed_closed
        return mutated + marginal_closed + confirmed_closed
    finally:
        dispatcher._recovery_counts_var.reset(token)
        if marginal_closed or confirmed_closed:
            await _persist_recovery_cycle_record(
                session, project_id, cycle_started_at, marginal_closed, confirmed_closed
            )
            await _check_marginal_recovery_rate_threshold(session, project_id)


async def _auto_close_recovered_manual_intervention(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[int, int]:
    """Auto-close manual-intervention cases whose signal has recovered."""
    from app.models import Project

    dispatcher = _dispatcher()
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        return 0, 0
    design_doc = project.design_doc

    candidate_rows = (
        await session.execute(
            select(
                RegressionRemediationCase.id,
                RegressionRemediationCase.regression_class,
            ).where(
                RegressionRemediationCase.project_id == project_id,
                RegressionRemediationCase.status
                == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            )
        )
    ).all()
    if not candidate_rows:
        return 0, 0

    window_hours = int(
        _runtime_config.get("regression_recovered_unattended_window_hours", 24) or 24
    )
    window = timedelta(hours=window_hours)
    now = _utcnow()
    marginal_closed = 0
    confirmed_closed = 0
    mutated_rows = 0
    for case_id, _candidate_class in candidate_rows:
        locked = (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.id == case_id,
                    RegressionRemediationCase.status
                    == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if locked is None:
            continue

        state = await dispatcher._current_signal_value_for_class(
            session, project_id, locked.regression_class, design_doc
        )
        if state is None:
            continue
        if state.get("tripped"):
            if locked.signal_healthy_since_at is not None:
                locked.signal_healthy_since_at = None
                session.add(locked)
                mutated_rows += 1
            continue

        if locked.signal_healthy_since_at is None:
            locked.signal_healthy_since_at = now
            session.add(locked)
            mutated_rows += 1
            first_healthy = now
        else:
            first_healthy = locked.signal_healthy_since_at
        if now - first_healthy < window:
            continue

        post_value = state.get("current_rate")
        pre_value = (locked.signal_snapshot_at_merge or {}).get("current_rate")
        close_reason = CLOSE_REASON_RECOVERED_UNATTENDED
        alert_severity = AlertSeverity.INFO
        marginal = False
        if isinstance(pre_value, (int, float)) and isinstance(post_value, (int, float)):
            higher_is_better = dispatcher._signal_higher_is_better(
                locked.regression_class
            )
            improvement = (
                post_value - pre_value if higher_is_better else pre_value - post_value
            )
            if higher_is_better:
                reference = max(1.0 - pre_value, 0.0)
            else:
                reference = pre_value if pre_value > 0 else 0.0
            rel_floor = float(
                _runtime_config.get(
                    "regression_recovered_marginal_relative_floor", 0.10
                )
                or 0.10
            )
            abs_floor = float(
                _runtime_config.get(
                    "regression_recovered_marginal_absolute_floor", 0.05
                )
                or 0.05
            )
            if improvement <= 0:
                marginal = True
            else:
                rel_improvement = (
                    improvement / reference if reference > 0 else float("inf")
                )
                if rel_improvement < rel_floor and improvement < abs_floor:
                    marginal = True
            if marginal:
                close_reason = CLOSE_REASON_RECOVERED_MARGINAL
                alert_severity = AlertSeverity.WARNING
        elif pre_value is not None or post_value is not None:
            logger.info(
                "[REGRESSION_WATCHDOG] snapshot-missing project=%s class=%s "
                "pre_value=%r post_value=%r - falling back to %s without "
                "magnitude check",
                project_id,
                locked.regression_class,
                pre_value,
                post_value,
                CLOSE_REASON_RECOVERED_UNATTENDED,
            )

        watchdog_mismatch = _compute_recovery_layer_mismatch(
            locked.fingerprint_to_fix_log, locked.symptoms_by_layer, now
        )
        locked.mark_closed()
        locked.last_remediation_result = close_reason
        watchdog_entries: list[dict[str, Any]] = [
            {
                "fingerprint": locked.regression_class,
                "recovered_at": now.isoformat(),
                "post_recovery_value": post_value,
                "reason": close_reason,
            }
        ]
        if watchdog_mismatch:
            watchdog_entries.append(watchdog_mismatch)
        locked.fingerprint_to_fix_log = (
            list(locked.fingerprint_to_fix_log or []) + watchdog_entries
        )
        locked.signal_healthy_since_at = None
        session.add(locked)
        if marginal:
            alert_title = f"Regression marginal recovery: {locked.regression_class}"[
                :200
            ]
            alert_message = (
                f"NEEDS_MANUAL_INTERVENTION case for '{locked.regression_class}' "
                f"auto-closed with marginal improvement: pre-fix={pre_value}, "
                f"post-fix={post_value}. Signal likely to re-trip."
            )[:1000]
        else:
            alert_title = f"Regression auto-recovered: {locked.regression_class}"[:200]
            alert_message = (
                f"NEEDS_MANUAL_INTERVENTION case for '{locked.regression_class}' "
                f"auto-closed: underlying signal stayed below threshold for "
                f"{window_hours}h without operator action."
            )[:1000]
        session.add(
            Alert(
                project_id=project_id,
                severity=alert_severity,
                title=alert_title,
                message=alert_message,
            )
        )
        if marginal and bool(
            _runtime_config.get("regression_marginal_followup_enabled", True)
        ):
            followup_cooldown_hours = int(
                _runtime_config.get("regression_marginal_followup_cooldown_hours", 72)
                or 72
            )
            last_followup_at = locked.marginal_followup_spawned_at
            if last_followup_at is not None and last_followup_at.tzinfo is not None:
                last_followup_at = last_followup_at.replace(tzinfo=None)
            if last_followup_at is None or now - last_followup_at >= timedelta(
                hours=followup_cooldown_hours
            ):
                seen_layers: list[str] = []
                for entry in locked.fingerprint_to_fix_log or []:
                    if isinstance(entry, dict):
                        for layer in entry.get("spawn_layers_enumerated") or []:
                            if layer not in seen_layers:
                                seen_layers.append(layer)
                layer_block = (
                    f"\n\nLayer history: {', '.join(seen_layers)}"
                    if seen_layers
                    else ""
                )
                followup_prompt = (
                    f"Prior fix for regression class '{locked.regression_class}' "
                    f"was closed as MARGINAL recovery: pre-fix signal={pre_value}, "
                    f"post-fix signal={post_value}. The improvement was insufficient "
                    f"(< 10% relative AND < 5% absolute).{layer_block}\n\n"
                    "Diagnose why the prior fix was insufficient - examine the "
                    "fix-log entries and propose either (a) a stronger remediation "
                    "in the same layer, or (b) a defense in a different layer."
                )
                followup_payload = validate_internal_payload(
                    {
                        "type": "investigate",
                        "prompt": followup_prompt,
                        "base_branch": _get_project_integration_branch(project),
                        "effort": "low",
                    },
                    "pipeline_regression_dispatcher:marginal_followup",
                )
                followup_task = Task(
                    project_id=project_id,
                    title=(f"Marginal-recovery follow-up: {locked.regression_class}")[
                        :200
                    ],
                    description=(
                        "Auto-spawned: prior fix closed as marginal recovery - "
                        "investigate why the prior remediation was insufficient."
                    )[:500],
                    payloads=[followup_payload],
                    context={
                        "spawned_by": "pipeline_regression_dispatcher_marginal_followup",
                        "regression_class": locked.regression_class,
                        "signal_name": "marginal_followup",
                        "pre_fix_value": pre_value,
                        "post_fix_value": post_value,
                        "remediation_case_id": str(locked.id),
                    },
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
                session.add(followup_task)
                await session.flush()
                session.add(
                    TaskHistory(
                        task_id=followup_task.id,
                        action="SPAWNED",
                        details={
                            "spawned_by": "PIPELINE_REGRESSION_DISPATCHER_MARGINAL_FOLLOWUP",
                            "regression_class": locked.regression_class,
                            "signal_name": "marginal_followup",
                        },
                    )
                )
                locked.marginal_followup_spawned_at = now
                locked.fingerprint_to_fix_log = [
                    *(locked.fingerprint_to_fix_log or []),
                    {
                        "type": "marginal_followup_spawned",
                        "spawned_at": now.isoformat(),
                        "task_id": str(followup_task.id),
                        "pre_value": pre_value,
                        "post_value": post_value,
                    },
                ]
                session.add(locked)
                logger.info(
                    "[REGRESSION_WATCHDOG] marginal-followup-spawned project=%s "
                    "class=%s task_id=%s pre_value=%s post_value=%s",
                    project_id,
                    locked.regression_class,
                    followup_task.id,
                    pre_value,
                    post_value,
                )
        logger.info(
            "[REGRESSION_WATCHDOG] auto-recovered project=%s class=%s "
            "window_hours=%d pre_value=%s post_value=%s reason=%s",
            project_id,
            locked.regression_class,
            window_hours,
            pre_value,
            post_value,
            close_reason,
        )
        if marginal:
            marginal_closed += 1
        else:
            confirmed_closed += 1

    if marginal_closed or confirmed_closed or mutated_rows:
        await session.commit()
    return marginal_closed, confirmed_closed


async def _run_compliance_followup_once(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Spawn compliance follow-up tasks for non-compliant cross-layer fixes."""
    from app.models import Project as ComplianceProject
    from app.regression_layer_classifier import ALL_LAYERS

    if not bool(_runtime_config.get("regression_compliance_followup_enabled", True)):
        return 0

    compliance_project = await session.get(ComplianceProject, project_id)
    cooldown_hours = int(
        _runtime_config.get("regression_compliance_followup_cooldown_hours", 72) or 72
    )
    now = _utcnow()
    all_layers = set(ALL_LAYERS)

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status.in_(
                        [RegressionCaseStatus.MERGED, RegressionCaseStatus.CLOSED]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    max_spawns = int(
        _runtime_config.get("regression_compliance_followup_max_spawns", 3) or 3
    )

    spawned = 0
    for case in rows:
        followup_count = getattr(case, "compliance_followup_count", 0) or 0
        if followup_count >= max_spawns:
            continue

        last_at = getattr(case, "compliance_followup_spawned_at", None)
        if last_at is not None:
            if last_at.tzinfo is not None:
                last_at = last_at.replace(tzinfo=None)
            if now - last_at < timedelta(hours=cooldown_hours):
                continue

        non_compliant_entry = None
        for entry in reversed(case.fingerprint_to_fix_log or []):
            if not isinstance(entry, dict):
                continue
            if not entry.get("spawn_was_cross_layer"):
                continue
            if "layers_targeted" not in entry:
                continue
            targeted = set(entry.get("layers_targeted") or []) & all_layers
            recommended_unmod = (
                set(entry.get("recommended_unmodifiable_layers") or []) & all_layers
            )
            if len(targeted | recommended_unmod) < 2:
                non_compliant_entry = entry
            break

        if non_compliant_entry is None:
            continue

        targeted = set(non_compliant_entry.get("layers_targeted") or []) & all_layers
        spawn_layers = list(non_compliant_entry.get("spawn_layers_enumerated") or [])
        missing = [layer for layer in spawn_layers if layer not in targeted]

        prompt = (
            f"Cross-layer investigation for '{case.regression_class}' required "
            f"defenses in >=2 distinct canonical layers, but PR #"
            f"{non_compliant_entry.get('fix_pr_number', 'unknown')} only targeted: "
            f"{sorted(targeted) or ['(none)']}. "
            f"Layers seen in symptoms but not yet defended: {missing or ['(unknown)']}. "
            "Propose a defense in a distinct canonical layer not yet addressed.\n\n"
            "Note: .github/workflows/ cannot be modified by the agent. A CI_WORKFLOW "
            "recommendation belongs in the diagnosis report (set "
            "'recommended_unmodifiable_layers': ['CI_WORKFLOW'] in context), not the diff."
        )
        payload = validate_internal_payload(
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": _get_project_integration_branch(compliance_project),
                "effort": "low",
            },
            "pipeline_regression_dispatcher:compliance_followup",
        )
        task = Task(
            project_id=project_id,
            title=f"Compliance follow-up: {case.regression_class}"[:200],
            description=(
                "Auto-spawned: cross-layer fix targeted <2 canonical layers - "
                "compliance follow-up required."
            )[:500],
            payloads=[payload],
            context={
                "spawned_by": "pipeline_regression_dispatcher_compliance_followup",
                "regression_class": case.regression_class,
                "signal_name": "compliance_followup",
                "targeted_layers": sorted(targeted),
                "missing_layers": missing,
                "remediation_case_id": str(case.id),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(task)
        await session.flush()
        session.add(
            TaskHistory(
                task_id=task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "PIPELINE_REGRESSION_DISPATCHER_COMPLIANCE_FOLLOWUP",
                    "regression_class": case.regression_class,
                    "signal_name": "compliance_followup",
                },
            )
        )
        case.compliance_followup_spawned_at = now
        case.compliance_followup_count = followup_count + 1
        case.fingerprint_to_fix_log = [
            *(case.fingerprint_to_fix_log or []),
            {
                "type": "compliance_followup_spawned",
                "spawned_at": now.isoformat(),
                "task_id": str(task.id),
                "targeted_layers": sorted(targeted),
                "missing_layers": missing,
            },
        ]
        session.add(case)
        spawned += 1
        logger.info(
            "[REGRESSION_WATCHDOG] compliance-followup-spawned project=%s "
            "class=%s task_id=%s targeted=%s missing=%s",
            project_id,
            case.regression_class,
            task.id,
            sorted(targeted),
            missing,
        )

    if spawned:
        await session.commit()
    return spawned


async def _ensure_watchdog_alert_open(
    session: AsyncSession,
    project_id: uuid.UUID,
    title_prefix: str,
    title: str,
    message: str,
    severity: AlertSeverity,
) -> None:
    """Open a watchdog alert if none is already open."""
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title.like(title_prefix + "%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        session.add(
            Alert(
                project_id=project_id,
                severity=severity,
                title=title[:200],
                message=message[:1000],
            )
        )
        await session.commit()


async def _resolve_watchdog_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    title_prefix: str,
) -> None:
    """Auto-resolve an open watchdog alert by setting dismissed_at."""
    open_alert = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title.like(title_prefix + "%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if open_alert is not None:
        open_alert.dismissed_at = _utcnow()
        await session.commit()


async def _check_cross_layer_backfill_lag(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> None:
    """Emit an info alert when cross-layer fix-log entries sit unbackfilled too long."""
    dispatcher = _dispatcher()
    threshold_hours = int(
        _runtime_config.get("cross_layer_backfill_alert_threshold_hours", 24) or 24
    )
    dedupe_hours = int(
        _runtime_config.get("cross_layer_backfill_alert_dedupe_hours", 24) or 24
    )
    disabled_projects = list(
        _runtime_config.get("cross_layer_backfill_alert_disabled_projects", []) or []
    )

    now = _utcnow()
    threshold_dt = now - timedelta(hours=threshold_hours)

    for pid in project_ids:
        if str(pid) in disabled_projects:
            continue

        cases = (
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )

        stale_count = 0
        oldest_merged_at: datetime | None = None

        for case in cases:
            for entry in case.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("spawn_was_cross_layer"):
                    continue
                if "layers_targeted" in entry:
                    continue
                merged_at_raw = entry.get("merged_at")
                if not merged_at_raw:
                    continue
                try:
                    merged_dt = _to_naive_utc(
                        datetime.fromisoformat(
                            str(merged_at_raw).replace("Z", "+00:00")
                        )
                    )
                except (ValueError, TypeError):
                    continue
                if merged_dt >= threshold_dt:
                    continue
                stale_count += 1
                if oldest_merged_at is None or merged_dt < oldest_merged_at:
                    oldest_merged_at = merged_dt

        if stale_count > 0:
            dedupe_cutoff = now - timedelta(hours=dedupe_hours)
            open_alert = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == pid,
                            Alert.title.like(
                                dispatcher._CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX
                                + "%"
                            ),
                            Alert.dismissed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if open_alert is not None:
                if open_alert.created_at >= dedupe_cutoff:
                    continue
                open_alert.dismissed_at = now
                await session.commit()

            oldest_str = (
                oldest_merged_at.date().isoformat() if oldest_merged_at else "unknown"
            )
            await _ensure_watchdog_alert_open(
                session,
                pid,
                dispatcher._CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX,
                f"{dispatcher._CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX} {stale_count} entries unbackfilled",
                (
                    f"{stale_count} cross-layer fix-log entries unbackfilled for "
                    f">{threshold_hours}h, oldest merged_at={oldest_str}; "
                    "compliance_rate may be biased"
                ),
                AlertSeverity.INFO,
            )
        else:
            await _resolve_watchdog_alert(
                session, pid, dispatcher._CROSS_LAYER_BACKFILL_ALERT_TITLE_PREFIX
            )


async def run_dispatcher_watchdog_alarms(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> None:
    """Check cycle-staleness and zero-spawn-streak alarms for all projects."""
    dispatcher = _dispatcher()
    await _check_cross_layer_backfill_lag(session, project_ids)

    now_mono = time.monotonic()

    for pid in project_ids:
        pid_str = str(pid)
        last_mono = dispatcher._last_cycle_monotonic.get(pid_str)
        if last_mono is not None:
            elapsed = now_mono - last_mono
            if elapsed > dispatcher._DISPATCHER_STALENESS_THRESHOLD_SECONDS:
                await _ensure_watchdog_alert_open(
                    session,
                    pid,
                    dispatcher._STALENESS_ALERT_TITLE_PREFIX,
                    f"{dispatcher._STALENESS_ALERT_TITLE_PREFIX} {int(elapsed)}s since last cycle",
                    f"Dispatcher has not completed a cycle for project {pid} in "
                    f"{int(elapsed)}s (threshold {dispatcher._DISPATCHER_STALENESS_THRESHOLD_SECONDS}s). "
                    "The dispatcher session may be wedged or the orchestrator tick stalled.",
                    AlertSeverity.ERROR,
                )
            else:
                await _resolve_watchdog_alert(
                    session, pid, dispatcher._STALENESS_ALERT_TITLE_PREFIX
                )

        recent = await dispatcher.get_dispatcher_cycle_records(
            session,
            project_id=pid_str,
            limit=dispatcher._DISPATCHER_ZERO_SPAWN_STREAK_K,
        )
        if len(recent) >= dispatcher._DISPATCHER_ZERO_SPAWN_STREAK_K:
            streak_broken = any(
                row["spawns_emitted"] > 0 or row.get("signals_tripped", 0) == 0
                for row in recent
            )
            if not streak_broken:
                await _ensure_watchdog_alert_open(
                    session,
                    pid,
                    dispatcher._ZERO_SPAWN_ALERT_TITLE_PREFIX,
                    f"{dispatcher._ZERO_SPAWN_ALERT_TITLE_PREFIX} {dispatcher._DISPATCHER_ZERO_SPAWN_STREAK_K} consecutive cycles suppressed",
                    f"The last {dispatcher._DISPATCHER_ZERO_SPAWN_STREAK_K} dispatcher cycles for project {pid} "
                    "all had tripped signals but zero spawns (cooldown/cap/dedup/manual-intervention). "
                    "Review active remediation cases or adjust spawn-suppression config.",
                    AlertSeverity.WARNING,
                )
            else:
                await _resolve_watchdog_alert(
                    session, pid, dispatcher._ZERO_SPAWN_ALERT_TITLE_PREFIX
                )
