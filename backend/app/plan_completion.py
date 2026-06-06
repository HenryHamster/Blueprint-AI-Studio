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
from app.depends_on_provenance import remember_structural_depends_on
from app.models import Project, Task, TaskHistory, TaskStatus
from app.payload_schemas import read_acceptance_evidence, validate_internal_payload
from app.task_capabilities import PLAN_KNOWLEDGE_SUBTASK_TYPES, PLAN_SUBTASK_TYPES
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


def _inject_build_on_deps(subtasks: List[Dict], label: str = "PLAN") -> List[tuple]:
    """Serialize cross-file BUILD-ON relationships between subtasks.

    ``_inject_file_overlap_deps`` only orders subtasks that touch a SHARED
    file. The canonical foundational shape — subtask A creates a brand-new
    module/file and a LATER subtask B (in a DIFFERENT file) imports or
    otherwise consumes A's new code — shares no files, so it gets no
    automatic edge and the same-base siblings would dispatch in parallel.

    This closes that hole conservatively: when a later subtask's prompt
    references a file that an earlier subtask OWNS in its ``related_files``
    but the later subtask does NOT edit itself, the later subtask is
    consuming the earlier one's output and must run after it. Only forward
    edges (consumer after producer) are added so independent work — and
    additive edits to shared infra, which the same-file pass already
    handles — stay parallel.

    Returns the list of (producer_idx, consumer_idx, file) edges added so the
    caller can observe them. Additive only — merged with existing deps.
    """
    added: List[tuple] = []
    for consumer_idx, consumer in enumerate(subtasks):
        prompt = consumer.get("prompt", "") or ""
        if not prompt:
            continue
        consumer_files = set(consumer.get("related_files", []))
        for producer_idx in range(consumer_idx):
            producer = subtasks[producer_idx]
            for f in producer.get("related_files", []):
                # Same-file case is already handled by overlap injection.
                if f in consumer_files:
                    continue
                # Bounded match so a short path (a.py) does not match inside a
                # longer token (data.py); the consumer must reference the
                # producer's file as a standalone path.
                if re.search(rf"(?<![\w/.]){re.escape(f)}(?![\w])", prompt):
                    existing = consumer.setdefault("depends_on_indices", [])
                    if producer_idx not in existing:
                        existing.append(producer_idx)
                        added.append((producer_idx, consumer_idx, f))
                        logger.info(
                            f"[{label}] Build-on edge: "
                            f"{producer.get('title', f'#{producer_idx}')!r} → "
                            f"{consumer.get('title', f'#{consumer_idx}')!r} "
                            f"(consumer prompt consumes producer file: {f})"
                        )
                    break  # one edge per producer/consumer pair is enough
    return added


def _resolve_injected_cycles(
    subtasks: List[Dict], explicit_deps: Dict[int, List[int]], label: str = "PLAN"
) -> List[Dict]:
    """Break dependency cycles by dropping only the INJECTED conflict-prevention
    edges that close a cycle, preserving explicit declarations and as many
    injected serialization edges as possible.

    The old behaviour wholesale-discarded EVERY injected edge (same-file
    chaining + build-on) the moment any combination cycled, so unrelated
    same-base siblings lost their serialization and could dispatch in parallel
    and conflict at merge. Instead, we rebuild the graph on top of the explicit
    declarations and greedily re-add each injected edge; an injected edge is
    dropped only if adding it would (re)introduce a cycle. Explicit edges are
    never touched (they are the agent's own declaration, not conflict
    prevention).

    Returns a list of dropped-edge records (successor/predecessor indices and
    titles) so the caller can record WHICH edges were removed and WHY, beyond a
    bare error log.
    """
    # Injected edges = whatever the injection passes added on top of explicit.
    injected: List[tuple] = []
    for successor, st in enumerate(subtasks):
        explicit = set(explicit_deps.get(successor, []))
        for predecessor in st.get("depends_on_indices", []):
            if predecessor not in explicit:
                injected.append((successor, predecessor))

    # Reset to the explicit-only baseline, then greedily re-add injected edges.
    for i, st in enumerate(subtasks):
        if explicit_deps.get(i):
            st["depends_on_indices"] = list(explicit_deps[i])
        else:
            st.pop("depends_on_indices", None)

    dropped: List[Dict] = []
    for successor, predecessor in injected:
        existing = subtasks[successor].setdefault("depends_on_indices", [])
        if predecessor in existing:
            continue
        existing.append(predecessor)
        if _validate_plan_dependencies(subtasks):
            continue
        # This injected edge closes a cycle — drop it and record why.
        existing.remove(predecessor)
        if not existing:
            subtasks[successor].pop("depends_on_indices", None)
        record = {
            "successor_index": successor,
            "predecessor_index": predecessor,
            "successor": subtasks[successor].get("title", f"#{successor}"),
            "predecessor": subtasks[predecessor].get("title", f"#{predecessor}"),
        }
        dropped.append(record)
        logger.warning(
            f"[{label}] Dropped conflict-prevention edge "
            f"{record['predecessor']!r} → {record['successor']!r}: it would "
            f"close a dependency cycle with the plan's explicit declarations."
        )
    return dropped


def _break_dependency_cycles(items: List[Dict], label: str = "PLAN") -> List[Dict]:
    """Minimally break dependency cycles in declared ``depends_on_indices`` edges.

    Unlike :func:`_resolve_injected_cycles` (which distinguishes injected
    conflict-prevention edges from explicit declarations), this treats EVERY
    declared edge as a candidate and drops only the smallest set needed to make
    the graph acyclic: each edge is greedily re-added on top of an empty
    baseline and dropped only if it would close a cycle. Valid prerequisite
    chains elsewhere in the batch survive intact instead of being collapsed by
    one bad edge.

    Mutates ``items`` in place and returns a list of dropped-edge records
    (successor/predecessor indices and titles) so the caller can record WHICH
    edges were removed and WHY.
    """
    # Snapshot declared edges, then reset to an empty baseline.
    declared: List[tuple] = []
    for successor, item in enumerate(items):
        for predecessor in item.get("depends_on_indices", []) or []:
            declared.append((successor, predecessor))
        item.pop("depends_on_indices", None)

    dropped: List[Dict] = []
    for successor, predecessor in declared:
        existing = items[successor].setdefault("depends_on_indices", [])
        if predecessor in existing:
            continue
        existing.append(predecessor)
        if _validate_plan_dependencies(items):
            continue
        # This edge closes a cycle — drop it and record why.
        existing.remove(predecessor)
        if not existing:
            items[successor].pop("depends_on_indices", None)
        record = {
            "successor_index": successor,
            "predecessor_index": predecessor,
            "successor": items[successor].get("title", f"#{successor}"),
            "predecessor": items[predecessor].get("title", f"#{predecessor}"),
        }
        dropped.append(record)
        logger.warning(
            f"[{label}] Dropped dependency edge "
            f"{record['predecessor']!r} → {record['successor']!r}: it would "
            f"close a dependency cycle; other declared edges preserved."
        )
    return dropped


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.task_completion import _ensure_branch

    project = await session.get(Project, task.project_id)
    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        # Affirmative plan no-op (mirrors the #7021 `./done --already-done`
        # principle one layer up): a plan agent that VERIFIED there is genuinely
        # nothing to decompose (work already shipped / not actionable) emits the
        # sentinel ``{"subtasks": [], "no_op_reason": "<reason>"}``. This is a
        # clean terminal, NOT a failure — so skip the CODE_ERROR classification,
        # the failure alert, and the direction decomp-quality ding.
        #
        # GUARDRAIL (load-bearing): the affirmation MUST be a >=30-char
        # ``no_op_reason`` carried in a parsed plan object. A crashed / timed-out
        # / malformed agent produces no plan_data (parser returns None) or no
        # such reason, so it still falls through to the FAILED branch below.
        # The >=30-char floor prevents a trivially auto-filled empty affirmation.
        _no_op_reason = None
        if isinstance(plan_data, dict):
            _raw_reason = plan_data.get("no_op_reason")
            if isinstance(_raw_reason, str) and len(_raw_reason.strip()) >= 30:
                _no_op_reason = _raw_reason.strip()[:2000]
        if _no_op_reason is not None:
            from app.failure_classification import SCOPE_EMPTY, persist_failure_category

            # Land CANCELLED (matching the SCOPE_EMPTY clean-no-op path in
            # agent_status.py): a deterministic "nothing to build" result that
            # must not be retried, alerted on, or counted as a decomposition
            # failure. CANCELLED plans are excluded from the direction
            # decomposition-quality rollup by construction (it filters on
            # COMPLETED/FAILED), so this also satisfies the no-decomp-ding goal.
            ctx = dict(task.context or {})
            ctx["no_op_reason"] = _no_op_reason
            task.context = ctx
            task.latest_status_message = (
                f"Plan no-op — nothing to decompose: {_no_op_reason}"
            )[:500]
            task.status = TaskStatus.CANCELLED
            persist_failure_category(task, SCOPE_EMPTY)
            session.add(task)
            await session.commit()
            logger.info(
                f"[PLAN] Affirmative no-op for plan {task.id} — CANCELLED "
                f"(reason: {_no_op_reason[:120]})"
            )
            return

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
        dropped_count = len(subtasks) - 10
        dropped_subtasks = subtasks[10:]
        dropped_titles = [st.get("title", f"#{10 + i}") for i, st in enumerate(dropped_subtasks)]

        # Check if any of the kept subtasks depend on a dropped subtask
        broken_deps = []
        for i in range(min(10, len(subtasks))):
            for dep_idx in subtasks[i].get("depends_on_indices", []):
                if dep_idx >= 10:
                    broken_deps.append({
                        "subtask_index": i,
                        "subtask_title": subtasks[i].get("title", f"#{i}"),
                        "broken_dep_index": dep_idx,
                        "broken_dep_title": (
                            subtasks[dep_idx].get("title", f"#{dep_idx}")
                            if dep_idx < len(subtasks)
                            else f"#{dep_idx}"
                        ),
                    })

        task.latest_status_message = (
            f"Plan exceeds 10-subtask limit: {len(subtasks)} subtasks provided, "
            f"dropped {dropped_count} (indices 10-{len(subtasks)-1}). "
            f"Kept subtasks have {len(broken_deps)} unsatisfiable "
            f"dependency/dependencies on dropped subtasks."
        )[:500]
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
        ctx = dict(task.context or {})
        ctx["subtask_cap_exceeded"] = {
            "total_subtasks": len(subtasks),
            "limit": 10,
            "dropped_count": dropped_count,
            "dropped_indices": list(range(10, len(subtasks))),
            "dropped_titles": dropped_titles,
            "broken_dependencies": broken_deps,
        }
        task.context = ctx
        session.add(task)
        await _maybe_create_failure_alert(
            task, session, task.latest_status_message
        )
        await session.commit()
        logger.error(
            "[PLAN] Plan %s failed — decomposition exceeded 10-subtask cap: "
            "%d subtasks provided, dropped %d. Kept subtasks have %d unsatisfiable "
            "dependencies on dropped subtasks.",
            task.id,
            len(subtasks),
            dropped_count,
            len(broken_deps),
        )
        return

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

    # plan_data is always {"subtasks": [...]} — a dict, not a list.  query_findings_index
    # (research_completion.py) only processes list-shaped plan_output, so plan tasks are
    # correctly excluded from the Findings Journal research feed.
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

    # Pre-validate: identify which subtasks will be skipped and check for unsatisfiable dependencies.
    # Do this before creating any child tasks so we can fail cleanly if a spawned subtask
    # depends on a skipped (non-eligible) producer.
    # A plan child is eligible if it is a PR-producing implementation subtype OR a
    # knowledge-prerequisite subtype (web_research / design). The latter spawns AS
    # ITSELF (no branch/PR) so a downstream implementation child can validly depend
    # on the knowledge it produces.
    plan_child_allowlist = PLAN_SUBTASK_TYPES | PLAN_KNOWLEDGE_SUBTASK_TYPES
    skipped_indices = set()
    for i, subtask_def in enumerate(subtasks):
        raw_type = subtask_def.get("type", "create")
        if raw_type not in plan_child_allowlist:
            skipped_indices.add(i)

    for i, subtask_def in enumerate(subtasks):
        if i in skipped_indices:
            continue
        dep_indices = subtask_def.get("depends_on_indices", [])
        for dep_idx in dep_indices:
            if 0 <= dep_idx < len(subtasks) and dep_idx in skipped_indices:
                skipped_title = subtasks[dep_idx].get("title", f"#{dep_idx}")
                skipped_type = subtasks[dep_idx].get("type", "create")
                dependent_title = subtask_def.get("title", f"#{i}")
                task.latest_status_message = (
                    f"Plan has unsatisfiable dependency: {dependent_title!r} "
                    f"depends on {skipped_title!r} (type {skipped_type!r}, "
                    f"not a registered plan-spawnable task type)"
                )[:500]
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
                    task, session, task.latest_status_message
                )
                await session.commit()
                logger.error(
                    "[PLAN] Plan %s failed — dependent %r references skipped producer %r",
                    task.id,
                    dependent_title,
                    skipped_title,
                )
                return

    child_tasks = []
    for i, subtask_def in enumerate(subtasks):
        # Gate the child type on the registry-derived plan-subtask allowlist
        # rather than a hardcoded literal. A requested type outside that set is
        # SKIPPED with an explicit log — never silently rewritten to "create" — so
        # a plan that asks for a non-eligible child surfaces the misconfiguration
        # instead of masquerading as a successfully-spawned create. A ``None``
        # placeholder keeps ``child_tasks`` index-aligned with ``subtasks`` for the
        # downstream dependency-injection pass.
        if i in skipped_indices:
            raw_type = subtask_def.get("type", "create")
            logger.warning(
                "[PLAN] Skipping subtask %r with non-eligible type %r "
                "(not a registered plan-spawnable write or knowledge task type) in plan %s",
                subtask_def.get("title", "Untitled subtask"),
                raw_type,
                task.id,
            )
            child_tasks.append(None)
            continue
        subtask_type = subtask_def.get("type", "create")
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
        # The "create" prompt suffix primes code/PR work; a knowledge-prerequisite
        # child (web_research / design) produces a knowledge document, not a branch,
        # so it must NOT receive the implementation suffix.
        create_suffix = (
            _runtime_config.get("agent_prompt_suffixes", {}).get("create")
            if subtask_type in PLAN_SUBTASK_TYPES
            else None
        )
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
        # Ignore subtask_def.model and parent plan model — legacy hints
        # that anchored every child to one provider regardless of the
        # operator's category_routing. Respect operator-level
        # task_type_models pins only; orchestrator resolves per child
        # from routing otherwise.
        subtask_model = _runtime_config.get("task_type_models", {}).get(subtask_type)
        # Forward-carry a plan-time acceptance_evidence declaration onto the
        # child payload so the requirement survives decomposition. Pre-validate
        # leniently here: an LLM-generated declaration is untrusted, and a
        # malformed one passed straight into the strict validate_internal_payload
        # below would raise ValueError, aborting the whole spawn loop and rolling
        # back EVERY child (recovery re-parses the same plan_output, so it fails
        # deterministically and the decomposition is lost permanently). Degrade a
        # malformed declaration to 'no declaration' (key omitted) instead.
        subtask_acceptance_evidence = subtask_def.get("acceptance_evidence")
        if subtask_acceptance_evidence is not None and (
            read_acceptance_evidence(
                {"acceptance_evidence": subtask_acceptance_evidence}
            )
            is None
        ):
            subtask_acceptance_evidence = None
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
                        **(
                            {"acceptance_evidence": subtask_acceptance_evidence}
                            if subtask_acceptance_evidence
                            else {}
                        ),
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

    # Flush to assign PKs without committing — children are not yet visible
    # to the dispatcher because the transaction is still open.
    await session.flush()

    # Inject serial dependencies for subtasks that share files (additive only).
    # Save original (explicit) deps so we can restore them if injection creates a cycle.
    original_deps = {
        i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)
    }
    _inject_file_overlap_deps(subtasks, "PLAN")
    # Close the cross-file build-on hole: a subtask consuming a NEW module a
    # sibling creates shares no file with it, so the same-file pass above adds
    # no edge. Order the consumer after the producer it references.
    _inject_build_on_deps(subtasks, "PLAN")

    if not _validate_plan_dependencies(subtasks):
        # A cycle formed once injected serialization edges combined with the
        # plan's explicit declarations. Remove only the minimal injected edges
        # that close a cycle — keep the rest of the conflict-prevention
        # serialization rather than wholesale-discarding all of it.
        dropped_edges = _resolve_injected_cycles(subtasks, original_deps, "PLAN")
        logger.error(
            "[PLAN] Circular dependency after dependency injection — dropped "
            f"{len(dropped_edges)} conflict-prevention edge(s) to restore "
            "acyclicity, preserved the remaining serialization"
        )
        ctx = dict(task.context or {})
        ctx["dropped_serialization_edges"] = dropped_edges
        task.context = ctx
        session.add(task)

    for i, subtask_def in enumerate(subtasks):
        if child_tasks[i] is None:
            continue
        dep_indices = subtask_def.get("depends_on_indices", [])
        dep_ids = [
            str(child_tasks[j].id)
            for j in dep_indices
            if 0 <= j < len(child_tasks) and child_tasks[j] is not None
        ]
        child_tasks[i].depends_on = dep_ids
        if dep_ids:
            remember_structural_depends_on(child_tasks[i], dep_ids)
        session.add(child_tasks[i])

    _log_file_overlaps(subtasks, "PLAN")

    for ct in child_tasks:
        if ct is None:
            continue
        history = TaskHistory(
            task_id=ct.id,
            action="SPAWNED",
            details={
                "spawned_by": "PLAN_DECOMPOSITION",
                "parent_plan_id": str(task.id),
            },
        )
        session.add(history)

    # Single atomic commit: children, their depends_on, and history records
    # all land together so the dispatcher never sees QUEUED children with
    # empty depends_on (the window that caused parallel dispatch instead of
    # the plan-specified serial order).
    await session.commit()

    for ct in child_tasks:
        if ct is None:
            continue
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
        )

    spawned_count = sum(1 for ct in child_tasks if ct is not None)
    logger.info(f"[PLAN] Created {spawned_count} subtasks from plan {task.id}")
