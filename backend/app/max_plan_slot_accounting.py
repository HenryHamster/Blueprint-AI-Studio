"""Release Max-plan slots while preserving a database-backed active floor."""

from __future__ import annotations


async def reconcile_account_active_counts(
    account_id: str,
    *,
    active_tasks: int,
    project_id: str,
    project_active_tasks: int,
) -> str:
    """Release one cached slot without dropping below committed DB activity."""

    # Lazy import avoids a module cycle. Other process-local caches converge via
    # the existing periodic DB reconciliation (normally within ~30 seconds).
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    async with _max_plan_pool_lock:
        account = _account_by_id.get(account_id)
        if account is None:
            return "reconciled_account_missing"
        cached_active = max(0, int(account.get("active_tasks") or 0))
        account["active_tasks"] = max(
            max(0, int(active_tasks)),
            max(0, cached_active - 1),
        )
        project_state = account.setdefault("project_state", {}).setdefault(
            project_id, {}
        )
        cached_project_active = max(0, int(project_state.get("active_tasks") or 0))
        project_state["active_tasks"] = max(
            max(0, int(project_active_tasks)),
            max(0, cached_project_active - 1),
        )
        return "reconciled_from_database"


__all__ = [
    "reconcile_account_active_counts",
]
