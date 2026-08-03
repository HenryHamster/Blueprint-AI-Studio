"""Warm-VM pool registry + lease helpers (efficiency direction d-5fd69fe1).

Foundation for a *real* warm VM pool: pre-provisioned, credentialed, idle agent
VMs tracked in the durable :class:`app.models.PoolVm` table, leasable to newly
queued tasks instead of cold-provisioning one VM per task.

This module owns the registry CRUD and the *atomic* lease selection. It does NOT
provision VMs (see ``compute.provision_pool_vm``) and it does NOT dispatch tasks
to leased VMs (the dispatch hook is a separate sibling task). Everything here is
INERT in production until ``warm_pool_enabled`` is flipped on.

Leasing mirrors the SKIP-LOCKED claim pattern used by ``warm_backlog_reuse``:
``SELECT ... FOR UPDATE SKIP LOCKED`` over real ``PoolVm`` rows so concurrent
dispatchers can't double-claim a member. The lease gate is a HARD
tenant-isolation boundary — a member is only ever returned for a task in the
SAME project, and only when its pinned LLM identity (account/provider/model) is
compatible with the candidate task's requirement (a reused VM runs on the
credentials frozen into its boot manifest).
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm.attributes import flag_modified

from app.models import PoolVm, PoolVmState, Task, TaskStatus
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

# Default lease TTL (seconds). Sized to comfortably exceed the cold-equivalent
# claim window (agent boot is already paid; this only covers the dispatch ->
# /claim round trip). The dispatch hook / maintainer (sibling tasks) may pass an
# explicit TTL; this is the fallback.
_DEFAULT_LEASE_TTL_SECS = 300

# Lifecycle states that represent a *live* pool member — a real GCP VM (and its
# pre-staged secret) that must NOT be reclaimed by the orphan-cleanup or
# secret-reconciliation sweeps. RETIRING is deliberately excluded: a retiring
# member is being torn down, so letting the generic cleanup finish it is fine.
LIVE_POOL_STATES = (PoolVmState.PROVISIONING, PoolVmState.IDLE, PoolVmState.LEASED)

# Members that are NOT bound to a task. A LEASED member is running a task whose
# row is already PROVISIONING/IN_PROGRESS (counted in the dispatch budget), so
# only PROVISIONING + IDLE members are the "extra" idle inventory that must be
# excluded from the dispatch concurrency budget (slot accounting).
IDLE_POOL_STATES = (PoolVmState.PROVISIONING, PoolVmState.IDLE)


def is_warm_pool_enabled() -> bool:
    """True iff the warm-pool master switch is on.

    Defaults False so every warm-pool code path is inert in production until the
    full machinery lands and an operator flips ``warm_pool_enabled``.
    """
    return bool(_runtime_config.get("warm_pool_enabled", False))


def is_warm_pool_account_retarget_enabled() -> bool:
    """True iff the pool may lease an ACCOUNT-MISMATCHED idle member.

    Master gate for the account-agnostic lease: when on, a same-tenant idle
    member may be leased for a task whose resolved account differs from the
    member's frozen boot account, and the warm-claim path re-materializes the
    resolved account's credentials before the role runs (see
    ``max_plan_pool.build_account_retarget_manifest`` +
    ``agent_warm_continuity``). Defaults False so the pool stays STRICT-equality
    (today's behavior) until the agent-side re-materialize step ships — a
    relaxed lease WITHOUT working re-materialization would silently run the task
    on the member's wrong frozen credentials, so the relaxation and the
    re-target capability MUST be enabled together by the operator.
    """
    return bool(_runtime_config.get("warm_pool_account_retarget_enabled", False))


def is_warm_pool_cross_provider_retarget_enabled() -> bool:
    """True iff an account-mismatched lease may also cross PROVIDERS.

    Cross-provider re-target (Anthropic<->OpenAI<->Gemini) additionally requires
    the agent to re-materialize the provider / auth-mode / execution-mode
    constants, not just the secret files. Defaults False so account retarget (on
    its own) stays restricted to SAME-provider same-tenant idle members until
    the cross-provider agent capability lands. Only meaningful when
    :func:`is_warm_pool_account_retarget_enabled` is also on.
    """
    return bool(_runtime_config.get("warm_pool_cross_provider_retarget_enabled", False))


def warm_pool_project_scope() -> set[str]:
    """Explicit allowlist of project IDs the warm pool is scoped to (as strings).

    Read from the global ``warm_pool_project_allowlist`` runtime-config value,
    which may be a JSON list, a comma-separated string, or a single id. An EMPTY
    (or unset) allowlist means the pool is fleet-wide — the legacy all-or-nothing
    behavior, preserved so flipping the master switch without a scope keeps
    today's semantics. A NON-EMPTY allowlist scopes provisioning to exactly the
    listed projects so an operator can enable the pool for one project, measure
    its lift and cost, then widen the scope — while every other project stays on
    the cold path. The master switch (``warm_pool_enabled``) remains the outer
    kill-switch above this scope.
    """
    raw = _runtime_config.get("warm_pool_project_allowlist") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return {str(p).strip() for p in raw if str(p).strip()}


def is_project_warm_pool_scoped(project_id) -> bool:
    """True iff the warm pool may provision/maintain a pool for ``project_id``.

    Returns True for EVERY project when :func:`warm_pool_project_scope` is empty
    (fleet-wide legacy mode); otherwise True only for projects in the explicit
    allowlist. The maintainer consults this so an unscoped project's pool target
    is forced to 0 (no provisioning, idle members drained) while a scoped
    project is held at its configured target.
    """
    scope = warm_pool_project_scope()
    if not scope:
        return True
    return str(project_id) in scope


def warm_pool_project_target_map() -> dict[str, int]:
    """Per-project warm-pool IDLE target overrides, as ``{project_id: int}``.

    Read from the durable ``warm_pool_target_size_by_project`` runtime-config
    value (modeled on ``warm_pool_project_allowlist``). Normalizes to trimmed,
    non-empty string project-id keys mapping to integer targets clamped to the
    same 0..50 range the global ``warm_pool_target_size`` uses, silently dropping
    malformed entries so a garbage value can never corrupt a reconcile target. An
    EMPTY map (the default) means every scoped project falls back to the single
    fleet-wide target. Parallel to :func:`warm_pool_project_scope`.
    """
    raw = _runtime_config.get("warm_pool_target_size_by_project") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        sid = str(key).strip()
        if not sid:
            continue
        try:
            ival = int(value)
        except (TypeError, ValueError):
            continue
        out[sid] = max(0, min(50, ival))
    return out


def resolve_project_warm_pool_target(project_id) -> int:
    """A project's EFFECTIVE warm-pool IDLE target.

    Returns the project's explicit per-project value from
    ``warm_pool_target_size_by_project`` when one is set, otherwise falls back to
    the global ``warm_pool_target_size``. Mirrors the global-target floor clamp in
    ``warm_pool_maintainer._pool_target_size`` (``max(0, …)``, default 0) for the
    fallback so the resolved value is byte-identical to the prior global behavior
    when no per-project override exists. Parallel to
    :func:`is_project_warm_pool_scoped`; this resolves the per-project SIZE the
    maintainer reconciles a scoped project toward (the scope decides whether the
    project is reconciled at all; this decides to what target).
    """
    per_project = warm_pool_project_target_map()
    sid = str(project_id)
    if sid in per_project:
        return per_project[sid]
    try:
        return max(0, int(_runtime_config.get("warm_pool_target_size", 0)))
    except (TypeError, ValueError):
        return 0


async def register_pool_vm(
    session,
    *,
    vm_instance_id: str,
    project_id: uuid.UUID,
    account_id: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    agent_image_fingerprint: str | None = None,
    state: PoolVmState = PoolVmState.PROVISIONING,
) -> PoolVm:
    """Insert a new pool member row and return it.

    Called by ``compute.provision_pool_vm`` after a taskless VM is booted. The
    member starts in PROVISIONING; the agent's first ready signal (or the
    provisioner, when it boots synchronously) transitions it to IDLE via
    :func:`mark_pool_vm_idle`. The caller owns the transaction (commit).
    """
    member = PoolVm(
        vm_instance_id=vm_instance_id,
        project_id=project_id,
        account_id=account_id,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        agent_image_fingerprint=agent_image_fingerprint,
        state=state,
        idle_since=_utcnow() if state == PoolVmState.IDLE else None,
    )
    session.add(member)
    await session.flush()
    logger.info(
        "[WARM-POOL] registered pool VM vm_instance_id=%s project=%s state=%s "
        "account=%s provider=%s model=%s",
        vm_instance_id,
        project_id,
        state.value,
        account_id,
        provider,
        model,
    )
    return member


async def get_pool_vm(session, vm_instance_id: str) -> PoolVm | None:
    """Return the pool member with this ``vm_instance_id``, or None."""
    return (
        (
            await session.execute(
                select(PoolVm).where(PoolVm.vm_instance_id == vm_instance_id)
            )
        )
        .scalars()
        .first()
    )


async def list_pool_vms(
    session,
    *,
    project_id: uuid.UUID | None = None,
    state: PoolVmState | None = None,
) -> list[PoolVm]:
    """List pool members, optionally filtered by project and/or lease state."""
    stmt = select(PoolVm)
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    if state is not None:
        stmt = stmt.where(PoolVm.state == state)
    stmt = stmt.order_by(PoolVm.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


async def get_live_pool_vm_ids(
    session, *, project_id: uuid.UUID | None = None
) -> set[str]:
    """Return the vm_instance_ids of all LIVE pool members (state != RETIRING).

    The orphan-cleanup and secret-reconciliation sweeps add this set to their
    "in-use" set so a pre-provisioned idle pool VM (and its pre-staged secret) is
    NOT deleted just because no Task row points at it. Excludes RETIRING members
    — those are intentionally being torn down, so the generic cleanup may finish
    the job.
    """
    stmt = select(PoolVm.vm_instance_id).where(PoolVm.state.in_(LIVE_POOL_STATES))
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    rows = (await session.execute(stmt)).scalars().all()
    return {vid for vid in rows if vid}


async def pool_vm_cleanup_exemptions(
    session, *, project_id: uuid.UUID | None = None
) -> set[str]:
    """vm_instance_ids the generic GCP/secret cleanup must spare — or an EMPTY
    set when the warm pool is disabled.

    When ``warm_pool_enabled`` is off BOTH pool reapers (maintainer + idle sweep)
    are inert and never clear ``PoolVm`` rows, so exempting members here would
    leak them (and their billed GCP VMs / pre-staged secrets) forever — the
    generic cleanup must be allowed to reclaim the now-unmanaged members. This
    centralizes that flag gate so the ``gcp_cleanup_service`` and
    ``secret_reconciliation_service`` can never drift apart or silently drop it.
    """
    if not is_warm_pool_enabled():
        return set()
    return await get_live_pool_vm_ids(session, project_id=project_id)


async def count_idle_pool_vms(
    session, *, project_id: uuid.UUID | None = None
) -> int:
    """Count non-task-bound pool members (PROVISIONING or IDLE) for slot accounting.

    These are the members the orchestrator must NOT charge against the dispatch
    concurrency budget — they are warm inventory, not in-flight tasks. LEASED
    members are excluded (their task already consumes a dispatch slot).
    """
    stmt = (
        select(func.count())
        .select_from(PoolVm)
        .where(PoolVm.state.in_(IDLE_POOL_STATES))
    )
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


async def count_idle_state_pool_vms(
    session, *, project_id: uuid.UUID | None = None
) -> int:
    """Count members in the IDLE state only (NOT PROVISIONING), per project or fleet-wide.

    Distinct from ``count_idle_pool_vms`` (which also counts PROVISIONING slots for
    concurrency accounting): this answers exactly "does leasable idle inventory exist
    for this project?" — used on the dispatch miss path to distinguish a genuinely
    empty pool (``no_idle_member``) from one whose idle members are present but
    identity/tenant-incompatible with the task (``no_compatible_member``). It
    deliberately ignores account/provider/model and the blacklist so it reflects raw
    idle inventory regardless of why the lease could not match.
    """
    stmt = (
        select(func.count()).select_from(PoolVm).where(PoolVm.state == PoolVmState.IDLE)
    )
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


async def count_live_pool_vms(
    session, *, project_id: uuid.UUID | None = None
) -> int:
    """Count live pool members (state != RETIRING), fleet-wide or per project.

    Used by the maintainer to compare the live pool against its target and the
    fleet-wide ``warm_pool_max_vms`` ceiling.
    """
    stmt = (
        select(func.count())
        .select_from(PoolVm)
        .where(PoolVm.state.in_(LIVE_POOL_STATES))
    )
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


async def count_pending_pool_leases(
    session, *, project_id: uuid.UUID | None = None
) -> int:
    """Count LEASED pool members whose pinned task is still QUEUED.

    These are pool VMs in the dispatch-to-claim handoff window: the dispatch
    hook has leased the VM and pinned the task to it, but the task is still
    QUEUED (the agent has not yet claimed it via /next-role). They occupy a
    real GCP slot but are invisible to the normal headroom calculation because
    neither ``active_tasks`` (PROVISIONING/IN_PROGRESS only) nor ``idle_pool``
    (PROVISIONING/IDLE only) captures them. Subtracting this count from the
    headroom prevents the maintainer from provisioning into slots already
    consumed by in-flight warm-pool handoffs.
    """
    stmt = (
        select(func.count())
        .select_from(PoolVm)
        .join(Task, PoolVm.leased_task_id == Task.id)
        .where(
            PoolVm.state == PoolVmState.LEASED,
            Task.status == TaskStatus.QUEUED,
        )
    )
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


# Terminal task statuses for orphaned-lease detection. A LEASED pool member whose
# backing task is in one of these (or whose task row has vanished) is an ORPHANED
# lease: the agent run is over, so the VM no longer backs active dispatch work.
# EVALUATING is deliberately EXCLUDED — an EVALUATING task is still legitimately
# in flight (waiting on its own children / a chain handoff), mirroring the
# ``pool_lease_orphaned`` reclaim in warm_pool_maintainer.
_ORPHANED_LEASE_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


async def count_orphaned_leased_pool_vms(
    session, *, project_id: uuid.UUID | None = None
) -> int:
    """Count LEASED pool members whose backing task is terminal or missing.

    Slot accounting (orchestrator dispatch loop) assumes a LEASED member is
    running an active task and is therefore already counted in ``active_count``
    (PROVISIONING/IN_PROGRESS tasks), so it deliberately does NOT subtract LEASED
    members from the GCP cross-check. When a leased member's task reaches a
    terminal state — or its row vanishes — without the lease being released (e.g.
    a leader crash / Cloud-Run cutover cancels the release mid-flight), the row
    lingers in LEASED. Such a member is in neither ``active_count`` nor
    ``IDLE_POOL_STATES``, so without this it phantom-consumes a dispatch slot
    forever, latching ``orchestrator_slots_full`` (prod 2026-06-13: a leader-flap
    deploy left ~16 stale leases that pinned ``slots_available`` at 0 with 0
    active tasks — a total dispatch freeze).

    Counting these lets the dispatcher exclude them from the budget as reclaimable
    inventory rather than consumed capacity; the maintainer's
    ``pool_lease_orphaned`` reaper tears the underlying VMs down out-of-band.
    """
    stmt = (
        select(func.count())
        .select_from(PoolVm)
        .outerjoin(Task, Task.id == PoolVm.leased_task_id)
        .where(
            PoolVm.state == PoolVmState.LEASED,
            or_(
                PoolVm.leased_task_id.is_(None),
                Task.id.is_(None),
                Task.status.in_(_ORPHANED_LEASE_TERMINAL_STATUSES),
            ),
        )
    )
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


async def is_pool_cost_guard_engaged(
    session, project_id: uuid.UUID | None = None
) -> bool:
    """True iff the cost-aware auto-drain guard is currently engaged.

    Cheap LIVE check — the in-memory dispatch funnel (lease-hit rate) plus a
    single pool idle-cost aggregate, never the once-per-day efficiency snapshot —
    so the dispatch hook can pause leasing the MOMENT the pool's idle spend stops
    being amortized by warm-start hits, rather than waiting for the next
    maintainer tick (~120s) to drain idle members. Delegates the threshold logic
    to ``warm_pool_maintainer.evaluate_pool_cost_guard`` so the lease side (here)
    and the provision side (the maintainer's reconcile) engage on the SAME
    configurable thresholds and disengage together once a healthy hit rate
    returns. When ``project_id`` is supplied, also evaluates the per-project
    guard (mirroring the maintainer's two-level check): if the fleet guard is
    disengaged but the per-project guard is engaged, leasing is still blocked for
    that project immediately — without waiting for the next maintainer tick to
    drain idle members. Deferred import avoids a warm_pool <->
    warm_pool_maintainer cycle.

    Degrades OPEN (returns False) on any evaluation error so a transient signal
    hiccup can never wedge the pool shut — the failure-based circuit breaker and
    the maintainer drain remain as backstops.
    """
    try:
        from app.warm_pool_maintainer import (
            evaluate_pool_cost_guard,
            evaluate_project_pool_cost_guard,
        )

        guard = await evaluate_pool_cost_guard(session, runtime_config=_runtime_config)
        if guard.get("engaged"):
            return True
        if project_id is not None:
            per_guard = await evaluate_project_pool_cost_guard(
                session, project_id, runtime_config=_runtime_config
            )
            return bool(per_guard.get("engaged"))
        return False
    except Exception:  # noqa: BLE001
        logger.debug(
            "[WARM-POOL] cost-guard evaluation failed; treating as disengaged",
            exc_info=True,
        )
        return False


def is_pool_vm_image_stale(member: PoolVm, stale_vm_ids: set[str]) -> bool:
    """True iff a pool member is running a stale agent image.

    Reuses the existing heartbeat-driven image-staleness detection
    (``compute.get_stale_image_vms``) — the caller passes the set of stale
    vm_instance_ids it produced — so a member whose baked image has aged past
    the staleness threshold is recycled rather than leased after a rebake.
    """
    return bool(member.vm_instance_id) and member.vm_instance_id in stale_vm_ids


async def lease_idle_pool_vm(
    session,
    *,
    project_id: uuid.UUID,
    account_id: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    excluded_account_ids: list[str] | None = None,
    leased_task_id: uuid.UUID | None = None,
    lease_ttl_secs: int | None = None,
    allow_account_mismatch: bool = False,
    allow_provider_mismatch: bool = False,
    lane_id: str | None = None,
) -> PoolVm | None:
    """Atomically lease an IDLE, compatible, same-tenant pool member, or None.

    Selects the oldest-idle IDLE member in ``project_id`` whose pinned identity
    is compatible with the candidate task's requirement, locking it with
    ``SELECT ... FOR UPDATE SKIP LOCKED`` so a concurrent dispatcher can never
    lease the same row twice (the loser's lock attempt skips it). The matched
    row is flipped to LEASED with ``leased_task_id`` / ``lease_expires_at`` set
    and ``idle_since`` cleared.

    Compatibility (all required for a match):
      - ``project_id`` MUST equal the member's project (hard tenant isolation;
        a member never serves another tenant's task).
      - When ``account_id`` / ``provider`` / ``auth_mode`` / ``model`` are provided, the member's
        corresponding pinned value MUST equal it. A None requirement is a
        wildcard for that dimension (the candidate task did not pin it). The VM
        runs on the credentials baked into its boot manifest, so an
        account/provider/auth-mode/model mismatch would silently run the task on the wrong
        account — hence these are equality gates, not best-effort. The caller is
        responsible for passing the RESOLVED identity (post LLM-routing), not the
        task's raw context, so routing/blacklist decisions are honored.
      - A member pinned to an account in ``excluded_account_ids`` is NEVER leased,
        even under an account wildcard. This is the rate-limit blacklist: the cold
        path routes around a blacklisted account, so the pool must too — otherwise
        the warm claim runs straight into the rate limit. NULL-account members are
        never blacklisted and stay eligible.

    Account-agnostic relaxation (``allow_account_mismatch`` /
    ``allow_provider_mismatch``): when ``allow_account_mismatch`` is True the
    account AND model equality gates are dropped, so ANY same-tenant idle member
    can be claimed for the task and its credentials are re-targeted to the
    resolved account at claim time (see ``agent_warm_continuity`` +
    ``build_account_retarget_manifest``). ``allow_provider_mismatch`` additionally
    drops the provider gate (cross-provider re-target). The HARD guarantees are
    NEVER relaxed: the ``project_id`` tenant gate and the ``excluded_account_ids``
    blacklist always apply. These flags default False, preserving strict equality;
    the caller (``try_pool_dispatch``) only sets them when the operator has
    enabled re-target AND the claim path will emit the re-materialization
    manifest, so a relaxed lease can never run on the wrong frozen credentials.

    The caller owns the transaction (commit). Returns the locked, now-LEASED
    member, or None when no compatible IDLE member is free.
    """
    now = _utcnow()
    filters = [
        PoolVm.project_id == project_id,
        PoolVm.state == PoolVmState.IDLE,
    ]
    # Account + auth mode + model are re-targeted together by the claim-time
    # manifest, so all three gates relax under allow_account_mismatch; provider
    # relaxes only with the additional cross-provider flag.
    if account_id is not None and not allow_account_mismatch:
        filters.append(PoolVm.account_id == account_id)
    if provider is not None and not allow_provider_mismatch:
        filters.append(PoolVm.provider == provider)
    if auth_mode is not None and not allow_account_mismatch:
        filters.append(PoolVm.auth_mode == auth_mode)
    if model is not None and not allow_account_mismatch:
        filters.append(PoolVm.model == model)
    if excluded_account_ids:
        _excluded = set(excluded_account_ids)
        filters.append(
            or_(
                PoolVm.account_id.is_(None),
                PoolVm.account_id.notin_(_excluded),
            )
        )

    # Reservation predicate: skip members reserved for a DIFFERENT (or any)
    # lane. A matching lane_id can claim its own reserved member; an expired
    # reservation makes the member generally claimable by anyone. These are
    # additional WHERE clauses (not a new ordering or a second query) so the
    # oldest-idle ordering and FOR UPDATE SKIP LOCKED are unchanged.
    if lane_id is not None:
        filters.append(
            or_(
                PoolVm.reserved_lane_id.is_(None),
                PoolVm.reservation_expires_at.is_(None),
                PoolVm.reservation_expires_at <= now,
                PoolVm.reserved_lane_id == uuid.UUID(str(lane_id)),
            )
        )
    else:
        # Non-lane (or lane-unaware) request: skip any actively reserved member.
        filters.append(
            or_(
                PoolVm.reserved_lane_id.is_(None),
                PoolVm.reservation_expires_at.is_(None),
                PoolVm.reservation_expires_at <= now,
            )
        )

    # When a lane_id is given, sort own-reserved members FIRST (CASE sort key 0)
    # before falling back to oldest-idle ordering. This ensures a lane's own
    # reserved member is always preferred over an older-idle generic member so
    # the warm workspace it checkpointed into is actually reused. The single
    # SELECT + FOR UPDATE SKIP LOCKED atomicity is fully preserved.
    if lane_id is not None:
        _lane_uuid = uuid.UUID(str(lane_id))
        _order_exprs = [
            case((PoolVm.reserved_lane_id == _lane_uuid, 0), else_=1).asc(),
            PoolVm.idle_since.asc().nullsfirst(),
            PoolVm.created_at.asc(),
        ]
    else:
        _order_exprs = [PoolVm.idle_since.asc().nullsfirst(), PoolVm.created_at.asc()]

    stmt = (
        select(PoolVm)
        .where(*filters)
        .order_by(*_order_exprs)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        logger.debug(
            "[WARM-POOL] no compatible IDLE member project=%s account=%s "
            "provider=%s model=%s lane=%s",
            project_id,
            account_id,
            provider,
            model,
            lane_id,
        )
        return None

    ttl = lease_ttl_secs if lease_ttl_secs is not None else _DEFAULT_LEASE_TTL_SECS
    expired_reservation_lane_id = (
        member.reserved_lane_id
        if member.reserved_lane_id is not None
        and member.reservation_expires_at is not None
        and member.reservation_expires_at <= now
        else None
    )
    # Claimed-by-fix: the lane is leasing a member STILL actively reserved to it
    # (reservation not expired) — the warm-resume success path. Captured before the
    # reservation fields are cleared so the expiry-cause telemetry can attribute it.
    claimed_own_reservation_lane_id = (
        member.reserved_lane_id
        if lane_id is not None
        and member.reserved_lane_id is not None
        and member.reserved_lane_id == uuid.UUID(str(lane_id))
        and member.reservation_expires_at is not None
        and member.reservation_expires_at > now
        else None
    )
    member.state = PoolVmState.LEASED
    member.leased_task_id = leased_task_id
    member.lease_expires_at = now + timedelta(seconds=ttl)
    # Bank the idle cost accrued while this member sat IDLE BEFORE clearing
    # idle_since — otherwise the measured idle spend of every leased member is
    # silently erased (the IDLE->LEASED transition is one of
    # settle_pool_vm_idle_cost's intended call sites). Deferred import avoids a
    # warm_pool <-> efficiency_snapshot import cycle.
    from app.efficiency_snapshot import settle_pool_vm_idle_cost

    settle_pool_vm_idle_cost(member, now=now, session=session)
    member.idle_since = None
    # Clear any reservation — a leased member holds no reservation.
    member.reserved_lane_id = None
    member.reservation_expires_at = None
    member.reservation_hard_expires_at = None
    object.__setattr__(
        member, "_expired_reservation_lane_id", expired_reservation_lane_id
    )
    session.add(member)
    if claimed_own_reservation_lane_id is not None:
        from app.runtime_config_state import is_improvement_lanes_enabled

        if is_improvement_lanes_enabled():
            await record_lane_reservation_cause(
                session,
                claimed_own_reservation_lane_id,
                "claimed_by_fix",
            )
    if expired_reservation_lane_id is not None:
        from app.runtime_config_state import is_improvement_lanes_enabled

        if is_improvement_lanes_enabled():
            await clear_lane_warm_workspace_if_matching_member(
                session,
                expired_reservation_lane_id,
                member.vm_instance_id,
            )
            await record_lane_reservation_cause(
                session,
                expired_reservation_lane_id,
                "expired_before_fix",
            )
    logger.info(
        "[WARM-POOL] leased pool VM vm_instance_id=%s project=%s task=%s "
        "lease_expires_at=%s",
        member.vm_instance_id,
        project_id,
        leased_task_id,
        member.lease_expires_at,
    )
    return member


async def promote_pool_vm_to_idle(session, vm_instance_id: str) -> bool:
    """Atomically flip a PROVISIONING member to IDLE on its first ready signal.

    The agent's EXISTING POST /api/agent/vm-heartbeat is the readiness signal: a
    booted pool VM polling /next-role fires this heartbeat, and the handler calls
    here to make the member leasable. This is an atomic, conditional Core UPDATE
    — NOT a read-then-write — that flips ONLY a member currently in PROVISIONING::

        UPDATE poolvm SET state='IDLE', idle_since=<now>
        WHERE vm_instance_id=:id AND state='PROVISIONING'

    The ``state == PROVISIONING`` predicate is the WHOLE correctness guard:
      - a concurrent promotion can never double-apply (the loser matches 0 rows);
      - a LEASED (mid-task) or RETIRING (mid-teardown) member is never flipped
        back to leasable — only a PROVISIONING member is touched;
      - repeated heartbeats are idempotent: once the member is IDLE the UPDATE
        matches 0 rows, so ``idle_since`` is never spuriously reset.

    Only ``state`` and ``idle_since`` are set; ``leased_task_id`` /
    ``lease_expires_at`` are deliberately NOT cleared here (those lease-binding
    semantics belong to the release path, :func:`mark_pool_vm_idle`). Unlike
    ``mark_pool_vm_idle`` this is conditional, so it is safe to call on the shared
    heartbeat path: a chain/backlog VM (no PoolVm row) or an already-IDLE/LEASED/
    RETIRING member is a no-op. Returns True iff a PROVISIONING member was flipped.
    The caller owns the transaction (commit).
    """
    result = await session.execute(
        update(PoolVm)
        .where(
            PoolVm.vm_instance_id == vm_instance_id,
            PoolVm.state == PoolVmState.PROVISIONING,
        )
        .values(state=PoolVmState.IDLE, idle_since=_utcnow())
    )
    return bool(result.rowcount)


async def release_pool_vm(
    session,
    vm_instance_id: str,
    *,
    owning_task_id: uuid.UUID | str,
    lane_id: str | None = None,
) -> PoolVm | None:
    """Atomically return a LEASED member to the IDLE pool so it can be re-leased.

    Called from the task-completion path when a pool-leased task reaches a
    *reusable* terminal state (COMPLETED): the member's agent loops back to
    polling ``/next-role`` (``bootstrapPoolMode`` -> ``pollForNextRole``), so the
    backend must promptly return the member to IDLE — otherwise it lingers
    LEASED, counting against the per-project target and the fleet budget
    (blocking replenishment) until the reaper's orphan-lease sweep eventually
    retires and DELETEs it, making every member single-use.

    This return is GUARDED to a ``state == LEASED`` row owned by
    ``owning_task_id`` and locked
    ``FOR UPDATE SKIP LOCKED`` before it is flipped to IDLE, so a member a
    concurrent reaper/maintainer already moved to RETIRING (mid-teardown) is
    NEVER resurrected onto the leasable set, a duplicate completion signal is a
    race-safe no-op, and a stale/non-owner task cannot return or reserve another
    task's lease. Returns the now-IDLE member, or None when no owned LEASED row
    matches (unknown / already reconciled / retiring / leased to another task).
    The caller owns the transaction (commit).

    Idle-cost banking is preserved trivially: a LEASED member's ``idle_since`` is
    already None (cleared at lease time, with its accrued span banked then), so
    the fresh IDLE window simply restarts from now with nothing to erase.

    ``lane_id``: when supplied (improvement lanes enabled, caller passes the lane's
    id), the owned member is returned IDLE stamped with ``reserved_lane_id`` /
    ``reservation_expires_at`` so the lane's next phase can reclaim its warm
    workspace. A normal (no-lane) release stamps no reservation and clears any
    prior reservation fields.
    """
    owner_id = uuid.UUID(str(owning_task_id))
    stmt = (
        select(PoolVm)
        .where(
            PoolVm.vm_instance_id == vm_instance_id,
            PoolVm.state == PoolVmState.LEASED,
            PoolVm.leased_task_id == owner_id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        return None
    now = _utcnow()
    member.state = PoolVmState.IDLE
    member.leased_task_id = None
    member.lease_expires_at = None
    member.idle_since = now
    if lane_id is not None:
        ttl = int(
            _runtime_config.get("warm_pool_lane_reservation_ttl_seconds", 1800) or 1800
        )
        hard_cap = int(
            _runtime_config.get("warm_pool_lane_reservation_hard_cap_seconds", 5400)
            or 5400
        )
        member.reserved_lane_id = uuid.UUID(str(lane_id))
        member.reservation_expires_at = now + timedelta(seconds=ttl)
        # Stamp the HARD absolute ceiling ONCE, here at reservation creation, with
        # its own anchor that survives the soft TTL being re-stamped by the
        # liveness sweep. The cap can never be exceeded, so a stuck lane cannot
        # pin a warm VM forever.
        member.reservation_hard_expires_at = now + timedelta(seconds=max(hard_cap, ttl))
    else:
        member.reserved_lane_id = None
        member.reservation_expires_at = None
        member.reservation_hard_expires_at = None
    session.add(member)
    logger.info(
        "[WARM-POOL] released pool VM vm_instance_id=%s task=%s -> IDLE "
        "(re-leasable) lane=%s",
        vm_instance_id,
        owner_id,
        lane_id,
    )
    return member


# Expiry-cause -> ImprovementLane counter column. Each warm reservation ends
# exactly one way; these counters make a low warm_resume_fraction diagnosable on
# the lanes analytics surface.
_LANE_RESERVATION_CAUSE_COLUMNS = {
    "claimed_by_fix": "reservation_claimed_by_fix_count",
    "expired_before_fix": "reservation_expired_before_fix_count",
    "force_expired": "reservation_force_expired_count",
}


async def record_lane_reservation_cause(
    session, lane_id: uuid.UUID | str, cause: str
) -> bool:
    """Increment the lane's expiry-cause counter for how a reservation ended.

    ``cause`` is one of ``claimed_by_fix`` / ``expired_before_fix`` /
    ``force_expired``. Implemented as a single atomic ``col = col + 1`` UPDATE so
    concurrent reservation lifecycle events never lose a count to a read-modify-
    write race. A no-op (returns False) for an unknown cause or a missing lane.
    The caller owns the transaction (commit).
    """
    column = _LANE_RESERVATION_CAUSE_COLUMNS.get(cause)
    if column is None:
        return False
    from app.models import ImprovementLane

    col = getattr(ImprovementLane, column)
    result = await session.execute(
        update(ImprovementLane)
        .where(ImprovementLane.id == uuid.UUID(str(lane_id)))
        .values({column: col + 1})
    )
    return bool(result.rowcount)


async def clear_lane_warm_workspace_if_matching_member(
    session,
    lane_id: uuid.UUID | str,
    workspace_vm_instance_id: str | None,
) -> bool:
    """Clear a lane workspace only while it still advertises a warm reservation."""
    from app.models import ImprovementLane

    result = await session.execute(
        update(ImprovementLane)
        .where(
            ImprovementLane.id == uuid.UUID(str(lane_id)),
            ImprovementLane.workspace_state != "COLD",
            ImprovementLane.workspace_vm_instance_id == workspace_vm_instance_id,
        )
        .values(workspace_state="COLD", workspace_vm_instance_id=None)
    )
    return bool(result.rowcount)


async def clear_lanes_for_torn_down_vm(
    session, vm_instance_id: str
) -> list["uuid.UUID"]:
    """Clear workspace state for all lanes whose backing VM was just torn down.

    Handles both WARM_RESERVED and LEASED states across ALL teardown paths
    (drain, cost-guard, stale-image retirement, idle-reaper) by matching on
    workspace_vm_instance_id rather than the pool VM's reserved_lane_id pointer.
    Returns the list of lane IDs that were cleared (empty list if none).
    """
    from app.models import ImprovementLane

    rows = (
        (
            await session.execute(
                select(ImprovementLane.id).where(
                    ImprovementLane.workspace_vm_instance_id == vm_instance_id,
                    ImprovementLane.workspace_state != "COLD",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    await session.execute(
        update(ImprovementLane)
        .where(ImprovementLane.id.in_(rows))
        .values(workspace_state="COLD", workspace_vm_instance_id=None)
    )
    return list(rows)


async def mark_pool_vm_retiring(
    session,
    vm_instance_id: str,
    *,
    source_states: tuple[PoolVmState, ...] = (
        PoolVmState.PROVISIONING,
        PoolVmState.IDLE,
    ),
) -> PoolVm | None:
    """Atomically mark a member RETIRING so it is never leased again.

    Uses ``SELECT … FOR UPDATE SKIP LOCKED`` restricted to ``source_states`` so a
    concurrent lease commit (IDLE → LEASED) causes this call to see no matching
    unlocked row and return None without clobbering the lease binding.

    Default ``source_states`` covers the vm-heartbeat pre-claim-failure path
    (member failed before its first lease — PROVISIONING or IDLE). Callers that
    need to retire an already-LEASED member (orchestrator pool-pin timeout,
    task-completion FAILED/CANCELLED, doomed-VM guard in try_pool_dispatch) must
    pass ``source_states=(PoolVmState.LEASED,)`` explicitly.

    Returns None when the member is unknown, not in a qualifying state, or the
    row is locked by a concurrent transaction (SKIP LOCKED). The caller owns the
    transaction (commit).
    """
    stmt = (
        select(PoolVm)
        .where(
            PoolVm.vm_instance_id == vm_instance_id,
            PoolVm.state.in_(source_states),
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        return None
    member.state = PoolVmState.RETIRING
    member.leased_task_id = None
    member.lease_expires_at = None
    # Bank accrued idle cost before clearing idle_since (IDLE->RETIRING is a
    # settle call site; a no-op for LEASED members whose idle_since is already
    # None — teardown must never erase measured idle spend).
    from app.efficiency_snapshot import settle_pool_vm_idle_cost

    settle_pool_vm_idle_cost(member, session=session)
    member.idle_since = None
    session.add(member)
    logger.info("[WARM-POOL] marked pool VM retiring vm_instance_id=%s", vm_instance_id)
    return member


# ---------------------------------------------------------------------------
# Dispatch hook: lease + pin an idle pool VM instead of cold-provisioning.
# ---------------------------------------------------------------------------
# Context marker stamped on a QUEUED task pinned to a pool VM by the dispatch
# hook. Read by the /next-role pool-claim branch so the warm claim is attributed
# to the pool (origin "warm_pool") and by the orchestrator's skip-provision
# branch so a pool pin is honored under the warm-pool flag, not warm-continuity.
WARM_POOL_PIN_KEY = "warm_pool_pin"
# Durable ownership marker for the account reservation acquired immediately
# before a warm-pool lease. This is deliberately distinct from llm_account_id:
# the latter is runtime attribution and can exist without a pre-acquired slot;
# this key means the lease owns exactly one active+daily reservation that must be
# consumed by a successful claim or reverted by an abandonment path.
WARM_POOL_RESERVATION_ACCOUNT_KEY = "_warm_pool_reservation_account_id"
# warm-claim origin value attributing a lift to the pool dispatch hook (alongside
# the existing "next_role_endpoint" and "backlog_reuse" origins).
WARM_POOL_ORIGIN = "warm_pool"


async def try_pool_dispatch(
    session,
    task: Task,
    *,
    required_provider: str | None = None,
    required_model: str | None = None,
    required_account: str | None = None,
    required_auth_mode: str | None = None,
    reserved_account_id: str | None = None,
    excluded_account_ids: list[str] | None = None,
    lease_ttl_secs: int | None = None,
) -> PoolVm | None:
    """Lease a compatible idle pool VM for a QUEUED task and pin it, or None.

    The orchestrator dispatch path calls this BEFORE cold-provisioning. On a
    non-None return the task has been pinned to the leased member (still QUEUED,
    ``vm_instance_id`` set, boot-account identity stamped into context) and the
    caller MUST commit and skip cold provisioning — the member's agent claims the
    task warm via ``/api/agent/next-role`` (origin ``"warm_pool"``). Returns None
    (caller cold-provisions as before) when:

      - the pool is disabled (``warm_pool_enabled`` False) or auto-suppressed by
        the pool circuit breaker;
      - no compatible, same-tenant IDLE member is free; or
      - the only candidate is already tearing down (in-flight-delete guard).

    ``required_provider`` / ``required_model`` / ``required_account`` are the
    task's RESOLVED LLM identity — the caller (orchestrator) computes them via
    ``select_llm_runtime_for_task`` so provider preference, category/quality
    routing, account availability AND the rate-limit blacklist are all honored
    BEFORE the pool is consulted. ``excluded_account_ids`` is the effective
    rate-limit blacklist; a member pinned to one of those accounts is never
    leased. Passing the task's RAW (unrouted) context here would silently bypass
    routing and lease a member on a blacklisted account — so the caller MUST pass
    the resolved values, not raw context.

    Compatibility/isolation is enforced by :func:`lease_idle_pool_vm`:
    ``project_id`` is a hard tenant gate and the resolved account/provider/model
    must be compatible with the member's pinned identity. The lease itself is
    atomic (FOR UPDATE SKIP LOCKED) so two concurrent dispatches — or a concurrent
    agent poll — can never double-lease one member.
    """

    if not is_warm_pool_enabled():
        return None
    from app.shared_state import is_warm_pool_suppressed, record_pool_claim_failure

    # Funnel: record WHY a consult produced no lease so lease_miss_by_reason /
    # lease_miss_total reflect real dispatch activity (the orchestrator falls to
    # the cold path on None without knowing the reason — only this helper does).
    from app.warm_backlog_reuse import record_pool_lease_miss

    # Per-project scope gate (same check the maintainer + admin-seed enforce).
    # The master switch above is the fleet kill-switch; this honors a BOUNDED
    # enable — when an operator scopes the pool to specific projects (or removes
    # one / clicks Disable), an out-of-scope task must fall to the cold path
    # IMMEDIATELY, not keep leasing the project's lingering idle members until the
    # maintainer's next reconcile drains them. Centralizing it here (alongside the
    # master-switch check) covers every current and future caller. Empty/unset
    # allowlist ⇒ fleet-wide (is_project_warm_pool_scoped returns True for all),
    # so legacy behavior is byte-identical.
    if not is_project_warm_pool_scoped(task.project_id):
        record_pool_lease_miss(
            "out_of_scope", account=required_account, project_id=task.project_id
        )
        return None

    if is_warm_pool_suppressed():
        record_pool_lease_miss("suppressed", project_id=task.project_id)
        return None

    # Cost-aware auto-drain: pause leasing while the pool's idle spend is not
    # being amortized by warm-start hits, so an unattended pool can't regress
    # cost-per-PR in the window before the maintainer drains idle members. The
    # guard reverses on its own (here and in the maintainer) once the hit rate
    # recovers above the configured floor.
    if await is_pool_cost_guard_engaged(session, project_id=task.project_id):
        record_pool_lease_miss("cost_guard", project_id=task.project_id)
        return None

    # The required identity is the caller's RESOLVED LLM selection (provider /
    # model / account post-routing, honoring the blacklist) — NOT the task's raw
    # context. The pool VM runs on the credentials baked into its boot manifest,
    # so a member may only serve a task whose resolved provider/model/account is
    # compatible with the VM's pinned identity (the lease helper treats a None
    # requirement as a wildcard for that dimension, and never leases a member
    # pinned to an account in ``excluded_account_ids``).
    # Account-agnostic lease (efficiency direction d-5fd69fe1). When the operator
    # has enabled account re-target, relax the account (+ model) equality gate so
    # ONE small idle pool serves the project's whole account mix — the claim path
    # re-materializes the resolved account's credentials before the role runs.
    # Cross-provider relaxation is a further opt-in. Both default off, so the pool
    # stays strict-equality until the agent re-materialize step ships; the HARD
    # tenant + blacklist gates always apply (see lease_idle_pool_vm).
    allow_account_mismatch = is_warm_pool_account_retarget_enabled()
    allow_provider_mismatch = (
        allow_account_mismatch and is_warm_pool_cross_provider_retarget_enabled()
    )

    # Lane-reservation awareness: read the improvement_lane_id from the task
    # context (when improvement lanes are enabled) and thread it into the lease
    # so the reserved-member predicate is applied. Reviews/audits carry no lane id
    # and are treated as non-lane lessees that skip reserved members. Completely
    # inert when improvement_lanes_enabled is False.
    from app.runtime_config_state import is_improvement_lanes_enabled

    lane_id: str | None = None
    if is_improvement_lanes_enabled():
        raw_lane = (task.context or {}).get("improvement_lane_id")
        if raw_lane is not None:
            lane_id = str(raw_lane)

    try:
        member = await lease_idle_pool_vm(
            session,
            project_id=task.project_id,
            account_id=required_account,
            provider=required_provider,
            auth_mode=required_auth_mode,
            model=required_model,
            excluded_account_ids=excluded_account_ids,
            leased_task_id=task.id,
            lease_ttl_secs=lease_ttl_secs,
            allow_account_mismatch=allow_account_mismatch,
            allow_provider_mismatch=allow_provider_mismatch,
            lane_id=lane_id,
        )
    except Exception:
        logger.exception("[WARM-POOL] lease error during dispatch for task %s", task.id)
        record_pool_claim_failure("lease_error")
        record_pool_lease_miss("lease_error", project_id=task.project_id)
        return None

    if member is None:
        # Distinguish the two opposite-fix causes of a pool miss: a genuinely
        # empty pool (grow it / raise the target) vs. idle members existing but
        # none compatible with the task's resolved account/provider/model while
        # retarget is off (flip the retarget toggle). The extra count runs ONLY
        # on the miss path, so the hit hot path is unaffected, and it respects
        # the same tenant/project scoping as the lease.
        idle_present = await count_idle_state_pool_vms(
            session, project_id=task.project_id
        )
        reason = "no_compatible_member" if idle_present else "no_idle_member"
        record_pool_lease_miss(
            reason, account=required_account, project_id=task.project_id
        )
        # Best-effort workspace-state correction: the lane cannot stay
        # WARM_RESERVED when no compatible member is available — its reservation
        # is stale or already gone. Degrade to COLD so the lane's next phase
        # cold-preps instead of waiting on a VM that is no longer there. Mirrors
        # the degradation pattern in sweep_idle_pool_vms Pass 2.
        if lane_id is not None and is_improvement_lanes_enabled():
            try:
                from app.models import ImprovementLane as _ImprovementLane

                _lane_vm_instance_id = (
                    (
                        await session.execute(
                            select(_ImprovementLane.workspace_vm_instance_id).where(
                                _ImprovementLane.id == uuid.UUID(lane_id),
                                _ImprovementLane.project_id == task.project_id,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if await clear_lane_warm_workspace_if_matching_member(
                    session,
                    lane_id,
                    _lane_vm_instance_id,
                ):
                    # Commit here: this is the miss path — we return None below
                    # so the caller never commits. The caller's only pending
                    # mutation on this path is task.context (blacklist update in
                    # orchestrator.py:3243-3249); that field is idempotent and
                    # expire_on_commit=False avoids the MissingGreenlet hazard,
                    # so committing ahead of the caller is safe on this branch.
                    await session.commit()
                    logger.debug(
                        "[WARM-POOL] lane %s has no compatible member — degraded to COLD",
                        lane_id,
                    )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[WARM-POOL] lane workspace-state correction failed lane=%s",
                    lane_id,
                    exc_info=True,
                )
        return None

    # In-flight-delete guard: never land a lease on a VM the orchestrator already
    # scheduled for teardown — the lease would point the task at a VM that dies
    # mid-claim. Retire the member and fall back to cold provisioning.
    from app.vm_lifecycle import is_orchestrator_initiated_delete

    is_doomed, delete_reason = is_orchestrator_initiated_delete(member.vm_instance_id)
    if is_doomed:
        logger.info(
            "[WARM-POOL] leased member %s is tearing down (reason=%s) — retiring; "
            "task %s falls back to cold provision",
            member.vm_instance_id,
            delete_reason,
            task.id,
        )
        await mark_pool_vm_retiring(
            session,
            member.vm_instance_id,
            source_states=(PoolVmState.LEASED,),
        )
        record_pool_claim_failure("doomed_vm")
        record_pool_lease_miss("doomed_vm", project_id=task.project_id)
        return None

    _pin_task_to_pool_vm(
        task,
        member,
        resolved_account=required_account,
        resolved_provider=required_provider,
        resolved_auth_mode=required_auth_mode,
        resolved_model=required_model,
        reserved_account_id=reserved_account_id,
    )
    # Best-effort: if the lane dispatched to a VM other than its reserved one,
    # update workspace_vm_instance_id so the lane row reflects the actual VM
    # the task is running on (the caller's commit will persist this).
    if lane_id is not None and is_improvement_lanes_enabled():
        try:
            from app.models import ImprovementLane as _ImprovementLane

            expired_reservation_lane_id = getattr(
                member, "_expired_reservation_lane_id", None
            )
            _lane_uuid = uuid.UUID(lane_id)
            _lane_obj = (
                (
                    await session.execute(
                        select(_ImprovementLane).where(
                            _ImprovementLane.id == _lane_uuid,
                            _ImprovementLane.project_id == task.project_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if (
                expired_reservation_lane_id is not None
                and _lane_uuid == expired_reservation_lane_id
            ):
                logger.debug(
                    "[WARM-POOL] lane %s claimed expired reservation on vm=%s — left workspace COLD",
                    lane_id,
                    member.vm_instance_id,
                )
            elif (
                _lane_obj is not None
                and _lane_obj.workspace_vm_instance_id != member.vm_instance_id
            ):
                _prior_vm = _lane_obj.workspace_vm_instance_id
                _lane_obj.workspace_vm_instance_id = member.vm_instance_id
                session.add(_lane_obj)
                logger.debug(
                    "[WARM-POOL] lane %s dispatched to vm=%s (was %s) — updated workspace_vm_instance_id",
                    lane_id,
                    member.vm_instance_id,
                    _prior_vm,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[WARM-POOL] lane workspace-vm correction failed lane=%s",
                lane_id,
                exc_info=True,
            )
    logger.info(
        "[WARM-POOL] dispatch hook leased member %s for task %s (project %s) — "
        "skipping cold provision",
        member.vm_instance_id,
        task.id,
        task.project_id,
    )
    return member


def _pin_task_to_pool_vm(
    task: Task,
    member: PoolVm,
    *,
    resolved_account: str | None = None,
    resolved_provider: str | None = None,
    resolved_auth_mode: str | None = None,
    resolved_model: str | None = None,
    reserved_account_id: str | None = None,
) -> None:
    """Pin a QUEUED task to a leased pool VM so its agent claims it warm.

    Mirrors the chain-continuity warm pin (``warm_target_vm_id`` +
    ``warm_handoff_deadline``) so the orchestrator's skip-provision branch leaves
    the task QUEUED while the member is alive, and the member's /next-role poll
    finds it. Adds the ``warm_pool_pin`` marker so the claim is attributed to the
    pool, and stamps the LLM identity the task will actually RUN ON into context
    so per-account concurrency / cost accounting is correct.

    The run identity is the task's RESOLVED account (``resolved_*``, post
    LLM-routing) when the task pinned one — under the account-agnostic lease the
    VM is re-targeted to that account at claim time, so accounting MUST attribute
    to it, NOT the member's frozen boot account. When the task pinned no account
    (account wildcard) there is nothing to re-target to, so the task runs on the
    member's boot identity and we attribute to that instead.
    """
    from app.max_plan_pool import apply_task_llm_runtime_context

    task.vm_instance_id = member.vm_instance_id
    new_ctx = dict(task.context or {})
    # Effective run identity: the resolved account the task will be re-targeted
    # to (if any), else the member's frozen boot identity.
    if resolved_account:
        run_account = resolved_account
        run_provider = resolved_provider
        run_auth_mode = resolved_auth_mode
        run_model = resolved_model
    else:
        run_account = member.account_id
        run_provider = member.provider
        run_auth_mode = member.auth_mode
        run_model = member.model
    if run_account:
        new_ctx = apply_task_llm_runtime_context(
            new_ctx,
            account_id=run_account,
            provider=run_provider,
            auth_mode=run_auth_mode,
            model=run_model,
        )
        # Keep the promoted column in sync with context (D2 over-fetch fix).
        task.llm_account_id = run_account
    new_ctx["warm_target_vm_id"] = member.vm_instance_id
    new_ctx[WARM_POOL_PIN_KEY] = True
    if reserved_account_id:
        new_ctx[WARM_POOL_RESERVATION_ACCOUNT_KEY] = str(reserved_account_id)
    deadline = member.lease_expires_at or (
        _utcnow() + timedelta(seconds=_DEFAULT_LEASE_TTL_SECS)
    )
    new_ctx["warm_handoff_deadline"] = deadline.isoformat()
    task.context = new_ctx
    flag_modified(task, "context")


def pool_reservation_account_id(task: Task | None) -> str | None:
    """Return the durable account reservation owned by a pool pin, if any."""
    if task is None or not isinstance(task.context, dict):
        return None
    value = task.context.get(WARM_POOL_RESERVATION_ACCOUNT_KEY)
    return str(value) if value else None


async def abandon_pool_dispatch(
    session,
    *,
    vm_instance_id: str,
    task: Task | None = None,
    task_id: uuid.UUID | str | None = None,
    reason: str,
) -> str | None:
    """Persistently abandon one pool handoff, then best-effort release its slot.

    The marker, task pin, and LEASED binding are cleared in one DB commit before
    the in-memory account counters are reverted. Re-entry after that commit sees
    no marker and therefore cannot stage another daily ``-1`` mutation.

    There is deliberately no outbox for the post-commit in-memory release. If
    that release fails or the process exits in the narrow gap, accounting stays
    conservatively over-counted: the existing DB-backed pool rebuild heals
    global/project active concurrency, while a persisted daily ``+1`` may remain
    until the existing daily reset. That conservative charge is the intentional
    no-outbox tradeoff; the task is still safely unpinned and cannot be
    dispatched twice. Successful claims consume the same marker and are
    released normally by task completion.
    """
    # Callers that pass ``task`` already hold its execution/selector lock and
    # intentionally use this helper to clean up non-QUEUED outcomes (for
    # example, capability-proof parking). Preserve that behavior. The task-id
    # path is different: forced drains race /next-role, so ownership must be
    # decided only after acquiring the same Task row lock as the claimant.
    member = None
    task_locked_here = False
    resolved_task_id = task.id if task is not None else task_id
    if task is None and resolved_task_id is None:
        member = await get_pool_vm(session, vm_instance_id)
        resolved_task_id = member.leased_task_id if member is not None else None
    if task is None and resolved_task_id is not None:
        task = (
            await session.execute(
                select(Task)
                .where(Task.id == uuid.UUID(str(resolved_task_id)))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        task_locked_here = True
    if member is None:
        # Lock order is Task -> PoolVm for the task-id path, matching
        # /next-role. get_pool_vm is an MVCC read (not FOR UPDATE), so a forced
        # drain that already owns the PoolVm CAS cannot deadlock with a claimant
        # that owns Task and is reading the member's prior committed identity.
        member = await get_pool_vm(session, vm_instance_id)

    cleanup_task = task
    if task_locked_here:
        # A successful /next-role commit transfers the reservation to normal
        # task-completion accounting. Once that happens the drain still owns
        # physical retirement, but it no longer owns the task pin/account slot.
        # Conversely, if this lock was acquired first, clearing the complete
        # QUEUED pool tuple makes the blocked claimant decline safely.
        locked_ctx = task.context if task is not None and isinstance(task.context, dict) else {}
        member_owns_handoff = bool(
            member is not None
            and member.state == PoolVmState.RETIRING
            and str(member.leased_task_id or "") == str(task.id if task else "")
        )
        still_owns_handoff = bool(
            task is not None
            and task.status == TaskStatus.QUEUED
            and locked_ctx.get(WARM_POOL_PIN_KEY)
            and str(locked_ctx.get("warm_target_vm_id") or "") == vm_instance_id
            and (
                task.vm_instance_id == vm_instance_id
                or member_owns_handoff
            )
        )
        if not still_owns_handoff:
            cleanup_task = None

    reservation_account_id = pool_reservation_account_id(cleanup_task)
    reservation_project_id = (
        str(cleanup_task.project_id)
        if cleanup_task is not None and cleanup_task.project_id
        else None
    )
    task_label = str(task.id) if task is not None else str(resolved_task_id or "")
    if cleanup_task is not None:
        ctx = dict(cleanup_task.context or {})
        for key in (
            "warm_target_vm_id",
            WARM_POOL_PIN_KEY,
            "warm_target_pr",
            "warm_handoff_deadline",
            WARM_POOL_RESERVATION_ACCOUNT_KEY,
        ):
            ctx.pop(key, None)
        cleanup_task.context = ctx
        if cleanup_task.vm_instance_id == vm_instance_id:
            cleanup_task.vm_instance_id = None
        flag_modified(cleanup_task, "context")
        session.add(cleanup_task)

    expected_task_id = task.id if task is not None else resolved_task_id
    if member is not None and (
        expected_task_id is None
        or member.leased_task_id is None
        or str(member.leased_task_id) == str(expected_task_id)
    ):
        if member.state == PoolVmState.RETIRING:
            # Forced drains atomically claimed LEASED -> RETIRING while retaining
            # the returned task id. Clear that binding in the same commit as the
            # task pin; the physical teardown follows after this helper returns.
            member.leased_task_id = None
            member.lease_expires_at = None
            session.add(member)
        else:
            await mark_pool_vm_retiring(
                session,
                vm_instance_id,
                source_states=(PoolVmState.LEASED,),
            )

    await session.commit()
    if reservation_account_id:
        from app.max_plan_pool import release_max_plan_account

        try:
            await release_max_plan_account(
                reservation_account_id,
                revert_daily_task_count=True,
                project_id=reservation_project_id,
            )
        except Exception:  # DB cleanup already committed
            logger.exception(
                "[WARM-POOL] reservation release failed after abandon commit; "
                "active capacity remains over-counted until pool rebuild and a "
                "persisted daily charge may remain until daily reset "
                "vm=%s task=%s account=%s",
                vm_instance_id,
                task_label,
                reservation_account_id,
            )
    logger.info(
        "[WARM-POOL] abandoned dispatch vm=%s task=%s reason=%s reservation=%s",
        vm_instance_id,
        task_label,
        reason,
        reservation_account_id,
    )
    return reservation_account_id


# NOTE: the pool-hit-vs-cold-dispatch funnel lives in warm_backlog_reuse
# (record_pool_lease_hit / record_pool_cold_dispatch / record_pool_lease_miss /
# get_pool_funnel_snapshot) — that is the set the orchestrator records into and
# admin_health surfaces. A duplicate trio used to live here but was unwired and
# divergent; it was removed so there is a single funnel source of truth.
