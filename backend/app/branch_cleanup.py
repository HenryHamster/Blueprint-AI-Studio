"""Stale-branch git cleanup: branch short-id extraction, PR-open detection,
per-project GitHub-token resolution, the stale-branch sweep, and its service loop.

Extracted from ``services.py`` (which re-exports these names for backward
compatibility — the same re-export pattern ``github.py`` uses after its split).
This cluster evolves independently from the rest of ``services.py`` (GCP/secret
loops, evaluating-timeout/CI-backstop recovery, post-commit sweep).
"""

import asyncio
import logging
import re
from datetime import timedelta

from sqlalchemy import cast, func
from sqlalchemy.types import String
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    TaskStatus,
    Alert,
    AlertSeverity,
)
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


_DEFAULT_PROTECTED_BRANCHES = ("main", "master", "develop", "dev")
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
#   {namespace}/{task|fix|refactor|docs|deps|migrate}-{short_id}
#   research/{short_id}  (default staging prefix)
#   plan/{short_id}      (default staging prefix)
# short_id is the first 8 hex chars of the owning task's UUID (dashes stripped).
# ``{namespace}`` is per-project (``project.task_branch_namespace``); the
# Blueprint self-repo uses ``"blueprint"``, other installs ship their own.
_DEFAULT_STAGING_PREFIXES: tuple[str, ...] = ("research/", "plan/")


def _build_branch_short_id_re(
    namespace: str,
    staging_prefixes: tuple[str, ...] = _DEFAULT_STAGING_PREFIXES,
) -> re.Pattern:
    """Compile the short-id extraction regex for a project's namespace.

    ``staging_prefixes`` controls which rollup-staging branch prefixes
    (e.g. ``research/``, ``plan/``) are recognized alongside the task-branch
    namespace.  Defaults to Blueprint's conventional pair; projects can
    override via ``build_config.staging_branch_prefixes``.
    """
    staging_alt = "|".join(re.escape(p) for p in staging_prefixes)
    return re.compile(
        rf"^(?:{re.escape(namespace)}/(?:task|fix|refactor|docs|deps|migrate)-|{staging_alt})"
        r"([a-f0-9]{8})$"
    )


# Default regex preserved for callers that pre-date per-project namespaces.
# New code paths inside the per-project sweep build the regex per-project so
# branches in a non-Blueprint namespace (e.g. ``autoproj/``) are recognized.
_BRANCH_SHORT_ID_RE = _build_branch_short_id_re("blueprint")


# Statuses a task can sit in after it's done. Only branches owned by tasks in
# one of these states are eligible for stale-branch cleanup.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)


def _extract_branch_short_id(
    branch_name: str, pattern: re.Pattern = _BRANCH_SHORT_ID_RE
) -> str | None:
    """Return the 8-char task short_id embedded in a branch name, or None.

    Used by the stale branch cleanup to attribute each branch to the task that
    created it without a GitHub API round-trip. ``pattern`` defaults to the
    Blueprint-namespace regex; per-project sweeps pass a project-specific
    pattern compiled from the project's task-branch namespace.
    """
    m = pattern.match(branch_name)
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


async def _get_project_github_token(project, session, *, integration=None):
    """Retrieve a GitHub token for a project via its UserIntegration.

    Reuses the same in-process cache as ``get_github_token_for_task`` so a
    dashboard endpoint polling /api/admin/open-prs every 10s (with its own
    60s cache) doesn't mint a fresh install token on every cache-miss.  That
    pattern was silently draining the ~60/hr App-JWT bucket even with zero
    agents running.

    Pass a pre-loaded ``integration`` to skip the per-project DB query (used
    by ``_run_stale_branch_cleanup`` which batch-loads all integrations upfront).
    """
    from app.models import UserIntegration
    from app.field_encryption import decrypt_field
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

    if integration is None:
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
        decrypted_pat_ref = decrypt_field(integration.github_pat)
        token = await get_user_secret(decrypted_pat_ref)
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

        # Batch-load UserIntegrations for all projects upfront to avoid an
        # N+1 query pattern (one SELECT per project inside the loop).
        from app.models import UserIntegration

        project_user_ids = list({p.user_id for p in projects})
        user_integrations = (
            (
                await session.execute(
                    select(UserIntegration).where(
                        UserIntegration.user_id.in_(project_user_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        user_integration_lookup: dict = {ui.user_id: ui for ui in user_integrations}

        ACTIVE_STATUSES = [
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
            # PARKED is live work waiting on an external wake: a RESUME wake
            # re-queues it onto a fresh VM that re-uses its branch. Treating its
            # branch as unused while parked would delete it out from under the
            # resumed task — strand. So a parked task pins its branch as active.
            TaskStatus.PARKED,
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

            project_namespace = _shared_state.get_project_task_branch_namespace(project)
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
                active_branches.add(
                    _shared_state.derive_task_branch(
                        row.id, task_type, project_namespace
                    )
                )

            try:
                token = await _get_project_github_token(
                    project,
                    session,
                    integration=user_integration_lookup.get(project.user_id),
                )
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
                    prefixes=list(project.stale_branch_prefixes),
                    max_branches=1000,
                )
            except Exception as e:
                logger.warning(
                    f"[BRANCH CLEANUP] Failed to list branches for {owner}/{repo}: {e}"
                )
                continue

            # Batch-load task metadata for every branch whose name matches our
            # known short_id shape. One SQL query replaces ~N GitHub calls.
            project_staging_prefixes = getattr(
                project, "staging_branch_prefixes", _DEFAULT_STAGING_PREFIXES
            )
            project_branch_re = _build_branch_short_id_re(
                project_namespace, project_staging_prefixes
            )
            branch_short_ids = {
                sid
                for b in branches
                if (sid := _extract_branch_short_id(b["name"], project_branch_re))
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
                if name in project.protected_branches:
                    continue

                # Gate 2: skip if the name matches any active task's branch
                if name in active_branches:
                    continue

                # Gate 3: name must match the known cleanup shape. Unknown
                # shapes (hand-created branches someone kept around, legacy
                # naming, etc.) are left alone.
                short_id = _extract_branch_short_id(name, project_branch_re)
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
                "connection is closed" in msg or "InterfaceError" in type(e).__name__
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
