"""Rollup PR management and branch cleanup.

Extracted from task_lifecycle.py to reduce its size.  Contains the three
rollup-related helpers: _verify_pr_merged, _cleanup_branches, and
_check_and_trigger_rollup.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (rollup_management is imported BY main indirectly via task_lifecycle)
and preserve test-patchability.
"""

# fmt: off
import logging
import time
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.migration_path import is_migration_file
from app.models import (
    Task,
    Artifact,
    Project,
    TaskStatus,
    ArtifactType,
)
from app.review_pipeline import _spawn_pipeline_task, _is_pipeline_task_stale
from app.rollup_state import (  # noqa: F401 — re-exported for legacy callers/tests
    _ROLLUP_TRANSIENT_CTX_KEYS,
    _clear_rollup_transient_state,
)
from app.shared_state import (
    runtime_config as _runtime_config,
    derive_task_branch as _derive_task_branch,
    get_project_integration_branch as _get_project_integration_branch,
)

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


def _extract_rollup_migration_sources(diff: str) -> dict[str, str]:
    """Reconstruct added/changed Alembic migration file contents from a PR diff.

    Returns a mapping of ``alembic/versions`` file path -> reconstructed source
    (the post-image, from the diff's ``+`` lines). Newly-added migration files
    appear in full; this is enough to read their in-file revision/down_revision.
    An empty result means the rollup diff does not touch the versions directory.
    """
    sources: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if is_migration_file(path) and path.endswith(".py"):
                current = sources.setdefault(path, [])
            else:
                current = None
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current.append(line[1:])
    return {path: "\n".join(lines) for path, lines in sources.items()}


def _touches_alembic_versions(filename: str) -> bool:
    return is_migration_file(filename) and filename.endswith(".py")


async def _validate_rollup_migration_chain(
    gh_token: str, owner: str, repo: str, pr_number: int
) -> tuple[str, str | None]:
    """Fail-safe Alembic-chain guard for rollup auto-merge.

    Returns ``("ok", None)`` when the rollup does not touch ``alembic/versions``
    or the combined migration set is valid (single head, unique revisions).
    Returns ``("defer", reason)`` when GitHub metadata cannot be fetched (retry
    next sweep) and ``("block", reason)`` when the combined chain is invalid or
    a touched migration file is unparseable. Never waves a merge through on an
    inability to fetch or parse the migration files.
    """
    from app.github import get_pr_files, get_pull_request_diff
    from app.migration_chain import validate_added_migrations

    try:
        files = await get_pr_files(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not list rollup files to check Alembic migrations: "
            f"{type(e).__name__}: {e}",
        )
    if not any(_touches_alembic_versions(f.get("filename", "")) for f in files):
        return ("ok", None)

    try:
        diff = await get_pull_request_diff(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not fetch rollup diff to validate Alembic migrations: "
            f"{type(e).__name__}: {e}",
        )
    if not diff:
        return (
            "defer",
            "could not fetch rollup diff to validate Alembic migrations (empty response)",
        )
    reason = validate_added_migrations(_extract_rollup_migration_sources(diff))
    return ("ok", None) if reason is None else ("block", reason)


class _ParentMutatedDuringPRCreation(Exception):
    """Sentinel: parent row was mutated by a peer during create_pull_request.

    Inside ``_check_and_trigger_rollup``, when ``_relock_parent`` returns None
    after the rollup PR has already been created on GitHub, the orphan-PR
    rescue path in the outer except handler must persist the rollup_pr_number
    marker via a fresh session, then bail — the rest of the handler depends
    on ``parent`` being a live ORM instance, which it isn't here.
    """


# Module-level negative cache for PR-merge verification keyed by task.id.
# When a cycle discovers a child's PR isn't merged yet, the reaper's next
# 60s tick (plus any fan-out from sibling completion events) would otherwise
# re-fetch the same PR.  Stamping here is session-independent — unlike a
# task.context mutation, which only persists if the caller happens to commit
# the attached object.  A 30s TTL is short enough that a merge happening
# between checks is detected within one cycle.
_PR_MERGE_NEGATIVE_TTL_S = 30
_pr_merge_negative_cache: dict[uuid.UUID, float] = {}


# Phase B3 (lock-window break): each GitHub API call inside
# _check_and_trigger_rollup can hang 5-30s (worst case minutes during
# rate-limit). Holding the parent's FOR UPDATE lock across that window
# starves concurrent heartbeats on sibling children — they see 30s
# lock_timeout and the reaper false-orphans them. The fix is to commit
# any pending mutations to release the lock, perform the GitHub call(s)
# without lock held, then re-acquire and validate `updated_at` to detect
# whether the staging-state changed during the unlocked window.
#
# Round 4 W1 (audit C1, C3, H6): three structural fixes layered on top:
#   - C1 (cache): SQLAlchemy identity map returned cached objects on
#     re-SELECT under ``expire_on_commit=False``, so the validation
#     never fired. Fix: use a fresh AsyncSession for the re-acquire so
#     the new SELECT bypasses the prior session's identity map.
#     ``populate_existing=True`` is also added as belt-and-suspenders
#     (defensive against future callers that re-use the same session).
#   - C3 (heartbeat): heartbeat ``onupdate=_utcnow`` advanced
#     ``updated_at`` every 30s, false-deferring on every overlapping
#     heartbeat. Fix: validate against a content snapshot of the fields
#     the unlocked window is actually meant to protect (status,
#     requires_approval, rollup_pr_number/_url markers, pr_merged) —
#     NOT ``updated_at``, NOT ``last_heartbeat``.
#   - H6 (pool): the prior pattern held a connection through the 5-150s
#     HTTP retry window. Fix: explicitly close the session after the
#     release-commit, run HTTP without DB connection, open a fresh
#     session for the re-acquire.
def _capture_rollup_parent_snapshot(parent: Task) -> dict:
    """Snapshot the rollup-parent fields that lock-window-break protects.

    The unlocked window between ``_release_parent_lock`` and ``_relock_parent``
    is meant to detect peer mutation of the rollup state machine: status
    transitions (EVALUATING → COMPLETED/FAILED), the rollup PR marker
    (``rollup_pr_number`` / ``rollup_pr_url``, the crash-recovery marker
    written at line ~1046), the ``pr_merged`` flag, and approval-gate flips.
    Heartbeat-only changes (last_heartbeat, last_metrics_json,
    heartbeat_count) don't touch any of these fields, so they don't trip
    the validator under load.
    """
    ctx = parent.context or {}
    return {
        "status": parent.status,
        "requires_approval": bool(parent.requires_approval),
        "rollup_pr_number": ctx.get("rollup_pr_number"),
        "rollup_pr_url": ctx.get("rollup_pr_url"),
        "pr_merged": ctx.get("pr_merged"),
    }


async def _release_parent_lock(
    parent: Task,
    session: AsyncSession,
    *,
    context_dirty: bool = False,
) -> dict:
    """Commit any pending mutations on parent and release the FOR UPDATE lock.

    Returns a content snapshot that ``_relock_parent`` will compare against
    after the unlocked I/O window to detect peer mutation of the rollup
    state machine. Heartbeat-driven ``updated_at`` advances DO NOT trip the
    validator (R4-W1 audit C3).

    Round 4 W1 (H6): SQLA 2.0 async sessions return their underlying
    connection to the pool on transaction commit (default
    ``reset_on_return="rollback"``). The HTTP work that follows therefore
    runs without a checked-out connection — the next operation on the
    session auto-acquires a fresh connection. This addresses the pool-
    exhaustion concern at 1k-user scale without forcing every caller to
    juggle two sessions. ``populate_existing=True`` in ``_relock_parent``
    bypasses the identity-map cache (audit C1) for the re-SELECT.

    R5-W3-B (audit 2026-04-30): the prior version of this function called
    ``flag_modified(parent, "context")`` UNCONDITIONALLY, which forced a
    JSONB-column rewrite on every commit even when the caller hadn't
    actually mutated ``parent.context``. Most release sites in
    ``_check_and_trigger_rollup`` (lines ~954, ~1033, ~1098, ~1224, ~1290,
    ~1343) are read-only — the function snapshots state, releases the lock
    around external I/O, then re-acquires. With ~6 release/relock cycles
    per rollup decision and parent contexts that can grow to 100+ KB,
    flag_modified-on-every-release amplified TOAST-write traffic by 6x for
    no DB-correctness benefit.

    Switched to caller-driven: callers that mutated ``parent.context``
    before this call MUST pass ``context_dirty=True``. The default of
    ``False`` matches the actual behavior of every read-only call site.
    Callers that mutate context after a re-lock typically do so via
    ``parent.context = {...}`` and ``session.add(parent)`` directly, then
    ``session.commit()`` — those don't go through this function and the
    SQLA assignment-tracking already detects the mutation.

    Historical note (Round 4 W1 audit C1): the unconditional
    ``flag_modified`` was added because, after a prior ``_relock_parent``
    did ``populate_existing`` + ``expire_all``, the NEXT mutation to
    ``parent.context`` could go undetected by SQLA's change-tracking —
    empirically verified against the merge handler test. That risk still
    applies for callers that DO mutate context before release, hence the
    explicit ``context_dirty`` flag (rather than removing the call
    entirely).
    """
    if context_dirty and parent.context is not None:
        flag_modified(parent, "context")
    session.add(parent)
    await session.commit()
    return _capture_rollup_parent_snapshot(parent)


async def _relock_parent(
    session: AsyncSession,
    parent_id,
    expected_snapshot: dict | None,
    *,
    parent_in_session: Task | None = None,
) -> Task | None:
    """Re-acquire FOR UPDATE on the parent row and validate staging-state.

    Returns the freshly-loaded Task if the row is still ours and the
    rollup-state snapshot still matches, or None if:
        - the row is held by another writer (skip_locked yielded nothing), OR
        - any field in the captured snapshot mismatches (peer mutated the
          rollup state machine during the unlocked window).

    Round 4 W1 (audit C1, C3): replaces the prior ``expected_updated_at``
    check (which never fired due to the identity-map cache, and would
    false-trip on heartbeats even if the cache were bypassed). Validation
    is now against ``_capture_rollup_parent_snapshot`` — a content-based
    check covering status, requires_approval, rollup_pr_number/url,
    pr_merged. ``populate_existing=True`` forces SQLA to refresh
    attributes on the cached identity-map instance from this SELECT,
    bypassing the cache that previously made the validation a no-op
    under ``expire_on_commit=False``. When the caller mutates
    ``parent.context`` before release, it must pass
    ``context_dirty=True`` to ``_release_parent_lock`` so the
    ``flag_modified`` call fires and the mutation persists across the
    populate_existing's change-tracking reset (R5-W3-B; verified
    empirically — see ci_completion.py for the canonical comment).
    Read-only release sites omit the flag and pay no JSONB-write cost.

    Round 5 W1 (HIGH-2 belt-and-suspenders): callers can pass the
    released-but-cached ``parent`` instance via ``parent_in_session`` so
    we ``session.expire`` it before the re-SELECT. ``populate_existing``
    alone has been observed to be unreliable under async +
    ``with_for_update(skip_locked=True)`` — expiring the cached instance
    first guarantees the re-SELECT cannot return stale identity-map
    attributes. Mirrors the ``session.expire(task)`` pattern at
    ``task_completion.py:1671`` and ``ci_completion._relock_merge_task``.
    (Earlier docs cited reaper.py:382 as the original documentation site;
    the bug-doc comment has since rotated and `_relock_parent` is now the
    canonical reference for the pattern.)

    Mirrors the early-commit pattern in routers/agent_status.py:1694: release
    the lock around long-running I/O, then resume on a fresh row read.
    """
    if parent_in_session is not None:
        try:
            session.expire(parent_in_session)
        except InvalidRequestError:
            # Object detached from this session (e.g. expunged elsewhere)
            # — expire_all() is a safe broader fallback; the populate_existing
            # SELECT below will repopulate whatever we still need.
            session.expire_all()
    stmt = (
        select(Task)
        .where(Task.id == parent_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(stmt)).scalars().first()
    if fresh is None:
        logger.info(
            "[ROLLUP] Re-lock skipped for parent %s — row held by peer, "
            "deferring to next cycle",
            parent_id,
        )
        return None
    if expected_snapshot is not None:
        actual = _capture_rollup_parent_snapshot(fresh)
        if actual != expected_snapshot:
            logger.info(
                "[ROLLUP] Re-lock detected concurrent mutation on parent %s "
                "(expected snapshot=%s, found %s) — deferring to next cycle",
                parent_id,
                expected_snapshot,
                actual,
            )
            return None
    return fresh


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


async def _dispatch_rollup_approval_alert(
    parent: Task, session: AsyncSession, reason: str, pr_number: int | None = None
) -> None:
    """Create a failure alert and fire an external APPROVAL_NEEDED notification
    when a rollup parent enters the approval-gated state so operators are
    notified instead of discovering the stall later.
    """
    import asyncio as _asyncio
    from app.main import _maybe_create_failure_alert
    from app.notification_dispatch import dispatch_external_notification

    try:
        await _maybe_create_failure_alert(parent, session, reason)
    except Exception as e:
        logger.warning(f"[NOTIFY] rollup approval alert creation failed: {e}")
    try:
        parent_ctx = parent.context if isinstance(parent.context, dict) else {}
        parent_payload = parent.first_payload or {}
        pr_url = parent_ctx.get("rollup_pr_url") or ""
        if not pr_url and pr_number:
            repo_full = parent_ctx.get("repo_full_name") or parent_ctx.get("repo") or ""
            if repo_full:
                pr_url = f"https://github.com/{repo_full}/pull/{pr_number}"
        raw_critical = (
            parent_ctx.get("critical_files")
            or parent_ctx.get("_pr_changed_files")
            or []
        )
        critical_files = (
            [f for f in raw_critical if isinstance(f, str)][:10]
            if isinstance(raw_critical, list)
            else []
        )
        direction_text = (
            parent_ctx.get("direction_text")
            or parent_payload.get("direction_text")
            or ""
        )
        scope = parent_ctx.get("scope") or parent_payload.get("scope") or ""
        # Fire-and-forget best-effort external notification; losing the task
        # on GC is acceptable and the call is already try/except-wrapped, so
        # RUF006 (store-a-reference) is intentionally suppressed below.
        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                parent.project_id,
                "APPROVAL_NEEDED",
                {
                    "task_id": str(parent.id),
                    "task_title": parent.title or str(parent.id)[:8],
                    "pr_number": pr_number,
                    "reason": reason,
                    "approval_reason": reason,
                    "pr_url": pr_url,
                    "critical_files": critical_files,
                    "direction_id": parent_ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": scope,
                },
            )
        )
    except Exception as e:
        logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {e}")


async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    from app.main import _maybe_create_failure_alert
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
                # Update in-memory first so caller sees the change even if DB write fails
                task.context = {**(task.context or {}), "pr_merged": True}
                # Persist to DB in separate session (best-effort — may fail if caller
                # holds FOR UPDATE lock on this row, but in-memory update is sufficient
                # for the current rollup cycle)
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
        branch_prefix = project.task_branch_prefix if project else "blueprint/"
        for payload in task.payloads or []:
            branch = payload.get("branch")
            if branch and branch.startswith(branch_prefix):
                branches_to_delete.append(branch)

    for branch in branches_to_delete:
        try:
            await _ensure_token()
            deleted = await delete_branch(gh_token, owner, repo, branch)
            if deleted:
                logger.info(f"[CLEANUP] Deleted branch {branch} (task {task.id})")
        except Exception as e:  # noqa: PERF203 — per-branch try is required so one delete failure doesn't abort the rest
            logger.warning(f"[CLEANUP] Failed to delete {branch}: {e}", exc_info=True)


def _build_rollup_pr_body(
    task_count: int,
    staging_branch: str,
    rollup_note: str = "",
    *,
    direction_id: str | None = None,
    direction_title: str | None = None,
    children: list | None = None,
) -> str:
    """Build the Markdown body for a rollup PR.

    Includes direction, task-type breakdown, and aggregate cost when available.
    """
    lines: list[str] = [f"Rollup of {task_count} subtasks from `{staging_branch}`"]

    if direction_title or direction_id:
        dir_label = (direction_title or "")[:80]
        dir_suffix = f" `{direction_id[:8]}`" if direction_id else ""
        lines.append("")
        lines.append(f"**Direction:** {dir_label}{dir_suffix}")

    if children:
        from collections import Counter

        type_counts: Counter = Counter()
        total_cost: float = 0.0
        has_cost = False
        for c in children:
            payloads = getattr(c, "payloads", None) or []
            t = payloads[0].get("type", "create") if payloads else "create"
            type_counts[t] += 1
            prov_at = getattr(c, "vm_provisioned_at", None)
            stop_at = getattr(c, "vm_stopped_at", None)
            if prov_at and stop_at:
                from app.admin import SPOT_RATE, ON_DEMAND_RATE

                rate = SPOT_RATE if getattr(c, "vm_spot", False) else ON_DEMAND_RATE
                total_cost += (stop_at - prov_at).total_seconds() * rate
                has_cost = True
        if type_counts:
            breakdown = ", ".join(
                f"{cnt} {typ}" for typ, cnt in sorted(type_counts.items())
            )
            lines.append("")
            lines.append(f"**Tasks:** {breakdown}")
        if has_cost:
            lines.append(f"**Estimated cost:** ~${total_cost:.2f}")

    if rollup_note:
        lines.append(rollup_note)

    lines.append("")
    lines.append("---")
    lines.append("*Automated by Blueprint Pipeline*")
    return "\n".join(lines)


async def _check_and_trigger_rollup(child_task: Task, session: AsyncSession) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.
    """
    from app.main import _maybe_create_failure_alert, _utcnow
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        delete_branch,
        create_pull_request,
        merge_pull_request,
        get_pull_request,
        get_pull_request_diff,
        get_pr_files,
        get_branch_sha,
        close_pull_request,
        update_branch_with_sha_retry,
    )

    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions.
    # skip_locked: if a sibling child is already running this check, defer — the
    # reaper's EVALUATING stale-parent sweep retries within 15 min as safety net.
    parent = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == child_task.parent_task_id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if parent is None:
        logger.info(
            f"[ROLLUP] Skipped rollup check for task {child_task.id} — parent lock held, "
            "reaper sweep will retry"
        )
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # Lock children with skip_locked to avoid deadlocks with concurrent
    # agent status updates that hold FOR UPDATE on individual child rows.
    all_children = (
        (
            await session.execute(
                select(Task)
                .where(Task.parent_task_id == parent.id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    # Verify we locked all children — if some were skipped (held by agent_status
    # or another rollup), defer to avoid acting on incomplete data.
    total_children = await session.scalar(
        select(_func.count()).select_from(Task).where(Task.parent_task_id == parent.id)
    )
    if len(all_children) < total_children:
        logger.info(
            f"[ROLLUP] Deferred rollup for parent {parent.id} — "
            f"locked {len(all_children)}/{total_children} children (others held)"
        )
        return

    # Consider create, refactor, plan, research, and specialized agent tasks for rollup decisions.
    # Review/fix tasks are pipeline helpers that operate on the same PR — excluded.
    ROLLUP_TYPES = {
        "create",
        "refactor",
        "migrate",
        "",
        "plan",
        "research",
        "security_audit",
        "qa",
        "deps",
        "docs",
        "smoketest",
        "perf",
    }
    children = [
        c
        for c in all_children
        if (c.first_payload).get("type", "create") in ROLLUP_TYPES
    ]

    # Guard: if all children are review/fix types (none in ROLLUP_TYPES), mark parent COMPLETED
    if not children and all_children:
        parent.status = TaskStatus.COMPLETED
        parent.context = _clear_rollup_transient_state(parent.context)
        parent.latest_status_message = (
            "All subtasks were pipeline tasks (review/fix) — no rollup needed"
        )
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} completed — all children were review/fix pipeline tasks"
        )
        return

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress. Stale tasks (no heartbeat for
    # 15+ min) are excluded so a silently-dead pipeline task can't permanently
    # block rollup; the orchestrator's reaper will eventually cancel them.
    pipeline_active = False
    for c in all_children:
        if (c.first_payload).get("type") not in ("review", "fix"):
            continue
        if c.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ):
            continue
        if _is_pipeline_task_stale(c):
            logger.warning(
                f"[ROLLUP] Ignoring stale pipeline task {str(c.id)[:8]} "
                f"(status={c.status}, last_heartbeat={c.last_heartbeat}) — not blocking rollup"
            )
            continue
        pipeline_active = True
        break

    # Tally non-IO statuses first so we can decide whether the GitHub-backed
    # PR-merge verification loop is even necessary. ``all_done`` starts True
    # and gets cleared by FAILED/active children below; the verify loop also
    # clears it for any unmerged COMPLETED child (mirrors original semantics).
    all_done = True
    cancelled_count = 0
    active_count = 0
    completed_children: list[Task] = []
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        if c.status == TaskStatus.COMPLETED:
            completed_children.append(c)

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        if pipeline_active and (parent.latest_status_message or "").startswith(
            "Blocked:"
        ):
            parent.latest_status_message = (
                "Fix/review pipeline running — waiting for PR to merge"
            )
            session.add(parent)
            await session.commit()
        return

    # ── Phase B3 lock-window break (Site 1: _verify_pr_merged) ──
    # _verify_pr_merged calls get_pull_request + get_github_token_for_task per
    # COMPLETED child. With 5-10 unmerged children x 30s GH the worst-case
    # held-lock window is 150-300s on the parent + N children. Release the
    # parent lock (and the children FOR UPDATE locks, which share this
    # session's transaction) around the verification loop and re-acquire the
    # parent afterwards so concurrent sibling heartbeats and admin writes
    # don't queue behind us. The verification only READS PR state from
    # GitHub and updates in-memory + DB via _verify_pr_merged's own
    # write_session, not the caller's session.
    parent_id_local = parent.id
    pre_verify_snapshot = await _release_parent_lock(parent, session)

    # Run the GitHub round-trips with no DB locks held. _verify_pr_merged
    # mutates the cached child's in-memory `task.status` (PR closed unmerged
    # → FAILED) and `task.context["pr_merged"]`, and commits the persisted
    # write via its own AsyncSessionLocal. The cached `children` list
    # objects retain those in-memory mutations across the lock-window break
    # (expire_on_commit=False), preserving original semantics: a child
    # whose verify-call returned True keeps the verify branch's
    # all_done contribution at True, even if the call also flipped its
    # status to FAILED. Downstream branches (the
    # `failed_count == len(children)` guard at line ~927 and the
    # rollup-prep loop) re-derive failed/completed_merged from `c.status`
    # on the same cached list and handle the post-verify FAILED case.
    for c in completed_children:
        if not await _verify_pr_merged(c, session):
            all_done = False

    # ── Re-acquire parent lock for downstream mutations ──
    # We deliberately do NOT re-fetch the children rows nor re-derive
    # all_done from the cached list: original behavior was that a verify
    # call returning True meant "child resolved" regardless of any
    # status side-effect (e.g. PR-closed-unmerged → FAILED), and the
    # downstream `completed_count == 0 and failed_count > 0` guard
    # finalizes the parent. Preserving this contract by carrying
    # `all_done` from the verify loop forward to the existing dispatch.
    parent = await _relock_parent(
        session,
        parent_id_local,
        pre_verify_snapshot,
        parent_in_session=parent,
    )
    if parent is None:
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # All children terminal but not all succeeded — finalize immediately instead of waiting 6 hours
    if not all_done and active_count == 0 and not pipeline_active:
        # Cap-exhausted children (requires_approval=True, PR auto-closed by
        # _handle_cap_exhaustion) are excluded from the unmerged respawn list:
        # respawning hits the cap again every cycle. Post-fix the cap handler
        # transitions them to FAILED with failure_detail=review_cap_exhausted;
        # this filter defensively covers any legacy COMPLETED+requires_approval
        # children that predate the fix.
        unmerged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED
            and not (c.context or {}).get("pr_merged")
            and not c.requires_approval
        ]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        completed_merged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED and (c.context or {}).get("pr_merged")
        ]

        if unmerged:
            # Some completed but PRs not yet merged.
            # Check if any are stale (>10min) with no active review — respawn pipeline.
            now = _utcnow()
            stale_threshold = timedelta(minutes=10)
            respawned = 0
            for c in unmerged:
                age = (now - c.updated_at).total_seconds() if c.updated_at else 0
                if age < stale_threshold.total_seconds():
                    continue  # Too recent — normal pipeline may still be working
                # Look up PR artifact to get pr_number and branch
                pr_art = (
                    (
                        await session.execute(
                            select(Artifact)
                            .where(
                                Artifact.task_id == c.id,
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            )
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                if not pr_art or not pr_art.pr_url:
                    # No PR artifact — _verify_pr_merged should have handled this.
                    # Force-set pr_merged so next cycle picks it up.
                    c.context = {**(c.context or {}), "pr_merged": True}
                    session.add(c)
                    logger.info(
                        f"[ROLLUP] Child {str(c.id)[:8]} has no PR artifact — marking as merged"
                    )
                    continue
                try:
                    pr_number = int(pr_art.pr_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    # Malformed pr_url (e.g. /commit/<sha>) — cannot parse a PR number.
                    # Force-set pr_merged so the next rollup cycle progresses instead of
                    # hanging forever. ROOT CAUSE 2/3 fixes upstream make this rare.
                    logger.warning(
                        f"[ROLLUP] malformed pr_url {pr_art.pr_url!r} for child "
                        f"{str(c.id)[:8]} — cannot parse PR number; "
                        f"force-setting pr_merged=True so rollup progresses"
                    )
                    c.context = {**(c.context or {}), "pr_merged": True}
                    session.add(c)
                    continue

                # Circuit-breaker: once a child's review has been respawned
                # MAX_REVIEW_RESPAWNS times without the PR merging, stop looping
                # and escalate to manual approval (mirrors the cap-exhaustion
                # escalation below). Guards against a review that keeps failing
                # fast — e.g. dispatch pre-emption — respawning every cycle.
                respawn_count = _review_respawn_count(c)
                if respawn_count >= MAX_REVIEW_RESPAWNS:
                    if not parent.requires_approval:
                        parent.requires_approval = True
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} — review "
                            f"respawned {respawn_count}x without merging; escalating: "
                            f"marked parent {parent.id} requires_approval"
                        )
                        try:
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup blocked: PR #{pr_number} review respawned "
                                f"{respawn_count}x without merging — manual intervention required",
                            )
                        except Exception as _alert_err:
                            logger.warning(
                                f"[ROLLUP] Failed to create respawn-cap alert "
                                f"for parent {parent.id}: {_alert_err}"
                            )
                    continue

                c_payload = c.first_payload
                project = await session.get(Project, c.project_id)
                project_namespace = (
                    project.task_branch_namespace if project else "blueprint"
                )
                # Branch must respect the child's task type — agents use
                # `blueprint/refactor-*` for refactor tasks, etc. Hardcoding
                # `task-` here stalled review respawns on refactor children
                # (incident 2026-05-25, PRs #5779/#5812/#5815/#5819 all hit
                # the review cap because the clone targeted blueprint/task-*
                # while the agent had pushed blueprint/refactor-*).
                branch = (c.context or {}).get("branch") or _derive_task_branch(
                    c.id, c_payload.get("type"), project_namespace
                )
                base_branch = c_payload.get("base_branch", "main")
                original_prompt = c_payload.get("prompt", c.description or "")
                review_task, was_spawned = await _spawn_pipeline_task(
                    c,
                    "review",
                    session,
                    pr_number,
                    branch,
                    base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(c.id),
                )
                if review_task and review_task.id != c.id:
                    respawned += 1
                    if was_spawned:
                        c.context = {
                            **(c.context or {}),
                            "_review_respawn_count": respawn_count + 1,
                        }
                        session.add(c)
                    logger.warning(
                        f"[ROLLUP] Respawned review for stale child {str(c.id)[:8]} "
                        f"PR #{pr_number} → review {str(review_task.id)[:8]} "
                        f"(respawn {_review_respawn_count(c)}/{MAX_REVIEW_RESPAWNS})"
                    )
                elif review_task is None and c.requires_approval:
                    # Cap exhaustion: _spawn_pipeline_task sets requires_approval=True on
                    # the child. But the child is COMPLETED, so the EVALUATING approval-gate
                    # timeout never fires on it — the parent would otherwise wait the full
                    # evaluating_fail_hours (default 48h) hard timeout. Escalate: set
                    # requires_approval on the parent so the 2-hour approval gate fires,
                    # and raise a failure alert.
                    if not parent.requires_approval:
                        parent.requires_approval = True
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} hit review-fix cap — "
                            f"escalating: marked parent {parent.id} requires_approval"
                        )
                        try:
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup blocked: child PR #{pr_number} review-fix cap exhausted — manual intervention required",
                            )
                        except Exception as _alert_err:
                            logger.warning(
                                f"[ROLLUP] Failed to create cap-exhaustion alert for parent {parent.id}: {_alert_err}"
                            )

            titles = [c.title[:40] for c in unmerged]
            if respawned:
                parent.latest_status_message = f"Recovering: respawned {respawned} review(s) for unmerged PR(s) — {', '.join(titles)}"
            else:
                parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — {len(unmerged)} unmerged, {respawned} reviews respawned"
            )
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain.
        # Before giving up on failed children, attempt retry via _requeue_parent_task.
        # Cap-exhausted children are terminal: their PR was auto-closed and a
        # fresh retry would hit the same merge conflict that triggered the cap.
        # Exclude them from requeue; count them as failed-for-rollup-decision only.
        retriable_failed = [
            c
            for c in failed
            if (c.context or {}).get("failure_detail") != "review_cap_exhausted"
        ]
        if retriable_failed:
            MAX_PARENT_REQUEUES = 3
            parent_ctx = parent.context or {}
            requeue_count = parent_ctx.get("_requeue_count", 0)
            if requeue_count < MAX_PARENT_REQUEUES:
                # Retry: clone and re-queue failed children; parent returns to EVALUATING.
                # Next rollup cycle will produce a full rollup if retries succeed, or fall
                # through to partial rollup once the requeue limit is exhausted.
                from app.task_lifecycle import _requeue_parent_task

                logger.info(
                    f"[ROLLUP] Parent {parent.id} — {len(retriable_failed)} retriable failed, "
                    f"{len(failed) - len(retriable_failed)} cap-exhausted, "
                    f"{len(completed_merged)} succeeded. Requeuing failed children "
                    f"(attempt {requeue_count + 1}/{MAX_PARENT_REQUEUES}) before partial rollup."
                )
                await _requeue_parent_task(parent, session)
                return
            # Requeue limit exhausted — fall through to partial rollup or full failure.
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — requeue limit reached "
                f"({requeue_count}/{MAX_PARENT_REQUEUES}); proceeding with partial rollup or failure."
            )

        if completed_merged:
            # Some succeeded — proceed with partial rollup of what worked
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, {len(completed_merged)} succeeded. Rolling up successes."
            )
            all_done = True  # Let rollup proceed with merged children
            # Fall through to rollup logic below
        else:
            # Nothing succeeded — fail the parent immediately
            parent.status = TaskStatus.FAILED
            parent.latest_status_message = (
                f"{len(failed)} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            parent.context = _clear_rollup_transient_state(parent.context)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{len(failed)} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {len(failed)} children failed, marking parent FAILED"
            )
            return

    child_statuses = [c.status for c in children]

    # Edge case: all children cancelled — nothing to rollup, mark parent cancelled
    if children and cancelled_count == len(children):
        parent.status = TaskStatus.CANCELLED
        parent.context = _clear_rollup_transient_state(parent.context)
        parent.latest_status_message = f"All {len(children)} subtasks were cancelled"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.CANCELLED},
        )
        logger.info(f"[ROLLUP] Parent {parent.id} cancelled — all children cancelled")
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get(
            "research_branch"
        )
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        failed_count = len(failed_children)
        completed_count = len(children) - cancelled_count - failed_count

        # Guard: _verify_pr_merged may have flipped COMPLETED children to FAILED
        # (e.g. PR closed without merging) after the all_done loop evaluated their
        # status. If every non-cancelled child is now FAILED, there is nothing
        # to roll up — fail the parent instead of creating an empty rollup PR
        # that would trip the zero-diff guard and spuriously mark it COMPLETED.
        if completed_count == 0 and failed_count > 0:
            parent.status = TaskStatus.FAILED
            parent.context = _clear_rollup_transient_state(parent.context)
            parent.latest_status_message = (
                f"{failed_count} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed_children)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{failed_count} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {failed_count} non-cancelled "
                f"children failed after PR verification, marking parent FAILED"
            )
            return

        cap_exhausted_children = [
            c
            for c in failed_children
            if (c.context or {}).get("failure_detail") == "review_cap_exhausted"
        ]
        plain_failed_count = failed_count - len(cap_exhausted_children)
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note += f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"
        if plain_failed_count:
            rollup_note += f"\n\n*{plain_failed_count} subtask(s) failed and were excluded from this rollup.*"
        if cap_exhausted_children:
            rollup_note += (
                f"\n\n*{len(cap_exhausted_children)} subtask(s) hit the review-fix cap "
                f"(PRs auto-closed, manual review required) and were excluded from this rollup.*"
            )

        if staging_branch and target_branch and staging_branch != target_branch:
            gh_token = owner = repo = None
            project = await session.get(Project, parent.project_id)
            # Track the rollup PR number/url across the try block so the
            # exception handler can rescue a newly-created PR whose marker
            # commit failed (see orphan-PR rescue below).
            rollup_pr_number: int | None = None
            rollup_pr_url: str = ""
            # Initialize before the try so the rescue path in the except
            # handler can name the parent even if `get_github_token_for_task`
            # below raises before the canonical capture at line ~981. Without
            # these, the except handler's `parent_id_local` reference would
            # raise UnboundLocalError, masking the original exception.
            parent_id_local = parent.id
            project_id_local = parent.project_id
            try:
                gh_token, owner, repo = await get_github_token_for_task(
                    parent.id, session
                )

                # ── Phase B3 lock-window break ──
                # The next three GitHub calls (get_open_pull_requests,
                # get_branch_sha x2) are read-only verifications that can
                # collectively hang 5-30s during rate-limit. Release the
                # parent lock so concurrent heartbeats on sibling children
                # don't queue behind us.
                #
                # Audit Phase C4 (M-C1): with `expire_on_commit=False` (see
                # database.py), attributes on `parent` are NOT refreshed after
                # the commit below. Code between this release and the
                # `_relock_parent` call at the bottom of this block MUST treat
                # `parent` as snapshot-stale: only stable identifiers (id,
                # project_id) and the captured `pre_gh_snapshot` are safe to
                # read. Any mutable attribute (status, context, payloads,
                # latest_status_message) is potentially out-of-date and MUST
                # be re-read from the freshly-locked instance returned by
                # `_relock_parent`. The `parent_id_local` / `project_id_local`
                # captures below exist precisely so error logs and follow-up
                # GitHub calls don't accidentally trigger a stale read.
                parent_id_local = parent.id
                project_id_local = parent.project_id
                pre_gh_snapshot = await _release_parent_lock(parent, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests

                try:
                    open_prs = await get_open_pull_requests(
                        gh_token, owner, repo, staging_branch
                    )
                except HTTPException as api_err:
                    if api_err.status_code == 401:
                        from app.routers.agent import invalidate_github_token_cache

                        invalidate_github_token_cache(str(project_id_local))
                        logger.warning(
                            f"[ROLLUP] Auth failure listing open PRs for {owner}/{repo} — retrying with fresh token"
                        )
                        gh_token, owner, repo = await get_github_token_for_task(
                            parent_id_local, session, force_refresh=True
                        )
                        open_prs = await get_open_pull_requests(
                            gh_token, owner, repo, staging_branch
                        )
                    elif api_err.status_code in (403, 500, 502, 503):
                        logger.warning(
                            f"[ROLLUP] GitHub error {api_err.status_code} listing open PRs — deferring to next cycle"
                        )
                        return
                    else:
                        raise

                # Verify target branch exists before creating PR.
                # Distinguish 404 (actually deleted) from transient GitHub errors
                # (5xx, rate-limit) — only fail on confirmed 404.
                try:
                    await get_branch_sha(gh_token, owner, repo, target_branch)
                except HTTPException as branch_err:
                    if branch_err.status_code == 404:
                        raise Exception(
                            f"Target branch '{target_branch}' does not exist — "
                            f"cannot create rollup PR. This indicates a branch was "
                            f"deleted out of order."
                        ) from branch_err
                    # Transient GitHub error — defer to next evaluation cycle
                    logger.warning(
                        f"[ROLLUP] Transient GitHub error checking target branch "
                        f"'{target_branch}': {branch_err.detail} — will retry"
                    )
                    return

                # Verify staging (head) branch exists — if it was deleted
                # (e.g., by a child rollup that found an empty diff and cleaned
                # up, or by GitHub auto-delete after a prior PR merge), check
                # whether all children completed successfully. If so, the work
                # already landed on the target branch and we can complete.
                staging_branch_missing = False
                try:
                    await get_branch_sha(gh_token, owner, repo, staging_branch)
                except HTTPException as http_err:
                    if http_err.status_code == 404:
                        # Use `parent_id_local` (captured pre-release) instead
                        # of `parent.id` to make the snapshot-stale contract
                        # explicit: see audit-C4 (M-C1) note above.
                        logger.warning(
                            f"[ROLLUP] Staging branch '{staging_branch}' not found (404) for task {parent_id_local}"
                        )
                        staging_branch_missing = True
                    elif http_err.status_code in (401, 403):
                        logger.exception(
                            f"[ROLLUP] Auth error {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                    else:
                        logger.warning(
                            f"[ROLLUP] GitHub {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Error checking staging branch '{staging_branch}': {e} — deferring"
                    )
                    return

                # ── Phase B3: re-acquire the lock before applying mutations ──
                # The three GitHub reads above were performed without the lock.
                # Validate updated_at hasn't changed; if it has, another writer
                # mutated the parent during our window — defer to next cycle.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_gh_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                # Re-validate parent status — a peer may have moved it past EVALUATING.
                if parent.status != TaskStatus.EVALUATING:
                    return

                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    parent.latest_status_message = f"Rollup waiting: {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)}"
                    session.add(parent)
                    await session.commit()
                    logger.info(
                        f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting"
                    )
                    return

                if staging_branch_missing:
                    # Check if all children completed — if so, work already landed
                    all_children_ok = completed_count > 0 and failed_count == 0
                    if all_children_ok:
                        logger.info(
                            f"[ROLLUP] Staging branch '{staging_branch}' gone but all "
                            f"{completed_count} children completed — work already on {target_branch}"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = (
                            f"All {completed_count} subtasks completed with no actionable "
                            f"changes — research produced no new code to merge"
                        )
                        # Note on the direction that this cycle found nothing actionable
                        research_payload = parent.first_payload
                        dir_id = research_payload.get("current_direction_id")
                        if dir_id:
                            project = await session.get(Project, parent.project_id)
                            if project and project.directions:
                                directions = list(project.directions)
                                for d in directions:
                                    if d.get("id") == dir_id:
                                        notes = d.get("notes") or []
                                        notes.append(
                                            {
                                                "text": "Research cycle completed with no actionable changes — all findings already implemented or not applicable.",
                                                "added_at": _utcnow().isoformat(),
                                                "source": "rollup_empty_diff",
                                            }
                                        )
                                        d["notes"] = notes[-10:]  # Keep last 10
                                        break
                                project.directions = directions
                                flag_modified(project, "directions")
                                session.add(project)
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return
                    raise Exception(
                        f"Staging branch '{staging_branch}' does not exist and "
                        f"{failed_count} child(ren) failed — cannot create rollup PR"
                    )

                is_target_intermediate = target_branch.startswith(
                    "research/"
                ) or target_branch.startswith("plan/")

                # Resume from prior crash — skip PR creation if already done
                existing_pr = parent_ctx.get("rollup_pr_number")
                if existing_pr:
                    # ── Phase B3: release lock around get_pull_request ──
                    pre_existing_pr_snapshot = await _release_parent_lock(
                        parent, session
                    )
                    pr_state: dict | None = None
                    pr_state_err: Exception | None = None
                    try:
                        pr_state = await get_pull_request(
                            gh_token, owner, repo, existing_pr
                        )
                    except Exception as e:
                        pr_state_err = e
                    # Re-acquire and validate
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_existing_pr_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return
                    # Re-read existing_pr from fresh context (a peer may have cleared it)
                    existing_pr = parent_ctx.get("rollup_pr_number")
                    if pr_state_err is not None:
                        logger.warning(
                            f"[ROLLUP] Could not verify PR #{existing_pr} state: {pr_state_err} — proceeding with existing marker"
                        )
                    elif existing_pr is not None and pr_state is not None:
                        if pr_state.get("merged"):
                            logger.info(
                                f"[ROLLUP] PR #{existing_pr} already merged — marking COMPLETED"
                            )
                            _integration_br = _get_project_integration_branch(project)
                            _landed = target_branch == _integration_br
                            parent.status = TaskStatus.COMPLETED
                            parent.context = {
                                **_clear_rollup_transient_state(parent_ctx),
                                **({"pr_merged": True} if _landed else {}),
                            }
                            parent.latest_status_message = (
                                f"Rollup PR #{existing_pr} already merged"
                            )
                            session.add(parent)
                            await session.commit()
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(
                                str(parent.project_id),
                                "task_updated",
                                {
                                    "task_id": str(parent.id),
                                    "status": TaskStatus.COMPLETED,
                                },
                            )
                            return
                        if pr_state.get("state") == "closed":
                            logger.warning(
                                f"[ROLLUP] PR #{existing_pr} was closed without merge — clearing marker, will retry"
                            )
                            parent.context = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k not in ("rollup_pr_number", "rollup_pr_url")
                            }
                            parent_ctx = parent.context
                            existing_pr = None
                            session.add(parent)
                            await session.commit()

                if existing_pr:
                    rollup_pr_number = existing_pr
                    rollup_pr_url = parent_ctx.get("rollup_pr_url", "")
                    logger.info(
                        f"[ROLLUP] Resuming with existing PR #{rollup_pr_number}"
                    )
                else:
                    pr_title = f"[Blueprint] Rollup: {parent.title}"
                    _dir_id = parent.first_payload.get(
                        "current_direction_id"
                    ) or parent_ctx.get("direction_id")
                    _dir_title = parent_ctx.get("direction_title") or parent.title
                    _completed_children = [
                        c for c in children if c.status == TaskStatus.COMPLETED
                    ]
                    pr_body = _build_rollup_pr_body(
                        completed_count,
                        staging_branch,
                        rollup_note,
                        direction_id=str(_dir_id) if _dir_id else None,
                        direction_title=_dir_title,
                        children=_completed_children,
                    )

                    # ── Phase B3: release lock around create_pull_request ──
                    # PR creation is the longest-running call in this function:
                    # 5-15s on the happy path, minutes during rate-limit. The
                    # rollup_pr_number/rollup_pr_url written below are the
                    # marker for crash-recovery, so the post-create re-acquire
                    # MUST validate and persist before any other writer can
                    # observe an empty marker.
                    project_id_str = str(parent.project_id)
                    pre_create_snapshot = await _release_parent_lock(parent, session)

                    pr_result = await create_pull_request(
                        gh_token,
                        owner,
                        repo,
                        title=pr_title,
                        head=staging_branch,
                        base=target_branch,
                        body=pr_body,
                        project_id=project_id_str,
                    )

                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_create_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent writer mutated the parent during PR creation.
                        # The orphan-PR rescue path in the except handler will
                        # persist the marker via a fresh session if we raise.
                        # Re-fetch without lock and bail safely.
                        logger.info(
                            f"[ROLLUP] Parent {parent_id_local} mutated during create_pull_request — "
                            f"orphan-PR rescue will persist marker"
                        )
                        rollup_pr_number = pr_result.get("number")
                        rollup_pr_url = pr_result.get("html_url", "")
                        # Trigger orphan-PR rescue path. The sentinel class lets
                        # the outer handler short-circuit past the post-rescue
                        # parent.context manipulation that would NPE here.
                        raise _ParentMutatedDuringPRCreation(
                            f"Parent {parent_id_local} mutated during create_pull_request — deferring"
                        )
                    parent_ctx = parent.context or {}

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(
                            f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent.id}: {e}"
                            )
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return

                    rollup_pr_url = pr_result.get("html_url", "")
                    rollup_pr_number = pr_result.get("number")
                    logger.info(
                        f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}"
                    )

                    # Commit marker immediately — if we crash after this point,
                    # restart will find the marker and skip PR creation
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_number": rollup_pr_number,
                        "rollup_pr_url": rollup_pr_url,
                    }
                    parent_ctx = parent.context  # Update local ref
                    session.add(parent)
                    await session.commit()

                # Re-acquire lock on parent for merge phase (marker commit released it).
                # skip_locked: rollup PR marker is already persisted, so if a concurrent
                # sibling holds the lock we defer safely — reaper sweep will retry within 15 min.
                parent_id = parent.id
                parent = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == parent_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if parent is None:
                    logger.info(
                        f"[ROLLUP] Skipped merge phase for parent {parent_id} — lock held, "
                        "reaper sweep will retry"
                    )
                    return
                parent_ctx = parent.context or {}

                # Guard: concurrent sibling may have created a different rollup PR in the gap
                if parent_ctx.get("rollup_pr_number") != rollup_pr_number:
                    logger.info(
                        f"[ROLLUP] Concurrent sibling created rollup PR for parent {parent.id} — yielding"
                    )
                    return

                # Check if any child task was high-risk — if so, require approval on the final merge.
                # Refactor tasks are structural (move code, no behavior change) and should not
                # trigger the high-risk gate even when classified as risk='high' by plan agents.
                # Gate is controlled by runtime setting (default True) — disable for repos
                # that don't need human-in-the-loop approval.
                has_high_risk_child = any(
                    (c.context or {}).get("risk") == "high"
                    for c in children
                    if c.status == TaskStatus.COMPLETED
                    and (c.first_payload or {}).get("type")
                    != "refactor"  # refactors are structural, not behavioral risk
                )
                if (
                    has_high_risk_child
                    and not is_target_intermediate
                    and _runtime_config.get("require_approval_gates", True)
                ):
                    parent.requires_approval = True
                    parent.latest_status_message = (
                        f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting approval "
                        f"(contains high-risk changes)"
                    )
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_url": rollup_pr_url,
                        "rollup_pr_number": rollup_pr_number,
                    }
                    session.add(parent)
                    await _dispatch_rollup_approval_alert(
                        parent,
                        session,
                        f"Rollup PR #{rollup_pr_number} awaiting approval — contains high-risk changes",
                        pr_number=rollup_pr_number,
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": parent.status},
                    )
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has high-risk children — requires approval before merge to {target_branch}"
                    )
                    return

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                # ── Phase B3: release lock around diff-size check + close on zero-diff ──
                # get_pull_request can hang 5-30s; close_pull_request + delete_branch
                # are also synchronous I/O. Releasing the lock around this cluster
                # lets concurrent heartbeats on sibling children proceed.
                project_id_str = str(parent.project_id)
                pre_diff_snapshot = await _release_parent_lock(parent, session)
                try:
                    pr_detail = await get_pull_request(
                        gh_token,
                        owner,
                        repo,
                        rollup_pr_number,
                        project_id=project_id_str,
                    )
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get(
                        "deletions", 0
                    )
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} diff size for task {parent_id_local}: {e}"
                    )
                    lines_changed = 999  # assume large if we can't check
                    pr_detail = None
                head_sha = (pr_detail.get("head") or {}).get("sha") if pr_detail else None

                # Zero-diff guard: squash-merged commits can produce a PR with commits
                # but no file changes. Close before spawning review/fix VMs.
                if pr_detail is not None and lines_changed == 0:
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has zero lines changed — closing and marking COMPLETED"
                    )
                    try:
                        await close_pull_request(
                            gh_token,
                            owner,
                            repo,
                            rollup_pr_number,
                            project_id=project_id_str,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to close zero-diff PR #{rollup_pr_number}: {e}"
                        )
                    try:
                        await delete_branch(gh_token, owner, repo, staging_branch)
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent_id_local}: {e}"
                        )
                    # Re-acquire to apply mutation
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_diff_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent.status = TaskStatus.COMPLETED
                    parent.latest_status_message = f"✓ All {completed_count} subtasks completed — rollup PR #{rollup_pr_number} had no file changes (already on {target_branch})"
                    _cleared = _clear_rollup_transient_state(parent.context)
                    parent.context = {
                        k: v
                        for k, v in _cleared.items()
                        if k not in ("rollup_pr_number", "rollup_pr_url")
                    }
                    session.add(parent)
                    await session.commit()
                    await _cleanup_branches(parent, session)
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                    )
                    return

                # Non-zero-diff path: re-acquire for downstream mutations.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_diff_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                if parent.status != TaskStatus.EVALUATING:
                    return

                # If diff size is unverifiable (metadata fetch failed), defer the
                # review-spawn decision to the next rollup cycle rather than
                # assuming large and burning VM time on a PR whose size is
                # unknown. Previously unverifiable → lines_changed=999 → review
                # spawned on potentially-empty PRs (2026-04-17 audit: ~15
                # VM-hours/day wasted on the empty-rollup class).
                if pr_detail is None:
                    logger.info(
                        f"[ROLLUP] Deferring review for PR #{rollup_pr_number} — could not verify "
                        f"diff size; will retry next cycle"
                    )
                    parent.latest_status_message = f"Rollup PR #{rollup_pr_number} — diff size unverified, retrying next cycle"
                    session.add(parent)
                    await session.commit()
                    return

                is_small_rollup = lines_changed <= 100
                logger.info(
                    f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}"
                )
                if is_target_intermediate and is_small_rollup:
                    # ── Phase B3: release lock around security scan + comment + merge ──
                    # The scan inspects PR diffs (multiple GH API calls), the comment
                    # is a write, and the merge is the slowest call of all. Holding
                    # the lock across this cluster previously starved sibling-child
                    # heartbeats during minutes-long rate-limit windows.
                    pre_merge_snapshot = await _release_parent_lock(parent, session)
                    rollup_scan: dict | None = None
                    rollup_scan_err: Exception | None = None
                    try:
                        rollup_scan = await scan_pr_for_injection(
                            gh_token, owner, repo, rollup_pr_number
                        )
                        logger.info(f"[SECURITY] Rollup scan: {rollup_scan['summary']}")
                    except Exception as e:
                        rollup_scan_err = e
                        logger.error(
                            f"[SECURITY] Rollup scan failed for PR #{rollup_pr_number}, blocking: {e}",
                            exc_info=True,
                        )

                    # Validate the combined Alembic migration chain on the rollup
                    # branch before auto-merge. Two child PRs can each add a
                    # migration with the same revision off the same head; each
                    # passes its own CI in isolation but the merged rollup then
                    # has duplicate revisions / multiple heads, breaking
                    # `alembic upgrade head` on the integration branch for every
                    # subsequent task. Fail-safe: fetch/parse failure does not
                    # auto-merge. Runs in this unlocked window (GitHub I/O).
                    alembic_status, alembic_reason = await _validate_rollup_migration_chain(
                        gh_token, owner, repo, rollup_pr_number
                    )

                    # Re-acquire to apply mutations / dispatch alerts.
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return

                    if rollup_scan_err is not None:
                        parent.requires_approval = True
                        parent.latest_status_message = f"Rollup security scan failed — requires manual approval: {type(rollup_scan_err).__name__}: {rollup_scan_err!r}"[
                            :500
                        ]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} security scan failed — requires manual approval: {type(rollup_scan_err).__name__}",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        return

                    if rollup_scan["blocked"]:
                        parent.requires_approval = True
                        parent.latest_status_message = (
                            f"Rollup security scan blocked: {rollup_scan['summary']}"
                        )
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        # Persist + commit BEFORE the comment (releases lock for I/O).
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} blocked by security scan: {rollup_scan['summary']}",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        # Comment after the lock is released — it's an external side-effect.
                        if rollup_scan["findings"]:
                            try:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    rollup_pr_number,
                                    rollup_scan["report"],
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[ROLLUP] Failed to post scan-block comment on PR #{rollup_pr_number}: {e}"
                                )
                        return
                    if rollup_scan["findings"]:
                        # Findings but not blocked — post comment outside lock window
                        # is fine; we don't mutate the parent for this case.
                        try:
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                rollup_scan["report"],
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to post scan comment on PR #{rollup_pr_number}: {e}"
                            )

                    # Alembic combined-chain guard (parent is locked here).
                    if alembic_status == "defer":
                        logger.info(
                            f"[ROLLUP] Deferring auto-merge for PR #{rollup_pr_number} — "
                            f"could not validate Alembic chain: {alembic_reason}"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — Alembic chain unverified, "
                            f"retrying next cycle"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        return
                    if alembic_status == "block":
                        parent.requires_approval = True
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        _alembic_fix_prompt = (
                            f"Rollup PR #{rollup_pr_number} merging {staging_branch} into "
                            f"{target_branch} has an invalid combined Alembic migration "
                            f"chain: {alembic_reason}. Two sibling PRs added migrations "
                            f"that collide. Do NOT modify any already-landed migration "
                            f"(applied history is immutable); instead rename and re-chain "
                            f"your migration to the next free NNNN revision with "
                            f"down_revision pointing at the current head, then bump "
                            f"EXPECTED_SCHEMA_REVISION in backend/app/main.py to match."
                        )
                        await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=_alembic_fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"chain ({alembic_reason}) — held for approval, spawned re-chain fix"
                        )
                        return

                    # Release lock around the actual merge call (slowest of all).
                    pre_actual_merge_snapshot = await _release_parent_lock(parent, session)
                    if head_sha:
                        try:
                            target_tip_sha = await get_branch_sha(
                                gh_token, owner, repo, target_branch,
                            )
                        except Exception as tip_err:
                            logger.warning(f"[ROLLUP] Failed to fetch target tip for PR #{rollup_pr_number}: {tip_err}")
                            return
                        if target_tip_sha != head_sha:
                            _upd = await update_branch_with_sha_retry(
                                gh_token, owner, repo, rollup_pr_number,
                                expected_head_sha=head_sha,
                                project_id=project_id_str,
                            )
                            parent = await _relock_parent(
                                session, parent_id_local, pre_actual_merge_snapshot,
                                parent_in_session=parent,
                            )
                            if parent is None:
                                return
                            parent_ctx = parent.context or {}
                            if _upd.get("updated"):
                                _new_ctx = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} was behind {target_branch} — updated branch, "
                                    f"awaiting fresh CI; next rollup sweep will re-attempt merge"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch succeeded, skipped merge"
                                )
                                return
                            _behind_retries = parent_ctx.get("_behind_update_retries", 0) + 1
                            _behind_budget = _runtime_config.get("rollup_behind_update_retries", 3)
                            _upd_msg = _upd.get("message") or "unknown"
                            if _behind_retries < _behind_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_behind_update_retries": _behind_retries,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring merge"
                                )
                                return
                            await _maybe_create_failure_alert(
                                parent, session,
                                f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} consecutive sweeps ({_upd_msg}); spawning fix",
                            )
                            _ctx_cleared = {k: v for k, v in parent_ctx.items() if k != "_behind_update_retries"}
                            parent.context = {
                                **_ctx_cleared,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch exhausted "
                                f"after {_behind_retries} sweeps; spawning fix"
                            )
                            session.add(parent)
                            await session.commit()
                            _fix_prompt = (
                                f"Rollup PR #{rollup_pr_number} merging {staging_branch} into {target_branch} "
                                f"is behind the target branch and update_branch failed {_behind_retries} consecutive "
                                f"sweeps ({_upd_msg}). Rebase manually onto origin/{target_branch} and force-push."
                            )
                            await _spawn_pipeline_task(
                                parent, "fix", session, rollup_pr_number, staging_branch, target_branch,
                                fix_prompt=_fix_prompt,
                                original_task_id=str(parent.id),
                            )
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch exhausted "
                                f"({_behind_retries} sweeps): {_upd_msg} — spawned fix"
                            )
                            return
                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number, project_id=str(parent.project_id))
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_actual_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent mutation; merge call already completed externally,
                        # next sweep will pick up the merged state.
                        return
                    parent_ctx = parent.context or {}
                    if merge_result.get("merged"):
                        _integration_br = _get_project_integration_branch(project)
                        _landed = target_branch == _integration_br
                        parent.status = TaskStatus.COMPLETED
                        parent.context = {
                            **_clear_rollup_transient_state(parent_ctx),
                            **({"pr_merged": True} if _landed else {}),
                            "rollup_pr_url": rollup_pr_url,
                        }
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR auto-merged"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as del_err:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch {staging_branch}: {del_err}"
                            )
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}")
                        try:
                            from app.notification_dispatch import post_pr_merged_message

                            _direction_id = (parent.context or {}).get("direction_id")
                            _direction_text = "unknown direction"
                            if _direction_id:
                                _proj = await session.get(Project, parent.project_id)
                                if _proj:
                                    for _d in _proj.directions or []:
                                        if str(_d.get("id", "")) == str(_direction_id):
                                            _direction_text = _d.get(
                                                "text", "unknown direction"
                                            )
                                            break
                            await post_pr_merged_message(
                                session,
                                parent.project_id,
                                rollup_pr_number,
                                _direction_text,
                                completed_count,
                                parent.id,
                            )
                        except Exception as _e:
                            logger.warning(
                                f"[NOTIFY] Failed to post PR merged message: {_e}"
                            )
                        try:
                            from app.github import get_pull_request_diff
                            from app.viewport_coverage import (
                                update_viewport_coverage_snapshot_from_diff,
                            )

                            _snap_proj = await session.get(Project, parent.project_id)
                            if _snap_proj is not None:
                                _diff_text = await get_pull_request_diff(
                                    gh_token, owner, repo, rollup_pr_number
                                )
                                await update_viewport_coverage_snapshot_from_diff(
                                    session, _snap_proj, rollup_pr_number, _diff_text
                                )
                        except Exception as _snap_e:
                            logger.warning(
                                f"[ROLLUP] Failed to update viewport_coverage_snapshot: {_snap_e}"
                            )
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        # Before spawning a fix task, check if the PR is merely
                        # behind its base (common when concurrent rollups land on
                        # the same target). If so, update-branch via GitHub API
                        # and let the next rollup sweep re-attempt the merge.
                        _pr_refetch_failed = False
                        try:
                            _pr_detail = await get_pull_request(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                project_id=str(parent.project_id),
                            )
                        except Exception as _pr_exc:
                            _pr_detail = {}
                            _pr_refetch_failed = True
                            logger.debug(
                                f"[ROLLUP] Could not refetch PR #{rollup_pr_number} for behind-check: {_pr_exc}"
                            )
                        # PR refetch retry budget: transient GitHub API errors (500,
                        # 503, network timeouts) shouldn't burn a fix VM. Defer the
                        # sweep on early failures; only fall through to fix-spawn
                        # once consecutive failures exhaust the budget.
                        if _pr_refetch_failed:
                            _refetch_failures = (
                                parent_ctx.get("_refetch_failure_count", 0) + 1
                            )
                            _refetch_budget = _runtime_config.get(
                                "rollup_refetch_failure_retries", 3
                            )
                            if _refetch_failures < _refetch_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_refetch_failure_count": _refetch_failures,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} refetch failed after merge attempt "
                                    f"— deferring to next sweep ({_refetch_failures}/{_refetch_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} refetch failed after failed merge "
                                    f"(attempt {_refetch_failures}/{_refetch_budget}) — deferring fix spawn"
                                )
                                return
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch failed {_refetch_failures} consecutive "
                                f"sweeps — falling through to fix spawn"
                            )
                        elif "_refetch_failure_count" in parent_ctx:
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                        # Classify mergeable_state before assuming this is a real
                        # conflict. Only 'dirty' (and 'behind' as fall-through if
                        # update_branch fails) warrant a fix VM. 'blocked' and
                        # 'unstable' resolve when checks/reviews complete; 'unknown'
                        # means GitHub is still computing — all should be retried by
                        # the next rollup sweep instead of burning a fix cycle.
                        _ms = (_pr_detail.get("mergeable_state") or "").lower()
                        # Budget-exhausted refetch-failed: _pr_detail is {} so _ms="".
                        # We already exhausted the retry budget above; treat it as an
                        # un-classifiable failed merge and fall through to fix-spawn
                        # instead of entering the deferral path (which would loop
                        # forever — next sweep would also have an empty _pr_detail
                        # if the API is persistently failing).
                        if _pr_refetch_failed:
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch budget exhausted "
                                f"— skipping state-classification, falling through to fix-spawn"
                            )
                        elif _ms == "behind":
                            _upd = await update_branch_with_sha_retry(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                expected_head_sha=_pr_detail.get("head", {}).get("sha")
                                or None,
                                project_id=str(parent.project_id),
                            )
                            if _upd.get("updated"):
                                # Reset the behind-retry counter — update succeeded, so any
                                # prior transient failures were just that, transient.
                                _new_ctx = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                }
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} was behind {target_branch} — updated branch, "
                                    f"awaiting fresh CI; next rollup sweep will re-attempt merge"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch succeeded, skipped fix spawn"
                                )
                                return
                            # update_branch failed — this is a transient GitHub API error,
                            # not a real merge conflict. Defer like the non-dirty path
                            # (counter + status message), retry on the next sweep, and
                            # only fall through to fix spawn once the retry budget is
                            # exhausted. Prevents premature fix VMs from wasting a
                            # review/fix cycle on a PR that just needs another API call.
                            _behind_retries = (
                                parent_ctx.get("_behind_update_retries", 0) + 1
                            )
                            _behind_budget = _runtime_config.get(
                                "rollup_behind_update_retries", 3
                            )
                            _upd_msg = _upd.get("message") or "unknown"
                            if _behind_retries < _behind_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_behind_update_retries": _behind_retries,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                                )
                                return
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn",
                            )
                            # Clear the counter so a future behind state starts fresh.
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_behind_update_retries"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                            )
                        elif _ms != "dirty":
                            # Non-conflict states: known stalls (blocked/unstable/unknown)
                            # or any unrecognized value GitHub may introduce. All defer
                            # to the next rollup sweep instead of spawning a fix. Track
                            # consecutive deferrals so silent stalls surface with an
                            # alert (and escalate to approval) instead of waiting out
                            # the 6h hard timeout. Refetch-failed+budget-exhausted cases
                            # are handled above and never reach this branch.
                            _ms_key = _ms or "empty"
                            _ms_display = _ms or "missing (field absent)"
                            _is_unrecognized = _ms_key not in (
                                "blocked",
                                "unstable",
                                "unknown",
                                "empty",
                            )
                            # For blocked/unstable, ask GitHub to auto-merge once gates
                            # clear — mirrors the task_lifecycle auto-merge fallback so a
                            # passing rollup PR lands without waiting on the reaper's
                            # 15-minute stale-parent sweep. Deferral counters below still
                            # run so a genuinely stuck PR still escalates to approval.
                            if _ms_key in ("blocked", "unstable"):
                                try:
                                    from app.github import enable_auto_merge

                                    am_result = await enable_auto_merge(
                                        gh_token,
                                        owner,
                                        repo,
                                        rollup_pr_number,
                                        project_id=str(parent.project_id),
                                    )
                                    if am_result.get("enabled"):
                                        logger.info(
                                            f"[ROLLUP] PR #{rollup_pr_number} mergeable_state={_ms_key} "
                                            f"— enabled GitHub auto-merge"
                                        )
                                    else:
                                        logger.info(
                                            f"[ROLLUP] PR #{rollup_pr_number} auto-merge enablement "
                                            f"declined: {am_result.get('message')}"
                                        )
                                except Exception as _am_exc:
                                    logger.warning(
                                        f"[ROLLUP] Auto-merge enablement failed for PR "
                                        f"#{rollup_pr_number}: {_am_exc}"
                                    )
                            prev_state = parent_ctx.get("_merge_deferred_state")
                            deferred_count = (
                                (parent_ctx.get("_merge_deferred_count", 0) + 1)
                                if prev_state == _ms_key
                                else 1
                            )
                            # Total-deferral counter: increments on every deferral regardless
                            # of state change. Prevents a flapping PR (blocked↔unstable↔…) from
                            # evading the per-state threshold forever.
                            total_deferrals = (
                                parent_ctx.get("_merge_total_deferrals", 0) + 1
                            )
                            now = _utcnow()
                            first_deferred_at = (
                                parent_ctx.get("_merge_first_deferred_at")
                                or now.isoformat()
                            )
                            try:
                                from datetime import datetime as _dt

                                _first_dt = _dt.fromisoformat(first_deferred_at)
                                elapsed_minutes = (
                                    now - _first_dt
                                ).total_seconds() / 60.0
                            except (TypeError, ValueError):
                                elapsed_minutes = 0.0
                            # 'unknown' / empty = GitHub stuck computing mergeability or refetch
                            # failing; unrecognized = GitHub returned a new state we don't know
                            # how to interpret. All escalate faster than 'blocked'/'unstable'
                            # which legitimately wait for checks/reviews.
                            _fast_escalate = _ms_key not in ("blocked", "unstable")
                            alert_threshold = _runtime_config.get(
                                "rollup_merge_deferred_alert_unknown"
                                if _fast_escalate
                                else "rollup_merge_deferred_alert",
                                5 if _fast_escalate else 10,
                            )
                            approval_threshold = _runtime_config.get(
                                "rollup_merge_deferred_approval", 30
                            )
                            total_alert_threshold = alert_threshold * 2
                            total_approval_threshold = approval_threshold * 2
                            time_alert_minutes = _runtime_config.get(
                                "rollup_merge_deferred_time_alert_minutes", 30
                            )
                            time_approval_minutes = _runtime_config.get(
                                "rollup_merge_deferred_time_approval_minutes", 60
                            )
                            # Per-state alert latch: tracks which states have already
                            # fired an alert so a flapping PR (blocked→unstable→blocked)
                            # doesn't re-fire the per-state alert each time a state
                            # re-crosses its threshold. A new state triggers a fresh
                            # alert; a state that's already alerted stays quiet.
                            alerted_states = set(
                                parent_ctx.get("_merge_alerted_states") or []
                            )
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                                "_merge_deferred_count": deferred_count,
                                "_merge_deferred_state": _ms_key,
                                "_merge_total_deferrals": total_deferrals,
                                "_merge_first_deferred_at": first_deferred_at,
                                "_merge_alerted_states": sorted(alerted_states),
                            }
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} merge deferred (mergeable_state={_ms_display}, "
                                f"deferral {deferred_count}/total {total_deferrals}) — next rollup sweep will re-attempt"
                            )
                            # Per-state alert: fires once per state when its counter
                            # crosses the alert threshold. Use `>= threshold AND not
                            # already alerted` so a state that crosses the threshold
                            # in a previous sweep doesn't re-fire this sweep. Alert/
                            # approval dispatch is wrapped in try/except so dispatch
                            # failures don't prevent the counter-persisting commit —
                            # otherwise counters reset each sweep and thresholds never fire.
                            if (
                                deferred_count >= alert_threshold
                                and _ms_key not in alerted_states
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                        f"for {deferred_count} consecutive sweeps",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create per-state deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                alerted_states.add(_ms_key)
                                parent.context = {
                                    **parent.context,
                                    "_merge_alerted_states": sorted(alerted_states),
                                }
                            # Total-count alert: fires once when total crosses threshold,
                            # catching state-flapping PRs that evade the per-state counter.
                            if (
                                total_deferrals >= total_alert_threshold
                                and not parent_ctx.get("_merge_total_alerted")
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} merge deferred {total_deferrals} times across "
                                        f"states (flapping) — manual investigation required",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create total-deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                parent.context = {
                                    **parent.context,
                                    "_merge_total_alerted": True,
                                }
                            # Time-based alert: fires once after total deferral time crosses threshold
                            if (
                                elapsed_minutes >= time_alert_minutes
                                and not parent_ctx.get("_merge_time_alerted")
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} merge has been deferred for "
                                        f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create time-based deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                parent.context = {
                                    **parent.context,
                                    "_merge_time_alerted": True,
                                }
                            # Approval gate: any of per-state, total, or time thresholds can fire it.
                            # Only one approval dispatch — the first condition wins.
                            if not parent.requires_approval:
                                if deferred_count >= approval_threshold:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                            f"for {deferred_count} sweeps — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch per-state approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                elif total_deferrals >= total_approval_threshold:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} deferred {total_deferrals} times across "
                                            f"states — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch total-deferral approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                elif elapsed_minutes >= time_approval_minutes:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} has been deferred for "
                                            f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch time-based approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                            session.add(parent)
                            await session.commit()
                            _log = logger.warning if _is_unrecognized else logger.info
                            _log(
                                f"[ROLLUP] PR #{rollup_pr_number} merge skipped: "
                                f"{'unrecognized ' if _is_unrecognized else ''}mergeable_state={_ms_display} "
                                f"(deferral {deferred_count}/total {total_deferrals}/{elapsed_minutes:.0f}min; "
                                f"alert@{alert_threshold}/{total_alert_threshold}/{time_alert_minutes}min, "
                                f"approval@{approval_threshold}/{total_approval_threshold}/{time_approval_minutes}min) "
                                f"— deferring to next rollup sweep, no fix spawn"
                            )
                            return
                        # Observability: check if concurrent rollups may explain this conflict
                        try:
                            # R4-W2 (M3): ``has_key`` uses the GIN ``?``
                            # operator and matches ``ix_task_context_gin``;
                            # the prior ``as_string().is_not(None)`` form
                            # bypassed the index.
                            recent_rollups = (
                                await session.execute(
                                    select(_func.count())
                                    .select_from(Task)
                                    .where(
                                        Task.status == TaskStatus.COMPLETED,
                                        Task.id != parent.id,
                                        Task.updated_at
                                        >= _utcnow() - timedelta(hours=1),
                                        Task.context.has_key("rollup_pr_url"),
                                    )
                                )
                            ).scalar() or 0
                            if recent_rollups > 0:
                                logger.warning(
                                    f"[ROLLUP] Conflict on PR #{rollup_pr_number} may be stale-window: "
                                    f"{recent_rollups} other rollup(s) merged to {target_branch} in the last hour"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to query concurrent rollup count for PR #{rollup_pr_number} (task {parent.id}): {e}"
                            )
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_is_rollup_review": True,
                        }
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, rollup_pr_number
                            )
                            files_list = "\n".join(
                                f"  - {f['filename']} ({f['status']})" for f in pr_files
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} file list for task {parent.id}: {e}"
                            )
                            files_list = "  (could not fetch file list)"
                            pr_files = []
                        has_migrations = any(
                            (f.get("filename") or "").startswith(
                                "backend/alembic/versions/"
                            )
                            for f in pr_files
                        )
                        migration_guidance = ""
                        if has_migrations:
                            migration_guidance = (
                                "\n\nMigration chain guidance:\n"
                                "This rollup contains database migrations from multiple subtasks. After resolving conflicts:\n"
                                "- The migration chain in backend/alembic/versions/ must be linear: each migration's `down_revision` "
                                "must point to the revision of the previous migration in the chain.\n"
                                "- All revision numbers (e.g., 0039, 0040, ...) must be unique and sequential — no gaps, no duplicates.\n"
                                "- Renumber/rewrite migration files as needed so the chain is contiguous from the latest pre-rollup revision.\n"
                                "- Update `EXPECTED_SCHEMA_REVISION` in backend/app/main.py to match the final (highest) migration's revision."
                            )
                        parent_goal = (parent.description or parent.title or "")[:500]
                        fix_prompt = (
                            f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                            f"Error: {merge_msg}\n\n"
                            f"Rollup goal: {parent_goal}\n\n"
                            f"Files in this PR:\n{files_list}\n\n"
                            f"Steps to resolve:\n"
                            f"1. Run: git fetch origin {target_branch}\n"
                            f"2. Run: git rebase origin/{target_branch}\n"
                            f"3. Resolve any conflicts — {target_branch} is the source of truth. Keep its refactors and restructuring.\n"
                            f"   Re-apply only the changes that serve the rollup goal (described above) on top of {target_branch}'s current state.\n"
                            f"   If files were heavily rewritten on {target_branch}, adapt the rollup changes to the new structure.\n"
                            f"4. Run: git rebase --continue after resolving each file"
                            f"{migration_guidance}"
                        )
                        fix_task, _ = await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        if fix_task:
                            logger.error(
                                f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}"
                            )
                        else:
                            from app.review_pipeline import (
                                SPAWN_REASON_KEY,
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                                SPAWN_REASON_ALREADY_MERGED,
                            )

                            spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                            if spawn_reason in (
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                            ):
                                # Another review/fix is in flight on this PR — leave
                                # parent EVALUATING; next rollup sweep retries once the
                                # in-flight task finishes. latest_status_message was
                                # already set by _spawn_pipeline_task.
                                logger.info(
                                    f"[ROLLUP] Fix spawn deferred for rollup PR #{rollup_pr_number} "
                                    f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                                )
                            elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                                # PR merged while we were preparing the fix — no-op.
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no fix needed"
                                )
                            else:
                                # cap / stuck / unknown — terminal.
                                parent.status = TaskStatus.FAILED
                                parent.context = _clear_rollup_transient_state(
                                    parent.context
                                )
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned "
                                    f"({spawn_reason or 'unknown'})"
                                )
                                from app.failure_classification import (
                                    classify_failure,
                                    persist_failure_category,
                                )

                                _fc = classify_failure(parent)
                                persist_failure_category(parent, _fc)
                                _record_rollup_failure_signals(parent, _fc)
                                await _maybe_create_failure_alert(
                                    parent,
                                    session,
                                    f"Rollup PR #{rollup_pr_number} merge blocked — fix iteration cap exhausted, manual intervention required",
                                )
                                logger.warning(
                                    f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                                )
                            # NOTE: Do NOT delete staging_branch here — it contains
                            # all merged child work and requeueing should be possible.
                else:
                    # Rollup-to-main / large rollup: review and CI run in parallel.
                    # ci_waiting=True only tells the orchestrator to poll CI; it is
                    # NOT permission to merge. Both ci_passed and review_approved
                    # must be set on this review_loop before the merge gate fires
                    # (see app.ci_completion._review_approved + the rollup gate
                    # handoff in app.task_lifecycle's verdict==APPROVE branch).
                    # Do NOT pre-seed review_approved here — the spawned review task
                    # writes it back to this parent's review_loop on APPROVE.
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_url": rollup_pr_url,
                        "rollup_pr_number": rollup_pr_number,
                        "_is_rollup_review": True,
                        "review_loop": {
                            "ci_waiting": True,
                            "pr_number": rollup_pr_number,
                            "branch": staging_branch,
                            "base_branch": target_branch,
                            "original_task_id": str(parent.id),
                            "ci_wait_started": _utcnow().isoformat(),
                        },
                    }
                    review_task, _ = await _spawn_pipeline_task(
                        parent,
                        "review",
                        session,
                        rollup_pr_number,
                        staging_branch,
                        target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id),
                    )
                    if review_task:
                        logger.info(
                            f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}"
                        )
                    else:
                        from app.review_pipeline import (
                            SPAWN_REASON_KEY,
                            SPAWN_REASON_DEFERRED,
                            SPAWN_REASON_DEDUP,
                            SPAWN_REASON_ALREADY_MERGED,
                        )

                        spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                        if spawn_reason in (SPAWN_REASON_DEFERRED, SPAWN_REASON_DEDUP):
                            # An active review/fix already owns this PR — leave parent
                            # EVALUATING; next rollup sweep retries. latest_status_message
                            # was already set by _spawn_pipeline_task.
                            logger.info(
                                f"[ROLLUP] Review spawn deferred for rollup PR #{rollup_pr_number} "
                                f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                            )
                        elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                            logger.info(
                                f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no review needed"
                            )
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.context = _clear_rollup_transient_state(
                                parent.context
                            )
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} created but review not spawned "
                                f"({spawn_reason or 'unknown'})"
                            )
                            from app.failure_classification import (
                                classify_failure,
                                persist_failure_category,
                            )

                            _fc = classify_failure(parent)
                            persist_failure_category(parent, _fc)
                            _record_rollup_failure_signals(parent, _fc)
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} created but review task could not be spawned — iteration cap exhausted",
                            )
                            logger.warning(
                                f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                            )
                        # NOTE: Do NOT delete staging_branch here — it contains
                        # all merged child work and requeueing should be possible.
            except Exception as e:
                # Orphan PR rescue: if we created a rollup PR during this try
                # but the marker commit failed (connection loss, session invalid),
                # the local variable still holds the PR number while the DB has
                # no record.  Persist it via a separate session so the retry
                # cycle's crash-recovery check resumes the existing PR instead
                # of creating a duplicate.
                #
                # Production 2026-05-04: when the "Parent state changed during
                # create_pull_request" raise at line ~1211 fired, ``parent``
                # had been reassigned to None by ``_relock_parent`` at the
                # call above. Subsequent ``parent.id`` access in this except
                # block crashed with AttributeError, swallowing the rescue.
                # Use ``parent_id_local`` / ``project_id_local`` (captured
                # pre-release in this same scope at lines 981-982) which
                # always hold the original parent identifiers regardless of
                # whether ``parent`` got reassigned to None mid-flow.
                _err_parent_id = (
                    parent_id_local
                    if parent_id_local is not None
                    else (parent.id if parent is not None else None)
                )
                _err_project_id = (
                    project_id_local
                    if project_id_local is not None
                    else (parent.project_id if parent is not None else None)
                )
                if rollup_pr_number is not None and _err_parent_id is not None:
                    try:
                        async with AsyncSessionLocal() as rescue_session:
                            db_parent = await rescue_session.get(Task, _err_parent_id)
                            if db_parent is not None:
                                db_ctx = db_parent.context or {}
                                if not db_ctx.get("rollup_pr_number"):
                                    db_parent.context = {
                                        **db_ctx,
                                        "rollup_pr_number": rollup_pr_number,
                                        "rollup_pr_url": rollup_pr_url,
                                    }
                                    rescue_session.add(db_parent)
                                    await rescue_session.commit()
                                    logger.warning(
                                        f"[ROLLUP] Rescued rollup PR #{rollup_pr_number} marker "
                                        f"for parent {_err_parent_id} via separate session — "
                                        f"preventing orphan on retry"
                                    )
                    except Exception as rescue_err:
                        logger.error(
                            f"[ROLLUP] Failed to rescue rollup PR #{rollup_pr_number} marker "
                            f"for parent {_err_parent_id}: {rescue_err}",
                            exc_info=True,
                        )
                # Sentinel deferral: the rest of this handler (retry counter,
                # status flip, failure classification, alert, post-handler
                # finalization at line ~2049) all dereference ``parent``, which
                # is None on this path. The rescue above already persisted the
                # PR marker; the next sweep cycle re-enters with fresh parent
                # state and resumes from the existing-PR branch.
                if isinstance(e, _ParentMutatedDuringPRCreation):
                    return
                err_str = str(e).lower()
                # Auth failures: invalidate the cached token so the next retry uses a fresh one
                if (
                    isinstance(e, HTTPException)
                    and e.status_code == 401
                    and _err_project_id is not None
                ):
                    from app.routers.agent import invalidate_github_token_cache

                    invalidate_github_token_cache(str(_err_project_id))
                    logger.warning(
                        f"[ROLLUP] Auth failure (401) for parent {_err_parent_id} — invalidated token cache"
                    )
                is_transient = (
                    isinstance(e, HTTPException)
                    and e.status_code in (401, 500, 502, 503, 504)
                ) or any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "500 internal server error",
                        "502 bad gateway",
                        "503 service unavailable",
                        "504 gateway timeout",
                        "internal server error",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        # Staging branch may have been deleted by a race — retry
                        # to allow recovery or manual intervention
                        "staging branch",
                        "validation failed",
                        # Auth failures — token expired/revoked; cache is invalidated above
                        "unauthorized",
                        "bad credentials",
                        "token expired",
                    ]
                )
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= (
                    max_rollup := _runtime_config.get("max_rollup_retries", 5)
                ):
                    # Transient GitHub error — leave parent EVALUATING, retry next cycle
                    parent.context = {
                        **(parent.context or {}),
                        "_rollup_retries": rollup_retries,
                    }
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/{max_rollup}), will retry: {type(e).__name__}"
                    logger.warning(
                        f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}"
                    )
                else:
                    logger.warning(
                        f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True
                    )
                    parent.status = TaskStatus.FAILED
                    parent.context = _clear_rollup_transient_state(parent.context)
                    parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[
                        :500
                    ]
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )

                    _fc = classify_failure(parent)
                    persist_failure_category(parent, _fc)
                    _record_rollup_failure_signals(parent, _fc)
                    await _maybe_create_failure_alert(
                        parent,
                        session,
                        f"All {completed_count} subtasks completed but rollup PR creation failed after {rollup_retries} retries — manual recovery required",
                    )
                    # NOTE: Do NOT delete staging_branch here — it contains all
                    # merged child work. Preserving it allows manual recovery or
                    # requeue. Branches are cleaned up by _cleanup_branches when
                    # the parent eventually reaches a terminal state, or by the
                    # stale branch cleanup service after 7 days.
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = (
                f"All {completed_count} subtasks completed and merged"
            )

        # Final guard: a parent with an unmerged rollup PR is not done yet —
        # the review/fix/CI pipeline is still working on landing the code.
        _pctx = parent.context or {}
        if (
            parent.status == TaskStatus.COMPLETED
            and _pctx.get("rollup_pr_number")
            and not _pctx.get("pr_merged")
        ):
            parent.status = TaskStatus.EVALUATING
            _rpr = _pctx["rollup_pr_number"]
            parent.latest_status_message = f"All {completed_count} subtasks done — rollup PR #{_rpr} awaiting merge"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — blocked COMPLETED: rollup PR #{_rpr} not yet merged"
            )

        # Terminal transition — scrub transient deferral/retry counters so a
        # future re-entry (admin requeue, reused parent) starts from zero.
        if parent.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            parent.context = _clear_rollup_transient_state(parent.context)

        session.add(parent)
        await session.commit()
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": parent.status},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})"
        )

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = (
            f"{failed_count} of {len(children)} subtasks failed"
        )
        from app.failure_classification import (
            worst_child_failure_category,
            persist_failure_category,
        )

        _failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        _fc = worst_child_failure_category(_failed_children)
        persist_failure_category(parent, _fc)
        _record_rollup_failure_signals(parent, _fc)
        parent.context = _clear_rollup_transient_state(parent.context)
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        logger.error(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")
# fmt: on
