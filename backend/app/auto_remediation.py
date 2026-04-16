"""CI auto-remediation: detect protected-branch CI failures and spawn fix tasks.

When a check_run webhook arrives with conclusion=failure on a protected branch
(e.g. main/master), this module spawns a create-type task that analyzes the
failure and opens a fix PR.  Guard rails prevent runaway spawning.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.github import get_check_runs, get_repo_installation_token
from app.models import Project, Task, TaskStatus
from app.notification_dispatch import post_auto_fix_spawned_message
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _handle_main_branch_ci_failure(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    head_branch: str,
    project_id: uuid.UUID,
    config: dict[str, Any],
) -> None:
    """Background handler: spawn a fix task for a protected-branch CI failure.

    Opens its own DB session (runs outside the request lifecycle).
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
            )
    except Exception as exc:
        logger.error(
            "[AUTO-REMEDIATION] Error handling CI failure on %s/%s@%s: %s",
            owner, repo, head_sha[:8], exc, exc_info=True,
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
) -> None:
    """Inner implementation, called with a live session."""
    max_per_hour = config.get("max_per_hour", 2)
    max_concurrent = config.get("max_concurrent", 2)
    now = _utcnow()

    # ── Guard: branch suspension (escalation after repeated failures) ──
    suspended_branches = config.get("suspended_branches", [])
    if head_branch in suspended_branches:
        logger.info(
            "[AUTO-REMEDIATION] Branch %s is suspended after repeated failures — skipping. "
            "Clear via PATCH /auto-remediation to re-enable.",
            head_branch,
        )
        return

    # ── Guard: rate limit ──
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    recent_auto_count = sum(
        1 for t in recent_tasks
        if (t.context or {}).get("auto_remediation") is True
    )
    if recent_auto_count >= max_per_hour:
        logger.info(
            "[AUTO-REMEDIATION] Rate limit hit (%d/%d in last hour) for project %s",
            recent_auto_count, max_per_hour, project_id,
        )
        return

    # ── Guard: duplicate detection (same branch, not SHA) ──
    # A fix task for the same branch is already working on the problem.
    # It clones latest, so it covers any new pushes to that branch.
    active_statuses = [
        TaskStatus.QUEUED, TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
    ]
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
        )
    )).scalars().all()
    for t in active_tasks:
        ctx = t.context or {}
        if ctx.get("auto_remediation") is True and ctx.get("ci_failure_branch") == head_branch:
            logger.info(
                "[AUTO-REMEDIATION] Duplicate: active task %s already covers branch %s",
                t.id, head_branch,
            )
            return

    # ── Guard: concurrent limit ──
    active_auto_count = sum(
        1 for t in active_tasks
        if (t.context or {}).get("auto_remediation") is True
    )
    if active_auto_count >= max_concurrent:
        logger.info(
            "[AUTO-REMEDIATION] Concurrent limit hit (%d/%d) for project %s",
            active_auto_count, max_concurrent, project_id,
        )
        return

    # ── Guard: check if CI already passed on latest commit ──
    # Between the webhook firing and this handler running, another push may
    # have fixed the issue. Verify the branch tip is still failing.
    try:
        from app.github import get_branch_sha
        latest_sha = await get_branch_sha(
            await get_repo_installation_token(owner, repo),
            owner, repo, head_branch,
        )
        if latest_sha != head_sha:
            latest_ci = await get_check_runs(
                await get_repo_installation_token(owner, repo),
                owner, repo, latest_sha,
            )
            if latest_ci.get("status") == "completed" and latest_ci.get("conclusion") == "success":
                logger.info(
                    "[AUTO-REMEDIATION] CI already passing on latest %s@%s — skipping",
                    head_branch, latest_sha[:8],
                )
                return
            # Update to latest SHA so the fix task prompt shows the current failure
            head_sha = latest_sha
    except Exception as exc:
        logger.warning("[AUTO-REMEDIATION] Could not verify latest CI status: %s", exc)
        # Proceed anyway — better to spawn a potentially unnecessary task than miss a real failure

    # ── CI context extraction ──
    failed_checks, error_summary, failure_type = await _extract_ci_context(
        owner, repo, head_sha, project_id, session,
    )

    # ── Guard: skip if no useful CI context was extracted ──
    if not failed_checks and not error_summary and failure_type == "unknown":
        logger.warning(
            "[AUTO-REMEDIATION] Skipping task creation for %s@%s: "
            "CI context extraction returned no useful information (no failed checks, no error summary)",
            head_branch, head_sha[:8],
        )
        try:
            from app.main import _send_webhook_notification
            import asyncio
            asyncio.ensure_future(_send_webhook_notification(
                "AUTO_FIX_SKIPPED",
                f"CI auto-remediation skipped on {head_branch}: insufficient context",
                f"SHA: {head_sha[:8]} — no failed checks, no error logs extracted",
            ))
        except Exception as exc:
            logger.warning("[AUTO-REMEDIATION] Failed to send skip webhook notification: %s", exc)
        return

    # ── Create the fix task ──
    prompt = _build_remediation_prompt(
        head_branch=head_branch,
        head_sha=head_sha,
        failed_checks=failed_checks,
        error_summary=error_summary,
        failure_type=failure_type,
    )

    task = Task(
        project_id=project_id,
        title=f"Auto-fix: CI failure on {head_branch}",
        payloads=[{
            "type": "create",
            "effort": "medium",
            "base_branch": head_branch,
            "title": f"fix: auto-remediate CI failure on {head_branch}",
            "prompt": prompt,
        }],
        context={
            "auto_remediation": True,
            "ci_failure_sha": head_sha,
            "ci_failure_branch": head_branch,
            "ci_failure_checks": failed_checks,
            "ci_failure_type": failure_type,
        },
        status=TaskStatus.QUEUED,
        created_by="SYSTEM_AUTO_REMEDIATION",
    )
    session.add(task)
    await session.flush()  # assign task.id

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
        from app.main import _send_webhook_notification
        import asyncio
        asyncio.ensure_future(_send_webhook_notification(
            "AUTO_FIX_SPAWNED",
            f"CI failure auto-remediation on {head_branch}",
            f"SHA: {head_sha[:8]}, failed checks: {', '.join(failed_checks[:5])}",
        ))
    except Exception as exc:
        logger.warning("[AUTO-REMEDIATION] Failed to send webhook notification: %s", exc)

    await session.commit()
    logger.info(
        "[AUTO-REMEDIATION] Spawned task %s for CI failure on %s@%s (%s)",
        task.id, head_branch, head_sha[:8], failure_type,
    )


async def _extract_ci_context(
    owner: str,
    repo: str,
    head_sha: str,
    project_id: uuid.UUID,
    session: AsyncSession,
) -> tuple[list[str], str, str]:
    """Fetch CI details and classify the failure.

    Returns (failed_checks, error_summary, failure_type).
    """
    failed_checks: list[str] = []
    error_summary = ""
    failure_type = "unknown"

    try:
        token = await get_repo_installation_token(owner, repo)
        if not token:
            logger.warning("[AUTO-REMEDIATION] No GitHub token available for %s/%s", owner, repo)
            return failed_checks, error_summary, failure_type

        ci_result = await get_check_runs(token, owner, repo, head_sha, project_id=str(project_id))
        details = ci_result.get("details", [])
        failed_checks = [
            d["name"] for d in details
            if d.get("conclusion") == "failure"
        ]
        raw_logs = ci_result.get("failed_logs", "")
        error_summary = raw_logs[:3000]

        # Heuristic failure type classification — check specific patterns
        # before generic ones to avoid "Build failed" matching as "test"
        if raw_logs:
            logs_lower = raw_logs.lower()
            if "importerror" in logs_lower or "syntaxerror" in logs_lower:
                failure_type = "build"
            elif "assertionerror" in logs_lower or "assert " in logs_lower or "pytest" in logs_lower:
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

    return failed_checks, error_summary, failure_type


def _build_remediation_prompt(
    *,
    head_branch: str,
    head_sha: str,
    failed_checks: list[str],
    error_summary: str,
    failure_type: str = "unknown",
) -> str:
    """Build the agent prompt for a CI auto-remediation task."""
    checks_text = "\n".join(f"  - {name}" for name in failed_checks) if failed_checks else "  (check names not available)"
    # Truncate error summary for prompt to avoid excessive token usage
    summary_text = error_summary[:2000] if error_summary else "(no error logs available)"

    type_guidance_map = {
        "test": "Focus on test assertions, fixtures, and test setup — the production code may be fine but tests may need updating to match changed behavior.",
        "build": "Check imports, missing dependencies, syntax errors, and module resolution — this is likely a compilation or import failure.",
        "lint": "Check style rules, formatting, and linting configuration — run the project's linter locally to identify all violations.",
        "mixed": "Multiple failure categories detected — address build/import errors first, then test failures, then lint issues.",
        "unknown": "Diagnose the failure type from the error output before applying a fix.",
    }
    type_guidance = type_guidance_map.get(failure_type, type_guidance_map["unknown"])

    return (
        f"CI is failing on the `{head_branch}` branch at commit {head_sha}.\n\n"
        f"Failure classification: {failure_type} (test failures, build errors, lint issues, or mixed)\n\n"
        f"Failed checks:\n{checks_text}\n\n"
        f"Error output (truncated):\n```\n{summary_text}\n```\n\n"
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
