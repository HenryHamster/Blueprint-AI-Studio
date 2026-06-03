"""Parent-child task lifecycle orchestration.

Extracted from main.py to reduce its size.  Contains the core task state
machine: _process_post_commit, _check_and_trigger_rollup, _spawn_pipeline_task,
and supporting functions.

Deferred imports of the extracted helpers (``_maybe_create_failure_alert`` from
``app.alert_dispatch``, ``_utcnow`` from ``app.time_utils``) are intentional —
they avoid circular imports (task_lifecycle is imported BY main) and preserve
test-patchability (tests mock via the helper's home module, e.g.
``app.alert_dispatch._maybe_create_failure_alert``).
"""

import asyncio
import contextlib
import json
import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    TaskHistory,
    Project,
    TaskStatus,
    ArtifactType,
    Alert,
    AlertSeverity,
    ProjectStatus,
)
from app.schemas import AgentStatusPayload
from app.shared_state import (
    runtime_config as _runtime_config,
    derive_task_branch as _derive_task_branch,
)
from app.task_capabilities import WRITE_TASK_TYPES
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project  # noqa: F401 — re-exported for test patching
from app.payload_schemas import validate_internal_payload
from app.review_pipeline import (
    REVIEW_FIX_GOAL_PREAMBLE,
    _spawn_pipeline_task,
    build_ci_failure_fix_prompt,
    detect_fix_all_issue_class,
    fix_all_occurrences_directive,
    should_auto_approve_diff,
)
from app.failure_classification import classify_failure
from app.branch_resolution import (
    classify_rollup_merge_outcome,
    is_custom_staged_rollup_root,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import apply_merged_landed_signal
from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches
from app.rollup_locking import _lock_original_task, _OriginalTaskLockTimeout
from app.notification_dispatch import (
    is_residual_remediation_task,
    post_remediation_result_message,
)

logger = logging.getLogger(__name__)


# Write task types whose COMPLETED handler opens a *fresh* PR and routes it
# straight into the review pipeline (the first branch of the completion if/elif
# chain in _process_post_commit). Derived from the canonical WRITE_TASK_TYPES so
# a new write type is honored automatically. Two deliberate deviations:
#   • "fix" is excluded — fix tasks have their own `elif task_type == "fix"`
#     branch (they iterate an EXISTING PR, not open a new one).
#   • "" is included — a legacy/untyped task defaults to create-like routing.
_PR_OPENING_COMPLETION_TYPES: frozenset[str] = (WRITE_TASK_TYPES - {"fix"}) | {""}


# Verdict-word aliases used by the structured-verdict fallback parser. Each
# canonical verdict maps to the surface forms an agent might emit. Matched only
# when *anchored* (quoted, JSON-field, or standalone verdict line) so a bare
# prose occurrence of "approve" cannot be read as an approval (see the fallback
# in _process_post_commit).
_VERDICT_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "APPROVE": ("APPROVE", "APPROVED"),
    "REQUEST_CHANGES": ("REQUEST_CHANGES", "REQUEST CHANGES"),
}


def _build_anchored_verdict_pattern(token: str) -> "re.Pattern[str]":
    """Compile the anchored matcher for one canonical verdict token.

    An anchored match is one of:
      - a quoted token:                "APPROVE" / 'APPROVE' / `APPROVE`
      - a JSON-style verdict field:    "verdict": "APPROVE"
      - a standalone verdict line:     APPROVE   (optionally `VERDICT: APPROVE`)

    A *bare* prose occurrence ("do not APPROVE this yet") matches NONE of these
    by design — that is the false-approval class this guards against.
    """
    alts = "|".join(re.escape(t) for t in _VERDICT_TOKEN_ALIASES[token])
    pattern = (
        rf"""(["'`])(?:{alts})\1"""
        rf"""|["']verdict["']\s*:\s*["'`](?:{alts})["'`]"""
        rf"""|^\s*(?:verdict\s*[:=]\s*)?(?:{alts})\s*\.?\s*$"""
    )
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


_ANCHORED_APPROVE_RE = _build_anchored_verdict_pattern("APPROVE")
_ANCHORED_REQUEST_CHANGES_RE = _build_anchored_verdict_pattern("REQUEST_CHANGES")


def _last_match_start(pattern: "re.Pattern[str]", text: str) -> int:
    """Return the start index of the *last* match, or -1 when there is none."""
    last = -1
    for m in pattern.finditer(text):
        last = m.start()
    return last


def _resolve_fallback_verdict(verdict_text: str) -> str | None:
    """Resolve a review verdict from free text when structured parsing failed.

    This is the last-resort fallback after the verdict_json field (Method 1) and
    JSON-in-log parsing (Method 2). It requires a STRUCTURED / anchored approval
    signal — NOT a bare prose substring — so prose like "do not APPROVE this yet"
    or "needs changes before I can approve" can never be read as an approval and
    merge unreviewed code.

    Returns:
      - "APPROVE" / "REQUEST_CHANGES" when an anchored verdict is found
        (rightmost anchored token wins, so a later REQUEST_CHANGES overrides an
        earlier APPROVE), or
      - None when no anchored verdict is present (caller defaults to the safe
        REQUEST_CHANGES + manual-review path).
    """
    approve_idx = _last_match_start(_ANCHORED_APPROVE_RE, verdict_text)
    rc_idx = _last_match_start(_ANCHORED_REQUEST_CHANGES_RE, verdict_text)
    if approve_idx == -1 and rc_idx == -1:
        return None
    if approve_idx > rc_idx:
        return "APPROVE"
    return "REQUEST_CHANGES"


class PostCommitLocked(Exception):
    """Raised when _process_post_commit can't acquire the task row within the
    retry budget. Distinct from DBAPIError so the caller in agent_status.py
    can treat "row still held by peer" as a retryable pending, not an error.
    """


class PostCommitFinalCommitFailed(Exception):
    """Raised when _process_post_commit's FINAL commit fails after the
    post-terminal chain ran.

    The final commit flushes the best-effort post-terminal side effects
    (spawned follow-up / remediation tasks, rollup nudges, analytics,
    artifacts). If it fails, those side effects are DROPPED — they were never
    persisted. The task's own terminal status is safe (it was committed on the
    early-commit / status-endpoint session before post-commit ran), so this
    is NOT a fatal error, but the dropped side effects MUST be retried.

    Distinct signal so the callers (inline status endpoint, the terminal
    idempotency retry path, _sweep_post_commit_pending) PRESERVE the
    write-ahead ``post_commit_pending`` marker for the sweep to retry instead
    of clearing it on what would otherwise look like a clean return. Crucially
    the inline / retry callers translate it to a clean 200 ("post_commit_retry
    _pending") rather than re-raising a 500 to the agent — the work survives in
    the marker, so there is nothing for the agent to do. See incident
    eefbed05: a swallowed final-commit failure cleared the marker, the sweep
    never retried, and the dependent chain orphaned.
    """


from app.failure_stage import (  # noqa: E402, F401, RUF100
    _TRANSIENT_SCAN_ERROR_PATTERNS,
    _derive_failure_stage,
    _is_transient_scan_error,
)


from app.retry_prompt import (  # noqa: E402, F401, RUF100
    _ANALYSIS_ARTIFACT_KEYS,
    _BUILD_GATE_PATTERN,
    _RETRY_SUFFIX_AUTO_PREFIXES,
    _SCOPE_OVERFLOW_THRESHOLDS,
    _append_approve_to_review_history,
    _build_retry_prompt_suffix,
    _build_scope_overflow_details,
    _copy_analysis_artifacts_to_previous,
    _detect_ci_review_disagreement,
    _extract_error_files,
    _extract_failing_tests,
    merge_retry_prompt_suffix,
)


from app.task_lifecycle_requeue import (  # noqa: E402, F401, RUF100
    _record_parent_failure,
    _requeue_parent_task,
)


from app.task_lifecycle_github_helpers import (  # noqa: E402, F401, RUF100
    _comment_with_retry,
    _get_approval_gate_file_lists,
    _merge_with_retry,
    _pick_merge_method,
)


async def _has_active_strategic_task(project_id, session: AsyncSession) -> bool:
    """Return True if any QUEUED/PROVISIONING/IN_PROGRESS task is a design_doc_update or strategic_review.

    Takes a FOR UPDATE lock on the project row so the check+spawn is transactionally
    atomic. Without it, two concurrent callers (e.g. design_doc completion chaining
    to phase 2 and an autonomous-research tick) can both read "none active" in the
    same window and each insert a duplicate strategic task.

    Phase A1: skip_locked=True. If the project row is held by another caller,
    we return True (fail-safe — defer this spawn to the next cycle); the
    other caller will resolve the spawn in their own transaction. Returning
    False under contention would risk both callers concluding "none active"
    and double-spawning, the exact race this lock is here to prevent.

    Phase L-C4 audit: this aggregate read after the project lock is NOT
    upgraded to SERIALIZABLE because the Project lock acquired here is the
    canonical (Project before Task) gate for any strategic spawn — every
    spawn path goes through the same lock helper (now centralized in
    ``lock_project_for_strategic_transition``), so phantom ``Task`` rows
    appearing between this SELECT and the eventual INSERT are impossible
    by construction: any concurrent inserter would have to be holding the
    same project row, which our SKIP LOCKED here just rejected (we already
    returned True). Symmetry assumption confirmed: as long as Project is
    locked before any strategic-task SELECT/INSERT, repeatable-read is
    sufficient and SERIALIZABLE upgrades aren't required. See
    ``docs/concurrency.md`` for the full canonical-order argument.
    """
    result = await session.execute(
        select(Project.id)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=True)
    )
    if result.first() is None:
        # Row missing or held — fail safe. The other holder will spawn (or
        # already has); our caller will skip and re-evaluate next cycle.
        return True
    # Phase B4 (H-P6): we only need first_payload.type per row to filter, so
    # drop status_log/plan_output JSONB. Loading payloads keeps first_payload
    # cheap (L-P2 — without payloads loaded, accessing first_payload would
    # trigger a per-row reload).
    active = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(Task.id, Task.status, Task.payloads, Task.project_id)
                )
                .where(
                    Task.project_id == project_id,
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
    return any(
        (t.first_payload).get("type") in ("design_doc_update", "strategic_review")
        for t in active
    )


async def _increment_research_spawn_count(
    project: Project, session: AsyncSession
) -> int:
    """Increment _research_spawn_count in project.vision and persist. Returns new count."""
    new_count = (project.vision or {}).get("_research_spawn_count", 0) + 1
    project.vision = {**(project.vision or {}), "_research_spawn_count": new_count}
    flag_modified(project, "vision")
    session.add(project)
    return new_count


async def _mark_dependents_warm_claimable(landed_task: Task) -> None:
    """Completion-side warm-reuse trigger for PRs that land on the integration
    branch from inside ``_process_post_commit`` (the small-scope auto-merge and
    review-approved merge paths).

    The orchestrator's CI-completion path (cluster 1) already stamps the sticky
    ``_deps_satisfied`` readiness marker on the dependents a landing unblocks,
    but the in-process merge paths here are a second class of integration
    landing that previously had no completion-side trigger: the just-unblocked
    dependent stayed unmarked until a later capacity-and-window-bounded dispatch
    scan happened to reach it, so ``landed_task``'s warm VM — polling for
    follow-on work inside its teardown-defer grace window — skipped it and tore
    down cold while the orchestrator cold-provisioned a fresh VM for that very
    dependent.

    Fully delegated to the single authoritative implementation in
    ``orchestrator._mark_unblocked_dependents`` (same GitHub-aware dependency
    resolution, its own sessions with no partial projection, and no loosening
    of the readiness signal — only the ``"eligible"`` result is stamped). The
    delegated call opens its own sessions, so it must run AFTER the caller has
    committed and released its FOR UPDATE lock. Best-effort: any failure here
    must never abort post-commit processing. Deferred import avoids a circular
    import (orchestrator is a heavy module that is already loaded by call time).
    """
    try:
        from app.orchestrator import _mark_unblocked_dependents
        from app.rollup_management import _verify_pr_merged

        unblocked = await _mark_unblocked_dependents(landed_task, _verify_pr_merged)
        if unblocked:
            logger.info(
                "[PIPELINE] Task %s landing unblocked %d dependent(s) — marked warm-claimable",
                landed_task.id,
                unblocked,
            )
    except Exception:
        logger.exception(
            "[PIPELINE] Dependent warm-claim marking failed for task %s",
            landed_task.id,
        )


async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
) -> dict:
    """Post-commit processing: review pipeline, completion handlers, rollup, aggregation.

    Uses its own session to avoid holding the FOR UPDATE lock from the
    status endpoint while making GitHub API calls (which can hang 15-120s).
    _session_factory is injectable for tests (defaults to AsyncSessionLocal).
    """
    from app.time_utils import _utcnow
    from app.broadcast import broadcast_to_project  # noqa: F811
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        merge_pull_request,
        get_pull_request,
        get_pr_files,
        get_check_runs_cached,
        submit_pr_review,
        update_check_run,
        close_pull_request,
    )
    from app.task_completion import (
        _handle_plan_completion,
        _handle_design_doc_completion,
        _handle_security_audit_completion,
        _handle_perf_completion,
        _handle_self_audit_completion,
        _handle_memo_completion,
        _try_recover_memo_from_parse_failure,
        _record_remediation_if_terminal,
    )
    from app.strategic_completion import (
        _handle_strategic_completion,
        _record_strategic_task_failure,
    )
    from app.research_completion import _handle_research_completion

    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        # Round 3 C3 + C4: strategic tasks (design_doc_update, strategic_review)
        # have a downstream completion chain that needs both the Project and
        # the Task locked. The canonical order is **Project before Task,
        # always** (docs/concurrency.md). Locking Task first then Project
        # second reintroduces the cross-table deadlock vector C5 originally
        # closed, so we peek at the task type via a non-locking read and use
        # the canonical pair-locker for strategic types. Non-strategic types
        # use the existing task-only lock pattern.
        _peek_row = (
            await session.execute(
                select(Task.payloads, Task.project_id).where(Task.id == task_id)
            )
        ).first()
        if _peek_row is None:
            return {"status": "accepted"}
        _peek_payload_list = _peek_row[0] or []
        _peek_first = _peek_payload_list[0] if _peek_payload_list else {}
        _peek_type = _peek_first.get("type") if isinstance(_peek_first, dict) else None
        _peek_project_id = _peek_row[1]
        _is_strategic_type = _peek_type in ("design_doc_update", "strategic_review")

        # skip_locked + bounded retry: the row is typically only held briefly
        # (by the agent_status early-commit path that just released it, or by a
        # concurrent post-commit for the same task). A plain FOR UPDATE would
        # queue behind GitHub API calls (15-120s) and hit the Postgres
        # lock_timeout as LockNotAvailableError, which tears down the session.
        task = None
        # Round 3 C3 + C4: ``locked_project`` is set ONLY when the post-commit
        # is processing a strategic task and the canonical pair-lock succeeded.
        # The strategic-completion chain handlers below receive this via
        # ``project=`` so they skip their own (legacy) re-acquisition.
        locked_project: Project | None = None
        for _attempt in range(3):
            if _is_strategic_type and _peek_project_id is not None:
                # Canonical Project → Task ordering for strategic tasks. The
                # helper acquires Project first; if the task row is held it
                # yields ``(project, None)`` and we retry. If the project is
                # held it yields ``(None, None)`` and we retry as well.
                from app.task_locking import lock_project_then_task

                async with lock_project_then_task(
                    session,
                    _peek_project_id,
                    task_id,
                    skip_locked=True,
                ) as (_p, _t):
                    if _t is not None:
                        # Both locks acquired. Capture references and break.
                        # SQLAlchemy session-level FOR UPDATE locks persist
                        # past the contextmanager exit; the surrounding
                        # ``async with factory() as session`` commit/rollback
                        # at the end of post-commit releases both row locks.
                        task = _t
                        locked_project = _p
                        break
                    # Half-pair acquired (project but not task) or no pair
                    # (project also held). Release any held lock before
                    # retrying so we don't accumulate session-level locks
                    # across retry iterations and don't hold a project lock
                    # while a peer attempts the same task lock.
                    if _p is not None:
                        await session.rollback()
            else:
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if task is not None:
                    break
            # Confirm the row still exists before retrying — if it's gone the
            # peer holder is irrelevant.
            exists = (
                await session.execute(select(Task.id).where(Task.id == task_id))
            ).first()
            if exists is None:
                return {"status": "accepted"}
            await asyncio.sleep(2)
        if task is None:
            raise PostCommitLocked(f"task {task_id} row held by peer after 3 retries")

        # Phase 0 telemetry: persist warm-VM continuity timing fields into
        # task.context["_timing"] so cold-vs-warm setup cost is observable.
        # Mirrors the existing _shown_playbook_keys storage pattern in
        # routers/agent.py — write all non-None fields under a stable key,
        # respect prior _timing entries (e.g. claim_latency_secs from claim).
        _timing_payload: dict = {}
        if payload.setup_secs is not None:
            _timing_payload["setup_secs"] = float(payload.setup_secs)
        if payload.warm_start is not None:
            _timing_payload["warm_start"] = bool(payload.warm_start)
        if payload.prior_role_vm_id is not None:
            _timing_payload["prior_role_vm_id"] = str(payload.prior_role_vm_id)
        if payload.handler_duration_secs is not None:
            # Wave 5 follow-up: wall-time of the role's handler (post-claim,
            # post-setup). Combined with setup_secs this decomposes total
            # role time so cold-vs-warm gains can be attributed correctly.
            _timing_payload["handler_duration_secs"] = float(
                payload.handler_duration_secs
            )
        # Fallback: derive setup_secs from VM timestamps when the agent exits
        # before computing it (timeout, pre-tool crash). vm_provisioned_at is
        # set at orchestrator cold-provision time and reset to "now" on warm
        # claim, so the difference approximates cold-start boot latency without
        # polluting warm-start buckets. Only writes when not already present.
        if "setup_secs" not in _timing_payload:
            _prior_timing = dict((task.context or {}).get("_timing") or {})
            if (
                "setup_secs" not in _prior_timing
                and task.vm_provisioned_at is not None
                and task.vm_started_at is not None
            ):
                _derived = (task.vm_started_at - task.vm_provisioned_at).total_seconds()
                if _derived >= 0:
                    _timing_payload["setup_secs"] = round(_derived, 1)
        if _timing_payload:
            ctx = dict(task.context or {})
            existing = dict(ctx.get("_timing") or {})
            existing.update(_timing_payload)
            ctx["_timing"] = existing
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)

        # Immediately unblock dependents when task terminally fails (retries exhausted).
        # Uses the same session (not a separate one) to avoid self-deadlock: a second
        # session scanning the task table would block on the FOR UPDATE lock held by
        # this session, causing a hang in Postgres (asyncio is single-threaded).
        if payload.status == TaskStatus.FAILED:
            try:
                dep_stmt = select(Task).where(
                    Task.project_id == task.project_id,
                    Task.status == TaskStatus.QUEUED,
                    Task.depends_on.contains([str(task.id)]),
                )
                dep_result = await session.execute(dep_stmt)
                waiting_tasks = dep_result.scalars().all()
                count = 0
                for wt in waiting_tasks:
                    wt.depends_on = [
                        d for d in (wt.depends_on or []) if d != str(task.id)
                    ]
                    session.add(wt)
                    count += 1
                if count:
                    # Don't commit here — let the session's final commit handle it.
                    # An intermediate commit would release the FOR UPDATE lock while
                    # failure-handling code below still needs the task.
                    logger.info(
                        f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                    )
            except Exception as e:
                logger.error(
                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                    exc_info=True,
                )

            # Record strategic task failures for the circuit breaker and cycle history
            if task.first_payload.get("type") in (
                "design_doc_update",
                "strategic_review",
            ):
                try:
                    # Round 3 C3 + C4: the canonical pair-lock acquired at the
                    # top of _process_post_commit (Project before Task, see
                    # docs/concurrency.md) is passed down here as
                    # ``locked_project``. _record_strategic_task_failure skips
                    # its own re-acquisition when ``project=`` is provided.
                    # Falls back to None for legacy callers / non-strategic
                    # types (the helper does an unlocked session.get(Project)
                    # in that case — the failure-record path doesn't strictly
                    # require the project lock).
                    await _record_strategic_task_failure(
                        task, session, project=locked_project
                    )
                except Exception as _strat_exc:
                    logger.error(
                        f"[STRATEGIC] Failed to record strategic failure for {task.id}: {_strat_exc!r}",
                        exc_info=True,
                    )

            # Record research task failures so _check_direction_health can see
            # cycles that never reached _handle_research_completion (VM crash,
            # provision failure, agent timeout, etc.)
            if task.first_payload.get("type") == "research":
                try:
                    from app.research_completion import _record_research_task_failure

                    await _record_research_task_failure(task, session)
                except Exception as _res_exc:
                    logger.error(
                        f"[RESEARCH] Failed to record research failure for {task.id}: {_res_exc!r}",
                        exc_info=True,
                    )

            # Post result message for permanently failed auto-remediation tasks.
            # Routes to residual lifecycle for residual-fingerprint tasks,
            # CI auto-fix lifecycle otherwise.
            if task.auto_remediation:
                try:
                    await post_remediation_result_message(
                        session,
                        task,
                        result="failed",
                    )
                    await session.commit()
                except Exception as _ar_exc:
                    logger.warning(
                        "[AUTO-REMEDIATION] Failed to post failure result message: %s",
                        _ar_exc,
                    )

            # FAILED memo recovery: when a memo task fails with any of the recoverable
            # failure categories, the agent may have emitted a valid memo JSON inside an
            # item.completed envelope that the deployed handler couldn't extract.
            # We widen the trigger beyond 'parse_failed' to also cover 'incomplete' and
            # 'claude_code_failed' — the recovery function is defensive (returns False
            # when no valid envelope is found) so widening is safe.
            if task.first_payload.get("type") == "memo":
                raw_fc = (task.context or {}).get("agent_failure_category_raw", "")
                if raw_fc in ("parse_failed", "incomplete", "claude_code_failed"):
                    try:
                        _recovery_logs = (
                            payload.logs or task.latest_status_message or ""
                        )
                        _recovered = await _try_recover_memo_from_parse_failure(
                            task, _recovery_logs, session
                        )
                        if _recovered:
                            logger.info(
                                "[MEMO] Promoted parse_failed task %s to COMPLETED via error-log recovery",
                                task.id,
                            )
                    except Exception as _memo_rec_exc:
                        logger.warning(
                            "[MEMO] Recovery attempt failed for task %s: %r",
                            task.id,
                            _memo_rec_exc,
                        )

            # C5: release the cross-cutting safeguard merge gate when the
            # safeguard task itself FAILS. The gate is released only on the
            # security_audit COMPLETED branch (_handle_security_audit_completion),
            # so a FAILED/reaped safeguard would otherwise leave orig.requires_
            # approval=True forever — a permanent strand (services.py then 2h-
            # timeout-FAILs the healthy parent, discarding merged siblings).
            # Pass findings=None: a failed safeguard produced no blocker, so the
            # gate releases (post-hoc review) rather than hanging indefinitely.
            # _maybe_release_safeguard_gate no-ops for non-safeguard tasks.
            if task.first_payload.get("type") == "security_audit":
                try:
                    from app.task_completion import _maybe_release_safeguard_gate

                    await _maybe_release_safeguard_gate(task, None, session)
                except Exception as _sg_exc:
                    logger.warning(
                        "[SAFEGUARD] Failed to release gate on FAILED safeguard %s: %r",
                        task.id,
                        _sg_exc,
                    )

        # Anti-poisoning: resolve playbook entries the task wrote while
        # IN_PROGRESS. On COMPLETED, clear pending_source so peers can see
        # them. On FAILED/CANCELLED, drop them entirely so a task that
        # never landed its code can't leave a "X exists" lie in the
        # playbook. Must run before commit so the design_doc mutation
        # is persisted alongside the rest of the post-commit work.
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            try:
                from app.playbook_learning import finalize_pending_playbook_entries

                await finalize_pending_playbook_entries(
                    task,
                    session,
                    succeeded=(payload.status == TaskStatus.COMPLETED),
                )
            except Exception:
                logger.exception(
                    "[PLAYBOOK] Failed to finalise pending entries for task %s", task.id
                )

        # Audit 2026-04-25 A7/A12: release the FOR UPDATE row lock before the
        # review pipeline runs its slow GitHub calls (PR fetch, check-runs,
        # PR review submission, merge attempts — each can hit GitHub rate
        # limits and stretch into the tens of seconds, occasionally minutes).
        # Heartbeats arriving in that window would otherwise hit the locked
        # row, get None from skip_locked, and return 409. Multiple skipped
        # heartbeats over 5+ minutes would let the reaper false-orphan the
        # task. Commit here flushes all pending state mutations from sections
        # 1–4 above (dependent unblocking, failure recording, investigation  # noqa: RUF003
        # spawns) and releases the lock; the rest of post-commit processing
        # operates on the already-loaded task object without re-locking.
        if session.dirty or session.new:
            await session.commit()

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = task.first_payload
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if (
                task_type in _PR_OPENING_COMPLETION_TYPES
                and payload.pr_url
                and payload.artifact_type == ArtifactType.PULL_REQUEST
            ):
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                _project = await session.get(Project, task.project_id)
                branch = _derive_task_branch(
                    task.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_project),
                )
                base_branch = task_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_project)
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = _project is not None and _project.is_staging_branch(
                    base_branch
                )
                scope = (task.context or {}).get("scope", "unknown")

                # Auto-approve trivially small main-branch diffs: skip review and route
                # through the same security-scan + auto-merge path used for intermediate
                # small/medium scope. CI still gates the merge.
                #
                # QP6: the gate is the single, unit-tested `should_auto_approve_diff`
                # helper (review_pipeline.py) — NOT a weaker inline copy. The helper
                # applies the strict checks the old inline logic skipped: source-type
                # eligibility, test-file exclusion from the line/file budgets, and the
                # scope-overflow warning. The main-branch path keeps two *additional*
                # live constraints on top of the helper: it only runs for non-staging
                # PRs and only when a resolvable direction has favorable acceptance
                # history (a missing/unknown direction is NOT auto-approved here, even
                # though the helper would pass `direction_data=None` by default).
                auto_approve_small = False
                if (
                    not is_intermediate
                    and scope == "small"
                    and _runtime_config.get("auto_approve_small_diff_enabled", True)
                ):
                    try:
                        # Resolve the direction so the helper's acceptance-rate gate
                        # runs against real history. The main-branch path additionally
                        # *requires* a resolvable direction (see comment above).
                        direction_id = (task.context or {}).get("direction_id")
                        _aa_dir = None
                        if direction_id:
                            _aa_proj = (
                                (
                                    await session.execute(
                                        select(Project).where(
                                            Project.id == task.project_id
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _aa_dir = next(
                                (
                                    d
                                    for d in (
                                        getattr(_aa_proj, "directions", None) or []
                                    )
                                    if d.get("id") == direction_id
                                ),
                                None,
                            )
                        # Main-branch path requires a *positive* acceptance signal:
                        # an unknown direction or a direction with no merge/reject
                        # history (acceptance == None) is NOT auto-approved here. This
                        # is intentionally stricter than the helper's default (which
                        # passes unknown acceptance) because main-branch merges skip
                        # review entirely.
                        from app.direction_metrics import _compute_acceptance_rate

                        acceptance = (
                            _compute_acceptance_rate(_aa_dir)
                            if _aa_dir is not None
                            else None
                        )
                        if acceptance is None or acceptance <= 0.5:
                            logger.info(
                                f"[PIPELINE] PR #{pr_number} not auto-approved — "
                                f"direction acceptance={acceptance} (require >0.5 with "
                                "history); defaulting to review"
                            )
                        else:
                            skip, reason = should_auto_approve_diff(task, _aa_dir)
                            if skip:
                                auto_approve_small = True
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} auto-approved for "
                                    f"merge ({reason}, acceptance={acceptance:.2f}) — "
                                    "skipping review"
                                )
                            else:
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} not auto-approved "
                                    f"({reason}) — defaulting to review"
                                )
                    except Exception as _aa_exc:
                        logger.info(
                            f"[PIPELINE] Auto-approve eligibility check failed for PR #{pr_number}: {_aa_exc!r} — defaulting to review"
                        )

                if (
                    is_intermediate and scope in ("small", "medium")
                ) or auto_approve_small:
                    # Small/medium scope on intermediate branch → security scan then auto-merge, skip review
                    # The rollup PR to main gets the full review — intermediate merges don't need it
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )

                        # Always run security scan before any merge
                        try:
                            scan_result = await scan_pr_for_injection(
                                gh_token, owner, repo, pr_number
                            )
                            logger.info(
                                f"[SECURITY] Small-scope scan: {scan_result['summary']}"
                            )
                            if scan_result["blocked"]:
                                task.requires_approval = True
                                task.latest_status_message = (
                                    f"Security scan blocked: {scan_result['summary']}"
                                )
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                                return {"status": "accepted"}
                            if scan_result["findings"]:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    scan_result["report"],
                                )
                        except Exception as e:
                            if _is_transient_scan_error(e):
                                logger.warning(
                                    f"[SECURITY] Small-scope scan transient error for PR #{pr_number}, retrying: {e}"
                                )
                                await asyncio.sleep(5)
                                try:
                                    scan_result = await scan_pr_for_injection(
                                        gh_token, owner, repo, pr_number
                                    )
                                    logger.info(
                                        f"[SECURITY] Small-scope scan retry succeeded: {scan_result['summary']}"
                                    )
                                    if scan_result["blocked"]:
                                        task.requires_approval = True
                                        task.latest_status_message = f"Security scan blocked: {scan_result['summary']}"
                                        session.add(task)
                                        if task.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    task,
                                                    result="review_needed",
                                                    pr_number=pr_number,
                                                )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                    _ar_exc,
                                                )
                                        await session.commit()
                                        if scan_result["findings"]:
                                            await create_pr_comment(
                                                gh_token,
                                                owner,
                                                repo,
                                                pr_number,
                                                scan_result["report"],
                                            )
                                        return {"status": "accepted"}
                                    if scan_result["findings"]:
                                        await create_pr_comment(
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number,
                                            scan_result["report"],
                                        )
                                except Exception as retry_exc:
                                    logger.error(
                                        f"[SECURITY] Small-scope scan retry also failed for PR #{pr_number}: {retry_exc}",
                                        exc_info=True,
                                    )
                                    task.requires_approval = True
                                    task.context = {
                                        **(task.context or {}),
                                        "scan_transient_failure": True,
                                    }
                                    task.latest_status_message = f"Security scan unavailable (infrastructure error) — requires manual approval: {type(retry_exc).__name__}: {retry_exc!r}"[
                                        :500
                                    ]
                                    session.add(task)
                                    if task.auto_remediation:
                                        try:
                                            await post_remediation_result_message(
                                                session,
                                                task,
                                                result="review_needed",
                                                pr_number=pr_number,
                                            )
                                        except Exception as _ar_exc:
                                            logger.warning(
                                                "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                _ar_exc,
                                            )
                                    await session.commit()
                                    return {"status": "accepted"}
                            else:
                                logger.error(
                                    f"[SECURITY] Small-scope scan failed for PR #{pr_number}, blocking: {e}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.latest_status_message = f"Security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[
                                    :500
                                ]
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                return {"status": "accepted"}

                        merge_result = await _merge_with_retry(
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                            merge_method=_pick_merge_method(task),
                        )
                        if merge_result.get("merged"):
                            _record_pr_merged = is_intermediate
                            if not _record_pr_merged:
                                _record_pr_merged = await (
                                    merged_pr_counts_as_integration_landing(
                                        task,
                                        _project,
                                        session,
                                        pr_number=pr_number,
                                        merged_base_branch=base_branch,
                                        source="task_lifecycle.small_scope_auto_merge",
                                    )
                                )
                            if _record_pr_merged:
                                apply_merged_landed_signal(
                                    task,
                                    _project,
                                    base_branch=base_branch,
                                    hotfix_lane=bool(
                                        (task.first_payload or {}).get("hotfix")
                                        or (task.context or {}).get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                )
                            session.add(task)
                            _t_ctx = task.context or {}
                            # Audit W1: auto_remediation is now a column.
                            if _t_ctx.get("diagnosis_task_id") or (
                                task.auto_remediation and _t_ctx.get("direction_id")
                            ):
                                try:
                                    from app.task_completion import (
                                        _record_remediation_outcome,
                                    )

                                    await _record_remediation_outcome(
                                        task, merged=True, session=session
                                    )
                                except Exception as _rr_exc:
                                    logger.warning(
                                        f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {task.id}: {_rr_exc!r}"
                                    )
                            await session.commit()
                            await _cleanup_branches(task, session)
                            # Completion-side warm-reuse trigger: the PR has
                            # landed on integration and the lock is released, so
                            # proactively mark any dependents this just unblocked
                            # as warm-claimable for this task's teardown-defer VM.
                            if _record_pr_merged:
                                await _mark_dependents_warm_claimable(task)
                            logger.info(
                                f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} (small scope, intermediate)"
                            )
                        else:
                            logger.error(
                                f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}"
                            )
                            # Probe GitHub for the PR's mergeable_state so we can
                            # route the recovery correctly:
                            #   blocked / unstable → CI gating; queue GitHub auto-merge
                            #     and let it fire when checks clear.
                            #   dirty → real merge conflict (sibling cluster touched
                            #     overlapping files between PR creation and the merge
                            #     attempt). Spawn a `fix` task — fix is the rebase-
                            #     and-resolve primitive; review is the assess-and-
                            #     give-feedback primitive, the wrong tool here.
                            #     Without this, cluster sub-PRs orphan: tasks COMPLETE,
                            #     the dep chain stays QUEUED on PR_MERGED, and the
                            #     research rollup stalls (see PR#4070, PR#4044 incident
                            #     on 2026-05-06).
                            #   default → fall back to review-spawn (preserves
                            #     existing behavior for unknown states).
                            #
                            # Reviews still fire downstream: when the fix task
                            # COMPLETES, the fix-completion handler at line ~2899
                            # spawns a review task before merging. So a DIRTY-routed
                            # cluster sub-PR gets: create → conflict → fix (rebase)
                            # → review → auto-merge. This change does not bypass
                            # review for tasks that need it.
                            recovery_type = "review"
                            try:
                                from app.github import enable_auto_merge

                                pr_detail = await get_pull_request(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    project_id=str(task.project_id),
                                )
                                ms = (pr_detail.get("mergeable_state") or "").lower()
                                if ms in ("blocked", "unstable", "clean"):
                                    am_result = await enable_auto_merge(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        project_id=str(task.project_id),
                                    )
                                    if am_result.get("enabled"):
                                        logger.info(
                                            f"[PIPELINE] Enabled GitHub auto-merge on PR #{pr_number} (mergeable_state={ms})"
                                        )
                                    else:
                                        logger.info(
                                            f"[PIPELINE] Auto-merge enablement declined for PR #{pr_number}: {am_result.get('message')}"
                                        )
                                    if ms == "clean":
                                        # PR is clean/ready — transient API failure, not a real
                                        # conflict. Skip recovery task spawn; let auto-merge
                                        # fire when GitHub is ready.
                                        recovery_type = None
                                elif ms == "dirty":
                                    recovery_type = "fix"
                            except Exception as _am_exc:
                                logger.warning(
                                    f"[PIPELINE] Mergeable-state probe failed for PR #{pr_number}: {_am_exc}"
                                )
                            # Spawn the recovery task. For DIRTY conflicts on cluster
                            # sub-PRs we use `fix` (rebase + resolve); otherwise
                            # `review` (the existing default).
                            # When recovery_type is None (clean state) no spawn fires —
                            # auto-merge was re-enabled above and the task parks in CI-wait.
                            #
                            # Pass fix_reason="merge_conflict" so the fix-completion
                            # handler can route straight to security-scan + auto-merge
                            # WITHOUT a re-review. Rationale (per the small/medium
                            # intermediate path): the original task was scope-gated
                            # to skip review entirely; once the conflict is resolved
                            # there's no review-worthy logic change to re-assess —
                            # rebase output is mechanical. This keeps the cost model
                            # symmetric with the no-conflict happy path.
                            if recovery_type is None:
                                logger.info(
                                    f"[PIPELINE] Auto-merge failed for PR #{pr_number} (mergeable_state=clean) — re-enabled auto-merge, parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge failed for PR #{pr_number} (clean state) — parked for CI-wait retry"
                                # Small/medium intermediate (and auto_approve_small) explicitly
                                # skip review — no review task will ever set review_approved.
                                # mergeable_state="clean" also implies CI is already green.
                                # Pre-satisfy both gate flags so handle_ci_completion can merge
                                # on the next signal instead of stranding the task on
                                # _review_approved.
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "review_approved": True,
                                        "ci_passed": True,
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                    },
                                }
                                session.add(task)
                                await session.commit()
                            else:
                                spawn_kwargs = {
                                    "original_prompt": original_prompt,
                                    "original_task_id": str(task.id),
                                }
                                if recovery_type == "fix":
                                    spawn_kwargs["fix_reason"] = "merge_conflict"
                                    spawn_kwargs["fix_prompt"] = (
                                        f"PR #{pr_number} cannot merge into `{base_branch}` due to a merge conflict "
                                        f"(GitHub mergeable_state='dirty'). The code review is already approved — "
                                        f"focus only on resolving the conflict.\n\n"
                                        f"After cloning:\n"
                                        f"  1. Run `git fetch origin {base_branch} && git rebase origin/{base_branch}`.\n"
                                        f"  2. Resolve every conflict marker — preserve the intent of both branches.\n"
                                        f"  3. Re-run lint/type/test suites for the affected files.\n"
                                        f"  4. Commit the resolution and force-push with `--force-with-lease`."
                                    )
                                recovery_task, was_created = await _spawn_pipeline_task(
                                    task,
                                    recovery_type,
                                    session,
                                    pr_number,
                                    branch,
                                    base_branch,
                                    **spawn_kwargs,
                                )
                                if was_created:
                                    logger.info(
                                        f"[PIPELINE] Spawned {recovery_type} task {recovery_task.id} for PR #{pr_number} after auto-merge failure"
                                    )
                                elif recovery_task is not None:
                                    logger.info(
                                        f"[PIPELINE] Auto-merge failed for PR #{pr_number} — same-chain {recovery_task.first_payload.get('type')} task {recovery_task.id} ({recovery_task.status}) already handling it"
                                    )
                                else:
                                    logger.warning(
                                        f"[PIPELINE] Auto-merge failed and no {recovery_type} spawned for PR #{pr_number} (cap hit or guard) — parking in CI-wait"
                                    )
                                    task.status = TaskStatus.EVALUATING
                                    task.latest_status_message = f"Auto-merge failed for PR #{pr_number} — parked for CI-wait retry"
                                    task.context = {
                                        **(task.context or {}),
                                        "review_loop": {
                                            "ci_waiting": True,
                                            "pr_number": pr_number,
                                            "branch": branch,
                                            "base_branch": base_branch,
                                            "original_task_id": str(task.id),
                                            "ci_wait_started": _utcnow().isoformat(),
                                        },
                                    }
                                    session.add(task)
                                    await session.commit()
                    except Exception as e:
                        logger.error(
                            f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}",
                            exc_info=True,
                        )
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            (
                                fallback_review,
                                fb_was_created,
                            ) = await _spawn_pipeline_task(
                                task,
                                "review",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                original_prompt=original_prompt,
                                original_task_id=str(task.id),
                            )
                            if fb_was_created:
                                logger.info(
                                    f"[PIPELINE] Spawned fallback review {fallback_review.id} for PR #{pr_number} after merge error"
                                )
                            elif fallback_review is not None:
                                logger.info(
                                    f"[PIPELINE] Merge errored for PR #{pr_number} — same-chain task {fallback_review.id} already handling it"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] Merge error and no fallback review spawned for PR #{pr_number} — parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge error for PR #{pr_number} — parked for CI-wait retry"
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                    },
                                }
                                session.add(task)
                                await session.commit()
                        except Exception as inner_e:
                            logger.error(
                                f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {inner_e}",
                                exc_info=True,
                            )
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Pipeline recovery: merge and review spawn failed for PR #{pr_number} — CI monitor will retry"
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    "ci_waiting": True,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": str(task.id),
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        scan_result = await scan_pr_for_injection(
                            gh_token, owner, repo, pr_number
                        )
                        logger.info(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(
                                gh_token, owner, repo, pr_number, scan_result["report"]
                            )
                    except Exception as e:
                        if _is_transient_scan_error(e):
                            logger.warning(
                                f"[SECURITY] Scan transient error for PR #{pr_number}, retrying: {e}"
                            )
                            await asyncio.sleep(5)
                            try:
                                scan_result = await scan_pr_for_injection(
                                    gh_token, owner, repo, pr_number
                                )
                                logger.info(
                                    f"[SECURITY] Scan retry succeeded: {scan_result['summary']}"
                                )
                                if scan_result["blocked"]:
                                    task.requires_approval = True
                                    task.latest_status_message = scan_result["summary"]
                                    session.add(task)
                                    await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                            except Exception as retry_exc:
                                logger.error(
                                    f"[SECURITY] Scan retry also failed for PR #{pr_number}: {retry_exc}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.context = {
                                    **(task.context or {}),
                                    "scan_transient_failure": True,
                                }
                                task.latest_status_message = f"Security scan unavailable (infrastructure error) — review will proceed but merge requires manual approval: {type(retry_exc).__name__}: {retry_exc!r}"[
                                    :500
                                ]
                                session.add(task)
                                await session.commit()
                        else:
                            logger.error(
                                f"[SECURITY] Scan failed for PR #{pr_number}, flagging for approval: {e}",
                                exc_info=True,
                            )
                            task.requires_approval = True
                            task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[
                                :500
                            ]
                            session.add(task)
                            await session.commit()

                    # PR file-path flagging: flag infrastructure/security-critical file changes
                    # Don't block here — let review run, block at merge time.
                    # Gated by require_approval_gates — uses project config if available,
                    # falls back to Blueprint defaults for self-repo.
                    if _runtime_config.get("require_approval_gates", True):
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, pr_number
                            )
                            # Fetch project to get approval gate config
                            project = await session.get(Project, task.project_id)
                            INFRA_FILES, CRITICAL_FILES = _get_approval_gate_file_lists(
                                project
                            )
                            touched_infra = [
                                f["filename"]
                                for f in pr_files
                                if f["filename"] in INFRA_FILES
                            ]
                            touched_critical = [
                                f for f in touched_infra if f in CRITICAL_FILES
                            ]
                            if touched_infra:
                                task.context = {
                                    **(task.context or {}),
                                    "extended_monitoring": True,
                                    "risk_tier": "large",
                                    "touched_infra": touched_infra,
                                    "touched_critical": touched_critical,
                                }
                                task.latest_status_message = f"Infrastructure change flagged: {', '.join(touched_infra)}"
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} touches infra files: {touched_infra} (critical: {touched_critical})"
                                )
                                session.add(task)
                                await session.commit()
                                _shared_state.deploy_monitoring["is_extended"] = True
                        except Exception as e:
                            logger.info(
                                f"[PIPELINE] Could not check PR files for infra flagging: {e}"
                            )

                    # Always spawn the review — requires_approval gates MERGING
                    # (checked at merge time in the review-complete path), not
                    # reviewing. A security scan failure or blocking finding
                    # should not orphan the PR by preventing review entirely.
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_prompt=original_prompt,
                        original_task_id=str(task.id),
                    )
                    if review_task:
                        if task.requires_approval:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for flagged PR #{pr_number} → task {review_task.id} (merge will require manual approval)"
                            )
                        else:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}"
                            )
                    else:
                        task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}"
                        )

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                _review_project = await session.get(Project, task.project_id)
                base_branch = (
                    review_loop.get("base_branch")
                    or task_payload.get("base_branch")
                    or _shared_state.get_project_integration_branch(_review_project)
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")

                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Review pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip only when the PR is genuinely merged/done.
                # C4: this MUST gate on merged==True, NOT on state=='closed'.
                # A non-merged 'closed' state may be a transient self-close
                # from the orchestrator's mergeability-reset loop (close+reopen
                # to force GitHub to recompute mergeable=None — see
                # orchestrator.py close/reopen nudge). Treating that transient
                # closed window as "review skipped" silently DROPS the
                # review_approved handoff to the parent, stranding the rollup
                # in EVALUATING until the 60-min/6h backstop fires. Gating on
                # merged only — and falling through on closed-not-merged so the
                # review verdict still reaches the parent — mirrors the fix
                # handler's already-corrected pattern (PR #5662).
                try:
                    (
                        gh_token_check,
                        owner_check,
                        repo_check,
                    ) = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(
                        gh_token_check, owner_check, repo_check, pr_number
                    )
                    if pr_data_check.get("merged"):
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} already merged — skipping review pipeline for task {task.id}"
                        )
                        task.latest_status_message = (
                            f"PR #{pr_number} already merged — review skipped"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                    if pr_data_check.get("state") == "closed":
                        # Closed but not merged — could be a transient
                        # mergeability-reset. Proceed with the review so the
                        # review_approved handoff is preserved; the merge gate
                        # handles a genuinely abandoned PR gracefully.
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} is closed (not merged) for task {task.id} — "
                            f"may be a transient mergeability-reset; proceeding with review"
                        )
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review"
                    )

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text
                issues = []
                investigated_files: list[str] = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                import re

                verdict_parsed = False
                verdict_unparseable = False
                review_was_parallelized = False

                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            issues = (
                                vj.get("issues", [])
                                if isinstance(vj.get("issues"), list)
                                else []
                            )
                            raw_investigated = vj.get("investigated_files")
                            if isinstance(raw_investigated, list):
                                investigated_files = [
                                    str(p)
                                    for p in raw_investigated
                                    if isinstance(p, str) and p
                                ][:25]
                            review_was_parallelized = bool(
                                vj.get("review_was_parallelized", False)
                            )
                            verdict_parsed = True
                            logger.info(
                                f"[REVIEW PIPELINE] Verdict from verdict_json field: {verdict}"
                            )
                    except (json.JSONDecodeError, TypeError):
                        logger.exception(
                            f"[REVIEW PIPELINE] verdict_json field present but invalid for task {task.id}"
                        )

                # Method 2: Parse JSON from log output (string-aware bracket counting)
                if not verdict_parsed:
                    try:
                        for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                            start = m.start()
                            depth = 0
                            in_string = False
                            escape_next = False
                            for i in range(start, len(verdict_text)):
                                c = verdict_text[i]
                                if escape_next:
                                    escape_next = False
                                    continue
                                if c == "\\" and in_string:
                                    escape_next = True
                                    continue
                                if c == '"' and not escape_next:
                                    in_string = not in_string
                                    continue
                                if in_string:
                                    continue
                                if c == "{":
                                    depth += 1
                                elif c == "}":
                                    depth -= 1
                                    if depth == 0:
                                        try:
                                            candidate = json.loads(
                                                verdict_text[start : i + 1]
                                            )
                                            if "verdict" in candidate:
                                                verdict = candidate["verdict"]
                                                feedback = candidate.get(
                                                    "feedback", verdict_text
                                                )
                                                issues = (
                                                    candidate.get("issues", [])
                                                    if isinstance(
                                                        candidate.get("issues"), list
                                                    )
                                                    else []
                                                )
                                                verdict_parsed = True
                                        except json.JSONDecodeError:
                                            pass
                                        break
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}",
                            exc_info=True,
                        )

                if not verdict_parsed:
                    # Fallback: require a STRUCTURED / anchored verdict signal — NOT a
                    # bare prose substring (see _resolve_fallback_verdict). Returns the
                    # canonical verdict, or None when no anchored verdict is present.
                    fallback_verdict = _resolve_fallback_verdict(verdict_text)
                    if fallback_verdict is None:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(
                            f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review"
                        )
                        verdict_unparseable = True
                    else:
                        verdict = fallback_verdict

                # Invariant: pipeline accepts only APPROVE or REQUEST_CHANGES.
                # Legacy COMMENT, typos, and other agent regressions normalize to
                # REQUEST_CHANGES so the pipeline always has a well-defined next step.
                if verdict not in ("APPROVE", "REQUEST_CHANGES"):
                    logger.warning(
                        f"[REVIEW PIPELINE] Task {task.id} produced unsupported verdict "
                        f"{verdict!r} — normalizing to REQUEST_CHANGES"
                    )
                    verdict = "REQUEST_CHANGES"

                # If verdict was unparseable, flag the original task for manual review
                if verdict_unparseable:
                    source_loop = (task.context or {}).get("review_loop", {})
                    original_task_id = source_loop.get("original_task_id")
                    if original_task_id:
                        try:
                            orig = await session.get(Task, uuid.UUID(original_task_id))
                            if orig:
                                orig.requires_approval = True
                                orig.latest_status_message = "Review agent did not produce a verdict — manual review required"
                                session.add(orig)
                                await session.commit()
                        except Exception as e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to mark requires_approval on {original_task_id}: {e}",
                                exc_info=True,
                            )

                # Post the review to the PR via GitHub Reviews API (falls back to plain comment)
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    event = "APPROVE" if verdict == "APPROVE" else "REQUEST_CHANGES"
                    inline_comments = []
                    body_issues = []
                    for issue in issues:
                        if issue.get("file") and issue.get("line"):
                            sev = issue.get("severity", "minor").upper()
                            inline_comments.append(
                                {
                                    "path": issue["file"],
                                    "line": issue["line"],
                                    "body": f"[{sev}] {issue.get('description', '')}",
                                }
                            )
                        else:
                            body_issues.append(issue)
                    review_body = (
                        f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    )
                    if body_issues:
                        issues_text = "\n".join(
                            f"- [{i.get('severity', '?').upper()}] {i.get('description', '')}"
                            for i in body_issues
                        )
                        review_body += f"\n\n**Issues:**\n{issues_text}"
                    result = await submit_pr_review(
                        gh_token,
                        owner,
                        repo,
                        pr_number,
                        event,
                        review_body,
                        inline_comments,
                    )
                    if result is None:
                        raise ValueError("PR not found")
                except Exception as e:
                    logger.warning(
                        f"[REVIEW PIPELINE] submit_pr_review failed, falling back: {e}"
                    )
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, review_comment
                        )
                    except Exception as e2:
                        logger.warning(
                            f"[REVIEW PIPELINE] Fallback comment also failed: {e2}",
                            exc_info=True,
                        )

                # Update Check Run with verdict conclusion (best-effort — errors are swallowed)
                check_run_id = (task.context or {}).get("check_run_id")
                if check_run_id:
                    try:
                        (
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _severity_map = {
                            "critical": "failure",
                            "major": "warning",
                            "minor": "notice",
                        }
                        _conclusion = (
                            "success" if verdict == "APPROVE" else "action_required"
                        )
                        _annotations = []
                        for _issue in issues:
                            if _issue.get("file") and _issue.get("line"):
                                _lvl = _severity_map.get(
                                    _issue.get("severity", "minor"), "notice"
                                )
                                _ln = int(_issue["line"])
                                _annotations.append(
                                    {
                                        "path": _issue["file"],
                                        "start_line": _ln,
                                        "end_line": _ln,
                                        "annotation_level": _lvl,
                                        "message": _issue.get("description", ""),
                                    }
                                )
                        _issue_counts = {}
                        for _issue in issues:
                            _sev = _issue.get("severity", "minor")
                            _issue_counts[_sev] = _issue_counts.get(_sev, 0) + 1
                        _counts_line = (
                            ", ".join(
                                f"{v} {k}" for k, v in sorted(_issue_counts.items())
                            )
                            if _issue_counts
                            else "no issues"
                        )
                        _cr_summary = (
                            f"**Blueprint Code Review**: {verdict}\n\n{feedback[:3000]}"
                        )
                        if _issue_counts:
                            _cr_summary += f"\n\n**Issue summary**: {_counts_line}"
                        await update_check_run(
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                            check_run_id,
                            conclusion=_conclusion,
                            title=f"Blueprint Code Review: {verdict}",
                            summary=_cr_summary,
                            annotations=_annotations or None,
                        )
                        logger.info(
                            f"[CHECK RUN] Updated check run {check_run_id} → {_conclusion} for task {task.id}"
                        )
                    except Exception as _cr_exc:
                        logger.warning(
                            f"[CHECK RUN] Failed to update check run {check_run_id} for task {task.id}: {_cr_exc}"
                        )

                # Store Blueprint's verdict on the review task context so the
                # pull_request_review webhook handler can correlate quality signals.
                task.context = {
                    **(task.context or {}),
                    "blueprint_review_verdict": verdict,
                    "review_was_parallelized": review_was_parallelized,
                }
                session.add(task)

                action_taken = False
                head_sha = ""
                if verdict == "APPROVE":
                    # Rollup gate handoff: when the original task is parking in CI wait
                    # (rollup-to-main / large rollup path in rollup_management), the parent
                    # owns the merge gate. The review task must NOT merge directly — it
                    # writes review_approved=True on the parent and lets the CI gate fire
                    # the merge once both signals are in. Without this branch, the review
                    # races the parent's CI poll and whichever finishes first wins, which
                    # is exactly the "merged before review" bug.
                    if original_task_id:
                        try:
                            from app.ci_completion import (
                                _attempt_merge_with_retry_and_escalation,
                            )
                            from app.routers.agent import get_github_token_for_task

                            # ── Site 2 lock-during-IO fix ──
                            # Previously we acquired FOR UPDATE on `_orig`
                            # unconditionally, then only the
                            # `if _orig and _orig_loop.get("ci_waiting"):` branch
                            # consumed the lock. The fall-through (review
                            # task's own merge cluster: get_pull_request,
                            # create_pr_comment, get_check_runs, get_pr_files,
                            # merge_pull_request, delete_branch — 60-120s) ran
                            # with `_orig`'s row lock still held by this
                            # session, blocking concurrent CI/heartbeat writes
                            # on the rollup parent. Fix: do a non-locking read
                            # to peek at ci_waiting first, and only acquire
                            # FOR UPDATE when we're committing to the rollup-
                            # handoff branch.
                            _orig_peek = (
                                (
                                    await session.execute(
                                        select(Task.context).where(
                                            Task.id == uuid.UUID(original_task_id)
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _orig_peek_loop = (
                                (_orig_peek or {}).get("review_loop", {})
                                if _orig_peek
                                else {}
                            )
                            if _orig_peek_loop.get("ci_waiting"):
                                # Phase A1: skip_locked + bounded retry. If the parent
                                # task row is held by a peer, defer the handoff —
                                # the parent's own poll will pick up review_approved
                                # on the next iteration once the lock clears.
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_rollup_handoff",
                                )
                                # Re-read the loop from the locked row — peer may
                                # have flipped ci_waiting in the gap between the
                                # peek and the lock acquisition.
                                _orig_loop = (
                                    (_orig.context or {}).get("review_loop", {})
                                    if _orig
                                    else {}
                                )
                                if _orig and _orig_loop.get("ci_waiting"):
                                    # Snapshot identity + status while _orig is
                                    # freshly locked. The merge gate below
                                    # (_attempt_merge_with_retry_and_escalation) can
                                    # roll back internally, which expires _orig even
                                    # with expire_on_commit=False; re-reading
                                    # _orig.project_id / .id / .status at broadcast
                                    # time would then sync lazy-load in async context
                                    # → MissingGreenlet / connection-acquire failure
                                    # (prod 2026-06-03 task_lifecycle:1838). The
                                    # parent stays EVALUATING on this CI-gate handoff,
                                    # so the snapshotted status is correct.
                                    _orig_pid_str = str(_orig.project_id)
                                    _orig_id_str = str(_orig.id)
                                    _orig_status_snap = _orig.status
                                    _orig_loop["review_approved"] = True
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                    }
                                    flag_modified(_orig, "context")
                                    session.add(_orig)
                                    task.status = TaskStatus.COMPLETED
                                    task.latest_status_message = f"Review approved for rollup PR #{pr_number} — handed off to CI gate"
                                    session.add(task)

                                    if _orig_loop.get("ci_passed"):
                                        try:
                                            (
                                                _gh,
                                                _own,
                                                _rep,
                                            ) = await get_github_token_for_task(
                                                _orig.id, session
                                            )
                                            await _attempt_merge_with_retry_and_escalation(
                                                _orig,
                                                _gh,
                                                _own,
                                                _rep,
                                                session,
                                                source="review_approval",
                                            )
                                        except Exception as _gate_e:
                                            logger.error(
                                                f"[REVIEW PIPELINE] Gate merge failed for rollup PR #{pr_number}: {_gate_e}",
                                                exc_info=True,
                                            )

                                    await session.commit()
                                    await broadcast_to_project(
                                        _orig_pid_str,
                                        "task_updated",
                                        {
                                            "task_id": _orig_id_str,
                                            "status": _orig_status_snap,
                                        },
                                    )
                                    logger.info(
                                        f"[REVIEW PIPELINE] Rollup PR #{pr_number}: review approved, handed to CI gate (parent {_orig.id})"
                                    )
                                    return {"status": "accepted"}
                        except Exception as _rollup_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Rollup gate handoff failed for PR #{pr_number}: {_rollup_e}",
                                exc_info=True,
                            )

                    head_sha = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            logger.info(
                                f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.",
                            )
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging the integration trunk.
                        #
                        # Pre-Phase-1 the integration branch IS "main", so this
                        # was historically written as `base_branch == "main"`.
                        # After the automain/LTS Phase 1 cutover rollup PRs
                        # target `automain` (the resolved integration branch),
                        # and a hard-coded "main" check would skip the pre-merge
                        # CI gate for every rollup — the orchestrator would merge
                        # rollups without waiting for CI.
                        #
                        # Gate on the resolved integration branch so the CI
                        # check runs for whichever branch the pipeline currently
                        # integrates onto. "main" is ALSO kept gated regardless:
                        # post-cutover it is the promotion / hotfix-rollup
                        # target, and a merge to main must never bypass CI.
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        project = (
                            await session.get(Project, task.project_id)
                            if task.project_id
                            else None
                        )
                        _integration_branch = (
                            _shared_state.get_project_integration_branch(project)
                        )
                        _promotion_target = _shared_state.get_project_promotion_target(
                            project
                        )
                        is_ci_gated_merge = base_branch in (
                            _integration_branch,
                            _promotion_target,
                        )
                        ci_status = (
                            await get_check_runs_cached(
                                gh_token,
                                owner,
                                repo,
                                head_sha,
                                project_id=str(task.project_id),
                            )
                            if (head_sha and is_ci_gated_merge)
                            else None
                        )

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            _approved_history = _append_approve_to_review_history(
                                review_loop, _approve_files
                            )
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = (
                                f"Review approved, waiting for CI on PR #{pr_number}..."
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    **review_loop,
                                    "ci_waiting": True,
                                    "review_approved": True,
                                    "review_history": _approved_history,
                                    "head_sha": head_sha,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": original_task_id,
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[CI] PR #{pr_number} approved, CI pending — waiting"
                            )
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = (
                                ci_status["failed_logs"]
                                or "CI checks failed (no details available)"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```",
                            )
                            # Conflict-aware canonical prompt (review_pipeline):
                            # the old prompt here only mentioned "build/lint/test
                            # errors" and said "don't change logic", so a PR that
                            # was ALSO conflicting never got rebased — the fix
                            # chased CI while the conflict outlived the global
                            # iteration cap (PR #5587, 2026-05-23).
                            ci_fix_prompt = build_ci_failure_fix_prompt(
                                pr_number=pr_number,
                                base_branch=base_branch,
                                ci_errors=ci_errors,
                                owner=owner,
                                repo=repo,
                            )
                            # Phase 2d: APPROVE branch must NOT propagate warm-VM
                            # continuity onto downstream spawns. The reviewer's VM
                            # is releasing on this APPROVE, so the new fix must
                            # cold-provision rather than wait for a dying poll.
                            fix_task, _ = await _spawn_pipeline_task(
                                task,
                                "fix",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id,
                                fix_reason="ci_failure",
                                ci_failure_logs=ci_errors,
                                suppress_warm_handoff=True,
                            )
                            if fix_task:
                                logger.error(
                                    f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}"
                                )
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            logger.info(f"[CI] PR #{pr_number} CI passed")

                        # Critical files gate: block merge if security-critical files were touched
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                critical_touched = (
                                    (orig_task.context or {}).get(
                                        "touched_critical", []
                                    )
                                    if orig_task
                                    else []
                                )
                                if critical_touched:
                                    orig_task.requires_approval = True
                                    orig_task.status = TaskStatus.EVALUATING
                                    orig_task.latest_status_message = f"Review approved — awaiting human approval to merge (critical files: {', '.join(critical_touched)})"
                                    session.add(orig_task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(orig_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(orig_task.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but PR touches security-critical files ({', '.join(critical_touched)}). Merge requires human approval.",
                                    )
                                    logger.error(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — critical files: {critical_touched}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking critical files for merge gate: {e}",
                                    exc_info=True,
                                )

                        # Security / approval gate: if the original create task was
                        # flagged (security scan blocking, scan failure, etc.), park
                        # for manual merge. The review still ran and posted feedback,
                        # but auto-merge is not safe without human sign-off.
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                _approval_orig = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if _approval_orig and _approval_orig.requires_approval:
                                    _approval_orig.status = TaskStatus.EVALUATING
                                    reason = (
                                        _approval_orig.latest_status_message
                                        or "requires manual approval"
                                    )
                                    _approval_orig.latest_status_message = f"Review approved — awaiting human approval to merge ({reason})"
                                    session.add(_approval_orig)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(_approval_orig.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(_approval_orig.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await _comment_with_retry(
                                        create_pr_comment,
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but merge requires human approval — {reason}",
                                    )
                                    logger.info(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — requires_approval set on {original_task_id[:8]}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking requires_approval for merge gate: {e}",
                                    exc_info=True,
                                )

                        merge_result = await _merge_with_retry(
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                            merge_method=_pick_merge_method(task),
                        )

                        if merge_result.get("merged"):
                            await _comment_with_retry(
                                create_pr_comment,
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.",
                            )
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    # C4: the orig (rollup parent) was previously
                                    # fetched via an UNLOCKED session.get, then
                                    # transitioned EVALUATING→COMPLETED and
                                    # committed on this shared session — racing
                                    # ci_completion, which mutates the SAME parent
                                    # guarded by status==EVALUATING. Two writers,
                                    # no row lock → lost-update / false-success.
                                    #
                                    # The merge has ALREADY succeeded on GitHub, so
                                    # the orig bookkeeping below carries ONE-SHOT
                                    # side effects with NO retry path: hotfix
                                    # back-merge (a missed back-merge breaks the
                                    # next promotion), security-fix rescan spawn,
                                    # remediation-outcome record + result message +
                                    # webhook, review-playbook extraction,
                                    # regression-case advancement, post-merge
                                    # smoketest. A skip_locked DEFER here would skip
                                    # the entire `if orig:` block and drop all of
                                    # them permanently. So we BLOCK on FOR UPDATE
                                    # (skip_locked=False) — bounded by a SET LOCAL
                                    # lock_timeout so we never hang — and the
                                    # peer's brief EVALUATING→terminal hold clears,
                                    # after which the one-shot effects run. On a
                                    # genuine (rare) timeout we log loudly + leave a
                                    # recoverable trail (below), never silently drop.
                                    orig = await _lock_original_task(
                                        session,
                                        original_task_id,
                                        source="review_merge_success",
                                        skip_locked=False,
                                    )
                                    if orig:
                                        _orig_project = await session.get(
                                            Project, orig.project_id
                                        )
                                        _orig_promotion_target = (
                                            _shared_state.get_project_promotion_target(
                                                _orig_project
                                            )
                                        )
                                        _orig_is_custom_staged_rollup = (
                                            is_custom_staged_rollup_root(
                                                orig, _orig_project, base_branch
                                            )
                                        )
                                        _terminal_rollup_merge = True
                                        if (
                                            _orig_is_custom_staged_rollup
                                            and _orig_project is not None
                                        ):
                                            _orig_integration_branch = (
                                                _shared_state.get_project_integration_branch(
                                                    _orig_project
                                                )
                                            )
                                            _landed_on_integration = (
                                                base_branch
                                                == _orig_integration_branch
                                            )
                                            _landed_on_promotion = (
                                                base_branch
                                                == _orig_promotion_target
                                            )
                                            if (
                                                not _landed_on_integration
                                                and _landed_on_promotion
                                            ):
                                                await merged_pr_counts_as_integration_landing(
                                                    orig,
                                                    _orig_project,
                                                    session,
                                                    pr_number=pr_number,
                                                    merged_base_branch=base_branch,
                                                    source="task_lifecycle.review_merge_success",
                                                )
                                            _record_pr_merged = (
                                                _landed_on_integration
                                            )
                                            _terminal_rollup_merge = (
                                                _landed_on_integration
                                                or _landed_on_promotion
                                            )
                                        else:
                                            _record_pr_merged = await (
                                                merged_pr_counts_as_integration_landing(
                                                    orig,
                                                    _orig_project,
                                                    session,
                                                    pr_number=pr_number,
                                                    merged_base_branch=base_branch,
                                                    source="task_lifecycle.review_merge_success",
                                                )
                                            )
                                        if _record_pr_merged:
                                            apply_merged_landed_signal(
                                                orig,
                                                _orig_project,
                                                base_branch=base_branch,
                                                hotfix_lane=bool(
                                                    (orig.first_payload or {}).get(
                                                        "hotfix"
                                                    )
                                                    or (orig.context or {}).get(
                                                        "hotfix"
                                                    )
                                                ),
                                                non_merge_reason=(
                                                    "merged_pr_non_integration_branch"
                                                ),
                                            )
                                        # If this was a rollup PR review, mark the
                                        # parent COMPLETED — but only if it is STILL
                                        # EVALUATING after the relock. A peer (e.g.
                                        # ci_completion / reaper) may have already
                                        # terminalized it during the merge window;
                                        # COMPLETED is irreversible, so we must not
                                        # overwrite a peer's terminal transition.
                                        # "Was this a rollup merge at all?" — true
                                        # whenever the original task was parked
                                        # EVALUATING awaiting its rollup PR (the
                                        # pre-mutation state captured here). This
                                        # drives the downstream smoketest and hotfix
                                        # back-merge gates, which must fire for ANY
                                        # rollup landing on the promotion target —
                                        # NOT only custom-staged (release/) ones.
                                        # The custom-staged terminal-vs-deferred
                                        # decision below uses _orig_is_custom_staged_rollup
                                        # directly; conflating the two (PR #7064 vs
                                        # automain merge) silently skipped the hotfix
                                        # back-merge for a plain hotfix->main landing.
                                        is_rollup_merge = (
                                            orig.status == TaskStatus.EVALUATING
                                        )
                                        if orig.status == TaskStatus.EVALUATING:
                                            if (
                                                not _orig_is_custom_staged_rollup
                                                or _terminal_rollup_merge
                                            ):
                                                orig.status = TaskStatus.COMPLETED
                                                orig.latest_status_message = (
                                                    "Rollup PR merged"
                                                )
                                            else:
                                                orig.latest_status_message = (
                                                    f"Rollup PR merged into "
                                                    f"{base_branch}; awaiting "
                                                    "downstream landing"
                                                )
                                        session.add(orig)
                                        _o_ctx = orig.context or {}
                                        # Audit W1: auto_remediation is now a column.
                                        if _o_ctx.get("diagnosis_task_id") or (
                                            orig.auto_remediation
                                            and _o_ctx.get("direction_id")
                                        ):
                                            try:
                                                from app.task_completion import (
                                                    _record_remediation_outcome,
                                                )

                                                await _record_remediation_outcome(
                                                    orig, merged=True, session=session
                                                )
                                            except Exception as _rr_exc:
                                                logger.warning(
                                                    f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {orig.id}: {_rr_exc!r}"
                                                )
                                        # Post result message for auto-remediation tasks
                                        # (routes between CI auto-fix and residual lifecycles).
                                        if orig.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    orig,
                                                    result="merged",
                                                    pr_number=pr_number,
                                                )
                                                try:
                                                    from app.notification_dispatch import (
                                                        dispatch_external_notification,
                                                    )

                                                    if is_residual_remediation_task(
                                                        orig
                                                    ):
                                                        _wh_event = (
                                                            "RESIDUAL_FIX_MERGED"
                                                        )
                                                        _wh_payload = {
                                                            "fingerprint": (
                                                                orig.context or {}
                                                            ).get(
                                                                "error_fingerprint", ""
                                                            ),
                                                            "pr_number": pr_number,
                                                        }
                                                    else:
                                                        _ar_branch = (
                                                            orig.context or {}
                                                        ).get(
                                                            "ci_failure_branch",
                                                            base_branch,
                                                        )
                                                        _wh_event = "AUTO_FIX_MERGED"
                                                        _wh_payload = {
                                                            "branch": _ar_branch,
                                                            "pr_number": pr_number,
                                                        }
                                                    asyncio.ensure_future(  # noqa: RUF006
                                                        dispatch_external_notification(  # noqa: RUF006, RUF100
                                                            orig.project_id,
                                                            _wh_event,
                                                            _wh_payload,
                                                        )
                                                    )
                                                except Exception as _wh_exc:
                                                    logger.warning(
                                                        "[AUTO-REMEDIATION] Failed to dispatch merged webhook: %s",
                                                        _wh_exc,
                                                    )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post result message: %s",
                                                    _ar_exc,
                                                )
                                        # Extract review feedback as persistent playbook entries
                                        # on every merge (captures first-attempt data at any iteration)
                                        try:
                                            from app.review_learning import (
                                                extract_review_feedback_to_playbook,
                                            )

                                            _orig_task_type = orig.first_payload.get(
                                                "type", "create"
                                            )
                                            await extract_review_feedback_to_playbook(
                                                task=task,
                                                review_loop=review_loop,
                                                pr_number=pr_number,
                                                original_task_type=_orig_task_type,
                                                session=session,
                                            )
                                        except Exception as _rp_exc:
                                            logger.warning(
                                                "[REVIEW_PLAYBOOK] Failed to extract review feedback: %s",
                                                _rp_exc,
                                            )
                                        await session.commit()
                                        # Heal the direction's research-history
                                        # merge counts for ORIG's cycle. ORIG (not
                                        # this review ``task``) is the merged child,
                                        # so the terminal-status recompute below —
                                        # which keys off ``task.context`` — never
                                        # covers ORIG's direction. Recompute now
                                        # that ORIG's landed signal is committed so
                                        # prs_merged doesn't stay stale-low and trip
                                        # the churn / lifetime-burn guardrails.
                                        if _record_pr_merged:
                                            from app.direction_outcomes import (
                                                refresh_direction_outcomes_after_merge,
                                            )

                                            await refresh_direction_outcomes_after_merge(
                                                orig, session
                                            )
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Completion-side warm-reuse trigger: the
                                        # original task's PR has landed on
                                        # integration and the lock is released —
                                        # mark its just-unblocked dependents
                                        # warm-claimable (see cluster 1 parity).
                                        if _record_pr_merged:
                                            await _mark_dependents_warm_claimable(orig)
                                        # Spawn a smoketest after rollup merges to the
                                        # promotion target. After Phase 1 (automain/LTS),
                                        # normal rollups target `automain`; only promotion
                                        # PRs (automain → promotion_target) still merge to
                                        # the promotion branch. So base_branch ==
                                        # promotion_target here means a PROMOTION just
                                        # landed — this smoketest acts as a post-promotion
                                        # production health-check, not a routine check.
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if (
                                            is_rollup_merge
                                            and base_branch == _orig_promotion_target
                                            and _runtime_config.get("smoketest_enabled")
                                        ):
                                            try:
                                                type_model = (
                                                    _runtime_config.get(
                                                        "task_type_models", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                type_effort = (
                                                    _runtime_config.get(
                                                        "task_type_efforts", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                health_check_urls = (
                                                    _runtime_config.get(
                                                        "health_check_urls"
                                                    )
                                                    or []
                                                )
                                                _smoketest_payload = validate_internal_payload(
                                                    {
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **(
                                                            {"model": type_model}
                                                            if type_model
                                                            else {}
                                                        ),
                                                        **(
                                                            {"effort": type_effort}
                                                            if type_effort
                                                            else {}
                                                        ),
                                                        **(
                                                            {
                                                                "health_check_urls": health_check_urls
                                                            }
                                                            if health_check_urls
                                                            else {}
                                                        ),
                                                    },
                                                    "task_lifecycle:smoketest",
                                                )
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[_smoketest_payload],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                    status=TaskStatus.QUEUED,
                                                    created_by="ORCHESTRATOR",
                                                )
                                                session.add(smoketest_task)
                                                await session.commit()
                                                await session.refresh(smoketest_task)
                                                history = TaskHistory(
                                                    task_id=smoketest_task.id,
                                                    action="SPAWNED",
                                                    details={
                                                        "spawned_by": "POST_ROLLUP_MERGE",
                                                        "base_branch": base_branch,
                                                    },
                                                )
                                                session.add(history)
                                                await session.commit()
                                                await broadcast_to_project(
                                                    str(task.project_id),
                                                    "task_updated",
                                                    {
                                                        "task_id": str(
                                                            smoketest_task.id
                                                        ),
                                                        "status": TaskStatus.QUEUED,
                                                    },
                                                )
                                                logger.info(
                                                    f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after rollup merge to {base_branch}"
                                                )
                                            except Exception as e:
                                                logger.warning(
                                                    f"[SMOKETEST] Failed to spawn smoketest after rollup merge: {e}",
                                                    exc_info=True,
                                                )
                                        # Back-merge: when a HOTFIX merges to the
                                        # promotion target, open a promotion_target ->
                                        # integration_branch PR so branches don't diverge.
                                        # A hotfix is a `plan` task with
                                        # base_branch=promotion_target and hotfix:true.
                                        # A missed back-merge makes the next promotion
                                        # conflict. (automain/LTS plan Phase 5)
                                        if (
                                            is_rollup_merge
                                            and base_branch == _orig_promotion_target
                                            and bool(orig.first_payload.get("hotfix"))
                                        ):
                                            try:
                                                from app import hotfix_backmerge

                                                _merged_pr_url = (
                                                    f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                                                    if owner and repo and pr_number
                                                    else ""
                                                )
                                                await (
                                                    hotfix_backmerge.open_backmerge_pr(
                                                        session,
                                                        task.project_id,
                                                        merged_pr_url=_merged_pr_url,
                                                        hotfix_task_id=orig.id,
                                                    )
                                                )
                                                # open_backmerge_pr deliberately
                                                # does NOT commit (its docstring:
                                                # "the caller commits the
                                                # session"). On a back-merge
                                                # conflict / 405 it session.add +
                                                # flush()es an operator-facing
                                                # ERROR Alert and a high-priority
                                                # conflict-resolution Task. After
                                                # flush() the session is
                                                # flushed-but-clean, so the
                                                # function's final
                                                # ``if session.dirty or
                                                # session.new`` commit is SKIPPED
                                                # and the ``async with`` exit
                                                # would ROLL BACK that Alert/Task
                                                # — leaving a back-merge conflict
                                                # completely unsignalled. Commit
                                                # here, mirroring the
                                                # smoketest-spawn block above.
                                                # The merged-PR work is already
                                                # committed (~:3470); this commit
                                                # only flushes the back-merge's
                                                # own Alert/Task — safe.
                                                await session.commit()
                                            except Exception as _bm_exc:
                                                logger.warning(
                                                    f"[HOTFIX_BACKMERGE] Back-merge trigger failed for hotfix task {orig.id}: {_bm_exc!r}",
                                                    exc_info=True,
                                                )
                                        # Spawn security rescan if merged task's parent was a security_audit
                                        if orig.parent_task_id:
                                            try:
                                                audit_parent = await session.get(
                                                    Task, orig.parent_task_id
                                                )
                                                if (
                                                    audit_parent
                                                    and audit_parent.first_payload.get(
                                                        "type"
                                                    )
                                                    == "security_audit"
                                                    and audit_parent.status
                                                    == TaskStatus.COMPLETED
                                                ):
                                                    # Only rescan once per original audit
                                                    existing_rescan = (
                                                        (
                                                            await session.execute(
                                                                select(Task).where(
                                                                    Task.parent_task_id
                                                                    == audit_parent.id,
                                                                    Task.title.ilike(
                                                                        "%rescan%"
                                                                    ),
                                                                )
                                                            )
                                                        )
                                                        .scalars()
                                                        .first()
                                                    )
                                                    if not existing_rescan:
                                                        try:
                                                            pr_files = (
                                                                await get_pr_files(
                                                                    gh_token,
                                                                    owner,
                                                                    repo,
                                                                    pr_number,
                                                                )
                                                            )
                                                            changed_files = ", ".join(
                                                                f["filename"]
                                                                for f in pr_files
                                                            )
                                                        except Exception:
                                                            changed_files = "(could not fetch file list)"
                                                        audit_title = audit_parent.title
                                                        type_model = (
                                                            _runtime_config.get(
                                                                "task_type_models", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        type_effort = (
                                                            _runtime_config.get(
                                                                "task_type_efforts", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        _rescan_payload = validate_internal_payload(
                                                            {
                                                                "type": "security_audit",
                                                                "prompt": (
                                                                    f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                    f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                    f"Output only PASS or remaining FAIL findings."
                                                                ),
                                                                **(
                                                                    {
                                                                        "model": type_model
                                                                    }
                                                                    if type_model
                                                                    else {}
                                                                ),
                                                                **(
                                                                    {
                                                                        "effort": type_effort
                                                                    }
                                                                    if type_effort
                                                                    else {}
                                                                ),
                                                            },
                                                            "task_lifecycle:security_audit",
                                                        )
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[_rescan_payload],
                                                            parent_task_id=audit_parent.id,
                                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                            status=TaskStatus.QUEUED,
                                                            requires_approval=False,
                                                            created_by="ORCHESTRATOR",
                                                        )
                                                        session.add(rescan_task)
                                                        await session.commit()
                                                        await session.refresh(
                                                            rescan_task
                                                        )
                                                        history = TaskHistory(
                                                            task_id=rescan_task.id,
                                                            action="SPAWNED",
                                                            details={
                                                                "spawned_by": "POST_SECURITY_FIX_MERGE",
                                                                "pr_number": pr_number,
                                                                "audit_parent_id": str(
                                                                    audit_parent.id
                                                                ),
                                                            },
                                                        )
                                                        session.add(history)
                                                        await session.commit()
                                                        await broadcast_to_project(
                                                            str(orig.project_id),
                                                            "task_updated",
                                                            {
                                                                "task_id": str(
                                                                    rescan_task.id
                                                                ),
                                                                "status": TaskStatus.QUEUED,
                                                            },
                                                        )
                                                        logger.info(
                                                            f"[SECURITY RESCAN] Spawned rescan task {rescan_task.id} after security fix merge PR #{pr_number}"
                                                        )
                                            except Exception as e:
                                                logger.info(
                                                    f"[SECURITY RESCAN] Failed to spawn security rescan: {e}"
                                                )
                                except _OriginalTaskLockTimeout:
                                    # C4 fix B: the BLOCKING FOR UPDATE on the
                                    # rollup parent hit its bounded lock_timeout —
                                    # a peer held the row past the wait. The merge
                                    # ALREADY landed on GitHub, so the one-shot
                                    # post-merge effects above could not run. Do NOT
                                    # silently drop them: leave a durable, visible,
                                    # recoverable trail. The shared session is in a
                                    # rolled-back state after the timeout, so use a
                                    # FRESH independent session.
                                    logger.error(  # noqa: TRY400 — lock_timeout is not a code fault; the traceback adds noise, the message + recovery marker are the signal
                                        "[REVIEW PIPELINE] C4: lock_timeout acquiring "
                                        "rollup parent %s after merging PR #%s — "
                                        "post-merge one-shots (back-merge / rescan / "
                                        "remediation outcome / playbook / smoketest) "
                                        "DEFERRED to recovery, not dropped. Marking "
                                        "parent with _post_merge_effects_deferred + "
                                        "operator alert.",
                                        original_task_id,
                                        pr_number,
                                    )
                                    try:
                                        from app.alert_dispatch import (
                                            _maybe_create_failure_alert,
                                        )

                                        async with AsyncSessionLocal() as _rec:
                                            _orig_rec = await _rec.get(
                                                Task, uuid.UUID(original_task_id)
                                            )
                                            if _orig_rec is not None:
                                                _rec_ctx = dict(
                                                    _orig_rec.context or {}
                                                )
                                                _rec_ctx[
                                                    "_post_merge_effects_deferred"
                                                ] = {
                                                    "pr_number": pr_number,
                                                    # task_id is the function's UUID
                                                    # param — use it, not task.id: the
                                                    # outer session is poisoned by the
                                                    # lock_timeout abort, so reading
                                                    # task.id would lazy-load on a dead
                                                    # session (prod 2026-06-03
                                                    # task_lifecycle:2726).
                                                    "review_task_id": str(task_id),
                                                    "deferred_at": _utcnow().isoformat(),
                                                    "reason": "lock_timeout acquiring "
                                                    "parent FOR UPDATE during "
                                                    "merge-success bookkeeping",
                                                }
                                                _orig_rec.context = _rec_ctx
                                                from sqlalchemy.orm.attributes import (
                                                    flag_modified as _fm,
                                                )

                                                _fm(_orig_rec, "context")
                                                _rec.add(_orig_rec)
                                                await _maybe_create_failure_alert(
                                                    _orig_rec,
                                                    _rec,
                                                    f"PR #{pr_number} merged but the "
                                                    f"rollup parent row was locked past "
                                                    f"the bounded wait — post-merge "
                                                    f"effects (hotfix back-merge / "
                                                    f"security rescan / remediation "
                                                    f"outcome) are deferred and need a "
                                                    f"re-drive or manual check.",
                                                )
                                                await _rec.commit()
                                    except Exception as _rec_exc:
                                        logger.error(
                                            "[REVIEW PIPELINE] C4: failed to persist "
                                            "deferred-effects trail for parent %s "
                                            "(PR #%s merged): %r",
                                            original_task_id,
                                            pr_number,
                                            _rec_exc,
                                            exc_info=True,
                                        )
                                    # The bounded blocking lock rolled the SHARED
                                    # session back (an aborted FOR UPDATE leaves the
                                    # tx in PendingRollback), which EXPIRED the
                                    # `task` ORM instance. Downstream post-handler
                                    # sections (rollup check, direction outcomes,
                                    # terminal records, final commit) read
                                    # ``task.<attr>`` on this same session — an
                                    # expired attribute access would sync lazy-load
                                    # → MissingGreenlet. Re-fetch by stable id so the
                                    # rest of _process_post_commit runs clean.
                                    with contextlib.suppress(Exception):
                                        _re_task = (
                                            await session.execute(
                                                select(Task).where(Task.id == task_id)
                                            )
                                        ).scalars().first()
                                        if _re_task is not None:
                                            task = _re_task
                                except Exception as e:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}",
                                        exc_info=True,
                                    )
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} approved and merged"
                            )
                            action_taken = True
                        else:
                            # Merge blocked — park in EVALUATING with ci_waiting so the
                            # ci_completion retry-with-backoff loop handles it instead of
                            # spawning a fix task immediately.  A first-attempt merge
                            # failure is frequently a stale GitHub mergeability flag
                            # (GitHub recomputes asynchronously after pushes); ci_completion
                            # absorbs that window via its internal 5/15/45s backoff inside
                            # a single merge attempt.  If the merge still fails after that
                            # quiet window, it spawns a rebase fix for the moving-parent
                            # case (sibling rollups advancing the base branch between
                            # fetches).  Fully autonomous; no requires_approval.
                            merge_msg = merge_result.get("message", "unknown")
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Retrying with backoff.",
                            )
                            from app import pause_clock

                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            merge_wait_loop = dict(review_loop)
                            merge_wait_loop["review_history"] = (
                                _append_approve_to_review_history(
                                    review_loop, _approve_files
                                )
                            )
                            merge_wait_loop["ci_waiting"] = True
                            merge_wait_loop["review_approved"] = True
                            merge_wait_loop["ci_passed"] = (
                                True  # CI was already verified green above
                            )
                            merge_wait_loop["ci_wait_started"] = _utcnow().isoformat()
                            merge_wait_loop[
                                "ci_wait_pause_baseline"
                            ] = await pause_clock.current_baseline(session)
                            merge_wait_loop["ci_poll_interval"] = 30
                            merge_wait_loop["pr_number"] = pr_number
                            merge_wait_loop["branch"] = branch
                            merge_wait_loop["base_branch"] = base_branch
                            merge_wait_loop["original_task_id"] = original_task_id
                            merge_wait_loop["head_sha"] = head_sha
                            merge_wait_loop["merge_blocked_msg"] = merge_msg[:300]
                            task.status = TaskStatus.EVALUATING
                            task.context = {
                                **(task.context or {}),
                                "review_loop": merge_wait_loop,
                            }
                            task.latest_status_message = f"Review approved, merge blocked on PR #{pr_number} ({merge_msg[:120]}) — retrying with backoff"
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} merge blocked, parked for ci_completion retry: {merge_msg[:200]}"
                            )
                            action_taken = True
                    except Exception as e:
                        # Merge/CI-check failed — park in EVALUATING with ci_waiting so
                        # the orchestrator's CI monitoring loop retries the merge instead
                        # of silently orphaning the PR.
                        logger.error(
                            f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}, parking in CI-wait: {e}",
                            exc_info=True,
                        )
                        # Best-effort: gh_token may not be in scope if the failure
                        # happened before the auth call.  An empty file list here
                        # still records that the iteration was approved.
                        try:
                            _approve_files = [
                                f.get("filename", "")
                                for f in (
                                    await get_pr_files(gh_token, owner, repo, pr_number)
                                )
                                if isinstance(f, dict) and f.get("filename")
                            ]
                        except Exception:
                            _approve_files = []
                        _approved_history = _append_approve_to_review_history(
                            review_loop, _approve_files
                        )
                        task.status = TaskStatus.EVALUATING
                        task.latest_status_message = f"Review approved, merge attempt failed ({type(e).__name__}) — CI monitor will retry"
                        task.context = {
                            **(task.context or {}),
                            "review_loop": {
                                **review_loop,
                                "ci_waiting": True,
                                "review_approved": True,
                                "review_history": _approved_history,
                                "head_sha": head_sha,
                                "pr_number": pr_number,
                                "branch": branch,
                                "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": _utcnow().isoformat(),
                            },
                        }
                        session.add(task)
                        await session.commit()
                        action_taken = True
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait).
                    # After verdict normalization above this is the only non-APPROVE branch;
                    # any non-standard verdict (e.g., legacy COMMENT) has already been
                    # rewritten to REQUEST_CHANGES and lands here.

                    # Unparseable verdict short-circuit: the fix agent would receive no
                    # structured issues and a placeholder feedback string, consuming a VM
                    # cycle without useful guidance. The original task is already flagged
                    # requires_approval above, so human intervention is expected.
                    if verdict_unparseable:
                        logger.warning(
                            f"[REVIEW PIPELINE] PR #{pr_number}: verdict unparseable — skipping fix spawn, original task flagged requires_approval"
                        )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review verdict unparseable for PR #{pr_number} — fix skipped, manual review required"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}

                    # Empty-PR short-circuit: if the PR has no file changes (because a
                    # prior fix cycle rebased the branch flat so all its commits are in
                    # the base), spawning another fix is futile — there's nothing to
                    # fix. Close the PR and classify the rollup parent's landing
                    # so intermediate staged targets keep waiting downstream.
                    try:
                        (
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _pr_files = await get_pr_files(
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                    except Exception as _empty_err:
                        logger.warning(
                            f"[REVIEW PIPELINE] Empty-PR check failed for #{pr_number}: {_empty_err} — proceeding with fix spawn"
                        )
                        _pr_files = None
                    if _pr_files == []:
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} has no file changes — closing as empty rollup"
                        )
                        try:
                            await close_pull_request(
                                _empty_gh,
                                _empty_owner,
                                _empty_repo,
                                pr_number,
                                project_id=str(task.project_id),
                            )
                        except Exception as _close_err:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to close empty PR #{pr_number}: {_close_err}"
                            )
                        if original_task_id:
                            _orig_broadcast_status = TaskStatus.EVALUATING
                            # Captured for the post-commit direction-outcome
                            # refresh: the empty-PR cleanup lands ORIG as a
                            # merge-equivalent integration landing, but ORIG (not
                            # this review ``task``) carries the direction lineage,
                            # so the terminal recompute below misses it.
                            _orig_to_refresh: Task | None = None
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, defer the empty-PR cleanup;
                                # the next poll cycle will retry. Better than
                                # 500'ing the review handler.
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_empty_pr_cleanup",
                                )
                                if _orig:
                                    _orig_loop = (_orig.context or {}).get(
                                        "review_loop", {}
                                    ) or {}
                                    _orig_loop.pop("ci_waiting", None)
                                    _orig_project = await session.get(
                                        Project, _orig.project_id
                                    )
                                    (
                                        _record_pr_merged,
                                        _terminal_rollup_merge,
                                        _landed_branch,
                                    ) = await classify_rollup_merge_outcome(
                                        _orig,
                                        _orig_project,
                                        session,
                                        pr_number=pr_number,
                                        merged_base_branch=base_branch,
                                        source="task_lifecycle.review_empty_pr_cleanup",
                                    )
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                        "pr_closed_empty": True,
                                    }
                                    if _record_pr_merged:
                                        apply_merged_landed_signal(
                                            _orig,
                                            _orig_project,
                                            base_branch=_landed_branch,
                                            hotfix_lane=bool(
                                                (_orig.first_payload or {}).get(
                                                    "hotfix"
                                                )
                                                or (_orig.context or {}).get(
                                                    "hotfix"
                                                )
                                            ),
                                            non_merge_reason=(
                                                "merged_pr_non_integration_branch"
                                            ),
                                        )
                                    flag_modified(_orig, "context")
                                    _orig.status = (
                                        TaskStatus.COMPLETED
                                        if _terminal_rollup_merge
                                        else TaskStatus.EVALUATING
                                    )
                                    if _terminal_rollup_merge:
                                        _orig.latest_status_message = (
                                            f"✓ Rollup PR #{pr_number} closed — "
                                            f"branch equivalent to {base_branch}, "
                                            "no new changes to merge"
                                        )
                                    else:
                                        _orig.latest_status_message = (
                                            f"✓ Rollup PR #{pr_number} closed — "
                                            f"branch equivalent to {_landed_branch}, "
                                            "no new changes to merge; awaiting "
                                            "downstream landing"
                                        )
                                    _orig_broadcast_status = _orig.status
                                    session.add(_orig)
                                    if _record_pr_merged:
                                        _orig_to_refresh = _orig
                                else:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Empty-PR cleanup deferred — parent {original_task_id[:8]} contended"
                                    )
                            except Exception as _orig_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to update rollup parent after empty-PR close: {_orig_err}"
                                )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review complete — PR #{pr_number} closed as empty (obsolete rollup)"
                        session.add(task)
                        await session.commit()
                        # Heal ORIG's direction merge counts now that its
                        # merge-equivalent landing is committed (mirrors the
                        # review_merge_success path). No-op unless ORIG carries a
                        # direction/research lineage; best-effort.
                        if original_task_id and _orig_to_refresh is not None:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            await refresh_direction_outcomes_after_merge(
                                _orig_to_refresh, session
                            )
                        if original_task_id:
                            try:  # noqa: SIM105
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": original_task_id,
                                        "status": _orig_broadcast_status,
                                    },
                                )
                            except Exception:  # noqa: S110
                                pass
                        return {"status": "accepted"}

                    ci_errors_text = ""
                    ci_check: dict | None = None
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        pr_data = await get_pull_request(
                            gh_token, owner, repo, pr_number
                        )
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs_cached(
                                gh_token, owner, repo, head_sha
                            )
                            if ci_check.get("conclusion") == "failure" and ci_check.get(
                                "failed_logs"
                            ):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}",
                            exc_info=True,
                        )
                    # CI vs reviewer build-gate disagreement detector. When the
                    # reviewer flags build/lint complaints but CI shows the
                    # corresponding job GREEN, the issue is almost always a
                    # broken reviewer-VM env — pre-warn the fix agent so it
                    # disputes via /tmp/fix_responses rather than thrashing on
                    # source code that isn't broken (PR #3884 saga, 2026-05-06).
                    disagreement_hint = _detect_ci_review_disagreement(
                        issues,
                        feedback,
                        ci_check,
                        pr_number,
                    )
                    if issues:

                        def _fmt_issue(i):
                            desc = i.get("description", "")
                            file_ = i.get("file")
                            line_ = i.get("line")
                            if file_ and line_ is not None:
                                return f"- [{file_}:{line_}] {desc}"
                            if file_:
                                return f"- [{file_}] {desc}"
                            return f"- {desc}"

                        critical = [
                            i for i in issues if i.get("severity") == "critical"
                        ]
                        major = [i for i in issues if i.get("severity") == "major"]
                        minor = [i for i in issues if i.get("severity") == "minor"]
                        structured = ""
                        if critical:
                            structured += (
                                "\n\nCRITICAL issues (must fix):\n"
                                + "\n".join(_fmt_issue(i) for i in critical)
                            )
                        if major:
                            structured += "\n\nMajor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in major
                            )
                        if minor:
                            structured += "\n\nMinor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in minor
                            )
                        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Issues to address:{structured}{ci_errors_text}"
                    else:
                        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Reviewer feedback:\n\n{feedback}{ci_errors_text}"
                    # FIX-ALL-OCCURRENCES: when the flagged issues look like
                    # instances of ONE file-spanning class (shared description
                    # stem, or an explicit "all instances"/"other call sites"
                    # cue), prepend a directive to grep + fix EVERY occurrence
                    # in this pass. Prevents the whack-a-mole loop where the fix
                    # agent migrates only the cited line and the next review
                    # finds the next sibling site. The directive explicitly
                    # reconciles with the narrow-focus guardrails injected later
                    # in _spawn_pipeline_task: migrating sibling sites of the
                    # flagged class is completing the fix, not refactoring.
                    if detect_fix_all_issue_class(issues):
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number}: flagged issues "
                            f"form a file-spanning class — injecting "
                            f"fix-all-occurrences directive"
                        )
                        fix_prompt_text = (
                            f"{fix_all_occurrences_directive()}{fix_prompt_text}"
                        )
                    # First-pass minor-only verdict: when every blocking finding
                    # from the initial review is minor-severity, the spawned fix
                    # only addresses nits. Mark the fix so its completion handler
                    # skips the follow-up re-review (CI gates the merge instead),
                    # saving one review iteration + one review-phase LLM call.
                    review_iteration = review_loop.get("iteration", 1)
                    first_attempt_minor_only = (
                        review_iteration == 1
                        and bool(issues)
                        and all(
                            (i.get("severity", "minor") or "minor").lower() == "minor"
                            for i in issues
                            if i.get("description")
                        )
                    )
                    fix_task, _ = await _spawn_pipeline_task(
                        task,
                        "fix",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        fix_prompt=fix_prompt_text,
                        original_prompt=original_prompt,
                        original_task_id=original_task_id,
                        current_feedback=feedback,
                        current_issues=issues,
                        investigated_files=investigated_files,
                        skip_rereview_minor_only=first_attempt_minor_only,
                    )
                    if fix_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}"
                        )
                        # Reset the parking parent's CI-wait clock: each spawned fix
                        # is a fresh cycle of work, so the 30-min backstop in
                        # services.py should count from here instead of the initial
                        # rollup-creation timestamp.  Without this, a rollup that
                        # legitimately cycles through 3-4 fix iterations gets killed
                        # mid-pipeline.
                        if original_task_id:
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, skip the ci_wait reset —
                                # it's an optimization (resets the 30-min
                                # backstop clock), not correctness; the parent
                                # will still terminate, just on the original
                                # baseline if reset is missed.
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_ci_wait_reset",
                                )
                                if _orig:
                                    _orig_loop = dict(
                                        (_orig.context or {}).get("review_loop", {})
                                        or {}
                                    )
                                    if _orig_loop.get("ci_waiting"):
                                        from app import pause_clock

                                        _orig_loop["ci_wait_started"] = (
                                            _utcnow().isoformat()
                                        )
                                        _orig_loop[
                                            "ci_wait_pause_baseline"
                                        ] = await pause_clock.current_baseline(session)
                                        _orig.context = {
                                            **(_orig.context or {}),
                                            "review_loop": _orig_loop,
                                        }
                                        flag_modified(_orig, "context")
                                        session.add(_orig)
                                else:
                                    logger.info(
                                        f"[REVIEW PIPELINE] ci_wait reset skipped for parent {original_task_id[:8]} — row contended"
                                    )
                            except Exception as _reset_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to reset ci_wait_started on parent {original_task_id[:8]}: {_reset_err}"
                                )
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = (
                            f"PR #{pr_number} stalled — iteration cap reached"
                        )
                        existing_cap_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.severity == AlertSeverity.WARNING,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == cap_alert_title,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Review requested changes on PR #{pr_number} but the fix iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        else:
                            await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}"
                        )
                    # On every review rejection, store which issue categories caused it
                    # on the original task.  Iteration 1 → _first_attempt_rejection_categories
                    # (existing field); all iterations → _all_iterations_rejection_categories
                    # (accumulated superset used by all-iterations catch-rate metric).
                    if issues and original_task_id:
                        try:
                            from app.review_learning import (
                                cache_first_attempt_classification,
                                _keyword_from_flat,
                            )

                            rh_items = [
                                {
                                    "description": i.get("description", ""),
                                    "severity": i.get("severity", "minor"),
                                    "iteration": review_iteration,
                                }
                                for i in issues
                                if i.get("description")
                            ]
                            if rh_items:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if orig_task:
                                    task_ctx = dict(orig_task.context or {})
                                    if review_iteration == 1:
                                        buckets = (
                                            await cache_first_attempt_classification(
                                                task_ctx, rh_items
                                            )
                                        )
                                    else:
                                        buckets = _keyword_from_flat(rh_items)
                                    rejection_cats = {
                                        cat: len(items)
                                        for cat, items in buckets.items()
                                        if items
                                    }
                                    if rejection_cats:
                                        if review_iteration == 1:
                                            task_ctx[
                                                "_first_attempt_rejection_categories"
                                            ] = rejection_cats
                                            task_ctx["_first_attempt_minor_only"] = (
                                                first_attempt_minor_only
                                            )
                                        existing_all = dict(
                                            task_ctx.get(
                                                "_all_iterations_rejection_categories"
                                            )
                                            or {}
                                        )
                                        for cat, cnt in rejection_cats.items():
                                            existing_all[cat] = (
                                                existing_all.get(cat, 0) + cnt
                                            )
                                        task_ctx[
                                            "_all_iterations_rejection_categories"
                                        ] = existing_all
                                    orig_task.context = task_ctx
                                    flag_modified(orig_task, "context")
                                    session.add(orig_task)
                                    await session.commit()
                        except Exception as _cat_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to record rejection categories: {_cat_e}"
                            )
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(
                        f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})"
                    )

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                _fix_project = await session.get(Project, task.project_id)
                base_branch = (
                    review_loop.get("base_branch")
                    or task_payload.get("base_branch")
                    or _shared_state.get_project_integration_branch(_fix_project)
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")

                # Backend stamp: ensure fix_iteration is set on the self-review
                # outcome so the catch-rate dashboard and reviewer priming can
                # identify which fix iteration produced the SR. The agent stamps
                # it via code-change.ts readSelfReviewOutcome; this is the
                # fallback for agents that ran before that stamp was added.
                _fix_sro = (task.context or {}).get("_self_review_outcome")
                if isinstance(_fix_sro, dict) and "fix_iteration" not in _fix_sro:
                    _pipeline_iter = int(review_loop.get("iteration", 2))
                    # Pipeline sequence: review(1)→fix(2)→review(3)→fix(4)→...
                    # fix_iteration = pipeline_iter // 2
                    _fix_iter_n = max(1, _pipeline_iter // 2)
                    _stamped_ctx = dict(task.context)
                    _stamped_ctx["_self_review_outcome"] = {
                        **_fix_sro,
                        "fix_iteration": _fix_iter_n,
                    }
                    task.context = _stamped_ctx
                    flag_modified(task, "context")
                    session.add(task)

                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Fix pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip only when PR is genuinely merged/done.
                # A non-merged 'closed' state may be a transient self-close
                # from the orchestrator's mergeability-reset loop
                # (close+reopen to force GitHub to recompute mergeable=None).
                # Gating on merged==True only prevents silently dropping the
                # re-review handoff during that brief closed window.
                try:
                    (
                        gh_token_check,
                        owner_check,
                        repo_check,
                    ) = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(
                        gh_token_check, owner_check, repo_check, pr_number
                    )
                    if pr_data_check.get("merged"):
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} already merged — skipping fix re-review for task {task.id}"
                        )
                        task.latest_status_message = (
                            f"PR #{pr_number} already merged — re-review skipped"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                    if pr_data_check.get("state") == "closed":
                        # PR is closed but not merged — could be a transient
                        # mergeability-reset. Proceed with re-review so the
                        # handoff is preserved; the review/merge gate will
                        # handle a genuinely abandoned PR gracefully.
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} is closed (not merged) for task {task.id} — "
                            f"may be a transient mergeability-reset; proceeding with re-review"
                        )
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding"
                    )

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    scan_result = await scan_pr_for_injection(
                        gh_token, owner, repo, pr_number
                    )
                    logger.info(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, scan_result["report"]
                        )
                except Exception as e:
                    logger.error(
                        f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}",
                        exc_info=True,
                    )
                    task.requires_approval = True
                    task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[
                        :500
                    ]
                    session.add(task)
                    await session.commit()

                fix_reason = review_loop.get("fix_reason", "code_review")
                # First-pass minor-only fix: the initial review flagged only
                # minor nits, so this fix carries skip_rereview_minor_only (set
                # at spawn time in _spawn_pipeline_task). It skips the re-review
                # the same way ci_failure/merge_conflict fixes do.
                skip_rereview_minor_only = bool(
                    review_loop.get("skip_rereview_minor_only")
                )
                # Skip re-review for fixes whose origin doesn't add review-worthy
                # logic on top of an already-decided merge gate:
                #   - ci_failure  → original review already approved code; CI is
                #                   the only gate. Wait for CI then merge.
                #   - merge_conflict → cluster sub-PR rebase. The original task
                #                   was scope-gated to skip review (small/medium
                #                   intermediate); rebase output is mechanical;
                #                   re-reviewing would be inconsistent with the
                #                   no-conflict happy path. Wait for CI then merge.
                #   - minor-only first-pass verdict → the fix only addressed
                #                   minor nits; a re-review would near-certainly
                #                   approve, so spend CI as the only gate.
                skip_rereview = (
                    fix_reason in ("ci_failure", "merge_conflict")
                    or skip_rereview_minor_only
                )
                if not scan_blocked and skip_rereview:
                    from app import pause_clock

                    ci_loop = review_loop.copy()
                    ci_loop["ci_waiting"] = True
                    ci_loop["review_approved"] = True
                    ci_loop["ci_wait_started"] = _utcnow().isoformat()
                    ci_loop[
                        "ci_wait_pause_baseline"
                    ] = await pause_clock.current_baseline(session)
                    ci_loop["ci_poll_interval"] = 10
                    task.status = TaskStatus.EVALUATING
                    task.context = {**(task.context or {}), "review_loop": ci_loop}
                    if fix_reason == "ci_failure":
                        label = "CI-only fix"
                    elif fix_reason == "merge_conflict":
                        label = "merge-conflict fix"
                    else:
                        label = "minor-nit fix"
                    skip_detail = (
                        fix_reason
                        if fix_reason in ("ci_failure", "merge_conflict")
                        else "minor_only"
                    )
                    task.latest_status_message = f"{label} applied for PR #{pr_number} — waiting for CI (review skipped: {skip_detail})"
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[REVIEW PIPELINE] {label} for PR #{pr_number} (skip reason={skip_detail}) — skipping re-review, waiting for CI"
                    )
                elif not scan_blocked:
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_task_id=original_task_id,
                    )
                    if review_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}"
                        )
                    else:
                        task.latest_status_message = f"Fix applied but review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = (
                            f"PR #{pr_number} stalled — iteration cap reached"
                        )
                        existing_cap_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.severity == AlertSeverity.WARNING,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == cap_alert_title,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Fix was applied to PR #{pr_number} but the review iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        else:
                            await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn after fix returned None for PR #{pr_number}"
                        )

            # Completion handlers — wrapped in try/except to prevent VM leak.
            # If a handler throws after the early commit, BackgroundTasks (including
            # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
            # VM cleanup via asyncio.create_task as a fallback.
            try:
                if task_type == "plan":
                    plan_logs = payload.logs or task.latest_status_message or ""
                    await _handle_plan_completion(task, plan_logs, session)

                elif task_type == "design_doc_update":
                    doc_logs = payload.logs or task.latest_status_message or ""
                    # Round 3 C3 + C4: ``locked_project`` was acquired in
                    # canonical Project → Task order at the top of
                    # _process_post_commit via ``lock_project_then_task``.
                    # The handler skips its own re-acquisition when given
                    # a pre-locked Project. None propagates the legacy
                    # bounded-retry path inside the handler (defensive
                    # only — the canonical pair-lock should have produced
                    # a non-None project for design_doc_update tasks).
                    # Canonical order is Project before Task — see
                    # docs/concurrency.md.
                    await _handle_design_doc_completion(
                        task, doc_logs, session, project=locked_project
                    )

                elif task_type == "strategic_review":
                    review_logs = payload.logs or task.latest_status_message or ""
                    # Round 3 C3 + C4: see _handle_design_doc_completion above.
                    # ``locked_project`` was acquired in canonical
                    # Project → Task order at the top of _process_post_commit.
                    await _handle_strategic_completion(
                        task, review_logs, session, project=locked_project
                    )

                elif task_type == "research":
                    research_logs = payload.logs or task.latest_status_message or ""
                    await _handle_research_completion(task, research_logs, session)

                elif task_type == "security_audit":
                    audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_security_audit_completion(task, audit_logs, session)

                elif task_type == "perf":
                    perf_logs = payload.logs or task.latest_status_message or ""
                    await _handle_perf_completion(task, perf_logs, session)

                elif task_type == "self_audit":
                    self_audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_self_audit_completion(task, self_audit_logs, session)

                elif task_type == "memo":
                    memo_logs = payload.logs or task.latest_status_message or ""
                    await _handle_memo_completion(task, memo_logs, session)
            except Exception as handler_exc:
                logger.error(
                    f"[STATUS] Completion handler failed for {task_type} task {task.id}: {handler_exc!r}",
                    exc_info=True,
                )
                # A handler that raised on a DB error (asyncpg QueryCanceledError
                # from statement_timeout, LockNotAvailableError, a closed
                # connection, …) leaves the session in an aborted transaction.
                # Every recovery write below — and the downstream
                # rollup/aggregation/final-commit sections — then fail on the
                # poisoned session ("current transaction is aborted" / "raised as
                # a result of Query-invoked autoflush"), so the requeue/FAIL never
                # persists and a task the agent reported terminal is silently
                # stranded with its work undispatched (research findings, plan
                # children, …). Production 2026-05-29: a research task hit a 120s
                # statement_timeout in _handle_research_completion, the requeue
                # commit then died on autoflush, and the task stuck COMPLETED with
                # 0 children and its findings undispatched. Roll the poisoned
                # transaction back so recovery runs on a clean transaction.
                # rollback() expires every ORM instance, so re-fetch the task by
                # its (stable, argument-supplied) id before reading or mutating
                # it — attribute access on the now-expired instance would issue a
                # synchronous lazy-load and raise MissingGreenlet.
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[HANDLER] Rollback after handler failure failed for task %s",
                        task_id,
                    )
                _recovered_task = (
                    (await session.execute(select(Task).where(Task.id == task_id)))
                    .scalars()
                    .first()
                )
                if _recovered_task is None:
                    # Row vanished (archived/deleted) during post-commit — nothing
                    # left to recover.
                    logger.warning(
                        "[HANDLER] Task %s gone after handler failure; skipping recovery",
                        task_id,
                    )
                    return {"status": "accepted"}
                task = _recovered_task
                # Detect transient errors (GitHub API, DB, network) and requeue instead of FAILED
                err_str = str(handler_exc).lower()
                is_transient = any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "502",
                        "503",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        "connection refused",
                    ]
                )
                if is_transient and task.attempt_count < _runtime_config["max_retries"]:
                    task.status = TaskStatus.QUEUED
                    # The earlier COMPLETED/FAILED assignment fired the SQLAlchemy set
                    # event which added task.id to _terminated_task_ids, revoking the
                    # JWT. Since the completion handler hit a transient error and we're
                    # requeuing, drop the cache entry so the next VM can claim.
                    from app.routers.agent import _terminated_task_ids

                    _terminated_task_ids.pop(str(task.id), None)
                    task.attempt_count += 1
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    task.started_at = None
                    # Handler ran after a successful VM provision — reset provision budget
                    # so a transient handler failure doesn't consume retries meant for VM issues.
                    handler_ctx = dict(task.context or {})
                    handler_ctx.pop("provision_failures", None)
                    task.context = handler_ctx
                    task.latest_status_message = f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}"
                    logger.warning(
                        f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}"
                    )
                else:
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = f"Completed but handler failed: {type(handler_exc).__name__}: {str(handler_exc)[:200]}"
                    from app.failure_classification import persist_failure_category
                    from app.routers.agent_status import record_task_result

                    _fc_handler = classify_failure(
                        task, at_time=task.last_heartbeat or task.started_at
                    )
                    persist_failure_category(task, _fc_handler)
                    record_task_result(
                        success=False,
                        failure_class=_fc_handler,
                        task_id=str(task.id),
                        task_type=(task.first_payload or {}).get("type", ""),
                    )
                session.add(task)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ]:
            try:
                await _check_and_trigger_rollup(task, session)
            except Exception as e:
                logger.error(
                    f"[ROLLUP] Rollup check failed for task {task.id}: {e!r}",
                    exc_info=True,
                )
                # C1: _check_and_trigger_rollup does FOR UPDATE selects +
                # internal commits that can raise on a DB error, leaving this
                # post-commit ``session`` in PendingRollback. Without this
                # rollback every subsequent post-terminal helper (direction
                # outcomes, remediation/regression, strategic backstop) and the
                # final commit silently fail or 500 the agent with
                # PendingRollbackError. Roll back so the chain continues clean.
                with contextlib.suppress(Exception):
                    await session.rollback()

        # 6b. Update direction research history outcomes for child tasks of a research cycle
        _task_ctx = task.context or {}
        if (
            _task_ctx.get("direction_id")
            and _task_ctx.get("research_task_id")
            and payload.status
            in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ):
            try:
                from app.direction_outcomes import _update_direction_research_outcomes

                await _update_direction_research_outcomes(
                    task.project_id,
                    _task_ctx["direction_id"],
                    _task_ctx["research_task_id"],
                    session,
                )
            except Exception as _outcome_exc:
                logger.warning(
                    f"[RESEARCH] Failed to update direction research outcomes for task {task.id}: {_outcome_exc!r}"
                )
                # C1: _update_direction_research_outcomes takes the directions
                # FOR NO KEY UPDATE lock and commits; an IntegrityError /
                # serialization failure there leaves this session aborted. Roll
                # back so the terminal-record helpers below + the final commit
                # don't inherit the PendingRollback.
                with contextlib.suppress(Exception):
                    await session.rollback()

        # C1: the terminal-record helper does its own session writes (it calls
        # _record_remediation_outcome which commits). It is an independent
        # operation on the shared post-commit session: a raise must not leave the
        # session in PendingRollback for the final commit below. Run it behind a
        # rollback-on-failure guard so the chain is poison-free.
        try:
            await _record_remediation_if_terminal(task, payload.status, session)
        except Exception as _helper_exc:
            logger.error(
                f"[LIFECYCLE] Terminal-record helper _record_remediation_if_terminal "
                f"failed for task {task.id}: {_helper_exc!r}",
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()

        # Release-qualifier finalization — fires from EVERY terminal path
        # (COMPLETED / FAILED / CANCELLED) for BOTH a release_audit task and a
        # qualifier-smoketest task. This deliberately lives in the terminal
        # section (not the COMPLETED-only review-pipeline block above): a
        # qualifier child that fails/cancels must still advance qualification,
        # or the ReleaseVersion stays {status:"pending"} forever and the
        # version is wedged.
        #
        # Why here and not in agent_status.py: this runs after the status
        # endpoint's early commit, on a fresh post-commit session. The task's
        # terminal status AND any SMOKETEST_RESULT / RELEASE_AUDIT_REPORT
        # Artifact the agent posted are therefore already committed and visible
        # to the finalize SELECT (_latest_artifact_logs). The old
        # agent_status.py hook ran pre-flush with autoflush disabled and could
        # not see its own just-added Artifact.
        #
        # maybe_finalize_qualification opens its OWN dedicated DB session
        # (AsyncSessionLocal) for the entire finalize→promote chain — it does
        # NOT use this post-commit ``session``. That decoupling is deliberate:
        # promote() merges to production main and issues an intermediate
        # commit() to release its row lock before 30-120s of GitHub I/O; on a
        # caller's session that commit would prematurely commit this handler's
        # unrelated dirty work (the completed task's terminal status,
        # TaskHistory, Artifacts, remediation/regression records). Because it
        # owns its session, this call also cannot self-deadlock against the
        # Task-row lock this handler holds (it locks the ReleaseVersion row, a
        # different row). It is idempotent (it FOR UPDATE-locks the version and
        # no-ops unless last_qualification.status == "pending" and the required
        # children are terminal) and a cheap O(1) no-op — no session opened, no
        # I/O — for the overwhelming majority of tasks (those with no
        # release_version_id in context). Keyed on context["release_version_id"]
        # and/or task_type == "release_audit" / context["qualifier_smoketest"].
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            _qual_ctx = task.context or {}
            _qual_first_payload = task.first_payload or {}
            _is_qualifier_child = bool(
                _qual_ctx.get("release_version_id")
                or _qual_first_payload.get("type") == "release_audit"
                or _qual_ctx.get("qualifier_smoketest")
            )
            if _is_qualifier_child:
                try:
                    from app import release_qualifier

                    # maybe_finalize_qualification opens its OWN dedicated
                    # session (see the comment above) — never this post-commit
                    # ``session``. ``_session_factory`` is threaded through so
                    # the dedicated session is opened from the SAME factory
                    # this handler runs on: in production that is None →
                    # AsyncSessionLocal (a genuine independent connection,
                    # preserving the transaction-decoupling); under the test
                    # harness it is the test-engine factory, so the finalize
                    # sees the committed seed data and its commit is visible to
                    # the test's assertions instead of opening an unrelated
                    # connection on a different event loop.
                    await release_qualifier.maybe_finalize_qualification(
                        task, session_factory=_session_factory
                    )
                except Exception as _qual_exc:
                    logger.error(
                        f"[RELEASE_QUALIFIER] maybe_finalize_qualification failed "
                        f"for terminal task {task.id} (status={payload.status}): "
                        f"{_qual_exc!r}",
                        exc_info=True,
                    )
                    # C1 (defensive): maybe_finalize_qualification owns its own
                    # dedicated session, so it should not poison this shared
                    # ``session`` — but the pre-call accessors (task.context /
                    # task.first_payload) can trigger a lazy load that aborts the
                    # transaction, and a future refactor might touch the shared
                    # session. Roll back to match the sibling terminal-record /
                    # backstop / final-commit guards so the chain below stays
                    # poison-free.
                    with contextlib.suppress(Exception):
                        await session.rollback()

        # 7. Project State Aggregation
        # Only consider executable tasks (those with a VM) for aggregation.
        # Parent/logical tasks are updated to match the project outcome.
        #
        # 2026-05-05 fix: a QUEUED leaf task waiting on warm-handoff also has
        # ``vm_instance_id=None`` (the warm target VM is in ``warm_target_vm_id``,
        # the real ``vm_instance_id`` is set only at claim). Without the QUEUED
        # exclusion below, an aggregation pass triggered by a sibling completing
        # will sweep that QUEUED task into ``parent_tasks`` and force-FAIL it
        # with "Project aggregation: child task failed" — the task never even
        # ran. Real parent/logical tasks (research, plan) sit in EVALUATING
        # after children spawn, never in QUEUED. Production incident: task
        # 582a0210 force-failed at 19:47:30 while waiting on a warm-handoff
        # from sibling 609b380c on agent-vm-540ee.
        all_tasks_stmt = select(Task).where(Task.project_id == task.project_id)
        all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
        executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
        parent_tasks = [
            t
            for t in all_tasks
            if not t.vm_instance_id
            and t.status
            not in [
                TaskStatus.QUEUED,  # leaf awaiting claim/warm-handoff, not a parent
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ]
        ]

        project_stmt = select(Project).where(Project.id == task.project_id)
        project = (await session.execute(project_stmt)).scalars().first()

        if (
            project
            and not project.autonomous_enabled
            and project.status != ProjectStatus.CANCELLED
            and executable_statuses
        ):
            # 2026-06-02 fix: live autonomous projects must NEVER be flipped to a
            # terminal status by this aggregation. Autonomous projects spawn work
            # in waves on a timer; between one wave draining and the next being
            # created there is a window where every task is briefly terminal. If
            # that mix is all-{COMPLETED, CANCELLED} (common — tasks are routinely
            # CANCELLED for benign reasons like dedup, scope-empty, supersession,
            # graceful reassignment, and keep their VM id), the all-terminal branch
            # below would flip the project to a terminal CANCELLED/COMPLETED/FAILED
            # status. Because the autonomous engine selects work purely by the
            # ``autonomous_enabled`` flag and never consults project status, the
            # project keeps running while displaying a terminal status to the
            # operator, and nothing ever moves it back. Terminal-status aggregation
            # applies only to FINITE (non-autonomous) projects. The all-terminal
            # guard below was originally meant to make this a no-op for autonomous
            # projects, but the wave-drain window defeats it; gating on
            # ``not autonomous_enabled`` is the correct, explicit fix.
            #
            # 2026-05-05 fix: only transition project status when ALL executable
            # tasks are terminal. Previously the ``any FAILED`` branch fired
            # any time an old FAILED row sat in ``executable_statuses`` (which
            # spans every task that ever had a VM, including stale FAILED
            # tasks not yet auto-archived for up to 7 days). For autonomous
            # projects with continuous work, that pinned the project at FAILED
            # forever and re-fired the parent-aggregation sweep on every new
            # task transition. Requiring all-terminal makes the aggregation a
            # no-op for autonomous projects (always something in flight) while
            # preserving finite-project semantics.
            all_terminal = all(
                s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
                for s in executable_statuses
            )
            new_project_status = None
            if all_terminal:
                if all(s == TaskStatus.COMPLETED for s in executable_statuses):
                    new_project_status = ProjectStatus.COMPLETED
                elif any(s == TaskStatus.FAILED for s in executable_statuses):
                    new_project_status = ProjectStatus.FAILED
                elif all(
                    s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]
                    for s in executable_statuses
                ):
                    new_project_status = ProjectStatus.CANCELLED

            if new_project_status:
                project.status = new_project_status
                session.add(project)
                # Update parent/logical tasks to match the project outcome
                terminal_task_status = (
                    TaskStatus.COMPLETED
                    if new_project_status == ProjectStatus.COMPLETED
                    else TaskStatus.FAILED
                    if new_project_status == ProjectStatus.FAILED
                    else TaskStatus.CANCELLED
                )
                for pt in parent_tasks:
                    pt.status = terminal_task_status
                    if terminal_task_status == TaskStatus.FAILED:
                        pt.latest_status_message = (
                            "Project aggregation: child task failed"
                        )
                    elif terminal_task_status == TaskStatus.COMPLETED:
                        pt.latest_status_message = "Project completed"
                    else:
                        pt.latest_status_message = "Project cancelled"
                    session.add(pt)
                await session.commit()
                await broadcast_to_project(
                    str(project.id), "project_updated", {"status": new_project_status}
                )

        # Audit W2 (D2/D4): backstop for the synchronous strategic chain.
        # If a strategic task (design_doc_update or strategic_review) reached
        # a terminal status but the completion handlers above failed to update
        # the StrategicCycle row, the cycle would stay "phase*_running"
        # forever and the stalled-cycle reaper would have to clean it up
        # later. Catch it here while the task lifecycle still has context.
        try:
            cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if cycle_id and payload.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                from app import strategic_cycle

                cycle_row = await strategic_cycle.get(session, cycle_id)
                if (
                    cycle_row is not None
                    and cycle_row.outcome in strategic_cycle.ACTIVE_OUTCOMES
                ):
                    task_type = task.first_payload.get("type", "")
                    failed_phase = (
                        "phase1" if task_type == "design_doc_update" else "phase2"
                    )
                    logger.warning(
                        f"[STRATEGIC] Task {task.id} ({task_type}) reached {payload.status} "
                        f"but cycle {cycle_id} still in {cycle_row.outcome} — backstop marking failed_{failed_phase}"
                    )
                    await strategic_cycle.mark_failure(
                        session,
                        cycle_id,
                        phase=failed_phase,
                        reason=f"lifecycle_backstop: task {task.id} terminal in {payload.status} without completion-handler cycle update",
                    )
                    await session.commit()
        except Exception as _bk_exc:
            logger.error(
                f"[STRATEGIC] Lifecycle backstop failed for task {task.id}: {_bk_exc!r}",
                exc_info=True,
            )
            # C1: strategic_cycle.mark_failure + the commit inside the backstop
            # can raise (lock contention / DB error), leaving the session
            # aborted. Roll back so the final commit below does not raise
            # PendingRollbackError and return a 500 to the agent.
            with contextlib.suppress(Exception):
                await session.rollback()

        # Final commit for any deferred changes (status, artifacts, spawned tasks).
        # C1: guard the commit. The task's own terminal status was already
        # committed on the early-commit (status-endpoint) session before this
        # post-commit handler ran, so a failure here is NOT fatal to the task.
        # BUT the deferred side effects it would flush (spawned follow-up /
        # remediation tasks, rollup nudges, artifacts) are now DROPPED — they
        # were never persisted. _process_post_commit runs behind the
        # write-ahead ``post_commit_pending`` marker + sweep-retry machinery;
        # both the inline path (_clear_post_commit_pending, agent_status.py
        # ~2946) and the sweep (services.py ~1954) clear that marker on a CLEAN
        # return, so swallowing this failure and returning {status:"received"}
        # would clear the marker and the sweep would NEVER retry the dropped
        # side effects (incident eefbed05: dependent chain orphaned).
        #
        # So roll back the poisoned transaction (cleanly closing the session)
        # and raise PostCommitFinalCommitFailed: a distinct signal the callers
        # treat as "PRESERVE the marker for the sweep" — while still NOT 500ing
        # the agent (the inline/retry callers translate it to a clean 200).
        if session.dirty or session.new:
            try:
                await session.commit()
            except Exception as _commit_exc:
                logger.error(
                    f"[LIFECYCLE] Final post-commit commit failed for task "
                    f"{task.id}: {_commit_exc!r} — preserving post_commit_pending "
                    f"marker for sweep retry (deferred side effects not persisted)",
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise PostCommitFinalCommitFailed(
                    f"final post-commit commit failed for task {task.id}: "
                    f"{_commit_exc!r}"
                ) from _commit_exc

        # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": payload.status},
        )

        return {
            "status": "received",
            "message": f"Status update for task {payload.task_id} recorded.",
        }


# ---------------------------------------------------------------------------
# Orphan-PR cleanup on cancellation
# ---------------------------------------------------------------------------
#
# When a task is cancelled, any PR it opened is left orphaned: the work is
# no longer wanted, but the PR sits there blocking the rollup gate (parent
# stays EVALUATING waiting on an open PR that nobody will merge). Most
# common cause is duplicate-dedup — a task's twin shipped the same finding,
# the twin merged via rollup, the duplicate was cancelled, but its PR
# stayed open. See c56fa118 / PR #5983 incident on 2026-05-26.
#
# We expose `close_orphan_pr_if_any` as an explicit helper that cancel-site
# callers invoke with their already-open session + the task being cancelled.
# An earlier rev tried to wire this into the Task.status terminal-transition
# event listener as a fire-and-forget; that opened a second session on the
# StaticPool SQLite engine the test fixtures share, racing the test's own
# session and breaking ~20 unrelated requeue tests. Explicit calls at known
# cancel sites are noisier but safer.
async def close_orphan_pr_if_any(
    task: Task, session: AsyncSession, *, reason: str = "task_cancelled"
) -> bool:
    """Best-effort: close the open PR associated with `task` if any.

    Returns True if a PR was closed, False otherwise. Skips merged PRs
    (state=closed,merged=true) and already-closed PRs. Failures (no token,
    GitHub 5xx, PR vanished) are logged but never raised — the caller's
    cancel must always complete.

    Caller should invoke this AFTER setting task.status to CANCELLED but
    BEFORE the session.commit() that persists the cancel — the GitHub PATCH
    is independent of the DB transaction either way, and grouping them
    lets the caller bail cleanly if the GitHub call hangs.
    """
    ctx = task.context or {}
    pr_url = ctx.get("_pr_url") or ctx.get("pr_url") or ""
    if not pr_url:
        return False
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        return False
    pr_number = int(m.group(1))

    from app.github import close_pull_request, get_pull_request
    from app.routers.agent import get_github_token_for_task

    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] get_github_token_for_task failed for "
            f"task {task.id} (reason={reason})"
        )
        return False
    try:
        pr = await get_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.warning(
            f"[CANCEL-PR-CLEANUP] get_pull_request failed for task "
            f"{task.id} PR #{pr_number} (reason={reason}) — likely already "
            f"closed or GitHub transient; skipping"
        )
        return False
    if pr.get("merged"):
        return False
    if pr.get("state") != "open":
        return False
    try:
        ok = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] close_pull_request raised for task "
            f"{task.id} PR #{pr_number} (reason={reason})"
        )
        return False
    if ok:
        logger.info(
            f"[CANCEL-PR-CLEANUP] Closed orphan PR #{pr_number} for "
            f"cancelled task {task.id} (reason={reason})"
        )
    return ok
