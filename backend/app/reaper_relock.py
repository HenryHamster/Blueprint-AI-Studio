"""Orphan re-lock helper extracted from reaper.reaper_service.

Contains _relock_orphan_task, which re-acquires a FOR UPDATE lock on an
orphan task and validates the pre-IO snapshot. Mirrors
rollup_management._relock_parent and ci_completion._relock_merge_task.
"""

import logging
from datetime import datetime

from sqlalchemy.exc import InvalidRequestError
from sqlmodel import select

from app.models import Task, TaskStatus, VmContact

logger = logging.getLogger(__name__)


async def _relock_orphan_task(
    session,
    task_id,
    cached_task: Task | None,
    expected_status,
    expected_last_heartbeat,
    *,
    vm_liveness_cutoff: datetime | None = None,
) -> Task | None:
    """Re-acquire FOR UPDATE on an orphan task and validate the pre-IO snapshot.

    Mirrors ``rollup_management._relock_parent`` (canonical reference) and
    ``ci_completion._relock_merge_task``: expire-then-populate_existing-SELECT
    handles the cached-identity-map staleness under async + skip_locked.

    Returns the freshly-loaded Task if:
      - the row is still ours (not held by another writer), AND
      - status is unchanged (still PROVISIONING or IN_PROGRESS, not COMPLETED/FAILED), AND
      - last_heartbeat hasn't advanced (agent didn't revive during the unlocked window), AND
      - no fresh ``vmcontact`` landed during the unlocked window (heartbeat
        false-reap fix: the agent's last_heartbeat write may be lock-starved, so
        a contact within ``vm_liveness_cutoff`` proves the VM is alive even
        though ``last_heartbeat`` is unchanged).

    Returns None on any of those conditions failing — caller should ``continue``
    to the next orphan task.
    """
    if cached_task is not None:
        try:
            session.expire(cached_task)
        except InvalidRequestError:
            session.expire_all()
    relock_stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(relock_stmt)).scalars().first()
    if fresh is None:
        logger.info(
            f"[REAPER] Orphan re-lock skipped for task {task_id} — held by peer "
            f"(deferring to next reaper cycle)"
        )
        return None
    if fresh.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status transitioned "
            f"to {fresh.status} during external I/O — skipping reap"
        )
        return None
    if fresh.last_heartbeat != expected_last_heartbeat:
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} heartbeat advanced "
            f"({expected_last_heartbeat} → {fresh.last_heartbeat}) during external I/O "
            f"— agent revived, skipping reap"
        )
        return None
    # Heartbeat false-reap fix (option B): a contact that landed during the
    # unlocked I/O window aborts the reap even when last_heartbeat is unchanged
    # (the agent's contended last_heartbeat write was skipped by the rollup
    # lock). Gated on the same cutoff: a genuinely dead VM has no fresh contact
    # and is still reaped. Best-effort — never let a lookup error block the reap.
    if vm_liveness_cutoff is not None and fresh.vm_instance_id:
        try:
            _contact_at = (
                await session.execute(
                    select(VmContact.last_contact_at).where(
                        VmContact.vm_instance_id == fresh.vm_instance_id
                    )
                )
            ).scalar_one_or_none()
        except Exception as _vc_exc:  # noqa: BLE001
            logger.warning(
                "[REAPER] vm_contact re-lock lookup failed for task %s vm=%s "
                "(non-fatal, proceeding with reap): %s",
                task_id,
                fresh.vm_instance_id,
                _vc_exc,
            )
            _contact_at = None
        if _contact_at is not None and _contact_at >= vm_liveness_cutoff:
            logger.info(
                f"[REAPER] Orphan re-lock found task {task_id} vm "
                f"{fresh.vm_instance_id} contacted the orchestrator at "
                f"{_contact_at} (>= cutoff {vm_liveness_cutoff}) during external "
                f"I/O — agent alive (lock-starved heartbeat), skipping reap"
            )
            return None
    if fresh.status != expected_status:
        # Defensive: status changed within the (PROVISIONING, IN_PROGRESS) set
        # — e.g. claim_task flipped PROVISIONING → IN_PROGRESS. Not the same as
        # a terminal transition, but the reap reason classification was based on
        # the original status, so safer to defer.
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status changed "
            f"({expected_status} → {fresh.status}) during external I/O — skipping reap"
        )
        return None
    return fresh
