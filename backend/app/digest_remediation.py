"""Remediation funnel + recurring-failure-pattern digest blocks.

Extracted from ``app.digest_metrics`` so future remediation/anomaly work
doesn't require editing the shared metrics module or the router directly.
``app.digest_metrics`` re-exports these symbols so existing import paths are
unchanged.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import resolve_dialect_name

from app.models import (
    Project,
    ResidualRemediationCase,
    ResidualRemediationCaseStatus,
    Task,
    TaskStatus,
    _utcnow,
)
from app.landed_signal import (
    is_merged_completion,
    is_verified_merge,
    landed_signal_predicate,
)
from app.time_utils import _to_naive_utc

logger = logging.getLogger(__name__)


# Structural-diagnosis keyword heuristics. Re-homed from the deleted
# ``task_completion_investigation`` (the investigation-conversion funnel below is
# the only surviving consumer). A diagnosis is "structural" when it points at a
# direction/goal-level scope problem rather than a code-local defect.
_STRUCTURAL_STRONG_KEYWORDS = (
    "fundamentally misaligned",
    "direction scope",
    "direction is too broad",
    "direction is too wide",
    "narrow the direction",
    "split the direction",
    "rewrite the direction",
)

# Weaker keywords — scope-related language that only signals a structural
# diagnosis when paired with a direction/goal-level context word. Without the
# context gate, phrases like "validation scope too narrow" or "out of scope for
# this module" get misclassified as structural.
_STRUCTURAL_WEAK_KEYWORDS = (
    "too broad",
    "too wide",
    "overly broad",
    "scope too",
    "misaligned",
    "out of scope",
    "narrow the scope",
)

_STRUCTURAL_CONTEXT_WORDS = ("direction", "goal", "vision")


def _is_structural_diagnosis(root_cause: str, recommended_fix: str) -> bool:
    """True when a diagnosis points at a direction/goal-level scope problem."""
    blob = f"{root_cause}\n{recommended_fix}".lower()
    if any(kw in blob for kw in _STRUCTURAL_STRONG_KEYWORDS):
        return True
    if not any(w in blob for w in _STRUCTURAL_CONTEXT_WORDS):
        return False
    return any(kw in blob for kw in _STRUCTURAL_WEAK_KEYWORDS)


async def compute_remediation_and_patterns(
    *,
    session: AsyncSession,
    project_id: Optional[uuid.UUID],
    window_start: datetime,
    failed_tasks: List[Task],
    all_tasks: List[Task],
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
    # Query inflight tasks without a row cap so a remediation task queued after
    # the display-list limit is not invisible to the status ladder.  No column
    # filter on previous_error_fingerprint — context-only fingerprints
    # (diagnosis_fingerprint / error_fingerprint) are resolved in the loop below.
    _inflight_fp_filters: list = [
        Task.status.in_(list(_inflight_statuses)),
    ]
    if project_id:
        _inflight_fp_filters.append(Task.project_id == project_id)
    _inflight_fp_result = await session.execute(
        select(Task)
        .where(*_inflight_fp_filters)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.previous_error_fingerprint,
                Task.context,
            raiseload=True,)
        )
    )
    _inflight_fp_tasks = list(_inflight_fp_result.scalars().all())
    for _t in list(all_tasks) + _inflight_fp_tasks:
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
            str(_fp_),
            {
                "inflight": False,
                "landed": False,
                "verified": False,
                "proactive": False,
            },
        )
        # Use the canonical landed/verified predicates the rest of the
        # remediation funnel relies on — NOT a loose "completed + pr_url"
        # proxy. ``landed`` means the PR actually landed on integration
        # (``is_merged_completion``); ``verified`` additionally requires the
        # system's verification standard (``is_verified_merge``: a merged
        # completion with no fall-open / degraded unverified-merge reason).
        if is_merged_completion(_t):
            _entry["landed"] = True
        if is_verified_merge(_t):
            _entry["verified"] = True
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
                        "affected_direction_count": len(_info.get("affected_directions", [])),
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
            # Status ladder grounded in real signals: ``fix_verified`` needs the
            # stronger verification standard AND no recurrence in the last 48h;
            # ``fix_shipped`` needs only a real landed remediation; otherwise
            # in-flight work reads as ``investigating``, else ``unaddressed``.
            if _state.get("verified") and _recent == 0:
                _status = "fix_verified"
            elif _state.get("landed"):
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


async def compute_remediation_funnel(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    window_start: "datetime",
    residual_remediation_funnel: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the investigation -> diagnosis -> remediation -> merge funnel.

    Runs its own self-contained Task/Project queries off ``session`` (scoped to
    ``window_start`` and the optional ``project_id``) and assembles the
    ``remediation_funnel`` digest block, including the adjacent CI auto-remediation
    funnel. The residual-remediation funnel is a separate responsibility computed by
    the caller and passed in via ``residual_remediation_funnel`` so it lands in the
    returned funnel dict at its original position.

    Returns a ``(remediation_funnel, investigation_conversion)`` tuple — the latter
    is a separate top-level digest key, spliced in by the caller unchanged.
    """
    from app.failure_classification import classify_failure as _classify_failure
    from app.routers.admin_analytics import _derive_completed_at

    # Query investigate + remediation tasks in-window regardless of status (the
    # main all_tasks query is terminal-only, so investigate tasks that are still
    # IN_PROGRESS would otherwise be missed).
    _funnel_filters = [Task.created_at >= window_start]
    if project_id:
        _funnel_filters.append(Task.project_id == project_id)
    _inv_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.plan_output,
                Task.context,
                Task.created_at,
                Task.completed_at,
                Task.status_log,
                raiseload=True,
            )
        )
        .where(
            *_funnel_filters,
            Task.payloads[0]["type"].as_string() == "investigate",
        )
        .limit(2000)
    )
    _inv_tasks = _inv_result.scalars().all()
    # R4-W2 (M3): ``has_key`` uses the JSONB ``?`` operator so the
    # ``ix_task_context_gin`` GIN index can satisfy the predicate. ``?`` is
    # Postgres-only, so branch on the resolved dialect and fall back to the
    # portable ``json_extract`` IS NOT NULL form on SQLite.
    if resolve_dialect_name(session) == "postgresql":
        _diagnosis_present = Task.context.has_key("diagnosis_task_id")
    else:
        _diagnosis_present = func.json_extract(
            Task.context, "$.diagnosis_task_id"
        ).isnot(None)
    _rem_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.started_at,
                Task.completed_at,
                Task.last_heartbeat,
                Task.latest_status_message,
                Task.status_log,
                Task.payloads,
                Task.api_call_summary,
                raiseload=True,
            )
        )
        .where(
            *_funnel_filters,
            _diagnosis_present,
        )
        .limit(10000)
    )
    _rem_tasks = _rem_result.scalars().all()

    investigations_spawned = len(_inv_tasks)
    investigations_completed = sum(
        1 for t in _inv_tasks if t.status == TaskStatus.COMPLETED
    )
    investigations_with_diagnosis = sum(
        1
        for t in _inv_tasks
        if t.status == TaskStatus.COMPLETED
        and isinstance(t.plan_output, dict)
        and str((t.plan_output or {}).get("summary") or "").strip()
    )
    active_investigations = sum(
        1
        for t in _inv_tasks
        if t.status
        in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        )
    )
    remediations_spawned = len(_rem_tasks)
    # "merged" must mean the PR actually landed on the integration branch, not
    # merely that the task reached a terminal COMPLETED status (a remediation can
    # close completed without merging). Use the canonical landed-signal predicate
    # — the same one the CI auto-remediation tally below uses — so "merged" means
    # merged everywhere in the funnel, and the success rate (merged / spawned)
    # the admin UI derives reflects real merges.
    remediations_merged = sum(1 for t in _rem_tasks if is_merged_completion(t))
    remediations_failed = sum(1 for t in _rem_tasks if t.status == TaskStatus.FAILED)

    _held_projects_q = select(Project).options(
        load_only(Project.id, Project.directions, raiseload=True)
    )
    if project_id:
        _held_projects_q = _held_projects_q.where(Project.id == project_id)
    else:
        _held_projects_q = _held_projects_q.where(Project.directions.isnot(None))
    _held_projects = (
        (await session.execute(_held_projects_q.limit(10000))).scalars().all()
    )

    fixes_held = 0
    _window_iso = window_start.isoformat()
    for _p in _held_projects:
        for _d in _p.directions or []:
            for _h in _d.get("remediation_history") or []:
                if (_h.get("result") or "") != "held":
                    continue
                _ts = _h.get("held_at") or _h.get("timestamp") or ""
                if _ts and _ts >= _window_iso:
                    fixes_held += 1

    _inv_by_id = {str(t.id): t for t in _inv_tasks}
    _ttr_values: List[float] = []
    for r in _rem_tasks:
        if r.status != TaskStatus.COMPLETED:
            continue
        diag_id = (r.context or {}).get("diagnosis_task_id")
        inv = _inv_by_id.get(str(diag_id)) if diag_id else None
        if inv is None:
            continue
        completed_at = _derive_completed_at(r)
        if completed_at and inv.created_at:
            inv_created = (
                inv.created_at.replace(tzinfo=None)
                if inv.created_at.tzinfo is not None
                else inv.created_at
            )
            delta = (completed_at - inv_created).total_seconds() / 3600.0
            if delta >= 0:
                _ttr_values.append(delta)
    avg_time_to_remediation_hours = (
        round(sum(_ttr_values) / len(_ttr_values), 2) if _ttr_values else None
    )

    investigations_by_confidence: Dict[str, int] = {}
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED or not isinstance(t.plan_output, dict):
            continue
        conf = (
            str((t.plan_output or {}).get("confidence") or "unknown").strip().lower()
            or "unknown"
        )
        investigations_by_confidence[conf] = (
            investigations_by_confidence.get(conf, 0) + 1
        )

    failed_remediations_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        if r.status != TaskStatus.FAILED:
            continue
        cls = (
            _classify_failure(r, at_time=r.last_heartbeat or r.started_at) or "unknown"
        )
        failed_remediations_by_class[cls] = failed_remediations_by_class.get(cls, 0) + 1

    investigations_spawned_by_class: Dict[str, int] = {}
    investigations_completed_by_class: Dict[str, int] = {}
    for t in _inv_tasks:
        cls = (
            str((t.context or {}).get("last_failure_class") or "unknown").strip()
            or "unknown"
        )
        investigations_spawned_by_class[cls] = (
            investigations_spawned_by_class.get(cls, 0) + 1
        )
        if t.status == TaskStatus.COMPLETED:
            investigations_completed_by_class[cls] = (
                investigations_completed_by_class.get(cls, 0) + 1
            )

    remediations_spawned_by_class: Dict[str, int] = {}
    for r in _rem_tasks:
        cls = (
            str((r.context or {}).get("trigger_failure_class") or "unknown").strip()
            or "unknown"
        )
        remediations_spawned_by_class[cls] = (
            remediations_spawned_by_class.get(cls, 0) + 1
        )

    merged_remediations_by_trigger_class: Dict[str, int] = {}
    for r in _rem_tasks:
        # "merged" here means the same thing as the headline remediations_merged
        # count and the CI auto-remediation tally: the PR actually landed, not
        # merely that the task reached COMPLETED.
        if not is_merged_completion(r):
            continue
        trigger_cls = (r.context or {}).get("trigger_failure_class")
        if not trigger_cls:
            diag_id = (r.context or {}).get("diagnosis_task_id")
            inv = _inv_by_id.get(str(diag_id)) if diag_id else None
            if inv is not None:
                trigger_cls = (inv.context or {}).get("last_failure_class")
        key = str(trigger_cls).strip() if trigger_cls else "unknown"
        if not key:
            key = "unknown"
        merged_remediations_by_trigger_class[key] = (
            merged_remediations_by_trigger_class.get(key, 0) + 1
        )

    remediation_success_by_confidence: Dict[str, Dict[str, int]] = {}
    for r in _rem_tasks:
        if r.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            continue
        diag_id = (r.context or {}).get("diagnosis_task_id")
        inv = _inv_by_id.get(str(diag_id)) if diag_id else None
        conf = "unknown"
        if inv is not None and isinstance(inv.plan_output, dict):
            conf = (
                str((inv.plan_output or {}).get("confidence") or "unknown")
                .strip()
                .lower()
                or "unknown"
            )
        bucket = remediation_success_by_confidence.setdefault(
            conf, {"merged": 0, "failed": 0}
        )
        # Consistent with the rest of the funnel: only a landed PR counts as
        # "merged". A completed-but-not-landed remediation is neither a merge nor
        # a hard failure, so it inflates neither arm of the success ratio.
        if is_merged_completion(r):
            bucket["merged"] += 1
        elif r.status == TaskStatus.FAILED:
            bucket["failed"] += 1

    _completed_inv = [
        t
        for t in _inv_tasks
        if t.status == TaskStatus.COMPLETED and isinstance(t.plan_output, dict)
    ]
    _completed_inv.sort(
        key=lambda t: _derive_completed_at(t) or t.created_at or _utcnow(),
        reverse=True,
    )
    recent_investigations: List[Dict[str, Any]] = []
    for t in _completed_inv[:5]:
        po = t.plan_output or {}
        summary = str(po.get("summary") or "").strip()
        if len(summary) > 200:
            summary = summary[:200].rstrip() + "…"
        completed_at = _derive_completed_at(t)
        recent_investigations.append(
            {
                "task_id": str(t.id),
                "direction_id": (t.context or {}).get("direction_id"),
                "summary": summary,
                "confidence": str(po.get("confidence") or "").strip().lower() or None,
                "severity": str(po.get("severity") or "").strip().lower() or None,
                "completed_at": completed_at.isoformat() if completed_at else None,
            }
        )

    active_investigations_detail: List[Dict[str, Any]] = []
    _now = _utcnow()
    for t in _inv_tasks:
        if t.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ):
            continue
        elapsed_hours = None
        if t.created_at:
            elapsed_hours = round(
                (_now - _to_naive_utc(t.created_at)).total_seconds() / 3600.0, 2
            )
        active_investigations_detail.append(
            {
                "task_id": str(t.id),
                "direction_id": (t.context or {}).get("direction_id"),
                "elapsed_hours": elapsed_hours,
            }
        )

    _ci_rem_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                raiseload=True,
            )
        )
        .where(
            *_funnel_filters,
            Task.auto_remediation.is_(True),  # promoted column (audit W1)
        )
    )
    _ci_rem_tasks = list(_ci_rem_result.scalars().all())
    ci_auto_remediation: Dict[str, Any] = {
        "spawned": len(_ci_rem_tasks),
        "merged": sum(
            1
            for t in _ci_rem_tasks
            if is_merged_completion(t)
        ),
        "failed": sum(1 for t in _ci_rem_tasks if t.status == TaskStatus.FAILED),
        "by_ci_failure_type": {},
    }
    _ci_type_buckets: Dict[str, Dict[str, int]] = {}
    for t in _ci_rem_tasks:
        ci_type = (
            str((t.context or {}).get("ci_failure_type") or "unknown").strip()
            or "unknown"
        )
        bucket = _ci_type_buckets.setdefault(
            ci_type, {"spawned": 0, "merged": 0, "failed": 0}
        )
        bucket["spawned"] += 1
        if is_merged_completion(t):
            bucket["merged"] += 1
        elif t.status == TaskStatus.FAILED:
            bucket["failed"] += 1
    ci_auto_remediation["by_ci_failure_type"] = _ci_type_buckets

    _prior_tasks = [
        t for t in _ci_rem_tasks if (t.context or {}).get("prior_diagnosis_task_id")
    ]
    _total_with_prior = len(_prior_tasks)
    _merged_with_prior = sum(1 for t in _prior_tasks if is_merged_completion(t))
    _failed_with_prior = sum(1 for t in _prior_tasks if t.status == TaskStatus.FAILED)
    _prior_resolved = _merged_with_prior + _failed_with_prior
    prior_investigation_reuse: Dict[str, Any] = {
        "total_with_prior": _total_with_prior,
        "merged_with_prior": _merged_with_prior,
        "failed_with_prior": _failed_with_prior,
        "merge_rate": round(_merged_with_prior / _prior_resolved, 4)
        if _prior_resolved
        else None,
    }

    # R4-W2 (M3): ``has_key`` uses the JSONB ``?`` operator so the
    # ``ix_task_context_gin`` GIN index can satisfy the predicate. ``?`` is
    # Postgres-only, so branch on the resolved dialect and fall back to the
    # portable ``json_extract`` IS NOT NULL form on SQLite.
    if resolve_dialect_name(session) == "postgresql":
        _cum_diagnosis_present = Task.context.has_key("diagnosis_task_id")
    else:
        _cum_diagnosis_present = func.json_extract(
            Task.context, "$.diagnosis_task_id"
        ).isnot(None)
    _cum_filters = [
        Task.status == TaskStatus.COMPLETED,
        or_(
            _cum_diagnosis_present,
            Task.auto_remediation.is_(True),  # promoted column (audit W1)
        ),
        landed_signal_predicate(session),
    ]
    if project_id:
        _cum_filters.append(Task.project_id == project_id)
    _CUM_TASK_CAP = 10000
    _cum_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.context,
                raiseload=True,
            )
        )
        .where(*_cum_filters)
        .limit(_CUM_TASK_CAP)
    )
    cumulative_merged_by_trigger_class: Dict[str, int] = {}
    _cum_rows = _cum_result.scalars().all()
    if len(_cum_rows) >= _CUM_TASK_CAP:
        logger.warning(
            "cumulative_merged_tasks: result capped at %d for project %s — "
            "metrics may be truncated",
            _CUM_TASK_CAP,
            project_id,
        )
    for r in _cum_rows:
        ctx = r.context or {}
        trigger_cls = ctx.get("trigger_failure_class")
        if not trigger_cls:
            diag_id = ctx.get("diagnosis_task_id")
            inv = _inv_by_id.get(str(diag_id)) if diag_id else None
            if inv is not None:
                trigger_cls = (inv.context or {}).get("last_failure_class")
        key = str(trigger_cls).strip() if trigger_cls else "unknown"
        if not key:
            key = "unknown"
        cumulative_merged_by_trigger_class[key] = (
            cumulative_merged_by_trigger_class.get(key, 0) + 1
        )

    _inv_ids_with_remediation = {
        str((r.context or {}).get("diagnosis_task_id"))
        for r in _rem_tasks
        if (r.context or {}).get("diagnosis_task_id")
    }
    _PARSEABLE_KEYS = ("summary", "root_cause", "recommended_fix", "confidence")
    ic_total = 0
    ic_parseable = 0
    ic_unparseable = 0
    ic_structural = 0
    ic_low_confidence = 0
    ic_actionable = 0
    ic_remediation_spawned = 0
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED:
            continue
        ic_total += 1
        po = t.plan_output if isinstance(t.plan_output, dict) else None
        parseable = bool(po) and any(
            str(po.get(k) or "").strip() for k in _PARSEABLE_KEYS
        )
        if parseable:
            ic_parseable += 1
        else:
            ic_unparseable += 1
            continue
        root_cause = str((po or {}).get("root_cause") or "")
        recommended_fix = str((po or {}).get("recommended_fix") or "")
        confidence = str((po or {}).get("confidence") or "").strip().lower()
        structural = _is_structural_diagnosis(root_cause, recommended_fix)
        if structural:
            ic_structural += 1
            continue
        if confidence == "low":
            ic_low_confidence += 1
        elif confidence in ("high", "medium") and recommended_fix.strip():
            ic_actionable += 1
        if str(t.id) in _inv_ids_with_remediation:
            ic_remediation_spawned += 1
    investigation_conversion = {
        "total_completed": ic_total,
        "parseable": ic_parseable,
        "unparseable": ic_unparseable,
        "structural": ic_structural,
        "low_confidence": ic_low_confidence,
        "actionable": ic_actionable,
        "remediation_spawned": ic_remediation_spawned,
    }

    diagnosis_drop_off_by_reason: Dict[str, int] = {}
    for t in _inv_tasks:
        if t.status != TaskStatus.COMPLETED:
            continue
        po = t.plan_output if isinstance(t.plan_output, dict) else None
        if not po:
            continue
        if not str(po.get("summary") or "").strip():
            continue
        if str(t.id) in _inv_ids_with_remediation:
            continue
        root_cause = str(po.get("root_cause") or "")
        recommended_fix = str(po.get("recommended_fix") or "").strip()
        confidence = str(po.get("confidence") or "").strip().lower()
        direction_id_val = (t.context or {}).get("direction_id")
        if _is_structural_diagnosis(root_cause, recommended_fix):
            reason = "structural"
        elif not recommended_fix:
            reason = "no_recommended_fix"
        elif confidence not in ("high", "medium"):
            reason = "low_confidence"
        elif not direction_id_val:
            reason = "no_direction"
        else:
            continue
        diagnosis_drop_off_by_reason[reason] = (
            diagnosis_drop_off_by_reason.get(reason, 0) + 1
        )

    remediation_funnel = {
        "investigations_spawned": investigations_spawned,
        "investigations_completed": investigations_completed,
        "investigations_with_diagnosis": investigations_with_diagnosis,
        "remediations_spawned": remediations_spawned,
        "remediations_merged": remediations_merged,
        "remediations_failed": remediations_failed,
        "fixes_held": fixes_held,
        "avg_time_to_remediation_hours": avg_time_to_remediation_hours,
        "active_investigations": active_investigations,
        "investigations_by_confidence": investigations_by_confidence,
        "failed_remediations_by_class": failed_remediations_by_class,
        "merged_remediations_by_trigger_class": merged_remediations_by_trigger_class,
        "investigations_spawned_by_class": investigations_spawned_by_class,
        "investigations_completed_by_class": investigations_completed_by_class,
        "remediations_spawned_by_class": remediations_spawned_by_class,
        "cumulative_merged_by_trigger_class": cumulative_merged_by_trigger_class,
        "remediation_success_by_confidence": remediation_success_by_confidence,
        "recent_investigations": recent_investigations,
        "active_investigations_detail": active_investigations_detail,
        "ci_auto_remediation": ci_auto_remediation,
        "residual_remediation_funnel": residual_remediation_funnel,
        "prior_investigation_reuse": prior_investigation_reuse,
        "diagnosis_drop_off_by_reason": diagnosis_drop_off_by_reason,
    }
    return remediation_funnel, investigation_conversion
