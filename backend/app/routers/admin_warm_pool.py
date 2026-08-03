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

A higher-level ENABLE / DISABLE pair builds on the same machinery to give an
operator a single, safe, one-project-at-a-time rollout lever:

* ENABLE scopes the warm-pool allowlist to the chosen project, forces the master
  switch on, establishes a sensible per-project target if none is configured, and
  warms the first members — all persisted via the durable settings path
  (:func:`admin_settings._apply_settings`) so the enable survives a restart, then
  the SAME seed reconcile as above.
* DISABLE drains the project's live members and removes it from the allowlist. The
  KEY SAFETY PROPERTY: removing the project never disables the pool for peers that
  are still scoped. Only when the project was the LAST explicitly-scoped one (the
  allowlist would become empty, which means fleet-wide) does DISABLE also flip the
  master switch off — so dropping the final project can never silently flip the
  whole fleet warm.

Backward-compatibility: these are NEW routes, so no existing contract changes.
The JSON response keys (``seeded`` / ``drained`` / ``recycled`` / ``reason`` /
``project_id`` / ``enabled`` / ``project_allowlist``) are ADDITIVE-ONLY — future
fields may be added, but existing keys will not be renamed or removed.
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
    count_pending_pool_leases,
    is_project_warm_pool_scoped,
    is_warm_pool_enabled,
    list_pool_vms,
    resolve_project_warm_pool_target,
    warm_pool_project_scope,
    warm_pool_project_target_map,
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


async def _seed_project_pool(
    session: AsyncSession, project_id: uuid.UUID, user_id: str
) -> dict:
    """Top a project's warm pool up to target via the production reconcile path.

    Shared by the manual SEED route and the ENABLE route. Mirrors
    ``run_pool_maintainer_cycle``'s budget accounting (dispatch-slot + pool-budget
    headroom) so a manual seed can never breach a ceiling the autonomous path
    respects, and routes provisioning through ``_default_pool_provision_fn``. Does
    NOT check ``warm_pool_enabled`` / scope — callers gate that themselves (SEED
    refuses a dark/unscoped pool; ENABLE has just turned both on).

    Returns the additive-only seed response dict.
    """
    from app.shared_state import is_warm_pool_suppressed, runtime_config

    max_concurrent = int(runtime_config.get("max_concurrent_vms", 0) or 0)
    pool_max = _pool_max_vms(runtime_config)
    active_tasks = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
            )
        ).scalar()
        or 0
    )
    idle_pool = await count_idle_pool_vms(session)
    live_pool = await count_live_pool_vms(session)
    pending_leases = await count_pending_pool_leases(session)
    dispatch_slots_remaining = max(
        0, max_concurrent - active_tasks - idle_pool - pending_leases
    )
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
        user_id,
        summary,
    )
    return {
        "project_id": str(project_id),
        "seeded": summary["provisioned"],
        "recycled": summary["recycled"],
        "drained": summary["drained"],
        "reason": "ok"
        if summary["provisioned"]
        else "no_provision_headroom_or_at_target",
    }


async def _drain_project_pool(
    session: AsyncSession, project_id: uuid.UUID, user_id: str, *, reason: str
) -> int:
    """Retire every live (PROVISIONING/IDLE/LEASED) warm-pool member for a project.

    Shared by the manual DRAIN route and the DISABLE route. Retires through the
    existing ``retire_and_delete_pool_vm`` teardown so idle cost is banked to the
    ledger and the GCP VM + pre-staged secret are cleaned up. A member a peer
    leased or is already tearing down between the scan and the claim is skipped
    (the atomic compare-and-swap loses), so the returned count covers only members
    THIS call retired. Returns the number drained.
    """
    members = await list_pool_vms(session, project_id=project_id)
    live_members = [m for m in members if m.state in LIVE_POOL_STATES]
    drained = 0
    for member in live_members:
        if await retire_and_delete_pool_vm(
            session,
            member,
            reason=reason,
            force=True,
            abandon_leased_handoff=True,
        ):
            drained += 1
    logger.info(
        "[WARM-POOL] operator %s project=%s by user=%s drained=%d of %d live",
        reason,
        project_id,
        user_id,
        drained,
        len(live_members),
    )
    return drained


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

    return await _seed_project_pool(session, project_id, current_user["user_id"])


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

    drained = await _drain_project_pool(
        session, project_id, current_user["user_id"], reason="operator_drain"
    )
    return {"project_id": str(project_id), "drained": drained}


@router.post("/projects/{project_id}/warm-pool/enable")
async def enable_warm_pool_for_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Enable the warm pool for ONE project and warm its first members.

    The safe, discoverable one-project-at-a-time rollout lever. In a single
    action this:

    * scopes ``warm_pool_project_allowlist`` to include this project (so the pool
      serves exactly the scoped set, not the whole fleet);
    * forces the master switch ``warm_pool_enabled`` on;
    * establishes a sensible per-project target (``warm_pool_target_size``) if
      none is configured (0/unset → 2), and otherwise keeps the operator's value;
    * seeds the first members up to that target.

    The scope + master-switch + target writes go through the durable settings path
    (:func:`admin_settings._apply_settings`, under ``runtime_config_lock``) so the
    enable is validated, persisted to ``DeploymentState`` and survives a restart;
    the seed reuses the existing reconcile machinery so the fleet/dispatch budget
    ceilings, credential resolution and idle-cost banking all still apply.

    Because enable always writes a NON-EMPTY allowlist, it can never produce the
    riskiest state (master-on with an empty allowlist = fleet-wide).

    Response keys (additive-only): the seed keys (``project_id``, ``seeded``,
    ``recycled``, ``drained``, ``reason``) plus ``enabled`` and
    ``project_allowlist`` (the resulting scope).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    from app.routers.admin_settings import _apply_settings
    from app.shared_state import runtime_config, runtime_config_lock

    async with runtime_config_lock:
        scope = warm_pool_project_scope()
        scope.add(str(project_id))
        payload: dict = {
            "warm_pool_enabled": True,
            "warm_pool_project_allowlist": sorted(scope),
        }
        # Establish a PER-PROJECT target for this project only when it has no
        # effective target yet — i.e. it has no explicit per-project entry AND no
        # global fallback is configured. In that case default to 2 so enabling
        # actually warms members; otherwise leave the map untouched so an existing
        # per-project value (or the global fallback) is preserved. This replaces
        # the old behavior of hardcoding the GLOBAL target to 2, which sized every
        # project at once.
        if (
            str(project_id) not in warm_pool_project_target_map()
            and resolve_project_warm_pool_target(project_id) <= 0
        ):
            new_target_map = dict(warm_pool_project_target_map())
            new_target_map[str(project_id)] = 2
            payload["warm_pool_target_size_by_project"] = new_target_map
        await _apply_settings(payload, session, runtime_config)

    logger.info(
        "[WARM-POOL] operator enable project=%s by user=%s allowlist=%s",
        project_id,
        current_user["user_id"],
        sorted(warm_pool_project_scope()),
    )

    summary = await _seed_project_pool(session, project_id, current_user["user_id"])
    return {
        **summary,
        "enabled": is_warm_pool_enabled(),
        "project_allowlist": sorted(warm_pool_project_scope()),
    }


@router.post("/projects/{project_id}/warm-pool/disable")
async def disable_warm_pool_for_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Disable the warm pool for ONE project, draining its members.

    Removes the project from ``warm_pool_project_allowlist`` and drains every live
    member through the existing teardown path (idle cost banked to the ledger).

    KEY SAFETY PROPERTY: this must NEVER turn the pool off for peers that are still
    scoped. The master switch (``warm_pool_enabled``) is left ON whenever any other
    project remains in the allowlist. It is flipped OFF only when this project was
    the LAST explicitly-scoped one — i.e. the allowlist would otherwise become
    empty, which means fleet-wide; flipping the switch off instead keeps removing
    the final project from silently warming the whole fleet. When the pool is
    already in fleet-wide mode (the allowlist was empty), the master switch is left
    untouched so peers are unaffected.

    Idempotent: draining an empty / unscoped project returns ``drained: 0``.

    Response keys (additive-only): ``project_id``, ``drained``, ``enabled``,
    ``project_allowlist`` (the resulting scope).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    from app.routers.admin_settings import _apply_settings
    from app.shared_state import runtime_config, runtime_config_lock

    drained = await _drain_project_pool(
        session, project_id, current_user["user_id"], reason="operator_disable"
    )

    async with runtime_config_lock:
        scope = warm_pool_project_scope()
        # was_scoped is only meaningful when the allowlist was non-empty: an empty
        # allowlist is fleet-wide, where there is no per-project scope to remove.
        was_scoped = bool(scope) and str(project_id) in scope
        scope.discard(str(project_id))
        payload: dict = {"warm_pool_project_allowlist": sorted(scope)}
        # Only when we removed the LAST explicitly-scoped project (non-empty → now
        # empty) do we flip the master switch off, so an empty allowlist never
        # silently reverts to fleet-wide. Peers in a still-non-empty allowlist keep
        # the pool enabled.
        if was_scoped and not scope:
            payload["warm_pool_enabled"] = False
        await _apply_settings(payload, session, runtime_config)

    logger.info(
        "[WARM-POOL] operator disable project=%s by user=%s drained=%d allowlist=%s enabled=%s",
        project_id,
        current_user["user_id"],
        drained,
        sorted(warm_pool_project_scope()),
        is_warm_pool_enabled(),
    )

    return {
        "project_id": str(project_id),
        "drained": drained,
        "enabled": is_warm_pool_enabled(),
        "project_allowlist": sorted(warm_pool_project_scope()),
    }


@router.post("/projects/{project_id}/warm-pool/target")
async def set_warm_pool_target_for_project(
    project_id: uuid.UUID,
    payload: dict,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Set (or clear) ONE project's per-project warm-pool IDLE target.

    Owner-gated. The request body is ``{"target_size": <int 0..50> | null}``:

    * an integer 0..50 establishes/overrides this project's explicit target in
      ``warm_pool_target_size_by_project`` — the maintainer then reconciles this
      project toward THAT number, independently of the fleet-wide
      ``warm_pool_target_size`` and of every other project's target. ``0`` keeps
      this project's pool empty while leaving the master switch and other
      projects untouched;
    * ``null`` (or omitting a numeric value) CLEARS this project's explicit
      target, so it falls back to the global ``warm_pool_target_size``.

    The map is read-modify-written under ``runtime_config_lock`` so a concurrent
    edit to a DIFFERENT project is never clobbered, then persisted via the durable
    settings path (:func:`admin_settings._apply_settings`) so the change survives a
    Cloud Run restart. Setting a target does NOT enable the pool or scope the
    project — it only sizes the pool the maintainer keeps for an already-scoped
    project; while ``warm_pool_enabled`` is False the value is inert.

    Response keys (additive-only): ``project_id``, ``target_size`` (the explicit
    per-project value now stored, or ``None`` when cleared), ``effective_target``
    (the value the maintainer will reconcile toward, after global fallback),
    ``cleared`` (whether the explicit entry was removed), and
    ``warm_pool_target_size_by_project`` (the resulting map).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if not isinstance(payload, dict) or "target_size" not in payload:
        raise HTTPException(
            status_code=400,
            detail=(
                "request body must include target_size (an integer 0..50, or "
                "null to clear the per-project target back to the global fallback)"
            ),
        )
    raw_target = payload["target_size"]

    # Validate BEFORE mutating so a bad value never leaves a half-applied state.
    if raw_target is not None:
        try:
            target_value = int(raw_target)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="target_size must be an integer (0..50) or null",
            )
        if not (0 <= target_value <= 50):
            raise HTTPException(
                status_code=400,
                detail="target_size must be between 0 and 50",
            )
    else:
        target_value = None

    from app.routers.admin_settings import _apply_settings
    from app.shared_state import runtime_config, runtime_config_lock

    sid = str(project_id)
    async with runtime_config_lock:
        # Read-modify-write the normalized map so a concurrent edit to ANOTHER
        # project's target is never clobbered.
        target_map = dict(warm_pool_project_target_map())
        if target_value is None:
            cleared = sid in target_map
            target_map.pop(sid, None)
        else:
            cleared = False
            target_map[sid] = target_value
        await _apply_settings(
            {"warm_pool_target_size_by_project": target_map}, session, runtime_config
        )
        resulting_map = warm_pool_project_target_map()
        effective_target = resolve_project_warm_pool_target(project_id)

    logger.info(
        "[WARM-POOL] operator set target project=%s by user=%s target_size=%s "
        "cleared=%s effective=%d",
        project_id,
        current_user["user_id"],
        target_value,
        cleared,
        effective_target,
    )

    return {
        "project_id": sid,
        "target_size": resulting_map.get(sid),
        "effective_target": effective_target,
        "cleared": cleared,
        "warm_pool_target_size_by_project": resulting_map,
    }
