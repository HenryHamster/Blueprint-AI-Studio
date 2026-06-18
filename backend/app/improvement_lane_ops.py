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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
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


async def reclaim_orphaned_lane_workspace_states(
    session: AsyncSession,
) -> int:
    """Reset workspace_state to COLD on lanes whose backing pool VM no longer exists.

    Complements the live-path ``clear_lanes_for_torn_down_vm`` call in
    ``retire_and_delete_pool_vm`` — handles pool VMs that were deleted while the
    orchestrator process was stopped (crash, deploy, manual removal).  Intended to
    be called once at orchestrator startup as a recovery sweep.

    Returns the count of lanes reclaimed.
    """
    from app.models import PoolVm

    result = await session.execute(
        select(ImprovementLane).where(
            ImprovementLane.workspace_state != "COLD",
            ImprovementLane.workspace_vm_instance_id.isnot(None),
        )
    )
    lanes = result.scalars().all()

    reclaimed = 0
    for lane in lanes:
        vm_result = await session.execute(
            select(PoolVm).where(PoolVm.vm_instance_id == lane.workspace_vm_instance_id)
        )
        if vm_result.scalar_one_or_none() is None:
            vm_id = lane.workspace_vm_instance_id
            lane.workspace_state = "COLD"
            lane.workspace_vm_instance_id = None
            reclaimed += 1
            logger.info(
                "[WARM-POOL] reclaimed orphaned workspace state on lane %s "
                "(vm %s no longer exists)",
                lane.id,
                vm_id,
            )

    return reclaimed
