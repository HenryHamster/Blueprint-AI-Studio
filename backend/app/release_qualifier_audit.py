"""Release-qualifier audit/spawn helpers.

Extracted from ``app.release_qualifier`` to keep the main coordinator below its
size cap while preserving ``app.release_qualifier`` as the canonical import and
test-patch surface.  ``release_qualifier.py`` re-exports the moved callables so
existing callers keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    QualificationVerdict,
    ReleaseVersion,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.release_promotion import _lts_branch_names
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

# Preserve the historical logger name so existing caplog filters and log
# processing continue to see these events under release_qualifier.
logger = logging.getLogger("backend.app.release_qualifier")

# Minimum consecutive qualifier infrastructure errors (FAILED/CANCELLED
# required child tasks) before raising a dedicated infra-failure alert.
# Mirrors _MIN_CALCIFICATION_ATTEMPTS: three consecutive infra failures
# strongly suggest the qualifier pipeline itself (agent VM / clone) is broken.
_MIN_INFRA_ERROR_STREAK = 3

# Strong references to in-flight fire-and-forget infra-failure webhook tasks.
# asyncio only holds weak references to scheduled tasks, so without this a
# webhook coroutine could be garbage-collected mid-flight (RUF006).
_INFRA_WEBHOOK_TASKS: set[asyncio.Task[Any]] = set()


def _release_qualifier_module():
    """Return the canonical release_qualifier module for compat patch points."""
    from app import release_qualifier as rq

    return rq


async def _spawn_release_audit_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    base_branch: str,
    diff_range: str,
    candidate_sha: str,
) -> Task:
    """Create + queue a ``release_audit`` task for the qualifier."""
    payload: dict[str, Any] = {
        "type": "release_audit",
        "base_branch": base_branch,
        "diff_range": diff_range,
        # Audit C4: pin the audit to the exact qualified commit.  The agent
        # reads this from the payload and checks out this SHA.
        "candidate_sha": candidate_sha,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        "release_audit"
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        "release_audit"
    )
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(payload, "release_qualifier:release_audit")
    except PayloadTooLargeError:
        # release_audit payloads are tiny; a too-large error here is a real bug.
        raise

    task = Task(
        project_id=project_id,
        title=f"Release audit: {base_branch}...{candidate_sha[:8]} diff",
        description=(
            f"Differential audit of {diff_range} against base_branch "
            f"standards for the release qualifier"
        ),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
            "scope": "medium",
        },
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "RELEASE_QUALIFIER",
                "task_type": "release_audit",
                "diff_range": diff_range,
                "release_version_id": str(version_id),
            },
        )
    )
    return task


async def _spawn_release_audit_challenge_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    base_branch: str,
    diff_range: str,
    candidate_sha: str,
    primary_audit_task_id: str | None = None,
) -> Task:
    """Create + queue a challenge task for the release-audit flow.

    The spawned task remains pinned to the same candidate/diff range.  When
    ``primary_audit_task_id`` is available, it is recorded for consumers that
    still expect the older advisory audit-challenge linkage; the new binding
    challenge gate treats it as best-effort metadata.
    """
    payload: dict[str, Any] = {
        "type": "release_audit_challenge",
        "base_branch": base_branch,
        "diff_range": diff_range,
        "candidate_sha": candidate_sha,
    }
    if primary_audit_task_id is not None:
        payload["primary_audit_task_id"] = primary_audit_task_id
        payload["primary_audit_report"] = None
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        "release_audit_challenge"
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        "release_audit_challenge"
    )
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(
            payload, "release_qualifier:release_audit_challenge"
        )
    except PayloadTooLargeError:
        # Challenger payloads are tiny; a too-large error here is a real bug.
        raise

    task = Task(
        project_id=project_id,
        title=f"Release audit challenge: {base_branch}...{candidate_sha[:8]} diff",
        description=(
            f"Independent adversarial re-judgement of the release_audit findings "
            f"for {diff_range} (advisory second opinion for the release qualifier)"
        ),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
            "scope": "medium",
        },
    )
    if primary_audit_task_id is not None:
        task.context["primary_audit_task_id"] = primary_audit_task_id
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "RELEASE_QUALIFIER",
                "task_type": "release_audit_challenge",
                "diff_range": diff_range,
                "release_version_id": str(version_id),
                **(
                    {"primary_audit_task_id": primary_audit_task_id}
                    if primary_audit_task_id is not None
                    else {}
                ),
            },
        )
    )
    return task


async def _spawn_qualifier_smoketest_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    candidate_sha: str,
) -> Task:
    """Create + queue a qualify-time ``smoketest`` task on the candidate commit."""
    project = await session.get(Project, project_id)
    integration_branch, _promotion_target = _lts_branch_names(project)
    payload: dict[str, Any] = {
        "type": "smoketest",
        "base_branch": integration_branch,
        # Audit C4: pin the smoketest to the exact qualified commit.  The agent
        # clones the integration branch then checks out this SHA.
        "candidate_sha": candidate_sha,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get("smoketest")
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get("smoketest")
    if type_effort:
        payload["effort"] = type_effort
    health_check_urls = _runtime_config.get("health_check_urls") or []
    if health_check_urls:
        payload["health_check_urls"] = health_check_urls
    try:
        payload = validate_internal_payload(payload, "release_qualifier:smoketest")
    except PayloadTooLargeError:
        raise

    task = Task(
        project_id=project_id,
        title=f"Qualifier smoketest: candidate {candidate_sha[:8]}",
        description=(f"Qualify-time smoketest of release candidate {candidate_sha}"),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "qualifier_smoketest": True,
            "candidate_sha": candidate_sha,
        },
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "RELEASE_QUALIFIER",
                "task_type": "smoketest",
                "qualifier_smoketest": True,
                "release_version_id": str(version_id),
            },
        )
    )
    return task


async def _latest_artifact_logs(
    session: AsyncSession,
    task_id: uuid.UUID,
    artifact_type: ArtifactType,
) -> dict[str, Any] | None:
    """Return the parsed JSON ``logs`` of the latest artifact of a type."""
    artifact = (
        (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == artifact_type,
                )
                .order_by(Artifact.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if artifact is None or not artifact.logs:
        return None
    try:
        parsed = json.loads(artifact.logs)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "[RELEASE_QUALIFIER] artifact %s for task %s has unparseable logs",
            artifact_type,
            task_id,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


async def get_latest_release_audit_report(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest ``release_audit`` task's parsed report for a project.

    When ``candidate_sha`` is provided, the lookup is scoped to audit tasks that
    evaluated that exact candidate. If the open version has no candidate SHA or
    no matching audit exists, return ``None`` rather than a mismatched report.
    """
    if not candidate_sha:
        return None
    audit_task = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.payloads[0]["type"].as_string() == "release_audit",
                    Task.context["candidate_sha"].as_string() == candidate_sha,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if audit_task is None:
        return None
    report = await _latest_artifact_logs(
        session, audit_task.id, ArtifactType.RELEASE_AUDIT_REPORT
    )
    ctx = audit_task.context or {}
    status_val = (
        audit_task.status.value
        if hasattr(audit_task.status, "value")
        else str(audit_task.status)
    )
    return {
        "audit_task_id": str(audit_task.id),
        "task_status": status_val,
        "candidate_sha": ctx.get("candidate_sha"),
        "created_at": audit_task.created_at.isoformat()
        if audit_task.created_at
        else None,
        "report": report,
    }


async def get_latest_release_audit_challenge(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest ``release_audit_challenge`` task's parsed verdict.

    Mirrors :func:`get_latest_release_audit_report` but scoped to the advisory
    challenge child and its ``RELEASE_AUDIT_CHALLENGE`` artifact.  When
    ``candidate_sha`` is provided, the lookup is restricted to challenge tasks
    that evaluated that exact candidate so a fresh version never inherits an
    older candidate's challenge verdicts.  Returns ``None`` when no candidate is
    supplied or no matching challenge exists (the consumer treats a missing
    challenge as "no second opinion available", never as a confirmation).
    """
    if not candidate_sha:
        return None
    challenge_task = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.payloads[0]["type"].as_string()
                    == "release_audit_challenge",
                    Task.context["candidate_sha"].as_string() == candidate_sha,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if challenge_task is None:
        return None
    verdict = await _latest_artifact_logs(
        session, challenge_task.id, ArtifactType.RELEASE_AUDIT_CHALLENGE
    )
    ctx = challenge_task.context or {}
    status_val = (
        challenge_task.status.value
        if hasattr(challenge_task.status, "value")
        else str(challenge_task.status)
    )
    return {
        "challenge_task_id": str(challenge_task.id),
        "task_status": status_val,
        "candidate_sha": ctx.get("candidate_sha"),
        "created_at": challenge_task.created_at.isoformat()
        if challenge_task.created_at
        else None,
        "verdict": verdict,
    }


async def _audit_report_for_task(
    session: AsyncSession,
    task: Task,
) -> dict[str, Any]:
    """Build the operator-facing report envelope for a single ``release_audit`` task."""
    report = await _latest_artifact_logs(
        session, task.id, ArtifactType.RELEASE_AUDIT_REPORT
    )
    ctx = task.context or {}
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
    return {
        "audit_task_id": str(task.id),
        "task_status": status_val,
        "candidate_sha": ctx.get("candidate_sha"),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "report": report,
    }


def _select_lifecycle_audit(
    audit_rows: list[dict[str, Any]],
    evaluated_sha: str | None,
) -> dict[str, Any]:
    """Classify version-scoped audit rows against the evaluated candidate SHA.

    ``audit_rows`` is a newest-first list of dicts, each carrying at least a
    ``candidate_sha`` key (the candidate the audit task was pinned to).
    ``evaluated_sha`` is the open version's currently-evaluated candidate (from
    ``last_qualification['candidate_sha']``).

    Returns a dict with:

    * ``state`` — ``current`` when the newest matching-candidate audit exists,
      ``stale`` when audits exist but none match the evaluated candidate (the
      newest is historical), or ``none`` when there are no audits at all.
    * ``current`` — the matching row when ``state == "current"`` else ``None``.
    * ``stale`` — the newest (historical) row when ``state == "stale"`` else
      ``None``.

    Pure and synchronous so the selection policy is unit-testable without a DB.
    """
    if not audit_rows:
        return {"state": "none", "current": None, "stale": None}
    if evaluated_sha:
        for row in audit_rows:
            if row.get("candidate_sha") == evaluated_sha:
                return {"state": "current", "current": row, "stale": None}
    # Audits exist but none was run against the evaluated candidate — the newest
    # is historical evidence; surface it as stale provenance, never as current.
    return {"state": "stale", "current": None, "stale": audit_rows[0]}


async def resolve_lifecycle_audit_evidence(
    session: AsyncSession,
    version: ReleaseVersion | None,
) -> dict[str, Any]:
    """Resolve audit evidence scoped to an open version's evaluated candidate.

    Unlike :func:`get_latest_release_audit_report` (which returns the newest
    ``release_audit`` anywhere in the project), this scopes evidence to the
    given version via ``Task.context['release_version_id']`` and classifies it
    against the version's evaluated candidate SHA so a fresh or newly-cut
    version never inherits another version's findings.

    Returns a dict:

    * ``state`` — ``current`` | ``stale`` | ``none``.
    * ``latest_audit`` — the matching audit report envelope when
      ``state == "current"``, else ``None`` (so stale findings are never
      presented as the current audit).
    * ``stale_audit`` — the historical audit report envelope (provenance the UI
      can use to explain the findings are stale) when ``state == "stale"``, else
      ``None``.
    """
    if version is None:
        return {"state": "none", "latest_audit": None, "stale_audit": None}

    audit_tasks = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == version.project_id,
                    Task.payloads[0]["type"].as_string() == "release_audit",
                    Task.context["release_version_id"].as_string() == str(version.id),
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    rows = [
        {"candidate_sha": (t.context or {}).get("candidate_sha"), "task": t}
        for t in audit_tasks
    ]
    evaluated_sha = (version.last_qualification or {}).get("candidate_sha")
    selection = _select_lifecycle_audit(rows, evaluated_sha)

    if selection["state"] == "current":
        report = await _audit_report_for_task(session, selection["current"]["task"])
        return {"state": "current", "latest_audit": report, "stale_audit": None}
    if selection["state"] == "stale":
        report = await _audit_report_for_task(session, selection["stale"]["task"])
        return {"state": "stale", "latest_audit": None, "stale_audit": report}
    return {"state": "none", "latest_audit": None, "stale_audit": None}


async def _handle_infra_failure(
    session: AsyncSession,
    version: ReleaseVersion,
    qual: dict[str, Any],
    infra_error: str,
) -> dict[str, Any]:
    """Finalize a qualifier run that ended due to an infrastructure failure."""
    finalized_at = _utcnow().isoformat()
    prior_streak = int(qual.get("prior_infra_streak") or 0)
    current_streak = prior_streak + 1

    record: dict[str, Any] = {
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", True),
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "audit_challenge_task_id": qual.get("audit_challenge_task_id"),
        "audit_challenge_spawn_error": qual.get("audit_challenge_spawn_error"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: infrastructure failure "
            f"({infra_error}) — not a quality finding, returning to stabilize"
        ),
    )
    session.add(version)

    if (
        current_streak >= _MIN_INFRA_ERROR_STREAK
        and current_streak % _MIN_INFRA_ERROR_STREAK == 0
    ):
        session.add(
            Alert(
                project_id=version.project_id,
                severity=AlertSeverity.WARNING,
                title=(
                    f"Release qualifier v{version.version_number} infrastructure "
                    f"failure: {current_streak} consecutive errors"
                ),
                message=(
                    f"The release qualifier for v{version.version_number} has "
                    f"failed {current_streak} consecutive time(s) due to "
                    f"infrastructure errors ({infra_error!r}) — the qualifier "
                    f"agent VM or clone pipeline is broken. This is NOT a "
                    f"quality finding about the release candidate. Inspect the "
                    f"release_audit / qualifier-smoketest agent infrastructure "
                    f"and re-queue to retry once the issue is resolved."
                ),
            )
        )
        logger.warning(
            "[RELEASE_QUALIFIER] infra-failure backstop: v%d has %d consecutive "
            "infrastructure errors (error=%s) — alert raised",
            version.version_number,
            current_streak,
            infra_error,
        )

    rq = _release_qualifier_module()
    await rq._notify_operator(
        session,
        version,
        "release_infra_failure",
        (
            f"v{version.version_number} qualifier infrastructure failure — "
            f"{infra_error.replace('_', ' ')} (agent VM / clone failure, "
            f"not a quality finding). Returned to stabilize for "
            f"re-qualification. Consecutive infra errors: {current_streak}."
        ),
        extra={
            "error": infra_error,
            "consecutive_infra_errors": current_streak,
        },
    )

    _webhook_task = asyncio.ensure_future(
        rq.dispatch_external_notification(
            version.project_id,
            "RELEASE_INFRA_FAILURE",
            {
                "version_number": version.version_number,
                "version_id": str(version.id),
                "infra_error": infra_error,
                "consecutive_infra_errors": current_streak,
            },
        )
    )
    _INFRA_WEBHOOK_TASKS.add(_webhook_task)
    _webhook_task.add_done_callback(_INFRA_WEBHOOK_TASKS.discard)

    logger.warning(
        "[RELEASE_QUALIFIER] v%d infra failure: %s (streak=%d) — "
        "no verdict recorded, version returned to stabilize",
        version.version_number,
        infra_error,
        current_streak,
    )
    return {
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
    }


async def get_infra_failure_stats(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window: int = 50,
) -> dict[str, Any]:
    """Return infra-failure summary for a project over the most-recent versions."""
    stmt = (
        select(ReleaseVersion.last_qualification)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.last_qualification.is_not(None),
        )
        .order_by(ReleaseVersion.updated_at.desc())
        .limit(window)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    infra_failure_count = sum(
        1 for q in rows if isinstance(q, dict) and q.get("status") == "infra_failure"
    )
    consecutive_infra_errors = 0
    for q in rows:
        if not isinstance(q, dict):
            continue
        if q.get("status") == "pending":
            consecutive_infra_errors = int(q.get("prior_infra_streak") or 0)
            break
        if q.get("status") == "infra_failure":
            consecutive_infra_errors = int(q.get("consecutive_infra_errors") or 1)
            break

    return {
        "infra_failure_count": infra_failure_count,
        "consecutive_infra_errors": consecutive_infra_errors,
    }


def _enrich_infra_error(base_label: str, task: Task | None) -> str:
    """Append agent_failure_category_raw to base_label for FAILED tasks."""
    if task is None or task.status != TaskStatus.FAILED:
        return base_label
    raw = (task.context or {}).get("agent_failure_category_raw", "")
    if not raw:
        return base_label
    sanitized = "".join(
        c if c.isalnum() or c == "_" else "_" for c in str(raw).strip().lower()
    )
    if not sanitized:
        return base_label
    return f"{base_label}_{sanitized}"


def get_latest_qualification_audit_findings(
    version: ReleaseVersion | None,
    verdict: QualificationVerdict | None = None,
) -> list[dict[str, Any]]:
    """Return the stored audit findings for a version's latest qualification.

    Prefers the durable per-attempt evidence on an immutable
    ``QualificationVerdict`` row when one is supplied (its ``audit_findings``
    column survives re-qualification overwrites), falling back to the version's
    mutable ``last_qualification.audit_findings`` blob for legacy rows that
    predate the evidence column.  Sync, returns ``list[dict]``.
    """
    if verdict is not None and isinstance(verdict.audit_findings, list):
        return [f for f in verdict.audit_findings if isinstance(f, dict)]
    qual = (version.last_qualification or {}) if version is not None else {}
    findings = qual.get("audit_findings")
    if not isinstance(findings, list):
        return []
    return [finding for finding in findings if isinstance(finding, dict)]
