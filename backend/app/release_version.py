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

  set_version_phase(session, version, phase, *, reasoning)
      Update the phase on an open version row (build ↔ stabilize).
      Stamps ``phase_changed_at`` to now and records the agent's reasoning.
      Caller must commit.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models import ReleaseVersion, Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_VALID_PHASES = frozenset({"build", "stabilize"})
_PHASE_REASONING_MAX = 500


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


def _pr_merged_predicate(session: AsyncSession) -> ColumnElement[bool]:
    """Return a dialect-appropriate ``task.context['pr_merged'] is true`` filter.

    ``Task.context`` is already a JSONB column (see ``models.py``), so the
    predicate never casts the column — only the literal, matching the codebase
    pattern in ``anomaly_detection.py``.

    - Postgres: ``context @> '{"pr_merged": true}'::jsonb`` — index-friendly
      containment that ``ix_task_context_gin`` can serve.
    - SQLite (test fixtures): ``json_extract(context, '$.pr_merged')`` reads
      the boolean as a primitive (``1`` for ``true``), so an equality check
      against ``True`` matches the Python-written boolean value.

    Only the canonical boolean ``True`` is matched — the convention
    throughout admin.py / rollup_management.py is ``context["pr_merged"] = True``;
    there is no string ``"true"`` form to accommodate.
    """
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    if dialect_name == "sqlite":
        return func.json_extract(Task.context, "$.pr_merged") == True  # noqa: E712
    return Task.context.op("@>")(cast({"pr_merged": True}, JSONB))


async def count_merged_since(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
) -> int:
    """Count tasks for ``project_id`` whose PR was merged at or after ``since``.

    "Merged" is defined as ``task.status = COMPLETED`` AND
    ``task.context['pr_merged'] is true`` AND ``task.updated_at >= since``.
    This mirrors the convention established throughout admin.py and
    rollup_management.py where a successful merge sets
    ``task.context["pr_merged"] = True``.

    The ``pr_merged`` filter is applied on both Postgres and SQLite via
    :func:`_pr_merged_predicate` — a COMPLETED task without ``pr_merged``
    is never counted on either dialect.

    ``updated_at`` is used as a proxy for merge time because no dedicated
    merged-at column exists on Task.  It is set via ``onupdate`` so it
    reflects the last status transition, which for a newly-merged task is
    the merge itself.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.updated_at >= since,
            _pr_merged_predicate(session),
        )
    )
    result = await session.execute(stmt)
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


def set_version_phase(
    version: ReleaseVersion,
    phase: str,
    *,
    reasoning: str | None = None,
) -> None:
    """Update the phase on an open ``ReleaseVersion`` row.

    Stamps ``phase_changed_at`` to now and records ``reasoning`` in
    ``phase_reasoning``.  No-ops silently when ``phase`` is already set
    to the requested value (idempotent).

    Caller is responsible for ``session.add(version)`` and committing.

    Raises ``ValueError`` when ``phase`` is not in the allowed set
    (``build`` | ``stabilize``).
    """
    if phase not in _VALID_PHASES:
        raise ValueError(
            f"[RELEASE_VERSION] Invalid phase {phase!r}; "
            f"must be one of {sorted(_VALID_PHASES)}"
        )
    if version.phase == phase:
        logger.debug(
            "[RELEASE_VERSION] set_version_phase: version v%d project=%s "
            "already in phase=%s — no-op",
            version.version_number,
            version.project_id,
            phase,
        )
        return
    old_phase = version.phase
    version.phase = phase
    version.phase_changed_at = _utcnow()
    if reasoning is not None:
        truncated = reasoning[:_PHASE_REASONING_MAX]
        version.phase_reasoning = truncated
    logger.info(
        "[RELEASE_VERSION] phase transition %s→%s for version v%d project=%s",
        old_phase,
        phase,
        version.version_number,
        version.project_id,
    )
