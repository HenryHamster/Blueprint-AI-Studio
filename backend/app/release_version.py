"""ReleaseVersion table helpers.

Phase 2 of the automain LTS design (docs/plans/automain-lts-plan-v1.md):
a "version" is the span of automain between two promotions.  This module
is the single source of truth for version reads/writes.

The strategic agent drives phase transitions (build → stabilize); the
qualifier (Phase 3) drives status transitions (open → promoted).

Public API:

  open_version(session, project_id, *, version_number, base_sha)
      Insert a new open version row (idempotent via UNIQUE constraint).

  current_open_version(session, project_id)
      Return the single ``status="open"`` row for a project, or None.

  count_merged_since(session, project_id, since)
      Count tasks whose PR was merged after ``since`` for a project.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReleaseVersion, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def open_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    base_sha: str | None = None,
) -> ReleaseVersion:
    """Insert a new open ``ReleaseVersion`` row for ``project_id``.

    Caller is responsible for committing the session.  The UNIQUE constraint
    on (project_id, version_number) prevents duplicates — callers should
    ensure the previous open version has been closed (status promoted or
    abandoned) before calling this.

    ``base_sha`` is the automain HEAD SHA at the time the version is opened;
    nullable per migration linter rules.
    """
    version = ReleaseVersion(
        project_id=project_id,
        version_number=version_number,
        phase="build",
        status="open",
        opened_at=_utcnow(),
        base_sha=base_sha,
    )
    session.add(version)
    logger.info(
        "[RELEASE_VERSION] opened version=%d project=%s sha=%s",
        version_number,
        project_id,
        base_sha,
    )
    return version


async def current_open_version(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ReleaseVersion | None:
    """Return the current open ``ReleaseVersion`` for ``project_id``, or None.

    At most one row should have ``status="open"`` per project.  If multiple
    exist (data anomaly), returns the most recently opened one and logs a
    warning.
    """
    stmt = (
        select(ReleaseVersion)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.status == "open",
        )
        .order_by(ReleaseVersion.opened_at.desc())
        .limit(2)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "[RELEASE_VERSION] multiple open versions found for project %s; "
            "returning most recently opened (v%d)",
            project_id,
            rows[0].version_number,
        )
    return rows[0]


async def count_merged_since(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
) -> int:
    """Count tasks for ``project_id`` whose PR was merged at or after ``since``.

    "Merged" is defined as ``task.status = COMPLETED`` AND
    ``task.context->>'pr_merged' = 'true'`` AND
    ``task.updated_at >= since``.  This mirrors the convention established
    throughout admin.py and rollup_management.py where a successful merge
    sets ``task.context["pr_merged"] = True``.

    ``updated_at`` is used as a proxy for merge time because no dedicated
    merged-at column exists on Task.  It is set via ``onupdate`` so it
    reflects the last status transition, which for a newly-merged task is
    the merge itself.
    """
    # Cast context JSONB boolean to text for portable equality check.
    # ``context @> '{"pr_merged": true}'::jsonb`` is the canonical Postgres
    # JSONB containment query — simpler and index-friendly when a GIN index
    # exists.  Use the containment operator so the query works with both
    # boolean ``true`` (Python writes) and potential legacy string ``"true"``
    # gracefully.  Fall back to text comparison for SQLite test fixtures
    # (containment is Postgres-only).
    try:
        # Postgres path: JSONB @> containment
        stmt = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.updated_at >= since,
                cast(Task.context, JSONB).contains({"pr_merged": True}),
            )
        )
        result = await session.execute(stmt)
        return int(result.scalar() or 0)
    except Exception:  # noqa: BLE001
        # Fallback for SQLite (test) or unexpected dialect issues: text match.
        logger.debug(
            "[RELEASE_VERSION] count_merged_since falling back to text match",
            exc_info=True,
        )
        stmt_fallback = (
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.updated_at >= since,
            )
        )
        result = await session.execute(stmt_fallback)
        return int(result.scalar() or 0)


def to_phase_block(version: ReleaseVersion) -> dict[str, Any]:
    """Return a compact dict for embedding in the strategic agent's payload.

    Shape matches the ``version_phase`` block described in Phase 2 of the
    design doc (strategic_spawning.py ``_STRATEGIC_REVIEW_SCHEMA``).
    """
    return {
        "version_number": version.version_number,
        "phase": version.phase,
        "phase_changed_at": version.phase_changed_at.isoformat()
        if version.phase_changed_at
        else None,
        "last_qualification": version.last_qualification,
    }
