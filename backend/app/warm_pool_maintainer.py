"""Warm-pool maintainer service + idle-reaper sweep (efficiency direction d-5fd69fe1).

Makes pre-provisioned idle pool VMs first-class to the fleet's lifecycle:

* :func:`pool_maintainer_service` is a LEADER-ONLY background service that
  reconciles the live pool toward its configured target — provisioning new
  taskless members below target, draining excess idle members above target, and
  recycling members whose baked agent image has gone stale. It respects both the
  dispatch concurrency budget (so the pool never starves real task dispatch) and
  a dedicated fleet-wide pool budget (``warm_pool_max_vms``).

* :func:`sweep_idle_pool_vms` is a dedicated reaper pass (called as the reaper's
  Step 11) that retires pool members that are over their idle-TTL, are zombies
  (no heartbeat beyond the liveness window), carry a stale image, or are stuck
  RETIRING — deleting the VM, its pre-staged secret, and its registry row.

Everything here is INERT while ``warm_pool_enabled`` is False (the default): the
maintainer no-ops, and the reaper sweep returns immediately. The pool circuit
breaker (``shared_state.is_warm_pool_suppressed``) gates replenishment so a bad
pool image silently failing a stream of tasks halts provisioning until it
recovers.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import func, or_, select, update

from app.database import AsyncSessionLocal
from app.models import (
    PoolIdleCostLedger,
    PoolVm,
    PoolVmState,
    Project,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
from app.warm_pool import (
    IDLE_POOL_STATES,
    LIVE_POOL_STATES,
    count_idle_pool_vms,
    count_live_pool_vms,
    is_pool_vm_image_stale,
    is_project_warm_pool_scoped,
    is_warm_pool_enabled,
    resolve_project_warm_pool_target,
)

logger = logging.getLogger("blueprint")

# A provisioner is an async callable that boots ONE taskless pool VM for a
# project and registers it in PoolVm, returning True on success. Injected so the
# reconcile logic is unit-testable with a stub and the production credential
# resolution lives in one place (_default_pool_provision_fn).
#
# Signature is intentionally loose (``Callable[..., ...]``): the production fn
# and demand-weighted reconcile accept an optional keyword ``account_id`` to
# steer WHICH account a member boots on, but legacy/test stubs with the old
# ``(session, project_id)`` arity still satisfy the alias — reconcile only
# passes ``account_id`` when it has actually chosen one (greedy fallback
# otherwise), so those stubs are never handed the extra keyword.
ProvisionFn = Callable[..., Awaitable[bool]]

# How recently a member must have been flipped to RETIRING for an *adoption*
# (``pool_retiring_cleanup``) sweep to treat the teardown as owned by a live
# peer and stand down. A pool row's ``updated_at`` is bumped only on an explicit
# DB write (PoolVm rows are NOT heartbeat-bumped — heartbeats live in
# warm_vm_state keyed on vm_instance_id), so a RETIRING row whose ``updated_at``
# is fresh means a peer claimed it within this window and is mid-teardown; one
# whose ``updated_at`` is older than this is a stuck row from a crashed sweep
# that this pass should adopt and finish. Sized to comfortably exceed a single
# ``_ensure_vm_deleted`` (~240s worst case) so a peer's in-flight teardown is
# never duplicated.
_RETIRING_CLAIM_GRACE_SECONDS = 300

# Wall-clock instant this PROCESS began (module import). The zombie-teardown
# branch of the idle reaper keys liveness off the PROCESS-LOCAL in-memory
# heartbeat map (``warm_vm_state.vm_last_heartbeat``), which is NEVER hydrated
# from a durable signal at startup and is populated only by /vm-heartbeat,
# /next-role, /claim handlers as they land on THIS instance. After any
# deploy / crash / Cloud-Run cutover / leader failover the fresh leader's map
# starts EMPTY, so every established IDLE/LEASED/PROVISIONING pool member older
# than the staleness window would be misclassified a zombie and torn down (real
# GCP VM + registry row) before its next heartbeat lands — draining the whole
# pool and defeating the feature. We suppress the destructive zombie teardown
# until the process has been up long enough for established members to have
# heartbeated back to it (the warmup window). The OTHER reap reasons
# (idle-TTL, stale-image, stuck-RETIRING, orphaned-lease) do NOT depend on the
# in-memory map for their PRIMARY signal, so they remain armed immediately.
_PROCESS_START_TS: datetime = _utcnow()


def pool_breaker_state(runtime_config) -> dict:
    """Telemetry snapshot of the warm-pool circuit breaker.

    ``record_pool_claim_failure`` (shared_state facade) feeds the breaker from
    the dispatch/finalize failure path when a pool-leased task fails; the
    maintainer and dispatch leasing consult ``is_warm_pool_suppressed`` and
    stand down while ``suppressed`` is True. Exposed for the admin health /
    diagnostics surface.
    """
    # Read the suppression deadline as a LIVE module attribute (a `from ... import`
    # would freeze the value at import time — the scalar is rebound in place).
    from app import shared_state as _shared_state

    deadline = _shared_state.warm_pool_suppressed_until
    return {
        "suppressed": deadline is not None and _utcnow() < deadline,
        "suppressed_until": deadline.isoformat() if deadline else None,
        "recent_failures_1h": _shared_state.count_recent_pool_claim_failures(3600),
        "failure_rate_threshold": int(
            runtime_config.get("warm_pool_failure_rate_threshold", 5)
        ),
    }


def _pool_target_size(runtime_config) -> int:
    """Per-project IDLE target. Global ``warm_pool_target_size`` (default 0).

    Default 0 keeps the pool empty even after the master switch flips — an
    operator must set a positive target before any provisioning happens.
    """
    try:
        return max(0, int(runtime_config.get("warm_pool_target_size", 0)))
    except (TypeError, ValueError):
        return 0


def _pool_max_vms(runtime_config) -> int:
    """Fleet-wide ceiling on live pool members (dedicated pool budget)."""
    try:
        return max(0, int(runtime_config.get("warm_pool_max_vms", 20)))
    except (TypeError, ValueError):
        return 20


def _demand_window_minutes(runtime_config) -> int:
    """Lookback window (minutes) for the recent-cold-dispatch demand signal."""
    try:
        return max(0, int(runtime_config.get("warm_pool_demand_window_minutes", 30)))
    except (TypeError, ValueError):
        return 30


_COST_GUARD_ALERT_TITLE = "Warm pool cost guard engaged: idle spend not amortized"


def _cost_guard_config(runtime_config) -> dict:
    """Thresholds for the cost-aware auto-drain guard (safe defaults).

    The guard is a cheap circuit-breaker on cost: it engages when the pool has
    accrued real idle spend that recent warm-start hits are NOT amortizing, so an
    operator can enable the dark pool without risking an unattended
    cost-per-merged-PR regression.
    """

    def _f(key, default):
        try:
            return float(runtime_config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key, default):
        try:
            return int(runtime_config.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(runtime_config.get("warm_pool_cost_guard_enabled", True)),
        # Below this hit rate the pool is spending on idle VMs without delivering
        # reuse. Disengages (provisioning resumes) once hit rate recovers above it.
        "min_hit_rate": _f("warm_pool_cost_guard_min_hit_rate", 0.10),
        # Don't act until enough dispatches have happened to trust the hit rate.
        "min_dispatches": _i("warm_pool_cost_guard_min_dispatches", 20),
        # Don't act until idle spend is material enough to be worth protecting.
        "min_idle_cost_usd": _f("warm_pool_cost_guard_min_idle_cost_usd", 1.0),
        "window_hours": _i("warm_pool_cost_guard_window_hours", 24),
    }


async def evaluate_pool_cost_guard(session, *, runtime_config) -> dict:
    """Cheap live cost-regression check on the warm pool.

    Engages when ALL hold over the rolling window: enough ELIGIBLE dispatches to
    trust the signal, an eligible pool hit rate below the floor, and fleet idle
    spend above the floor. Uses the LIVE rolling dispatch funnel
    (``get_pool_funnel_snapshot``) and the LIVE pool idle-cost accrual
    (``compute_pool_idle_cost``) — never the once-per-day efficiency snapshot —
    so it is cheap to evaluate every tick and reverses as soon as a healthy hit
    rate returns.

    Gates on ``eligible_hit_rate`` / ``eligible_dispatches`` (hits + only the
    misses where the pool was genuinely able to serve the task), NOT the raw
    fleet-wide ``pool_hit_rate``. The raw rate counts dispatches the pool was
    never eligible to serve — other projects (the pool is per-project scoped),
    the pool's own breaker/cost-guard pauses, and tasks that never consult the
    pool — so gating on it let a scoped pool's rate get crushed by out-of-scope
    traffic, engage the guard, drain the pool, and stay engaged (the guard's own
    ``cost_guard`` misses fed back into the raw rate). The eligible rate excludes
    all of that, so the guard only acts on demand the pool could actually serve
    and can recover once real warm starts resume.
    """
    from app.efficiency_snapshot import compute_pool_idle_cost
    from app.warm_backlog_reuse import get_pool_funnel_snapshot

    cfg = _cost_guard_config(runtime_config)
    funnel = get_pool_funnel_snapshot(cfg["window_hours"])
    hit_rate = funnel.get("eligible_hit_rate")
    total = funnel.get("eligible_dispatches", 0)
    raw_hit_rate = funnel.get("pool_hit_rate")
    raw_total = funnel.get("total_dispatches", 0)
    idle_cost = (await compute_pool_idle_cost(session)).get("total_idle_cost_usd", 0.0)

    engaged = (
        cfg["enabled"]
        and total >= cfg["min_dispatches"]
        and hit_rate is not None
        and hit_rate < cfg["min_hit_rate"]
        and idle_cost >= cfg["min_idle_cost_usd"]
    )
    reason = None
    if engaged:
        reason = (
            f"Eligible pool hit rate {hit_rate:.1%} is below the "
            f"{cfg['min_hit_rate']:.0%} floor over {cfg['window_hours']}h "
            f"({total} eligible dispatches) while idle spend is ${idle_cost:.2f} "
            f"— draining idle members and pausing provisioning until the hit rate "
            f"recovers."
        )
    return {
        "engaged": engaged,
        "reason": reason,
        # The rate/volume the guard acts on (eligible). Field names kept stable so
        # the operator panel renders the decision values; raw_* carries the
        # fleet-wide figures for reference.
        "hit_rate": hit_rate,
        "total_dispatches": total,
        "raw_hit_rate": raw_hit_rate,
        "raw_total_dispatches": raw_total,
        "idle_cost_usd": idle_cost,
        # Floors the panel renders the current values against (hit-rate-vs-floor,
        # idle-spend-vs-floor) so an operator can see how far the pool is from
        # disengaging the guard. Additive — existing consumers ignore them.
        "min_hit_rate": cfg["min_hit_rate"],
        "min_idle_cost_usd": cfg["min_idle_cost_usd"],
    }


async def _maybe_emit_cost_guard_alert(session, project_id: uuid.UUID, reason: str) -> None:
    """Emit a deduplicated WARNING alert when the cost guard engages for a project.

    Mirrors the circuit-breaker alert pattern (alert_dispatch): skip if an
    undismissed cost-guard alert already exists, otherwise create + broadcast one.
    Best-effort — a broadcast hiccup never blocks the drain.
    """
    from app.models import Alert, AlertSeverity

    existing = (
        await session.execute(
            select(Alert).where(
                Alert.project_id == project_id,
                Alert.task_id.is_(None),
                Alert.dismissed_at.is_(None),
                Alert.title == _COST_GUARD_ALERT_TITLE,
            )
        )
    ).scalars().first()
    if existing is not None:
        return
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=_COST_GUARD_ALERT_TITLE,
            message=reason,
        )
    )
    await session.commit()
    try:
        from app.broadcast import broadcast_to_project

        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {"severity": "WARNING", "title": _COST_GUARD_ALERT_TITLE},
        )
    except Exception:
        logger.debug("[WARM-POOL] cost-guard alert broadcast failed", exc_info=True)


async def retire_and_delete_pool_vm(
    session,
    member: PoolVm,
    *,
    reason: str,
    claim_grace_seconds: int = _RETIRING_CLAIM_GRACE_SECONDS,
) -> bool:
    """Atomically CLAIM a pool member, then tear it down. Returns True iff we won.

    Concurrency safety is by an atomic compare-and-swap claim, NOT by a row lock
    held across the loop. The idle-reaper (reaper Step 11) and the maintainer
    tick are both leader-only but run as interleaving asyncio tasks on SEPARATE
    DB sessions, and their candidate scans are lock-free — so both can hold a
    reference to the same member at once. A FOR-UPDATE lock would not help here:
    this function must ``commit`` to release the RETIRING marker before the slow
    (~240s) GCP ``_ensure_vm_deleted``, and that commit drops every row lock in
    the transaction (the exact footgun the orphan reaper documents at
    reaper.py:1148). Instead we claim the row with a single conditional
    ``UPDATE ... WHERE <still-claimable>`` whose affected-row count tells us
    whether THIS call won:

      * an IDLE/PROVISIONING member is flipped to RETIRING → we won, proceed.
      * a member a peer already LEASED (a dispatcher grabbed it after the scan),
        already flipped to RETIRING and is mid-teardown, or already deleted →
        the UPDATE matches 0 rows → we lost, return False without touching GCP.

    The ``pool_retiring_cleanup`` reason adopts a STUCK RETIRING row left by a
    crashed prior teardown, guarded on ``updated_at`` staleness
    (``claim_grace_seconds``) so a peer's in-flight teardown is never duplicated.

    After winning, the RETIRING marker is committed, then the GCP VM + its
    pre-staged secret are deleted, then the registry row is removed. A crash
    after the marker commit leaves a stale-``updated_at`` RETIRING row that the
    next sweep adopts.
    """
    from app.vm_lifecycle import _ensure_vm_deleted

    vm_id = member.vm_instance_id
    member_id = member.id
    now = _utcnow()

    if reason == "pool_retiring_cleanup":
        # Adopt a STUCK RETIRING row only — never a freshly-claimed one (a live
        # peer owns that teardown). updated_at staleness is the liveness proxy.
        claim_where = (
            PoolVm.state == PoolVmState.RETIRING,
            PoolVm.updated_at < now - timedelta(seconds=claim_grace_seconds),
        )
    elif reason == "pool_lease_orphaned":
        # Reclaim a STRANDED lease: the idle-reaper found a LEASED member whose
        # handoff window (lease_expires_at) expired and whose task already reached
        # a terminal state (or vanished). A LEASED member is never re-leased
        # (leases only select IDLE rows), so re-validating state==LEASED under the
        # atomic claim is race-safe — a peer can only have already retired/deleted
        # it (claim matches 0 rows → we stand down). Without this branch a
        # completed/failed leased member stays LEASED forever and the pool
        # self-exhausts (it keeps counting as a live member, halting replenish).
        claim_where = (PoolVm.state == PoolVmState.LEASED,)
    else:
        # Claim a non-task-bound, not-yet-retiring member. A LEASED member is
        # excluded so a dispatcher that leased it between the scan and now is
        # never torn down mid-task; an already-RETIRING member is excluded so a
        # peer's teardown is never duplicated.
        claim_where = (
            PoolVm.state.in_((PoolVmState.PROVISIONING, PoolVmState.IDLE)),
        )

    # Bank any idle cost accrued since idle_since BEFORE the claim nulls it,
    # folded into the atomic UPDATE so only the claim winner persists it. We must
    # NOT mutate the member object first (the canonical settle_pool_vm_idle_cost
    # path) because losing the compare-and-swap would otherwise still flush a
    # banked-cost write onto a row a peer owns, double-counting the span. Mirrors
    # efficiency_snapshot.settle_pool_vm_idle_cost; teardown never erases spend.
    from app.vm_cost_rates import idle_cost_usd

    accrued = 0.0
    if member.idle_since is not None:
        elapsed = (now - member.idle_since).total_seconds()
        if elapsed > 0:
            accrued = idle_cost_usd(elapsed)
    banked = (member.idle_cost_banked or 0.0) + accrued

    result = await session.execute(
        update(PoolVm)
        .where(PoolVm.id == member_id, *claim_where)
        .values(
            state=PoolVmState.RETIRING,
            leased_task_id=None,
            lease_expires_at=None,
            idle_since=None,
            idle_cost_banked=banked,
            updated_at=now,
        )
    )
    claimed = result.rowcount or 0
    await session.commit()
    if claimed == 0:
        # Lost the claim — a peer leased / is retiring / already deleted it.
        logger.debug(
            "[WARM-POOL] skip teardown of %s (reason=%s) — already "
            "claimed/leased/retired by a peer",
            vm_id,
            reason,
        )
        return False

    # Delete the VM (best-effort; _ensure_vm_deleted also cleans the secret and
    # is idempotent if the orphan-cleanup sweep already removed it).
    try:
        await _ensure_vm_deleted(vm_id, reason=reason)
    except Exception:
        logger.warning(
            "[WARM-POOL] VM delete failed for retiring pool member %s (reason=%s); "
            "row left RETIRING for the next sweep",
            vm_id,
            reason,
            exc_info=True,
        )
        return False

    # Drop the registry row. Re-fetch within this session and delete only if it
    # still exists — idempotent if a peer/orphan-cleanup already removed it (no
    # StaleDataError on a concurrently-deleted row). BEFORE deleting, carry the
    # member's banked idle cost forward into the durable ledger IN THE SAME
    # transaction as the delete — otherwise the hard-delete erases measured idle
    # spend and cost_per_merged_pr_incl_pool_idle undercounts (the deleted row's
    # idle_cost_banked is summed by compute_pool_idle_cost only while the row
    # exists). One atomic commit means a concurrent snapshot never sees both the
    # RETIRING row AND its ledger row (no double count).
    fresh = await session.get(PoolVm, member_id)
    if fresh is not None:
        carried = fresh.idle_cost_banked or 0.0
        if carried > 0:
            session.add(
                PoolIdleCostLedger(
                    project_id=fresh.project_id,
                    vm_instance_id=vm_id,
                    idle_cost_usd=carried,
                    reason=reason,
                )
            )
        await session.delete(fresh)
        await session.commit()
    logger.info(
        "[WARM-POOL] retired pool VM vm_instance_id=%s reason=%s", vm_id, reason
    )
    return True


async def _default_pool_provision_fn(
    session, project_id: uuid.UUID, *, account_id: str | None = None
) -> bool:
    """Production provisioner: resolve a credentialed account and boot a pool VM.

    Reads an eligible LLM account for the project's owner from the in-memory pool
    WITHOUT reserving a concurrency slot (an idle pool VM is not running a task;
    the account slot is consumed only when the member is later leased — see the
    boot-account credit fix). Returns False (and logs) when no ready account is
    available so the maintainer simply tries again next tick.

    When ``account_id`` is given (the demand-weighted reconcile path), boot on
    THAT specific account so the same small idle pool spreads across a project's
    account mix; the account must still pass the existing readiness/cap gates. A
    ``None`` ``account_id`` preserves the original greedy first-eligible
    behavior.
    """
    from app.max_plan_pool import (
        _account_at_cap,
        _account_runtime_ready,
        _max_plan_pool_lock,
        _max_plan_pools,
        _normalize_auth_mode,
        _normalize_provider_name,
    )

    project = await session.get(Project, project_id)
    if project is None:
        return False
    owner_id = project.user_id
    if not owner_id:
        logger.debug("[WARM-POOL] project %s has no owner — skip provision", project_id)
        return False

    async with _max_plan_pool_lock:
        pool = list(_max_plan_pools.get(owner_id, []))
    account = None
    for acct in pool:
        if not acct.get("enabled", True):
            continue
        if _account_at_cap(acct):
            continue
        if not _account_runtime_ready(acct):
            continue
        # Demand-weighted path: only the requested account is acceptable. A
        # None account_id keeps the greedy first-eligible behavior.
        if account_id is not None and str(acct.get("id")) != str(account_id):
            continue
        account = acct
        break
    if account is None:
        logger.info(
            "[WARM-POOL] no ready account for project %s owner %s (account_id=%s) "
            "— skip provision",
            project_id,
            owner_id,
            account_id,
        )
        return False

    from app.compute import provision_pool_vm

    vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
    provider = _normalize_provider_name(account.get("provider"))
    auth_mode = _normalize_auth_mode(account.get("auth_mode"))
    try:
        booted = await provision_pool_vm(
            session,
            vm_instance_id,
            project_id,
            account_id=account.get("id"),
            oauth_token=account.get("runtime_secret") or account.get("oauth_token"),
            claude_config_json=account.get("claude_config_json"),
            provider=provider,
            auth_mode=auth_mode,
            model=account.get("model"),
            provider_metadata=account.get("provider_metadata") or {},
            provider_settings=account.get("provider_settings"),
        )
        if booted is None:
            # _provision_gcp hit a normal failure (capacity/timeout) and already
            # cleaned up the staged secret — no PoolVm row was registered, so there
            # is nothing to commit. Roll back and retry next tick. Suppress the
            # rollback: if the connection was severed mid-provision by a deploy
            # cutover / instance drain, rollback() raises InterfaceError
            # ("connection is closed") and escapes the maintainer loop (prod
            # 2026-06-03). There is nothing to roll back when the conn is already
            # dead — the pool listener has invalidated it.
            with contextlib.suppress(Exception):
                await session.rollback()
            logger.info(
                "[WARM-POOL] pool VM did not boot for project %s — skip provision",
                project_id,
            )
            return False
        await session.commit()
        logger.info(
            "[WARM-POOL] provisioned pool VM %s for project %s (account=%s)",
            vm_instance_id,
            project_id,
            account.get("id"),
        )
        return True
    except Exception:
        # Guard the rollback for the same reason as the booted-None branch above:
        # a severed connection makes rollback() raise and mask the real error.
        with contextlib.suppress(Exception):
            await session.rollback()
        logger.warning(
            "[WARM-POOL] pool provision failed for project %s vm=%s",
            project_id,
            vm_instance_id,
            exc_info=True,
        )
        return False


async def _stale_pool_image_ids() -> set[str]:
    """vm_instance_ids whose heartbeat-reported image should be recycled.

    Union of two signals so the fleet converges on the promoted image:
      * temporal staleness (``compute.get_stale_image_vms`` — the reported image
        aged past the fixed staleness threshold); and
      * pre-promotion outdatedness (``compute.get_outdated_image_vms`` — a newer
        valid image already exists in the family target), so a VM booted before a
        promotion is recycled as soon as the newer image lands rather than
        lingering on its old boot.
    """
    try:
        from app.compute import get_outdated_image_vms, get_stale_image_vms

        ids = {v["vm_id"] for v in get_stale_image_vms() if v.get("vm_id")}
        ids |= {v["vm_id"] for v in get_outdated_image_vms() if v.get("vm_id")}
        return ids
    except Exception:
        logger.warning("[WARM-POOL] stale-image detection failed", exc_info=True)
        return set()


async def _account_demand_for_project(
    session, project_id: uuid.UUID, *, runtime_config
) -> dict[str | None, int]:
    """Per-account dispatch demand for ONE project, derived purely from the DB.

    Mirrors the project-level :func:`_demand_active_projects` signal but bucketed
    by the LLM account each task resolved/booted on: count this project's
    eligible QUEUED backlog plus its recent COLD dispatches
    (``Task.vm_dispatched_at`` within ``warm_pool_demand_window_minutes``), and
    attribute each to ``get_task_llm_account_id`` (the account recorded in the
    task's context). The returned mapping ``{account_id: count}`` is the supply
    side's target distribution — it tells the maintainer WHICH accounts have
    real, recent demand so the same small idle pool spreads across a project's
    account mix instead of piling onto the first eligible account.

    Tasks with no resolvable account land under the ``None`` key; the caller
    ignores that bucket (it cannot be attributed to a bootable account) and
    falls back to the greedy first-eligible provision for those slots.

    Full Task rows are loaded (not a projection) so reading ``task.context`` via
    ``get_task_llm_account_id`` never triggers an async lazy-load.
    """
    from app.max_plan_pool import get_task_llm_account_id

    cutoff = _utcnow() - timedelta(minutes=_demand_window_minutes(runtime_config))
    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    or_(
                        Task.status == TaskStatus.QUEUED,
                        Task.vm_dispatched_at >= cutoff,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    demand: dict[str | None, int] = {}
    for task in tasks:
        acct = get_task_llm_account_id(task)
        key = str(acct) if acct else None
        demand[key] = demand.get(key, 0) + 1
    return demand


async def _eligible_pool_accounts(session, project_id: uuid.UUID) -> dict[str, dict]:
    """``{account_id: account_dict}`` of the project owner's bootable accounts.

    An account is bootable when it is enabled, runtime-ready, not at any usage
    cap, and not in a rate-limit cooldown — the SAME gates the greedy
    :func:`_default_pool_provision_fn` applies, plus the rate-limit blacklist.
    Used by :func:`reconcile_project_pool` to restrict demand-weighted
    distribution to accounts that can actually boot a member right now.
    """
    from app.max_plan_pool import (
        _account_at_cap,
        _account_runtime_ready,
        _max_plan_pool_lock,
        _max_plan_pools,
        account_in_rate_limit_cooldown,
    )

    project = await session.get(Project, project_id)
    if project is None or not project.user_id:
        return {}
    async with _max_plan_pool_lock:
        pool = list(_max_plan_pools.get(project.user_id, []))
    eligible: dict[str, dict] = {}
    for acct in pool:
        acct_id = acct.get("id")
        if not acct_id:
            continue
        if not acct.get("enabled", True):
            continue
        if _account_at_cap(acct):
            continue
        if not _account_runtime_ready(acct):
            continue
        if account_in_rate_limit_cooldown(str(acct_id)):
            continue
        eligible[str(acct_id)] = acct
    return eligible


def _pick_demand_weighted_account(
    demand_eligible: dict[str, int], supply: dict[str | None, int]
) -> str | None:
    """Water-fill: the demand-active account most under-served by current supply.

    Returns the eligible account with the largest ``demand - current_idle_supply``
    gap so each provisioned member broadens coverage; an account already
    over-represented among idle members has a smaller (or negative) gap and is
    deprioritized. Returns None when there is no demand-attributable eligible
    account (caller then falls back to greedy provisioning).
    """
    best: str | None = None
    best_gap = 0
    for acct_id, want in demand_eligible.items():
        gap = want - supply.get(acct_id, 0)
        if best is None or gap > best_gap:
            best, best_gap = acct_id, gap
    return best


async def reconcile_project_pool(
    session,
    project_id: uuid.UUID,
    *,
    runtime_config,
    provision_fn: ProvisionFn,
    dispatch_slots_remaining: int,
    pool_budget_remaining: int,
    stale_vm_ids: set[str],
    suppressed: bool,
    cost_guard_engaged: bool = False,
) -> dict:
    """Reconcile one project's live pool toward its target. Returns a summary.

    Order of operations:
      1. Recycle stale-image members (retire+delete) — always, even when the
         circuit breaker is tripped (a bad image is exactly what trips it).
      2. Drain excess IDLE members when live > target.
      3. Provision up to the deficit when live < target, capped by the dispatch
         slot budget (so dispatch is never starved) AND the pool budget. Skipped
         while the circuit breaker is tripped.

    When ``cost_guard_engaged`` the effective IDLE target is forced to 0 so step 2
    drains every idle member, and provisioning (step 3) is paused — the
    cost-aware auto-drain. It reverses on the next tick the guard disengages.

    The same target-0 forcing applies when this project is NOT in the warm-pool
    scope (:func:`is_project_warm_pool_scoped`): an unscoped project provisions
    nothing and any stray idle members are drained, so the pool can be rolled out
    to one project at a time while every other project stays on the cold path.
    A scoped project (or any project when the allowlist is empty) is reconciled
    to its configured target, so its manually-seeded members are held, not drained.
    """
    summary = {"recycled": 0, "drained": 0, "provisioned": 0}
    in_scope = is_project_warm_pool_scoped(project_id)
    drain_only = cost_guard_engaged or not in_scope
    # PER-PROJECT target: the project's explicit warm_pool_target_size_by_project
    # value when set, else the global warm_pool_target_size. This replaces ONLY
    # the single per-project IDLE target — every other guard/ceiling below (the
    # fleet-wide warm_pool_max_vms ceiling, dispatch_slots_remaining, the
    # demand-weighted account steering, and the cost-guard / out-of-scope
    # drain-to-0 forcing) is unchanged.
    target = 0 if drain_only else resolve_project_warm_pool_target(project_id)

    # Lock-free candidate read: the recycle/drain mutations below go through
    # retire_and_delete_pool_vm, whose atomic compare-and-swap claim is the sole
    # single-action safety mechanism (see its docstring). A FOR-UPDATE here would
    # buy nothing — retire's first commit releases it before the slow GCP I/O,
    # and the concurrent idle-reaper holds no overlapping lock anyway — so we
    # read without one and let the claim arbitrate any maintainer/reaper race.
    members = list(
        (
            await session.execute(
                select(PoolVm)
                .where(
                    PoolVm.project_id == project_id,
                    PoolVm.state.in_(LIVE_POOL_STATES),
                )
                .order_by(PoolVm.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    # 1. Recycle stale-image members. Drop each from the live-target math whether
    #    or not WE won the claim (a peer that beat us is also tearing it down).
    for member in list(members):
        if is_pool_vm_image_stale(member, stale_vm_ids):
            if await retire_and_delete_pool_vm(
                session, member, reason="pool_stale_image"
            ):
                summary["recycled"] += 1
            members.remove(member)

    # 2. Drain excess warm capacity. ``target`` is a per-project IDLE target, so
    #    only non-leased members (IDLE + PROVISIONING) count against it — a LEASED
    #    member is busy serving a task and is NOT idle warm capacity. Counting
    #    LEASED here would (a) stop replenishment under load (target=2 with 2
    #    LEASED + 0 IDLE → deficit 0, no warm VM kept ready) and (b) drain the
    #    last IDLE member (target=1, 1 IDLE + 1 LEASED → live 2 > 1). Drain still
    #    only ever removes IDLE members (never a PROVISIONING one about to become
    #    useful, nor a LEASED one running a task).
    warm_capacity = [m for m in members if m.state in IDLE_POOL_STATES]
    live = len(warm_capacity)
    if live > target:
        idle_members = [m for m in warm_capacity if m.state == PoolVmState.IDLE]
        # Oldest-idle first so the freshest warm capacity survives.
        idle_members.sort(key=lambda m: (m.idle_since or m.created_at))
        to_drain = min(live - target, len(idle_members))
        for member in idle_members[:to_drain]:
            if await retire_and_delete_pool_vm(
                session, member, reason="pool_over_target"
            ):
                summary["drained"] += 1
        return summary

    # 3. Provision the deficit, yielding to real dispatch and the pool budget.
    #    The TOTAL number of boots is unchanged (still bounded by the global
    #    per-project target and both budget ceilings) — this step only steers
    #    WHICH account each boot lands on so the same small idle pool spreads
    #    across the accounts that actually have recent demand, instead of always
    #    the first eligible account. No concurrency slot is reserved for an idle
    #    member (preserved); per-account cost stays attributed to the boot account.
    if suppressed or drain_only:
        return summary
    deficit = target - live
    allowed = min(deficit, max(0, dispatch_slots_remaining), max(0, pool_budget_remaining))
    if allowed <= 0:
        return summary

    # Demand-weighted account selection. Restrict to accounts that are BOTH
    # demand-active for this project AND currently bootable (ready / non-at-cap /
    # non-cooldown). Seed current per-account idle supply from the members we
    # already fetched (no extra query) so water-fill deprioritizes accounts that
    # are already over-represented among idle members.
    demand = await _account_demand_for_project(
        session, project_id, runtime_config=runtime_config
    )
    eligible = await _eligible_pool_accounts(session, project_id)
    demand_eligible = {
        acct_id: count
        for acct_id, count in demand.items()
        if acct_id is not None and acct_id in eligible and count > 0
    }
    supply: dict[str | None, int] = {}
    for member in warm_capacity:
        supply[member.account_id] = supply.get(member.account_id, 0) + 1

    for _ in range(allowed):
        chosen = _pick_demand_weighted_account(demand_eligible, supply)
        if chosen is not None:
            ok = await provision_fn(session, project_id, account_id=chosen)
        else:
            # No demand-attributable eligible account (empty pool, unattributed
            # demand, etc.) — fall back to the greedy first-eligible provision.
            ok = await provision_fn(session, project_id)
        if not ok:
            break
        summary["provisioned"] += 1
        if chosen is not None:
            supply[chosen] = supply.get(chosen, 0) + 1
    return summary


async def _projects_with_pool_members(session) -> list[uuid.UUID]:
    """Projects that already have at least one live pool member.

    The maintainer maintains EXISTING pools rather than bootstrapping a pool for
    every project (which would explode VM spend). An operator seeds the first
    member of a project's pool via the taskless provisioning entrypoint; from
    then on the maintainer keeps it at target.
    """
    rows = (
        await session.execute(
            select(PoolVm.project_id)
            .where(PoolVm.state.in_(LIVE_POOL_STATES))
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def _demand_active_projects(session, *, runtime_config) -> set[uuid.UUID]:
    """Projects with real dispatch demand, derived purely from the DB.

    A project is demand-active when it has eligible QUEUED backlog (a task
    waiting for a VM right now) OR a recent COLD dispatch
    (``Task.vm_dispatched_at`` within ``warm_pool_demand_window_minutes``). This
    is the signal that lets a pool FORM for a project that has ZERO live members
    yet: :func:`run_pool_maintainer_cycle` reconciles the UNION of this set with
    :func:`_projects_with_pool_members`, so a demand-active zero-member project
    is bootstrapped up to target through the normal ``reconcile_project_pool``
    deficit path (``target - 0``), within all the existing budget ceilings.

    A DORMANT project (no QUEUED backlog, no recent dispatch, no live members)
    is NEVER returned here, so it is never seeded — blanket-seeding every
    project would explode VM spend, which is exactly what the maintainer's
    members-only design avoided. The demand is DB-derived rather than read from
    the in-process ``warm_backlog_reuse`` funnel counters because those counters
    are not project-keyed and reset on every leader cutover.

    Backed by the ``ix_task_project_status_created`` (project_id, status,
    created_at) index for the QUEUED leg.
    """
    cutoff = _utcnow() - timedelta(minutes=_demand_window_minutes(runtime_config))
    rows = (
        await session.execute(
            select(Task.project_id)
            .where(
                or_(
                    Task.status == TaskStatus.QUEUED,
                    Task.vm_dispatched_at >= cutoff,
                )
            )
            .distinct()
        )
    ).scalars().all()
    return set(rows)


async def run_pool_maintainer_cycle(
    *,
    runtime_config,
    provision_fn: ProvisionFn | None = None,
    session_factory=AsyncSessionLocal,
) -> dict:
    """One maintainer tick across projects with pool members OR live demand.

    Reconciles the union of projects that already have live pool members and
    demand-active projects (eligible QUEUED backlog or a recent cold dispatch),
    so a demand-active project with zero members is bootstrapped up to target.
    Inert (early-return zero totals, no demand query) when warm_pool_enabled is
    off.
    """
    totals = {"recycled": 0, "drained": 0, "provisioned": 0, "projects": 0}
    if not is_warm_pool_enabled():
        return totals
    if provision_fn is None:
        provision_fn = _default_pool_provision_fn

    from app.shared_state import is_warm_pool_suppressed

    suppressed = is_warm_pool_suppressed()
    stale_vm_ids = await _stale_pool_image_ids()
    max_concurrent = int(runtime_config.get("max_concurrent_vms", 0) or 0)
    pool_max = _pool_max_vms(runtime_config)

    async with session_factory() as session:
        # Cost-aware auto-drain guard: evaluated ONCE per tick from the live pool
        # idle-cost + lease-hit signals. When engaged, every project's reconcile
        # forces target 0 (drain idle) and pauses provisioning until the hit rate
        # recovers — protecting cost-per-PR while reuse is still low.
        guard = await evaluate_pool_cost_guard(session, runtime_config=runtime_config)
        cost_guard_engaged = guard["engaged"]
        if cost_guard_engaged:
            logger.warning("[WARM-POOL] cost guard engaged: %s", guard["reason"])
        # Reconcile the UNION of (a) projects that already have live pool members
        # and (b) demand-active projects with no members yet. (b) is what lets a
        # pool actually FORM: a demand-active zero-member project flows through
        # the same reconcile_project_pool deficit path and is bootstrapped up to
        # target within every existing budget ceiling. A set dedupes the overlap
        # so a project in both groups is reconciled exactly once.
        project_ids = set(await _projects_with_pool_members(session)) | (
            await _demand_active_projects(session, runtime_config=runtime_config)
        )
        if not project_ids:
            return totals

        # Dispatch-slot accounting: a new pool VM occupies a real GCP slot, so it
        # must not be provisioned into a slot dispatch needs. Count task-bound VMs
        # (PROVISIONING/IN_PROGRESS tasks) + non-task-bound pool members, and only
        # provision into whatever headroom is left under max_concurrent_vms.
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

        for project_id in project_ids:
            try:
                summary = await reconcile_project_pool(
                    session,
                    project_id,
                    runtime_config=runtime_config,
                    provision_fn=provision_fn,
                    dispatch_slots_remaining=dispatch_slots_remaining,
                    pool_budget_remaining=pool_budget_remaining,
                    stale_vm_ids=stale_vm_ids,
                    suppressed=suppressed,
                    cost_guard_engaged=cost_guard_engaged,
                )
                if cost_guard_engaged:
                    await _maybe_emit_cost_guard_alert(
                        session, project_id, guard["reason"]
                    )
            except Exception:
                await session.rollback()
                logger.warning(
                    "[WARM-POOL] maintainer reconcile failed for project %s",
                    project_id,
                    exc_info=True,
                )
                continue
            # Each provisioned member consumes one dispatch slot and one pool
            # budget unit — decrement so later projects in the same tick can't
            # collectively breach either ceiling.
            dispatch_slots_remaining = max(
                0, dispatch_slots_remaining - summary["provisioned"]
            )
            pool_budget_remaining = max(
                0, pool_budget_remaining - summary["provisioned"]
            )
            for key in ("recycled", "drained", "provisioned"):
                totals[key] += summary[key]
            totals["projects"] += 1

    if totals["recycled"] or totals["drained"] or totals["provisioned"]:
        logger.info("[WARM-POOL] maintainer cycle: %s", totals)
    return totals


async def pool_maintainer_service():
    """Leader-only background loop reconciling the warm pool toward target.

    Bounded by a per-tick ``asyncio.timeout`` so a hung GCP provision can't pin
    the service; the monitor restarts it on crash/timeout. Inert while
    ``warm_pool_enabled`` is False.
    """
    from app.shared_state import runtime_config as _runtime_config

    while True:
        interval = int(
            _runtime_config.get("warm_pool_maintainer_interval_seconds", 120) or 120
        )
        await asyncio.sleep(interval)
        tick_budget = max(30, interval - 10)
        try:
            async with asyncio.timeout(tick_budget):
                await run_pool_maintainer_cycle(runtime_config=_runtime_config)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "[WARM-POOL] maintainer tick exceeded %ds budget — next tick retries",
                tick_budget,
            )
        except Exception as exc:
            logger.error("[WARM-POOL] maintainer tick error: %s", exc, exc_info=True)


async def sweep_idle_pool_vms(session, now: datetime, *, runtime_config) -> int:
    """Reaper pass: retire over-TTL / zombie / stale-image / stuck-RETIRING pool VMs.

    Returns the number of members retired. Inert when ``warm_pool_enabled`` is
    False.

    Concurrency: the candidate scan below is LOCK-FREE (mirrors the orphan
    reaper's Step-1 status scan). Single-action safety against a concurrent
    maintainer tick is NOT provided by a row lock held across this loop — the
    per-member commit inside :func:`retire_and_delete_pool_vm` would drop such a
    lock before the slow GCP I/O anyway (the footgun documented at
    reaper.py:1148). Instead :func:`retire_and_delete_pool_vm` claims each member
    with an atomic compare-and-swap ``UPDATE``, so whichever leader task (this
    sweep or the maintainer) wins the claim is the only one that tears the member
    down; the loser no-ops.
    """
    if not is_warm_pool_enabled():
        return 0

    from app.warm_vm_state import is_vm_alive

    idle_ttl = int(runtime_config.get("warm_pool_idle_ttl_seconds", 1800) or 1800)
    hb_stale = int(runtime_config.get("warm_pool_heartbeat_stale_seconds", 300) or 300)
    claim_grace = int(
        runtime_config.get(
            "warm_pool_retiring_claim_grace_seconds", _RETIRING_CLAIM_GRACE_SECONDS
        )
        or _RETIRING_CLAIM_GRACE_SECONDS
    )
    # Process-start warmup: suppress the heartbeat-keyed zombie teardown until
    # this process has been alive long enough for established members to have
    # heartbeated back to it. Defaults to the staleness window (a member that
    # truly died gets reaped one window later — acceptable) and is overridable
    # via warm_pool_reaper_warmup_seconds. Without this gate a fresh leader after
    # a deploy/failover, whose in-memory heartbeat map starts empty, would tear
    # down the entire established pool. The module-level _PROCESS_START_TS anchors
    # this; tests patch it to simulate a long-lived process.
    warmup = int(
        runtime_config.get("warm_pool_reaper_warmup_seconds", hb_stale) or hb_stale
    )
    zombie_teardown_armed = (
        now - _PROCESS_START_TS
    ).total_seconds() >= warmup

    stale_vm_ids = await _stale_pool_image_ids()
    idle_cutoff = now - timedelta(seconds=idle_ttl)

    # Candidate scan WITHOUT locks (mirrors the reaper's lock-free Step 1). We
    # consider every non-terminal member; the per-member predicate below decides
    # whether it is actually reapable, and the atomic claim inside
    # retire_and_delete_pool_vm re-validates state-under-mutation so a member
    # leased/retired by a peer between this scan and the teardown is skipped.
    candidates = list(
        (
            await session.execute(
                select(PoolVm)
                .where(
                    PoolVm.state.in_(
                        (
                            PoolVmState.PROVISIONING,
                            PoolVmState.IDLE,
                            PoolVmState.LEASED,
                            PoolVmState.RETIRING,
                        )
                    )
                )
                .order_by(PoolVm.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    retired = 0
    # Iterate by id and re-fetch each member fresh. retire_and_delete_pool_vm
    # commits per member, and a failed teardown below issues session.rollback() —
    # both expire the batch-loaded ``candidates`` instances. Reading a later
    # candidate's attributes (or the failed member's vm_instance_id in the except)
    # off an expired instance triggers a sync lazy-load under async ->
    # MissingGreenlet, which masks the real error AND aborts the rest of the
    # sweep (leaving idle/zombie members unswept). session.get re-loads a fresh
    # instance (identity-map hit until a rollback expires it). Stale-ORM-after-
    # rollback pattern; observed via the 2026-06-04 lock-contention incident.
    candidate_ids = [member.id for member in candidates]
    for member_id in candidate_ids:
        member = await session.get(PoolVm, member_id)
        if member is None:
            continue  # retired/deleted by a peer since the lock-free scan
        reason: str | None = None
        if member.state == PoolVmState.RETIRING:
            # Stuck mid-teardown from a prior interrupted sweep — finish it.
            reason = "pool_retiring_cleanup"
        elif member.state == PoolVmState.LEASED:
            # Reclaim a STRANDED lease. lease_expires_at bounds the dispatch->claim
            # handoff; once it passes, a still-LEASED member is reclaimable ONLY
            # when BOTH (a) its leased task already reached a terminal state (or
            # vanished) — the lease is fulfilled/void — AND (b) its VM is no longer
            # heartbeating, so we never tear down a VM that is still running its
            # task OR was deferred and reused for fresh backlog work (the pool/
            # backlog-reuse intersection). When LEASED rows are never reclaimed a
            # completed/failed member stays LEASED forever, keeps counting as a
            # live pool member, and the pool self-exhausts (replenishment halts
            # once phantom LEASED rows saturate the budget). Reading lease_expires_at
            # here is the expiry/reclaim path the lease write was always for.
            if (
                member.lease_expires_at is not None
                and member.lease_expires_at < now
                and not is_vm_alive(member.vm_instance_id, max_age_seconds=hb_stale)
            ):
                leased_task = (
                    await session.get(Task, member.leased_task_id)
                    if member.leased_task_id is not None
                    else None
                )
                # EVALUATING counts as lease-fulfilled: a parent in EVALUATING
                # is only waiting on children (which run on their own VMs) —
                # its VM-side work is done and guard (b) above already proved
                # the VM is dead. Without it, every EVALUATING parent that ran
                # on a pool member left a LEASED row no cleanup path could
                # touch (completion release fires only on terminal statuses,
                # _ensure_vm_deleted skips governed members, the generic orphan
                # sweep exempts LIVE_POOL_STATES, drain only takes IDLE) — the
                # powered-off VM sat TERMINATED holding its 50GB SSD disk until
                # the parent eventually completed. Five such zombies exhausted
                # the region SSD_TOTAL_GB quota and halted all provisioning on
                # 2026-06-07.
                if leased_task is None or leased_task.status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.EVALUATING,
                ):
                    reason = "pool_lease_orphaned"
        elif is_pool_vm_image_stale(member, stale_vm_ids):
            reason = "pool_stale_image"
        elif (
            member.state == PoolVmState.IDLE
            and member.idle_since is not None
            and member.idle_since < idle_cutoff
        ):
            reason = "pool_idle_ttl"
        elif zombie_teardown_armed and not is_vm_alive(
            member.vm_instance_id, max_age_seconds=hb_stale
        ):
            # Zombie: no heartbeat within the liveness window. Guarded with an
            # idle_since/created_at grace so a member still booting (just
            # PROVISIONING, no heartbeat yet) is not killed prematurely, AND by
            # the process-start warmup gate above so a fresh leader's empty
            # in-memory heartbeat map cannot mass-classify established members
            # as zombies right after a deploy/failover.
            age_anchor = member.idle_since or member.created_at
            if age_anchor is not None and (now - age_anchor) > timedelta(
                seconds=hb_stale
            ):
                reason = "pool_zombie_no_heartbeat"
        if reason is None:
            continue
        # Snapshot before the teardown so the failure log never reads an
        # attribute off an instance the except's rollback just expired.
        vm_id = member.vm_instance_id
        try:
            if await retire_and_delete_pool_vm(
                session, member, reason=reason, claim_grace_seconds=claim_grace
            ):
                retired += 1
        except Exception:
            await session.rollback()
            logger.warning(
                "[WARM-POOL] idle-reaper failed to retire %s",
                vm_id,
                exc_info=True,
            )
    return retired
