"""Plan completion handler — splits a PLAN task's output into child subtasks.

Extracted from app.task_completion to keep that module within its size budget.
"""

import json
import logging
import re
import uuid
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast import broadcast_to_project
from app.models import Project, Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
    scope_to_effort,
)

logger = logging.getLogger(__name__)

# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}


def _parse_plan_output(text: str) -> Dict[str, Any] | None:
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
                    candidate = text[start : i + 1]
                    return json.loads(candidate)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[PLAN] Failed to parse plan JSON: {e}", exc_info=True)
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
                logger.info(
                    f"[{label}] File overlap: '{a_title}' and '{b_title}' share {', '.join(sorted(overlap))}"
                )
                overlaps.append((i, j, overlap))
    return overlaps


def _inject_file_overlap_deps(subtasks: List[Dict], label: str = "PLAN") -> None:
    """Add depends_on_indices entries to enforce serial execution for subtasks sharing files.

    For each file touched by more than one subtask, builds a chain in index order so that
    subtask[i+1] depends on subtask[i].  Auto-generated deps are merged (union, no duplicates)
    with any explicit depends_on_indices already present in the plan.
    """
    file_to_indices: Dict[str, List[int]] = {}
    for idx, subtask in enumerate(subtasks):
        for f in subtask.get("related_files", []):
            file_to_indices.setdefault(f, []).append(idx)

    for file_path, indices in file_to_indices.items():
        if len(indices) < 2:
            continue
        for pos in range(len(indices) - 1):
            predecessor = indices[pos]
            successor = indices[pos + 1]
            existing = subtasks[successor].setdefault("depends_on_indices", [])
            if predecessor not in existing:
                existing.append(predecessor)
                succ_title = subtasks[successor].get("title", f"#{successor}")
                pred_title = subtasks[predecessor].get("title", f"#{predecessor}")
                logger.debug(
                    f"[{label}] Serializing subtasks {pred_title!r} → {succ_title!r} due to shared file: {file_path}"
                )


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    from app.main import _maybe_create_failure_alert
    from app.task_completion import _ensure_branch

    project = await session.get(Project, task.project_id)
    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
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
        await _maybe_create_failure_alert(
            task, session, "Plan agent produced no valid subtasks"
        )
        await session.commit()
        return

    subtasks = plan_data["subtasks"]

    if len(subtasks) > 10:
        subtasks = subtasks[:10]
        logger.info(
            f"[PLAN] Capped subtask count from {len(plan_data['subtasks'])} to 10"
        )

    if not _validate_plan_dependencies(subtasks):
        task.latest_status_message = "Plan has circular dependencies"
        task.status = TaskStatus.FAILED
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
        await _maybe_create_failure_alert(
            task, session, "Plan has circular dependencies"
        )
        await session.commit()
        return

    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = (task.first_payload).get(
        "base_branch"
    ) or get_project_integration_branch(project)
    parent_hotfix = bool(task.first_payload.get("hotfix"))
    short_id = str(task.id).replace("-", "")[:8]
    plan_branch_prefix = getattr(project, "plan_branch_prefix", "plan/")
    plan_branch = f"{plan_branch_prefix}{short_id}"

    # Round 4 W1 (audit C2): _ensure_branch now returns Task | None.
    # On success, returns the freshly-locked Task (caller mutates under
    # the new lock). On failure or peer-mutation defer, returns None.
    relocked = await _ensure_branch(task, plan_branch, parent_base, "PLAN", session)
    if relocked is None:
        return
    task = relocked

    task.context = {
        **(task.context or {}),
        "plan_branch": plan_branch,
        "parent_base": parent_base,
    }
    session.add(task)

    parent_payload = task.first_payload
    # Don't inherit the plan task's resolved model — that was picked by
    # balanced selection for THIS task's run and would anchor every child
    # to the same provider regardless of the operator's category_routing.
    # Let the orchestrator resolve each child fresh from routing.
    effort = parent_payload.get("effort")
    plan_parent_context = {
        "parent_task_id": str(task.id),
        "parent_title": task.title,
        "parent_prompt": (
            parent_payload.get("prompt") or parent_payload.get("description") or ""
        )[:500],
    }
    parent_direction_id = (task.context or {}).get("direction_id")
    parent_research_task_id = (task.context or {}).get("research_task_id")
    child_tasks = []
    for subtask_def in subtasks:
        related_files = subtask_def.get("related_files", [])
        prompt_text = subtask_def.get("prompt", "")
        full_prompt = (
            prompt_text
            + (
                "\n\nKey files to review or modify:\n"
                + "\n".join(f"- {f}" for f in related_files)
            )
            if related_files
            else prompt_text
        )
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        subtask_priority = TASK_PRIORITY_MAP.get(
            subtask_def.get("priority", "medium"), 50
        )
        if parent_hotfix:
            subtask_priority = max(subtask_priority, 90)
        subtask_effort = subtask_def.get("effort") or effort
        child_scope = subtask_def.get("scope", "small")
        # Scope-aware effort floor: small subtasks run cheaper, large subtasks
        # need deeper analysis. Max-by-rank so an explicit subtask or parent
        # effort still wins when it is higher than the scope-derived floor.
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
        _scope_effort = scope_to_effort(child_scope)
        if _effort_rank.get(_scope_effort, 0) > _effort_rank.get(
            subtask_effort or "", 0
        ):
            subtask_effort = _scope_effort
        child_risk = subtask_def.get("risk", "low")
        child_approval_reason = (
            subtask_def.get("approval_reason") if child_risk == "high" else None
        )
        _allowed_types = {"create", "refactor", "migrate", "qa"}
        raw_type = subtask_def.get("type", "create")
        subtask_type = raw_type if raw_type in _allowed_types else "create"
        # Ignore subtask_def.model and parent plan model — legacy hints
        # that anchored every child to one provider regardless of the
        # operator's category_routing. Respect operator-level
        # task_type_models pins only; orchestrator resolves per child
        # from routing otherwise.
        subtask_model = _runtime_config.get("task_type_models", {}).get(subtask_type)
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            requires_approval=False,
            payloads=[
                validate_internal_payload(
                    {
                        "type": subtask_type,
                        "prompt": full_prompt,
                        "base_branch": plan_branch,
                        **({"model": subtask_model} if subtask_model else {}),
                        **({"effort": subtask_effort} if subtask_effort else {}),
                        **({"prompt_suffix": create_suffix} if create_suffix else {}),
                        **({"hotfix": True} if parent_hotfix else {}),
                    },
                    f"task_completion:plan_subtask:{subtask_type}",
                )
            ],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_scope,
                "risk": child_risk,
                "related_files": related_files,
                "parent_context": plan_parent_context,
                **(
                    {"approval_reason": child_approval_reason}
                    if child_approval_reason
                    else {}
                ),
                **(
                    {"direction_id": parent_direction_id} if parent_direction_id else {}
                ),
                **(
                    {"research_task_id": parent_research_task_id}
                    if parent_research_task_id
                    else {}
                ),
                **({"hotfix": True} if parent_hotfix else {}),
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

    # Inject serial dependencies for subtasks that share files (additive only).
    # Save original (explicit) deps so we can restore them if injection creates a cycle.
    original_deps = {
        i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)
    }
    _inject_file_overlap_deps(subtasks, "PLAN")

    if not _validate_plan_dependencies(subtasks):
        logger.error(
            "[PLAN] Circular dependency after file overlap injection — reverting to explicit deps only"
        )
        for i, st in enumerate(subtasks):
            if original_deps.get(i):
                st["depends_on_indices"] = original_deps[i]
            else:
                st.pop("depends_on_indices", None)

    for i, subtask_def in enumerate(subtasks):
        dep_indices = subtask_def.get("depends_on_indices", [])
        child_tasks[i].depends_on = [
            str(child_tasks[j].id) for j in dep_indices if 0 <= j < len(child_tasks)
        ]
        session.add(child_tasks[i])

    _log_file_overlaps(subtasks, "PLAN")

    await session.commit()

    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id,
            action="SPAWNED",
            details={
                "spawned_by": "PLAN_DECOMPOSITION",
                "parent_plan_id": str(task.id),
            },
        )
        session.add(history)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
        )

    await session.commit()
    logger.info(f"[PLAN] Created {len(child_tasks)} subtasks from plan {task.id}")
