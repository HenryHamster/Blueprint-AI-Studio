"""Background service functions: GCP cleanup, secret reconciliation, and orchestrator helpers."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task, TaskStatus, Artifact, ArtifactType, Alert, AlertSeverity,
)
import app.shared_state as _shared_state

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def gcp_cleanup_service():
    """
    Background worker that runs every 5 minutes to ensure GCP VMs are in sync with the database.
    Deletes any VMs prefixed with 'agent-vm-' that do not have an active task.
    Zone-parallel with per-cycle timeout.
    """
    from app.compute import cleanup_orphaned_vms  # noqa: PLC0415

    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Get all active tasks that should have a VM running
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = [vid for vid in active_vm_ids if vid]

                # Run cleanup with hard timeout (2 minutes)
                try:
                    cleanup_result = await asyncio.wait_for(
                        cleanup_orphaned_vms(active_vm_ids),
                        timeout=120
                    )
                    # Log divergence and partial failures from structured result
                    if hasattr(cleanup_result, 'failed') and cleanup_result.failed:
                        logger.warning(
                            f"[GCP CLEANUP] Partial failure: deleted {len(cleanup_result.deleted)}, "
                            f"failed {len(cleanup_result.failed)}: {cleanup_result.failed}"
                        )
                    # Cache total GCP VM count for orchestrator concurrency check
                    if hasattr(cleanup_result, 'total_gcp_vms'):
                        import app.main as _app_main  # noqa: PLC0415
                        _app_main._last_gcp_vm_count = cleanup_result.total_gcp_vms
                        _app_main._last_gcp_vm_check = time.time()
                except asyncio.TimeoutError:
                    logger.error("[GCP CLEANUP] Cycle timed out after 120s")

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


async def secret_reconciliation_service():
    """Periodic cleanup of orphaned agent credential secrets (every 30 min)."""
    from app.compute import reconcile_orphaned_secrets  # noqa: PLC0415

    while True:
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = {vid for vid in active_vm_ids if vid}

            recon_result = await asyncio.wait_for(
                reconcile_orphaned_secrets(active_vm_ids),
                timeout=120
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


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    from app.main import _get_deploy_state_for_update  # noqa: PLC0415

    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            # Prune: don't persist CLOSED breakers with no failures (default state)
            if v.get("state") == "CLOSED" and v.get("failure_count", 0) == 0:
                continue
            serializable[k] = {
                **v,
                "opened_at": v["opened_at"].isoformat() if v.get("opened_at") else None,
            }
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.circuit_breaker_states = serializable
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[CIRCUIT BREAKER] Failed to persist state: {e}", exc_info=True)


async def _persist_zone_health():
    """Snapshot zone health to DB so cooldowns survive restarts."""
    from app.main import _get_deploy_state_for_update  # noqa: PLC0415
    import app.main as _app_main  # noqa: PLC0415

    try:
        from app.compute import _zone_health, _zone_lock
        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {
                    k: v.isoformat() if isinstance(v, datetime) else v
                    for k, v in data.items()
                }
        snapshot["_pause_failure_streak"] = _app_main._pause_failure_streak
        snapshot["_health_recovery_streak"] = _app_main._health_recovery_streak
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


async def _check_evaluating_timeouts():
    """Detect EVALUATING tasks that are stuck and recover or fail them.

    - If all children are terminal but parent is still EVALUATING → trigger missed rollup.
    - If evaluating >2h with non-terminal children → WARNING alert.
    - If evaluating >6h → FAIL with ERROR alert.
    - Skips CI-waiting tasks (they have their own 30-min timeout).
    """
    from app.main import _check_and_trigger_rollup, _maybe_create_failure_alert, broadcast_to_project  # noqa: PLC0415

    EVALUATING_WARN_SECONDS = 2 * 3600   # 2 hours
    EVALUATING_FAIL_SECONDS = 6 * 3600   # 6 hours
    TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    try:
        async with AsyncSessionLocal() as session:
            now = _utcnow()
            evaluating_tasks = (await session.execute(
                select(Task).where(
                    Task.status == TaskStatus.EVALUATING,
                    Task.archived.is_(False),
                )
            )).scalars().all()

            for task in evaluating_tasks:
                ctx = task.context or {}
                # Skip CI-waiting tasks — they have their own timeout
                if ctx.get("ci_waiting") is True:
                    continue
                if ctx.get("review_loop", {}).get("ci_waiting") is True:
                    continue

                # Determine how long the task has been EVALUATING
                evaluating_since = None
                # Walk status_log backwards for the last EVALUATING transition
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        try:
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        except (KeyError, ValueError, TypeError):
                            pass
                        break
                if evaluating_since is None:
                    # Fallback to started_at or created_at
                    evaluating_since = task.started_at or task.created_at

                elapsed = (now - evaluating_since).total_seconds()

                # Fetch children
                children = (await session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )).scalars().all()

                all_children_terminal = bool(children) and all(
                    c.status in TERMINAL for c in children
                )

                # Recovery for missed rollup: all children done but parent still EVALUATING
                if all_children_terminal:
                    logger.warning(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has all children terminal "
                        f"but is still EVALUATING — triggering missed rollup"
                    )
                    # Use any terminal child to trigger rollup
                    await _check_and_trigger_rollup(children[0], session)
                    await session.commit()
                    continue

                # 6-hour hard timeout
                if elapsed > EVALUATING_FAIL_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    logger.error(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                        f"for {hours}h — hard timeout, marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        "Evaluation timeout — children did not complete within 6 hours"
                    )
                    session.add(task)

                    # Cascade-cancel non-terminal children
                    from app.main import _ensure_vm_deleted, release_max_plan_account  # noqa: PLC0415
                    non_terminal_children = [c for c in children if c.status not in TERMINAL]
                    vms_to_delete = []
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = "Cancelled: parent task timed out"
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    if non_terminal_children:
                        logger.info(
                            f"[EVAL TIMEOUT] Cascade-cancelled {len(non_terminal_children)} "
                            f"children of timed-out task {str(task.id)[:8]}"
                        )

                    # Create ERROR alert (deduplicated by _maybe_create_failure_alert)
                    await _maybe_create_failure_alert(
                        task, session,
                        f"Evaluation timeout after {hours}h — children did not complete",
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(task.project_id), "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    for child in non_terminal_children:
                        await broadcast_to_project(
                            str(task.project_id), "task_updated",
                            {"task_id": str(child.id), "status": child.status},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="parent_eval_timeout")
                    continue

                # 2-hour warning
                if elapsed > EVALUATING_WARN_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    # Deduplicate: check for existing undismissed alert on this task
                    existing = (await session.execute(
                        select(Alert).where(
                            Alert.task_id == task.id,
                            Alert.dismissed_at.is_(None),
                        )
                    )).scalars().first()
                    if existing is None:
                        logger.warning(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                            f"for {hours}h — children may be stuck"
                        )
                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.WARNING,
                            title=f"Task {task.title or str(task.id)[:8]} stuck evaluating",
                            message=(
                                f"Task has been evaluating for {hours} hours "
                                f"— children may be stuck"
                            ),
                            task_id=task.id,
                        )
                        session.add(alert)
                        await session.commit()
                        await broadcast_to_project(
                            str(task.project_id), "alert_created",
                            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
                        )
    except Exception as e:
        logger.error(f"[EVAL TIMEOUT] Evaluating timeout sweep failed: {e}", exc_info=True)


async def _recover_orphaned_tasks():
    """Detect COMPLETED tasks with no PR artifact and no review child. Look up PR on GitHub and spawn review."""
    from app.main import broadcast_to_project, _spawn_pipeline_task, get_github_token_for_task  # noqa: PLC0415

    try:
        async with AsyncSessionLocal() as session:
            five_min_ago = _utcnow() - timedelta(minutes=5)
            one_hour_ago = _utcnow() - timedelta(hours=1)
            candidates = (await session.execute(
                select(Task).where(
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_by == "ORCHESTRATOR",
                    Task.created_at <= five_min_ago,
                    Task.created_at >= one_hour_ago,  # Only check recent tasks, not entire history
                )
            )).scalars().all()

            for task in candidates:
                task_type = task.first_payload.get("type", "")
                if task_type not in ("create", "refactor", "fix", "docs", "deps", "migrate"):
                    continue

                # Check if PR artifact exists
                has_artifact = (await session.execute(
                    select(Artifact.id).where(
                        Artifact.task_id == task.id,
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    ).limit(1)
                )).first()
                if has_artifact:
                    continue

                # Check if review child exists
                children = (await session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )).scalars().all()
                if any(t.first_payload.get("type") == "review" for t in children):
                    continue

                # Orphaned — try to find PR on GitHub
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                except Exception:
                    logger.warning(f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub token fetch failed", exc_info=True)
                    continue

                short_id = str(task.id).replace('-', '')[:8]
                type_prefix = {"docs": "docs", "deps": "deps", "migrate": "migrate", "refactor": "refactor"}.get(task_type, "task")
                branch = f"blueprint/{type_prefix}-{short_id}"

                try:
                    from app.github import _github_request
                    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"}
                    resp = await _github_request("GET",
                        f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open",
                        headers)
                    prs = resp.json() if resp.status_code == 200 else []
                except Exception:
                    logger.warning(f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub PR lookup failed", exc_info=True)
                    continue

                if not prs:
                    logger.info(f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — no open PR found for branch {branch}")
                    continue

                pr = prs[0]
                pr_url = pr["html_url"]
                pr_number = pr["number"]
                base_branch = task.first_payload.get("base_branch", "main")

                # Create the missing artifact
                artifact = Artifact(task_id=task.id, artifact_type=ArtifactType.PULL_REQUEST, pr_url=pr_url)
                session.add(artifact)

                # Spawn review
                original_prompt = task.first_payload.get("prompt", task.description or "")
                review_task = await _spawn_pipeline_task(
                    task, "review", session, pr_number, branch, base_branch,
                    original_prompt=original_prompt, original_task_id=str(task.id)
                )

                # Create alert
                alert = Alert(
                    project_id=task.project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Auto-recovered orphaned PR #{pr_number}",
                    message=f"Task {str(task.id)[:8]} completed with PR #{pr_number} but review was never spawned. Auto-recovered.",
                    task_id=task.id,
                )
                session.add(alert)
                await session.commit()

                await broadcast_to_project(str(task.project_id), "alert_created",
                    {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                logger.warning(f"[RECOVERY] Auto-recovered orphaned task {str(task.id)[:8]} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}")

            # Phase 2: Reaped-with-artifact recovery.
            # Tasks that were reaped (set back to QUEUED/PROVISIONING) after the PR was already
            # created.  The PR endpoint saves the artifact, so these tasks have an artifact but
            # never reached COMPLETED and thus never triggered pipeline routing.
            reaped_candidates = (await session.execute(
                select(Task).where(
                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
                    Task.created_by == "ORCHESTRATOR",
                    Task.attempt_count > 0,
                    Task.created_at >= one_hour_ago,
                )
            )).scalars().all()

            for task in reaped_candidates:
                task_type = task.first_payload.get("type", "")
                if task_type not in ("create", "refactor", "fix", "docs", "deps", "migrate"):
                    continue

                # Only recover tasks that already have a PR artifact
                pr_artifact = (await session.execute(
                    select(Artifact).where(
                        Artifact.task_id == task.id,
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    ).limit(1)
                )).scalars().first()
                if not pr_artifact or not pr_artifact.pr_url:
                    continue

                # Skip if review child already exists
                children = (await session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )).scalars().all()
                if any(t.first_payload.get("type") == "review" for t in children):
                    continue

                # Lock row to prevent race with reaper/dispatcher
                locked = (await session.execute(
                    select(Task).where(Task.id == task.id).with_for_update(skip_locked=True)
                )).scalars().first()
                if not locked or locked.status not in (TaskStatus.QUEUED, TaskStatus.PROVISIONING):
                    continue

                pr_url = pr_artifact.pr_url
                pr_number = int(pr_url.rstrip("/").split("/")[-1])
                base_branch = locked.first_payload.get("base_branch", "main")
                short_id = str(locked.id).replace('-', '')[:8]
                type_prefix = {"docs": "docs", "deps": "deps", "migrate": "migrate", "refactor": "refactor"}.get(task_type, "task")
                branch = f"blueprint/{type_prefix}-{short_id}"

                locked.status = TaskStatus.COMPLETED
                locked.latest_status_message = f"Auto-completed: PR #{pr_number} found after reap recovery"
                session.add(locked)

                original_prompt = locked.first_payload.get("prompt", locked.description or "")
                review_task = await _spawn_pipeline_task(
                    locked, "review", session, pr_number, branch, base_branch,
                    original_prompt=original_prompt, original_task_id=str(locked.id)
                )

                alert = Alert(
                    project_id=locked.project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Auto-recovered reaped task with PR #{pr_number}",
                    message=f"Task {short_id} was reaped after creating PR #{pr_number}. Auto-completed and review spawned.",
                    task_id=locked.id,
                )
                session.add(alert)
                await session.commit()

                await broadcast_to_project(str(locked.project_id), "alert_created",
                    {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                logger.warning(f"[RECOVERY] Auto-completed reaped task {short_id} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}")
    except Exception as e:
        logger.error(f"[RECOVERY] Orphaned task sweep failed: {e}", exc_info=True)
