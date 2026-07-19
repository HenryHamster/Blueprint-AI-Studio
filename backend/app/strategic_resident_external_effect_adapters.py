"""External-effect adapters for Strategic Resident mutations.

Each adapter builds the idempotent ``perform`` coroutine that
``execute_external_effect_operation`` runs in its perform phase, plus the
receipt ``operation_kind``. These are gated per-capability by a default-empty
runtime_config allowlist, so the whole external-effect path ships inert until an
operator arms a specific capability.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


# Capabilities whose service performs an external side effect through this module.
EXTERNAL_EFFECT_SERVICES = frozenset({"github_pulls"})

# capability_id -> receipt operation_kind. pr.create is medium-risk and
# self-gated; pr.merge is critical and additionally gated by a real, per-operation
# operator approval verified in the router. deployment.* stay proposal-only until
# safe programmatic deploy primitives exist.
_OPERATION_KINDS = {
    "pr.create": "create_pull_request",
    "pr.merge": "merge_pull_request",
}


def _pr_create_effect(
    request_project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    parameters: dict[str, Any],
) -> Callable[[AsyncSession], Awaitable[dict[str, Any]]]:
    async def perform(session: AsyncSession) -> dict[str, Any]:
        # Deferred imports: the adapter module stays importable without the
        # GitHub stack when the path is disabled.
        from app.github_pulls import create_pull_request
        from app.github_token_resolver import get_github_token_for_task

        token, owner, repo = await get_github_token_for_task(turn_task_id, session)
        result = await create_pull_request(
            token,
            owner,
            repo,
            title=str(parameters["title"]),
            head=str(parameters["head"]),
            base=str(parameters["base"]),
            project_id=str(request_project_id),
        )
        return {
            "pr_number": result.get("number"),
            "html_url": result.get("html_url"),
            "head": str(parameters["head"]),
            "base": str(parameters["base"]),
        }

    return perform


def _pr_merge_effect(
    request_project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    parameters: dict[str, Any],
) -> Callable[[AsyncSession], Awaitable[dict[str, Any]]]:
    async def perform(session: AsyncSession) -> dict[str, Any]:
        from app.github_pulls import get_pull_request, merge_pull_request
        from app.github_token_resolver import get_github_token_for_task
        from app.rollup_migration_validation import enforce_premerge_migration_guard

        token, owner, repo = await get_github_token_for_task(turn_task_id, session)
        pr_number = int(parameters["pr_number"])
        # Pre-merge dual-head Alembic guard. An autonomous resident merge can
        # carry a migration onto an integration/promotion branch; merging a PR
        # that creates two Alembic heads / a duplicate revision id wedges
        # `alembic upgrade head` on that branch. Run the same combined-chain
        # guard the operator-approval and CI-completion merge paths use and fail
        # closed on a block-or-defer verdict (`block` = invalid combined chain;
        # `defer` = GitHub metadata unavailable so migration safety is unknown).
        pr = await get_pull_request(token, owner, repo, pr_number)
        target_branch = (pr.get("base") or {}).get("ref")
        _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
            token,
            owner,
            repo,
            pr_number,
            target_branch,
        )
        if not _ok_to_merge:
            raise RuntimeError(
                "pre-merge migration guard blocked "
                f"PR #{pr_number}: "
                f"{_hold_reason or 'combined Alembic chain would create a dual head'}"
            )
        # Fail closed on CHANGES_REQUESTED (merge_pull_request checks the review
        # decision before the PUT) and on a head that advanced past the approved
        # sha (expected_head_sha -> GitHub's merge `sha` guard). Never override
        # with allow_changes_requested.
        result = await merge_pull_request(
            token,
            owner,
            repo,
            pr_number,
            project_id=str(request_project_id),
            expected_head_sha=str(parameters["expected_head_sha"]),
        )
        return {
            "merged": bool(result.get("merged")),
            "pr_number": int(parameters["pr_number"]),
            "sha": result.get("sha"),
        }

    return perform


def external_effect_operation_kind(capability_id: str) -> str | None:
    """Return the receipt operation_kind for a wired external capability, or None."""

    return _OPERATION_KINDS.get(capability_id)


def build_external_effect(
    capability_id: str,
    *,
    project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    parameters: dict[str, Any],
) -> Callable[[AsyncSession], Awaitable[dict[str, Any]]] | None:
    """Return the idempotent perform coroutine for a wired capability, or None."""

    if capability_id == "pr.create":
        return _pr_create_effect(project_id, turn_task_id, parameters)
    if capability_id == "pr.merge":
        return _pr_merge_effect(project_id, turn_task_id, parameters)
    return None


__all__ = [
    "EXTERNAL_EFFECT_SERVICES",
    "build_external_effect",
    "external_effect_operation_kind",
]
