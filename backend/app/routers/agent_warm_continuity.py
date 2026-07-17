"""Agent-facing warm-VM continuity endpoints (/vm-heartbeat + /next-role).

Extracted from app.routers.agent to give the warm-VM continuity surface its
own seam, so warm-handoff work (chain continuity, backlog reuse, VM-scoped
liveness) can evolve independently of the agent lifecycle (claim/heartbeat/
GitHub) flow and stop colliding in the main agent router.

Behavior is byte-identical to the prior in-router implementation:
  - POST /api/agent/vm-heartbeat — VM-scoped liveness for warm VMs polling
    /next-role; also records VM image identity + runtime fingerprint.
  - POST /api/agent/next-role — look up a warm-pinned QUEUED task for this VM
    (chain continuity), fall back to backlog reuse, then chain-fork exit / 204.

Auth is shared with the main agent router via ``verify_agent_token``; the
router is registered under the same ``/api/agent`` prefix in app.main so the
route paths stay byte-identical and the agent-side warm-poll script needs no
change.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.auth import (
    create_agent_token,
    AGENT_TOKEN_TTL_HOURS,
    AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
    VM_SCOPED_TOKEN_SCOPE,
)
from app.broadcast import broadcast_to_project
from app.claim_analytics import stamp_warm_claim_timing
from app.database import get_session, resolve_dialect_name
from app.models import Task, TaskStatus, _utcnow
from app.routers.agent import verify_agent_token
from app.shared_state import limiter
import app.shared_state as _shared_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# Phase 2 (warm-VM continuity): /next-role + /vm-heartbeat
# ---------------------------------------------------------------------------


class NextRoleRequest(BaseModel):
    """Body for POST /api/agent/next-role.

    The agent on a warm VM polls this endpoint after a role completes to
    discover whether another QUEUED task has been pinned to the same VM.
    The JWT proves the agent owns the prior task; the body re-asserts the
    VM identity (defence in depth — JWT + body cross-check) and the prior
    PR number so the orchestrator can scope the lookup.
    """

    model_config = ConfigDict(extra="ignore")
    vm_instance_id: str = Field(..., min_length=1)
    # Optional: a taskless warm-pool VM polls for its FIRST lease with no prior
    # task, so this is absent on pool-mode polls. The chain/backlog paths still
    # send it (it scopes the prior-PR lookup and labels logs).
    prior_task_id: uuid.UUID | None = None
    prior_pr_number: int | None = None


class VmHeartbeatRequest(BaseModel):
    """Body for POST /api/agent/vm-heartbeat — VM-scoped liveness."""

    model_config = ConfigDict(extra="ignore")
    vm_instance_id: str = Field(..., min_length=1)
    # Same field names as HeartbeatRequest (schemas.py) so agent can use
    # one payload shape for both endpoints. Recorded into compute._vm_images
    # so the orchestrator can detect stale Packer images on warm VMs that
    # never run a task heartbeat.
    vm_image_name: str | None = Field(None, max_length=200)
    vm_image_build_ts: int | None = None
    agent_runtime_fingerprint: str | None = Field(None, pattern="^[0-9a-f]{64}$")
    # Git commit the image was built from (baked AGENT_SOURCE_SHA marker). Same
    # additive provenance field as HeartbeatRequest; None on pre-marker images.
    agent_source_sha: str | None = Field(None, pattern="^[0-9a-f]{7,40}$")
    # A taskless pool VM that fails pre-claim auth has no task to attribute the
    # failure to, so it reports the reason here. Surfaced as a WARNING so a
    # bad-credential pool boot is an EXPLAINED death in the logs, not an
    # unexplained zombie the reaper later kills with no cause.
    pre_claim_failure: str | None = Field(None, max_length=500)


@router.post("/vm-heartbeat", status_code=204)
@limiter.limit("600/minute")
async def vm_heartbeat(
    request: Request,
    req: VmHeartbeatRequest,
    token: dict = Depends(verify_agent_token),
):
    """VM-scoped heartbeat used by warm VMs while polling /next-role.

    The reaper kills VMs whose task hasn't heartbeated, but during the
    next-role poll the prior task is already terminal, so the agent
    needs a separate liveness signal that targets the VM identity, not
    the task identity. The orchestrator's provisioning loop consults
    `_shared_state.vm_last_heartbeat` to decide whether to skip
    cold-provisioning a fresh VM.

    Doubles as the warm-pool readiness signal: a freshly-booted taskless
    pool VM fires this heartbeat before it begins polling /next-role, which
    is exactly the moment its agent is alive and ready to claim work — so a
    CLEAN heartbeat promotes its PoolVm row from PROVISIONING to IDLE (via
    :func:`warm_pool.promote_pool_vm_to_idle`), making it leasable by the
    dispatch hook. A heartbeat carrying ``pre_claim_failure`` is the opposite
    signal — the member's agent died in early boot — so it is RETIRED instead
    of promoted, never handed to a real task (see the ``pre_claim_failure``
    branch below).
    """
    if req.vm_instance_id != token.get("vm_instance_id"):
        raise HTTPException(status_code=403, detail="vm_instance_mismatch")
    _shared_state.record_vm_heartbeat(req.vm_instance_id)
    if req.pre_claim_failure:
        # Explain a pool-VM pre-claim death (bad creds, etc.) instead of leaving
        # it a silent zombie for the reaper. Pool claim-failure telemetry also
        # feeds the pool circuit breaker so repeated bad boots back off.
        logger.warning(
            "[WARM-POOL] pool VM %s reported pre-claim failure: %s",
            req.vm_instance_id,
            req.pre_claim_failure,
        )
        _shared_state.record_pool_claim_failure("pre_claim_auth")
        # A taskless pool member that died in early boot validation must NEVER be
        # promoted to IDLE — a credential-broken VM that becomes leasable is handed
        # to a real task that can never be claimed, turning an explained death into
        # a downstream task timeout and defeating the pool. record_pool_claim_failure
        # only feeds the circuit breaker; it does NOT touch the registry row, so we
        # retire the member here so lease_idle_pool_vm (IDLE-only) can never select
        # it and the reaper tears it down. Gated on is_warm_pool_enabled() so a dark
        # pool acquires no DB session; best-effort so the liveness ping never 500s
        # on a DB hiccup; a no-op for a non-pool VM (no PoolVm row). Returns early:
        # the readiness-promotion block below is mutually exclusive with this path.
        from app.warm_pool import is_warm_pool_enabled

        if is_warm_pool_enabled():
            from app.database import AsyncSessionLocal
            from app.warm_pool import mark_pool_vm_retiring

            try:
                async with AsyncSessionLocal() as session:
                    await mark_pool_vm_retiring(session, req.vm_instance_id)
                    await session.commit()
            except Exception:  # noqa: BLE001 — liveness must never 500 on a DB hiccup
                logger.warning(
                    "[WARM-POOL] heartbeat retire-on-pre-claim-failure failed for vm %s",
                    req.vm_instance_id,
                    exc_info=True,
                )
        return Response(status_code=204)
    if req.vm_image_name is not None or req.vm_image_build_ts is not None:
        from app.compute import record_vm_image

        record_vm_image(req.vm_instance_id, req.vm_image_name, req.vm_image_build_ts)
    if req.agent_runtime_fingerprint is not None:
        from app.compute import record_vm_runtime_fingerprint

        record_vm_runtime_fingerprint(req.vm_instance_id, req.agent_runtime_fingerprint)
    if req.agent_source_sha is not None:
        from app.compute import record_vm_source_sha

        record_vm_source_sha(req.vm_instance_id, req.agent_source_sha)

    # Warm-pool readiness signal (efficiency direction d-5fd69fe1). Reached only
    # on a CLEAN heartbeat — the pre_claim_failure branch above returns early, so a
    # member that died in early boot is retired here, never promoted. A
    # maintainer-booted pool member sits in PROVISIONING until something marks it
    # IDLE; the agent's EXISTING heartbeat IS that ready signal. Promote the
    # heartbeat's own VM (the token-verified id) PROVISIONING -> IDLE so
    # try_pool_dispatch can lease it. GATED on is_warm_pool_enabled() FIRST so
    # when the pool is dark (the default) this path acquires NO DB session and
    # issues NO query — preserving today's zero-DB heartbeat for the live
    # chain-continuity / backlog-reuse VMs that also hit this endpoint at
    # 600/min/VM. Best-effort: a DB hiccup must never fail the liveness ping, so
    # any error is swallowed (the next heartbeat retries). Idempotent: after the
    # first beat the member is IDLE so the conditional UPDATE matches 0 rows.
    from app.warm_pool import is_warm_pool_enabled

    if is_warm_pool_enabled():
        from app.compute import persist_pool_vm_image_state
        from app.database import AsyncSessionLocal
        from app.warm_pool import promote_pool_vm_to_idle

        # Durably mirror the heartbeat-reported image identity onto the PoolVm row
        # in the SAME session as the IDLE-promotion, so the registry reflects the
        # actual image/source SHA the warm member is running across restarts. No-op
        # for non-pool VMs (returns False when there is no PoolVm row).
        _img_kwargs: dict = {}
        if req.vm_image_name is not None or req.vm_image_build_ts is not None:
            _img_kwargs["image_name"] = req.vm_image_name
            _img_kwargs["build_ts"] = req.vm_image_build_ts
        if req.agent_runtime_fingerprint is not None:
            _img_kwargs["runtime_fingerprint"] = req.agent_runtime_fingerprint
        if req.agent_source_sha is not None:
            _img_kwargs["source_sha"] = req.agent_source_sha

        try:
            async with AsyncSessionLocal() as session:
                await promote_pool_vm_to_idle(session, req.vm_instance_id)
                if _img_kwargs:
                    await persist_pool_vm_image_state(
                        session, req.vm_instance_id, **_img_kwargs
                    )
                # Stamp a durable heartbeat on the PoolVm row so the reaper can
                # distinguish a live member from a zombie even after a process
                # restart (whose in-memory vm_last_heartbeat map starts empty).
                # Targets only non-RETIRING states — idempotent, no-op for non-
                # pool VMs (no matching row). The same UPDATE runs for PROVISIONING,
                # IDLE, and LEASED members so a pool VM heartbeating while serving
                # a task also refreshes the durable stamp.
                from app.models import PoolVm, PoolVmState

                await session.execute(
                    update(PoolVm)
                    .where(
                        PoolVm.vm_instance_id == req.vm_instance_id,
                        PoolVm.state.in_(
                            (
                                PoolVmState.PROVISIONING,
                                PoolVmState.IDLE,
                                PoolVmState.LEASED,
                            )
                        ),
                    )
                    .values(last_heartbeat_at=_utcnow())
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — liveness must never 500 on a DB hiccup
            logger.warning(
                "[WARM-POOL] heartbeat IDLE-promotion failed for vm %s",
                req.vm_instance_id,
                exc_info=True,
            )

    return Response(status_code=204)


async def _vm_is_refresh_capable(
    session: AsyncSession, vm_instance_id: str | None
) -> bool:
    """Return True if any task previously assigned to this VM proved it can
    refresh (i.e. recorded ``last_token_refresh_at``).

    The warm-handoff path mints a token for a brand-new QUEUED task that has
    never refreshed, so capability cannot be read off the task being claimed.
    Instead we look across the VM's whole role chain: a single successful
    /api/agent/refresh-token on ANY prior role on this VM proves the
    long-lived agent process can refresh. Reusing the existing
    ``last_token_refresh_at`` signal (set in ``agent_refresh_token``) keeps
    this VM-level check from inventing a parallel capability marker. An old
    image that cannot refresh never stamps the column, so it correctly keeps
    receiving the long bootstrap window across handoffs.
    """
    if not vm_instance_id:
        return False
    stmt = (
        select(Task.id)
        .where(
            Task.vm_instance_id == vm_instance_id,
            Task.last_token_refresh_at.is_not(None),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _prior_task_vm_boot(
    session: AsyncSession, prior_task_id: uuid.UUID
) -> dict | None:
    """Return the boot identity frozen into the VM that ran ``prior_task_id``.

    ``Task.provider/account_id/model`` are stamped at task completion
    (agent_status.py) from the runtime the task actually ran on, so for a
    just-completed prior role they faithfully reflect the VM's frozen boot
    account — the comparison key the re-target path uses to decide whether to
    re-materialize credentials.
    """
    result = await session.execute(
        select(Task.provider, Task.account_id, Task.model, Task.context).where(
            Task.id == prior_task_id
        )
    )
    prior_task = result.first()
    if prior_task is None:
        return None
    return {
        "provider": prior_task.provider,
        "account_id": prior_task.account_id,
        "model": prior_task.model,
        "auth_mode": (prior_task.context or {}).get("llm_auth_mode"),
    }


def _norm_account(value) -> str | None:
    """Stripped string form of an account id, or None for empty/missing."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


async def _project_user_id(session: AsyncSession, project_id) -> str:
    """Return the owning user_id for ``project_id`` (empty string if absent).

    This is the FALLBACK pool owner. Cold dispatch (orchestrator.py) scopes the
    account pool to ``task.launched_by_user_id`` when set (the launcher pays) and
    falls back to ``Project.user_id`` only for autonomous tasks with no launcher;
    the re-target path mirrors that order, using this helper for the fallback leg.
    """
    if project_id is None:
        return ""
    from app.models import Project

    result = await session.execute(
        select(Project.user_id).where(Project.id == project_id)
    )
    row = result.first()
    return str(row[0]) if row and row[0] else ""


async def _resolve_retarget(
    session: AsyncSession,
    task: Task,
    vm_instance_id: str,
    vm_boot: dict | None,
    ctx: dict,
    task_type: str,
) -> tuple[dict | None, dict, str | None]:
    """Re-target a warm-claimed task to its own resolved LLM account.

    Mirrors cold dispatch: resolves the claimed task's LLM runtime via
    ``select_llm_runtime_for_task`` (which pre-increments / ACQUIRES the
    resolved account's slot exactly as the orchestrator does), stamps the
    resolved provider/auth/model/account onto the task context so the durable
    slot/cost reconciliation and the completion-time slot release both follow
    the account the task actually runs on, and builds the provider secret
    manifest ONLY when the resolved account differs from the VM's frozen boot
    account.

    Returns ``(secret_manifest_or_None, updated_ctx, acquired_account_id)``.
    ``acquired_account_id`` is the slot the caller must release if finalization
    later fails (so acquire/release stays exactly balanced). When no pool
    account resolves (env-fallback / empty pool) nothing is acquired and the
    manifest is None — the VM keeps its boot credentials.
    """
    from app.compute_manifest import build_agent_secret_manifest
    from app.max_plan_pool import (
        apply_task_llm_runtime_context,
        select_llm_runtime_for_task,
    )

    runtime_config = _shared_state.runtime_config
    # Mirror cold dispatch's pool scoping: the launcher pays, so prefer
    # ``task.launched_by_user_id`` and fall back to the project owner only for
    # autonomous tasks with no launcher. Backlog reuse has no launched_by filter,
    # so a user-launched task whose launcher != project owner is reachable here;
    # selecting from the project owner's pool would mis-attribute cost and, when
    # the owner's pool has no matching account, yield a synthetic account=None
    # result (no manifest/slot) that strands the task on the VM's boot creds.
    user_id = task.launched_by_user_id or await _project_user_id(
        session, task.project_id
    )
    # Union the same two blacklist keys cold dispatch unions into its effective
    # blacklist: caller-supplied ``blacklisted_accounts`` plus sticky
    # ``provision_failed_accounts`` (accounts that died pre-claim).
    _blacklist = ctx.get("blacklisted_accounts") or []
    _sticky = ctx.get("provision_failed_accounts") or []
    blacklist = (
        list(
            {
                *(_blacklist if isinstance(_blacklist, list) else []),
                *(_sticky if isinstance(_sticky, list) else []),
            }
        )
        or None
    )
    selection = await select_llm_runtime_for_task(
        user_id=user_id,
        task=task,
        runtime_config=runtime_config,
        blacklist=blacklist,
        project_id=str(task.project_id) if task.project_id else None,
        # No runner_available_clis: warm retarget dispatches to cloud VMs that
        # have all LLM CLIs pre-installed, so CLI-based filtering is not needed.
    )
    if selection is None:
        return None, ctx, None

    account = selection.account
    if account is None:
        # No pool account (synthetic/env-fallback result) — no slot acquired and
        # nothing to re-materialize. Leave the VM on its boot credentials.
        new_ctx = apply_task_llm_runtime_context(
            ctx,
            account_id=None,
            provider=selection.provider,
            auth_mode=selection.auth_mode,
            model=selection.model,
            execution_mode=selection.execution_mode,
        )
        if ctx.get("parked_for_retarget"):
            new_ctx["park_retarget_claim_blocked"] = True
        return None, new_ctx, None

    # The account slot is now ACQUIRED (select_max_plan_account pre-incremented
    # active_tasks). From here on any raise must release it before propagating,
    # otherwise the in-memory slot leaks until per-cycle reconciliation heals it.
    # The caller cannot release it for us (it only learns acquired_account_id from
    # our return value), so this function owns release-on-throw for its own steps.
    acquired_account_id = account["id"]
    try:
        new_ctx = apply_task_llm_runtime_context(
            ctx,
            account_id=acquired_account_id,
            provider=selection.provider,
            auth_mode=selection.auth_mode,
            model=selection.model,
            execution_mode=selection.execution_mode,
        )

        boot_account = _norm_account(vm_boot.get("account_id")) if vm_boot else None
        boot_provider = (
            str(vm_boot.get("provider") or "").strip().lower() if vm_boot else ""
        )
        boot_auth_mode = (
            str(vm_boot.get("auth_mode") or "").strip().lower() if vm_boot else ""
        )
        resolved_provider = str(selection.provider or "").strip().lower()
        resolved_auth_mode = str(selection.auth_mode or "").strip().lower()
        if (
            _norm_account(acquired_account_id) == boot_account
            and resolved_provider == boot_provider
            and resolved_auth_mode == boot_auth_mode
        ):
            # Same account as the VM's boot manifest — the VM already holds the
            # right runtime and credentials, so no manifest is needed. Account
            # id alone is insufficient because its provider/auth configuration
            # can change while a VM is warm.
            return None, new_ctx, acquired_account_id

        manifest = build_agent_secret_manifest(
            vm_instance_id=vm_instance_id,
            task_id=task.id,
            oauth_token=account.get("runtime_secret") or account.get("oauth_token"),
            claude_config_json=account.get("claude_config_json"),
            task_type=task_type,
            provider=selection.provider,
            auth_mode=selection.auth_mode,
            model=selection.model,
            execution_mode=selection.execution_mode,
            provider_metadata=account.get("provider_metadata") or {},
            provider_settings=selection.settings,
            project_id=str(task.project_id) if task.project_id else "",
        )
    except Exception:
        from app.max_plan_pool import release_max_plan_account

        await release_max_plan_account(
            acquired_account_id,
            project_id=str(task.project_id) if task.project_id else None,
        )
        raise
    # The manifest is PURELY the provider/credential payload. The authoritative,
    # TTL-gated agent JWT is the response's rotated ``agent_token``; drop the
    # bootstrap-TTL token build_agent_secret_manifest mints as a side effect so
    # the two never collide (and we never duplicate the authoritative JWT).
    if isinstance(manifest.get("env"), dict):
        manifest["env"].pop("AGENT_SECRET_TOKEN", None)
    return manifest, new_ctx, acquired_account_id


async def _vm_boot_account_identity(
    session: AsyncSession, vm_instance_id: str | None, *, exclude_task_id=None
) -> dict | None:
    """Resolve the LLM-account identity baked into this VM at cold boot.

    A warm/backlog-reuse claim runs on the credentials frozen into the VM's
    secret manifest at its original cold provision (see
    compute_manifest.build_agent_secret_manifest) — but the QUEUED task being
    claimed was never cold-dispatched, so its context carries no account id
    (or a stale one from a different VM). We recover the boot account the same
    way ``_vm_is_refresh_capable`` recovers refresh capability: by looking
    across this VM's whole role chain. The cold-dispatched root task on this VM
    stamped ``context.llm_account_id`` at select-time; the most recent prior
    task on this VM that carries one identifies the account the reused VM is
    actually running on.

    Returns ``{account_id, provider, auth_mode, model}`` (provider/auth_mode/
    model may be None) for the most recent qualifying task, or None when no
    prior task on this VM recorded an account (e.g. a local/test VM).
    """
    if not vm_instance_id:
        return None
    # Filter in SQL for tasks that actually recorded an account, so .limit(1)
    # skips unattributed tasks rather than returning None when the latest task
    # has no account but an older one does.
    if resolve_dialect_name(session) == "postgresql":
        has_account = Task.context.op("?")(  # type: ignore[attr-defined]
            "llm_account_id"
        ) | Task.context.op("?")("max_plan_account_id")
    else:
        has_account = (
            func.json_extract(Task.context, "$.llm_account_id").isnot(None)
            | func.json_extract(Task.context, "$.max_plan_account_id").isnot(None)
        )
    stmt = (
        select(Task.context)
        .where(Task.vm_instance_id == vm_instance_id, has_account)
        .order_by(Task.created_at.desc())
    )
    if exclude_task_id is not None:
        stmt = stmt.where(Task.id != exclude_task_id)
    stmt = stmt.limit(1)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    ctx = row[0] or {}
    account_id = ctx.get("llm_account_id") or ctx.get("max_plan_account_id")
    if not account_id:
        return None
    return {
        "account_id": str(account_id),
        "provider": ctx.get("llm_provider"),
        "auth_mode": ctx.get("llm_auth_mode"),
        "model": ctx.get("llm_model"),
    }


async def _finalize_warm_claim(
    session: AsyncSession,
    task: Task,
    vm_instance_id: str,
    *,
    origin: str,
    refresh_capable: bool,
    vm_boot: dict | None = None,
    boot_account: dict | None = None,
) -> dict:
    """Flip a locked QUEUED task to IN_PROGRESS for a warm VM and build the
    /next-role claim response.

    Shared by the chain-continuity (``origin="next_role_endpoint"``), the
    backlog-reuse (``origin="backlog_reuse"``) and the warm-pool dispatch
    (``origin="warm_pool"``) paths so all stamp warm telemetry identically.

    ``boot_account`` lets a caller inject the VM's boot LLM identity directly
    (``{account_id, provider, auth_mode, model}``) instead of recovering it from
    the VM's prior-task chain. The pool path needs this: a pool VM is taskless,
    so it has no prior task on this VM to read the account off — the identity
    lives on its ``PoolVm`` registry row. When omitted (chain/backlog) the boot
    account is recovered from the VM's role chain as before. Sets ``context._timing.warm_start`` and resets
    ``vm_provisioned_at`` to now so the task is counted as warm (and pays ~0
    cold-start) by the efficiency snapshot, mints a fresh task-scoped JWT,
    commits, and broadcasts the status change. On JWT-mint failure it rolls
    back (restoring QUEUED) and raises 500.

    ``refresh_capable`` gates the minted token's TTL: a VM already proven
    refresh-capable gets the short security-bounded TTL (no fresh bootstrap
    window), while an unconfirmed VM keeps the long bootstrap window so a
    deploy/rebake cutover can't 401 it mid-task.

    ``vm_boot`` is the VM's frozen boot identity (provider/account/model). When
    warm re-targeting is enabled (``warm_retarget_enabled``) and ``vm_boot`` is
    supplied, the claimed task is re-targeted to its own resolved LLM account:
    the resolved provider/auth/model/account is acquired and stamped onto the
    task context, and — only when the resolved account differs from the boot
    account — the resolved account's secret manifest is added to the response
    under ``secret_manifest`` so the agent can re-materialize credentials.
    OpenAI OAuth claims also receive the latest manifest when staying on the
    same account because refresh tokens rotate independently of account identity.
    The field remains additive for older agents that ignore it.
    """
    from app.compute import VM_PROVISIONING_MODEL

    task.vm_instance_id = vm_instance_id
    task.status = TaskStatus.IN_PROGRESS
    task.last_heartbeat = _utcnow()
    # vm_provisioned_at marks when the VM was made available to the task. For
    # warm-start we set it to "now" so claim_latency / reaper logic operates on
    # a fresh window — and so the subsequent idempotent /claim never stamps a
    # cold-start sample (the warm task stays vm_cold_start_ms=NULL).
    task.vm_provisioned_at = _utcnow()
    task.vm_spot = VM_PROVISIONING_MODEL == "SPOT"
    task.vm_warm_start = True
    if task.started_at is None:
        task.started_at = _utcnow()

    task_type = ""
    try:
        payloads = task.payloads or []
        if payloads and isinstance(payloads[0], dict):
            task_type = str(payloads[0].get("type") or "")
    except Exception:  # noqa: BLE001 — task_type is best-effort metadata
        task_type = ""

    # Carry warm-start signal forward into context._timing. setdefault keeps
    # this server-side write idempotent; the agent's status update remains the
    # authoritative source and may overwrite warm_start. warm_claim_origin
    # distinguishes chain continuity from backlog reuse for post-mortem and
    # telemetry (efficiency direction d-91d8dede).
    ctx = dict(task.context or {})

    # Recover the VM's boot LLM identity (provider/account/model) so the claim is
    # attributed to the account the reused VM actually runs on. The pool path
    # injects it from the PoolVm registry row (a taskless pool VM has no prior
    # task to read it off); chain/backlog recover it from the VM's role chain.
    # Used for the baseline attribution + the slot reservation below, and skipped
    # entirely when warm re-target resolves the task's own account instead.
    if boot_account is None:
        boot_account = await _vm_boot_account_identity(
            session, vm_instance_id, exclude_task_id=task.id
        )

    # Warm re-target: resolve + acquire the claimed task's own account and stamp
    # it onto ctx BEFORE the timing write so a single context commit persists
    # both. The whole resolve→stamp→mint→commit sequence runs INSIDE the try so
    # ``acquired_account_id`` is released on ANY finalize-path failure (including
    # a raise from build_agent_secret_manifest or select_llm_runtime_for_task
    # after the slot was acquired) — acquire/release stays airtight rather than
    # reconciliation-dependent.
    secret_manifest: dict | None = None
    acquired_account_id: str | None = None
    retargeted = False
    # Mint the new JWT BEFORE committing the IN_PROGRESS transition. If mint
    # raises, the rollback restores QUEUED so a retry can succeed instead of
    # leaving the task stuck IN_PROGRESS with no live agent.
    ttl_hours = (
        AGENT_TOKEN_TTL_HOURS if refresh_capable else AGENT_BOOTSTRAP_TOKEN_TTL_HOURS
    )
    _warm_task_id_str = str(task.id)
    try:
        if vm_boot is not None and _shared_state.runtime_config.get(
            "warm_retarget_enabled", False
        ):
            secret_manifest, ctx, acquired_account_id = await _resolve_retarget(
                session, task, vm_instance_id, vm_boot, ctx, task_type
            )
            retargeted = True
            if ctx.pop("park_retarget_claim_blocked", False):
                await session.rollback()
                logger.info(
                    "[NEXT-ROLE] Parked retarget claim for task %s on vm %s "
                    "deferred: no alternate account is claimable yet",
                    _warm_task_id_str,
                    vm_instance_id,
                )
                return Response(status_code=204)

        # Baseline boot-account attribution. The VM's LLM credentials are frozen
        # into its boot secret manifest, so a backlog/chain claim that bypasses
        # select_max_plan_account would otherwise be attributed to the wrong
        # account (or none) — undercounting the boot account's concurrency / daily
        # caps and risking silent over-subscription (429 cascades). Stamping the
        # boot account lets the orchestrator's per-cycle reconciliation and the
        # completion-release path (both keyed on context.llm_account_id) count and
        # release it like a cold dispatch. When warm re-target already resolved the
        # task's own account it owns the identity + its own slot, so this baseline
        # stamp/reserve is skipped.
        if not retargeted and boot_account is not None:
            from app.max_plan_pool import apply_task_llm_runtime_context

            ctx = apply_task_llm_runtime_context(
                ctx,
                account_id=boot_account["account_id"],
                provider=boot_account.get("provider"),
                auth_mode=boot_account.get("auth_mode"),
                model=boot_account.get("model"),
            )

        # OpenAI OAuth credentials rotate independently of account identity.
        # A same-account warm VM may therefore hold an older auth.json than the
        # backend even though its provider/account/model tuple still matches.
        # Always re-materialize the authoritative credential before every warm
        # Codex role; account-mismatch-only retargeting is insufficient here.
        boot_provider = (
            str(boot_account.get("provider") or "").strip().lower()
            if boot_account
            else ""
        )
        boot_auth_mode = (
            str(boot_account.get("auth_mode") or "").strip().lower()
            if boot_account
            else ""
        )
        if (
            secret_manifest is None
            and boot_account is not None
            and boot_provider == "openai"
            and boot_auth_mode == "oauth"
        ):
            from app.max_plan_pool import build_account_retarget_manifest

            secret_manifest = build_account_retarget_manifest(
                boot_account.get("account_id"),
                provider=boot_provider,
                auth_mode=boot_auth_mode,
                model=boot_account.get("model"),
                task=task,
                runtime_config=_shared_state.runtime_config,
            )
            if secret_manifest is None:
                raise HTTPException(
                    status_code=503,
                    detail="latest Codex OAuth credential unavailable for warm claim",
                )
            metadata = dict(secret_manifest.get("metadata") or {})
            metadata["credential_refresh_only"] = True
            secret_manifest["metadata"] = metadata

        # Park-and-retarget is a transient QUEUED-state marker only. Once this
        # warm claim lands the task is active again, so clear the bounded-park
        # bookkeeping before committing the IN_PROGRESS transition.
        ctx.pop("parked_for_retarget", None)
        ctx.pop("park_deadline", None)
        ctx = stamp_warm_claim_timing(
            ctx,
            origin=origin,
            vm_instance_id=vm_instance_id,
        )
        # Ground warm-resume receipts in actual outcome: stamp only here, at the
        # warm-claim site, so a cold-fallback fix (reserved member reaped, lease
        # expired, or pool fallback) leaves the receipt absent. Gate receipt stamping
        # on the same condition as SHA delivery (improvement_lanes_enabled) so a flag
        # flip between spawn and warm-claim can never produce a receipt that claims
        # a checkpoint reset that did not (and could not) happen.
        from app.runtime_config_state import (
            is_improvement_lanes_enabled as _is_lanes_enabled,
        )

        if _is_lanes_enabled() and ctx.get("is_lane_phase"):
            from app.review_fix_lane_pilot import confirm_lane_warm_resume

            confirm_lane_warm_resume(
                ctx, has_checkpoint=bool(ctx.get("lane_checkpoint_sha"))
            )
        task.context = ctx
        task.llm_account_id = ctx.get("llm_account_id")
        flag_modified(task, "context")
        agent_token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id=vm_instance_id,
            task_type=task_type,
            project_id=str(task.project_id) if task.project_id else "",
            ttl_hours=ttl_hours,
        )
        session.add(task)
        await session.commit()
    except Exception as exc:
        # Release the slot acquired by the re-target so acquire/release stays
        # balanced when the claim does not land.
        if acquired_account_id:
            from app.max_plan_pool import release_max_plan_account

            await release_max_plan_account(
                acquired_account_id,
                project_id=str(task.project_id) if task.project_id else None,
            )
        await session.rollback()
        if isinstance(exc, HTTPException):
            raise
        logger.exception("[NEXT-ROLE] warm-claim finalize failed; rolling back")
        raise HTTPException(status_code=500, detail="token_mint_failed") from exc

    await session.refresh(task)

    # Reserve the boot account's concurrency slot immediately. The DB context is
    # already stamped above, so the next reconciliation cycle will re-base this
    # count from the DB — but until then the in-memory pool would under-count the
    # reused work. Reserving here closes that sub-cycle window so per-account caps
    # cannot be bypassed by reuse. No-op when the account is absent from the pool.
    # Skipped when re-target acquired the task's own account (its slot was already
    # reserved inside _resolve_retarget).
    if not retargeted and boot_account is not None:
        from app.max_plan_pool import reserve_max_plan_account

        await reserve_max_plan_account(boot_account["account_id"])

    # Broadcast AFTER the commit so a rollback path doesn't fire a spurious
    # event.
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
    )
    response = {
        "action": "claim",
        "task_id": str(task.id),
        "warm_start": True,
        "agent_token": agent_token,
    }
    if secret_manifest is not None:
        response["secret_manifest"] = secret_manifest
    # Lane workspace-mode: pass the checkpoint SHA + lane marker to the agent
    # when a lane phase is dispatched warm. The agent uses these to hard-reset
    # the warm workspace to the last verified checkpoint (treating it as truth)
    # instead of resetting to origin/<branch>. Gated on improvement_lanes_enabled
    # so the path is completely inert when the feature flag is off.
    from app.runtime_config_state import is_improvement_lanes_enabled

    if is_improvement_lanes_enabled():
        task_ctx = task.context or {}
        _lane_checkpoint_sha = task_ctx.get("lane_checkpoint_sha")
        if task_ctx.get("is_lane_phase") and _lane_checkpoint_sha:
            response["lane_checkpoint_sha"] = str(_lane_checkpoint_sha)
            response["is_lane_phase"] = True
    return response


@router.post("/next-role")
@limiter.limit("600/minute")
async def next_role(
    request: Request,
    req: NextRoleRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Look up a warm-pinned QUEUED task for this VM and claim it.

    Phase 2b. Returns one of:
      - 200 ``{"action": "claim", "task_id": ..., "warm_start": True}`` —
        a QUEUED task whose context.warm_target_vm_id matches this VM
        was found and atomically transitioned to IN_PROGRESS.
      - 200 ``{"action": "exit", "reason": "another_vm_owns_this_pr"}`` —
        an IN_PROGRESS task for the same PR is pinned to a different
        VM; the agent should exit cleanly.
      - 204 — no warm-pinned task for this VM right now; the agent
        should keep polling until ``next_role_poll_timeout_secs``.

    JWT cross-check: the body's ``vm_instance_id`` MUST equal the JWT's
    ``vm_instance_id`` claim. Any mismatch is rejected 403.
    """
    if req.vm_instance_id != token.get("vm_instance_id"):
        raise HTTPException(status_code=403, detail="vm_instance_mismatch")

    # Liveness signal: receiving the request itself proves the VM is alive.
    _shared_state.record_vm_heartbeat(req.vm_instance_id)
    # Enrolled local runners must reacquire work through the capability-aware
    # runner dispatcher. Warm chain/backlog handoff intentionally bypasses runner
    # selection and therefore cannot prove browser, project-verification, GPU, or
    # task-type eligibility. Letting a ``runner:*`` agent claim here caused visual
    # tasks to reach Chromium-less sandboxes and backend authoring to reach a host
    # without Docker/Postgres. Exit this role immediately so the local-runner
    # daemon releases its slot; the normal dispatcher can then pin only compatible
    # work or fall through to cloud without the 180-second warm-poll delay.
    if req.vm_instance_id.startswith("runner:"):
        logger.info(
            "[NEXT-ROLE] Local runner %s returning to capability-aware dispatch",
            req.vm_instance_id,
        )
        return {"action": "exit", "reason": "local_runner_capability_recheck"}


    from app.warm_backlog_reuse import record_chain_poll as _record_chain_poll

    _record_chain_poll()
    now = _utcnow()

    # Race guard: refuse warm-claim on a VM that was just scheduled for
    # orchestrator-initiated deletion. With fire_and_forget_vm_delete
    # (main.py — released the agent_completion lock window), there is now
    # a ~30-60s window where the VM is still alive in GCP but the delete
    # is in flight. Without this check, the warm-start claim races into
    # that window, gets the VM, and the pending delete kills the agent
    # mid-task with [FATAL:SIGTERM] (production 2026-05-05 task cfd73cec
    # on VM agent-vm-c44258...). The marker is recorded synchronously
    # before scheduling so it is always observable here.
    from app.vm_lifecycle import is_orchestrator_initiated_delete

    _is_doomed, _delete_reason = is_orchestrator_initiated_delete(req.vm_instance_id)
    if _is_doomed:
        logger.info(
            f"[NEXT-ROLE] Refusing warm-claim on doomed VM {req.vm_instance_id} "
            f"(delete reason={_delete_reason})"
        )
        from app.warm_backlog_reuse import record_chain_doomed as _record_chain_doomed

        _record_chain_doomed()
        return Response(status_code=204)

    # Postgres can use the JSONB ``?`` operator (``has_key``) against
    # ``ix_task_context_gin``; SQLite test runs fall back to the portable
    # JSON-extract form. Computed once here because both the chain-continuity
    # lookup and the chain-fork exit below need it.
    use_jsonb_has_key = resolve_dialect_name(session) == "postgresql"

    # Determine once whether this VM has proven it can refresh, so every warm
    # claim below (chain-continuity and backlog-reuse) mints the short TTL for
    # a confirmed VM and only falls back to the long bootstrap window for a VM
    # that has never refreshed (e.g. an old image). See _vm_is_refresh_capable.
    vm_refresh_capable = await _vm_is_refresh_capable(session, req.vm_instance_id)

    # Warm-pool dispatch claim (efficiency direction d-5fd69fe1). The orchestrator
    # dispatch hook leases an IDLE pool VM and pins a QUEUED task to it (marker
    # warm_pool_pin). The member's agent claims that task HERE, warm, routed
    # through the same _finalize_warm_claim as the other warm paths but tagged
    # origin "warm_pool" so vm_reuse_rate can attribute the lift to the pool.
    # Runs BEFORE the chain-continuity branch and is gated independently on
    # warm_pool_enabled + the pool circuit breaker, so it is inert when the pool
    # is off and decoupled from warm-continuity. The boot account is sourced from
    # the PoolVm registry row (a taskless pool VM has no prior task to read it
    # off), keeping per-account concurrency accounting correct.
    from app.warm_pool import (
        WARM_POOL_ORIGIN,
        WARM_POOL_PIN_KEY,
        get_pool_vm,
        is_warm_pool_enabled,
    )

    if is_warm_pool_enabled() and not _shared_state.is_warm_pool_suppressed():
        from app.models import Project

        # A paused project's pool pin is never claimable. A stale pin created
        # just before pause could otherwise still be claimed during drain;
        # filter Project.paused for parity with the cold dispatch, backlog-reuse
        # and chain-continuity paths so pause blocks work on EVERY dispatch
        # path. The join is safe: project_id is a non-nullable FK.
        pool_filters = [
            Task.status == TaskStatus.QUEUED,
            Project.paused.is_(False),
            Task.context["warm_target_vm_id"].as_string() == req.vm_instance_id,
            or_(
                Task.retry_after_until.is_(None),
                Task.retry_after_until <= now,
            ),
        ]
        if use_jsonb_has_key:
            pool_filters.insert(2, Task.context.has_key("warm_target_vm_id"))
        # Lock only the Task row (of=Task): the join to Project is purely a
        # pause filter. Locking the shared Project row would let skip_locked
        # silently drop a claimable task whenever a sibling claim in the same
        # project held it.
        pool_stmt = (
            select(Task)
            .join(Project, Task.project_id == Project.id)
            .where(*pool_filters)
            .with_for_update(skip_locked=True, of=Task)
        )
        pool_task = (await session.execute(pool_stmt)).scalars().first()
        # Only claim genuine pool pins here; a chain pin (no warm_pool_pin marker)
        # falls through to the chain-continuity branch below, which re-finds the
        # row in this same transaction and finalizes it with its own origin.
        if pool_task is not None and (pool_task.context or {}).get(WARM_POOL_PIN_KEY):
            member = await get_pool_vm(session, req.vm_instance_id)
            # The member's FROZEN boot identity (credentials baked into its boot
            # manifest at provision time) lives on its PoolVm registry row.
            member_boot = None
            if member is not None and member.account_id:
                member_boot = {
                    "account_id": member.account_id,
                    "provider": member.provider,
                    "auth_mode": member.auth_mode,
                    "model": member.model,
                }
            # The RESOLVED account the task will run on was stamped into context
            # by the dispatch pin (_pin_task_to_pool_vm). Under the account-agnostic
            # lease this may DIFFER from the member's boot account — accounting must
            # attribute to the resolved (run) account, and the agent must
            # re-materialize its credentials before the role runs.
            pctx = pool_task.context or {}
            run_account_id = pctx.get("llm_account_id") or pctx.get(
                "max_plan_account_id"
            )
            if run_account_id:
                run_account = {
                    "account_id": str(run_account_id),
                    # Older pins and a few direct dispatch seams may omit one
                    # or more identity fields while still naming the same boot
                    # account. Inherit those fields from the durable PoolVm row
                    # so OAuth refresh handling does not silently disappear.
                    "provider": pctx.get("llm_provider")
                    or (member_boot or {}).get("provider"),
                    "auth_mode": pctx.get("llm_auth_mode")
                    or (member_boot or {}).get("auth_mode"),
                    "model": pctx.get("llm_model")
                    or (member_boot or {}).get("model"),
                }
            else:
                # Account wildcard / local VM: no re-target, run on boot identity.
                run_account = member_boot
            # Re-target the agent's credentials when the resolved run account
            # differs from the member's frozen boot identity. Without this the
            # task would silently execute on the member's wrong frozen account
            # (rate-limit / billing misattribution, 429 cascades). A mismatch can
            # only arise when the lease was relaxed (operator enabled re-target),
            # and emitting the manifest is exactly what keeps the relaxed lease
            # safe — so this is unconditional on any detected mismatch.
            secret_manifest = None
            account_mismatch = (
                run_account is not None
                and member_boot is not None
                and (
                    run_account["account_id"] != member_boot["account_id"]
                    or run_account.get("provider") != member_boot.get("provider")
                    or run_account.get("model") != member_boot.get("model")
                )
            )
            if account_mismatch:
                from app.max_plan_pool import build_account_retarget_manifest

                secret_manifest = build_account_retarget_manifest(
                    run_account["account_id"],
                    provider=run_account.get("provider"),
                    auth_mode=run_account.get("auth_mode"),
                    model=run_account.get("model"),
                    task=pool_task,
                    runtime_config=_shared_state.runtime_config,
                )
                if secret_manifest is None:
                    # LANDS-TOGETHER INVARIANT: a relaxed (account-mismatched)
                    # lease MUST re-materialize the resolved account's
                    # credentials before the role runs. The manifest builder
                    # returns None when the resolved account is unknown to the
                    # in-memory pool (e.g. evicted/reloaded between dispatch and
                    # claim). The lease is already relaxed + pinned by this
                    # point, so we CANNOT honor the "leave on boot creds" promise
                    # by continuing — finalizing here would run the task on the
                    # member's WRONG frozen boot credentials (rate-limit / billing
                    # misattribution, 429 cascades). Abort the warm claim: roll
                    # back (releasing the FOR UPDATE lock, leaving the task
                    # QUEUED untouched) and 204 so the orchestrator re-dispatches
                    # it — cold once the warm-handoff deadline expires — on the
                    # correct account.
                    from app.warm_vm_state import record_pool_claim_failure

                    record_pool_claim_failure("retarget_manifest_unavailable")
                    logger.warning(
                        "[NEXT-ROLE] Aborting warm_pool claim for task "
                        f"{pool_task.id} on vm {req.vm_instance_id}: account "
                        f"mismatch (boot={member_boot['account_id']} "
                        f"run={run_account['account_id']}) but re-target manifest "
                        "unavailable; falling back to cold dispatch"
                    )
                    await session.rollback()
                    return Response(status_code=204)
            try:
                result = await _finalize_warm_claim(
                    session,
                    pool_task,
                    req.vm_instance_id,
                    origin=WARM_POOL_ORIGIN,
                    refresh_capable=vm_refresh_capable,
                    boot_account=run_account,
                )
            except Exception:
                from app.warm_vm_state import record_pool_claim_failure

                record_pool_claim_failure("finalize_error")
                raise
            if isinstance(result, Response):
                return result
            if secret_manifest is not None:
                # Transient credential re-target grant; the agent re-materializes
                # it BEFORE running the role. Not persisted anywhere.
                result["secret_manifest"] = secret_manifest
            logger.info(
                f"[NEXT-ROLE] Warm-start claim (warm_pool): task={result['task_id']} "
                f"vm={req.vm_instance_id} project={token.get('project_id')} "
                f"retarget={'yes' if secret_manifest is not None else 'no'}"
            )
            return result

    # The VM's frozen boot identity (provider/account/model of the prior role).
    # Computed once here because both the chain-continuity and backlog-reuse
    # claims need it: as the boot-compat pre-check for backlog eligibility and
    # as the re-target comparison key in _finalize_warm_claim.
    vm_boot = await _prior_task_vm_boot(session, req.prior_task_id)

    # Circuit-breaker / feature-flag gate: if warm continuity is disabled or
    # auto-suppressed (pin-clear rate exceeded the threshold), skip the warm-
    # chain-continuity lookup so no new warm chains start. The orchestrator's
    # provisioning loop clears outstanding warm pins when the breaker is active;
    # skipping the lookup here closes the brief race window between the breaker
    # tripping and the next orchestrator cycle. This gate governs ONLY
    # chain-continuity claims — backlog-reuse (its own ``warm_backlog_reuse_enabled``
    # flag) and the chain-fork exit below still run.
    warm_continuity_active = (
        _shared_state.runtime_config.get("warm_continuity_enabled", True)
        and not _shared_state.is_warm_continuity_suppressed()
    )
    if warm_continuity_active:
        from app.models import Project

        # Look up a QUEUED task pinned to this VM. Optional prior_pr_number
        # filter narrows further so a stray pin from an unrelated chain
        # doesn't get claimed.
        #
        # A paused project's warm chain is never claimable: pause is a
        # first-class persisted switch (distinct from terminal status and from
        # autonomous_enabled) that blocks ALL new dispatch on EVERY path. The
        # orchestrator's cold candidate query and the backlog-reuse path both
        # filter Project.paused; the chain-continuity claim must filter it
        # identically so a paused project's create->review->fix->... chain
        # can't keep spawning and running new QUEUED work. The join is safe:
        # project_id is a non-nullable FK.
        warm_vm_filters = [
            Task.status == TaskStatus.QUEUED,
            Project.paused.is_(False),
        ]
        if use_jsonb_has_key:
            warm_vm_filters.append(Task.context.has_key("warm_target_vm_id"))
        warm_vm_filters.append(
            or_(
                Task.retry_after_until.is_(None),
                Task.retry_after_until <= now,
            )
        )
        warm_vm_filters.append(
            Task.context["warm_target_vm_id"].as_string() == req.vm_instance_id
        )
        stmt = (
            select(Task)
            .join(Project, Task.project_id == Project.id)
            .where(*warm_vm_filters)
        )
        if req.prior_pr_number is not None:
            prior_pr_filters = []
            if use_jsonb_has_key:
                prior_pr_filters.append(Task.context.has_key("warm_target_pr"))
            prior_pr_filters.append(
                Task.context["warm_target_pr"].as_string() == str(req.prior_pr_number)
            )
            stmt = stmt.where(*prior_pr_filters)
        # Lock only the Task row (of=Task): the join to Project is purely the
        # pause filter; locking the shared Project row would let skip_locked
        # silently drop a claimable task when a sibling claim in the same
        # project held it.
        stmt = stmt.with_for_update(skip_locked=True, of=Task)

        task = (await session.execute(stmt)).scalars().first()

        # A pool pin (warm_pool_pin marker) reuses warm_target_vm_id but is
        # owned exclusively by the warm-pool branch above (origin "warm_pool").
        # If one reaches here the pool branch was inert (flag off/suppressed), so
        # leave it QUEUED rather than claim it under the chain origin.
        if task is not None and (task.context or {}).get("warm_pool_pin"):
            task = None

        if task is not None:
            result = await _finalize_warm_claim(
                session,
                task,
                req.vm_instance_id,
                origin="next_role_endpoint",
                refresh_capable=vm_refresh_capable,
                vm_boot=vm_boot,
            )
            from app.warm_backlog_reuse import (
                record_chain_claimed as _record_chain_claimed,
            )

            if isinstance(result, Response):
                return result
            _record_chain_claimed()
            logger.info(
                f"[NEXT-ROLE] Warm-start claim (chain): task={result['task_id']} "
                f"vm={req.vm_instance_id} prior_task={req.prior_task_id} "
                f"prior_pr={req.prior_pr_number}"
            )
            return result
    else:
        logger.info(
            "[NEXT-ROLE] Warm continuity disabled/suppressed (circuit breaker) — "
            "skipping chain-continuity claim for VM %s",
            req.vm_instance_id,
        )
        from app.warm_backlog_reuse import (
            record_chain_skipped as _record_chain_skipped,
        )

        if not _shared_state.runtime_config.get("warm_continuity_enabled", True):
            _record_chain_skipped("disabled")
        else:
            _record_chain_skipped("breaker_tripped")

    # Backlog-reuse branch (efficiency direction d-91d8dede). When the flag is
    # enabled an idle VM with no chain-pinned follow-on may claim the next
    # eligible QUEUED task from its project's general backlog and run it warm.
    # Attempted BEFORE the chain-fork exit below so a forked chain doesn't
    # strand an otherwise reusable VM. When the flag is OFF this whole block is
    # skipped and behavior is byte-for-byte the pre-existing chain-only path —
    # including, on no claim, the chain-fork exit / 204 so no idle VM is kept
    # alive when there is no genuine pending work to reuse it for.
    # A VM-scoped (pool) token must NEVER reach the credential-agnostic
    # backlog-reuse branch. A pool VM runs on the LLM creds frozen into its boot
    # manifest, and the ONLY path allowed to hand it work is the warm-pool
    # dispatch lease above (lease_idle_pool_vm enforces account/provider/model
    # compatibility + the hard tenant gate). select_backlog_reuse_candidate
    # applies NO such gate, so letting an IDLE pool VM (no warm_pool_pin task yet)
    # claim here would run a credential-incompatible task on its boot account and
    # bypass per-account concurrency accounting. A taskless pool VM with no pinned
    # task simply keeps polling (204) until the orchestrator leases it.
    _is_pool_token = token.get("token_scope") == VM_SCOPED_TOKEN_SCOPE
    if (
        _shared_state.runtime_config.get("warm_backlog_reuse_enabled", False)
        and not _is_pool_token
    ):
        if _shared_state.is_warm_backlog_reuse_suppressed():
            from app.warm_backlog_reuse import (
                record_claim_skipped as _record_claim_skipped,
            )

            _record_claim_skipped("circuit_breaker_tripped")
        else:
            _project_claim = token.get("project_id")
            _project_uuid = None
            if _project_claim:
                try:
                    _project_uuid = uuid.UUID(str(_project_claim))
                except (ValueError, TypeError):
                    _project_uuid = None
            if _project_uuid is not None:
                from app.warm_backlog_reuse import (
                    record_next_role_poll_during_defer as _record_next_role_poll,
                    record_warm_claim_succeeded as _record_warm_claim_succeeded,
                    select_backlog_reuse_candidate,
                )

                # Record poll if we're inside a teardown-defer window the
                # agent_status completion handler already registered. Check
                # BEFORE refreshing so only pre-existing windows count.
                if _shared_state.is_backlog_first_refusal_active(str(_project_uuid)):
                    _record_next_role_poll()

                # Refresh the project's first-refusal window so the orchestrator
                # holds off cold-provisioning eligible backlog tasks while this VM
                # polls — biasing the race toward reuse. The SKIP-LOCKED claim
                # below still guarantees no double-provision if both race.
                _shared_state.register_backlog_first_refusal(str(_project_uuid))

                backlog_task = await select_backlog_reuse_candidate(
                    session,
                    _project_uuid,
                    req.vm_instance_id,
                    vm_boot=vm_boot,
                )
                if backlog_task is not None:
                    try:
                        result = await _finalize_warm_claim(
                            session,
                            backlog_task,
                            req.vm_instance_id,
                            origin="backlog_reuse",
                            refresh_capable=vm_refresh_capable,
                            vm_boot=vm_boot,
                        )
                    except Exception:
                        _shared_state.record_backlog_reuse_failure("finalize_error")
                        raise
                    if isinstance(result, Response):
                        return result
                    _record_warm_claim_succeeded()
                    logger.info(
                        f"[NEXT-ROLE] Warm-start claim (backlog_reuse): "
                        f"task={result['task_id']} vm={req.vm_instance_id} "
                        f"project={_project_uuid}"
                    )
                    return result
                # select_backlog_reuse_candidate records the skip reason
                # (no_eligible vs lost_claim_race) itself — it is the only
                # caller that can tell the two None cases apart.

    # No matching warm-pinned task. Check whether another VM owns an
    # IN_PROGRESS task for this PR — if so, the warm chain has already
    # forked and this VM should exit cleanly.
    if req.prior_pr_number is not None:
        other_pr_filters = [
            Task.status == TaskStatus.IN_PROGRESS,
            Task.context["warm_target_pr"].as_string() == str(req.prior_pr_number),
            Task.vm_instance_id.is_not(None),
            Task.vm_instance_id != req.vm_instance_id,
        ]
        if use_jsonb_has_key:
            other_pr_filters.insert(1, Task.context.has_key("warm_target_pr"))
        other_stmt = select(Task).where(*other_pr_filters)
        other = (await session.execute(other_stmt)).scalars().first()
        if other is not None:
            if warm_continuity_active:
                from app.warm_backlog_reuse import (
                    record_chain_other_vm_pr as _record_chain_other_vm_pr,
                )

                _record_chain_other_vm_pr()
            return {
                "action": "exit",
                "reason": "another_vm_owns_this_pr",
            }

    # Nothing to claim. Tell the agent to keep polling.
    if warm_continuity_active:
        from app.warm_backlog_reuse import (
            record_chain_no_pinned as _record_chain_no_pinned,
        )

        _record_chain_no_pinned()
    return Response(status_code=204)
