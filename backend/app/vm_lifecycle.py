"""Agent VM teardown lifecycle.

Extracted from ``app.main`` (P5 service-locator decomposition) so callers
(orchestrator, reaper, services, admin, agent_status, directions, projects,
admin_settings, admin_agent_ops, admin_task_mutations, agent_warm_continuity)
can import VM-teardown helpers directly without depending on the FastAPI app
entrypoint. ``app.main`` re-exports these symbols for backward compatibility.

Holds the in-memory GCP VM-count cache and the orchestrator-initiated
VM-delete tracker the reaper consults to distinguish an orchestrator-completed
teardown from a spot preemption. Pure leaf module — depends only on
``app.compute``, ``app.database``, ``app.models``, ``app.warm_backlog_reuse``.

Module-level mutable counters (``_last_gcp_vm_count``, ``_last_gcp_vm_check``)
are rebound by the functions below, so readers outside this module must access
them as ``vm_lifecycle._last_gcp_vm_count`` (module-qualified) rather than via
``from app.vm_lifecycle import _last_gcp_vm_count`` (which captures a stale
snapshot). Tests patch the GCP boundary via ``app.vm_lifecycle.delete_agent_vm``
and the teardown helpers via ``app.vm_lifecycle.<name>``.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.compute import delete_agent_vm
from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# Best-effort serial-console forensics capture is time-boxed so a hung GCE
# serial RPC can never block or delay an actual VM teardown.
_TEARDOWN_FORENSICS_TIMEOUT = 15

# GCP VM count cache — updated by cleanup service, used by orchestrator concurrency check
_last_gcp_vm_count: int = 0
_last_gcp_vm_check: float = 0.0
_GCP_VM_CHECK_INTERVAL: float = 120.0  # Seconds between GCP VM count refreshes

# Orchestrator-initiated VM-delete tracker. Reaper consults this to distinguish
# "VM DELETED because orchestrator told it to" from "VM DELETED because GCP
# preempted it." Without this, the reaper sees vm_status=DELETED on a SPOT VM
# and bumps spot_preemption_count, treating an orchestrator-completed task as
# a preemption — observed in production 2026-05-04 when status POST 500'd
# during cutover but agent_completion still deleted the VM (reaper later
# misclassified the orphan).
# Key: vm_instance_id (str). Value: (unix_ts, reason). TTL = 30min — pruned
# lazily on read. In-memory only (Cloud Run = single instance per region for
# this service); restart loses entries but the misclassification window then
# closes with the worker.
_orchestrator_deleted_vms: Dict[str, tuple[float, str]] = {}
_ORCHESTRATOR_DELETE_TTL: float = 1800.0  # 30 minutes
# Hard cap so a write-faster-than-read pattern (e.g. reaper paused while
# many deletes happen) cannot grow this dict unboundedly. At ~80 bytes/entry
# this caps memory at ~400 KB. Pruning is by oldest-first when over the cap,
# preserving the most-recently-deleted entries that the reaper is most
# likely to consult next.
_ORCHESTRATOR_DELETE_MAX_ENTRIES: int = 5000


def is_orchestrator_initiated_delete(vm_id: str) -> tuple[bool, str | None]:
    """Return (True, reason) if `vm_id` was deleted by the orchestrator
    within the last _ORCHESTRATOR_DELETE_TTL seconds.

    Side-effect: prunes expired entries on every call AND enforces the size
    cap. Module-level state so the reaper can import it without a circular
    dep on `_ensure_vm_deleted`.
    """
    now = time.time()
    expired = [
        k
        for k, (ts, _) in _orchestrator_deleted_vms.items()
        if now - ts > _ORCHESTRATOR_DELETE_TTL
    ]
    for k in expired:
        _orchestrator_deleted_vms.pop(k, None)
    # Hard-cap eviction: oldest entries first. Defensive — if the dict still
    # has more than the cap after TTL pruning, we drop the oldest until it
    # fits. Logged so operators see the eviction and can investigate why
    # writes outpaced reads.
    if len(_orchestrator_deleted_vms) > _ORCHESTRATOR_DELETE_MAX_ENTRIES:
        sorted_by_ts = sorted(
            _orchestrator_deleted_vms.items(), key=lambda kv: kv[1][0]
        )
        excess = len(_orchestrator_deleted_vms) - _ORCHESTRATOR_DELETE_MAX_ENTRIES
        for k, _ in sorted_by_ts[:excess]:
            _orchestrator_deleted_vms.pop(k, None)
        logger.warning(
            "[VM TRACK] _orchestrator_deleted_vms exceeded %d entries — evicted %d oldest",
            _ORCHESTRATOR_DELETE_MAX_ENTRIES,
            excess,
        )
    entry = _orchestrator_deleted_vms.get(vm_id)
    if entry is None:
        return False, None
    return True, entry[1]


async def _vm_is_governed_pool_member(vm_id: str) -> bool:
    """True iff *vm_id* is a LIVE warm-pool member whose teardown is owned by the
    warm-pool lifecycle, not the generic task-completion teardown path.

    Mirrors :func:`app.warm_pool.pool_vm_cleanup_exemptions` (the orphan-sweep
    exemption) for a single VM. When a pool-leased task reaches a terminal state
    the agent_status handler fires ``fire_and_forget_vm_delete`` like any other
    task — but a pool member must NOT be cold-deleted there: the pool reconciles it
    itself, returning it to IDLE for re-lease (:func:`app.warm_pool.release_pool_vm`,
    COMPLETED) or flipping it RETIRING for the maintainer's
    ``retire_and_delete_pool_vm`` (FAILED/CANCELLED), which also clears the
    pre-staged secret and the registry row. Deleting the VM here would make every
    member single-use and strand the registry row counting against the pool budget.

    Only LIVE members (``LIVE_POOL_STATES`` = PROVISIONING/IDLE/LEASED) are
    governed; a RETIRING member is intentionally excluded so the pool's OWN retire
    path — which itself calls ``_ensure_vm_deleted`` — still tears it down. Returns
    False (caller proceeds with teardown) when the pool is disabled, the VM is not a
    registered member, or the lookup fails — we never leak a live VM by assuming
    reuse. The flag check short-circuits before any DB I/O, so the common
    cold-provisioned teardown pays nothing while the pool is dark.
    """
    try:
        from app.warm_pool import is_warm_pool_enabled

        if not is_warm_pool_enabled():
            return False
        from app.warm_pool import LIVE_POOL_STATES, get_pool_vm

        async with AsyncSessionLocal() as s:
            member = await get_pool_vm(s, vm_id)
        return member is not None and member.state in LIVE_POOL_STATES
    except Exception:
        logger.exception(
            "[WARM-POOL] pool-member teardown guard failed for VM %s — "
            "proceeding with teardown",
            vm_id,
        )
        return False


async def _capture_and_persist_teardown_forensics(vm_id: str, reason: str) -> None:
    """Capture a VM's serial-console tail BEFORE it is deleted and persist it
    onto the owning task's ``context['teardown_forensics']``.

    GCE ``get-serial-port-output`` returns "not ready" once an instance stops,
    so a silently-dead agent's last serial output is lost the instant the VM is
    deleted. Capturing at the single teardown chokepoint (``_ensure_vm_deleted``)
    means every teardown path — fire-and-forget completion delete, deferred
    backlog teardown, the reaper's post-commit delete loop, and ad-hoc
    orchestrator deletes — records the tail before the VM goes away.

    Entirely best-effort: the zone is resolved from the in-memory cache only (no
    cross-zone probe), and the tail is persisted ONLY when a non-empty tail is
    actually returned. Existing keys in ``task.context`` are preserved. The
    caller time-boxes this with ``asyncio.wait_for`` and swallows every
    exception so forensics can never block or delay the real teardown.
    """
    from app import compute

    zone = compute.get_vm_zone(vm_id)
    # Synchronous GCE RPC — keep it off the event loop.
    serial_tail = await asyncio.to_thread(
        compute._capture_serial_for_vm, vm_id, zone
    )
    if not serial_tail:
        return

    forensics = {
        "reason": reason,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "vm_instance_id": vm_id,
        "zone": zone,
        "serial_tail": serial_tail,
    }

    async with AsyncSessionLocal() as s:
        task = (
            await s.execute(select(Task).where(Task.vm_instance_id == vm_id).limit(1))
        ).scalar_one_or_none()
        if task is None:
            return
        # Preserve any existing context keys; reassign a new dict + flag_modified
        # so SQLAlchemy persists the JSONB mutation.
        new_context = dict(task.context or {})
        new_context["teardown_forensics"] = forensics
        task.context = new_context
        flag_modified(task, "context")
        await s.commit()


async def _ensure_vm_deleted(
    task_or_vm_id, *, reason: str = "terminal_transition"
) -> bool:
    """Guarantee VM deletion for any terminal state transition.

    Accepts either a Task object or a raw vm_instance_id string.
    Retries up to 3 times with exponential backoff.
    Returns True if deletion succeeded or was unnecessary.
    """
    global _last_gcp_vm_count

    if isinstance(task_or_vm_id, Task):
        vm_id = task_or_vm_id.vm_instance_id
        task_id = str(task_or_vm_id.id)
    else:
        vm_id = task_or_vm_id
        task_id = "unknown"

    if not vm_id:
        return True  # No VM to delete

    # Runner guard: vm_instance_ids prefixed 'runner:' are logical names, not
    # GCE instance names.  There is no VM to delete — skip GCP calls entirely.
    # Also drop any doomed marker a caller pre-recorded: the runner is a
    # persistent process that stays alive across task completions, and a
    # lingering marker makes next_role refuse every warm-claim from it for
    # _ORCHESTRATOR_DELETE_TTL while dispatch keeps pinning tasks to it —
    # each pin then heartbeat-reaps at ~380s and requeues to the same runner
    # (production 2026-07-11 runner:dd59526e, 10 tasks reaped per cycle).
    if vm_id.startswith("runner:"):
        _orchestrator_deleted_vms.pop(vm_id, None)
        logger.info(
            "[VM CLEANUP] Skipping GCP teardown for runner-pinned task (vm_id=%s, task=%s) "
            "— no GCE instance exists for runner tasks",
            vm_id,
            task_id,
        )
        return True

    # Warm-pool guard: a live pool member that just finished a task is reconciled
    # by the pool lifecycle (returned to IDLE for re-lease, or retired by the
    # maintainer which also clears its secret + registry row). Tearing its VM down
    # here would make every member single-use and strand the registry row. Skip,
    # and drop any orchestrator-deleted marker fire_and_forget_vm_delete
    # pre-recorded — the VM is staying alive, so a later DELETED observation must
    # not be mis-attributed. RETIRING members are NOT governed (see helper) so the
    # pool's own retire path still deletes them here.
    if await _vm_is_governed_pool_member(vm_id):
        _orchestrator_deleted_vms.pop(vm_id, None)
        logger.info(
            "[WARM-POOL] VM %s is a live pool member — skipping completion teardown "
            "(task=%s); pool lifecycle governs its return-to-IDLE / retire",
            vm_id,
            task_id,
        )
        return True

    logger.info(f"[VM CLEANUP] Deleting VM {vm_id} (task={task_id}, reason={reason})")
    # Record the orchestrator-initiated delete BEFORE attempting the GCP
    # call. Even if the call fails partway and the VM ends up in TERMINATED
    # rather than DELETED, the marker still tells the reaper this was an
    # orchestrator action — preventing a spot_preemption misclassification
    # if the VM is later cleaned up by another path.
    _orchestrator_deleted_vms[vm_id] = (time.time(), reason)

    # Best-effort serial-console forensics BEFORE the delete loop: once the VM
    # is deleted (or even stopped) its serial output is unrecoverable. Time-boxed
    # and fully guarded so a hung/erroring capture never blocks the teardown.
    try:
        await asyncio.wait_for(
            _capture_and_persist_teardown_forensics(vm_id, reason),
            _TEARDOWN_FORENSICS_TIMEOUT,
        )
    except Exception as e:
        logger.warning(
            "[VM CLEANUP] teardown forensics capture failed/timed out for VM %s "
            "(%s) — proceeding with teardown",
            vm_id,
            type(e).__name__,
        )

    for attempt in range(3):
        try:
            result = await delete_agent_vm(vm_id)
            if result:
                logger.info(f"[VM CLEANUP] Deleted VM {vm_id}")
                if _last_gcp_vm_count > 0:
                    _last_gcp_vm_count -= 1
                    logger.info(
                        f"[VM CLEANUP] Decremented cached VM count to {_last_gcp_vm_count}"
                    )
                return True
            else:
                logger.warning(
                    f"[VM CLEANUP] delete_agent_vm returned False for {vm_id} (attempt {attempt + 1}/3)"
                )
        except Exception as e:
            logger.error(
                f"[VM CLEANUP] Failed to delete VM {vm_id} (attempt {attempt + 1}/3): {e}",
                exc_info=True,
            )

        if attempt < 2:
            await asyncio.sleep(2**attempt)  # 1s, 2s backoff

    logger.error(
        f"[VM CLEANUP] FAILED to delete VM {vm_id} after 3 attempts — will rely on cleanup sweep"
    )
    return False


# Strong references for fire-and-forget VM-delete tasks. asyncio.create_task
# returns a weak handle — without holding a reference here, GC may collect
# the coroutine mid-flight and the GCP delete is silently dropped.
_background_vm_deletes: set[asyncio.Task] = set()


def fire_and_forget_vm_delete(
    task_or_vm_id, *, reason: str = "terminal_transition"
) -> None:
    """Schedule _ensure_vm_deleted in the background; release the caller's
    row lock immediately.

    Use from terminal-status request handlers (e.g. /api/agent/status). The
    awaited form blocks the FOR UPDATE row lock for 60-90s during GCP I/O,
    causing concurrent agent POSTs on the same row to receive 429 and
    risking a lost-COMPLETED-commit if Cloud Run terminates the request
    mid-block. Production incident 2026-05-05 task 04605f9d: agent posted
    APPROVE verdict, handler held lock through GCP delete, retry got 429,
    status update never committed, reaper swept work that had already
    succeeded and re-ran the entire review.

    The orchestrator-deleted marker is recorded synchronously by
    _ensure_vm_deleted (line above) before its first await, so the reaper
    sees the orchestrator's intent immediately even if the background
    delete is delayed by event-loop pressure. Cleanup-sweep is the safety
    net if the background task is dropped (Cloud Run shutdown mid-flight).
    """
    if isinstance(task_or_vm_id, Task):
        vm_id = task_or_vm_id.vm_instance_id
    else:
        vm_id = task_or_vm_id
    if not vm_id:
        return
    # Persistent runner pseudo-VMs ('runner:<id>') are never deleted —
    # _ensure_vm_deleted no-ops on them — so never record the doomed marker
    # for one. The marker means "a VM delete is in flight; don't claim into
    # it", which is never true for a runner, and a stale marker starves the
    # runner's next-role claims for _ORCHESTRATOR_DELETE_TTL after every
    # completion while dispatch keeps pinning work to it (dispatch → refused
    # claim → heartbeat reap → requeue loop, production 2026-07-11).
    if vm_id.startswith("runner:"):
        return
    # Record marker synchronously before scheduling. _ensure_vm_deleted
    # also writes this on its first non-await line, but writing here too
    # closes the window where Cloud Run terminates the worker between
    # create_task() and the event loop picking up the coroutine — a peer
    # observing vm_status=DELETED during that window would otherwise
    # misclassify the orchestrator action as spot-preemption.
    _orchestrator_deleted_vms[vm_id] = (time.time(), reason)
    bg = asyncio.create_task(_ensure_vm_deleted(vm_id, reason=reason))
    _background_vm_deletes.add(bg)
    bg.add_done_callback(_background_vm_deletes.discard)


async def _vm_claimed_backlog_work(vm_id: str) -> bool:
    """True iff a task is currently running on *vm_id* (claimed during the hold).

    On query failure returns False so the caller falls through to teardown —
    never leak a VM by assuming it was reused. A claim flips a QUEUED task to
    PROVISIONING/IN_PROGRESS (and EVALUATING once it posts a verdict), so any of
    those bound to this VM means the agent picked up backlog work.
    """
    try:
        async with AsyncSessionLocal() as s:
            claimed = (
                await s.execute(
                    select(Task.id)
                    .where(
                        Task.vm_instance_id == vm_id,
                        Task.status.in_(
                            [
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                            ]
                        ),
                    )
                    .limit(1)
                )
            ).first()
        return claimed is not None
    except Exception:
        logger.exception(
            "[BACKLOG-REUSE] Deferred-teardown re-check failed for VM %s — "
            "treating as unclaimed",
            vm_id,
        )
        return False


async def _deferred_backlog_teardown(vm_id: str, reason: str) -> None:
    """Hold a just-idle VM's teardown so it can claim backlog work — bounded and
    activity-gated.

    Backlog reuse (efficiency direction d-91d8dede): when a COMPLETED task's VM
    has eligible QUEUED backlog work in its project, the agent re-enters its
    /api/agent/next-role poll loop and keeps polling for up to
    next_role_poll_timeout_secs (~180s). The original fixed
    ``warm_backlog_teardown_defer_secs`` hold (~20s) tore the VM down long before
    the agent stopped polling, so same-project work that arrived a little after
    completion was never claimed warm. This holds the VM through a longer window
    so those existing polls can claim it:

      * an unconditional minimum hold of ``_teardown_defer_secs()`` covers the
        first poll round-trip (before any heartbeat is recorded);
      * after that the hold is **activity-gated** — it is extended one poll
        interval at a time only while the VM is still heartbeating
        (``is_vm_alive`` reads the heartbeat the /next-role poll records), and
        released early once the agent goes quiet for ``_grace_idle_cutoff_secs``
        (gave up / process exited / VM preempted) so an abandoned VM is not held
        idle to the ceiling;
      * the hold is **bounded** by ``_grace_window_secs()`` (floored at the
        minimum hold) so a still-heartbeating-but-never-claiming VM cannot be
        held indefinitely.

    The VM is deleted iff it never claimed anything before the gate/ceiling
    released the hold. Each loop turn re-checks for a claim first, and one final
    re-check runs immediately before deletion, so a claim landing late in the
    window still wins.

    Crucially this does NOT pre-record the orchestrator-deleted marker (unlike
    fire_and_forget_vm_delete): that marker makes is_orchestrator_initiated_delete
    return True, and the /next-role doomed-VM guard would then refuse the VM's
    own warm claim. The marker is only written if/when we actually delete (inside
    _ensure_vm_deleted, synchronously before its first await).
    """
    from app.warm_backlog_reuse import (
        _grace_idle_cutoff_secs,
        _grace_poll_interval_secs,
        _grace_window_secs,
        _teardown_defer_secs,
        record_grace_ceiling_release,
        record_grace_idle_release,
        record_grace_window_extension,
    )
    from app.warm_vm_state import is_vm_alive

    min_hold = _teardown_defer_secs()
    # Ceiling can never undercut the unconditional minimum hold.
    ceiling = max(_grace_window_secs(), min_hold)
    idle_cutoff = _grace_idle_cutoff_secs()
    poll_interval = _grace_poll_interval_secs()

    # Unconditional minimum hold: give the agent's first poll round-trip a chance
    # to record a heartbeat before activity-gating begins.
    await asyncio.sleep(min_hold)
    elapsed = float(min_hold)

    release_reason = "ceiling"
    while True:
        if await _vm_claimed_backlog_work(vm_id):
            logger.info(
                "[BACKLOG-REUSE] VM %s claimed backlog work during grace window "
                "(after %.0fs) — skipping teardown",
                vm_id,
                elapsed,
            )
            return
        if elapsed >= ceiling:
            release_reason = "ceiling"
            break
        # Activity gate: only keep holding while the agent is still polling.
        if not is_vm_alive(vm_id, max_age_seconds=idle_cutoff):
            release_reason = "idle"
            break
        record_grace_window_extension()
        # Don't overshoot the ceiling on the last interval.
        step = min(float(poll_interval), ceiling - elapsed)
        await asyncio.sleep(step)
        elapsed += step

    # Final re-check: a claim may have landed between the last loop check and
    # here. Deletion records the orchestrator marker synchronously before its
    # first await, so no poll can interleave between this check and the marker.
    if await _vm_claimed_backlog_work(vm_id):
        logger.info(
            "[BACKLOG-REUSE] VM %s claimed backlog work at grace-window close — "
            "skipping teardown",
            vm_id,
        )
        return

    if release_reason == "idle":
        record_grace_idle_release()
        logger.info(
            "[BACKLOG-REUSE] VM %s stopped polling within grace window "
            "(%.0fs, no heartbeat for >%ds) — tearing down",
            vm_id,
            elapsed,
            idle_cutoff,
        )
    else:
        record_grace_ceiling_release()
        logger.info(
            "[BACKLOG-REUSE] VM %s claimed nothing within grace ceiling "
            "(%.0fs) — tearing down",
            vm_id,
            elapsed,
        )
    await _ensure_vm_deleted(vm_id, reason=reason)


def schedule_deferred_backlog_teardown(
    vm_id: str, *, reason: str = "agent_completion"
) -> None:
    """Fire-and-forget the deferred-teardown coroutine (see above)."""
    if not vm_id:
        return
    bg = asyncio.create_task(_deferred_backlog_teardown(vm_id, reason))
    _background_vm_deletes.add(bg)
    bg.add_done_callback(_background_vm_deletes.discard)


async def _get_actual_gcp_vm_count() -> int:
    """Count actual agent-vm-* instances across all GCP zones.
    Cached for _GCP_VM_CHECK_INTERVAL seconds to avoid API spam.
    Updated passively by the cleanup service, with active refresh as fallback.
    Compute-job VMs (compute-job-vm-*) are excluded — they do not consume
    agent dispatch slots."""
    global _last_gcp_vm_count, _last_gcp_vm_check

    now = time.time()
    if now - _last_gcp_vm_check < _GCP_VM_CHECK_INTERVAL:
        return _last_gcp_vm_count

    from app.compute import FALLBACK_ZONES, _list_vms_in_zone, LOCAL_MODE

    if LOCAL_MODE:
        from app.compute import _local_processes

        _last_gcp_vm_count = len(_local_processes)
        _last_gcp_vm_check = now
        return _last_gcp_vm_count

    try:
        unique_zones = list({z for z, _ in FALLBACK_ZONES})
        zone_tasks = [
            asyncio.to_thread(_list_vms_in_zone, zone) for zone in unique_zones
        ]
        zone_results = await asyncio.wait_for(
            asyncio.gather(*zone_tasks, return_exceptions=True),
            timeout=10,  # Short timeout — cache is usually fresh from cleanup service
        )
        total = 0
        for zr in zone_results:
            if isinstance(zr, list):
                total += sum(1 for name, _, _ in zr if name.startswith("agent-vm-"))
        _last_gcp_vm_count = total
        _last_gcp_vm_check = now
        return total
    except Exception as e:
        logger.warning(f"[GCP] Failed to count VMs: {e}")
        return _last_gcp_vm_count  # Return stale count on failure
