"""Project-owned admission policy for automatic Strategic cycles.

``Project.autonomous_enabled`` authorizes the broader autonomous research loop,
which contains two distinct authoring surfaces: tactical direction research and
periodic/forced Strategic cycles.  Operators sometimes need to canary the first
surface without releasing the second.  Keep that distinction in one leaf module
so the scheduler and its debug projections cannot disagree about the
``build_config`` default or malformed-value behavior.

Explicit/manual Strategic launches and the conversational Resident do not
consult this policy.  Every automatic Strategic producer does, including the
architecture-to-strategy continuation and qualification-triggered refresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
    automatic_task_creation_decision,
    project_lock,
)


AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY = (
    "automatic_strategic_cycles_enabled"
)
AUTOMATIC_STRATEGIC_CYCLES_DISABLED = "automatic_strategic_cycles_disabled"


def automatic_strategic_cycles_enabled(project: Any) -> bool:
    """Return whether the autonomous loop may originate Strategic cycles.

    Existing projects have no stored key, so the compatibility default is
    enabled.  Only a validated, explicit boolean ``False`` disables the lane;
    malformed raw JSON fails back to the historical enabled behavior instead of
    silently turning off Strategic work.
    """

    build_config = getattr(project, "build_config", None)
    if not isinstance(build_config, Mapping):
        return True
    configured = build_config.get(
        AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY,
        True,
    )
    return configured if isinstance(configured, bool) else True


async def acquire_automatic_strategic_cycle_creation_authority(
    session: AsyncSession,
    project_id: Any,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> str:
    """Linearize an automatic Strategic INSERT against its full authority.

    Repository/config detection and operator PATCHes can change the Strategic
    lane while an automatic producer spends seconds assembling a payload.  Read
    the *current* Project authority columns only after acquiring the canonical
    ``FOR NO KEY UPDATE`` lock, then retain that transaction-scoped lock through
    the caller's Task INSERT and commit.  This gives a concurrent lane-disable
    and spawn one deterministic order:

    * disable wins (or still owns the row) -> the automatic spawn fails closed;
    * spawn wins -> its Task commits before the disable can become visible.

    Manual/directions and Resident callers deliberately never invoke this
    helper.  ``autonomous_enabled`` is also deliberately absent: it controls
    tactical scheduling, while this dedicated flag controls automatic Strategic
    authorship and pause/archive remain the universal task fences.
    """

    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
        load_columns=[
            Project.id,
            Project.paused,
            Project.archived,
            Project.build_config,
        ],
    ) as locked_project:
        decision = automatic_task_creation_decision(locked_project)
        if decision != AUTOMATIC_TASK_CREATION_ALLOWED:
            return decision
        if not automatic_strategic_cycles_enabled(locked_project):
            return AUTOMATIC_STRATEGIC_CYCLES_DISABLED
        return AUTOMATIC_TASK_CREATION_ALLOWED

    # The context manager always returns from inside the block.  Keep an
    # explicit fail-closed fallback for static analyzers and future refactors.
    return AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
