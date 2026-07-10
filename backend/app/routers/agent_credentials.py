"""Agent-facing credential rotation endpoint.

Some CLIs (currently Codex) refresh their OAuth tokens in-place during a run
by rewriting `auth.json`. Without propagating the refreshed token back to
the backend, every subsequent task re-pushes the original (possibly now-
rotated) refresh token and may hit `invalid_grant`.

This module exposes POST /api/agent/credentials/rotate for running agents to
propagate a refreshed secret. It is provider-gated: only openai/oauth rotates
today. Calls for other provider/auth-mode combinations return a no-op so the
agent can invoke unconditionally.

Concurrency: two agents rotating the same account will last-write-win. Refreshed
tokens for the same account are interchangeable from the upstream provider's
perspective, so this is acceptable.

Kept separate from routers/agent.py to keep that file under its size guardrail.
"""

import logging
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount, Project, Task
from app.routers.agent import verify_agent_token
from app.shared_state import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/credentials/rotate")
@limiter.limit("30/minute")
async def rotate_agent_credentials(
    request: Request,
    payload: Dict,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    from app.max_plan_pool import (
        get_task_llm_account_id,
        get_task_llm_auth_mode,
        get_task_llm_provider,
    )
    from app.providers.registry import get_provider
    from app.field_encryption import encrypt_field
    from app.secrets_manager import store_user_secret
    from app.warm_pool import get_pool_vm

    task_id = token.get("sub")
    raw_secret = payload.get("secret")
    if not isinstance(raw_secret, str) or not raw_secret.strip():
        raise HTTPException(status_code=400, detail="'secret' is required")

    if task_id:
        task = await session.get(Task, uuid.UUID(task_id))
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        project_id = task.project_id
        account_id = get_task_llm_account_id(task)
        provider_raw = get_task_llm_provider(task)
        auth_mode_raw = get_task_llm_auth_mode(task)
        rotation_source = f"task {task_id}"
    else:
        vm_instance_id = token.get("vm_instance_id")
        if not isinstance(vm_instance_id, str) or not vm_instance_id.strip():
            raise HTTPException(
                status_code=400, detail="Token missing vm_instance_id"
            )
        member = await get_pool_vm(session, vm_instance_id)
        if not member:
            raise HTTPException(status_code=404, detail="Pool VM not found")
        token_project_id = token.get("project_id")
        if not token_project_id:
            raise HTTPException(
                status_code=401, detail="vm_scoped_token_missing_project"
            )
        if str(member.project_id) != str(token_project_id):
            raise HTTPException(status_code=403, detail="Pool VM project mismatch")

        project_id = member.project_id
        account_id = member.account_id
        provider_raw = member.provider
        auth_mode_raw = member.auth_mode
        rotation_source = f"pool VM {vm_instance_id}"

    if not account_id or not provider_raw or not auth_mode_raw:
        context_kind = "task" if task_id else "pool VM"
        return {
            "rotated": False,
            "reason": f"{context_kind} has no LLM runtime context",
        }

    try:
        provider = LLMProviderName(provider_raw)
        auth_mode = LLMAuthMode(auth_mode_raw)
    except ValueError:
        return {
            "rotated": False,
            "reason": f"unknown provider/auth_mode {provider_raw}/{auth_mode_raw}",
        }

    # Only openai/oauth rotates on the agent today. Anthropic OAuth tokens
    # don't auto-refresh in Claude Code CLI, and Gemini SA has no refresh
    # mechanism. Silent no-op so the agent can call unconditionally.
    if provider != LLMProviderName.OPENAI or auth_mode != LLMAuthMode.OAUTH:
        return {
            "rotated": False,
            "reason": "provider/auth_mode does not support agent-side rotation",
        }

    try:
        account_uuid = (
            account_id
            if isinstance(account_id, uuid.UUID)
            else uuid.UUID(str(account_id))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid LLM account id") from exc

    account = await session.get(MaxPlanAccount, account_uuid)
    if not account:
        raise HTTPException(status_code=404, detail="LLM account not found")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if account.user_id != project.user_id:
        raise HTTPException(status_code=403, detail="LLM account project mismatch")
    try:
        account_provider = LLMProviderName(
            getattr(account.provider, "value", str(account.provider))
        )
        account_auth_mode = LLMAuthMode(
            getattr(account.auth_mode, "value", str(account.auth_mode))
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="LLM account provider/auth_mode mismatch",
        ) from exc
    if account_provider != provider or account_auth_mode != auth_mode:
        raise HTTPException(
            status_code=409,
            detail="LLM account provider/auth_mode mismatch",
        )

    # Reuse the provider's normal validator — rotated secrets must pass the
    # same shape check as operator-supplied ones. If Codex ever writes a
    # malformed auth.json, we'd rather reject here than corrupt the stored
    # credential for future tasks.
    provider_impl = get_provider(provider)
    try:
        result = provider_impl.validate_secret(
            raw_secret.strip(),
            auth_mode,
            account.provider_metadata or {},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid rotated secret: {exc}"
        ) from exc

    secret_name = f"llm-account-{account.id}"
    try:
        new_ref = await store_user_secret("system", secret_name, result.secret)
    except Exception as exc:
        logger.warning(
            f"[ROTATE] Secret Manager unavailable for account {account.id}: {exc}"
        )
        raise HTTPException(
            status_code=503,
            detail="Secret rotation temporarily unavailable, retry shortly",
        ) from exc

    account.secret_blob_ref = encrypt_field(new_ref)
    if result.provider_metadata:
        account.provider_metadata = result.provider_metadata
    session.add(account)
    await session.commit()

    # Update in-memory pool so the next dispatch sees the refreshed secret
    # without waiting for a pool rebuild. Guarded by the pool lock to avoid
    # racing with dispatch reads.
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    async with _max_plan_pool_lock:
        entry = _account_by_id.get(str(account.id))
        if entry is not None:
            entry["secret_blob_ref"] = new_ref
            entry["runtime_secret"] = result.secret
            entry["runtime_ready"] = True

    logger.info(
        f"[ROTATE] Refreshed {provider.value}/{auth_mode.value} secret for account {account.id} via {rotation_source}"
    )
    return {"rotated": True}
