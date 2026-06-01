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
from typing import Dict

from sqlmodel import select

from app.compute import delete_agent_vm
from app.database import AsyncSessionLocal
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

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

    logger.info(f"[VM CLEANUP] Deleting VM {vm_id} (task={task_id}, reason={reason})")
    # Record the orchestrator-initiated delete BEFORE attempting the GCP
    # call. Even if the call fails partway and the VM ends up in TERMINATED
    # rather than DELETED, the marker still tells the reaper this was an
    # orchestrator action — preventing a spot_preemption misclassification
    # if the VM is later cleaned up by another path.
    _orchestrator_deleted_vms[vm_id] = (time.time(), reason)

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


async def _deferred_backlog_teardown(vm_id: str, reason: str) -> None:
    """Hold a just-idle VM's teardown briefly so it can claim backlog work.

    Backlog reuse (efficiency direction d-91d8dede): when a COMPLETED task's
    VM has eligible QUEUED backlog work in its project, this defers teardown
    by runtime_config['warm_backlog_teardown_defer_secs'] (read fresh per call)
    so the agent's /next-role poll can claim that work and run it warm. After
    the window, the VM is deleted iff it never claimed anything.

    Crucially this does NOT pre-record the orchestrator-deleted marker (unlike
    fire_and_forget_vm_delete): that marker makes is_orchestrator_initiated_delete
    return True, and the /next-role doomed-VM guard would then refuse the VM's
    own warm claim. The marker is only written if/when we actually delete.
    """
    from app.warm_backlog_reuse import _teardown_defer_secs

    await asyncio.sleep(_teardown_defer_secs())
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
    except Exception:
        # If the re-check query fails, fall through to teardown — never leak a
        # VM by assuming it was reused.
        logger.exception(
            "[BACKLOG-REUSE] Deferred-teardown re-check failed for VM %s — "
            "tearing down",
            vm_id,
        )
        claimed = None
    if claimed is not None:
        logger.info(
            "[BACKLOG-REUSE] VM %s claimed backlog work during defer window — "
            "skipping teardown",
            vm_id,
        )
        return
    logger.info(
        "[BACKLOG-REUSE] VM %s claimed nothing within defer window — tearing down",
        vm_id,
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
    Updated passively by the cleanup service, with active refresh as fallback."""
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
                total += len(zr)
        _last_gcp_vm_count = total
        _last_gcp_vm_check = now
        return total
    except Exception as e:
        logger.warning(f"[GCP] Failed to count VMs: {e}")
        return _last_gcp_vm_count  # Return stale count on failure
