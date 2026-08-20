"""Owner-only operator endpoints for narrow release-machinery hotfix promotion."""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_qualifier
from app.admin_helpers import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session

router = APIRouter(tags=["admin"])

class _HotfixPromoteRequest(BaseModel):
    candidate_sha: str | None = None
    # REQUIRED, non-empty: this narrower lane's reason IS the audit justification.
    reason: str = Field(..., min_length=1)
    # The operator's DECLARED scope — cross-checked/audited against the actual
    # diff, NEVER the authorization source (the diff allowlist is).
    scope_modules: List[str] = Field(default_factory=list)
    # NARROW lane: the commits (already in the integration branch's history) that
    # together constitute the repair.  When supplied, the lane scope-checks and
    # ships ONLY those commits' own file set, materialized on top of the
    # promotion target — instead of everything integration has accumulated since
    # the last promotion.  Still not an authorization source: the same
    # fail-closed allowlist runs over the resulting diff.
    repair_commit_shas: List[str] = Field(default_factory=list)
    # The operator must affirm they understand the self-reference gap: the fix
    # stays inert until the deploy lands the promoted content.
    ack_inert_until_deploy: bool = False


@router.post("/projects/{project_id}/promote/hotfix")
async def trigger_hotfix_promote(
    project_id: uuid.UUID,
    body: _HotfixPromoteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """HOTFIX-PROMOTE the project's open version — a NARROW, audited force-promote.

    Fast-tracks a release-machinery fix ahead of the normal qualification gate,
    but STRICTLY narrower than ``/promote/force``: it can only ship allowlisted
    release-machinery files (never product code or migrations), the allowlist is
    enforced fail-closed against the candidate's ACTUAL changed files, and every
    hotfix is recorded as owed completion debt + an operator Alert.  Skips the
    AUDIT/scoring gate ONLY — CI and promote()'s merge-time migration-chain
    backstop still gate the merge.

    ``repair_commit_shas`` selects the NARROW lane: name the commits that are the
    repair and the lane materializes a machinery-only candidate from just those
    commits' files on top of the promotion target, instead of refusing because
    integration has accumulated unrelated work since the last promotion.

    OWNER-only.  ``reason`` is mandatory (it is the audit justification) and
    ``ack_inert_until_deploy`` must be true.  Maps: scope_rejected → 400 (with the
    offending files), no_effective_change → 400, no_open_version → 404,
    promotion_check_wait → 409, diff/content/commit failures → 502.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    if not body.reason or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="A non-empty 'reason' is required — it is the hotfix audit justification.",
        )
    if not body.ack_inert_until_deploy:
        raise HTTPException(
            status_code=400,
            detail=(
                "ack_inert_until_deploy must be true — affirm you understand the "
                "hotfix fix stays inert until the deploy lands the promoted content."
            ),
        )
    result = await release_qualifier.hotfix_promote_current_version(
        session,
        project_id,
        candidate_sha=body.candidate_sha,
        actor=str(current_user["user_id"]),
        reason=body.reason.strip(),
        scope_modules=list(body.scope_modules),
        repair_commit_shas=list(body.repair_commit_shas),
    )
    await session.commit()
    status = result.get("status")
    if status == "scope_rejected":
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Hotfix scope rejected — the candidate diff is not a "
                    "release-machinery-only change: " + str(result.get("reason") or "")
                ),
                "offending_files": result.get("offending_files") or [],
            },
        )
    if status == "no_effective_change":
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Hotfix refused — the named repair commits change nothing "
                    "relative to the promotion target: "
                    + str(result.get("reason") or "")
                ),
                "offending_files": [],
            },
        )
    if status in ("diff_unavailable", "content_unavailable", "commit_failed"):
        raise HTTPException(
            status_code=502,
            detail=(
                "Hotfix could not resolve or materialize the named repair "
                f"({status}): " + str(result.get("reason") or "")
            ),
        )
    if status == "no_open_version":
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )
    if status == "promotion_check_wait":
        detail = (
            "a managed promotion is pending — wait for it to land or "
            "cancel it before hotfix-promoting"
        )
        pr_number = result.get("promotion_pr_number")
        if pr_number is not None:
            detail += f" (PR #{pr_number})"
        raise HTTPException(status_code=409, detail=detail)
    return result


@router.get("/projects/{project_id}/promote/hotfix/preview")
async def get_hotfix_scope_preview(
    project_id: uuid.UUID,
    candidate_sha: str | None = None,
    repair_commit_shas: List[str] | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Read-only preview: report whether the current open version's candidate diff
    is hotfix-eligible WITHOUT promoting.

    OWNER-only.  Returns the scope verdict (eligible/ineligible), the list of all
    changed files between the promotion target and the candidate, and the specific
    files (if any) that would be rejected.  The optional ``candidate_sha`` query
    parameter pins the check to a specific commit; when omitted the integration
    branch HEAD is used (same default as ``POST /promote/hotfix``).  Repeating the
    ``repair_commit_shas`` parameter previews the NARROW lane instead: the union
    of those commits' own diffs — exactly what ``POST /promote/hotfix`` would
    materialize and ship.

    Maps: no_open_version → 404, no_credentials/sha_error → 503.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    # Unit tests call this coroutine directly, where an omitted query parameter
    # arrives as FastAPI's default marker rather than a list.
    _repair_shas = (
        list(repair_commit_shas) if isinstance(repair_commit_shas, list) else []
    )
    result = await release_qualifier.check_hotfix_scope_preview(
        session,
        project_id,
        candidate_sha=candidate_sha,
        repair_commit_shas=_repair_shas,
    )
    status = result.get("status")
    if status == "no_open_version":
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )
    if status in ("no_credentials", "sha_error", "diff_unavailable"):
        raise HTTPException(
            status_code=503,
            detail=result.get("detail") or result.get("reason") or status,
        )
    return result
