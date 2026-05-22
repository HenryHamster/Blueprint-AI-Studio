"""Background service functions: GCP cleanup, secret reconciliation, and orchestrator helpers."""

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import or_, cast, func, update, text
from sqlalchemy.orm import load_only
from sqlalchemy.types import String
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    Alert,
    AlertSeverity,
)
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


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
                # Get all active tasks that should have a VM running
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = [vid for vid in active_vm_ids if vid]

                # Run cleanup with hard timeout (2 minutes)
                try:
                    cleanup_result = await asyncio.wait_for(
                        cleanup_orphaned_vms(active_vm_ids), timeout=120
                    )
                    # Log divergence and partial failures from structured result
                    if hasattr(cleanup_result, "failed") and cleanup_result.failed:
                        logger.warning(
                            f"[GCP CLEANUP] Partial failure: deleted {len(cleanup_result.deleted)}, "
                            f"failed {len(cleanup_result.failed)}: {cleanup_result.failed}"
                        )
                    # Cache total GCP VM count for orchestrator concurrency check
                    if hasattr(cleanup_result, "total_gcp_vms"):
                        import app.main as _app_main

                        _app_main._last_gcp_vm_count = cleanup_result.total_gcp_vms
                        _app_main._last_gcp_vm_check = time.time()
                except asyncio.TimeoutError:
                    logger.exception("[GCP CLEANUP] Cycle timed out after 120s")

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
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = {vid for vid in active_vm_ids if vid}

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


_STALE_BRANCH_PREFIXES = ("blueprint/", "research/", "plan/")
_PROTECTED_BRANCHES = frozenset(["main", "master", "develop", "dev"])
# Per-cycle caps. The age check costs one GitHub commit-date call per candidate
# branch (i.e. branches that already passed the terminal-task filter), plus a
# DELETE per actually-deleted branch. At 4 cycles/day these caps fit well
# inside the 5000/hr install-token budget even during backlog drain.
_MAX_DELETES_PER_PROJECT = 500
_MAX_DELETES_TOTAL = 1000
# Shortened from 7d. Age now comes from the branch HEAD commit date (external,
# immutable) rather than task.updated_at (which auto-archive's bulk UPDATE
# used to bump, silently resetting a 7d clock on every hop). Two days still
# leaves a comfortable buffer to review/merge a PR before its branch becomes
# eligible for cleanup.
_STALE_BRANCH_AGE_DAYS = 2
# Skip an entire cleanup cycle when either quota bucket is low — leaves
# headroom for task spawning and CI polling, and prevents the branch cleanup
# from being the last straw when something else has already burned the budget.
_QUOTA_FLOOR = 500  # install-token (5000/hr bucket)
_JWT_QUOTA_FLOOR = 10  # App-JWT (~60/hr bucket)

# Branch naming conventions these prefixes emit:
#   blueprint/{task|fix|refactor|docs|deps|migrate}-{short_id}
#   research/{short_id}
#   plan/{short_id}
# short_id is the first 8 hex chars of the owning task's UUID (dashes stripped).
_BRANCH_SHORT_ID_RE = re.compile(
    r"^(?:blueprint/(?:task|fix|refactor|docs|deps|migrate)-|research/|plan/)"
    r"([a-f0-9]{8})$"
)

# Statuses a task can sit in after it's done. Only branches owned by tasks in
# one of these states are eligible for stale-branch cleanup.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)


def _extract_branch_short_id(branch_name: str) -> str | None:
    """Return the 8-char task short_id embedded in a branch name, or None.

    Used by the stale branch cleanup to attribute each branch to the task that
    created it without a GitHub API round-trip.
    """
    m = _BRANCH_SHORT_ID_RE.match(branch_name)
    return m.group(1) if m else None


def _pr_possibly_open(ctx: dict | None) -> bool:
    """True if task context references a PR without an explicit closure marker.

    GitHub auto-closes any open PR whose HEAD is the deleted branch, so if a
    reviewer is still sitting on a PR, deleting the branch silently kills the
    review. We err heavily on the false-positive side: the cost of a false
    positive is one orphan branch; the cost of a false negative is a closed
    open PR and lost work.

    Closure markers set elsewhere in the codebase:
      - ci_completion.py / admin.py set pr_merged=True when a PR merges
      - review_pipeline.py sets pr_rejected=True when a review closes a PR
      - rollup_management.py sets pr_closed_unmerged=True for closed-unmerged

    PR reference keys observed across the codebase: pr_url, pr_number,
    rollup_pr_url, rollup_pr_number, review_loop.pr_number/pr_url.
    """
    if not ctx:
        return False
    if (
        ctx.get("pr_merged") is True
        or ctx.get("pr_rejected") is True
        or ctx.get("pr_closed_unmerged") is True
    ):
        return False
    review_loop = ctx.get("review_loop") or {}
    return bool(
        ctx.get("pr_url")
        or ctx.get("pr_number")
        or ctx.get("rollup_pr_url")
        or ctx.get("rollup_pr_number")
        or review_loop.get("pr_number")
        or review_loop.get("pr_url")
    )


async def _get_project_github_token(project, session):
    """Retrieve a GitHub token for a project via its UserIntegration.

    Reuses the same in-process cache as ``get_github_token_for_task`` so a
    dashboard endpoint polling /api/admin/open-prs every 10s (with its own
    60s cache) doesn't mint a fresh install token on every cache-miss.  That
    pattern was silently draining the ~60/hr App-JWT bucket even with zero
    agents running.
    """
    from app.models import UserIntegration
    from app.github import (
        generate_installation_access_token,
        get_repo_installation_token,
    )
    from app.secrets_manager import get_user_secret
    from app.routers.agent import (
        _github_token_cache,
        _github_token_cache_lock,
        _GITHUB_TOKEN_TTL,
    )

    project_id_str = str(project.id)

    # Cache check — installation tokens are good for 60min; we treat them as
    # valid for 50min (3000s) to leave safety margin for clock skew.
    async with _github_token_cache_lock:
        cached = _github_token_cache.get(project_id_str)
        if (
            cached
            and (_utcnow() - cached["fetched_at"]).total_seconds() < _GITHUB_TOKEN_TTL
        ):
            return cached["token"]

    integration = (
        (
            await session.execute(
                select(UserIntegration).where(
                    UserIntegration.user_id == project.user_id
                )
            )
        )
        .scalars()
        .first()
    )

    token: str | None = None
    if integration and integration.github_installation_id:
        token = await generate_installation_access_token(
            integration.github_installation_id,
            repositories=[project.github_repo],
        )
    elif integration and integration.github_pat:
        # PATs are long-lived — caching isn't necessary but is harmless and
        # keeps every code path symmetric.
        token = await get_user_secret(integration.github_pat)
    else:
        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )

    if token and project.github_owner and project.github_repo:
        async with _github_token_cache_lock:
            _github_token_cache[project_id_str] = {
                "token": token,
                "owner": project.github_owner,
                "repo": project.github_repo,
                "fetched_at": _utcnow(),
            }
    return token


async def _run_stale_branch_cleanup():
    """Delete branches whose owning task is terminal and HEAD commit is stale.

    Age is measured from the branch's HEAD commit date via the GitHub API, not
    from task.updated_at. Task.updated_at has `onupdate=_utcnow`, so any write
    to the task row — notably auto-archive's bulk UPDATE at the same staleness
    threshold — silently resets the clock, stalling cleanup indefinitely. The
    commit date is external and immune to that.

    Safety: each branch must pass six independent gates before deletion — name
    isn't protected, isn't on an active task, matches the known naming shape,
    resolves to a task row we own, that task is terminal, and the branch HEAD
    commit is older than the stale threshold.
    """
    from app.models import Project
    from app.github import list_branches, delete_branch, get_commit_date

    # Quota gate — skip the whole cycle if either bucket is low. Avoids the
    # fail-cascade pattern where cleanup tips an already-strained budget over.
    it_remaining = _shared_state.github_quota.get("remaining")
    jwt_remaining = _shared_state.github_jwt_quota.get("remaining")
    if it_remaining is not None and it_remaining < _QUOTA_FLOOR:
        logger.info(
            f"[BRANCH CLEANUP] Skipping cycle — install-token quota "
            f"{it_remaining} < floor {_QUOTA_FLOOR}"
        )
        return
    if jwt_remaining is not None and jwt_remaining < _JWT_QUOTA_FLOOR:
        logger.info(
            f"[BRANCH CLEANUP] Skipping cycle — JWT quota "
            f"{jwt_remaining} < floor {_JWT_QUOTA_FLOOR}"
        )
        return

    stale_age = timedelta(days=_STALE_BRANCH_AGE_DAYS)
    total_deleted = 0

    async with AsyncSessionLocal() as session:
        # Projects with GitHub repos configured
        projects = (
            (
                await session.execute(
                    select(Project).where(
                        Project.github_owner.is_not(None),
                        Project.github_repo.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        ACTIVE_STATUSES = [
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ]
        now = _utcnow()

        for project in projects:
            if total_deleted >= _MAX_DELETES_TOTAL:
                logger.info(
                    f"[BRANCH CLEANUP] Global cap ({_MAX_DELETES_TOTAL}) reached, stopping"
                )
                break

            # Collect branch names referenced by this project's active tasks only,
            # selecting only the columns needed to avoid loading large JSONB payloads.
            active_task_rows = (
                await session.execute(
                    select(Task.id, Task.context, Task.payloads).where(
                        Task.status.in_(ACTIVE_STATUSES),
                        Task.project_id == project.id,
                    )
                )
            ).all()

            active_branches: set = set()
            for row in active_task_rows:
                ctx = row.context or {}
                # Review loop branch
                rl_branch = ctx.get("review_loop", {}).get("branch")
                if rl_branch:
                    active_branches.add(rl_branch)
                # Staging branches
                for key in ("plan_branch", "research_branch", "branch"):
                    b = ctx.get(key)
                    if b:
                        active_branches.add(b)
                # Payload branches
                for payload in row.payloads or []:
                    pb = payload.get("branch")
                    if pb:
                        active_branches.add(pb)
                # Computed branch name from task ID (covers cases not in context)
                first_payload = (row.payloads or [{}])[0]
                task_type = first_payload.get("type", "task")
                type_prefix = {
                    "create": "task",
                    "fix": "fix",
                    "refactor": "refactor",
                    "docs": "docs",
                    "deps": "deps",
                    "migrate": "migrate",
                }.get(task_type, "task")
                short_id = str(row.id).replace("-", "")[:8]
                active_branches.add(f"blueprint/{type_prefix}-{short_id}")

            try:
                token = await _get_project_github_token(project, session)
                if not token:
                    logger.warning(
                        f"[BRANCH CLEANUP] No token for project {project.id}, skipping"
                    )
                    continue
            except Exception as e:
                logger.warning(
                    f"[BRANCH CLEANUP] Token error for project {project.id}: {e}"
                )
                continue

            owner, repo = project.github_owner, project.github_repo

            try:
                branches = await list_branches(
                    token,
                    owner,
                    repo,
                    prefixes=list(_STALE_BRANCH_PREFIXES),
                    max_branches=1000,
                )
            except Exception as e:
                logger.warning(
                    f"[BRANCH CLEANUP] Failed to list branches for {owner}/{repo}: {e}"
                )
                continue

            # Batch-load task metadata for every branch whose name matches our
            # known short_id shape. One SQL query replaces ~N GitHub calls.
            branch_short_ids = {
                sid for b in branches if (sid := _extract_branch_short_id(b["name"]))
            }
            # short_id -> (status, context). context is loaded so Gate 5.5 can
            # check for PR closure markers before deleting the branch.
            task_meta: dict[str, tuple] = {}
            if branch_short_ids:
                rows = (
                    await session.execute(
                        select(Task.id, Task.status, Task.context).where(
                            Task.project_id == project.id,
                            func.substr(cast(Task.id, String), 1, 8).in_(
                                branch_short_ids
                            ),
                        )
                    )
                ).all()
                for row in rows:
                    sid = str(row.id).replace("-", "")[:8]
                    task_meta[sid] = (row.status, row.context)

            project_deleted = 0
            for branch_info in branches:
                if project_deleted >= _MAX_DELETES_PER_PROJECT:
                    break
                if total_deleted >= _MAX_DELETES_TOTAL:
                    break

                name = branch_info["name"]

                # Gate 1: never touch protected branches
                if name in _PROTECTED_BRANCHES:
                    continue

                # Gate 2: skip if the name matches any active task's branch
                if name in active_branches:
                    continue

                # Gate 3: name must match the known cleanup shape. Unknown
                # shapes (hand-created branches someone kept around, legacy
                # naming, etc.) are left alone.
                short_id = _extract_branch_short_id(name)
                if not short_id:
                    continue

                # Gate 4: short_id must resolve to a task we own. Missing
                # means the task row was deleted or the branch belongs to a
                # different system — in either case, not ours to delete.
                meta = task_meta.get(short_id)
                if meta is None:
                    continue
                status, ctx = meta

                # Gate 5: task must be terminal. This catches the case where
                # a research/plan task is active but active_branches didn't
                # include its computed `research/{short_id}` or
                # `plan/{short_id}` name (the existing active_branches
                # derivation only computes `blueprint/{prefix}-{short_id}`).
                if status not in _TERMINAL_TASK_STATUSES:
                    continue

                # Gate 5.5: if the task opened a PR and no closure marker is
                # set, the PR may still be open awaiting review. Deleting the
                # branch would auto-close it and lose the reviewer's context.
                if _pr_possibly_open(ctx):
                    continue

                # Gate 6: commit-date age check. Uses the branch HEAD commit
                # date from GitHub — external and immutable, unlike
                # task.updated_at which any row-write can reset.
                commit_date = await get_commit_date(
                    token, owner, repo, branch_info["sha"]
                )
                if commit_date is None:
                    # API error or missing commit — leave the branch alone;
                    # next cycle will retry.
                    continue
                if commit_date.tzinfo is not None:
                    commit_date = commit_date.replace(tzinfo=None)
                if (now - commit_date) < stale_age:
                    continue

                try:
                    deleted = await delete_branch(token, owner, repo, name)
                    if deleted:
                        age_days = (now - commit_date).days
                        logger.info(
                            f"[BRANCH CLEANUP] Deleted {name} from {owner}/{repo} "
                            f"(task {short_id}, {status}, age: {age_days}d)"
                        )
                        project_deleted += 1
                        total_deleted += 1
                except Exception as e:
                    logger.warning(f"[BRANCH CLEANUP] Failed to delete {name}: {e}")

            if project_deleted > 0:
                logger.info(
                    f"[BRANCH CLEANUP] Deleted {project_deleted} stale branch(es) "
                    f"from {owner}/{repo}"
                )

            # INFO alert when many branches cleaned in one cycle
            if project_deleted > 10:
                try:
                    alert = Alert(
                        project_id=project.id,
                        severity=AlertSeverity.INFO,
                        title=f"Cleaned up {project_deleted} stale branches",
                        message=(
                            f"Stale branch cleanup removed {project_deleted} branches from "
                            f"{owner}/{repo} that were older than {_STALE_BRANCH_AGE_DAYS} days "
                            f"with no corresponding active tasks."
                        ),
                    )
                    session.add(alert)
                    await session.commit()
                    await broadcast_to_project(
                        str(project.id),
                        "alert_created",
                        {
                            "alert_id": str(alert.id),
                            "severity": "INFO",
                            "title": alert.title,
                        },
                    )
                except Exception as e:
                    logger.warning(
                        f"[BRANCH CLEANUP] Failed to create cleanup alert: {e}"
                    )

    if total_deleted > 0:
        logger.info(f"[BRANCH CLEANUP] Total deleted this cycle: {total_deleted}")


async def stale_branch_cleanup_service():
    """Periodic cleanup of stale blueprint/research/plan branches (default every 6 hours)."""
    while True:
        try:
            await _run_stale_branch_cleanup()
        except asyncio.CancelledError:
            logger.info("[BRANCH CLEANUP] Service shutting down.")
            break
        except Exception as e:
            # Cloud Run instance shutdowns mid-cycle close the connection while
            # the long-lived AsyncSessionLocal context is still open; the
            # __aexit__ rollback then raises InterfaceError("connection is
            # closed"). Branches were already deleted before the rollback
            # fired — this is shutdown collateral, not a cleanup failure.
            # Demote to a one-line warning so the dashboard doesn't flag it.
            msg = str(e)
            is_close_collateral = (
                "connection is closed" in msg
                or "InterfaceError" in type(e).__name__
            )
            if is_close_collateral:
                logger.warning(
                    f"[BRANCH CLEANUP] Session closed mid-cycle "
                    f"(instance shutdown collateral): {type(e).__name__}: {e}"
                )
            else:
                logger.error(f"[BRANCH CLEANUP] Error: {e}", exc_info=True)

        interval_minutes = _shared_state.runtime_config.get(
            "branch_cleanup_interval_minutes", 360
        )
        try:
            await asyncio.sleep(interval_minutes * 60)
        except asyncio.CancelledError:
            logger.info("[BRANCH CLEANUP] Service shutting down.")
            break


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


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    from app.deploy_health import _get_deploy_state_for_update

    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            # Prune: don't persist CLOSED breakers with no failures (default state)
            if (
                v.get("state") == "CLOSED"
                and v.get("failure_count", 0) == 0
                and v.get("infra_failure_count", 0) == 0
            ):
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
    from app.deploy_health import (
        _get_deploy_state_for_update,
        _pause_failure_streak,
        _health_recovery_history,
        _last_auto_resume_at,
        _recent_auto_resumes,
    )

    try:
        from app.compute import _zone_health, _zone_lock

        def _serialize_value(v):
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, deque):
                return list(v)
            return v

        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {k: _serialize_value(v) for k, v in data.items()}
        snapshot["_pause_failure_streak"] = _pause_failure_streak
        snapshot["_health_recovery_history"] = list(_health_recovery_history)
        snapshot["_last_auto_resume_at"] = (
            _last_auto_resume_at.isoformat() if _last_auto_resume_at else None
        )
        snapshot["_recent_auto_resumes"] = [t.isoformat() for t in _recent_auto_resumes]
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


async def _chain_has_active_pipeline_work(
    parent: Task,
    session,
    stale_seconds: int = 600,
    queued_max_age_seconds: int = 1800,
) -> bool:
    """True if any review/fix task on this chain has heartbeated within ``stale_seconds``.

    The backstop uses wall-clock time as the sole kill signal, but a rollup PR
    that's legitimately cycling through REQUEST_CHANGES → fix → re-review can
    easily exceed the 30-min CI backstop while the pipeline is actively making
    progress.  Before killing such a task, check whether there is a review or
    fix task on the same chain (matching pr_number + original_task_id) whose
    last_heartbeat is recent — if so, the pipeline is alive and we defer to
    the next sweep.
    """
    rl = (parent.context or {}).get("review_loop", {})
    pr_number = rl.get("pr_number")
    original_id = rl.get("original_task_id") or str(parent.id)
    if not pr_number:
        return False
    try:
        candidates = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == parent.project_id,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        cutoff = _utcnow() - timedelta(seconds=stale_seconds)
        for t in candidates:
            fp = t.first_payload
            if fp.get("type") not in ("review", "fix"):
                continue
            if fp.get("pr_number") != pr_number:
                continue
            if (fp.get("original_task_id") or "") != original_id:
                continue
            if t.last_heartbeat and t.last_heartbeat > cutoff:
                return True
            # QUEUED/PROVISIONING tasks have no heartbeat yet but count as live
            # work only if recently created — beyond queued_max_age_seconds they
            # are likely stuck (slot caps, direction holds) and must not suppress
            # the parent's hard timeout.
            if t.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING):
                queued_cutoff = _utcnow() - timedelta(seconds=queued_max_age_seconds)
                if t.created_at and t.created_at > queued_cutoff:
                    return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check chain activity for task {str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _chain_has_recoverable_failed_work(parent: Task, session) -> bool:
    """True if the chain has FAILED tasks with infra-class failure_category.

    The 6h EVALUATING wall-clock was designed for live deadlock — child stuck
    IN_PROGRESS, no heartbeat, parent waiting forever. It misfires when children
    are FAILED but RECOVERABLE: a PROVISION_FAILURE / RATE_LIMIT / EXTERNAL_API
    child is eligible for the resurrection sweep once zones recover, and an
    operator deploying a real fix may requeue these children.  Cascade-killing
    them at the wall-clock destroys recoverable work.

    Returns True if any direct child OR any chain pipeline task (review/fix
    matching this parent's chain) is FAILED with failure_category in
    INFRA_CLASSES. The caller defers the kill in that case for a bounded window.
    """
    from app.failure_classification import INFRA_CLASSES

    direct_children = (
        (await session.execute(select(Task).where(Task.parent_task_id == parent.id)))
        .scalars()
        .all()
    )
    direct_child_ids = {str(c.id) for c in direct_children}

    for c in direct_children:
        if c.status != TaskStatus.FAILED:
            continue
        cat = (c.context or {}).get("failure_category")
        if cat in INFRA_CLASSES:
            return True

    if not direct_child_ids:
        return False

    # Pipeline / descendant tasks whose original_task_id points at one of our
    # direct children — covers review/fix tasks attached to create children.
    try:
        candidates = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.project_id == parent.project_id)
                    .where(Task.status == TaskStatus.FAILED)
                    .order_by(Task.created_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        for t in candidates:
            orig = t.first_payload.get("original_task_id") or ""
            if orig not in direct_child_ids:
                continue
            cat = (t.context or {}).get("failure_category")
            if cat in INFRA_CLASSES:
                return True
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to check chain recoverability for task {str(parent.id)[:8]}",
            exc_info=True,
        )
    return False


async def _summarize_latest_review_for_chain(parent: Task, session) -> dict | None:
    """Return a small summary of the most recent review task on the same chain.

    Used by the CI-wait backstop to build an honest diagnostic reason when the
    review-approved gate never closed. Matches on project + pr_number + the
    parent's chain id (original_task_id or parent.id).
    """
    rl = (parent.context or {}).get("review_loop", {})
    pr_number = rl.get("pr_number")
    original_id = rl.get("original_task_id") or str(parent.id)
    if not pr_number:
        return None
    try:
        # Bound the candidate set: filter to relevant statuses and recent rows.
        # Payload-based filtering (type/pr_number/original_task_id) still happens
        # in Python since these live in JSONB.
        candidates = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.project_id == parent.project_id)
                    .where(
                        Task.status.in_(
                            (
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                            )
                        )
                    )
                    .order_by(Task.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        matches = [
            t
            for t in candidates
            if t.first_payload.get("type") == "review"
            and t.first_payload.get("pr_number") == pr_number
            and (t.first_payload.get("original_task_id") or "") == original_id
        ]
        if not matches:
            return None
        # datetime.min is a naive sentinel matching our naive-UTC column convention;
        # only used when created_at is unset (should not happen — DB sets a default).
        latest = max(matches, key=lambda t: t.created_at or datetime.min)  # noqa: DTZ901
        status_val = (
            latest.status.value
            if hasattr(latest.status, "value")
            else str(latest.status)
        )
        return {
            "id": str(latest.id),
            "short_id": str(latest.id)[:8],
            "status": status_val,
            "last_heartbeat": latest.last_heartbeat,
        }
    except Exception:
        logger.debug(
            f"[EVAL TIMEOUT] Failed to summarize review for task {str(parent.id)[:8]}",
            exc_info=True,
        )
        return None


# Threshold for considering a never-claimed QUEUED pipeline task "abandoned"
# during CI backstop recovery. Matches the queued_max_age_seconds default in
# _chain_has_active_pipeline_work so the two paths agree on what "stuck" means.
_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS = 30 * 60


async def _supersede_long_queued_pipeline_tasks(
    parent: Task,
    session,
    pr_number: int,
    original_task_id: str,
) -> int:
    """Cancel review/fix tasks on this chain that have been QUEUED past the
    abandon threshold without ever being claimed.

    Called from CI backstop recovery to unblock the dedup guard in
    _spawn_pipeline_task — a fix task that's sat in the queue for an hour
    waiting for an account is the same blocker as a dead VM, just slower
    to detect. Without this, the recovery review spawn defers and the
    parent gets hard-killed when it could have been recovered.

    Returns the count of tasks superseded so the caller can log it.
    """
    from app.failure_classification import AGENT_TIMEOUT, persist_failure_category

    cutoff = _utcnow() - timedelta(seconds=_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS)
    candidates = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == parent.project_id,
                    Task.status == TaskStatus.QUEUED,
                )
            )
        )
        .scalars()
        .all()
    )
    superseded = 0
    for t in candidates:
        if t.id == parent.id:
            continue
        fp = t.first_payload
        if fp.get("type") not in ("review", "fix"):
            continue
        if fp.get("pr_number") != pr_number:
            continue
        if (fp.get("original_task_id") or "") != original_task_id:
            continue
        # Skip tasks that have actually been claimed and are working.
        if t.last_heartbeat is not None or t.started_at is not None:
            continue
        if t.created_at and t.created_at >= cutoff:
            continue  # too fresh — could legitimately be waiting on capacity
        t.status = TaskStatus.CANCELLED
        t.latest_status_message = (
            f"Superseded by CI backstop recovery on PR #{pr_number}: "
            f"queued >{_QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS // 60}min "
            f"without claim, blocking parent recovery"
        )
        persist_failure_category(t, AGENT_TIMEOUT)
        session.add(t)
        superseded += 1
        logger.warning(
            f"[BACKSTOP RECOVERY] Cancelling long-queued {fp.get('type')} "
            f"{str(t.id)[:8]} on PR #{pr_number} so recovery review can proceed"
        )
    if superseded:
        await session.flush()
    return superseded


async def _attempt_ci_backstop_recovery(
    parent: Task, session, review_loop: dict
) -> bool:
    """One-shot attempt to unstick a parent whose review gate never closed.

    Used when CI has passed but review_approved is absent — typically because
    the original review VM died (rate-limit, crash) before posting status.
    Spawns a fresh review via the normal pipeline path; the new dedup guard
    will supersede any stale corpse on the same chain.

    Before spawning, supersedes any review/fix on this chain that has sat in
    QUEUED past the abandon threshold (account exhaustion, slot starvation).
    Without this step, _spawn_pipeline_task's dedup guard defers the recovery
    review and the parent gets hard-killed instead of recovered.

    Returns True iff a fresh review was spawned (parent should reset its CI
    wait window and skip the hard kill for this tick).
    """
    from app.review_pipeline import _spawn_pipeline_task

    pr_number = review_loop.get("pr_number")
    branch = review_loop.get("branch")
    base_branch = review_loop.get("base_branch", "main")
    original_task_id = review_loop.get("original_task_id") or str(parent.id)
    if not (pr_number and branch):
        return False
    try:
        await _supersede_long_queued_pipeline_tasks(
            parent,
            session,
            pr_number,
            original_task_id,
        )
        review_task, _ = await _spawn_pipeline_task(
            parent,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=(parent.description or parent.title or ""),
            original_task_id=original_task_id,
        )
        return review_task is not None
    except Exception as e:
        logger.warning(
            f"[EVAL TIMEOUT] Backstop review-recovery spawn failed for task "
            f"{str(parent.id)[:8]}: {e}",
            exc_info=True,
        )
        return False


async def _check_evaluating_timeouts():
    """Detect EVALUATING tasks that are stuck and recover or fail them.

    - If all children are terminal but parent is still EVALUATING → trigger missed rollup.
    - If evaluating >2h with non-terminal children → WARNING alert.
    - If evaluating >evaluating_fail_hours (default 48h) → FAIL with ERROR alert.
    - Skips CI-waiting tasks (they have their own 20-min escalation timeout).
    """
    from app.main import _maybe_create_failure_alert
    from app.rollup_management import _check_and_trigger_rollup
    from app.shared_state import runtime_config as _runtime_config

    EVALUATING_WARN_SECONDS = 2 * 3600  # 2 hours
    # Default backstop bumped to 48h while silent-death v2 stabilizes
    # (2026-05-04): tasks reaped mid-flight by the 6h wall-clock were getting
    # cascade-killed during operator-intervention windows. Runtime knob
    # `evaluating_fail_hours` lets us drop it back once the recurrence rate
    # for silent-death + reaper sweeps comes down.
    # Defensive read: the PATCH endpoint validates 1..168, but a corrupted
    # runtime_config_json on disk could surface a non-int / out-of-range value
    # which would raise ValueError on `int(...)` and abort the whole sweep
    # cycle silently — no EVALUATING cleanup until someone PATCHes a fix.
    try:
        _eval_hours_raw = _runtime_config.get("evaluating_fail_hours", 48)
        _eval_hours = int(_eval_hours_raw)
        if not (1 <= _eval_hours <= 168):
            _eval_hours = 48
    except (ValueError, TypeError):
        _eval_hours = 48
    EVALUATING_FAIL_SECONDS = _eval_hours * 3600
    # Hard kill for CI-waiting tasks. Tunable so multi-iteration review/fix
    # cycles (each ~5-15 min, plus pipeline-cap queueing) don't trigger a
    # premature kill that cancels in-flight children. 60 min is the default
    # because realistic cycles routinely run 30-50 min; 30 min was firing
    # mid-iteration during pipeline-throttled periods.
    CI_BACKSTOP_SECONDS = (
        int(_runtime_config.get("ci_backstop_timeout_minutes", 60)) * 60
    )
    # Approval-gate companion timeout: parents flagged requires_approval can
    # otherwise stall indefinitely when _chain_has_active_pipeline_work defers
    # the EVALUATING_FAIL backstop. 2h gives operators a reasonable window
    # to approve/deny independent of the configured wall-clock.
    APPROVAL_GATE_FAIL_SECONDS = 2 * 3600
    TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    try:
        async with AsyncSessionLocal() as session:
            from app import pause_clock
            from app.models import DeploymentState

            now = _utcnow()
            # Fetch DeployState once per cycle so CI backstop can subtract
            # pause windows from elapsed (admin pause + auto-pause both count).
            _deploy_state = await session.get(DeploymentState, 1)
            # Phase B4 (H-P2): drop ``plan_output`` JSONB; the EVALUATING-timeout
            # loop only reads context/payloads/status_log of the heavy JSONB
            # columns. status_log is consumed at lines ~1216 and ~1384 to find
            # the last EVALUATING transition, so it must remain loaded.
            evaluating_tasks = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.title,
                                Task.project_id,
                                Task.parent_task_id,
                                Task.archived,
                                Task.requires_approval,
                                Task.created_at,
                                Task.started_at,
                                Task.updated_at,
                                Task.latest_status_message,
                                Task.context,
                                Task.payloads,
                                Task.status_log,
                                Task.last_heartbeat,
                                Task.attempt_count,
                                Task.account_id,
                                Task.vm_instance_id,
                            )
                        )
                        .where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.archived.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )

            # Phase B4 (H-P2): batch the child lookup for all evaluating parents
            # into one ``IN (...)`` query instead of one-per-parent (N+1).
            #
            # Only a plain (id, status) snapshot is kept — NOT live ORM rows.
            # This loop commits per task, and a commit/rollback inside
            # _check_and_trigger_rollup expires every loaded instance; a later
            # iteration reading ``child.status`` off a stale batch fires a sync
            # lazy-load that crashes the cycle on an unhealthy connection (prod
            # 2026-05-21, services.py:1868). The hot all-terminal check below
            # reads the snapshot; the rare branches that mutate children
            # re-fetch them fresh.
            _eval_child_status_by_parent: Dict[uuid.UUID, list[tuple]] = {}
            if evaluating_tasks:
                _eval_parent_ids = [t.id for t in evaluating_tasks]
                _eval_child_rows = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id, Task.parent_task_id, Task.status
                                )
                            )
                            .where(Task.parent_task_id.in_(_eval_parent_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for _ec in _eval_child_rows:
                    _eval_child_status_by_parent.setdefault(
                        _ec.parent_task_id, []
                    ).append((_ec.id, _ec.status))

            for task in evaluating_tasks:
                ctx = task.context or {}

                # _pr_state_check_pending consumer. The agent_status FAILED→
                # EVALUATING recovery (agent_status.py ~1567) sets this flag
                # before its GH PR check; if the post-GH lock re-acquire
                # races a concurrent writer (skip_locked returns None at
                # ~1616) the recovery returns ``accepted_pr_check_deferred``
                # leaving the flag stuck and no review task spawned. Without
                # this consumer the task sits until the 48h reaper backstop.
                if ctx.get("_pr_state_check_pending"):
                    pr_num = (ctx.get("review_loop") or {}).get("pr_number") or ctx.get(
                        "rollup_pr_number"
                    )
                    if not pr_num:
                        for entry in reversed(task.status_log or []):
                            m = re.search(r"open PR #(\d+)", entry.get("msg", "") or "")
                            if m:
                                pr_num = int(m.group(1))
                                break
                    # 5-min grace: let the original recovery's lock finish if it's
                    # going to. Without this we'd race the same code path.
                    flag_set_at = None
                    for entry in reversed(task.status_log or []):
                        if "checking PR state" in (entry.get("msg", "") or ""):
                            with contextlib.suppress(KeyError, ValueError, TypeError):
                                flag_set_at = datetime.fromisoformat(entry["t"])
                            break
                    if flag_set_at and (now - flag_set_at).total_seconds() < 300:
                        continue

                    from sqlalchemy.orm.attributes import flag_modified

                    _ctx2 = dict(ctx)
                    _ctx2.pop("_pr_state_check_pending", None)

                    if not pr_num:
                        # No PR number recoverable — clear flag, fall through to
                        # the normal evaluating timeout logic below.
                        task.context = _ctx2
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        ctx = _ctx2
                    else:
                        gh_pr_state = "open"
                        gh_pr_merged = False
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import get_pull_request

                            gh_token, owner, repo = await get_github_token_for_task(
                                task.id, session
                            )
                            pr_data = await get_pull_request(
                                gh_token, owner, repo, pr_num
                            )
                            gh_pr_state = pr_data.get("state") or "open"
                            gh_pr_merged = bool(pr_data.get("merged"))
                        except Exception as gh_exc:
                            logger.warning(
                                f"[EVAL TIMEOUT pr_check] Task {str(task.id)[:8]} "
                                f"PR #{pr_num} GH lookup failed: {type(gh_exc).__name__}: "
                                f"{gh_exc} — assuming open"
                            )

                        if gh_pr_state == "closed" and gh_pr_merged:
                            task.status = TaskStatus.COMPLETED
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = f"Recovered _pr_state_check_pending: PR #{pr_num} merged out-of-band"
                            _ctx2["pr_merged"] = True
                        elif gh_pr_state == "closed" and not gh_pr_merged:
                            task.status = TaskStatus.FAILED
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = f"Recovered _pr_state_check_pending: PR #{pr_num} closed without merge"
                            _ctx2["pr_closed_unmerged"] = True
                        else:
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            try:
                                from app.review_pipeline import _spawn_pipeline_task

                                short_id = str(task.id).replace("-", "")[:8]
                                task_type_str = (task.first_payload or {}).get(
                                    "type", ""
                                ) or ""
                                type_prefix_map = {
                                    "docs": "docs",
                                    "deps": "deps",
                                    "migrate": "migrate",
                                    "refactor": "refactor",
                                    "perf": "task",
                                    "qa": "task",
                                    "security_audit": "task",
                                    "debug": "task",
                                    "smoketest": "task",
                                }
                                branch_prefix = type_prefix_map.get(
                                    task_type_str, "task"
                                )
                                branch = f"blueprint/{branch_prefix}-{short_id}"
                                base_branch = (task.first_payload or {}).get(
                                    "base_branch", "main"
                                )
                                original_prompt = (task.first_payload or {}).get(
                                    "prompt", task.description or ""
                                )
                                await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    pr_num,
                                    branch,
                                    base_branch,
                                    original_prompt=original_prompt,
                                    original_task_id=str(task.id),
                                )
                                task.latest_status_message = f"Recovered _pr_state_check_pending: spawned review for PR #{pr_num}"
                            except Exception as spawn_exc:
                                logger.warning(
                                    f"[EVAL TIMEOUT pr_check] Review spawn failed for "
                                    f"task {str(task.id)[:8]}: {type(spawn_exc).__name__}: "
                                    f"{spawn_exc}",
                                    exc_info=True,
                                )
                                task.latest_status_message = "Recovered _pr_state_check_pending: review spawn failed, will retry"

                        task.context = _ctx2
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status},
                        )
                        continue

                # Approval-gate hard timeout. Parents with requires_approval=True
                # remain in EVALUATING waiting for human approval; the
                # evaluating_fail_hours backstop can be deferred by an active
                # pipeline chain, so approval-gated parents need their own
                # shorter companion timeout.
                if task.requires_approval:
                    approval_set_at = ctx.get("_approval_set_at")
                    if not approval_set_at:
                        from sqlalchemy.orm.attributes import flag_modified

                        task.context = {**ctx, "_approval_set_at": now.isoformat()}
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        continue
                    try:
                        approval_since = datetime.fromisoformat(approval_set_at)
                        approval_elapsed = (now - approval_since).total_seconds()
                    except (ValueError, TypeError):
                        approval_elapsed = 0.0
                    if approval_elapsed > APPROVAL_GATE_FAIL_SECONDS:
                        hours = round(approval_elapsed / 3600, 1)
                        logger.error(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been "
                            f"requires_approval for {hours}h without resolution — "
                            f"approval-gate timeout, marking FAILED"
                        )
                        task.status = TaskStatus.FAILED
                        task.latest_status_message = (
                            f"Approval-gate timeout after {hours}h — no manual "
                            f"approval received, escalating to FAILED"
                        )
                        from app.failure_classification import (
                            classify_failure,
                            persist_failure_category,
                        )
                        from app.routers.agent_status import record_task_result

                        _fc = classify_failure(task)
                        persist_failure_category(task, _fc)
                        record_task_result(
                            success=False,
                            failure_class=_fc,
                            task_id=str(task.id),
                            task_type=task.first_payload.get("type", ""),
                        )
                        session.add(task)
                        await _maybe_create_failure_alert(
                            task,
                            session,
                            f"Approval-gate timeout after {hours}h — parent failed "
                            f"without manual resolution",
                        )
                        await session.commit()
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status},
                        )
                        continue

                ci_waiting = (
                    ctx.get("ci_waiting") is True
                    or ctx.get("review_loop", {}).get("ci_waiting") is True
                )
                if ci_waiting:
                    # CI-waiting tasks have their own 20-min escalation in orchestrator.py.
                    # Apply a 30-minute absolute backstop so tasks can never
                    # hang forever regardless of field state.
                    #
                    # Time base: prefer review_loop.ci_wait_started (when CI wait
                    # actually began) over the last EVALUATING transition. A task
                    # may sit in EVALUATING for a long time waiting on subtasks
                    # before the rollup PR is opened and CI polling starts — using
                    # the EVALUATING transition would false-positive the backstop
                    # the instant CI finally begins.
                    rl = ctx.get("review_loop", {})
                    ci_ws_val = rl.get("ci_wait_started")
                    was_escalated = rl.get("escalated", False)
                    evaluating_since = None
                    ci_ws_valid = False
                    if ci_ws_val is not None:
                        try:
                            evaluating_since = datetime.fromisoformat(ci_ws_val)
                            ci_ws_valid = True
                        except (ValueError, TypeError):
                            evaluating_since = None
                    if evaluating_since is None:
                        for entry in reversed(task.status_log or []):
                            msg = entry.get("msg", "")
                            if "→ EVALUATING" in msg:
                                with contextlib.suppress(
                                    KeyError, ValueError, TypeError
                                ):
                                    evaluating_since = datetime.fromisoformat(
                                        entry["t"]
                                    )
                                break
                    if evaluating_since is None:
                        evaluating_since = task.started_at or task.created_at

                    # Pause-aware elapsed: subtract any pause windows that
                    # occurred since the timer started. Legacy timers without
                    # a stashed baseline fall back to raw elapsed.
                    _ci_baseline = rl.get("ci_wait_pause_baseline")
                    elapsed = (
                        pause_clock.active_elapsed(
                            _deploy_state,
                            evaluating_since,
                            _ci_baseline,
                        )
                        if _deploy_state
                        else (now - evaluating_since).total_seconds()
                    )
                    # Early trigger: if CI has passed but review_approved is
                    # missing AND the review child task is already terminal
                    # (FAILED/CANCELLED), the review VM died silently — we
                    # have enough signal to recover now without waiting for
                    # the 30-min backstop. The elapsed guard remains as the
                    # fallback for cases where review status hasn't become
                    # terminal yet.
                    rl_ci_passed = bool(rl.get("ci_passed"))
                    rl_review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    rl_recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    early_recovery_eligible = False
                    if (
                        rl_ci_passed
                        and not rl_review_approved
                        and not rl_recovery_attempted
                    ):
                        _early_summary = await _summarize_latest_review_for_chain(
                            task, session
                        )
                        if _early_summary and _early_summary["status"] in (
                            "FAILED",
                            "CANCELLED",
                        ):
                            early_recovery_eligible = True
                    if elapsed <= CI_BACKSTOP_SECONDS and not early_recovery_eligible:
                        # Under backstop threshold — let normal CI timeout handle it
                        continue
                    # Pipeline still actively working on the chain (review/fix
                    # task heartbeated within 10min) — defer to next sweep so
                    # we don't kill legitimate in-flight iteration cycles.
                    if await _chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop but chain "
                            f"has fresh review/fix heartbeat — skipping kill"
                        )
                        continue
                    hours = round(elapsed / 3600, 1)
                    ci_passed = bool(rl.get("ci_passed"))
                    review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    review_summary = await _summarize_latest_review_for_chain(
                        task, session
                    )

                    # Compose an honest reason. The historic "CI polling stalled after
                    # escalation" was a lie when the actual blocker was a missing review
                    # callback (review VM died silently) — name the gate that is open.
                    if ci_ws_val is None:
                        reason = "ci_wait_started missing from review_loop"
                    elif not ci_ws_valid:
                        reason = f"ci_wait_started corrupt: {ci_ws_val!r}"
                    elif ci_passed and not review_approved:
                        if review_summary:
                            reason = (
                                f"CI passed but review {review_summary['short_id']} is "
                                f"{review_summary['status']} without verdict callback"
                            )
                        else:
                            reason = (
                                "CI passed but no review task exists for this chain"
                            )
                    elif review_approved and not ci_passed:
                        reason = "review approved but CI never completed within backstop window"
                    elif was_escalated:
                        reason = "both gates unmet after 20-min escalation"
                    else:
                        reason = (
                            "neither CI nor review completed within backstop window"
                        )

                    # One-shot recovery: if CI passed but the review-approved gate never
                    # closed (common when the review VM died before POSTing status),
                    # spawn a fresh review and reset the CI-wait clock instead of hard
                    # killing. The dedup guard will supersede any stale corpse.
                    if ci_passed and not review_approved and not recovery_attempted:
                        recovered = await _attempt_ci_backstop_recovery(
                            task, session, rl
                        )
                        if recovered:
                            from sqlalchemy.orm.attributes import flag_modified

                            new_rl = dict(rl)
                            new_rl["review_recovery_attempted"] = True
                            new_rl["review_recovery_at"] = _utcnow().isoformat()
                            new_rl["ci_wait_started"] = _utcnow().isoformat()
                            new_rl["ci_wait_pause_baseline"] = (
                                pause_clock.total_paused_seconds(_deploy_state)
                                if _deploy_state
                                else 0.0
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": new_rl,
                            }
                            flag_modified(task, "context")
                            task.latest_status_message = (
                                f"CI passed on PR #{new_rl.get('pr_number')} but review never "
                                f"completed ({reason}) — spawned fresh review, backstop reset"
                            )
                            session.add(task)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": task.status},
                            )
                            logger.warning(
                                f"[EVAL TIMEOUT] Backstop recovery for task "
                                f"{str(task.id)[:8]}: spawned fresh review on "
                                f"PR #{rl.get('pr_number')} ({reason})"
                            )
                            continue

                    logger.error(
                        f"[EVAL TIMEOUT] CI-waiting task {str(task.id)[:8]} has been "
                        f"EVALUATING for {hours}h — backstop timeout ({reason}), marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"CI wait backstop timeout after {hours}h ({reason})"
                    )
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )
                    from app.routers.agent_status import record_task_result

                    _fc = classify_failure(task)
                    persist_failure_category(task, _fc)
                    record_task_result(
                        success=False,
                        failure_class=_fc,
                        task_id=str(task.id),
                        task_type=task.first_payload.get("type", ""),
                    )
                    session.add(task)
                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and this branch reads + mutates the live
                    # rows (cancel, latest_status_message, vm_instance_id).
                    children = (
                        (
                            await session.execute(
                                select(Task).where(
                                    Task.parent_task_id == task.id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    from app.main import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    child_cancel_msg = f"Cancelled: parent CI wait backstop timeout after {hours}h ({reason})"
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = child_cancel_msg
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"CI wait backstop timeout after {hours}h — {reason}",
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    for child in non_terminal_children:
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": child.status},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="ci_backstop_timeout")
                    continue

                # Determine how long the task has been EVALUATING
                evaluating_since = None
                # Walk status_log backwards for the last EVALUATING transition
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        break
                if evaluating_since is None:
                    # Fallback to started_at or created_at
                    evaluating_since = task.started_at or task.created_at

                elapsed = (now - evaluating_since).total_seconds()

                # Child statuses snapshotted at batch-fetch time (plain tuples).
                # Reading live ORM children here would fire a sync lazy-load if an
                # earlier iteration's commit/rollback expired the shared batch.
                child_status_snap = _eval_child_status_by_parent.get(task.id, [])

                all_children_terminal = bool(child_status_snap) and all(
                    c_status in TERMINAL for _, c_status in child_status_snap
                )

                # Recovery for missed rollup: all children done but parent still EVALUATING
                if all_children_terminal:
                    logger.warning(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has all children terminal "
                        f"but is still EVALUATING — triggering missed rollup"
                    )
                    # Re-fetch a terminal child fresh to trigger rollup.
                    trigger_child = await session.get(
                        Task, child_status_snap[0][0]
                    )
                    if trigger_child is not None:
                        await _check_and_trigger_rollup(trigger_child, session)
                    await session.commit()
                    continue

                # 6-hour hard timeout
                if elapsed > EVALUATING_FAIL_SECONDS:
                    # Same protection as the CI backstop: if the chain has an
                    # actively heartbeating review/fix task, the pipeline is
                    # still doing real work — defer to next sweep.
                    if await _chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                            f"{EVALUATING_FAIL_SECONDS // 3600}h hard timeout but "
                            f"chain has fresh review/fix heartbeat — skipping kill"
                        )
                        continue

                    # Recoverable-FAILED defer: a child / chain pipeline task in
                    # FAILED with infra-class failure_category may come back via
                    # resurrection sweep or operator requeue. Cascade-killing
                    # mid-recovery destroys salvageable work (incident
                    # 2026-04-29: PRs #3450/#3408 + parents c30701d3/ddd987fc).
                    # Defer for a bounded window so a truly stuck case still
                    # terminates eventually.
                    EVAL_DEFER_WINDOW_HOURS = 24
                    first_defer_at = ctx.get("_eval_timeout_first_defer_at")
                    # Treat a defer marker older than evaluating_since as stale
                    # (left over from a previous EVALUATING transition).
                    if first_defer_at:
                        try:
                            _fd = datetime.fromisoformat(first_defer_at)
                            if _fd < evaluating_since:
                                first_defer_at = None
                        except (ValueError, TypeError):
                            first_defer_at = None
                    has_recoverable = await _chain_has_recoverable_failed_work(
                        task, session
                    )
                    if has_recoverable:
                        if first_defer_at:
                            try:
                                _fd = datetime.fromisoformat(first_defer_at)
                                defer_elapsed = (now - _fd).total_seconds()
                            except (ValueError, TypeError):
                                defer_elapsed = 0.0
                            if defer_elapsed <= EVAL_DEFER_WINDOW_HOURS * 3600:
                                logger.info(
                                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                    f"{EVALUATING_FAIL_SECONDS // 3600}h but chain "
                                    f"has recoverable FAILED children — deferring "
                                    f"({round(defer_elapsed / 3600, 1)}h into "
                                    f"{EVAL_DEFER_WINDOW_HOURS}h window)"
                                )
                                continue
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                                f"{EVAL_DEFER_WINDOW_HOURS}h defer window despite recoverable "
                                f"FAILED children — proceeding to kill"
                            )
                            # fall through to existing kill
                        else:
                            from sqlalchemy.orm.attributes import flag_modified

                            task.context = {
                                **ctx,
                                "_eval_timeout_first_defer_at": now.isoformat(),
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                f"{EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable FAILED children — deferring cascade-cancel "
                                f"for up to {EVAL_DEFER_WINDOW_HOURS}h"
                            )
                            await _maybe_create_failure_alert(
                                task,
                                session,
                                f"Evaluation past {EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable infra failures in chain — deferring "
                                f"cascade-cancel for up to {EVAL_DEFER_WINDOW_HOURS}h",
                            )
                            await session.commit()
                            continue

                    hours = round(elapsed / 3600, 1)
                    logger.error(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                        f"for {hours}h — hard timeout, marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Evaluation timeout — children did not complete "
                        f"within {EVALUATING_FAIL_SECONDS // 3600} hours"
                    )
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )
                    from app.routers.agent_status import record_task_result

                    _fc = classify_failure(task)
                    persist_failure_category(task, _fc)
                    record_task_result(
                        success=False,
                        failure_class=_fc,
                        task_id=str(task.id),
                        task_type=task.first_payload.get("type", ""),
                    )
                    session.add(task)

                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and the cascade-cancel + PR-close below
                    # read and mutate the live rows.
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )

                    # Cascade-cancel non-terminal children
                    from app.main import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    hard_timeout_cancel_msg = (
                        f"Cancelled: parent evaluation timeout after {hours}h"
                    )
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = hard_timeout_cancel_msg
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

                    # Close orphaned rollup PRs from CI-waiting children
                    from app.github import close_pull_request

                    for child in children:
                        child_pr = (
                            (child.context or {})
                            .get("review_loop", {})
                            .get("pr_number")
                        )
                        if child_pr and not (child.context or {}).get("pr_merged"):
                            try:
                                from app.routers.agent import get_github_token_for_task

                                _gh, _owner, _repo = await get_github_token_for_task(
                                    child.id, session
                                )
                                await close_pull_request(_gh, _owner, _repo, child_pr)
                                logger.info(
                                    f"[EVAL TIMEOUT] Closed orphaned PR #{child_pr} from child {str(child.id)[:8]}"
                                )
                            except Exception as pr_err:
                                logger.warning(
                                    f"[EVAL TIMEOUT] Failed to close PR #{child_pr}: {pr_err}"
                                )

                    # Create ERROR alert (deduplicated by _maybe_create_failure_alert)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"Evaluation timeout after {hours}h — children did not complete",
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": task.status},
                    )
                    for child in non_terminal_children:
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": child.status},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="parent_eval_timeout")
                    continue

                # 2-hour warning
                if elapsed > EVALUATING_WARN_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    # Deduplicate: skip if an undismissed alert exists OR one was
                    # dismissed within the last 2 hours (prevents re-firing after dismiss).
                    dismiss_cooldown = _utcnow() - timedelta(hours=2)
                    existing = (
                        (
                            await session.execute(
                                select(Alert).where(
                                    Alert.task_id == task.id,
                                    or_(
                                        Alert.dismissed_at.is_(None),
                                        Alert.dismissed_at >= dismiss_cooldown,
                                    ),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
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
                            str(task.project_id),
                            "alert_created",
                            {
                                "alert_id": str(alert.id),
                                "severity": "WARNING",
                                "title": alert.title,
                            },
                        )
    except Exception as e:
        logger.error(
            f"[EVAL TIMEOUT] Evaluating timeout sweep failed: {e}", exc_info=True
        )


async def _sweep_post_commit_pending():
    """Re-run _process_post_commit for terminal tasks whose prior attempt died.

    When _process_post_commit (review-pipeline spawn, rollup gate handoff,
    completion handlers) times out — usually because GitHub quota hit 0 and
    a defensive call took > 240s to give up — agent_status.py sets
    Task.post_commit_pending = True and stores retry metadata under
    context['_post_commit_pending_meta'] = {at, reason, payload, attempts}.
    The idempotency guard in agent_status re-runs post-commit when the agent
    retries its status POST, but the agent VM has almost always terminated
    by then, so without this sweeper the task sits with its verdict/rollup
    state silently lost.

    Audit W1 (A2/A5): post_commit_pending is a real column now; the scan
    filter and clear go through the column instead of JSONB.

    Write-ahead markers (reason="inline_running", 2026-05-21): agent_status
    sets the marker BEFORE _process_post_commit runs, so a hard instance kill
    that bypasses the except handler still leaves a retry trail. Such a marker
    is age-gated below — a recent one may belong to a live inline run, so it
    is only retried once provably orphaned (older than the 240s post-commit
    budget plus the Cloud Run request ceiling).

    This runs every 60s.  For each pending task:
      - Reconstruct AgentStatusPayload from the saved dict.
      - Call _process_post_commit on a fresh session.
      - On success: clear the post_commit_pending column.
      - On failure/timeout: bump attempts counter (max 5 — avoid infinite
        loop if the underlying cause is non-transient).
    """
    from app.routers.agent_status import _process_post_commit
    from app.schemas import AgentStatusPayload

    MAX_ATTEMPTS = 5
    # A write-ahead "inline_running" marker younger than this may still be a
    # live _process_post_commit pass (240s budget, 300s Cloud Run ceiling) —
    # retrying it would double-process. Past this age a still-"inline_running"
    # marker is provably orphaned (the inline pass was killed mid-flight).
    INLINE_RUNNING_GRACE_S = 600
    try:
        async with AsyncSessionLocal() as session:
            # Status filter: COMPLETED / FAILED only. Tasks that an admin
            # explicitly CANCELLED must NOT have their post-commit retried —
            # a successful retry of `_handle_plan_completion` (or analogous
            # handlers) would unconditionally flip status back to EVALUATING
            # and spawn children, silently overriding the admin's cancel
            # decision (incident 2026-05-04: cancelled plan b9456e05 was
            # zombied 2 hours later — sweep retried, status flipped
            # CANCELLED → EVALUATING, child b403b76a spawned).
            #
            # CANCELLED tasks with post_commit_pending=True are cleaned up
            # below as a separate sub-query so the flag doesn't keep
            # reappearing in the sweep candidate set.
            tasks = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.post_commit_pending.is_(True),
                            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                            Task.archived.is_(False),
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )

            # Clear the pending flag on admin-cancelled tasks — the admin
            # cancel is sticky; we don't retry post-commit and we don't
            # want this row to clutter future sweeps.
            cancelled_pending = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.post_commit_pending.is_(True),
                            Task.status == TaskStatus.CANCELLED,
                            Task.archived.is_(False),
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            for task in cancelled_pending:
                logger.info(
                    f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} is CANCELLED — "
                    f"clearing post_commit_pending without retry"
                )
                from sqlalchemy.orm.attributes import flag_modified

                new_ctx = dict(task.context or {})
                new_ctx.pop("_post_commit_pending_meta", None)
                task.context = new_ctx
                task.post_commit_pending = False
                flag_modified(task, "context")
                session.add(task)
            if cancelled_pending:
                await session.commit()

            if not tasks:
                return

            logger.info(
                f"[POST_COMMIT SWEEP] Found {len(tasks)} task(s) with post_commit_pending"
            )

            for task in tasks:
                ctx = task.context or {}
                pending_meta = ctx.get("_post_commit_pending_meta") or {}
                # Write-ahead markers are persisted BEFORE _process_post_commit
                # runs, so a recent one may be a live inline pass rather than a
                # dead one — skip until provably orphaned. Failure-set markers
                # (post_commit_timeout/error/locked) carry a different reason
                # and retry immediately, as before. If the age can't be
                # determined, fall through and retry (safer than skip-forever).
                if pending_meta.get("reason") == "inline_running":
                    _at = pending_meta.get("at")
                    _age = None
                    if _at:
                        try:
                            _age = (
                                _utcnow() - datetime.fromisoformat(_at)
                            ).total_seconds()
                        except (ValueError, TypeError):
                            _age = None
                    if _age is not None and _age < INLINE_RUNNING_GRACE_S:
                        continue
                attempts = int(pending_meta.get("attempts", 0) or 0)
                payload_dump = pending_meta.get("payload")

                if attempts >= MAX_ATTEMPTS:
                    # Give up.  Mark abandoned + clear pending so the task
                    # doesn't keep getting picked up every sweep.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} exceeded "
                        f"{MAX_ATTEMPTS} attempts — marking post_commit_abandoned"
                    )
                    new_ctx = dict(ctx)
                    new_ctx["_post_commit_abandoned"] = {
                        "at": _utcnow().isoformat(),
                        "after_attempts": attempts,
                        "last_reason": pending_meta.get("reason"),
                    }
                    new_ctx.pop("_post_commit_pending_meta", None)
                    task.context = new_ctx
                    task.post_commit_pending = False
                    task.post_commit_failed = True
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    continue

                if not payload_dump:
                    # Older pending flag that didn't store the payload — we
                    # can't reconstruct. Mark abandoned.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} has no saved payload — cannot retry"
                    )
                    new_ctx = dict(ctx)
                    new_ctx["_post_commit_abandoned"] = {
                        "at": _utcnow().isoformat(),
                        "reason": "no_saved_payload",
                    }
                    new_ctx.pop("_post_commit_pending_meta", None)
                    task.context = new_ctx
                    task.post_commit_pending = False
                    task.post_commit_failed = True
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    continue

                try:
                    _payload = AgentStatusPayload(**payload_dump)
                except Exception as e:
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} payload reconstruct failed: {e}"
                    )
                    continue

                logger.info(
                    f"[POST_COMMIT SWEEP] Retrying post-commit for task {str(task.id)[:8]} "
                    f"(attempt {attempts + 1}/{MAX_ATTEMPTS}, reason={pending_meta.get('reason')})"
                )
                try:
                    await _process_post_commit(task.id, _payload)
                    # Success — clear the flag on a fresh read (post_commit may
                    # have mutated context in other ways we want to keep).
                    async with AsyncSessionLocal() as clear_session:
                        refreshed = await clear_session.get(Task, task.id)
                        if refreshed:
                            refreshed.post_commit_pending = False
                            new_ctx = dict(refreshed.context or {})
                            new_ctx.pop("_post_commit_pending_meta", None)
                            refreshed.context = new_ctx
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(refreshed, "context")
                            clear_session.add(refreshed)
                            await clear_session.commit()
                    logger.info(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} recovered"
                    )
                except Exception as e:
                    # Failure — bump attempts so we eventually give up.  The
                    # retry path in _process_post_commit's exception handler
                    # already bumps attempts itself if it got to set the
                    # flag again; this covers cases where it didn't.
                    logger.warning(
                        f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} retry failed: {e}"
                    )
                    # Bump in its own try/except so a bump failure on one task
                    # doesn't poison the entire sweep batch (which would let
                    # the outer except at the bottom abort the loop, leaving
                    # all subsequent pending tasks unprocessed). Short
                    # lock_timeout (5s) so we fail fast on row contention
                    # instead of burning 30s of orchestrator-cycle time per
                    # task — the next sweep will try again.
                    try:
                        async with AsyncSessionLocal() as bump_session:
                            # Postgres-only fast-fail on row contention; on
                            # SQLite (test DB) SET LOCAL is a no-op syntax
                            # error, so dialect-gate it.
                            if (
                                bump_session.bind
                                and bump_session.bind.dialect.name == "postgresql"
                            ):
                                await bump_session.execute(
                                    text("SET LOCAL lock_timeout = '5s'")
                                )
                            refreshed = await bump_session.get(Task, task.id)
                            if refreshed:
                                new_ctx = dict(refreshed.context or {})
                                new_pending = dict(
                                    new_ctx.get("_post_commit_pending_meta")
                                    or pending_meta
                                )
                                new_pending["attempts"] = (
                                    int(new_pending.get("attempts", attempts)) + 1
                                )
                                new_pending["last_retry_at"] = _utcnow().isoformat()
                                new_ctx["_post_commit_pending_meta"] = new_pending
                                refreshed.context = new_ctx
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(refreshed, "context")
                                bump_session.add(refreshed)
                                await bump_session.commit()
                    except Exception as bump_exc:
                        logger.warning(
                            f"[POST_COMMIT SWEEP] Task {str(task.id)[:8]} "
                            f"attempt-bump failed (likely row-lock contention): "
                            f"{type(bump_exc).__name__}: {bump_exc}"
                        )
                        # Continue to next task — attempts stays where it
                        # was, but the sweep proceeds. If contention persists
                        # the operator must clear post_commit_pending via the
                        # admin PATCH (admin.py modify_task).
    except Exception as e:
        logger.error(f"[POST_COMMIT SWEEP] Sweep failed: {e}", exc_info=True)


async def _recover_orphaned_tasks():
    """Detect COMPLETED tasks with no PR artifact and no review child. Look up PR on GitHub and spawn review."""
    from app.routers.agent import get_github_token_for_task
    from app.review_pipeline import _spawn_pipeline_task

    try:
        async with AsyncSessionLocal() as session:
            five_min_ago = _utcnow() - timedelta(minutes=5)
            one_hour_ago = _utcnow() - timedelta(hours=1)
            # Phase B4 (H-P6): drop status_log/plan_output JSONB. The recovery
            # loop only reads first_payload / context / description / project_id /
            # id and writes context. status_log is not consumed in this branch.
            candidates = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.project_id,
                                Task.created_by,
                                Task.created_at,
                                Task.description,
                                Task.payloads,
                                Task.context,
                            )
                        )
                        .where(
                            Task.status == TaskStatus.COMPLETED,
                            Task.created_by == "ORCHESTRATOR",
                            Task.created_at <= five_min_ago,
                            Task.created_at
                            >= one_hour_ago,  # Only check recent tasks, not entire history
                        )
                    )
                )
                .scalars()
                .all()
            )

            cand_ids = [t.id for t in candidates]
            if cand_ids:
                artifact_rows = (
                    await session.execute(
                        select(Artifact.task_id).where(
                            Artifact.task_id.in_(cand_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                tasks_with_pr = {row[0] for row in artifact_rows}

                child_rows = (
                    await session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(cand_ids)
                        )
                    )
                ).all()
                children_by_parent: dict = {}
                for parent_id, payloads in child_rows:
                    children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                tasks_with_pr = set()
                children_by_parent = {}

            for task in candidates:
                task_type = task.first_payload.get("type", "")
                if task_type not in (
                    "create",
                    "refactor",
                    "fix",
                    "docs",
                    "deps",
                    "migrate",
                ):
                    continue

                # Check if PR artifact exists
                if task.id in tasks_with_pr:
                    continue

                # Check if review child exists
                child_payloads_list = children_by_parent.get(task.id, [])
                if any(
                    (payloads or [{}])[0].get("type") == "review"
                    for payloads in child_payloads_list
                ):
                    continue

                # Orphan has no PR yet. Cap retries — if we've looked 3 times
                # and found nothing, stop burning API quota on this row until
                # new evidence (an artifact showing up via webhook) arrives.
                # Recovery service runs every 120s; 3 attempts = ~6 min window
                # which is enough to catch the usual "PR created a few seconds
                # after agent reported done" race but won't keep polling for
                # the full 55-minute scan window.
                ctx = task.context or {}
                attempts_so_far = int(ctx.get("_recovery_attempts", 0))
                if attempts_so_far >= 3:
                    continue

                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub token fetch failed",
                        exc_info=True,
                    )
                    continue

                short_id = str(task.id).replace("-", "")[:8]
                type_prefix = {
                    "docs": "docs",
                    "deps": "deps",
                    "migrate": "migrate",
                    "refactor": "refactor",
                }.get(task_type, "task")
                branch = f"blueprint/{type_prefix}-{short_id}"

                try:
                    from app.github import _github_request

                    headers = {
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github.v3+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    }
                    resp = await _github_request(
                        "GET",
                        f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open",
                        headers,
                    )
                    prs = resp.json() if resp.status_code == 200 else []
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub PR lookup failed",
                        exc_info=True,
                    )
                    continue

                if not prs:
                    # Record the miss so we back off after N failed lookups.
                    task.context = {**ctx, "_recovery_attempts": attempts_so_far + 1}
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — no open PR found for branch {branch} (attempt {attempts_so_far + 1}/3)"
                    )
                    continue

                pr = prs[0]
                pr_url = pr["html_url"]
                pr_number = pr["number"]
                base_branch = task.first_payload.get("base_branch", "main")

                # Create the missing artifact
                artifact = Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.PULL_REQUEST,
                    pr_url=pr_url,
                )
                session.add(artifact)

                # Spawn review
                original_prompt = task.first_payload.get(
                    "prompt", task.description or ""
                )
                review_task, _ = await _spawn_pipeline_task(
                    task,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task.id),
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

                await broadcast_to_project(
                    str(task.project_id),
                    "alert_created",
                    {
                        "alert_id": str(alert.id),
                        "severity": "WARNING",
                        "title": alert.title,
                    },
                )
                logger.warning(
                    f"[RECOVERY] Auto-recovered orphaned task {str(task.id)[:8]} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}"
                )

            # Phase 2: Reaped-with-artifact recovery.
            # Tasks that were reaped (set back to QUEUED/PROVISIONING) after the PR was already
            # created.  The PR endpoint saves the artifact, so these tasks have an artifact but
            # never reached COMPLETED and thus never triggered pipeline routing.
            reaped_candidates = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.status.in_(
                                [TaskStatus.QUEUED, TaskStatus.PROVISIONING]
                            ),
                            Task.created_by == "ORCHESTRATOR",
                            Task.attempt_count > 0,
                            Task.created_at >= one_hour_ago,
                        )
                    )
                )
                .scalars()
                .all()
            )

            reaped_ids = [t.id for t in reaped_candidates]
            if reaped_ids:
                pr_artifact_rows = (
                    await session.execute(
                        select(Artifact.task_id, Artifact.pr_url).where(
                            Artifact.task_id.in_(reaped_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                pr_url_by_task: dict = {}
                for task_id, pr_url in pr_artifact_rows:
                    if pr_url and not pr_url_by_task.get(task_id):
                        pr_url_by_task[task_id] = pr_url

                reaped_child_rows = (
                    await session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(reaped_ids)
                        )
                    )
                ).all()
                reaped_children_by_parent: dict = {}
                for parent_id, payloads in reaped_child_rows:
                    reaped_children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                pr_url_by_task = {}
                reaped_children_by_parent = {}

            for task in reaped_candidates:
                task_type = task.first_payload.get("type", "")
                if task_type not in (
                    "create",
                    "refactor",
                    "fix",
                    "docs",
                    "deps",
                    "migrate",
                ):
                    continue

                # Only recover tasks that already have a PR artifact
                pr_url = pr_url_by_task.get(task.id)
                if not pr_url:
                    continue

                # Skip if review child already exists
                child_payloads_list = reaped_children_by_parent.get(task.id, [])
                if any(
                    (payloads or [{}])[0].get("type") == "review"
                    for payloads in child_payloads_list
                ):
                    continue

                # ── Site 3 lock-during-IO fix ──
                # Previously this loop held FOR UPDATE on `task` across
                # `_spawn_pipeline_task`, which acquires
                # `pg_advisory_xact_lock(pr_number)` via
                # `acquire_pr_spawn_lock` (task_locking.py:223). If a
                # peer worker was mid-spawn for the same PR, this
                # iteration would block on the advisory lock with the
                # task FOR UPDATE held — starving any other writer
                # touching this row. Fix: hold the task FOR UPDATE only
                # for the COMPLETED transition, commit to release it,
                # then call `_spawn_pipeline_task` (the advisory lock is
                # per-PR and self-coordinating). The spawn-and-alert
                # commit follows on a fresh transaction.
                locked = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task.id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if not locked or locked.status not in (
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                ):
                    continue

                pr_number = int(pr_url.rstrip("/").split("/")[-1])
                base_branch = locked.first_payload.get("base_branch", "main")
                short_id = str(locked.id).replace("-", "")[:8]
                type_prefix = {
                    "docs": "docs",
                    "deps": "deps",
                    "migrate": "migrate",
                    "refactor": "refactor",
                }.get(task_type, "task")
                branch = f"blueprint/{type_prefix}-{short_id}"
                original_prompt = locked.first_payload.get(
                    "prompt", locked.description or ""
                )
                project_id_local = locked.project_id
                task_id_local = locked.id

                locked.status = TaskStatus.COMPLETED
                locked.latest_status_message = (
                    f"Auto-completed: PR #{pr_number} found after reap recovery"
                )
                session.add(locked)
                # Commit-release the task FOR UPDATE before invoking the
                # spawn (which acquires the per-PR advisory lock). The
                # task is now COMPLETED and persisted; the spawn can
                # proceed on a fresh transaction without holding the
                # task row lock across the advisory-lock acquire.
                await session.commit()

                # Re-fetch a fresh, unlocked instance for the spawn. The
                # spawn path reads source_task fields (context, payloads,
                # title) and writes back ``latest_status_message`` /
                # ``context`` markers; using the same `locked` object
                # without re-attach risks DetachedInstanceError after the
                # commit released the SQLA identity for some attributes.
                refreshed = await session.get(Task, task_id_local)
                if refreshed is None:
                    logger.warning(
                        f"[RECOVERY] Task {short_id} disappeared after release-commit — skipping spawn"
                    )
                    continue
                review_task, _ = await _spawn_pipeline_task(
                    refreshed,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task_id_local),
                )

                alert = Alert(
                    project_id=project_id_local,
                    severity=AlertSeverity.WARNING,
                    title=f"Auto-recovered reaped task with PR #{pr_number}",
                    message=f"Task {short_id} was reaped after creating PR #{pr_number}. Auto-completed and review spawned.",
                    task_id=task_id_local,
                )
                session.add(alert)
                await session.commit()

                await broadcast_to_project(
                    str(project_id_local),
                    "alert_created",
                    {
                        "alert_id": str(alert.id),
                        "severity": "WARNING",
                        "title": alert.title,
                    },
                )
                logger.warning(
                    f"[RECOVERY] Auto-completed reaped task {short_id} → PR #{pr_number}, review={str(review_task.id)[:8] if review_task else 'NONE'}"
                )
    except Exception as e:
        logger.error(f"[RECOVERY] Orphaned task sweep failed: {e}", exc_info=True)
