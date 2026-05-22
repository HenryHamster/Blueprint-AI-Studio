"""Warm-VM backlog reuse (efficiency direction d-91d8dede).

Extends the existing warm-VM continuity mechanism so an idle agent VM can
claim the next eligible QUEUED task from its project's GENERAL backlog --
not only a PR-chain follow-on pinned to its VM. Gated by the runtime config
flag ``warm_backlog_reuse_enabled`` (default OFF), independent from
``warm_continuity_enabled``.

This module owns the *eligibility* and *atomic selection* for a backlog
claim. The actual QUEUED->IN_PROGRESS flip, telemetry stamping, and JWT mint
live in the /api/agent/next-role handler so the chain and backlog paths share
one claim-finalize routine.
"""

import logging
import uuid
from typing import Optional

from sqlalchemy import or_, select

from app.models import Task, TaskStatus
from app.orchestrator_filters import _prod_files
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

# Bounded window (seconds) for which a COMPLETED task's VM teardown is held so
# the agent can poll /api/agent/next-role and claim backlog work before the VM
# is torn down. Sized to comfortably exceed the agent's poll cadence (5s) plus
# one round-trip. If nothing is claimed within the window, teardown proceeds
# exactly as before.
WARM_BACKLOG_TEARDOWN_DEFER_SECS = 20

# How many QUEUED candidates to scan per poll. A small cap keeps the endpoint
# cheap (it can be hit by many idle VMs at up to 600/min) while still giving
# the file-conflict / skip-locked filters enough rows to find a claimable one.
_BACKLOG_SCAN_LIMIT = 10


async def _project_in_progress_files(session, project_id) -> set[str]:
    """Production files currently being modified by IN_PROGRESS tasks in a project.

    Mirrors the orchestrator's ``filter_file_conflicts`` isolation guarantee:
    a warm backlog claim must never run a task that overlaps files an
    IN_PROGRESS task is already editing, or the two diverge and collide at
    merge time. Test files are additive and excluded (same as the filter).
    """
    rows = (
        await session.execute(
            select(Task.context).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.IN_PROGRESS,
            )
        )
    ).all()
    files: set[str] = set()
    for (ctx,) in rows:
        files |= _prod_files((ctx or {}).get("related_files", []))
    return files


async def _eligible_backlog_ids(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> list[uuid.UUID]:
    """QUEUED task ids in *project_id* an idle VM may claim, priority-ordered.

    Conservative subset of the orchestrator's dispatch gate (kept simple
    rather than replicating the full async filter stack):
      - status == QUEUED, same project_id
      - not requires_approval (orchestrator skips these too)
      - retry_after_until null or already elapsed
      - no ``depends_on`` (avoids re-implementing dependency / PR-merge
        resolution; dependency-bearing tasks simply cold-dispatch as before)
      - not pinned to a *different* VM via ``warm_target_vm_id`` (a pin to
        this VM is the chain path's job; a pin elsewhere must not be stolen)
      - no related_files overlap with an IN_PROGRESS task in the same project

    Any task type is runnable by any agent VM (the workspace is re-cloned per
    role via resetForNextRole), so no per-VM task-type capability gate is
    needed — same-project scoping is the capability boundary.

    ``in_progress_files`` lets a caller that already computed the project's
    IN_PROGRESS file set (e.g. an eligibility gate immediately followed by a
    claim) pass it in so the file-conflict scan runs once instead of once per
    call. When omitted it is computed lazily — and only if the QUEUED scan
    returned rows, so an empty backlog never pays for the IN_PROGRESS scan.
    """
    now = _utcnow()
    rows = (
        await session.execute(
            select(
                Task.id,
                Task.depends_on,
                Task.context,
            )
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.project_id == project_id,
                Task.requires_approval.is_(False),
                or_(
                    Task.retry_after_until.is_(None),
                    Task.retry_after_until <= now,
                ),
            )
            .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
            .limit(_BACKLOG_SCAN_LIMIT)
        )
    ).all()
    if not rows:
        return []

    if in_progress_files is None:
        in_progress_files = await _project_in_progress_files(session, project_id)

    eligible: list[uuid.UUID] = []
    for task_id, depends_on, ctx in rows:
        if depends_on:
            continue
        ctx = ctx or {}
        pinned_vm = ctx.get("warm_target_vm_id")
        if pinned_vm and pinned_vm != vm_instance_id:
            continue
        if in_progress_files:
            cfiles = _prod_files(ctx.get("related_files", []))
            if cfiles & in_progress_files:
                continue
        eligible.append(task_id)
    return eligible


async def has_eligible_backlog_task(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> bool:
    """True iff *project_id* has at least one backlog task this VM could claim.

    Used by the teardown-deferral decision: only worth keeping an idle VM
    alive if there is genuine pending work it can run warm.

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this gate is paired with a claim.
    """
    return bool(
        await _eligible_backlog_ids(
            session, project_id, vm_instance_id, in_progress_files=in_progress_files
        )
    )


async def select_backlog_reuse_candidate(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> Optional[Task]:
    """Atomically lock the next claimable backlog task for an idle VM, or None.

    Walks the eligible ids in priority order and locks the first one still
    QUEUED with ``SELECT ... FOR UPDATE SKIP LOCKED`` — so a concurrent
    orchestrator cold-dispatch or a peer VM's next-role poll can never
    double-claim the same row (whoever wins the lock flips it; the other
    path's lock attempt skips it and tries the next candidate).

    The returned Task is locked and still QUEUED; the caller finalizes the
    claim (status flip, telemetry, commit).

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this claim follows an eligibility
    gate in the same flow, so the file-conflict scan is not run twice.
    """
    eligible = await _eligible_backlog_ids(
        session, project_id, vm_instance_id, in_progress_files=in_progress_files
    )
    for task_id in eligible:
        locked = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is not None:
            return locked
    return None
