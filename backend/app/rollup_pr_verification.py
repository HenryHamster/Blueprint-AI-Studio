"""Rollup PR-merge verification seam.

Extracted from ``rollup_management.py`` to keep that module under its size
guardrail. Contains the PR-merge status lookup (``_verify_pr_merged``) plus its
negative cache, the review-respawn cap constant/counter, and the rollup-side
terminal-failure bookkeeping it depends on.

These symbols are re-exported from ``app.rollup_management`` so existing caller
and test import paths keep working unchanged.

All deferred imports from ``app.main`` / routers are intentional — they avoid
circular imports and preserve test-patchability.
"""

# fmt: off
import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    Artifact,
    TaskStatus,
    ArtifactType,
    Project,
)
from app.branch_resolution import merged_pr_counts_as_integration_landing

logger = logging.getLogger(__name__)


# Circuit-breaker: cap how many times the stale-PR rollup recovery in
# ``_check_and_trigger_rollup`` respawns a review for one unmerged child.
# Without a cap, a review that keeps failing fast — e.g. pre-empted at dispatch
# — is respawned every rollup cycle unbounded (2026-05-09 incident: 795
# SCOPE_EMPTY review tasks in 9.5h). Past the cap the rollup escalates the
# parent to manual approval instead of looping.
MAX_REVIEW_RESPAWNS = 5


def _review_respawn_count(task: Task) -> int:
    """Times the stale-PR rollup recovery has respawned a review for *task*.

    Stored in ``context._review_respawn_count``; capped by ``MAX_REVIEW_RESPAWNS``.
    The key is underscore-prefixed so ``_spawn_pipeline_task``'s context
    propagation (which copies all non-underscore keys onto the spawned review
    task) drops it — a non-prefixed key would leak a stale count onto every
    review task spawned for this PR.
    Tolerates a missing/None context or a non-numeric value (-> 0).
    """
    try:
        return int((task.context or {}).get("_review_respawn_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


# Module-level negative cache for PR-merge verification keyed by task.id.
# When a cycle discovers a child's PR isn't merged yet, the reaper's next
# 60s tick (plus any fan-out from sibling completion events) would otherwise
# re-fetch the same PR.  Stamping here is session-independent — unlike a
# task.context mutation, which only persists if the caller happens to commit
# the attached object.  A 30s TTL is short enough that a merge happening
# between checks is detected within one cycle.
_PR_MERGE_NEGATIVE_TTL_S = 30
_pr_merge_negative_cache: dict[uuid.UUID, float] = {}


def _record_rollup_failure_signals(task: Task, failure_class: str) -> None:
    """Mirror agent_status's terminal-failure bookkeeping for rollup-side FAILEDs.

    Without this, parent failures terminated inside _check_and_trigger_rollup
    never reach deploy_monitoring['task_results'] or the per-direction streak,
    so anomaly detection and direction-hold logic can't see them.
    """
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    record_task_result(
        success=False,
        failure_class=failure_class,
        task_id=str(task.id),
        task_type=task.first_payload.get("type", ""),
    )
    direction_id = str((task.context or {}).get("direction_id") or "")
    if direction_id:
        record_direction_result(
            direction_id,
            success=False,
            failure_class=failure_class,
            project_id=str(task.project_id) if task.project_id else "",
        )


async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.routers.agent import get_github_token_for_task
    from app.github import get_pull_request

    if (task.context or {}).get("pr_merged"):
        return True

    # Negative-result cache: when a batch of sibling children completes in
    # quick succession, every completion event triggers _check_and_trigger_rollup
    # which re-verifies every child.  Without this, 20 children completing at
    # once == 400 PR lookups in a short window.  Module-level dict keyed by
    # task.id — session-independent so the check works regardless of whether
    # the caller commits between fan-out calls.
    _last_negative = _pr_merge_negative_cache.get(task.id)
    if (
        _last_negative is not None
        and (time.time() - _last_negative) < _PR_MERGE_NEGATIVE_TTL_S
    ):
        import app.shared_state as _ss

        _ss.github_cache_hits["pr_merge_negative"] = (
            _ss.github_cache_hits.get("pr_merge_negative", 0) + 1
        )
        return False

    # Look for PR artifact to verify via API
    art_result = await session.execute(
        select(Artifact).where(
            Artifact.task_id == task.id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
    )
    artifact = art_result.scalars().first()
    if not artifact or not artifact.pr_url:
        # No PR artifact — task completed without creating a PR (e.g., no changes needed).
        # Treat as "done" so it doesn't block rollup.
        # Cache in context so partial-rollup path (which checks context.pr_merged directly)
        # doesn't see this task as "unmerged".
        task.context = {**(task.context or {}), "pr_merged": True}
        return True
    if artifact and artifact.pr_url:
        # Shape-validate before parsing. Production incident 2026-05-06: a
        # haiku retry whose ./check viewport gate was wedged improvised a
        # `https://github.com/.../compare/main...blueprint/task-<id>` URL into
        # the wrapper's PR-URL marker file; the wrapper trusted it and saved
        # a PULL_REQUEST artifact with that pr_url. Without this guard the
        # int(...) below crashed every rollup cycle (~36x/window) and dirtied
        # the log with full tracebacks. The wrapper now validates too
        # (agent/src/handlers/code-change.ts), but bad rows from before that
        # fix still exist.
        last_seg = artifact.pr_url.rstrip("/").split("/")[-1]
        if "/pull/" not in artifact.pr_url or not last_seg.isdigit():
            logger.warning(
                f"[PR_VERIFY] Task {task.id} has malformed PR artifact "
                f"(pr_url={artifact.pr_url!r}); treating as not-merged"
            )
            _pr_merge_negative_cache[task.id] = time.time()
            return False
        try:
            pr_number = int(last_seg)
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            try:
                pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            except HTTPException as api_err:
                if api_err.status_code not in (401, 500, 502, 503):
                    raise
                if api_err.status_code != 401:
                    # Transient GitHub API failure — can't verify now, will retry next cycle
                    logger.warning(
                        f"[PR_VERIFY] GitHub API error ({api_err.status_code}) checking PR #{pr_number} for task {task.id} — skipping"
                    )
                    return False
                # Token expired — invalidate cache and retry once with a fresh token
                from app.routers.agent import invalidate_github_token_cache

                invalidate_github_token_cache(str(task.project_id))
                logger.warning(
                    f"[PR_VERIFY] Auth failure for task {task.id} PR #{pr_number} — retrying with fresh token"
                )
                gh_token, owner, repo = await get_github_token_for_task(
                    task.id, session, force_refresh=True
                )
                pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("merged"):
                project = (
                    await session.get(Project, task.project_id)
                    if task.project_id is not None
                    else None
                )
                merged_base_branch = (pr_data.get("base") or {}).get("ref")
                record_pr_merged = await merged_pr_counts_as_integration_landing(
                    task,
                    project,
                    session,
                    pr_number=pr_number,
                    merged_base_branch=merged_base_branch,
                    source="rollup_pr_verification.verify_pr_merged",
                )
                if record_pr_merged:
                    # Update in-memory first so caller sees the change even if DB write
                    # fails.
                    task.context = {**(task.context or {}), "pr_merged": True}
                    # Persist to DB in separate session (best-effort — may fail if
                    # caller holds FOR UPDATE lock on this row, but in-memory update is
                    # sufficient for the current rollup cycle).
                    try:
                        async with AsyncSessionLocal() as write_session:
                            db_task = await write_session.get(Task, task.id)
                            if db_task:
                                db_task.context = {
                                    **(db_task.context or {}),
                                    "pr_merged": True,
                                }
                                write_session.add(db_task)
                                await write_session.commit()
                    except Exception as write_err:
                        logger.warning(
                            f"[PR_VERIFY] Failed to persist pr_merged for task {task.id}: {write_err}"
                        )
                return True
            # Detect closed-but-not-merged (rejected/abandoned PR) — don't block rollup forever
            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                _ctx = task.context or {}
                # Distinguish a DELIBERATE success-close from a rejection. A PR
                # can be closed *because the work was already achieved* — not
                # rejected:
                #   • pr_closed_empty   — the empty/branch-equivalent rollup PR
                #     close (review pipeline) found no diff to merge.
                #   • pr_closed_success — the same change merged via a sibling /
                #     dedup PR ("already merged by another task", set by
                #     ci_completion), or an agent exercised its early-exit right.
                # In those cases the task's desired outcome IS in the target
                # branch, so the child is COMPLETED, not FAILED — failing it
                # mislabels a success and can cascade a rollup parent into a
                # partial-failure finalize. A genuine rejection (review-cap
                # exhausted, manual close) carries neither marker → stays FAILED.
                if _ctx.get("pr_closed_empty") or _ctx.get("pr_closed_success"):
                    logger.info(
                        f"[PR_VERIFY] Task {task.id} PR #{pr_number} closed without "
                        f"merging, but marked as a deliberate success-close (work "
                        f"landed elsewhere) — completing instead of failing"
                    )
                    merged_base_branch = (pr_data.get("base") or {}).get("ref")
                    project = (
                        await session.get(Project, task.project_id)
                        if task.project_id is not None
                        else None
                    )
                    record_pr_merged = await merged_pr_counts_as_integration_landing(
                        task,
                        project,
                        session,
                        pr_number=pr_number,
                        merged_base_branch=merged_base_branch,
                        source="rollup_pr_verification.success_close",
                    )
                    task.context = (
                        {**_ctx, "pr_merged": True}
                        if record_pr_merged
                        else dict(_ctx)
                    )
                    if task.status != TaskStatus.COMPLETED:
                        task.status = TaskStatus.COMPLETED
                    try:
                        async with AsyncSessionLocal() as write_session:
                            db_task = await write_session.get(Task, task.id)
                            if db_task:
                                if record_pr_merged:
                                    db_task.context = {
                                        **(db_task.context or {}),
                                        "pr_merged": True,
                                    }
                                if db_task.status != TaskStatus.COMPLETED:
                                    db_task.status = TaskStatus.COMPLETED
                                write_session.add(db_task)
                                await write_session.commit()
                    except Exception as _sc_err:
                        logger.warning(
                            f"[PR_VERIFY] Failed to persist success-close for task "
                            f"{task.id}: {_sc_err}"
                        )
                    return True
                logger.warning(
                    f"[PR_VERIFY] Task {task.id} PR #{pr_number} was closed without merging — marking as rejected"
                )
                # Update in-memory first — set message AND status together so the
                # outer session's commit always carries the explanatory message,
                # even if the separate write_session below fails (row lock, etc.).
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                task.latest_status_message = f"PR #{pr_number} closed without merging"
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                _record_rollup_failure_signals(task, _fc)
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {
                                **(db_task.context or {}),
                                "pr_closed_unmerged": True,
                                "failure_category": _fc,
                            }
                            db_task.status = TaskStatus.FAILED
                            db_task.latest_status_message = (
                                f"PR #{pr_number} closed without merging"
                            )
                            write_session.add(db_task)
                            await _maybe_create_failure_alert(
                                db_task,
                                write_session,
                                f"PR #{pr_number} was closed without merging",
                            )
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(
                        f"[PR_VERIFY] Failed to persist pr_closed_unmerged for task {task.id}: {write_err}"
                    )
                return True  # Return True so callers don't block — task is now FAILED which they handle
        except Exception as e:
            logger.warning(
                f"[PR_VERIFY] Failed to verify PR merge for task {task.id}: {e}",
                exc_info=True,
            )
    # Stamp the negative cache so the next caller within the TTL skips the
    # GitHub round-trip.  Lazy eviction: keep it lightweight, drop entries
    # that are clearly stale whenever we detect the dict is oversized.
    _pr_merge_negative_cache[task.id] = time.time()
    if len(_pr_merge_negative_cache) > 2000:
        cutoff = time.time() - (_PR_MERGE_NEGATIVE_TTL_S * 4)
        for k in [k for k, ts in _pr_merge_negative_cache.items() if ts < cutoff]:
            _pr_merge_negative_cache.pop(k, None)
    return False
