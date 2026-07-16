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

# Require both a repository-wide/baseline marker and a recognized gate surface.
# This keeps a task's own typecheck/test failure from being mistaken for an
# inherited gate failure while covering the wording emitted by ./check and
# ./pr open.
GATE_REVALIDATION_QUERY_MARKERS = (
    "repo-wide",
    "untouched",
    "pre-existing",
    "outside this diff",
    "unrelated to this diff",
    "baseline failure",
    "sanctioned standalone",
    "mandatory impacted backend gate",
)
_REPO_WIDE_FAILURE_MARKERS = frozenset(GATE_REVALIDATION_QUERY_MARKERS)
_GATE_SURFACE_MARKERS = frozenset(
    {
        "standalone_smoke",
        "standalone smoke",
        "standalone suite",
        "./check test",
        "./pr open",
        "typecheck",
        "vitest",
        "websocket inventory",
        "test_websocket_emitter_inventory",
        "test_invariants.py",
        "test_operator_workbench_contracts.py",
        "impacted backend gate",
    }
)

_ACTIVE_RESPAWN_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)


def _task_context(task: Task) -> dict:
    ctx = task.context or {}
    return ctx if isinstance(ctx, dict) else {}


def _normalize_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _same_sha(left: str, right: str) -> bool:
    """Compare full or abbreviated Git SHAs without accepting tiny prefixes."""
    if min(len(left), len(right)) < 7:
        return left == right
    return left.startswith(right) or right.startswith(left)


def head_advanced_since_gate_failure(task: Task, head_sha: object) -> bool:
    """Return True only when integration HEAD differs from the task baseline.

    ``generated_head_sha`` is the branch point recorded when research spawned
    the task. After an automatic revalidation, ``_gate_revalidated_head`` takes
    precedence so the same green HEAD cannot repeatedly unblock a task that
    blocks again without any repository change.
    """
    head = _normalize_sha(head_sha)
    ctx = _task_context(task)
    baseline = _normalize_sha(
        ctx.get("_gate_revalidated_head") or ctx.get("generated_head_sha")
    )
    return bool(head and baseline and not _same_sha(head, baseline))


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
    return any(m in lower for m in _REPO_WIDE_FAILURE_MARKERS) and any(
        m in lower for m in _GATE_SURFACE_MARKERS
    )


def _ci_conclusion_is_green(ci_result: dict) -> bool:
    """Does the GitHub checks result indicate a clean bill of health?"""
    if ci_result.get("_from_api_error"):
        return False
    # Missing checks are not proof that a previously failing repository gate is
    # fixed. Only an explicitly completed, successful check set is sufficient.
    status = ci_result.get("status")
    conclusion = ci_result.get("conclusion")
    return status == "completed" and conclusion == "success"


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


def _task_scope_keys(task: Task) -> set[tuple[str, str]]:
    """Stable scope identities carried across retries and decompositions."""
    ctx = _task_context(task)
    keys: set[tuple[str, str]] = set()
    fingerprint = task.prompt_fingerprint or ctx.get("prompt_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        keys.add(("prompt", fingerprint.strip()))
    finding_hash = ctx.get("finding_hash")
    if isinstance(finding_hash, str) and finding_hash.strip():
        keys.add(("finding", finding_hash.strip()))
    return keys


def _same_task_scope(left: Task, right: Task) -> bool:
    if _task_scope_keys(left) & _task_scope_keys(right):
        return True
    left_title = " ".join(str(left.title or "").split()).casefold()
    right_title = " ".join(str(right.title or "").split()).casefold()
    return bool(
        left_title
        and left_title == right_title
        and (left.first_payload or {}).get("type")
        == (right.first_payload or {}).get("type")
    )


async def has_live_respawn(task: Task, session) -> bool:
    """Dedup guard: check for active sibling tasks covering the same scope.

    Before unblocking a gate-failure-blocked task, call this to check whether
    a live respawn (an active sibling with a shared prompt/finding identity, or
    an exact same normalized title/type fallback) already covers the same scope.
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
        select(Task)
        .where(
            Task.project_id == task.project_id,
            Task.parent_task_id == task.parent_task_id,
            Task.id != task.id,
            Task.status.in_(_ACTIVE_RESPAWN_STATUSES),
        )
    )
    result = await session.execute(stmt)
    return any(_same_task_scope(task, sibling) for sibling in result.scalars())
