"""Operations on ImprovementLane rows: create, CAS transitions, checkpoint.

All mutations are CAS-guarded via SELECT … FOR UPDATE so concurrent workers
cannot double-transition or double-checkpoint.  The module is deliberately
narrow: it owns lane row mutations only, not task spawning or GitHub calls.

Enabled only when ``improvement_lanes_enabled`` runtime-config flag is True.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    PoolVm,
    PoolVmState,
    Task,
    TaskStatus,
    _IMPROVEMENT_LANE_TRANSITIONS,
    _LANE_CHECKPOINT_LIMIT,
    _LANE_MANIFEST_MAX_BYTES,
    _LANE_MAX_PHASE_REQUEUES,
)
from app.payload_schemas import LaneCheckpointPayload
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


class LaneCASError(Exception):
    """Raised when a CAS transition fails due to unexpected current status."""


class LaneCheckpointError(Exception):
    """Raised when a checkpoint cannot be appended (bad sha, budget, etc.)."""


class LaneOwnerMismatchError(LaneCheckpointError):
    """Raised when the completing task does not own the active lane."""


_TERMINAL_STATUSES = frozenset(
    {ImprovementLaneStatus.RETIRED.value, ImprovementLaneStatus.KILLED.value}
)

_VM_RELEASED_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)


def _empty_lane_workspace_reconciliation_result(*, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "lanes_examined": 0,
        "lanes_would_clear": 0,
        "lanes_cleared": 0,
        "missing_backing_vm": 0,
        "project_mismatch": 0,
        "terminal_lane_workspace": 0,
        "terminal_or_missing_leased_task": 0,
        "stale_workspace_reservation": 0,
        "reservations_examined": 0,
        "reservations_would_clear": 0,
        "reservations_cleared": 0,
        "stale_reservations": 0,
        "non_idle_reservations": 0,
        "missing_reservation_lane": 0,
        "terminal_reservation_lane": 0,
        "reservation_project_mismatch": 0,
        "hard_expired_reservations": 0,
        "soft_expired_reservations": 0,
        "race_noops": 0,
    }


def _reservation_cleanup_reason(
    member: PoolVm,
    lane: ImprovementLane | None,
    *,
    now: datetime,
) -> tuple[str, str | None] | None:
    """Return (count key, telemetry cause) when a PoolVm reservation is stale."""
    if member.state != PoolVmState.IDLE:
        return ("non_idle_reservations", "force_expired")
    if lane is None:
        return ("missing_reservation_lane", None)
    if lane.project_id != member.project_id:
        return ("reservation_project_mismatch", "force_expired")
    if lane.status in _TERMINAL_STATUSES:
        return ("terminal_reservation_lane", "force_expired")
    hard_expiry = member.reservation_hard_expires_at
    if hard_expiry is not None and hard_expiry <= now:
        return ("hard_expired_reservations", "force_expired")
    soft_expiry = member.reservation_expires_at
    if soft_expiry is None:
        return ("soft_expired_reservations", "expired_before_fix")
    if soft_expiry <= now and lane.status != ImprovementLaneStatus.PARKED.value:
        return ("soft_expired_reservations", "expired_before_fix")
    return None


def _workspace_reservation_is_stale(
    lane: ImprovementLane, member: PoolVm, *, now: datetime
) -> bool:
    if member.reserved_lane_id != lane.id:
        return False
    if member.state != PoolVmState.IDLE:
        return True
    return _reservation_cleanup_reason(member, lane, now=now) is not None


async def _clear_lane_workspace_if_snapshot_matches(
    session: AsyncSession,
    lane: ImprovementLane,
) -> bool:
    result = await session.execute(
        update(ImprovementLane)
        .where(
            ImprovementLane.id == lane.id,
            ImprovementLane.workspace_state == lane.workspace_state,
            ImprovementLane.workspace_vm_instance_id == lane.workspace_vm_instance_id,
        )
        .values(workspace_state="COLD", workspace_vm_instance_id=None)
    )
    return bool(result.rowcount)


async def _clear_pool_vm_reservation_if_snapshot_matches(
    session: AsyncSession,
    member: PoolVm,
) -> bool:
    result = await session.execute(
        update(PoolVm)
        .where(
            PoolVm.id == member.id,
            PoolVm.vm_instance_id == member.vm_instance_id,
            PoolVm.state == member.state,
            PoolVm.reserved_lane_id == member.reserved_lane_id,
            PoolVm.reservation_expires_at == member.reservation_expires_at,
            PoolVm.reservation_hard_expires_at == member.reservation_hard_expires_at,
        )
        .values(
            reserved_lane_id=None,
            reservation_expires_at=None,
            reservation_hard_expires_at=None,
        )
    )
    return bool(result.rowcount)


async def create_lane(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    direction_id: str | None,
    title: str,
    goal: str,
    branch_name: str,
    base_branch: str,
    version_number: int | None = None,
    pilot_arm: str | None = None,
) -> ImprovementLane:
    """Create a new ImprovementLane in PROPOSED status.

    The caller is responsible for committing the session; this function only
    adds the row so it can be wrapped in a larger transaction.
    """
    lane = ImprovementLane(
        id=uuid.uuid4(),
        project_id=project_id,
        direction_id=direction_id,
        version_number=version_number,
        title=title,
        goal=goal,
        status=ImprovementLaneStatus.PROPOSED.value,
        branch_name=branch_name,
        base_branch=base_branch,
        manifest={},
        checkpoints=[],
        pilot_arm=pilot_arm,
    )
    session.add(lane)
    return lane


async def cas_transition(
    session: AsyncSession,
    lane_id: uuid.UUID,
    expected: ImprovementLaneStatus,
    next_status: ImprovementLaneStatus,
    *,
    current_task_id: uuid.UUID | None = None,
    clear_task: bool = False,
) -> ImprovementLane:
    """Compare-and-swap the lane status from *expected* to *next_status*.

    Uses SELECT … FOR UPDATE to prevent concurrent transitions.  Raises
    :class:`LaneCASError` when the lane's actual status differs from
    *expected* (stale read) or when the transition is not in the allowed
    graph.

    ``current_task_id`` — set on ACTIVE transitions (binds the dispatched task).
    ``clear_task`` — clear current_task_id on PARKED transitions.
    """
    transition = (expected.value, next_status.value)
    if transition not in _IMPROVEMENT_LANE_TRANSITIONS:
        raise LaneCASError(
            f"Transition {expected.value}->{next_status.value} is not allowed"
        )

    result = await session.execute(
        select(ImprovementLane).where(ImprovementLane.id == lane_id).with_for_update()
    )
    lane = result.scalar_one_or_none()
    if lane is None:
        raise LaneCASError(f"Lane {lane_id} not found")
    if lane.status != expected.value:
        raise LaneCASError(
            f"Lane {lane_id} expected status {expected.value}, got {lane.status}"
        )

    lane.status = next_status.value
    now = _utcnow()

    if next_status == ImprovementLaneStatus.ACTIVE:
        lane.last_resumed_at = now
        if current_task_id is not None:
            lane.current_task_id = current_task_id

    if next_status == ImprovementLaneStatus.PARKED:
        lane.parked_at = now
        if clear_task:
            lane.current_task_id = None

    if next_status in (
        ImprovementLaneStatus.RETIRED,
        ImprovementLaneStatus.KILLED,
    ):
        lane.retired_at = now
        lane.current_task_id = None
        lane.workspace_state = "COLD"
        lane.workspace_vm_instance_id = None

    lane.updated_at = now
    return lane


def _merge_manifest_patch(
    existing: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """Merge *patch* into *existing* manifest with bounded list fields.

    List fields (progress, remaining, critical_files, failing_tests, risks,
    phase_log) are appended and trimmed to keep the total manifest under
    _LANE_MANIFEST_MAX_BYTES.  Other fields are overwritten by patch.
    """
    # Eviction-priority order: fields listed first are trimmed first when the
    # manifest is over budget.  phase_log and failing_tests grow fastest and have
    # the lowest per-entry value; critical_files and risks are small and high-value
    # so they survive longest.  Tie-breaking uses the first match in this sequence
    # (the > guard in the loop keeps the earliest-seen winner on equal lengths).
    _LIST_FIELDS = [
        "phase_log",
        "failing_tests",
        "progress",
        "remaining",
        "critical_files",
        "risks",
    ]
    merged = dict(existing)
    for key, value in patch.items():
        if key in _LIST_FIELDS and isinstance(value, list):
            existing_list = merged.get(key, [])
            if not isinstance(existing_list, list):
                existing_list = []
            merged[key] = existing_list + value
        else:
            merged[key] = value

    # Trim if over budget by pruning the longest list fields first.
    while len(json.dumps(merged, default=str).encode()) > _LANE_MANIFEST_MAX_BYTES:
        longest_field: str | None = None
        longest_size = 0
        for field in _LIST_FIELDS:
            lst = merged.get(field)
            if isinstance(lst, list) and len(lst) > 1 and len(lst) > longest_size:
                longest_field = field
                longest_size = len(lst)
        if longest_field is None:
            break
        merged[longest_field] = merged[longest_field][1:]

    return merged


async def checkpoint_lane(
    session: AsyncSession,
    lane_id: uuid.UUID,
    phase: str,
    task_id: uuid.UUID,
    checkpoint: LaneCheckpointPayload,
) -> ImprovementLane:
    """Append a validated checkpoint record and CAS the lane ACTIVE -> PARKED.

    Raises :class:`LaneCheckpointError` if the lane is not ACTIVE.
    The caller must verify ``checkpoint.sha`` is reachable on the lane branch
    BEFORE calling this function (via GitHub API or equivalent).
    """
    result = await session.execute(
        select(ImprovementLane).where(ImprovementLane.id == lane_id).with_for_update()
    )
    lane = result.scalar_one_or_none()
    if lane is None:
        raise LaneCheckpointError(f"Lane {lane_id} not found")
    if lane.status != ImprovementLaneStatus.ACTIVE.value:
        raise LaneCheckpointError(
            f"Lane {lane_id} must be ACTIVE to checkpoint, got {lane.status}"
        )
    if lane.current_task_id != task_id:
        raise LaneOwnerMismatchError(
            f"Lane {lane_id} is owned by task {lane.current_task_id}, "
            f"but checkpoint was attempted by task {task_id}"
        )

    now = _utcnow()
    seq = lane.checkpoint_seq + 1

    record: dict[str, Any] = {
        "seq": seq,
        "phase": phase,
        "task_id": str(task_id),
        "sha": checkpoint.sha,
        "parent_sha": lane.last_checkpoint_sha,
        "created_at": now.isoformat(),
        "summary": checkpoint.summary,
        "validated": {
            "lint": checkpoint.validated.lint,
            "tests": checkpoint.validated.tests,
        },
        "wip_failing": checkpoint.wip_failing,
        "tokens_in": checkpoint.tokens_in,
        "tokens_out": checkpoint.tokens_out,
        "vm_cost_usd": checkpoint.vm_cost_usd,
        "warm_resume": checkpoint.warm_resume,
    }

    # Bounded rolling window — keep last _LANE_CHECKPOINT_LIMIT entries.
    existing = list(lane.checkpoints or [])
    existing.append(record)
    if len(existing) > _LANE_CHECKPOINT_LIMIT:
        existing = existing[-_LANE_CHECKPOINT_LIMIT:]
    lane.checkpoints = existing

    # Merge manifest patch.
    if checkpoint.manifest_patch:
        lane.manifest = _merge_manifest_patch(
            dict(lane.manifest or {}), checkpoint.manifest_patch
        )

    # Update checkpoint pointers.
    lane.last_checkpoint_sha = checkpoint.sha
    lane.last_checkpoint_phase = phase
    lane.checkpoint_seq = seq

    # Accumulate accounting.
    lane.llm_input_tokens_total = (
        lane.llm_input_tokens_total or 0
    ) + checkpoint.tokens_in
    lane.llm_output_tokens_total = (
        lane.llm_output_tokens_total or 0
    ) + checkpoint.tokens_out
    lane.vm_cost_usd_total = (lane.vm_cost_usd_total or 0.0) + checkpoint.vm_cost_usd

    # CAS ACTIVE -> PARKED.
    lane.status = ImprovementLaneStatus.PARKED.value
    lane.parked_at = now
    lane.current_task_id = None
    lane.updated_at = now

    return lane


async def mark_checkpoint_missing(
    session: AsyncSession,
    lane_id: uuid.UUID,
    reason: str = "checkpoint_missing",
) -> tuple[ImprovementLane, bool]:
    """Record a failed checkpoint attempt and decide whether to requeue.

    Returns ``(lane, should_requeue)``.  ``should_requeue=True`` when
    ``phase_requeues`` is still under the budget; the caller is responsible
    for actually creating the requeue task.  When the budget is exhausted
    the lane is left ACTIVE here — the live review-spawn path
    (``review_fix_lane_pilot.checkpoint_or_recover_phase``) closes it inline
    (KILLED + reservation released), with ``sweep_exhausted_lane_budgets`` as a
    backstop for any over-budget lane that escaped inline handling.
    """
    result = await session.execute(
        select(ImprovementLane).where(ImprovementLane.id == lane_id).with_for_update()
    )
    lane = result.scalar_one_or_none()
    if lane is None:
        raise LaneCheckpointError(f"Lane {lane_id} not found")

    lane.phase_requeues = (lane.phase_requeues or 0) + 1
    lane.updated_at = _utcnow()

    should_requeue = lane.phase_requeues <= _LANE_MAX_PHASE_REQUEUES
    if lane.phase_requeues == _LANE_MAX_PHASE_REQUEUES - 1:
        logger.warning(
            "lane %s approaching phase_requeues exhaustion (%d/%d) reason=%s; "
            "one more miss will trigger kill",
            lane_id,
            lane.phase_requeues,
            _LANE_MAX_PHASE_REQUEUES,
            reason,
        )
    if not should_requeue:
        logger.warning(
            "lane %s exhausted phase_requeues budget (%d/%d) reason=%s; "
            "surface to strategist",
            lane_id,
            lane.phase_requeues,
            _LANE_MAX_PHASE_REQUEUES,
            reason,
        )
    return lane, should_requeue


async def reconcile_lane_workspaces(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    project_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Repair lane workspace/reservation state against the warm-pool registry.

    The reconciliation is intentionally conservative and idempotent. It clears
    lane workspace pointers only when the existing state is already impossible:
    missing backing PoolVm, cross-project backing PoolVm, terminal lane, LEASED
    lane whose owning task is gone or VM-released, or WARM_RESERVED lane whose
    matching PoolVm reservation is stale. It also clears stale PoolVm reservation
    fields when the owning lane is missing, terminal, cross-project, hard-expired,
    or soft-expired while no longer PARKED.

    ``dry_run=True`` returns the same structured counts without mutating rows.
    All writes are snapshot-guarded, so a lane or reservation that moves between
    the scan and the repair becomes a counted race-safe no-op instead of being
    clobbered.
    """
    if now is None:
        now = _utcnow()
    project_uuid = uuid.UUID(str(project_id)) if project_id is not None else None
    result = _empty_lane_workspace_reconciliation_result(dry_run=dry_run)

    lane_stmt = select(ImprovementLane).where(
        ImprovementLane.workspace_state != "COLD",
        ImprovementLane.workspace_vm_instance_id.isnot(None),
    )
    if project_uuid is not None:
        lane_stmt = lane_stmt.where(ImprovementLane.project_id == project_uuid)
    lanes = (await session.execute(lane_stmt)).scalars().all()
    result["lanes_examined"] = len(lanes)

    vm_ids = sorted(
        {
            str(lane.workspace_vm_instance_id)
            for lane in lanes
            if lane.workspace_vm_instance_id
        }
    )
    vm_map: dict[str, PoolVm] = {}
    if vm_ids:
        pool_members = (
            (
                await session.execute(
                    select(PoolVm).where(PoolVm.vm_instance_id.in_(vm_ids))
                )
            )
            .scalars()
            .all()
        )
        vm_map = {member.vm_instance_id: member for member in pool_members}

    task_ids = {
        lane.current_task_id
        for lane in lanes
        if lane.workspace_state == "LEASED" and lane.current_task_id is not None
    }
    task_map: dict[uuid.UUID, Task] = {}
    if task_ids:
        tasks = (
            (
                await session.execute(select(Task).where(Task.id.in_(task_ids)))
            )
            .scalars()
            .all()
        )
        task_map = {task.id: task for task in tasks}

    lanes_to_clear: list[tuple[ImprovementLane, str]] = []
    for lane in lanes:
        member = vm_map.get(str(lane.workspace_vm_instance_id))
        reason: str | None = None
        if lane.status in _TERMINAL_STATUSES:
            reason = "terminal_lane_workspace"
        elif member is None:
            reason = "missing_backing_vm"
        elif member.project_id != lane.project_id:
            reason = "project_mismatch"
        elif lane.workspace_state == "LEASED":
            task = (
                task_map.get(lane.current_task_id)
                if lane.current_task_id is not None
                else None
            )
            if (
                lane.current_task_id is not None
                and (
                    task is None
                    or task.status in _VM_RELEASED_TASK_STATUSES
                )
            ):
                reason = "terminal_or_missing_leased_task"
        elif lane.workspace_state == "WARM_RESERVED" and _workspace_reservation_is_stale(
            lane, member, now=now
        ):
            reason = "stale_workspace_reservation"

        if reason is None:
            continue
        result[reason] += 1
        lanes_to_clear.append((lane, reason))

    if dry_run:
        result["lanes_would_clear"] = len(lanes_to_clear)
    else:
        for lane, _reason in lanes_to_clear:
            if await _clear_lane_workspace_if_snapshot_matches(session, lane):
                result["lanes_cleared"] += 1
            else:
                result["race_noops"] += 1

    reservation_stmt = select(PoolVm).where(PoolVm.reserved_lane_id.isnot(None))
    if project_uuid is not None:
        reservation_stmt = reservation_stmt.where(PoolVm.project_id == project_uuid)
    reserved_members = (await session.execute(reservation_stmt)).scalars().all()
    result["reservations_examined"] = len(reserved_members)

    reserved_lane_ids = {
        member.reserved_lane_id
        for member in reserved_members
        if member.reserved_lane_id is not None
    }
    reserved_lane_map: dict[uuid.UUID, ImprovementLane] = {}
    if reserved_lane_ids:
        reserved_lanes = (
            (
                await session.execute(
                    select(ImprovementLane).where(
                        ImprovementLane.id.in_(reserved_lane_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        reserved_lane_map = {lane.id: lane for lane in reserved_lanes}

    stale_reservations: list[tuple[PoolVm, str | None]] = []
    for member in reserved_members:
        lane = reserved_lane_map.get(member.reserved_lane_id)
        stale = _reservation_cleanup_reason(member, lane, now=now)
        if stale is None:
            continue
        count_key, telemetry_cause = stale
        result[count_key] += 1
        result["stale_reservations"] += 1
        stale_reservations.append((member, telemetry_cause))

    if dry_run:
        result["reservations_would_clear"] = len(stale_reservations)
    else:
        for member, telemetry_cause in stale_reservations:
            lane_id = member.reserved_lane_id
            if await _clear_pool_vm_reservation_if_snapshot_matches(session, member):
                result["reservations_cleared"] += 1
                if telemetry_cause is not None and lane_id is not None:
                    from app.warm_pool import record_lane_reservation_cause

                    await record_lane_reservation_cause(
                        session, lane_id, telemetry_cause
                    )
            else:
                result["race_noops"] += 1

    return result


async def reclaim_orphaned_lane_workspace_states(
    session: AsyncSession,
) -> int:
    """Compatibility wrapper for the startup orphan-reclaim sweep.

    Historically this returned only the number of lane workspaces reset because
    their backing PoolVm vanished. It now delegates to the shared reconciliation
    path so startup receives the same repairs as periodic/admin callers while
    preserving the integer return contract.
    """
    result = await reconcile_lane_workspaces(session)
    return int(result["lanes_cleared"])
