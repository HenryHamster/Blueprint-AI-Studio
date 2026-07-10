"""Agent-facing API endpoints (lifecycle + auth helpers)."""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.auth import (
    verify_agent_jwt,
    create_agent_token,
    AGENT_TOKEN_TTL_HOURS,
    VM_SCOPED_TOKEN_SCOPE,
)
from app.database import get_session, AsyncSessionLocal, resolve_dialect_name
from app.landed_signal import is_merged_completion
from app.models import (
    Project,
    Task,
    TaskStatus,
    TaskHistory,
    Artifact,
    ArtifactType,
    VmContact,
    KnowledgeDocument,
    SourceKind,
    _on_task_terminal_callbacks,
    _on_task_revive_callbacks,
    _utcnow,
)
from app.time_utils import _to_naive_utc
from app.schemas import (
    ArtifactDownloadResponse,
    BuildDiscoveryReport,
    UploadUrlRequest,
    HeartbeatRequest,
    SpawnTaskRequest,
)
from pydantic import BaseModel, ConfigDict, Field
from app.payload_schemas import validate_task_payload
from app.shared_state import (
    limiter,
    get_project_promotion_target,
    get_project_integration_branch,
)
from app.task_capabilities import (
    CODE_REVIEW_GATED_TASK_TYPES,
    agent_capability_slice,
)
import app.shared_state as _shared_state
from app.compute_experiment_contract import resolve_compute_spend_approval
from app.storage import generate_upload_signed_url, ARTIFACT_BUCKET_NAME
from app.artifact_prerequisites import (
    ARTIFACT_PREREQUISITES_KEY,
    backfill_artifact_prerequisites,
)
from app.github import (
    create_check_run,
    get_pull_request,
)
from app.broadcast import broadcast_to_project
from app.claim_analytics import stamp_cold_claim_timing
from app.sr_blind_spots import (
    aggregate_sr_blind_spot_themes,
    aggregate_sr_co_failure_pairs,
    safe_check_results,
)
from app.pipeline_health import compute_pipeline_health_summary
from app.knowledge_query import (
    get_knowledge_document as kq_get_knowledge_document,
    list_knowledge_documents as kq_list_knowledge_documents,
    serialize_knowledge_document,
)
from app.claim_task_helpers import (
    _has_local_cycle,  # noqa: F401 — re-exported for use in task_lifecycle_requeue
    _parse_failed_approach,
    _compute_failure_stage_pattern,
    _build_vision_summary,
    _build_direction_narrative,
    _build_problem_model_summary,
    _build_direction_contract,
    annotate_stale_build_config_dirs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def _truncate_vm_instance_id(vm_instance_id: object) -> str:
    if vm_instance_id is None:
        return "none"
    value = str(vm_instance_id)
    if len(value) <= 12:
        return value
    return f"{value[:12]}..."


def _effective_repo_map_holdback(project: object) -> float:
    """Return the repo_map holdback fraction for *project*, consulting trial verdicts."""
    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    default = float(_shared_state.runtime_config.get("repo_map_holdback_fraction", 0.0))
    if project is None:
        return default
    return get_effective_holdback_fraction(
        get_trial_verdicts(project), "repo_map", default
    )


def _effective_module_interfaces_holdback(project: object) -> float:
    """Return the module_interfaces holdback fraction for *project*, consulting trial verdicts."""
    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    default = float(
        _shared_state.runtime_config.get("module_interfaces_holdback_fraction", 0.0)
    )
    if project is None:
        return default
    return get_effective_holdback_fraction(
        get_trial_verdicts(project), "module_interfaces", default
    )


def _effective_related_tests_holdback(project: object) -> float:
    """Return the related_tests holdback fraction for *project*, consulting trial verdicts."""
    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    default = float(
        _shared_state.runtime_config.get("related_tests_holdback_fraction", 0.0)
    )
    if project is None:
        return default
    return get_effective_holdback_fraction(
        get_trial_verdicts(project), "related_tests", default
    )


def _effective_affected_consumers_holdback(project: object) -> float:
    """Return the affected_consumers holdback fraction for *project*, consulting trial verdicts."""
    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    default = float(
        _shared_state.runtime_config.get("affected_consumers_holdback_fraction", 0.0)
    )
    if project is None:
        return default
    return get_effective_holdback_fraction(
        get_trial_verdicts(project), "affected_consumers", default
    )


def _effective_diagnostics_after_edit_holdback(project: object) -> float:
    """Return the diagnostics_after_edit holdback fraction for *project*, consulting trial verdicts."""
    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    default = float(
        _shared_state.runtime_config.get(
            "diagnostics_after_edit_holdback_fraction", 0.1
        )
    )
    if project is None:
        return default
    return get_effective_holdback_fraction(
        get_trial_verdicts(project), "diagnostics_after_edit", default
    )


def _log_agent_auth_reject(
    reason: str,
    *,
    path: str,
    task_id: object,
    token_vm_instance_id: object = None,
    token_project_id: object = None,
    error: object | None = None,
) -> None:
    logger.info(
        "[AGENT_AUTH_REJECT] reason=%s path=%s task_id=%s token_vm_instance_id=%s token_project_id=%s error=%s",
        reason,
        path,
        task_id,
        _truncate_vm_instance_id(token_vm_instance_id),
        token_project_id or "none",
        error if error is not None else "none",
    )


async def _create_review_check_run_bg(
    task_id: uuid.UUID,
    pr_number: int,
    check_run_name: str,
) -> None:
    """Background helper: create an in_progress Check Run when a review task claims work.

    Opens its own DB session so it can run after the claim response is returned.
    Stores the check_run_id in task.context so the verdict handler can update it later.
    Errors are swallowed — check run creation is best-effort and must not affect the pipeline.
    """
    try:
        async with AsyncSessionLocal() as session:
            gh_token, owner, repo = await get_github_token_for_task(task_id, session)
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            head_sha = pr_data.get("head", {}).get("sha", "")
            if not head_sha:
                logger.warning(
                    f"[CHECK RUN] No head_sha for PR #{pr_number} task {task_id}"
                )
                return
            result = await create_check_run(
                gh_token, owner, repo, head_sha, name=check_run_name
            )
            if result and result.get("id"):
                task = await session.get(Task, task_id)
                if task:
                    task.context = {
                        **(task.context or {}),
                        "check_run_id": result["id"],
                    }
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[CHECK RUN] Created check run {result['id']} for review task {task_id} PR #{pr_number}"
                    )
    except Exception as e:  # noqa: BLE001 — best-effort background task, never block claim response
        logger.warning(
            f"[CHECK RUN] Failed to create check run for task {task_id}: {e}"
        )


# ---------------------------------------------------------------------------
# JWT revocation state (moved from main.py)
# ---------------------------------------------------------------------------

# Maps task_id -> timestamp of termination. Entries older than _TERMINATED_TTL
# are evicted to prevent unbounded growth (JWTs expire after 1h).
_terminated_task_ids: dict[str, float] = {}
_TERMINATED_TTL = 75 * 60  # 75 min — 60 min JWT lifetime + 15 min grace
_TERMINATED_TASK_IDS_MAX = 5000  # evict expired entries when cache exceeds this size
_on_task_terminal_callbacks.append(
    lambda tid: _terminated_task_ids.__setitem__(tid, time.time())
)
# Production 2026-05-05: when a task is requeued (terminal → QUEUED, or any
# in-flight set to a non-terminal status), drop the deny-list entry so the
# next ``/api/agent/claim`` doesn't 401 with "Task is no longer active". The
# cache could otherwise persist across the lifecycle of the orchestrator
# revision, causing claim 401 loops. See models.py:_on_task_revive_callbacks
# for the full rationale.
_on_task_revive_callbacks.append(lambda tid: _terminated_task_ids.pop(tid, None))

_AGENT_INACTIVE_TASK_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)

# Peer code-change and analysis task types that do real code/analysis work and
# therefore benefit from the same baseline launch context (concurrent/sibling/
# queued task awareness, recent completions, lessons, and direction
# goal/contract/narrative) that create/fix/refactor/research/plan/review get.
# Single source of truth for the two claim-path context gates below. These reuse
# the existing per-project queries — no new heavy computation on the hot claim
# path; the expensive review-only analytics stay gated to their narrower sets.
_PEER_CODE_ANALYSIS_TYPES = frozenset({"qa", "migrate", "debug", "perf", "investigate"})

# Issue M3 (warm-VM continuity audit): JWTs for terminated tasks remain valid
# for the JWT's full TTL (1h). The is_idempotent_path branch accepts these
# terminal-task tokens on /next-role and /vm-heartbeat so a legitimate warm
# chain can poll for the next role immediately after task completion. This
# window is intentionally short (default 5 min) — it covers only the brief
# next-role poll and warm-claim handoff. A replay well after that window is
# suspicious; reject terminal-task tokens that are too old.
_TERMINAL_IDEMPOTENT_FRESHNESS_SECS = int(
    os.getenv("AGENT_TERMINAL_IDEMPOTENT_FRESHNESS_SECS", "300")
)

# Retain strong references to fire-and-forget background tasks so the event loop
# doesn't GC them before completion (asyncio only holds a weak reference).
_bg_tasks: set[asyncio.Task] = set()

# Keys an agent JWT-holder MUST NOT be able to inject into a spawned child
# task's context. Without this filter, a compromised or malicious agent could
# call /api/agent/spawn with `req.context = {"warm_target_vm_id": "<victim>"}`
# and the orchestrator's skip-provision branch would honor the pin, hijacking
# another VM's warm chain. Phase 2c (warm-VM continuity) issue #3.
_AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "warm_target_vm_id",
        "warm_target_pr",
        "warm_handoff_deadline",
        # Defense-in-depth — these are also orchestrator-internal:
        "_timing",
    }
)


async def verify_agent_token(
    request: Request,
    x_agent_token: str = Header(
        ..., description="Secret token for agent authentication"
    ),
):
    """
    Validates the short-lived JWT passed by the agent VM.

    Audit 2026-04-25 (W3/E2): the path-based exemption for /status and
    /credentials/rotate has been replaced by a stricter VM-instance check.
    Same-VM idempotent retries on terminal tasks are still allowed (so a
    legitimate post-commit retry from the original agent succeeds), but a
    DIFFERENT VM trying to override the status of a requeued task is now
    rejected with 403 vm_instance_mismatch — failing fast on a real
    failure mode, not on legitimate work.

    Rejects tokens for tasks that are no longer active
    (completed/failed/cancelled/blocked), EXCEPT when the same VM that the task
    is currently assigned to is retrying
    /status or /credentials/rotate (idempotency).
    """
    payload = verify_agent_jwt(x_agent_token)
    task_id = payload.get("sub")
    # VM-scoped (taskless / warm-pool) token: a pool VM that has not yet claimed
    # any task. It carries no task ``sub`` — only ``vm_instance_id`` + project
    # scope, tagged ``token_scope == "vm"``. Accept it here (so the taskless
    # endpoints vm-heartbeat / next-role can use it; /heartbeat and /claim still
    # require a task ``sub`` and reject a bare VM-scoped token) and skip
    # the task-binding / terminal-freshness checks below, which are meaningless
    # without a task. The endpoints themselves still scope every action by the
    # JWT's ``vm_instance_id``. The task-scoped path (``sub`` present, no
    # ``token_scope``) is unchanged. A VM-scoped token MUST carry a
    # vm_instance_id, or there is nothing to authorize against.
    if payload.get("token_scope") == VM_SCOPED_TOKEN_SCOPE or not task_id:
        if not payload.get("vm_instance_id"):
            _log_agent_auth_reject(
                "vm_scoped_token_missing_vm",
                path=request.url.path,
                task_id=task_id,
                token_vm_instance_id=payload.get("vm_instance_id"),
                token_project_id=payload.get("project_id"),
            )
            raise HTTPException(status_code=401, detail="vm_scoped_token_missing_vm")
        return payload
    token_vm = payload.get("vm_instance_id")
    # /next-role and /vm-heartbeat are reached AFTER a task has terminated
    # (the agent polls for the next role using the prior task's JWT). Treat
    # them as idempotent paths so the verify-agent-token guard accepts the
    # terminal-task JWT, but still rejects a different VM trying to use it.
    is_idempotent_path = request.url.path in (
        "/api/agent/status",
        "/api/agent/credentials/rotate",
        "/api/agent/next-role",
        "/api/agent/vm-heartbeat",
    ) or (
        # Self-merge: an automatic fresh-reviewer pass runs AFTER ./done has
        # already posted the task's terminal status, then persists its findings
        # back into the now-COMPLETED task's own _self_review_outcome. Treat the
        # reviewer-merge path as terminal-task-allowed so the same VM can land
        # that merge; the vm_instance_mismatch guard below still rejects a
        # DIFFERENT VM replaying a leaked terminal-task token.
        request.url.path.startswith("/api/agent/tasks/")
        and request.url.path.endswith("/reviewer-merge")
    )
    # Issue M3 freshness check (warm-VM rev): only the warm-VM idempotent
    # paths /next-role and /vm-heartbeat carry the replay risk for terminal
    # tasks (the in-progress paths /status and /credentials/rotate already
    # guard via vm_instance_mismatch). Apply the freshness window only to
    # the warm-VM paths so the same-VM /status retry path stays intact.
    is_warm_idempotent_path = request.url.path in (
        "/api/agent/next-role",
        "/api/agent/vm-heartbeat",
    )

    def _enforce_terminal_freshness() -> None:
        """Reject a terminal-task JWT on warm-idempotent paths if its iat is
        outside the freshness window. A leaked JWT replayed an hour later is
        not a legitimate warm-chain continuation."""
        if not is_warm_idempotent_path:
            return
        iat = payload.get("iat")
        if iat is None:
            _log_agent_auth_reject(
                "terminal_token_expired_for_idempotent",
                path=request.url.path,
                task_id=task_id,
                token_vm_instance_id=token_vm,
                token_project_id=payload.get("project_id"),
            )
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            )
        try:
            iat_secs = float(iat)
        except (TypeError, ValueError) as conv_exc:
            _log_agent_auth_reject(
                "terminal_token_expired_for_idempotent",
                path=request.url.path,
                task_id=task_id,
                token_vm_instance_id=token_vm,
                token_project_id=payload.get("project_id"),
                error=conv_exc,
            )
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            ) from conv_exc
        if (time.time() - iat_secs) > _TERMINAL_IDEMPOTENT_FRESHNESS_SECS:
            _log_agent_auth_reject(
                "terminal_token_expired_for_idempotent",
                path=request.url.path,
                task_id=task_id,
                token_vm_instance_id=token_vm,
                token_project_id=payload.get("project_id"),
            )
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            )

    # Fast path: known terminated (in-memory, O(1))
    # Same-VM retry on idempotent paths is allowed; a different-VM call is rejected.
    if task_id in _terminated_task_ids and not is_idempotent_path:
        _log_agent_auth_reject(
            "deny_list_hit_non_idempotent_path",
            path=request.url.path,
            task_id=task_id,
            token_vm_instance_id=token_vm,
            token_project_id=payload.get("project_id"),
        )
        raise HTTPException(status_code=401, detail="Task is no longer active")
    if task_id in _terminated_task_ids and is_warm_idempotent_path:
        # Cache says terminated AND we're on a warm-VM idempotent path —
        # apply the freshness window before falling through to the DB check.
        _enforce_terminal_freshness()
    # On idempotent paths with terminated task we fall through to the DB
    # check so we can compare token_vm against task.vm_instance_id (the
    # cache alone doesn't carry that field).

    # DB proof path: required before every authenticated request so fail-closed
    # behavior applies even when an earlier call warmed the cache.
    # Use app.dependency_overrides to respect test session overrides
    try:
        app = request.app
        session_factory = app.dependency_overrides.get(get_session, get_session)
        async for session in session_factory():
            task = await session.get(Task, uuid.UUID(task_id))
            if task is None:
                _log_agent_auth_reject(
                    "task_missing",
                    path=request.url.path,
                    task_id=task_id,
                    token_vm_instance_id=token_vm,
                    token_project_id=payload.get("project_id"),
                )
                raise HTTPException(status_code=401, detail="Task is no longer active")
            # Audit W3/E10: project_id authz when the token carries it.
            token_project = payload.get("project_id")
            if token_project and str(task.project_id) != token_project:
                _log_agent_auth_reject(
                    "project_mismatch",
                    path=request.url.path,
                    task_id=task_id,
                    token_vm_instance_id=token_vm,
                    token_project_id=token_project,
                )
                raise HTTPException(status_code=403, detail="project_mismatch")
            # Audit W3/E2: stale-VM guard. If the task has been reassigned
            # to a different VM (reaper requeue), the original agent's
            # token must NOT be allowed to mutate state for the new VM's
            # task. Same-VM retries are still allowed (idempotent path).
            if token_vm and task.vm_instance_id and task.vm_instance_id != token_vm:
                _log_agent_auth_reject(
                    "vm_instance_mismatch",
                    path=request.url.path,
                    task_id=task_id,
                    token_vm_instance_id=token_vm,
                    token_project_id=token_project,
                )
                raise HTTPException(status_code=403, detail="vm_instance_mismatch")
            if task.status in _AGENT_INACTIVE_TASK_STATUSES:
                _terminated_task_ids[task_id] = time.time()
                # Same-VM idempotent retry on terminal task is OK; different
                # VM (or any non-idempotent path) fails fast.
                if not is_idempotent_path:
                    _log_agent_auth_reject(
                        "deny_list_hit_non_idempotent_path",
                        path=request.url.path,
                        task_id=task_id,
                        token_vm_instance_id=token_vm,
                        token_project_id=token_project,
                    )
                    raise HTTPException(
                        status_code=401, detail="Task is no longer active"
                    )
                # Issue M3: warm-VM idempotent paths additionally require a
                # fresh JWT (iat within _TERMINAL_IDEMPOTENT_FRESHNESS_SECS)
                # so a leaked terminal-task token can't be replayed hours
                # later to spam-keepalive a phantom VM identity.
                _enforce_terminal_freshness()
    except HTTPException:
        raise
    except Exception as e:
        _log_agent_auth_reject(
            "db_unavailable",
            path=request.url.path,
            task_id=task_id,
            token_vm_instance_id=token_vm,
            token_project_id=payload.get("project_id"),
            error=e,
        )
        raise HTTPException(status_code=503, detail="agent_auth_db_unavailable") from e

    now = time.time()
    # Evict stale terminated entries (JWT expired, entry is redundant)
    if len(_terminated_task_ids) > _TERMINATED_TASK_IDS_MAX:
        cutoff = now - _TERMINATED_TTL
        stale = [k for k, v in _terminated_task_ids.items() if v <= cutoff]
        for k in stale:
            _terminated_task_ids.pop(k, None)
    return payload


# ---------------------------------------------------------------------------
# GitHub token cache — extracted to app.github_token_resolver (router-free).
# Re-exported here so the 15+ existing
# `from app.routers.agent import get_github_token_for_task` (and the cache
# internals / invalidation helper) call sites keep working unchanged.
# ---------------------------------------------------------------------------

from app.github_token_resolver import (  # noqa: F401
    _GITHUB_TOKEN_TTL,
    _github_token_cache,
    _github_token_cache_lock,
    get_github_token_for_task,
    invalidate_github_token_cache,
)


# ---------------------------------------------------------------------------
# Agent lifecycle endpoints
# ---------------------------------------------------------------------------


@router.get("/task-capabilities")
@limiter.limit("120/minute")
async def agent_task_capabilities(
    request: Request,
    token: dict = Depends(verify_agent_token),
):
    """Publish the authoritative task-capability contract to the agent VM.

    Mirrors the operator-facing GET /api/admin/task-capabilities (which is
    Bearer/Firebase-authed and therefore unreachable with the agent's
    ``X-Agent-Token`` JWT). The agent consumes this contract to derive its
    known task types and read_only/write permission partitioning from the
    single backend-owned registry instead of a hand-maintained agent table,
    so adding a backend task type does not require an agent-side edit.
    """
    from app.task_capabilities import task_capability_contract

    return {"task_capabilities": task_capability_contract()}


async def _load_active_agent_task(session: AsyncSession, token: dict) -> Task:
    """Load the task named by the agent JWT and assert it is still the live,
    same-VM active task before minting fresh credentials for it.

    Shared authorization for the credential-minting agent endpoints
    (``/refresh-token`` and ``/git-read-token``). ``verify_agent_token`` already
    rejects a JWT whose task reached an ``_AGENT_INACTIVE_TASK_STATUSES``
    terminal state or was reassigned to a *different* VM, but that leaves two
    liveness gaps a fresh credential must not slip through:

    * a task in a non-terminal-but-non-active status (e.g. ``EVALUATING``) that
      is no longer executing agent work, and
    * a requeued task whose ``vm_instance_id`` was cleared (the
      ``verify_agent_token`` mismatch guard short-circuits when either side is
      falsy).

    Enforcing the same ``vm_instance_id`` match + active-status allowlist the
    refresh endpoint has used since the 2026-04-25 W3/E1/E2 audit closes both,
    so a stale-but-unexpired JWT for a requeued/completed task cannot keep
    minting credentials.
    """
    task_id = token["sub"]
    vm_id = token["vm_instance_id"]
    task = await session.get(Task, uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    if task.vm_instance_id != vm_id:
        raise HTTPException(status_code=409, detail="vm_instance_mismatch")
    if task.status not in (
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QUEUED,
    ):
        raise HTTPException(status_code=409, detail=f"task_not_active:{task.status}")
    return task


@router.post("/refresh-token")
@limiter.limit("120/minute")
async def agent_refresh_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 1-hour TTL.

    Audit 2026-04-25 (W3/E1/E2): refresh now also validates that the task
    is still assigned to this VM — a stale agent whose task has been
    requeued to a different VM gets 409 instead of a renewed token. The
    project_id is included in the new token so the W3 cross-project
    authz check (E10) can apply uniformly.
    """
    task_id = token["sub"]
    vm_id = token["vm_instance_id"]
    task = await _load_active_agent_task(session, token)
    # A successful refresh PROVES the agent is refresh-capable. Stamping
    # last_token_refresh_at marks this task — and, via the VM-level lookup in
    # _vm_is_refresh_capable, every subsequent warm handoff on this VM — as
    # confirmed, so the long cold-start bootstrap window never sticks past the
    # first refresh.
    task.last_token_refresh_at = _utcnow()
    await session.commit()
    # The refreshed token always carries the short security-bounded TTL
    # (create_agent_token defaults to AGENT_TOKEN_TTL_HOURS), regardless of
    # whether the incoming token was a long bootstrap grant. This is the
    # downgrade point: proving refresh capability immediately retires the
    # wider bootstrap replay window.
    new_token = create_agent_token(
        task_id,
        vm_id,
        task_type=token.get("task_type", ""),
        project_id=token.get("project_id", "") or str(task.project_id),
    )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=AGENT_TOKEN_TTL_HOURS)
    return {"token": new_token, "token_expires_at": expires_at.isoformat()}


@router.get("/git-read-token")
@limiter.limit("120/minute")
async def agent_git_read_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a fresh GitHub App installation read token for the task's repo.

    Called periodically by the strategic resident VM to keep its local
    git remote credential fresh (resident needs `git fetch origin` for
    automain verification; it never pushes — push credentials are per-task).

    Uses `get_repo_installation_read_token` (contents:read scoped, cached 50 min
    per repo) so a warm cache costs zero GitHub API calls.

    Authorization is at parity with `/refresh-token`: both mint a fresh
    credential, so both require the JWT's task to still be the same-VM active
    task via `_load_active_agent_task`. Without it a stale-but-unexpired JWT for
    a requeued/completed task could keep minting read tokens until JWT expiry.
    """
    task = await _load_active_agent_task(session, token)
    project = await session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")
    owner = project.github_owner
    repo = project.github_repo
    if not owner or not repo:
        raise HTTPException(status_code=422, detail="project_has_no_github_repo")
    from app.github_auth import get_repo_installation_read_token
    gh_token = await get_repo_installation_read_token(owner, repo)
    if not gh_token:
        raise HTTPException(status_code=502, detail="github_token_unavailable")
    return {"token": gh_token, "owner": owner, "repo": repo}


async def _stamp_reviewer_recovery_count(
    session: AsyncSession,
    project_id: uuid.UUID,
    high_iter_rows,
) -> None:
    """Count tasks tagged as reviewer-recovery-after-incomplete-SR and stamp the
    aggregate onto ``Project.design_doc.sr_catch_rate_trends``.

    Detection scans both the top-level ``reviewer_recovery_origin`` marker and
    per-category ``check_results[*].origin`` entries — the marker can land in
    either spot depending on which path tagged it. No-op when nothing matched.
    """
    recovery_count = sum(
        1
        for t in high_iter_rows
        if (
            (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_recovery_origin")
            == "reviewer_recovery_after_incomplete_sr"
        )
        or any(
            isinstance(v, dict)
            and v.get("origin") == "reviewer_recovery_after_incomplete_sr"
            for v in safe_check_results(
                (t.context or {}).get("_self_review_outcome", {})
            ).values()
        )
    )
    if recovery_count <= 0:
        return
    from app.task_locking import project_lock

    async with project_lock(session, project_id, skip_locked=True) as proj:
        if proj is None:
            return
        doc = dict(proj.design_doc or {})
        trends = dict(doc.get("sr_catch_rate_trends") or {})
        trends["reviewer_recovery_after_incomplete_sr_count"] = recovery_count
        doc["sr_catch_rate_trends"] = trends
        proj.design_doc = doc
        flag_modified(proj, "design_doc")
        session.add(proj)


async def _refresh_sr_catch_rate(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Fetch SR-relevant tasks and refresh the catch-rate trend metric.

    Restricts the fetch to tasks carrying ``_self_review_outcome`` so the
    2000-row budget spans more calendar time on high-throughput projects.
    Postgres: ``has_key`` hits the GIN index on context; SQLite (standalone
    tests) omits the JSON filter and ``_stats_for_tasks`` skips non-SR rows.

    Debounced: this recompute is a ~2000-row JSONB context scan + a project
    write-lock, and it ran on EVERY claim — serializing claims per project at
    100-200 VMs. We skip the recompute (the entire scan + the lock) when it
    last ran within ``SR_CATCH_RATE_DEBOUNCE_SECONDS`` on this instance. The
    trend still updates at a bounded cadence; the claim fast-path returns its
    payload without waiting on the expensive recompute.
    """
    if not _shared_state.should_refresh_sr_catch_rate(project_id):
        logger.debug(
            "[SR_CATCH_RATE] debounced refresh for project %s (recomputed recently)",
            project_id,
        )
        return

    from app.sr_analytics import compute_sr_catch_rate_trends

    _sr_cutoff = _utcnow() - timedelta(days=28)
    dialect_name = resolve_dialect_name(session)
    _where = [
        Task.project_id == project_id,
        Task.status.in_([TaskStatus.FAILED, TaskStatus.COMPLETED]),
        Task.created_at >= _sr_cutoff,
    ]
    if dialect_name != "sqlite":
        _where.append(Task.context.has_key("_self_review_outcome"))
    _sr_rows = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.context,
                        Task.created_at,
                        Task.project_id,
                        Task.strategic_cycle_id,
                        Task.payloads,
                        raiseload=True,
                    )
                )
                .where(*_where)
                .order_by(Task.created_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    logger.debug(
        "[SR_CATCH_RATE] fetched %d SR-relevant tasks for catch-rate window",
        len(_sr_rows),
    )
    await compute_sr_catch_rate_trends(
        project_id=project_id,
        session=session,
        all_tasks=list(_sr_rows),
    )
    # Mark the bounded-cadence debounce window AFTER a successful recompute so
    # the next claim within the window skips the expensive scan + lock.
    _shared_state.mark_sr_catch_rate_refreshed(project_id)


@router.post("/claim")
@limiter.limit("200/minute")
async def claim_task(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Claim a QUEUED or PROVISIONING task for execution.

    Agent VMs call this upon startup to claim their assigned work. Transitions
    the task from QUEUED/PROVISIONING to IN_PROGRESS and returns full task
    details including payloads and context.

    Authentication: Agent JWT token (2-hour or 8-hour bootstrap TTL).

    Returns:
        {
            "id": str, "vm_instance_id": str, "payloads": list,
            "context": dict, "parent_task_id": str | null,
            ... (full Task object)
        }

    Status codes:
        200: Task claimed successfully
        404: No unclaimed task on this VM
        429: Task locked by another process (retry recommended)
        401: Invalid or expired token

    Rate limit: 200 requests/minute.
    Idempotency: Replays last successful claim within 5 minutes if present.
    """
    vm_id = token.get("vm_instance_id")
    # Phase 2c Race A mitigation: a successful claim is a strong liveness
    # signal — we wouldn't be here without an authenticated VM. Recording
    # this widens the warm-VM continuity liveness window beyond just the
    # /vm-heartbeat and /next-role endpoints.
    _shared_state.record_vm_heartbeat(vm_id)
    stmt = (
        select(Task)
        .where(
            Task.vm_instance_id == vm_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
        )
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    task = result.scalars().first()

    idempotent_replay = False
    if not task:
        # Distinguish "no task" from "task locked by another process"
        check = await session.execute(
            select(Task.id).where(
                Task.vm_instance_id == vm_id,
                Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
            )
        )
        if check.scalars().first():
            # Use 429 (not 503) so Cloud Run doesn't classify this as ContainerError
            # and quarantine the instance — see commit 88a77903 for the same pattern
            # applied to agent_status.py.
            raise HTTPException(
                status_code=429,
                detail="Task is temporarily locked, retry shortly",
                headers={"Retry-After": "2"},
            )
        # Idempotency window: apiPost retries on network errors / 30s timeout
        # aborts, but the first POST may have already committed PROVISIONING →
        # IN_PROGRESS server-side before the response was lost. Without this
        # replay, the retry hits the QUEUED/PROVISIONING filter, returns 404,
        # and the agent dies with PRE_CLAIM_FAILED on a task that was actually
        # claimed successfully. Match this VM's just-claimed task (last heartbeat
        # within 5 min — covers slow GCP provisioning and the agent's worst-case
        # retry window of API_TIMEOUT * API_MAX_RETRIES with backoff) and replay
        # the same response.
        replay_cutoff = _utcnow() - timedelta(minutes=5)
        replay_stmt = select(Task).where(
            Task.vm_instance_id == vm_id,
            Task.status == TaskStatus.IN_PROGRESS,
            Task.last_heartbeat >= replay_cutoff,
        )
        task = (await session.execute(replay_stmt)).scalars().first()
        if not task:
            raise HTTPException(
                status_code=404, detail="No queued task found for this VM instance."
            )
        idempotent_replay = True

    if not idempotent_replay:
        task.status = TaskStatus.IN_PROGRESS
        task.last_heartbeat = (
            _utcnow()
        )  # Immediate heartbeat — don't wait 30s for agent interval
        stamp_cold_claim_timing(task)
        session.add(task)
        await session.commit()
        await session.refresh(task)

    # Backfill artifact prerequisites from already-completed direct dependencies.
    # A dependent queued after its producer completed never received the push-path
    # delivery, so we hydrate the pointers here before snapshotting the context.
    await backfill_artifact_prerequisites(task, session)
    if session.new or session.dirty:
        await session.commit()
        await session.refresh(task)

    # Snapshot the claim-time scalars used while building the rest of the
    # response. The best-effort enrichment below (SR-trend analytics, review
    # effectiveness) shares this session; a DB error in any of it aborts the
    # transaction, and the next commit then does an internal rollback that
    # EXPIRES ``task``. After that a plain ``task.<attr>`` read does a sync
    # lazy-load with no greenlet -> MissingGreenlet (or PendingRollbackError on
    # the still-aborted txn) and 500s a claim that was already committed above.
    # These values are immutable for the life of the claim, so reading them
    # from locals keeps response building independent of the session's
    # post-enrichment state. (incident 2026-05-29: claim 500s at agent.py:2243
    # reading task.context after an enrichment failure expired the instance.)
    _claimed_task_id = task.id
    _claimed_project_id = task.project_id
    _claimed_context = dict(task.context or {})
    _claimed_payloads = list(task.payloads or [])
    _claimed_first_payload = task.first_payload

    # Broadcast status change to frontend
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
    )

    # Strip internal runtime-routing fields before returning task context to the agent.
    safe_context = {
        k: v
        for k, v in _claimed_context.items()
        if not k.startswith("max_plan_") and not k.startswith("llm_")
    }
    # Resolve per-project branch metadata once so we can inject it into both
    # project_metadata and each payload entry the agent sees. Agent handlers
    # (standard.ts, review-fix.ts, specialized.ts/migrate) read
    # payload.promotion_target to drive LTS-aware behavior; without injection
    # here they silently fall back to comparing base_branch to 'main' and get
    # the wrong answer for any project whose promotion_target != 'main'.
    project = await session.get(Project, task.project_id)
    project_promotion_target = (
        get_project_promotion_target(project) if project else None
    )
    project_integration_branch = (
        get_project_integration_branch(project) if project else None
    )

    _claim_runtime_model = _claimed_context.get("llm_model")
    _claim_runtime_execution_mode = _claimed_context.get("llm_execution_mode")

    response_payloads: list = []
    for entry in _claimed_payloads:
        if isinstance(entry, dict):
            patched = dict(entry)
            if isinstance(_claim_runtime_model, str) and _claim_runtime_model.strip():
                patched["model"] = _claim_runtime_model.strip()
            if (
                isinstance(_claim_runtime_execution_mode, str)
                and _claim_runtime_execution_mode.strip()
            ):
                patched["execution_mode"] = _claim_runtime_execution_mode.strip()
            if project_promotion_target and not patched.get("promotion_target"):
                patched["promotion_target"] = project_promotion_target
            if project_integration_branch and not patched.get("integration_branch"):
                patched["integration_branch"] = project_integration_branch
            response_payloads.append(patched)
        else:
            response_payloads.append(entry)

    from app.capability_trial_verdict import (
        get_effective_holdback_fraction,
        get_trial_verdicts,
    )

    _project_verdicts = get_trial_verdicts(project) if project else {}
    response = {
        "task_id": str(task.id),
        "title": task.title,
        "description": task.description,
        "payloads": response_payloads,
        "context": safe_context,
        "repo_map_holdback_fraction": get_effective_holdback_fraction(
            _project_verdicts, "repo_map", 0.0
        ),
        "related_tests_holdback_fraction": get_effective_holdback_fraction(
            _project_verdicts, "related_tests", 0.0
        ),
    }

    # Add project metadata so agents can adapt to different project structures
    if project:
        _build_cfg = project.build_config or {}
        _build_cfg_annotated = annotate_stale_build_config_dirs(_build_cfg)
        response["project_metadata"] = {
            "project_id": str(project.id),
            "project_name": project.name,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "build_config": _build_cfg_annotated,
            "approval_gate_config": project.approval_gate_config or {},
            "enable_fresh_reviewer": _build_cfg.get("enable_fresh_reviewer", False),
            "enable_parallel_review": _build_cfg.get("enable_parallel_review", False),
            "enable_parallel_fix": _build_cfg.get("enable_parallel_fix", False),
            "enable_self_improvement": project.enable_self_improvement,
            "promotion_target": project_promotion_target,
            "integration_branch": project_integration_branch,
            "repo_map_holdback_fraction": _effective_repo_map_holdback(project),
            "module_interfaces_holdback_fraction": _effective_module_interfaces_holdback(
                project
            ),
            "related_tests_holdback_fraction": _effective_related_tests_holdback(
                project
            ),
            "affected_consumers_holdback_fraction": _effective_affected_consumers_holdback(
                project
            ),
            "diagnostics_after_edit_holdback_fraction": _effective_diagnostics_after_edit_holdback(
                project
            ),
            "diagnostics_after_edit_enabled": bool(
                _shared_state.runtime_config.get("diagnostics_after_edit_enabled", True)
            ),
        }

    task_type = _claimed_first_payload.get("type", "")
    task_capability = agent_capability_slice(task_type)
    if task_capability is not None:
        response["task_capability"] = task_capability

    # Surface a reserved provisional Alembic revision (if the orchestrator
    # allocated one for this migration-producing task) so the agent consumes the
    # locked id verbatim instead of racing to derive max+1 against the live
    # chain. Additive: a claim with no reservation simply omits the key and the
    # agent falls back to local numbering, preserving backward compatibility for
    # tasks queued before the reservation rollout.
    #
    # Fix and review tasks have their own task.id but no reservation of their
    # own; they inherit the reservation allocated to the original (create) task.
    #
    # The parent_task_id chain alone is NOT sufficient: fix/review tasks are
    # spawned as SIBLINGS of the create task (review_pipeline sets their
    # parent_task_id to the create task's plan/research parent, not the create
    # task itself), so walking parents never reaches the reservation holder. The
    # authoritative link is the original_task_id carried in the payload /
    # review_loop context, which always points at the create task that holds the
    # reservation. Seed the ancestor set with it first, then still walk the
    # parent chain to cover the rare no-plan-parent topology where the create
    # task IS the direct parent.
    from app.migration_reservations import (
        get_migration_reservation_for_task_chain,
    )

    _ancestor_ids: list = [task.id]

    def _seed_original_task_id(raw: object) -> None:
        if not raw:
            return
        try:
            parsed = uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            return
        if parsed not in _ancestor_ids:
            _ancestor_ids.append(parsed)

    _seed_original_task_id(_claimed_first_payload.get("original_task_id"))
    _seed_original_task_id(
        (_claimed_context.get("review_loop") or {}).get("original_task_id")
    )

    _MAX_ANCESTOR_DEPTH = 5
    if task.parent_task_id is not None:
        _ancestor_result = await session.execute(
            text(
                """
                WITH RECURSIVE ancestor_chain(id, parent_task_id, depth) AS (
                    SELECT id, parent_task_id, 0
                    FROM task
                    WHERE id = :start_id
                    UNION ALL
                    SELECT t.id, t.parent_task_id, a.depth + 1
                    FROM task t
                    JOIN ancestor_chain a ON t.id = a.parent_task_id
                    WHERE a.parent_task_id IS NOT NULL AND a.depth < :max_depth
                )
                SELECT id FROM ancestor_chain
                WHERE id != :self_id
                """
            ),
            {
                "start_id": str(task.parent_task_id),
                "max_depth": _MAX_ANCESTOR_DEPTH,
                "self_id": str(task.id),
            },
        )
        for _row in _ancestor_result:
            _row_id = uuid.UUID(str(_row[0]))
            if _row_id not in _ancestor_ids:
                _ancestor_ids.append(_row_id)

    _migration_reservation = await get_migration_reservation_for_task_chain(
        session, task_ids=_ancestor_ids
    )
    if _migration_reservation is not None:
        response["migration_reservation"] = {
            "provisional_revision": _migration_reservation.provisional_revision,
            "provisional_down_revision": (
                _migration_reservation.provisional_down_revision
            ),
        }
    if (
        task_type in ("research", "plan", "create", "fix", "refactor", "review")
        or task_type in _PEER_CODE_ANALYSIS_TYPES
    ):
        # Active and queued tasks in the same project. The downstream payload
        # only needs the latest context snippets, so keep one round-trip while
        # preserving the previous per-status caps. A per-status ROW_NUMBER()
        # window prevents queued work from crowding out active work (or vice
        # versa) when one bucket is much newer than the other.
        _active_queued_loadonly = load_only(
            Task.id,
            Task.title,
            Task.status,
            Task.payloads,
            Task.context,
            Task.project_id,
            raiseload=True,
        )
        _active_queued_ranked = (
            select(
                Task.id.label("task_id"),
                Task.status.label("task_status"),
                func.row_number()
                .over(
                    partition_by=Task.status,
                    order_by=Task.created_at.desc(),
                )
                .label("rn"),
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_(
                    [
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                        TaskStatus.QUEUED,
                        TaskStatus.BLOCKED,
                    ]
                ),
            )
            .cte("active_queued_ranked")
        )
        active_queued_stmt = (
            select(Task)
            .select_from(Task)
            .join(_active_queued_ranked, _active_queued_ranked.c.task_id == Task.id)
            .options(_active_queued_loadonly)
            .where(_active_queued_ranked.c.rn <= 50)
            .order_by(Task.created_at.desc())
        )
        active_queued_rows = (await session.execute(active_queued_stmt)).scalars().all()
        active_tasks = []
        queued_tasks = []
        for t in active_queued_rows:
            p = t.first_payload
            entry = {
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "prompt": p.get("prompt", p.get("description", "")),
                "related_files": (t.context or {}).get("related_files", []),
                "direction_id": (t.context or {}).get("direction_id"),
            }
            if t.status == TaskStatus.QUEUED:
                queued_tasks.append(entry)
            else:
                entry["status"] = t.status
                active_tasks.append(entry)
        active_tasks = active_tasks[:50]
        queued_tasks = queued_tasks[:50]

        # Recently completed tasks, high-iteration lessons, and direction-
        # scoped historical context all come from the same terminal-task
        # window. Rank terminal tasks by status first so a task with many
        # artifacts cannot burn through the row budget before its peers are
        # even visible, then join artifacts only for the selected task ids.
        _terminal_loadonly = load_only(
            Task.id,
            Task.title,
            Task.status,
            Task.payloads,
            Task.context,
            Task.project_id,
            Task.description,
            Task.created_at,
            Task.latest_status_message,
            Task.account_id,
            Task.provider,
            Task.model,
            Task.strategic_cycle_id,
            Task.fix_loop_history,
            raiseload=True,
        )
        _terminal_ranked = (
            select(
                Task.id.label("task_id"),
                Task.status.label("task_status"),
                func.row_number()
                .over(
                    partition_by=Task.status,
                    order_by=Task.created_at.desc(),
                )
                .label("rn"),
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
            )
            .cte("terminal_ranked")
        )
        terminal_stmt = (
            select(Task, Artifact)
            .select_from(Task)
            .join(_terminal_ranked, _terminal_ranked.c.task_id == Task.id)
            .options(_terminal_loadonly)
            .outerjoin(
                Artifact,
                and_(
                    Artifact.task_id == Task.id,
                    Artifact.artifact_type.in_(
                        [ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS]
                    ),
                ),
            )
            .where(
                or_(
                    and_(
                        _terminal_ranked.c.task_status == TaskStatus.COMPLETED,
                        _terminal_ranked.c.rn <= 10,
                    ),
                    and_(
                        _terminal_ranked.c.task_status.in_(
                            [TaskStatus.FAILED, TaskStatus.CANCELLED]
                        ),
                        _terminal_ranked.c.rn <= 15,
                    ),
                )
            )
            .order_by(Task.created_at.desc())
        )
        terminal_rows = (await session.execute(terminal_stmt)).all()
        terminal_tasks_by_id: dict = {}
        terminal_task_order: list = []
        terminal_artifacts_by_task: dict = {}
        for terminal_task, terminal_artifact in terminal_rows:
            if terminal_task.id not in terminal_tasks_by_id:
                terminal_tasks_by_id[terminal_task.id] = terminal_task
                terminal_task_order.append(terminal_task.id)
            if terminal_artifact is not None:
                terminal_artifacts_by_task.setdefault(terminal_task.id, []).append(
                    terminal_artifact
                )
        terminal_tasks_rows = [terminal_tasks_by_id[tid] for tid in terminal_task_order]
        completed_tasks_rows = [
            t for t in terminal_tasks_rows if t.status == TaskStatus.COMPLETED
        ][:10]
        failed_cancelled_rows = [
            t
            for t in terminal_tasks_rows
            if t.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]
        ][: max(0, 15 - len(completed_tasks_rows))]

        _history_loadonly = load_only(
            Task.id,
            Task.title,
            Task.status,
            Task.payloads,
            Task.context,
            Task.description,
            Task.created_at,
            Task.latest_status_message,
            Task.account_id,
            Task.provider,
            Task.model,
            Task.strategic_cycle_id,
            Task.fix_loop_history,
            raiseload=True,
        )
        _history_ranked = (
            select(
                Task.id.label("task_id"),
                func.row_number().over(order_by=Task.created_at.desc()).label("rn"),
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
            )
            .cte("terminal_history_ranked")
        )
        history_stmt = (
            select(Task, Artifact)
            .select_from(Task)
            .join(_history_ranked, _history_ranked.c.task_id == Task.id)
            .options(_history_loadonly)
            .outerjoin(
                Artifact,
                and_(
                    Artifact.task_id == Task.id,
                    Artifact.artifact_type.in_(
                        [ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS]
                    ),
                ),
            )
            .where(_history_ranked.c.rn <= 50)
            .order_by(Task.created_at.desc())
        )
        history_rows = (await session.execute(history_stmt)).all()
        history_tasks_by_id: dict = {}
        history_task_order: list = []
        history_artifacts_by_task: dict = {}
        for history_task, history_artifact in history_rows:
            if history_task.id not in history_tasks_by_id:
                history_tasks_by_id[history_task.id] = history_task
                history_task_order.append(history_task.id)
            if history_artifact is not None:
                history_artifacts_by_task.setdefault(history_task.id, []).append(
                    history_artifact
                )
        history_tasks_rows = [history_tasks_by_id[tid] for tid in history_task_order]
        # Detach history rows from the session so that a session.rollback() in
        # one SR-analytics error handler cannot expire the already-loaded
        # attributes and cascade MissingGreenlet lazy-load failures into sibling
        # analytics calls (C1 rollback-on-swallow discipline; incident
        # 2026-05-29, reference_load_only_lazy_load_trap).  All columns
        # required by the SR functions are covered by _history_loadonly, so
        # detached objects are fully readable.
        for _hr in history_tasks_rows:
            session.expunge(_hr)

        from app.failure_classification import classify_failure

        recently_completed_tasks = []
        for t in completed_tasks_rows:
            p = t.first_payload
            task_artifacts = terminal_artifacts_by_task.get(t.id, [])
            pr_url = t.context.get("rollup_pr_url") if t.context else None
            if not pr_url:
                for a in task_artifacts:
                    if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                        pr_url = a.pr_url
                        break
            research_findings_summary = None
            for a in task_artifacts:
                if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                    research_findings_summary = a.logs[:8000]
                    break
            t_ctx = t.context or {}
            t_prompt = (p.get("prompt") or p.get("description") or t.description or "")[
                :300
            ]
            t_related_files = (t_ctx.get("related_files") or [])[:10]
            t_direction_id = t_ctx.get("direction_id")
            is_failed = t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            t_changed_files = (t_ctx.get("_pr_changed_files") or [])[:10] or None
            recently_completed_tasks.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "status": t.status.value
                    if hasattr(t.status, "value")
                    else str(t.status),
                    "pr_url": pr_url,
                    "landed_on_head": is_merged_completion(t),
                    "research_findings_summary": research_findings_summary,
                    "failure_reason": (t.latest_status_message or "")[:500]
                    if is_failed and t.latest_status_message
                    else None,
                    "failure_class": classify_failure(t)
                    if is_failed and t.latest_status_message
                    else None,
                    "review_iterations": (t_ctx.get("review_loop", {}) or {}).get(
                        "iteration", 0
                    )
                    if t_ctx.get("review_loop")
                    else None,
                    "prompt": t_prompt or None,
                    "related_files": t_related_files,
                    "direction_id": t_direction_id,
                    "changed_files": t_changed_files,
                }
            )
        for t in failed_cancelled_rows:
            p = t.first_payload
            task_artifacts = terminal_artifacts_by_task.get(t.id, [])
            pr_url = t.context.get("rollup_pr_url") if t.context else None
            if not pr_url:
                for a in task_artifacts:
                    if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                        pr_url = a.pr_url
                        break
            research_findings_summary = None
            for a in task_artifacts:
                if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                    research_findings_summary = a.logs[:8000]
                    break
            t_ctx = t.context or {}
            t_prompt = (p.get("prompt") or p.get("description") or t.description or "")[
                :300
            ]
            t_related_files = (t_ctx.get("related_files") or [])[:10]
            t_direction_id = t_ctx.get("direction_id")
            t_changed_files = (t_ctx.get("_pr_changed_files") or [])[:10] or None
            recently_completed_tasks.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "status": t.status.value
                    if hasattr(t.status, "value")
                    else str(t.status),
                    "pr_url": pr_url,
                    "landed_on_head": False,
                    "research_findings_summary": research_findings_summary,
                    "failure_reason": (t.latest_status_message or "")[:500]
                    if t.latest_status_message
                    else None,
                    "failure_class": classify_failure(t)
                    if t.latest_status_message
                    else None,
                    "review_iterations": (t_ctx.get("review_loop", {}) or {}).get(
                        "iteration", 0
                    )
                    if t_ctx.get("review_loop")
                    else None,
                    "prompt": t_prompt or None,
                    "related_files": t_related_files,
                    "direction_id": t_direction_id,
                    "changed_files": t_changed_files,
                }
            )

        # Build lessons from high-iteration tasks (3+ review cycles).
        # Phase B4 (H-P6): drop status_log/plan_output. Loop reads context,
        # title, status, latest_status_message, plus classify_failure() inputs.
        # These same rows are passed as `all_tasks` to the SR-trend analytics
        # (compute_*_trend), which read `strategic_cycle_id` and
        # `fix_loop_history` — both MUST be eager-loaded here, or the trend
        # functions' `getattr(t, "strategic_cycle_id")` triggers a lazy-load
        # outside the async greenlet (MissingGreenlet), which aborts the
        # request transaction and 500s claim_task downstream (incident
        # 2026-05-29; see reference_load_only_lazy_load_trap).
        lessons = []
        high_iter_rows = history_tasks_rows
        for ht in high_iter_rows:
            review_loop = (ht.context or {}).get("review_loop", {})
            if not review_loop:
                continue
            iteration = review_loop.get("iteration", 0)
            if iteration < 2:
                continue
            review_hist = review_loop.get("review_history", [])
            if review_hist:
                last_feedback = review_hist[-1].get("feedback", "")[:300]
                first_entry = next(
                    (e for e in review_hist if e.get("iteration") == 0),
                    review_hist[0],
                )
                first_feedback = first_entry.get("feedback", "")[:300]
                lesson = {
                    "task_title": ht.title,
                    "iterations": len(review_hist) + 1,
                    "last_feedback": last_feedback,
                    "direction_id": (ht.context or {}).get("direction_id"),
                }
                if first_feedback and first_feedback != last_feedback:
                    lesson["first_feedback"] = first_feedback
                lessons.append(lesson)
            if len(lessons) >= 8:
                break

        # Second pass: collect CODE_ERROR failures with no review loop
        if len(lessons) < 8:
            for ht in high_iter_rows:
                if (ht.context or {}).get("review_loop"):
                    continue  # already handled above
                if ht.status == TaskStatus.FAILED and ht.latest_status_message:
                    fc = classify_failure(ht)
                    if fc == "CODE_ERROR":
                        lessons.append(
                            {
                                "task_title": ht.title,
                                "iterations": 0,
                                "last_feedback": (ht.latest_status_message or "")[:500],
                                "failure_class": "CODE_ERROR",
                                "direction_id": (ht.context or {}).get("direction_id"),
                            }
                        )
                if len(lessons) >= 8:
                    break

        # Direction-filtered recent tasks: up to 5 completed/failed from the same direction
        current_direction_id = (task.context or {}).get("direction_id")
        direction_recent_tasks = []
        if current_direction_id:
            dir_matches = [
                t
                for t in history_tasks_rows
                if (t.context or {}).get("direction_id") == current_direction_id
            ][:5]
            dir_match_ids = [t.id for t in dir_matches]
            dir_artifacts_by_task: dict = {}
            if dir_match_ids:
                for dir_match_id in dir_match_ids:
                    dir_artifacts_by_task[dir_match_id] = history_artifacts_by_task.get(
                        dir_match_id, []
                    )
            for t in dir_matches:
                p = t.first_payload
                t_ctx = t.context or {}
                t_arts = dir_artifacts_by_task.get(t.id, [])
                pr_url = t_ctx.get("rollup_pr_url")
                if not pr_url:
                    for a in t_arts:
                        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                            pr_url = a.pr_url
                            break
                research_findings_summary = None
                for a in t_arts:
                    if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                        research_findings_summary = a.logs[:8000]
                        break
                is_failed = t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                t_prompt = (
                    p.get("prompt") or p.get("description") or t.description or ""
                )[:300]
                direction_recent_tasks.append(
                    {
                        "id": str(t.id),
                        "title": t.title,
                        "task_type": p.get("type", ""),
                        "status": t.status.value
                        if hasattr(t.status, "value")
                        else str(t.status),
                        "pr_url": pr_url,
                        "research_findings_summary": research_findings_summary,
                        "failure_reason": (t.latest_status_message or "")[:500]
                        if is_failed and t.latest_status_message
                        else None,
                        "failure_class": classify_failure(t)
                        if is_failed and t.latest_status_message
                        else None,
                        "review_iterations": (t_ctx.get("review_loop", {}) or {}).get(
                            "iteration", 0
                        )
                        if t_ctx.get("review_loop")
                        else None,
                        "prompt": t_prompt or None,
                        "related_files": (t_ctx.get("related_files") or [])[:10],
                        "direction_id": t_ctx.get("direction_id"),
                        "scope": t_ctx.get("scope"),
                        "changed_files": (t_ctx.get("_pr_changed_files") or [])[:10]
                        or None,
                    }
                )

        # Open PRs: distinct pr_url from PULL_REQUEST artifacts for non-terminal tasks in project
        open_prs_stmt = (
            select(Artifact.pr_url)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == task.project_id,
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.isnot(None),
                Task.status.not_in(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
            )
            .distinct()
            .order_by(Artifact.pr_url)
            .limit(20)
        )
        open_prs_rows = (await session.execute(open_prs_stmt)).scalars().all()

        sibling_tasks: list[dict] = []
        if task.parent_task_id:
            _sibling_loadonly = load_only(
                Task.id,
                Task.title,
                Task.status,
                Task.payloads,
                Task.context,
                Task.latest_status_message,
                Task.description,
                Task.created_at,
                Task.parent_task_id,
                Task.project_id,
                Task.account_id,
                Task.provider,
                Task.model,
                raiseload=True,
            )
            _sibling_ranked = (
                select(
                    Task.id.label("task_id"),
                    Task.status.label("task_status"),
                    func.row_number()
                    .over(
                        partition_by=Task.status,
                        order_by=Task.created_at.desc(),
                    )
                    .label("rn"),
                )
                .where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.CANCELLED,
                        ]
                    ),
                )
                .cte("sibling_ranked")
            )
            sibling_stmt = (
                select(Task, Artifact)
                .select_from(Task)
                .join(_sibling_ranked, _sibling_ranked.c.task_id == Task.id)
                .options(_sibling_loadonly)
                .outerjoin(
                    Artifact,
                    and_(
                        Artifact.task_id == Task.id,
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    ),
                )
                .where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.CANCELLED,
                        ]
                    ),
                )
                .where(
                    or_(
                        _sibling_ranked.c.task_status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                            ]
                        ),
                        and_(
                            _sibling_ranked.c.task_status.in_(
                                [
                                    TaskStatus.COMPLETED,
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                ]
                            ),
                            _sibling_ranked.c.rn <= 5,
                        ),
                    )
                )
                .order_by(Task.created_at.desc())
            )
            sibling_rows = (await session.execute(sibling_stmt)).all()
            sibling_tasks_by_id: dict = {}
            sibling_task_order: list = []
            sibling_pr_urls: dict = {}
            for sib, sib_art in sibling_rows:
                if sib.id not in sibling_tasks_by_id:
                    sibling_tasks_by_id[sib.id] = sib
                    sibling_task_order.append(sib.id)
                if sib_art is not None and sib_art.pr_url:
                    sibling_pr_urls.setdefault(sib.id, sib_art.pr_url)
            sibling_rows = [sibling_tasks_by_id[sid] for sid in sibling_task_order]
            active_siblings = [
                sib
                for sib in sibling_rows
                if sib.status
                in [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
            ]
            sibling_tasks.extend(
                {
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib.status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": (sib.first_payload).get("type", ""),
                    "active": True,
                }
                for sib in active_siblings
            )
            terminal_siblings = [
                sib
                for sib in sibling_rows
                if sib.status
                in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ][:5]
            for sib in terminal_siblings:
                sib_status = (
                    sib.status.value
                    if hasattr(sib.status, "value")
                    else str(sib.status)
                )
                pr_url = (sib.context or {}).get(
                    "rollup_pr_url"
                ) or sibling_pr_urls.get(sib.id)
                entry = {
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib_status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": (sib.first_payload).get("type", ""),
                    "active": False,
                    "pr_url": pr_url,
                }
                if sib.status == TaskStatus.FAILED and sib.latest_status_message:
                    entry["failure_class"] = classify_failure(sib)
                    entry["error_excerpt"] = sib.latest_status_message[:150]
                sib_changed = ((sib.context or {}).get("_pr_changed_files") or [])[:5]
                if sib_changed:
                    entry["changed_files"] = sib_changed
                sibling_tasks.append(entry)

        # Pipeline health summary (for gated task types; else-branch covers the rest)
        pipeline_health = None
        try:
            pipeline_health = await compute_pipeline_health_summary(
                session, task.project_id
            )
        except Exception:
            logger.exception(
                "[AGENT] Failed to compute pipeline health summary (non-critical)"
            )

        # Aggregate review issue distribution for create/fix/refactor tasks (last 14 days)
        review_issue_distribution = None
        top_first_review_issues = None
        direction_top_first_review_issues = None
        scope_specific_review_issues = None
        current_task_scope = (task.context or {}).get("scope")
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.review_learning import _categorize_review_issues

                cutoff_14d = _utcnow() - timedelta(days=14)
                combined_review_history: list[dict] = []
                for ht in high_iter_rows:
                    # Task.created_at is TIMESTAMPTZ since 0061; cutoff_14d is naive.
                    ht_created = _to_naive_utc(ht.created_at)
                    if ht_created and ht_created < cutoff_14d:
                        continue
                    review_loop = (ht.context or {}).get("review_loop", {})
                    rev_hist = (
                        review_loop.get("review_history", []) if review_loop else []
                    )
                    if rev_hist:
                        combined_review_history.extend(rev_hist)
                if combined_review_history:
                    buckets = _categorize_review_issues(combined_review_history)
                    total_issues = sum(len(v) for v in buckets.values())
                    if total_issues >= 5:
                        sorted_cats = sorted(buckets.items(), key=lambda x: -len(x[1]))
                        top3 = sorted_cats[:3]
                        review_issue_distribution = {
                            cat: round(len(issues) / total_issues, 2)
                            for cat, issues in top3
                        }

                from app.review_learning import (
                    compute_recurring_first_review_descriptions,
                )

                try:
                    top_first_review_issues = (
                        compute_recurring_first_review_descriptions(
                            high_iter_rows,
                            cutoff_14d,
                        )
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute top_first_review_issues (non-critical)"
                    )

                # Direction-scoped recurring issues: per-task presence within the current direction.
                # Requires at least 3 direction-scoped tasks with review history to avoid noisy signals.
                try:
                    if current_direction_id:
                        direction_top_first_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                cutoff_14d,
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("direction_id")
                                    == current_direction_id
                                ),
                                min_tasks=3,
                                count_unique_tasks=True,
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute direction_top_first_review_issues (non-critical)"
                    )

                try:
                    if current_task_scope:
                        scope_specific_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                cutoff_14d,
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("scope")
                                    == current_task_scope
                                ),
                                min_tasks=3,
                                extra_fields={"scope": current_task_scope},
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute scope_specific_review_issues (non-critical)"
                    )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute review_issue_distribution (non-critical)"
                )

        # Compute review effectiveness trends (stores in project.design_doc as side effect)
        review_effectiveness = None
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.review_learning import compute_review_effectiveness

                review_effectiveness = await compute_review_effectiveness(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute review_effectiveness (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                await _refresh_sr_catch_rate(session, _claimed_project_id)
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_catch_rate_trends (non-critical)"
                )
                # C1: this analytics write runs on the post-claim session (the
                # claim was already committed at the top of this handler). A
                # raise can abort the session; without this rollback the next
                # analytics call AND the design_doc commit below silently fail
                # on a PendingRollback session. Mirrors agent_status.py's
                # rollback-on-swallow discipline.
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                from app.sr_analytics import compute_sr_false_attestation_trends

                await compute_sr_false_attestation_trends(
                    project_id=_claimed_project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_false_attestation_trends (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                from app.sr_analytics import (
                    compute_sr_regression_risk_test_signal_trends,
                )

                await compute_sr_regression_risk_test_signal_trends(
                    project_id=_claimed_project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_regression_risk_test_signal_trends (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                from app.sr_analytics import compute_fresh_reviewer_trend

                await compute_fresh_reviewer_trend(
                    project_id=_claimed_project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute fresh_reviewer_trend (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                from app.sr_analytics import compute_edges_trend

                await compute_edges_trend(
                    project_id=_claimed_project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception("[AGENT] Failed to compute edges_trend (non-critical)")
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                await _stamp_reviewer_recovery_count(
                    session, _claimed_project_id, high_iter_rows
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute reviewer_recovery_after_incomplete_sr_count (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            try:
                from app.sr_analytics import compute_sr_reviewer_disagreements

                await compute_sr_reviewer_disagreements(
                    project_id=_claimed_project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_reviewer_disagreements (non-critical)"
                )
                with contextlib.suppress(Exception):
                    await session.rollback()

        direction_concurrent_files: list[str] = []
        co_batch_sibling_count = 0
        if current_direction_id:
            _seen_files: set[str] = set()
            for _at in active_tasks:
                if _at.get("direction_id") != current_direction_id:
                    continue
                co_batch_sibling_count += 1
                for _f in _at.get("related_files") or []:
                    if isinstance(_f, str) and _f and _f not in _seen_files:
                        _seen_files.add(_f)
                        direction_concurrent_files.append(_f)

        response["project_context"] = {
            "active_tasks": active_tasks,
            "direction_concurrent_files": direction_concurrent_files,
            "co_batch_sibling_count": co_batch_sibling_count,
            "queued_tasks": queued_tasks,
            "recently_completed_tasks": recently_completed_tasks,
            "direction_recent_tasks": direction_recent_tasks
            if current_direction_id
            else [],
            "open_prs": list(open_prs_rows),
            "sibling_tasks": sibling_tasks,
            "lessons": lessons,
            "pipeline_health": pipeline_health,
            "review_issue_distribution": review_issue_distribution,
            "top_first_review_issues": top_first_review_issues,
            "scope_specific_review_issues": scope_specific_review_issues,
            "review_effectiveness": review_effectiveness,
        }

        # Record review measurement attribution in task.context for outcome tracking
        try:
            _had_measurements = bool(review_issue_distribution)
            _ctx = dict(_claimed_context)
            _ctx["_had_review_measurements"] = _had_measurements
            if _had_measurements:
                _ctx["_top_review_issue_categories"] = list(
                    review_issue_distribution.keys()
                )
            task.context = _ctx
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
        except Exception:
            logger.exception(
                "[AGENT] Failed to record review measurement attribution (non-critical)"
            )
            # C1: if a prior analytics call left the session aborted, this
            # commit raises PendingRollbackError. Roll back so the playbook /
            # direction-context code and the later commits run on a clean
            # session instead of cascading silent failures.
            with contextlib.suppress(Exception):
                await session.rollback()
    else:
        # Lightweight pipeline health for all other task types
        try:
            pipeline_health = await compute_pipeline_health_summary(
                session, task.project_id
            )
            if pipeline_health:
                response["project_context"] = {"pipeline_health": pipeline_health}
        except Exception:
            logger.exception(
                "[AGENT] Failed to compute lightweight pipeline health (non-critical)"
            )

    # Task types that benefit from untagged operational playbook entries.
    # Types not in this set only receive entries with explicit applicable_types matching them.
    _PLAYBOOK_DEFAULT_TYPES = {
        "create",
        "fix",
        "refactor",
        "review",
        "qa",
        "smoketest",
        "deps",
        "migrate",
        "debug",
        "docs",
    }

    # Inject playbook for ALL task types (outside the project_context gate).
    # First, recover the shared session once, centrally. The best-effort
    # enrichment above (SR-trend analytics, review effectiveness) runs on this
    # same session; a DB error in any of it aborts the transaction, and the
    # subsequent commit then does an internal rollback that EXPIRES ``task``,
    # after which any plain ``task.<attr>`` read below does a sync lazy-load
    # with no greenlet (MissingGreenlet) or raises PendingRollbackError on the
    # still-aborted txn — 500ing a claim that was already committed far above.
    # Roll back any residual transaction (the claim's own write is long since
    # committed, so nothing is lost) and re-attach the durable task + project by
    # PK so the guarded writes below (playbook ref-counts, _shown_playbook_keys)
    # operate on clean, non-expired instances. If recovery itself fails we
    # degrade gracefully: every UNGUARDED downstream read uses the ``_claimed_*``
    # snapshots rather than ``task.*``, so a still-broken session here can no
    # longer 500 the (committed) claim. (incident 2026-05-29: claim 500s at
    # agent.py:2243 reading task.context after an enrichment failure.)
    try:
        await session.rollback()
        task = await session.get(Task, _claimed_task_id) or task
        project_for_playbook = await session.get(Project, _claimed_project_id)
    except Exception:
        logger.exception(
            "[AGENT] claim_task post-enrichment session recovery failed — "
            "proceeding with degraded response (claim already committed)"
        )
        project_for_playbook = None
    if project_for_playbook and project_for_playbook.design_doc:
        task_type = (_claimed_first_payload or {}).get("type", "create")
        playbook_entries = {}
        _playbook_candidates = []
        _task_direction_id = str(_claimed_context.get("direction_id") or "")
        _task_related_files = {
            str(f)
            for f in (_claimed_context.get("related_files") or [])
            if isinstance(f, str) and f
        }
        _file_to_system_raw = (project_for_playbook.design_doc or {}).get(
            "_file_to_system"
        ) or {}
        _file_to_system = (
            _file_to_system_raw if isinstance(_file_to_system_raw, dict) else {}
        )
        _task_systems = {
            str(_file_to_system.get(f))
            for f in _task_related_files
            if isinstance(_file_to_system.get(f), str) and _file_to_system.get(f)
        }
        _my_task_id = str(_claimed_task_id)
        for k, v in (project_for_playbook.design_doc or {}).items():
            if k.startswith("playbook/") and isinstance(v, dict):
                # Anti-poisoning: skip entries that are pending writes from
                # OTHER non-completed tasks. The source task can still see
                # its own pending entries; peers see them only after the
                # source task COMPLETES (pending_source cleared) or skip
                # them entirely if the source FAILED (entry removed). See
                # main.py update_playbook for the rationale.
                pending_source = v.get("pending_source")
                if pending_source and pending_source != _my_task_id:
                    continue
                applicable_types = v.get("applicable_types")
                if applicable_types is not None:
                    if task_type not in applicable_types:
                        continue
                elif task_type not in _PLAYBOOK_DEFAULT_TYPES:
                    continue
                short_key = k[len("playbook/") :]
                # Skip auto-archived (stale) entries
                if v.get("stale"):
                    continue
                confirmed = v.get("confirmed_count", 0)
                referenced = v.get("reference_count", 0)
                # Demote entries with high reference count but zero confirmations
                ineffective = 1 if (referenced >= 10 and confirmed == 0) else 0
                if ineffective:
                    logger.info(
                        "Playbook entry '%s' demoted (reference_count=%d, confirmed_count=0)",
                        short_key,
                        referenced,
                    )
                # Age penalty: deprioritise entries older than 30 days with < 3 confirmations
                age_penalty = 0
                updated_at_str = v.get("updated_at", "")
                if updated_at_str and confirmed < 3:
                    try:
                        updated_dt = datetime.fromisoformat(
                            updated_at_str.replace("Z", "+00:00")
                        )
                        # _utcnow() returns naive UTC; strip tzinfo so comparison is always naive
                        if updated_dt.tzinfo is not None:
                            updated_dt = updated_dt.replace(tzinfo=None)
                        if (_utcnow() - updated_dt).days > 30:
                            age_penalty = 1
                    except (ValueError, TypeError):
                        pass
                # Stale file-reference penalty
                stale_penalty = 1 if v.get("stale_file_refs") else 0
                if age_penalty or stale_penalty:
                    logger.debug(
                        "Playbook entry '%s' penalty: age=%d stale=%d",
                        short_key,
                        age_penalty,
                        stale_penalty,
                    )
                # Effectiveness score: confirmed / referenced ratio (higher = better)
                effectiveness_score = confirmed / max(referenced, 1)
                entry_direction = str(
                    v.get("learned_from_direction") or v.get("direction_key") or ""
                )
                raw_direction_successes = v.get("direction_successes")
                direction_successes = (
                    raw_direction_successes
                    if isinstance(raw_direction_successes, dict)
                    else {}
                )
                direction_success_count = (
                    direction_successes.get(_task_direction_id, 0)
                    if _task_direction_id
                    else 0
                )
                direction_match = int(
                    bool(
                        _task_direction_id
                        and (
                            (entry_direction and entry_direction == _task_direction_id)
                            or (
                                isinstance(direction_success_count, (int, float))
                                and direction_success_count > 0
                            )
                        )
                    )
                )
                entry_systems = {
                    str(s)
                    for s in (
                        [v.get("learned_from_system")]
                        if v.get("learned_from_system")
                        else (v.get("systems") or [])
                    )
                    if isinstance(s, str) and s
                }
                system_match = int(
                    bool(
                        _task_systems
                        and entry_systems
                        and (_task_systems & entry_systems)
                    )
                )
                entry_related = {
                    str(f)
                    for f in (v.get("related_files") or [])
                    if isinstance(f, str) and f
                }
                file_overlap_count = len(_task_related_files & entry_related)
                relevance_reasons: list[str] = []
                if direction_match:
                    relevance_reasons.append("same direction")
                if system_match:
                    relevance_reasons.append("same system")
                if file_overlap_count > 0:
                    relevance_reasons.append(
                        f"{file_overlap_count} overlapping file(s)"
                    )
                # Provenance: the branch/HEAD this entry was authored against.
                # The diverged signal is NOT a raw branch-name comparison —
                # every task runs on its own short-lived feature branch, so a
                # name mismatch is the norm, not a divergence. Whether the
                # referenced work is actually absent from the reader's branch is
                # derived AFTER capping from the source task's landed/merge state
                # (see _landed_source_ids batch lookup below). Here we only
                # capture the raw provenance fields + source id the lookup needs.
                entry_branch = str(v.get("branch") or "")
                entry_head_sha = str(v.get("head_sha") or "")
                entry_source_id = str(v.get("source_task_id") or "")
                # Age in days for metadata (0 if updated_at unavailable)
                age_days = 0
                if updated_at_str:
                    try:
                        updated_dt2 = datetime.fromisoformat(
                            updated_at_str.replace("Z", "+00:00")
                        )
                        if updated_dt2.tzinfo is not None:
                            updated_dt2 = updated_dt2.replace(tzinfo=None)
                        age_days = (_utcnow() - updated_dt2).days
                    except (ValueError, TypeError):
                        pass
                _playbook_candidates.append(
                    (
                        -direction_match,
                        -system_match,
                        -file_overlap_count,
                        ineffective,
                        stale_penalty,
                        age_penalty,
                        -confirmed,
                        -effectiveness_score,
                        short_key,
                        v.get("content", ""),
                        effectiveness_score,
                        confirmed,
                        age_days,
                        bool(age_penalty or stale_penalty),
                        relevance_reasons,
                        entry_branch,
                        entry_head_sha,
                        entry_source_id,
                    )
                )
        _playbook_candidates.sort()
        # Cap shown entries: the sort already prioritizes direction match →
        # system match → file overlap → effectiveness, so the head of the
        # list is the most relevant subset. Without a cap, every fix task
        # was getting flooded with ~30 entries (most irrelevant), burning
        # prompt tokens and attention. Configurable via runtime_config so
        # we can tune per task type without a redeploy. Defaults: fix/qa/
        # review get 8 (focused work, narrow scope); create/refactor get 12
        # (broader investigation, more priors useful); other types get 10.
        _playbook_caps = (
            (_shared_state.runtime_config.get("playbook_max_entries") or {})
            if isinstance(
                _shared_state.runtime_config.get("playbook_max_entries"), dict
            )
            else {}
        )
        _default_caps = {"fix": 8, "qa": 8, "review": 8, "create": 12, "refactor": 12}
        _cap = int(_playbook_caps.get(task_type, _default_caps.get(task_type, 10)))
        _capped_candidates = _playbook_candidates[: max(_cap, 0)]
        # Apply a total-byte cap on top of the entry-count cap. Candidates are
        # already sorted highest-relevance-first, so we drop from the tail
        # (lowest-relevance) until the cumulative content bytes fit within the
        # limit. Index 9 in the tuple holds the content string.
        _PLAYBOOK_BYTE_CAP = 12_000
        _byte_total = sum(len(_c[9]) for _c in _capped_candidates)
        if _byte_total > _PLAYBOOK_BYTE_CAP:
            _trimmed: list = []
            _running = 0
            for _c in _capped_candidates:
                _entry_bytes = len(_c[9])
                if _running + _entry_bytes <= _PLAYBOOK_BYTE_CAP:
                    _trimmed.append(_c)
                    _running += _entry_bytes
            logger.warning(
                "Playbook byte-capped to %d/%d entries (%d→%d bytes) for task %s",
                len(_trimmed),
                len(_capped_candidates),
                _byte_total,
                _running,
                _claimed_task_id,
            )
            _capped_candidates = _trimmed
        # Derive landed-vs-not-landed for the SMALL capped set with a single
        # batched query (diverged is not a sort key, so doing this after the cap
        # is safe and keeps it off the per-entry hot loop). An entry is diverged
        # only when it carries branch provenance AND its source task did NOT land
        # on the integration branch — i.e. the referenced work is genuinely still
        # unmerged / completed-but-non-merge. Squash-merges rewrite commit
        # identity, so we key on the source task's merge state, never head_sha.
        _candidate_source_ids: set[str] = set()
        for _c in _capped_candidates:
            _branch_v, _src_v = _c[15], _c[17]
            if _branch_v and _src_v and _src_v != _my_task_id:
                _candidate_source_ids.add(_src_v)
        _landed_source_ids: set[str] = set()
        # When the lookup itself fails we cannot tell landed from unlanded, so
        # we suppress the diverged signal entirely (favour low false positives
        # over distrusting healthy entries on a transient DB hiccup).
        _landed_lookup_failed = False
        if _candidate_source_ids:
            from app.orchestrator_filters import _coerce_uuid

            # Malformed/unresolvable ids are skipped (None); the entry falls
            # through to NOT diverged (favouring low false positives).
            _parsed_ids = [
                _parsed
                for _sid in _candidate_source_ids
                if (_parsed := _coerce_uuid(_sid)) is not None
            ]
            if _parsed_ids:
                try:
                    _src_rows = (
                        await session.execute(
                            select(Task)
                            .where(Task.id.in_(_parsed_ids))
                            .options(
                                load_only(
                                    Task.id, Task.status, Task.context, raiseload=True
                                )
                            )
                        )
                    ).scalars()
                    for _src_task in _src_rows:
                        if is_merged_completion(_src_task):
                            _landed_source_ids.add(str(_src_task.id))
                except Exception:
                    # A degraded session must not 500 the already-committed
                    # claim. Mark the lookup failed so provenance-bearing
                    # entries fall through to NOT diverged (low false positives).
                    _landed_lookup_failed = True
                    logger.exception(
                        "[AGENT] playbook landed-state lookup failed — "
                        "treating provenance-bearing entries as non-diverged"
                    )
        for (
            _,
            _,
            _,
            _,
            _,
            _,
            _,
            _,
            short_key,
            content,
            eff_score,
            conf_count,
            age_days_val,
            is_stale,
            relevance_reasons,
            entry_branch_v,
            entry_head_sha_v,
            entry_source_id_v,
        ) in _capped_candidates:
            # Diverged ⇔ branch-bearing entry whose source task is neither the
            # reader's own task nor a task that landed on the integration
            # branch. A failed landed-state lookup suppresses the signal.
            prov_diverged = bool(
                entry_branch_v
                and entry_source_id_v
                and entry_source_id_v != _my_task_id
                and not _landed_lookup_failed
                and entry_source_id_v not in _landed_source_ids
            )
            playbook_entries[short_key] = {
                "content": content,
                "effectiveness_score": round(eff_score, 3),
                "confirmed_count": conf_count,
                "age_days": age_days_val,
                # Divergent-provenance entries self-mark as stale so the prompt
                # renderer surfaces the possibly-outdated caveat automatically.
                "stale": is_stale or prov_diverged,
                "relevance": {
                    "reasons": relevance_reasons,
                    "local_match": bool(relevance_reasons),
                },
                "provenance": {
                    "branch": entry_branch_v,
                    "head_sha": entry_head_sha_v,
                    "diverged": prov_diverged,
                },
            }
        if len(_playbook_candidates) > len(_capped_candidates):
            logger.info(
                "Playbook capped to %d/%d entries for task %s (type=%s)",
                len(_capped_candidates),
                len(_playbook_candidates),
                _claimed_task_id,
                task_type,
            )
        total_playbook_count = sum(
            1
            for k in (project_for_playbook.design_doc or {})
            if k.startswith("playbook/")
        )
        response["first_task_on_repo"] = total_playbook_count == 0
        # Deterministic empty/near-empty repo signal persisted at onboarding
        # (build_config.repo_is_empty). Distinct from first_task_on_repo (a
        # playbook-count proxy): this reflects actual source presence, so the
        # research prompt can gate bootstrap-from-goal guidance on real code.
        response["repo_is_empty"] = bool(
            (project_for_playbook.build_config or {}).get("repo_is_empty")
        )
        if playbook_entries:
            response["playbook"] = playbook_entries
            # Track reference counts + stale-ref check — fire-and-forget, must not fail claim response
            try:
                now_iso = _utcnow().isoformat()
                from app.task_locking import project_lock as _pb_project_lock

                async with _pb_project_lock(
                    session, _claimed_project_id, skip_locked=True
                ) as _pb_proj:
                    if _pb_proj is not None:
                        doc = dict(_pb_proj.design_doc or {})
                        for short_key in playbook_entries:
                            full_key = f"playbook/{short_key}"
                            if full_key in doc and isinstance(doc[full_key], dict):
                                entry = dict(doc[full_key])
                                entry["reference_count"] = (
                                    entry.get("reference_count", 0) + 1
                                )
                                entry["last_referenced_at"] = now_iso
                                outcomes = list(entry.get("recent_outcomes", []))
                                outcomes.append(False)
                                outcomes = outcomes[-10:]
                                entry["recent_outcomes"] = outcomes
                                # Auto-archive: if 10 entries and all False, mark stale
                                if len(outcomes) >= 10 and not any(outcomes):
                                    entry["stale"] = True
                                    entry["effectiveness_score"] = 0.0
                                doc[full_key] = entry
                        # Ground stale playbook file refs AND present-status
                        # design-doc systems/capabilities against the project's
                        # REAL integration branch (GitHub contents API) — the same
                        # mechanism the strategic/research reconcile uses — so the
                        # downgrade persists even on the backend, which has no local
                        # checkout. The old local-only path silently skipped on the
                        # backend (empty playbook_repo_root), giving a false
                        # impression that grounding occurred.
                        #
                        # The GitHub network I/O is kept OFF the per-claim hot path:
                        # branch_grounding_due() bounds it to at most once per
                        # cooldown window per project, and the whole pass is
                        # best-effort (never blocks the claim). When GitHub creds do
                        # not resolve the branch-aware helpers fall back to the local
                        # checkout (a no-op when none is configured).
                        try:
                            from app.playbook_learning import (
                                branch_grounding_due,
                                check_and_flag_stale_refs_branch_aware,
                                ground_design_doc_systems_branch_aware,
                                mark_branch_grounding_done,
                                recover_grounding_downgrades_branch_aware,
                            )
                            from app.source_ref_validation import (
                                resolve_branch_verification_context,
                                resolve_head_commit,
                            )

                            repo_root: str = (
                                _shared_state.runtime_config.get(
                                    "playbook_repo_root", ""
                                )
                                or ""
                            )
                            verification_kwargs = (
                                await resolve_branch_verification_context(
                                    session, _pb_proj
                                )
                            )
                            grounding_kwargs = dict(verification_kwargs)
                            if all(
                                key in verification_kwargs
                                for key in ("token", "owner", "repo", "branch")
                            ):
                                try:
                                    resolved_sha = await resolve_head_commit(
                                        **verification_kwargs
                                    )
                                except Exception:  # noqa: BLE001 — degrade, never block claim
                                    logger.debug(
                                        "[AGENT] Branch head SHA resolution skipped "
                                        "(non-critical)",
                                        exc_info=True,
                                    )
                                    resolved_sha = None
                                if resolved_sha:
                                    grounding_kwargs["sha"] = resolved_sha
                            # Recovery runs unconditionally — only scans entries
                            # already in the 'missing' state with a prior_status,
                            # so its GitHub API cost is much lower than the full
                            # downgrade sweep. False-missing markers should clear
                            # on the next claim, not wait for the 6h cooldown.
                            doc = await recover_grounding_downgrades_branch_aware(
                                doc, repo_root=repo_root, **verification_kwargs
                            )
                            if branch_grounding_due(doc):
                                doc = await check_and_flag_stale_refs_branch_aware(
                                    doc, repo_root=repo_root, **verification_kwargs
                                )
                                doc = await ground_design_doc_systems_branch_aware(
                                    doc, repo_root=repo_root, **grounding_kwargs
                                )
                                meta = doc.get("_meta")
                                grounding_diagnostic = None
                                if isinstance(meta, dict):
                                    grounding_diagnostic = meta.pop(
                                        "grounding_diagnostic", None
                                    )
                                    if not meta:
                                        doc.pop("_meta", None)
                                if (
                                    isinstance(grounding_diagnostic, dict)
                                    and grounding_diagnostic.get("kind") == "cap_breach"
                                ):
                                    logger.debug(
                                        "[AGENT] Branch-aware design-doc grounding "
                                        "cap breach: candidate_count=%s cap=%s "
                                        "source=%s affected_keys=%s",
                                        grounding_diagnostic.get("candidate_count"),
                                        grounding_diagnostic.get("cap"),
                                        grounding_diagnostic.get("source"),
                                        grounding_diagnostic.get("affected_keys"),
                                    )
                                else:
                                    mark_branch_grounding_done(doc)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Branch-aware design-doc grounding skipped "
                                "(non-critical)"
                            )
                        try:
                            from app.playbook_learning import retire_idle_auto_entries

                            idle_days = int(
                                _shared_state.runtime_config.get(
                                    "playbook_idle_auto_days", 30
                                )
                            )
                            retire_idle_auto_entries(doc, idle_days=idle_days)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Idle auto-entry retirement skipped (non-critical)"
                            )
                        try:
                            from app.playbook_learning import (
                                retire_ineffective_patterns,
                            )

                            retire_ineffective_patterns(doc)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Low-success-rate retirement skipped (non-critical)"
                            )
                        _pb_proj.design_doc = doc
                        flag_modified(_pb_proj, "design_doc")
                        session.add(_pb_proj)
                # Store shown playbook keys in task context for outcome tracking at completion
                task_ctx = dict(task.context or {})
                task_ctx["_shown_playbook_keys"] = list(playbook_entries.keys())
                task.context = task_ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
            except Exception:
                logger.exception(
                    "[AGENT] Failed to update playbook reference counts (non-critical)"
                )

    # Inject direction quality signal when total PRs >= 3 (reuse project already loaded above)
    direction_id = _claimed_context.get("direction_id")
    if direction_id and project_for_playbook:
        direction = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if direction:
            # Operator/strategist-authored implementation constraints for whoever
            # implements this direction (e.g. "Must not modify the public API
            # surface"). Stored bounded (<=500 chars) by the strategic/admin write
            # paths; re-cap defensively and only surface non-empty strings so
            # directions without constraints add nothing to the claim response.
            _impl_constraints_raw = (direction.get("context") or {}).get(
                "implementation_constraints"
            )
            direction_implementation_constraints = (
                _impl_constraints_raw.strip()[:500]
                if isinstance(_impl_constraints_raw, str)
                and _impl_constraints_raw.strip()
                else None
            )
            history = direction.get("research_history", [])
            agg = direction.get("history_aggregate", {})
            total_merged = sum(e.get("prs_merged", 0) for e in history) + (
                agg.get("total_prs_merged") or 0
            )
            total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (
                agg.get("total_prs_rejected") or 0
            )
            total_prs = total_merged + total_rejected
            if total_prs >= 3:
                acceptance_rate = total_merged / total_prs
                merge_time_values = [
                    h["avg_time_to_merge_hours"]
                    for h in history
                    if h.get("avg_time_to_merge_hours") is not None
                ]
                avg_ttm = (
                    round(sum(merge_time_values) / len(merge_time_values), 2)
                    if merge_time_values
                    else None
                )
                # Fetch last 3 tasks rejected by operator for this direction
                rejected_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == _claimed_project_id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.context["pr_rejected"].as_string() == "true",
                    )
                    .order_by(Task.created_at.desc())
                    .limit(3)
                )
                rejected_rows = (await session.execute(rejected_stmt)).scalars().all()
                rejection_context = [
                    {
                        "title": rt.title,
                        "reason": (
                            (rt.context or {}).get("pr_rejection_note")
                            or rt.latest_status_message
                            or ""
                        )[:200],
                    }
                    for rt in rejected_rows
                ]
                direction_quality = {
                    "direction_id": direction_id,
                    "acceptance_rate": round(acceptance_rate, 3),
                    "prs_merged": total_merged,
                    "prs_rejected": total_rejected,
                    "avg_time_to_merge_hours": avg_ttm,
                    "rejection_context": rejection_context,
                }
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["direction_quality"] = direction_quality

            # Inject direction context (title, goals, failed approaches) for implementing agents
            if task_type in ("create", "fix", "refactor"):
                failed_approaches: list = []
                rc_text = direction.get("research_context", "") or ""
                in_failed_section = False
                for rc_line in rc_text.splitlines():
                    rc_stripped = rc_line.strip()
                    if rc_stripped == "## Failed Approaches":
                        in_failed_section = True
                    elif rc_stripped.startswith("## "):
                        in_failed_section = False
                    elif in_failed_section and rc_stripped.startswith("- "):
                        failed_approaches.append(rc_stripped[2:])
                structured_failed_approaches = [
                    _parse_failed_approach(a) for a in failed_approaches[:5]
                ]
                recent_outcomes = [
                    {
                        "tasks_created": h.get("tasks_created", 0),
                        "prs_merged": h.get("prs_merged", 0),
                    }
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                failure_stage_pattern = _compute_failure_stage_pattern(history)

                # Aggregate scope_breakdown from the last 3 productive research cycles
                scope_success_rates: dict[str, dict[str, float | int]] = {}
                expansion_weighted: dict[str, dict[str, float]] = {}
                productive_cycles = [
                    h for h in history if h.get("tasks_created", 0) > 0
                ][-3:]
                for cycle in productive_cycles:
                    sb = cycle.get("scope_breakdown") or {}
                    if not isinstance(sb, dict):
                        continue
                    for scope_name, counts in sb.items():
                        if not isinstance(counts, dict):
                            continue
                        agg_entry = scope_success_rates.setdefault(
                            scope_name,
                            {
                                "created": 0,
                                "merged": 0,
                                "failed": 0,
                                "merged_cross_module": 0,
                            },
                        )
                        agg_entry["created"] += int(counts.get("created", 0) or 0)
                        merged_this = int(counts.get("merged", 0) or 0)
                        agg_entry["merged"] += merged_this
                        agg_entry["failed"] += int(counts.get("failed", 0) or 0)
                        agg_entry["merged_cross_module"] += int(
                            counts.get("merged_cross_module", 0) or 0
                        )
                        ratio = counts.get("avg_expansion_ratio_merged") or counts.get(
                            "avg_expansion_ratio"
                        )
                        if (
                            isinstance(ratio, (int, float))
                            and ratio > 0
                            and merged_this > 0
                        ):
                            ew = expansion_weighted.setdefault(
                                scope_name, {"sum": 0.0, "weight": 0.0}
                            )
                            ew["sum"] += float(ratio) * merged_this
                            ew["weight"] += merged_this
                scope_success_rates = {
                    k: v for k, v in scope_success_rates.items() if v["created"] >= 1
                }
                for scope_name, ew in expansion_weighted.items():
                    if ew["weight"] > 0 and scope_name in scope_success_rates:
                        scope_success_rates[scope_name]["avg_expansion_ratio"] = round(
                            ew["sum"] / ew["weight"], 2
                        )

                # Review-iteration trend: rolling median across last 3 cycles + last-cycle value
                from app.direction_metrics import (
                    _compute_rolling_median_review_iterations,
                )

                review_iteration_rolling_median = (
                    _compute_rolling_median_review_iterations(direction)
                )
                review_iteration_last_cycle: float | None = None
                if history:
                    last_val = history[-1].get("median_review_iterations")
                    if isinstance(last_val, (int, float)):
                        review_iteration_last_cycle = float(last_val)

                # Pull recent failed/cancelled sibling tasks with analysis artifacts
                prior_analysis_cutoff = _utcnow() - timedelta(days=14)
                prior_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == _claimed_project_id,
                        Task.id != _claimed_task_id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                        Task.created_at >= prior_analysis_cutoff,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(5)
                )
                prior_rows = (await session.execute(prior_stmt)).scalars().all()
                direction_prior_analysis: list = []
                total_chars = 0
                for pt in prior_rows:
                    pctx = pt.context or {}
                    analysis_plan = pctx.get("_analysis_plan")
                    test_failure_summary = pctx.get("_test_failure_summary")
                    status_message = (pt.latest_status_message or "").strip()
                    self_review_outcome = pctx.get("_self_review_outcome")
                    if (
                        not analysis_plan
                        and not test_failure_summary
                        and not status_message
                    ):
                        continue
                    entry = {
                        "title": pt.title,
                        "failure_category": classify_failure(pt),
                    }
                    scope = pctx.get("scope")
                    if isinstance(scope, str) and scope:
                        entry["scope"] = scope
                    if analysis_plan:
                        entry["analysis_plan"] = str(analysis_plan)[:500]
                    if test_failure_summary:
                        entry["test_failure_summary"] = str(test_failure_summary)[:300]
                    if status_message:
                        entry["latest_status_message"] = status_message[:300]
                    if isinstance(self_review_outcome, dict) and self_review_outcome:
                        sro_compact: dict = {}
                        verdict = self_review_outcome.get("verdict")
                        if verdict:
                            sro_compact["verdict"] = str(verdict)[:50]
                        notes = self_review_outcome.get("notes")
                        if notes:
                            sro_compact["notes"] = str(notes)[:200]
                        prompt_version = self_review_outcome.get("prompt_version")
                        if prompt_version is not None:
                            sro_compact["prompt_version"] = prompt_version
                        check_results = self_review_outcome.get("check_results")
                        if isinstance(check_results, dict):
                            failed_checks = [
                                str(name)
                                for name, result in check_results.items()
                                if isinstance(result, dict)
                                and result.get("passed") is False
                            ]
                            if failed_checks:
                                sro_compact["failed_checks"] = failed_checks[:10]
                        if sro_compact:
                            entry["self_review_outcome"] = sro_compact
                    changed_files = pctx.get("_pr_changed_files")
                    if isinstance(changed_files, list) and changed_files:
                        entry["pr_changed_files"] = [str(f) for f in changed_files[:10]]
                    diff_summary = pctx.get("_pr_diff_summary")
                    if isinstance(diff_summary, list) and diff_summary:
                        compact_diff = []
                        for d in diff_summary[:10]:
                            if not isinstance(d, dict):
                                continue
                            item = {"file": str(d.get("file", ""))}
                            added = d.get("additions", d.get("added"))
                            removed = d.get("deletions", d.get("removed"))
                            if isinstance(added, int):
                                item["added"] = added
                            if isinstance(removed, int):
                                item["removed"] = removed
                            change_type = d.get("change_type")
                            if change_type:
                                item["change_type"] = str(change_type)
                            if item["file"]:
                                compact_diff.append(item)
                        if compact_diff:
                            entry["pr_diff_summary"] = compact_diff
                    # Audit W1: previous_error_fingerprint is now a column on the Task.
                    fingerprint = pt.previous_error_fingerprint
                    if fingerprint:
                        entry["previous_error_fingerprint"] = str(fingerprint)
                    review_loop = pctx.get("review_loop")
                    if isinstance(review_loop, dict):
                        iteration = review_loop.get("iteration", 0)
                        if isinstance(iteration, int):
                            entry["review_iterations"] = iteration
                        review_history = review_loop.get("review_history")
                        if isinstance(review_history, list) and review_history:
                            last_entry = review_history[-1]
                            if isinstance(last_entry, dict):
                                issues = last_entry.get("issues")
                                compact_issues: list = []
                                if isinstance(issues, list):
                                    for sev_filter in ("critical", "major"):
                                        for issue in issues:
                                            if not isinstance(issue, dict):
                                                continue
                                            sev = str(issue.get("severity", "")).lower()
                                            if sev != sev_filter:
                                                continue
                                            desc = str(
                                                issue.get("description", "")
                                            ).strip()
                                            if not desc:
                                                continue
                                            item = {
                                                "severity": sev.upper(),
                                                "description": desc[:150],
                                            }
                                            file_path = issue.get("file")
                                            if file_path:
                                                item["file"] = str(file_path)
                                                line = issue.get("line")
                                                if isinstance(line, int):
                                                    item["line"] = line
                                            compact_issues.append(item)
                                            if len(compact_issues) >= 3:
                                                break
                                        if len(compact_issues) >= 3:
                                            break
                                if compact_issues:
                                    entry["review_feedback"] = compact_issues
                                else:
                                    feedback_text = str(
                                        last_entry.get("feedback") or ""
                                    ).strip()
                                    if feedback_text:
                                        entry["review_feedback"] = feedback_text[:200]
                    entry_len = len(json.dumps(entry))
                    if total_chars + entry_len > 2000:
                        break
                    total_chars += entry_len
                    direction_prior_analysis.append(entry)

                # Pull recent successful sibling tasks with analysis artifacts
                success_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == _claimed_project_id,
                        Task.id != _claimed_task_id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.status == TaskStatus.COMPLETED,
                        Task.created_at >= prior_analysis_cutoff,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(5)
                )
                success_rows = (await session.execute(success_stmt)).scalars().all()
                direction_successful_analysis: list = []
                success_chars = 0
                for st in success_rows:
                    sctx = st.context or {}
                    analysis_plan = sctx.get("_analysis_plan")
                    changed_files = sctx.get("_pr_changed_files")
                    has_changed_files = (
                        isinstance(changed_files, list) and changed_files
                    )
                    if not analysis_plan and not has_changed_files:
                        continue
                    s_entry = {
                        "title": st.title,
                    }
                    if analysis_plan:
                        s_entry["analysis_plan"] = str(analysis_plan)[:500]
                    scope = sctx.get("scope")
                    if scope:
                        s_entry["scope"] = str(scope)
                    if has_changed_files:
                        s_entry["pr_changed_files"] = [
                            str(f) for f in changed_files[:10]
                        ]
                    review_loop = sctx.get("review_loop")
                    if isinstance(review_loop, dict):
                        iteration = review_loop.get("iteration", 0)
                        if isinstance(iteration, int):
                            s_entry["review_iterations"] = iteration
                    s_entry_len = len(json.dumps(s_entry))
                    if success_chars + s_entry_len > 1500:
                        break
                    success_chars += s_entry_len
                    direction_successful_analysis.append(s_entry)

                direction_first_attempt_feedback: Dict[str, int] | None = None
                dfa_entry: dict = {}
                try:
                    dfa_all = (project_for_playbook.design_doc or {}).get(
                        "direction_first_attempt_feedback"
                    ) or {}
                    dfa_entry = dfa_all.get(str(direction_id)) or {}
                    dfa_cats = {
                        k: v
                        for k, v in dfa_entry.items()
                        if k not in ("total", "last_updated")
                        and isinstance(v, int)
                        and v > 0
                    }
                    if dfa_cats:
                        direction_first_attempt_feedback = dict(
                            sorted(dfa_cats.items(), key=lambda kv: -kv[1])
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to read direction_first_attempt_feedback (non-critical)"
                    )

                response["direction_context"] = {
                    "title": direction.get("title", ""),
                    "priority": direction.get("priority", ""),
                    "failed_approaches": structured_failed_approaches,
                    "recent_outcomes": recent_outcomes,
                    "failure_stage_pattern": failure_stage_pattern,
                    "direction_prior_analysis": direction_prior_analysis,
                    "direction_successful_analysis": direction_successful_analysis,
                    "scope_success_rates": scope_success_rates,
                    "top_first_review_issues": direction_top_first_review_issues,
                    "first_attempt_feedback": direction_first_attempt_feedback,
                }
                if co_batch_sibling_count >= 3:
                    response["direction_context"]["co_batch_sibling_count"] = (
                        co_batch_sibling_count
                    )
                if direction_implementation_constraints:
                    response["direction_context"]["implementation_constraints"] = (
                        direction_implementation_constraints
                    )
                if review_iteration_rolling_median is not None:
                    response["direction_context"]["review_iteration_rolling_median"] = (
                        review_iteration_rolling_median
                    )
                if review_iteration_last_cycle is not None:
                    response["direction_context"]["review_iteration_last_cycle"] = (
                        review_iteration_last_cycle
                    )
                from app.direction_outcomes import summarize_review_quality_feedback

                review_quality_feedback = summarize_review_quality_feedback(
                    history, dfa_entry
                )
                if review_quality_feedback:
                    response["direction_context"]["review_quality_feedback"] = (
                        review_quality_feedback
                    )

                # recent_findings_evidence: structured claims+sources for the most
                # recent merged/proposed findings under this direction. Populated here
                # so the context-prefix renderer (task.direction_context.recent_findings_evidence)
                # receives it via the claim response rather than via the bulkier
                # research-payload path (autonomous_research.py) where no renderer
                # consumed it.
                _rfe = direction.get("recent_findings_evidence")
                if _rfe and isinstance(_rfe, list):
                    response["direction_context"]["recent_findings_evidence"] = _rfe

            # Surface saturation / deadlock signals to research agents so they can
            # pivot to direction_update/direction_note findings on stuck directions.
            if task_type == "research":
                from app.research_completion import _detect_dedup_deadlock
                from app.research_direction_state import _detect_already_fixed_deadlock

                deadlocked, _ = _detect_dedup_deadlock(history)
                af_deadlocked, _ = _detect_already_fixed_deadlock(history)
                research_recent_outcomes = [
                    {
                        "tasks_created": h.get("tasks_created", 0),
                        "prs_merged": h.get("prs_merged", 0),
                    }
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                research_failure_stage_pattern = _compute_failure_stage_pattern(history)
                research_scope_rates: dict[str, dict[str, int]] = {}
                for cycle in [h for h in history if h.get("tasks_created", 0) > 0][-3:]:
                    sb = cycle.get("scope_breakdown") or {}
                    if not isinstance(sb, dict):
                        continue
                    for scope_name, counts in sb.items():
                        if not isinstance(counts, dict):
                            continue
                        agg = research_scope_rates.setdefault(
                            scope_name,
                            {
                                "created": 0,
                                "merged": 0,
                                "failed": 0,
                                "merged_cross_module": 0,
                            },
                        )
                        agg["created"] += int(counts.get("created", 0) or 0)
                        agg["merged"] += int(counts.get("merged", 0) or 0)
                        agg["failed"] += int(counts.get("failed", 0) or 0)
                        agg["merged_cross_module"] += int(
                            counts.get("merged_cross_module", 0) or 0
                        )
                research_scope_rates = {
                    k: v for k, v in research_scope_rates.items() if v["created"] >= 1
                }
                research_top_review_issues = None
                try:
                    from app.review_learning import (
                        compute_recurring_first_review_descriptions,
                    )

                    if current_direction_id:
                        research_top_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                _utcnow() - timedelta(days=14),
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("direction_id")
                                    == current_direction_id
                                ),
                                min_tasks=3,
                                count_unique_tasks=True,
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research top_first_review_issues (non-critical)"
                    )
                research_review_effectiveness = None
                try:
                    from app.review_learning import compute_review_effectiveness

                    research_review_effectiveness = await compute_review_effectiveness(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research review_effectiveness (non-critical)"
                    )
                try:
                    await _refresh_sr_catch_rate(session, task.project_id)
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_catch_rate_trends (non-critical)"
                    )
                try:
                    from app.playbook_learning import (
                        compute_sr_false_attestation_trends,
                    )

                    await compute_sr_false_attestation_trends(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_false_attestation_trends (non-critical)"
                    )
                try:
                    from app.playbook_learning import (
                        compute_sr_regression_risk_test_signal_trends,
                    )

                    await compute_sr_regression_risk_test_signal_trends(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_regression_risk_test_signal_trends (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_fresh_reviewer_trend

                    await compute_fresh_reviewer_trend(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research fresh_reviewer_trend (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_edges_trend

                    await compute_edges_trend(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research edges_trend (non-critical)"
                    )
                try:
                    await _stamp_reviewer_recovery_count(
                        session, task.project_id, high_iter_rows
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research reviewer_recovery_after_incomplete_sr_count (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_sr_reviewer_disagreements

                    await compute_sr_reviewer_disagreements(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_reviewer_disagreements (non-critical)"
                    )
                success_summary: list[str] = []
                for scope_name, counts in research_scope_rates.items():
                    created = counts["created"]
                    merged = counts["merged"]
                    if created >= 2:
                        success_summary.append(
                            f"{scope_name}: {merged}/{created} merged ({round(merged / created * 100)}%)"
                        )
                from app.direction_outcomes import (
                    summarize_review_quality_feedback,
                    synthesize_prompt_quality_patterns,
                )

                prompt_quality_patterns = synthesize_prompt_quality_patterns(history)
                response["direction_context"] = {
                    "possibly_saturated": bool(
                        direction.get("possibly_saturated", False)
                    ),
                    "deadlock_detected": bool(deadlocked),
                    "already_fixed_deadlock_detected": bool(af_deadlocked),
                    "recent_outcomes": research_recent_outcomes,
                    "failure_stage_pattern": research_failure_stage_pattern,
                    "scope_success_rates": research_scope_rates,
                    "top_first_review_issues": research_top_review_issues,
                    "success_patterns": success_summary,
                    "review_effectiveness": research_review_effectiveness,
                }
                if co_batch_sibling_count >= 3:
                    response["direction_context"]["co_batch_sibling_count"] = (
                        co_batch_sibling_count
                    )
                if prompt_quality_patterns:
                    response["direction_context"]["prompt_quality_patterns"] = (
                        prompt_quality_patterns
                    )
                research_dfa_entry: dict = {}
                try:
                    if project_for_playbook and current_direction_id:
                        research_dfa_entry = (
                            (project_for_playbook.design_doc or {}).get(
                                "direction_first_attempt_feedback"
                            )
                            or {}
                        ).get(str(current_direction_id)) or {}
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to read direction_first_attempt_feedback for research (non-critical)"
                    )
                research_review_quality_feedback = summarize_review_quality_feedback(
                    history, research_dfa_entry
                )
                if research_review_quality_feedback:
                    response["direction_context"]["review_quality_feedback"] = (
                        research_review_quality_feedback
                    )

                from app.direction_outcomes import (
                    summarize_dedup_filter,
                    summarize_failure_hotspots,
                )

                failure_hotspots = summarize_failure_hotspots(
                    direction, _claimed_project_id
                )
                if failure_hotspots:
                    response["direction_context"]["failure_hotspots"] = failure_hotspots
                dedup_summary = summarize_dedup_filter(history)
                if dedup_summary:
                    response["direction_context"]["dedup_filter_summary"] = (
                        dedup_summary
                    )

    # Inject co-change affinity context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        related_files = _claimed_context.get("related_files", [])
        if related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get(
                "cochange_index"
            )
            if cochange_index:
                cochange_scores: dict[str, float] = {}
                related_set = set(related_files)
                for rf in related_files:
                    for co_file, score in (cochange_index.get(rf) or {}).items():
                        if co_file not in related_set:
                            cochange_scores[co_file] = (
                                cochange_scores.get(co_file, 0.0) + score
                            )
                if cochange_scores:
                    top_cochange = sorted(
                        cochange_scores.items(), key=lambda x: x[1], reverse=True
                    )[:10]
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["cochange_files"] = [
                        f for f, _ in top_cochange
                    ]

    # Inject within-set co-change affinity hints for plan tasks so the planner
    # can group high-affinity files into the same subtask and split low-affinity
    # files into independent ones. Only pairs where both files are in the
    # finding's related_files and score >= 1.0 are surfaced.
    if task_type == "plan" and project_for_playbook:
        plan_related_files = _claimed_context.get("related_files", [])
        if plan_related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get(
                "cochange_index"
            ) or {}
            cochange_hints: dict[str, dict[str, float]] = {}
            related_set = set(plan_related_files)
            for rf in plan_related_files:
                for co_file, score in (cochange_index.get(rf) or {}).items():
                    if (
                        co_file in related_set
                        and isinstance(score, (int, float))
                        and score >= 1.0
                    ):
                        cochange_hints.setdefault(rf, {})[co_file] = round(
                            float(score), 3
                        )
            if cochange_hints:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["cochange_hints"] = cochange_hints

    if task_type == "plan" and direction_id and project_for_playbook:
        direction_for_decomp = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if direction_for_decomp:
            # Surface operator/strategist implementation constraints to the planner
            # too, so decomposition respects them (e.g. "Must not modify the public
            # API surface"). Same bounded, omit-when-empty contract as the
            # create/fix/refactor branch above.
            _plan_impl_constraints = (direction_for_decomp.get("context") or {}).get(
                "implementation_constraints"
            )
            if (
                isinstance(_plan_impl_constraints, str)
                and _plan_impl_constraints.strip()
            ):
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["implementation_constraints"] = (
                    _plan_impl_constraints.strip()[:500]
                )
            from app.playbook_learning import summarize_decomposition_outcomes

            decomposition_outcomes = summarize_decomposition_outcomes(
                direction_for_decomp
            )
            if decomposition_outcomes:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["decomposition_outcomes"] = (
                    decomposition_outcomes
                )

    # Inject per-module review feedback warnings for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        module_feedback = (project_for_playbook.design_doc or {}).get("module_feedback")
        if module_feedback:
            task_related_files = _claimed_context.get("related_files", [])
            module_warnings = []
            for file_path in task_related_files:
                profile = module_feedback.get(file_path)
                if not profile or not isinstance(profile, dict):
                    continue
                total = profile.get("total", 0)
                if total < 3:
                    continue
                cat_counts = {
                    k: v
                    for k, v in profile.items()
                    if k not in ("total", "last_updated") and isinstance(v, int)
                }
                sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
                top_cats = {
                    cat: round(count / total, 2)
                    for cat, count in sorted_cats[:3]
                    if count / total >= 0.15
                }
                if top_cats:
                    module_warnings.append(
                        {
                            "file": file_path,
                            "top_categories": top_cats,
                            "sample_size": total,
                        }
                    )
            if module_warnings:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["module_feedback_warnings"] = (
                    module_warnings
                )

    # Inject SR catch-rate blind spots for every code-review-gated task type
    # (create/fix/refactor/migrate/qa/docs/deps) — all ship reviewed code.
    if task_type in CODE_REVIEW_GATED_TASK_TYPES and project_for_playbook:
        try:
            sr_trends = (project_for_playbook.design_doc or {}).get(
                "sr_catch_rate_trends"
            )
            if sr_trends and isinstance(sr_trends.get("categories"), dict):

                def _cat_entry(data: dict) -> dict:
                    entry: dict = {"miss_rate": data["miss_rate"]}
                    if isinstance(data.get("weighted_miss_rate"), (int, float)):
                        entry["weighted_miss_rate"] = data["weighted_miss_rate"]
                    if isinstance(data.get("examples"), list) and data["examples"]:
                        entry["examples"] = data["examples"]
                    fp_rate = data.get("false_positive_rate")
                    if isinstance(fp_rate, (int, float)):
                        entry["false_positive_rate"] = fp_rate
                        entry["precision"] = round(1 - float(fp_rate), 3)
                    fp_count = data.get("fp_count")
                    tp_count = data.get("tp_count")
                    if isinstance(fp_count, int) and isinstance(tp_count, int):
                        entry["sr_flag_sample_size"] = fp_count + tp_count
                    sev_cal = data.get("severity_calibration")
                    if isinstance(sev_cal, dict):
                        entry["severity_calibration"] = sev_cal
                    quadrant = data.get("precision_recall_quadrant")
                    if isinstance(quadrant, str) and quadrant:
                        entry["precision_recall_quadrant"] = quadrant
                    p_delta = data.get("precision_delta")
                    if isinstance(p_delta, (int, float)):
                        entry["precision_delta"] = p_delta
                    r_delta = data.get("recall_delta")
                    if isinstance(r_delta, (int, float)):
                        entry["recall_delta"] = r_delta
                    # Regression signals consumed by the agent-side
                    # failure-mode hint (self-review.ts:formatFailureModeHintForSrPrompt):
                    # it renders only when below_floor/trend marks the category
                    # regressing AND miss_mode_breakdown carries a dominant_mode.
                    trend = data.get("trend")
                    if isinstance(trend, str) and trend:
                        entry["trend"] = trend
                    below_floor = data.get("below_floor")
                    if isinstance(below_floor, bool):
                        entry["below_floor"] = below_floor
                    engagement_thinning = data.get("engagement_thinning")
                    if isinstance(engagement_thinning, bool):
                        entry["engagement_thinning"] = engagement_thinning
                    miss_mode_breakdown = data.get("miss_mode_breakdown")
                    if isinstance(miss_mode_breakdown, dict):
                        entry["miss_mode_breakdown"] = miss_mode_breakdown
                    return entry

                cats = {
                    cat: _cat_entry(data)
                    for cat, data in sr_trends["categories"].items()
                    if isinstance(data, dict)
                    and isinstance(data.get("miss_rate"), (int, float))
                }
                if cats:
                    catch_rate_payload: dict = {"categories": cats}
                    by_task_type = sr_trends.get("by_task_type") or {}
                    if isinstance(by_task_type, dict) and task_type in by_task_type:
                        type_cats = by_task_type[task_type]
                        if isinstance(type_cats, dict):
                            catch_rate_payload["for_task_type"] = {
                                cat: _cat_entry(cdata)
                                for cat, cdata in type_cats.items()
                                if isinstance(cdata, dict)
                                and isinstance(cdata.get("miss_rate"), (int, float))
                            }
                    response["project_context"]["self_review_catch_rate"] = (
                        catch_rate_payload
                    )

                by_direction = sr_trends.get("by_direction") or {}
                task_direction_id = _claimed_context.get("direction_id")
                if (
                    task_direction_id
                    and isinstance(by_direction, dict)
                    and isinstance(by_direction.get(task_direction_id), dict)
                ):
                    dir_entry = by_direction[task_direction_id]
                    dir_categories_raw = dir_entry.get("categories")
                    if isinstance(dir_categories_raw, dict):
                        dir_cats = {
                            cat: _cat_entry(cdata)
                            for cat, cdata in dir_categories_raw.items()
                            if isinstance(cdata, dict)
                            and isinstance(cdata.get("miss_rate"), (int, float))
                        }
                        if dir_cats:
                            # Prefer direction_text already stored in the snapshot;
                            # fall back to scanning project.directions for older entries.
                            direction_text = dir_entry.get("direction_text") or ""
                            if not direction_text:
                                for d in project_for_playbook.directions or []:
                                    if d.get("id") == task_direction_id:
                                        direction_text = (
                                            d.get("text")
                                            or d.get("description")
                                            or d.get("title")
                                            or ""
                                        )
                                        break
                            direction_payload: dict = {
                                "direction_id": task_direction_id,
                                "direction_text": direction_text,
                                "categories": dir_cats,
                            }
                            if isinstance(dir_entry.get("sample_count"), int):
                                direction_payload["sample_count"] = dir_entry[
                                    "sample_count"
                                ]
                            if isinstance(dir_entry.get("miss_rate"), (int, float)):
                                direction_payload["miss_rate"] = dir_entry["miss_rate"]
                            if isinstance(
                                dir_entry.get("overall_catch_rate"), (int, float)
                            ):
                                direction_payload["overall_catch_rate"] = dir_entry[
                                    "overall_catch_rate"
                                ]
                            if isinstance(dir_entry.get("trend"), str):
                                direction_payload["trend"] = dir_entry["trend"]
                            if isinstance(
                                dir_entry.get("previous_miss_rate"), (int, float)
                            ):
                                direction_payload["previous_miss_rate"] = dir_entry[
                                    "previous_miss_rate"
                                ]
                            if isinstance(dir_entry.get("rate_history"), list):
                                direction_payload["rate_history"] = dir_entry[
                                    "rate_history"
                                ]
                            response["project_context"][
                                "self_review_catch_rate_for_direction"
                            ] = direction_payload
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject self_review_catch_rate (non-critical)"
            )

    # Inject project-wide SR blind-spot themes (last 14 days, top 8). Surfaces
    # recurring areas where self-review has been most uncertain so the agent
    # can apply extra scrutiny in those areas. Shape:
    #   project_context.sr_blind_spot_themes = [{"theme": str, "count": int}, ...]
    # No design_doc cache exists for this aggregate today — we scan tasks
    # directly. Mirroring the sr_catch_rate_trends precompute path is a
    # future optimization (see app/sr_blind_spots.py).
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            themes = await aggregate_sr_blind_spot_themes(
                session,
                project_for_playbook.id,
                days=14,
                limit=8,
            )
            if themes:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["sr_blind_spot_themes"] = themes
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_blind_spot_themes (non-critical)"
            )

    # Inject SR co-failure pairs (top 5 pairs, lift >= 1.5). When categories A and B
    # miss together at a significantly higher rate than by chance, fixing A requires
    # re-checking B. Shape: project_context.sr_co_failure_pairs =
    #   [{"cat_a": str, "cat_b": str, "lift": float}, ...]
    # Scans last 14 days of tasks; returns None when sample < min_sample threshold.
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            high_iter_tasks = await session.scalars(
                select(Task)
                .where(
                    Task.project_id == project_for_playbook.id,
                    Task.status == TaskStatus.COMPLETED,
                    # Task has no `completed_at` column — that name is a property
                    # on the strategic-cycle model, not Task. For a COMPLETED
                    # task, created_at bounds the recent-window scan (the standard
                    # bounded recent-task SR scan pattern).
                    Task.created_at > _utcnow() - timedelta(days=14),
                )
                .options(
                    load_only(
                        Task.id,
                        Task.context,
                        Task.payloads,
                        Task.created_at,
                        raiseload=True,
                    )
                )
                .order_by(Task.created_at.desc())
                .limit(2000)
            )
            pairs = aggregate_sr_co_failure_pairs(high_iter_tasks)
            if pairs:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["sr_co_failure_pairs"] = [
                    {"cat_a": p["cat_a"], "cat_b": p["cat_b"], "lift": p["lift"]}
                    for p in pairs
                ]
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_co_failure_pairs (non-critical)"
            )

    # Inject persistent SR blind-spot hint for categories exceeding disagreement threshold
    # across >=2 consecutive 7-day windows. Shape: project_context.persistent_blind_spots
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            from app.sr_blind_spots import (
                get_sr_persistent_blind_spot_hint,
            )

            _pbs = await get_sr_persistent_blind_spot_hint(
                session, project_for_playbook.id
            )
            if _pbs:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["persistent_blind_spots"] = _pbs
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject persistent_blind_spots (non-critical)"
            )

    # Inject concrete recent miss examples for the worst-performing SR categories.
    # Unlike sr_blind_spot_themes (aggregated keyword patterns), these are verbatim
    # reviewer findings that SR missed in the most recent window — giving the agent
    # specific "this exact thing slipped past last time" examples to act on.
    # Shape: project_context.sr_recent_miss_examples = {cat: [{severity, desc}, ...]}
    # Bounded: top 3 categories by weighted_miss_rate, ≤3 examples each, ≤1200 chars total.
    # When the task has a direction_id, prefer per-direction examples; fall back to global
    # when the direction has no qualifying data in by_direction.
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            _miss_trends = (project_for_playbook.design_doc or {}).get(
                "sr_catch_rate_trends"
            )
            if _miss_trends and isinstance(_miss_trends.get("categories"), dict):
                # Resolve candidate categories: prefer direction-scoped when available.
                _direction_id = _claimed_context.get("direction_id")
                _candidate_cats: dict | None = None
                if _direction_id:
                    _dir_entry = (_miss_trends.get("by_direction") or {}).get(
                        str(_direction_id)
                    ) or {}
                    _dir_cats = _dir_entry.get("categories")
                    if isinstance(_dir_cats, dict) and any(
                        isinstance(v, dict)
                        and isinstance(v.get("examples"), list)
                        and v["examples"]
                        for v in _dir_cats.values()
                    ):
                        _candidate_cats = _dir_cats
                if _candidate_cats is None:
                    _candidate_cats = _miss_trends["categories"]

                _ranked = sorted(
                    [
                        (cat, data)
                        for cat, data in _candidate_cats.items()
                        if isinstance(data, dict)
                        and isinstance(data.get("examples"), list)
                        and data["examples"]
                    ],
                    key=lambda x: (
                        x[1].get("weighted_miss_rate") or x[1].get("miss_rate") or 0.0
                    ),
                    reverse=True,
                )[:3]
                _miss_examples: dict = {}
                _total_chars = 0
                for _cat, _data in _ranked:
                    _cat_exs = []
                    for _ex in _data["examples"][:3]:
                        _desc = (str(_ex.get("desc") or ""))[:200]
                        if not _desc:
                            continue
                        if _total_chars + len(_desc) > 1200:
                            break
                        _total_chars += len(_desc)
                        _cat_exs.append(
                            {
                                "severity": _ex.get("severity", "MINOR"),
                                "desc": _desc,
                            }
                        )
                    if _cat_exs:
                        _miss_examples[_cat] = _cat_exs
                    if _total_chars >= 1200:
                        break
                if _miss_examples:
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["sr_recent_miss_examples"] = (
                        _miss_examples
                    )
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_recent_miss_examples (non-critical)"
            )

    # Inject pre-computed SR vs fresh-reviewer disagreements. Stored by
    # compute_sr_reviewer_disagreements into design_doc["sr_reviewer_disagreements"].
    # Shape: project_context.sr_reviewer_disagreements =
    #   {window_days, disagreements: [{category, task_type, count, examples?}]}
    if task_type in CODE_REVIEW_GATED_TASK_TYPES and project_for_playbook:
        try:
            _disagreements_raw = (project_for_playbook.design_doc or {}).get(
                "sr_reviewer_disagreements"
            )
            if (
                isinstance(_disagreements_raw, dict)
                and isinstance(_disagreements_raw.get("disagreements"), list)
                and _disagreements_raw["disagreements"]
            ):
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["sr_reviewer_disagreements"] = {
                    "window_days": _disagreements_raw.get("window_days", 30),
                    "disagreements": _disagreements_raw["disagreements"],
                }
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_reviewer_disagreements (non-critical)"
            )

    # Inject scoped architecture context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        design_doc = project_for_playbook.design_doc or {}
        file_to_system_raw = design_doc.get("_file_to_system", {})
        file_to_system = (
            file_to_system_raw if isinstance(file_to_system_raw, dict) else {}
        )
        if file_to_system:
            arch_related_files = _claimed_context.get("related_files", [])
            seen_systems: dict[str, dict] = {}
            for rf in arch_related_files:
                sys_name = file_to_system.get(rf)
                if sys_name and sys_name not in seen_systems:
                    sys_entry = design_doc.get(f"systems/{sys_name}")
                    if isinstance(sys_entry, dict):
                        seen_systems[sys_name] = sys_entry
                        if len(seen_systems) >= 5:
                            break
            if seen_systems:
                arch_ctx = []
                for sys_name, sys_data in seen_systems.items():
                    desc = sys_data.get("description", "")
                    arch_ctx.append(
                        {
                            "name": sys_data.get("name", sys_name),
                            "status": str(sys_data.get("status", "")),
                            "ready_for_features": bool(
                                sys_data.get("ready_for_features", False)
                            ),
                            "description": desc[:200] if isinstance(desc, str) else "",
                        }
                    )
                # Safety: drop trailing entries if total exceeds 1500 chars
                while arch_ctx and len(json.dumps(arch_ctx)) > 1500:
                    arch_ctx.pop()
                if arch_ctx:
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["architecture_context"] = arch_ctx
                    # Track arch context injection in task.context for effectiveness measurement
                    try:
                        _arch_task_ctx = dict(task.context or {})
                        _arch_task_ctx["_had_arch_context"] = True
                        _arch_task_ctx["_shown_arch_system_names"] = list(
                            seen_systems.keys()
                        )
                        task.context = _arch_task_ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                    except Exception:
                        logger.exception(
                            "[AGENT] Failed to track arch context injection (non-critical)"
                        )

    # Inject per-type first-review rejection patterns (last 7 days, time-decayed)
    # so the next create/fix agent sees WHY work like its own got rejected —
    # closing the loop from task_lifecycle's _first_attempt_rejection_categories
    # writes into the prompts of the agents that can act on them. Shape:
    #   project_context.rejection_patterns = {task_type, window_days, sample_size,
    #     categories: [{category, weight, task_count, share, guidance}, ...]}
    # Rendered agent-side by context-prefix.ts:formatRejectionPatternsSection as
    # observed frequencies (context, not a checklist). Deliberately injected LAST
    # and packed LOW priority: inject_rejection_patterns ships the block only when
    # the fully-assembled project_context leaves room under its soft byte budget,
    # so under budget pressure this advisory field drops first and never displaces
    # the mission-critical fields assembled above.
    if task_type in CODE_REVIEW_GATED_TASK_TYPES and project_for_playbook:
        try:
            from app.rejection_patterns import (
                aggregate_rejection_patterns,
                build_rejection_patterns_block,
                inject_rejection_patterns,
            )

            _rejection_aggregates = await aggregate_rejection_patterns(
                session, project_for_playbook.id
            )
            _rejection_block = build_rejection_patterns_block(
                _rejection_aggregates, task_type
            )
            if _rejection_block:
                _rejection_target = response.get("project_context") or {}
                if inject_rejection_patterns(_rejection_target, _rejection_block):
                    response["project_context"] = _rejection_target
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject rejection_patterns (non-critical)"
            )

    # Inject strategic intent so tactical agents understand the "why" behind
    # their work. Useful for every task type. Prefer the LIVE, operator-editable
    # problem model (onboarding + Problem Model editor write here) so current
    # steering reaches the very next worker without waiting for a strategic
    # cycle to mirror it into the legacy vision blob. Fall back to the legacy
    # vision summary when the problem model is empty/malformed so existing
    # projects are unaffected. Exactly one source is sent to avoid the agent
    # double-rendering overlapping "why" sections.
    if project_for_playbook:
        strategic_intent = _build_problem_model_summary(
            project_for_playbook.problem_model
        )
        if strategic_intent:
            response["strategic_intent"] = strategic_intent
        else:
            vision_summary = _build_vision_summary(project_for_playbook.vision)
            if vision_summary:
                response["vision"] = vision_summary

    # Inject direction narrative (established findings + open questions + last
    # cycle outcome) for task types that work inside a direction. Gives agents
    # a sense of trajectory rather than just a snapshot of recent tasks.
    if (
        direction_id
        and project_for_playbook
        and (
            task_type
            in (
                "create",
                "fix",
                "refactor",
                "research",
                "plan",
            )
            or task_type in _PEER_CODE_ANALYSIS_TYPES
        )
    ):
        _narrative_direction = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if _narrative_direction:
            narrative = _build_direction_narrative(_narrative_direction)
            if narrative:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["narrative"] = narrative
            # Surface the current direction's contract (success evidence /
            # completion criteria / retirement condition) so workers can align
            # to what "done" means for this direction, not just its analytics.
            contract = _build_direction_contract(_narrative_direction)
            if contract:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["contract"] = contract
            # Surface the direction goal/title so peer code-change and analysis
            # agents (qa/migrate/debug/perf/investigate) launch knowing which
            # direction they serve, not just its narrative/contract bodies.
            if narrative or contract:
                response["direction_context"].setdefault(
                    "title", _narrative_direction.get("title", "")
                )

    # Inject build_config so the agent knows how to lint/test/build
    if project_for_playbook and project_for_playbook.build_config:
        response["build_config"] = project_for_playbook.build_config
        if project_for_playbook.build_config.get("directories"):
            response["build_config_writeback_endpoint"] = (
                f"PATCH /api/agent/projects/{_claimed_project_id}/build-config/commands"
            )
    else:
        response["build_config"] = {}

    # Inject repo_overview for repo-reading code-change, review, and analysis
    # tasks so agents know the tech stack and layout discovered during
    # repo_bootstrap without re-discovering it from scratch. Omitted entirely
    # when absent so the claim shape is byte-identical for projects that never
    # ran bootstrap.
    _REPO_OVERVIEW_TASK_TYPES = {
        "create",
        "fix",
        "refactor",
        "migrate",
        "qa",
        "docs",
        "review",
        "research",
        "plan",
        "design",
        "debug",
        "investigate",
        "security_audit",
        "perf",
        "self_audit",
        "release_audit",
        "release_audit_challenge",
        "smoketest",
        "design_doc_update",
        "strategic_review",
    }
    if task_type in _REPO_OVERVIEW_TASK_TYPES and project_for_playbook:
        _raw_overview = (project_for_playbook.design_doc or {}).get("repo_overview")
        if (
            isinstance(_raw_overview, dict)
            and isinstance(_raw_overview.get("tech_stack"), str)
            and _raw_overview["tech_stack"]
            and isinstance(_raw_overview.get("directory_layout"), str)
            and _raw_overview["directory_layout"]
            and isinstance(_raw_overview.get("first_steps_summary"), str)
            and _raw_overview["first_steps_summary"]
        ):
            _repo_overview: dict = {
                "tech_stack": _raw_overview["tech_stack"],
                "directory_layout": _raw_overview["directory_layout"],
                "first_steps_summary": _raw_overview["first_steps_summary"],
            }
            for _opt_field in (
                "entry_points",
                "test_layout",
                "key_conventions",
                "environment_requirements",
            ):
                _opt_val = _raw_overview.get(_opt_field)
                if isinstance(_opt_val, str) and _opt_val:
                    _repo_overview[_opt_field] = _opt_val
            _bootstrap_completed_at = (project_for_playbook.build_config or {}).get(
                "bootstrap_completed_at"
            )
            if _bootstrap_completed_at:
                _repo_overview["bootstrap_completed_at"] = _bootstrap_completed_at
            _provenance = _raw_overview.get("provenance")
            if isinstance(_provenance, dict):
                _repo_provenance = {
                    k: v
                    for k, v in _provenance.items()
                    if k in {"refreshed_at", "source_branch", "source_revision"}
                    and isinstance(v, str)
                    and v.strip()
                }
                if _repo_provenance:
                    _repo_overview["provenance"] = _repo_provenance
            response["repo_overview"] = _repo_overview

    # Inject domain_overview for repo-reading task types when a domain
    # KnowledgeDocument exists (produced by repo_bootstrap completion).
    # Best-effort: exceptions are swallowed so a DB hiccup never breaks claim.
    if task_type in _REPO_OVERVIEW_TASK_TYPES and project_for_playbook:
        try:
            _domain_result = await session.execute(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.project_id == _claimed_project_id,
                    KnowledgeDocument.topic == "domain-knowledge",
                    KnowledgeDocument.source_kind == SourceKind.REPO_DOMAIN.value,
                )
                .limit(1)
            )
            _domain_doc = _domain_result.scalar_one_or_none()
            if _domain_doc is not None:
                response["domain_overview"] = {
                    "summary": _domain_doc.summary,
                    "body": _domain_doc.body,
                }
        except Exception:
            pass

    # Inject bootstrap_guidance for repo_bootstrap tasks: structured prompting
    # for the agent to read README/manifests, produce a repo overview payload,
    # and write back confirmed build/test commands.
    # The overview is returned as repo_overview in the terminal completion payload
    # and persisted server-side by the completion handler into design_doc["repo_overview"].
    if task_type == "repo_bootstrap":
        _bootstrap_payload = _claimed_first_payload or {}
        response["bootstrap_guidance"] = {
            "files_to_read": [
                "README.md",
                "README.rst",
                "README",
                "package.json",
                "yarn.lock",
                "pnpm-lock.yaml",
                "Makefile",
                "makefile",
                "GNUmakefile",
                "Dockerfile",
                "docker-compose.yml",
                "docker-compose.yaml",
                ".github/workflows/*.yml",
            ],
            "design_doc_section": "repo_overview",
            "design_doc_section_fields": [
                "tech_stack",
                "directory_layout",
                "first_steps_summary",
                "entry_points",
                "test_layout",
                "key_conventions",
                "environment_requirements",
                "source_revision",
            ],
            "completion_payload_field": "repo_overview",
            "build_config_writeback_endpoint": (
                f"PATCH /api/agent/projects/{_claimed_project_id}/build-config/commands"
            ),
            "completion_steps": [
                "Read listed files to understand project structure",
                "Write /tmp/bootstrap_overview_<task_id>.json with tech_stack, directory_layout, first_steps_summary, entry_points (main runnable scripts/services and their purposes), test_layout (where tests live, unit/integration/e2e organization, and run commands), key_conventions (branch naming, commit style, required review steps), environment_requirements (required env vars, system deps, version constraints beyond install_cmd), source_revision (the checked-out HEAD SHA after clone/checkout)",
                "Confirm working build/test commands by running them",
                "Write confirmed commands back via the build_config_writeback_endpoint",
                "Post COMPLETED with repo_overview in the completion payload",
            ],
            "github_owner": _bootstrap_payload.get("github_owner", ""),
            "github_repo": _bootstrap_payload.get("github_repo", ""),
            "integration_branch": _bootstrap_payload.get("integration_branch", ""),
        }

    # Fire-and-forget: create an in_progress Check Run for review tasks so the
    # Checks tab shows the review is underway.  Must not block the claim response.
    if task_type == "review":
        _pr_number = (_claimed_first_payload or {}).get("pr_number")
        if _pr_number and project_for_playbook:
            _check_run_name = (project_for_playbook.build_config or {}).get(
                "check_run_name", "Blueprint Code Review"
            )
            _check_run_fut = asyncio.ensure_future(
                _create_review_check_run_bg(
                    _claimed_task_id, _pr_number, _check_run_name
                )
            )
            _bg_tasks.add(_check_run_fut)
            _check_run_fut.add_done_callback(_bg_tasks.discard)

    return response


@router.post("/artifacts/upload-url")
@limiter.limit("300/minute")
async def get_upload_url(
    request: Request,
    req: UploadUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Agents call this to get a temporary Signed URL to upload screenshots/logs directly to GCS.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    import os

    safe_filename = os.path.basename(req.filename)
    url = await generate_upload_signed_url(str(req.task_id), safe_filename)
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{req.task_id}/{safe_filename}"

    return {"upload_url": url, "storage_uri": storage_uri}


@router.get("/artifacts/download-url", response_model=ArtifactDownloadResponse)
@limiter.limit("300/minute")
async def get_agent_artifact_download_url(
    request: Request,
    artifact_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Generate a signed GET URL for an artifact delivered to this task."""
    task_id = uuid.UUID(str(token.get("sub")))
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    delivered = (task.context or {}).get(ARTIFACT_PREREQUISITES_KEY)
    allowed_ids = (
        {
            str(p.get("artifact_id"))
            for p in delivered
            if isinstance(p, dict) and p.get("artifact_id")
        }
        if isinstance(delivered, list)
        else set()
    )
    if str(artifact_id) not in allowed_ids:
        raise HTTPException(status_code=404, detail="Artifact not found")

    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.storage_uri is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    producer = await session.get(Task, artifact.task_id)
    if producer is None or producer.project_id != task.project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")

    from app.storage import StorageUriError, generate_download_signed_url

    try:
        url = await generate_download_signed_url(artifact.storage_uri)
    except StorageUriError:
        raise HTTPException(status_code=400, detail="Invalid artifact reference")
    except Exception:
        raise HTTPException(status_code=503, detail="Artifact storage unavailable")
    return ArtifactDownloadResponse(url=url)


class ViewportFailureScreenshotUrlRequest(BaseModel):
    task_id: uuid.UUID
    component: str = Field(max_length=200)
    width: int = Field(ge=1, le=4096)


@router.post("/viewport/failure-screenshot-upload-url")
@limiter.limit("120/minute")
async def get_viewport_failure_screenshot_upload_url(
    request: Request,
    req: ViewportFailureScreenshotUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Generate a signed upload URL for a viewport failure screenshot at a stable
    per-project/component/width GCS path.

    The stable path (viewport-failures/{project_id}/{component}-{width}.png) lets the
    regression panel construct a download URL without a per-task DB lookup.  The GCS URI
    is also stamped onto task.context so the regression dispatcher can propagate it to the
    RegressionRemediationCase.failure_screenshot_url when that column is added.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    import datetime as _dt

    from google.cloud import storage as _gcs

    from app.storage import ARTIFACT_BUCKET_NAME, GCP_PROJECT_ID
    from app.viewport_evidence import viewport_object_name

    object_name = viewport_object_name(task.project_id, req.component, req.width)
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    def _generate() -> str:
        blob = (
            _gcs.Client(project=GCP_PROJECT_ID)
            .bucket(ARTIFACT_BUCKET_NAME)
            .blob(object_name)
        )
        return blob.generate_signed_url(
            version="v4",
            expiration=_dt.timedelta(minutes=15),
            method="PUT",
            content_type="image/png",
        )

    try:
        upload_url = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed upload URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(
            status_code=503, detail="Artifact storage unavailable"
        ) from exc

    ctx = dict(task.context or {})
    ctx["viewport_failure_screenshot_uri"] = storage_uri
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)
    await session.commit()

    return {"upload_url": upload_url, "storage_uri": storage_uri}


class ViewportScreenshotUploadUrlRequest(BaseModel):
    task_id: uuid.UUID
    name: str = Field(max_length=200)
    width: int = Field(ge=1, le=4096)
    phase: Literal["before", "after", "diff"]
    magnitude: Optional[float] = Field(default=None, ge=0.0, le=1.0)


@router.post("/viewport/screenshot-upload-url")
@limiter.limit("120/minute")
async def get_viewport_screenshot_upload_url(
    request: Request,
    req: ViewportScreenshotUploadUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Generate a signed upload URL for a task-scoped before/after viewport screenshot.

    The object path (viewport-shots/{project_id}/{task_id}/{safe_name}-{width}-{phase}.png)
    is task-scoped to avoid collisions across tasks.  The storage_uri is appended to
    task.context["viewport_screenshots"] (capped at 24 entries) so the PR renderer can
    later retrieve the list and inject the screenshots into the PR body.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    import datetime as _dt
    import re as _re

    from google.cloud import storage as _gcs

    from app.storage import ARTIFACT_BUCKET_NAME, GCP_PROJECT_ID

    _SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")
    safe_name = _SAFE_NAME_RE.sub("_", req.name)[:100]

    screenshots = list((task.context or {}).get("viewport_screenshots", []))
    if len(screenshots) >= 24:
        raise HTTPException(status_code=429, detail="Upload cap reached")

    object_name = f"viewport-shots/{task.project_id}/{task.id}/{safe_name}-{req.width}-{req.phase}.png"
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    def _generate() -> str:
        blob = (
            _gcs.Client(project=GCP_PROJECT_ID)
            .bucket(ARTIFACT_BUCKET_NAME)
            .blob(object_name)
        )
        return blob.generate_signed_url(
            version="v4",
            expiration=_dt.timedelta(minutes=15),
            method="PUT",
            content_type="image/png",
        )

    try:
        upload_url = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed upload URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(
            status_code=503, detail="Artifact storage unavailable"
        ) from exc

    ctx = dict(task.context or {})
    entry_name = f"{safe_name}-{req.width}-{req.phase}"
    entry: dict = {"name": entry_name, "storage_uri": storage_uri}
    if req.magnitude is not None:
        entry["magnitude"] = req.magnitude
    screenshots.append(entry)
    ctx["viewport_screenshots"] = screenshots
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)
    await session.commit()

    return {"upload_url": upload_url, "storage_uri": storage_uri}


@router.get("/viewport/failure-screenshot-download-url")
@limiter.limit("120/minute")
async def get_viewport_failure_screenshot_download_url_agent(
    request: Request,
    project_id: uuid.UUID,
    component: str,
    width: int,
    token: dict = Depends(verify_agent_token),
    session: AsyncSession = Depends(get_session),
):
    """Generate a signed download URL for a viewport failure screenshot (agent-token path).

    Scoped to the agent's own task (sub claim) — cross-task reads are denied.
    The calling task must have previously uploaded a screenshot at the requested
    path (stamped in task.context['viewport_failure_screenshot_uri']).
    """
    if str(project_id) != token.get("project_id"):
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    task_id = token.get("sub")
    if not task_id:
        raise HTTPException(status_code=403, detail="Agent token has no task scope")

    result = await session.execute(select(Task).where(Task.id == uuid.UUID(task_id)))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=403, detail="Task not found")

    from app.storage import (
        ARTIFACT_BUCKET_NAME,
        StorageUriError,
        generate_download_signed_url,
    )
    from app.viewport_evidence import viewport_object_name

    object_name = viewport_object_name(project_id, component, width)
    stamped_uri = (task.context or {}).get("viewport_failure_screenshot_uri", "")
    if not stamped_uri or not stamped_uri.endswith(object_name):
        raise HTTPException(
            status_code=403,
            detail="Agent may only download its own uploaded screenshot",
        )
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        url = await generate_download_signed_url(storage_uri, expiration_minutes=60)
    except StorageUriError as exc:
        logger.warning("[VIEWPORT] invalid screenshot reference: %s", exc)
        raise HTTPException(
            status_code=400, detail="Invalid screenshot reference"
        ) from exc  # noqa: B904
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[VIEWPORT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(
            status_code=503, detail="Artifact storage unavailable"
        ) from exc  # noqa: B904
    return {"url": url}


@router.get("/viewport/screenshot-download-url")
@limiter.limit("120/minute")
async def get_viewport_screenshot_download_url_agent(
    request: Request,
    task_id: uuid.UUID,
    name: str,
    token: dict = Depends(verify_agent_token),
):
    """Generate a signed download URL for a task-scoped viewport screenshot (agent-token path).

    Scoped to the agent's own task (sub claim) — cross-task reads are denied.
    The GCS path mirrors the upload endpoint exactly.
    """
    import re as _re

    if str(task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    from app.storage import (
        ARTIFACT_BUCKET_NAME,
        StorageUriError,
        generate_download_signed_url,
    )

    _SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")
    safe_name = _SAFE_NAME_RE.sub("_", name)

    project_id = token.get("project_id")
    object_name = f"viewport-shots/{project_id}/{task_id}/{safe_name}.png"
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        url = await generate_download_signed_url(storage_uri, expiration_minutes=60)
    except StorageUriError as exc:
        logger.warning("[VIEWPORT] invalid screenshot reference: %s", exc)
        raise HTTPException(
            status_code=400, detail="Invalid screenshot reference"
        ) from exc  # noqa: B904
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[VIEWPORT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(
            status_code=503, detail="Artifact storage unavailable"
        ) from exc  # noqa: B904
    return {"url": url}


# Beacon prefix → _cold_start_stages key mapping for per-stage breakdown.
# Prefixes mirror compute_startup_script.py beacon strings; checked on every
# heartbeat that carries a status_message matching one of these prefixes.
_COLD_START_BEACON_MAP: dict[str, str] = {
    "PROVISION_PROGRESS: creds fetched": "beacon_a_at",
    "PROVISION_PROGRESS: env file materialized": "beacon_b_at",
    "PROVISION_PROGRESS: payload ready": "beacon_c_at",
    "PROVISION_PROGRESS: reached systemctl phase": "beacon_d_at",
    "Startup script completed": "startup_complete_at",
}


def _parse_latest_tool_use_at(tool_trace) -> datetime | None:
    """Parse the most-recent tool-use timestamp from a heartbeat's tool_trace tail.

    The agent stamps each tool_trace entry's ``t`` as ISO-8601. Returns the
    parsed (naive-UTC) timestamp of the last entry, falling back to ``_utcnow``
    if the tail is present but unparseable so we never crash a heartbeat on a
    bad value. Returns ``None`` when there is no tool_trace at all (the caller
    then leaves the off-row progress stamp untouched). Shared by the off-row
    ``vmcontact`` upsert (Fix 3b) and the on-row ``Task.last_tool_use_at``
    column write so both derive the same value from one parse.
    """
    if not tool_trace:
        return None
    try:
        last_t = tool_trace[-1].t
        parsed = datetime.fromisoformat(last_t.replace("Z", "+00:00"))
        # Strip tz so the (naive DateTime) columns don't reject tz-aware values.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (ValueError, AttributeError, IndexError):
        return _utcnow()


async def _record_vm_contact(
    vm_instance_id: str | None,
    last_tool_use_at: datetime | None = None,
) -> None:
    """Record an uncontended liveness signal that the orchestrator heard from a VM.

    This is the heartbeat false-reap fix (option B). The heartbeat handler
    updates the contended ``Task`` row under
    ``with_for_update(skip_locked=True)``; while the rollup holds that row FOR
    UPDATE the heartbeat is skipped (409) and ``Task.last_heartbeat`` never
    advances even though the agent is alive. The reaper reads only
    ``Task.last_heartbeat`` so the live-but-lock-starved VM is falsely reaped.

    We upsert ``vmcontact(vm_instance_id, last_contact_at=now())`` on its OWN
    row, on a fresh session committed independently of the request session, so
    this write:
      * never contends with the rollup's task-row lock (different row), and
      * persists even if the later task-row update 409s / rolls back.

    The reaper reads this row and suppresses a reap only when ``last_contact_at``
    is within the SAME heartbeat-stale cutoff — so a genuinely dead agent (no
    requests) is still reaped on the existing schedule. A failure here must NEVER
    break the heartbeat response: it is best-effort liveness, the existing
    ``Task.last_heartbeat`` path is unchanged, and the worst case on failure is
    falling back to today's behaviour.

    ``last_tool_use_at`` (Fix 3b) carries the same lock-starvation immunity to
    the PROGRESS-stall branch: a lock-starved heartbeat also drops
    ``Task.last_tool_use_at``, so the reaper's stall watchdog can false-reap a
    live PROGRESSING agent. We upsert it OFF the contended task row here so the
    reaper can corroborate genuine progress. We only ADVANCE it (never regress
    it to NULL or to an older value), so a tool-less heartbeat between two
    tool-bearing ones can't blank a fresh progress signal; a heartbeat that
    carries no tool telemetry passes ``None`` and leaves the stored value
    untouched.
    """
    if not vm_instance_id:
        return
    now = _utcnow()
    try:
        async with AsyncSessionLocal() as contact_session:
            dialect = resolve_dialect_name(contact_session)
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as _pg_insert

                insert_values = {
                    "vm_instance_id": vm_instance_id,
                    "last_contact_at": now,
                }
                set_ = {"last_contact_at": now}
                if last_tool_use_at is not None:
                    insert_values["last_tool_use_at"] = last_tool_use_at
                    # Monotonic: never regress the stored progress stamp. GREATEST
                    # over the existing value tolerates out-of-order heartbeats and
                    # a tool_trace tail older than what we already recorded.
                    set_["last_tool_use_at"] = func.greatest(
                        VmContact.last_tool_use_at, last_tool_use_at
                    )
                stmt = _pg_insert(VmContact).values(**insert_values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[VmContact.vm_instance_id],
                    set_=set_,
                )
                await contact_session.execute(stmt)
            else:
                # SQLite (test fixture) has no shared on_conflict helper here;
                # emulate the upsert with a get-then-write on the primary key.
                existing = await contact_session.get(VmContact, vm_instance_id)
                if existing is None:
                    contact_session.add(
                        VmContact(
                            vm_instance_id=vm_instance_id,
                            last_contact_at=now,
                            last_tool_use_at=last_tool_use_at,
                        )
                    )
                else:
                    existing.last_contact_at = now
                    # Monotonic: only advance last_tool_use_at.
                    if last_tool_use_at is not None and (
                        existing.last_tool_use_at is None
                        or last_tool_use_at > existing.last_tool_use_at
                    ):
                        existing.last_tool_use_at = last_tool_use_at
            await contact_session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort, never break heartbeat
        logger.warning(
            "[VM_CONTACT] upsert failed for vm=%s (non-fatal, falling back to "
            "last_heartbeat): %s",
            vm_instance_id,
            exc,
        )


@router.post("/heartbeat")
@limiter.limit("600/minute")
async def agent_heartbeat(
    request: Request,
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Send a keep-alive heartbeat with optional telemetry.

    Agents periodically call this endpoint (~30-60s interval) to signal
    liveness and optionally report progress, metrics, tool usage, or status
    beacons. Reaper uses 5-minute heartbeat timeout to detect stuck agents.

    Request body (HeartbeatRequest):
        {
            "task_id": str, "status_message": str | null,
            "metrics": HeartbeatMetrics | null,
            "tool_traces": list[ToolTraceEntry] | null
        }

    Returns:
        {"status": "ok"} | {"status": "locked", "detail": "..."}

    Status codes:
        200: Heartbeat recorded successfully
        404: Task not found on this VM
        409: Task locked or already terminal (retry or stop)
        401: Invalid token
        403: Task ID mismatch

    Rate limit: 600 requests/minute.
    Heartbeat timeout (reaper): 5 minutes.
    Lock handling: Uses SKIP_LOCKED to avoid stalling on concurrent updates.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # Phase 2c Race A mitigation: every backend contact from a VM is a
    # liveness signal. Without this, the orchestrator's skip-provision
    # branch could clear a freshly-written warm pin before the agent's
    # first /vm-heartbeat call lands (~200-700 ms after spawn). Source the
    # vm_instance_id from the trusted JWT, never from the request body.
    _shared_state.record_vm_heartbeat(token.get("vm_instance_id"))

    # Heartbeat false-reap fix (option B): record an UNCONTENDED durable
    # liveness signal BEFORE the contended task-row read below. The
    # ``with_for_update(skip_locked=True)`` task-row update can be skipped (409)
    # while the rollup holds the row, leaving ``Task.last_heartbeat`` frozen on
    # a live VM — which the reaper then false-reaps. ``vmcontact`` is keyed on
    # ``vm_instance_id`` (its own row, never the task row) and committed on its
    # own session, so it persists regardless of whether the task-row write below
    # succeeds, 409s, or rolls back. Best-effort: a failure here never breaks the
    # heartbeat response and the reaper degrades to the existing last_heartbeat
    # behaviour. The vm_instance_id is sourced from the trusted JWT.
    #
    # Fix 3b: also carry the heartbeat's latest tool-use timestamp off-row. A
    # lock-starved heartbeat drops the contended ``Task.last_tool_use_at`` write
    # too, so the reaper's PROGRESS-stall branch can false-reap a live,
    # actively-PROGRESSING agent. Parsed ONCE here from the tool_trace tail and
    # reused below for the on-row column write so both stay in sync.
    _latest_tool_use_at = _parse_latest_tool_use_at(req.tool_trace)
    await _record_vm_contact(token.get("vm_instance_id"), _latest_tool_use_at)

    # SKIP LOCKED: if the status endpoint holds FOR UPDATE on this row
    # (processing completion/pipeline), skip instead of blocking.  Blocking
    # would starve the heartbeat (agent's 30s HTTP timeout fires before the
    # 10s lock_timeout, but retries compound and the lock holder may take
    # longer than the timeout), causing last_heartbeat to go stale and the
    # reaper to kill the task.
    # Skipping one heartbeat is safe — the reaper's threshold is 5 minutes,
    # and the next heartbeat (30s later) will succeed once the lock releases.
    # Defer the three large JSONB blobs never read by the heartbeat handler
    # (payloads/plan_output/depends_on can be multi-MB for plan/research tasks).
    # All other columns are loaded because context, status_log, fix_loop_history,
    # and many scalar fields are conditionally written each cycle.
    _hb_load_only = load_only(
        Task.id,
        Task.status,
        Task.vm_instance_id,
        Task.last_heartbeat,
        Task.vm_started_at,
        Task.latest_status_message,
        Task.context,
        Task.status_log,
        Task.heartbeat_count,
        Task.last_metrics_json,
        Task.last_tool_use_at,
        Task.fix_loop_history,
        Task.prompt_fingerprint,
        Task.api_call_summary,
        Task.format_compliance_summary,
        raiseload=True,
    )
    stmt = (
        select(Task)
        .where(
            Task.id == req.task_id, Task.vm_instance_id == token.get("vm_instance_id")
        )
        .options(_hb_load_only)
        .with_for_update(skip_locked=True)
    )
    task = (await session.execute(stmt)).scalars().first()

    if not task:
        # Row is either locked (skip_locked skipped it) or vm_instance_id changed.
        # Distinguish: check without FOR UPDATE to see if the task exists at all.
        check_stmt = (
            select(Task)
            .where(Task.id == req.task_id)
            .options(
                load_only(Task.id, Task.vm_instance_id, Task.status, raiseload=True)
            )
        )
        check_task = (await session.execute(check_stmt)).scalars().first()
        if not check_task:
            raise HTTPException(status_code=404, detail="Task not found")
        if check_task.vm_instance_id != token.get("vm_instance_id"):
            raise HTTPException(status_code=404, detail="Task not found for this VM")
        # Row exists but was locked — tell agent to retry next cycle
        return JSONResponse(
            status_code=409,
            content={"status": "locked", "detail": "Task row locked, retry next cycle"},
            headers={"Retry-After": "30"},
        )

    # Signal agent to stop if task was cancelled/completed/blocked externally.
    # BLOCKED is terminal-for-now (precondition absent): the VM must stop rather
    # than keep heartbeating against work that can't run yet.
    if task.status in _AGENT_INACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="Task already terminal")

    task.last_heartbeat = _utcnow()
    if task.vm_started_at is None:
        task.vm_started_at = _utcnow()

    if req.status_message:
        task.latest_status_message = req.status_message
        # Log interesting lifecycle beacons so they're visible in Cloud
        # Logging without needing to inspect the task row. Anything that
        # looks like a structured forensic signal (PROVISION_*, AGENT_*,
        # PRE_CLAIM_*, FATAL:, STARTUP_FAILED) gets echoed — a silent
        # pre-claim death should then show up as a specific beacon landing
        # instead of a log void.
        msg = req.status_message
        if any(
            msg.startswith(prefix)
            for prefix in (
                "PROVISION_",
                "AGENT_BOOTED",
                "AGENT_AUTH_OK",
                "AGENT_CLAIMING",
                "PRE_CLAIM_",
                "[FATAL",
                "STARTUP_FAILED",
            )
        ):
            logger.info(
                "[HEARTBEAT_BEACON] task=%s vm=%s msg=%s",
                req.task_id,
                token.get("vm_instance_id"),
                msg[:300],
            )
        # Stamp per-stage cold-start timestamps for breakdown telemetry.
        for _beacon_prefix, _stage_key in _COLD_START_BEACON_MAP.items():
            if msg.startswith(_beacon_prefix):
                _stgs = dict((task.context or {}).get("_cold_start_stages") or {})
                if _stage_key not in _stgs:
                    _stgs[_stage_key] = _utcnow().isoformat()
                    task.context = {**(task.context or {}), "_cold_start_stages": _stgs}
                break
    # Heartbeat counter (R4-W2 H1+H2+H3+M10): increment on EVERY heartbeat
    # (hoisted out of the metrics-conditional block — metrics-less heartbeats
    # were never advancing the counter, biasing the audit-trail gate). ORM
    # mutation replaces the prior Core ``UPDATE`` (1 round-trip vs 2; in-
    # memory value stays in sync with persisted value, fixing the
    # ``synchronize_session="auto"`` off-by-one).
    task.heartbeat_count = (task.heartbeat_count or 0) + 1
    hb_count = task.heartbeat_count  # already incremented in-memory

    if req.metrics or req.tool_trace is not None:
        # Phase B2 (audit 2026-04-29 / finding C3): hot fields (last_metrics,
        # last_tool_use_at) are dedicated columns — cheap in-place column
        # UPDATEs, no whole-blob TOAST page rewrites every 30s/VM. JSONB
        # mirror writes for these two fields were retired in Phase F (audit
        # 2026-04-29 round 2 / finding H8) so the column writes are now the
        # sole source of truth. Frontend (TaskDetailDrawerMeta) must read
        # the columns directly — reading task.context.last_metrics will go
        # stale.
        #
        # Audit-trail fields (metrics_history ring buffer, _tool_trace death-
        # trace window) are post-mortem-only, so we persist them every 5th
        # heartbeat (~2.5 min at 30s cadence) instead of every cycle. The
        # agent-side ring buffer is the in-flight source of truth.
        metrics_dump: dict | None = None
        if req.metrics:
            metrics_dump = req.metrics.model_dump(exclude_none=True)

        # last_tool_use_at is parsed once at the top of the handler (reused for
        # the off-row ``vmcontact`` upsert in Fix 3b) so the on-row column write
        # and the off-row progress signal can never diverge.
        latest_tool_use_at: datetime | None = _latest_tool_use_at

        # Hot-path writes: dedicated columns. Cheap in-place column UPDATE.
        if metrics_dump is not None:
            task.last_metrics_json = metrics_dump
        if latest_tool_use_at is not None:
            task.last_tool_use_at = latest_tool_use_at

        # Every-5th-heartbeat gate for the audit-trail ring buffers. Round-1
        # used ``int(last_heartbeat.timestamp()) % 5`` which was a per-VM
        # lottery: at 30s cadence consecutive heartbeats from the same VM
        # hit the same modulo bucket (delta=30s, 30%5==0), so ~20% of VMs
        # always persisted and ~80% never persisted (audit C1).
        # The counter lives in a dedicated ``Task.heartbeat_count`` Integer
        # column (alembic 0066) and is bumped above (R4-W2) — the gate just
        # reads the in-memory value.
        persist_audit_trail = hb_count % 5 == 0

        if persist_audit_trail:
            ctx = dict(task.context or {})
            if metrics_dump is not None:
                history = list(ctx.get("metrics_history", []))
                entry: dict = {"t": _utcnow().isoformat()}
                for k in (
                    "rss_mb",
                    "heap_used_mb",
                    "disk_free_mb",
                    "disk_total_mb",
                    "cgroup_mb",
                    "cgroup_limit_mb",
                ):
                    if metrics_dump.get(k) is not None:
                        entry[k] = metrics_dump[k]
                history.append(entry)
                if len(history) > 30:
                    history = history[-30:]
                ctx["metrics_history"] = history
            if req.tool_trace is not None:
                # Overwrite — the agent's ring buffer already trims to the
                # latest window, so each heartbeat carries the authoritative
                # recent-tool-calls window. Last-successful value is the
                # death trace after reap.
                ctx["_tool_trace"] = [t.model_dump() for t in req.tool_trace]
            # Only assign on the 5th-cycle write — assigning on every
            # heartbeat would re-serialize the whole JSONB blob and defeat
            # the column-counter optimisation we just made.
            task.context = ctx

    if req.fix_loop_events:
        # Fold optional fix-loop telemetry events into Task.fix_loop_history.
        # Validation lives at the Pydantic boundary (FixLoopEventIn); invalid
        # entries are dropped silently so a malformed event from a forward-rolled
        # agent doesn't 422 the whole heartbeat.
        from app.fix_loop_telemetry import FixLoopEventIn, apply_fix_loop_events

        validated: list[FixLoopEventIn] = []
        for raw in req.fix_loop_events:
            try:
                validated.append(FixLoopEventIn(**raw))
            except Exception as exc:  # noqa: BLE001, PERF203
                logger.info(
                    "[FIX_LOOP] dropping invalid event for task %s: %s",
                    req.task_id,
                    exc,
                )
        if validated:
            apply_fix_loop_events(task, validated)

    # Persist prompt fingerprint on the dedicated column. Mirror to context only
    # when needed for older readers / backfills, so repeated heartbeats stay
    # idempotent and don't force unnecessary JSONB rewrites.
    if req.prompt_fingerprint:
        if task.prompt_fingerprint != req.prompt_fingerprint:
            task.prompt_fingerprint = req.prompt_fingerprint
        if (task.context or {}).get("prompt_fingerprint") != req.prompt_fingerprint:
            ctx = dict(task.context or {})
            ctx["prompt_fingerprint"] = req.prompt_fingerprint
            task.context = ctx

    # Write-once repo-map capability flag. Set on the first heartbeat that
    # carries had_repo_map so that later heartbeats (which don't re-send it)
    # cannot clear the flag; mirrors the _had_arch_context write-once pattern.
    if (
        req.had_repo_map is not None
        and (task.context or {}).get("_had_repo_map") is None
    ):
        ctx = dict(task.context or {})
        ctx["_had_repo_map"] = req.had_repo_map
        task.context = ctx
    if (
        req.had_module_interfaces is not None
        and (task.context or {}).get("_had_module_interfaces") is None
    ):
        ctx = dict(task.context or {})
        ctx["_had_module_interfaces"] = req.had_module_interfaces
        task.context = ctx
    if (
        req.had_related_tests is not None
        and (task.context or {}).get("_had_related_tests") is None
    ):
        ctx = dict(task.context or {})
        ctx["_had_related_tests"] = req.had_related_tests
        task.context = ctx
    if (
        req.had_affected_consumers is not None
        and (task.context or {}).get("_had_affected_consumers") is None
    ):
        ctx = dict(task.context or {})
        ctx["_had_affected_consumers"] = req.had_affected_consumers
        task.context = ctx

    # Write-once repo-map arm-assignment flag. Records the deterministic
    # holdback vs treatment assignment independently of receipt; used for
    # intent-to-treat arm partitioning in digest_metrics.
    if (
        req.repo_map_arm is not None
        and (task.context or {}).get("_repo_map_arm") is None
    ):
        ctx = dict(task.context or {})
        ctx["_repo_map_arm"] = req.repo_map_arm
        task.context = ctx
    if (
        req.module_interfaces_arm is not None
        and (task.context or {}).get("_module_interfaces_arm") is None
    ):
        ctx = dict(task.context or {})
        ctx["_module_interfaces_arm"] = req.module_interfaces_arm
        task.context = ctx
    if (
        req.related_tests_arm is not None
        and (task.context or {}).get("_related_tests_arm") is None
    ):
        ctx = dict(task.context or {})
        ctx["_related_tests_arm"] = req.related_tests_arm
        task.context = ctx
    if (
        req.affected_consumers_arm is not None
        and (task.context or {}).get("_affected_consumers_arm") is None
    ):
        ctx = dict(task.context or {})
        ctx["_affected_consumers_arm"] = req.affected_consumers_arm
        task.context = ctx

    # Write-once diagnostics-after-edit capability flag. Set on the first
    # heartbeat that carries had_diagnostics_after_edit; mirrors had_repo_map pattern.
    if (
        req.had_diagnostics_after_edit is not None
        and (task.context or {}).get("_had_diagnostics_after_edit") is None
    ):
        ctx = dict(task.context or {})
        ctx["_had_diagnostics_after_edit"] = req.had_diagnostics_after_edit
        task.context = ctx
    # Write-once diagnostics-after-edit arm-assignment flag. Records the
    # deterministic holdback vs treatment assignment for intent-to-treat analysis.
    if (
        req.diagnostics_after_edit_arm is not None
        and (task.context or {}).get("_diagnostics_after_edit_arm") is None
    ):
        ctx = dict(task.context or {})
        ctx["_diagnostics_after_edit_arm"] = req.diagnostics_after_edit_arm
        task.context = ctx

    # Per-task LLM telemetry (efficiency direction d-91d8dede). Cumulative
    # counters posted on every heartbeat — last write wins. Backwards-
    # compatible: older agents that don't ship these fields leave the column
    # untouched. Mirrors the every-heartbeat cadence of last_metrics_json so
    # downstream aggregations always see the freshest snapshot.
    if (
        req.llm_call_count is not None
        or req.num_turns is not None
        or req.total_input_tokens is not None
        or req.total_output_tokens is not None
        or req.total_cache_creation_input_tokens is not None
        or req.total_cache_read_input_tokens is not None
        or req.viewport_screenshot_forwarded_count is not None
        or req.total_tool_result_bytes is not None
        or req.prompt_size_bytes is not None
        or req.prompt_section_count is not None
        or req.prompt_section_sizes is not None
    ):
        summary = {
            "llm_call_count": int(req.llm_call_count or 0),
            "num_turns": int(req.num_turns or 0),
            "total_input_tokens": int(req.total_input_tokens or 0),
            "total_output_tokens": int(req.total_output_tokens or 0),
            "viewport_screenshot_forwarded_count": int(
                req.viewport_screenshot_forwarded_count or 0
            ),
            "last_updated_at": _utcnow().isoformat(),
        }
        if req.num_turns is not None:
            summary["num_turns"] = int(req.num_turns)
        # Persist the cache-class sub-counts only when the agent actually sent
        # them, so an older agent's heartbeat writes a byte-for-byte unchanged
        # summary. total_input_tokens stays the INCLUSIVE input total; these are
        # sub-counts of it priced at their true cache rates by efficiency_snapshot.
        if req.total_cache_creation_input_tokens is not None:
            summary["total_cache_creation_input_tokens"] = int(
                req.total_cache_creation_input_tokens
            )
        if req.total_cache_read_input_tokens is not None:
            summary["total_cache_read_input_tokens"] = int(
                req.total_cache_read_input_tokens
            )
        # Persist total_tool_result_bytes only when the agent sent it so older
        # agents leave the key absent (unknown ≠ zero) in downstream aggregations.
        if req.total_tool_result_bytes is not None:
            summary["total_tool_result_bytes"] = int(req.total_tool_result_bytes)
        # Persist prompt-size telemetry only when sent — backwards-compatible
        # with older agents that don't emit these fields.
        if req.prompt_size_bytes is not None:
            summary["prompt_size_bytes"] = int(req.prompt_size_bytes)
        if req.prompt_section_count is not None:
            summary["prompt_section_count"] = int(req.prompt_section_count)
        if req.prompt_section_sizes is not None:
            summary["prompt_section_sizes"] = [
                s.model_dump() for s in req.prompt_section_sizes
            ]
        task.api_call_summary = summary

    # Per-task structured-output format-compliance telemetry (equipping
    # direction d-46468a41), sibling to api_call_summary. Cumulative counters
    # posted on every heartbeat — last write wins. Backwards-compatible: older
    # agents that don't ship these fields leave format_compliance_summary NULL.
    if (
        req.format_miss_by_reason is not None
        or req.format_repair_miss_by_reason is not None
        or req.format_error_total is not None
        or req.format_repair_attempts is not None
        or req.format_repair_successes is not None
        or req.format_miss_by_schema is not None
        or req.format_repair_attempts_by_schema is not None
        or req.format_repair_successes_by_schema is not None
        or req.format_repair_infra_failures is not None
        or req.format_repair_infra_failures_by_schema is not None
        or req.format_repair_input_tokens is not None
        or req.format_repair_output_tokens is not None
        or req.format_repair_input_tokens_by_schema is not None
        or req.format_repair_output_tokens_by_schema is not None
        or req.format_repair_miss_by_schema is not None
        or req.format_miss_by_schema_and_reason is not None
    ):
        task.format_compliance_summary = {
            "format_miss_by_reason": {
                str(reason): int(count)
                for reason, count in (req.format_miss_by_reason or {}).items()
            },
            "format_repair_miss_by_reason": {
                str(reason): int(count)
                for reason, count in (req.format_repair_miss_by_reason or {}).items()
            },
            "format_error_total": int(req.format_error_total or 0),
            "format_repair_attempts": int(req.format_repair_attempts or 0),
            "format_repair_successes": int(req.format_repair_successes or 0),
            "format_miss_by_schema": {
                str(schema): int(count)
                for schema, count in (req.format_miss_by_schema or {}).items()
            },
            "format_repair_attempts_by_schema": {
                str(schema): int(count)
                for schema, count in (
                    req.format_repair_attempts_by_schema or {}
                ).items()
            },
            "format_repair_successes_by_schema": {
                str(schema): int(count)
                for schema, count in (
                    req.format_repair_successes_by_schema or {}
                ).items()
            },
            "format_repair_infra_failures": int(req.format_repair_infra_failures or 0),
            "format_repair_infra_failures_by_schema": {
                str(schema): int(count)
                for schema, count in (
                    req.format_repair_infra_failures_by_schema or {}
                ).items()
            },
            "format_repair_input_tokens": int(req.format_repair_input_tokens or 0),
            "format_repair_output_tokens": int(req.format_repair_output_tokens or 0),
            "format_repair_input_tokens_by_schema": {
                str(schema): int(count)
                for schema, count in (
                    req.format_repair_input_tokens_by_schema or {}
                ).items()
            },
            "format_repair_output_tokens_by_schema": {
                str(schema): int(count)
                for schema, count in (
                    req.format_repair_output_tokens_by_schema or {}
                ).items()
            },
            "format_repair_miss_by_schema": {
                str(schema): int(count)
                for schema, count in (req.format_repair_miss_by_schema or {}).items()
            },
            "format_miss_by_schema_and_reason": {
                str(schema): {
                    str(reason): int(count)
                    for reason, count in reasons.items()
                }
                for schema, reasons in (
                    req.format_miss_by_schema_and_reason or {}
                ).items()
            },
            "last_updated_at": _utcnow().isoformat(),
        }

    # Record VM image identity so the orchestrator can detect stale Packer images.
    # Called on every heartbeat that carries image fields; no-ops when both are None.
    if req.vm_image_name is not None or req.vm_image_build_ts is not None:
        from app.compute import record_vm_image

        record_vm_image(
            token.get("vm_instance_id"), req.vm_image_name, req.vm_image_build_ts
        )

    # Record agent runtime fingerprint for stale-image diagnostics. Distinct from
    # image identity: two images with the same build_ts may ship different agent code.
    if req.agent_runtime_fingerprint is not None:
        from app.compute import record_vm_runtime_fingerprint

        record_vm_runtime_fingerprint(
            token.get("vm_instance_id"), req.agent_runtime_fingerprint
        )

    # Record the source git SHA baked into the image so a running VM maps back to
    # its exact build commit. Additive provenance; no-ops when the field is absent
    # (older images / local dev that never baked the AGENT_SOURCE_SHA marker).
    if req.agent_source_sha is not None:
        from app.compute import record_vm_source_sha

        record_vm_source_sha(token.get("vm_instance_id"), req.agent_source_sha)

    # Mirror the heartbeat-reported image identity onto the durable PoolVm row
    # when this VM is a warm-pool member, so fleet / release-artifact checks
    # survive an orchestrator restart (the in-memory caches above do not). No-op
    # for non-pool task VMs (persist_pool_vm_image_state returns False when there
    # is no PoolVm row). Gated on is_warm_pool_enabled() so a dark pool issues no
    # extra query; shares the task heartbeat's session + commit below.
    from app.warm_pool import is_warm_pool_enabled

    if is_warm_pool_enabled():
        from app.compute import persist_pool_vm_image_state

        _img_kwargs: dict = {}
        if req.vm_image_name is not None or req.vm_image_build_ts is not None:
            _img_kwargs["image_name"] = req.vm_image_name
            _img_kwargs["build_ts"] = req.vm_image_build_ts
        if req.agent_runtime_fingerprint is not None:
            _img_kwargs["runtime_fingerprint"] = req.agent_runtime_fingerprint
        if req.agent_source_sha is not None:
            _img_kwargs["source_sha"] = req.agent_source_sha
        if _img_kwargs:
            await persist_pool_vm_image_state(
                session, token.get("vm_instance_id"), **_img_kwargs
            )

    session.add(task)
    await session.commit()

    return {"status": "ok"}


@router.post("/spawn")
@limiter.limit("150/minute")
async def spawn_subtask(
    request: Request,
    req: SpawnTaskRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Endpoint for agents to request the creation of a new sub-task/VM.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # Get parent task
    parent_task = await session.get(Task, req.task_id)
    if not parent_task:
        raise HTTPException(status_code=404, detail="Parent task not found")

    # Depth guard: prevent unbounded recursive spawning
    MAX_SPAWN_DEPTH = 5
    spawn_depth = (req.context or {}).get("spawn_depth", 0)
    if not spawn_depth:
        # Infer depth from parent chain
        depth = 0
        current = parent_task
        while current.parent_task_id and depth < MAX_SPAWN_DEPTH:
            current = await session.get(Task, current.parent_task_id)
            if not current:
                break
            depth += 1
        spawn_depth = depth
    if spawn_depth >= MAX_SPAWN_DEPTH:
        raise HTTPException(
            status_code=400, detail=f"Spawn depth limit ({MAX_SPAWN_DEPTH}) exceeded"
        )

    # Plan-spawned children are created by _handle_plan_completion AFTER branch creation,
    # so they get the correct base_branch. Don't pre-spawn them here.
    if (req.context or {}).get("agent_spawned"):
        return {
            "message": "Plan children deferred to post-branch-creation",
            "task_ids": [],
            "requires_approval": False,
        }

    # Strip orchestrator-internal keys before merging into the new task's
    # context. An agent JWT-holder MUST NOT be able to forge a warm-VM pin
    # that hijacks another VM's chain. Log a warning when this happens so
    # we can spot the attack pattern in production.
    raw_context = req.context or {}
    forbidden_present = [
        k for k in raw_context if k in _AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS
    ]
    if forbidden_present:
        logger.warning(
            "[SPAWN] Stripped forbidden context keys from agent spawn: parent_task=%s keys=%s",
            parent_task.id,
            forbidden_present,
        )
    sanitized_context = {
        k: v
        for k, v in raw_context.items()
        if k not in _AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS
    }

    new_tasks = []
    for payload in req.payloads:
        try:
            payload = validate_task_payload(payload)
        except ValueError as exc:
            logger.warning(
                "Validation error in task payload: %s: %s", type(exc).__name__, exc
            )
            raise HTTPException(status_code=422, detail="Invalid request") from exc
        parent_payload = parent_task.first_payload
        parent_context = {
            "parent_task_id": str(parent_task.id),
            "parent_title": parent_task.title,
            "parent_prompt": (
                parent_payload.get("prompt") or parent_payload.get("description") or ""
            )[:500],
        }
        if payload.get("type") == "compute_experiment":
            try:
                spend_cap = float(payload.get("spend_cap_usd") or 0)
                if spend_cap <= 0:
                    raise ValueError("spend_cap_usd must be positive")
                task_requires_approval, _ = resolve_compute_spend_approval(
                    runtime_config=_shared_state.runtime_config,
                    direction=None,
                    spend_cap_usd=spend_cap,
                )
            except (TypeError, ValueError):
                task_requires_approval = True
        else:
            task_requires_approval = req.requires_approval
        new_task = Task(
            project_id=parent_task.project_id,
            title=payload.get("title", f"Subtask of {parent_task.title}"),
            description=payload.get("description", ""),
            payloads=[payload],
            context={
                **sanitized_context,
                "spawn_depth": spawn_depth + 1,
                "parent_context": parent_context,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            # Fixed value — parent linkage lives on parent_task_id, no need
            # to encode parent UUID into the created_by string. The CHECK
            # constraint chk_task_created_by (alembic 0065) only allows the
            # canonical set, of which "AGENT" is the spawn-by-running-agent member.
            created_by="AGENT",
            requires_approval=task_requires_approval,
            parent_task_id=parent_task.id,
            **({"priority": req.priority} if req.priority is not None else {}),
        )
        session.add(new_task)
        new_tasks.append(new_task)

    await session.commit()

    # Log history for each new task
    # Batch-reload all new tasks (N+1 guard: commit expired the session;
    # one batch SELECT + identity-map lookups replaces N individual
    # session.refresh calls).
    if new_tasks:
        await session.execute(
            select(Task).where(Task.id.in_([t.id for t in new_tasks]))
        )
    for task in new_tasks:
        fresh_task = await session.get(Task, task.id)
        history = TaskHistory(
            task_id=fresh_task.id if fresh_task else task.id,
            action="SPAWNED",
            details={
                "spawned_by": f"AGENT_{parent_task.id}",
                "requires_approval": fresh_task.requires_approval if fresh_task else task.requires_approval,
                "context": req.context,
            },
        )
        session.add(history)

    await session.commit()

    return {
        "message": f"Successfully spawned {len(new_tasks)} subtasks",
        "task_ids": [str(t.id) for t in new_tasks],
        "requires_approval": req.requires_approval,
    }


# ---------------------------------------------------------------------------
# Strategic-context fetch endpoints
# ---------------------------------------------------------------------------
# The read-only strategic direction/playbook/gap fetch endpoints now live in
# their own seam: app.routers.agent_strategic_fetch (registered under the same
# /api/agent prefix in app.main). They share auth via verify_agent_token,
# which stays here.


# Recognized recovery-origin flavors an agent may stamp on a merged SR outcome.
# When an agent omits ``reviewer_recovery_origin`` the operator-requested path
# defaults to ``operator_requested_recovery`` (byte-for-byte legacy behavior);
# a self-merge from the enabled (non-recovery) path leaves no recovery origin.
_VALID_REVIEWER_RECOVERY_ORIGINS = frozenset(
    {
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    }
)


class _ReviewerFinding(BaseModel):
    model_config = ConfigDict(extra="allow")
    category: str | None = Field(None, max_length=100)
    severity: str | None = Field(None, max_length=20)
    description: str | None = Field(None, max_length=2000)


class ReviewerMergeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    reviewer_findings: list[_ReviewerFinding] = Field(default=[], max_length=100)
    reviewer_edges_considered: list[dict] = Field(default=[], max_length=50)
    # Optional recovery-origin flavor the agent computed in-VM. Absent for the
    # legacy operator-requested request shape (defaults preserve that path).
    reviewer_recovery_origin: str | None = None


@router.post("/tasks/{original_task_id}/reviewer-merge")
@limiter.limit("30/minute")
async def merge_operator_reviewer_findings(
    request: Request,
    original_task_id: uuid.UUID,
    req: ReviewerMergeRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Merge fresh-reviewer findings into an existing task's SR outcome.

    Two authorization paths are accepted:

    * **operator-requested** — the requesting task's
      ``context.operator_requested_review_for`` equals the target task id (an
      operator spawned a dedicated review task for another task's PR); and
    * **self-merge** — the requesting task id (token ``sub``) equals the target
      task id (a task persisting its own automatic fresh-reviewer output into
      its ``_self_review_outcome`` after the terminal status was already posted).

    Anything else is rejected with 403.
    """
    requesting_task_id = token.get("sub")
    if not requesting_task_id:
        raise HTTPException(status_code=403, detail="No task in token")

    requesting_task = await session.get(Task, uuid.UUID(requesting_task_id))
    if not requesting_task:
        raise HTTPException(status_code=404, detail="Requesting task not found")

    ctx = requesting_task.context or {}
    is_self = str(requesting_task_id) == str(original_task_id)
    is_operator = str(ctx.get("operator_requested_review_for")) == str(original_task_id)
    if not (is_self or is_operator):
        raise HTTPException(
            status_code=403, detail="Not authorized for this target task"
        )

    original_task = await session.get(Task, original_task_id)
    if not original_task:
        raise HTTPException(status_code=404, detail="Original task not found")

    # Resolve the effective recovery origin. A request that supplies a recognized
    # flavor wins; otherwise the operator path defaults to
    # ``operator_requested_recovery`` (legacy behavior) and a non-operator
    # self-merge carries no recovery origin (plain reviewer / enabled path).
    recovery_origin = req.reviewer_recovery_origin
    if recovery_origin not in _VALID_REVIEWER_RECOVERY_ORIGINS:
        recovery_origin = "operator_requested_recovery" if is_operator else None

    orig_ctx = dict(original_task.context or {})
    sr = dict(orig_ctx.get("_self_review_outcome") or {})
    findings_dicts = [f.model_dump() for f in req.reviewer_findings]
    sr["reviewer_findings"] = findings_dicts
    if recovery_origin:
        sr["reviewer_recovery_origin"] = recovery_origin

    check_results = dict(safe_check_results(sr))
    for finding in findings_dicts:
        cat = finding.get("category")
        if cat:
            existing = check_results.get(cat)
            if not existing or existing.get("passed") is True:
                if recovery_origin:
                    finding_origin = recovery_origin
                elif existing and existing.get("passed") is True:
                    finding_origin = "reviewer_disagreement"
                else:
                    finding_origin = "reviewer"
                check_results[cat] = {
                    "passed": False,
                    "issue": f"[reviewer] {finding.get('description', '')}",
                    "origin": finding_origin,
                }
    sr["check_results"] = check_results

    if req.reviewer_edges_considered:
        sr["reviewer_edges_considered"] = req.reviewer_edges_considered

    orig_ctx["_self_review_outcome"] = sr
    original_task.context = orig_ctx
    flag_modified(original_task, "context")
    session.add(original_task)
    await session.commit()

    await broadcast_to_project(
        str(original_task.project_id),
        "task_updated",
        {"task_id": str(original_task_id)},
    )
    return {"status": "merged", "finding_count": len(req.reviewer_findings)}


# ---------------------------------------------------------------------------
# Agent-facing knowledge-document query/read API
#
# Closes the loop on the knowledge store: web_research tasks produce durable
# KnowledgeDocument rows, and these endpoints let an authenticated agent query
# and read them so accumulated knowledge can feed back into the system. The
# project scope is derived from the AUTHENTICATED TASK (token's ``sub`` →
# task.project_id), never from the URL/query — an agent can only ever see its
# own project's documents. Search/serialization is shared with the operator
# admin endpoints via ``app.knowledge_query`` so behavior is identical.
# ---------------------------------------------------------------------------


async def _resolve_token_project_id(session: AsyncSession, token: dict) -> uuid.UUID:
    """Resolve the authenticated task's project_id from the agent token.

    The agent never supplies a project_id; scope is bound to the task the token
    was minted for. A missing/unparseable task id is a 404 (the token is no
    longer usable for project-scoped reads).
    """
    task_id = token.get("sub")
    try:
        task = await session.get(Task, uuid.UUID(str(task_id)))
    except (ValueError, TypeError):
        task = None
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.project_id


@router.get("/knowledge-documents")
@limiter.limit("120/minute")
async def agent_list_knowledge_documents(
    request: Request,
    q: str | None = Query(default=None, description="Full-text search term"),
    topic: str | None = Query(default=None, description="Exact topic filter"),
    tag: str | None = Query(default=None, description="Exact tag filter"),
    direction: str | None = Query(
        default=None, description="Exact direction_id filter"
    ),
    source_kind: str | None = Query(
        default=None, description="Exact source_kind filter"
    ),
    origin_state: str | None = Query(
        default=None, description="Exact origin provenance state filter"
    ),
    challenge_state: str | None = Query(
        default=None, description="Exact origin challenge state filter"
    ),
    finding_hash: str | None = Query(
        default=None, description="Exact originating research finding hash filter"
    ),
    sweep_id: str | None = Query(
        default=None, description="Exact sweep_id filter (compute experiment sweeps)"
    ),
    lifecycle_status: str | None = Query(
        default=None, description="Filter by lifecycle status: pending, consumed, dismissed"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """List / search knowledge documents for the authenticated task's project.

    Mirrors the operator list endpoint (``q`` over title/summary/body/topic/tags,
    exact ``topic`` / ``tag`` / ``direction`` / ``source_kind`` filters,
    newest-first, limit/offset paginated) but scopes to the token's task project
    — never a URL-supplied project — so an agent cannot read another project's
    documents. The list shape omits the markdown body; use get-by-id for the
    full document. The new ``direction`` / ``source_kind`` / ``sweep_id`` filters
    are additive: omitting them preserves the prior behavior and response shape
    exactly.
    """
    project_id = await _resolve_token_project_id(session, token)
    return await kq_list_knowledge_documents(
        session,
        project_id,
        q=q,
        topic=topic,
        tag=tag,
        direction=direction,
        source_kind=source_kind,
        origin_state=origin_state,
        challenge_state=challenge_state,
        finding_hash=finding_hash,
        sweep_id=sweep_id,
        lifecycle_status=lifecycle_status,
        limit=limit,
        offset=offset,
    )


@router.get("/knowledge-documents/{document_id}")
@limiter.limit("120/minute")
async def agent_get_knowledge_document(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single knowledge document (including its markdown body).

    Scoped to the authenticated task's project: a document from another project
    (or a missing id) is a 404.
    """
    project_id = await _resolve_token_project_id(session, token)
    doc = await kq_get_knowledge_document(session, project_id, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return serialize_knowledge_document(doc, include_body=True)


# ---------------------------------------------------------------------------
# Agent build-config command write-back
# ---------------------------------------------------------------------------


class _ConfirmedCommandEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(..., min_length=1)
    confirmed_lint_cmd: str | None = None
    confirmed_test_cmd: str | None = None
    confirmed_build_cmd: str | None = None
    confirmed_install_cmd: str | None = None


class _BuildConfigCommandsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    entries: list[_ConfirmedCommandEntry]


@router.patch("/projects/{project_id}/build-config/commands")
@limiter.limit("30/minute")
async def agent_write_back_build_config_commands(
    request: Request,
    project_id: uuid.UUID,
    payload: _BuildConfigCommandsRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent write-back endpoint for confirmed lint/build/test/install commands.

    Merges agent-verified commands into the per-directory confirmed_* fields
    without overwriting operator-set lint_cmd / test_cmd / build_cmd / install_cmd.
    Sets commands_confirmed_at (UTC ISO) and confirming_task_id on each
    updated entry.
    """
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await session.get(Project, project_id)
    if not project or str(project.id) != str(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if str(task.project_id) != str(project_id):
        raise HTTPException(status_code=403, detail="Task does not belong to project")

    from app.build_config_detection import _normalize_dir_path

    existing = dict(project.build_config or {})
    directories = [dict(d) for d in (existing.get("directories") or [])]
    index_by_path = {
        _normalize_dir_path(d.get("path")): i for i, d in enumerate(directories)
    }

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    task_id_str = str(payload.task_id)
    updated_paths: list[str] = []

    def _confirmed_command(value: str | None) -> str | None:
        if isinstance(value, str) and value.strip():
            return value
        return None

    for entry in payload.entries:
        norm = _normalize_dir_path(entry.path)
        confirmed_values = {
            "confirmed_lint_cmd": _confirmed_command(entry.confirmed_lint_cmd),
            "confirmed_test_cmd": _confirmed_command(entry.confirmed_test_cmd),
            "confirmed_build_cmd": _confirmed_command(entry.confirmed_build_cmd),
            "confirmed_install_cmd": _confirmed_command(entry.confirmed_install_cmd),
        }
        has_confirmed = any(value is not None for value in confirmed_values.values())
        if norm not in index_by_path:
            # Unknown path — create a new directory entry when the agent has
            # confirmed a non-empty command.  This is the empty-detection case
            # where bootstrap discovers a real test command but detection found
            # no directories. Only confirmed_* fields are written; detected
            # test_cmd/build_cmd/install_cmd stay absent on the new entry.
            if not has_confirmed:
                continue
            new_entry: dict = {"path": norm}
            for key, value in confirmed_values.items():
                if value is not None:
                    new_entry[key] = value
            new_entry["commands_confirmed_at"] = now_iso
            new_entry["confirming_task_id"] = task_id_str
            index_by_path[norm] = len(directories)
            directories.append(new_entry)
            updated_paths.append(norm)
            continue
        if not has_confirmed:
            continue
        idx = index_by_path[norm]
        dir_entry = directories[idx]
        for key, value in confirmed_values.items():
            if value is not None:
                dir_entry[key] = value
        dir_entry["commands_confirmed_at"] = now_iso
        dir_entry["confirming_task_id"] = task_id_str
        directories[idx] = dir_entry
        updated_paths.append(norm)

    existing["directories"] = directories
    # Stamp a top-level write-back marker so repo_bootstrap completion can
    # verify command-memory success without re-scanning every directory entry.
    # The per-directory confirming_task_id stamps above remain the durable
    # per-entry provenance; this marker is the completion-handler evidence that
    # *this* task actually persisted confirmed commands.
    if updated_paths:
        existing["last_command_writeback_at"] = now_iso
        existing["last_command_writeback_task_id"] = task_id_str
        existing["last_command_writeback_paths"] = updated_paths
    project.build_config = existing
    flag_modified(project, "build_config")
    await session.commit()

    return {"updated_paths": updated_paths, "commands_confirmed_at": now_iso}


@router.post("/build-config-discovery")
@limiter.limit("30/minute")
async def agent_build_config_discovery(
    request: Request,
    body: BuildDiscoveryReport,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Persist agent-discovered build commands into project build_config.

    Called by the agent VM near task end when it has discovered runnable
    test/lint/build/install commands from a full repo clone. Uses FILL-ONLY
    semantics — never overwrites existing operator-set values. The call is
    advisory: a malformed or no-evidence report produces a clean 200/no-op.
    """
    try:
        project_id = await _resolve_token_project_id(session, token)
        project = await session.get(Project, project_id)
        if project is None:
            return {"persisted": False}

        from app.build_config_detection import persist_discovered_build_commands

        persisted = await persist_discovered_build_commands(
            project,
            session,
            [d.model_dump() for d in body.discovered_directories],
        )
        return {"persisted": persisted}
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "[BUILD_CONFIG] build-config-discovery endpoint error for token sub=%s",
            token.get("sub"),
            exc_info=True,
        )
        return {"persisted": False}
