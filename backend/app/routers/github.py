"""GitHub OAuth, PAT registration, and webhook endpoints."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import get_current_user
from app.database import get_session
from app.github import (
    GITHUB_CLIENT_ID,
    GITHUB_WEBHOOK_SECRET,
    exchange_oauth_code,
    get_github_user,
    get_user_installations,
    validate_fine_grained_pat,
)
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus, UserIntegration
from app.secrets_manager import store_user_secret
from app.shared_state import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["github"])

# Webhook dedup: track recent GitHub delivery IDs to prevent duplicate processing
_webhook_delivery_ids: OrderedDict = OrderedDict()  # id -> timestamp (in-memory fallback)
_webhook_dedup_lock = asyncio.Lock()
_WEBHOOK_DEDUP_MAX = 1000
_WEBHOOK_DEDUP_TTL = 600  # 10 minutes


def _utcnow():
    from app.main import _utcnow as _main_utcnow
    return _main_utcnow()


class GitHubPATInstallRequest(BaseModel):
    pat: str = Field(..., min_length=10)


@router.post("/pat")
@limiter.limit("5/minute")
async def register_github_pat(
    request: Request,
    req: GitHubPATInstallRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Registers a GitHub Fine-Grained PAT for the current user.
    """
    # Validate PAT
    await validate_fine_grained_pat(req.pat)

    # Store the PAT securely in GCP Secret Manager
    try:
        secret_name = await store_user_secret(current_user["user_id"], "github-pat", req.pat)
    except Exception as e:
        logger.warning(f"[PAT] Secret Manager unavailable, storing PAT directly in DB: {e}")
        secret_name = req.pat  # Fallback: store raw token in DB

    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()

    if integration:
        integration.github_pat = secret_name
        integration.updated_at = _utcnow()
    else:
        integration = UserIntegration(
            user_id=current_user["user_id"],
            github_pat=secret_name
        )

    session.add(integration)
    await session.commit()

    return {"status": "success", "message": "GitHub PAT registered successfully."}

@router.get("/oauth/authorize")
@limiter.limit("10/minute")
async def github_oauth_authorize(
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Returns the GitHub OAuth URL. The frontend redirects the user there.
    A signed state token ties the OAuth flow to the authenticated user.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")

    # Sign a state token so the callback can verify which user initiated the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    state = pyjwt.encode(
        {"sub": current_user["user_id"], "exp": _utcnow() + timedelta(minutes=10), "type": "github_oauth"},
        AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
    )

    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "")
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        f"&allow_signup=false"
    )
    if orchestrator_url:
        from urllib.parse import quote
        url += f"&redirect_uri={quote(orchestrator_url + '/api/github/oauth/callback', safe='')}"
    return {"url": url}


@router.get("/oauth/callback")
@limiter.limit("10/minute")
async def github_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """
    GitHub OAuth callback. Exchanges the code for a user token, verifies the user's
    installations, and auto-registers them. This proves the user actually owns the
    GitHub account — no spoofing possible.
    """
    admin_url = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")

    # Verify the state token to identify the Firebase user who started the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    try:
        state_payload = pyjwt.decode(state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if state_payload.get("type") != "github_oauth":
            return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")
        firebase_user_id = state_payload["sub"]
    except pyjwt.ExpiredSignatureError:
        return RedirectResponse(url=f"{admin_url}?github_error=state_expired")
    except pyjwt.PyJWTError:
        return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")

    try:
        # Exchange code for GitHub user access token
        token_data = await exchange_oauth_code(code)
        user_token = token_data["access_token"]

        # Get the GitHub user's profile to confirm identity
        await get_github_user(user_token)

        # Discover which of this App's installations the user can access
        installations = await get_user_installations(user_token)
        if not installations:
            return RedirectResponse(url=f"{admin_url}?github_error=no_installations")

        # Store the first installation ID; collect all account names for display.
        # Token generation uses dynamic App-level discovery per repo, so the
        # stored installation_id is a fallback, not the authority.
        installation = installations[0]
        installation_id = str(installation["id"])
        all_account_names = [inst["account"]["login"] for inst in installations]
        account_name = ", ".join(all_account_names)

        # Save to UserIntegration
        stmt = select(UserIntegration).where(UserIntegration.user_id == firebase_user_id)
        integration = (await session.execute(stmt)).scalars().first()

        if integration:
            integration.github_installation_id = installation_id
            integration.github_account_name = account_name
            integration.updated_at = _utcnow()
        else:
            integration = UserIntegration(
                user_id=firebase_user_id,
                github_installation_id=installation_id,
                github_account_name=account_name
            )

        session.add(integration)
        await session.commit()

        display_name = all_account_names[0]
        return RedirectResponse(url=f"{admin_url}?github_connected={display_name}")

    except Exception as e:
        logger.error(f"[GITHUB OAUTH] Error: {e}", exc_info=True)
        return RedirectResponse(url=f"{admin_url}?github_error=failed")

@router.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """
    Receives webhooks from the GitHub App (e.g., push, pull_request).
    """
    signature = request.headers.get("x-hub-signature-256")
    if not signature or not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Missing signature or webhook secret")

    payload = await request.body()

    # Verify signature
    expected_signature = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Dedup by GitHub delivery ID — prevents duplicate processing on webhook retries
    # Uses Redis for cross-instance dedup when available, else per-instance in-memory
    delivery_id = request.headers.get("x-github-delivery")
    if delivery_id:
        from app.redis_client import get_redis as _get_redis
        _r = await _get_redis()
        if _r:
            try:
                was_new = await _r.set(f"webhook:{delivery_id}", "1", nx=True, ex=_WEBHOOK_DEDUP_TTL)
                if not was_new:
                    logger.info(f"[WEBHOOK] Duplicate delivery {delivery_id}, skipping (Redis)")
                    return {"status": "duplicate", "delivery_id": delivery_id}
            except Exception:
                pass  # Fall through to in-memory check
        else:
            async with _webhook_dedup_lock:
                now = time.time()
                while _webhook_delivery_ids:
                    oldest_id, oldest_time = next(iter(_webhook_delivery_ids.items()))
                    if now - oldest_time > _WEBHOOK_DEDUP_TTL:
                        _webhook_delivery_ids.pop(oldest_id)
                    else:
                        break
                if delivery_id in _webhook_delivery_ids:
                    logger.info(f"[WEBHOOK] Duplicate delivery {delivery_id}, skipping")
                    return {"status": "duplicate", "delivery_id": delivery_id}
                _webhook_delivery_ids[delivery_id] = now
                if len(_webhook_delivery_ids) > _WEBHOOK_DEDUP_MAX:
                    _webhook_delivery_ids.popitem(last=False)

    event_type = request.headers.get("x-github-event")
    data = json.loads(payload)

    logger.info(f"[WEBHOOK] Received {event_type} event (delivery={delivery_id})")

    # Post-merge trigger: check if we should spawn autonomous research
    if event_type == "pull_request":
        action = data.get("action")
        merged = data.get("pull_request", {}).get("merged", False)

        if action == "closed" and merged:
            repo_full = data.get("repository", {}).get("full_name", "")
            if "/" in repo_full:
                owner, repo = repo_full.split("/", 1)
                # Verify repo belongs to a registered project
                project_check = await session.execute(
                    select(Project).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo,
                    )
                )
                if not project_check.scalars().first():
                    logger.info(f"[WEBHOOK] Ignoring event for unregistered repo {repo_full}")
                    return {"status": "ignored"}
                from app.autonomous_research import _check_autonomous_research
                background_tasks.add_task(_check_autonomous_research, owner, repo)

                # Propagate pr_merged to tasks that created this PR
                pr_number = data["pull_request"].get("number")
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact).where(
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
                        )
                    )
                    pr_artifacts = art_result.scalars().all()
                    if not pr_artifacts:
                        logger.info(f"[WEBHOOK] PR {pr_url_suffix} merged but no matching artifacts found — agent may not have reported yet")
                    affected_tasks = []
                    for art in pr_artifacts:
                        pr_task = await session.get(Task, art.task_id)
                        if pr_task and not (pr_task.context or {}).get("pr_merged"):
                            pr_task.context = {**(pr_task.context or {}), "pr_merged": True}
                            session.add(pr_task)
                            affected_tasks.append(pr_task)
                            logger.info(f"[WEBHOOK] Set pr_merged on task {pr_task.id} (PR #{pr_number})")
                    if affected_tasks:
                        await session.commit()
                        from app.rollup_management import _check_and_trigger_rollup
                        for at in affected_tasks:
                            await _check_and_trigger_rollup(at, session)

    elif event_type == 'check_run':
        action = data.get('action')
        if action != 'completed':
            return {'status': 'ignored'}

        check_run = data.get('check_run', {})
        head_sha = check_run.get('head_sha')
        repo_full = data.get('repository', {}).get('full_name', '')
        if '/' not in repo_full or not head_sha:
            return {'status': 'ignored'}

        owner, repo = repo_full.split('/', 1)

        # Find ci_waiting tasks matching this commit SHA
        ci_tasks = (await session.execute(
            select(Task).where(Task.status == TaskStatus.EVALUATING)
        )).scalars().all()

        for task in ci_tasks:
            loop = (task.context or {}).get('review_loop', {})
            if not loop.get('ci_waiting'):
                continue
            task_sha = loop.get('head_sha')
            if task_sha == head_sha:
                # Reset the polling interval to trigger immediate check on next cycle
                loop['ci_poll_interval'] = 0
                loop['ci_last_polled'] = None
                task.context = {**(task.context or {}), 'review_loop': loop}
                session.add(task)

        await session.commit()
        logger.info(f'[WEBHOOK] check_run completed for {head_sha[:8]} — reset polling for matching tasks')

    return {"status": "received"}
