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

# capability_id -> receipt operation_kind. Only the low-risk, reversible,
# already-idempotent pr.create is wired; pr.merge/deployment.* (critical, explicit
# approval) remain proposal-only until real approval verification exists.
_OPERATION_KINDS = {
    "pr.create": "create_pull_request",
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
    return None


def external_effect_capability_is_armed(
    capability_id: str,
    allowlist: Any,
) -> bool:
    """True only if an operator has explicitly armed this capability id."""

    return isinstance(allowlist, (list, tuple, set)) and capability_id in set(allowlist)


__all__ = [
    "EXTERNAL_EFFECT_SERVICES",
    "build_external_effect",
    "external_effect_capability_is_armed",
    "external_effect_operation_kind",
]
