"""Task completion handlers and their supporting functions.

Extracted from app.main to keep main.py focused on routing and orchestration.
These handlers are called from _process_post_commit() in main.py based on task type.

Anomaly detection, research completion, and strategic completion handlers have
been further extracted into their own modules (anomaly_detection.py,
research_completion.py, strategic_completion.py).
"""
import asyncio
import json
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.github import create_branch, create_pr_comment, get_branch_sha, get_open_pull_requests
from app.models import (
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.routers.agent import get_github_token_for_task
from app.shared_state import runtime_config as _runtime_config
from app.payload_schemas import validate_internal_payload
from app.broadcast import broadcast_to_project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

def _compute_first_attempt_succeeded(attempt_count: int, status: "TaskStatus") -> bool:
    """Return True if the task completed successfully on its first attempt (attempt_count <= 1)."""
    return attempt_count <= 1 and status == TaskStatus.COMPLETED

# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# ---------------------------------------------------------------------------
# Parser / Validator helpers
# ---------------------------------------------------------------------------

def _parse_plan_output(text: str) -> Optional[Dict[str, Any]]:
    """Extract structured plan JSON from agent output text.

    Uses string-aware bracket counting so braces inside JSON string values
    (e.g. code snippets in prompts) don't break extraction.
    """
    text = text[:500_000]
    try:
        match = re.search(r'\{\s*"subtasks"\s*:\s*\[', text)
        if not match:
            return None
        start = match.start()
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    return json.loads(candidate)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[PLAN] Failed to parse plan JSON: {e}", exc_info=True)
    return None

def _extract_json_array(text: str, *required_keys: str) -> Optional[str]:
    """Find the first JSON array in text containing objects with the required keys.

    Uses bracket-depth counting (O(n)) instead of regex with greedy groups,
    which can cause O(n*k) backtracking on malformed input and block the
    async event loop for seconds.
    """
    # Cap input to prevent scanning megabytes of raw output
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    # Find outermost [ ... ] containing the required keys
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
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
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        return candidate
                    break  # This bracket pair didn't match, try next [
        start = text.find("[", start + 1)
    return None

def _validate_plan_dependencies(subtasks: List[Dict]) -> bool:
    """Validate that plan dependencies are acyclic (topological sort check)."""
    n = len(subtasks)
    visited = set()
    in_stack = set()

    def has_cycle(node: int) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in subtasks[node].get("depends_on_indices", []):
            if 0 <= dep < n and has_cycle(dep):
                return True
        in_stack.discard(node)
        return False

    return not any(has_cycle(i) for i in range(n) if i not in visited)

def _log_file_overlaps(subtasks: List[Dict], label: str = "PLAN") -> List[tuple]:
    """Log warnings when subtasks share related_files (for observability).

    Returns a list of (i, j, overlapping_files) tuples for each overlapping pair.
    """
    overlaps = []
    for i, a in enumerate(subtasks):
        a_files = set(a.get("related_files", []))
        if not a_files:
            continue
        for j, b in enumerate(subtasks):
            if j <= i:
                continue
            b_files = set(b.get("related_files", []))
            overlap = a_files & b_files
            if overlap:
                a_title = a.get("title", f"#{i}")
                b_title = b.get("title", f"#{j}")
                logger.info(f"[{label}] File overlap: '{a_title}' and '{b_title}' share {', '.join(sorted(overlap))}")
                overlaps.append((i, j, overlap))
    return overlaps

def _inject_file_overlap_deps(subtasks: List[Dict], label: str = "PLAN") -> None:
    """Add depends_on_indices entries to enforce serial execution for subtasks sharing files.

    For each file touched by more than one subtask, builds a chain in index order so that
    subtask[i+1] depends on subtask[i].  Auto-generated deps are merged (union, no duplicates)
    with any explicit depends_on_indices already present in the plan.
    """
    # Build file → [subtask indices] mapping
    file_to_indices: Dict[str, List[int]] = {}
    for idx, subtask in enumerate(subtasks):
        for f in subtask.get("related_files", []):
            file_to_indices.setdefault(f, []).append(idx)

    # For each file group with >1 subtask, inject chain deps
    for file_path, indices in file_to_indices.items():
        if len(indices) < 2:
            continue
        # indices are already in insertion order (which matches plan order)
        for pos in range(len(indices) - 1):
            predecessor = indices[pos]
            successor = indices[pos + 1]
            existing = subtasks[successor].setdefault("depends_on_indices", [])
            if predecessor not in existing:
                existing.append(predecessor)
                succ_title = subtasks[successor].get("title", f"#{successor}")
                pred_title = subtasks[predecessor].get("title", f"#{predecessor}")
                logger.debug(f"[{label}] Serializing subtasks {pred_title!r} → {succ_title!r} due to shared file: {file_path}")

async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str,
    label: str, session: AsyncSession
) -> bool:
    """Create a branch, or verify it already exists.

    Returns True on success, False on failure (task set to FAILED).
    """
    # Deferred import to avoid circular dependency
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    max_retries = 5
    gh_token = owner = repo = None
    last_err = None
    for _attempt in range(max_retries):
        try:
            gh_token, owner, repo = await get_github_token_for_task(
                task.id, session, force_refresh=(_attempt > 0))
            sha = await get_branch_sha(gh_token, owner, repo, source_branch)
            await create_branch(gh_token, owner, repo, branch_name, sha)
            logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
            return True
        except Exception as e:
            last_err = e
            if gh_token:
                try:
                    await get_branch_sha(gh_token, owner, repo, branch_name)
                    logger.info(f"[{label}] Branch {branch_name} already exists — reusing")
                    return True
                except HTTPException as verify_exc:
                    if verify_exc.status_code == 404:
                        # Branch genuinely doesn't exist — count against attempt budget
                        pass
                    else:
                        # Transient server error during verification (e.g. 502) — don't
                        # conclude the branch is missing; continue retrying.
                        logger.warning(f"[{label}] Transient error verifying branch {branch_name} ({verify_exc.status_code}), will retry")
                except Exception:
                    logger.exception(f"[{label}] Non-HTTP error verifying branch {branch_name}")
            if _attempt < max_retries - 1:
                backoff = 10 * (2 ** _attempt) * (0.75 + 0.5 * random.random())  # jitter: 10-20s, 20-40s, …
                logger.warning(f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {backoff:.1f}s: {e!r}")
                await asyncio.sleep(backoff)
            else:
                if gh_token:
                    logger.exception(f"[{label}] Branch {branch_name} creation failed after {max_retries} attempts")
                else:
                    logger.exception(f"[{label}] Cannot create branch — GitHub token unavailable")
    e = last_err
    task.status = TaskStatus.FAILED
    task.latest_status_message = f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415
    _fc = classify_failure(task)
    persist_failure_category(task, _fc)
    record_task_result(success=False, failure_class=_fc, task_id=str(task.id))
    session.add(task)
    await _maybe_create_failure_alert(task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200])
    await session.commit()
    return False

# ---------------------------------------------------------------------------
# Completion handlers
# ---------------------------------------------------------------------------

async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    # Deferred import to avoid circular dependency
    from app.main import _maybe_create_failure_alert  # noqa: PLC0415

    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
        from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
        from app.routers.agent_status import record_task_result  # noqa: PLC0415
        _fc = classify_failure(task)
        persist_failure_category(task, _fc)
        record_task_result(success=False, failure_class=_fc, task_id=str(task.id))
        session.add(task)
        await _maybe_create_failure_alert(task, session, "Plan agent produced no valid subtasks")
        await session.commit()
        return

    subtasks = plan_data["subtasks"]

    # Cap subtask count to prevent runaway VM provisioning
    if len(subtasks) > 10:
        subtasks = subtasks[:10]
        logger.info(f"[PLAN] Capped subtask count from {len(plan_data['subtasks'])} to 10")

    if not _validate_plan_dependencies(subtasks):
        task.latest_status_message = "Plan has circular dependencies"
        task.status = TaskStatus.FAILED
        from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
        from app.routers.agent_status import record_task_result  # noqa: PLC0415
        _fc = classify_failure(task)
        persist_failure_category(task, _fc)
        record_task_result(success=False, failure_class=_fc, task_id=str(task.id))
        session.add(task)
        await _maybe_create_failure_alert(task, session, "Plan has circular dependencies")
        await session.commit()
        return

    # Store parsed plan and set parent to EVALUATING (children executing)
    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = (task.first_payload).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    plan_branch = f"plan/{short_id}"

    # Create staging branch for this plan's subtasks
    if not await _ensure_branch(task, plan_branch, parent_base, "PLAN", session):
        return

    # Store branch info on the plan task for rollup later
    task.context = {**(task.context or {}), "plan_branch": plan_branch, "parent_base": parent_base}
    session.add(task)

    # Children are created here (after branch creation) so they get the correct
    # base_branch and don't need a requires_approval hold.

    # First pass: create all child tasks
    parent_payload = task.first_payload
    model = parent_payload.get('model')
    effort = parent_payload.get('effort')
    plan_parent_context = {
        "parent_task_id": str(task.id),
        "parent_title": task.title,
        "parent_prompt": (parent_payload.get("prompt") or parent_payload.get("description") or "")[:500]
    }
    parent_direction_id = (task.context or {}).get('direction_id')
    parent_research_task_id = (task.context or {}).get('research_task_id')
    child_tasks = []
    for subtask_def in subtasks:
        related_files = subtask_def.get("related_files", [])
        prompt_text = subtask_def.get("prompt", "")
        full_prompt = prompt_text + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt_text
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        subtask_priority = TASK_PRIORITY_MAP.get(subtask_def.get("priority", "medium"), 50)
        subtask_model = subtask_def.get('model') or model
        subtask_effort = subtask_def.get('effort') or effort
        child_scope = subtask_def.get('scope', 'small')
        child_risk = subtask_def.get('risk', 'low')
        child_approval_reason = subtask_def.get('approval_reason') if child_risk == 'high' else None
        _allowed_types = {'create', 'refactor', 'migrate', 'qa'}
        raw_type = subtask_def.get('type', 'create')
        subtask_type = raw_type if raw_type in _allowed_types else 'create'
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            payloads=[validate_internal_payload({
                "type": subtask_type,
                "prompt": full_prompt,
                "base_branch": plan_branch,
                **({"model": subtask_model} if subtask_model else {}),
                **({"effort": subtask_effort} if subtask_effort else {}),
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }, f"task_completion:plan_subtask:{subtask_type}")],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_scope,
                "risk": child_risk,
                "related_files": related_files,
                "parent_context": plan_parent_context,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
                # Inherit direction_id from plan parent if present and not overridden by finding
                **({"direction_id": parent_direction_id} if parent_direction_id else {}),
                **({"research_task_id": parent_research_task_id} if parent_research_task_id else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
            priority=subtask_priority,
        )
        session.add(child)
        child_tasks.append(child)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Inject serial dependencies for subtasks that share files (additive only)
    # Save original (explicit) deps so we can restore them if injection creates a cycle
    original_deps = {i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)}
    _inject_file_overlap_deps(subtasks, "PLAN")

    # Re-validate after file overlap injection — injection can create cycles
    # (e.g., A explicitly depends on B, but file overlap adds B → A)
    if not _validate_plan_dependencies(subtasks):
        logger.error("[PLAN] Circular dependency after file overlap injection — reverting to explicit deps only")
        for i, st in enumerate(subtasks):
            if original_deps.get(i):
                st["depends_on_indices"] = original_deps[i]
            else:
                st.pop("depends_on_indices", None)

    # Second pass: resolve depends_on_indices to actual UUIDs
    for i, subtask_def in enumerate(subtasks):
        dep_indices = subtask_def.get("depends_on_indices", [])
        child_tasks[i].depends_on = [
            str(child_tasks[j].id)
            for j in dep_indices
            if 0 <= j < len(child_tasks)
        ]
        session.add(child_tasks[i])

    # Log file overlap warnings for observability
    _log_file_overlaps(subtasks, "PLAN")

    await session.commit()

    # Record history
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PLAN_DECOMPOSITION", "parent_plan_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()
    logger.info(f"[PLAN] Created {len(child_tasks)} subtasks from plan {task.id}")

async def _handle_security_audit_completion(task: Task, logs: str, session: AsyncSession):
    """Parse security audit findings and spawn create tasks for critical/high severity issues."""
    # Parse the JSON findings array from logs
    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = "Security audit completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)
    await session.commit()

    base_branch = (task.first_payload).get("base_branch", "main")

    # Spawn create tasks for critical and high severity findings
    actionable = [f for f in findings if f.get("severity") in ("critical", "high")]
    if len(actionable) > 10:
        actionable = actionable[:10]
    if not actionable:
        task.latest_status_message = f"Security audit found {len(findings)} finding(s), none critical/high — no tasks spawned"
        session.add(task)
        await session.commit()
        return

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Security fix #{i+1}")
        severity = finding.get("severity", "high")

        new_task = Task(
            project_id=task.project_id,
            title=f"[{severity.upper()}] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[validate_internal_payload({
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }, "task_completion:security_finding")],
            context={
                "security_audit_task_id": str(task.id),
                "finding_index": i,
                "severity": severity,
                "file_path": finding.get("file_path", ""),
                "line_number": finding.get("line_number", 0),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "SECURITY_AUDIT", "parent_audit_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()

    # Post PR comment for medium/low findings that don't get auto-fixed
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    if informational:
        rows = "\n".join(
            "| {severity} | {title} | {loc} | {desc} |".format(
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                loc=(f.get("file_path", "") + (f":{f['line_number']}" if f.get("line_number") else "")),
                desc=f.get("description", ""),
            )
            for f in informational
        )
        comment_body = (
            "## Security Audit Findings (Informational)\n\n"
            "The following medium/low severity findings were identified. "
            "They have not been auto-fixed and require manual review:\n\n"
            "| Severity | Title | File | Description |\n"
            "|----------|-------|------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(f"[SECURITY AUDIT] Posted informational findings comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[SECURITY AUDIT] Failed to post PR comment for informational findings: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} critical/high fixed, {len(informational)} medium/low surfaced as PR comment"
    )
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"security_audit_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[SECURITY AUDIT] Created {len(child_tasks)} fix tasks from audit {task.id}")

async def _handle_perf_completion(task: Task, logs: str, session: AsyncSession):
    """Parse perf findings and spawn create tasks for high-severity issues."""
    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = "Perf analysis completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)

    base_branch = (task.first_payload).get("base_branch", "main")

    # Spawn create tasks for high-severity findings
    actionable = [f for f in findings if f.get("severity") == "high"]
    if len(actionable) > 10:
        actionable = actionable[:10]

    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Perf fix #{i+1}")
        area = finding.get("area", "")

        new_task = Task(
            project_id=task.project_id,
            title=f"[PERF] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[validate_internal_payload({
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
            }, "task_completion:perf_finding")],
            context={
                "perf_task_id": str(task.id),
                "finding_index": i,
                "severity": "high",
                "area": area,
                "related_files": finding.get("related_files", []),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.flush()
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PERF_AUDIT", "parent_perf_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    # Post a summary comment on open PRs for medium/low findings
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    all_surfaced = informational + [f for f in findings if f.get("severity") == "high" and f not in actionable]
    if all_surfaced or findings:
        rows = "\n".join(
            "| {area} | {severity} | {title} | {desc} |".format(
                area=f.get("area", ""),
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                desc=f.get("description", "")[:120],
            )
            for f in findings
        )
        comment_body = (
            "## Performance Analysis Findings\n\n"
            f"Analyzed codebase for performance bottlenecks. "
            f"{len(child_tasks)} high-severity fix task(s) spawned automatically.\n\n"
            "| Area | Severity | Title | Description |\n"
            "|------|----------|-------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(f"[PERF] Posted findings summary comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[PERF] Failed to post PR comment: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} high-severity fix task(s) spawned, "
        f"{len(informational)} medium/low finding(s) surfaced as PR comment"
    )
    session.add(task)

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"perf_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[PERF] Created {len(child_tasks)} fix tasks from perf analysis {task.id}")

async def _handle_investigation_completion(task: Task, payload, session: AsyncSession):
    """Store investigation report logs in plan_output and mark task completed."""
    if payload.logs:
        try:
            task.plan_output = json.loads(payload.logs)
        except (json.JSONDecodeError, Exception):
            task.plan_output = {"raw": payload.logs}
    task.status = TaskStatus.COMPLETED
    session.add(task)

async def _handle_design_doc_completion(task: Task, logs: str, session: AsyncSession):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.
    """
    # Deferred imports to avoid circular dependency
    from app.strategic_spawning import _spawn_strategic_review  # noqa: PLC0415
    from app.main import _create_strategic_alert  # noqa: PLC0415

    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        logger.error(f"[DESIGN DOC] Task {task.id}: project {task.project_id} not found")
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update failed: project not found (task {str(task.id)[:8]})")
        return

    doc = project.design_doc or {}
    rev = doc.get("_meta", {}).get("revision", 0)
    page_count = sum(1 for k in doc if not k.startswith("_"))
    logger.info(f"[DESIGN DOC] Task {task.id} completed: {page_count} pages, rev {rev}")

    if page_count == 0:
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update produced 0 pages (task {str(task.id)[:8]})")

    # Chain: spawn Phase 2 (strategic review)
    # Do this BEFORE committing task status so a failed spawn doesn't leave
    # the task as COMPLETED with no Phase 2 follow-up.
    cycle_id = (task.context or {}).get("strategic_cycle_id")
    spawned = None
    try:
        await session.refresh(project)
        spawned = await _spawn_strategic_review(project, session, cycle_id=cycle_id)
        if spawned:
            task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
        else:
            # Spawn silently skipped (active strategic task) — create retry marker
            logger.warning(f"[DESIGN DOC] Phase 2 spawn skipped for task {task.id} (active strategic task exists)")
            task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review deferred (active task exists)"
            project.vision = {**(project.vision or {}), '_pending_strategic_review': {
                'design_doc_task_id': str(task.id),
                'failed_at': _utcnow().isoformat(),
                'error': 'skipped: active strategic task exists',
                'cycle_id': cycle_id,
            }}
            flag_modified(project, 'vision')
            session.add(project)
    except Exception as chain_exc:
        logger.error(f"[DESIGN DOC] Failed to chain Phase 2 for task {task.id}: {chain_exc}", exc_info=True)
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — FAILED to spawn strategic review: {chain_exc!r}"[:500]
        await _create_strategic_alert(task.project_id, session,
            f"Strategic review failed to spawn after design doc update (task {str(task.id)[:8]}): {chain_exc!r}")
        # Save a retry marker so the next research spawn can retry the strategic review
        project.vision = {**(project.vision or {}), '_pending_strategic_review': {
            'design_doc_task_id': str(task.id),
            'failed_at': _utcnow().isoformat(),
            'error': str(chain_exc)[:200],
            'cycle_id': cycle_id,
        }}
        if cycle_id:
            from app.research_completion import _update_cycle_history  # noqa: PLC0415
            _update_cycle_history(project, cycle_id, {
                "phase1_completed_at": _utcnow().isoformat(),
                "outcome": "failed_phase2_spawn",
                "error": str(chain_exc)[:200],
            })
        flag_modified(project, 'vision')
        session.add(project)
    if spawned:
        phase1_msg = ProjectMessage(
            project_id=task.project_id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            from_task_id=task.id,
            content=f"Architecture analysis completed ({page_count} pages, rev {rev}). Strategic review starting.",
            context={
                "event_type": "cycle_phase1_complete",
                "cycle_id": cycle_id,
                "page_count": page_count,
                "revision": rev,
            },
        )
        session.add(phase1_msg)
    session.add(task)
    await session.commit()

