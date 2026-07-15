"""External-I/O helpers for the per-orphan reap loop.

Extracted from reaper.reaper_service()'s per-orphan mutation loop. The
I/O block (GCP VM-status check, account-disable calls, serial-console capture,
GitHub PR-state
lookup) runs deliberately WITHOUT the task row lock held. This module
bundles all four serial I/O calls into _run_orphan_io(), which returns a
frozen OrphanIoSnapshot to the caller. Keeping the lock-free window
explicit makes the I/O independently unit-testable.
"""

import asyncio
import dataclasses
import logging

from app.compute import (
    _capture_serial_for_vm,
    get_provision_error,
    get_vm_zone,
    get_vm_status,
)
from app.compute_cloud_run import is_cloud_run_task
from app.reaper_orphan_classify import _task_has_open_pr

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class OrphanIoSnapshot:
    """Frozen bundle of all I/O results gathered for one orphaned task.

    Produced by _run_orphan_io() and consumed by the mutation phase that
    follows the lock-free I/O window in reaper_service().
    """

    # Pre-IO task snapshot (captured before any external call so the
    # relock can detect peer mutation during the unlocked window).
    pre_io_status: str
    pre_io_last_hb: object  # datetime | None

    # GCP VM-status result
    is_spot_preemption: bool

    # Evidence-based rate-limit / dead-auth classification
    is_rate_limited: bool
    rl_acct_id: str | None
    rl_model: str | None

    # Dead-auth classification
    da_acct_id: str | None

    # Provision-failure serial-console capture
    io_serial_log: str | None
    io_prov_zone: str | None
    io_provision_err: str | None

    # GitHub PR-state lookup
    has_pr_artifact: bool
    pr_num: int | None
    gh_pr_state: str | None
    gh_pr_merged: bool
    gh_pr_base_branch: str | None


async def _run_orphan_io(
    task,
    fresh_task,
    reap_reason_code: str,
    is_provision_failure: bool,
    is_rate_limited: bool,
    is_dead_auth: bool,
    session,
    runtime_config: dict,
) -> OrphanIoSnapshot:
    """Gather all external I/O for one orphaned task while the row lock is NOT held.

    Deferred imports inside this function preserve the circular-import
    safety established in reaper_service() (same pattern as the original
    deferred-import block at the top of that function).

    Returns an OrphanIoSnapshot with all results frozen for the
    mutation phase that follows.
    """
    # Deferred imports — avoid circular dependency with app.main
    from app.max_plan_pool import (
        disable_account_model,
        disable_account_for_auth,
        get_task_llm_account_id,
        _extract_task_model,
    )

    # Capture pre-IO snapshot so the relock can detect peer mutation
    # during the unlocked window. status + last_heartbeat are the two
    # fields the heartbeat path writes; an advance in either signals the
    # agent revived and the reap should be aborted.
    pre_io_status = fresh_task.status
    pre_io_last_hb = fresh_task.last_heartbeat

    # 1. Spot preemption check (GCP API)
    is_spot_preemption = False
    if (
        getattr(fresh_task, "vm_spot", False)
        and fresh_task.vm_instance_id
        and reap_reason_code
        in (
            "heartbeat_timeout_post_claim",
            "provision_timeout_pre_claim",
            # absolute_timeout: a PROVISIONING task that aged past the
            # 15-min absolute ceiling before earlier reaper codes had a
            # chance to classify it (typically because the reaper itself
            # was lagging — e.g., 2026-05-07 MissingGreenlet crash on
            # last_tool_use_at). Without this branch, the spot check is
            # skipped and the account gets blacklisted for what's
            # actually a GCE capacity issue.
            "absolute_timeout",
        )
    ):
        prov_zone_for_status = (task.context or {}).get(
            "provision_zone"
        ) or get_vm_zone(fresh_task.vm_instance_id)
        if prov_zone_for_status:
            try:
                io_vm_status = await asyncio.to_thread(
                    get_vm_status,
                    fresh_task.vm_instance_id,
                    prov_zone_for_status,
                )
                # TERMINATED = preempted by GCP. DELETED = already gone.
                # DELETED is ambiguous: GCP delete-on-preempt OR
                # orchestrator-initiated cleanup (agent_completion,
                # terminal_transition, admin_requeue, etc). Production
                # 2026-05-04: reaper misclassified orchestrator-deleted
                # VMs as preemptions, inflating spot_preemption_count for
                # tasks that were really completed but whose status POST
                # 500'd during a cutover.
                # Fix: consult the orchestrator-initiated-delete tracker.
                # If the VM was deleted by us within the last 30min,
                # it's NOT a preemption.
                from app.vm_lifecycle import (
                    is_orchestrator_initiated_delete,
                )

                io_orch_delete_info = is_orchestrator_initiated_delete(
                    fresh_task.vm_instance_id
                )
                if io_orch_delete_info[0]:
                    logger.info(
                        f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                        f"vm_status={io_vm_status} but orchestrator-initiated delete "
                        f"(reason={io_orch_delete_info[1]}) — NOT classifying as preemption"
                    )
                elif io_vm_status in ("TERMINATED", "DELETED"):
                    is_spot_preemption = True
                    logger.warning(
                        f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                        f"in {prov_zone_for_status} — vm_status={io_vm_status}, treating "
                        f"as preemption (skipping attempt_count++)"
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    f"[REAPER] get_vm_status failed for {fresh_task.vm_instance_id}: {e}"
                )

    # 2. Rate-limited account-disable I/O (called only when is_rate_limited
    #    AND not is_spot_preemption; mirrors the original elif chain where
    #    the rate-limited branch was skipped if spot preempt fired first).
    #    Side-effect-only — disables the account/model in the in-memory
    #    pool + persists to DB via its own internal session.
    rl_acct_id: str | None = None
    rl_model: str | None = None
    if is_rate_limited and not is_spot_preemption:
        rl_acct_id = get_task_llm_account_id(task)
        logger.info(
            f"[REAPER] Task {task.id} rate-limited (account={rl_acct_id}) — "
            f"requeuing immediately with account blacklisted"
        )
        # Disable the exhausted model on the account so the orchestrator's
        # blacklist pruning preserves this entry on the next dispatch cycle.
        rl_model = _extract_task_model(task)
        if not rl_model:
            _rl_task_type = (task.first_payload or {}).get("type", "create")
            rl_model = runtime_config.get("task_type_models", {}).get(_rl_task_type)
        if rl_acct_id and rl_model:
            _reaper_ctx_io = task.context or {}
            _reaper_kwargs_io = {
                "retry_after_seconds": _reaper_ctx_io.get(
                    "rate_limit_retry_after_seconds"
                ),
                "kind": _reaper_ctx_io.get("rate_limit_kind") or "rate_limit",
            }
            try:
                await disable_account_model(rl_acct_id, rl_model, **_reaper_kwargs_io)
            except Exception as _disable_err:  # noqa: BLE001
                logger.warning(
                    f"[REAPER] disable_account_model failed for {rl_acct_id}/{rl_model}: "
                    f"{_disable_err}; retrying in 1s"
                )
                await asyncio.sleep(1)
                try:
                    await disable_account_model(
                        rl_acct_id, rl_model, **_reaper_kwargs_io
                    )
                except Exception as _disable_err2:  # noqa: BLE001
                    logger.error(  # noqa: TRY400
                        f"[REAPER] disable_account_model retry also failed for "
                        f"{rl_acct_id}/{rl_model}: {_disable_err2}. "
                        "Account remains active in pool — tight re-selection loop risk."
                    )

    # 2b. Dead-auth account hard-disable I/O.
    # Called only when is_dead_auth AND not is_spot_preemption.
    # Uses disable_account_for_auth (permanent hard-disable, no
    # auto-recover) rather than disable_account_model (cooldown).
    da_acct_id: str | None = None
    if is_dead_auth and not is_spot_preemption:
        da_acct_id = get_task_llm_account_id(task)
        if da_acct_id:
            _da_pattern = (task.context or {}).get(
                "dead_auth_matched_pattern", "dead_oauth_session"
            )
            _da_reason = f"dead-auth: {_da_pattern}"
            logger.warning(
                "[REAPER] Task %s dead-auth (account=%s pattern=%r) — "
                "hard-disabling account fleet-wide; requeuing onto different account",
                task.id,
                da_acct_id,
                _da_pattern,
            )
            try:
                await disable_account_for_auth(da_acct_id, _da_reason)
            except Exception as _da_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] disable_account_for_auth failed for %s: %s; retrying in 1s",
                    da_acct_id,
                    _da_err,
                )
                await asyncio.sleep(1)
                try:
                    await disable_account_for_auth(da_acct_id, _da_reason)
                except Exception as _da_err2:  # noqa: BLE001
                    logger.error(  # noqa: TRY400
                        "[REAPER] disable_account_for_auth retry also failed for %s: %s. "
                        "Account remains active — dead-auth re-selection risk.",
                        da_acct_id,
                        _da_err2,
                    )

    # 3. Provision-failure serial capture I/O (GCP serial console).
    # Run unconditionally on provision failures: the spot-preempt branch
    # falls through to the provision-failure mutation block below (which
    # uses io_serial_log), so we need the capture whenever
    # is_provision_failure regardless of spot/rate-limit branch. Tiny
    # perf cost when spot/rate fast-paths skip the mutation block, but
    # avoids re-introducing the held-lock-during-IO bug as a fallback path.
    io_serial_log: str | None = None
    io_prov_zone: str | None = None
    io_provision_err: str | None = None
    if is_provision_failure and not is_cloud_run_task(fresh_task.vm_machine_type):
        # Resolve zone (in-memory cache + persisted fallback). The two
        # values prov_zone and provision_err are stable post-fail;
        # reading them while unlocked is safe — no peer mutates them.
        if fresh_task.vm_instance_id:
            cached = get_vm_zone(fresh_task.vm_instance_id)
            if cached and cached != "unknown":
                io_prov_zone = cached
        if not io_prov_zone:
            io_prov_zone = (task.context or {}).get("provision_zone")
        io_provision_err = get_provision_error(fresh_task.vm_instance_id or "")
        if io_prov_zone and fresh_task.vm_instance_id:
            try:
                io_serial_log = await asyncio.to_thread(
                    _capture_serial_for_vm,
                    fresh_task.vm_instance_id,
                    io_prov_zone,
                )
                if io_serial_log:
                    logger.warning(
                        f"[REAPER] Serial log for dying agent {fresh_task.vm_instance_id} "
                        f"(task {task.id}):\n{io_serial_log}"
                    )
            except Exception:
                logger.exception(
                    f"[REAPER] Failed to capture serial log for VM {fresh_task.vm_instance_id}"
                )

    # 4. PR-state lookup I/O (GitHub API)
    has_pr_artifact, pr_num = await _task_has_open_pr(task, session)
    gh_pr_state: str | None = None
    gh_pr_merged: bool = False
    gh_pr_base_branch: str | None = None
    if has_pr_artifact and pr_num:
        try:
            from app.routers.agent import get_github_token_for_task
            from app.github import get_pull_request

            (
                _gh_token,
                _gh_owner,
                _gh_repo,
            ) = await get_github_token_for_task(task.id, session)
            _pr_data = await get_pull_request(_gh_token, _gh_owner, _gh_repo, pr_num)
            gh_pr_state = _pr_data.get("state")
            gh_pr_merged = bool(_pr_data.get("merged"))
            gh_pr_base_branch = (_pr_data.get("base") or {}).get("ref")
        except Exception as _gh_exc:  # noqa: BLE001
            # GH API unreachable: fall back to "open" — handing off
            # to review pipeline is the safer default.
            logger.warning(
                f"[REAPER] PR-state lookup failed for task {task.id} PR #{pr_num}: "
                f"{type(_gh_exc).__name__}: {_gh_exc} — assuming open",
                exc_info=True,
            )
            gh_pr_state = "open"

    return OrphanIoSnapshot(
        pre_io_status=pre_io_status,
        pre_io_last_hb=pre_io_last_hb,
        is_spot_preemption=is_spot_preemption,
        is_rate_limited=is_rate_limited,
        rl_acct_id=rl_acct_id,
        rl_model=rl_model,
        da_acct_id=da_acct_id,
        io_serial_log=io_serial_log,
        io_prov_zone=io_prov_zone,
        io_provision_err=io_provision_err,
        has_pr_artifact=has_pr_artifact,
        pr_num=pr_num,
        gh_pr_state=gh_pr_state,
        gh_pr_merged=gh_pr_merged,
        gh_pr_base_branch=gh_pr_base_branch,
    )
