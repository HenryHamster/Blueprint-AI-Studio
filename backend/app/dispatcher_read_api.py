"""Read-only dispatcher helpers for non-dispatch consumers.

This module owns query/reporting helpers that admin and agent code can call
without depending on the dispatcher loop implementation module.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    DispatcherCycleRecord,
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow

# Give merged signals time to settle before the recovery scan re-evaluates them.
_REGRESSION_RECOVERY_COOLDOWN = timedelta(minutes=15)


async def get_dispatcher_cycle_records(
    session: AsyncSession,
    project_id: str | None = None,
    limit: int = 100,
    window_hours: int | None = None,
) -> list[dict]:
    """Return recent dispatcher cycle records from the durable table."""
    stmt = select(DispatcherCycleRecord)
    if project_id is not None:
        stmt = stmt.where(DispatcherCycleRecord.project_id == uuid.UUID(project_id))
    if window_hours is not None:
        cutoff = _utcnow() - timedelta(hours=window_hours)
        stmt = stmt.where(DispatcherCycleRecord.cycle_started_at >= cutoff)
    stmt = stmt.order_by(DispatcherCycleRecord.cycle_started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    rows = list(reversed(rows))
    return [
        {
            "project_id": str(r.project_id),
            "cycle_started_at": r.cycle_started_at.isoformat(),
            "cycle_finished_at": r.cycle_finished_at.isoformat(),
            "signals_evaluated": r.signals_evaluated or 0,
            "signals_tripped": r.signals_tripped or 0,
            "spawns_emitted": r.spawns_emitted or 0,
            "rejections_by_reason": dict(r.rejections_by_reason or {}),
            "mttd_p50_sec": r.mttd_p50_sec,
            "marginal_recoveries_count": r.marginal_recoveries_count or 0,
            "confirmed_recoveries_count": r.confirmed_recoveries_count or 0,
        }
        for r in rows
    ]


async def get_regression_mtta_data(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    """Return MTTA lifecycle stats per regression class."""
    cutoff = _utcnow() - timedelta(days=window_days)
    where: list[Any] = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(*where).limit(2000)
            )
        )
        .scalars()
        .all()
    )

    now = _utcnow()

    def _pct(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        return s[min(int(len(s) * p), len(s) - 1)]

    def _parse_dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except (ValueError, TypeError):
            return None

    class_cases: dict[str, list] = defaultdict(list)
    for c in cases:
        class_cases[c.regression_class].append(c)

    rows = []
    for rclass, cls_cases in class_cases.items():
        detect_s: list[float] = []
        invest_s: list[float] = []
        itom_s: list[float] = []
        mtor_s: list[float] = []
        active_info: list[dict[str, Any]] = []

        for c in cls_cases:
            if (ttd := c.time_to_detect_sec) is not None and ttd >= 0:
                detect_s.append(ttd)
            if (tti := c.time_to_investigate_sec) is not None and tti >= 0:
                invest_s.append(float(tti))
            ep_investigate_start = c.investigate_spawned_at
            for entry in c.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                merged_dt = _parse_dt(entry.get("merged_at"))
                recovered_dt = _parse_dt(entry.get("recovered_at"))
                if merged_dt and ep_investigate_start:
                    d = (merged_dt - ep_investigate_start).total_seconds()
                    if d >= 0:
                        itom_s.append(d)
                if merged_dt and recovered_dt:
                    d2 = (recovered_dt - merged_dt).total_seconds()
                    if d2 >= 0:
                        mtor_s.append(d2)
                if recovered_dt is not None:
                    ep_investigate_start = recovered_dt

            if c.status == RegressionCaseStatus.CLOSED:
                continue
            anchor = (
                c.signal_first_crossed_at or c.signal_first_seen_at or c.first_seen_at
            )
            live_elapsed = round((now - anchor).total_seconds()) if anchor else None
            live_inv = (
                round((now - ep_investigate_start).total_seconds())
                if ep_investigate_start
                else None
            )
            rec_check_in: int | None = None
            elapsed_since_merge: int | None = None
            if c.status == RegressionCaseStatus.MERGED:
                last_entry = next(
                    (
                        e
                        for e in reversed(c.fingerprint_to_fix_log or [])
                        if isinstance(e, dict) and e.get("merged_at")
                    ),
                    None,
                )
                if last_entry and (m := _parse_dt(last_entry["merged_at"])):
                    secs = int(
                        (m + _REGRESSION_RECOVERY_COOLDOWN - now).total_seconds()
                    )
                    rec_check_in = secs if secs > 0 else None
                    elapsed_since_merge = int((now - m).total_seconds())
            active_info.append(
                {
                    "case_id": str(c.id),
                    "status": c.status,
                    "live_elapsed_sec": live_elapsed,
                    "live_elapsed_investigate_sec": live_inv,
                    "recovery_check_in_sec": rec_check_in,
                    "elapsed_since_merge_sec": elapsed_since_merge,
                }
            )

        p50_detect = _pct(detect_s, 0.5)
        p50_invest = _pct(invest_s, 0.5)
        p50_itom = _pct(itom_s, 0.5)
        p50_mtor = _pct(mtor_s, 0.5)

        for ai in active_info:
            ai["eta_to_investigate_sec"] = (
                int(p50_detect - ai["live_elapsed_sec"])
                if p50_detect is not None and ai["live_elapsed_sec"] is not None
                else None
            )
            ai["eta_to_merge_sec"] = (
                int(p50_itom - ai["live_elapsed_investigate_sec"])
                if p50_itom is not None
                and ai["live_elapsed_investigate_sec"] is not None
                else None
            )
            ai["eta_to_recovery_sec"] = (
                int(p50_mtor - ai["elapsed_since_merge_sec"])
                if p50_mtor is not None and ai["elapsed_since_merge_sec"] is not None
                else None
            )

        rows.append(
            {
                "regression_class": rclass,
                "sample_count": len(cls_cases),
                "stages": {
                    "signal_trip_to_detect": {
                        "p50_sec": p50_detect,
                        "p90_sec": _pct(detect_s, 0.9),
                        "sample_count": len(detect_s),
                    },
                    "detect_to_investigate": {
                        "p50_sec": p50_invest,
                        "p90_sec": _pct(invest_s, 0.9),
                        "sample_count": len(invest_s),
                    },
                    "investigate_to_merge": {
                        "p50_sec": p50_itom,
                        "p90_sec": _pct(itom_s, 0.9),
                        "sample_count": len(itom_s),
                    },
                    "merge_to_recovery": {
                        "p50_sec": p50_mtor,
                        "p90_sec": _pct(mtor_s, 0.9),
                        "sample_count": len(mtor_s),
                    },
                },
                "active_cases": active_info,
            }
        )

    rows.sort(key=lambda r: r["sample_count"], reverse=True)
    return {
        "project_id": str(project_id) if project_id else None,
        "window_days": window_days,
        "class_count": len(rows),
        "classes": rows,
        "recovery_cooldown_sec": int(_REGRESSION_RECOVERY_COOLDOWN.total_seconds()),
    }


async def get_regression_mtta_dashboard_data(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    """Return the admin-dashboard MTTA payload with its historical field shape."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    where: list[Any] = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where)
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    now = datetime.utcnow()

    def _pct(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        return s[min(int(len(s) * p), len(s) - 1)]

    def _parse_dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except (ValueError, TypeError):
            return None

    class_cases: dict[str, list] = defaultdict(list)
    for c in cases:
        class_cases[c.regression_class].append(c)

    rows = []
    for rclass, cls_cases in class_cases.items():
        detect_s: list[float] = []
        investigate_s: list[float] = []
        inv_to_merge_s: list[float] = []
        merge_to_rec_s: list[float] = []
        active_info: list[dict[str, Any]] = []

        for c in cls_cases:
            ttd = c.time_to_detect_sec
            if ttd is not None and ttd >= 0:
                detect_s.append(ttd)
            tti = c.time_to_investigate_sec
            if tti is not None and tti >= 0:
                investigate_s.append(float(tti))
            inv_anchor: datetime | None = c.investigate_spawned_at
            latest_merged_fix: datetime | None = None
            for entry in c.fingerprint_to_fix_log or []:
                if not isinstance(entry, dict):
                    continue
                merged_dt = _parse_dt(entry.get("merged_at"))
                recovered_dt = _parse_dt(entry.get("recovered_at"))
                if merged_dt and inv_anchor:
                    d = (merged_dt - inv_anchor).total_seconds()
                    if d >= 0:
                        inv_to_merge_s.append(d)
                    latest_merged_fix = merged_dt
                if merged_dt and recovered_dt:
                    d2 = (recovered_dt - merged_dt).total_seconds()
                    if d2 >= 0:
                        merge_to_rec_s.append(d2)
                if recovered_dt and latest_merged_fix is not None and not merged_dt:
                    inv_anchor = recovered_dt
                    latest_merged_fix = None

            if c.status != RegressionCaseStatus.CLOSED:
                anchor = (
                    c.signal_first_crossed_at
                    or c.signal_first_seen_at
                    or c.first_seen_at
                )
                live_elapsed = round((now - anchor).total_seconds()) if anchor else None
                live_inv = (
                    round((now - c.investigate_spawned_at).total_seconds())
                    if c.investigate_spawned_at
                    else None
                )
                rec_check_in: int | None = None
                if c.status == RegressionCaseStatus.MERGED:
                    last_entry = next(
                        (
                            e
                            for e in reversed(c.fingerprint_to_fix_log or [])
                            if isinstance(e, dict) and e.get("merged_at")
                        ),
                        None,
                    )
                    if last_entry and (m := _parse_dt(last_entry["merged_at"])):
                        secs = int(
                            (m + _REGRESSION_RECOVERY_COOLDOWN - now).total_seconds()
                        )
                        rec_check_in = secs if secs > 0 else None
                active_info.append(
                    {
                        "case_id": str(c.id),
                        "status": c.status,
                        "live_elapsed_sec": live_elapsed,
                        "live_elapsed_investigate_sec": live_inv,
                        "recovery_check_in_sec": rec_check_in,
                    }
                )

        rows.append(
            {
                "regression_class": rclass,
                "sample_count": len(cls_cases),
                "stages": {
                    "signal_trip_to_detect": {
                        "p50_sec": _pct(detect_s, 0.5),
                        "p90_sec": _pct(detect_s, 0.9),
                        "sample_count": len(detect_s),
                    },
                    "detect_to_investigate": {
                        "p50_sec": _pct(investigate_s, 0.5),
                        "p90_sec": _pct(investigate_s, 0.9),
                        "sample_count": len(investigate_s),
                    },
                    "investigate_to_merge": {
                        "p50_sec": _pct(inv_to_merge_s, 0.5),
                        "p90_sec": _pct(inv_to_merge_s, 0.9),
                        "sample_count": len(inv_to_merge_s),
                    },
                    "merge_to_recovery": {
                        "p50_sec": _pct(merge_to_rec_s, 0.5),
                        "p90_sec": _pct(merge_to_rec_s, 0.9),
                        "sample_count": len(merge_to_rec_s),
                    },
                },
                "active_cases": active_info,
            }
        )

    rows.sort(key=lambda r: r["sample_count"], reverse=True)
    return {
        "project_id": str(project_id) if project_id else None,
        "window_days": window_days,
        "class_count": len(rows),
        "classes": rows,
        "recovery_cooldown_sec": int(_REGRESSION_RECOVERY_COOLDOWN.total_seconds()),
    }


async def get_sr_persistent_blind_spot_hint(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int = 14,
) -> dict[str, Any] | None:
    """Return the persistent SR blind-spot hint for agent task context injection."""
    from app.sr_blind_spots import compute_persistent_sr_blind_spot_hint

    now = _utcnow()
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at >= now - timedelta(days=days),
                )
            )
        )
        .scalars()
        .all()
    )
    result = compute_persistent_sr_blind_spot_hint(tasks, now=now)
    return result if result.get("categories") else None
