"""PR-state-check-pending handler, extracted from ``app.recovery_evaluating``.

A single self-contained per-row recovery branch lifted out of the
``_check_evaluating_timeouts`` Phase-B loop. It handles EVALUATING parents
whose agent-status recovery deferred a GitHub PR state check
(``context._pr_state_check_pending=True``) because the post-GH lock
re-acquire raced a concurrent writer.

The block:

  * Extracts the PR number from context or status_log.
  * Applies a 5-min grace period to avoid racing the original recovery's lock.
  * Makes a GitHub PR lookup to determine state (merged / closed / open).
  * On merged: calls ``_apply_rollup_merge_classification`` and sets a
    ``_recovery_merge_refresh_needed`` flag so direction-outcome refresh runs
    after the per-row commit.
  * On closed-unmerged: transitions task to FAILED.
  * On open: spawns a review via ``_spawn_pipeline_task``, re-staging the
    marker on spawn failure so the consumer self-retries next sweep.
  * Commits, heals direction outcomes if merge occurred, broadcasts, and
    signals ``True`` so the caller ``continue``s.

``_handle_pr_state_check_pending`` returns ``True`` when it handled the row
(the caller ``continue``s) or ``False`` to fall through to the remaining
recovery branches unchanged (flag absent or too-soon grace window). The move
is purely structural — every commit / notification / classification semantic
is preserved byte-for-byte. ``services.*`` refs and function-local deferred
imports are kept unchanged so test patches against ``app.services.<name>``,
``app.routers.agent.*``, ``app.github.*``, ``app.review_pipeline.*``, etc.
still take effect.
"""

import contextlib
import logging
import re
from datetime import datetime

import app.shared_state as _shared_state
from app import services
from app.models import Task, TaskStatus
from app.recovery_evaluating_helpers import _apply_rollup_merge_classification

logger = logging.getLogger(__name__)


async def _handle_pr_state_check_pending(
    task,
    ctx,
    session,
    now,
    _deploy_state,
    _heartbeat,
    maybe_create_failure_alert,
) -> bool:
    """PR-state-check-pending consumer for EVALUATING parents.

    Returns ``True`` when the row was handled (caller ``continue``s) and
    ``False`` to fall through to the remaining recovery branches — namely
    when the ``_pr_state_check_pending`` flag is absent, or the 5-min grace
    window has not yet elapsed.
    """
    if not ctx.get("_pr_state_check_pending"):
        return False

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
        return False

    from sqlalchemy.orm.attributes import flag_modified

    _ctx2 = dict(ctx)
    _ctx2.pop("_pr_state_check_pending", None)
    # Set when this recovery records a real integration landing
    # (out-of-band merge), so the direction-outcome refresh runs
    # after the per-row commit. Re-fetched in a fresh session below
    # because the per-row commit can expire ``task`` (and this row
    # is loaded with load_only).
    _recovery_merge_refresh_needed = False
    # Capture the task id for the direction-outcome refresh below
    # (``task`` may be expired by the per-row commit).
    _eval_task_id = task.id

    if not pr_num:
        # No PR number recoverable — clear flag, fall through to
        # the normal evaluating timeout logic below.
        task.context = _ctx2
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        return False
    else:
        gh_pr_state = "open"
        gh_pr_merged = False
        try:
            from app.routers.agent import get_github_token_for_task
            from app.github import get_pull_request

            # Audit P1: get_github_token_for_task runs DB I/O on
            # ``session``. ``session`` is now this ROW's own
            # short-lived session, so a _bounded() timeout
            # cancellation mid-query that leaves it in
            # PendingRollback is confined to this iteration — the
            # per-row try/except below logs+skips and the
            # per-row ``async with`` closes the poisoned session;
            # the next row gets a clean one.
            gh_token, owner, repo = await services._bounded(
                get_github_token_for_task(task.id, session),
                services._GH_CALL_TIMEOUT_S,
                what=f"eval-timeout token {str(task.id)[:8]}",
            )
            pr_data = await services._bounded(
                get_pull_request(gh_token, owner, repo, pr_num),
                services._GH_CALL_TIMEOUT_S,
                what=f"eval-timeout get_pull_request #{pr_num}",
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
            from app.models import Project

            _proj = await session.get(Project, task.project_id)
            (
                record_pr_merged,
                terminal_rollup_merge,
                landed_branch,
            ) = await _apply_rollup_merge_classification(
                task,
                _proj,
                session,
                pr_number=pr_num,
                merged_base_branch=(pr_data.get("base") or {}).get("ref"),
                source="services._check_evaluating_timeouts",
                msg_terminal=(
                    f"Recovered _pr_state_check_pending: PR "
                    f"#{pr_num} merged out-of-band"
                ),
                msg_non_terminal=(
                    f"Recovered _pr_state_check_pending: PR "
                    f"#{pr_num} merged into {{landed}}; "
                    "awaiting downstream landing"
                ),
                hotfix_lane=bool(
                    (task.first_payload or {}).get("hotfix") or _ctx2.get("hotfix")
                ),
                ctx_override=_ctx2,
                non_merge_reason="merged_pr_non_integration_branch",
                merge_commit_sha=pr_data.get("merge_commit_sha"),
            )
            if record_pr_merged:
                _ctx2 = dict(task.context or {})
                _recovery_merge_refresh_needed = True
        elif gh_pr_state == "closed" and not gh_pr_merged:
            task.status = TaskStatus.FAILED
            task.vm_instance_id = None
            task.last_heartbeat = None
            task.latest_status_message = (
                f"Recovered _pr_state_check_pending: PR #{pr_num} closed without merge"
            )
            _ctx2["pr_closed_unmerged"] = True
        else:
            task.vm_instance_id = None
            task.last_heartbeat = None
            try:
                from app.review_pipeline import _spawn_pipeline_task
                from app.models import Project

                task_type_str = (task.first_payload or {}).get("type", "") or ""
                _proj = await session.get(Project, task.project_id)
                branch = services._derive_task_branch(
                    task.id,
                    task_type_str,
                    _shared_state.get_project_task_branch_namespace(_proj),
                )
                base_branch = (task.first_payload or {}).get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_proj)
                # C6 rework (A): resolve the prompt from the
                # payload first, and only fall back to
                # ``task.description`` if it is absent. The old
                # ``.get("prompt", task.description or "")`` form
                # evaluated the default EAGERLY, touching
                # ``task.description`` on every call — but the
                # EVALUATING candidate query loads this row with
                # ``load_only`` and does NOT include
                # ``description``, so the read fired a sync
                # lazy-load that raises MissingGreenlet under the
                # async session (see [[load_only_lazy_load_trap]]),
                # pushing every recovery into the spawn-failure
                # branch in production. Read the payload key
                # explicitly so the deferred column is only
                # touched when actually needed.
                original_prompt = (task.first_payload or {}).get("prompt") or (
                    task.description or ""
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
                    f"{spawn_exc} — re-staging _pr_state_check_pending "
                    f"so this consumer self-retries next sweep",
                    exc_info=True,
                )
                task.latest_status_message = (
                    "Recovered _pr_state_check_pending: review spawn failed, will retry"
                )
                # C6 rework (A): this consumer is NOT one-shot. The
                # marker was popped from _ctx2 up front (line ~1081)
                # and _ctx2 is committed UNCONDITIONALLY below, so a
                # failed re-spawn would otherwise DROP the marker and
                # abandon the childless EVALUATING parent to the
                # 6h/48h backstop — exactly the sustained-transient-
                # outage scenario the classifier targets (producer
                # keeps the marker on pass 1; consumer pass-2 spawn
                # fails). Re-add it so the consumer re-checks the PR
                # and re-spawns on the next sweep. Mirrors the
                # producer-side discipline in agent_status.py /
                # reaper.py: clear ONLY on confirmed spawn SUCCESS
                # (the try path above, which falls through to the
                # commit without re-adding the marker).
                _ctx2["_pr_state_check_pending"] = True

        task.context = _ctx2
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        # Heal the direction's research-history merge counts for an
        # out-of-band merge recovered here. Done in a FRESH session
        # re-fetching by id: the per-row commit + internal spawn can
        # expire ``task`` (loaded with load_only), and the refresh
        # takes the directions lock + commits, so it must not share
        # this row's session. No-op unless the task carries
        # direction/research lineage; best-effort.
        if _recovery_merge_refresh_needed:
            from app.direction_outcomes import (
                refresh_direction_outcomes_after_merge,
            )

            try:
                async with services.AsyncSessionLocal() as _dir_session:
                    _dir_task = await _dir_session.get(Task, _eval_task_id)
                    if _dir_task is not None:
                        await refresh_direction_outcomes_after_merge(
                            _dir_task, _dir_session
                        )
            except Exception as _dir_exc:
                logger.warning(
                    "[EVAL TIMEOUT pr_check] direction-outcome "
                    "refresh failed for out-of-band merge of task "
                    "%s: %r",
                    _eval_task_id,
                    _dir_exc,
                )
        # Status is unchanged on this recovery path (task stays
        # EVALUATING). The _spawn_pipeline_task above can roll back
        # internally and expire `task` (even with expire_on_commit=
        # False), so a post-commit task.status read would sync
        # lazy-load in async ctx → MissingGreenlet. Use the literal.
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
        )
        return True
