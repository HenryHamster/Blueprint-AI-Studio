"""Remediation funnel + recurring-failure-pattern digest blocks.

Extracted from ``app.digest_metrics`` so future remediation/anomaly work
doesn't require editing the shared metrics module or the router directly.
``app.digest_metrics`` re-exports this symbol so existing import paths are
unchanged.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    ResidualRemediationCase,
    ResidualRemediationCaseStatus,
    Task,
    TaskStatus,
    _utcnow,
)


async def compute_remediation_and_patterns(
    *,
    session: AsyncSession,
    project_id: Optional[uuid.UUID],
    window_start: datetime,
    failed_tasks: List[Task],
    all_tasks: List[Task],
    active_tasks_all: List[Task],
    dir_id_to_text: Dict[str, str],
) -> Dict[str, Any]:
    """Build the remediation funnel + recurring failure patterns digest blocks.

    Extracts the inline analytic logic from ``_compute_digest`` so future
    remediation/anomaly work doesn't require editing the router.

    Returns a dict with keys: ``remediation_funnel``, ``investigation_conversion``,
    ``recurring_patterns``, ``early_warnings``.
    """
    import re as _re  # noqa: PLC0415
    from app.routers.admin_analytics import _derive_completed_at  # noqa: PLC0415
    from app.shared_state import persistent_anomalies as _persistent_anomalies  # noqa: PLC0415
    from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415
    from app.digest_metrics import compute_remediation_funnel  # noqa: PLC0415

    # --- Residual remediation funnel (sourced from ResidualRemediationCase rows) ---
    _rr_spawned_filters = [ResidualRemediationCase.last_spawned_at >= window_start]
    if project_id:
        _rr_spawned_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_spawned_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_spawned_filters).limit(500)
    )
    _rr_cases = list(_rr_spawned_result.scalars().all())
    _rr_active_filters = [
        ResidualRemediationCase.status == ResidualRemediationCaseStatus.REMEDIATING,
    ]
    if project_id:
        _rr_active_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_active_result = await session.execute(
        select(ResidualRemediationCase).where(*_rr_active_filters)
    )
    _rr_open_filters = [
        ResidualRemediationCase.status.in_(
            [
                ResidualRemediationCaseStatus.ACTIVE,
                ResidualRemediationCaseStatus.REMEDIATING,
                ResidualRemediationCaseStatus.SUSPENDED,
            ]
        ),
    ]
    if project_id:
        _rr_open_filters.append(ResidualRemediationCase.project_id == project_id)
    _rr_open_result = await session.execute(
        select(ResidualRemediationCase)
        .where(*_rr_open_filters)
        .order_by(ResidualRemediationCase.last_seen_at.desc())
        .limit(500)
    )
    _rr_open_cases = list(_rr_open_result.scalars().all())
    residual_remediation_funnel: Dict[str, Any] = {
        "spawned": len(_rr_cases),
        "merged": sum(
            1 for c in _rr_cases if c.status == ResidualRemediationCaseStatus.MERGED
        ),
        "failed": sum(
            1
            for c in _rr_cases
            if c.status
            in (
                ResidualRemediationCaseStatus.FAILED,
                ResidualRemediationCaseStatus.SUSPENDED,
            )
        ),
        "remediating": len(list(_rr_active_result.scalars().all())),
        "open_cases_by_error_class": [
            {
                "error_class": "CODE_ERROR",
                "count": len(_rr_open_cases),
                "most_recent_at": _rr_open_cases[0].last_seen_at.isoformat()
                if _rr_open_cases
                else None,
                "cases": [
                    {
                        "fingerprint": c.fingerprint,
                        "status": c.status,
                        "hit_count": c.hit_count,
                        "last_seen_at": c.last_seen_at.isoformat()
                        if c.last_seen_at
                        else None,
                        "last_spawned_at": c.last_spawned_at.isoformat()
                        if c.last_spawned_at
                        else None,
                        "sample_message": c.sample_message,
                        "consecutive_failure_count": c.consecutive_failure_count,
                    }
                    for c in _rr_open_cases[:30]
                ],
            }
        ]
        if _rr_open_cases
        else [],
    }

    remediation_funnel, investigation_conversion = await compute_remediation_funnel(
        session,
        project_id=project_id,
        window_start=window_start,
        residual_remediation_funnel=residual_remediation_funnel,
    )

    # --- Recurring failure patterns (from persistent_anomalies) ---
    _fp_48h_cutoff = _utcnow() - timedelta(hours=48)
    _fp_dirs_48h: Dict[str, set] = {}
    _fp_count_48h: Dict[str, int] = {}
    for _t in failed_tasks:
        if _derive_completed_at(_t) < _fp_48h_cutoff:
            continue
        _msg = _t.latest_status_message or ""
        if not _msg:
            continue
        _fp_ = _error_fingerprint(_msg)
        _did = (_t.context or {}).get("direction_id")
        if _did:
            _fp_dirs_48h.setdefault(_fp_, set()).add(str(_did))
        _fp_count_48h[_fp_] = _fp_count_48h.get(_fp_, 0) + 1

    _fp_task_states: Dict[str, Dict[str, Any]] = {}
    _inflight_statuses = {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
    for _t in list(all_tasks) + list(active_tasks_all):
        _ctx = _t.context or {}
        # Audit W1: previous_error_fingerprint is a column.
        _fp_ = (
            _ctx.get("diagnosis_fingerprint")
            or _ctx.get("error_fingerprint")
            or _t.previous_error_fingerprint
        )
        if not _fp_:
            continue
        _entry = _fp_task_states.setdefault(
            str(_fp_), {"inflight": False, "merged": False, "proactive": False}
        )
        if _t.status == TaskStatus.COMPLETED and _ctx.get("pr_url"):
            _entry["merged"] = True
        if _t.status in _inflight_statuses:
            _entry["inflight"] = True
        if _ctx.get("proactive") or _ctx.get("proactive_investigation"):
            _entry["proactive"] = True

    recurring_patterns: List[Dict[str, Any]] = []
    early_warnings: List[Dict[str, Any]] = []
    _anoms_map = (
        {str(project_id): _persistent_anomalies.get(str(project_id), {})}
        if project_id
        else _persistent_anomalies
    )

    _momentum_rate_re = _re.compile(r"(\d+)%")
    _recent_24h_cutoff = _utcnow() - timedelta(hours=24)
    _dirs_last_24h = {
        str((_t.context or {}).get("direction_id"))
        for _t in failed_tasks
        if (_t.context or {}).get("direction_id")
        and _derive_completed_at(_t) >= _recent_24h_cutoff
    }
    for _proj_anoms in _anoms_map.values():
        for _key, _info in _proj_anoms.items():
            if not isinstance(_info, dict):
                continue
            if _info.get("occurrence_count", 0) < 2:
                continue
            if _key.startswith("FailureRateMomentum"):
                _pcts = _momentum_rate_re.findall(_info.get("latest_details") or "")[:3]
                early_warnings.append(
                    {
                        "failure_class": _key,
                        "rate_progression": [int(p) / 100 for p in _pcts]
                        if len(_pcts) == 3
                        else [],
                        "occurrence_count": _info.get("occurrence_count", 0),
                        "first_seen": _info.get("first_seen"),
                        "last_seen": _info.get("last_seen"),
                        "severity": _info.get("severity"),
                        "details": _info.get("latest_details") or "",
                        "affected_direction_count": len(_dirs_last_24h),
                        "suggested_action": (
                            "Investigate the upward trend before it trips the surge "
                            "threshold — check recent deploys, infra health, and "
                            "direction-level failure patterns."
                        ),
                    }
                )
                continue
            _fp_key = (
                _key[len("CrossDirectionFingerprint_") :]
                if _key.startswith("CrossDirectionFingerprint_")
                else _key
            )
            _state = _fp_task_states.get(_fp_key, {})
            _recent = _fp_count_48h.get(_fp_key, 0)
            if _state.get("merged") and _recent == 0:
                _status = "fix_verified"
            elif _state.get("merged"):
                _status = "fix_shipped"
            elif _state.get("inflight"):
                _status = "investigating"
            else:
                _status = "unaddressed"
            _affected_dir_ids = sorted(_fp_dirs_48h.get(_fp_key, set()))
            recurring_patterns.append(
                {
                    "fingerprint": _fp_key,
                    "occurrence_count": _info.get("occurrence_count", 0),
                    "first_seen": _info.get("first_seen"),
                    "last_seen": _info.get("last_seen"),
                    "severity": _info.get("severity"),
                    "example_message": (_info.get("latest_details") or "")[:240],
                    "affected_direction_count": len(_affected_dir_ids),
                    "affected_directions": [
                        {
                            "direction_id": _did,
                            "direction_text": dir_id_to_text.get(_did, ""),
                        }
                        for _did in _affected_dir_ids
                    ],
                    "remediation_status": _status,
                    "proactive": bool(_state.get("proactive", False)),
                }
            )
    recurring_patterns.sort(key=lambda x: x["occurrence_count"], reverse=True)

    return {
        "remediation_funnel": remediation_funnel,
        "investigation_conversion": investigation_conversion,
        "recurring_patterns": recurring_patterns,
        "early_warnings": early_warnings,
    }
