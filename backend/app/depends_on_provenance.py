"""Helpers for durable per-edge ``depends_on`` provenance.

``depends_on`` now carries both legacy queue-wave edges and modern structural
prerequisites. Some structural edges can later become "eligible" according to
the dispatch gate (for example once a prerequisite lands), but they are still
real dependency edges and should not be mistaken for legacy serialization by
cleanup paths such as ``clear-wave-deps``.

This module stores a task-level list of dependency IDs whose edges were created
explicitly as structural prerequisites, so readers can preserve them on a
per-edge basis instead of relying on coarse task-level heuristics.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.models import Task

STRUCTURAL_DEPENDS_ON_IDS_KEY = "_structural_depends_on_ids"


def _normalize_dep_ids(dep_ids: Iterable[str] | str) -> list[str]:
    if isinstance(dep_ids, str):
        raw_ids = [dep_ids]
    else:
        raw_ids = list(dep_ids)
    out: list[str] = []
    for dep_id in raw_ids:
        dep_text = str(dep_id).strip()
        if dep_text:
            out.append(dep_text)
    return out


def get_structural_depends_on_ids(task: Task) -> list[str]:
    """Return the dependency IDs explicitly marked structural on *task*."""
    ctx = task.context or {}
    raw = ctx.get(STRUCTURAL_DEPENDS_ON_IDS_KEY) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    out: list[str] = []
    for dep_id in raw:
        dep_text = str(dep_id).strip()
        if dep_text and dep_text not in seen:
            seen.add(dep_text)
            out.append(dep_text)
    return out


def remember_structural_depends_on(task: Task, dep_ids: Iterable[str] | str) -> None:
    """Persist dependency IDs whose edges are real structural prerequisites."""
    new_ids = _normalize_dep_ids(dep_ids)
    if not new_ids:
        return

    existing = get_structural_depends_on_ids(task)
    merged = list(existing)
    for dep_id in new_ids:
        if dep_id not in merged:
            merged.append(dep_id)

    ctx = dict(task.context or {})
    ctx[STRUCTURAL_DEPENDS_ON_IDS_KEY] = merged
    task.context = ctx


def has_structural_depends_on(task: Task, dep_id: str) -> bool:
    """True when *task* explicitly marked ``dep_id`` as a structural edge."""
    dep_text = str(dep_id).strip()
    if not dep_text:
        return False
    return dep_text in set(get_structural_depends_on_ids(task))


def remap_structural_depends_on(task: Task, old_to_new: dict[str, str]) -> None:
    """Rewrite stored structural dependency IDs through a requeue remap."""
    existing = get_structural_depends_on_ids(task)
    if not existing:
        return

    remapped = [old_to_new.get(dep_id, dep_id) for dep_id in existing]
    ctx = dict(task.context or {})
    ctx[STRUCTURAL_DEPENDS_ON_IDS_KEY] = remapped
    task.context = ctx
