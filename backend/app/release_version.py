"""ReleaseVersion table helpers.

Phase 2 of the automain LTS design (docs/plans/automain-lts-plan-v1.md):
a "version" is the span of automain between two promotions.  This module
is the single source of truth for version reads/writes.

The strategic agent and operators drive phase transitions (build → stabilize);
the qualifier (Phase 3) drives status transitions (open → promoted).

Public API:

  open_version(session, project_id, *, version_number, base_sha, phase, goal)
      Insert a new open version row (idempotent via UNIQUE constraint).

  current_open_version(session, project_id)
      Return the single ``status="open"`` row for a project, or None.

  ensure_initial_release_version(session, project_id, *, integration_branch, base_sha, goal, promotion_target)
      Bootstrap version 1 in the ``stabilize`` phase when no version row
      exists for the project and the integration branch differs from the
      promotion target (default "main").
      Idempotent: no-op if any version row already exists.  Caller must commit.

  count_merged_since(session, project_id, since)
      Count tasks whose PR was merged after ``since`` for a project.

  recent_closed_versions(session, project_id, limit=5)
      Return the most recently closed/promoted versions for a project,
      newest first.  Used by the operator version-lifecycle panel.

  set_version_phase(version, phase, *, reasoning)
      Update the phase on an open version row (build ↔ stabilize).
      Stamps ``phase_changed_at`` to now and records the caller's reasoning.
      Caller is responsible for session.add(version) and committing.

  to_phase_block(version)
      Return a compact dict for embedding the version's phase state in the
      strategic agent's payload.
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

# Cap on the persisted version ``goal`` — a snapshot of intent.current_focus,
# which is meant to be a concise statement.  Mirrors _PHASE_REASONING_MAX.
_GOAL_MAX = 500


async def open_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_number: int,
    base_sha: str | None = None,
    phase: str = "build",
    goal: str | None = None,
) -> ReleaseVersion:
    """Insert a new open ``ReleaseVersion`` row for ``project_id``.

    Caller is responsible for committing the session.  The UNIQUE constraint
    on (project_id, version_number) prevents duplicates — callers should
    ensure the previous open version has been closed (status promoted or
    abandoned) before calling this.

    ``base_sha`` is the automain HEAD SHA at the time the version is opened;
    nullable per migration linter rules.

    ``phase`` defaults to ``"build"``; pass ``"stabilize"`` to bootstrap a
    version over an already-diverged trunk that is ready to qualify.

    ``goal`` is a point-in-time snapshot of ``ProblemModel.intent.current_focus``
    — what this version is *for*.  Stored verbatim (truncated to ``_GOAL_MAX``)
    and frozen for the version's lifetime; ``None`` when the project has no
    current focus.
    """
    if phase not in _VALID_PHASES:
        raise ValueError(
            f"[RELEASE_VERSION] Invalid phase {phase!r}; "
            f"must be one of {sorted(_VALID_PHASES)}"
        )
    version = ReleaseVersion(
        project_id=project_id,
        version_number=version_number,
        phase=phase,
        status="open",
        opened_at=_utcnow(),
        base_sha=base_sha,
        goal=goal[:_GOAL_MAX] if goal else None,
    )
    session.add(version)
    logger.info(
        "[RELEASE_VERSION] opened version=%d project=%s sha=%s phase=%s",
        version_number,
        project_id,
        base_sha,
        phase,
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


async def ensure_initial_release_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    integration_branch: str,
    base_sha: str | None = None,
    goal: str | None = None,
    promotion_target: str = "main",
) -> ReleaseVersion | None:
    """Bootstrap version 1 when no ReleaseVersion row exists for the project.

    Opens the version in the ``stabilize`` phase (not ``build``) because a
    bootstrap over an already-diverged integration trunk represents accumulated
    work that is ready to be qualified immediately, not built from scratch.
    Waiting out the build→stabilize stall gate (~14 days) would delay the
    first real qualification verdict indefinitely.

    Returns ``None`` (no-op) when:
    - ``integration_branch == promotion_target`` — same-branch LTS is degenerate;
      the qualifier's ``promotion_target…HEAD`` diff would be empty.
    - Any ``ReleaseVersion`` row already exists — the lifecycle has already
      started (either open, or previously promoted / abandoned).

    ``promotion_target`` is the configured deploy/LTS branch (default ``"main"``).
    Callers should pass the project's resolved promotion target so the degenerate
    check is correct for repos whose deploy branch is not literally ``"main"``.

    ``goal`` snapshots ``ProblemModel.intent.current_focus`` onto the new
    version (see :func:`open_version`).

    Caller is responsible for committing the session.
    """
    if integration_branch == promotion_target:
        return None

    existing_open = await current_open_version(session, project_id)
    if existing_open is not None:
        return None

    count_stmt = (
        select(func.count())
        .select_from(ReleaseVersion)
        .where(ReleaseVersion.project_id == project_id)
    )
    count = int((await session.execute(count_stmt)).scalar() or 0)
    if count > 0:
        return None

    logger.info(
        "[RELEASE_VERSION] bootstrapping initial version 1 for project=%s branch=%s sha=%s",
        project_id,
        integration_branch,
        base_sha,
    )
    return await open_version(
        session,
        project_id,
        version_number=1,
        base_sha=base_sha,
        phase="stabilize",
        goal=goal,
    )


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


async def recent_closed_versions(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int = 5,
) -> list[ReleaseVersion]:
    """Return the most recently closed (non-open) ``ReleaseVersion`` rows.

    Results are ordered newest-first by ``opened_at``.  Returns at most
    ``limit`` rows (default 5).  Projects with no version history return an
    empty list.  Used by the operator version-lifecycle panel to show the
    confidence trend over recent versions.
    """
    stmt = (
        select(ReleaseVersion)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.status != "open",
        )
        .order_by(ReleaseVersion.opened_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def to_phase_block(version: ReleaseVersion) -> dict[str, Any]:
    """Return a compact dict for embedding in the strategic agent's payload.

    Shape matches the ``version_phase`` block described in Phase 2 of the
    design doc (strategic_spawning.py ``_STRATEGIC_REVIEW_SCHEMA``).
    """
    return {
        "version_number": version.version_number,
        "phase": version.phase,
        "goal": version.goal,
        "phase_changed_at": version.phase_changed_at.isoformat()
        if version.phase_changed_at
        else None,
        "phase_reasoning": version.phase_reasoning,
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
