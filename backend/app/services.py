"""Background service functions: GCP cleanup, secret reconciliation, and orchestrator helpers."""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import func, update, text
from sqlmodel import select

from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import (
    Task,
    TaskStatus,
)
import app.shared_state as _shared_state

# broadcast_to_project and WRITE_TASK_TYPES are kept imported here (no longer
# used directly in this module after the recovery-sweep extraction) because
# app.recovery_sweeps references them via ``services.broadcast_to_project`` /
# ``services.WRITE_TASK_TYPES`` so test patches against ``app.services.<name>``
# keep taking effect on the moved sweeps.
from app.broadcast import broadcast_to_project  # noqa: F401
from app.task_capabilities import WRITE_TASK_TYPES  # noqa: F401
from app.time_utils import _coerce_naive_utc, _utcnow

logger = logging.getLogger(__name__)


# ── Cluster C3: bound the leader-cycle recovery sweeps ────────────────────────
# These three sweeps (_check_evaluating_timeouts, _recover_orphaned_tasks,
# _sweep_post_commit_pending) run inline on the orchestrator's monitoring tail
# and each performs serial GitHub / post-commit I/O per row. Under a large
# EVALUATING/orphan/post-commit backlog the unbounded fan-out is O(N) serial
# network work, which is exactly the leader-loop saturation the 2026-05-30
# watchdog-kill incident traced to (reaper/orchestrator cycle wall-time grew
# past the liveness window mid-I/O). We bound them with:
#   • a per-cycle row cap (deferred rows re-qualify next cycle — the scans are
#     status-based, no separate deferral queue needed; mirrors the reaper's
#     _REAPER_MAX_ORPHANS_PER_CYCLE), and
#   • a per-call asyncio.wait_for around every GitHub / post-commit await, so a
#     single hung network call can never blow the whole sweep past the watchdog
#     budget. A timeout on one row is logged and skipped; the next sweep retries.
# The orchestrator heartbeat is pumped per row by the caller-side loops below.
_EVAL_TIMEOUT_MAX_PER_CYCLE = 200
_ORPHAN_RECOVERY_MAX_PER_CYCLE = 100
# Per-call ceilings (seconds). Generous enough for a healthy GitHub round-trip
# (typ. <2s, p99 ~10-15s) but well under the orchestrator's 180s hang window.
_GH_CALL_TIMEOUT_S = 30.0
# _process_post_commit has its own internal 240s budget; this is a hard outer
# bound so a wedged inline pass (e.g. a GitHub call that never returns) can't
# pin the post-commit sweep indefinitely. Set just above the inner budget.
_POST_COMMIT_CALL_TIMEOUT_S = 260.0
# C6 rework (C): hard outer bound on post-commit sweep retries. The 5-attempt
# cap (MAX_ATTEMPTS, local to _sweep_post_commit_pending) only counts PERSISTENT
# failures, so a persistent failure whose error string merely CONTAINS a
# transient substring ("429"/"quota"/"lock_timeout") is forever classified
# transient and would re-run every sweep forever. ``total_attempts`` increments
# on EVERY retry and terminalizes past this ceiling. Module-level so tests can
# assert against the exact bound. Far above MAX_ATTEMPTS so a genuine multi-hour
# transient outage still gets ~20 minutes of 60s sweeps to recover.
MAX_TOTAL_ATTEMPTS = 20


class _DeployPauseSnapshot:
    """Plain (session-free) snapshot of the DeploymentState pause fields that
    ``pause_clock.total_paused_seconds`` / ``active_elapsed`` read.

    Audit P1 (session hygiene): the EVALUATING-timeout sweep now opens a fresh
    session per row (so a _bounded cancellation on one row can't poison the
    whole tick). The pause clock needs DeploymentState; rather than reuse the
    cycle-read session's ORM instance across per-row sessions (which would risk
    a cross-session lazy-load / MissingGreenlet), we re-fetch it per row and
    keep this attribute-compatible snapshot only as a fallback if the singleton
    row went away mid-cycle. Exposes exactly the attributes pause_clock reads.
    """

    __slots__ = (
        "paused_at",
        "total_auto_paused_seconds",
        "total_manual_paused_seconds",
    )

    def __init__(
        self, paused_at, total_manual_paused_seconds, total_auto_paused_seconds
    ):
        self.paused_at = paused_at
        self.total_manual_paused_seconds = total_manual_paused_seconds
        self.total_auto_paused_seconds = total_auto_paused_seconds

    @classmethod
    def from_state(cls, state) -> "_DeployPauseSnapshot":
        return cls(
            paused_at=state.paused_at,
            total_manual_paused_seconds=state.total_manual_paused_seconds,
            total_auto_paused_seconds=state.total_auto_paused_seconds,
        )


async def _bounded(coro, timeout_s: float, *, what: str):
    """Await ``coro`` with a hard ``timeout_s`` ceiling.

    On timeout, cancels the coroutine and re-raises ``asyncio.TimeoutError`` so
    the caller's per-row try/except logs-and-skips that one row rather than
    letting a single hung network call saturate the whole leader-cycle sweep
    (cluster C3). ``what`` is a short label for the log line on timeout.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning(
            "[C3 BOUNDED] %s exceeded %.0fs ceiling — skipping this row this cycle",
            what,
            timeout_s,
        )
        raise


def _parse_naive_utc_iso(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _coerce_naive_utc(parsed)


async def _collect_gcp_cleanup_active_vm_ids(session) -> set[str]:
    """Return the VM ids that should survive the generic GCP orphan sweep."""
    active_vm_ids = (
        await session.execute(
            select(Task.vm_instance_id).where(
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
            ).limit(5000)
        )
    ).scalars().all()
    active_vm_ids = {vid for vid in active_vm_ids if vid}

    # Warm-handoff successors pin their source VM via QUEUED context; keep the
    # source alive until the deadline elapses so the successor can claim it
    # warm instead of cold-provisioning.
    if resolve_dialect_name(session) == "postgresql":
        warm_target_present = Task.context.has_key("warm_target_vm_id")
    else:
        warm_target_present = func.json_extract(
            Task.context, "$.warm_target_vm_id"
        ).isnot(None)
    warm_handoff_rows = (
        await session.execute(
            select(Task.context).where(
                Task.status == TaskStatus.QUEUED,
                warm_target_present,
            )
        )
    ).scalars().all()
    now = _utcnow()
    warm_handoff_vm_ids: set[str] = set()
    for ctx in warm_handoff_rows:
        if not isinstance(ctx, dict):
            continue
        target = ctx.get("warm_target_vm_id")
        if not isinstance(target, str) or not target:
            continue
        deadline = _parse_naive_utc_iso(
            ctx.get("warm_handoff_deadline")
            if isinstance(ctx.get("warm_handoff_deadline"), str)
            else None
        )
        if deadline is not None and deadline > now:
            warm_handoff_vm_ids.add(target)

    # Spare live warm-pool members: a pre-provisioned idle pool VM has NO Task
    # row, so without this it would be deleted within minutes.
    # RETIRING members are excluded (get_live_pool_vm_ids) so genuinely
    # orphaned/retiring pool VMs are still reclaimed. Gated on the feature flag:
    # when warm_pool_enabled is off BOTH pool reapers (maintainer + idle sweep)
    # are inert, so exempting members here would leak them (and their billed GCP
    # VMs) forever. With the gate off, this generic cleanup reclaims the
    # now-unmanaged members.
    from app.warm_pool import pool_vm_cleanup_exemptions

    # Compute-job VMs (compute-job-vm-*) have NO Task row; the orphan sweep now
    # inventories them, so a RUNNING job's VM must be held active here or it
    # would be deleted out from under the monitor.
    from app.compute_jobs import active_compute_job_vm_ids

    # Parked tasks are QUEUED (not PROVISIONING/IN_PROGRESS) but still hold a
    # live VM during the park-and-retarget cooldown window.
    # Without this exemption the orphan sweep would delete the held VM within 5
    # minutes — well inside the up-to-1800s park budget.
    parked_rows = (
        await session.execute(
            select(Task.vm_instance_id, Task.context).where(
                Task.status == TaskStatus.QUEUED,
                Task.vm_instance_id.is_not(None),
            )
        )
    ).all()
    parked_vm_ids = {
        vid
        for vid, ctx in parked_rows
        if vid
        and isinstance(ctx, dict)
        and ctx.get("parked_for_retarget")
    }

    return (
        active_vm_ids
        | warm_handoff_vm_ids
        | await pool_vm_cleanup_exemptions(session)
        | await active_compute_job_vm_ids(session)
        | parked_vm_ids
    )


async def gcp_cleanup_service():
    """
    Background worker that runs every 5 minutes to ensure GCP VMs are in sync with the database.
    Deletes any VMs prefixed with 'agent-vm-' that do not have an active task.
    Zone-parallel with per-cycle timeout.
    """
    from app.compute import cleanup_orphaned_vms

    while True:
        try:
            async with AsyncSessionLocal() as session:
                active_vm_ids = list(await _collect_gcp_cleanup_active_vm_ids(session))

                # Run cleanup with a hard timeout. 270s, not 120: deleting a
                # RUNNING/STOPPING orphan blocks through the guest shutdown
                # (~90-150s each), so a cycle with 2-3 such orphans routinely
                # overran the old 120s budget — the deletes still landed
                # server-side, but every overrun logged a full traceback and
                # left the _last_gcp_vm_count cache stale (prod 2026-06-07,
                # 4 cycles in one morning). 270s still fits inside the 300s
                # cycle cadence.
                try:
                    cleanup_result = await asyncio.wait_for(
                        cleanup_orphaned_vms(active_vm_ids), timeout=270
                    )
                    # Log divergence and partial failures from structured result
                    if hasattr(cleanup_result, "failed") and cleanup_result.failed:
                        logger.warning(
                            f"[GCP CLEANUP] Partial failure: deleted {len(cleanup_result.deleted)}, "
                            f"failed {len(cleanup_result.failed)}: {cleanup_result.failed}"
                        )
                    # Cache agent-VM count for orchestrator concurrency check
                    # (compute-job VMs are excluded — they don't consume dispatch slots)
                    if hasattr(cleanup_result, "agent_vm_count"):
                        import app.vm_lifecycle as _vm_lifecycle

                        _vm_lifecycle._last_gcp_vm_count = cleanup_result.agent_vm_count
                        _vm_lifecycle._last_gcp_vm_check = time.time()
                except asyncio.TimeoutError:
                    logger.exception("[GCP CLEANUP] Cycle timed out after 270s")

        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[GCP CLEANUP] Error: {e}", exc_info=True)

        try:
            await asyncio.sleep(300)  # Run every 5 minutes
        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break


async def gcp_image_cleanup_service():
    """Hourly pruner for the agent VM image family.

    Packer adds one new image to the `blueprint-agent` family per build; without pruning,
    these accumulate until GCP's per-project image quota is exhausted and VM provisioning
    starts failing. This service keeps only the `max_retained_images` most recent.
    """
    from app.compute import cleanup_old_images, AGENT_IMAGE_FAMILY
    from app.shared_state import runtime_config as _runtime_config

    # Initial delay so startup isn't racing image validation and DB init.
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("[IMAGE CLEANUP] Service shutting down during initial delay.")
        return

    while True:
        try:
            keep = int(_runtime_config.get("max_retained_images", 10))
            if keep < 1:
                logger.warning(
                    f"[IMAGE CLEANUP] max_retained_images={keep} invalid; skipping cycle"
                )
            else:
                result = await asyncio.wait_for(
                    cleanup_old_images(keep=keep, family=AGENT_IMAGE_FAMILY),
                    timeout=600,
                )
                if result.get("deleted") or result.get("failed"):
                    logger.info(
                        f"[IMAGE CLEANUP] total={result.get('total')} deleted={result.get('deleted')} "
                        f"kept={result.get('kept')} failed={len(result.get('failed') or [])}"
                    )
        except asyncio.CancelledError:
            logger.info("[IMAGE CLEANUP] Service shutting down.")
            break
        except asyncio.TimeoutError:
            logger.exception("[IMAGE CLEANUP] Cycle timed out after 600s")
        except Exception as e:
            logger.error(f"[IMAGE CLEANUP] Error: {e}", exc_info=True)

        try:
            await asyncio.sleep(3600)  # Hourly — images accumulate slowly
        except asyncio.CancelledError:
            logger.info("[IMAGE CLEANUP] Service shutting down.")
            break


async def account_availability_refresh_service():
    """Periodically refresh stale per-account model-availability caches.

    Audit 2026-04-25 finding E13: ``account_available_models`` was only
    refreshed by manual admin action. A key that loses preview access
    (or is added without preview) stays in dispatch rotation for hours,
    burning slots before the runtime 404 reroutes them. Sweeping every
    30 min bounds the staleness without spamming the provider.
    """
    from app.account_model_availability import (
        account_availability_is_stale,
        refresh_account_available_models,
    )
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock
    from app.models import MaxPlanAccount

    while True:
        try:
            async with _max_plan_pool_lock:
                stale_ids = [
                    acct["id"]
                    for acct in _account_by_id.values()
                    if account_availability_is_stale(acct)
                ]
            if stale_ids:
                logger.info(
                    f"[AVAILABILITY] Refreshing {len(stale_ids)} stale account(s)"
                )
                refreshed = 0
                for acct_id in stale_ids:
                    acct = _account_by_id.get(acct_id)
                    if not acct:
                        continue
                    try:
                        result = await asyncio.wait_for(
                            refresh_account_available_models(acct),
                            timeout=30,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[AVAILABILITY] Refresh failed for {acct_id}: {e}"
                        )
                        continue
                    # refresh_account_available_models mutates acct['provider_metadata']
                    # in place (success or error). Persist the snapshot so a restart
                    # doesn't lose the cached list / error string.
                    metadata = acct.get("provider_metadata") or {}
                    try:
                        async with AsyncSessionLocal() as ps:
                            row = await ps.get(MaxPlanAccount, uuid.UUID(acct_id))
                            if row is not None:
                                row.provider_metadata = dict(metadata) or None
                                ps.add(row)
                                await ps.commit()
                    except Exception as e:
                        logger.warning(
                            f"[AVAILABILITY] Persist failed for {acct_id}: {e}"
                        )
                        continue
                    if result is not None:
                        refreshed += 1
                logger.info(
                    f"[AVAILABILITY] Refreshed {refreshed}/{len(stale_ids)} accounts"
                )
        except asyncio.CancelledError:
            logger.info("[AVAILABILITY] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[AVAILABILITY] Sweeper error: {e}", exc_info=True)
        try:
            await asyncio.sleep(1800)  # Run every 30 minutes
        except asyncio.CancelledError:
            logger.info("[AVAILABILITY] Service shutting down.")
            break


async def secret_reconciliation_service():
    """Periodic cleanup of orphaned agent credential secrets (every 30 min)."""
    from app.compute import reconcile_orphaned_secrets

    while True:
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                ).limit(5000)
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = {vid for vid in active_vm_ids if vid}
                # Spare the pre-staged secrets of live warm-pool members — their
                # boot manifest was stored ahead of any task. RETIRING members are
                # excluded so truly orphaned pool secrets are still reclaimed.
                # Gated on the feature flag for the same reason as the GCP cleanup
                # above: with warm_pool_enabled off no pool reaper retires members,
                # so exempting their secrets would leak them indefinitely.
                from app.warm_pool import pool_vm_cleanup_exemptions

                active_vm_ids |= await pool_vm_cleanup_exemptions(session)

            recon_result = await asyncio.wait_for(
                reconcile_orphaned_secrets(active_vm_ids), timeout=120
            )
            if recon_result and recon_result.get("deleted"):
                logger.info(f"[SECRET RECONCILIATION] {recon_result}")
        except asyncio.CancelledError:
            logger.info("[SECRET RECONCILIATION] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[SECRET RECONCILIATION] Error: {e}", exc_info=True)

        try:
            await asyncio.sleep(1800)  # Run every 30 minutes
        except asyncio.CancelledError:
            logger.info("[SECRET RECONCILIATION] Service shutting down.")
            break


# Stale-branch cleanup (config constants, regex helpers, PR-open detection,
# per-project GitHub-token resolution, the sweep, and its service loop) now
# lives in ``app.branch_cleanup``. Re-exported here so existing
# ``from app.services import ...`` call sites (lifecycle service loop, the
# analytics token helper, the models default-protected-branches constant) and
# test patch targets that pre-date the split keep resolving — the same
# re-export pattern ``github.py`` adopted after its own split.
from app.branch_cleanup import (  # noqa: F401
    _DEFAULT_PROTECTED_BRANCHES,
    _MAX_DELETES_PER_PROJECT,
    _MAX_DELETES_TOTAL,
    _STALE_BRANCH_AGE_DAYS,
    _QUOTA_FLOOR,
    _JWT_QUOTA_FLOOR,
    _DEFAULT_STAGING_PREFIXES,
    _build_branch_short_id_re,
    _BRANCH_SHORT_ID_RE,
    _TERMINAL_TASK_STATUSES,
    _extract_branch_short_id,
    _pr_possibly_open,
    _get_project_github_token,
    _run_stale_branch_cleanup,
    stale_branch_cleanup_service,
)


# Canonical branch-derivation helpers live in shared_state, the single source
# of truth for blueprint/{prefix}-{8hex} branch names. Every backend module
# that needs to derive a task branch imports from there.
_TASK_TYPE_BRANCH_PREFIX = _shared_state.TASK_TYPE_BRANCH_PREFIX
_derive_task_branch = _shared_state.derive_task_branch


_AUTO_ARCHIVE_BATCH_SIZE = 500


async def _run_auto_archive_cycle() -> Dict[str, int]:
    """Archive terminal tasks via two caps: age threshold and per-project count.

    A task is archived if EITHER:
      - its updated_at is older than auto_archive_terminal_after_days, OR
      - its project's unarchived-terminal backlog exceeds
        auto_archive_max_terminal_tasks_per_project (keep the N most recent).

    Both passes are batched + SKIP LOCKED so the update never blocks on rows
    held by the orchestrator. Returns {"age": n, "count": n} for logging.
    """
    age_days = _shared_state.runtime_config.get("auto_archive_terminal_after_days", 7)
    per_project_cap = int(
        _shared_state.runtime_config.get(
            "auto_archive_max_terminal_tasks_per_project", 0
        )
        or 0
    )
    cutoff = _utcnow() - timedelta(days=age_days)
    # Task.updated_at is TIMESTAMP WITHOUT TIME ZONE; asyncpg rejects tz-aware
    # comparands. See get_all_tasks's created_after handling for the same fix.
    if cutoff.tzinfo is not None:
        cutoff = cutoff.replace(tzinfo=None)

    terminal = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
    age_total = 0
    count_total = 0

    # --- Pass 1: age-based ---
    # Audit Phase C4 (M-C5): open a FRESH session per batch instead of holding
    # one session for the entire cycle. A long-lived session pins a connection
    # from the pool for the full archive run (potentially minutes if the backlog
    # is large), and an unhandled error mid-loop leaks the connection until GC
    # closes it. Per-batch sessions return the connection to the pool between
    # iterations and cap the blast radius of any one batch's failure.
    while True:
        async with AsyncSessionLocal() as session:
            # Audit Phase C2 (H-C3): bulk lock fan-out is acceptable here — every
            # row we lock is terminal (COMPLETED/FAILED/CANCELLED), and the only
            # mutation inside the lock window is a flat UPDATE that flips
            # archived=True. No external calls (GitHub, GCE, secret manager) run
            # while the lock is held, so the lock window is bounded by Postgres
            # round-trip time. Keep skip_locked=True so a concurrent writer on
            # one row doesn't stall the rest of the batch.
            select_stmt = (
                select(Task.id)
                .where(
                    Task.archived.is_(False),
                    Task.status.in_(terminal),
                    Task.updated_at < cutoff,
                )
                .limit(_AUTO_ARCHIVE_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
            ids = (await session.execute(select_stmt)).scalars().all()
            if not ids:
                break
            # Wave F4 C4: bulk Core UPDATEs bypass `version_id_col`. Even though
            # F1 dropped the mapper-level enforcement, we keep the version
            # column as a forensic counter — bumping it here preserves
            # "concurrent writes can be detected by version drift" semantics.
            await session.execute(
                update(Task)
                .where(Task.id.in_(ids))
                .values(
                    archived=True,
                    version=Task.version + 1,
                )
            )
            await session.commit()
            age_total += len(ids)
            if len(ids) < _AUTO_ARCHIVE_BATCH_SIZE:
                break

    # --- Pass 2: per-project count cap ---
    # Window-function ranks unarchived-terminal tasks per project by
    # updated_at DESC; anything beyond rank N is archived. Done as a single
    # query returning all candidate IDs, then batched update with
    # skip_locked so a concurrent writer on one row doesn't block the rest.
    # Same audit-C2 reasoning as Pass 1: no external calls inside lock,
    # bulk lock fan-out is safe.
    if per_project_cap > 0:
        # Bind-param the archived=false literal so asyncpg/aiosqlite each
        # coerce the python bool to their native representation (Postgres
        # boolean vs SQLite integer 0/1); a raw `false` literal is not
        # cross-dialect.
        candidate_sql = text(
            """
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY project_id ORDER BY updated_at DESC, id DESC
                ) AS rn
                FROM task
                WHERE archived = :archived_false
                  AND status IN ('COMPLETED', 'FAILED', 'CANCELLED')
            )
            SELECT id FROM ranked WHERE rn > :cap
            """
        )
        # Audit Phase C4 (M-C5): candidate-ID query gets its own short-lived
        # session — read-only, no lock held during the result iteration below.
        async with AsyncSessionLocal() as session:
            candidate_ids = [
                row[0]
                for row in (
                    await session.execute(
                        candidate_sql, {"cap": per_project_cap, "archived_false": False}
                    )
                ).all()
            ]
        for i in range(0, len(candidate_ids), _AUTO_ARCHIVE_BATCH_SIZE):
            batch = candidate_ids[i : i + _AUTO_ARCHIVE_BATCH_SIZE]
            # Audit Phase C4 (M-C5): fresh session per batch — see Pass 1.
            async with AsyncSessionLocal() as session:
                # Re-acquire row locks with skip_locked before updating so a
                # row held by the orchestrator is deferred, not blocked.
                lockable = (
                    (
                        await session.execute(
                            select(Task.id)
                            .where(Task.id.in_(batch))
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not lockable:
                    continue
                # Wave F4 C4: see Pass 1 note — bump `version` so forensic
                # counter reflects this bulk archive even though the mapper
                # `version_id_col` enforcement was dropped in F1.
                await session.execute(
                    update(Task)
                    .where(Task.id.in_(lockable))
                    .values(
                        archived=True,
                        version=Task.version + 1,
                    )
                )
                await session.commit()
                count_total += len(lockable)

    return {"age": age_total, "count": count_total}


async def auto_archive_service():
    """Periodically archive stale terminal tasks to keep the admin list bounded.

    Disabled when runtime_config['auto_archive_enabled'] is False. Age threshold
    and cadence are operator-tunable (auto_archive_terminal_after_days,
    auto_archive_interval_minutes).
    """
    while True:
        try:
            if _shared_state.runtime_config.get("auto_archive_enabled", True):
                archived = await _run_auto_archive_cycle()
                total = archived["age"] + archived["count"]
                if total:
                    age_days = _shared_state.runtime_config.get(
                        "auto_archive_terminal_after_days", 7
                    )
                    cap = _shared_state.runtime_config.get(
                        "auto_archive_max_terminal_tasks_per_project", 0
                    )
                    logger.info(
                        f"[AUTO-ARCHIVE] Archived {total} task(s): "
                        f"{archived['age']} older than {age_days}d, "
                        f"{archived['count']} over per-project cap of {cap}"
                    )
        except asyncio.CancelledError:
            logger.info("[AUTO-ARCHIVE] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[AUTO-ARCHIVE] Error: {e}", exc_info=True)

        interval_minutes = _shared_state.runtime_config.get(
            "auto_archive_interval_minutes", 60
        )
        try:
            await asyncio.sleep(max(interval_minutes, 1) * 60)
        except asyncio.CancelledError:
            logger.info("[AUTO-ARCHIVE] Service shutting down.")
            break


# ── Leader-cycle recovery sweeps ──────────────────────────────────────────────
# The EVALUATING-timeout / post-commit-pending / orphan-recovery sweeps and their
# helpers were extracted to ``app.recovery_sweeps`` to keep this module below its
# size cap. They are re-exported here so existing ``from app.services import ...``
# call sites (lifecycle/orchestrator service loops) and test patch targets
# (``app.services._check_evaluating_timeouts``, ``_recover_orphaned_tasks``,
# ``_chain_has_active_pipeline_work``, ``_ci_backstop_timeout_reason``, ...) keep
# resolving against ``app.services`` unchanged. The shared bounding infra
# (C3 constants, ``_bounded``, ``_DeployPauseSnapshot``) stays defined above and
# ``recovery_sweeps`` references it back through this module.
from app.recovery_sweeps import (  # noqa: F401
    _QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS,
    _POST_COMMIT_TRANSIENT_SUBSTRINGS,
    _chain_has_active_pipeline_work,
    _chain_has_recoverable_failed_work,
    _chain_has_unexhausted_optimistic_retry_work,
    _summarize_latest_review_for_chain,
    _ci_backstop_timeout_reason,
    _supersede_long_queued_pipeline_tasks,
    _attempt_ci_backstop_recovery,
    _verify_ci_before_backstop,
    _check_evaluating_timeouts,
    _post_commit_retry_is_transient,
    _sweep_post_commit_pending,
    _recover_childless_evaluating_without_marker,
    _sweep_markerless_research_spawn_pending,
    _recover_orphaned_tasks,
    _sweep_stranded_sidecar_sources,
    _sweep_bootstrap_recovery,
    _sweep_lane_baseline_backfill,
)
