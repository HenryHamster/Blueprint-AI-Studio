"""Orphan re-lock helper extracted from reaper.reaper_service.

Contains _relock_orphan_task, which re-acquires a FOR UPDATE lock on an
orphan task and validates the pre-IO snapshot. Mirrors
rollup_management._relock_parent and ci_completion._relock_merge_task.
"""

import logging
from datetime import datetime

from sqlalchemy.exc import InvalidRequestError
from sqlmodel import select

from app.models import Task, TaskContact, TaskStatus, VmContact
from app.reaper_orphan_classify import (
    _is_multiplexed_vm_id,
    _runner_report_excludes_task,
)

logger = logging.getLogger(__name__)


async def _relock_contact_at(session, fresh) -> tuple[datetime | None, str]:
    """Fresh-read the liveness contact stamp that is authoritative FOR THIS TASK.

    The relock stage deliberately re-queries (rather than reusing the sweep-time
    lookup maps ``_classify_orphan`` was given): its whole job is to catch a
    contact that landed DURING the unlocked external-I/O window, which can be
    30-120s wide. A stale dict would defeat that. So this mirrors
    ``_classify_orphan``'s *semantics* with the relock's *fresh-read* design.

    Which row is authoritative depends on whether the vm id is shared:

      * ``runner:<id>`` (fleet runner, MANY tasks per vm id) → the ``vmcontact``
        row is SHARED with every sibling task on that runner, so a live sibling's
        heartbeat keeps it fresh and it proves nothing about THIS task. Read the
        TASK-scoped ``taskcontact`` row instead. ``taskcontact.last_contact_at``
        is NOT NULL, so "returned a value" is exactly "a row exists" — and a row
        that exists is authoritative: we do NOT fall back to the shared row when
        its stamp is merely stale, because that fallback IS the sibling-masking
        bug (a hung agent's own stamp goes stale while its sibling keeps the
        shared row fresh forever).
      * row ABSENT for a ``runner:`` task → pre-rollout / not yet heartbeated
        under the new handler. Absence is NEVER evidence of death: fall back to
        the shared row, i.e. exactly today's behaviour. No task can be
        false-reaped by rolling this out.
      * GCE agent VM (one task per VM) → ``vmcontact`` already IS the task's row.
        Read it unchanged, byte-for-byte, preserving the genuine
        lock-starved-but-alive false-reap protection this override exists for.

    Returns ``(last_contact_at | None, scope)`` where ``scope`` is ``"taskcontact"``
    or ``"vmcontact"``, purely so the caller can log WHICH signal it acted on.
    """
    if _is_multiplexed_vm_id(fresh):
        _tc_at = (
            await session.execute(
                select(TaskContact.last_contact_at).where(
                    TaskContact.task_id == fresh.id
                )
            )
        ).scalar_one_or_none()
        if _tc_at is not None:
            return _tc_at, "taskcontact"
    return (
        await session.execute(
            select(VmContact.last_contact_at).where(
                VmContact.vm_instance_id == fresh.vm_instance_id
            )
        )
    ).scalar_one_or_none(), "vmcontact"


async def _relock_orphan_task(
    session,
    task_id,
    cached_task: Task | None,
    expected_status,
    expected_last_heartbeat,
    *,
    vm_liveness_cutoff: datetime | None = None,
    runner_report_lookup: dict | None = None,
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
        though ``last_heartbeat`` is unchanged) — UNLESS the task's own fleet
        runner freshly self-reports that it is no longer running this task
        (``runner_report_lookup``), which defeats that contact override.

    Returns None on any of those conditions failing — caller should ``continue``
    to the next orphan task.

    ``runner_report_lookup`` is the ``{vm_instance_id: (reported_at,
    frozenset(task_id_strs))}`` map built by
    ``reaper_orphan_classify._build_runner_report_lookup``. It closes the
    shared-VmContact zombie mask at THIS stage: every task on a fleet runner
    shares ``vm_instance_id='runner:<id>'`` and therefore ONE ``vmcontact`` row,
    so any live sibling agent keeps ``last_contact_at`` fresh and the abort above
    fires forever on a task whose agent is dead — the reap is correctly
    classified by ``_classify_orphan`` (which got the runner-report override in
    PR #14413) and then silently un-reaped here, every cycle. Recomputed against
    the FRESH (post-relock) row because ``started_at`` may have been stamped
    during the unlocked I/O window and the exclusion is claim-grace-gated.
    Fail-safe: absent / stale / pre-grace report, or a non-``runner:`` vm id →
    the vm_contact abort applies exactly as before.
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
        # Shared-VmContact zombie mask, relock stage (follow-up to #14413, which
        # only closed it in _classify_orphan). The contact row below is keyed on
        # vm_instance_id, and ALL tasks on a fleet runner share
        # 'runner:<id>' — a live sibling's heartbeat keeps it fresh, so the abort
        # below would un-reap a dead task on every cycle, forever. The runner's
        # own fresh self-report (active_task_ids) is per-TASK ground truth: when
        # it omits this task the agent process is gone and the sibling's contact
        # must not shield it. Evaluated on the FRESH row (started_at may have
        # been stamped during the unlocked I/O window; the exclusion is
        # claim-grace-gated on it).
        #
        # CONSCIOUS ACCEPTANCE (reviewed 2026-07-12) — this override makes the
        # runner self-report the LAST safety net: before it, a false positive in
        # _runner_report_excludes_task could not reap (the contact abort below
        # caught it — which is precisely why runner zombies were unreapable);
        # now it can. The one realizable false positive: the runner is `kill -9`d
        # (graceful shutdown terminates its agent process trees, so only SIGKILL /
        # OOM / host crash orphans them), its agent children survive
        # (start_new_session), and the restarted process posts active_task_ids=[]
        # with active_work_report=True. A surviving agent whose on-row heartbeat
        # write is ALSO lock-starved for the whole stale window (a fresh
        # heartbeat means the task is never an orphan candidate at all) is then
        # reaped despite its own fresh vm_contact. Accepted: that agent is
        # already doomed — the restarted runner's _cleanup_stale_workspaces
        # (local_runner.py, every ~60s) rmtree's every workspace dir not in
        # active_task_ids once its mtime is >=300s old, and the runner has
        # dropped it from its capacity accounting, so nothing will ever
        # reconcile its result. Requeuing the task is the correct outcome. The
        # same is true of the other exclusion path (an overdue agent whose
        # terminate_process_tree failed is removed from active_tasks anyway) —
        # in every realizable case the runner has already given up on the agent.
        _runner_excluded = _runner_report_excludes_task(
            fresh, vm_liveness_cutoff, runner_report_lookup
        )
        if _runner_excluded:
            logger.warning(
                "[REAPER] Orphan re-lock: the runner hosting task %s (vm %s) "
                "freshly reports it is NOT running this task — the shared "
                "vm_contact row carries a sibling's liveness, not this task's; "
                "proceeding with the reap.",
                task_id,
                fresh.vm_instance_id,
            )
        else:
            # The runner still CLAIMS this task — but that is exactly the
            # hung-but-alive case, and it says nothing about whether the task's
            # own agent is making progress. Reading the SHARED vmcontact row here
            # is what made the heartbeat-stale reap inert: a working sibling on the
            # same runner keeps it fresh, so the abort below fired forever on a
            # task whose agent was dead. _runner_report_excludes_task cannot close
            # that by construction — a hung-but-alive process is still listed in
            # ``active_task_ids``, so the branch above is False precisely here.
            # _relock_contact_at picks the row that is authoritative for THIS task:
            # the TASK-scoped ``taskcontact`` row for ``runner:`` ids when one
            # exists, and the shared ``vmcontact`` row for GCE (1 task per VM,
            # where it already IS the task's row) and for pre-rollout tasks with no
            # row yet. Fresh-read, like the query it replaces.
            try:
                _contact_at, _contact_scope = await _relock_contact_at(session, fresh)
            except Exception as _vc_exc:  # noqa: BLE001
                logger.warning(
                    "[REAPER] contact re-lock lookup failed for task %s vm=%s "
                    "(non-fatal, proceeding with reap): %s",
                    task_id,
                    fresh.vm_instance_id,
                    _vc_exc,
                )
                _contact_at = None
                _contact_scope = "vmcontact"
            if _contact_at is not None and _contact_at >= vm_liveness_cutoff:
                logger.info(
                    f"[REAPER] Orphan re-lock found task {task_id} vm "
                    f"{fresh.vm_instance_id} contacted the orchestrator at "
                    f"{_contact_at} (>= cutoff {vm_liveness_cutoff}, scope "
                    f"{_contact_scope}) during external I/O — agent alive "
                    f"(lock-starved heartbeat), skipping reap"
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
