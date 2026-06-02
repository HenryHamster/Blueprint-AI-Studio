"""Admin warm-pool seed/drain control endpoints (efficiency direction d-5fd69fe1).

The warm-VM pool forms autonomously for demand-active projects via the
leader-only maintainer (``warm_pool_maintainer.run_pool_maintainer_cycle``).
These endpoints are a COMPLEMENT to that autonomous bootstrap, NOT a
replacement: they give an operator an explicit, manual lever to seed (top a
project's pool up to its target) or drain (retire every live member) on demand.

Both routes are deliberately thin orchestrators over the existing, in-flight
warm-pool machinery so credential resolution, the fleet budget ceiling
(``warm_pool_max_vms``), the per-project target (``warm_pool_target_size``) and
the claim circuit breaker all stay in ONE place:

* SEED delegates to :func:`warm_pool_maintainer.reconcile_project_pool` with the
  production provisioner (:func:`warm_pool_maintainer._default_pool_provision_fn`,
  which routes through ``compute.provision_pool_vm``). It computes the same
  dispatch-slot / pool-budget headroom that ``run_pool_maintainer_cycle`` does, so
  a manual seed can never breach a ceiling the autonomous path would respect.
* DRAIN lists the project's live members and retires each through
  :func:`warm_pool_maintainer.retire_and_delete_pool_vm`, so idle cost is banked
  to ``PoolIdleCostLedger`` and the GCP VM + pre-staged secret are cleaned up.

Backward-compatibility: these are NEW routes, so no existing contract changes.
The JSON response keys (``seeded`` / ``drained`` / ``recycled`` / ``reason`` /
``project_id``) are ADDITIVE-ONLY — future fields may be added, but existing
keys will not be renamed or removed.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user, require_admin
from app.database import get_session
from app.models import Task, TaskStatus
from app.warm_pool import (
    LIVE_POOL_STATES,
    count_idle_pool_vms,
    count_live_pool_vms,
    is_project_warm_pool_scoped,
    is_warm_pool_enabled,
    list_pool_vms,
)
from app.warm_pool_maintainer import (
    _default_pool_provision_fn,
    _pool_max_vms,
    _stale_pool_image_ids,
    reconcile_project_pool,
    retire_and_delete_pool_vm,
)

logger = logging.getLogger("blueprint")

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-warm-pool"],
    dependencies=[Depends(get_current_user)],
)


@router.post("/projects/{project_id}/warm-pool/seed")
async def seed_warm_pool(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Manually top a project's warm pool up to its configured target.

    Provisions taskless pool VMs up to ``warm_pool_target_size`` for the project,
    bounded by the fleet ``warm_pool_max_vms`` ceiling and the live dispatch-slot
    budget — the SAME ceilings the autonomous maintainer respects. Provisioning
    routes through the production provisioner (``compute.provision_pool_vm``) so
    credential resolution and registry insertion stay centralized.

    Returns HTTP 409 when ``warm_pool_enabled`` is off (the pool is dark; nothing
    to seed) or when the project is not in the warm-pool scope (allowlist): the
    autonomous maintainer forces an unscoped project's target to 0 and drains it
    on its next tick (see ``reconcile_project_pool``), so seeding it would be
    immediately undone — the operator must add the project to
    ``warm_pool_project_allowlist`` first. When the circuit breaker is tripped or
    no credentialed account is available, the reconcile path provisions zero
    members and ``seeded`` is 0.

    Response keys (additive-only): ``project_id``, ``seeded``, ``recycled``,
    ``drained``, ``reason``.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if not is_warm_pool_enabled():
        raise HTTPException(
            status_code=409,
            detail="warm_pool_enabled is off — the warm pool is dark; nothing to seed",
        )

    # An unscoped project (the allowlist is non-empty and excludes it) is held at
    # target 0 by the maintainer, which would drain anything seeded here on its
    # next tick. Refuse with a clear 409 so the operator scopes the project in
    # first, rather than seeing a confusing seeded:0 (or a seed that vanishes).
    if not is_project_warm_pool_scoped(project_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "project is not in the warm-pool scope — add it to "
                "warm_pool_project_allowlist before seeding, otherwise the "
                "maintainer will drain the seeded members on its next tick"
            ),
        )

    from app.shared_state import is_warm_pool_suppressed, runtime_config

    # Mirror run_pool_maintainer_cycle's budget accounting so a manual seed can
    # never breach a ceiling the autonomous path would respect. A new pool VM
    # occupies a real GCP slot, so headroom = max_concurrent_vms - task-bound VMs
    # - idle pool members; the pool budget is warm_pool_max_vms - live members.
    max_concurrent = int(runtime_config.get("max_concurrent_vms", 0) or 0)
    pool_max = _pool_max_vms(runtime_config)
    active_tasks = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_(
                        [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                    )
                )
            )
        ).scalar()
        or 0
    )
    idle_pool = await count_idle_pool_vms(session)
    live_pool = await count_live_pool_vms(session)
    dispatch_slots_remaining = max(0, max_concurrent - active_tasks - idle_pool)
    pool_budget_remaining = max(0, pool_max - live_pool)
    stale_vm_ids = await _stale_pool_image_ids()

    summary = await reconcile_project_pool(
        session,
        project_id,
        runtime_config=runtime_config,
        provision_fn=_default_pool_provision_fn,
        dispatch_slots_remaining=dispatch_slots_remaining,
        pool_budget_remaining=pool_budget_remaining,
        stale_vm_ids=stale_vm_ids,
        suppressed=is_warm_pool_suppressed(),
    )
    logger.info(
        "[WARM-POOL] operator seed project=%s by user=%s summary=%s",
        project_id,
        current_user["user_id"],
        summary,
    )
    return {
        "project_id": str(project_id),
        "seeded": summary["provisioned"],
        "recycled": summary["recycled"],
        "drained": summary["drained"],
        "reason": "ok" if summary["provisioned"] else "no_provision_headroom_or_at_target",
    }


@router.post("/projects/{project_id}/warm-pool/drain")
async def drain_warm_pool(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Manually retire every live warm-pool member for a project.

    Retires + deletes each live member (PROVISIONING / IDLE / LEASED) through the
    existing teardown path so idle cost is banked to ``PoolIdleCostLedger`` and the
    GCP VM + its pre-staged secret are cleaned up. A member a peer leased or is
    already tearing down between the scan and the claim is skipped (the atomic
    compare-and-swap loses), so ``drained`` counts only members THIS call retired.

    Idempotent: draining an empty / already-drained pool returns ``drained: 0``.

    Response keys (additive-only): ``project_id``, ``drained``.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    members = await list_pool_vms(session, project_id=project_id)
    live_members = [m for m in members if m.state in LIVE_POOL_STATES]
    drained = 0
    for member in live_members:
        if await retire_and_delete_pool_vm(session, member, reason="operator_drain"):
            drained += 1
    logger.info(
        "[WARM-POOL] operator drain project=%s by user=%s drained=%d of %d live",
        project_id,
        current_user["user_id"],
        drained,
        len(live_members),
    )
    return {"project_id": str(project_id), "drained": drained}
