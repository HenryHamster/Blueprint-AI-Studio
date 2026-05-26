"""Qualifier I/O surface — child task spawners + artifact readers.

Extracted from ``app.release_qualifier`` so that changes to the qualifier's
child-task payload contracts and artifact-reading idiom don't require editing
the qualifier coordinator itself.

This module owns:

  * ``_spawn_release_audit_task`` — creates the ``release_audit`` child task
    (audit C4 ``candidate_sha`` pinning).
  * ``_spawn_qualifier_smoketest_task`` — creates the qualify-time
    ``smoketest`` child task (``qualifier_smoketest=True`` context flag so its
    failure does NOT spawn a fix task, per design doc §6 gotcha 5).
  * ``_latest_artifact_logs`` — reads the parsed-JSON ``logs`` field of the
    latest artifact of a given type for a task; the idiom ``_finalize_*``
    depends on.
  * ``get_latest_release_audit_report`` — observability helper used by
    ``admin_digest`` to surface the latest release_audit task's report.

All four are re-exported by ``release_qualifier`` so that callers and tests
that reach them via ``rq.<name>`` (including monkeypatch targets like
``rq._spawn_release_audit_task``) keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.release_promotion import _lts_branch_names
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


async def _spawn_release_audit_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    base_branch: str,
    diff_range: str,
    candidate_sha: str,
) -> Task:
    """Create + queue a ``release_audit`` task for the qualifier.

    The audit clones ``base_branch`` (``main``) and reviews the ``diff_range``
    (``main...<candidate_sha>`` — pinned to the exact snapshot commit, audit
    C4) against main's standards — the differential audit at the heart of the
    qualifier (design doc §6 gotcha 2: it MUST run on main).  Mirrors the
    ``_spawn_self_audit_task`` spawn pattern.

    The payload carries ``candidate_sha`` (the full 40-char SHA) so the agent's
    ``handleReleaseAudit`` checks out exactly that commit rather than whatever
    ``automain`` has drifted to by the task's runtime (audit C4 contract — the
    payload field name is exactly ``candidate_sha``).
    """
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
        title=f"Release audit: main...{candidate_sha[:8]} diff",
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


async def _spawn_qualifier_smoketest_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    candidate_sha: str,
) -> Task:
    """Create + queue a qualify-time ``smoketest`` task on the candidate commit.

    The context flag ``qualifier_smoketest=True`` marks this as a
    pre-promotion qualifier input — distinct from the post-promotion
    production smoketest — so its failure does NOT spawn a fix task
    (design doc §6 gotcha 5).

    The payload carries ``candidate_sha`` (the full 40-char SHA, audit C4) so
    the agent's qualifier-smoketest handler checks out exactly that commit
    after cloning ``automain`` — the candidate commit is reachable from the
    ``automain`` branch at spawn time, and pinning to the SHA means the
    smoketest scores the same commit the audit does even though ``automain``
    keeps advancing while the task is queued (audit C4 contract — the payload
    field name is exactly ``candidate_sha``).
    """
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


# ---------------------------------------------------------------------------
# Artifact parsing — read the children's reports.
# ---------------------------------------------------------------------------


async def _latest_artifact_logs(
    session: AsyncSession,
    task_id: uuid.UUID,
    artifact_type: ArtifactType,
) -> dict[str, Any] | None:
    """Return the parsed JSON ``logs`` of the latest artifact of a type.

    Both RELEASE_AUDIT_REPORT and SMOKETEST_RESULT store a JSON document in
    the ``Artifact.logs`` column.  Returns None when no artifact exists or the
    logs are not parseable JSON.
    """
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
) -> dict[str, Any] | None:
    """Return the latest ``release_audit`` task's parsed report for a project.

    Observability: a qualification verdict only persists the composite +
    sub-scores, and the task API strips artifact ``logs`` — so operators are
    blind to WHY a candidate scored what it did.  This surfaces the audit
    agent's actual reasoning (``audit_score``, ``findings``, ``test_erosion``)
    from the most recent ``release_audit`` task's RELEASE_AUDIT_REPORT artifact.

    Returns ``None`` when no ``release_audit`` task exists yet.  ``report`` is
    ``None`` when the task ran but posted no parseable report (crashed / killed).
    """
    audit_task = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.payloads[0]["type"].as_string() == "release_audit",
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
