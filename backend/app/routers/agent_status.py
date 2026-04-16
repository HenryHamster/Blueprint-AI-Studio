"""Agent task status update endpoint.

Extracted from app.main to reduce main.py size.  Contains the POST /api/agent/status
endpoint (update_agent_status) and its supporting helpers: record_task_result and
_recorded_task_ids.

All deferred imports from app.main are intentional — they avoid circular imports
(agent_status is imported BY main) and preserve test-patchability.
"""
import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from app.database import get_session
from app.models import (
    Task, Artifact, TaskHistory, Alert, AlertSeverity,
    TaskStatus, ArtifactType, Project,
)
from app.schemas import AgentStatusPayload
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
import app.orchestrator as _orch
from app.broadcast import broadcast_to_project
from app.routers.agent import verify_agent_token
from app.max_plan_pool import (
    _max_plan_pool_lock,
    _account_by_id,
    release_max_plan_account,
    _account_available_for_model,
    _max_plan_pools,
    _extract_task_model,
    clear_rate_limit_state_on_success,
    disable_account_model,
    record_completion_event,
)
from app.task_lifecycle import _process_post_commit
from app.rollup_management import _check_and_trigger_rollup
from app.payload_schemas import validate_internal_payload as _validate_internal_payload
from app.task_completion import _compute_first_attempt_succeeded
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

router = APIRouter()


_recorded_task_ids: set = set()  # Track which task IDs have already recorded results


def record_task_result(success: bool, failure_class: str = "", task_id: str = "", task_type: str = ""):
    """Record a task result for deployment monitoring. Deduplicates by task_id."""
    from app.shared_state import deploy_monitoring
    # Deduplicate: only record one result per task
    if task_id:
        if task_id in _recorded_task_ids:
            return
        _recorded_task_ids.add(task_id)
        # Keep the set bounded
        if len(_recorded_task_ids) > 200:
            _recorded_task_ids.clear()

    entry = (_utcnow().isoformat(), "success" if success else "failure", failure_class, task_type)
    deploy_monitoring["task_results"].append(entry)  # deque(maxlen=100) handles cap automatically

    if success:
        _shared_state.deploy_monitoring["last_completion_time"] = _utcnow()


async def _check_daily_cost_ceiling(task: Task, session: AsyncSession) -> None:
    """Create a WARNING alert if the daily VM cost total exceeds the configured ceiling.

    Runs at most once per task completion. The alert is deduplicated — only one
    undismissed 'Daily cost ceiling exceeded' alert per project is created.
    """
    ceiling = float(_runtime_config.get("daily_cost_ceiling_usd", 0))
    if ceiling <= 0:
        return

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    # Exclude current task from DB query to avoid double-counting (its vm_stopped_at
    # was just set in-memory and may be flushed before the query runs).
    rows = (await session.execute(
        select(Task.vm_provisioned_at, Task.vm_stopped_at, Task.vm_spot).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.vm_stopped_at >= today_start,
            Task.vm_provisioned_at.is_not(None),
        )
    )).all()

    daily_total = 0.0
    for prov_at, stop_at, is_spot in rows:
        if prov_at and stop_at:
            secs = (stop_at - prov_at).total_seconds()
            rate = SPOT_RATE if is_spot else ON_DEMAND_RATE
            daily_total += secs * rate

    # Always include the current task's cost explicitly (it is in-memory, not yet committed).
    if task.vm_provisioned_at and task.vm_stopped_at and task.vm_stopped_at >= today_start:
        secs = (task.vm_stopped_at - task.vm_provisioned_at).total_seconds()
        daily_total += secs * (SPOT_RATE if task.vm_spot else ON_DEMAND_RATE)

    if daily_total <= ceiling:
        return

    alert_title = "Daily cost ceiling exceeded"
    existing = (await session.execute(
        select(Alert).where(
            Alert.project_id == task.project_id,
            Alert.task_id.is_(None),
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == alert_title,
        )
    )).scalars().first()
    if existing is not None:
        return

    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=alert_title,
        message=f"Daily VM cost ${daily_total:.2f} exceeds ceiling ${ceiling:.2f}.",
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert_title},
    )
    logger.warning(
        f"[COST] Daily cost ceiling exceeded for project {task.project_id}: "
        f"${daily_total:.2f} > ${ceiling:.2f}"
    )
    try:
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
        asyncio.ensure_future(dispatch_external_notification(
            task.project_id,
            "BUDGET_EXCEEDED",
            {
                "task_id": str(task.id),
                "daily_total_usd": round(daily_total, 2),
                "ceiling_usd": round(ceiling, 2),
            },
        ))
    except Exception as _ne:
        logger.warning(f"[NOTIFY] BUDGET_EXCEEDED dispatch setup failed: {_ne}")


_STOPWORDS = frozenset([
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "not", "no", "nor",
    "so", "yet", "both", "either", "neither", "each", "few", "more", "most",
    "other", "some", "such", "than", "too", "very", "just", "task", "error",
    "failed", "failure", "retries", "exhausted", "attempt", "status",
    "message", "timeout", "unknown", "none", "null", "true", "false",
    "code", "line", "file", "test", "run", "running", "agent", "vm",
])

_AUTO_PLAYBOOK_CAP = 5


async def _check_recurring_failures(task: Task, session: AsyncSession) -> None:
    """Check for recurring failure patterns and auto-create playbook entries.

    Queries the last 30 FAILED tasks for this project within the past 7 days,
    groups by failure_class, and creates a playbook entry when 3+ tasks share
    common keywords in their status messages. Capped at 5 auto-generated entries
    per project.
    """
    import hashlib as _hashlib  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    from app.failure_classification import classify_failure  # noqa: PLC0415

    cutoff = _utcnow() - timedelta(days=7)

    recent_failed = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,  # exclude current task (session-commit-exclusion-pattern)
            Task.status == TaskStatus.FAILED,
            Task.created_at >= cutoff,
        ).order_by(Task.created_at.desc()).limit(30)
    )).scalars().all()

    # Add the current task's contribution manually (it was excluded above)
    all_failed = list(recent_failed) + [task]

    # Group by failure_class
    by_class: dict[str, list] = {}
    for t in all_failed:
        fc = classify_failure(t)
        by_class.setdefault(fc, []).append(t)

    for failure_class, tasks_in_class in by_class.items():
        if len(tasks_in_class) < 3:
            continue

        # Tokenize status messages and find keywords appearing in 3+ tasks
        task_keyword_sets: list[set[str]] = []
        for t in tasks_in_class:
            msg = (t.latest_status_message or "").lower()
            tokens = set()
            for word in msg.split():
                word = word.strip(".,;:!?\"'()[]{}|/\\-")
                if len(word) >= 4 and word not in _STOPWORDS and word.isalpha():
                    tokens.add(word)
            task_keyword_sets.append(tokens)

        keyword_counts = Counter(kw for kw_set in task_keyword_sets for kw in kw_set)
        common_keywords = sorted(
            kw for kw, count in keyword_counts.items() if count >= 3
        )[:10]

        if len(common_keywords) < 2:
            continue

        # Check if an existing auto-entry already covers these keywords
        project = await session.get(Project, task.project_id)
        if not project:
            continue

        doc = dict(project.design_doc or {})
        existing_auto_keys = [k for k in doc if k.startswith("playbook/auto-")]

        # Check duplicate: look for any existing entry whose keywords overlap significantly
        already_covered = False
        keyword_set = set(common_keywords)
        for existing_key in existing_auto_keys:
            entry = doc.get(existing_key, {})
            entry_content = entry.get("content", "").lower()
            overlap = sum(1 for kw in keyword_set if kw in entry_content)
            if overlap >= max(2, len(keyword_set) // 2):
                # Update the count in the existing entry
                updated_entry = dict(entry)
                updated_entry["content"] = (
                    f"WARNING: {failure_class} failures have occurred {len(tasks_in_class)} times recently. "
                    f"Common pattern: {', '.join(common_keywords)}. "
                    f"Recent examples: {_truncate_examples(tasks_in_class[-2:])}. "
                    f"Agents should check for this issue before starting work."
                )
                updated_entry["updated_at"] = _utcnow().isoformat()
                updated_entry["applicable_types"] = _get_applicable_types(tasks_in_class)
                doc[existing_key] = updated_entry
                project.design_doc = doc
                flag_modified(project, "design_doc")
                session.add(project)
                logger.info(f"[PLAYBOOK] Updated existing auto-entry {existing_key} for {failure_class} pattern")
                already_covered = True
                break

        if already_covered:
            continue

        # Cap at 5 auto-generated entries
        if len(existing_auto_keys) >= _AUTO_PLAYBOOK_CAP:
            logger.info(f"[PLAYBOOK] Auto-playbook cap ({_AUTO_PLAYBOOK_CAP}) reached for project {task.project_id}, skipping")
            continue

        # Create a new auto-entry
        short_hash = _hashlib.md5(",".join(sorted(common_keywords)).encode()).hexdigest()[:6]
        entry_key = f"playbook/auto-{failure_class.lower()}-{short_hash}"

        applicable_types = _get_applicable_types(tasks_in_class)
        content = (
            f"WARNING: {failure_class} failures have occurred {len(tasks_in_class)} times recently. "
            f"Common pattern: {', '.join(common_keywords)}. "
            f"Recent examples: {_truncate_examples(tasks_in_class[-2:])}. "
            f"Agents should check for this issue before starting work."
        )

        doc[entry_key] = {
            "content": content,
            "source_task_id": None,
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "applicable_types": applicable_types,
        }
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        logger.info(
            f"[PLAYBOOK] Auto-created entry {entry_key} for {failure_class} pattern "
            f"({len(tasks_in_class)} occurrences, keywords: {common_keywords})"
        )


def _truncate_examples(tasks: list) -> str:
    """Build a short string of recent failure messages for playbook content."""
    parts = []
    for t in tasks:
        msg = (t.latest_status_message or "")[:150].strip()
        if msg:
            parts.append(f'"{msg}"')
    return "; ".join(parts) if parts else "(no message)"


def _get_applicable_types(tasks: list) -> list[str]:
    """Extract unique task types from a list of tasks."""
    types = set()
    for t in tasks:
        fp = t.first_payload
        if fp:
            task_type = fp.get("type", "")
            if task_type:
                types.add(task_type)
    return sorted(types) if types else []


@router.post("/api/agent/status")
@limiter.limit("120/minute")
async def update_agent_status(
    request: Request,
    payload: AgentStatusPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to report progress, save git artifacts, and update task status.
    """
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # 1. Fetch task — validate vm_instance_id to prevent stale agents from
    # overwriting status after a task was requeued with a new VM.
    # FOR UPDATE prevents race with reaper: without it, a stale agent's
    # COMPLETED can overwrite the reaper's QUEUED (requeue) between read and write.
    stmt = select(Task).where(
        Task.id == payload.task_id,
        Task.vm_instance_id == token.get("vm_instance_id")
    ).with_for_update()
    result = await session.execute(stmt)
    task = result.scalars().first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this VM")

    # Idempotency guard: if the task is already in a terminal state, ignore duplicate
    # callbacks (e.g. agent HTTP retries).  Still return 200 so the agent doesn't retry.
    # Exception: if _post_commit_pending is set, the early-commit succeeded but
    # _process_post_commit raised before spawning review/fix tasks. Let the retry
    # re-run post-commit processing so the PR doesn't get orphaned.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        pending_info = (task.context or {}).get("_post_commit_pending")
        if pending_info and isinstance(pending_info, dict):
            logger.info(f"[PIPELINE] Retrying post-commit for terminal task {task.id} (_post_commit_pending set)")
            _bind = session.bind
            _factory = async_sessionmaker(_bind, class_=AsyncSession, expire_on_commit=False)
            try:
                result = await _process_post_commit(task.id, payload, _session_factory=_factory)
                await session.refresh(task)
                ctx = dict(task.context or {})
                ctx.pop("_post_commit_pending", None)
                task.context = ctx
                session.add(task)
                await session.commit()
                return result
            except Exception as e:
                logger.error(f"[PIPELINE] Post-commit retry also failed for {task.id}: {e}", exc_info=True)
                raise
        return {"status": "already_terminal", "task_id": str(task.id)}
    if task.status == TaskStatus.EVALUATING:
        return {"status": "already_evaluating", "task_id": str(task.id)}

    # 2. Update task status
    task.status = payload.status
    # Preserve logs as status message when:
    # - no artifact is created (e.g., "no changes" completion), OR
    # - task is failing (artifact-bearing failures must not silently lose diagnostic text)
    if payload.logs and (not payload.artifact_type or payload.status == TaskStatus.FAILED):
        task.latest_status_message = payload.logs[:2000]
    if payload.failure_category:
        from app.failure_classification import normalize_agent_failure_category  # noqa: PLC0415
        ctx = dict(task.context or {})
        ctx['failure_category'] = normalize_agent_failure_category(payload.failure_category, task)
        task.context = ctx
    if payload.file_change_details:
        ctx = dict(task.context or {})
        ctx['_file_change_details'] = payload.file_change_details[:50]
        task.context = ctx
        flag_modified(task, 'context')
    session.add(task)

    # 3. Create Artifact if provided (with duplicate prevention for PR artifacts)
    if payload.artifact_type:
        if payload.artifact_type == ArtifactType.PULL_REQUEST:
            existing = (await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST
                )
            )).scalars().first()
            if existing:
                logger.info(f"[STATUS] Task {task.id} already has PR artifact, skipping duplicate")
            else:
                artifact = Artifact(
                    task_id=task.id,
                    artifact_type=payload.artifact_type,
                    commit_hash=payload.commit_hash,
                    repo_url=payload.repo_url,
                    pr_url=payload.pr_url,
                    storage_uri=payload.storage_uri,
                    logs=payload.logs
                )
                session.add(artifact)
                # Store changed files and enriched retry info in task context
                if payload.changed_files or payload.diff_summary or payload.self_review_outcome:
                    ctx = dict(task.context or {})
                    if payload.changed_files:
                        ctx['_pr_changed_files'] = payload.changed_files[:50]
                    ctx['_pr_url'] = payload.pr_url or ''
                    if payload.diff_summary:
                        ctx['_pr_diff_summary'] = payload.diff_summary[:50]
                    if payload.self_review_outcome:
                        ctx['_self_review_outcome'] = payload.self_review_outcome
                    if payload.test_failure_summary:
                        ctx['_test_failure_summary'] = payload.test_failure_summary[:5000]
                    task.context = ctx
                    flag_modified(task, 'context')
        else:
            artifact = Artifact(
                task_id=task.id,
                artifact_type=payload.artifact_type,
                commit_hash=payload.commit_hash,
                repo_url=payload.repo_url,
                pr_url=payload.pr_url,
                storage_uri=payload.storage_uri,
                logs=payload.logs
            )
            session.add(artifact)
            # Persist enriched retry info for non-PR failures (e.g. test/lint failures)
            if payload.changed_files or payload.diff_summary or payload.test_failure_summary:
                ctx = dict(task.context or {})
                if payload.changed_files:
                    ctx['_pr_changed_files'] = payload.changed_files[:50]
                if payload.diff_summary:
                    ctx['_pr_diff_summary'] = payload.diff_summary[:50]
                if payload.test_failure_summary:
                    ctx['_test_failure_summary'] = payload.test_failure_summary[:5000]
                task.context = ctx
                flag_modified(task, 'context')

        # Broadcast artifact creation (e.g., new logs or screenshot available)
        await broadcast_to_project(
            str(task.project_id),
            "artifact_created",
            {
                "task_id": str(task.id),
                "artifact_type": payload.artifact_type,
                "logs": payload.logs,
                "storage_uri": payload.storage_uri
            }
        )

    # 3b. Parse and store smoketest result in task.plan_output + document in history
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            task.plan_output = smoketest_data
            session.add(task)
            # Document every smoketest result (pass or fail) in task history
            all_tests = smoketest_data.get("tests", [])
            pass_count = len([t for t in all_tests if t.get("status") == "pass"])
            fail_count = len([t for t in all_tests if t.get("status") == "fail"])
            session.add(TaskHistory(
                task_id=task.id, action="SMOKETEST_RESULT",
                details={
                    "passed": smoketest_data.get("passed", False),
                    "total_tests": len(all_tests),
                    "passed_count": pass_count,
                    "failed_count": fail_count,
                    "suite_results": smoketest_data.get("suites", {}),
                    "guardrail_violations": smoketest_data.get("guardrail_violations", []),
                }
            ))
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[SMOKETEST] Failed to parse smoketest result for task {task.id}: {e}", exc_info=True)

    # 3b-ii. On smoketest failure, spawn a plan task and create a persistent alert
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.status == TaskStatus.FAILED and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            if not smoketest_data.get("passed"):
                failed_tests = [t for t in smoketest_data.get("tests", []) if t.get("status") == "fail"]
                if failed_tests:
                    # Duplicate prevention: skip if a fix task already exists for this smoketest
                    existing_fix = (await session.execute(
                        select(Task).where(Task.parent_task_id == task.id).limit(1)
                    )).scalars().first()
                    if existing_fix:
                        logger.info(f"[SMOKETEST] Fix task {existing_fix.id} already exists for smoketest {task.id}, skipping duplicate")
                    else:
                        base_branch = (task.first_payload).get("base_branch", "main")
                        failure_summary = "\n".join(
                            f"- {t['name']}: {t.get('message', 'no details')}" for t in failed_tests
                        )

                        # Determine priority based on which suite failed (higher = dispatched first)
                        suite_results = smoketest_data.get("suites", {})
                        if suite_results.get("startup", {}).get("passed") is False:
                            fix_priority = 90  # Startup failure — critical
                        elif suite_results.get("curated", {}).get("passed") is False:
                            fix_priority = 70  # Curated test failure — high
                        else:
                            fix_priority = 50  # Regression-only failure — normal

                        type_model = _runtime_config.get("task_type_models", {}).get("plan") or None
                        type_effort = _runtime_config.get("task_type_efforts", {}).get("plan") or None
                        plan_task = Task(
                            project_id=task.project_id,
                            title=f"Fix smoketest failures on {base_branch}",
                            description=f"{len(failed_tests)} smoke test(s) failed after merge to {base_branch}",
                            parent_task_id=task.id,
                            priority=fix_priority,
                            payloads=[_validate_internal_payload({
                                "type": "plan",
                                "prompt": (
                                    f"Smoke tests failed after a merge to {base_branch}. "
                                    f"Investigate and plan fixes for these failures:\n\n"
                                    f"{failure_summary}\n\n"
                                    f"IMPORTANT GUARDRAILS:\n"
                                    f"- Do NOT delete or skip tests just to make them pass\n"
                                    f"- First diagnose: is the failure in the CODE or the TEST?\n"
                                    f"- If the test is wrong, fix it to correctly test the intended behavior\n"
                                    f"- If the code is wrong, fix the code\n"
                                    f"- Tests may only be removed if the tested functionality was removed — explain why\n"
                                    f"- Do NOT weaken assertions without justification\n"
                                    f"Diagnose root causes, identify which recent changes caused each failure, "
                                    f"and produce subtasks to fix them."
                                ),
                                "base_branch": base_branch,
                                **({"model": type_model} if type_model else {}),
                                **({"effort": type_effort} if type_effort else {}),
                            }, "main:smoketest_fix_plan")],
                            context={
                                "smoketest_task_id": str(task.id),
                                "failed_tests": [t["name"] for t in failed_tests],
                                "suite_results": suite_results,
                                "guardrail_violations": smoketest_data.get("guardrail_violations", []),
                                "total_tests": len(smoketest_data.get("tests", [])),
                                "passed_count": len([t for t in smoketest_data.get("tests", []) if t.get("status") == "pass"]),
                                "failed_count": len(failed_tests),
                            },
                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                            status=TaskStatus.QUEUED,
                            created_by="ORCHESTRATOR",
                        )
                        session.add(plan_task)
                        await session.flush()
                        history = TaskHistory(
                            task_id=plan_task.id, action="SPAWNED",
                            details={"spawned_by": "SMOKETEST_FAILURE", "priority": fix_priority, "failed_tests": [t["name"] for t in failed_tests]}
                        )
                        session.add(history)

                        # Build suite breakdown for alert message
                        suite_detail = ""
                        for suite_name, suite_data in suite_results.items():
                            if isinstance(suite_data, dict) and suite_data.get("passed") is False:
                                suite_fails = [t for t in suite_data.get("tests", []) if t.get("status") == "fail"]
                                if suite_fails:
                                    suite_detail += f" [{suite_name}: {len(suite_fails)} failed]"

                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.ERROR,
                            title=f"Smoke tests failed on {base_branch}",
                            message=f"{len(failed_tests)} test(s) failed: {', '.join(t['name'] for t in failed_tests[:5])}"
                                    + (f" (+{len(failed_tests) - 5} more)" if len(failed_tests) > 5 else "")
                                    + (suite_detail if suite_detail else ""),
                            task_id=task.id,
                        )
                        session.add(alert)

                        await broadcast_to_project(
                            str(task.project_id), "task_updated",
                            {"task_id": str(plan_task.id), "status": TaskStatus.QUEUED}
                        )
                        await broadcast_to_project(
                            str(task.project_id), "alert_created",
                            {"alert_id": str(alert.id), "severity": alert.severity, "title": alert.title}
                        )
                        logger.error(f"[SMOKETEST] Spawned plan task {plan_task.id} (priority={fix_priority}) + alert for {len(failed_tests)} failed smoke tests")
        except Exception as e:
            logger.warning(f"[SMOKETEST] Failed to spawn plan task after smoketest failure: {e}", exc_info=True)

    # 3c. Parse debug result, store in plan_output, and optionally spawn a fix task
    if payload.artifact_type == ArtifactType.DEBUG_RESULT and payload.logs:
        try:
            debug_data = json.loads(payload.logs)
            task.plan_output = debug_data
            session.add(task)

            # If confidence is high, auto-spawn a create task using the fix_prompt
            if debug_data.get("confidence") == "high" and debug_data.get("fix_prompt"):
                failed_task_id = (task.context or {}).get("failed_task_id", "")
                base_branch = (task.first_payload).get("base_branch", "main")
                debug_title = task.title or ""
                fix_title = debug_title.removeprefix("Debug: ") if debug_title.startswith("Debug: ") else debug_title
                fix_task = Task(
                    project_id=task.project_id,
                    title=f"Fix: {fix_title}",
                    description=debug_data["fix_prompt"],
                    parent_task_id=task.parent_task_id or task.id,
                    payloads=[_validate_internal_payload({
                        "type": "create",
                        "prompt": debug_data["fix_prompt"],
                        "base_branch": base_branch,
                    }, "main:debug_auto_fix")],
                    context={
                        "debug_task_id": str(task.id),
                        "failed_task_id": failed_task_id,
                        "root_cause": debug_data.get("root_cause", ""),
                    },
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
                session.add(fix_task)
                await session.flush()
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {"task_id": str(fix_task.id), "status": TaskStatus.QUEUED, "title": fix_task.title}
                )
                logger.info(f"[DEBUG] Spawned fix task {fix_task.id} from debug analysis (high confidence)")
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[DEBUG] Failed to parse debug result for task {task.id}: {e}", exc_info=True)

    # 4. External VM Termination
    # If the task is finished or failed, immediately kill the VM to save costs
    from app.failure_classification import classify_failure  # noqa: PLC0415
    from app.main import _ensure_vm_deleted  # noqa: PLC0415 — deferred to avoid circular import
    from app.main import _maybe_create_circuit_breaker_alert  # noqa: PLC0415 — deferred to avoid circular import
    if payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
        # Record result for deployment monitoring and update Prometheus counters
        # Wrapped in try-except so slot release (below) always runs even if
        # classify_failure() or record_task_result() throw.
        try:
            _direction_id = str((task.context or {}).get("direction_id") or "")
            _project_id = str(task.project_id) if task.project_id else ""
            if payload.status == TaskStatus.COMPLETED:
                record_task_result(success=True, task_id=str(task.id), task_type=task.first_payload.get("type", ""))
                _shared_state.tasks_completed_total += 1
                if _direction_id:
                    _shared_state.record_direction_result(
                        _direction_id, success=True, project_id=_project_id
                    )
                # Probe success: clear cascade-scoped disabled_models entries.
                # No-op if the account wasn't probing anything for this model.
                acct_id_clear = (task.context or {}).get("max_plan_account_id")
                model_clear = _extract_task_model(task)
                if acct_id_clear and model_clear:
                    try:
                        await clear_rate_limit_state_on_success(acct_id_clear, model_clear)
                    except Exception as e:
                        logger.warning(f"[STATUS] clear_rate_limit_state_on_success failed for {acct_id_clear}: {e}", exc_info=True)
                if acct_id_clear and task.vm_provisioned_at:
                    try:
                        duration_secs = (_utcnow() - task.vm_provisioned_at).total_seconds()
                        await record_completion_event(acct_id_clear, duration_secs)
                    except Exception as e:
                        logger.warning(f"[STATUS] record_completion_event failed for {acct_id_clear}: {e}", exc_info=True)
            else:
                failure_class = classify_failure(task, payload.logs)
                from app.failure_classification import persist_failure_category  # noqa: PLC0415
                persist_failure_category(task, failure_class)
                record_task_result(success=False, failure_class=failure_class, task_id=str(task.id), task_type=task.first_payload.get("type", ""))
                _shared_state.tasks_failed_total += 1
                if _direction_id:
                    _shared_state.record_direction_result(
                        _direction_id, success=False, failure_class=failure_class, project_id=_project_id
                    )

                # Rate-limited or transient API failures: requeue instead of permanently failing
                if failure_class in ("RATE_LIMIT", "EXTERNAL_API"):
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    model = _extract_task_model(task)
                    if acct_id:
                        await release_max_plan_account(acct_id)
                        # Mark slot as released so the fallback release (line ~6232) doesn't double-release
                        task.context = {**(task.context or {}), "_slot_released": True}
                        if failure_class == "RATE_LIMIT" and model:
                            # Only disable a specific model when we know which one hit the limit.
                            # cascade_disabled_models() inside the helper handles tier cascading
                            # (Opus 429 → sonnet + haiku disabled too).
                            await disable_account_model(acct_id, model)
                        # EXTERNAL_API: don't disable account (transient outage)

                    # Track API failure retries to prevent infinite loops
                    ctx = dict(task.context or {})
                    # Blacklist the account that just hit rate limit so retry picks a different one
                    acct_id_for_blacklist = (task.context or {}).get("max_plan_account_id")
                    if acct_id_for_blacklist and failure_class == "RATE_LIMIT":
                        blacklisted = list(ctx.get("blacklisted_accounts") or [])
                        if acct_id_for_blacklist not in blacklisted:
                            blacklisted.append(acct_id_for_blacklist)
                        ctx["blacklisted_accounts"] = blacklisted
                    should_requeue = True
                    if failure_class == "EXTERNAL_API":
                        # Permanent auth failures (bad/expired credentials) should never be
                        # retried — they will keep failing until the operator re-authenticates.
                        # Detect these by inspecting the failure message before burning retries.
                        _logs_lower = (payload.logs or "").lower()
                        _PERMANENT_AUTH_INDICATORS = (
                            "token expired", "token revoked", "bad credentials",
                            "unauthorized", "401",
                        )
                        if any(ind in _logs_lower for ind in _PERMANENT_AUTH_INDICATORS):
                            logger.warning(
                                f"[AGENT STATUS] Task {task.id} EXTERNAL_API permanent auth failure "
                                f"— not requeuing (operator must re-authenticate)"
                            )
                            task.latest_status_message = (
                                "External API auth failure — token expired or credentials invalid. "
                                "Re-authenticate and retry."
                            )
                            # Exhaust the general retry budget so the fallback retry path
                            # (attempt_count < max_retries) also skips this task.
                            task.attempt_count = _runtime_config["max_retries"]
                            should_requeue = False
                        else:
                            # Transient infrastructure outage: retry with exponential backoff.
                            # Budget is 5 retries (up from 3) to survive longer outages.
                            api_retries = ctx.get("_api_failure_retries", 0) + 1
                            ctx["_api_failure_retries"] = api_retries
                            if api_retries > 5:
                                logger.warning(f"[AGENT STATUS] Task {task.id} hit {api_retries} EXTERNAL_API failures — not requeuing")
                                should_requeue = False
                            else:
                                delay = min(2 ** api_retries * 120, 1800)
                                ctx["retry_after"] = (_utcnow() + timedelta(seconds=delay)).isoformat()
                                logger.info(f"[AGENT STATUS] Task {task.id} EXTERNAL_API retry {api_retries} — backoff {delay}s")

                    if should_requeue:
                        old_vm_id_rl = task.vm_instance_id  # Save BEFORE reassignment
                        task.status = TaskStatus.QUEUED
                        # The earlier FAILED assignment fired the SQLAlchemy set event
                        # which added task.id to _terminated_task_ids, revoking the JWT.
                        # Since we're requeuing, drop the cache entry so the next
                        # provisioned VM can successfully claim.
                        from app.routers.agent import _terminated_task_ids  # noqa: PLC0415
                        _terminated_task_ids.pop(str(task.id), None)
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = f"{failure_class.replace('_', ' ').title()} — requeued"
                        # For rate-limited tasks: check if any account is still available.
                        # If all are exhausted, set retry_after based on earliest recovery
                        # to prevent rapid cycling through exhausted accounts.
                        if failure_class == "RATE_LIMIT":
                            model = _extract_task_model(task)
                            blacklisted = ctx.get("blacklisted_accounts") or []
                            # Check this user's accounts to see if any are available.
                            # Find which user pool the failed account belongs to.
                            any_available = False
                            earliest_recovery = None
                            async with _max_plan_pool_lock:
                                # Find user pool via the account that just failed
                                user_pool = []
                                if acct_id:
                                    for uid, pool in _max_plan_pools.items():
                                        if any(a["id"] == acct_id for a in pool):
                                            user_pool = pool
                                            break
                                for a in user_pool:
                                    if a["id"] in blacklisted:
                                        continue
                                    if _account_available_for_model(a, model):
                                        any_available = True
                                        break
                                    # Track earliest model recovery across user's accounts
                                    dm = a.get("disabled_models", {})
                                    if model and model in dm:
                                        info = dm[model]
                                        disabled_at = info.get("at")
                                        if disabled_at:
                                            hits = info.get("hits", 1)
                                            cooldown_min = min(30 * (2 ** (hits - 1)), 240)
                                            recovery_dt = disabled_at + timedelta(minutes=cooldown_min)
                                            if earliest_recovery is None or recovery_dt < earliest_recovery:
                                                earliest_recovery = recovery_dt
                            if any_available:
                                ctx.pop("retry_after", None)
                            elif earliest_recovery:
                                ctx["retry_after"] = earliest_recovery.isoformat()
                                task.latest_status_message = f"Rate Limited — all accounts exhausted, waiting for recovery"
                            else:
                                ctx["retry_after"] = (_utcnow() + timedelta(minutes=5)).isoformat()
                                task.latest_status_message = f"Rate Limited — all accounts exhausted, retrying in 5min"
                        elif failure_class != "EXTERNAL_API":
                            ctx.pop("retry_after", None)
                        ctx.pop("max_plan_account_id", None)
                        ctx.pop("_slot_released", None)
                        # The task just ran on a VM successfully enough to POST a result —
                        # provisioning path worked. Don't carry a stale provision budget
                        # into the retry, or one more provision hiccup will fail it outright.
                        ctx.pop("provision_failures", None)
                        task.context = ctx
                        session.add(task)
                        await session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED})
                        logger.info(f"[AGENT STATUS] Task {task.id} {failure_class} — requeued without burning retry")
                        await _ensure_vm_deleted(old_vm_id_rl, reason="rate_limit_requeue")
                        return {"status": "accepted"}
        except Exception as e:
            logger.error(f"[STATUS] Exception during result recording for task {task.id}: {e!r}", exc_info=True)

        # Release Max plan account slot (idempotent — skip if already released by reaper)
        # Runs regardless of whether result recording above succeeded or threw.
        # Flush immediately so _slot_released persists even if downstream processing fails.
        acct_id = (task.context or {}).get("max_plan_account_id")
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(acct_id)
            task.context = {**(task.context or {}), "_slot_released": True}
            session.add(task)
            await session.flush()

        task.vm_stopped_at = _utcnow()
        if task.vm_provisioned_at:
            _runtime_secs = (task.vm_stopped_at - task.vm_provisioned_at).total_seconds()
            _rate = SPOT_RATE if task.vm_spot else ON_DEMAND_RATE
            ctx = dict(task.context or {})
            ctx['estimated_cost_usd'] = round(_runtime_secs * _rate, 4)
            task.context = ctx
            await _check_daily_cost_ceiling(task, session)
        old_vm_id = task.vm_instance_id
        await _ensure_vm_deleted(old_vm_id, reason="agent_completion")

        # Retry logic for failed tasks
        if payload.status == TaskStatus.FAILED and task.attempt_count < _runtime_config["max_retries"]:
            task.attempt_count += 1
            task.status = TaskStatus.QUEUED
            # The earlier FAILED assignment fired the SQLAlchemy set event
            # which added task.id to _terminated_task_ids, revoking the JWT.
            # Since we're retrying, drop the cache entry so the next
            # provisioned VM can successfully claim.
            from app.routers.agent import _terminated_task_ids  # noqa: PLC0415
            _terminated_task_ids.pop(str(task.id), None)
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            task.last_heartbeat = None
            task.started_at = None  # Fresh timeout window for the new VM
            # Add retry delay for transient failures to avoid hammering exhausted resources
            ctx = dict(task.context or {})
            if failure_class == "RATE_LIMIT":
                # Track per-account rate-limit hit count for exponential backoff
                rl_counts = dict(ctx.get("rate_limit_counts") or {})
                if acct_id:
                    rl_counts[str(acct_id)] = rl_counts.get(str(acct_id), 0) + 1
                ctx["rate_limit_counts"] = rl_counts

                rl_hit_count = rl_counts.get(str(acct_id), 1) if acct_id else 1
                # Exponential backoff: 30min * 2^(n-1), capped at 4 hours, ±20% jitter
                base_delay_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                delay_minutes = base_delay_minutes * random.uniform(0.8, 1.2)
                retry_after = (_utcnow() + timedelta(minutes=delay_minutes)).isoformat()
                ctx["retry_after"] = retry_after
                logger.info(f"[ORCHESTRATOR] Task {task.id} rate-limited (hit #{rl_hit_count}) — holding back {delay_minutes:.0f}min (retry_after={retry_after})")
                # Blacklist rate-limited account so the retry picks a different one
                if acct_id:
                    blacklisted = list(ctx.get("blacklisted_accounts") or [])
                    if acct_id not in blacklisted:
                        blacklisted.append(acct_id)
                    ctx["blacklisted_accounts"] = blacklisted
            elif failure_class == "PROVISION_FAILURE":
                delay_minutes = 5 * random.uniform(0.75, 1.25)  # 3.75-6.25 minutes
                retry_after = (_utcnow() + timedelta(minutes=delay_minutes)).isoformat()
                ctx["retry_after"] = retry_after
                logger.info(f"[ORCHESTRATOR] Task {task.id} provision failure — holding back {delay_minutes:.1f}min (retry_after={retry_after})")
            else:
                ctx.pop("retry_after", None)  # Clear any stale retry_after on non-delayed retries
                # Agent reached the point of POSTing a result, so provisioning worked —
                # don't carry a stale provision budget into the retry.
                ctx.pop("provision_failures", None)
            task.context = ctx
            session.add(task)

            await session.commit()

            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {"task_id": str(task.id), "status": TaskStatus.QUEUED, "attempt_count": task.attempt_count}
            )
            return {"status": "retrying", "message": f"Task {task.id} failed, retrying (Attempt {task.attempt_count}/3)"}

        else:
            # Retries exhausted — annotate the final status message with context
            if payload.status == TaskStatus.FAILED:
                max_r = _runtime_config["max_retries"]
                existing_msg = task.latest_status_message or ""
                if "retries exhausted" not in existing_msg:
                    task.latest_status_message = (
                        f"[{failure_class}] Retries exhausted ({task.attempt_count}/{max_r}). "
                        + existing_msg[:300]
                    ).strip()
                    session.add(task)

        # Record first-attempt success metrics for playbook effectiveness tracking
        if payload.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            try:
                _ctx = dict(task.context or {})
                _ctx["_first_attempt_succeeded"] = _compute_first_attempt_succeeded(
                    task.attempt_count, payload.status
                )
                _proj = None
                try:
                    _proj = await session.get(Project, task.project_id)
                    _playbook_count = sum(
                        1 for k in (_proj.design_doc or {}) if k.startswith("playbook/")
                    ) if _proj else 0
                except Exception:
                    _playbook_count = 0
                _ctx["_had_playbook"] = bool(_playbook_count)
                task.context = _ctx
                session.add(task)
                if payload.status == TaskStatus.COMPLETED and not bool(_playbook_count):
                    logger.info(
                        f"[METRICS] first_task_no_playbook project={task.project_id} task={task.id} "
                        f"type={task.first_payload.get('type', 'unknown')}"
                    )
                # Increment confirmed_count on applicable playbook entries for successful first attempts
                if _ctx["_first_attempt_succeeded"] and _proj and _playbook_count > 0:
                    try:
                        _task_type = task.first_payload.get("type", "create")
                        _PLAYBOOK_DEFAULT_TYPES = {
                            "create", "fix", "refactor", "review", "qa", "smoketest",
                            "deps", "migrate", "debug", "docs",
                        }
                        _doc = dict(_proj.design_doc or {})
                        _confirmed_changed = False
                        for _pk, _pv in list(_doc.items()):
                            if _pk.startswith("playbook/") and isinstance(_pv, dict):
                                _applicable = _pv.get("applicable_types")
                                if _applicable is not None:
                                    if _task_type not in _applicable:
                                        continue
                                elif _task_type not in _PLAYBOOK_DEFAULT_TYPES:
                                    continue
                                _entry = dict(_pv)
                                _entry["confirmed_count"] = _entry.get("confirmed_count", 0) + 1
                                _doc[_pk] = _entry
                                _confirmed_changed = True
                        if _confirmed_changed:
                            _proj.design_doc = _doc
                            flag_modified(_proj, "design_doc")
                            session.add(_proj)
                    except Exception as _pce:
                        logger.warning(f"[PLAYBOOK] Failed to increment confirmed_count for task {task.id}: {_pce}")
                # Capture positive pattern for sampling-eligible first-attempt successes
                if _ctx["_first_attempt_succeeded"] and _proj:
                    try:
                        from app.playbook_learning import capture_success_pattern  # noqa: PLC0415
                        await capture_success_pattern(task, session)
                    except Exception as _sce:
                        logger.warning(f"[PLAYBOOK] Failed to capture success pattern for task {task.id}: {_sce}")
            except Exception as _e:
                logger.warning(f"[METRICS] Failed to record first-attempt metrics for task {task.id}: {_e}")

        # Auto-create playbook entries from recurring failure patterns
        if payload.status == TaskStatus.FAILED:
            try:
                await _check_recurring_failures(task, session)
            except Exception as _pbe:
                logger.warning(f"[PLAYBOOK] Failed to check recurring failures for task {task.id}: {_pbe}")

        # Spawn a debug task for permanently failed tasks (retries exhausted)
        failed_task_payload = task.first_payload
        failed_task_type = failed_task_payload.get("type", "")
        should_auto_debug = (
            payload.status == TaskStatus.FAILED and  # Only for failed tasks (failure_class is unset for COMPLETED)
            failed_task_type != "debug" and  # Don't recursively debug debug tasks
            (task.context or {}).get("debug_task_id") is None and  # Don't re-debug fix tasks spawned by debug
            payload.logs and
            failure_class == "CODE_ERROR" and  # Only debug actual code errors, not infra/timeout/rate-limit
            (
                (task.context or {}).get("auto_debug") is True or
                task.parent_task_id is not None or  # Part of a pipeline (plan/research subtask)
                task.created_by == "ORCHESTRATOR"  # Autonomous research/QA/security audit tasks
            )
        )
        if should_auto_debug:
            base_branch = failed_task_payload.get("base_branch", "main")
            debug_task = Task(
                project_id=task.project_id,
                title=f"Debug: {task.title}",
                description=f"Automated root cause analysis for failed task: {task.title}",
                parent_task_id=task.id,
                payloads=[_validate_internal_payload({
                    "type": "debug",
                    "failed_task_id": str(task.id),
                    "failed_task_title": task.title,
                    "error_logs": (payload.logs or "")[:20000],
                    "base_branch": base_branch,
                    "failed_task_prompt": (failed_task_payload.get("prompt") or task.description or "")[:2000],
                    "failed_task_type": failed_task_type,
                }, "main:auto_debug")],
                context={
                    "failed_task_id": str(task.id),
                    "parent_base": base_branch,
                    "related_files": (task.context or {}).get("related_files", []),
                    "scope": (task.context or {}).get("scope"),
                    "failure_class": failure_class,
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
            )
            session.add(debug_task)
            await session.flush()
            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {"task_id": str(debug_task.id), "status": TaskStatus.QUEUED, "title": debug_task.title}
            )
            logger.error(f"[DEBUG] Spawned debug task {debug_task.id} for permanently failed task {task.id}")

        # Circuit breaker for autonomous tasks (CLOSED/OPEN/HALF-OPEN)
        # Tracks two separate failure classes:
        #  - failure_count: code-quality failures (CODE_ERROR, AGENT_TIMEOUT) → opens at 3
        #  - infra_failure_count: infra wall (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API) → opens at 5
        # Both walls block autonomous spawning, but infra opens use a shorter cooldown
        # because infra cascades typically self-heal faster than code regressions.
        _cb_failure_class = classify_failure(task, payload.logs)
        if payload.status == TaskStatus.FAILED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            is_code_failure = _cb_failure_class in ("CODE_ERROR", "AGENT_TIMEOUT")
            is_infra_failure = _cb_failure_class in ("PROVISION_FAILURE", "RATE_LIMIT", "EXTERNAL_API")
            if is_code_failure:
                breaker["failure_count"] += 1
                breaker["success_count"] = 0
            elif is_infra_failure:
                breaker["infra_failure_count"] = breaker.get("infra_failure_count", 0) + 1
                breaker["success_count"] = 0

            # HALF-OPEN test task failed → back to OPEN with doubled cooldown
            # (any failure on the test task re-opens, including infra failures)
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get("half_open_task_id"):
                breaker["state"] = "OPEN"
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)  # cap at 2 hours
                breaker["opened_at"] = _utcnow()
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → OPEN for {project_key} (cooldown doubled to {breaker['cooldown_seconds']}s)")
                await _maybe_create_circuit_breaker_alert(
                    task.project_id, session,
                    f"HALF-OPEN test task failed — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )

            # CLOSED → OPEN after 3 consecutive code failures
            elif breaker["state"] == "CLOSED" and breaker["failure_count"] >= 3:
                breaker["state"] = "OPEN"
                breaker["open_reason"] = "code"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                await broadcast_to_project(project_key, "autonomous_paused",
                                           {"reason": f"Circuit breaker OPEN after {breaker['failure_count']} consecutive code failures"})
                logger.warning(f"[CIRCUIT BREAKER] CLOSED → OPEN (code) for {project_key} after {breaker['failure_count']} failures")
                _cb_alert_text = f"Circuit breaker opened after {breaker['failure_count']} consecutive code failures (cooldown: {breaker['cooldown_seconds']}s)"
                await _maybe_create_circuit_breaker_alert(
                    task.project_id, session,
                    _cb_alert_text,
                )
                try:
                    from app.notification_dispatch import post_failure_alert_message  # noqa: PLC0415
                    await post_failure_alert_message(session, task.project_id, _cb_alert_text, task.id)
                except Exception as _e:
                    logger.warning(f"[NOTIFY] Failed to post circuit breaker message: {_e}")

            # CLOSED → OPEN after 5 consecutive infra failures (wall of provisioning/rate-limit)
            elif breaker["state"] == "CLOSED" and breaker.get("infra_failure_count", 0) >= 5:
                breaker["state"] = "OPEN"
                breaker["open_reason"] = "infra"
                breaker["opened_at"] = _utcnow()
                # Shorter cooldown than code breaker — infra outages usually self-heal faster
                breaker["cooldown_seconds"] = 300  # 5 min
                await broadcast_to_project(project_key, "autonomous_paused",
                                           {"reason": f"Circuit breaker OPEN after {breaker['infra_failure_count']} consecutive infra failures ({_cb_failure_class})"})
                logger.warning(f"[CIRCUIT BREAKER] CLOSED → OPEN (infra) for {project_key} after {breaker['infra_failure_count']} infra failures")
                _cb_alert_text = (
                    f"Circuit breaker opened: {breaker['infra_failure_count']} consecutive infra failures "
                    f"({_cb_failure_class}). Cooldown: {breaker['cooldown_seconds']}s."
                )
                await _maybe_create_circuit_breaker_alert(
                    task.project_id, session,
                    _cb_alert_text,
                )
                try:
                    from app.notification_dispatch import post_failure_alert_message  # noqa: PLC0415
                    await post_failure_alert_message(session, task.project_id, _cb_alert_text, task.id)
                except Exception as _e:
                    logger.warning(f"[NOTIFY] Failed to post circuit breaker message: {_e}")
            # Persist breaker state on any transition
            if (
                breaker["state"] != "CLOSED"
                or breaker["failure_count"] > 0
                or breaker.get("infra_failure_count", 0) > 0
            ):
                await _orch._persist_circuit_breakers()
        # When review/fix fails permanently, cancel the original create task so rollup
        # skips it (via cancelled-child logic) instead of hanging forever.
        # Other children's work is preserved — only this PR is excluded from the rollup.
        if payload.status == TaskStatus.FAILED:
            task_payload = task.first_payload
            task_type = task_payload.get("type", "")
            if task_type in ("review", "fix"):
                original_task_id = ((task.context or {}).get("review_loop", {}).get("original_task_id", "")
                                    or task_payload.get("original_task_id", ""))
                if original_task_id:
                    try:
                        orig = await session.get(Task, uuid.UUID(original_task_id))
                        if orig and orig.status == TaskStatus.COMPLETED:
                            orig.status = TaskStatus.CANCELLED
                            orig.latest_status_message = f"Pipeline {task_type} task failed — PR excluded from rollup"
                            session.add(orig)
                            await session.commit()
                            # Trigger rollup check so parent can proceed with remaining children
                            if orig.parent_task_id:
                                await _check_and_trigger_rollup(orig, session)
                            logger.error(f"[PIPELINE] Cancelled original task {original_task_id} — {task_type} failed, skipping in rollup")
                    except Exception as e:
                        logger.warning(f"[PIPELINE] Failed to propagate failure to {original_task_id}: {e}", exc_info=True)

        if payload.status == TaskStatus.COMPLETED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            breaker["failure_count"] = 0
            breaker["infra_failure_count"] = 0
            breaker["success_count"] += 1

            # HALF-OPEN test task succeeded → back to CLOSED
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get("half_open_task_id"):
                breaker["state"] = "CLOSED"
                breaker["open_reason"] = None
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → CLOSED for {project_key} (test task succeeded)")
            elif breaker["state"] == "OPEN":
                # Shouldn't happen (OPEN blocks spawning), but reset if it does
                breaker["state"] = "CLOSED"
                breaker["open_reason"] = None
            # Persist on state transition back to CLOSED
            if (
                breaker["state"] == "CLOSED"
                and breaker["failure_count"] == 0
                and breaker["infra_failure_count"] == 0
            ):
                await _orch._persist_circuit_breakers()

        if payload.status == TaskStatus.COMPLETED:
            try:
                from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
                asyncio.ensure_future(dispatch_external_notification(
                    task.project_id,
                    "TASK_COMPLETED",
                    {
                        "task_id": str(task.id),
                        "task_title": task.title or str(task.id)[:8],
                        "task_type": task.first_payload.get("type", ""),
                    },
                ))
            except Exception as _ne:
                logger.warning(f"[NOTIFY] TASK_COMPLETED dispatch setup failed: {_ne}")

        # ── EARLY COMMIT ──
        # Release the FOR UPDATE lock NOW, before downstream processing.
        # Everything below (review pipeline, completion handlers, rollup) makes
        # GitHub API calls that can hang 15-120+ seconds during connectivity issues.
        # Holding the lock during those calls starves the heartbeat endpoint
        # (which also needs FOR UPDATE on this row), causing last_heartbeat to go
        # stale and the reaper to kill the task.
        # At this point the status change, artifact, and retry logic are all
        # persisted.  expire_on_commit=False keeps the task object usable.
        try:
            if session.dirty or session.new:
                await session.commit()
        except Exception as early_exc:
            logger.error(f"[PIPELINE] Early commit failed for {task.id}: {early_exc}", exc_info=True)
            try:
                await session.rollback()
            except Exception:
                pass
            raise

        # Pass a session factory bound to the SAME engine the request used.
        # In production this is AsyncSessionLocal; in tests it's the test engine.
        # This ensures the helper's fresh session doesn't bypass test DB overrides.
        _bind = session.bind
        _factory = async_sessionmaker(_bind, class_=AsyncSession, expire_on_commit=False)
        try:
            return await _process_post_commit(task.id, payload, _session_factory=_factory)
        except Exception as e:
            # Early-commit already persisted the task status. If post-commit
            # (review pipeline, rollup, etc.) fails, the idempotency guard
            # would block agent retries — permanently orphaning any PR that
            # needed a review/fix spawn. Set a flag so the guard lets the
            # retry re-run post-commit processing.
            logger.error(f"[PIPELINE] Post-commit processing failed for task {task.id}: {e}", exc_info=True)
            try:
                await session.rollback()
                await session.refresh(task)
                task.context = {**(task.context or {}), "_post_commit_pending": {"at": _utcnow().isoformat()}}
                session.add(task)
                await session.commit()
            except Exception as ctx_err:
                logger.exception(f"[PIPELINE] Failed to set _post_commit_pending for task {task.id}: {ctx_err}")
            raise
