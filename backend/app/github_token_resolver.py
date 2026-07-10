"""GitHub per-task token resolution and in-process token cache.

Extracted from ``app.routers.agent`` (which had previously inherited it from
``app.main``) so that the 15+ non-router modules that resolve a GitHub token
no longer transitively depend on the entire agent FastAPI router. The router
re-exports these symbols unchanged, so every existing
``from app.routers.agent import get_github_token_for_task`` (and friends)
keeps working.

This module is intentionally router-free.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.shared_state as _shared_state
from app.field_encryption import decrypt_field
from app.github import generate_installation_access_token
from app.models import Project, Task, UserIntegration, _utcnow
from app.secrets_manager import get_user_secret

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub token cache (moved from main.py -> routers/agent.py -> here)
# ---------------------------------------------------------------------------

_github_token_cache: Dict[
    str, Dict
] = {}  # project_id -> {"token", "owner", "repo", "fetched_at"}
_github_token_cache_lock = (
    asyncio.Lock()
)  # Prevent concurrent cache miss → duplicate token generation
_GITHUB_TOKEN_TTL = 3000  # 50 minutes (installation tokens expire after 60min)


def invalidate_github_token_cache(project_id: str) -> None:
    """Remove a project's cached GitHub token, forcing re-generation on next use."""
    removed = _github_token_cache.pop(project_id, None)
    if removed:
        logger.info(f"[GITHUB] Invalidated cached token for project {project_id}")


async def get_github_token_for_task(
    task_id: uuid.UUID,
    session: AsyncSession,
    force_refresh: bool = False,
) -> tuple[str, str, str]:
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_id_str = str(task.project_id)

    # Return cached token if still fresh (installation tokens expire after 60min)
    if not force_refresh:
        async with _github_token_cache_lock:
            cached = _github_token_cache.get(project_id_str)
            if (
                cached
                and (_utcnow() - cached["fetched_at"]).total_seconds()
                < _GITHUB_TOKEN_TTL
            ):
                return cached["token"], cached["owner"], cached["repo"]

    project_stmt = select(Project).where(Project.id == task.project_id)
    project = (await session.execute(project_stmt)).scalars().first()
    if not project or not project.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")

    if not project.github_owner or not project.github_repo:
        raise HTTPException(
            status_code=403,
            detail="Project does not have an authorized GitHub repository",
        )

    integration_stmt = select(UserIntegration).where(
        UserIntegration.user_id == project.user_id
    )
    integration = (await session.execute(integration_stmt)).scalars().first()

    # Prefer dynamic App-level discovery — always finds the correct installation
    # for this specific repo, even with multiple installations across orgs.
    token = None
    from app.github import get_repo_installation_token

    try:
        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )
    except Exception:  # noqa: BLE001 — fall through to stored-installation + PAT fallbacks
        logger.debug(
            f"[GITHUB] App-level discovery failed for {project.github_owner}/{project.github_repo}, trying fallbacks"
        )

    if not token and integration and integration.github_installation_id:
        try:
            token = await generate_installation_access_token(
                integration.github_installation_id, repositories=[project.github_repo]
            )
        except Exception:  # noqa: BLE001 — fall through to PAT fallback
            logger.debug(
                f"[GITHUB] Stored installation token failed for {project.github_owner}/{project.github_repo}"
            )

    if not token and integration and integration.github_pat:
        decrypted_pat_ref = decrypt_field(integration.github_pat)
        token = await get_user_secret(decrypted_pat_ref)

    if not token:
        raise HTTPException(
            status_code=400,
            detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat.",
        )

    # Track successful GitHub API interaction for passive health checking
    _shared_state.last_github_success = _utcnow()

    # Cache token to avoid re-generating for every API call within the same project
    async with _github_token_cache_lock:
        _github_token_cache[project_id_str] = {
            "token": token,
            "owner": project.github_owner,
            "repo": project.github_repo,
            "fetched_at": _utcnow(),
        }
        # Evict stale entries
        stale_cutoff = _utcnow() - timedelta(seconds=_GITHUB_TOKEN_TTL + 600)
        for k in [
            k for k, v in _github_token_cache.items() if v["fetched_at"] < stale_cutoff
        ]:
            _github_token_cache.pop(k, None)

    return token, project.github_owner, project.github_repo
