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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import get_current_user
from app.branch_resolution import classify_rollup_merge_outcome
from app.database import get_session
from app.field_encryption import encrypt_field
from app.github import (
    GITHUB_CLIENT_ID,
    GITHUB_WEBHOOK_SECRET,
    exchange_oauth_code,
    get_github_user,
    get_user_installations,
    validate_fine_grained_pat,
)
from app.landed_signal import apply_merged_landed_signal
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
    UserIntegration,
)
from app.secrets_manager import store_user_secret
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
    limiter,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["github"])

# Webhook dedup: track recent GitHub delivery IDs to prevent duplicate processing
_webhook_delivery_ids: OrderedDict = (
    OrderedDict()
)  # id -> timestamp (in-memory fallback)
_webhook_dedup_lock = asyncio.Lock()
_WEBHOOK_DEDUP_MAX = 1000
_WEBHOOK_DEDUP_TTL = 600  # 10 minutes


async def _load_tasks_for_artifacts(
    session: AsyncSession, artifacts: list[Artifact]
) -> dict:
    task_ids = {art.task_id for art in artifacts if art.task_id is not None}
    if not task_ids:
        return {}

    task_rows = (
        await session.execute(select(Task).where(Task.id.in_(task_ids)))
    ).scalars().all()
    return {task.id: task for task in task_rows}


class _GitHubPATInstallRequest(BaseModel):
    pat: str = Field(..., min_length=10)


@router.post("/pat")
@limiter.limit("5/minute")
async def register_github_pat(
    request: Request,
    req: _GitHubPATInstallRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Registers a GitHub Fine-Grained PAT for the current user.
    """
    # Validate PAT
    await validate_fine_grained_pat(req.pat)

    # Store the PAT securely in GCP Secret Manager
    try:
        secret_name = await store_user_secret(
            current_user["user_id"], "github-pat", req.pat
        )
    except Exception as e:
        logger.error(f"[PAT] Secret Manager unavailable: {e}")
        raise HTTPException(
            status_code=503,
            detail="Secret storage temporarily unavailable, please retry",
        )

    stmt = select(UserIntegration).where(
        UserIntegration.user_id == current_user["user_id"]
    )
    integration = (await session.execute(stmt)).scalars().first()

    if integration:
        integration.github_pat = encrypt_field(secret_name)
        integration.updated_at = _utcnow()
    else:
        integration = UserIntegration(
            user_id=current_user["user_id"],
            github_pat=encrypt_field(secret_name),
        )

    session.add(integration)
    await session.commit()

    return {"status": "success", "message": "GitHub PAT registered successfully."}


@router.get("/oauth/authorize")
@limiter.limit("10/minute")
async def github_oauth_authorize(
    request: Request,
    redirect_uri: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """
    Returns the GitHub OAuth URL. The frontend redirects the user there.
    A signed state token ties the OAuth flow to the authenticated user.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)",
        )

    # Default to ADMIN_UI_URL when the caller did not pass one. A dynamic value is
    # accepted so localhost dev and PR-preview hosts can come back to themselves,
    # but it must clear an allowlist or we'd be a happy open-redirect helper.
    default_admin_url = os.getenv(
        "ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app"
    )
    admin_url = default_admin_url
    if redirect_uri is not None and redirect_uri != "":
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        allowed_hosts = {
            urllib.parse.urlparse(default_admin_url).netloc,
            "localhost",
            "127.0.0.1",
        }
        if (
            parsed_redirect.scheme not in ("http", "https")
            or parsed_redirect.netloc not in allowed_hosts
        ):
            raise HTTPException(
                status_code=400, detail="Invalid redirect_uri: host not in allowlist"
            )
        admin_url = redirect_uri

    # Sign a state token so the callback can verify which user initiated the flow.
    import jwt as pyjwt
    from app.auth import AGENT_JWT_ALGORITHM, AGENT_JWT_SECRET

    state_claims: dict[str, object] = {
        "type": "github_oauth",
        "sub": current_user["user_id"],
        "admin_url": admin_url,
        "exp": _utcnow() + timedelta(minutes=10),
    }
    state = pyjwt.encode(state_claims, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)

    auth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        # The onboarding authorization check calls GET /repos/{owner}/{repo}
        # with this user token and must see private repositories too.
        f"&scope=repo"
        f"&allow_signup=false"
    )
    orch_base = (os.getenv("ORCHESTRATOR_URL") or "").rstrip("/")
    if orch_base:
        callback = f"{orch_base}/api/github/oauth/callback"
        auth_url = f"{auth_url}&redirect_uri={urllib.parse.quote(callback, safe='')}"
    return {"url": auth_url}


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
    # Decode the state token to identify which Firebase user kicked off the flow.
    # Hoist the fallback URL up so we don't repeat the env lookup in every except.
    import jwt as pyjwt
    from app.auth import AGENT_JWT_ALGORITHM, AGENT_JWT_SECRET

    fallback_admin = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")
    try:
        state_payload = pyjwt.decode(
            state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM]
        )
    except pyjwt.ExpiredSignatureError:
        return RedirectResponse(url=f"{fallback_admin}?github_error=state_expired")
    except pyjwt.PyJWTError:
        return RedirectResponse(url=f"{fallback_admin}?github_error=invalid_state")

    if state_payload.get("type") != "github_oauth":
        return RedirectResponse(url=f"{fallback_admin}?github_error=invalid_state")
    firebase_user_id = state_payload["sub"]
    admin_url = state_payload.get("admin_url") or fallback_admin

    try:
        # GitHub requires the same redirect_uri here that was passed to /authorize.
        orch_base = (os.getenv("ORCHESTRATOR_URL") or "").rstrip("/")
        callback_uri = f"{orch_base}/api/github/oauth/callback" if orch_base else None
        token_data = await exchange_oauth_code(code, redirect_uri=callback_uri)
        user_token = token_data["access_token"]

        # Get the GitHub user's profile to confirm identity
        await get_github_user(user_token)

        # Discover which of this App's installations the user can access
        installations = await get_user_installations(user_token)
        if not installations:
            # No installations yet — try to bounce the user to the App's install
            # page using its slug. If we can't look the slug up for any reason,
            # surface a generic error to the admin UI instead.
            install_redirect: str | None = None
            try:
                from app.github import get_app_info

                app_info = await get_app_info()
                app_slug = app_info.get("slug")
                if app_slug:
                    install_redirect = (
                        f"https://github.com/apps/{app_slug}/installations/new"
                    )
                    logger.info(
                        "[GITHUB OAUTH] User %s has no installations; sending to install page for app %r",
                        firebase_user_id,
                        app_slug,
                    )
            except Exception as app_err:
                logger.warning(
                    "Could not look up GitHub App slug for install redirect: %s",
                    app_err,
                )

            if install_redirect:
                return RedirectResponse(url=install_redirect)
            return RedirectResponse(url=f"{admin_url}?github_error=no_installations")

        # Store the first installation ID; collect all account names for display.
        # Token generation uses dynamic App-level discovery per repo, so the
        # stored installation_id is a fallback, not the authority.
        installation = installations[0]
        installation_id = str(installation["id"])
        all_account_names = [inst["account"]["login"] for inst in installations]
        account_name = ", ".join(all_account_names)

        # Save to UserIntegration
        stmt = select(UserIntegration).where(
            UserIntegration.user_id == firebase_user_id
        )
        integration = (await session.execute(stmt)).scalars().first()

        # Retain a caller-scoped credential separately from the App
        # installation ID. The latter identifies an installation but cannot
        # establish that this user may write an individual repository.
        oauth_secret_name = await store_user_secret(
            firebase_user_id, "github-oauth", user_token
        )

        if integration:
            integration.github_installation_id = installation_id
            integration.github_account_name = account_name
            integration.github_oauth_token = encrypt_field(oauth_secret_name)
            integration.updated_at = _utcnow()
        else:
            integration = UserIntegration(
                user_id=firebase_user_id,
                github_installation_id=installation_id,
                github_account_name=account_name,
                github_oauth_token=encrypt_field(oauth_secret_name),
            )

        session.add(integration)
        await session.commit()

        display_name = urllib.parse.quote(all_account_names[0])
        return RedirectResponse(url=f"{admin_url}?github_connected={display_name}")

    except Exception as e:
        logger.error(f"[GITHUB OAUTH] Error: {e}", exc_info=True)
        return RedirectResponse(url=f"{admin_url}?github_error=failed")


async def _stamp_github_verified_changed_files(
    pr_task: Task,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: object,
) -> None:
    """Override the agent's self-reported changed-file list with the authoritative
    list from GitHub's API and stamp ``_pr_changed_files_github_verified=True``.

    This closes the self-declaration trust gap: agents declare what test they
    added as acceptance evidence, but until the webhook fetches the real PR file
    list, the gate has only the agent's word that the test was in the diff.

    Fails silently (logs a warning) so that a transient GitHub API outage never
    blocks the PR-merged signal from landing.
    """
    try:
        from app.acceptance_gate import stamp_changed_files_signal
        from app.github_auth import get_repo_installation_token
        from app.github_pulls import get_pr_files

        gh_token = await get_repo_installation_token(owner, repo)
        if not gh_token:
            logger.warning(
                "[WEBHOOK] No installation token for %s/%s — "
                "falling back to agent self-report for PR #%s",
                owner, repo, pr_number,
            )
            return
        raw_files = await get_pr_files(gh_token, owner, repo, pr_number, str(project_id))
        file_paths = [
            f["filename"]
            for f in raw_files
            if isinstance(f, dict) and isinstance(f.get("filename"), str)
        ]
        ctx = dict(pr_task.context or {})
        ctx.update(stamp_changed_files_signal(file_paths, github_verified=True))
        pr_task.context = ctx
        logger.info(
            "[WEBHOOK] Stamped %d GitHub-verified changed files for task %s (PR #%s)",
            len(file_paths), pr_task.id, pr_number,
        )
    except Exception:
        logger.warning(
            "[WEBHOOK] get_pr_files failed for PR #%s; "
            "falling back to agent self-report for acceptance gate",
            pr_number, exc_info=True,
        )


@router.post("/webhook")
@limiter.limit("120/minute")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """
    Receives webhooks from the GitHub App (e.g., push, pull_request).
    """
    signature = request.headers.get("x-hub-signature-256")
    if not signature or not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=401, detail="Missing signature or webhook secret"
        )

    payload = await request.body()

    # Verify signature
    expected_signature = (
        "sha256="
        + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    )

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
                was_new = await _r.set(
                    f"webhook:{delivery_id}", "1", nx=True, ex=_WEBHOOK_DEDUP_TTL
                )
                if not was_new:
                    logger.info(
                        f"[WEBHOOK] Duplicate delivery {delivery_id}, skipping (Redis)"
                    )
                    return {"status": "duplicate", "delivery_id": delivery_id}
            except Exception:
                logger.exception(
                    f"[WEBHOOK] Redis dedup check failed for delivery {delivery_id}; falling through to in-memory"
                )
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
    data = await asyncio.to_thread(json.loads, payload)

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
                    logger.info(
                        f"[WEBHOOK] Ignoring event for unregistered repo {repo_full}"
                    )
                    return {"status": "ignored"}
                from app.autonomous_research import _check_autonomous_research

                background_tasks.add_task(_check_autonomous_research, owner, repo)

                # Propagate pr_merged to tasks that created this PR
                pr_number = data["pull_request"].get("number")
                pr_base_branch = (data.get("pull_request", {}).get("base") or {}).get(
                    "ref"
                )
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact)
                        .join(Task, Artifact.task_id == Task.id)
                        .where(
                            Task.project_id == project.id,
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
                        )
                        .limit(50)
                    )
                    pr_artifacts = art_result.scalars().all()
                    if len(pr_artifacts) == 50:
                        logger.warning(
                            f"[WEBHOOK] PR {pr_url_suffix} artifact lookup hit limit=50; some artifacts may be missed"
                        )
                    if not pr_artifacts:
                        logger.info(
                            f"[WEBHOOK] PR {pr_url_suffix} merged but no matching artifacts found — agent may not have reported yet"
                        )
                    task_map = await _load_tasks_for_artifacts(session, pr_artifacts)
                    affected_tasks = []
                    mutated_tasks = False
                    for art in pr_artifacts:
                        pr_task = task_map.get(art.task_id)
                        if pr_task and not (pr_task.context or {}).get("pr_merged"):
                            (
                                record_pr_merged,
                                terminal_rollup_merge,
                                landed_branch,
                            ) = await classify_rollup_merge_outcome(
                                pr_task,
                                project,
                                session,
                                pr_number=pr_number,
                                merged_base_branch=pr_base_branch,
                                source="github.pull_request.closed",
                            )
                            if record_pr_merged:
                                await _stamp_github_verified_changed_files(
                                    pr_task, owner, repo, pr_number, project.id
                                )
                                apply_merged_landed_signal(
                                    pr_task,
                                    project,
                                    base_branch=landed_branch,
                                    hotfix_lane=bool(
                                        (pr_task.first_payload or {}).get("hotfix")
                                        or (pr_task.context or {}).get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                )
                                affected_tasks.append(pr_task)
                                logger.info(
                                    f"[WEBHOOK] Set pr_merged on task {pr_task.id} (PR #{pr_number})"
                                )
                            elif (
                                not terminal_rollup_merge
                                and pr_task.status == TaskStatus.EVALUATING
                            ):
                                pr_task.latest_status_message = (
                                    f"Rollup PR merged into {landed_branch}; "
                                    "awaiting downstream landing"
                                )
                            session.add(pr_task)
                            mutated_tasks = True
                    if mutated_tasks:
                        await session.commit()
                    # Once a bootstrapped repo gains a project structure on its
                    # integration branch, refresh an empty build_config so
                    # verification commands exist without a manual admin step.
                    # Best-effort + idempotent (cheap in-memory directories
                    # guard); skip staging/rollup merges by gating on the
                    # integration branch.
                    if pr_base_branch and pr_base_branch == (
                        get_project_integration_branch(project)
                    ):
                        from app.build_config_detection import (
                            maybe_refresh_empty_build_config,
                        )

                        await maybe_refresh_empty_build_config(
                            project, session, ref=pr_base_branch
                        )
                    if affected_tasks:
                        from app.rollup_management import _check_and_trigger_rollup

                        for at in affected_tasks:
                            await _check_and_trigger_rollup(at, session)
                        try:
                            from app.notification_dispatch import (
                                dispatch_external_notification,
                            )  # noqa: PLC0415

                            for at in affected_tasks:
                                at_ctx = (
                                    at.context if isinstance(at.context, dict) else {}
                                )
                                at_payload = at.first_payload or {}
                                raw_changed = at_ctx.get("_pr_changed_files")
                                changed_files_count = (
                                    len([f for f in raw_changed if isinstance(f, str)])
                                    if isinstance(raw_changed, list)
                                    else 0
                                )
                                direction_text = (
                                    at_ctx.get("direction_text")
                                    or at_payload.get("direction_text")
                                    or ""
                                )
                                asyncio.ensure_future(
                                    dispatch_external_notification(
                                        at.project_id,
                                        "PR_MERGED",
                                        {
                                            "task_id": str(at.id),
                                            "task_title": at.title or str(at.id)[:8],
                                            "pr_number": pr_number,
                                            "repo": f"{owner}/{repo}",
                                            "direction_id": at_ctx.get("direction_id")
                                            or "",
                                            "direction_text": direction_text[:200],
                                            "scope": at_ctx.get("scope")
                                            or at_payload.get("scope")
                                            or "",
                                            "changed_files": changed_files_count,
                                        },
                                    )
                                )
                        except Exception as _ne:
                            logger.warning(
                                f"[NOTIFY] PR_MERGED dispatch setup failed: {_ne}"
                            )

        elif action in ("opened", "synchronize"):
            # Auto-review: spawn a review task when a PR is opened or pushed to
            pr_data = data.get("pull_request", {})
            repo_full = data.get("repository", {}).get("full_name", "")
            pr_number = pr_data.get("number")
            if "/" in repo_full and pr_number:
                ar_owner, ar_repo = repo_full.split("/", 1)
                auto_review_project = (
                    (
                        await session.execute(
                            select(Project).where(
                                Project.github_owner == ar_owner,
                                Project.github_repo == ar_repo,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if auto_review_project:
                    auto_review_config = (auto_review_project.design_doc or {}).get(
                        "_auto_review", {}
                    )
                    if auto_review_config.get("enabled"):
                        branch = pr_data.get("head", {}).get("ref", "")
                        base_branch = pr_data.get("base", {}).get(
                            "ref"
                        ) or get_project_integration_branch(auto_review_project)
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
                            webhook_pr=pr_data,
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
                    logger.info(
                        f"[WEBHOOK] Ignoring PR close event for unregistered repo {repo_full}"
                    )
                    return {"status": "ignored"}
                pr_number = data["pull_request"].get("number")
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact)
                        .join(Task, Artifact.task_id == Task.id)
                        .where(
                            Task.project_id == closed_project.id,
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
                        )
                        .limit(50)
                    )
                    pr_artifacts = art_result.scalars().all()
                    if len(pr_artifacts) == 50:
                        logger.warning(
                            f"[WEBHOOK] PR {pr_url_suffix} close artifact lookup hit limit=50; some artifacts may be missed"
                        )
                    if pr_artifacts:
                        pr_body = data.get("pull_request", {}).get("body") or ""
                        rejection_note = pr_body[:500] if pr_body else None
                        task_map = await _load_tasks_for_artifacts(session, pr_artifacts)
                        for art in pr_artifacts:
                            pr_task = task_map.get(art.task_id)
                            if pr_task and not (pr_task.context or {}).get(
                                "pr_rejected"
                            ):
                                update: dict = {"pr_rejected": True}
                                if rejection_note:
                                    update["pr_rejection_note"] = rejection_note
                                pr_task.context = {**(pr_task.context or {}), **update}
                                session.add(pr_task)
                                logger.info(
                                    f"[WEBHOOK] Set pr_rejected on task {pr_task.id} (PR #{pr_number})"
                                )
                        await session.commit()

    elif event_type == "check_run":
        action = data.get("action")
        if action != "completed":
            return {"status": "ignored"}

        check_run = data.get("check_run", {})
        head_sha = check_run.get("head_sha")
        repo_full = data.get("repository", {}).get("full_name", "")
        if "/" not in repo_full or not head_sha:
            return {"status": "ignored"}

        owner, repo = repo_full.split("/", 1)

        # Find ci_waiting tasks matching this commit SHA, scoped to this repo's project
        project_result = await session.execute(
            select(Project).where(
                Project.github_owner == owner, Project.github_repo == repo
            )
        )
        project = project_result.scalars().first()
        if not project:
            return {"status": "ignored", "reason": "unregistered_repo"}

        ci_tasks = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.EVALUATING,
                        Task.project_id == project.id,
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        if len(ci_tasks) == 200:
            logger.warning(
                f"[WEBHOOK] github_webhook CI task lookup for {owner}/{repo} hit limit=200; "
                "some EVALUATING tasks may be missed"
            )

        # Match strategies (in order of specificity):
        #   1. head_sha equality — the original precise match, set by the review
        #      APPROVE path (task_lifecycle.py) for non-rollup PRs.
        #   2. pr_number match against any PR number surfaced in the webhook's
        #      check_run.pull_requests[].  Rollup parents (rollup_management.py)
        #      set ci_waiting=True + pr_number but NOT head_sha, so strategy (1)
        #      would miss them and the orchestrator's 30s poll loop would be the
        #      only fallback — a major GitHub API quota burn.  Matching on
        #      pr_number lets the webhook handle rollup CI completions too.
        # GitHub puts the PR list at check_run.pull_requests and historically
        # also under check_run.check_suite.pull_requests — accept either.
        _pr_candidates = (check_run.get("pull_requests") or []) + (
            (check_run.get("check_suite") or {}).get("pull_requests") or []
        )
        webhook_pr_numbers = {
            pr.get("number") for pr in _pr_candidates if pr.get("number") is not None
        }
        matching = []
        for t in ci_tasks:
            rl = (t.context or {}).get("review_loop", {}) or {}
            if not rl.get("ci_waiting"):
                continue
            if rl.get("head_sha") == head_sha:
                matching.append(t)
                continue
            task_pr = rl.get("pr_number")
            if task_pr is not None and task_pr in webhook_pr_numbers:
                matching.append(t)
                # Backfill head_sha so future webhooks hit strategy (1) directly
                # and the orchestrator's poll-skip can use head_sha too.
                rl["head_sha"] = head_sha
                t.context = {**(t.context or {}), "review_loop": rl}
                session.add(t)

        if not matching:
            # No EVALUATING tasks match — check if this is a protected-branch CI failure
            conclusion = check_run.get("conclusion")
            head_branch = (check_run.get("check_suite") or {}).get("head_branch")
            if (
                conclusion == "success"
                and head_branch
                and head_branch == get_project_integration_branch(project)
            ):
                # Direct pushes / non-PR landings on the integration branch can
                # still turn an initially empty repo into a real app. Refresh
                # the empty build_config best-effort and keep the webhook
                # response non-blocking even if refresh/logging fails.
                from app.payload_schemas import _validate_branch_name  # noqa: PLC0415

                try:
                    _validate_branch_name(head_branch)
                except Exception:
                    logger.warning(
                        f"[WEBHOOK] Rejecting invalid head_branch for refresh: {head_branch!r}"
                    )
                else:
                    from app.build_config_detection import (
                        maybe_refresh_empty_build_config,
                    )

                    await maybe_refresh_empty_build_config(
                        project,
                        session,
                        ref=head_branch,
                    )
            if conclusion == "failure" and head_branch:
                # Validate branch name from webhook before passing downstream
                from app.payload_schemas import _validate_branch_name  # noqa: PLC0415

                try:
                    _validate_branch_name(head_branch)
                except Exception:
                    logger.warning(
                        f"[WEBHOOK] Rejecting invalid head_branch: {head_branch!r}"
                    )
                    return {"status": "ignored_invalid_branch"}
                # Read auto-remediation config (telemetry-first, design_doc
                # fallback during the rolling God-column cutover).
                from app.project_telemetry import (
                    read_telemetry_or_doc,
                )

                auto_remediation_config = (
                    await read_telemetry_or_doc(
                        session, project.id, project.design_doc, "_auto_remediation", {}
                    )
                    or {}
                )
                enabled = auto_remediation_config.get("enabled", False)
                branch_whitelist = auto_remediation_config.get(
                    "branch_whitelist", ["main", "master"]
                )
                # Always include the configured integration branch so CI failures
                # on the working trunk are self-healed without manual whitelist edits.
                effective_whitelist = set(branch_whitelist) | {
                    get_project_integration_branch(project),
                    get_project_promotion_target(project),
                }

                if not enabled or head_branch not in effective_whitelist:
                    return {"status": "ignored_auto_remediation_disabled"}

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
                return {"status": "auto_remediation_triggered"}

            return {"status": "no_match"}

        # Fetch full check status for this SHA once — every matching sibling
        # shares the same head_sha, so the cached variant collapses N calls
        # into 1 (webhook fan-out was one of the biggest per-event burners
        # when rollup PRs had many children).
        from app.github import get_check_runs_cached
        from app.routers.agent import get_github_token_for_task
        from app.ci_completion import handle_ci_completion

        for task in matching:
            try:
                gh_token, gh_owner, gh_repo = await get_github_token_for_task(
                    task.id, session
                )
                ci_result = await get_check_runs_cached(
                    gh_token,
                    gh_owner,
                    gh_repo,
                    head_sha,
                    project_id=str(task.project_id),
                )

                if ci_result["status"] != "completed":
                    # Some checks still pending — more webhooks will come
                    logger.info(
                        f"[WEBHOOK] check_run for {head_sha[:8]}: some checks still pending, waiting"
                    )
                    continue

                await handle_ci_completion(
                    task,
                    ci_result,
                    gh_token,
                    gh_owner,
                    gh_repo,
                    session,
                    source="webhook",
                    checked_head_sha=head_sha,
                )
                logger.info(
                    f"[WEBHOOK] CI completed for {head_sha[:8]} — handled task {task.id} via webhook"
                )
            except Exception as e:
                # Fallback: reduce polling interval so orchestrator picks it up soon
                logger.error(
                    f"[WEBHOOK] Error handling CI completion for task {task.id}: {e}",
                    exc_info=True,
                )
                loop = (task.context or {}).get("review_loop", {})
                loop["ci_poll_interval"] = 15
                loop["ci_last_polled"] = None
                task.context = {**(task.context or {}), "review_loop": loop}
                session.add(task)
                await session.commit()

    elif event_type == "pull_request_review":
        action = data.get("action")
        # Only process submitted and dismissed actions; ignore edits
        if action not in ("submitted", "dismissed"):
            return {"status": "ignored"}

        review = data.get("review", {})
        review_state = review.get("state", "")
        pull_request_data = data.get("pull_request", {})
        pr_number = pull_request_data.get("number")
        repo_full = data.get("repository", {}).get("full_name", "")

        if not pr_number or "/" not in repo_full:
            return {"status": "ignored"}

        owner, repo_name = repo_full.split("/", 1)
        project = (
            (
                await session.execute(
                    select(Project).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo_name,
                    )
                )
            )
            .scalars()
            .first()
        )
        if not project:
            logger.info(
                f"[WEBHOOK] pull_request_review for unregistered repo {repo_full}"
            )
            return {"status": "ignored"}

        # Find original create task via the PR artifact
        pr_url_suffix = f"/{owner}/{repo_name}/pull/{pr_number}"
        art_result = await session.execute(
            select(Artifact)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == project.id,
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
            )
            .limit(50)
        )
        pr_artifacts = art_result.scalars().all()
        if len(pr_artifacts) == 50:
            logger.warning(
                f"[WEBHOOK] pull_request_review PR {pr_url_suffix} artifact lookup hit limit=50; some artifacts may be missed"
            )
        if not pr_artifacts:
            logger.info(
                f"[WEBHOOK] pull_request_review for PR {pr_url_suffix}: no matching artifact"
            )
            return {"status": "ignored"}

        review_user = review.get("user", {})
        is_bot_review = review_user.get("type") == "Bot" or str(
            review_user.get("login", "")
        ).endswith("[bot]")

        signals_recorded = []
        task_map = await _load_tasks_for_artifacts(session, pr_artifacts)
        for art in pr_artifacts:
            pr_task = task_map.get(art.task_id)
            if not pr_task:
                continue

            blueprint_verdict = (pr_task.context or {}).get("blueprint_review_verdict")
            update: dict = {}
            signal: str | None = None
            detail: str | None = None

            if action == "dismissed" and is_bot_review:
                # A Blueprint review was dismissed by a user
                dismissed_info = review.get("dismissed_review", {})
                dismissal_message = (dismissed_info.get("dismissal_message") or "")[
                    :500
                ]
                update["review_dismissed"] = True
                if dismissal_message:
                    update["review_dismissal_message"] = dismissal_message
                signal = "review_dismissed"
                detail = dismissal_message or None
                logger.info(
                    f"[REVIEW QUALITY] PR #{pr_number}: Blueprint review dismissed "
                    f"on task {pr_task.id}"
                )

            elif (
                action == "submitted"
                and review_state == "approved"
                and blueprint_verdict == "REQUEST_CHANGES"
            ):
                # User approved after Blueprint requested changes — disagreement signal
                if not (pr_task.context or {}).get("user_overrode_review"):
                    update["user_overrode_review"] = True
                    signal = "user_overrode_review"
                    logger.info(
                        f"[REVIEW QUALITY] PR #{pr_number}: User approved after Blueprint "
                        f"REQUEST_CHANGES on task {pr_task.id}"
                    )

            elif (
                action == "submitted"
                and review_state == "changes_requested"
                and blueprint_verdict == "APPROVE"
            ):
                # User caught issues on a PR that Blueprint approved
                if not (pr_task.context or {}).get("user_caught_issues"):
                    update["user_caught_issues"] = True
                    signal = "user_caught_issues"
                    logger.info(
                        f"[REVIEW QUALITY] PR #{pr_number}: User caught issues after Blueprint "
                        f"APPROVE on task {pr_task.id}"
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
                logger.warning(f"[REVIEW QUALITY] In-app message failed: {_ne}")
            await session.commit()
            # Fire-and-forget external notification for each signal
            try:
                from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415

                for sig in signals_recorded:
                    asyncio.ensure_future(
                        dispatch_external_notification(
                            project.id,
                            "REVIEW_FEEDBACK",
                            {
                                "pr_number": pr_number,
                                "repo": f"{owner}/{repo_name}",
                                "signal": sig,
                                "task_id": str(pr_artifacts[0].task_id),
                            },
                        )
                    )
            except Exception as _ne:
                logger.warning(f"[REVIEW QUALITY] External notification failed: {_ne}")

    # NOTE: The 'issues' event must be enabled in the GitHub App's webhook settings.
    elif event_type == "issues":
        action = data.get("action")
        if action == "labeled":
            issue = data.get("issue", {})
            label = data.get("label", {})
            repo_data = data.get("repository", {})
            repo_full = repo_data.get("full_name", "")
            issue_number = issue.get("number")
            issue_title = issue.get("title", "")
            label_name = label.get("name", "")

            if "/" in repo_full and issue_number:
                owner, repo_name = repo_full.split("/", 1)
                project = (
                    (
                        await session.execute(
                            select(Project).where(
                                Project.github_owner == owner,
                                Project.github_repo == repo_name,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if project:
                    config = (project.design_doc or {}).get("_issue_implementation", {})
                    if config.get("enabled") and label_name == config.get(
                        "trigger_label", "auto:implement"
                    ):
                        from app.issue_implementation import (
                            _handle_issue_implementation,
                        )

                        background_tasks.add_task(
                            _handle_issue_implementation,
                            owner=owner,
                            repo=repo_name,
                            issue_number=issue_number,
                            issue_title=issue_title,
                            project_id=project.id,
                            config=config,
                        )
                        return {"status": "issue_implementation_triggered"}

    return {"status": "received"}
