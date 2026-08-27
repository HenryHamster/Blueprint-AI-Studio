"""Agent-facing GitHub proxy endpoints.

Extracted from app.routers.agent so the authenticated GitHub proxy surface can
evolve independently while keeping the /api/agent/github/* paths byte-identical.
Auth is shared through ``verify_agent_token`` from the parent router.
"""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.routers.agent as _agent_router
from app.database import get_session
from app.github import (
    create_branch,
    create_issue,
    create_pr_comment,
    create_pr_review_comment,
    create_pull_request,
    reconcile_open_pull_request,
    get_check_runs_cached,
    get_issues,
    get_pull_request,
    submit_pr_review,
    update_commit_status,
)
from app.models import Artifact, ArtifactType, Task
from app.routers.agent import verify_agent_token
from app.task_pr_linkage import parse_pr_number_from_url, persist_task_pr_linkage
from app.schemas import (
    GitHubCommitStatusRequest,
    GitHubCreateBranchRequest,
    GitHubCreateIssueRequest,
    GitHubCreatePRRequest,
    GitHubGetIssuesRequest,
    GitHubPRCommentRequest,
    GitHubPRReviewCommentRequest,
    GitHubSubmitPRReviewRequest,
)
from app.shared_state import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


@router.get("/github/token")
@limiter.limit("200/minute")
async def agent_github_get_token(
    request: Request,
    task_id: uuid.UUID,
    force_refresh: bool = False,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # force_refresh busts the ~50min in-process token cache so the agent can
    # re-mint a fresh installation token when a baked-in one has expired (e.g.
    # a clone that fails authentication after a long queue/provision delay).
    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        task_id, session, force_refresh=force_refresh
    )
    return {
        "status": "success",
        "data": {"token": gh_token, "owner": owner, "repo": repo},
    }


@router.get("/github/check-runs")
@limiter.limit("200/minute")
async def agent_github_get_check_runs(
    request: Request,
    commit_sha: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    task_id_str = token.get("sub")
    if not task_id_str:
        raise HTTPException(status_code=403, detail="Invalid token: missing sub")
    task_id = uuid.UUID(task_id_str)
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise HTTPException(status_code=422, detail="Invalid commit_sha")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        task_id, session
    )
    # Budget: this endpoint is POLLED by every agent waiting on CI, and it
    # returns only status/conclusion/details — ``failed_logs`` is discarded.
    #   * get_check_runs_cached (30s TTL, the same wrapper auto_remediation /
    #     ci_resolution / hotfix_backmerge / orchestrator_ci_monitor use)
    #     collapses sibling agents polling the same sha into one fetch;
    #   * include_failed_logs=False skips the RED-sha failure fan-out (workflow
    #     runs + per-run jobs + per-failed-check job logs + annotations — 3-10+
    #     calls) whose output this handler throws away.
    # Both draw on the SHARED 5000/hr installation-token bucket whose exhaustion
    # has stalled production twice.
    result = await get_check_runs_cached(
        token=gh_token,
        owner=owner,
        repo=repo,
        head_sha=commit_sha,
        include_failed_logs=False,
    )
    return {
        "status": "success",
        "data": {
            "status": result.get("status"),
            "conclusion": result.get("conclusion"),
            "details": result.get("details", []),
        },
    }


@router.post("/github/commit-status")
@limiter.limit("500/minute")
async def agent_github_commit_status(
    request: Request,
    req: GitHubCommitStatusRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await update_commit_status(
        token=gh_token,
        owner=owner,
        repo=repo,
        sha=req.sha,
        state=req.state,
        description=req.description,
        context=req.context,
        target_url=req.target_url,
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-comment")
@limiter.limit("200/minute")
async def agent_github_pr_comment(
    request: Request,
    req: GitHubPRCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await create_pr_comment(
        token=gh_token, owner=owner, repo=repo, pr_number=req.pr_number, body=req.body
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-review-comment")
@limiter.limit("200/minute")
async def agent_github_pr_review_comment(
    request: Request,
    req: GitHubPRReviewCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await create_pr_review_comment(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        commit_id=req.commit_id,
        path=req.path,
        line=req.line,
        body=req.body,
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-review")
@limiter.limit("150/minute")
async def agent_github_submit_pr_review(
    request: Request,
    req: GitHubSubmitPRReviewRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await submit_pr_review(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        event=req.event,
        body=req.body,
        comments=req.comments,
    )
    return {"status": "success", "data": result}


@router.post("/github/issues")
@limiter.limit("100/minute")
async def agent_github_create_issue(
    request: Request,
    req: GitHubCreateIssueRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await create_issue(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        body=req.body,
        assignees=req.assignees,
    )
    return {"status": "success", "data": result}


@router.get("/github/issues")
@limiter.limit("150/minute")
async def agent_github_get_issues(
    request: Request,
    req: GitHubGetIssuesRequest = Depends(),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await get_issues(token=gh_token, owner=owner, repo=repo, state=req.state)
    return {"status": "success", "data": result}


@router.post("/github/branches")
@limiter.limit("100/minute")
async def agent_github_create_branch(
    request: Request,
    req: GitHubCreateBranchRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )
    result = await create_branch(
        token=gh_token, owner=owner, repo=repo, branch_name=req.branch_name, sha=req.sha
    )
    # create_branch reports reuse of a pre-existing ref instead of silently
    # adopting it. The proxy still returns success (the branch the agent asked
    # for does exist), but the discriminating fields are passed through in
    # ``data`` (reused / existing_sha / requested_sha / sha_matches) and a
    # sha-mismatch is logged — an agent branching off a stale tip left by a
    # crashed prior run is otherwise invisible.
    if (
        isinstance(result, dict)
        and result.get("reused")
        and not result.get("sha_matches")
    ):
        logger.warning(
            f"[BRANCH] Task {req.task_id}: branch {req.branch_name!r} already existed at "
            f"{result.get('existing_sha')} — NOT the requested {req.sha}"
        )
    return {"status": "success", "data": result}


@router.post("/github/pull-requests")
@limiter.limit("100/minute")
async def agent_github_create_pr(
    request: Request,
    req: GitHubCreatePRRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await _agent_router.get_github_token_for_task(
        req.task_id, session
    )

    existing_artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == req.task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing_artifact and existing_artifact.pr_url:
        try:
            pr_number = int(existing_artifact.pr_url.rstrip("/").split("/")[-1])
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("state") == "open":
                if req.expected_head_sha is None:
                    logger.info(
                        f"[PR] Task {req.task_id} already has open PR artifact: "
                        f"{existing_artifact.pr_url}; legacy caller omitted exact head SHA"
                    )
                    return {
                        "status": "success",
                        "data": {"html_url": existing_artifact.pr_url},
                    }
                logger.info(
                    f"[PR] Task {req.task_id} already has open PR artifact; "
                    f"reconciling its fresh title/body: {existing_artifact.pr_url}"
                )
                result = await reconcile_open_pull_request(
                    gh_token,
                    owner,
                    repo,
                    pr_data,
                    title=req.title,
                    body=req.body,
                    expected_head=req.head,
                    expected_head_sha=req.expected_head_sha,
                    expected_base=req.base,
                )
                result.setdefault("html_url", existing_artifact.pr_url)
                return {
                    "status": "success",
                    "data": result,
                }
            logger.warning(
                f"[PR] Task {req.task_id} cached PR #{pr_number} is {pr_data.get('state')} "
                f"(merged={pr_data.get('merged')}) — recreating PR and replacing artifact"
            )
            await session.delete(existing_artifact)
            await session.flush()
        except HTTPException as exc:
            if exc.status_code in (404, 410):
                logger.warning(
                    f"[PR] Task {req.task_id} cached PR no longer exists ({exc.status_code}) — recreating"
                )
                await session.delete(existing_artifact)
                await session.flush()
            else:
                raise
        except (ValueError, IndexError):
            logger.warning(
                f"[PR] Could not parse PR number from cached URL {existing_artifact.pr_url} — recreating"
            )
            await session.delete(existing_artifact)
            await session.flush()

    # create_pull_request is FAIL-CLOSED under GitHub rate-limit exhaustion: when
    # it cannot confirm whether an open PR already exists, it raises
    # HTTPException(503, headers={"Retry-After": ...}) instead of POSTing (which
    # would create a duplicate PR once the quota window resets). That 503 +
    # Retry-After propagates through the app HTTPException handler unchanged, so
    # the agent's create-PR backoff loop respects it rather than seeing a 500.
    result = await create_pull_request(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        head=req.head,
        base=req.base,
        body=req.body,
        reconcile_existing=req.expected_head_sha is not None,
        expected_head_sha=req.expected_head_sha,
    )

    pr_url = result.get("html_url", "")
    if pr_url:
        artifact = Artifact(
            task_id=req.task_id, artifact_type=ArtifactType.PULL_REQUEST, pr_url=pr_url
        )
        session.add(artifact)
        # Persist PR linkage onto the task row, not just the artifact. Standalone
        # (parentless) tasks otherwise lose pr_number/branch entirely — they live
        # only in the artifact, so GET /api/admin/tasks returns null/null. The
        # leading payload is the canonical home the admin UI + branch_resolution
        # already read from.
        task = await session.get(Task, req.task_id)
        if task is not None:
            persist_task_pr_linkage(
                task,
                pr_number=parse_pr_number_from_url(pr_url),
                branch=req.head,
            )
        await session.commit()
        logger.info(f"[PR] Saved PR artifact for task {req.task_id}: {pr_url}")

    return {"status": "success", "data": result}
