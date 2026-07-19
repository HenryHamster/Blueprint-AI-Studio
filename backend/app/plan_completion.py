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
from app.json_extract import _extract_json_object
from app.capability_registry import (
    off_registry_capability_refs,
    registry_capability_names,
)
from app.capability_execution_fence import stamp_capability_requirements
from app.compute_experiment_contract import (
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY,
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY,
    _author_compute_launch_contract,
    _stamp_container_image_pin_status,
    _validate_compute_launch_contract,
    resolve_compute_spend_approval,
)
from app.depends_on_provenance import remember_structural_depends_on
from app.finding_deduplication import normalize_prerequisite, resolve_prerequisite_task
from app.models import Project, Task, TaskHistory, TaskStatus
from app.payload_schemas import (
    parse_acceptance_evidence,
    read_acceptance_evidence,
    validate_internal_payload,
)
from app.task_capabilities import PLAN_KNOWLEDGE_SUBTASK_TYPES, PLAN_SUBTASK_TYPES
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
    scope_to_effort,
)

logger = logging.getLogger(__name__)

# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# Maps mislabeled fresh-work type synonyms (LLM conventional-commit prefixes that
# are not plan-spawnable) to the nearest eligible plan-spawnable type.  Applied
# in-place before the skip gate in _handle_plan_completion so a subtask typed
# "fix" or "feat" survives decomposition without widening PLAN_SUBTASK_TYPES.
# Only types NOT already in the plan-child allowlist need an entry here.
PLAN_SUBTASK_TYPE_COERCIONS: dict[str, str] = {
    "fix": "create",
    "bug": "create",
    "feature": "create",
    "feat": "create",
    "improvement": "create",
    "perf": "create",
    "test": "qa",
}


def _parse_plan_output(text: str) -> Dict[str, Any] | None:
    """Extract structured plan JSON from agent output text.

    Uses string-aware bracket counting so braces inside JSON string values
    (e.g. code snippets in prompts) don't break extraction.
    """
    text = text[:500_000]
    try:
        candidate = _extract_json_object(text, "subtasks")
        if candidate is None:
            return None
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


def _distribute_parent_acceptance_evidence(
    subtasks: List[Dict], skipped_indices: set, seam_path: str | None
) -> set:
    """Pick the child subtask indices that should inherit the parent plan task's
    declared acceptance evidence when they do not declare their own.

    "Deliver the seam" policy: a finding's declared acceptance evidence must
    survive one decomposition hop STRUCTURALLY, but blindly copying one named
    test onto every child would make every unrelated child fail its gate. So:

      * If the declaration names a concrete path (``seam_path`` — a test file or
        selector), target the kept subtasks whose ``related_files`` or prompt
        reference that path — the children that actually deliver the seam.
      * Otherwise (behavior/artifact evidence with no path, or no subtask
        references the path), fall back to the TERMINAL children — kept subtasks
        no other kept subtask depends on. These are the integrating sinks; the
        requirement lands on the work that ties the decomposition together
        rather than being smeared across every leaf.

    Returns the set of target subtask indices (subset of the kept indices).
    """
    kept = [i for i in range(len(subtasks)) if i not in skipped_indices]
    if not kept:
        return set()

    if seam_path:
        path_part = seam_path.partition("::")[0].strip()
        if path_part:
            matched = {
                i
                for i in kept
                if path_part in (subtasks[i].get("related_files", []) or [])
                or path_part in (subtasks[i].get("prompt", "") or "")
            }
            if matched:
                return matched

    # Fallback: terminal children (sinks) — kept subtasks no other kept subtask
    # depends on. With no dependency structure every kept subtask is terminal.
    depended_on: set = set()
    for i in kept:
        for dep in subtasks[i].get("depends_on_indices", []) or []:
            depended_on.add(dep)
    sinks = {i for i in kept if i not in depended_on}
    return sinks or set(kept)


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.task_completion import _ensure_branch

    project = await session.get(Project, task.project_id)

    # Default a child compute_experiment's container_image to the project's
    # declared, digest-pinned baked image. Shared with the dispatch path via
    # resolve_project_default_container_image so the two cannot drift (fail-open).
    from app.compute_backends import resolve_project_default_container_image
    _project_default_container_image = await resolve_project_default_container_image(
        session, project
    )

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
            # GREENFIELD GUARD: a plan that cites emptiness on a greenfield project
            # silently abandons onboarding — no foundation is built and nothing
            # retries.  Reject with FAILED so the failure is visible and retried
            # rather than accepted as a clean CANCELLED no-op.  Only fires when the
            # durable signal is authoritatively True (is True check); absent or False
            # falls through to the normal CANCELLED path below.
            if (project.build_config or {}).get("repo_is_empty") is True:
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )
                from app.routers.agent_status import record_task_result

                _gf_msg = (
                    "Plan emitted emptiness no-op on a greenfield repo "
                    "— foundation must be built"
                )
                task.latest_status_message = _gf_msg[:500]
                task.status = TaskStatus.FAILED
                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(
                    success=False,
                    failure_class=_fc,
                    task_id=str(task.id),
                    task_type=task.first_payload.get("type", ""),
                )
                ctx = dict(task.context or {})
                ctx["greenfield_no_op_rejected"] = True
                ctx["no_op_reason"] = _no_op_reason
                task.context = ctx
                session.add(task)
                await _maybe_create_failure_alert(task, session, _gf_msg)
                await session.commit()
                logger.error(
                    "[PLAN] Greenfield no-op rejected for plan %s — FAILED "
                    "(reason: %s)",
                    task.id,
                    _no_op_reason[:120],
                )
                return

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
        dropped_titles = [
            st.get("title", f"#{10 + i}") for i, st in enumerate(dropped_subtasks)
        ]

        # Check if any of the kept subtasks depend on a dropped subtask
        broken_deps = []
        for i in range(min(10, len(subtasks))):
            for dep_idx in subtasks[i].get("depends_on_indices", []):
                if dep_idx >= 10:
                    broken_deps.append(
                        {
                            "subtask_index": i,
                            "subtask_title": subtasks[i].get("title", f"#{i}"),
                            "broken_dep_index": dep_idx,
                            "broken_dep_title": (
                                subtasks[dep_idx].get("title", f"#{dep_idx}")
                                if dep_idx < len(subtasks)
                                else f"#{dep_idx}"
                            ),
                        }
                    )

        task.latest_status_message = (
            f"Plan exceeds 10-subtask limit: {len(subtasks)} subtasks provided, "
            f"dropped {dropped_count} (indices 10-{len(subtasks) - 1}). "
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
        await _maybe_create_failure_alert(task, session, task.latest_status_message)
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
    # When this task is a requeue-clone that already opened a rollup PR, carry
    # the original staging branch forward instead of deriving a new plan/<id>
    # branch from the clone's task ID.  Reusing the branch keeps the merge
    # guard invariant (reviewed-branch == PR-head) intact: the PR head still
    # points at the original branch, so the merge guard won't abort with a
    # branch-mismatch error on every subsequent iteration.
    # Guard: only reuse when BOTH plan_branch AND rollup_pr_number are present
    # — rollup_pr_number confirms a PR was actually opened (not just a
    # branch-setup step that failed before the PR was created).
    _ctx = task.context or {}
    _inherited_plan_branch = _ctx.get("plan_branch")
    _has_rollup_pr = bool(_ctx.get("rollup_pr_number"))
    _adopted_foreign_branch = False
    if _inherited_plan_branch and _has_rollup_pr:
        plan_branch = _inherited_plan_branch
        # This requeue-clone adopts an ancestor-named (foreign) staging branch
        # to keep the merge-guard invariant intact. Stamp a DURABLE ownership
        # flag so the rollup OWNER-gate lets this task carry the branch to base
        # even though the name doesn't match its own id-prefix (invariant #2).
        _adopted_foreign_branch = True
    else:
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
        **({"owns_rollup_branch": True} if _adopted_foreign_branch else {}),
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
    parent_parent_research_task_id = (task.context or {}).get("parent_research_task_id")
    parent_finding_index = (task.context or {}).get("finding_index")
    parent_finding_hash = (task.context or {}).get("finding_hash")
    parent_finding_challenge = (task.context or {}).get("finding_challenge")

    # Resolve criterion_ids from the parent direction so plan-spawned children
    # inherit them — same pattern as research_completion.py's
    # _direction_criterion_cache.  Best-effort: if the direction row is missing
    # or has no active keys, children get an empty list and
    # record_task_merge_evidence falls back to direction lookup at merge time.
    _plan_criterion_ids: list[str] = []
    from app.acceptance_criteria import (
        direction_criterion_release_version_id as _direction_criterion_version,
        resolve_task_criterion_release_version_id as _resolve_criterion_version,
        task_criterion_release_version_id as _task_criterion_version,
    )

    _plan_criterion_release_version_id = _task_criterion_version(task)
    _plan_version_was_explicit = _plan_criterion_release_version_id is not None
    if _plan_criterion_release_version_id is None:
        _plan_criterion_release_version_id = await _resolve_criterion_version(
            session, task, task.project_id
        )
    if parent_direction_id:
        try:
            from app.directions_repo import get_direction_row as _get_dir_row

            _dir_row = await _get_dir_row(session, task.project_id, parent_direction_id)
            if (
                _dir_row is not None
                and _dir_row.criterion_keys
                and (
                    _direction_criterion_version(_dir_row)
                    == _plan_criterion_release_version_id
                    or (
                        not _plan_version_was_explicit
                        and _direction_criterion_version(_dir_row) is None
                    )
                )
            ):
                from app.acceptance_criteria import (
                    filter_active_criterion_keys as _filter_active,
                )
                if _plan_criterion_release_version_id is not None:
                    _cached_ids = await _filter_active(
                        session,
                        task.project_id,
                        list(_dir_row.criterion_keys),
                        release_version_id=_plan_criterion_release_version_id,
                    )
                    if _cached_ids:
                        _plan_criterion_ids = _cached_ids
        except Exception:
            logger.debug(
                "[PLAN] criterion_keys lookup failed for direction %s",
                parent_direction_id,
            )

    # Pre-validate: identify which subtasks will be skipped and check for unsatisfiable dependencies.
    # Do this before creating any child tasks so we can fail cleanly if a spawned subtask
    # depends on a skipped (non-eligible) producer.
    # A plan child is eligible if it is a PR-producing implementation subtype OR a
    # knowledge-prerequisite subtype (web_research / design / compute_experiment).
    # The latter spawns AS ITSELF (no branch/PR) so a downstream implementation
    # child can validly depend on the knowledge it produces.
    plan_child_allowlist = PLAN_SUBTASK_TYPES | PLAN_KNOWLEDGE_SUBTASK_TYPES
    # Coerce mislabeled fresh-work type synonyms before the skip gate so a
    # subtask typed "fix" or "feat" survives without widening the allowlist.
    type_coercions: dict[int, tuple[str, str]] = {}
    for i, subtask_def in enumerate(subtasks):
        raw_type = subtask_def.get("type", "create")
        if (
            raw_type not in plan_child_allowlist
            and raw_type in PLAN_SUBTASK_TYPE_COERCIONS
        ):
            target = PLAN_SUBTASK_TYPE_COERCIONS[raw_type]
            subtask_def["type"] = target
            type_coercions[i] = (raw_type, target)
            logger.info(
                "[PLAN] Coerced subtask %r type %r → %r in plan %s",
                subtask_def.get("title", f"#{i}"),
                raw_type,
                target,
                task.id,
            )
    skipped_indices = set()
    skipped_reasons: dict[int, str] = {}
    for i, subtask_def in enumerate(subtasks):
        raw_type = subtask_def.get("type", "create")
        if raw_type not in plan_child_allowlist:
            skipped_indices.add(i)
            skipped_reasons[i] = "not a registered plan-spawnable task type"
            continue
        if raw_type == "compute_experiment":
            compute_contract = _author_compute_launch_contract(subtask_def)
            if not compute_contract.get("container_image") and _project_default_container_image:
                compute_contract["container_image"] = _project_default_container_image
            contract_ok, contract_reason = _validate_compute_launch_contract(
                compute_contract
            )
            if not contract_ok:
                skipped_indices.add(i)
                skipped_reasons[i] = (
                    f"invalid compute_experiment launch contract ({contract_reason})"
                )

    # Transitive cascade-skip: propagate skips to dependents whose declared
    # producer was skipped.  Repeat until stable so multi-hop chains are
    # fully resolved.  Only fail outright when nothing spawnable remains;
    # when unrelated siblings exist they are still spawned.
    cascade_changed = True
    cascade_skipped: dict[int, tuple[str, int]] = {}
    while cascade_changed:
        cascade_changed = False
        for i, subtask_def in enumerate(subtasks):
            if i in skipped_indices:
                continue
            for dep_idx in subtask_def.get("depends_on_indices", []):
                if 0 <= dep_idx < len(subtasks) and dep_idx in skipped_indices:
                    dep_title = subtasks[dep_idx].get("title", f"#{dep_idx}")
                    skipped_indices.add(i)
                    skipped_reasons[i] = (
                        f"cascade-skipped: depends on skipped subtask {dep_title!r}"
                    )
                    cascade_skipped[i] = (subtask_def.get("title", f"#{i}"), dep_idx)
                    cascade_changed = True
                    break

    if cascade_skipped:
        ctx = dict(task.context or {})
        ctx["plan_subtask_cascade_skipped"] = [
            {"index": i, "title": title, "skipped_dep_index": dep_idx}
            for i, (title, dep_idx) in cascade_skipped.items()
        ]
        task.context = ctx
        session.add(task)

    if len(skipped_indices) == len(subtasks):
        if cascade_skipped:
            first_cs_dep = next(iter(cascade_skipped.values()))[1]
            root_title = subtasks[first_cs_dep].get("title", f"#{first_cs_dep}")
            root_reason = skipped_reasons.get(
                first_cs_dep, "not a registered plan-spawnable task type"
            )
            msg = (
                f"Plan has unsatisfiable dependency: all subtasks skipped "
                f"(root: {root_title!r} — {root_reason})"
            )
        else:
            msg = "Plan has no spawnable subtasks: all subtask types are ineligible"
        task.latest_status_message = msg[:500]
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
        await _maybe_create_failure_alert(task, session, task.latest_status_message)
        await session.commit()
        logger.error(
            "[PLAN] Plan %s failed — no spawnable subtasks after cascade-skip "
            "(%d of %d total skipped)",
            task.id,
            len(skipped_indices),
            len(subtasks),
        )
        return

    # Acceptance-evidence forward-carry through decomposition (parent → child).
    # A finding routed through the plan agent carries its declared acceptance
    # evidence on the PLAN task in two channels: the payload (AcceptanceEvidence
    # vocabulary, consumed by the merge gate) and the context
    # (PlanAcceptanceEvidence vocabulary, the proof the agent renders). The plan
    # agent MAY re-declare per-subtask evidence; when it does NOT, the parent's
    # declaration must still survive this one decomposition hop. Read both
    # channels once here, then distribute to the children that deliver the
    # declared seam. Fail-safe: an absent/malformed parent declaration degrades
    # to None (the helpers never raise) and the children are unchanged from today.
    _parent_payload_ev_obj = read_acceptance_evidence(parent_payload)
    _parent_payload_evidence = (
        _parent_payload_ev_obj.model_dump(exclude_none=True)
        if _parent_payload_ev_obj is not None
        else None
    )
    _parent_context_evidence = parse_acceptance_evidence(
        (task.context or {}).get("acceptance_evidence")
    )
    # The seam path the declaration points at (payload test_path preferred, then
    # the context selector) drives which children are judged to deliver it.
    _seam_path = None
    if _parent_payload_ev_obj is not None and _parent_payload_ev_obj.test_path:
        _seam_path = _parent_payload_ev_obj.test_path
    elif _parent_context_evidence is not None:
        _seam_path = _parent_context_evidence.get("selector")
    _parent_evidence_targets = (
        _distribute_parent_acceptance_evidence(subtasks, skipped_indices, _seam_path)
        if (_parent_payload_evidence or _parent_context_evidence)
        else set()
    )

    # Strategist-curated capability vocabulary for soft, fail-open off-registry
    # flagging of a subtask's declared prerequisite / provides_capability refs.
    _plan_registry_names = registry_capability_names(
        getattr(project, "problem_model", None)
    )

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
            skip_reason = skipped_reasons.get(
                i, "not a registered plan-spawnable task type"
            )
            logger.warning(
                "[PLAN] Skipping subtask %r with type %r in plan %s: %s",
                subtask_def.get("title", "Untitled subtask"),
                raw_type,
                task.id,
                skip_reason,
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
        # Optional plan-time acceptance-evidence contract: propagate it onto the
        # child context the same way scope/risk/related_files are. Validated via
        # the shared helper, so an absent or malformed declaration yields None and
        # the child context is byte-for-byte unchanged from today.
        child_acceptance_evidence = parse_acceptance_evidence(
            subtask_def.get("acceptance_evidence")
        )
        # Inherit the parent's context-channel declaration onto a child that
        # delivers the declared seam but did NOT self-declare. A subtask that
        # declared its own evidence is never overwritten.
        if (
            child_acceptance_evidence is None
            and _parent_context_evidence is not None
            and i in _parent_evidence_targets
        ):
            child_acceptance_evidence = _parent_context_evidence
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
        # Inherit the parent's payload-channel declaration onto a child that
        # delivers the declared seam but did NOT self-declare its own — so the
        # merge gate still has the parent finding's proof to check.
        if (
            subtask_acceptance_evidence is None
            and _parent_payload_evidence is not None
            and i in _parent_evidence_targets
        ):
            subtask_acceptance_evidence = _parent_payload_evidence
        # Shared-vocabulary capability declarations: carry a subtask's declared
        # provides_capability stamp and normalize its prerequisite onto the child
        # context exactly as research_completion does for findings, so a plan
        # subtask participates in the same cross-lane capability framework. The
        # floor marker (derive_provider_marker) still rides alongside these via
        # compose_capability_markers — they coexist, never overwrite. Off-registry
        # refs are flagged advisory (never dropped); matchers stay exact.
        _child_provides = subtask_def.get("provides_capability") or subtask_def.get(
            "capability"
        )
        _child_provides = (
            _child_provides.strip()
            if isinstance(_child_provides, str) and _child_provides.strip()
            else None
        )
        _child_prereq = normalize_prerequisite(
            subtask_def.get("prerequisite") or subtask_def.get("requires")
        )
        _child_cap_refs: List[str] = []
        if _child_provides:
            _child_cap_refs.append(_child_provides)
        if _child_prereq is not None and _child_prereq.get("kind") == "capability":
            _child_cap_refs.append(_child_prereq["ref"])
        _child_off_registry = off_registry_capability_refs(
            _child_cap_refs, _plan_registry_names
        )
        if subtask_type == "compute_experiment":
            compute_contract = _author_compute_launch_contract(subtask_def)
            if not compute_contract.get("container_image") and _project_default_container_image:
                compute_contract["container_image"] = _project_default_container_image
            _stamp_container_image_pin_status(compute_contract, task)
            child_payload = validate_internal_payload(
                {
                    "type": "compute_experiment",
                    "command": compute_contract["command"],
                    "spend_cap_usd": float(compute_contract["spend_cap_usd"]),
                    "deadline_seconds": int(compute_contract["deadline_seconds"]),
                    "base_branch": plan_branch,
                    **(
                        {"success_criterion": compute_contract["success_criterion"]}
                        if compute_contract.get("success_criterion") is not None
                        else {}
                    ),
                    **{
                        key: compute_contract[key]
                        for key in (
                            "machine_type",
                            "experiment",
                            "benchmark",
                            "metric",
                            "hypothesis",
                            "cwd",
                            "env",
                            "accelerator",
                            "provisioning_model",
                            "live_proof",
                            "live_proof_marker",
                            "live_release_proof",
                            "live_release_proof_marker",
                            "requires_live_release",
                            "require_durable_artifact",
                            "sweep_id",
                            "setup_step",
                            "no_progress_timeout_seconds",
                            "input_artifact_job_id",
                            "container_image",
                            "container_image_digest_pinned",
                            "container_registry_auth",
                            "declared_metric",
                            "arms",
                            "smoke_sample",
                        )
                        if compute_contract.get(key) is not None
                    },
                    **({"model": subtask_model} if subtask_model else {}),
                    **({"effort": subtask_effort} if subtask_effort else {}),
                    **(
                        {"acceptance_evidence": subtask_acceptance_evidence}
                        if subtask_acceptance_evidence
                        else {}
                    ),
                },
                "task_completion:plan_subtask:compute_experiment",
            )
        else:
            child_payload = validate_internal_payload(
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
        if subtask_type == "compute_experiment":
            _ce_direction = (
                next(
                    (
                        d
                        for d in (getattr(project, "directions", None) or [])
                        if d.get("id") == str(parent_direction_id)
                    ),
                    None,
                )
                if parent_direction_id
                else None
            )
            child_requires_approval, _ = resolve_compute_spend_approval(
                runtime_config=_runtime_config,
                direction=_ce_direction,
                spend_cap_usd=float(compute_contract["spend_cap_usd"]),
            )
        else:
            child_requires_approval = False
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            criterion_ids=_plan_criterion_ids,
            requires_approval=child_requires_approval,
            payloads=[child_payload],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_scope,
                "risk": child_risk,
                "related_files": related_files,
                "parent_context": plan_parent_context,
                **(
                    {"acceptance_evidence": child_acceptance_evidence}
                    if child_acceptance_evidence
                    else {}
                ),
                **(
                    {"approval_reason": child_approval_reason}
                    if child_approval_reason
                    else {}
                ),
                **(
                    {"direction_id": parent_direction_id} if parent_direction_id else {}
                ),
                **(
                    {
                        "criterion_release_version_id": str(
                            _plan_criterion_release_version_id
                        )
                    }
                    if _plan_criterion_release_version_id is not None
                    else {}
                ),
                **(
                    {"research_task_id": parent_research_task_id}
                    if parent_research_task_id
                    else {}
                ),
                **(
                    {"parent_research_task_id": parent_parent_research_task_id}
                    if parent_parent_research_task_id
                    else {}
                ),
                **(
                    {"finding_index": parent_finding_index}
                    if parent_finding_index is not None
                    else {}
                ),
                **(
                    {"finding_hash": parent_finding_hash} if parent_finding_hash else {}
                ),
                **(
                    {"finding_challenge": parent_finding_challenge}
                    if parent_finding_challenge
                    else {}
                ),
                **({"provides_capability": _child_provides} if _child_provides else {}),
                **({"prerequisite": _child_prereq} if _child_prereq else {}),
                **(
                    {"off_registry_capabilities": _child_off_registry}
                    if _child_off_registry
                    else {}
                ),
                **(
                    {
                        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY: (
                            COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY
                        )
                    }
                    if subtask_type == "compute_experiment"
                    else {}
                ),
                **({"hotfix": True} if parent_hotfix else {}),
                **(
                    {
                        "type_coercion": {
                            "original_type": type_coercions[i][0],
                            "coerced_type": type_coercions[i][1],
                        }
                    }
                    if i in type_coercions
                    else {}
                ),
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

    # Snapshot active project tasks for cross-tree base-branch inheritance.
    # Queried after flush so the freshly-created children (inside the open
    # transaction) are invisible to this query; we hold them in child_tasks.
    from sqlmodel import select as _select_tasks
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    # A prerequisite must never attach a newly spawned child to this plan (or an
    # enclosing plan): those parents remain EVALUATING until their children
    # finish, so such an edge would deadlock even though it is not a graph cycle.
    _plan_ancestor_ids = {task.id}
    _ancestor_id = task.parent_task_id
    while _ancestor_id is not None and _ancestor_id not in _plan_ancestor_ids:
        _plan_ancestor_ids.add(_ancestor_id)
        _ancestor = await session.get(Task, _ancestor_id)
        _ancestor_id = _ancestor.parent_task_id if _ancestor is not None else None

    _existing_prereq_candidates = list(
        (
            await session.execute(
                _select_tasks(Task).where(
                    Task.project_id == task.project_id,
                    Task.id.notin_(_plan_ancestor_ids),
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                            TaskStatus.PROVISIONING,
                            TaskStatus.BLOCKED,
                            TaskStatus.PARKED,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    def _maybe_inherit_base(dependent: Task, foundation: Task) -> None:
        """Inherit *foundation*'s base_branch when it is a genuine cross-tree
        code-dep of *dependent*, preventing the cross-base landed-signal wedge."""
        if not dependent.payloads:
            return
        from app.orchestrator_health import (
            _same_base_branch,
            is_genuine_code_dependency,
        )

        if is_genuine_code_dependency(dependent, foundation) and not _same_base_branch(
            dependent, foundation
        ):
            foundation_base = (foundation.first_payload or {}).get("base_branch")
            if foundation_base is None:
                return
            dependent.payloads[0]["base_branch"] = foundation_base
            _flag_modified(dependent, "payloads")
            session.add(dependent)
            logger.info(
                "[PLAN] Subtask '%s' cross-tree genuine code-dep: set "
                "base_branch to %r (foundation %s)",
                dependent.title,
                foundation_base,
                str(foundation.id),
            )

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
        # Inherit sibling foundation's base_branch for genuine cross-tree code deps.
        for j in dep_indices:
            if 0 <= j < len(child_tasks) and child_tasks[j] is not None:
                _maybe_inherit_base(child_tasks[i], child_tasks[j])
        session.add(child_tasks[i])

    # Explicit prerequisites are resolved before the atomic commit, matching the
    # research finding lifecycle: active/sibling task foundations become durable
    # dependency edges; working registry capabilities satisfy only when their
    # proof status permits it; everything else is parked with a structured need
    # for the normal blocked-need resolver to resume later.
    from app.blocked_need_resolution import _would_create_cycle
    from app.blocked_need_resolution import (
        completed_foundation_for_need,
        knowledge_substance_satisfies_need,
    )
    from app.capability_registry import (
        consumer_gate_proof_status,
        registry_satisfies_capability_need,
        task_backed_capability_need_blocked_by_proof_gate,
        working_capability_names,
    )
    from app.task_blocked import build_need, enter_blocked_state
    from app.knowledge_query import fetch_knowledge_substance_index
    from app.research_completion import (
        SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY,
        spawn_capability_gate_disposition,
    )

    _all_prereq_candidates = _existing_prereq_candidates + [
        ct for ct in child_tasks if ct is not None
    ]
    # The live candidate set above deliberately excludes terminal work, but a
    # task prerequisite may already be fulfilled by shipped completed work or a
    # durable knowledge document. Resolve those rungs now instead of needlessly
    # parking a runnable child until the blocked-need sweep runs.
    _completed_foundations = list(
        (
            await session.execute(
                _select_tasks(Task).where(
                    Task.project_id == task.project_id,
                    Task.status == TaskStatus.COMPLETED,
                )
            )
        )
        .scalars()
        .all()
    )
    _potential_ghost_parent_ids = {
        completed.parent_task_id
        for completed in _completed_foundations
        if completed.parent_task_id
        and (completed.context or {}).get("pr_merged") is True
    }
    _failed_parent_ids = set()
    if _potential_ghost_parent_ids:
        _potential_ghost_parents = (
            (
                await session.execute(
                    _select_tasks(Task).where(Task.id.in_(_potential_ghost_parent_ids))
                )
            )
            .scalars()
            .all()
        )
        _failed_parent_ids = {
            parent.id
            for parent in _potential_ghost_parents
            if parent.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            or (
                parent.status == TaskStatus.COMPLETED
                and not (parent.context or {}).get("pr_merged")
            )
        }
    _knowledge_substance_index = await fetch_knowledge_substance_index(
        session, task.project_id
    )
    _working_registry_caps = working_capability_names(
        getattr(project, "problem_model", None)
    )
    _working_registry_proof = consumer_gate_proof_status(
        getattr(project, "problem_model", None),
        getattr(project, "capability_proof_records", None),
    )
    _proof_gate_mode = str(
        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
    )
    for ct in child_tasks:
        if ct is None:
            continue
        _prereq = (ct.context or {}).get("prerequisite")
        if not isinstance(_prereq, dict):
            continue
        foundation = resolve_prerequisite_task(_prereq, _all_prereq_candidates)
        if foundation is not None and foundation.id != ct.id:
            if _would_create_cycle(ct, foundation, _all_prereq_candidates):
                logger.warning(
                    "[PLAN] Subtask '%s' prerequisite %s:%s would create a "
                    "dependency cycle; leaving it unresolved",
                    ct.title,
                    _prereq.get("kind"),
                    _prereq.get("ref"),
                )
            else:
                _active_need = build_need(
                    _prereq.get("kind"), _prereq.get("ref"), label=_prereq.get("label")
                )
                # Durably record the capability prerequisite REGARDLESS of gate
                # disposition (block or wire-edge) so the shared execution-authority
                # fence re-grades this task-backed foundation at cold dispatch / warm
                # claim even after the gate graduates off/observe -> enforce. No-op
                # for a non-capability (task-kind) prereq. Survives generic retry
                # (key excluded from FRESH_ATTEMPT_CONTEXT_STRIP_KEYS).
                if _active_need:
                    stamp_capability_requirements(ct, [_active_need])
                    session.add(ct)
                if _active_need and task_backed_capability_need_blocked_by_proof_gate(
                    _active_need,
                    getattr(project, "capability_proof_records", None),
                    getattr(project, "problem_model", None),
                    mode=_proof_gate_mode,
                ):
                    # Enforce mode + unproven: the active foundation's capability is
                    # not yet proven, so the dependent must park BLOCKED instead of
                    # wiring a depends_on edge that would let it dispatch as soon as
                    # the foundation task completes (bypassing the proof gate).
                    _ctx = dict(ct.context or {})
                    _ctx["unresolved_prerequisite"] = [
                        *list(_ctx.get("unresolved_prerequisite") or []), _prereq
                    ]
                    ct.context = _ctx
                    enter_blocked_state(
                        ct,
                        waiting_on=f"foundation {_active_need['kind']}:{_active_need['ref']}",
                        reason=(
                            f"Plan subtask '{ct.title}' declared prerequisite "
                            f"{_active_need['kind']}:{_active_need['ref']} is not "
                            f"proven (capability proof gate enforced)."
                        ),
                        source="plan_prerequisite",
                        blocker_kind="upstream_dependency",
                        needs=[_active_need],
                    )
                    session.add(ct)
                    continue
                else:
                    foundation_id = str(foundation.id)
                    depends_on = list(ct.depends_on or [])
                    if foundation_id not in depends_on:
                        depends_on.append(foundation_id)
                        ct.depends_on = depends_on
                        remember_structural_depends_on(ct, foundation_id)
                    _maybe_inherit_base(ct, foundation)
                    session.add(ct)
                    continue

        need = build_need(
            _prereq.get("kind"), _prereq.get("ref"), label=_prereq.get("label")
        )
        if need is None:
            continue
        _completed_task_found = completed_foundation_for_need(
            ct, need, _completed_foundations, _failed_parent_ids
        )
        _knowledge_satisfies = knowledge_substance_satisfies_need(need, _knowledge_substance_index)
        _completed_gate_blocked = (
            _completed_task_found is not None
            and not _knowledge_satisfies
            and task_backed_capability_need_blocked_by_proof_gate(
                need,
                getattr(project, "capability_proof_records", None),
                getattr(project, "problem_model", None),
                mode=_proof_gate_mode,
            )
        )
        if (_completed_task_found is not None or _knowledge_satisfies) and not _completed_gate_blocked:
            context = dict(ct.context or {})
            context["_spawn_prereq_resolved"] = [
                *list(context.get("_spawn_prereq_resolved") or []),
                {"kind": need["kind"], "ref": need["ref"]},
            ]
            ct.context = context
            # Durably stamp a task-backed capability prerequisite even when it
            # resolved to an already-completed foundation, so the fence re-grades
            # it at cold dispatch / warm claim if the gate later enforces and the
            # foundation's platform proof is missing/revoked. No-op for task-kind.
            stamp_capability_requirements(ct, [need])
            session.add(ct)
            continue
        if registry_satisfies_capability_need(need, _working_registry_caps):
            _disposition, _proof_status = spawn_capability_gate_disposition(
                need, _working_registry_proof, mode=_proof_gate_mode
            )
            # Durably record this capability prerequisite REGARDLESS of
            # disposition so the shared execution-authority fence can re-grade it
            # at cold dispatch / warm claim (proof current at admission may expire
            # or be revoked before the subtask gains authority). Survives generic
            # retry (key excluded from FRESH_ATTEMPT_CONTEXT_STRIP_KEYS).
            stamp_capability_requirements(ct, [need])
            session.add(ct)
            if _disposition == "surface":
                context = dict(ct.context or {})
                context[SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY] = [
                    *(context.get(SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY) or []),
                    {
                        "kind": need["kind"],
                        "ref": need["ref"],
                        "proof_status": _proof_status,
                    },
                ]
                ct.context = context
                session.add(ct)
                continue
            elif _disposition == "ok":
                continue
            # _disposition == "block": fall through to enter_blocked_state below

        context = dict(ct.context or {})
        context["unresolved_prerequisite"] = [
            *list(context.get("unresolved_prerequisite") or []), _prereq
        ]
        ct.context = context
        enter_blocked_state(
            ct,
            waiting_on=f"foundation {need['kind']}:{need['ref']}",
            reason=(
                f"Plan subtask '{ct.title}' declared prerequisite "
                f"{need['kind']}:{need['ref']} not satisfied this cycle."
            ),
            source="plan_prerequisite",
            blocker_kind="upstream_dependency",
            needs=[need],
        )
        session.add(ct)

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
            {"task_id": str(ct.id), "status": ct.status},
        )

    spawned_count = sum(1 for ct in child_tasks if ct is not None)
    logger.info(f"[PLAN] Created {spawned_count} subtasks from plan {task.id}")
