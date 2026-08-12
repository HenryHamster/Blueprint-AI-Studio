"""Structural dependency inference for children of one research campaign."""

import logging
from typing import Dict, List

from app.depends_on_provenance import remember_structural_depends_on
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

RESEARCH_SIBLING_SERIALIZATION_JACCARD = 0.5
RESEARCH_SIBLING_SERIALIZATION_CONTAINMENT = 1.0


def wire_research_sibling_edges(
    child_tasks: List[Task],
    finding_to_child: Dict[int, int],
    overlaps: List[tuple],
    candidates: List[Task],
) -> List[tuple]:
    """Wire dependencies for siblings whose complete file scopes overlap."""

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
        file_union = predecessor_files | successor_files
        actual_shared = predecessor_files & successor_files
        smaller_scope = min(len(predecessor_files), len(successor_files))
        jaccard_overlap = len(actual_shared) / len(file_union) if file_union else 0
        containment_overlap = (
            len(actual_shared) / smaller_scope if smaller_scope else 0
        )
        if (
            jaccard_overlap < RESEARCH_SIBLING_SERIALIZATION_JACCARD
            and containment_overlap < RESEARCH_SIBLING_SERIALIZATION_CONTAINMENT
        ):
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
_RESEARCH_SIBLING_SERIALIZATION_JACCARD = RESEARCH_SIBLING_SERIALIZATION_JACCARD
_RESEARCH_SIBLING_SERIALIZATION_CONTAINMENT = (
    RESEARCH_SIBLING_SERIALIZATION_CONTAINMENT
)
_wire_research_sibling_edges = wire_research_sibling_edges
