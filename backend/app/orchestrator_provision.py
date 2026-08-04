"""VM provisioning + provision-failure handling, extracted from
``orchestrator_worker``.

This module owns ``provision_and_handle`` — the coroutine the dispatch loop
fires via ``asyncio.create_task`` once a task has been committed to
PROVISIONING. It uploads the payload to GCS, stores the agent secret, launches
the VM (Cloud Run job when allow-listed, otherwise a GCE VM, with a Cloud-Run
→ GCE fallback), verifies the task still wants the VM, and on failure classifies
the error (infra-wide vs task-specific), requeues or fails the task, releases
the Max Plan slot, and tears down the partial VM.

It was lifted verbatim (behaviour-preserving) out of the ``orchestrator_worker``
inline region to shrink the orchestrator monolith, mirroring the sibling
``orchestrator_ci_monitor`` / ``orchestrator_runner_dispatch`` extractions.

Import discipline (preserves existing test mock targets with no test churn):

* Callees that tests patch at ``app.orchestrator.<name>`` (``provision_agent_vm``,
  ``launch_agent_cloud_run_job``, ``prepare_and_store_agent_secret``,
  ``upload_json_payload``, ``_capture_serial_for_vm``), the orchestrator-owned
  ``_lock_provisioning_task_for_vm``, and ``AsyncSessionLocal`` (redirected to the
  test database via ``app.orchestrator.AsyncSessionLocal``) are reached through
  ``import app.orchestrator as _orch`` at call time, so patching the orchestrator
  attribute still takes effect here — exactly as the former inline closure
  resolved these names against the orchestrator module globals.
  ``app.orchestrator`` keeps those imports (``# noqa: F401``) purely as the patch
  surface.
* Callees that tests patch at their source module are re-imported lazily inside
  ``provision_and_handle`` (see the late-import block).
* Everything else is a plain module-level import — bound at load, matching the
  former closure's module-global snapshot semantics.

``provision_and_handle`` is re-exported from ``app.orchestrator`` so
``from app.orchestrator import provision_and_handle`` keeps working.
"""

import asyncio
import logging
import uuid

import app.shared_state as _shared_state
from app.compute import (
    INFRA_PROVISION_PATTERNS,
    VM_PROVISIONING_MODEL,
    any_zone_available,
    get_provision_error,
    get_vm_zone,
    record_zone_failure,
    zone_recovery_wait_seconds,
)
from app.compute_cloud_run import (
    _resolve_machine_type as _cr_resolve_machine_type,
    is_cloud_run_task as _is_cloud_run_task,
)
from app.models import (
    Project,
    Task,
    TaskStatus,
    bank_task_current_attempt,
    with_task_attempt_banking,
)
from app.shared_state import runtime_config as _runtime_config
from app.task_capabilities import (
    CLOUD_RUN_CANDIDATE_TASK_TYPES,
    DIRECT_PACKER_GCE_EXECUTION_SITE,
    execution_site_for_task_type,
)
from app.time_utils import _utcnow, set_retry_after
from app.resource_envelope import resolve_project_execution_placement_policy

logger = logging.getLogger("blueprint")


async def _project_placement_fence(
    session_factory,
    project_id: uuid.UUID,
) -> tuple[bool, bool]:
    """Return ``(local_only, unavailable)`` for a pre-billing policy check.

    An unreadable production policy must never turn into a paid fallback.  Test
    and local SQLite environments retain their existing permissive behaviour so
    synthetic launch tests do not need a durable project row.  The second return
    value lets callers distinguish an actual ``local_only`` policy from an
    infrastructure hold in their operator-visible task context.
    """

    try:
        async with session_factory() as policy_session:
            project = await policy_session.get(Project, project_id)
            return (
                bool(
                    project is not None
                    and resolve_project_execution_placement_policy(project)
                    == "local_only"
                ),
                False,
            )
    except Exception:
        from app.database import DATABASE_URL

        unavailable = True
        if not DATABASE_URL or "sqlite" in DATABASE_URL.lower():
            logger.warning(
                "[ORCHESTRATOR] Could not read project placement for %s in "
                "local/test mode; preserving local behaviour",
                project_id,
                exc_info=True,
            )
            return False, unavailable
        logger.exception(
            "[ORCHESTRATOR] Could not read project placement for %s; "
            "withholding managed execution",
            project_id,
        )
        return True, unavailable


async def provision_and_handle(
    vm_id: str,
    t_id: uuid.UUID,
    p_id: uuid.UUID,
    payloads: list,
    secret_value: str | None,
    config_json: str | None,
    acct_id_for_release: str,
    llm_provider: str | None,
    llm_auth_mode: str | None,
    llm_model: str | None,
    llm_execution_mode: str | None,
    llm_provider_metadata: dict | None,
    llm_provider_settings: dict | None,
    force_standard: bool = False,
):
    # Late imports mirror the ``orchestrator_worker`` late-import block: these
    # names were captured as enclosing-scope freevars by the former inline
    # closure, so re-importing them here per call preserves their existing
    # test mock targets at the source modules (``app.max_plan_pool`` /
    # ``app.vm_lifecycle`` / ``app.routers.agent_status`` / ``app.alert_dispatch``
    # / ``app.broadcast``) rather than snapshotting at module-load time.
    #
    # ``app.orchestrator`` is imported lazily here (never at module top level) so
    # the ``orchestrator -> orchestrator_provision`` re-export cannot deadlock on
    # import order; by call time both modules are fully initialised. Reaching the
    # provision/lock helpers through ``_orch`` keeps their ``app.orchestrator.*``
    # test-patch targets live.
    import app.orchestrator as _orch
    from app.max_plan_pool import (
        clear_task_llm_runtime_context,
        release_max_plan_account,
    )
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.routers.agent_status import record_task_result
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.broadcast import broadcast_to_project
    from app.managed_compute import (
        LocalOnlyExecutionPlacement,
        ManagedComputeDisabled,
        is_managed_compute_enabled_authoritative,
        managed_compute_denial_snapshot,
    )

    account_released = False

    async def _release_account_once() -> None:
        """Undo the reservation exactly once across every hold/failure path."""

        nonlocal account_released
        if not account_released and acct_id_for_release:
            await release_max_plan_account(
                acct_id_for_release,
                revert_daily_task_count=True,
                project_id=str(p_id) if p_id else None,
            )
            account_released = True

    async def _restore_to_queue_if_execution_disallowed(
        *,
        force: bool = False,
        local_only_hint: bool = False,
    ) -> bool:
        """Undo a pre-RPC PROVISIONING reservation when policy changed."""

        managed_enabled = await is_managed_compute_enabled_authoritative()
        local_only, placement_unavailable = await _project_placement_fence(
            _orch.AsyncSessionLocal, p_id
        )
        if managed_enabled and not local_only and not force:
            return False
        local_only = local_only or local_only_hint

        async with _orch.AsyncSessionLocal() as hold_session:
            held_task = await _orch._lock_provisioning_task_for_vm(
                hold_session, t_id, vm_id
            )
            if held_task is not None:
                held_task.status = TaskStatus.QUEUED
                held_task.started_at = None
                held_task.vm_provisioned_at = None
                held_task.last_heartbeat = None
                # Cloud Run marks the task before attempting RunJob. A typed
                # final-fence denial has no execution to attribute or tear down,
                # so do not leave a queued task looking like a Cloud Run job.
                if _is_cloud_run_task(held_task.vm_machine_type):
                    held_task.vm_machine_type = None
                if placement_unavailable:
                    held_task.context = {
                        **(held_task.context or {}),
                        "managed_compute": {
                            "reason": "project_placement_unavailable",
                            "backend": "agent_provision",
                            "setting": "execution_placement_policy",
                            "enabled": False,
                            "recorded_at": _utcnow().isoformat(),
                        },
                    }
                    held_task.latest_status_message = (
                        "Waiting for local execution capacity: project placement "
                        "could not be read, so managed execution is withheld."
                    )
                elif local_only:
                    held_task.context = {
                        **(held_task.context or {}),
                        "execution_placement_wait": {
                            "policy": "local_only",
                            "reason": "policy_changed_while_provisioning",
                            "since": _utcnow().isoformat(),
                        },
                    }
                    held_task.latest_status_message = (
                        "Waiting for local execution capacity (project policy: local_only)."
                    )
                else:
                    held_task.context = {
                        **(held_task.context or {}),
                        "managed_compute": managed_compute_denial_snapshot(
                            backend="agent_provision"
                        ),
                    }
                    held_task.latest_status_message = (
                        "Waiting for local capacity: managed cloud execution is disabled"
                    )
                hold_session.add(held_task)
                await hold_session.commit()
        await _release_account_once()
        logger.info(
            "[ORCHESTRATOR] Restored task %s to QUEUED before managed provisioning "
            "because %s",
            t_id,
            (
                "project placement could not be read"
                if placement_unavailable
                else (
                    "project placement changed to local_only"
                    if local_only
                    else "managed execution is disabled"
                )
            ),
        )
        return True

    # This is also needed by generic failure handling after the try/finally, so
    # compute it before the initial fail-closed placement fence.
    t_type = (payloads[0] or {}).get("type", "") if payloads else ""
    success = False
    try:
        # A settings update can race the dispatch commit. The dispatcher checks
        # the switch before writing PROVISIONING, while this second check
        # restores a just-committed task to QUEUED before any upload, secret
        # write, Cloud Run Job, or GCE call. Keep it inside this try/finally:
        # even a failed policy-read/hold transaction must release the reserved
        # Max-plan account exactly once.
        if await _restore_to_queue_if_execution_disallowed():
            return

        # Upload payload to GCS and store agent credentials in Secret Manager
        # concurrently — both are independent I/O operations (~500ms each)
        # that produce inputs for the VM startup script.
        async def _upload_payload_safe() -> str | None:
            if not payloads:
                return None
            try:
                return await _orch.upload_json_payload(
                    str(t_id), payloads
                )
            except Exception as e:
                logger.error(
                    f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}",
                    exc_info=True,
                )
                return None

        payload_uri, secret_version_name = await asyncio.gather(
            _upload_payload_safe(),
            _orch.prepare_and_store_agent_secret(
                vm_id,
                t_id,
                secret_value,
                config_json,
                t_type,
                provider=llm_provider,
                auth_mode=llm_auth_mode,
                model=llm_model,
                execution_mode=llm_execution_mode,
                provider_metadata=llm_provider_metadata,
                provider_settings=llm_provider_settings,
                project_id=str(p_id),  # audit W3/E10 (was task.project_id closure)
            ),
        )

        # This coroutine may have spent time uploading payloads and minting
        # credentials.  Re-read both the durable fleet switch and project
        # placement directly before the first paid launch path.
        if await _restore_to_queue_if_execution_disallowed():
            return

        # Dispatch to Cloud Run Jobs when the task type
        # is in the effective Cloud Run allowlist
        # (CLOUD_RUN_CANDIDATE_TASK_TYPES ∩ runtime allowlist).
        _cr_allowlist = frozenset(
            _runtime_config.get(
                "cloud_run_task_type_allowlist", []
            )
        )
        _effective_cr_types = (
            CLOUD_RUN_CANDIDATE_TASK_TYPES & _cr_allowlist
        )
        _direct_packer_gce_dispatch = (
            execution_site_for_task_type(t_type)
            == DIRECT_PACKER_GCE_EXECUTION_SITE
        )
        success = False
        if t_type in _effective_cr_types and not _direct_packer_gce_dispatch:
            # Stamp the Cloud Run sentinel machine type
            # so cost attribution and teardown skips work.
            async with _orch.AsyncSessionLocal() as _cr_stamp_session:
                _cr_task = await _cr_stamp_session.get(
                    Task, t_id
                )
                if _cr_task is not None:
                    _cr_task.vm_machine_type = (
                        _cr_resolve_machine_type(t_type)
                    )
                    _cr_stamp_session.add(_cr_task)
                    await _cr_stamp_session.commit()
            success = await _orch.launch_agent_cloud_run_job(
                vm_id,
                t_id,
                payload_uri,
                secret_version_name,
                t_type,
                oauth_token=secret_value,
                provider=llm_provider,
                auth_mode=llm_auth_mode,
                model=llm_model,
                execution_mode=llm_execution_mode,
                provider_metadata=llm_provider_metadata,
                provider_settings=llm_provider_settings,
            )
            if not success:
                # Cloud Run dispatch failed — fall back to a
                # GCE VM rather than stranding/looping the task.
                # The PROVISION_FAILURE path below would requeue
                # it, and with the allowlist still set it would
                # re-dispatch to Cloud Run and hit the same wall
                # (a flap). Clear the CR sentinel first so cost
                # attribution + teardown treat the task as the
                # GCE VM it is about to become.
                logger.warning(
                    f"[ORCHESTRATOR] Cloud Run dispatch failed for "
                    f"task {t_id} ({t_type}); falling back to GCE "
                    f"VM provisioning"
                )
                async with _orch.AsyncSessionLocal() as _cr_unstamp_session:
                    _cr_task = await _cr_unstamp_session.get(
                        Task, t_id
                    )
                    if _cr_task is not None:
                        _cr_task.vm_machine_type = None
                        _cr_unstamp_session.add(_cr_task)
                        await _cr_unstamp_session.commit()
        if not success:
            success = await _orch.provision_agent_vm(
                vm_id,
                t_id,
                payload_uri,
                oauth_token=secret_value,
                claude_config_json=config_json,
                task_type=t_type,
                secret_version_name=secret_version_name,
                provider=llm_provider,
                auth_mode=llm_auth_mode,
                model=llm_model,
                execution_mode=llm_execution_mode,
                provider_metadata=llm_provider_metadata,
                provider_settings=llm_provider_settings,
                provisioning_model="STANDARD"
                if force_standard
                else None,
            )
        if success:
            # Verify the task still wants this VM — stale recovery may have
            # requeued the task with a new vm_id while we were provisioning.
            # If so, this VM is an orphan: delete it immediately.
            try:
                async with _orch.AsyncSessionLocal() as hb_session:
                    hb_task = (
                        await _orch._lock_provisioning_task_for_vm(
                            hb_session, t_id, vm_id
                        )
                    )
                    if hb_task is None:
                        current_task = await hb_session.get(
                            Task, t_id
                        )
                        if (
                            not current_task
                            or current_task.vm_instance_id
                            != vm_id
                        ):
                            current_vm = (
                                current_task.vm_instance_id
                                if current_task
                                else "gone"
                            )
                            logger.warning(
                                f"[ORCHESTRATOR] VM {vm_id} created but task {t_id} now has vm_id={current_vm} — deleting orphan"
                            )
                            await _ensure_vm_deleted(
                                vm_id, reason="vm_id_mismatch"
                            )
                            success = False
                            return
                    else:
                        # Refresh vm_provisioned_at to the point the GCE API
                        # confirmed the VM is running. This gives the agent a
                        # fresh window to claim before the reaper considers it
                        # a silent startup failure. last_heartbeat is left alone
                        # so only real agent activity writes it.
                        hb_task.vm_provisioned_at = _utcnow()
                        # vm_spot mirrors orchestrator-mode at provision-confirm time;
                        # see comment at the dispatch-site write above.
                        hb_task.vm_spot = (
                            VM_PROVISIONING_MODEL == "SPOT"
                        ) and not force_standard
                        # Save provision zone for diagnostics
                        prov_zone = get_vm_zone(vm_id)
                        hb_task.context = {
                            **(hb_task.context or {}),
                            "provision_zone": prov_zone,
                        }
                        hb_session.add(hb_task)
                        await hb_session.commit()
            except Exception as e:
                logger.warning(
                    f"[ORCHESTRATOR] Failed to verify/touch heartbeat after provision for {t_id}: {e}"
                )
    except asyncio.CancelledError:
        # Stale recovery cancelled us, but the underlying GCP thread may
        # still create the VM. Schedule a delayed cleanup to catch it.
        logger.warning(
            f"[ORCHESTRATOR] Provision for {vm_id} cancelled — scheduling orphan cleanup"
        )

        async def _delayed_orphan_cleanup(orphan_vm_id: str):
            await asyncio.sleep(
                120
            )  # Wait for GCP thread to finish
            await _ensure_vm_deleted(
                orphan_vm_id,
                reason="cancelled_provision_orphan",
            )

        _cleanup_key = f"orphan_cleanup_{vm_id}"
        _cleanup_task = asyncio.create_task(
            _delayed_orphan_cleanup(vm_id)
        )
        _shared_state.background_tasks[_cleanup_key] = (
            _cleanup_task
        )
        # Drop the dict entry once the task finishes so we don't retain
        # completed asyncio.Task objects indefinitely (seen ~100s of these
        # accumulating per day of provision churn — direct memory growth).
        _cleanup_task.add_done_callback(
            lambda _t, _k=_cleanup_key: (
                _shared_state.background_tasks.pop(_k, None)
            )
        )
        raise
    except ManagedComputeDisabled as exc:
        # A final GCE/Cloud Run fence observed an intentional cost/placement
        # change after the preflight check. Never turn that into a fallback or
        # a PROVISION_FAILURE retry; force a queued hold even if an operator
        # re-enabled the setting in the few milliseconds since the fence.
        await _restore_to_queue_if_execution_disallowed(
            force=True,
            local_only_hint=isinstance(exc, LocalOnlyExecutionPlacement),
        )
        return
    except Exception as e:
        logger.error(
            f"[ORCHESTRATOR] Provision crashed for task {t_id}: {e}",
            exc_info=True,
        )
        success = False
    finally:
        # Always release the Max Plan account slot on failure, even if an
        # exception escapes the try/except above (e.g. from record_task_result)
        if not success:
            await _release_account_once()
    if not success:
        provision_err = get_provision_error(vm_id)
        logger.error(
            f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}: {provision_err}"
        )
        try:
            record_task_result(
                success=False,
                failure_class="PROVISION_FAILURE",
                task_id=str(t_id),
                task_type=t_type,
            )
            # Hoist zone lookup so it can be saved to context and used for zone failure recording
            prov_zone = get_vm_zone(vm_id)
            # Capture serial port logs before VM deletion (best-effort)
            serial_log = None
            try:
                if prov_zone and prov_zone != "unknown":
                    serial_log = await asyncio.to_thread(
                        _orch._capture_serial_for_vm, vm_id, prov_zone
                    )
                    if serial_log:
                        logger.error(
                            f"[ORCHESTRATOR] Serial log for failed VM {vm_id}:\n{serial_log}"
                        )
            except Exception:
                logger.exception(
                    f"[ORCHESTRATOR] Failed to capture serial log for VM {vm_id}"
                )
            # Requeue with retry tracking — zone exhaustion is transient
            try:
                async with _orch.AsyncSessionLocal() as inner_session:
                    failed_task = (
                        await _orch._lock_provisioning_task_for_vm(
                            inner_session, t_id, vm_id
                        )
                    )
                    if failed_task:
                        ctx = dict(failed_task.context or {})
                        if prov_zone:
                            ctx["provision_zone"] = prov_zone
                        if serial_log:
                            ctx["last_serial_log"] = serial_log

                        # Classify: infra-wide (zone/quota exhaustion) vs task-specific
                        err_lower = (
                            provision_err or ""
                        ).lower()
                        is_infra = (
                            any(
                                p in err_lower
                                for p in INFRA_PROVISION_PATTERNS
                            )
                            or not any_zone_available()
                        )

                        if is_infra:
                            # Infrastructure-wide failure — don't burn retry budget
                            infra_count = (
                                ctx.get(
                                    "provision_failures_infra",
                                    0,
                                )
                                + 1
                            )
                            ctx["provision_failures_infra"] = (
                                infra_count
                            )
                            ctx["_infra_wait"] = True
                            bank_task_current_attempt(
                                failed_task,
                                stopped_at=_utcnow(),
                            )
                            ctx = with_task_attempt_banking(
                                failed_task, ctx
                            )
                            failed_task.context = ctx
                            failed_task.status = (
                                TaskStatus.QUEUED
                            )
                            failed_task.vm_instance_id = (
                                f"agent-vm-{uuid.uuid4().hex}"
                            )
                            failed_task.last_heartbeat = None
                            failed_task.started_at = None
                            # Align retry with zone recovery; cap at 30 min.
                            # Audit C10: zone_recovery_wait_seconds() does
                            # the tz-AWARE subtraction internally (matches
                            # the reaper's infra-requeue path). The previous
                            # naive _utcnow() subtraction here raised
                            # TypeError, rolling back this QUEUED status +
                            # retry_after_until.
                            recovery_wait = (
                                zone_recovery_wait_seconds()
                            )
                            if recovery_wait is not None:
                                wait_sec = max(
                                    60, recovery_wait + 60
                                )
                                delay_min = min(
                                    wait_sec / 60, 30
                                )
                            else:
                                delay_min = 10
                            set_retry_after(
                                failed_task, delay_min
                            )
                            ctx = dict(
                                failed_task.context or {}
                            )
                            failed_task.latest_status_message = f"Infra-wide provision failure (#{infra_count}), requeued — waiting for zone recovery: {provision_err or 'unknown'}"[
                                :500
                            ]
                            logger.warning(
                                f"[ORCHESTRATOR] Task {t_id} infra provision failure — requeuing without burning retry budget (delay {delay_min:.0f}min)"
                            )
                        else:
                            # Task-specific failure — burn retry budget
                            prov_failures = (
                                ctx.get("provision_failures", 0)
                                + 1
                            )
                            ctx["provision_failures"] = (
                                prov_failures
                            )
                            ctx.pop("_infra_wait", None)
                            max_prov_retries = (
                                _runtime_config.get(
                                    "max_provision_retries", 3
                                )
                            )
                            if (
                                prov_failures
                                >= max_prov_retries
                            ):
                                # Exhausted retries — permanent failure
                                failed_task.status = (
                                    TaskStatus.FAILED
                                )
                                failed_task.latest_status_message = f"VM provisioning failed {prov_failures} times — giving up: {provision_err or 'unknown error'}"
                                ctx["failure_category"] = (
                                    "PROVISION_FAILURE"
                                )
                                await (
                                    _maybe_create_failure_alert(
                                        failed_task,
                                        inner_session,
                                        (
                                            provision_err
                                            or "unknown error"
                                        )[:200],
                                    )
                                )
                                try:
                                    record_task_result(
                                        success=False,
                                        failure_class="PROVISION_FAILURE",
                                        task_id=str(
                                            failed_task.id
                                        ),
                                        task_type=(
                                            failed_task.first_payload
                                            or {}
                                        ).get("type", ""),
                                    )
                                    _direction_id = str(
                                        (
                                            failed_task.context
                                            or {}
                                        ).get("direction_id")
                                        or ""
                                    )
                                    if _direction_id:
                                        from app.shared_state import (
                                            record_direction_result as _rdr,
                                        )

                                        _rdr(
                                            _direction_id,
                                            success=False,
                                            failure_class="PROVISION_FAILURE",
                                            project_id=str(
                                                failed_task.project_id
                                            )
                                            if failed_task.project_id
                                            else "",
                                        )
                                except Exception:
                                    logger.exception(
                                        f"[ORCHESTRATOR] Failed to record provision-exhaustion result for task {t_id}"
                                    )
                                logger.error(
                                    f"[ORCHESTRATOR] Task {t_id} provision failed {prov_failures} times — permanent failure"
                                )
                            else:
                                # Requeue for retry
                                bank_task_current_attempt(
                                    failed_task,
                                    stopped_at=_utcnow(),
                                )
                                ctx = with_task_attempt_banking(
                                    failed_task, ctx
                                )
                                failed_task.context = ctx
                                failed_task.status = (
                                    TaskStatus.QUEUED
                                )
                                failed_task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                failed_task.last_heartbeat = (
                                    None
                                )
                                failed_task.started_at = None
                                # Back off 2 min per failure to let zones recover
                                set_retry_after(
                                    failed_task,
                                    2 * prov_failures,
                                )
                                ctx = dict(
                                    failed_task.context or {}
                                )
                                failed_task.latest_status_message = f"Provision failed (attempt {prov_failures}/{max_prov_retries}), requeued: {provision_err or 'unknown'}"
                                logger.warning(
                                    f"[ORCHESTRATOR] Task {t_id} provision failed (attempt {prov_failures}) — requeuing"
                                )

                        if (
                            failed_task.status
                            == TaskStatus.QUEUED
                        ):
                            ctx = (
                                clear_task_llm_runtime_context(
                                    ctx
                                )
                            )
                            failed_task.llm_account_id = None
                        failed_task.context = ctx
                        inner_session.add(failed_task)
                        await inner_session.commit()
                        await broadcast_to_project(
                            str(p_id),
                            "task_updated",
                            {
                                "task_id": str(t_id),
                                "status": failed_task.status,
                            },
                        )
                        # Record zone failure so the zone health system avoids this zone
                        if prov_zone and prov_zone != "unknown":
                            record_zone_failure(
                                prov_zone,
                                reason="PROVISION_INLINE_FAILURE",
                            )
            except Exception as e:
                logger.error(
                    f"[ORCHESTRATOR] Error handling provision failure for task {t_id}: {e}",
                    exc_info=True,
                )
        finally:
            # ALWAYS delete partially-created VM, even if requeue logic crashes
            await _ensure_vm_deleted(
                vm_id, reason="provision_failure"
            )
