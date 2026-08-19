"""Structural dependency inference for children of one research campaign."""

import logging
from typing import Dict, List

from sqlalchemy.orm.attributes import flag_modified

from app.depends_on_provenance import remember_structural_depends_on
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def wire_research_sibling_edges(
    child_tasks: List[Task],
    finding_to_child: Dict[int, int],
    overlaps: List[tuple],
    candidates: List[Task],
) -> List[tuple]:
    """Wire a serialization edge for every sibling pair that shares a file.

    A shared file is positive evidence of coupling — the later sibling's PR is
    otherwise built on a base that lacks the earlier sibling's change to that
    file, so the pair races at merge and the survivors break each other's
    contracts. There is deliberately no overlap-ratio threshold: even a single
    shared integration file (models.py, a shared schema) is the contract
    surface where parallel siblings collide. Genuinely disjoint siblings still
    run fully parallel.
    """

    from app.blocked_need_resolution import _would_create_cycle

    wired: List[tuple] = []
    for i, j, _shared_files in overlaps:
        child_i = finding_to_child.get(i)
        child_j = finding_to_child.get(j)
        if (
            child_i is None
            or child_j is None
            or child_i >= len(child_tasks)
            or child_j >= len(child_tasks)
        ):
            continue
        predecessor, successor = child_tasks[child_i], child_tasks[child_j]
        predecessor_related = (predecessor.context or {}).get("related_files", [])
        successor_related = (successor.context or {}).get("related_files", [])
        predecessor_files = (
            {
                value
                for value in predecessor_related
                if isinstance(value, str) and value
            }
            if isinstance(predecessor_related, list)
            else set()
        )
        successor_files = (
            {
                value
                for value in successor_related
                if isinstance(value, str) and value
            }
            if isinstance(successor_related, list)
            else set()
        )
        actual_shared = predecessor_files & successor_files
        if not actual_shared:
            continue
        if (
            predecessor.status in (TaskStatus.CANCELLED, TaskStatus.FAILED)
            or successor.status in (TaskStatus.CANCELLED, TaskStatus.FAILED)
        ):
            continue
        predecessor_id = str(predecessor.id)
        existing = list(successor.depends_on or [])
        if predecessor_id in existing or _would_create_cycle(
            successor, predecessor, candidates
        ):
            continue
        existing.append(predecessor_id)
        successor.depends_on = existing
        remember_structural_depends_on(successor, predecessor_id)
        wired.append((predecessor_id, str(successor.id), sorted(actual_shared)))
    return wired


DUPLICATE_FINDING_DROP_KEY = "_duplicate_finding_dropped"


def _identical_finding_origin(dependent: Task, foundation: Task) -> bool:
    from app.finding_deduplication import _task_finding_hash

    dependent_hash = _task_finding_hash(dependent)
    return bool(dependent_hash) and dependent_hash == _task_finding_hash(foundation)


def _drop_would_strand(
    dependent: Task, foundation: Task, candidates: List[Task]
) -> bool:
    """A declared/wired edge touching ``dependent`` marks coordination, not a
    duplicate to discard: dropping would strand the linked sibling's edge."""

    dependent_id = str(dependent.id)
    if str(foundation.id) in {str(d) for d in (dependent.depends_on or [])}:
        return True
    return any(
        dependent_id in {str(d) for d in (row.depends_on or [])}
        for row in candidates
        if row is not dependent
    )


def converge_spawn_batch_foundations(
    child_tasks: List[Task], active_tasks: List[Task]
) -> List[Task]:
    """Serialize genuine duplicate foundations before children execute.

    An identical non-empty ``finding_hash`` is the exception: that child IS the
    same finding, not a foundation to build on, and serializing it produces a
    rebase-then-duplicate PR. Such dependents are removed from ``child_tasks``
    in place, each stamped with :data:`DUPLICATE_FINDING_DROP_KEY` naming the
    surviving task so the caller can publish a skip receipt. Capability and
    near-identical-file-set duplicates keep today's serialize behavior, as does
    an identical-hash pair already linked by explicit ``depends_on`` (declared
    coordination — the apply-with-guards helper refuses those edges anyway).
    """

    from app.digest_metrics import (
        apply_foundation_convergence_action,
        compute_foundation_convergence_actions,
    )

    candidates = list(active_tasks) + list(child_tasks)
    by_id = {str(task.id): task for task in candidates}
    child_ids = {str(child.id) for child in child_tasks}
    converged: List[Task] = []
    dropped_ids: set[str] = set()
    for action in compute_foundation_convergence_actions(
        candidates, exclude_same_parent=False
    ):
        if (
            action["dependent_id"] not in child_ids
            or action["dependent_id"] in dropped_ids
            or action["foundation_id"] in dropped_ids
        ):
            continue
        dependent = by_id[action["dependent_id"]]
        foundation = by_id[action["foundation_id"]]
        if _identical_finding_origin(
            dependent, foundation
        ) and not _drop_would_strand(dependent, foundation, candidates):
            dropped_ids.add(str(dependent.id))
            ctx = dict(dependent.context or {})
            ctx[DUPLICATE_FINDING_DROP_KEY] = {
                "surviving_task_id": str(foundation.id),
                "finding_hash": (ctx.get("finding_hash") or ""),
                "reason": action["reason"],
            }
            dependent.context = ctx
            logger.warning(
                "[RESEARCH] Dropped duplicate finding child %s at spawn — "
                "task %s already carries the identical finding (%s)",
                dependent.id,
                foundation.id,
                action["reason"],
            )
            continue
        dependent = apply_foundation_convergence_action(
            action, by_id, candidates, logger=logger
        )
        if dependent is None:
            continue
        converged.append(dependent)
        logger.info(
            "[RESEARCH] Serialized duplicate foundation at spawn — task %s now "
            "depends on %s (%s)",
            dependent.id,
            action["foundation_id"],
            action["reason"],
        )
    if dropped_ids:
        child_tasks[:] = [
            child for child in child_tasks if str(child.id) not in dropped_ids
        ]
    if converged:
        logger.info(
            "[RESEARCH] Spawn-time foundation convergence wired %d edge(s)",
            len(converged),
        )
    return converged


def drop_replay_duplicate_children(
    task: Task,
    child_tasks: List[Task],
    auxiliary_tasks: List[Task],
    prior_delivery_children: List[Task],
    *,
    skip_limit: int,
) -> tuple[List[Task], int]:
    """Same-parent sibling dedup under the held spawn-boundary lock.

    A pending child whose finding identity an authenticated prior sibling
    already delivers is a guaranteed duplicate (the double-PR shape a
    replay/re-entry produces when the bounded dedup corpus misses the
    sibling). Terminal siblings suppress exactly like active ones — a replay
    must never resurrect a finding a prior attempt already delivered; retries
    of unshipped work arrive via fresh research cycles. Each drop publishes a
    skip receipt on ``task.context``: a silent drop is as bad as a duplicate.

    Returns ``(kept_children, dropped_count)``.
    """

    from app.research_execution_authority import research_delivery_identities

    delivered_by_identity: Dict[tuple, str] = {}
    for sibling in prior_delivery_children:
        for identity in research_delivery_identities(task, [sibling]):
            delivered_by_identity.setdefault(identity, str(sibling.id))
    if not delivered_by_identity or not child_tasks:
        return child_tasks, 0

    kept_children: List[Task] = []
    sibling_by_dropped_id: Dict[str, str] = {}
    boundary_skips: List[dict] = []
    for candidate in child_tasks:
        dup_identity = next(
            (
                identity
                for identity in research_delivery_identities(task, [candidate])
                if identity[0] == "finding" and identity in delivered_by_identity
            ),
            None,
        )
        if dup_identity is None:
            kept_children.append(candidate)
            continue
        surviving_id = delivered_by_identity[dup_identity]
        sibling_by_dropped_id[str(candidate.id)] = surviving_id
        boundary_skips.append(
            {
                "finding_hash": dup_identity[1],
                "surviving_task_id": surviving_id,
                "stage": "spawn_boundary_replay",
                "at": _utcnow().isoformat(),
            }
        )
    if not boundary_skips:
        return child_tasks, 0

    # Re-point edges declared against a dropped duplicate at its persisted
    # surviving sibling so no pending row keeps a depends_on ref to a task
    # that is never inserted.
    for pending_row in (*kept_children, *auxiliary_tasks):
        edges = [str(d) for d in (pending_row.depends_on or [])]
        if not any(d in sibling_by_dropped_id for d in edges):
            continue
        rewired: List[str] = []
        for edge in edges:
            target = sibling_by_dropped_id.get(edge, edge)
            if target not in rewired:
                rewired.append(target)
        pending_row.depends_on = rewired
    task.context = {
        **(task.context or {}),
        "research_duplicate_finding_skips": (
            list(
                (task.context or {}).get("research_duplicate_finding_skips") or []
            )
            + boundary_skips
        )[-skip_limit:],
    }
    flag_modified(task, "context")
    task.latest_status_message = (
        f"{(task.latest_status_message or '').rstrip()} Skipped "
        f"{len(boundary_skips)} duplicate finding child(ren) already "
        "delivered by prior sibling(s)."
    ).strip()
    logger.warning(
        "[RESEARCH] Task %s: dropped %d pending duplicate finding "
        "child(ren) already delivered by authenticated sibling(s): %s",
        task.id,
        len(boundary_skips),
        ", ".join(
            f"{skip['finding_hash']}->{skip['surviving_task_id']}"
            for skip in boundary_skips
        ),
    )
    return kept_children, len(boundary_skips)


# Established private/public names remain import-compatible.
_wire_research_sibling_edges = wire_research_sibling_edges
