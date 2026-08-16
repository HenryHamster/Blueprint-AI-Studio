"""CI auto-remediation: detect protected-branch CI failures and spawn fix tasks.

When a check_run webhook arrives with conclusion=failure on a protected branch
(e.g. main/master), this module spawns a create-type task that analyzes the
failure and opens a fix PR.  Guard rails prevent runaway spawning.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.direction_lifecycle import _REMEDIATION_MONITOR_EXPIRY_DAYS
from app.github import (
    get_check_runs_cached,
    get_pull_request,
    get_repo_installation_token,
    is_ancestor_of,
)
from app.models import Project, Task, TaskStatus
from app.notification_dispatch import post_auto_fix_spawned_message
from app.task_completion import _stamp_remediation_spawn_on_direction
from app.task_locking import with_design_doc_locked, with_directions_locked
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Auto-recovery of suspended branches: after SUSPENSION_BASE_COOLDOWN_HOURS,
# attempt remediation again. Cooldown doubles per recovery attempt
# (exponential backoff). After MAX_AUTO_RECOVERY_ATTEMPTS, the branch stays
# permanently suspended and requires manual intervention.
SUSPENSION_BASE_COOLDOWN_HOURS = 24
MAX_AUTO_RECOVERY_ATTEMPTS = 3


def _suspension_cooldown_elapsed(meta: dict, now: datetime) -> bool:
    """Return True if the suspension entry's cooldown has elapsed."""
    attempts = int(meta.get("recovery_attempts", 0) or 0)
    if attempts >= MAX_AUTO_RECOVERY_ATTEMPTS:
        return False
    suspended_at_str = meta.get("suspended_at")
    if not suspended_at_str:
        return False
    try:
        suspended_at = datetime.fromisoformat(suspended_at_str)
    except (TypeError, ValueError):
        return False
    cooldown = timedelta(hours=SUSPENSION_BASE_COOLDOWN_HOURS * (2**attempts))
    return now >= suspended_at + cooldown


async def _try_auto_recover_suspension(
    *,
    project_id: uuid.UUID,
    branch: str,
    config: dict[str, Any],
    session: AsyncSession,
) -> bool:
    """Clear a suspended branch when its cooldown has elapsed.

    Returns True if the branch was recovered (caller should proceed with
    remediation), False if still in cooldown or permanently suspended.
    Persists changes to the project's _auto_remediation config.

    The read-modify-write of design_doc["_auto_remediation"] runs through
    ``with_design_doc_locked`` (skip_locked=False, blocking) so that two
    webhook-driven CI-failure handlers for the same project cannot interleave
    and silently overwrite each other's suspension/recovery-attempt state — a
    lost-update race on the per-branch config that gates fix-task spawning.
    The recovery decision is read off the freshly-locked doc (not the stale
    ``config`` argument) and surfaced via a closure flag.
    """
    now = _utcnow()
    recovered = False
    # Captures the final config so it can be dual-written to project_telemetry
    # after the design_doc lock closure runs (None => no mutation occurred).
    persisted_config: dict | None = None

    def _mutate(doc: dict) -> None:
        nonlocal recovered, persisted_config
        ar_config = dict(doc.get("_auto_remediation", {}))
        suspension_meta = dict(ar_config.get("suspension_metadata", {}))
        meta = dict(suspension_meta.get(branch) or {})

        if not _suspension_cooldown_elapsed(meta, now):
            # Backfill missing timestamp so the first auto-recovery can happen
            # after a full cooldown from this point.
            if (
                not meta.get("suspended_at")
                and int(meta.get("recovery_attempts", 0) or 0)
                < MAX_AUTO_RECOVERY_ATTEMPTS
            ):
                meta["suspended_at"] = now.isoformat()
                meta.setdefault("recovery_attempts", 0)
                suspension_meta[branch] = meta
                ar_config["suspension_metadata"] = suspension_meta
                doc["_auto_remediation"] = ar_config
                persisted_config = ar_config
            return

        # Cooldown elapsed — recover the branch: clear it from the suspended
        # list / failure counts and bump the recovery-attempt count for the
        # next exponential-backoff window.
        suspended = [b for b in ar_config.get("suspended_branches", []) if b != branch]
        failure_counts = dict(ar_config.get("branch_failure_counts", {}))
        failure_counts.pop(branch, None)
        attempts = int(meta.get("recovery_attempts", 0) or 0)
        suspension_meta[branch] = {
            "suspended_at": meta.get("suspended_at"),
            "recovery_attempts": attempts + 1,
        }
        ar_config["suspended_branches"] = suspended
        ar_config["branch_failure_counts"] = failure_counts
        ar_config["suspension_metadata"] = suspension_meta
        doc["_auto_remediation"] = ar_config
        persisted_config = ar_config
        recovered = True
        logger.info(
            "[AUTO-REMEDIATION] Auto-recovering suspended branch %s (attempt %d/%d)",
            branch,
            attempts + 1,
            MAX_AUTO_RECOVERY_ATTEMPTS,
        )

    await with_design_doc_locked(session, project_id, _mutate, skip_locked=False)
    if persisted_config is not None:
        # Dual-write to project_telemetry (design_doc copy kept for rollback).
        from app.project_telemetry import set_project_telemetry

        await set_project_telemetry(
            session, project_id, "_auto_remediation", persisted_config
        )
    return recovered


async def _handle_main_branch_ci_failure(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    head_branch: str,
    project_id: uuid.UUID,
    config: dict[str, Any],
    ci_source_sha: str | None = None,
    release_version_id: uuid.UUID | None = None,
) -> None:
    """Background handler: spawn a fix task for a protected-branch CI failure.

    Opens its own DB session (runs outside the request lifecycle).

    ``ci_source_sha`` decouples *where the CI failed* from *where the fix lands*.
    The normal webhook path leaves it None: the CI failed on ``head_branch`` at
    ``head_sha`` and the fix branches off ``head_branch``.  The promotion-gate
    harvest sets it to the (throwaway) release-branch commit whose full CI matrix
    failed, while ``head_branch`` is the integration branch (automain) the fix
    must land on — so the failing-test context is read from ``ci_source_sha`` but
    the fix PR targets ``head_branch``.  ``release_version_id`` tags the spawned
    task so the re-qualification gate can see remediation is in flight.
    """
    try:
        async with AsyncSessionLocal() as session:
            await _handle_main_branch_ci_failure_inner(
                owner=owner,
                repo=repo,
                head_sha=head_sha,
                head_branch=head_branch,
                project_id=project_id,
                config=config,
                session=session,
                ci_source_sha=ci_source_sha,
                release_version_id=release_version_id,
            )
    except Exception as exc:
        logger.error(
            "[AUTO-REMEDIATION] Error handling CI failure on %s/%s@%s: %s",
            owner,
            repo,
            head_sha[:8],
            exc,
            exc_info=True,
        )


async def _handle_main_branch_ci_failure_inner(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    head_branch: str,
    project_id: uuid.UUID,
    config: dict[str, Any],
    session: AsyncSession,
    ci_source_sha: str | None = None,
    release_version_id: uuid.UUID | None = None,
) -> None:
    """Inner implementation, called with a live session.

    See ``_handle_main_branch_ci_failure`` for the meaning of ``ci_source_sha``
    and ``release_version_id``.
    """
    max_per_hour = config.get("max_per_hour", 2)
    max_concurrent = config.get("max_concurrent", 2)
    now = _utcnow()

    # ── Guard: branch suspension (escalation after repeated failures) ──
    suspended_branches = config.get("suspended_branches", [])
    if head_branch in suspended_branches:
        recovered = await _try_auto_recover_suspension(
            project_id=project_id,
            branch=head_branch,
            config=config,
            session=session,
        )
        if not recovered:
            logger.info(
                "[AUTO-REMEDIATION] Branch %s is suspended (cooldown not elapsed or "
                "max auto-recovery attempts reached) — skipping.",
                head_branch,
            )
            meta = (config.get("suspension_metadata") or {}).get(head_branch) or {}
            if int(meta.get("recovery_attempts", 0) or 0) >= MAX_AUTO_RECOVERY_ATTEMPTS:
                try:
                    from app.notification import _send_webhook_notification
                    import asyncio

                    asyncio.ensure_future(
                        _send_webhook_notification(
                            "AUTO_FIX_PERMANENT_ESCALATION",
                            f"Branch {head_branch} permanently suspended",
                            f"Auto-recovery exhausted after {MAX_AUTO_RECOVERY_ATTEMPTS} attempts — manual intervention required",
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "[AUTO-REMEDIATION] Failed to send permanent-escalation webhook: %s",
                        exc,
                    )
            return

        # Recovery succeeded. Commit the suspension-lifted write now so the
        # project-row FOR UPDATE lock taken by with_design_doc_locked is
        # released before the network-heavy section below (branch-SHA /
        # check-run lookups, CI-context extraction, PR/ancestry checks).
        # Holding that lock across external GitHub round-trips would block
        # concurrent same-project design_doc/directions/vision writers
        # (skip_locked=False) for up to the 30s lock_timeout. The recovery is
        # an independent, idempotent state change, so persisting it regardless
        # of whether a fix task is ultimately spawned below is correct.
        await session.commit()

    # ── Guard: rate limit ──
    # Audit W1 (B4/B14): auto_remediation is now an indexed column, so this
    # query can push the filter into SQL instead of scanning all recent tasks
    # and filtering in Python.
    one_hour_ago = now - timedelta(hours=1)
    recent_auto_count = (
        await session.execute(
            select(_func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.created_at >= one_hour_ago,
                Task.auto_remediation.is_(True),
            )
        )
    ).scalar() or 0
    if recent_auto_count >= max_per_hour:
        logger.info(
            "[AUTO-REMEDIATION] Rate limit hit (%d/%d in last hour) for project %s",
            recent_auto_count,
            max_per_hour,
            project_id,
        )
        return

    # ── Guard: duplicate detection (same branch, not SHA) ──
    # A fix task for the same branch is already working on the problem.
    # It clones latest, so it covers any new pushes to that branch.
    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(active_statuses),
                    Task.auto_remediation.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active_tasks:
        ctx = t.context or {}
        if ctx.get("ci_failure_branch") == head_branch:
            logger.info(
                "[AUTO-REMEDIATION] Duplicate: active task %s already covers branch %s",
                t.id,
                head_branch,
            )
            return

    # ── Guard: concurrent limit ──
    # Audit W1 (B4/B14): active_tasks is now already filtered to
    # auto_remediation=True, so the count is a direct len().
    active_auto_count = len(active_tasks)
    if active_auto_count >= max_concurrent:
        logger.info(
            "[AUTO-REMEDIATION] Concurrent limit hit (%d/%d) for project %s",
            active_auto_count,
            max_concurrent,
            project_id,
        )
        return

    # ── Guard: check if CI already passed on latest commit ──
    # Between the webhook firing and this handler running, another push may
    # have fixed the issue. Verify the branch tip is still failing.
    #
    # Skipped for the promotion-gate harvest (ci_source_sha set): there the CI
    # failed on a throwaway release-branch commit, while head_branch is the
    # integration branch (automain) the fix must land on. The two diverge by
    # design, so the "is head_branch's tip still failing?" check does not apply —
    # the failure context lives at ci_source_sha, which we read directly.
    if ci_source_sha is not None:
        head_sha = ci_source_sha
    else:
        try:
            from app.github import get_branch_sha

            latest_sha = await get_branch_sha(
                await get_repo_installation_token(owner, repo),
                owner,
                repo,
                head_branch,
            )
            if latest_sha != head_sha:
                latest_ci = await get_check_runs_cached(
                    await get_repo_installation_token(owner, repo),
                    owner,
                    repo,
                    latest_sha,
                )
                if (
                    latest_ci.get("status") == "completed"
                    and latest_ci.get("conclusion") == "success"
                ):
                    logger.info(
                        "[AUTO-REMEDIATION] CI already passing on latest %s@%s — skipping",
                        head_branch,
                        latest_sha[:8],
                    )
                    return
                # Update to latest SHA so the fix task prompt shows the current failure
                head_sha = latest_sha
        except Exception as exc:
            logger.warning("[AUTO-REMEDIATION] Could not verify latest CI status: %s", exc)
            # Proceed anyway — better to spawn a potentially unnecessary task than miss a real failure

    # ── CI context extraction ──
    (
        failed_checks,
        error_summary,
        failure_type,
        structured_signals,
    ) = await _extract_ci_context(
        owner,
        repo,
        head_sha,
        project_id,
        session,
    )

    # ── Guard: skip if no useful CI context was extracted ──
    if not failed_checks and not error_summary and failure_type == "unknown":
        logger.warning(
            "[AUTO-REMEDIATION] Skipping task creation for %s@%s: "
            "CI context extraction returned no useful information (no failed checks, no error summary)",
            head_branch,
            head_sha[:8],
        )
        try:
            from app.notification import _send_webhook_notification
            import asyncio

            asyncio.ensure_future(
                _send_webhook_notification(
                    "AUTO_FIX_SKIPPED",
                    f"CI auto-remediation skipped on {head_branch}: insufficient context",
                    f"SHA: {head_sha[:8]} — no failed checks, no error logs extracted",
                )
            )
        except Exception as exc:
            logger.warning(
                "[AUTO-REMEDIATION] Failed to send skip webhook notification: %s", exc
            )
        return

    # ── Guard: per-fingerprint repeat-attempt cap ──
    # Prevent the same root failure from spawning unbounded retry tasks even
    # when branch suspension hasn't kicked in yet (suspension is governed by
    # consecutive failures via post_auto_fix_result_message; this cap acts on
    # the spawn side using the error fingerprint so a flapping check can't
    # burn through quota). Must use the stage-aware fingerprint to match what
    # gets stamped on spawned tasks (see below: stage = _derive_failure_stage(...)
    # then fingerprint = _error_fingerprint(error_summary, stage)). A stage-less
    # probe would never match because _derive_failure_stage classifies typical
    # CI text into build/test/lint/etc.
    from app.anomaly_detection import _error_fingerprint
    from app.failure_stage import _derive_failure_stage

    fingerprint = (
        _error_fingerprint(
            error_summary, _derive_failure_stage("CODE_ERROR", error_summary)
        )
        if error_summary
        else ""
    )
    if fingerprint:
        max_per_fingerprint = config.get("max_per_fingerprint", 3)
        fp_window_start = now - timedelta(hours=24)
        # previous_error_fingerprint is indexed as part of
        # ix_task_auto_remediation_active. Include QUEUED/IN_PROGRESS active
        # spawns alongside terminal FAILED/CANCELLED so concurrent in-flight
        # remediations count toward the cap (fixes the parallel-spawn-overshoot
        # race).
        cap_states = [
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ]
        fp_failed_count = (
            await session.execute(
                select(_func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.created_at >= fp_window_start,
                    Task.status.in_(cap_states),
                    Task.auto_remediation.is_(True),
                    Task.previous_error_fingerprint == fingerprint,
                )
            )
        ).scalar() or 0
        if fp_failed_count >= max_per_fingerprint:
            logger.warning(
                "[AUTO-REMEDIATION] Per-fingerprint cap hit (%d/%d) for branch %s "
                "fingerprint %s — skipping repeat attempt",
                fp_failed_count,
                max_per_fingerprint,
                head_branch,
                fingerprint[:16],
            )
            try:
                from app.notification import _send_webhook_notification
                import asyncio

                asyncio.ensure_future(
                    _send_webhook_notification(
                        "AUTO_FIX_SKIPPED",
                        f"CI auto-remediation skipped on {head_branch}: fingerprint cap reached",
                        f"SHA: {head_sha[:8]} — {fp_failed_count} prior failed attempts with same fingerprint",
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[AUTO-REMEDIATION] Failed to send fingerprint skip webhook: %s",
                    exc,
                )
            return

    # ── Look up prior investigation that may have diagnosed this failure ──
    prior_investigation = await _find_matching_prior_investigation(
        project_id=project_id,
        error_summary=error_summary,
        session=session,
    )

    # ── Regression detection: recently-merged remediation for same branch+fingerprint ──
    regression_of: Task | None = None
    if error_summary:
        from app.anomaly_detection import _error_fingerprint as _fp
        from app.failure_stage import _derive_failure_stage

        # Match the stage-aware fingerprint that new remediation tasks persist
        # (see below) so regression lookups can actually hit on prior stored rows.
        _probe_stage = _derive_failure_stage("CODE_ERROR", error_summary)
        _fingerprint_probe = _fp(error_summary, _probe_stage)
        regression_of = await _find_recent_merged_remediation(
            project_id=project_id,
            branch=head_branch,
            fingerprint=_fingerprint_probe,
            session=session,
        )

    # ── Ancestry verification: confirm the prior fix commit is actually on this branch ──
    # A COMPLETED task whose PR was auto-closed, merged elsewhere, or never landed must
    # not be treated as a held fix. Safe-degrade: unconfirmed ancestry → not-landed.
    if regression_of is not None:
        _reg_ctx = regression_of.context or {}
        _reg_pr_num = _reg_ctx.get("merged_pr_number") or _reg_ctx.get("pr_number")
        _ancestry_confirmed = False
        try:
            if _reg_pr_num:
                _anc_token = await get_repo_installation_token(owner, repo)
                if _anc_token:
                    _pr_data = await get_pull_request(
                        _anc_token, owner, repo, int(_reg_pr_num)
                    )
                    _merge_sha = _pr_data.get("merge_commit_sha")
                    if _merge_sha:
                        _is_anc = await is_ancestor_of(
                            _anc_token, owner, repo, _merge_sha, head_branch
                        )
                        _ancestry_confirmed = _is_anc is True
        except Exception as _anc_exc:
            logger.warning(
                "[AUTO-REMEDIATION] Ancestry check failed for prior remediation task %s"
                " (PR #%s): %s — safe-degrading to not-landed.",
                str(regression_of.id)[:8],
                _reg_pr_num,
                _anc_exc,
            )
        if not _ancestry_confirmed:
            logger.info(
                "[AUTO-REMEDIATION] Prior remediation task %s not confirmed as an ancestor"
                " of %s — not treating as regression.",
                str(regression_of.id)[:8],
                head_branch,
            )
            regression_of = None

    # ── Create the fix task ──
    prompt = _build_remediation_prompt(
        head_branch=head_branch,
        head_sha=head_sha,
        failed_checks=failed_checks,
        error_summary=error_summary,
        failure_type=failure_type,
        prior_investigation=prior_investigation,
        regression_of=regression_of,
        structured_signals=structured_signals,
    )

    from app.failure_classification import _external_api_subtype_from_message

    _ext_api_subtype = (
        _external_api_subtype_from_message(error_summary) if error_summary else None
    )

    task_context: dict[str, Any] = {
        "auto_remediation": True,
        "ci_failure_sha": head_sha,
        "ci_failure_branch": head_branch,
        "ci_failure_checks": failed_checks,
        "ci_failure_type": failure_type,
        "trigger_failure_class": "MIGRATION_ERROR"
        if failure_type == "migration"
        else "CODE_ERROR",
    }
    if release_version_id is not None:
        # Promotion-gate harvest: tag the task so compute_requalification gating
        # can see a fix is in flight for this version and defer re-qualification
        # until it lands on the integration branch (see release_qualifier_sweep).
        task_context["promotion_ci_remediation"] = True
        task_context["promotion_ci_remediation_version_id"] = str(release_version_id)
    if _ext_api_subtype and _ext_api_subtype != "other":
        task_context["external_api_subtype"] = _ext_api_subtype
    if regression_of is not None:
        task_context["regression_of_task_id"] = str(regression_of.id)
        prev_pr = (regression_of.context or {}).get("merged_pr_number") or (
            regression_of.context or {}
        ).get("pr_number")
        if prev_pr:
            task_context["regression_prior_pr_number"] = prev_pr
    fingerprint: str | None = None
    if error_summary:
        from app.anomaly_detection import _error_fingerprint
        from app.failure_stage import _derive_failure_stage

        # CI remediation tasks classify as CODE_ERROR; stage is inferred from
        # the error text so build/test/lint failures fingerprint distinctly.
        stage = _derive_failure_stage("CODE_ERROR", error_summary)
        fingerprint = _error_fingerprint(error_summary, stage)
        task_context["_previous_error_fingerprint"] = fingerprint
    if prior_investigation is not None:
        task_context["prior_diagnosis_task_id"] = str(prior_investigation.id)
        # Propagate diagnosis linkage so _record_remediation_outcome can stamp
        # the direction's remediation_history when this task reaches a terminal
        # outcome. Without these fields, the merge/fail handlers early-return
        # and the direction never observes the remediation result.
        prior_ctx = prior_investigation.context or {}
        prior_direction_id = prior_ctx.get("direction_id")
        if prior_direction_id:
            task_context["diagnosis_task_id"] = str(prior_investigation.id)
            task_context["direction_id"] = str(prior_direction_id)
            prior_fingerprint = prior_ctx.get("diagnosis_fingerprint") or prior_ctx.get(
                "error_fingerprint"
            )
            if prior_fingerprint:
                task_context["diagnosis_fingerprint"] = str(prior_fingerprint)
    # Fallback: no prior-investigation linkage, but if the CI error fingerprint
    # matches an existing direction's fingerprint, stamp direction_id so the
    # fix's outcome still reaches _record_remediation_outcome.
    if "direction_id" not in task_context and fingerprint:
        matched_direction_id = await _find_direction_by_fingerprint(
            project_id=project_id,
            fingerprint=fingerprint,
            session=session,
        )
        if matched_direction_id:
            task_context["direction_id"] = matched_direction_id
            task_context["diagnosis_fingerprint"] = fingerprint

    # Audit W1 (B4/B14): set the auto_remediation column + previous_error_fingerprint
    # column directly so the cap queries above see this spawn immediately.
    # Strip the now-redundant context keys (no dual-source).
    task_context.pop("auto_remediation", None)
    task_context.pop("_previous_error_fingerprint", None)
    # Forward the failing pytest node-ids as ci_failure_selectors so the agent's
    # ./check script runs those selectors against real Postgres (via the existing
    # buildCiFailureVerifyBlock path in check-helper.ts) before ./pr open — this
    # closes the SQLite-vs-Postgres masking gap described in the harvest prompt.
    # Cap at 20 selectors to keep the ./check command line manageable; the most
    # specific failing-test ids are listed first by _extract_structured_signals.
    _ci_failure_selectors: list[str] = (
        structured_signals.get("failing_tests") or []
    )[:20]
    task_payload: dict[str, object] = {
        "type": "create",
        "effort": "medium",
        "base_branch": head_branch,
        "title": f"fix: auto-remediate CI failure on {head_branch}",
        "prompt": prompt,
    }
    if _ci_failure_selectors:
        task_payload["ci_failure_selectors"] = _ci_failure_selectors

    # The webhook may spend time on GitHub and diagnostic lookups above.  Take
    # the canonical Project authority only at the publication edge, then retain
    # it through the Task INSERT and commit so pause/archive cannot race a
    # latent remediation into the queue.
    from app.task_locking import (
        AUTOMATIC_TASK_CREATION_ALLOWED,
        acquire_automatic_task_creation_authority,
    )

    creation_admission = await acquire_automatic_task_creation_authority(
        session, project_id
    )
    if creation_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
        logger.info(
            "[AUTO-REMEDIATION] Automatic task creation held for %s@%s: %s",
            head_branch,
            head_sha[:8],
            creation_admission,
        )
        await session.rollback()
        return

    task = Task(
        project_id=project_id,
        title=f"Auto-fix: CI failure on {head_branch}",
        payloads=[task_payload],
        context=task_context,
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REMEDIATION",
        auto_remediation=True,
        previous_error_fingerprint=fingerprint,
    )
    session.add(task)
    await session.flush()  # assign task.id
    direction_id = task_context.get("direction_id")
    if direction_id:

        def _stamp_direction(directions: list) -> None:
            target = next((d for d in directions if d.get("id") == direction_id), None)
            if target is not None:
                _stamp_remediation_spawn_on_direction(target, task)

        await with_directions_locked(
            session, project_id, _stamp_direction, skip_locked=False
        )

    # ── Notifications ──
    try:
        await post_auto_fix_spawned_message(
            session,
            project_id,
            branch=head_branch,
            failed_checks=failed_checks,
            task_id=task.id,
        )
    except Exception as exc:
        logger.warning("[AUTO-REMEDIATION] Failed to post event message: %s", exc)

    try:
        from app.notification import _send_webhook_notification
        import asyncio

        asyncio.ensure_future(
            _send_webhook_notification(
                "AUTO_FIX_SPAWNED",
                f"CI failure auto-remediation on {head_branch}",
                f"SHA: {head_sha[:8]}, failed checks: {', '.join(failed_checks[:5])}",
            )
        )
    except Exception as exc:
        logger.warning(
            "[AUTO-REMEDIATION] Failed to send webhook notification: %s", exc
        )

    await session.commit()
    logger.info(
        "[AUTO-REMEDIATION] Spawned task %s for CI failure on %s@%s (%s)",
        task.id,
        head_branch,
        head_sha[:8],
        failure_type,
    )


# Structured-signal extraction patterns — run against full raw logs BEFORE
# the 3000-char truncation so critical test names / file paths / error lines
# beyond the cutoff still reach the remediation prompt.
_FAILING_TEST_PATTERNS = (
    re.compile(r"FAILED\s+(\S+::\S+)"),  # pytest: FAILED tests/x.py::test_y
    re.compile(r"^FAIL\s+(\S+)", re.MULTILINE),  # jest/generic: FAIL path/to/spec
    re.compile(r"✗\s+(\S+)"),  # vitest/tap-style failure marker
)
_FILE_LOCATION_PATTERNS = (
    re.compile(r'File "([^"]+)", line (\d+)'),  # python traceback
    re.compile(
        r"^\s*(\S+\.(?:ts|tsx|js|jsx|py)):(\d+)(?::\d+)?", re.MULTILINE
    ),  # tsc/eslint/pytest short
)
_ERROR_MESSAGE_PATTERNS = (
    re.compile(r"([A-Z][A-Za-z0-9_.]*Error:[^\n]*)"),
    re.compile(r"^(assert [^\n]+)", re.MULTILINE),
    re.compile(r"(error TS\d+:[^\n]+)"),
    re.compile(
        r"^\s*([A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|py):\d+:\d+\s+error\s+[^\n]+)",
        re.MULTILINE,
    ),
)


def _extract_structured_signals(raw_logs: str) -> dict[str, list[str]]:
    """Parse failing tests, file:line locations, and error messages from full logs.

    Runs against the untruncated raw_logs so signals past the 3000-char cutoff
    still reach the agent prompt via the structured-signals section.
    """
    signals: dict[str, list[str]] = {
        "failing_tests": [],
        "error_locations": [],
        "error_messages": [],
    }
    if not raw_logs:
        return signals

    def _push(key: str, value: str, limit: int) -> None:
        value = value.strip()
        if value and value not in signals[key] and len(signals[key]) < limit:
            signals[key].append(value)

    for pat in _FAILING_TEST_PATTERNS:
        for m in pat.finditer(raw_logs):
            _push("failing_tests", m.group(1), 20)
    for pat in _FILE_LOCATION_PATTERNS:
        for m in pat.finditer(raw_logs):
            _push("error_locations", f"{m.group(1)}:{m.group(2)}", 20)
    for pat in _ERROR_MESSAGE_PATTERNS:
        for m in pat.finditer(raw_logs):
            _push("error_messages", m.group(1)[:300], 15)
    return signals


async def _extract_ci_context(
    owner: str,
    repo: str,
    head_sha: str,
    project_id: uuid.UUID,
    session: AsyncSession,
) -> tuple[list[str], str, str, dict[str, list[str]]]:
    """Fetch CI details and classify the failure.

    Returns (failed_checks, error_summary, failure_type, structured_signals).
    """
    failed_checks: list[str] = []
    error_summary = ""
    failure_type = "unknown"
    structured_signals: dict[str, list[str]] = {
        "failing_tests": [],
        "error_locations": [],
        "error_messages": [],
    }

    try:
        token = await get_repo_installation_token(owner, repo)
        if not token:
            logger.warning(
                "[AUTO-REMEDIATION] No GitHub token available for %s/%s", owner, repo
            )
            return failed_checks, error_summary, failure_type, structured_signals

        ci_result = await get_check_runs_cached(
            token, owner, repo, head_sha, project_id=str(project_id)
        )
        details = ci_result.get("details", [])
        if not isinstance(details, list):
            details = []
        failed_checks = []
        for d in details:
            if not isinstance(d, dict) or d.get("conclusion") != "failure":
                continue
            name = d.get("name")
            if name:
                failed_checks.append(str(name))
        raw_logs = ci_result.get("failed_logs", "")
        if not isinstance(raw_logs, str):
            raw_logs = str(raw_logs or "")
        structured_signals = _extract_structured_signals(raw_logs)
        error_summary = raw_logs[:3000]

        # Heuristic failure type classification — check specific patterns
        # before generic ones to avoid "Build failed" matching as "test"
        if raw_logs:
            logs_lower = raw_logs.lower()
            from app.failure_classification import _MIGRATION_PATTERNS

            if any(p in logs_lower for p in _MIGRATION_PATTERNS):
                failure_type = "migration"
            elif "importerror" in logs_lower or "syntaxerror" in logs_lower:
                failure_type = "build"
            elif (
                "assertionerror" in logs_lower
                or "assert " in logs_lower
                or "pytest" in logs_lower
            ):
                failure_type = "test"
            elif "error:" in logs_lower and any(
                c.isdigit() for c in raw_logs.split("error:")[0][-20:] if c
            ):
                failure_type = "lint"
            elif "failed" in logs_lower:
                failure_type = "test"
            else:
                failure_type = "mixed"
    except Exception as exc:
        logger.warning("[AUTO-REMEDIATION] CI context extraction failed: %s", exc)

    return failed_checks, error_summary, failure_type, structured_signals


async def _find_direction_by_fingerprint(
    *,
    project_id: uuid.UUID,
    fingerprint: str,
    session: AsyncSession,
) -> str | None:
    """Return a direction id whose error/last-remediation fingerprint matches."""
    if not fingerprint:
        return None
    project = await session.get(Project, project_id)
    if not project:
        return None
    matches: list[dict] = []
    for d in project.directions or []:
        if not d.get("id"):
            continue
        if (
            d.get("error_fingerprint") == fingerprint
            or d.get("last_remediation_fingerprint") == fingerprint
        ):
            matches.append(d)
    if not matches:
        return None

    def _rank(d: dict) -> tuple[int, str]:
        # Lower tuple sorts first. Prefer status == "active" and not auto_paused,
        # then most recent last_remediation_at (descending via negation trick:
        # use string compare, and invert by sorting reverse within bucket).
        is_active = (
            0 if (d.get("status") == "active" and not d.get("auto_paused")) else 1
        )
        return (is_active, str(d.get("last_remediation_at") or ""))

    matches.sort(key=lambda d: (_rank(d)[0],))
    top_bucket = _rank(matches[0])[0]
    top = [d for d in matches if _rank(d)[0] == top_bucket]
    top.sort(key=lambda d: str(d.get("last_remediation_at") or ""), reverse=True)
    return str(top[0].get("id"))


async def _find_recent_merged_remediation(
    *,
    project_id: uuid.UUID,
    branch: str,
    fingerprint: str | None,
    session: AsyncSession,
    window_hours: int = _REMEDIATION_MONITOR_EXPIRY_DAYS * 24,
) -> Task | None:
    """Return a recently-completed auto-remediation task for the same branch+fingerprint.

    Used to detect regressions: if a prior fix merged for this error fingerprint
    and CI is failing again on the same branch with the same signature, the fix
    did not hold. The default window matches the remediation monitor expiry so
    any failure within the monitored period is flagged as a regression.
    """
    if not fingerprint:
        return None
    cutoff = _utcnow() - timedelta(hours=window_hours)
    # Audit W1: filter on the indexed columns instead of scanning JSONB.
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at >= cutoff,
                    Task.auto_remediation.is_(True),
                    Task.previous_error_fingerprint == fingerprint,
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for t in rows:
        ctx = t.context or {}
        if ctx.get("ci_failure_branch") == branch:
            return t
    return None


async def _find_matching_prior_investigation(
    *,
    project_id: uuid.UUID,
    error_summary: str,
    session: AsyncSession,
) -> Task | None:
    """Return a recent completed investigate task whose diagnosis matches the CI failure.

    Matches via (1) any diagnosed affected-file path appearing in error_summary, or
    (2) keyword overlap between the investigation's summary/root_cause and
    error_summary. Requires a non-empty summary and root_cause in plan_output.
    """
    if not error_summary:
        return None
    cutoff = _utcnow() - timedelta(hours=48)
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    err_lower = error_summary.lower()
    err_tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", err_lower))

    best: Task | None = None
    best_score = 0
    for t in rows:
        payloads = t.payloads or []
        if not payloads or (payloads[0] or {}).get("type") != "investigate":
            continue
        po = t.plan_output if isinstance(t.plan_output, dict) else None
        if not po:
            continue
        summary = str(po.get("summary") or "").strip()
        root_cause = str(po.get("root_cause") or "").strip()
        if not summary or not root_cause:
            continue

        score = 0
        for entry in po.get("affected_files") or []:
            p = entry.get("path") if isinstance(entry, dict) else entry
            if isinstance(p, str) and p and p.lower() in err_lower:
                score += 5

        diag_tokens = set(
            re.findall(
                r"[a-zA-Z_][a-zA-Z0-9_]{3,}",
                (summary + " " + root_cause).lower(),
            )
        )
        score += len(err_tokens & diag_tokens)

        if score > best_score and score >= 3:
            best_score = score
            best = t
    return best


def _extract_migration_chain_signals(error_summary: str) -> str:
    """Extract concrete alembic chain state (revisions, heads, missing IDs) from error logs.

    Returns a multi-line bullet string for inclusion in remediation guidance,
    or an empty string when no signals are found.
    """
    if not error_summary:
        return ""
    text = error_summary
    lower = text.lower()
    bullets: list[str] = []

    # Schema version mismatch: "database at 'XXXX', code expects 'YYYY'"
    m = re.search(
        r"database at ['\"]([0-9a-f_]+)['\"][^'\"]*expects ['\"]([0-9a-f_]+)['\"]",
        text,
        re.IGNORECASE,
    )
    if m:
        bullets.append(
            f"- Schema mismatch: database revision `{m.group(1)}`, code EXPECTED_SCHEMA_REVISION `{m.group(2)}` — bump EXPECTED_SCHEMA_REVISION in backend/app/main.py or add the missing migration."
        )
    else:
        m2 = re.search(
            r"expected_schema_revision[^0-9a-f]*([0-9a-f_]{4,})[^0-9a-f]+([0-9a-f_]{4,})",
            lower,
        )
        if m2:
            bullets.append(
                f"- EXPECTED_SCHEMA_REVISION mismatch involving `{m2.group(1)}` and `{m2.group(2)}`."
            )

    # Multiple head revisions: alembic prints them like "Multiple head revisions are present for given argument 'head'; please specify a specific target revision, '<branchname>@head' to narrow to a specific head, or 'heads' for all heads"
    # Try to capture explicit revision IDs mentioned nearby.
    if "multiple head" in lower or "only a single head supported" in lower:
        ids = re.findall(r"\b([0-9a-f]{8,12}|\d{4})\b", text)
        unique_ids = list(dict.fromkeys(ids))[:6]
        if unique_ids:
            bullets.append(
                f"- Multiple alembic heads detected. Candidate revision IDs from logs: {', '.join('`' + i + '`' for i in unique_ids)}. Re-chain so exactly one head remains."
            )
        else:
            bullets.append(
                "- Multiple alembic heads detected — inspect `alembic heads` and re-chain so exactly one head remains."
            )

    # Can't locate revision: alembic message "Can't locate revision identified by 'XXXX'"
    for m in re.finditer(
        r"can'?t locate revision identified by ['\"]([0-9a-f_]+)['\"]",
        text,
        re.IGNORECASE,
    ):
        bullets.append(
            f"- Missing revision `{m.group(1)}` referenced as down_revision but no file defines it. Restore the file or fix the down_revision pointer."
        )

    # UndefinedColumn: extract column name
    for m in re.finditer(
        r"undefinedcolumn[^a-z]+(?:column )?['\"]?([a-zA-Z_][a-zA-Z0-9_.]*)['\"]?",
        text,
        re.IGNORECASE,
    ):
        bullets.append(
            f"- UndefinedColumn on `{m.group(1)}` — a migration adding this column is missing or out of order."
        )
        break

    return "\n".join(bullets)


def _build_remediation_prompt(
    *,
    head_branch: str,
    head_sha: str,
    failed_checks: list[str],
    error_summary: str,
    failure_type: str = "unknown",
    prior_investigation: Task | None = None,
    regression_of: Task | None = None,
    structured_signals: dict[str, list[str]] | None = None,
) -> str:
    """Build the agent prompt for a CI auto-remediation task."""
    checks_text = (
        "\n".join(f"  - {name}" for name in failed_checks)
        if failed_checks
        else "  (check names not available)"
    )
    # Truncate error summary for prompt to avoid excessive token usage
    summary_text = (
        error_summary[:2000] if error_summary else "(no error logs available)"
    )

    migration_chain_hint = (
        _extract_migration_chain_signals(error_summary)
        if failure_type == "migration"
        else ""
    )

    type_guidance_map = {
        "test": "Focus on test assertions, fixtures, and test setup — the production code may be fine but tests may need updating to match changed behavior.",
        "build": "Check imports, missing dependencies, syntax errors, and module resolution — this is likely a compilation or import failure.",
        "lint": "Check style rules, formatting, and linting configuration — run the project's linter locally to identify all violations.",
        "mixed": "Multiple failure categories detected — address build/import errors first, then test failures, then lint issues.",
        "migration": (
            "Migration / schema failure detected. Check the alembic version chain: "
            "the newest file in backend/alembic/versions/ must chain via down_revision "
            "from the previous head, and backend/app/main.py's EXPECTED_SCHEMA_REVISION "
            "must match that newest revision. Ensure there is exactly one head (no "
            "multiple heads), UndefinedColumn errors imply a missing or misordered "
            "migration, and re-run the backend invariant tests "
            "(bash scripts/test-backend.sh testing/test_invariants.py) to verify."
            + (
                f"\n\nExtracted chain state from logs:\n{migration_chain_hint}"
                if migration_chain_hint
                else ""
            )
        ),
        "unknown": "Diagnose the failure type from the error output before applying a fix.",
    }
    type_guidance = type_guidance_map.get(failure_type, type_guidance_map["unknown"])

    diagnosis_section = ""
    if prior_investigation is not None and isinstance(
        prior_investigation.plan_output, dict
    ):
        po = prior_investigation.plan_output
        root_cause = str(po.get("root_cause") or "").strip()
        recommended_fix = str(po.get("recommended_fix") or "").strip()
        if root_cause or recommended_fix:
            diagnosis_section = (
                f"\nDIAGNOSIS CONTEXT (from prior investigation {str(prior_investigation.id)[:8]}):\n"
                f"- Root cause: {root_cause[:800] or '(none recorded)'}\n"
                f"- Recommended fix: {recommended_fix[:800] or '(none recorded)'}\n"
                f"Use this diagnosis as a starting point, but verify it still applies.\n"
            )

    regression_section = ""
    if regression_of is not None:
        prev_ctx = regression_of.context or {}
        prev_pr = prev_ctx.get("merged_pr_number") or prev_ctx.get("pr_number")
        pr_text = f"PR #{prev_pr}" if prev_pr else f"task {str(regression_of.id)[:8]}"
        regression_section = (
            f"\n⚠️ REGRESSION DETECTED:\n"
            f"A prior auto-remediation ({pr_text}) for the same branch recently merged\n"
            f"with a matching error fingerprint, but CI is failing again with the same\n"
            f"signature. The previous fix did not resolve the underlying problem.\n"
            f"Review {pr_text} to see what was attempted, then apply a deeper fix that\n"
            f"addresses the root cause rather than repeating the prior surface-level change.\n"
        )

    signals_section = ""
    if structured_signals and any(
        structured_signals.get(k)
        for k in ("failing_tests", "error_locations", "error_messages")
    ):
        lines = [
            "\n## Structured Failure Signals\n"
            "(Extracted from full logs before truncation — use these as authoritative even if the raw log below is cut off.)\n"
        ]
        if structured_signals.get("failing_tests"):
            lines.append("Failing tests:")
            lines.extend(f"  - {t}" for t in structured_signals["failing_tests"])
        if structured_signals.get("error_locations"):
            lines.append("Error locations (file:line):")
            lines.extend(f"  - {loc}" for loc in structured_signals["error_locations"])
        if structured_signals.get("error_messages"):
            lines.append("Error messages:")
            lines.extend(f"  - {msg}" for msg in structured_signals["error_messages"])
        signals_section = "\n".join(lines) + "\n"

    return (
        f"CI is failing on the `{head_branch}` branch at commit {head_sha}.\n\n"
        f"{regression_section}"
        f"Failure classification: {failure_type} (test failures, build errors, lint issues, or mixed)\n\n"
        f"Failed checks:\n{checks_text}\n"
        f"{signals_section}\n"
        f"Error output (truncated):\n```\n{summary_text}\n```\n"
        f"{diagnosis_section}\n"
        f"IMPORTANT — CI environment differences:\n"
        f"CI runs backend tests against PostgreSQL, not SQLite. Patterns that behave\n"
        f"differently include: FOR UPDATE / skip_locked (no-op in SQLite, blocks in\n"
        f"Postgres), multi-session transactions (separate connections in Postgres,\n"
        f"shared connection in SQLite's StaticPool), and JSONB operators. If a test\n"
        f"times out or hangs in CI, suspect lock contention between sessions — a\n"
        f"second session scanning a table will block on rows locked by the first.\n"
        f"In asyncio (single-threaded), this is a self-deadlock.\n\n"
        f"Instructions:\n"
        f"1. Create a fix branch off `{head_branch}`.\n"
        f"2. Diagnose the root cause from the error context above.\n"
        f"   Guidance: {type_guidance}\n"
        f"3. Fix the issue — the fix should be minimal and targeted.\n"
        f"4. Run the project's test suite before committing to verify the fix.\n"
        f"5. Open a PR targeting `{head_branch}`.\n"
    )
