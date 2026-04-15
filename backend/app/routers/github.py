"""GitHub OAuth, PAT registration, and webhook endpoints."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from collections import OrderedDict
from datetime import timedelta
from typing import Optional

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
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["github"])

# Webhook dedup: track recent GitHub delivery IDs to prevent duplicate processing
_webhook_delivery_ids: OrderedDict = OrderedDict()  # id -> timestamp (in-memory fallback)
_webhook_dedup_lock = asyncio.Lock()
_WEBHOOK_DEDUP_MAX = 1000
_WEBHOOK_DEDUP_TTL = 600  # 10 minutes


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
    redirect_uri: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """
    Returns the GitHub OAuth URL. The frontend redirects the user there.
    A signed state token ties the OAuth flow to the authenticated user.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")

    # Default to ADMIN_UI_URL if not provided, but allow dynamic override (for localhost/PR previews).
    # Validate redirect_uri against allowlist to prevent open redirect.
    default_admin_url = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
    if redirect_uri:
        from urllib.parse import urlparse
        parsed = urlparse(redirect_uri)
        allowed_hosts = {urlparse(default_admin_url).netloc, "localhost", "127.0.0.1"}
        if parsed.scheme not in ("http", "https") or parsed.netloc not in allowed_hosts:
            raise HTTPException(status_code=400, detail="Invalid redirect_uri: host not in allowlist")
        admin_url = redirect_uri
    else:
        admin_url = default_admin_url

    # Sign a state token so the callback can verify which user initiated the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    state = pyjwt.encode(
        {
            "sub": current_user["user_id"],
            "exp": _utcnow() + timedelta(minutes=10),
            "type": "github_oauth",
            "admin_url": admin_url
        },
        AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
    )

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        f"&allow_signup=false"
    )
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "").rstrip("/")
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
    # Verify the state token to identify the Firebase user who started the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    try:
        state_payload = pyjwt.decode(state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if state_payload.get("type") != "github_oauth":
            # Can't even trust the state for a redirect URL here, use fallback
            fallback_admin = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
            return RedirectResponse(url=f"{fallback_admin}?github_error=invalid_state")
        
        firebase_user_id = state_payload["sub"]
        admin_url = state_payload.get("admin_url") or os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
    except pyjwt.ExpiredSignatureError:
        fallback_admin = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
        return RedirectResponse(url=f"{fallback_admin}?github_error=state_expired")
    except pyjwt.PyJWTError:
        fallback_admin = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
        return RedirectResponse(url=f"{fallback_admin}?github_error=invalid_state")

    try:
        # Exchange code for GitHub user access token
        # redirect_uri must match what was passed in the authorize URL (if any)
        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "").rstrip("/")
        callback_uri = (orchestrator_url + "/api/github/oauth/callback") if orchestrator_url else None
        token_data = await exchange_oauth_code(code, redirect_uri=callback_uri)
        user_token = token_data["access_token"]

        # Get the GitHub user's profile to confirm identity
        await get_github_user(user_token)

        # Discover which of this App's installations the user can access
        installations = await get_user_installations(user_token)
        if not installations:
            # If no installations found, try to help the user install the app
            try:
                from app.github import get_app_info
                app_info = await get_app_info()
                app_slug = app_info.get("slug")
                if app_slug:
                    # Redirect to the app installation page
                    # We pass the state along so the user might eventually come back? 
                    # Actually GitHub App installation doesn't support state param in this way,
                    # but it will redirect back to the "Setup URL" configured in the App settings.
                    logger.info(f"[GITHUB OAUTH] No installations for user {firebase_user_id}, redirecting to app install: {app_slug}")
                    return RedirectResponse(url=f"https://github.com/apps/{app_slug}/installations/new")
            except Exception as app_err:
                logger.warning(f"Failed to fetch app info for installation redirect: {app_err}")
            
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

        display_name = urllib.parse.quote(all_account_names[0])
        return RedirectResponse(url=f"{admin_url}?github_connected={display_name}")

    except Exception as e:
        logger.error(f"[GITHUB OAUTH] Error: {e}", exc_info=True)
        return RedirectResponse(url=f"{admin_url}?github_error=failed")

@router.post("/webhook")
@limiter.limit("120/minute")
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
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery header")
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
                logger.exception(f"[WEBHOOK] Redis dedup check failed for delivery {delivery_id}; falling through to in-memory")
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
                project = project_check.scalars().first()
                if not project:
                    logger.info(f"[WEBHOOK] Ignoring event for unregistered repo {repo_full}")
                    return {"status": "ignored"}
                from app.autonomous_research import _check_autonomous_research
                background_tasks.add_task(_check_autonomous_research, owner, repo)

                # Propagate pr_merged to tasks that created this PR
                pr_number = data["pull_request"].get("number")
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact).join(Task, Artifact.task_id == Task.id).where(
                            Task.project_id == project.id,
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
                        try:
                            from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
                            for at in affected_tasks:
                                asyncio.ensure_future(dispatch_external_notification(
                                    at.project_id,
                                    "PR_MERGED",
                                    {
                                        "task_id": str(at.id),
                                        "task_title": at.title or str(at.id)[:8],
                                        "pr_number": pr_number,
                                        "repo": f"{owner}/{repo}",
                                    },
                                ))
                        except Exception as _ne:
                            logger.warning(f"[NOTIFY] PR_MERGED dispatch setup failed: {_ne}")

        elif action in ("opened", "synchronize"):
            # Auto-review: spawn a review task when a PR is opened or pushed to
            pr_data = data.get("pull_request", {})
            repo_full = data.get("repository", {}).get("full_name", "")
            pr_number = pr_data.get("number")
            if "/" in repo_full and pr_number:
                ar_owner, ar_repo = repo_full.split("/", 1)
                auto_review_project = (await session.execute(
                    select(Project).where(
                        Project.github_owner == ar_owner,
                        Project.github_repo == ar_repo,
                    )
                )).scalars().first()
                if auto_review_project:
                    auto_review_config = (auto_review_project.design_doc or {}).get("_auto_review", {})
                    if auto_review_config.get("enabled"):
                        branch = pr_data.get("head", {}).get("ref", "")
                        base_branch = pr_data.get("base", {}).get("ref", "main")
                        pr_title = pr_data.get("title", "")
                        from app.auto_review import _handle_auto_review  # noqa: PLC0415
                        background_tasks.add_task(
                            _handle_auto_review,
                            owner=ar_owner,
                            repo=ar_repo,
                            pr_number=pr_number,
                            branch=branch,
                            base_branch=base_branch,
                            pr_title=pr_title,
                            project_id=auto_review_project.id,
                            config=auto_review_config,
                        )
                        return {"status": "auto_review_triggered"}

        elif action == "closed" and not merged:
            # PR closed without merge — record as operator rejection signal
            repo_full = data.get("repository", {}).get("full_name", "")
            if "/" in repo_full:
                owner, repo = repo_full.split("/", 1)
                # Verify repo belongs to a registered project (same guard as merged branch)
                project_check = await session.execute(
                    select(Project).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo,
                    )
                )
                closed_project = project_check.scalars().first()
                if not closed_project:
                    logger.info(f"[WEBHOOK] Ignoring PR close event for unregistered repo {repo_full}")
                    return {"status": "ignored"}
                pr_number = data["pull_request"].get("number")
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact).join(Task, Artifact.task_id == Task.id).where(
                            Task.project_id == closed_project.id,
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
                        )
                    )
                    pr_artifacts = art_result.scalars().all()
                    if pr_artifacts:
                        pr_body = data.get("pull_request", {}).get("body") or ""
                        rejection_note = pr_body[:500] if pr_body else None
                        for art in pr_artifacts:
                            pr_task = await session.get(Task, art.task_id)
                            if pr_task and not (pr_task.context or {}).get("pr_rejected"):
                                update: dict = {"pr_rejected": True}
                                if rejection_note:
                                    update["pr_rejection_note"] = rejection_note
                                pr_task.context = {**(pr_task.context or {}), **update}
                                session.add(pr_task)
                                logger.info(f"[WEBHOOK] Set pr_rejected on task {pr_task.id} (PR #{pr_number})")
                        await session.commit()

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

        # Find ci_waiting tasks matching this commit SHA, scoped to this repo's project
        project_result = await session.execute(
            select(Project).where(Project.github_owner == owner, Project.github_repo == repo)
        )
        project = project_result.scalars().first()
        if not project:
            return {'status': 'ignored', 'reason': 'unregistered_repo'}

        ci_tasks = (await session.execute(
            select(Task).where(Task.status == TaskStatus.EVALUATING, Task.project_id == project.id)
        )).scalars().all()

        matching = [t for t in ci_tasks
                    if (t.context or {}).get('review_loop', {}).get('ci_waiting')
                    and (t.context or {}).get('review_loop', {}).get('head_sha') == head_sha]

        if not matching:
            # No EVALUATING tasks match — check if this is a protected-branch CI failure
            conclusion = check_run.get('conclusion')
            head_branch = (check_run.get('check_suite') or {}).get('head_branch')
            if conclusion == 'failure' and head_branch:
                # Validate branch name from webhook before passing downstream
                from app.payload_schemas import _validate_branch_name  # noqa: PLC0415
                try:
                    _validate_branch_name(head_branch)
                except Exception:
                    logger.warning(f"[WEBHOOK] Rejecting invalid head_branch: {head_branch!r}")
                    return {'status': 'ignored_invalid_branch'}
                # Read auto-remediation config from project
                auto_remediation_config = (project.design_doc or {}).get('_auto_remediation', {})
                enabled = auto_remediation_config.get('enabled', False)
                branch_whitelist = auto_remediation_config.get('branch_whitelist', ['main', 'master'])

                if not enabled or head_branch not in branch_whitelist:
                    return {'status': 'ignored_auto_remediation_disabled'}

                from app.auto_remediation import _handle_main_branch_ci_failure
                background_tasks.add_task(
                    _handle_main_branch_ci_failure,
                    owner=owner,
                    repo=repo,
                    head_sha=head_sha,
                    head_branch=head_branch,
                    project_id=project.id,
                    config=auto_remediation_config,
                )
                return {'status': 'auto_remediation_triggered'}

            return {'status': 'no_match'}

        # Fetch full check status for this SHA (1 API call covers all check runs)
        from app.github import get_check_runs as _get_check_runs
        from app.routers.agent import get_github_token_for_task
        from app.ci_completion import handle_ci_completion

        for task in matching:
            try:
                gh_token, gh_owner, gh_repo = await get_github_token_for_task(task.id, session)
                ci_result = await _get_check_runs(gh_token, gh_owner, gh_repo, head_sha, project_id=str(task.project_id))

                if ci_result['status'] != 'completed':
                    # Some checks still pending — more webhooks will come
                    logger.info(f'[WEBHOOK] check_run for {head_sha[:8]}: some checks still pending, waiting')
                    continue

                await handle_ci_completion(task, ci_result, gh_token, gh_owner, gh_repo, session, source='webhook')
                logger.info(f'[WEBHOOK] CI completed for {head_sha[:8]} — handled task {task.id} via webhook')
            except Exception as e:
                # Fallback: reduce polling interval so orchestrator picks it up soon
                logger.error(f'[WEBHOOK] Error handling CI completion for task {task.id}: {e}', exc_info=True)
                loop = (task.context or {}).get('review_loop', {})
                loop['ci_poll_interval'] = 15
                loop['ci_last_polled'] = None
                task.context = {**(task.context or {}), 'review_loop': loop}
                session.add(task)
                await session.commit()

    elif event_type == 'pull_request_review':
        action = data.get('action')
        # Only process submitted and dismissed actions; ignore edits
        if action not in ('submitted', 'dismissed'):
            return {'status': 'ignored'}

        review = data.get('review', {})
        review_state = review.get('state', '')
        pull_request_data = data.get('pull_request', {})
        pr_number = pull_request_data.get('number')
        repo_full = data.get('repository', {}).get('full_name', '')

        if not pr_number or '/' not in repo_full:
            return {'status': 'ignored'}

        owner, repo_name = repo_full.split('/', 1)
        project = (await session.execute(
            select(Project).where(
                Project.github_owner == owner,
                Project.github_repo == repo_name,
            )
        )).scalars().first()
        if not project:
            logger.info(f'[WEBHOOK] pull_request_review for unregistered repo {repo_full}')
            return {'status': 'ignored'}

        # Find original create task via the PR artifact
        pr_url_suffix = f'/{owner}/{repo_name}/pull/{pr_number}'
        art_result = await session.execute(
            select(Artifact).join(Task, Artifact.task_id == Task.id).where(
                Task.project_id == project.id,
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.ilike(f'%{pr_url_suffix}'),
            )
        )
        pr_artifacts = art_result.scalars().all()
        if not pr_artifacts:
            logger.info(f'[WEBHOOK] pull_request_review for PR {pr_url_suffix}: no matching artifact')
            return {'status': 'ignored'}

        review_user = review.get('user', {})
        is_bot_review = (
            review_user.get('type') == 'Bot'
            or str(review_user.get('login', '')).endswith('[bot]')
        )

        signals_recorded = []
        for art in pr_artifacts:
            pr_task = await session.get(Task, art.task_id)
            if not pr_task:
                continue

            blueprint_verdict = (pr_task.context or {}).get('blueprint_review_verdict')
            update: dict = {}
            signal: str | None = None
            detail: str | None = None

            if action == 'dismissed' and is_bot_review:
                # A Blueprint review was dismissed by a user
                dismissed_info = review.get('dismissed_review', {})
                dismissal_message = (dismissed_info.get('dismissal_message') or '')[:500]
                update['review_dismissed'] = True
                if dismissal_message:
                    update['review_dismissal_message'] = dismissal_message
                signal = 'review_dismissed'
                detail = dismissal_message or None
                logger.info(
                    f'[REVIEW QUALITY] PR #{pr_number}: Blueprint review dismissed '
                    f'on task {pr_task.id}'
                )

            elif action == 'submitted' and review_state == 'approved' and blueprint_verdict == 'REQUEST_CHANGES':
                # User approved after Blueprint requested changes — disagreement signal
                if not (pr_task.context or {}).get('user_overrode_review'):
                    update['user_overrode_review'] = True
                    signal = 'user_overrode_review'
                    logger.info(
                        f'[REVIEW QUALITY] PR #{pr_number}: User approved after Blueprint '
                        f'REQUEST_CHANGES on task {pr_task.id}'
                    )

            elif action == 'submitted' and review_state == 'changes_requested' and blueprint_verdict == 'APPROVE':
                # User caught issues on a PR that Blueprint approved
                if not (pr_task.context or {}).get('user_caught_issues'):
                    update['user_caught_issues'] = True
                    signal = 'user_caught_issues'
                    logger.info(
                        f'[REVIEW QUALITY] PR #{pr_number}: User caught issues after Blueprint '
                        f'APPROVE on task {pr_task.id}'
                    )

            if update:
                pr_task.context = {**(pr_task.context or {}), **update}
                session.add(pr_task)
                signals_recorded.append(signal)

        if signals_recorded:
            # Post in-app message for the first signal before committing
            try:
                from app.notification_dispatch import post_review_feedback_message  # noqa: PLC0415
                first_task_id = pr_artifacts[0].task_id
                await post_review_feedback_message(
                    session,
                    project.id,
                    pr_number=pr_number,
                    signal=signals_recorded[0],
                    detail=detail,
                    task_id=first_task_id,
                )
            except Exception as _ne:
                logger.warning(f'[REVIEW QUALITY] In-app message failed: {_ne}')
            await session.commit()
            # Fire-and-forget external notification for each signal
            try:
                from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
                for sig in signals_recorded:
                    asyncio.ensure_future(dispatch_external_notification(
                        project.id,
                        'REVIEW_FEEDBACK',
                        {
                            'pr_number': pr_number,
                            'repo': f'{owner}/{repo_name}',
                            'signal': sig,
                            'task_id': str(pr_artifacts[0].task_id),
                        },
                    ))
            except Exception as _ne:
                logger.warning(f'[REVIEW QUALITY] External notification failed: {_ne}')

    # NOTE: The 'issues' event must be enabled in the GitHub App's webhook settings.
    elif event_type == 'issues':
        action = data.get('action')
        if action == 'labeled':
            issue = data.get('issue', {})
            label = data.get('label', {})
            repo_data = data.get('repository', {})
            repo_full = repo_data.get('full_name', '')
            issue_number = issue.get('number')
            issue_title = issue.get('title', '')
            label_name = label.get('name', '')

            if '/' in repo_full and issue_number:
                owner, repo_name = repo_full.split('/', 1)
                project = (await session.execute(
                    select(Project).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo_name,
                    )
                )).scalars().first()
                if project:
                    config = (project.design_doc or {}).get('_issue_implementation', {})
                    if config.get('enabled') and label_name == config.get('trigger_label', 'blueprint:implement'):
                        from app.issue_implementation import _handle_issue_implementation
                        background_tasks.add_task(
                            _handle_issue_implementation,
                            owner=owner,
                            repo=repo_name,
                            issue_number=issue_number,
                            issue_title=issue_title,
                            project_id=project.id,
                            config=config,
                        )
                        return {'status': 'issue_implementation_triggered'}

    return {"status": "received"}
