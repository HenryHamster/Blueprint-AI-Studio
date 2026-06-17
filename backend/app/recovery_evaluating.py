"""EVALUATING-timeout recovery sweep, extracted from ``app.recovery_sweeps``.

Holds the bounded leader-cycle EVALUATING-timeout sweep
(``_check_evaluating_timeouts``). Its chain-inspection helpers
(``_chain_has_active_pipeline_work`` and friends), the CI-backstop reason
picker/verifier, the long-queued-pipeline superseder, and the spend-hold signal
emitters were split out again into ``app.recovery_evaluating_helpers`` to keep
each module below its size cap; they are re-exported here so the sweep (and its
callers) reference them as bare module-globals exactly as before.

Split out of ``app.recovery_sweeps`` to keep each module below its size cap.
Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing ``from app.services import ...`` call sites and
test patch targets keep resolving unchanged. Shared bounding infrastructure
(C3 constants, ``_bounded``, ``_DeployPauseSnapshot``, session/broadcast
helpers) lives in ``app.services`` and is referenced through the ``services``
module object so test patches against ``app.services.<name>`` still take effect.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict

from sqlalchemy import or_
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app import services
from app.branch_resolution import classify_rollup_merge_outcome
from app.landed_signal import (
    apply_merged_landed_signal,
)
from app.models import (
    Task,
    TaskStatus,
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
)
from app.recovery_evaluating_strand import (
    _handle_evaluating_strand_backstop,
    _handle_rollup_lost_marker,
)
from app.recovery_evaluating_approval import _handle_approval_gate_timeout
from app.recovery_evaluating_helpers import (  # noqa: F401
    _last_status_transition_at,
    _chain_has_active_pipeline_work,
    _approval_gate_chain_busy,
    _chain_has_recoverable_failed_work,
    _chain_has_unexhausted_optimistic_retry_work,
    _summarize_latest_review_for_chain,
    _QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS,
    _ci_backstop_timeout_reason,
    _supersede_long_queued_pipeline_tasks,
    _attempt_ci_backstop_recovery,
    _verify_ci_before_backstop,
    _dispatch_spend_hold_notification,
    _emit_spend_hold_created_signals,
    _heal_direction_outcomes_best_effort,
    _apply_rollup_merge_classification,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _check_evaluating_timeouts(progress_cb=None):
    """Detect EVALUATING tasks that are stuck and recover or fail them.

    - If all children are terminal but parent is still EVALUATING → trigger missed rollup.
    - If evaluating >2h with non-terminal children → WARNING alert.
    - If evaluating >evaluating_fail_hours (default 48h) → FAIL with ERROR alert.
    - Skips CI-waiting tasks (they have their own 20-min escalation timeout).

    ``progress_cb`` (C3): a zero-arg liveness heartbeat pumped once per processed
    row. This sweep now runs in its OWN background task
    (``_evaluating_timeout_service``, lifecycle.py), so it must NOT pump the
    orchestrator_worker's shared ``_orchestrator_last_progress`` marker — a
    foreign task pumping the worker's heartbeat would MASK a genuinely hung
    orchestrator_worker from its /health gate + watchdog. Instead the caller
    threads its OWN marker (or relies on the background task's per-tick
    asyncio.timeout + _monitor_background_tasks supervision). Defaults to a
    no-op for direct callers / tests that don't supply one.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.rollup_management import _check_and_trigger_rollup
    from app.shared_state import runtime_config as _runtime_config

    _heartbeat = progress_cb if progress_cb is not None else (lambda: None)

    EVALUATING_WARN_SECONDS = 2 * 3600  # 2 hours
    _REAPED_OWNER_FALLBACK_GRACE_S = (
        1800  # 30 min grace before GH poll for marker-less owners
    )
    # Default backstop bumped to 48h while silent-death v2 stabilizes
    # (2026-05-04): tasks reaped mid-flight by the 6h wall-clock were getting
    # cascade-killed during operator-intervention windows. Runtime knob
    # `evaluating_fail_hours` lets us drop it back once the recurrence rate
    # for silent-death + reaper sweeps comes down.
    # Defensive read: the PATCH endpoint validates 1..168, but a corrupted
    # runtime_config_json on disk could surface a non-int / out-of-range value
    # which would raise ValueError on `int(...)` and abort the whole sweep
    # cycle silently — no EVALUATING cleanup until someone PATCHes a fix.
    try:
        _eval_hours_raw = _runtime_config.get("evaluating_fail_hours", 48)
        _eval_hours = int(_eval_hours_raw)
        if not (1 <= _eval_hours <= 168):
            _eval_hours = 48
    except (ValueError, TypeError):
        _eval_hours = 48
    EVALUATING_FAIL_SECONDS = _eval_hours * 3600
    # Hard kill for CI-waiting tasks. Tunable so multi-iteration review/fix
    # cycles (each ~5-15 min, plus pipeline-cap queueing) don't trigger a
    # premature kill that cancels in-flight children. 60 min is the default
    # because realistic cycles routinely run 30-50 min; 30 min was firing
    # mid-iteration during pipeline-throttled periods.
    CI_BACKSTOP_SECONDS = (
        int(_runtime_config.get("ci_backstop_timeout_minutes", 60)) * 60
    )
    # Bounded defer window for the CI-wait backstop when the chain still holds
    # recoverable (infra-class FAILED / freshly-requeued) work — mirrors the 6h
    # hard-timeout path's ``EVAL_DEFER_WINDOW_HOURS``. A parent must not be
    # FAILED (nor its children cascade-cancelled) while salvageable work exists,
    # but a truly stuck case must still terminate eventually. Shorter than the
    # 6h path's 24h window: the CI-wait backstop guards a much tighter loop
    # (rollup PR CI + review iteration), and the optimistic-retry cap bounds how
    # long a recoverable child can keep re-arming anyway.
    CI_BACKSTOP_DEFER_WINDOW_HOURS = 6
    # Approval-gate companion timeout constants (APPROVAL_GATE_FAIL_SECONDS /
    # APPROVAL_GATE_DEFER_WINDOW_HOURS) now live in
    # ``app.recovery_evaluating_approval`` alongside the extracted handler.
    # Deliberate spend/budget approval holds (requires_approval=True AND
    # context.approval_hold_reason == "spend_budget") get their OWN longer,
    # operator-configurable window and resolve to a CLEAN NON-FAILURE CANCELLED
    # terminal on expiry — a cost decision an operator simply has not reached is
    # not a pipeline failure. Defensive read mirrors evaluating_fail_hours: a
    # corrupted runtime_config_json must not abort the whole sweep cycle.
    try:
        _spend_hours_raw = _runtime_config.get("spend_approval_timeout_hours", 24)
        _spend_hours = int(_spend_hours_raw)
        if not (1 <= _spend_hours <= 168):
            _spend_hours = 24
    except (ValueError, TypeError):
        _spend_hours = 24
    SPEND_APPROVAL_FAIL_SECONDS = _spend_hours * 3600
    # BLOCKED and PARKED are treated as terminal-for-now in this EVALUATING-timeout
    # sweep: both are durable non-terminal states that must NOT be cascade-CANCELLED
    # on a timeout. (a) Cancelling them destroys durable child state — a BLOCKED
    # child's blocked state, or a PARKED child's in-flight wake registration and
    # compute-job result — when each would have stayed put exactly as a FAILED child
    # stays terminal. (b) A parent whose remaining children are all BLOCKED/PARKED
    # must still count as all-children-settled so the missed-rollup recovery fires
    # instead of pinning EVALUATING forever.
    TERMINAL = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }

    try:
        from app import pause_clock
        from app.models import DeploymentState

        now = _utcnow()

        # ── Phase A: cycle reads on a short-lived session ──────────────────
        # Audit P1 (session hygiene): the cycle-level candidate scan + child
        # snapshot run on their OWN session that is CLOSED before the per-row
        # loop opens any per-row session. Previously a single shared session
        # was held open across the whole loop, and a _bounded() timeout /
        # cancellation mid-DB-op left that shared session in PendingRollback
        # for the REST of the tick — so every later per-row recovery mutation
        # was silently dropped (exactly under the load — many EVALUATING rows +
        # slow GitHub — where recovery matters most). The fix below gives each
        # row its OWN short-lived session, so a cancellation/rollback on one
        # row can never poison the others. Only PLAIN snapshots (ids, statuses)
        # cross the session boundary; no live ORM instance is reused across
        # sessions (which would risk a cross-session lazy-load / MissingGreenlet).
        # NB the in-memory SQLite test DB shares one connection across sessions
        # (StaticPool), so two concurrently-open sessions self-deadlock; Phase A
        # is fully closed before Phase B opens, and per-row sessions are
        # strictly sequential — never two open at once.
        async with services.AsyncSessionLocal() as read_session:
            # Fetch DeployState once per cycle so CI backstop can subtract
            # pause windows from elapsed (admin pause + auto-pause both count).
            # Snapshot the pause fields into a plain object so the per-row
            # sessions (Phase B) don't reuse this read_session's ORM instance.
            _ds_row = await read_session.get(DeploymentState, 1)
            _deploy_state_snapshot = (
                services._DeployPauseSnapshot.from_state(_ds_row) if _ds_row else None
            )
            # Phase B4 (H-P2): drop ``plan_output`` JSONB; the EVALUATING-timeout
            # loop only reads context/payloads/status_log of the heavy JSONB
            # columns. status_log is consumed at lines ~1216 and ~1384 to find
            # the last EVALUATING transition, so it must remain loaded.
            evaluating_tasks = (
                (
                    await read_session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id, Task.status, Task.archived, raiseload=True
                            )
                        )
                        .where(
                            Task.status == TaskStatus.EVALUATING,
                            Task.archived.is_(False),
                        )
                        # C3: per-cycle row cap. Without it this sweep is O(N
                        # EVALUATING backlog) of serial GitHub I/O on the leader
                        # cycle. Deferred rows re-qualify next cycle (the scan is
                        # status-based; oldest-updated first so the most-stuck
                        # parents are always serviced before the cap truncates).
                        .order_by(Task.updated_at.asc())
                        .limit(services._EVAL_TIMEOUT_MAX_PER_CYCLE)
                    )
                )
                .scalars()
                .all()
            )
            # Snapshot just the candidate IDs — each row is re-fetched fresh in
            # its own per-row session below.
            evaluating_task_ids = [t.id for t in evaluating_tasks]
            if len(evaluating_task_ids) >= services._EVAL_TIMEOUT_MAX_PER_CYCLE:
                logger.warning(
                    "[EVAL TIMEOUT] Hit per-cycle cap of %d EVALUATING rows — "
                    "remainder deferred to next sweep",
                    services._EVAL_TIMEOUT_MAX_PER_CYCLE,
                )

            # Phase B4 (H-P2): batch the child lookup for all evaluating parents
            # into one ``IN (...)`` query instead of one-per-parent (N+1).
            #
            # Only a plain (id, status) snapshot is kept — NOT live ORM rows.
            # The hot all-terminal check below reads the snapshot; the rare
            # branches that mutate children re-fetch them fresh on the per-row
            # session.
            _eval_child_status_by_parent: Dict[uuid.UUID, list[tuple]] = {}
            if evaluating_task_ids:
                _eval_child_rows = (
                    (
                        await read_session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.parent_task_id,
                                    Task.status,
                                    raiseload=True,
                                )
                            )
                            .where(Task.parent_task_id.in_(evaluating_task_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for _ec in _eval_child_rows:
                    _eval_child_status_by_parent.setdefault(
                        _ec.parent_task_id, []
                    ).append((_ec.id, _ec.status))
        # read_session is now closed — no session is held across the loop.

        # ── Phase B: per-row recovery, each on its OWN short-lived session ──
        for _eval_task_id in evaluating_task_ids:
            # C3 heartbeat: this loop performs serial GitHub I/O
            # (get_pull_request / close_pull_request / _check_and_trigger_rollup)
            # per task and is the dominant contributor to the EVALUATING-sweep
            # wall time. Pump the CALLER'S OWN liveness marker each row so a
            # legitimately long-but-progressing sweep is never mistaken for a
            # hung task. NB: this is the background task's own heartbeat, NOT
            # the orchestrator_worker's — pumping the worker's shared marker
            # from here would mask a genuinely hung worker (fix D).
            _heartbeat()
            # Audit P1: open a FRESH session per row. A _bounded() timeout /
            # cancellation that poisons THIS session (PendingRollback) is
            # confined to this iteration — the `async with` closes it on the
            # way out and the next row gets a clean session, so one slow /
            # cancelled row no longer silently drops the rest of the sweep's
            # recovery mutations.
            async with services.AsyncSessionLocal() as session:
                # Re-fetch the candidate fresh on this session and re-validate
                # it is still an unarchived EVALUATING row (it may have been
                # resolved by another worker between Phase A and now). Same
                # load_only column set the cycle scan used to use, so deferred
                # heavy JSONB stays deferred and ``description`` stays loaded
                # for the _pr_state_check_pending consumer's fallback prompt
                # ([[load_only_lazy_load_trap]]).
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.status,
                                    Task.title,
                                    Task.project_id,
                                    Task.parent_task_id,
                                    Task.archived,
                                    Task.requires_approval,
                                    Task.created_at,
                                    Task.started_at,
                                    Task.updated_at,
                                    Task.latest_status_message,
                                    Task.context,
                                    Task.payloads,
                                    Task.status_log,
                                    Task.last_heartbeat,
                                    Task.attempt_count,
                                    Task.account_id,
                                    Task.vm_instance_id,
                                    Task.description,
                                    raiseload=True,
                                )
                            )
                            .where(Task.id == _eval_task_id)
                        )
                    )
                    .scalars()
                    .first()
                )
                if (
                    task is None
                    or task.status != TaskStatus.EVALUATING
                    or task.archived
                ):
                    continue
                # Re-fetch the live DeploymentState on THIS session for the
                # pause-aware elapsed calcs (the snapshot is only used as a
                # fallback if the row went away). Reusing a foreign-session ORM
                # instance here would risk a cross-session lazy-load.
                _deploy_state = (
                    await session.get(DeploymentState, 1)
                ) or _deploy_state_snapshot
                ctx = task.context or {}

                # _pr_state_check_pending consumer. The agent_status FAILED→
                # EVALUATING recovery (agent_status.py ~1567) sets this flag
                # before its GH PR check; if the post-GH lock re-acquire
                # races a concurrent writer (skip_locked returns None at
                # ~1616) the recovery returns ``accepted_pr_check_deferred``
                # leaving the flag stuck and no review task spawned. Without
                # this consumer the task sits until the 48h reaper backstop.
                if ctx.get("_pr_state_check_pending"):
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
                        continue

                    from sqlalchemy.orm.attributes import flag_modified

                    _ctx2 = dict(ctx)
                    _ctx2.pop("_pr_state_check_pending", None)
                    # Set when this recovery records a real integration landing
                    # (out-of-band merge), so the direction-outcome refresh runs
                    # after the per-row commit. Re-fetched in a fresh session below
                    # because the per-row commit can expire ``task`` (and this row
                    # is loaded with load_only).
                    _recovery_merge_refresh_needed = False

                    if not pr_num:
                        # No PR number recoverable — clear flag, fall through to
                        # the normal evaluating timeout logic below.
                        task.context = _ctx2
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        ctx = _ctx2
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
                                merged_base_branch=(pr_data.get("base") or {}).get(
                                    "ref"
                                ),
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
                                    (task.first_payload or {}).get("hotfix")
                                    or _ctx2.get("hotfix")
                                ),
                                ctx_override=_ctx2,
                                non_merge_reason="merged_pr_non_integration_branch",
                            )
                            if record_pr_merged:
                                _ctx2 = dict(task.context or {})
                                _recovery_merge_refresh_needed = True
                        elif gh_pr_state == "closed" and not gh_pr_merged:
                            task.status = TaskStatus.FAILED
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = f"Recovered _pr_state_check_pending: PR #{pr_num} closed without merge"
                            _ctx2["pr_closed_unmerged"] = True
                        else:
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            try:
                                from app.review_pipeline import _spawn_pipeline_task
                                from app.models import Project

                                task_type_str = (task.first_payload or {}).get(
                                    "type", ""
                                ) or ""
                                _proj = await session.get(Project, task.project_id)
                                branch = services._derive_task_branch(
                                    task.id,
                                    task_type_str,
                                    _shared_state.get_project_task_branch_namespace(
                                        _proj
                                    ),
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
                                original_prompt = (task.first_payload or {}).get(
                                    "prompt"
                                ) or (task.description or "")
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
                                task.latest_status_message = "Recovered _pr_state_check_pending: review spawn failed, will retry"
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
                                    _dir_task = await _dir_session.get(
                                        Task, _eval_task_id
                                    )
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
                        continue

                if await _handle_evaluating_strand_backstop(
                    task,
                    ctx,
                    session,
                    maybe_create_failure_alert=_maybe_create_failure_alert,
                ):
                    continue

                if await _handle_rollup_lost_marker(task, ctx, session):
                    continue
                # The lost-marker no-merged path stamps _lost_marker_checked_at
                # on task.context and commits without converging; re-sync the
                # loop-local ctx so downstream context writes preserve that
                # back-off timestamp (matches the original inline reassignment).
                ctx = task.context or {}

                # Approval-gate hard timeout. Parents with requires_approval=True
                # remain in EVALUATING waiting for human approval; the
                # evaluating_fail_hours backstop can be deferred by an active
                # pipeline chain, so approval-gated parents need their own
                # companion timeout.
                #
                # TWO disposition paths, keyed on context.approval_hold_reason:
                #  - DELIBERATE spend/budget hold (== "spend_budget"): a longer,
                #    operator-configurable window resolving to a CLEAN NON-FAILURE
                #    CANCELLED terminal (a cost decision an operator has not
                #    reached is not a pipeline failure). NEVER records a failed
                #    task result / failure category / failure alert.
                #  - Everything else (no marker / review-escalation churn): the
                #    EXACT current 2h → FAILED behavior, unchanged.
                if await _handle_approval_gate_timeout(
                    task,
                    ctx,
                    session,
                    now,
                    spend_approval_fail_seconds=SPEND_APPROVAL_FAIL_SECONDS,
                    spend_hours=_spend_hours,
                    maybe_create_failure_alert=_maybe_create_failure_alert,
                ):
                    continue

                ci_waiting = (
                    ctx.get("ci_waiting") is True
                    or ctx.get("review_loop", {}).get("ci_waiting") is True
                )
                if ci_waiting:
                    # CI-waiting tasks have their own 20-min escalation in orchestrator.py.
                    # Apply a 30-minute absolute backstop so tasks can never
                    # hang forever regardless of field state.
                    #
                    # Time base: prefer review_loop.ci_wait_started (when CI wait
                    # actually began) over the last EVALUATING transition. A task
                    # may sit in EVALUATING for a long time waiting on subtasks
                    # before the rollup PR is opened and CI polling starts — using
                    # the EVALUATING transition would false-positive the backstop
                    # the instant CI finally begins.
                    rl = ctx.get("review_loop", {})
                    ci_ws_val = rl.get("ci_wait_started")
                    was_escalated = rl.get("escalated", False)
                    evaluating_since = None
                    ci_ws_valid = False
                    if ci_ws_val is not None:
                        try:
                            evaluating_since = datetime.fromisoformat(ci_ws_val)
                            ci_ws_valid = True
                        except (ValueError, TypeError):
                            evaluating_since = None
                    if evaluating_since is None:
                        for entry in reversed(task.status_log or []):
                            msg = entry.get("msg", "")
                            if "→ EVALUATING" in msg:
                                with contextlib.suppress(
                                    KeyError, ValueError, TypeError
                                ):
                                    evaluating_since = datetime.fromisoformat(
                                        entry["t"]
                                    )
                                break
                    if evaluating_since is None:
                        evaluating_since = task.started_at or task.created_at

                    # Pause-aware elapsed: subtract any pause windows that
                    # occurred since the timer started. Legacy timers without
                    # a stashed baseline fall back to raw elapsed.
                    _ci_baseline = rl.get("ci_wait_pause_baseline")
                    elapsed = (
                        pause_clock.active_elapsed(
                            _deploy_state,
                            evaluating_since,
                            _ci_baseline,
                        )
                        if _deploy_state
                        else (now - evaluating_since).total_seconds()
                    )
                    # Early trigger: if CI has passed but review_approved is
                    # missing AND the review child task is already terminal
                    # (FAILED/CANCELLED), the review VM died silently — we
                    # have enough signal to recover now without waiting for
                    # the 30-min backstop. The elapsed guard remains as the
                    # fallback for cases where review status hasn't become
                    # terminal yet.
                    rl_ci_passed = bool(rl.get("ci_passed"))
                    rl_review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    rl_recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    early_recovery_eligible = False
                    if (
                        rl_ci_passed
                        and not rl_review_approved
                        and not rl_recovery_attempted
                    ):
                        _early_summary = await _summarize_latest_review_for_chain(
                            task, session
                        )
                        if _early_summary and _early_summary["status"] in (
                            "FAILED",
                            "CANCELLED",
                        ):
                            early_recovery_eligible = True
                    if elapsed <= CI_BACKSTOP_SECONDS and not early_recovery_eligible:
                        # Under backstop threshold — let normal CI timeout handle it
                        continue
                    # Pipeline still actively working on the chain (review/fix
                    # task heartbeated within 10min) — defer to next sweep so
                    # we don't kill legitimate in-flight iteration cycles.
                    if await services._chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop but chain "
                            f"has fresh review/fix heartbeat — skipping kill"
                        )
                        continue

                    # Gap C: verify REAL CI state before declaring a timeout.
                    # The backstop fires on a wall-clock that can outlast a CI
                    # conclusion that actually landed before the wait window even
                    # started (incident 2026-06-07: review_loop.ci_wait_started
                    # was re-armed AFTER CI had concluded, so the backstop "waited
                    # 1h for a conclusion that was already in"). Before failing a
                    # parent whose PR still has a head_sha to check, read the PR's
                    # live check-run conclusion and route on the truth:
                    #   concluded FAILURE → existing CI-failure handling
                    #       (handle_ci_completion fix-spawn path / ci_passed=False)
                    #   concluded SUCCESS → record the pass + clear the wait
                    #   pending / GitHub error → fall through to the timeout
                    #       (fail-OPEN: never kill, but never block on an outage).
                    # ALL GitHub I/O is strictly OUTSIDE the FOR-UPDATE window
                    # below — handle_ci_completion re-locks the row itself.
                    _ci_head_sha = rl.get("head_sha")
                    if _ci_head_sha and rl.get("pr_number"):
                        _ci_resolved = await _verify_ci_before_backstop(
                            task, rl, _ci_head_sha, session
                        )
                        if _ci_resolved:
                            # CI had actually concluded — handled (fix-spawn /
                            # pass recorded / wait cleared). Skip the kill.
                            continue

                    # Gap A: recoverable / freshly-requeuing work guard. Mirror
                    # the 6h hard-timeout path: a child / chain pipeline task that
                    # is FAILED with an infra-class category (resurrection-sweep
                    # or operator-requeue eligible) OR is QUEUED with optimistic-
                    # retry budget still left must NOT be cascade-cancelled while
                    # it can still come back. Defer for a bounded window so a truly
                    # stuck case still terminates, and respect the optimistic-retry
                    # cap so an exhausted child stops deferring the backstop.
                    _has_recoverable = await _chain_has_recoverable_failed_work(
                        task, session
                    )
                    _has_retrying = await _chain_has_unexhausted_optimistic_retry_work(
                        task, session
                    )
                    if _has_recoverable or _has_retrying:
                        from sqlalchemy.orm.attributes import flag_modified

                        _ci_first_defer_at = rl.get("_ci_backstop_first_defer_at")
                        # A defer marker older than this CI-wait episode is stale
                        # (left from a prior ci_wait_started) — discard it so the
                        # window measures from the current wait.
                        if _ci_first_defer_at and ci_ws_valid and evaluating_since:
                            try:
                                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                                if _cfd < evaluating_since:
                                    _ci_first_defer_at = None
                            except (ValueError, TypeError):
                                _ci_first_defer_at = None
                        if _ci_first_defer_at:
                            try:
                                _cfd = datetime.fromisoformat(_ci_first_defer_at)
                                _ci_defer_elapsed = (now - _cfd).total_seconds()
                            except (ValueError, TypeError):
                                _ci_defer_elapsed = 0.0
                            if (
                                _ci_defer_elapsed
                                <= CI_BACKSTOP_DEFER_WINDOW_HOURS * 3600
                            ):
                                logger.info(
                                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI "
                                    f"backstop but chain has recoverable/requeuing work "
                                    f"— deferring ({round(_ci_defer_elapsed / 3600, 1)}h "
                                    f"into {CI_BACKSTOP_DEFER_WINDOW_HOURS}h window)"
                                )
                                continue
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                                f"{CI_BACKSTOP_DEFER_WINDOW_HOURS}h CI-backstop defer "
                                f"window despite recoverable work — proceeding to kill"
                            )
                            # fall through to existing kill
                        else:
                            new_rl = dict(rl)
                            new_rl["_ci_backstop_first_defer_at"] = now.isoformat()
                            task.context = {
                                **(task.context or {}),
                                "review_loop": new_rl,
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past CI backstop "
                                f"with recoverable/requeuing chain work — deferring "
                                f"cascade-cancel for up to "
                                f"{CI_BACKSTOP_DEFER_WINDOW_HOURS}h"
                            )
                            await session.commit()
                            continue

                    hours = round(elapsed / 3600, 1)
                    ci_passed = bool(rl.get("ci_passed"))
                    review_approved = (
                        bool(rl.get("review_approved"))
                        or rl.get("verdict") == "APPROVE"
                    )
                    recovery_attempted = bool(rl.get("review_recovery_attempted"))
                    review_summary = await _summarize_latest_review_for_chain(
                        task, session
                    )

                    # Compose an honest reason. The historic "CI polling stalled after
                    # escalation" was a lie when the actual blocker was a missing review
                    # callback (review VM died silently) — name the gate that is open.
                    reason = _ci_backstop_timeout_reason(
                        ci_ws_val=ci_ws_val,
                        ci_ws_valid=ci_ws_valid,
                        ci_passed=ci_passed,
                        review_approved=review_approved,
                        was_escalated=was_escalated,
                        review_summary=review_summary,
                        review_loop=rl,
                    )

                    # One-shot recovery: if CI passed but the review-approved gate never
                    # closed (common when the review VM died before POSTing status),
                    # spawn a fresh review and reset the CI-wait clock instead of hard
                    # killing. The dedup guard will supersede any stale corpse.
                    if ci_passed and not review_approved and not recovery_attempted:
                        recovered = await _attempt_ci_backstop_recovery(
                            task, session, rl
                        )
                        if recovered:
                            from sqlalchemy.orm.attributes import flag_modified

                            new_rl = dict(rl)
                            new_rl["review_recovery_attempted"] = True
                            new_rl["review_recovery_at"] = _utcnow().isoformat()
                            new_rl["ci_wait_started"] = _utcnow().isoformat()
                            new_rl["ci_wait_pause_baseline"] = (
                                pause_clock.total_paused_seconds(_deploy_state)
                                if _deploy_state
                                else 0.0
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": new_rl,
                            }
                            flag_modified(task, "context")
                            task.latest_status_message = (
                                f"CI passed on PR #{new_rl.get('pr_number')} but review never "
                                f"completed ({reason}) — spawned fresh review, backstop reset"
                            )
                            session.add(task)
                            await session.commit()
                            # Recovery path keeps the task EVALUATING (reset CI-wait
                            # clock). _attempt_ci_backstop_recovery above can poison/
                            # roll back the session and expire `task`, so use the
                            # literal rather than a post-commit lazy-load.
                            await services.broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {
                                    "task_id": str(task.id),
                                    "status": TaskStatus.EVALUATING,
                                },
                            )
                            logger.warning(
                                f"[EVAL TIMEOUT] Backstop recovery for task "
                                f"{str(task.id)[:8]}: spawned fresh review on "
                                f"PR #{rl.get('pr_number')} ({reason})"
                            )
                            continue

                    # C3 lost-update guard: this backstop branch reached the
                    # FAILED decision from a NON-locked read of `task`, after
                    # several awaits (_chain_has_active_pipeline_work,
                    # _summarize_latest_review_for_chain, _attempt_ci_backstop_
                    # recovery). This sweep now runs in its OWN background task
                    # (_evaluating_timeout_service, lifecycle.py) and can
                    # interleave with the orchestrator's CI-poll loop
                    # (orchestrator.py:2994), which locks the SAME row FOR UPDATE
                    # and may resolve it (e.g. COMPLETED on merge) in the window
                    # between our read and this commit. Acquire the row lock and
                    # RE-VALIDATE under it before clobbering: if the CI loop holds
                    # the lock (skip_locked → None) or already moved the row out
                    # of EVALUATING, or the timeout no longer holds on the fresh
                    # context, defer to the next sweep instead of overwriting a
                    # state the CI loop just committed. Locking the single row at
                    # the decision point adds no serial coupling across rows.
                    from app.orchestrator import _lock_task_for_mutation

                    locked_task = await _lock_task_for_mutation(
                        session,
                        task.id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="eval-timeout CI backstop FAIL",
                    )
                    if locked_task is None:
                        # Row is locked by the CI-poll loop, or no longer
                        # EVALUATING (already resolved). _lock_task_for_mutation
                        # has already committed/rolled back to release. Skip.
                        continue
                    # populate_existing=True refreshed `locked_task` from the DB,
                    # so re-derive the CI-wait timer + active-pipeline guard from
                    # the *current* context and confirm the backstop still fires.
                    _rl_fresh = (locked_task.context or {}).get("review_loop", {}) or {}
                    if not (
                        _rl_fresh.get("ci_waiting") is True
                        or (locked_task.context or {}).get("ci_waiting") is True
                    ):
                        # CI loop cleared ci_waiting (e.g. merged/closed) between
                        # our read and the lock — no longer our row to fail.
                        await session.rollback()
                        continue
                    _ci_ws_fresh = _rl_fresh.get("ci_wait_started")
                    _since_fresh = None
                    if _ci_ws_fresh is not None:
                        with contextlib.suppress(ValueError, TypeError):
                            _since_fresh = datetime.fromisoformat(_ci_ws_fresh)
                    if _since_fresh is None:
                        _since_fresh = evaluating_since
                    _baseline_fresh = _rl_fresh.get("ci_wait_pause_baseline")
                    _elapsed_fresh = (
                        pause_clock.active_elapsed(
                            _deploy_state, _since_fresh, _baseline_fresh
                        )
                        if _deploy_state
                        else (now - _since_fresh).total_seconds()
                    )
                    if (
                        _elapsed_fresh <= CI_BACKSTOP_SECONDS
                        and not early_recovery_eligible
                    ):
                        # CI loop reset the wait clock (e.g. recovery / fresh
                        # poll) in the unlocked window — backstop no longer due.
                        await session.rollback()
                        continue
                    # Re-validated under the FOR UPDATE lock: still EVALUATING,
                    # still CI-waiting, still past the backstop. Safe to fail.
                    task = locked_task
                    logger.error(
                        f"[EVAL TIMEOUT] CI-waiting task {str(task.id)[:8]} has been "
                        f"EVALUATING for {hours}h — backstop timeout ({reason}), marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"CI wait backstop timeout after {hours}h ({reason})"
                    )
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )
                    from app.routers.agent_status import record_task_result

                    _fc = classify_failure(task)
                    persist_failure_category(task, _fc)
                    record_task_result(
                        success=False,
                        failure_class=_fc,
                        task_id=str(task.id),
                        task_type=task.first_payload.get("type", ""),
                    )
                    session.add(task)
                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and this branch reads + mutates the live
                    # rows (cancel, latest_status_message, vm_instance_id).
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    from app.vm_lifecycle import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    child_cancel_msg = f"Cancelled: parent CI wait backstop timeout after {hours}h ({reason})"
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = child_cancel_msg
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"CI wait backstop timeout after {hours}h — {reason}",
                    )
                    await session.commit()
                    # task.status=FAILED / children=CANCELLED were set above; the
                    # alert + commit can poison/expire these instances, so broadcast
                    # the known literals rather than post-commit lazy-loads. child.id
                    # (PK) is always safe to read.
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.FAILED},
                    )
                    for child in non_terminal_children:
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": TaskStatus.CANCELLED},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="ci_backstop_timeout")
                    continue

                # Determine how long the task has been EVALUATING
                evaluating_since = None
                # Walk status_log backwards for the last EVALUATING transition
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        break
                if evaluating_since is None:
                    # Fallback to started_at or created_at
                    evaluating_since = task.started_at or task.created_at

                elapsed = (now - evaluating_since).total_seconds()

                # Child statuses snapshotted at cycle-read time (plain tuples,
                # Phase A). Kept as plain data so they survive across the
                # per-row sessions; the rare branches that MUTATE children
                # re-fetch them fresh on this row's session.
                child_status_snap = _eval_child_status_by_parent.get(task.id, [])

                all_children_terminal = bool(child_status_snap) and all(
                    c_status in TERMINAL for _, c_status in child_status_snap
                )

                # REAPED-OWNER MARKER-LESS FALLBACK (Defect 2).
                # An EVALUATING task with a PULL_REQUEST artifact but no
                # ``review_loop`` or ``_pr_state_check_pending`` is invisible to
                # every existing sweep path once the reaper clears the write-ahead
                # flag on successful Review spawn.  When the PR merges out-of-band
                # while the pipeline is still active this section catches it before
                # ``all_children_terminal`` ever becomes True.  The grace period
                # avoids a GH API call on every cycle for freshly-handed-off owners.
                if (
                    not all_children_terminal
                    and not ctx.get("review_loop")
                    and not ctx.get("_pr_state_check_pending")
                    and not ctx.get("pr_merged")
                    and not ctx.get("rollup_pr_number")
                    and task.vm_instance_id is None
                    and elapsed >= _REAPED_OWNER_FALLBACK_GRACE_S
                ):
                    _fb_artifact = (
                        (
                            await session.execute(
                                select(Artifact).where(
                                    Artifact.task_id == task.id,
                                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if _fb_artifact and _fb_artifact.pr_url:
                        _fb_pr_num: int | None = None
                        with contextlib.suppress(ValueError, IndexError):
                            _fb_pr_num = int(
                                _fb_artifact.pr_url.rstrip("/").rsplit("/", 1)[-1]
                            )
                        if _fb_pr_num:
                            _fb_pr_data: dict | None = None
                            try:
                                from app.routers.agent import get_github_token_for_task
                                from app.github import get_pull_request

                                (
                                    _fb_tok,
                                    _fb_owner,
                                    _fb_repo,
                                ) = await services._bounded(
                                    get_github_token_for_task(task.id, session),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"reaped-fallback token {str(task.id)[:8]}",
                                )
                                _fb_pr_data = await services._bounded(
                                    get_pull_request(
                                        _fb_tok, _fb_owner, _fb_repo, _fb_pr_num
                                    ),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"reaped-fallback get_pull_request #{_fb_pr_num}",
                                )
                            except Exception as _fb_exc:
                                logger.warning(
                                    f"[EVAL TIMEOUT reaped-fallback] Task "
                                    f"{str(task.id)[:8]} PR #{_fb_pr_num} lookup "
                                    f"failed: {type(_fb_exc).__name__}: {_fb_exc} — "
                                    f"deferring to next sweep"
                                )
                            if _fb_pr_data is not None and _fb_pr_data.get("merged"):
                                from sqlalchemy.orm.attributes import flag_modified
                                from app.models import Project

                                _fb_proj = await session.get(Project, task.project_id)
                                (
                                    _fb_record,
                                    _fb_terminal,
                                    _fb_landed,
                                ) = await classify_rollup_merge_outcome(
                                    task,
                                    _fb_proj,
                                    session,
                                    pr_number=_fb_pr_num,
                                    merged_base_branch=(
                                        (_fb_pr_data.get("base") or {}).get("ref")
                                    ),
                                    source="recovery_evaluating.reaped_owner_fallback",
                                )
                                task.status = (
                                    TaskStatus.COMPLETED
                                    if _fb_terminal
                                    else TaskStatus.EVALUATING
                                )
                                task.vm_instance_id = None
                                task.last_heartbeat = None
                                task.latest_status_message = (
                                    f"Reaped-owner fallback: PR #{_fb_pr_num} "
                                    "merged out-of-band; owner reconciled"
                                )
                                if _fb_record:
                                    task.context = dict(ctx)
                                    apply_merged_landed_signal(
                                        task,
                                        _fb_proj,
                                        base_branch=_fb_landed,
                                        hotfix_lane=bool(
                                            (task.first_payload or {}).get("hotfix")
                                            or ctx.get("hotfix")
                                        ),
                                    )
                                    flag_modified(task, "context")
                                session.add(task)
                                await session.commit()
                                logger.info(
                                    f"[EVAL TIMEOUT reaped-fallback] Task "
                                    f"{str(task.id)[:8]} reconciled → {task.status} "
                                    f"(PR #{_fb_pr_num} merged out-of-band)"
                                )
                                if _fb_record:
                                    await _heal_direction_outcomes_best_effort(
                                        _eval_task_id, label="reaped-fallback"
                                    )
                                await services.broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {"task_id": str(task.id), "status": task.status},
                                )
                                if _fb_terminal and task.parent_task_id:
                                    with contextlib.suppress(Exception):
                                        await services._bounded(
                                            _check_and_trigger_rollup(
                                                task,
                                                session,
                                                progress_cb=_heartbeat,
                                            ),
                                            services._GH_CALL_TIMEOUT_S,
                                            what=f"reaped-fallback parent rollup {str(task.id)[:8]}",
                                        )
                                continue

                # Recovery for missed rollup: all children done but parent still EVALUATING
                if all_children_terminal:
                    # REAPED-OWNER RECONCILE (Defect 1).
                    # When all Review/Fix children are terminal but the EVALUATING
                    # owner still has a PULL_REQUEST artifact and no ``review_loop``
                    # of its own, check GitHub: merged → reconcile to COMPLETED and
                    # trigger parent rollup; open (Fix pushed a commit but review
                    # not yet done) → re-arm ``_pr_state_check_pending`` so the
                    # existing consumer spawns the next Review iteration.
                    if (
                        not ctx.get("review_loop")
                        and not ctx.get("pr_merged")
                        and not ctx.get("rollup_pr_number")
                    ):
                        _ro_artifact = (
                            (
                                await session.execute(
                                    select(Artifact).where(
                                        Artifact.task_id == task.id,
                                        Artifact.artifact_type
                                        == ArtifactType.PULL_REQUEST,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if _ro_artifact and _ro_artifact.pr_url:
                            _ro_pr_num: int | None = None
                            with contextlib.suppress(ValueError, IndexError):
                                _ro_pr_num = int(
                                    _ro_artifact.pr_url.rstrip("/").rsplit("/", 1)[-1]
                                )
                            if _ro_pr_num:
                                _ro_pr_data: dict | None = None
                                try:
                                    from app.routers.agent import (
                                        get_github_token_for_task,
                                    )
                                    from app.github import get_pull_request

                                    (
                                        _ro_tok,
                                        _ro_owner,
                                        _ro_repo,
                                    ) = await services._bounded(
                                        get_github_token_for_task(task.id, session),
                                        services._GH_CALL_TIMEOUT_S,
                                        what=f"reaped-owner token {str(task.id)[:8]}",
                                    )
                                    _ro_pr_data = await services._bounded(
                                        get_pull_request(
                                            _ro_tok, _ro_owner, _ro_repo, _ro_pr_num
                                        ),
                                        services._GH_CALL_TIMEOUT_S,
                                        what=f"reaped-owner get_pull_request #{_ro_pr_num}",
                                    )
                                except Exception as _ro_exc:
                                    logger.warning(
                                        f"[EVAL TIMEOUT reaped-owner] Task "
                                        f"{str(task.id)[:8]} PR #{_ro_pr_num} lookup "
                                        f"failed: {type(_ro_exc).__name__}: {_ro_exc} "
                                        f"— falling through to standard rollup trigger"
                                    )
                                if _ro_pr_data is not None:
                                    from sqlalchemy.orm.attributes import flag_modified

                                    if _ro_pr_data.get("merged"):
                                        from app.models import Project

                                        _ro_proj = await session.get(
                                            Project, task.project_id
                                        )
                                        (
                                            _ro_record,
                                            _ro_terminal,
                                            _ro_landed,
                                        ) = await classify_rollup_merge_outcome(
                                            task,
                                            _ro_proj,
                                            session,
                                            pr_number=_ro_pr_num,
                                            merged_base_branch=(
                                                (_ro_pr_data.get("base") or {}).get(
                                                    "ref"
                                                )
                                            ),
                                            source="recovery_evaluating.reaped_owner_reconcile",
                                        )
                                        task.status = (
                                            TaskStatus.COMPLETED
                                            if _ro_terminal
                                            else TaskStatus.EVALUATING
                                        )
                                        task.vm_instance_id = None
                                        task.last_heartbeat = None
                                        task.latest_status_message = (
                                            f"Reaped-owner reconcile: PR #{_ro_pr_num} "
                                            "merged; review/fix sub-pipeline complete"
                                        )
                                        if _ro_record:
                                            task.context = dict(ctx)
                                            apply_merged_landed_signal(
                                                task,
                                                _ro_proj,
                                                base_branch=_ro_landed,
                                                hotfix_lane=bool(
                                                    (task.first_payload or {}).get(
                                                        "hotfix"
                                                    )
                                                    or ctx.get("hotfix")
                                                ),
                                            )
                                            flag_modified(task, "context")
                                        session.add(task)
                                        await session.commit()
                                        logger.info(
                                            f"[EVAL TIMEOUT reaped-owner] Task "
                                            f"{str(task.id)[:8]} reconciled → "
                                            f"{task.status} (PR #{_ro_pr_num} merged, "
                                            f"sub-pipeline complete)"
                                        )
                                        if _ro_record:
                                            await _heal_direction_outcomes_best_effort(
                                                _eval_task_id, label="reaped-owner"
                                            )
                                        await services.broadcast_to_project(
                                            str(task.project_id),
                                            "task_updated",
                                            {
                                                "task_id": str(task.id),
                                                "status": task.status,
                                            },
                                        )
                                        if _ro_terminal and task.parent_task_id:
                                            with contextlib.suppress(Exception):
                                                await services._bounded(
                                                    _check_and_trigger_rollup(
                                                        task,
                                                        session,
                                                        progress_cb=_heartbeat,
                                                    ),
                                                    services._GH_CALL_TIMEOUT_S,
                                                    what=f"reaped-owner parent rollup {str(task.id)[:8]}",
                                                )
                                        continue
                                    else:
                                        # PR still open, all pipeline children done →
                                        # re-arm so the existing consumer spawns the
                                        # next Review iteration on the next sweep.
                                        _ro_ctx = dict(ctx)
                                        _ro_ctx["_pr_state_check_pending"] = True
                                        task.context = _ro_ctx
                                        flag_modified(task, "context")
                                        session.add(task)
                                        await session.commit()
                                        logger.info(
                                            f"[EVAL TIMEOUT reaped-owner] Task "
                                            f"{str(task.id)[:8]} PR #{_ro_pr_num} "
                                            f"open, sub-pipeline done — re-arming "
                                            f"_pr_state_check_pending"
                                        )
                                        await services.broadcast_to_project(
                                            str(task.project_id),
                                            "task_updated",
                                            {
                                                "task_id": str(task.id),
                                                "status": TaskStatus.EVALUATING,
                                            },
                                        )
                                        continue
                    logger.warning(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has all children terminal "
                        f"but is still EVALUATING — triggering missed rollup"
                    )
                    # Re-fetch a terminal child fresh to trigger rollup.
                    trigger_child = await session.get(Task, child_status_snap[0][0])
                    if trigger_child is not None:
                        # C3: bound the rollup trigger — _check_and_trigger_rollup
                        # does serial GitHub I/O (PR verify/create/merge) per
                        # child. A hung call here would otherwise pin the whole
                        # sweep.
                        #
                        # Audit P1: this _bounded() wraps a coroutine that does
                        # DB I/O on ``session`` — now this ROW's own short-lived
                        # session. An asyncio.wait_for cancellation mid-DB-op
                        # that leaves it in PendingRollback is confined to this
                        # iteration: the explicit rollback below clears it for
                        # the rest of this row, and the per-row ``async with``
                        # closes it before the next row gets a clean session.
                        try:
                            await services._bounded(
                                # C3: thread the caller's own heartbeat so the
                                # per-child PR-verify loop pumps the background
                                # task's liveness marker, not the orchestrator's.
                                _check_and_trigger_rollup(
                                    trigger_child, session, progress_cb=_heartbeat
                                ),
                                services._GH_CALL_TIMEOUT_S,
                                what=f"eval-timeout rollup {str(task.id)[:8]}",
                            )
                        except asyncio.TimeoutError:
                            with contextlib.suppress(Exception):
                                await session.rollback()
                            continue
                    await session.commit()
                    continue

                # 6-hour hard timeout
                if elapsed > EVALUATING_FAIL_SECONDS:
                    # Same protection as the CI backstop: if the chain has an
                    # actively heartbeating review/fix task, the pipeline is
                    # still doing real work — defer to next sweep.
                    if await services._chain_has_active_pipeline_work(task, session):
                        logger.info(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                            f"{EVALUATING_FAIL_SECONDS // 3600}h hard timeout but "
                            f"chain has fresh review/fix heartbeat — skipping kill"
                        )
                        continue

                    # Recoverable-FAILED defer: a child / chain pipeline task in
                    # FAILED with infra-class failure_category may come back via
                    # resurrection sweep or operator requeue. Cascade-killing
                    # mid-recovery destroys salvageable work (incident
                    # 2026-04-29: PRs #3450/#3408 + parents c30701d3/ddd987fc).
                    # Defer for a bounded window so a truly stuck case still
                    # terminates eventually.
                    EVAL_DEFER_WINDOW_HOURS = 24
                    first_defer_at = ctx.get("_eval_timeout_first_defer_at")
                    # Treat a defer marker older than evaluating_since as stale
                    # (left over from a previous EVALUATING transition).
                    if first_defer_at:
                        try:
                            _fd = datetime.fromisoformat(first_defer_at)
                            if _fd < evaluating_since:
                                first_defer_at = None
                        except (ValueError, TypeError):
                            first_defer_at = None
                    has_recoverable = await _chain_has_recoverable_failed_work(
                        task, session
                    )
                    if has_recoverable:
                        if first_defer_at:
                            try:
                                _fd = datetime.fromisoformat(first_defer_at)
                                defer_elapsed = (now - _fd).total_seconds()
                            except (ValueError, TypeError):
                                defer_elapsed = 0.0
                            if defer_elapsed <= EVAL_DEFER_WINDOW_HOURS * 3600:
                                logger.info(
                                    f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                    f"{EVALUATING_FAIL_SECONDS // 3600}h but chain "
                                    f"has recoverable FAILED children — deferring "
                                    f"({round(defer_elapsed / 3600, 1)}h into "
                                    f"{EVAL_DEFER_WINDOW_HOURS}h window)"
                                )
                                continue
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} exhausted "
                                f"{EVAL_DEFER_WINDOW_HOURS}h defer window despite recoverable "
                                f"FAILED children — proceeding to kill"
                            )
                            # fall through to existing kill
                        else:
                            from sqlalchemy.orm.attributes import flag_modified

                            task.context = {
                                **ctx,
                                "_eval_timeout_first_defer_at": now.isoformat(),
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            logger.warning(
                                f"[EVAL TIMEOUT] Task {str(task.id)[:8]} past "
                                f"{EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable FAILED children — deferring cascade-cancel "
                                f"for up to {EVAL_DEFER_WINDOW_HOURS}h"
                            )
                            await _maybe_create_failure_alert(
                                task,
                                session,
                                f"Evaluation past {EVALUATING_FAIL_SECONDS // 3600}h with "
                                f"recoverable infra failures in chain — deferring "
                                f"cascade-cancel for up to {EVAL_DEFER_WINDOW_HOURS}h",
                            )
                            await session.commit()
                            continue

                    hours = round(elapsed / 3600, 1)
                    logger.error(
                        f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                        f"for {hours}h — hard timeout, marking FAILED"
                    )
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Evaluation timeout — children did not complete "
                        f"within {EVALUATING_FAIL_SECONDS // 3600} hours"
                    )
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )
                    from app.routers.agent_status import record_task_result

                    _fc = classify_failure(task)
                    persist_failure_category(task, _fc)
                    record_task_result(
                        success=False,
                        failure_class=_fc,
                        task_id=str(task.id),
                        task_type=task.first_payload.get("type", ""),
                    )
                    session.add(task)

                    # Re-fetch children fresh — the batch holds only a plain
                    # status snapshot, and the cascade-cancel + PR-close below
                    # read and mutate the live rows.
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )

                    # Cascade-cancel non-terminal children
                    from app.vm_lifecycle import _ensure_vm_deleted
                    from app.max_plan_pool import release_max_plan_account

                    non_terminal_children = [
                        c for c in children if c.status not in TERMINAL
                    ]
                    vms_to_delete = []
                    hard_timeout_cancel_msg = (
                        f"Cancelled: parent evaluation timeout after {hours}h"
                    )
                    for child in non_terminal_children:
                        child.status = TaskStatus.CANCELLED
                        child.latest_status_message = hard_timeout_cancel_msg
                        if child.vm_instance_id:
                            vms_to_delete.append(child)
                        child_acct = (child.context or {}).get("max_plan_account_id")
                        if child_acct:
                            await release_max_plan_account(child_acct)
                        session.add(child)
                    if non_terminal_children:
                        logger.info(
                            f"[EVAL TIMEOUT] Cascade-cancelled {len(non_terminal_children)} "
                            f"children of timed-out task {str(task.id)[:8]}"
                        )

                    # Close orphaned rollup PRs from CI-waiting children
                    from app.github import close_pull_request

                    for child in children:
                        child_pr = (
                            (child.context or {})
                            .get("review_loop", {})
                            .get("pr_number")
                        )
                        if child_pr and not (child.context or {}).get("pr_merged"):
                            try:
                                from app.routers.agent import get_github_token_for_task

                                _gh, _owner, _repo = await services._bounded(
                                    get_github_token_for_task(child.id, session),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"eval-timeout token {str(child.id)[:8]}",
                                )
                                await services._bounded(
                                    close_pull_request(_gh, _owner, _repo, child_pr),
                                    services._GH_CALL_TIMEOUT_S,
                                    what=f"eval-timeout close PR #{child_pr}",
                                )
                                logger.info(
                                    f"[EVAL TIMEOUT] Closed orphaned PR #{child_pr} from child {str(child.id)[:8]}"
                                )
                            except Exception as pr_err:
                                logger.warning(
                                    f"[EVAL TIMEOUT] Failed to close PR #{child_pr}: {pr_err}"
                                )

                    # Create ERROR alert (deduplicated by _maybe_create_failure_alert)
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"Evaluation timeout after {hours}h — children did not complete",
                    )
                    await session.commit()
                    # task.status=FAILED / children=CANCELLED were set above; the
                    # PR-close calls + alert + commit can poison/expire these
                    # instances, so broadcast the known literals rather than
                    # post-commit lazy-loads. child.id (PK) is always safe to read.
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.FAILED},
                    )
                    for child in non_terminal_children:
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(child.id), "status": TaskStatus.CANCELLED},
                        )
                    for child_vm in vms_to_delete:
                        await _ensure_vm_deleted(child_vm, reason="parent_eval_timeout")
                    continue

                # 2-hour warning
                if elapsed > EVALUATING_WARN_SECONDS:
                    hours = round(elapsed / 3600, 1)
                    # Deduplicate: skip if an undismissed alert exists OR one was
                    # dismissed within the last 2 hours (prevents re-firing after dismiss).
                    dismiss_cooldown = _utcnow() - timedelta(hours=2)
                    existing = (
                        (
                            await session.execute(
                                select(Alert).where(
                                    Alert.task_id == task.id,
                                    or_(
                                        Alert.dismissed_at.is_(None),
                                        Alert.dismissed_at >= dismiss_cooldown,
                                    ),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing is None:
                        logger.warning(
                            f"[EVAL TIMEOUT] Task {str(task.id)[:8]} has been EVALUATING "
                            f"for {hours}h — children may be stuck"
                        )
                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.WARNING,
                            title=f"Task {task.title or str(task.id)[:8]} stuck evaluating",
                            message=(
                                f"Task has been evaluating for {hours} hours "
                                f"— children may be stuck"
                            ),
                            task_id=task.id,
                        )
                        session.add(alert)
                        await session.commit()
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "alert_created",
                            {
                                "alert_id": str(alert.id),
                                "severity": "WARNING",
                                "title": alert.title,
                            },
                        )
    except Exception as e:
        logger.error(
            f"[EVAL TIMEOUT] Evaluating timeout sweep failed: {e}", exc_info=True
        )
