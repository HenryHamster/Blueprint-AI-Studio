"""Structural dependency inference for children of one research campaign."""

import logging
from typing import Dict, List

from app.depends_on_provenance import remember_structural_depends_on
from app.models import Task, TaskStatus

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


def converge_spawn_batch_foundations(
    child_tasks: List[Task], active_tasks: List[Task]
) -> List[Task]:
    """Serialize genuine duplicate foundations before children execute."""

    from app.digest_metrics import (
        apply_foundation_convergence_action,
        compute_foundation_convergence_actions,
    )

    candidates = list(active_tasks) + list(child_tasks)
    by_id = {str(task.id): task for task in candidates}
    child_ids = {str(child.id) for child in child_tasks}
    converged: List[Task] = []
    for action in compute_foundation_convergence_actions(
        candidates, exclude_same_parent=False
    ):
        if action["dependent_id"] not in child_ids:
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
    if converged:
        logger.info(
            "[RESEARCH] Spawn-time foundation convergence wired %d edge(s)",
            len(converged),
        )
    return converged


# Established private/public names remain import-compatible.
_wire_research_sibling_edges = wire_research_sibling_edges
