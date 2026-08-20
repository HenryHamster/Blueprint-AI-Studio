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
      promotion target.  ``promotion_target`` is required — callers must
      resolve the project's configured deploy branch explicitly.
      Idempotent: no-op if any version row already exists.  Caller must commit.

  count_merged_since(session, project_id, since, until=None)
      Count tasks whose PR was merged after ``since`` for a project.
      Optional ``until`` closes the upper bound (for closed versions).

  compute_version_acceptance_aggregate(session, project_id, *, since, until=None)
      Classify merged-since tasks by acceptance-evidence outcome
      using the canonical acceptance aggregate buckets.
      Optional ``until`` closes the upper bound (for closed versions).

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

  build_version_highlights(version, *, merged_count, aggregate_serving_status)
      Build the shared human-readable ``highlights`` block (headline plus
      at-a-glance metrics) for a version.  The 'serving' word in the headline is
      driven off the aggregate required-artifact serving status, not the scalar
      column.  Used by both the per-version summary endpoint and the
      version-lifecycle current-version block so they render identical text.
"""

from __future__ import annotations


import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.acceptance_gate import (
    ACCEPTANCE_AGGREGATE_BUCKETS,
    classify_merged_acceptance_context,
)
from app.landed_signal import landed_signal_predicate
from app.models import Artifact, ArtifactType, Project, ReleaseVersion, Task, TaskStatus
from app.release_audit_child_state import _audit_child_crashed
from app.task_surface_visibility import non_resident_infrastructure_task_filter
from app.time_utils import _coerce_naive_utc, _utcnow
_MODULE_SIZE_GOVERNANCE_CAP: int = 1839

logger = logging.getLogger(__name__)

_VALID_PHASES = frozenset({"build", "stabilize"})
_PHASE_REASONING_MAX = 500

# Fraction of the promotion timeout cap past which a pending promotion is
# flagged as ``promotion_nearing_timeout`` (so operators/strategist get a warning
# before the reconciliation sweep force-reverts it to stabilize).
_PROMOTION_NEARING_TIMEOUT_FRACTION = 0.8

# Qualification sub-states that mean "a promotion PR is open and the version is
# mid-promotion" — re-qualifying, cutting, or flipping phase here would clobber
# the promotion-pending provenance the reconciler relies on, so every operator
# lever (qualify duplicate-block, phase-flip 409, version-cut 409) refuses on
# this set. Every persisted promotion PR and the short pre-PR claim are owned by
# the same managed check-wait state. Exporting one frozenset keeps all guards in
# agreement by construction.
PROMOTION_BLOCKING_STATES = frozenset({"promotion_check_wait"})

# Exhaustive vocabulary of ``state`` values returned by
# ``summarize_qualification_state``.  Pin this against the function body so
# callers can assert membership rather than re-declaring their own subsets.
QUALIFICATION_STATE_VOCAB: frozenset[str] = frozenset(
    {
        "qualifying",
        "audit_crashed",
        "promotion_check_wait",
        "promotion_failed",
        "required_check_failed",
        "protection_blocked_review",
        "not_promoted",
        "dry_run",
        "promoted",
        "promoted_unconfirmed",
        "promotion_deploy_failed",
        "promotion_confirmation_timed_out",
        "deferred_unsynced",
        "error",
        "infra_failure",
        # Recoverable rubric holds: the acceptance rubric changed mid-flight or
        # provenance could not be reconstructed. A fresh grade resolves either.
        "rubric_changed",
        "rubric_provenance_unknown",
    }
)

# Cap on the persisted version ``goal`` — a snapshot of intent.current_focus,
# which is meant to be a concise statement.  Mirrors _PHASE_REASONING_MAX.
_GOAL_MAX = 500


class VersionCutBlockedByPromotion(RuntimeError):
    """Raised when an operator tries to cut a version with a pending promotion."""

    def __init__(
        self,
        promotion_pr_number: int | None = None,
        promotion_pr_url: str | None = None,
        promotion_freshness: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("promotion PR is awaiting managed checks and merge")
        self.promotion_pr_number = promotion_pr_number
        self.promotion_pr_url = promotion_pr_url
        # Additive freshness context (age / timeout) so the version-cut 409
        # can surface elapsed age and near/past-timeout state. ``None`` when the
        # promotion_pending record had no usable ``enabled_at``.
        self.promotion_freshness = (
            dict(promotion_freshness) if promotion_freshness else None
        )


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

    ``goal`` is a strategist-authored objective for this version — a 1-3
    sentence statement of what the version is for, set by the strategic agent
    in its first cycle after version-open.  Opens as ``None``; the strategic
    completion handler applies ``new_version_goal`` from the agent output.
    Stored verbatim (truncated to ``_GOAL_MAX``) and frozen once set; operator
    can override via ``POST /api/admin/projects/{id}/version-goal``.
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
    # A version that is only now being opened cannot yet have any
    # ReleaseDeployArtifact rows pointing at it. Setting the collection
    # explicitly marks it loaded, so the synchronous readers that rely on the
    # eager-load contract (e.g. version_serving_summary's
    # `getattr(version, "deploy_artifacts", None)`) never need a lazy load on
    # this instance — once it is flushed and persistent, an unloaded
    # collection would emit one from async context (MissingGreenlet). The
    # session factory sets expire_on_commit=False, so a commit does not undo
    # this. Covered by
    # testing/test_release_version.py::test_open_version_deploy_artifacts_readable_without_lazy_load.
    version.deploy_artifacts = []
    session.add(version)
    from app.acceptance_criteria import ensure_operator_authorship_criterion

    await ensure_operator_authorship_criterion(session, project_id, version.id)
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
    *,
    include_artifacts: bool = False,
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
    if include_artifacts:
        stmt = stmt.options(selectinload(ReleaseVersion.deploy_artifacts))
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
    promotion_target: str,
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

    ``promotion_target`` is the configured deploy/LTS branch (required — no
    default).  Callers must resolve the project's promotion target explicitly
    so the degenerate check is correct for repos whose deploy branch is not
    literally ``"main"``.

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


async def count_merged_since(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> int:
    """Count tasks for ``project_id`` whose PR was merged at or after ``since``.

    "Merged" is defined as ``task.status = COMPLETED`` AND the task carrying
    the canonical integration-landing signal at or after ``since``.

    The stable terminal timestamp comes from the last status transition in
    ``status_log`` when available, with ``created_at`` as the legacy fallback.
    That keeps post-terminal writes (for example consumption-proof updates to
    a merged producer task) from re-opening the version window.

    ``until`` (optional) closes the upper bound — tasks whose stable terminal
    timestamp is strictly after ``until`` are excluded.  Used to bound closed
    (promoted/abandoned) versions to their own span so tasks that merged in
    later versions are not double-counted.
    """
    common_filters = [
        Task.project_id == project_id,
        Task.status == TaskStatus.COMPLETED,
        landed_signal_predicate(session),
        Task.updated_at >= since,
    ]
    modern_filters = [
        *common_filters,
        Task.completed_at.isnot(None),
        Task.completed_at >= since,
    ]
    if until is not None:
        modern_filters.append(Task.completed_at <= until)
    modern_count = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(*modern_filters)
            )
        ).scalar_one()
    )

    # completed_at has been stamped centrally since migration 0223 and is
    # covered by (project_id, status, completed_at). Keep the JSON status-log
    # fallback isolated to genuinely legacy rows instead of transferring every
    # landed task's full audit history to Python on every version poll.
    legacy_stmt = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status_log,
                Task.created_at,
                Task.completed_at,
                raiseload=True,
            )
        )
        .where(*common_filters, Task.completed_at.is_(None))
    )
    legacy_rows = (await session.execute(legacy_stmt)).scalars().all()
    legacy_count = sum(
        1
        for task in legacy_rows
        if _task_completed_at(task) >= since
        and (until is None or _task_completed_at(task) <= until)
    )
    return modern_count + legacy_count


async def list_merged_tasks_for_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """List tasks merged in the version window, newest-completion-first.

    Operates over the same predicate as :func:`count_merged_since` —
    ``COMPLETED`` tasks carrying the canonical landing signal whose stable
    terminal timestamp is at or after ``since`` (and at or before ``until``
    when supplied).

    Returns a list of dicts with ``task_type``, ``title``, and ``pr_number``
    per landed task.  Empty when no tasks have merged in the window.
    """
    common_filters = [
        Task.project_id == project_id,
        Task.status == TaskStatus.COMPLETED,
        landed_signal_predicate(session),
        Task.updated_at >= since,
    ]
    modern_filters = [
        *common_filters,
        Task.completed_at.isnot(None),
        Task.completed_at >= since,
    ]
    if until is not None:
        modern_filters.append(Task.completed_at <= until)
    modern_stmt = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.payloads,
                Task.created_at,
                Task.completed_at,
                raiseload=True,
            )
        )
        .where(*modern_filters)
    )
    modern_rows = (await session.execute(modern_stmt)).scalars().all()

    legacy_stmt = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.payloads,
                Task.status_log,
                Task.created_at,
                Task.completed_at,
                raiseload=True,
            )
        )
        .where(*common_filters, Task.completed_at.is_(None))
    )
    legacy_rows = (await session.execute(legacy_stmt)).scalars().all()

    # Filter to the version window first so the artifact query is scoped to
    # only the tasks that will appear in the changelog.
    windowed: list[tuple[datetime, Task]] = []
    for task in [*modern_rows, *legacy_rows]:
        completed_at = _task_completed_at(task)
        if completed_at < since:
            continue
        if until is not None and completed_at > until:
            continue
        windowed.append((completed_at, task))

    # Batch-fetch PULL_REQUEST artifacts for the windowed task set so each
    # changelog entry can carry a clickable pr_url without N+1 queries.
    pr_url_by_task: dict = {}
    if windowed:
        task_ids = [task.id for _, task in windowed]
        artifact_stmt = select(Artifact.task_id, Artifact.pr_url).where(
            Artifact.task_id.in_(task_ids),
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
        artifact_rows = (await session.execute(artifact_stmt)).all()
        pr_url_by_task = {row.task_id: row.pr_url for row in artifact_rows}

    entries: list[tuple[datetime, dict[str, Any]]] = []
    for completed_at, task in windowed:
        payload = task.first_payload
        entries.append(
            (
                completed_at,
                {
                    "task_type": payload.get("type"),
                    "title": task.title,
                    "pr_number": payload.get("pr_number"),
                    "pr_url": pr_url_by_task.get(task.id),
                },
            )
        )
    entries.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in entries]


async def compute_version_acceptance_aggregate(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, int]:
    """Classify version-window merged tasks by acceptance-evidence outcome.

    Operates over the same merged-since window as :func:`count_merged_since`
    (``COMPLETED`` tasks carrying the canonical landing signal whose stable
    terminal timestamp is at or after ``since``, and at or before ``until``
    when supplied).  For each landed task it reads the durable ``Task.context``
    and delegates to
    :func:`app.acceptance_gate.classify_merged_acceptance_context`, so the
    version-window aggregate uses the same bucket vocabulary as the Task-object
    classifier used by final qualification storage.

    Returns the canonical six bucket counts plus ``total``.  A malformed
    recorded verdict is surfaced as ``malformed`` for visibility, but readiness
    treats it as advisory only.
    """
    filters = [
        Task.project_id == project_id,
        Task.status == TaskStatus.COMPLETED,
        landed_signal_predicate(session),
        Task.updated_at >= since,
    ]
    stmt = select(Task).where(*filters)
    rows = [
        task
        for task in (await session.execute(stmt)).scalars().all()
        if _task_completed_at(task) >= since
        and (until is None or _task_completed_at(task) <= until)
    ]

    aggregate: dict[str, int] = {bucket: 0 for bucket in ACCEPTANCE_AGGREGATE_BUCKETS}
    for task in rows:
        bucket = classify_merged_acceptance_context(
            task.context if isinstance(task.context, dict) else {}
        )
        aggregate[bucket] += 1

    aggregate["total"] = len(rows)
    return aggregate


def _task_completed_at(task: Task) -> datetime:
    """Return the stable terminal timestamp for a task.

    Prefers the durable ``Task.completed_at`` column, which is centrally stamped
    on terminal status transitions and cleared on revive (see models.py). It is
    immune to post-terminal ``latest_status_message`` writes, which append audit
    rows to ``status_log`` and would otherwise move the version window forward.

    IMPORTANT: ``task.completed_at`` must be present in the ``load_only(...)``
    column list of every query whose rows flow here; under ``raiseload=True`` an
    unloaded column raises rather than lazy-loading.

    When ``completed_at`` is null (legacy rows never stamped), falls back to the
    prior behavior: the last ``Status changed: `` transition entry in
    ``status_log`` when available, else ``created_at``. Message-only writes append
    to ``status_log`` too, so the legacy cutoff must ignore those audit entries.
    """

    def _strip_tz(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    durable = task.completed_at
    if durable is not None:
        return _strip_tz(durable)

    for entry in reversed(task.status_log or []):
        if not isinstance(entry, dict):
            continue
        msg = entry.get("msg")
        if not isinstance(msg, str) or not msg.startswith("Status changed: "):
            continue
        try:
            return _strip_tz(datetime.fromisoformat(entry["t"]))
        except (KeyError, TypeError, ValueError):
            continue
    completed_at = task.created_at
    return _strip_tz(completed_at)


# Non-terminal task statuses — work that is still "in flight" against a version.
# Mirrors the active-status set used by the idempotency partial index in
# models.py (alembic 0138): QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING/BLOCKED/PARKED.
# COMPLETED/FAILED/CANCELLED are terminal and excluded.
_IN_FLIGHT_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)


async def count_in_flight_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Count ordinary ``project_id`` work still in flight (non-terminal status).

    "In flight" means the task is in a non-terminal status
    (``QUEUED`` / ``PROVISIONING`` / ``IN_PROGRESS`` / ``EVALUATING`` /
    ``BLOCKED`` / ``PARKED``) — work that a version cut does NOT stop. Such
    ordinary tasks keep running against the freshly-opened next version. The
    confirmation surfaces that consequence. Resident generations are audit records,
    not independent work, and are excluded from this ordinary-work consequence.
    Terminal statuses are already resolved and excluded.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(_IN_FLIGHT_TASK_STATUSES),
            non_resident_infrastructure_task_filter(),
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def recent_closed_versions(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int = 5,
    *,
    include_artifacts: bool = False,
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
    if include_artifacts:
        stmt = stmt.options(selectinload(ReleaseVersion.deploy_artifacts))
    return list((await session.execute(stmt)).scalars().all())


_PROMOTION_FRESHNESS_KEYS = (
    "promotion_enabled_at",
    "promotion_age_seconds",
    "promotion_timeout_seconds",
    "promotion_timeout_at",
    "promotion_past_timeout",
    "promotion_nearing_timeout",
)


def _humanize_seconds(seconds: float) -> str:
    """Render a duration as a compact ``Xh Ym`` / ``Xh`` / ``Ym`` string."""
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _promotion_pending_freshness(promo: dict[str, Any]) -> dict[str, Any]:
    """Derive additive age / timeout context for a pending promotion.

    Reads ``promotion_pending.enabled_at`` (the moment the managed wait began) and
    the SAME configurable cap the reconciliation sweep uses to force-fail a
    stranded ``promotion_pending`` (``_promotion_pending_timeout_seconds``), so
    every consumer of the shared qualification summary agrees by construction on
    elapsed age and whether the promotion is nearing or past its timeout.

    Returns ``{}`` (today's timeless behavior) when ``enabled_at`` is missing or
    unparseable — callers fall back to the PR-number-only summary rather than
    erroring.
    """
    enabled_at_raw = promo.get("enabled_at")
    if not isinstance(enabled_at_raw, str) or not enabled_at_raw:
        return {}
    try:
        enabled_at = _coerce_naive_utc(datetime.fromisoformat(enabled_at_raw))
    except (ValueError, TypeError):
        return {}
    # Lazy import: release_reconciliation imports release_version at module load,
    # so a top-level import here would form a cycle.
    from app.release_reconciliation import _promotion_pending_timeout_seconds

    cap_seconds = _promotion_pending_timeout_seconds()
    age_seconds = max(0.0, (_utcnow() - enabled_at).total_seconds())
    past_timeout = age_seconds >= cap_seconds
    nearing_timeout = (not past_timeout) and age_seconds >= (
        cap_seconds * _PROMOTION_NEARING_TIMEOUT_FRACTION
    )
    return {
        "promotion_enabled_at": enabled_at.isoformat(),
        "promotion_age_seconds": age_seconds,
        "promotion_timeout_seconds": cap_seconds,
        "promotion_timeout_at": (
            enabled_at + timedelta(seconds=cap_seconds)
        ).isoformat(),
        "promotion_past_timeout": past_timeout,
        "promotion_nearing_timeout": nearing_timeout,
    }


def format_promotion_freshness(state: Mapping[str, Any] | None) -> str:
    """Return a short operator-facing age / timeout suffix for 409 details.

    Reads the additive freshness keys carried on the shared qualification
    summary (or the version-cut exception). Returns ``""`` when no freshness is
    available (missing/invalid ``enabled_at``) so messages fall back unchanged.
    """
    if not state:
        return ""
    age = state.get("promotion_age_seconds")
    if age is None:
        return ""
    phrase = f"waiting ~{_humanize_seconds(age)}"
    if state.get("promotion_past_timeout"):
        phrase += ", past timeout"
    elif state.get("promotion_nearing_timeout"):
        phrase += ", nearing timeout"
    return f" — {phrase}"


def summarize_qualification_state(
    last_qualification: dict[str, Any] | None,
    serving_status: str | None = None,
    deploy_failure_reason: str | None = None,
    *,
    audit_child: dict[str, Any] | None = None,
    aggregate_serving_status: str | None = None,
    aggregate_deploy_failure_reason: str | None = None,
    artifact_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Disambiguate the overloaded ``last_qualification.status`` for the agent.

    ``status == "pending"`` collapses two very different situations:

    * the qualification **audit is still scoring** the candidate (wait), and
    * the audit **PASSED and a managed promotion is pending** —
      ``promote()`` records a ``promotion_pending`` sub-key for this case. It is
      NOT "still auditing": the promotion PR can fail its own CI and bounce the
      version back to stabilize.

    The strategic agent reads ``last_qualification`` as the source of truth for
    "what is happening with qualification", so a bare ``status="pending"`` made
    it conflate "audit in flight, wait" with "audit passed, promotion failing"
    and burn whole cycles waiting (incident 2026-06-02). This derives a single
    explicit sub-state from the raw verdict — pure data, no I/O.

    Returns ``{"state", "summary", ...}`` where ``state`` ∈
    ``QUALIFICATION_STATE_VOCAB`` (``qualifying`` | ``audit_crashed`` |
    ``promotion_check_wait`` | ``promotion_failed`` |
    ``required_check_failed`` | ``protection_blocked_review`` | ``not_promoted`` |
    ``dry_run`` | ``promoted`` | ``promoted_unconfirmed`` |
    ``promotion_deploy_failed`` | ``promotion_confirmation_timed_out`` |
    ``deferred_unsynced``), or ``None`` when unqualified (no verdict yet).

    Audit-crash reconciliation: when ``status == "pending"`` with no
    ``promotion_pending`` sub-key, the bare result was always ``qualifying``
    ("audit in flight, wait"). But the audit child can crash (terminal-failed,
    no report) while the verdict is still pending — and the operator panel's
    audit-findings section already reads that crashed status, so the two
    surfaces contradicted each other. When the optional ``audit_child`` envelope
    (the version-scoped ``latest_audit`` dict carrying ``task_status``/``report``)
    shows a crash, return ``audit_crashed`` instead of ``qualifying`` so neither
    the panel badge nor the strategic payload tells the reader to wait for a dead
    audit. A still-running smoketest child does NOT mask an already-crashed audit
    child. ``audit_child=None`` preserves the prior behavior for callers without
    the signal.

    Post-promotion serving disambiguation: when ``status == "promoted"`` the
    optional ``serving_status`` (with ``deploy_failure_reason``) splits the bare
    "promoted" case into serving-confirmed, unconfirmed-in-window, a confirmation
    timeout (``promotion_confirmation_timed_out`` — window elapsed, no active
    failure), or a distinct ``promotion_deploy_failed`` state (an ACTIVE
    failure).  This is the SINGLE summary the UI,
    operator-control guards, and the strategic payload all read, so they agree
    on whether a promotion actually shipped by construction.
    """
    lq = last_qualification or {}
    status = lq.get("status")
    if not lq or status is None:
        return None
    if status == "pending":
        promo = lq.get("promotion_pending")
        if isinstance(promo, dict):
            # A bare CLAIM (``claiming`` with no ``pr_number``) is written
            # under-lock by promote() before its GitHub I/O so a concurrent
            # re-qualify can't clobber the pending promotion — there is NO PR
            # yet.  Report that honestly rather than as "a promotion PR is open".
            _is_claim = bool(promo.get("claiming")) and not promo.get("pr_number")
            if _is_claim:
                summary = (
                    "Audit PASSED; a promotion is STARTING — promote() is cutting "
                    "the release branch and opening the PR (no PR number yet). "
                    "This is NOT 'still auditing' and is NOT a stuck promotion; it "
                    "resolves within the promotion I/O window and re-qualification "
                    "is blocked until then."
                )
            else:
                summary = (
                    "Audit PASSED; a promotion PR is open. Blueprint is WAITING "
                    "for the promotion target's required checks and will "
                    "revalidate the exact base/head pair and retry the merge once "
                    "they pass. This is NOT 'still auditing' and does not need a "
                    "fresh qualification."
                )
            freshness = _promotion_pending_freshness(promo)
            # Skip timeout messaging for a bare claim: its GC bound is
            # the short claim timeout, not the 24h promotion-pending
            # freshness reports, so that text would misstate the deadline.
            if freshness and not _is_claim:
                age_str = _humanize_seconds(freshness["promotion_age_seconds"])
                cap_str = _humanize_seconds(freshness["promotion_timeout_seconds"])
                summary += f" Waiting ~{age_str} (timeout cap {cap_str})."
                if freshness["promotion_past_timeout"]:
                    summary += (
                        " It has PASSED the timeout — the reconciliation "
                        "sweep will force-fail it back to stabilize."
                    )
                elif freshness["promotion_nearing_timeout"]:
                    summary += (
                        " It is NEARING the timeout that reverts it to stabilize."
                    )
            result: dict[str, Any] = {
                "state": "promotion_check_wait",
                "summary": summary,
                "promotion_pr_number": promo.get("pr_number"),
                "promotion_pr_url": promo.get("pr_url"),
                **freshness,
            }
            if promo.get("check_snapshot") is not None:
                result["check_snapshot"] = promo.get("check_snapshot")
            if promo.get("ci_state") is not None:
                result["ci_state"] = promo.get("ci_state")
            return result
        if _audit_child_crashed(audit_child):
            return {
                "state": "audit_crashed",
                "summary": (
                    "The qualification audit child CRASHED (terminal-failed with "
                    "no report) while the verdict is still pending. The audit is "
                    "DEAD — do NOT treat this as 'audit in flight' and do NOT wait "
                    "for a verdict from it; it will never produce one. Re-run the "
                    "qualifier on a fresh candidate to get a new audit."
                ),
            }
        return {
            "state": "qualifying",
            "summary": (
                "Qualification audit is in flight (scoring the candidate). Wait "
                "for the verdict — re-requesting while one is pending is ignored."
            ),
        }
    if status == "promotion_failed":
        reason = lq.get("error")
        detail = lq.get("detail")
        if reason == "required_check_failed":
            # The managed check-wait reconciler observed RED required checks on
            # the promotion PR. The failing check names are in ``detail``. A
            # fresh qualify will not help unless the underlying check failure is
            # fixed first.
            return {
                "state": "required_check_failed",
                "summary": (
                    "The promotion PR's REQUIRED status checks FAILED"
                    + (f" — {detail}" if detail else "")
                    + ". The version is back in stabilize. This needs the FAILING "
                    "checks fixed on a fresh candidate, then a re-qualify — "
                    "re-qualifying without addressing the failing checks will not "
                    "unblock it."
                ),
                "error": reason,
                "detail": detail,
            }
        if reason == "protection_blocked_review":
            # Green required checks but the merge was refused by branch
            # protection (e.g. a required approving review is missing). This is
            # a HUMAN action — re-qualifying cannot satisfy it.
            return {
                "state": "protection_blocked_review",
                "summary": (
                    "The promotion PR passed its required checks but the merge "
                    "was REFUSED by branch protection"
                    + (f" — {detail}" if detail else "")
                    + ". A HUMAN branch-protection action is required (e.g. an "
                    "approving review). Re-qualifying will NOT help and will not "
                    "unblock the merge — resolve the branch-protection condition "
                    "in GitHub."
                ),
                "error": reason,
                "detail": detail,
            }
        return {
            "state": "promotion_failed",
            "summary": (
                "The last promotion FAILED after a PASSING audit"
                + (f" ({reason})" if reason else "")
                + ". The version is back in stabilize. This is not a pending "
                "audit — it needs a FRESH qualify on a candidate synced with the "
                "promotion target; waiting for a verdict will not unblock it."
            ),
            "error": reason,
            "detail": detail,
        }
    if status == "not_promoted":
        conf = lq.get("confidence")
        thr = lq.get("threshold")
        sub_scores = lq.get("sub_scores")
        resolved = lq.get("resolved_dimensions")
        retired_smoke_participated = bool(
            lq.get("smoketest_enabled") is True
            or (isinstance(sub_scores, dict) and "smoketest" in sub_scores)
            or (isinstance(resolved, dict) and "smoketest" in resolved)
            or lq.get("blocker") == "smoketest_startup_failed"
        )
        if retired_smoke_participated:
            return {
                "state": "not_promoted",
                "summary": (
                    "This historical not-promoted verdict is STALE because retired "
                    "qualifier VM smoke participated in it. Do not remediate or wait "
                    "on the retired smoke gate; re-qualify under the current audit + "
                    "test-erosion policy."
                ),
                "stale": True,
                "stale_reason": "retired_qualifier_smoketest",
                # Preserve raw archival evidence for operator diagnosis without
                # presenting the retired blocker as current guidance.
                "blocker": lq.get("blocker"),
                "detail": lq.get("detail"),
                "sub_scores": sub_scores,
            }
        return {
            "state": "not_promoted",
            "summary": (
                "Candidate scored BELOW the promotion threshold"
                + (f" (confidence {conf}, threshold {thr})" if conf is not None else "")
                + ". Resolve the binding blocker, then re-qualify."
            ),
            "blocker": lq.get("blocker"),
        }
    if status == "dry_run":
        return {
            "state": "dry_run",
            "summary": (
                "Candidate PASSED qualification in dry-run (promotion disabled). "
                "It is promotion-ready — no further audit work is needed to clear "
                "the bar; enabling promotion is the remaining step."
            ),
        }
    if status == "promoted":
        effective_serving_status = (
            aggregate_serving_status
            if artifact_evidence is not None
            else serving_status
        )
        effective_failure_reason = (
            aggregate_deploy_failure_reason
            if artifact_evidence is not None
            else deploy_failure_reason
        )
        artifact_fields = {"artifacts": artifact_evidence} if artifact_evidence else {}
        if effective_serving_status == "serving_confirmed":
            return {
                "state": "promoted",
                "serving_status": "serving_confirmed",
                "summary": (
                    "Candidate was promoted AND its deploy is CONFIRMED serving "
                    "in production — the version actually shipped."
                ),
                **artifact_fields,
            }
        if effective_serving_status == "deploy_failed":
            return {
                "state": "promotion_deploy_failed",
                "serving_status": "deploy_failed",
                "deploy_failure_reason": effective_failure_reason,
                "summary": (
                    "Candidate merged to the promotion target but its deploy "
                    "NEVER confirmed serving"
                    + (
                        f" ({effective_failure_reason})"
                        if effective_failure_reason
                        else ""
                    )
                    + ". This promotion did NOT ship — the deployed code is not "
                    "live. Treat the version as NOT shipped: investigate the "
                    "deploy / roll forward a fix rather than advancing as if it "
                    "succeeded."
                ),
                **artifact_fields,
            }
        if effective_serving_status == "unconfirmed":
            return {
                "state": "promoted_unconfirmed",
                "serving_status": "unconfirmed",
                "summary": (
                    "Candidate was promoted (merged) but serving is NOT yet "
                    "confirmed — the deploy is still within its confirmation "
                    "window. Do not treat it as live-confirmed yet; the sweep "
                    "resolves it to serving_confirmed, confirmation_timed_out, "
                    "or deploy_failed."
                ),
                **artifact_fields,
            }
        if effective_serving_status == "confirmation_timed_out":
            return {
                "state": "promotion_confirmation_timed_out",
                "serving_status": "confirmation_timed_out",
                "deploy_failure_reason": effective_failure_reason,
                "summary": (
                    "Candidate merged to the promotion target, but automated "
                    "serving confirmation TIMED OUT within the window"
                    + (
                        f" ({effective_failure_reason})"
                        if effective_failure_reason
                        else ""
                    )
                    + ". This is DIFFERENT from a failed deploy: NO active "
                    "failure (rollback / halt / sha-mismatch) was observed — the "
                    "promoted SHA simply could not be independently confirmed "
                    "serving in time (often an observability gap, e.g. the live "
                    "deploy SHA could not be read). The code likely shipped but "
                    "is UNVERIFIED. Verify serving and confirm rather than "
                    "treating it as a crisis or rolling back; it auto-recovers "
                    "to serving_confirmed if the SHA is later observed serving."
                ),
                **artifact_fields,
            }
        return {
            "state": "promoted",
            "summary": "Candidate was promoted — the version was cut.",
            **artifact_fields,
        }
    if status == "deferred_unsynced":
        return {
            "state": "deferred_unsynced",
            "summary": (
                "Qualification is DEFERRED: the integration branch hasn't "
                "absorbed the promotion target yet (a back-merge is pending), so "
                "scoring now would cut a candidate that fails the promotion merge. "
                "It auto-retries once synced — no action needed (a bounded "
                "fall-through qualifies anyway if the back-merge stalls)."
            ),
        }
    if status == "error":
        error_sub = lq.get("error")
        detail = lq.get("detail")
        if error_sub == "no_credentials":
            sub_summary = (
                "Qualification FAILED to start: no credentials are configured. "
                "This is a configuration/human problem — fix the credentials, "
                "then re-qualify."
            )
        elif error_sub == "sha_error":
            sub_summary = (
                "Qualification FAILED: the candidate SHA could not be resolved. "
                "Re-qualify on a fresh candidate."
            )
        elif error_sub == "spawn_failed":
            sub_summary = (
                "Qualification FAILED to start: a qualifier child spawn failed. "
                "This is an operational problem — re-qualify to retry."
            )
        else:
            sub_summary = (
                "Qualification FAILED due to a setup or operational error"
                + (f" ({error_sub})" if error_sub else "")
                + ". This is NON-conclusive (no verdict). Re-qualify to retry."
            )
        if detail:
            sub_summary += f" Detail: {detail}."
        return {
            "state": "error",
            "summary": sub_summary,
            "error": error_sub,
            "detail": detail,
        }
    if status == "infra_failure":
        detail = lq.get("detail")
        return {
            "state": "infra_failure",
            "summary": (
                "Qualification audit INFRA FAILED mid-execution — the "
                "infrastructure could not produce a conclusive verdict. "
                "This is NON-conclusive (no score, no promotion decision). "
                "Re-qualify to get a fresh verdict."
                + (f" Detail: {detail}." if detail else "")
            ),
            "detail": detail,
        }
    if status == "rubric_changed":
        # Recoverable hold: the acceptance rubric changed while the blind
        # merge-grade was in flight. A fresh grade will use the new rubric.
        return {
            "state": "rubric_changed",
            "summary": (
                "The acceptance rubric CHANGED while the blind merge-grade was in "
                "flight. This is a RECOVERABLE hold — NOT a failed qualification. "
                "The in-flight grade was cancelled to avoid grading against a stale "
                "rubric. Launch a FRESH grade; it will automatically use the updated "
                "rubric and produce a new verdict."
            ),
            "blocker": lq.get("blocker"),
            "rubric_drift": lq.get("rubric_drift"),
            "rubric_provenance_error": lq.get("rubric_provenance_error"),
        }
    if status == "rubric_provenance_unknown":
        # Recoverable hold: the pending grade's rubric provenance could not be
        # reconstructed. A fresh grade resolves this automatically.
        return {
            "state": "rubric_provenance_unknown",
            "summary": (
                "The pending grade's acceptance rubric PROVENANCE could not be "
                "reconstructed. This is a RECOVERABLE hold — NOT a failed "
                "qualification. The attempt was held rather than graded against an "
                "unverifiable contract. Launch a FRESH grade to get a new verdict "
                "with a fully-verified rubric provenance."
            ),
            "blocker": lq.get("blocker"),
            "rubric_drift": lq.get("rubric_drift"),
            "rubric_provenance_error": lq.get("rubric_provenance_error"),
        }
    logger.warning(
        "[RELEASE_VERSION] summarize_qualification_state: unrecognized status %r; "
        "passing through as-is — update QUALIFICATION_STATE_VOCAB if this is a new "
        "producer status",
        status,
    )
    return {"state": status, "summary": f"Qualification status: {status}."}


def to_phase_block(
    version: ReleaseVersion,
    audit_child: dict[str, Any] | None = None,
    *,
    aggregate_serving_status: str | None = None,
    aggregate_deploy_failure_reason: str | None = None,
    artifact_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a compact dict for embedding in the strategic agent's payload.

    Shape matches the ``version_phase`` block described in Phase 2 of the
    design doc (strategic_spawning.py ``_STRATEGIC_REVIEW_SCHEMA``).

    ``audit_child`` (optional, defaults to ``None`` to preserve existing
    callers/tests) is the version-scoped audit-evidence envelope. When the
    strategic caller resolves the open version's audit child, threading it here
    lets ``qualification_state`` reflect an ``audit_crashed`` reconciliation
    instead of a misleading ``qualifying`` while the audit is dead.
    """
    effective_serving_status = (
        aggregate_serving_status
        if artifact_evidence is not None
        else version.serving_status
    )
    effective_failure_reason = (
        aggregate_deploy_failure_reason
        if artifact_evidence is not None
        else version.deploy_failure_reason
    )
    block = {
        "version_number": version.version_number,
        "phase": version.phase,
        "goal": version.goal,
        "phase_changed_at": version.phase_changed_at.isoformat()
        if version.phase_changed_at
        else None,
        "phase_reasoning": version.phase_reasoning,
        "last_qualification": version.last_qualification,
        "qualification_state": summarize_qualification_state(
            version.last_qualification,
            version.serving_status,
            version.deploy_failure_reason,
            audit_child=audit_child,
            aggregate_serving_status=aggregate_serving_status,
            aggregate_deploy_failure_reason=aggregate_deploy_failure_reason,
            artifact_evidence=artifact_evidence,
        ),
        "serving_status": effective_serving_status,
        "served_sha": version.served_sha,
        "served_at": version.served_at.isoformat() if version.served_at else None,
        "deploy_failure_reason": effective_failure_reason,
    }
    if artifact_evidence is not None:
        block["artifacts"] = artifact_evidence
    return block


def _synthesize_narrative(
    version: ReleaseVersion,
    *,
    merged_count: int,
    aggregate_serving_status: str | None,
    confidence: float | None,
    threshold: float | None,
) -> str:
    """Synthesize a 2-4 sentence plain-text digest of the version's state.

    Pure function — no I/O, no side effects. Called only by
    ``build_version_highlights`` so all summary surfaces render identical text.
    """
    task_word = "task" if merged_count == 1 else "tasks"

    if version.status == "promoted":
        if aggregate_serving_status == "serving_confirmed":
            intro = "Promoted and actively serving."
        else:
            intro = "Promoted."
        if merged_count == 0:
            return f"{intro} No tasks were merged into this version."
        return f"{intro} Delivered {merged_count} merged {task_word}."

    if version.status == "abandoned":
        if merged_count == 0:
            return "Abandoned before promotion with no merged work."
        return f"Abandoned before promotion with {merged_count} merged {task_word}."

    # open
    if merged_count == 0:
        return f"Open in the {version.phase} phase with no merged work yet."

    qual_part = ""
    if confidence is not None and threshold is not None:
        conf_pct = round(confidence * 100)
        thr_pct = round(threshold * 100)
        direction = "meeting" if confidence >= threshold else "below"
        qual_part = f" Qualification confidence is {conf_pct}%, {direction} the {thr_pct}% threshold."
    elif confidence is not None:
        conf_pct = round(confidence * 100)
        qual_part = f" Qualification confidence is {conf_pct}%."

    return (
        f"Open in the {version.phase} phase with {merged_count} merged {task_word}."
        f"{qual_part}"
    )


def build_version_highlights(
    version: ReleaseVersion,
    *,
    merged_count: int,
    aggregate_serving_status: str | None,
) -> dict[str, Any]:
    """Build the human-readable ``highlights`` block for a release version.

    Single source of truth for the version ``headline`` string and its
    at-a-glance metrics, shared by the per-version summary endpoint
    (``get_release_version_summary``) and the version-lifecycle endpoint's
    ``current_version`` block so both surfaces render byte-identical text.

    The 'serving' determination drives off ``aggregate_serving_status`` — the
    truthful required-artifact aggregate read — NOT the scalar
    ``version.serving_status`` column: a promoted version reads
    ``"promoted & serving"`` only when every required artifact confirms,
    otherwise ``"promoted"``.

    Returns a dict with keys ``merged_count``, ``proven``,
    ``total_merged_for_acceptance``, ``qualification_confidence``,
    ``qualification_threshold`` and ``headline``. The retired acceptance metrics
    are kept as zero-valued compatibility fields for existing clients.

    Canonical criterion coverage now drives version acceptance qualification;
    the retired acceptance aggregate no longer contributes to highlights.
    """
    qual = version.last_qualification or {}
    confidence = qual.get("confidence")
    threshold = qual.get("threshold")

    if version.status == "promoted" and aggregate_serving_status == "serving_confirmed":
        headline = "promoted & serving"
    elif version.status == "promoted":
        headline = "promoted"
    elif version.status == "abandoned":
        headline = "abandoned"
    elif merged_count == 0:
        headline = f"open in {version.phase} with no merged work yet"
    else:
        headline = f"open in {version.phase} with {merged_count} merged"

    narrative = _synthesize_narrative(
        version,
        merged_count=merged_count,
        aggregate_serving_status=aggregate_serving_status,
        confidence=confidence,
        threshold=threshold,
    )

    return {
        "merged_count": merged_count,
        "proven": 0,
        "total_merged_for_acceptance": 0,
        "qualification_confidence": confidence,
        "qualification_threshold": threshold,
        "headline": headline,
        "narrative": narrative,
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


def set_version_goal(
    version: ReleaseVersion,
    goal: str | None,
    *,
    editor_id: str | None = None,
    source: str = "operator",
) -> None:
    """Update the goal on an open ``ReleaseVersion`` row.

    Stamps ``goal_updated_at`` to now and records the ``editor_id`` in
    ``goal_editor_info``.  No-ops silently when ``goal`` is already set
    to the requested value (idempotent).

    Caller is responsible for ``session.add(version)`` and committing.

    ``editor_id`` should be a user email or unique identifier; it is prefixed
    with ``[{source}:`` (default ``"operator"``) to match phase_reasoning style.
    Pass ``source="strategist"`` for strategic-agent-authored goals.
    """
    new_goal = goal[:_GOAL_MAX] if goal else None
    if version.goal == new_goal:
        logger.debug(
            "[RELEASE_VERSION] set_version_goal: version v%d project=%s "
            "already set to same goal — no-op",
            version.version_number,
            version.project_id,
        )
        return
    old_goal = version.goal
    version.goal = new_goal
    version.goal_updated_at = _utcnow()
    if editor_id:
        version.goal_editor_info = f"[{source}:{editor_id}]"
    logger.info(
        "[RELEASE_VERSION] goal update for version v%d project=%s "
        "(old: %.50s) (new: %.50s) editor=%s",
        version.version_number,
        version.project_id,
        old_goal or "(none)",
        new_goal or "(none)",
        editor_id or "unknown",
    )


async def refresh_open_version_goal_if_auto_derived(
    session: AsyncSession,
    project_id: uuid.UUID,
    current_focus: str | None,
) -> ReleaseVersion | None:
    """Refresh the open version's goal when it is still auto-derived.

    Called after the strategic agent rewrites ``Project.problem_model.intent.current_focus``
    so the operator-facing Version Lifecycle Goal stays in sync with the new
    version's mission without requiring a manual operator resync.

    No-ops when:
    - ``current_focus`` is None or blank.
    - No open version exists for the project.
    - The version's ``goal_editor_info`` is non-null (operator-edited — never clobber).
    - The version's ``goal`` already equals the new focus (already in sync).

    Does NOT set ``goal_editor_info`` so the goal stays auto-derived.
    Caller is responsible for committing the session.
    """
    if not current_focus or not current_focus.strip():
        return None

    try:
        async with session.begin_nested():
            locked = (
                (
                    await session.execute(
                        select(ReleaseVersion)
                        .where(
                            ReleaseVersion.project_id == project_id,
                            ReleaseVersion.status == "open",
                        )
                        .order_by(ReleaseVersion.opened_at.desc())
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if locked is None:
                return None
            if locked.goal_editor_info is not None:
                # Operator-edited goal — never auto-clobber.
                return None
            new_goal = current_focus[:_GOAL_MAX]
            if locked.goal == new_goal:
                return None

            old_goal = locked.goal
            locked.goal = new_goal
            locked.goal_updated_at = _utcnow()
            session.add(locked)
            logger.info(
                "[RELEASE_VERSION] auto-refresh goal for open version v%d project=%s "
                "(old: %.50s) (new: %.50s)",
                locked.version_number,
                project_id,
                old_goal or "(none)",
                new_goal,
            )
            return locked
    except Exception:
        logger.exception(
            "[RELEASE_VERSION] Failed to refresh open version goal for project %s",
            project_id,
        )
        return None


async def _resolve_integration_head_sha(
    session: AsyncSession,
    project: Project | None,
) -> str | None:
    """Best-effort: resolve the integration branch HEAD SHA for a new version.

    Used to set the ``base_sha`` of the version opened by an operator-initiated
    abandon.  ALL failure modes (no project, no GitHub credentials, branch
    unreachable, any GitHub I/O error) degrade to ``None`` — a SHA-resolution
    failure must NEVER abort the cut, exactly mirroring the best-effort
    promoted-SHA re-resolve in ``release_promotion.promote``.
    """
    if project is None:
        return None
    try:
        from app.github import get_branch_sha
        from app.release_promotion import _resolve_github_with_session
        from app.shared_state import get_project_integration_branch

        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return None
        token, owner, repo = resolved
        branch = get_project_integration_branch(project)
        return await get_branch_sha(token, owner, repo, branch, live=True)
    except Exception as exc:  # noqa: BLE001 — SHA resolution is best-effort
        logger.warning(
            "[RELEASE_VERSION] abandon: could not resolve integration HEAD for "
            "project=%s (non-fatal, falling back): %s",
            getattr(project, "id", "?"),
            exc,
        )
        return None


async def _find_cut_by_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[ReleaseVersion, ReleaseVersion | None] | None:
    """Return the ``(abandoned, successor)`` pair from a prior cut using ``key``.

    The operator idempotency key is stamped into the abandoned version's
    ``last_qualification`` provenance, so a repeated cut carrying the same key
    can be replayed: this finds the version already abandoned under ``key`` and
    pairs it with the next-numbered version that cut opened. Returns ``None`` when
    no prior cut used this key (the caller then performs a fresh cut).
    """
    prior = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "abandoned",
                    ReleaseVersion.last_qualification["idempotency_key"].astext
                    == idempotency_key,
                )
                .order_by(ReleaseVersion.version_number.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if prior is None:
        return None
    successor = (
        (
            await session.execute(
                select(ReleaseVersion).where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.version_number == prior.version_number + 1,
                )
            )
        )
        .scalars()
        .first()
    )
    return prior, successor


async def abandon_current_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    reason: str | None = None,
    operator_id: str,
    idempotency_key: str | None = None,
) -> tuple[ReleaseVersion | None, ReleaseVersion | None]:
    """Operator-initiated version cut: abandon the open version, open the next.

    The operator template is ``release_promotion._record_promotion_success`` — a
    cut is "the awaited version resolved", just by operator fiat rather than a
    passing qualification.  Returns ``(abandoned_version, next_version)`` on a
    successful cut, or ``(None, None)`` when there is no open version to cut
    (the double-cut race guard).

    Discipline mirrored from ``_record_promotion_success``:

    * **Re-lock + re-check** — the open row is re-read under ``SELECT ... FOR
      UPDATE`` and only mutated while ``status == "open"``.  A concurrent
      promotion / abandon that already closed the row makes this a NO-OP
      returning ``(None, None)`` — so two operators racing the same cut never
      open a duplicate vN+1.
    * **No schema change** — the operator REASON + IDENTITY + abandoned-at
      timestamp are stored in the existing ``last_qualification`` JSONB
      (``{status: "abandoned", reason, operator, abandoned_at}``) and the human
      reason is mirrored into ``phase_reasoning`` with the ``[operator:<id>]``
      convention.  No new columns.
    * **Open N+1 in build** — base_sha is the integration-branch HEAD resolved
      best-effort (falling back to the abandoned row's ``base_sha``); the goal
      snapshots ``ProblemModel.intent.current_focus`` exactly like the promotion
      path.

    The staged-direction activation, strategic refresh, and operator
    notification are all best-effort and isolated so a failure there can NEVER
    abort the abandon (mirroring the promotion path).

    **Idempotency** — when ``idempotency_key`` is supplied, a repeat request
    carrying the same key (a double-click or a lost-response retry that arrives
    AFTER the first cut committed) is replayed: the prior cut's
    ``(abandoned, next_version)`` pair is returned instead of cutting again, so
    the version number is not advanced a second time. The key is recorded in the
    abandoned row's ``last_qualification`` provenance — no schema change. When
    no key is supplied, behavior is byte-identical to before.

    Caller is responsible for committing the session.
    """
    # Idempotency replay: a repeat cut carrying a key already used for a prior
    # cut returns that cut's result rather than abandoning the freshly-opened
    # successor. Mirrors the pre-insert dedup in create_message.
    idem_key = (idempotency_key or "").strip() or None
    if idem_key:
        replay = await _find_cut_by_idempotency_key(session, project_id, idem_key)
        if replay is not None:
            logger.info(
                "[RELEASE_VERSION] abandon: idempotent replay for key=%s project=%s "
                "(operator=%s) — returning prior cut, no second cut",
                idem_key,
                project_id,
                operator_id,
            )
            return replay

    # Re-read + re-lock the open row.  with_for_update serializes concurrent
    # cuts; the status re-check below is the double-cut guard.
    locked = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if locked is None or locked.status != "open":
        # No open version (or a concurrent cut already closed it) — NO-OP.
        logger.info(
            "[RELEASE_VERSION] abandon: no open version to cut for project=%s "
            "(operator=%s) — no-op",
            project_id,
            operator_id,
        )
        return None, None

    from app.qualification_state_resolver import resolve_promotion_blocking_result

    _promo = await resolve_promotion_blocking_result(session, locked)
    if _promo is not None:
        raise VersionCutBlockedByPromotion(
            _promo.get("promotion_pr_number"),
            _promo.get("promotion_pr_url"),
            promotion_freshness=_promo,
        )

    project = await session.get(Project, project_id)

    now = _utcnow()
    truncated_reason = reason[:_PHASE_REASONING_MAX] if reason else None

    # Provenance into the existing last_qualification JSONB (no schema change).
    record = dict(locked.last_qualification or {})
    record["status"] = "abandoned"
    record["reason"] = truncated_reason
    record["operator"] = operator_id
    record["abandoned_at"] = now.isoformat()
    if idem_key:
        record["idempotency_key"] = idem_key
    locked.last_qualification = record
    flag_modified(locked, "last_qualification")

    # Mirror the human reason into phase_reasoning with the operator convention.
    human = (
        f"[operator:{operator_id}] abandoned: {truncated_reason}"
        if truncated_reason
        else f"[operator:{operator_id}] abandoned"
    )
    locked.phase_reasoning = human[:_PHASE_REASONING_MAX]

    locked.status = "abandoned"
    session.add(locked)

    # Open the successor in build over the integration-branch HEAD (best-effort),
    # falling back to the abandoned row's base_sha so a SHA-resolution failure
    # never aborts the cut.
    resolved_head = await _resolve_integration_head_sha(session, project)
    next_base_sha = resolved_head or locked.base_sha

    # goal opens as None — the strategic agent will author the objective in
    # its first cycle after version-open (via new_version_goal output field).
    next_version = await open_version(
        session,
        project_id,
        version_number=locked.version_number + 1,
        base_sha=next_base_sha,
        phase="build",
        goal=None,
    )
    logger.info(
        "[RELEASE_VERSION] abandon: v%d abandoned by operator=%s; opened v%d "
        "(base_sha=%s) for project=%s",
        locked.version_number,
        operator_id,
        next_version.version_number,
        (next_base_sha or "(none)")[:8] if next_base_sha else "(none)",
        project_id,
    )

    # Best-effort: activate staged directions awaiting the abandoned version and
    # arm a strategic refresh.  An abandon counts as the awaited version
    # resolving — directions staged behind it must not be orphaned.  Fully
    # isolated in its OWN session + try/except so a sweep failure can NEVER abort
    # the abandon, exactly mirroring the promotion path.
    try:
        from app.database import AsyncSessionLocal
        from app.directions_repo import activate_eligible_staged_directions
        from app.strategic_trigger import request_strategic_refresh

        _abandoned_n = locked.version_number
        async with AsyncSessionLocal() as _staged_session:
            _activated = await activate_eligible_staged_directions(
                _staged_session, project_id, _abandoned_n
            )
            await request_strategic_refresh(
                _staged_session,
                project_id,
                f"version_abandoned:v{_abandoned_n}",
            )
            await _staged_session.commit()
            if _activated:
                logger.info(
                    "[RELEASE_VERSION] abandon: v%d cut activated %d staged "
                    "direction(s) for project=%s",
                    _abandoned_n,
                    len(_activated),
                    project_id,
                )
    except Exception as exc:  # noqa: BLE001 — staged activation must not abort the cut
        logger.warning(
            "[RELEASE_VERSION] abandon: staged-direction activation / refresh "
            "failed after v%d cut (non-fatal; reaper backstop retries): %s",
            locked.version_number,
            exc,
        )

    # Best-effort operator notification — rides the caller's commit.
    try:
        from app.release_promotion import _notify_operator

        await _notify_operator(
            session,
            locked,
            "release_version_abandoned",
            (
                f"v{locked.version_number} was abandoned by operator "
                f"{operator_id}"
                + (f" ({truncated_reason})" if truncated_reason else "")
                + f". v{next_version.version_number} is now open for build."
            ),
            extra={
                "reason": truncated_reason,
                "operator": operator_id,
                "next_version_number": next_version.version_number,
            },
        )
    except Exception as exc:  # noqa: BLE001 — notification must not abort the cut
        logger.warning(
            "[RELEASE_VERSION] abandon: operator notification failed after v%d "
            "cut (non-fatal): %s",
            locked.version_number,
            exc,
        )

    # Best-effort: snapshot the lane baseline for the just-abandoned version.
    # Isolated in its OWN session so a failure here can never abort the cut —
    # exactly mirroring the staged-direction activation / notification pattern.
    try:
        from app.database import AsyncSessionLocal
        from app.lane_baseline_snapshot import persist_version_lane_baseline

        async with AsyncSessionLocal() as _snap_session:
            await persist_version_lane_baseline(
                _snap_session,
                project_id=project_id,
                version_number=locked.version_number,
                opened_at=locked.opened_at,
                closed_at=now,
                terminal_status="abandoned",
            )
            await _snap_session.commit()
    except Exception as exc:  # noqa: BLE001 — baseline snapshot must not abort the cut
        logger.warning(
            "[RELEASE_VERSION] abandon: lane baseline persistence failed after "
            "v%d cut (non-fatal): %s",
            locked.version_number,
            exc,
        )

    # Best-effort: auto-supersede still-open, non-bug-class operator messages that
    # belong to versions now at least two releases behind the freshly-opened one.
    # Runs in its OWN session and can NEVER abort the cut — mode-gated, default
    # observe. Mirrors the lane-baseline / staged-activation best-effort blocks.
    try:
        from app.message_supersession import run_supersession_on_version_close

        await run_supersession_on_version_close(
            project_id=project_id,
            closed_version_number=locked.version_number,
            closed_version_opened_at=locked.opened_at,
            terminal_status="abandoned",
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — supersession must not abort the cut
        logger.warning(
            "[RELEASE_VERSION] abandon: message supersession failed after v%d "
            "cut (non-fatal): %s",
            locked.version_number,
            exc,
        )

    return locked, next_version
