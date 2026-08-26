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
from datetime import datetime, timezone

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


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def head_advanced_since_gate_failure(
    task: Task,
    head_sha: object,
    *,
    head_committed_at: object = None,
) -> bool:
    """Return True only when integration HEAD differs from the task baseline.

    ``generated_head_sha`` is the branch point recorded when research spawned
    the task. After an automatic revalidation, ``_gate_revalidated_head`` takes
    precedence so the same green HEAD cannot repeatedly unblock a task that
    blocks again without any repository change.

    Legacy/direct-fix tasks may predate branch-point capture. For those rows,
    an integration commit strictly newer than ``blocked_at`` is equivalent
    advancement proof: the repository changed after the gate failure occurred.
    The caller still separately requires either explicit green CI or the
    project's explicit tests-gated/no-integration-CI policy.
    """
    head = _normalize_sha(head_sha)
    ctx = _task_context(task)
    # ``_gate_revalidated_head`` first (a prior automatic revalidation already
    # consumed that HEAD), then the STRUCTURED baseline frozen into the blocker
    # record at block time, then the spawn-time branch point. The structured
    # baseline is preferred over ``generated_head_sha`` because it names the base
    # the blocker was actually OBSERVED on, which is what "advanced since the
    # failure" means; ``generated_head_sha`` only names where the branch was cut.
    from app.task_blocked import blocker_recheck_baseline

    baseline = _normalize_sha(
        ctx.get("_gate_revalidated_head")
        or blocker_recheck_baseline(task)
        or ctx.get("generated_head_sha")
    )
    if not head:
        return False
    if baseline:
        return not _same_sha(head, baseline)

    blocked_at = _as_utc(task.blocked_at)
    committed_at = _as_utc(head_committed_at)
    return bool(blocked_at and committed_at and committed_at > blocked_at)


def base_state_recheck_eligible(task: Task) -> bool:
    """Predicate: does *task* carry the structured ``base_state`` recheck record?

    This is the STRUCTURAL counterpart to :func:`is_gate_failure_blocked`. That
    function answers the same question — "is this blocker a property of the
    repository base rather than of the task's own diff?" — by matching the
    agent's free prose against two hardcoded phrase lists. Prose matching only
    ever recognizes the wordings someone thought to enumerate: a live incident
    (task ``d58aa94c``, "Publication gate cannot pass without unrelated artifact
    repairs … an inherited 1806-line breach") hit NEITHER list, so the
    revalidation machinery that exists precisely for it never ran and the task
    sat escalated to a human for 20 hours after both halves of its blocker had
    been fixed on the integration branch.

    ``task_blocked.build_blocker_recheck`` records the classification at block
    time from the blocker's SHAPE (agent-sourced precondition, no named
    foundation), so recognition no longer depends on wording. The prose path is
    kept as-is for rows written before this field existed.
    """
    from app.task_blocked import (
        BLOCKER_RECHECK_BASE_STATE,
        blocker_recheck_predicate,
    )

    return blocker_recheck_predicate(task) == BLOCKER_RECHECK_BASE_STATE


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
    from app.github import get_check_runs_cached, get_commit_date

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
    # Blueprint intentionally skips GitHub CI on its high-churn integration
    # branch and executes real tests in the task PR helper instead. Respect the
    # same explicit policy used by the merge monitor: a definitive no-checks
    # result is revalidation-ready only when this branch is configured to
    # proceed without GitHub checks. Pending, red, and API-error results remain
    # fail-closed.
    from app.ci_resolution import proceed_on_no_checks

    no_checks_allowed = (
        ci_result.get("status") == "no_checks"
        and proceed_on_no_checks(project, branch)
    )
    try:
        head_committed_at = await get_commit_date(token, owner, repo, head_sha)
    except Exception:  # noqa: BLE001
        head_committed_at = None
    return {
        "head_sha": head_sha,
        "green_at_head": green,
        "no_checks_allowed": no_checks_allowed,
        "revalidation_ready": green or no_checks_allowed,
        "head_committed_at": head_committed_at,
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
