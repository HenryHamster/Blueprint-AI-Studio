"""Mode-aware self_audit dispatch helpers.

Decouples the audit-decision and audit-spawn code from autonomous_research
so the larger module stays under its size guardrail. The two entry points
are imported and called by ``autonomous_research._spawn_research_task_inner``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, TaskHistory, TaskStatus
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _record_auto_clear_note(direction: dict, cleared_from: str, count: int) -> None:
    """Append an explanatory note to a direction dict when its mode is auto-cleared.

    Called by task_completion._maybe_auto_clear_direction_mode so the note lives
    alongside the self_audit dispatch logic rather than in the general completion module.
    """
    notes = list(direction.get("notes") or [])
    notes.append(
        {
            "text": (
                f"Auto-cleared from '{cleared_from}': system health recovered "
                f"({count} consecutive 'ship' maturity assessments)"
            ),
            "source": "auto_clear",
            "created_at": _utcnow().isoformat(),
        }
    )
    direction["notes"] = notes


# Default freshness threshold for SystemMaturity rows; rows older than this
# count as stale and qualify the system for an audit dispatch. Configurable
# via runtime_config.self_audit_staleness_days.
_SELF_AUDIT_DEFAULT_STALENESS_DAYS = 7

# Cap on number of focus_systems passed to a single self_audit dispatch so a
# single run doesn't try to audit the world. Subsequent ticks round-robin
# over the remainder via the round_robin_offset bookkeeping below.
_SELF_AUDIT_FOCUS_SYSTEMS_CAP = 3


async def _should_dispatch_self_audit(
    project: Project,
    session: AsyncSession,
) -> dict | None:
    """Decide whether this tick should dispatch a self_audit task.

    Returns a dict shaped like SelfAuditPayload's optional fields when an
    audit is needed: ``{"focus_systems": [...], "max_systems": int}``.
    Returns None when no audit is needed and the regular dispatch flow
    should run.

    Triggers (any one is sufficient):
      1. Any direction's system_id has no SystemMaturity row, OR
         the row's last_audited_at is older than the staleness threshold.
      2. Any direction has high drift this cycle (over_claim_flag or
         stalled_despite_churn_flag set).
      3. The most recent SystemMaturity row for a system_id has
         recommended_mode in {"audit", "fundamentals"}.

    Cool-down: when a self_audit was dispatched within the last cool-down
    window (default 6h), skip another dispatch. This prevents the pipeline
    from looping forever on a broken or paused audit.

    Round-robin: the function reads ``vision._self_audit_round_robin_offset``
    (initialized to 0) and uses it to slice the candidate system list so
    over multiple ticks the pipeline cycles through every stale or
    high-drift system, never leaving any starved.
    """
    # Deferred imports keep this module import-light during cold start.
    from app.drift_detection import compute_direction_drift
    from app.models import SystemMaturity

    vision = project.vision or {}

    # Cool-down: if a self_audit was dispatched within the last cycle and
    # is still pending OR failed in the previous cycle, skip another
    # dispatch this tick. Without this guard the pipeline would loop
    # forever on a broken audit.
    last_dispatched_iso = vision.get("_self_audit_dispatched_at")
    if last_dispatched_iso:
        try:
            last_dispatched = datetime.fromisoformat(
                str(last_dispatched_iso).replace("Z", "+00:00")
            )
            if last_dispatched.tzinfo is not None:
                last_dispatched = last_dispatched.replace(tzinfo=None)
        except (ValueError, TypeError):
            last_dispatched = None
        if last_dispatched is not None:
            cooldown_h = _runtime_config.get(
                "self_audit_dispatch_cooldown_hours",
                6,
            )
            if (_utcnow() - last_dispatched).total_seconds() < cooldown_h * 3600:
                # An audit was recently dispatched. Don't queue another
                # one — yields the slot back to the regular dispatch flow.
                return None

    staleness_days = _runtime_config.get(
        "self_audit_staleness_days",
        _SELF_AUDIT_DEFAULT_STALENESS_DAYS,
    )
    staleness_cutoff = _utcnow() - timedelta(days=staleness_days)

    # Collect all system_ids currently referenced by directions.
    # Build a direction-level mode map so the candidate loop can short-circuit
    # on 'audit'/'fundamentals' and skip 'pause' entirely.
    direction_system_ids: set[str] = set()
    dir_mode_by_sid: dict[str, str] = {}
    high_drift_directions: list[str] = []
    for d in project.directions or []:
        sid = d.get("system_id")
        if not sid:
            continue
        mode = d.get("recommended_mode") or ""
        if mode:
            dir_mode_by_sid[sid] = mode
        if mode == "pause":
            # Strategic agent explicitly paused this direction; skip its system
            # so audit dispatch doesn't compete with the pause intent.
            continue
        direction_system_ids.add(sid)

    # If no directions reference a system_id, there's nothing to audit
    # per-system. Fall through to the existing dispatch flow rather than
    # dispatching a noisy audit-all every tick. Cold-start coverage will
    # come once the strategic agent tags directions with system_ids.
    if not direction_system_ids:
        return None

    # Fetch existing maturity rows for every direction system_id.
    existing = (
        (
            await session.execute(
                select(SystemMaturity).where(
                    SystemMaturity.project_id == project.id,
                    SystemMaturity.system_id.in_(direction_system_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_sid = {row.system_id: row for row in existing}

    candidate_systems: list[str] = []
    # Direction-level 'audit'/'fundamentals' mode forces candidacy regardless of
    # SystemMaturity freshness — the strategic agent has signalled this system
    # needs attention now. Stale/missing rows also qualify. 'ship'/unset falls
    # through to the existing SystemMaturity staleness path (no behaviour change).
    for sid in sorted(direction_system_ids):
        if dir_mode_by_sid.get(sid) in ("audit", "fundamentals"):
            candidate_systems.append(sid)
            continue
        row = by_sid.get(sid)
        if row is None:
            candidate_systems.append(sid)
            continue
        # Round 3 H9: SystemMaturity.last_audited_at routes through
        # NaiveUTCDateTime now, so the ORM-loaded value is already naive UTC.
        last_audited = row.last_audited_at
        if last_audited is None or last_audited < staleness_cutoff:
            candidate_systems.append(sid)
            continue
        if row.recommended_mode in ("audit", "fundamentals"):
            candidate_systems.append(sid)

    # Drift-driven candidates: compute drift signals and add high-drift
    # systems even if their maturity rows look fresh.
    try:
        drift = await compute_direction_drift(session, project)
    except Exception:
        logger.exception(
            "[SELF_AUDIT] drift computation failed; skipping drift-driven dispatch",
        )
        drift = {"by_direction": {}, "by_system": {}}
    drift_by_system: dict[str, dict] = {}
    for did, sig in (drift.get("by_direction") or {}).items():
        if sig.get("over_claim_flag") or sig.get("stalled_despite_churn_flag"):
            high_drift_directions.append(did)
    for sid, sig in (drift.get("by_system") or {}).items():
        if sig.get("over_claim_flag") or sig.get("stalled_despite_churn_flag"):
            drift_by_system[sid] = sig
            if sid not in candidate_systems:
                candidate_systems.append(sid)

    if not candidate_systems and not high_drift_directions:
        return None

    # Round-robin: slice candidate_systems starting from the stored offset
    # so over time every stale system gets covered.
    offset = int(vision.get("_self_audit_round_robin_offset", 0)) or 0
    if candidate_systems:
        offset = offset % len(candidate_systems)
        rotated = candidate_systems[offset:] + candidate_systems[:offset]
    else:
        rotated = []
    focus = rotated[:_SELF_AUDIT_FOCUS_SYSTEMS_CAP]
    focus_drift = {sid: drift_by_system[sid] for sid in focus if sid in drift_by_system}
    return {
        "focus_systems": focus or None,
        "max_systems": _SELF_AUDIT_FOCUS_SYSTEMS_CAP,
        "drift_context": focus_drift or None,
    }


async def _record_residual_audit_deferral(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    system_id: str,
) -> None:
    """Stamp a deferral event on the ResidualRemediationCase for *fingerprint*.

    Increments ``audit_defer_count`` and records ``audit_deferred_system_id`` so
    operators can see via GET /residual-cases why a case is held.
    The caller (``_spawn_residual_fingerprint_remediation``) commits after return.
    """
    from app.models import ResidualRemediationCase

    case = (
        await session.execute(
            select(ResidualRemediationCase).where(
                ResidualRemediationCase.project_id == project_id,
                ResidualRemediationCase.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()
    if case is None:
        return
    case.audit_defer_count = (case.audit_defer_count or 0) + 1
    case.audit_deferred_system_id = system_id
    case.updated_at = _utcnow()
    session.add(case)

    # One-shot escalation: fire exactly when count first reaches the threshold
    # (== not >=) so the alert fires once per deferral streak. The streak
    # resets to 0 in post_residual_fix_spawned_message when a fix is spawned,
    # allowing re-escalation on a future audit cycle.
    from app.notification_dispatch import (  # deferred — avoids circular import
        _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        post_residual_fix_audit_deferred_escalation_message,
    )

    threshold = int(
        _runtime_config.get(
            "residual_audit_defer_escalation_threshold",
            _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        )
    )
    if case.audit_defer_count == threshold:
        await post_residual_fix_audit_deferred_escalation_message(
            session,
            project_id,
            fingerprint=fingerprint,
            defer_count=case.audit_defer_count,
        )


async def _spawn_self_audit_task(
    project: Project,
    session: AsyncSession,
    audit_payload: dict,
) -> bool:
    """Create and queue a self_audit task with focus_systems set.

    Mirrors the lightweight pattern used by ``_spawn_periodic_task`` for
    security_audit. Returns True if a task was created, False otherwise.
    Stamps ``vision._self_audit_dispatched_at`` so the cool-down logic in
    ``_should_dispatch_self_audit`` can short-circuit subsequent ticks.
    """
    # Guard: don't queue a second self_audit when one is already in flight.
    active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    ]
                ),
            )
        )
    ).scalar() or 0
    if active:
        # Inspect first_payload of recent active tasks to check for a
        # self_audit specifically; cheaper than a JSONB query.
        active_tasks = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for t in active_tasks:
            if (t.first_payload or {}).get("type") == "self_audit":
                logger.info(
                    f"[SELF_AUDIT] self_audit already active for project "
                    f"{project.id}; skipping dispatch",
                )
                # Stamp cool-down even though we're not spawning, so the
                # dispatch decision isn't recomputed every tick while the
                # in-flight audit runs.
                vision = dict(project.vision or {})
                vision["_self_audit_dispatched_at"] = _utcnow().isoformat()
                project.vision = vision
                flag_modified(project, "vision")
                session.add(project)
                await session.commit()
                return False

    payload: dict = {
        "type": "self_audit",
        "base_branch": get_project_integration_branch(project),
    }
    focus = audit_payload.get("focus_systems")
    if focus:
        payload["focus_systems"] = focus
    if audit_payload.get("max_systems"):
        payload["max_systems"] = audit_payload["max_systems"]
    try:
        payload = validate_internal_payload(
            payload,
            "autonomous_research:self_audit",
        )
    except PayloadTooLargeError as e:
        logger.warning(
            f"[SELF_AUDIT] payload too large for project {project.id}: {e}; skipping"
        )
        return False
    task_context: dict = {}
    drift_ctx = audit_payload.get("drift_context")
    if drift_ctx:
        task_context["drift_context"] = drift_ctx
    new_task = Task(
        project_id=project.id,
        title=f"Self-audit: {project.name}",
        description="Automated per-system maturity audit",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context=task_context,
    )
    session.add(new_task)
    await session.flush()
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "AUTONOMOUS",
            "task_type": "self_audit",
            "focus_systems": focus or "audit-all",
        },
    )
    session.add(history)

    # Stamp dispatch timestamp + advance round-robin offset so subsequent
    # ticks rotate through additional systems.
    vision = dict(project.vision or {})
    vision["_self_audit_dispatched_at"] = _utcnow().isoformat()
    if focus:
        offset = int(vision.get("_self_audit_round_robin_offset", 0)) or 0
        vision["_self_audit_round_robin_offset"] = offset + len(focus)
    project.vision = vision
    flag_modified(project, "vision")
    session.add(project)
    await session.commit()
    logger.info(
        f"[SELF_AUDIT] Spawned self_audit task {new_task.id} for project "
        f"{project.id} (focus_systems={focus or 'audit-all'})"
    )
    return True
