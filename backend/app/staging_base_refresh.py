"""Keep research/plan staging bases current with their project's integration trunk.

THE DEFECT THIS CLOSES
----------------------
A research (or plan) parent creates its rollup staging branch —
``research/{short_id}`` — exactly ONCE, via ``_ensure_branch`` at parent-spawn
time, and then hands that branch name to every child as ``base_branch``. The
branch is never re-synced afterwards. Integration keeps advancing underneath it,
so every child dispatched later is cut from, built on, and GRADED AGAINST a tree
frozen at the moment the parent spawned.

The visible symptom is a child that reports an accurate blocker about a failure
its own diff did not cause and that nobody can fix: the fix already landed on
integration, but it is not reachable from the frozen base. Retrying does not
help — ``prepareTaskWorkBranch`` re-clones from the SAME frozen ``base_branch``,
so the identical stale tree comes back every attempt. The longer a research
batch runs, the more already-fixed failures its later children inherit; a
multi-hour batch can burn its whole retry budget on them.

WHY A MERGE AND NOT A RESET
---------------------------
``task_completion._reconcile_reused_branch`` may only delete+recreate a staging
branch that is strictly ``"behind"`` its source, because that is the one state
where the branch provably has no commits of its own to destroy. A staging branch
whose children have started merging is ``"diverged"``, and resetting it would
close the open rollup PR and throw away landed child commits.

``POST /repos/{owner}/{repo}/merges`` (``github_refs.merge_branch``) has no such
restriction: it brings integration INTO the staging branch without rewriting any
commit already on it. That is the same operation as GitHub's "Update branch"
button on a PR, and it is correct in BOTH states — a ``"behind"`` branch
fast-forwards, a ``"diverged"`` one gains a merge commit and keeps every child
rollup. So this sweep does not need the compare at all: a merge of a head the
base already contains is a 204 no-op, which covers ``identical`` and ``ahead``
for free at one API call instead of two.

A conflict (409) is left ALONE and reported. It means integration and the
already-merged child work genuinely disagree; auto-resolving that is a human
decision, and dispatching against the un-refreshed base is still no worse than
today's behaviour.

SCOPE
-----
Only branches that are (a) named by a live project's ``is_staging_branch`` and
(b) the ``base_branch`` of at least one task that has not started yet, or will
start again (``QUEUED`` / ``PROVISIONING`` / ``BLOCKED`` / ``PARKED``). Tasks
already ``IN_PROGRESS``/``EVALUATING`` have cloned their base and cannot benefit;
terminal tasks never will. This keeps the sweep's GitHub cost proportional to
pending work rather than to branch-list size, and it deliberately keeps the
merge OFF the claim path — ``next-task`` must never wait on GitHub.
"""

import asyncio
import logging

from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import Project, Task, TaskStatus
import app.shared_state as _shared_state

logger = logging.getLogger(__name__)


# Statuses whose NEXT dispatch will re-read ``base_branch``. IN_PROGRESS /
# EVALUATING are excluded on purpose: those agents already hold a clone, so a
# refresh cannot reach them, and including them would spend API calls on
# branches with no pending consumer.
_PENDING_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)

# Per-cycle caps. Cost is ONE merge POST per distinct staging branch with
# pending children (plus one token mint per project). At the default 30-minute
# cadence these caps sit far inside the 5000/hr install-token budget even when a
# large research fan-out is mid-flight.
_MAX_REFRESH_PER_PROJECT = 50
_MAX_REFRESH_TOTAL = 200

# Skip the whole cycle when either quota bucket is low — a base refresh is an
# optimisation, and must never be the call that starves task spawning or CI
# polling. Mirrors ``branch_cleanup``'s floors.
_QUOTA_FLOOR = 500  # install-token (5000/hr bucket)
_JWT_QUOTA_FLOOR = 10  # App-JWT (~60/hr bucket)

_DEFAULT_INTERVAL_MINUTES = 30

# Bounds the pending-task scan so a pathological backlog cannot turn one cycle
# into an unbounded read. Distinct staging branches are far fewer than tasks.
_MAX_PENDING_TASKS_SCANNED = 5000


def collect_pending_staging_bases(project: Project, tasks) -> set[str]:
    """Distinct staging ``base_branch`` values among ``tasks`` for ``project``.

    Pure (no I/O) so the selection rule is testable without GitHub or a DB.
    Non-staging bases — project integration, a task branch, a release branch —
    are excluded: this sweep only ever moves rollup staging branches.
    """
    bases: set[str] = set()
    for task in tasks:
        base = (getattr(task, "first_payload", None) or {}).get("base_branch")
        if not base or not isinstance(base, str):
            continue
        if not project.is_staging_branch(base):
            continue
        bases.add(base)
    return bases


async def _refresh_project_staging_bases(project, token, session) -> dict:
    """Merge integration into every stale staging base with pending children.

    Returns a counts dict: ``refreshed`` (a merge commit was created),
    ``already`` (base already contained integration), ``conflict``, ``missing``,
    ``error``.
    """
    from app.branch_resolution import get_project_integration_branch
    from app.github import merge_branch

    counts = {
        "refreshed": 0,
        "already": 0,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }

    integration = get_project_integration_branch(project)
    if not integration:
        return counts

    pending = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status.in_(list(_PENDING_STATUSES)),
                )
                .limit(_MAX_PENDING_TASKS_SCANNED)
            )
        )
        .scalars()
        .all()
    )
    bases = collect_pending_staging_bases(project, pending)
    if not bases:
        return counts

    owner = project.github_owner
    repo = project.github_repo
    for branch in sorted(bases)[:_MAX_REFRESH_PER_PROJECT]:
        if branch == integration:
            # Defensive: a project configured with a staging-prefixed
            # integration branch must never merge into itself.
            continue
        try:
            result = await merge_branch(
                token,
                owner,
                repo,
                base=branch,
                head=integration,
                commit_message=(
                    f"Merge {integration} into {branch} to refresh the staging "
                    f"base for its pending children"
                ),
                project_id=str(project.id),
            )
        except Exception as exc:
            counts["error"] += 1
            logger.warning(
                "[STAGING BASE] %s/%s: refresh of %s from %s errored: %s",
                owner,
                repo,
                branch,
                integration,
                exc,
            )
            continue

        if result.get("merged") and not result.get("already"):
            counts["refreshed"] += 1
            logger.info(
                "[STAGING BASE] %s/%s: refreshed %s from %s — its pending "
                "children are no longer graded against a frozen tree",
                owner,
                repo,
                branch,
                integration,
            )
        elif result.get("merged"):
            counts["already"] += 1
        elif result.get("conflict"):
            counts["conflict"] += 1
            logger.warning(
                "[STAGING BASE] %s/%s: %s CONFLICTS with %s — left untouched. "
                "Its children keep the current base; resolving the rollup "
                "against integration needs a human or a fix task.",
                owner,
                repo,
                branch,
                integration,
            )
        elif result.get("missing"):
            counts["missing"] += 1
            logger.warning(
                "[STAGING BASE] %s/%s: %s or %s no longer exists — skipping "
                "(the staging branch was cleaned up, or the rollup already merged)",
                owner,
                repo,
                branch,
                integration,
            )
        else:
            counts["error"] += 1

    return counts


async def refresh_stale_staging_bases() -> dict:
    """One sweep across every project with a GitHub repo configured."""
    from app.branch_cleanup import _get_project_github_token
    from app.models import UserIntegration

    totals = {
        "refreshed": 0,
        "already": 0,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }

    it_remaining = _shared_state.github_quota.get("remaining")
    jwt_remaining = _shared_state.github_jwt_quota.get("remaining")
    if it_remaining is not None and it_remaining < _QUOTA_FLOOR:
        logger.info(
            "[STAGING BASE] Skipping cycle — install-token quota %s < floor %s",
            it_remaining,
            _QUOTA_FLOOR,
        )
        return totals
    if jwt_remaining is not None and jwt_remaining < _JWT_QUOTA_FLOOR:
        logger.info(
            "[STAGING BASE] Skipping cycle — JWT quota %s < floor %s",
            jwt_remaining,
            _JWT_QUOTA_FLOOR,
        )
        return totals

    async with AsyncSessionLocal() as session:
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
        if not projects:
            return totals

        integrations_by_user = {}
        user_ids = list({p.user_id for p in projects})
        if user_ids:
            for row in (
                (
                    await session.execute(
                        select(UserIntegration).where(
                            UserIntegration.user_id.in_(user_ids)
                        )
                    )
                )
                .scalars()
                .all()
            ):
                integrations_by_user.setdefault(row.user_id, row)

        for project in projects:
            if sum(totals.values()) >= _MAX_REFRESH_TOTAL:
                logger.info(
                    "[STAGING BASE] Per-cycle cap %s reached — remaining "
                    "projects deferred to the next cycle",
                    _MAX_REFRESH_TOTAL,
                )
                break
            try:
                token = await _get_project_github_token(
                    project,
                    session,
                    integration=integrations_by_user.get(project.user_id),
                )
            except Exception as exc:
                logger.warning(
                    "[STAGING BASE] project=%s token resolution failed: %s",
                    project.id,
                    exc,
                )
                continue
            if not token:
                continue
            try:
                counts = await _refresh_project_staging_bases(project, token, session)
            except Exception as exc:
                logger.warning(
                    "[STAGING BASE] project=%s sweep failed: %s", project.id, exc
                )
                continue
            for key, value in counts.items():
                totals[key] += value

    actionable = totals["refreshed"] + totals["conflict"] + totals["missing"]
    if actionable:
        logger.info("[STAGING BASE] cycle summary: %s", totals)
    return totals


async def staging_base_refresh_service():
    """Periodic refresh of research/plan staging bases (default every 30 min)."""
    while True:
        try:
            await refresh_stale_staging_bases()
        except asyncio.CancelledError:
            logger.info("[STAGING BASE] Service shutting down.")
            break
        except Exception as exc:
            # Same shutdown collateral as branch cleanup: an instance going away
            # mid-cycle closes the connection under the open session context and
            # the __aexit__ rollback raises InterfaceError. Not a sweep failure.
            msg = str(exc)
            if "connection is closed" in msg or "InterfaceError" in type(exc).__name__:
                logger.warning(
                    "[STAGING BASE] Session closed mid-cycle "
                    "(instance shutdown collateral): %s: %s",
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.error("[STAGING BASE] Error: %s", exc, exc_info=True)

        interval_minutes = _shared_state.runtime_config.get(
            "staging_base_refresh_interval_minutes", _DEFAULT_INTERVAL_MINUTES
        )
        try:
            await asyncio.sleep(max(1, int(interval_minutes)) * 60)
        except asyncio.CancelledError:
            logger.info("[STAGING BASE] Service shutting down.")
            break
