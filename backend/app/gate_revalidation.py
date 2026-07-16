"""Auto-revalidation of BLOCKED tasks citing repo-wide gate failures.

When integration HEAD advances past a BLOCKED task's branch point, this module
re-checks the task's cited gate-failure precondition against current HEAD. If
the gate is green (integration CI passes at the new HEAD), the task is unblocked
onto a fresh base.

Integrates with the existing blocked-task sweep
(``blocked_task_unblock_sweeps.sweep_unblock_blocked_tasks``) — extends it,
does not replace it.
"""

import logging

from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Gate-failure sentinel substrings matched case-insensitively against a task's
# ``blocked_reason`` / ``latest_status_message``. Each names a repo-wide suite
# or gate (standalone smoke, typecheck, vitest) rather than a task-specific
# blocker.
_GATE_FAILURE_SENTINELS = frozenset({
    "standalone_smoke",
    "standalone smoke",
    "typecheck",
    "vitest",
})


def is_gate_failure_blocked(task: Task) -> bool:
    """Predicate: is *task* BLOCKED on a repo-wide gate failure?

    Checks the task's ``blocked_reason`` (and fallback
    ``latest_status_message``) against known gate-failure sentinels.
    Case-insensitive.
    """
    text = " ".join(
        str(v or "")
        for v in (task.blocked_reason, task.latest_status_message)
    )
    if not text.strip():
        return False
    lower = text.lower()
    return any(s in lower for s in _GATE_FAILURE_SENTINELS)


def _ci_conclusion_is_green(ci_result: dict) -> bool:
    """Does the GitHub checks result indicate a clean bill of health?"""
    if ci_result.get("_from_api_error"):
        return False
    # "completed" + "success" = green.  "no_checks" on an integration branch
    # whose CI never runs is also treated as green (gate was never an issue).
    status = ci_result.get("status")
    conclusion = ci_result.get("conclusion")
    if status == "completed" and conclusion == "success":
        return True
    return status == "no_checks"


async def resolve_gate_state(
    session,
    project_id: str | object,
    *,
    github_info_cache: dict | None = None,
) -> dict | None:
    """Resolve integration HEAD CI state for a project.

    Cheap revalidation: makes at most ONE GitHub check-runs API call per
    project per call-site (the caller is expected to cache the returned dict
    for the duration of a sweep cycle).  Returns ``None`` when GitHub
    credentials cannot be resolved or the integration HEAD cannot be
    determined — the revalidation is simply skipped and existing blocker
    machinery handles the task.

    Returns
        ``{"head_sha": str, "green_at_head": bool, "checked_at_utc": str}``
        or ``None``.
    """
    # 1 ── Fetch the project (needed for GitHub owner/repo / integration branch).
    project: Project | None = await session.get(Project, project_id)
    if project is None:
        return None
    owner = getattr(project, "github_owner", None)
    repo = getattr(project, "github_repo", None)
    if not owner or not repo:
        return None

    # 2 ── Resolve GitHub installation token (cached across the caller's batch).
    from app.github import get_repo_installation_token

    pid = str(project.id)
    token = None
    if github_info_cache is not None and pid in github_info_cache:
        token = github_info_cache[pid]
    if not token:
        try:
            token = await get_repo_installation_token(owner, repo)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[GATE-REVALIDATION] App-level token discovery failed for %s/%s",
                owner,
                repo,
            )
        if token and github_info_cache is not None:
            github_info_cache[pid] = token

    if not token:
        return None

    # 3 ── Resolve integration HEAD SHA.
    from app.branch_resolution import get_project_integration_branch
    from app.github import get_branch_sha

    branch = get_project_integration_branch(project)
    try:
        head_sha = await get_branch_sha(
            token, owner, repo, branch, project_id=pid
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[GATE-REVALIDATION] Failed to resolve integration branch SHA for "
            "%s/%s (%s)",
            owner,
            repo,
            branch,
        )
        return None

    # 4 ── Check CI state at HEAD.
    from app.github import get_check_runs_cached

    ci_result = await get_check_runs_cached(
        token,
        owner,
        repo,
        head_sha,
        project_id=pid,
        treat_missing_as_success=False,
        include_failed_logs=False,
    )

    green = _ci_conclusion_is_green(ci_result)
    return {
        "head_sha": head_sha,
        "green_at_head": green,
        "checked_at_utc": _utcnow().isoformat(),
    }


async def has_live_respawn(task: Task, session) -> bool:
    """Dedup guard: check for active sibling tasks covering the same scope.

    Before unblocking a gate-failure-blocked task, call this to check whether
    a live respawn (a sibling task in the same project sharing the same
    ``parent_task_id`` with an active status) already covers the same scope.
    If one exists, the blocked original should NOT resume — the respawn
    replaces it and the two would race.

    Returns ``True`` when at least one such sibling is found (caller should
    skip the unblock and let the respawn complete).
    """
    if not task.parent_task_id:
        # No parent: can't authoritatively determine scope.  Skip the dedup
        # guard and let the caller's existing blocker machinery decide.
        return False

    stmt = (
        select(Task.id)
        .where(
            Task.project_id == task.project_id,
            Task.parent_task_id == task.parent_task_id,
            Task.id != task.id,
            Task.status.in_(
                [
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.EVALUATING,
                    TaskStatus.BLOCKED,
                ]
            ),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None
