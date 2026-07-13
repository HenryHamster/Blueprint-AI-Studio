"""Bounding the per-account ``project_state`` map.

A ``MaxPlanAccount`` carries a ``project_state`` JSONB map of per-project usage
counters::

    {project_id: {"active_tasks": int, "daily_task_count": int}}

``max_plan_pool`` writes it with ``setdefault(project_id, {})`` on every claim
and never removed an entry, so the map grew one row per DISTINCT PROJECT ever
dispatched on that account — copied forward on every ~300s pool rebuild and
re-persisted on every cap flush, forever, including for deleted projects.

This module holds the collection rule. It lives outside ``max_plan_pool`` because
that module is at its size guardrail (``testing/module_size_limits.json``); a pure,
side-effect-free function is also far easier to test in isolation than anything
reachable only through the pool's locks and DB fixtures.
"""
from __future__ import annotations

from typing import Any, Dict, Set


def prune_project_state(
    project_state: Dict[str, Any] | None,
    *,
    live_project_ids: Set[str] | None = None,
) -> Dict[str, Dict[str, int]]:
    """Drop per-project entries that carry no information.

    An entry holds exactly two counters, ``active_tasks`` and
    ``daily_task_count``. An entry whose counters are BOTH zero is
    indistinguishable from an absent one: every reader treats a missing entry as
    ``{}`` / zero, and the next dispatch recreates it via ``setdefault``. Dropping
    it is therefore behaviour-preserving BY CONSTRUCTION, not merely
    behaviour-preserving in practice.

    That single rule bounds the map without special-casing:

    * a **live** project keeps its entry while it has running tasks
      (``active_tasks > 0``) or has dispatched within the current daily window
      (``daily_task_count > 0``);
    * an **idle** project's entry survives until the daily reset zeroes its
      ``daily_task_count``, then falls out on the next prune;
    * a **deleted** project's entry has no live tasks and stops being recreated,
      so it falls out the same way.

    ``live_project_ids``, when supplied, evicts entries for projects that no
    longer exist immediately rather than waiting on the daily window — but never
    one still holding active tasks, whose counter a release path still has to
    decrement.

    Callers hold the pool lock; this function itself is pure.
    """
    pruned: Dict[str, Dict[str, int]] = {}
    for pid, pstate in (project_state or {}).items():
        if not isinstance(pstate, dict):
            continue
        active = int(pstate.get("active_tasks") or 0)
        daily = int(pstate.get("daily_task_count") or 0)
        if active <= 0 and daily <= 0:
            continue
        if (
            live_project_ids is not None
            and active <= 0
            and str(pid) not in live_project_ids
        ):
            continue
        pruned[pid] = pstate
    return pruned


def reconcile_project_state(
    db_project_state: Dict[str, Any] | None,
    active_counts: Dict[str, int] | None,
    *,
    live_project_ids: Set[str] | None = None,
) -> Dict[str, Dict[str, int]]:
    """Rebuild an account's per-project state for the pool, bounded.

    Called once per account on every ~300s pool rebuild. Three steps:

    1. **Copy the persisted state, zeroing ``active_tasks``.** The tasks table is
       the single source of truth for the live active count at startup. A project
       whose persisted ``active_tasks`` is a stale nonzero value but which has
       ZERO currently-active tasks would otherwise keep that stale count forever
       (nothing overrides it below), permanently starving that (account, project)
       pair once per-project concurrency caps are configured. Other keys
       (``daily_task_count``, …) survive.
    2. **Override ``active_tasks``** with the reconciled per-(account, project)
       counts from the tasks table.
    3. **Prune** — AFTER the override, so a project with live tasks is never
       evicted. This is what keeps the map from growing one permanent entry per
       project ever dispatched on the account.
    """
    reconciled: Dict[str, Dict[str, int]] = {}
    for pid, pstate in (db_project_state or {}).items():
        if isinstance(pstate, dict):
            copied = dict(pstate)
            copied["active_tasks"] = 0
            reconciled[pid] = copied
    for pid, count in (active_counts or {}).items():
        reconciled.setdefault(pid, {})["active_tasks"] = count
    return prune_project_state(reconciled, live_project_ids=live_project_ids)
