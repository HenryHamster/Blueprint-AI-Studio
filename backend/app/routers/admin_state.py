"""GET /api/admin/state — canonical system-state read endpoint.

Single-call status surface for the blueprint CLI `status` subcommand and
UI health cards. Wraps compute_system_state() in a 15s DigestCache TTL so
concurrent polling stays cheap.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.digest_cache import DigestCache, get_digest_cache
from app.system_state import compute_system_state

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-state"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/state")
async def get_system_state(
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    cache: DigestCache = Depends(get_digest_cache),
) -> Dict[str, Any]:
    """Return the canonical system state snapshot.

    When project_id is omitted: returns deploy + fleet (version=None).
    Restricted to admin role.

    When project_id is supplied: also returns the version block.
    Requires at least viewer role on the project.
    """
    if project_id is None:
        # Fleet-wide view: admin only.
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
    else:
        # Project-scoped view: enforce viewer membership.
        from app.admin_helpers import _get_user_project_with_role

        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, min_role="viewer"
        )

    cache_key = ("system_state", str(project_id) if project_id else None)
    result, _served_stale = await cache.get_or_compute_meta(
        cache_key,
        lambda: compute_system_state(session, project_id=project_id),
        ttl=15.0,
    )
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return result
