"""Rollup orphan-PR attribution and stale-branch cleanup seam.

Extracted from ``rollup_management.py`` to keep that module under its size
guardrail. Contains the work-branch attribution helpers
(``_branch_task_id_prefix``, ``_partition_orphan_prs``) and the staging /
feature branch cleanup (``_cleanup_branches``).

These symbols are re-exported from ``app.rollup_management`` so existing caller
and test import paths keep working unchanged.

All deferred imports from ``app.main`` / routers are intentional — they avoid
circular imports and preserve test-patchability.
"""

# fmt: off
import logging
import re
import uuid as _uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Task,
    Project,
    TaskStatus,
)

logger = logging.getLogger(__name__)


def _branch_task_id_prefix(branch: str | None) -> str:
    """Recover the owning task's id-prefix from an agent work-branch name.

    Agent work branches are ``{namespace}/{prefix}-{8hex}`` where ``8hex`` is
    the first 8 hex chars of the task UUID (see
    ``shared_state.derive_task_branch``). Returns that trailing token
    lowercased, or ``""`` when the branch doesn't match the shape (custom /
    promotion / CI-fix branches), so callers can fall back to treating the PR
    as un-attributable.
    """
    if not branch or "/" not in branch:
        return ""
    last = branch.rsplit("/", 1)[-1]
    if "-" not in last:
        return ""
    token = last.rsplit("-", 1)[-1].strip().lower()
    if len(token) == 8 and all(c in "0123456789abcdef" for c in token):
        return token
    return ""


def _partition_orphan_prs(
    open_prs: list[dict],
    cancelled_child_id_prefixes: set[str],
) -> tuple[list[dict], list[dict]]:
    """Split staging-branch open PRs into ``(live, orphaned)``.

    A PR is *orphaned* when its head branch maps to a child task that has been
    CANCELLED: no live task will ever drive it to merge, so blocking the rollup
    on it strands the parent in EVALUATING forever (incident 2026-05-28: an
    orphan PR from a cancelled, retry-replaced child wedged a research
    rollup — the cancel path left the PR open and it carried 0 CI checks, so the
    merge gate never fired either).

    Attribution is by the work-branch id-prefix (see
    :func:`_branch_task_id_prefix`). PRs whose head ref can't be positively
    attributed to a *cancelled* child are kept **live** — we never close a PR we
    can't prove belongs to a dead task.
    """
    live: list[dict] = []
    orphan: list[dict] = []
    for pr in open_prs:
        head_ref = (pr.get("head") or {}).get("ref") or ""
        prefix = _branch_task_id_prefix(head_ref)
        if prefix and prefix in cancelled_child_id_prefixes:
            orphan.append(pr)
        else:
            live.append(pr)
    return live, orphan


def _extract_task_pr_numbers(task) -> frozenset:
    """Extract all PR numbers referenced in a task's context or payload.

    Checks all canonical storage locations:
    - ``context['review_loop']['pr_number']`` (set by the review pipeline)
    - ``context['_pr_url']`` / ``context['pr_url']`` (set by cancel paths)
    - ``context['rollup_pr_number']`` (set for rollup parents in awaiting-approval
      and security-blocked states — never seeds review_loop)
    - ``context['rollup_pr_url']`` (URL counterpart of rollup_pr_number)
    - ``first_payload['pr_number']`` (canonical linkage for review/fix tasks)
    """
    ctx = getattr(task, "context", None) or {}
    nums: set = set()

    rl = ctx.get("review_loop") or {}
    if isinstance(rl, dict):
        rln = rl.get("pr_number")
        if rln is not None:
            try:
                nums.add(int(rln))
            except (ValueError, TypeError):
                pass

    for key in ("_pr_url", "pr_url", "rollup_pr_url"):
        url = ctx.get(key) or ""
        m = re.search(r"/pull/(\d+)", url)
        if m:
            nums.add(int(m.group(1)))

    for key in ("rollup_pr_number",):
        val = ctx.get(key)
        if val is not None:
            try:
                nums.add(int(val))
            except (ValueError, TypeError):
                pass

    fp_pr = (getattr(task, "first_payload", None) or {}).get("pr_number")
    if fp_pr is not None:
        try:
            nums.add(int(fp_pr))
        except (ValueError, TypeError):
            pass

    return frozenset(nums)


def _is_owner_chain_live(task, tasks_by_id: dict) -> bool:
    """Return True if ``task`` (or any replacement in its chain) is live.

    "Live" means a non-terminal task that will drive the PR to merge.
    CANCELLED tasks with a ``_replaced_by_task_id`` are chased through their
    replacement chains using the same liveness predicate as
    :func:`app.orchestrator_health._check_candidate_task_deps`.

    Conservative rule: if a replacement ID is present but the replacement task
    isn't in ``tasks_by_id``, we treat the chain as potentially live — we cannot
    confirm it's dead.
    """
    from app.failure_classification import is_non_death_cancellation

    visited: set = set()
    current = task
    while current is not None:
        tid = getattr(current, "id", None)
        if tid in visited:
            break
        visited.add(tid)

        status = getattr(current, "status", None)

        if status not in (TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED):
            return True  # non-terminal → live

        if status == TaskStatus.COMPLETED:
            return False  # work done, not blocking

        if status == TaskStatus.FAILED:
            # Mirror the CANCELLED branch: follow the replacement chain first so
            # a FAILED owner with a live replacement is treated as live.
            # Genuine FAILED (no replacement, no SCOPE_EMPTY) is dead — not live.
            ctx = getattr(current, "context", None) or {}
            rep_id_str = ctx.get("_replaced_by_task_id")
            if rep_id_str:
                try:
                    rep_id = _uuid.UUID(rep_id_str)
                    replacement = tasks_by_id.get(rep_id)
                    if replacement is not None:
                        current = replacement
                        continue
                    # Replacement exists but not loaded — conservative: treat as live
                    return True
                except (ValueError, AttributeError):
                    pass
            return False  # genuinely failed, SCOPE_EMPTY, or co-deferred — all non-blocking

        # CANCELLED — follow replacement chain
        ctx = getattr(current, "context", None) or {}
        rep_id_str = ctx.get("_replaced_by_task_id")
        if rep_id_str:
            try:
                rep_id = _uuid.UUID(rep_id_str)
                replacement = tasks_by_id.get(rep_id)
                if replacement is not None:
                    current = replacement
                    continue
                # Replacement exists but not in loaded set — conservative: treat as live
                return True
            except (ValueError, AttributeError):
                pass

        if is_non_death_cancellation(current):
            return False  # SCOPE_EMPTY / co-deferred: satisfied, not live

        return False  # permanently cancelled with no replacement

    return False


def _classify_prs_authoritative(
    open_prs: list,
    child_tasks: list,
) -> tuple[list, list]:
    """Classify open PRs into ``(blocking, orphaned)`` using authoritative task ownership.

    Replaces the branch-regex attribution in :func:`_partition_orphan_prs` with
    recorded ``pr_number`` lookup so that staging-branch-headed PRs
    (``plan/8hex``, ``research/8hex``) are attributed correctly regardless of
    head branch shape.

    A PR is **orphaned** when every recorded owner task (found via
    :func:`_extract_task_pr_numbers`) is terminal-and-dead-ended (no live
    replacement chain). PRs with **no recorded owner** stay blocking — we never
    auto-close a PR we can't positively attribute to a task.
    """
    tasks_by_id = {getattr(t, "id", None): t for t in child_tasks}

    pr_owners: dict = {}
    for task in child_tasks:
        for pr_num in _extract_task_pr_numbers(task):
            pr_owners.setdefault(pr_num, []).append(task)

    blocking: list = []
    orphaned: list = []

    for pr in open_prs:
        pr_num = pr.get("number")
        if pr_num is None:
            blocking.append(pr)
            continue

        owners = pr_owners.get(int(pr_num), [])
        if not owners:
            # No recorded owner → conservative: keep blocking
            blocking.append(pr)
            continue

        if any(_is_owner_chain_live(t, tasks_by_id) for t in owners):
            blocking.append(pr)
        else:
            orphaned.append(pr)

    return blocking, orphaned


async def _cleanup_branches(task: Task, session: AsyncSession) -> None:
    """Delete staging and feature branches that are no longer needed."""
    from app.routers.agent import get_github_token_for_task
    from app.github import delete_branch

    ctx = task.context or {}
    gh_token = owner = repo = None

    async def _ensure_token():
        nonlocal gh_token, owner, repo
        if not gh_token:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    branches_to_delete = []

    # Staging branches (research/*, plan/*) are shared by ALL sibling tasks.
    # Only delete when the PARENT task completes (rollup), not on individual child completion.
    # Check if this task IS the parent (has plan_output or is a research task).
    is_parent = task.plan_output is not None or (task.first_payload).get("type") in (
        "research",
        "plan",
    )
    if is_parent:
        # Route by task_type so we only target the staging branch THIS task
        # itself owns. A plan child of a research task inherits the parent's
        # research_branch in its own context; the prior
        # `plan_branch or research_branch` OR-fallback would grab that inherited
        # value when its own plan_branch was unset and delete the parent's
        # branch on child cancel — orphaning sibling work that had merged into
        # it but not yet rolled up to main (incident 2026-05-04: cancelling
        # plan child b9456e05 of research 4759796b deleted research/4759796b
        # 670ms before the parent's rollup, stranding PRs #3606/3607/3612/3613).
        task_type = (task.first_payload).get("type", "")
        if task_type == "research":
            staging = ctx.get("research_branch")
        elif task_type == "plan":
            staging = ctx.get("plan_branch")
        else:
            # is_parent was True via plan_output on a non-research/non-plan
            # task — we don't know which branch type that should own. Skip
            # rather than guess (safer than the prior fallback).
            staging = None
        if staging:
            # Guard 4 (transitive base_branch): keep the branch alive if ANY
            # non-terminal task — at ANY depth, not just direct children —
            # targets it via its payload ``base_branch``. Guards 1-3 only see
            # DIRECT children (parent_task_id), EVALUATING rollups referencing
            # it via context parent_base/original_base, and open PRs. A QUEUED
            # grandchild fix task (child of an intermediate audit task) that
            # targets the staging branch via its payload base_branch and has
            # not yet opened a PR falls through all three — the branch was
            # deleted out from under such tasks ("Could not find remote branch
            # research/<hex> to clone" → permanent failure).
            base_branch_targeters = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.project_id == task.project_id,
                            Task.payloads[0]["base_branch"].as_string() == staging,
                            Task.status.notin_(
                                [
                                    TaskStatus.COMPLETED,
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if base_branch_targeters:
                logger.warning(
                    f"[CLEANUP] Skipping deletion of {staging} — "
                    f"{len(base_branch_targeters)} non-terminal task(s) target it "
                    f"via base_branch"
                )
                # Short-circuit Guards 1-3 and the deletion append below.
                staging = None

        if staging:
            # Guard 1: Don't delete if children are still active (they push to this branch)
            active_children = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.parent_task_id == task.id,
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
            if active_children:
                logger.warning(
                    f"[CLEANUP] Skipping deletion of {staging} — "
                    f"{len(active_children)} child task(s) still active"
                )
            else:
                # Guard 2: Don't delete branches that are target branches for other active rollups
                from sqlalchemy import or_ as sa_or

                referencing_parents = (
                    (
                        await session.execute(
                            select(Task).where(
                                Task.status == TaskStatus.EVALUATING,
                                Task.id != task.id,
                                sa_or(
                                    Task.context["parent_base"].as_string() == staging,
                                    Task.context["original_base"].as_string()
                                    == staging,
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if referencing_parents:
                    logger.warning(
                        f"[CLEANUP] Skipping deletion of {staging} — "
                        f"{len(referencing_parents)} active parent(s) target it as rollup base"
                    )
                else:
                    # Guard 3: Don't delete if there are still open PRs targeting it
                    try:
                        _gh, _owner, _repo = await get_github_token_for_task(
                            task.id, session
                        )
                        from app.github import get_open_pull_requests

                        open_prs = await get_open_pull_requests(
                            _gh, _owner, _repo, staging
                        )
                        if open_prs:
                            pr_nums = [f"#{p['number']}" for p in open_prs]
                            logger.warning(
                                f"[CLEANUP] Skipping deletion of {staging} — {len(open_prs)} open PR(s) targeting it: {', '.join(pr_nums)}"
                            )
                        else:
                            branches_to_delete.append(staging)
                    except Exception:
                        # If we can't check, don't delete (safe default)
                        logger.warning(
                            f"[CLEANUP] Skipping deletion of {staging} — could not verify open PRs"
                        )

    # Feature branches after PR merge
    if ctx.get("pr_merged"):
        project = await session.get(Project, task.project_id)
        if not project:
            # project should never be None in practice (loaded at task creation);
            # a missing row signals a config bug. Log and skip to avoid silent
            # cross-project branch mis-cleanup (would occur if using hardcoded 'blueprint/').
            logger.warning(f"[CLEANUP] Missing project for task {task.id}; skipping branch cleanup")
        else:
            for payload in task.payloads or []:
                branch = payload.get("branch")
                if branch and branch.startswith(project.task_branch_prefix):
                    branches_to_delete.append(branch)

    for branch in branches_to_delete:
        try:
            await _ensure_token()
            deleted = await delete_branch(gh_token, owner, repo, branch)
            if deleted:
                logger.info(f"[CLEANUP] Deleted branch {branch} (task {task.id})")
        except Exception as e:  # noqa: PERF203 — per-branch try is required so one delete failure doesn't abort the rest
            logger.warning(f"[CLEANUP] Failed to delete {branch}: {e}", exc_info=True)
