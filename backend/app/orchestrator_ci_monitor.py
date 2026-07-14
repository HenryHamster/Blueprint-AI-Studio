"""EVALUATING / CI-waiting monitor sweep, extracted from ``orchestrator_worker``.

This module owns the post-dispatch stage of the leader cycle that polls tasks
sitting in ``EVALUATING`` with ``review_loop.ci_waiting`` set: it resolves each
PR's CI state, spawns fix tasks on failure, drives the no-CI verify-tests gate,
and hands terminal outcomes to ``handle_ci_completion``. It was lifted verbatim
out of the ``orchestrator_worker`` inline block (behaviour-preserving) to shrink
the orchestrator monolith, mirroring the session-parameterized shape of
``reaper_sweeps.py``: the single entry point ``_monitor_evaluating_tasks`` takes
the leader's ``session`` and preserves every log prefix and commit/error path.

Lock/hot-path primitives (``_lock_task_for_mutation``) stay in ``app.orchestrator``
and are imported lazily inside the sweep to avoid a load-time import cycle (the
orchestrator module re-exports the symbols below). All extracted symbols are
re-exported from ``app.orchestrator`` so ``from app.orchestrator import ...``
keeps working for existing callers and tests.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.shared_state as _shared_state
from app.database import resolve_dialect_name
from app.dep_lifecycle import _mark_unblocked_dependents
from app.models import Project, Task, TaskStatus
from app.orchestrator_liveness import _mark_orchestrator_progress
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow, clear_retry_after

logger = logging.getLogger("blueprint")


# No-CI merge gate: how many times a CRASHED verify_tests task (FAILED/CANCELLED,
# or COMPLETED with no usable verdict artifact) may be re-spawned before the gate
# falls open to an UNVERIFIED merge. A crashed verify task is most often transient
# infra noise (a failed cold clone of the PR head), so one retry recovers the
# common flake; the hard cap precludes any re-spawn loop. A genuine
# detected=false verdict (a suite that ran and found nothing) is NOT a crash and
# never retries — it falls open immediately, as before.
_VERIFY_TESTS_MAX_RETRIES = 1

# No-CI merge gate: how many consecutive cycles the verify_tests spawn may be
# deferred (i.e. _spawn_pipeline_task returns None due to resource pressure)
# before the parent task is escalated to FAILED + operator review.  A single
# transient deferral is expected during resource pressure, but a persistent
# deferral across 5 cycles means the PR is stranded with no self-recovery path.
_VERIFY_TESTS_SPAWN_DEFERRAL_CAP = 5


def _ci_result_from_resolved_pr_ci_status(resolved: dict) -> dict | None:
    """Build the ci_result handoff for terminal resolver states."""
    state = resolved.get("state")
    if state == "green":
        return {
            "status": "completed",
            "conclusion": "success",
            "details": resolved.get("details", []),
            "failed_logs": "",
        }
    if state != "red":
        return None

    failed_logs = resolved.get("failed_logs", "")
    if not failed_logs:
        fail_check_names = ", ".join(
            d.get("name", "")
            for d in resolved.get("details", [])
            if d.get("conclusion") == "failure"
        )
        failed_logs = (
            f"CI checks failed: {fail_check_names}"
            if fail_check_names
            else "CI checks failed (see workflow run)"
        )
    return {
        "status": "completed",
        "conclusion": "failure",
        "details": resolved.get("details", []),
        "failed_logs": failed_logs,
    }


def _clear_retry_after_state(task_or_ctx) -> None:
    """Clear retry_after hold and its reset counter on a Task or context dict.

    Why: the counter feeds an escalation alert; leaving it across recovery
    episodes causes false escalations on the next rate-limit episode.

    Audit 2026-04-25 W1: ``task.retry_after_until`` is now a real column
    so callers should pass the Task; the dict-only form is kept for legacy
    callsites that operate on bare context dicts (e.g. spawn paths).
    """
    clear_retry_after(task_or_ctx)


def _capture_ci_polling_snapshot(task: Task) -> dict:
    """Snapshot the CI-polling fields a lock-window break must protect.

    R5-W2-A: ``updated_at`` advances every ~30s via the heartbeat
    ``onupdate=_utcnow`` and would false-defer the CI-polling re-acquire
    on every overlapping heartbeat. We validate against content fields
    instead — the ones whose mutation indicates a peer took over CI
    handling for this task:

      - ``status``: the most load-bearing; if a peer transitioned the
        task out of EVALUATING (e.g. to COMPLETED on PR merge or FAILED
        on PR delete), we must not re-apply CI-polling mutations on top.
      - ``review_loop.{pr_number, ci_passed, review_approved, iteration}``:
        if any of these moved, the review pipeline advanced under us.

    Heartbeat-only updates touch none of these.
    """
    rl = (task.context or {}).get("review_loop") or {}
    return {
        "status": task.status,
        "pr_number": rl.get("pr_number"),
        "ci_passed": rl.get("ci_passed"),
        "review_approved": rl.get("review_approved"),
        "iteration": rl.get("iteration"),
    }


def _ci_waiting_sql_filter(session):
    """Dialect-portable WHERE clause selecting only EVALUATING rows whose
    ``context.review_loop.ci_waiting`` is truthy.

    C3: the CI-poll loop previously fetched ALL EVALUATING rows (up to 200) and
    then ``_lock_task_for_mutation``'d each one only to discover most are not
    CI-waiting and immediately ``rollback()`` — an O(N) lock-and-rollback cost
    on the leader's hot cycle proportional to the whole EVALUATING backlog, not
    to the (usually small) CI-waiting subset. Pushing the filter into SQL drops
    that cost: only genuinely CI-waiting rows are locked.

    This matches EXACTLY the loop's own gate (``ci_loop.get("ci_waiting")`` where
    ``ci_loop = context["review_loop"]``), so it is a pure prefilter — no
    behavioural change to which tasks the loop acts on. The Python-side guard
    inside the loop remains as cheap defense-in-depth.

    Postgres: ``->'review_loop'->>'ci_waiting'`` compared to the string
    ``'true'`` (JSONB booleans render as ``true``/``false`` via ``->>``).
    SQLite (standalone tests): ``json_extract`` returns the integer 1 for a JSON
    ``true``, so compare against 1.
    """
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        return _func.json_extract(Task.context, "$.review_loop.ci_waiting") == 1
    return Task.context["review_loop"]["ci_waiting"].astext == "true"


def _resolve_ci_fix_branch(
    review_loop: dict | None, payload: dict | None, pr_data: dict | None
) -> str:
    """Resolve the head branch for a CI/conflict fix-task spawn.

    Prefers our stored branch (``review_loop`` then ``payload``) but falls back
    to the PR's *live* head ref when both are empty/stale, so a missing stored
    branch can't reach ``_spawn_pipeline_task`` as an empty ``FixPayload.branch``
    — which raises a ValueError every cycle and wedges the task as a poison pill
    (incident 2026-05-28: task cf1d2a25 / 7f0e7ec5)."""
    branch = (review_loop or {}).get("branch", "") or (payload or {}).get("branch", "")
    if not branch:
        branch = ((pr_data or {}).get("head", {}) or {}).get("ref", "")
    return branch or ""


class _ProjectSnapshot:
    """Lightweight, non-ORM snapshot of Project fields for the CI monitor cache.

    Pre-loaded once per sweep and survives session.rollback() because it is not
    tracked by the session.  Contains only the fields needed by read-only consumers
    downstream in the CI monitor loop so the pre-load dict never returns a stale
    (expired) ORM object.
    """

    __slots__ = (
        "build_config",
        "github_owner",
        "github_repo",
        "id",
        "integration_branch",
        "is_self_repo",
        "name",
        "promotion_target",
    )

    def __init__(self, project: Project) -> None:
        self.id = project.id
        self.integration_branch = getattr(project, "integration_branch", None)
        self.promotion_target = getattr(project, "promotion_target", None)
        self.name = getattr(project, "name", "")
        self.is_self_repo = getattr(project, "is_self_repo", False)
        self.github_repo = getattr(project, "github_repo", "") or ""
        self.github_owner = getattr(project, "github_owner", "") or ""
        self.build_config = dict(getattr(project, "build_config", None) or {})

    @property
    def staging_branch_prefixes(self) -> tuple[str, ...]:
        configured = self.build_config.get("staging_branch_prefixes")
        if configured is not None:
            prefixes = tuple(str(prefix) for prefix in configured if prefix)
            if prefixes:
                return prefixes
        return ("research/", "plan/")

    def is_staging_branch(self, branch: str | None) -> bool:
        if not branch:
            return False
        return branch.startswith(tuple(self.staging_branch_prefixes))


async def _apply_ci_merged_landed_signal(
    ci_task: Task,
    session: AsyncSession,
    *,
    base_branch: str,
    review_loop: dict,
    project: Project | None = None,
) -> bool:
    """Stage the pr_merged landed signal only when the merged PR landed on
    the project's integration branch.

    Returns True when the merged PR landed on the integration branch. When the
    base branch resolves to a distinct promotion target, the helper withholds
    pr_merged and stages the same branch-safety alert the rollup boundary uses.

    ``project`` may be pre-loaded from the caller's batch cache to avoid an
    N+1 session.get(Project, ...) on the hot CI-polling path.  When omitted
    (default), the function falls back to the legacy per-call session.get.
    """
    from app.landed_signal import apply_merged_landed_signal

    if project is None:
        project = (
            await session.get(Project, ci_task.project_id) if ci_task.project_id else None
        )
    hotfix_lane = bool(
        (ci_task.first_payload or {}).get("hotfix")
        or (ci_task.context or {}).get("hotfix")
        or review_loop.get("hotfix")
    )
    return apply_merged_landed_signal(
        ci_task,
        project,
        base_branch=base_branch,
        hotfix_lane=hotfix_lane,
    )


async def _monitor_evaluating_tasks(session: AsyncSession) -> None:
    """Poll EVALUATING/CI-waiting tasks and drive their CI outcomes.

    Extracted verbatim from ``orchestrator_worker``; ``session`` is the
    leader cycle's live session. Behaviour-preserving.
    """
    # Lock primitive stays in app.orchestrator; imported lazily to avoid a
    # load-time import cycle (orchestrator re-exports this module).
    from app.orchestrator import _lock_task_for_mutation
    # Hot-path helpers the block relied on from the enclosing worker scope.
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.rollup_management import _verify_pr_merged
    from app.review_pipeline import _spawn_pipeline_task
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    # Imported at function entry (not module level) to avoid a load-time cycle:
    # agent_status → orchestrator → orchestrator_ci_monitor. Placing it here
    # ensures all code paths in this function see it as a bound local variable.
    from app.routers.agent_status import record_task_result
    from app.github import (
        close_pull_request,
        get_check_runs_cached,
        get_pull_request,
        pr_is_not_behind_for,
        reopen_pull_request,
        update_branch_with_sha_retry,
    )
    # Check tasks waiting on CI (review approved, CI pending)
    CI_WAIT_TIMEOUT_MINUTES = 20
    ci_waiting_task_ids = (
        (
            await session.execute(
                select(Task.id)
                .where(
                    Task.status == TaskStatus.EVALUATING,
                    # C3: only CI-waiting rows — see
                    # _ci_waiting_sql_filter. Drops the per-row
                    # lock-and-rollback cost on the (typically large)
                    # non-CI-waiting EVALUATING backlog.
                    _ci_waiting_sql_filter(session),
                )
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    # Batch-load all unique projects referenced by the CI-waiting tasks to avoid
    # N+1 session.get(Project, ...) queries per task. Uses _ProjectSnapshot
    # (non-ORM lightweight data) so the cache survives session.rollback() —
    # rollback unconditionally expires all ORM objects, which would make a
    # plain ORM cache unusable after any early-skip rollback path.  Write paths
    # that need a tracked ORM Project re-fetch individually (rare — merged PR
    # signal, test-command re-detection, recovered test persistence).
    _preloaded_projects: dict[uuid.UUID, _ProjectSnapshot] = {}
    if ci_waiting_task_ids:
        _project_ids_raw = (
            (
                await session.execute(
                    select(Task.project_id).where(
                        Task.id.in_(ci_waiting_task_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        _unique_pids = {pid for pid in _project_ids_raw if pid is not None}
        if _unique_pids:
            _proj_rows = await session.execute(
                select(Project).where(
                    Project.id.in_(list(_unique_pids))
                )
            )
            _preloaded_projects = {
                p.id: _ProjectSnapshot(p) for p in _proj_rows.scalars().all()
            }

    def _get_project(
        project_id: uuid.UUID | None,
    ) -> _ProjectSnapshot | None:
        """Resolve a project from the pre-loaded cache, falling back to None
        when the id is absent or not in the batch (should not occur in practice
        since all task project_ids were collected above)."""
        if project_id is None:
            return None
        return _preloaded_projects.get(project_id)

    async def _resnapshot_project(
        project: Project | None,
    ) -> _ProjectSnapshot | None:
        """Refresh and replace a cached project snapshot after a write helper.

        The CI sweep intentionally uses non-ORM snapshots so rollbacks cannot
        expire the hot-path project cache. Rare self-heal helpers re-fetch and
        commit tracked ORM Projects, though; after they mutate build_config the
        same poll must re-run gate predicates against a fresh snapshot rather
        than the stale preloaded one.
        """
        if project is None:
            return None
        try:
            await session.refresh(project)
        except Exception:
            logger.warning(
                "[CI] Could not refresh project %s after build_config self-heal",
                getattr(project, "id", None),
                exc_info=True,
            )
            return _get_project(getattr(project, "id", None))
        snapshot = _ProjectSnapshot(project)
        _preloaded_projects[snapshot.id] = snapshot
        return snapshot

    for ci_task_id in ci_waiting_task_ids:
        # Heartbeat per task: this loop is O(EVALUATING backlog) and
        # is the dominant contributor to cycle wall-time. Marking
        # progress each iteration keeps liveness fresh across an
        # arbitrarily long poll pass (incident 2026-05-28).
        _mark_orchestrator_progress()
        ci_task = await _lock_task_for_mutation(
            session,
            ci_task_id,
            allowed_statuses=(TaskStatus.EVALUATING,),
            reason="CI polling",
        )
        if ci_task is None:
            continue
        ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
        if not ci_loop.get("ci_waiting"):
            await session.rollback()
            continue
        if ci_loop.get("conflict_fix_spawned"):
            await session.rollback()
            continue  # Already handled — fix task spawned for merge conflicts
        if ci_loop.get("ci_fix_spawned"):
            await session.rollback()
            continue  # Already handled — fix task spawned for CI failure
        if ci_loop.get("merge_rule_blocked"):
            await session.rollback()
            continue  # Merge blocked by branch protection — requires manual intervention
        # C5: skip approval/safeguard-gated rows while the gate is set
        # (and gates are enabled). handle_ci_completion ->
        # _attempt_merge_with_retry_and_escalation now re-checks
        # requires_approval and refuses to merge, so polling a gated
        # row would just churn GitHub I/O every cycle for no progress.
        # The merge resumes once the safeguard/operator clears the flag
        # (which also re-arms ci_waiting). With gates OFF the flag is
        # advisory only, so do not skip. This is the orchestrator-side
        # half of the merge-call invariant (cluster C5 fix #2).
        if ci_task.requires_approval and _runtime_config.get(
            "require_approval_gates", True
        ):
            await session.rollback()
            continue
        # PR-identity split-brain fix: resolve via the canonical
        # accessor (review_loop.pr_number OR context.rollup_pr_number),
        # the SAME fallback the recovery sweeps use — a rollup whose
        # identity lived only at context.rollup_pr_number was previously
        # SKIPPED here and drained only out-of-band.
        from app.ci_merge_helpers import effective_pr_number

        ci_pr_number = effective_pr_number(ci_task)
        if not ci_pr_number:
            await session.rollback()
            continue

        # Ensure ci_wait_started AND pr_number are always co-present:
        # some paths arm ci_waiting without ci_wait_started (silently
        # disables the timeout), and when the number came from the
        # rollup_pr_number fallback, stamp it into review_loop.pr_number
        # so the backfill + every downstream read stay consistent.
        # Commit, then reacquire task_lock before more mutations below.
        _needs_pr_stamp = not ci_loop.get("pr_number")
        if not ci_loop.get("ci_wait_started") or _needs_pr_stamp:
            from app import pause_clock

            if _needs_pr_stamp:
                ci_loop["pr_number"] = ci_pr_number
            if not ci_loop.get("ci_wait_started"):
                ci_loop["ci_wait_started"] = _utcnow().isoformat()
                ci_loop[
                    "ci_wait_pause_baseline"
                ] = await pause_clock.current_baseline(session)
            ci_task.context = {
                **(ci_task.context or {}),
                "review_loop": ci_loop,
            }
            # Without flag_modified, JSONB nested-dict mutations
            # may not be detected by SQLAlchemy when the new outer
            # dict shares the same review_loop reference as the
            # old. Drives the 2026-04-24 "CI wait exceeded 20min"
            # flap where escalated/ci_last_polled never persisted.
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(ci_task, "context")
            session.add(ci_task)
            await session.commit()
            ci_task = await _lock_task_for_mutation(
                session,
                ci_task_id,
                allowed_statuses=(TaskStatus.EVALUATING,),
                reason="CI polling after wait-start persist",
            )
            if ci_task is None:
                continue
            ci_loop = dict(
                (ci_task.context or {}).get("review_loop", {}) or {}
            )

        # Exponential backoff: skip if not enough time has elapsed since last poll.
        # Webhooks handle the fast path — polling is a fallback.
        ci_poll_interval = ci_loop.get("ci_poll_interval", 30)
        ci_last_polled = ci_loop.get("ci_last_polled")
        if ci_last_polled:
            try:
                _last_polled_dt = datetime.fromisoformat(ci_last_polled)
                if (
                    _utcnow() - _last_polled_dt
                ).total_seconds() < ci_poll_interval:
                    await session.rollback()
                    continue
            except (ValueError, TypeError):
                pass

        try:
            # Timeout: if CI has been pending too long, proceed to merge anyway
            # Use ci_wait_started if available, otherwise fall back to started_at/created_at
            ci_wait_started = ci_loop.get("ci_wait_started")
            try:
                started = (
                    datetime.fromisoformat(ci_wait_started)
                    if ci_wait_started
                    else None
                )
            except (ValueError, TypeError):
                started = None
            if started is None:
                started = ci_task.started_at or ci_task.created_at
            if (
                started
                and (_utcnow() - started).total_seconds()
                > CI_WAIT_TIMEOUT_MINUTES * 60
            ):
                # Escalate: alert the user but keep retrying.
                # The 30-min backstop in services.py is the true hard-kill.
                if not ci_loop.get("escalated"):
                    ci_loop["escalated"] = True
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    # See note at the ci_wait_started persist above
                    # — without flag_modified the escalated flag
                    # silently fails to persist, the next cycle
                    # reads escalated=False, and this whole block
                    # re-fires every 2-4s, which is exactly the
                    # "CI wait exceeded 20min" flap pattern.
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(ci_task, "context")
                    ci_task.latest_status_message = f"CI wait exceeded {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — retrying, alert sent"
                    session.add(ci_task)
                    await _maybe_create_failure_alert(
                        ci_task,
                        session,
                        f"Rollup PR #{ci_pr_number} stuck in CI-wait for >{CI_WAIT_TIMEOUT_MINUTES}min — still retrying",
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling after escalation persist",
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict(
                        (ci_task.context or {}).get("review_loop", {}) or {}
                    )
                # Fall through — continue polling CI even after escalation

            (
                ci_gh_token,
                ci_owner,
                ci_repo,
            ) = await get_github_token_for_task(ci_task.id, session)

            # Use cached head SHA when available to skip the PR fetch (saves 1 API call per poll).
            # Re-fetch PR every 5th poll to detect force-pushes and merge state changes.
            cached_head_sha = ci_loop.get("head_sha", "")
            polls_since_fetch = (
                ci_loop.get("polls_since_pr_fetch", 5) + 1
            )  # default 5 to force first fetch
            need_pr_fetch = not cached_head_sha or polls_since_fetch >= 5
            pr_data = None

            if not need_pr_fetch:
                # Fast path: skip PR fetch, use cached SHA, just check CI
                pr_data = {
                    "state": "open",
                    "head": {"sha": cached_head_sha},
                    "mergeable": True,
                }
                ci_loop["polls_since_pr_fetch"] = polls_since_fetch

            if need_pr_fetch:
                ci_loop["polls_since_pr_fetch"] = 0
                # ── Phase B3: release lock around get_pull_request ──
                # GitHub rate-limit can hold this call for minutes;
                # holding the lock starves concurrent heartbeats on
                # the same task. Release, fetch, re-acquire-validate.
                # R5-W2-A: content snapshot, not updated_at —
                # heartbeat onupdate would false-defer every poll.
                ci_pre_pr_snapshot = _capture_ci_polling_snapshot(ci_task)
                await session.commit()
                try:
                    pr_data = await get_pull_request(
                        ci_gh_token, ci_owner, ci_repo, ci_pr_number
                    )
                except HTTPException as exc:
                    # Re-acquire to apply error-path mutations
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling after PR fetch error",
                        expected_snapshot=ci_pre_pr_snapshot,
                        snapshot_fn=_capture_ci_polling_snapshot,
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict(
                        (ci_task.context or {}).get("review_loop", {}) or {}
                    )
                    if exc.status_code in (404, 410):
                        _pr_gone_msg = f"PR #{ci_pr_number} no longer exists (deleted or repo archived)"
                        logger.exception(
                            f"[CI] {_pr_gone_msg} for task {ci_task.id}"
                        )
                        ci_task.status = TaskStatus.FAILED
                        if ci_task.vm_provisioned_at:
                            ci_task.vm_stopped_at = _utcnow()
                        ci_task.latest_status_message = _pr_gone_msg
                        from app.failure_classification import (
                            persist_failure_category,
                        )
                        from app.routers.agent_status import (
                            record_task_result,
                        )

                        persist_failure_category(ci_task, "EXTERNAL_API")
                        record_task_result(
                            success=False,
                            failure_class="EXTERNAL_API",
                            task_id=str(ci_task.id),
                            task_type=(ci_task.first_payload).get(
                                "type", ""
                            ),
                        )
                        session.add(ci_task)
                        await _maybe_create_failure_alert(
                            ci_task, session, _pr_gone_msg
                        )
                        # Snapshot the broadcast scalars into plain locals BEFORE
                        # commit: session.commit() expires all ORM state, so reading
                        # ci_task.project_id/.id/.status afterwards lazy-loads from
                        # async context and raises MissingGreenlet on asyncpg.
                        _bcast_project_id = str(ci_task.project_id)
                        _bcast_task_id = str(ci_task.id)
                        _bcast_status = ci_task.status
                        await session.commit()
                        await broadcast_to_project(
                            _bcast_project_id,
                            "task_updated",
                            {
                                "task_id": _bcast_task_id,
                                "status": _bcast_status,
                            },
                        )
                        continue
                    # Transient GitHub API error — backoff and retry
                    _fetch_failures = (
                        ci_loop.get("pr_fetch_failures", 0) + 1
                    )
                    ci_loop["pr_fetch_failures"] = _fetch_failures
                    ci_loop["ci_last_polled"] = _utcnow().isoformat()
                    ci_loop["ci_poll_interval"] = min(
                        (ci_loop.get("ci_poll_interval", 30)) * 2, 300
                    )
                    if _fetch_failures >= 10 and not ci_loop.get(
                        "fetch_escalated"
                    ):
                        ci_loop["fetch_escalated"] = True
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} GitHub API failures ({_fetch_failures}x) — still retrying",
                        )
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(ci_task, "context")
                    logger.warning(
                        f"[CI] Transient error fetching PR #{ci_pr_number} for task {ci_task.id} "
                        f"(attempt {_fetch_failures}): {exc.detail}"
                    )
                    session.add(ci_task)
                    await session.commit()
                    continue

                if not pr_data:
                    await session.rollback()
                    continue

                # Re-acquire lock after successful PR fetch.
                ci_task = await _lock_task_for_mutation(
                    session,
                    ci_task_id,
                    allowed_statuses=(TaskStatus.EVALUATING,),
                    reason="CI polling after PR fetch",
                    expected_snapshot=ci_pre_pr_snapshot,
                    snapshot_fn=_capture_ci_polling_snapshot,
                )
                if ci_task is None:
                    continue
                ci_loop = dict(
                    (ci_task.context or {}).get("review_loop", {}) or {}
                )

                if ci_loop.get("pr_fetch_failures"):
                    ci_loop["pr_fetch_failures"] = 0

            pr_state = pr_data.get("state", "open")

            # Check if PR was already merged (prevents double merge).
            # If we self-closed for a mergeability reset and reopen failed,
            # retry the reopen instead of marking COMPLETED — otherwise a
            # transient reopen failure would silently discard the work.
            if pr_state == "closed" or pr_data.get("merged"):
                if (
                    pr_state == "closed"
                    and not pr_data.get("merged")
                    and ci_loop.get("pending_reopen")
                ):
                    from sqlalchemy.orm.attributes import flag_modified

                    _reopen_attempts = ci_loop.get("reopen_attempts", 0) + 1
                    ci_loop["reopen_attempts"] = _reopen_attempts
                    _reopen_ok = False
                    try:
                        _reopen_ok = bool(
                            await reopen_pull_request(
                                ci_gh_token,
                                ci_owner,
                                ci_repo,
                                ci_pr_number,
                                project_id=str(ci_task.project_id),
                            )
                        )
                    except Exception as _reopen_exc:
                        logger.warning(
                            f"[CI] retry reopen of PR #{ci_pr_number} raised: {_reopen_exc}"
                        )
                    if _reopen_ok:
                        ci_loop["pending_reopen"] = False
                        ci_loop["reopen_attempts"] = 0
                        ci_loop.pop("reopen_alert_fired", None)
                        ci_loop["polls_since_pr_fetch"] = 999
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} reopened after prior reset failure "
                            f"(after {_reopen_attempts} attempt{'s' if _reopen_attempts != 1 else ''})"
                        )
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        continue
                    _reopen_alert_threshold = _runtime_config.get(
                        "ci_reopen_alert_threshold", 5
                    )
                    _reopen_approval_cap = _runtime_config.get(
                        "ci_reopen_approval_cap", 10
                    )
                    ci_task.latest_status_message = (
                        f"PR #{ci_pr_number} reopen failed (attempt {_reopen_attempts}) — "
                        f"will retry on next poll"
                    )
                    if (
                        _reopen_attempts >= _reopen_alert_threshold
                        and not ci_loop.get("reopen_alert_fired")
                    ):
                        ci_loop["reopen_alert_fired"] = True
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                            f"GitHub may be rejecting the reopen, still retrying",
                        )
                    if (
                        _reopen_attempts >= _reopen_approval_cap
                        and not ci_task.requires_approval
                    ):
                        ci_task.status = TaskStatus.FAILED
                        if ci_task.vm_provisioned_at:
                            ci_task.vm_stopped_at = _utcnow()
                        # Gate only the approval flag — FAILED is the
                        # terminalization and stays. Gates off → task FAILs
                        # without approval-gating (no 2h timeout strand).
                        if _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            ci_task.requires_approval = True
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                            f"manual intervention required"
                        )
                        from app.failure_classification import (
                            persist_failure_category as _pfc_reopen,
                        )

                        _pfc_reopen(ci_task, "EXTERNAL_API")
                        record_task_result(
                            success=False,
                            failure_class="EXTERNAL_API",
                            task_id=str(ci_task.id),
                            task_type=(ci_task.first_payload).get(
                                "type", ""
                            ),
                        )
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} reopen cap exhausted ({_reopen_attempts} attempts) — "
                            f"task FAILED, manual intervention required",
                        )
                        logger.warning(
                            f"[CI] PR #{ci_pr_number} reopen attempts ({_reopen_attempts}) "
                            f"hit cap — marking task {ci_task.id} FAILED (requires_approval)"
                        )
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    flag_modified(ci_task, "context")
                    session.add(ci_task)
                    await session.commit()
                    if ci_task.status == TaskStatus.FAILED:
                        await broadcast_to_project(
                            str(ci_task.project_id),
                            "task_updated",
                            {
                                "task_id": str(ci_task.id),
                                "status": ci_task.status,
                            },
                        )
                    continue
                if pr_data.get("merged"):
                    _orch_ci_project = _get_project(ci_task.project_id)
                    _ci_base = (
                        (pr_data.get("base") or {}).get("ref", "")
                        or ci_loop.get("base_branch", "")
                        or (ci_task.first_payload or {}).get(
                            "base_branch", ""
                        )
                        or _shared_state.get_project_integration_branch(
                            _orch_ci_project
                        )
                    )
                    # Re-fetch tracked ORM Project for the write path:
                    # _apply_ci_merged_landed_signal -> apply_merged_landed_signal
                    # -> record_capability_proof modifies
                    # project.capability_proof_records (flag_modified).  The
                    # lightweight _ProjectSnapshot in the cache cannot be tracked
                    # by the session, so a fresh ORM fetch is needed here.
                    # This is a rare path (only when the PR is already merged).
                    _orch_ci_project_orm = (
                        await session.get(Project, ci_task.project_id)
                        if ci_task.project_id is not None
                        else None
                    )
                    _landed_on_integration = (
                        await _apply_ci_merged_landed_signal(
                            ci_task,
                            session,
                            base_branch=_ci_base,
                            review_loop=ci_loop,
                            project=_orch_ci_project_orm,
                        )
                    )
                    ci_task.status = TaskStatus.COMPLETED
                    ci_task.latest_status_message = (
                        f"PR #{ci_pr_number} merged"
                    )
                    session.add(ci_task)
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    logger.info(
                        f"[CI] PR #{ci_pr_number} merged, marking task COMPLETED"
                    )
                    # Completion-side warm-reuse trigger: now that this
                    # PR has landed on integration, proactively mark any
                    # dependents it just unblocked as warm-claimable so
                    # this task's warm VM can claim follow-on work inside
                    # its teardown-defer grace window instead of tearing
                    # down cold. Best-effort: a failure here must never
                    # abort the CI completion cycle.
                    if _landed_on_integration:
                        try:
                            _unblocked = await _mark_unblocked_dependents(
                                ci_task, _verify_pr_merged
                            )
                            if _unblocked:
                                logger.info(
                                    f"[CI] PR #{ci_pr_number} unblocked "
                                    f"{_unblocked} dependent(s) — marked warm-claimable"
                                )
                        except Exception:
                            logger.exception(
                                "[CI] dependent warm-claim marking failed "
                                f"for task {ci_task.id}"
                            )
                else:
                    # Closed without merge (human/bot intervention, not a self-close
                    # reset — pending_reopen case handled above). Do NOT mark
                    # COMPLETED — the code never landed. Fail the task so rollup
                    # parents don't silently report success.
                    _closed_msg = f"PR #{ci_pr_number} was closed without being merged"
                    ci_task.status = TaskStatus.FAILED
                    if ci_task.vm_provisioned_at:
                        ci_task.vm_stopped_at = _utcnow()
                    ci_task.latest_status_message = _closed_msg
                    from app.failure_classification import (
                        persist_failure_category as _pfc_closed,
                    )

                    _pfc_closed(ci_task, "EXTERNAL_API")
                    record_task_result(
                        success=False,
                        failure_class="EXTERNAL_API",
                        task_id=str(ci_task.id),
                        task_type=(ci_task.first_payload).get("type", ""),
                    )
                    session.add(ci_task)
                    await _maybe_create_failure_alert(
                        ci_task, session, _closed_msg
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    logger.warning(
                        f"[CI] {_closed_msg} — marking task {ci_task.id} FAILED"
                    )
                continue

            # Check for merge conflicts — CI won't trigger on conflicting PRs.
            # GitHub returns None while computing mergeability; usually transient
            # but can get stuck after force-pushes or branch-protection changes.
            # Track consecutive nulls and force a recompute via close+reopen
            # before the 30-min CI backstop kills the task and discards all work.
            if pr_data.get("mergeable") is None:
                _null_count = ci_loop.get("mergeable_null_count", 0) + 1
                ci_loop["mergeable_null_count"] = _null_count
                _null_first_ts = (
                    ci_loop.get("mergeable_null_first_ts")
                    or _utcnow().isoformat()
                )
                ci_loop["mergeable_null_first_ts"] = _null_first_ts
                try:
                    _null_elapsed = (
                        _utcnow() - datetime.fromisoformat(_null_first_ts)
                    ).total_seconds()
                except (ValueError, TypeError):
                    _null_elapsed = 0.0
                if _null_count >= 5 and _null_elapsed >= 180:
                    _reopen_cap = 3
                    _reopen_tries = ci_loop.get(
                        "mergeable_null_reopen_attempts", 0
                    )
                    if _reopen_tries >= _reopen_cap:
                        if not ci_task.requires_approval:
                            # Discretionary gate: keep the status message +
                            # alert (visibility) but only approval-gate when
                            # require_approval_gates is on, so a non-terminal
                            # ci_task isn't stranded on the 2h timeout.
                            if _runtime_config.get(
                                "require_approval_gates", True
                            ):
                                ci_task.requires_approval = True
                            ci_task.latest_status_message = (
                                f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                f"close/reopen attempts — manual intervention required"
                            )
                            await _maybe_create_failure_alert(
                                ci_task,
                                session,
                                f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                f"close/reopen attempts — escalating for manual intervention",
                            )
                        ci_loop["mergeable_null_count"] = 0
                        ci_loop["mergeable_null_first_ts"] = None
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        session.add(ci_task)
                        await session.commit()
                        continue
                    logger.warning(
                        f"[CI] PR #{ci_pr_number} mergeable=None for {_null_count} polls over "
                        f"{_null_elapsed:.0f}s — closing/reopening to force GitHub to recompute "
                        f"(attempt {_reopen_tries + 1}/{_reopen_cap})"
                    )
                    _reset_ok = False
                    _did_close = False
                    try:
                        if await close_pull_request(
                            ci_gh_token,
                            ci_owner,
                            ci_repo,
                            ci_pr_number,
                            project_id=str(ci_task.project_id),
                        ):
                            _did_close = True
                            _reset_ok = await reopen_pull_request(
                                ci_gh_token,
                                ci_owner,
                                ci_repo,
                                ci_pr_number,
                                project_id=str(ci_task.project_id),
                            )
                    except Exception as _reset_exc:
                        logger.warning(
                            f"[CI] close/reopen of PR #{ci_pr_number} raised: {_reset_exc}"
                        )
                    ci_loop["mergeable_null_count"] = 0
                    ci_loop["mergeable_null_first_ts"] = None
                    ci_loop["mergeable_null_reopen_attempts"] = (
                        _reopen_tries + 1
                    )
                    if _reset_ok:
                        ci_task.latest_status_message = f"PR #{ci_pr_number} mergeability stuck — closed/reopened to force recompute"
                        ci_loop["polls_since_pr_fetch"] = 999
                        ci_loop["pending_reopen"] = False
                    else:
                        if _did_close:
                            ci_loop["pending_reopen"] = True
                            ci_loop["polls_since_pr_fetch"] = 999
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} mergeability stuck ({_null_count} null polls) — close/reopen failed, still polling",
                        )
                ci_task.context = {
                    **(ci_task.context or {}),
                    "review_loop": ci_loop,
                }
                session.add(ci_task)
                await session.commit()
                continue
            if ci_loop.get("mergeable_null_count"):
                ci_loop["mergeable_null_count"] = 0
                ci_loop["mergeable_null_first_ts"] = None
            if pr_data.get("mergeable") is False:
                # GitHub's mergeable_state has several values that don't
                # indicate a real merge conflict — only 'dirty' (and
                # 'behind' as a fall-through fallback) should burn a VM
                # fix cycle. Other states are recoverable by waiting:
                # 'blocked' clears when required checks/reviews land,
                # 'unstable' means CI is failing but the PR is mergeable,
                # 'unknown' means GitHub is still computing.
                _ms = (pr_data.get("mergeable_state") or "").lower()
                _behind_fallthrough = False
                if _ms == "behind":
                    # ── Phase B3: release lock around update_branch_with_sha_retry ──
                    # The retry helper internally retries on 422; cumulative
                    # wait can be 30s+. Drop the lock so concurrent heartbeats
                    # on this same task (rollup parents share a row) don't queue.
                    # R5-W2-A: content snapshot, not updated_at —
                    # heartbeat onupdate would false-defer every poll.
                    ci_pre_update_branch_snapshot = (
                        _capture_ci_polling_snapshot(ci_task)
                    )
                    ci_pid_str = str(ci_task.project_id)
                    _expected_head_sha = (
                        pr_data.get("head", {}).get("sha") or None
                    )
                    # Short-circuit when GitHub's 'behind' state is stale —
                    # if the base tip is already an ancestor of the PR head,
                    # update_branch_with_sha_retry would just churn CI for
                    # no real change. Mirrors ci_completion.py's pattern.
                    _orch_ci_project_pre = _get_project(ci_task.project_id)
                    _ci_payload_pre = ci_task.first_payload
                    _ci_base_pre = (
                        ci_loop.get("base_branch", "")
                        or _ci_payload_pre.get("base_branch", "")
                        or _shared_state.get_project_integration_branch(
                            _orch_ci_project_pre
                        )
                    )
                    _not_behind_pre = await pr_is_not_behind_for(
                        ci_gh_token,
                        ci_owner,
                        ci_repo,
                        pr_detail=pr_data,
                        base_branch=_ci_base_pre,
                        project_id=ci_pid_str,
                        call_site="orchestrator_ci",
                    )
                    if _not_behind_pre is True:
                        logger.info(
                            f"[CI] PR #{ci_pr_number} mergeable_state='behind' but "
                            f"pr_is_not_behind=True — skipping update_branch_with_sha_retry"
                        )
                        await session.rollback()
                        continue
                    await session.commit()
                    # Trivially resolvable via GitHub's update-branch API.
                    # Update the branch in-place; the merge-commit
                    # triggers fresh CI and the next poll re-evaluates.
                    _upd = await update_branch_with_sha_retry(
                        ci_gh_token,
                        ci_owner,
                        ci_repo,
                        ci_pr_number,
                        expected_head_sha=_expected_head_sha,
                        project_id=ci_pid_str,
                    )
                    # Re-acquire to apply downstream mutations.
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling after update_branch",
                        expected_snapshot=ci_pre_update_branch_snapshot,
                        snapshot_fn=_capture_ci_polling_snapshot,
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict(
                        (ci_task.context or {}).get("review_loop", {}) or {}
                    )
                    if _upd.get("updated"):
                        ci_task.latest_status_message = f"PR #{ci_pr_number} was behind base — updated branch via GitHub API, awaiting fresh CI"
                        # Force a refetch next cycle so mergeable/head_sha refresh.
                        ci_loop["polls_since_pr_fetch"] = 999
                        ci_loop["head_sha"] = ""
                        # Reset behind-retry counter — any prior transient failures resolved.
                        ci_loop.pop("_behind_update_retries", None)
                        from sqlalchemy.orm.attributes import flag_modified

                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(
                            str(ci_task.project_id),
                            "task_updated",
                            {
                                "task_id": str(ci_task.id),
                                "status": ci_task.status,
                            },
                        )
                        logger.info(
                            f"[CI] PR #{ci_pr_number} behind base — update_branch succeeded, skipped fix spawn"
                        )
                        continue
                    if _upd.get("not_behind"):
                        # GitHub's cached mergeable_state was stale — the
                        # PR is not actually behind (no new commits on
                        # base). Treat exactly like the pr_is_not_behind
                        # short-circuit above: clear any prior retry
                        # counter and defer to the next sweep without
                        # escalating to fix-spawn.
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} not behind base "
                            f"(no new commits on base) — cached "
                            f"mergeable_state was stale, deferring to next CI sweep"
                        )
                        ci_loop["polls_since_pr_fetch"] = 999
                        ci_loop["head_sha"] = ""
                        ci_loop.pop("_behind_update_retries", None)
                        from sqlalchemy.orm.attributes import flag_modified

                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        logger.info(
                            f"[CI] PR #{ci_pr_number} behind base — "
                            f"update_branch reports no new commits on base, deferring to next sweep"
                        )
                        continue
                    # update_branch failed — likely a transient GitHub API error,
                    # not a real merge conflict. Defer to the next CI sweep with a
                    # bounded retry budget instead of immediately burning a fix
                    # iteration. Mirrors the rollup_management behind-update pattern.
                    _behind_retries = (
                        ci_loop.get("_behind_update_retries", 0) + 1
                    )
                    _behind_budget = _runtime_config.get(
                        "ci_behind_update_retries", 3
                    )
                    _upd_msg = _upd.get("message") or "unknown"
                    if _behind_retries < _behind_budget:
                        ci_loop["_behind_update_retries"] = _behind_retries
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} behind base — update_branch failed "
                            f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                        )
                        from sqlalchemy.orm.attributes import flag_modified

                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        logger.info(
                            f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                            f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                        )
                        continue
                    # Budget exhausted — alert, clear the counter, and fall through
                    # to fix spawn so a real engineer-style fix can resolve whatever
                    # GitHub can't auto-update.
                    await _maybe_create_failure_alert(
                        ci_task,
                        session,
                        f"PR #{ci_pr_number} behind base — update_branch failed "
                        f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn",
                    )
                    ci_loop.pop("_behind_update_retries", None)
                    _behind_fallthrough = True
                    logger.warning(
                        f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                        f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                    )
                elif _ms != "dirty":
                    # Non-dirty non-behind mergeable_state values (blocked,
                    # unstable, unknown, empty, or anything unrecognized) are
                    # all recoverable by waiting rather than spawning a fix VM.
                    # Track consecutive same-state defers so silent stalls
                    # surface an alert before the 30-min CI backstop kills the
                    # task with no diagnostic breadcrumbs.
                    _ms_key = _ms or "empty"
                    _is_unrecognized = _ms_key not in (
                        "blocked",
                        "unstable",
                        "unknown",
                    )
                    prev_state = ci_loop.get("_conflict_deferred_state")
                    deferred_count = (
                        ci_loop.get("_conflict_deferred_count", 0) + 1
                        if prev_state == _ms_key
                        else 1
                    )
                    ci_loop["_conflict_deferred_count"] = deferred_count
                    ci_loop["_conflict_deferred_state"] = _ms_key
                    alert_threshold = (
                        10 if _ms_key in ("blocked", "unstable") else 5
                    )
                    ci_task.latest_status_message = (
                        f"PR #{ci_pr_number} merge deferred "
                        f"(mergeable_state={_ms_key}, deferral {deferred_count}) — CI polling continues"
                    )
                    if deferred_count == alert_threshold:
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} stuck in mergeable_state={_ms_key} "
                            f"for {deferred_count} consecutive CI polls",
                        )
                    from sqlalchemy.orm.attributes import flag_modified

                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    flag_modified(ci_task, "context")
                    session.add(ci_task)
                    await session.commit()
                    _log = (
                        logger.warning if _is_unrecognized else logger.debug
                    )
                    _log(
                        f"[CI] PR #{ci_pr_number} mergeable=False, mergeable_state={_ms_key!r} "
                        f"(deferral {deferred_count}/alert@{alert_threshold}) — continuing CI polling, not spawning fix"
                    )
                    continue
                # Merge-queue double-fire guard: a proactive
                # update-branch from the rollup merge train landed
                # on this PR moments ago, and GitHub's mergeable
                # computation lags the new merge commit — a 'dirty'
                # read inside the freshness window is likely stale.
                # Defer the conflict-fix spawn to the next poll so
                # the queue and the agent path never double-fire; a
                # GENUINE conflict outlives the window and falls
                # through to the unchanged fix path below.
                if _ms == "dirty":
                    from app.rollup_state import (
                        _merge_queue_update_in_flight,
                    )

                    if _merge_queue_update_in_flight(ci_task.context):
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} reported dirty shortly "
                            f"after a merge-queue branch update — "
                            f"deferring conflict-fix spawn to next poll"
                        )
                        session.add(ci_task)
                        await session.commit()
                        logger.info(
                            f"[CI] PR #{ci_pr_number} dirty within the "
                            f"merge-queue update window — deferring fix spawn"
                        )
                        continue
                ci_payload = ci_task.first_payload
                # Resolve the fix-spawn head branch with a live-PR
                # fallback so a missing/stale stored branch can't
                # produce an empty FixPayload.branch (incident
                # 2026-05-28). See _resolve_ci_fix_branch.
                ci_branch = _resolve_ci_fix_branch(
                    ci_loop, ci_payload, pr_data
                )
                _orch_ci_project = _get_project(ci_task.project_id)
                ci_base = (
                    ci_loop.get("base_branch", "")
                    or ci_payload.get("base_branch", "")
                    or _shared_state.get_project_integration_branch(
                        _orch_ci_project
                    )
                )
                ci_orig_id = ci_loop.get(
                    "original_task_id", ""
                ) or ci_payload.get("original_task_id", "")
                # Peek at CI state for this PR's head SHA so the fix task's prompt
                # can describe BOTH blockers (conflicts + CI) if they coexist.
                # The mergeable state can flip between now and when the fix agent
                # runs — so we also tell the agent to re-verify before acting.
                ci_preview_section = ""
                try:
                    _conflict_head_sha = pr_data.get("head", {}).get(
                        "sha", ""
                    )
                    if _conflict_head_sha:
                        _conflict_ci_result = await get_check_runs_cached(
                            ci_gh_token,
                            ci_owner,
                            ci_repo,
                            _conflict_head_sha,
                        )
                        if _conflict_ci_result.get("status") == "completed":
                            _conflict_failed_logs = _conflict_ci_result.get(
                                "failed_logs", ""
                            )
                            if _conflict_failed_logs:
                                ci_preview_section = (
                                    f"\n\nAdditionally, CI checks are currently failing on this PR head "
                                    f"(the conflict is blocking a re-run, but these failures pre-date the conflict "
                                    f"or exist independently):\n```\n{_conflict_failed_logs[:1500]}\n```\n"
                                    f"After resolving conflicts, fix the CI failures above in the same push."
                                )
                except Exception as _ci_preview_exc:
                    logger.debug(
                        f"[CI] conflict-fix CI preview fetch failed for PR #{ci_pr_number}: {_ci_preview_exc}"
                    )
                # Pre-spawn ancestor recheck: the PR head may have advanced
                # (e.g. a sibling rollup) while update_branch retries were
                # running. If it's now not-behind, skip the fix spawn entirely
                # and clear the retry counter so the next sweep re-evaluates.
                if _behind_fallthrough:
                    _late_not_behind = await pr_is_not_behind_for(
                        ci_gh_token,
                        ci_owner,
                        ci_repo,
                        pr_detail=pr_data,
                        base_branch=ci_base,
                        project_id=ci_pid_str,
                        call_site="orchestrator_ci",
                    )
                    if _late_not_behind is True:
                        logger.info(
                            f"[CI] PR #{ci_pr_number} pr_is_not_behind=True at spawn time "
                            f"after update_branch retries exhausted — skipping fix spawn"
                        )
                        ci_loop.pop("_behind_update_retries", None)
                        from sqlalchemy.orm.attributes import flag_modified

                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        continue
                if _behind_fallthrough:
                    # The PR is merely BEHIND base, not conflicting — but the
                    # GitHub update-branch API failed across the retry budget,
                    # so a manual rebase is needed. Describe the actual state
                    # (behind, not conflicting) so the agent does not chase
                    # nonexistent conflict markers.
                    fix_prompt = (
                        f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                        f"Orchestrator observation at spawn time: GitHub reported the PR as BEHIND `{ci_base}` "
                        f"(no conflicts — just out-of-date). The orchestrator attempted to update the branch "
                        f"via the GitHub update-branch API across the retry budget but every attempt failed; "
                        f"a manual rebase is needed to unblock the merge.\n\n"
                        f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                        f"between spawn and execution. After cloning:\n"
                        f"  1. Run `git fetch origin && git rebase origin/{ci_base}`.\n"
                        f"  2. If no conflicts and no commits were replayed (git says 'Current branch "
                        f"is up to date'), the PR is already up-to-date — GitHub's mergeability flag "
                        f"is just stale. Report no changes needed (don't force-push); retries will "
                        f"clear it.\n"
                        f"  3. If the rebase surfaces unexpected conflicts (it shouldn't, given the "
                        f"behind state), check `git status` and `git diff --name-only --diff-filter=U` "
                        f"and resolve them.\n"
                        f"  4. If the branch state changed while you were working, inspect the latest "
                        f"failing checks via the orchestrator's read-only proxy "
                        f'(`curl -sS -H "X-Agent-Token: $AGENT_API_TOKEN" '
                        f'"$ORCHESTRATOR_URL/api/agent/github/check-runs?commit_sha=$(git rev-parse HEAD)" '
                        f"| jq '.data.details[]'`) so you only act on current CI failures.\n\n"
                        f"Primary action: rebase onto `{ci_base}` and force-push so the PR is no longer behind."
                        f"{ci_preview_section}\n\n"
                        f"Before declaring done: ensure (a) no conflict markers remain (if any surfaced), "
                        f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                        f"every fix — do not leave uncommitted changes."
                    )
                else:
                    fix_prompt = (
                        f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                        f"Orchestrator observation at spawn time: GitHub reported the PR as UNMERGEABLE "
                        f"(merge conflicts against `{ci_base}`).\n\n"
                        f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                        f"between spawn and execution (a concurrent rollup may have resolved the conflict, "
                        f"or new commits may have reintroduced one). After cloning:\n"
                        f"  1. Attempt `git rebase origin/{ci_base}` — note whether it succeeds cleanly.\n"
                        f"  2. Check `git status` and `git diff --name-only --diff-filter=U` for unmerged paths.\n"
                        f"  3. If neither shows conflicts, the premise is stale — inspect the latest "
                        f"failing checks via the orchestrator's read-only proxy "
                        f'(`curl -sS -H "X-Agent-Token: $AGENT_API_TOKEN" '
                        f'"$ORCHESTRATOR_URL/api/agent/github/check-runs?commit_sha=$(git rev-parse HEAD)" '
                        f"| jq '.data.details[]'`) and fix whatever is actually blocking the merge now.\n\n"
                        f"Primary action: rebase onto `{ci_base}` and resolve any conflicts."
                        f"{ci_preview_section}\n\n"
                        f"Before declaring done: ensure (a) no conflict markers remain in any file, "
                        f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                        f"every fix — do not leave uncommitted changes."
                    )
                if not ci_branch:
                    # Could not resolve a head branch from review_loop,
                    # payload, or the live PR. Skip the spawn (it would
                    # raise a FixPayload ValueError) and re-evaluate next
                    # cycle rather than wedge on a poison task.
                    logger.error(
                        f"[CI] PR #{ci_pr_number}: cannot resolve head branch "
                        f"for conflict-fix spawn (task {ci_task_id}) — skipping this cycle"
                    )
                    await session.rollback()
                    continue
                fix_task, fix_was_created = await _spawn_pipeline_task(
                    ci_task,
                    "fix",
                    session,
                    ci_pr_number,
                    ci_branch,
                    ci_base,
                    fix_prompt=fix_prompt,
                    original_task_id=ci_orig_id,
                )
                ci_task = await _lock_task_for_mutation(
                    session,
                    ci_task_id,
                    allowed_statuses=(TaskStatus.EVALUATING,),
                    reason="CI conflict result",
                )
                if ci_task is None:
                    continue
                ci_loop = dict(
                    (ci_task.context or {}).get("review_loop", {}) or {}
                )
                if fix_task:
                    from app.rollup_state import (
                        _is_rollup_parent_awaiting_merge,
                    )

                    ci_loop_ctx = dict(ci_loop)
                    ci_loop_ctx["ci_waiting"] = False
                    ci_loop_ctx["conflict_fix_spawned"] = str(fix_task.id)
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop_ctx,
                    }
                    # Rollup parents must stay in EVALUATING until the rollup PR
                    # actually merges — ci_completion will promote them via
                    # pr_merged=True. Marking COMPLETED here produces a ghost
                    # success while the fix task is still resolving conflicts.
                    if not _is_rollup_parent_awaiting_merge(
                        ci_task.context
                    ):
                        ci_task.status = TaskStatus.COMPLETED
                    if fix_was_created:
                        ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                        logger.info(
                            f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                        )
                    else:
                        ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}"
                        logger.info(
                            f"[CI] PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}"
                        )
                else:
                    # Fix not spawned — either iteration cap exhausted, or a
                    # different-type task (e.g. a review) is still running on
                    # the same chain and must finish first. In both cases the
                    # CI-wait sweep on the next tick will re-evaluate; only
                    # escalate to FAILED + requires_approval when we actually
                    # know the cap is exhausted.
                    msg = (ci_task.latest_status_message or "").lower()
                    deferred = "deferred" in msg
                    if deferred:
                        ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — waiting on in-flight pipeline task"
                        logger.info(
                            f"[CI] PR #{ci_pr_number} conflict-fix deferred — a different-type pipeline task is still running, will retry on next sweep"
                        )
                    else:
                        ci_task.status = TaskStatus.FAILED
                        if ci_task.vm_provisioned_at:
                            ci_task.vm_stopped_at = _utcnow()
                        # Gate only the approval flag — FAILED is the
                        # terminalization and stays. Gates off → no strand.
                        if _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            ci_task.requires_approval = True
                        ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — fix iterations exhausted, requires manual intervention"
                        logger.warning(
                            f"[CI] PR #{ci_pr_number} merge conflicts, fix not spawned — marking FAILED (requires_approval)"
                        )
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number} merge conflicts — fix iteration cap exhausted, manual intervention required",
                        )
                        from app.failure_classification import (
                            classify_failure,
                            persist_failure_category,
                        )

                        _fc_mc = classify_failure(ci_task)
                        persist_failure_category(ci_task, _fc_mc)
                        record_task_result(
                            success=False,
                            failure_class=_fc_mc,
                            task_id=str(ci_task.id),
                            task_type=(ci_task.first_payload).get(
                                "type", ""
                            ),
                        )
                session.add(ci_task)
                await session.commit()
                await broadcast_to_project(
                    str(ci_task.project_id),
                    "task_updated",
                    {"task_id": str(ci_task.id), "status": ci_task.status},
                )
                continue

            ci_head_sha = pr_data.get("head", {}).get("sha", "")
            if not ci_head_sha:
                ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                session.add(ci_task)
                await session.commit()
                await broadcast_to_project(
                    str(ci_task.project_id),
                    "task_updated",
                    {"task_id": str(ci_task.id), "status": ci_task.status},
                )
                logger.info(
                    f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle"
                )
                continue

            # ── Phase B3: release lock around get_check_runs_cached ──
            # Even with TTL caching, a cache miss makes a real GitHub
            # API call that can hang during rate-limit. Drop the lock
            # so concurrent heartbeats proceed.
            # R5-W2-A: content snapshot, not updated_at —
            # heartbeat onupdate would false-defer every poll.
            ci_pre_check_snapshot = _capture_ci_polling_snapshot(ci_task)
            ci_project_id_str = str(ci_task.project_id)
            await session.commit()
            # Dedup cached — siblings on the same head_sha collapse
            # into one underlying GitHub call within the 30s TTL.
            #
            # Strict mode (default): route through
            # ``ci_resolution.resolve_pr_ci_status`` so a bare branch
            # HEAD with 0 check-runs no longer fail-opens to
            # ``ci_passed=True``.  When the HEAD has no check-runs
            # the resolver falls back to the PR-event merge SHA
            # where the ``pull_request``-triggered build actually
            # ran; when neither path yields a completed build it
            # reports unresolved/pending so we keep polling rather
            # than try to merge against the absence of a signal.
            # ``ci_resolution_strict_mode=False`` is the operator
            # rollback handle to the legacy lenient path.
            from app.shared_state import (
                runtime_config as _rt_cfg,
            )

            # No-CI test-suite merge gate state for this poll. Set in
            # the no_checks branch when an EXTERNAL no-CI repo should
            # run its detected suite (verify_tests) before merging;
            # consumed in the locked block after the re-acquire below.
            _no_ci_verify_gate = False
            _no_ci_verify_base = ""
            # When a no-CI merge proceeds UNVERIFIED (immediate-pass
            # or undetected verify-verdict), record WHY so the
            # merge-confirmed block in handle_ci_completion can raise
            # exactly one operator alert. None = verified/normal path.
            _unverified_merge_reason = None

            if _rt_cfg.get("ci_resolution_strict_mode", True):
                from app import ci_resolution

                _resolved = await ci_resolution.resolve_pr_ci_status(
                    pr_number=ci_pr_number,
                    branch_sha=ci_head_sha,
                    token=ci_gh_token,
                    owner=ci_owner,
                    repo=ci_repo,
                    project_id=ci_project_id_str,
                )
                _r_state = _resolved["state"]
                _r_source = _resolved["source"]
                _terminal_ci_result = _ci_result_from_resolved_pr_ci_status(
                    _resolved
                )
                if _terminal_ci_result is not None:
                    ci_result = _terminal_ci_result
                elif _r_state == "no_checks":
                    # CI is *definitively* absent: the branch HEAD
                    # and every reachable PR-event SHA were queried
                    # cleanly and carry zero check-runs. For PRs
                    # targeting an internal staging branch
                    # (research/*, plan/*), CI is path-filtered out
                    # by design and will NEVER run — polling forever
                    # strands the rollup (incident 2026-05-28:
                    # 0-check PR wedged parent c4d3402e). Treat
                    # 0-checks on a staging base as CI-not-applicable
                    # → green so the orchestrator proceeds to the
                    # mergeability-gated merge (the merge attempt
                    # itself still enforces no-conflicts). CI-gated
                    # bases (main/automain) without checks keep
                    # polling — never merge on absence of signal.
                    _ci_base_ref = (pr_data.get("base") or {}).get(
                        "ref", ""
                    )
                    _ci_proj = _get_project(ci_task.project_id)
                    # A *definitive* no_checks proceeds to the
                    # mergeability-gated merge when the PR targets an
                    # internal staging branch (research/plan, CI
                    # path-filtered by design) OR the project declares
                    # it has no CI (build_config.requires_ci_checks=
                    # False) — otherwise (CI-gated repo, checks merely
                    # not registered yet) keep polling. See
                    # Project.proceed_on_no_checks.
                    if (
                        _ci_proj is not None
                        and ci_resolution.proceed_on_no_checks(
                            _ci_proj, _ci_base_ref
                        )
                    ):
                        _is_staging = _ci_proj.is_staging_branch(
                            _ci_base_ref
                        )
                        _gate_on = _rt_cfg.get(
                            "no_ci_test_gate_enabled", True
                        )
                        if ci_resolution.should_run_no_ci_test_gate(
                            _ci_proj,
                            _ci_base_ref,
                            gate_enabled=_gate_on,
                        ):
                            # EXTERNAL no-CI repo (requires_ci_checks=
                            # False) on a real base WITH a runnable
                            # suite: gate the merge on a verify_tests
                            # verdict instead of merging blind. Defer
                            # the spawn / verdict-await to the locked
                            # block after the re-acquire below; keep
                            # ci_result PENDING so the loop keeps
                            # polling until the verdict lands.
                            _no_ci_verify_gate = True
                            _no_ci_verify_base = _ci_base_ref
                            logger.info(
                                f"[CI] PR #{ci_pr_number} has 0 check-runs "
                                f"and the project declares no CI — gating "
                                f"the merge on the repo's detected test "
                                f"suite (verify_tests) before proceeding"
                            )
                            ci_result = {
                                "status": "pending",
                                "conclusion": None,
                                "details": _resolved.get("details", []),
                                "failed_logs": "",
                            }
                        else:
                            # Immediate-pass (unchanged legacy behavior).
                            # Internal staging base (CI path-filtered by
                            # design), kill-switch off, or NO detectable
                            # test command — an absent-tests repo must
                            # still progress, so it merges UNVERIFIED.
                            if _is_staging:
                                _ci_reason = (
                                    f"targets internal staging branch "
                                    f"'{_ci_base_ref}'"
                                )
                                _unverified_merge_reason = (
                                    "internal_staging_base"
                                )
                            elif not _gate_on:
                                _ci_reason = (
                                    "the project declares no CI "
                                    "(requires_ci_checks=False) and the "
                                    "no_ci_test_gate kill-switch is off"
                                )
                                _unverified_merge_reason = "kill_switch_off"
                            else:
                                # Before taking the unverified
                                # immediate-pass, attempt a bounded
                                # re-detection. A heuristic miss at
                                # onboarding time must not permanently
                                # disable the gate for this repo.
                                from app.build_config_detection import (
                                    maybe_redetect_missing_test_command,
                                )

                                # Re-fetch tracked ORM Project for the write path:
                                # maybe_redetect_missing_test_command modifies
                                # project.build_config (flag_modified + commit).
                                # The snapshot cache cannot provide a tracked
                                # object; this is a rare path (one re-detect per
                                # 6-hour cooldown per project).
                                _ci_proj_orm = (
                                    await session.get(
                                        Project, ci_task.project_id
                                    )
                                    if ci_task.project_id is not None
                                    else None
                                )
                                _redetected = await maybe_redetect_missing_test_command(
                                    _ci_proj_orm,
                                    session,
                                    ref=_ci_base_ref,
                                )
                                if _redetected:
                                    _ci_proj = (
                                        await _resnapshot_project(_ci_proj_orm)
                                        or _ci_proj
                                    )
                                if (
                                    _redetected
                                    and ci_resolution.should_run_no_ci_test_gate(
                                        _ci_proj,
                                        _ci_base_ref,
                                        gate_enabled=_gate_on,
                                    )
                                ):
                                    _no_ci_verify_gate = True
                                    _no_ci_verify_base = _ci_base_ref
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} has 0 "
                                        f"check-runs — re-detection found "
                                        f"a test command for external "
                                        f"no-CI repo; gating on "
                                        f"verify_tests instead of merging "
                                        f"UNVERIFIED"
                                    )
                                    ci_result = {
                                        "status": "pending",
                                        "conclusion": None,
                                        "details": _resolved.get(
                                            "details", []
                                        ),
                                        "failed_logs": "",
                                    }
                                else:
                                    _ci_reason = (
                                        "the project declares no CI "
                                        "(requires_ci_checks=False) and no "
                                        "test command is detectable — merging "
                                        "UNVERIFIED"
                                    )
                                    _unverified_merge_reason = (
                                        "no_detectable_test_command"
                                    )
                            if not _no_ci_verify_gate:
                                logger.info(
                                    f"[CI] PR #{ci_pr_number} has 0 check-runs and "
                                    f"{_ci_reason} — treating as passed so the "
                                    f"merge can proceed"
                                )
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "success",
                                    "details": _resolved.get("details", []),
                                    "failed_logs": "",
                                }
                    else:
                        ci_result = {
                            "status": "pending",
                            "conclusion": None,
                            "details": _resolved.get("details", []),
                            "failed_logs": "",
                        }
                elif _r_source == "unresolved":
                    # Transient: couldn't resolve a verdict (GitHub
                    # I/O error, or checks not yet visible). Keep
                    # polling — never merge on the absence of a
                    # signal.
                    ci_result = {
                        "status": "pending",
                        "conclusion": None,
                        "details": _resolved.get("details", []),
                        "failed_logs": "",
                    }
                else:
                    ci_result = {
                        "status": "in_progress",
                        "conclusion": None,
                        "details": _resolved.get("details", []),
                        "failed_logs": "",
                    }
            else:
                ci_result = await get_check_runs_cached(
                    ci_gh_token,
                    ci_owner,
                    ci_repo,
                    ci_head_sha,
                    project_id=ci_project_id_str,
                )
            # Re-acquire to apply downstream mutations.
            ci_task = await _lock_task_for_mutation(
                session,
                ci_task_id,
                allowed_statuses=(TaskStatus.EVALUATING,),
                reason="CI polling after check_runs fetch",
                expected_snapshot=ci_pre_check_snapshot,
                snapshot_fn=_capture_ci_polling_snapshot,
            )
            if ci_task is None:
                continue
            ci_loop = dict(
                (ci_task.context or {}).get("review_loop", {}) or {}
            )

            # ── No-CI test-suite merge gate (verify_tests) ──────────
            # Reached when the no_checks branch flagged an external
            # no-CI repo with a runnable suite. We hold the task lock
            # and a fresh ci_loop here. First poll: spawn verify_tests
            # on the PR head, record its id, keep polling. Subsequent
            # polls: once that task is terminal, read its latest
            # MERGE_TEST_RESULT verdict and set ci_result —
            #   pass          → completed/success (merge proceeds,
            #                   still subject to the review gate);
            #   fail          → completed/failure so the existing
            #                   handle_ci_completion fix path fires
            #                   carrying the failing-test output;
            #   detected=false / no usable verdict → completed/success
            #                   UNVERIFIED (absent-tests repos must
            #                   still progress — logged as such).
            # Kill-switch (no_ci_test_gate_enabled) was checked in the
            # no_checks branch; when off, _no_ci_verify_gate is never
            # set and today's immediate-pass applies.
            #
            # Escalation backstop continuation: a prior poll's
            # no-checks escalation may have promoted this no-CI strand
            # to the verify gate even though strict resolution never
            # returned a definitive no_checks (it stayed transient /
            # pending). Honor that sticky decision so the gate runs on
            # subsequent polls — otherwise the promotion would be lost
            # the moment we leave the no_checks branch.
            if not _no_ci_verify_gate and ci_loop.get(
                "no_ci_escalation_verify_gate"
            ):
                _no_ci_verify_gate = True
                _no_ci_verify_base = (
                    ci_loop.get("no_ci_escalation_base")
                    or _no_ci_verify_base
                )
            if _no_ci_verify_gate:
                from sqlalchemy.orm.attributes import flag_modified
                from app.review_pipeline import _spawn_pipeline_task
                from app.release_qualifier_audit import (
                    _latest_artifact_logs,
                )
                from app.models import ArtifactType

                # Mark active so the "no CI checks appeared" stall
                # escalation below is bypassed while we verify.
                ci_loop["ci_checks_seen"] = True
                _v_recorded = ci_loop.get("verify_tests_verdict")
                _v_task_id = ci_loop.get("verify_tests_task_id")
                _v_poll_interval = min(ci_poll_interval * 2, 90)

                if _v_recorded in ("pass", "undetected"):
                    # Verdict already obtained on a prior poll —
                    # proceed (idempotent until the merge lands).
                    ci_result = ci_resolution.build_no_ci_gate_ci_result(
                        _v_recorded
                    )
                elif _v_recorded == "fail":
                    # Already classified failed — belt-and-suspenders
                    # if we re-enter before ci_fix_spawned is set.
                    ci_result = ci_resolution.build_no_ci_gate_ci_result(
                        "fail", ci_loop.get("verify_tests_output", "")
                    )
                elif not _v_task_id:
                    # First encounter: spawn verify_tests on PR head.
                    _v_branch = _resolve_ci_fix_branch(
                        ci_loop, ci_task.first_payload, pr_data
                    )
                    if not _v_branch:
                        logger.error(
                            f"[CI] PR #{ci_pr_number}: cannot resolve "
                            f"head branch for verify_tests spawn — "
                            f"retrying next cycle"
                        )
                        ci_loop["ci_poll_interval"] = _v_poll_interval
                        ci_loop["ci_last_polled"] = _utcnow().isoformat()
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        continue
                    _v_orig = ci_loop.get(
                        "original_task_id", ""
                    ) or ci_task.first_payload.get("original_task_id", "")
                    _v_attempt = int(
                        ci_loop.get("verify_tests_retries", 0) or 0
                    )
                    _verify_task, _ = await _spawn_pipeline_task(
                        ci_task,
                        "verify_tests",
                        session,
                        ci_pr_number,
                        _v_branch,
                        _no_ci_verify_base,
                        original_task_id=_v_orig,
                        suppress_warm_handoff=True,
                        attempt=_v_attempt,
                    )
                    # _spawn_pipeline_task commits internally — re-lock
                    # the parent before mutating it.
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling after verify_tests spawn",
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict(
                        (ci_task.context or {}).get("review_loop", {}) or {}
                    )
                    ci_loop["ci_checks_seen"] = True
                    if _verify_task is not None:
                        ci_loop["verify_tests_task_id"] = str(
                            _verify_task.id
                        )
                        ci_task.latest_status_message = (
                            f"No CI on PR #{ci_pr_number} — running "
                            f"detected test suite to gate merge → "
                            f"{_verify_task.id}"
                        )
                        logger.info(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                            f"spawned verify_tests → {_verify_task.id}"
                        )
                    else:
                        _spawn_deferrals = (
                            ci_loop.get("verify_tests_spawn_deferrals", 0)
                            or 0
                        ) + 1
                        ci_loop["verify_tests_spawn_deferrals"] = (
                            _spawn_deferrals
                        )
                        if (
                            _spawn_deferrals
                            >= _VERIFY_TESTS_SPAWN_DEFERRAL_CAP
                            and not ci_task.requires_approval
                        ):
                            ci_task.status = TaskStatus.FAILED
                            if ci_task.vm_provisioned_at:
                                ci_task.vm_stopped_at = _utcnow()
                            if _runtime_config.get(
                                "require_approval_gates", True
                            ):
                                ci_task.requires_approval = True
                            ci_task.latest_status_message = (
                                f"No CI on PR #{ci_pr_number} — verify_tests "
                                f"spawn deferred {_spawn_deferrals} consecutive "
                                f"times — escalating for operator review"
                            )
                            await _maybe_create_failure_alert(
                                ci_task,
                                session,
                                f"PR #{ci_pr_number} no-CI verify_tests spawn "
                                f"deferred {_spawn_deferrals} consecutive cycles "
                                f"— escalating for operator review",
                            )
                            logger.warning(
                                f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                f"verify_tests spawn deferred "
                                f"{_spawn_deferrals} times — task "
                                f"{ci_task.id} FAILED (requires_approval)"
                            )
                        else:
                            ci_task.latest_status_message = (
                                f"No CI on PR #{ci_pr_number} — verify_tests "
                                f"spawn deferred ({_spawn_deferrals}/"
                                f"{_VERIFY_TESTS_SPAWN_DEFERRAL_CAP}), will retry"
                            )
                            logger.info(
                                f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                f"verify_tests spawn suppressed/deferred "
                                f"({_spawn_deferrals}/"
                                f"{_VERIFY_TESTS_SPAWN_DEFERRAL_CAP}) — retrying"
                            )
                    ci_loop["ci_poll_interval"] = _v_poll_interval
                    ci_loop["ci_last_polled"] = _utcnow().isoformat()
                    ci_loop["head_sha"] = ci_head_sha
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    flag_modified(ci_task, "context")
                    session.add(ci_task)
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    continue
                else:
                    # Await: verify task spawned on a prior poll.
                    _verify_task = None
                    try:
                        _verify_task = await session.get(
                            Task, uuid.UUID(_v_task_id)
                        )
                    except (ValueError, TypeError):
                        _verify_task = None
                    _terminal = {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }
                    if (
                        _verify_task is None
                        or _verify_task.status not in _terminal
                    ):
                        # Still running — keep polling.
                        ci_loop["ci_poll_interval"] = _v_poll_interval
                        ci_loop["ci_last_polled"] = _utcnow().isoformat()
                        ci_loop["head_sha"] = ci_head_sha
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} — awaiting detected-test "
                            f"verdict (verify_tests {_v_task_id[:8]})"
                        )
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(
                            str(ci_task.project_id),
                            "task_updated",
                            {
                                "task_id": str(ci_task.id),
                                "status": ci_task.status,
                            },
                        )
                        continue
                    # Terminal — read the latest verdict artifact.
                    # Only a COMPLETED verify task carries a usable
                    # verdict; a FAILED/CANCELLED one is treated as a
                    # crash (``_verdict`` stays None → crash-retry).
                    _verdict = None
                    if _verify_task.status == TaskStatus.COMPLETED:
                        _verdict = await _latest_artifact_logs(
                            session,
                            _verify_task.id,
                            ArtifactType.MERGE_TEST_RESULT,
                        )
                    # Peek (does NOT feed the verdict): a FAILED
                    # verify task that posted setupFailed=true could
                    # not run an EXISTING suite due to infra (dep
                    # install / runner spawn), distinct from a cold
                    # clone crash. Used only to label an
                    # exhausted-retry UNVERIFIED merge for operators.
                    _verify_setup_failed = False
                    if _verify_task.status == TaskStatus.FAILED:
                        _failed_artifact = await _latest_artifact_logs(
                            session,
                            _verify_task.id,
                            ArtifactType.MERGE_TEST_RESULT,
                        )
                        # Either an all-runners setup failure
                        # (setupFailed) or a partial one
                        # (partialSetupFailed — some runners ran but a
                        # sibling's environment broke) means a suite
                        # could not run due to infra, distinct from a
                        # cold-clone crash. Both get the setup-failed
                        # operator reason on an exhausted-retry merge.
                        _verify_setup_failed = bool(
                            isinstance(_failed_artifact, dict)
                            and (
                                _failed_artifact.get("setupFailed")
                                or _failed_artifact.get(
                                    "partialSetupFailed"
                                )
                            )
                        )
                        # The suite could not run, but the cheap
                        # acceptance probe DID run on the setup-failure
                        # fall-open and the changed code CLEARLY failed
                        # to compile/build (not a could-not-run degrade
                        # from the same environment problem). Feed the
                        # FAILED artifact as the verdict so it maps to a
                        # merge BLOCK + fix instead of the
                        # crash-retry-then-fall-open path — a genuine
                        # build break must not merge UNVERIFIED.
                        if isinstance(
                            _failed_artifact, dict
                        ) and ci_resolution.probe_is_clear_failure(
                            _failed_artifact.get("probe")
                        ):
                            _verdict = _failed_artifact
                    _outcome, _detail = (
                        ci_resolution.classify_merge_test_verdict(_verdict)
                    )
                    # Crash vs. genuinely-undetected split. A FAILED/
                    # CANCELLED verify task — or a COMPLETED one that
                    # posted no usable verdict artifact (``_verdict``
                    # is None) — did not actually run the suite: this
                    # is transient infra noise (most often a failed
                    # cold clone of the PR head). Re-spawn once before
                    # failing open. A detected=false verdict (a suite
                    # that ran and found nothing — ``_verdict`` is a
                    # dict) is NOT a crash and falls open immediately.
                    _is_verify_crash = (
                        _outcome == "undetected" and _verdict is None
                    )
                    _verify_retries = int(
                        ci_loop.get("verify_tests_retries", 0) or 0
                    )
                    if (
                        _is_verify_crash
                        and _verify_retries < _VERIFY_TESTS_MAX_RETRIES
                    ):
                        # Clear the crashed task id + verdict so the
                        # ``not _v_task_id`` branch re-spawns next
                        # poll (with attempt=verify_tests_retries for a
                        # distinct idempotency key).
                        ci_loop["verify_tests_retries"] = (
                            _verify_retries + 1
                        )
                        ci_loop.pop("verify_tests_task_id", None)
                        ci_loop.pop("verify_tests_verdict", None)
                        logger.warning(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                            f"verify_tests {_v_task_id[:8]} crashed "
                            f"(status={_verify_task.status}, no verdict "
                            f"artifact) — re-spawning "
                            f"(retry {_verify_retries + 1}/"
                            f"{_VERIFY_TESTS_MAX_RETRIES}) before "
                            f"failing open"
                        )
                        ci_loop["ci_poll_interval"] = _v_poll_interval
                        ci_loop["ci_last_polled"] = _utcnow().isoformat()
                        ci_loop["head_sha"] = ci_head_sha
                        ci_task.latest_status_message = (
                            f"PR #{ci_pr_number} — verify_tests crashed, "
                            f"retrying detected-test gate "
                            f"({_verify_retries + 1}/"
                            f"{_VERIFY_TESTS_MAX_RETRIES})"
                        )
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(
                            str(ci_task.project_id),
                            "task_updated",
                            {
                                "task_id": str(ci_task.id),
                                "status": ci_task.status,
                            },
                        )
                        continue
                    ci_loop["verify_tests_verdict"] = _outcome
                    # Targeted declared-acceptance-test green signal (additive,
                    # never changes the merge outcome). When the agent
                    # independently ran the SPECIFIC declared acceptance test as
                    # a TARGETED run and it exited green, stamp a durable signal
                    # onto the ci_task's review_loop so the landing path
                    # (mark_landed_on_integration -> record_code_acceptance_verdict)
                    # can mint proven=True for the named_test even when the
                    # full-suite merge fell open UNVERIFIED. This project's task
                    # PRs target automain (relaxed CI), so the full-suite gate
                    # legitimately falls open benign; the targeted green run is
                    # honest machine-checked proof of the exact claim. Defensive:
                    # an older agent that omits the ``passed`` field yields no
                    # green signal (acceptance_test_is_green returns False) and
                    # today's behavior is preserved. Never blocks; only records.
                    _accept_block = ci_resolution.acceptance_test_block(_verdict)
                    if ci_resolution.acceptance_test_is_green(_accept_block):
                        ci_loop["acceptance_test_targeted_green"] = True
                        _accept_desc = str(
                            _accept_block.get("selector")
                            or _accept_block.get("descriptor")
                            or ""
                        ).strip()
                        if _accept_desc:
                            ci_loop["acceptance_test_targeted_descriptor"] = (
                                _accept_desc
                            )
                        logger.info(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: declared "
                            f"acceptance test ran GREEN via targeted execution "
                            f"({_accept_desc or 'unnamed'}) — stamping proven "
                            f"signal for the named_test acceptance gate"
                        )
                    # Extract coverage data from the verify-tests verdict for the
                    # coverage_delta acceptance gate. The MERGE_TEST_RESULT artifact
                    # carries a ``coverage`` block with test-count fields
                    # (passed/skipped/total). When the test runner includes coverage
                    # instrumentation (pytest-cov or c8), an additional
                    # ``line_coverage_pct`` field carries the actual line/statement
                    # coverage percentage from the instrumentation tool — prefer this
                    # over the test-pass-rate fallback computed from passed/total.
                    if isinstance(_verdict, dict):
                        _cov = _verdict.get("coverage")
                        if isinstance(_cov, dict):
                            # Prefer line_coverage_pct from coverage
                            # instrumentation (pytest-cov / c8) when present.
                            _line_cov = _cov.get("line_coverage_pct")
                            if (
                                isinstance(_line_cov, (int, float))
                                and not isinstance(_line_cov, bool)
                            ):
                                ci_loop["test_coverage_result"] = {
                                    "coverage_pct": round(
                                        float(_line_cov), 2
                                    )
                                }
                            else:
                                _total = _cov.get("total", 0) or 0
                                _passed = _cov.get("passed", 0) or 0
                                if _total > 0:
                                    ci_loop["test_coverage_result"] = {
                                        "coverage_pct": round(
                                            (_passed / _total) * 100.0, 2
                                        ),
                                        "source": "pass_rate",
                                    }
                    # Auto-heal a stale stored test command: when the
                    # verify run discovered the stored command was
                    # stale and ran a real replacement suite, persist
                    # the corrected per-directory command back into the
                    # project's build_config so FUTURE merges use it
                    # instead of re-discovering it every poll. This is
                    # additive and best-effort — it never alters the
                    # pass/fail/undetected merge decision and runs
                    # regardless of the replacement suite's outcome (a
                    # recovered-but-failing suite still means the stored
                    # command should be corrected).
                    if isinstance(_verdict, dict) and _verdict.get(
                        "recovered"
                    ):
                        _recovered_dirs = (
                            _verdict.get("recovered_directories") or []
                        )
                        if _recovered_dirs:
                            from app.build_config_detection import (
                                persist_recovered_test_command,
                            )

                            # Re-fetch tracked ORM Project for the write path:
                            # persist_recovered_test_command modifies
                            # project.build_config (flag_modified + commit).
                            # The snapshot cache cannot provide a tracked
                            # object; this is a rare recovery path.
                            _recover_proj = (
                                await session.get(
                                    Project, ci_task.project_id
                                )
                                if ci_task.project_id is not None
                                else None
                            )
                            if _recover_proj is not None:
                                _persisted = (
                                    await persist_recovered_test_command(
                                        _recover_proj,
                                        session,
                                        _recovered_dirs,
                                    )
                                )
                                if _persisted:
                                    ci_loop["recovered_test_command"] = (
                                        _recovered_dirs
                                    )
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} no-CI "
                                        f"merge gate: stored test command "
                                        f"was stale — persisted recovered "
                                        f"command(s) for "
                                        f"{len(_recovered_dirs)} "
                                        f"directory(ies) to converge "
                                        f"build_config"
                                    )
                    if _outcome == "pass":
                        logger.info(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                            f"detected test suite PASSED — proceeding to "
                            f"merge (subject to review gate)"
                        )
                    elif _outcome == "fail":
                        logger.warning(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                            f"detected test suite FAILED — blocking "
                            f"merge, spawning fix with failing output"
                        )
                        ci_loop["verify_tests_output"] = _detail[:4000]
                    else:
                        # Fall open to proceed-without-checks (merge
                        # UNVERIFIED). Two paths reach here:
                        #   - detected=false: a suite ran and found no
                        #     runnable command — an absent-tests repo
                        #     must still progress.
                        #   - crash with retries exhausted: the verify
                        #     task never produced a verdict even after
                        #     ``_VERIFY_TESTS_MAX_RETRIES`` re-spawns.
                        _crash_exhausted = (
                            _is_verify_crash
                            and _verify_retries >= _VERIFY_TESTS_MAX_RETRIES
                        )
                        # A setup-failure crash (suite exists but
                        # could not run) gets a distinct operator
                        # reason from a cold-clone crash or a genuine
                        # no-suite repo, so the resulting UNVERIFIED
                        # merge is diagnosable.
                        if _crash_exhausted and _verify_setup_failed:
                            _reason_phrase = (
                                f"verify_tests setup failed and retries "
                                f"exhausted ({_verify_retries}/"
                                f"{_VERIFY_TESTS_MAX_RETRIES}) — could "
                                f"not run an existing suite"
                            )
                            _unverified_merge_reason = "verify_setup_failed"
                        elif _crash_exhausted:
                            _reason_phrase = (
                                f"verify_tests crashed and retries "
                                f"exhausted ({_verify_retries}/"
                                f"{_VERIFY_TESTS_MAX_RETRIES})"
                            )
                            _unverified_merge_reason = "undetected_runtime"
                        else:
                            # detected=false fall-open. Refine the
                            # operator-visible reason: a suite that
                            # was PRESENT but skipped every test (per
                            # the verdict's noTestsReason) gets a
                            # distinct key/phrase from a genuine
                            # no-suite repo. Decision is unchanged.
                            (
                                _reason_phrase,
                                _unverified_merge_reason,
                            ) = ci_resolution.classify_unverified_fall_open_reason(
                                _verdict
                            )
                        logger.warning(
                            f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                            + _reason_phrase
                            + f" (verify_tests {_v_task_id[:8]} status="
                            f"{_verify_task.status}) — merge proceeding "
                            f"UNVERIFIED"
                        )
                    ci_result = ci_resolution.build_no_ci_gate_ci_result(
                        _outcome, _detail
                    )
                    # Persist the recorded verdict before
                    # handle_ci_completion re-reads task.context.
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    flag_modified(ci_task, "context")
                    session.add(ci_task)
                    # Fall through: ci_result is "completed" so the
                    # pending-skip below is bypassed and
                    # handle_ci_completion runs.

            # Track whether CI checks have ever been observed for this PR
            ci_details = ci_result.get("details", [])
            if ci_details and not ci_loop.get("ci_checks_seen"):
                ci_loop["ci_checks_seen"] = True
                ci_loop["ci_first_check_seen_at"] = _utcnow().isoformat()

            # Detect stalled CI: no checks appeared after reasonable wait
            if not ci_loop.get("ci_checks_seen") and not ci_details:
                _cws = ci_loop.get("ci_wait_started")
                if _cws:
                    try:
                        _no_checks_elapsed = (
                            _utcnow() - datetime.fromisoformat(_cws)
                        ).total_seconds()
                        if _no_checks_elapsed > 600 and not ci_loop.get(
                            "no_checks_escalated"
                        ):
                            ci_loop["no_checks_escalated"] = True
                            _mins = int(_no_checks_elapsed / 60)
                            # ── No-checks escalation backstop ──────
                            # Instead of resigning to wait forever for
                            # a manual merge, try to AUTO-RESOLVE a
                            # no-CI strand. Strictly gated on
                            # proceed_on_no_checks so a CI-gated repo
                            # (has_ci=True or has_ci=None/unknown) is
                            # never auto-merged — it keeps today's
                            # alert-and-wait.
                            from app import ci_resolution as _ci_res

                            _esc_base = (pr_data.get("base") or {}).get(
                                "ref", ""
                            ) or ci_loop.get("base_branch", "")
                            _esc_proj = _get_project(ci_task.project_id)
                            _gate_on = _rt_cfg.get(
                                "no_ci_test_gate_enabled", True
                            )
                            # Self-heal stale has_ci=None: a genuine
                            # no-CI repo whose onboarding detection was
                            # incomplete stores has_ci=None, so
                            # proceed_on_no_checks is False and it
                            # strands forever. Attempt ONE bounded
                            # best-effort re-detect; if it flips has_ci
                            # to a definitive False the action below
                            # resolves the strand on this very poll.
                            if (
                                _esc_proj is not None
                                and not _esc_proj.is_staging_branch(
                                    _esc_base
                                )
                                and not _ci_res.proceed_on_no_checks(
                                    _esc_proj, _esc_base
                                )
                                and (_esc_proj.build_config or {}).get(
                                    "has_ci"
                                )
                                is None
                            ):
                                from app.build_config_detection import (
                                    maybe_redetect_ci_presence,
                                )

                                # Re-fetch tracked ORM Project for the write
                                # path: maybe_redetect_ci_presence modifies
                                # project.build_config (flag_modified + commit).
                                _esc_proj_orm = (
                                    await session.get(
                                        Project, ci_task.project_id
                                    )
                                    if ci_task.project_id is not None
                                    else None
                                )
                                _esc_redetected = await maybe_redetect_ci_presence(
                                    _esc_proj_orm,
                                    session,
                                    ref=_esc_base,
                                )
                                if _esc_redetected:
                                    _esc_proj = (
                                        await _resnapshot_project(_esc_proj_orm)
                                        or _esc_proj
                                    )
                            _esc_action = (
                                _ci_res.resolve_no_checks_escalation(
                                    _esc_proj,
                                    _esc_base,
                                    gate_enabled=_gate_on,
                                )
                                if _esc_proj is not None
                                else "keep_polling"
                            )
                            from sqlalchemy.orm.attributes import (
                                flag_modified as _esc_flag_modified,
                            )

                            if _esc_action == "verify_tests":
                                # Promote to the no-CI verify gate; the
                                # sticky marker is honored by the gate
                                # block on the next poll.
                                ci_loop["no_ci_escalation_verify_gate"] = (
                                    True
                                )
                                ci_loop["no_ci_escalation_base"] = _esc_base
                                ci_task.latest_status_message = (
                                    f"No CI checks on PR #{ci_pr_number} "
                                    f"after {_mins}min — auto-resolving "
                                    f"no-CI strand via verify_tests"
                                )
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                _esc_flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                continue
                            elif _esc_action == "merge_unverified":
                                # Truly test-less (or kill-switch off /
                                # staging base): merge UNVERIFIED,
                                # mirroring the definitive-no_checks
                                # immediate-pass reason keys.
                                if _esc_proj.is_staging_branch(_esc_base):
                                    _unverified_merge_reason = (
                                        "internal_staging_base"
                                    )
                                elif not _gate_on:
                                    _unverified_merge_reason = (
                                        "kill_switch_off"
                                    )
                                else:
                                    _unverified_merge_reason = (
                                        "no_detectable_test_command"
                                    )
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "success",
                                    "details": [],
                                    "failed_logs": "",
                                }
                                ci_task.latest_status_message = (
                                    f"No CI checks on PR #{ci_pr_number} "
                                    f"after {_mins}min — auto-resolving "
                                    f"no-CI strand (merging UNVERIFIED)"
                                )
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                _esc_flag_modified(ci_task, "context")
                                session.add(ci_task)
                                # Fall through: ci_result is completed
                                # so handle_ci_completion merges below.
                            else:
                                # keep_polling — CI-gated repo: preserve
                                # today's alert-once-and-wait behavior.
                                ci_task.latest_status_message = (
                                    f"No CI checks appeared on PR #{ci_pr_number} after "
                                    f"{_mins}min — may need manual trigger"
                                )
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                session.add(ci_task)
                                await _maybe_create_failure_alert(
                                    ci_task,
                                    session,
                                    f"PR #{ci_pr_number}: no CI checks after {_mins}min",
                                )
                                await session.commit()
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(
                                        TaskStatus.EVALUATING,
                                    ),
                                    reason="CI polling after no-checks alert",
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict(
                                    (ci_task.context or {}).get(
                                        "review_loop", {}
                                    )
                                    or {}
                                )
                    except (ValueError, TypeError):
                        pass

            if ci_result["status"] != "completed":
                details = [
                    d["name"]
                    for d in ci_details
                    if d.get("status") in ("queued", "in_progress")
                ]
                ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                # Smarter backoff: faster when CI is actively running
                if ci_loop.get("escalated"):
                    max_interval = 60  # Post-escalation: poll frequently
                elif ci_loop.get("ci_checks_seen"):
                    max_interval = 90  # Checks running: moderate backoff
                else:
                    max_interval = 120  # No checks yet: cautious backoff
                ci_loop["ci_poll_interval"] = min(
                    ci_poll_interval * 2, max_interval
                )
                ci_loop["ci_last_polled"] = _utcnow().isoformat()
                ci_loop["head_sha"] = ci_head_sha
                ci_task.context = {
                    **(ci_task.context or {}),
                    "review_loop": ci_loop,
                }
                # JSONB nested-dict mutation needs flag_modified —
                # otherwise ci_last_polled/poll_interval don't
                # persist and the per-task 30s skip never engages,
                # so the orchestrator hammers GitHub every 2s.
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(ci_task, "context")
                session.add(ci_task)
                await session.commit()
                await broadcast_to_project(
                    str(ci_task.project_id),
                    "task_updated",
                    {"task_id": str(ci_task.id), "status": ci_task.status},
                )
                continue

            # ── Opt-in honest-block for DEGRADED unverified merges ──
            # Default-OFF per-project policy
            # (build_config.block_degraded_unverified_merge). When ON
            # and the no-CI gate fell open with a DEGRADED reason (it
            # EXPECTED to verify but could NOT run the suite), HOLD the
            # PR open instead of merging it UNVERIFIED. Benign
            # fall-opens (nothing to verify) and policy-OFF projects
            # take no branch here and merge exactly as today, so
            # default behavior is byte-for-byte unchanged. We do NOT
            # spawn a fix (no suite ran → there is no failing test to
            # fix) — we park the gate and surface a distinct,
            # operator-visible degraded-block signal (a recorded block
            # reason on merge_gate_runtime plus one alert). Subsequent
            # polls re-evaluate, re-park, and do NOT re-alert (sticky
            # ci_loop marker), so the PR stays held without spam.
            if _unverified_merge_reason:
                from app import ci_resolution as _blk_ci_res

                _block_proj = (
                    (
                        await session.execute(
                            select(Project)
                            .where(Project.id == ci_task.project_id)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .first()
                    if ci_task.project_id
                    else None
                )
                if _blk_ci_res.should_block_degraded_unverified_merge(
                    _block_proj, _unverified_merge_reason
                ):
                    from sqlalchemy.orm.attributes import (
                        flag_modified as _blk_flag_modified,
                    )

                    # Record the block + raise the alert ONCE per PR
                    # strand. Subsequent parking polls re-enter here
                    # (the verdict stays degraded) but must not inflate
                    # the block tally or re-alert — the sticky marker
                    # gates both.
                    _already_blocked = bool(
                        ci_loop.get("no_ci_degraded_blocked")
                    )
                    if not _already_blocked:
                        _blk_ci_res.record_no_ci_gate_degraded_block(
                            session,
                            _block_proj,
                            unverified_reason=_unverified_merge_reason,
                            when=_utcnow(),
                        )
                    ci_loop["no_ci_degraded_blocked"] = True
                    ci_loop["no_ci_degraded_block_reason"] = (
                        _unverified_merge_reason
                    )
                    ci_task.latest_status_message = (
                        f"PR #{ci_pr_number}: no-CI merge gate could "
                        f"not verify the suite "
                        f"({_unverified_merge_reason}); the project's "
                        f"block-degraded-unverified-merge policy is ON "
                        f"— holding the PR open instead of merging "
                        f"UNVERIFIED"
                    )
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    _blk_flag_modified(ci_task, "context")
                    session.add(ci_task)
                    if not _already_blocked:
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number}: no-CI merge gate "
                            f"BLOCKED an unverified merge (degraded "
                            f"reason: {_unverified_merge_reason}) per "
                            f"the project's opt-in honest-block "
                            f"policy — the suite could not run, so "
                            f"the PR is held open for operator "
                            f"attention",
                        )
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    continue

            # ── Opt-in honest-block for no-detectable-test merges ──
            # Default-OFF per-project policy
            # (build_config.block_no_test_unverified_merge). When ON
            # and the no-CI gate fell open with
            # ``no_detectable_test_command`` (a BENIGN reason — the
            # repo has no detectable suite), HOLD the PR open instead
            # of merging it UNVERIFIED. DEGRADED reasons and
            # policy-OFF projects take no branch here, so default
            # behavior is byte-for-byte unchanged. Sticky
            # ci_loop marker prevents re-alerting across polls.
            if _unverified_merge_reason:
                if _block_proj is None and ci_task.project_id:
                    _block_proj = (
                        await session.execute(
                            select(Project)
                            .where(Project.id == ci_task.project_id)
                            .with_for_update()
                        )
                    ).scalars().first()
                from app import ci_resolution as _nt_ci_res

                if _nt_ci_res.should_block_no_test_unverified_merge(
                    _block_proj, _unverified_merge_reason
                ):
                    from sqlalchemy.orm.attributes import (
                        flag_modified as _nt_flag_modified,
                    )

                    _already_nt_blocked = bool(
                        ci_loop.get("no_ci_no_test_blocked")
                    )
                    if not _already_nt_blocked:
                        _nt_ci_res.record_no_ci_gate_no_test_block(
                            session,
                            _block_proj,
                            unverified_reason=_unverified_merge_reason,
                            when=_utcnow(),
                        )
                    ci_loop["no_ci_no_test_blocked"] = True
                    ci_loop["no_ci_no_test_block_reason"] = (
                        _unverified_merge_reason
                    )
                    ci_task.latest_status_message = (
                        f"PR #{ci_pr_number}: no-CI merge gate found "
                        f"no detectable test command "
                        f"({_unverified_merge_reason}); the project's "
                        f"block-no-test-unverified-merge policy is ON "
                        f"— holding the PR open instead of merging "
                        f"UNVERIFIED"
                    )
                    ci_task.context = {
                        **(ci_task.context or {}),
                        "review_loop": ci_loop,
                    }
                    _nt_flag_modified(ci_task, "context")
                    session.add(ci_task)
                    if not _already_nt_blocked:
                        await _maybe_create_failure_alert(
                            ci_task,
                            session,
                            f"PR #{ci_pr_number}: no-CI merge gate "
                            f"BLOCKED an unverified merge (no-test "
                            f"reason: {_unverified_merge_reason}) per "
                            f"the project's opt-in no-test-block "
                            f"policy — no test command was detectable, "
                            f"so the PR is held open for operator "
                            f"attention",
                        )
                    await session.commit()
                    await broadcast_to_project(
                        str(ci_task.project_id),
                        "task_updated",
                        {
                            "task_id": str(ci_task.id),
                            "status": ci_task.status,
                        },
                    )
                    continue

            # CI completed — delegate to shared handler
            from app.ci_completion import handle_ci_completion

            # Thread the UNVERIFIED-merge reason (if any) into the
            # task context so the merge-confirmed block raises the
            # operator alert against an ACTUAL merge — not a merely
            # opened gate. flag_modified: nested JSONB dict mutation.
            if _unverified_merge_reason:
                from sqlalchemy.orm.attributes import flag_modified

                ci_loop["unverified_merge_reason"] = (
                    _unverified_merge_reason
                )
                ci_task.context = {
                    **(ci_task.context or {}),
                    "review_loop": ci_loop,
                }
                flag_modified(ci_task, "context")
                session.add(ci_task)

            # Persist a Project-level record of what the no-CI merge
            # gate ACTUALLY did this poll so an operator can tell a
            # config that claims tests are gated from one whose live
            # path fell open to an UNVERIFIED merge. Only the no-CI
            # gate outcomes record here (verify gate ran, or an
            # immediate-pass set an unverified reason); normal
            # green/red CI leaves both flags falsy and is skipped.
            # executed_real_tests is True only when verify_tests
            # produced a real pass/fail verdict. Best-effort: the
            # helper never raises, so it cannot break the merge path.
            # Sticky marker mirrors the sibling block-recorders: a
            # re-polled still-EVALUATING strand must not inflate
            # recent_outcomes or unverified_reason_counts a second time
            # for the same gate event.
            if (_no_ci_verify_gate or _unverified_merge_reason) and not ci_loop.get(
                "no_ci_runtime_recorded"
            ):
                from app import ci_resolution

                _rt_proj = (
                    (
                        await session.execute(
                            select(Project)
                            .where(Project.id == ci_task.project_id)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .first()
                    if ci_task.project_id
                    else None
                )
                await ci_resolution.arecord_no_ci_gate_runtime(
                    session,
                    _rt_proj,
                    executed_real_tests=(
                        ci_loop.get("verify_tests_verdict")
                        in ("pass", "fail")
                    ),
                    unverified_reason=_unverified_merge_reason,
                    when=_utcnow(),
                )
                ci_loop["no_ci_runtime_recorded"] = True

            logger.info(
                f"[CI] Poll fallback: CI completed for PR #{ci_pr_number}, processing"
            )
            await handle_ci_completion(
                ci_task,
                ci_result,
                ci_gh_token,
                ci_owner,
                ci_repo,
                session,
                source="poll",
            )
        except Exception as e:
            # ci_task may be expired after handle_ci_completion's
            # internal commit path; accessing ci_task.id would fire
            # a sync lazy-load on the async session and raise
            # MissingGreenlet. Use the loop-variable id (already a
            # plain UUID/str from the SELECT projection) instead.
            logger.exception(
                f"[CI] Error checking CI for task {ci_task_id}"
            )
            try:
                # Re-fetch ci_task fresh inside the recovery: if the
                # outer exception was a connection-class failure, the
                # session was rolled back and every loaded attribute
                # is expired — accessing ci_task.context / .id below
                # would lazy-load on a poisoned connection and crash
                # this handler too. ``_lock_task_for_mutation`` uses
                # ``populate_existing=True`` which gives us a live
                # row; a None return means another worker took the
                # task or the row is gone, in which case we have
                # nothing to mutate. (Production 2026-05-08:
                # orchestrator.py:4750 + models.py:700 cascade.)
                try:
                    await session.rollback()
                except Exception:
                    logger.debug(
                        "[CI] Pre-recovery rollback raised, continuing"
                    )
                ci_task = await _lock_task_for_mutation(
                    session,
                    ci_task_id,
                    reason="CI polling error recovery",
                )
                if ci_task is None:
                    logger.warning(
                        f"[CI] Skipping error recovery for task {ci_task_id}: row not lockable"
                    )
                    continue
                status_code = getattr(e, "status_code", None)
                if status_code == 404:
                    ci_task.status = TaskStatus.FAILED
                    if ci_task.vm_provisioned_at:
                        ci_task.vm_stopped_at = _utcnow()
                    ci_task.latest_status_message = "CI polling failed: PR not found (404) — PR may have been deleted"
                    _pr_num_404 = (
                        (ci_task.context or {})
                        .get("review_loop", {})
                        .get("pr_number")
                    )
                    logger.exception(
                        f"[CI] PR #{_pr_num_404} returned 404 — marking task {ci_task_id} FAILED"
                    )
                    await _maybe_create_failure_alert(
                        ci_task,
                        session,
                        "CI polling failed: PR returned 404 — PR may have been deleted or archived",
                    )
                    from app.failure_classification import (
                        persist_failure_category as _pfc404,
                    )

                    _pfc404(ci_task, "EXTERNAL_API")
                    record_task_result(
                        success=False,
                        failure_class="EXTERNAL_API",
                        task_id=str(ci_task_id),
                        task_type=(ci_task.first_payload).get("type", ""),
                    )
                else:
                    ci_task.latest_status_message = (
                        f"CI check error: {type(e).__name__}: {e!r}"[:500]
                    )
                session.add(ci_task)
                await session.commit()
            except Exception:
                logger.exception(
                    f"[CI] Failed to update ci_task status after error for task {ci_task_id}"
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        f"[CI] Rollback also failed for task {ci_task_id}"
                    )
