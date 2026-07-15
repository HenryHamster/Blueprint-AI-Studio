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
from app.release_promotion import _lts_branch_names, _record_qualification_verdict
from app.runtime_config_state import resolve_project_promotion_enabled
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow
from app.json_extract import async_json_loads

# Preserve the historical logger name so existing caplog filters and log
# processing continue to see these events under release_qualifier.
logger = logging.getLogger("backend.app.release_qualifier")

# Minimum consecutive qualifier infrastructure errors (FAILED/CANCELLED
# required child tasks) before raising a dedicated infra-failure alert.
# Mirrors _MIN_CALCIFICATION_ATTEMPTS: three consecutive infra failures
# strongly suggest the qualifier pipeline itself (agent VM / clone) is broken.
_MIN_INFRA_ERROR_STREAK = 3
_INFRA_ATTEMPT_HISTORY_LIMIT = 5

# Strong references to in-flight fire-and-forget infra-failure webhook tasks.
# asyncio only holds weak references to scheduled tasks, so without this a
# webhook coroutine could be garbage-collected mid-flight (RUF006).
_INFRA_WEBHOOK_TASKS: set[asyncio.Task[Any]] = set()


def _bounded_infra_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = [row for row in value if isinstance(row, dict)]
    return rows[-_INFRA_ATTEMPT_HISTORY_LIMIT:]


def _task_failure_category(task: Task | None) -> str | None:
    if task is None:
        return None
    ctx = task.context or {}
    category = ctx.get("failure_category") or ctx.get("failure_class")
    return str(category) if category else None


def _compact_infra_child_event(
    *,
    leg: str,
    failed_task: Task | None,
    replacement_task_id: str | None = None,
    label: str | None = None,
    respawn_count: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"leg": leg}
    if failed_task is not None:
        event["failed_task_id"] = str(failed_task.id)
        event["status"] = failed_task.status
        category = _task_failure_category(failed_task)
        if category:
            event["failure_category"] = category
    if replacement_task_id:
        event["replacement_task_id"] = replacement_task_id
    if label:
        event["label"] = label
    if respawn_count is not None:
        event["respawn_count"] = respawn_count
    return event


def append_infra_failure_child_event(
    qual: dict[str, Any],
    *,
    leg: str,
    failed_task: Task | None,
    replacement_task_id: str | None = None,
    label: str | None = None,
    respawn_count: int | None = None,
) -> dict[str, Any]:
    """Return ``qual`` with a compact child-failure event appended."""
    events = qual.get("infra_failure_child_events")
    if not isinstance(events, list):
        events = []
    event = _compact_infra_child_event(
        leg=leg,
        failed_task=failed_task,
        replacement_task_id=replacement_task_id,
        label=label,
        respawn_count=respawn_count,
    )
    return {**qual, "infra_failure_child_events": [*events, event][-20:]}


def _infer_infra_leg(infra_error: str) -> str:
    """Map an infra_error code to its logical qualifier leg.

    The leg forms the first component of the infra-failure signature
    (``{leg}:{category}:{code}``).  Ordered by specificity so that a sub-leg
    (``audit_challenge``) is matched before its parent (``audit``), and distinct
    migration gates (identity vs apply) before a bare ``migration`` fallback.
    Unrecognized codes default to ``"audit"`` — the historical, dominant leg.
    """
    if infra_error.startswith("audit_challenge") or infra_error.startswith(
        "challenge_"
    ):
        return "challenge"
    if infra_error.startswith("migration_identity"):
        return "migration_identity"
    if infra_error.startswith("migration_apply"):
        return "migration_apply"
    if infra_error.startswith("migration"):
        return "migration_identity"
    if infra_error.startswith("smoketest"):
        return "smoketest"
    return "audit"


def _infer_dimension_from_infra_error(infra_error: str) -> str:
    """Map an infra_error code to the qualification ledger dimension it belongs to.

    Per-dimension failure isolation (Phase 1, design doc "From Batch Attempts to a
    Per-Dimension Fact Ledger"): when a qualifier run holds on an infrastructure
    failure, ONLY the failing dimension's ``(candidate_sha, dimension)`` fact is
    marked unavailable — the dimensions that already resolved for the same
    candidate tree are preserved.  Codes are matched by prefix because
    ``_enrich_infra_error`` may append a sanitized agent-failure-category suffix
    (e.g. ``"audit_task_failed_provision_failure"``).  Ordering is deliberate: the
    ``audit_challenge*`` codes are a sub-leg of the audit but own a distinct
    ``challenge`` dimension, so they are matched before the bare ``audit`` prefix,
    and ``migration_identity`` / ``migration_apply`` before the bare ``migration``
    fallback.  Unrecognized codes default to ``audit`` — the dominant,
    always-present dimension — matching ``_infer_infra_leg``'s fallback.
    """
    code = (infra_error or "").lower()
    if code.startswith("audit_challenge") or code.startswith("challenge"):
        return "challenge"
    if code.startswith("migration_identity"):
        return "migration_identity"
    if code.startswith("migration_apply"):
        return "migration_apply"
    if code.startswith("migration"):
        return "migration_identity"
    if code.startswith("smoketest"):
        return "smoketest"
    if code.startswith("merge_grade"):
        return "merge_grade"
    if code.startswith("ci"):
        return "ci"
    if code.startswith("test_erosion"):
        return "test_erosion"
    if code.startswith("audit"):
        return "audit"
    # Ledger-integrity codes (qualification_dimension_ledger_incomplete /
    # qualification_dimension_ledger_empty) are not a single-dimension failure —
    # recording them as 'audit' would overwrite a legitimately-passed audit fact.
    # Return None so the caller skips the per-dimension upsert entirely.
    if code.startswith("qualification_dimension_ledger"):
        return None
    return "audit"


def _infra_failure_signature(
    infra_error: str, child_failures: list[dict[str, Any]]
) -> str:
    """Build a repetition key from the failing gate, not its diagnostics.

    Child task failures are useful evidence, but are unrelated to a deterministic
    gate error (and can vary between attempts).  Including them made a stable
    migration gate look transient and prevented incapacity escalation.
    """
    del child_failures
    return f"{_infer_infra_leg(infra_error)}:unknown:{infra_error}"


async def _collect_terminal_infra_child_events(
    session: AsyncSession,
    qual: dict[str, Any],
    infra_error: str,
) -> list[dict[str, Any]]:
    existing = [
        row
        for row in qual.get("infra_failure_child_events", [])
        if isinstance(row, dict)
    ]
    terminal_ids: list[tuple[str, str]] = []
    if qual.get("audit_task_id"):
        terminal_ids.append(("audit", str(qual.get("audit_task_id"))))
    challenge_id = qual.get("challenge_task_id") or qual.get("audit_challenge_task_id")
    if challenge_id:
        terminal_ids.append(("challenge", str(challenge_id)))
    if qual.get("smoketest_task_id"):
        terminal_ids.append(("smoketest", str(qual.get("smoketest_task_id"))))

    seen_failed_ids = {
        str(row.get("failed_task_id"))
        for row in existing
        if row.get("failed_task_id") is not None
    }

    # Collect valid UUIDs not yet in seen_failed_ids, then batch-fetch in one
    # query instead of one session.get per leg.
    valid_uuids: list[uuid.UUID] = []
    for leg, task_id in terminal_ids:
        if task_id in seen_failed_ids:
            continue
        try:
            valid_uuids.append(uuid.UUID(task_id))
        except (TypeError, ValueError):
            continue

    tasks_by_id: dict[uuid.UUID, Task] = {}
    if valid_uuids:
        rows = (
            await session.execute(select(Task).where(Task.id.in_(valid_uuids)))
        ).scalars().all()
        tasks_by_id = {t.id: t for t in rows}

    terminal_events: list[dict[str, Any]] = []
    for leg, task_id in terminal_ids:
        if task_id in seen_failed_ids:
            continue
        try:
            task_uuid = uuid.UUID(task_id)
        except (TypeError, ValueError):
            continue
        task = tasks_by_id.get(task_uuid)
        if task is None or task.status not in (
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        ):
            continue
        terminal_events.append(
            _compact_infra_child_event(
                leg=leg,
                failed_task=task,
                label=infra_error if leg == _infer_infra_leg(infra_error) else None,
            )
        )
    return [*existing, *terminal_events][-20:]


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
    audit_base_sha: str | None = None,
    version_goal: str | None = None,
) -> Task:
    """Create + queue a ``release_audit`` task for the qualifier."""
    payload: dict[str, Any] = {
        "type": "release_audit",
        "base_branch": base_branch,
        "diff_range": diff_range,
        # Audit C4: pin the audit to the exact qualified commit.  The agent
        # reads this from the payload and checks out this SHA.
        "candidate_sha": candidate_sha,
        # Explicitly pin to Opus — regression guard against silent downgrade.
        "quality_tier": "opus",
    }
    if version_goal:
        payload["version_goal"] = version_goal
    if audit_base_sha:
        # A hint, not an assertion: the agent verifies ancestry/range
        # resolvability and falls back to base_branch...candidate_sha if stale.
        payload["audit_base_sha"] = audit_base_sha
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
                "audit_base_sha": audit_base_sha,
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
    primary_findings: list[dict[str, str]] | None = None,
    version_goal: str | None = None,
) -> Task:
    """Create + queue a challenge task for the release-audit flow.

    The spawned task remains pinned to the same candidate/diff range.  When
    ``primary_audit_task_id`` is available, it is recorded for consumers that
    still expect audit/challenge task linkage; the binding challenge gate treats
    it as best-effort metadata.
    """
    payload: dict[str, Any] = {
        "type": "release_audit_challenge",
        "base_branch": base_branch,
        "diff_range": diff_range,
        "candidate_sha": candidate_sha,
        # Explicitly pin to Opus — regression guard against silent downgrade.
        "quality_tier": "opus",
    }
    if version_goal:
        payload["version_goal"] = version_goal
    if primary_audit_task_id is not None:
        payload["primary_audit_task_id"] = primary_audit_task_id
        payload["primary_audit_report"] = None
    if primary_findings is not None:
        payload["primary_findings"] = primary_findings
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
            f"for {diff_range} (binding second opinion for the release qualifier)"
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


async def _spawn_release_merge_grade_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    base_branch: str,
    diff_range: str,
    candidate_sha: str,
    version_goal: str,
    merged_pr_numbers: list[int],
    categories: list[dict[str, Any]] | None = None,
) -> Task:
    """Create + queue a blind ``release_merge_grade`` task for the qualifier.

    The merge-grade agent grades the MERGED DIFFS of this version (the
    ``base_branch...candidate_sha`` range plus the merged-PR window) against the
    frozen ``version_goal`` and returns the holistic ``{covers_goal,
    coverage_score, biggest_gap}`` plus — per the goal-verification redesign — a
    per-category verdict for each active acceptance-criterion CATEGORY in
    ``categories`` (``{category_key, text}``) and a residual "gap no category
    captures" answer.  The categories are the ACTIVE ``VersionAcceptanceCriterion``
    rows; the agent keys its verdicts on the supplied ``category_key`` values.

    BLINDNESS CONTRACT (dir d-c0fe439f): this grade must be UNINVESTED — the
    agent sees the version goal and the merged diffs ONLY.  The strategist's
    ``Project.running_update`` narrative (or any running-update text) MUST NOT
    be threaded into this payload; doing so would let the optimistic strategist
    narrative bias a grade that is meant to independently check whether the
    merges actually advanced the goal.  The ``assert`` below is a tripwire that
    fails loudly if a future edit ever leaks a running-update key in.
    """
    payload: dict[str, Any] = {
        "type": "release_merge_grade",
        "base_branch": base_branch,
        "diff_range": diff_range,
        # Pin the grade to the exact qualified commit (mirrors release_audit).
        "candidate_sha": candidate_sha,
        # The ONLY narrative the grader is allowed to see: the frozen goal.
        "version_goal": version_goal,
        # The merged-PR window for this version (same acceptance-window /
        # merged-PR discovery the blind merge-grade uses); the agent fetches each
        # PR's diff to grade goal coverage.
        "merged_prs": list(merged_pr_numbers),
        # Goal-verification redesign: the ACTIVE acceptance-criterion CATEGORIES
        # the agent grades the merged diff against (per-category met/not_met/
        # partial + cited diff evidence + gap).  Empty list ⇒ no rubric; the agent
        # then grades only the holistic goal coverage.
        "categories": list(categories or []),
        # Explicitly pin to Opus — regression guard against silent downgrade
        # (mirrors release_audit / release_audit_challenge).
        "quality_tier": "opus",
    }
    # Blindness guard — see the contract note above. No payload key may carry
    # the strategist running-update narrative into a grade that must be blind to
    # it. This is the explicit guard the gate's design requires.
    assert not any("running_update" in str(key).lower() for key in payload), (
        "release_merge_grade payload must stay BLIND to the strategist "
        "running_update — no running-update text may be threaded in"
    )
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        "release_merge_grade"
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        "release_merge_grade"
    )
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(
            payload, "release_qualifier:release_merge_grade"
        )
    except PayloadTooLargeError:
        # A too-large merge-grade payload (an enormous goal or PR list) is a real
        # bug worth surfacing — let it raise to the spawn caller's handler.
        raise

    task = Task(
        project_id=project_id,
        title=f"Release merge grade: {base_branch}...{candidate_sha[:8]} vs goal",
        description=(
            f"Blind goal-coverage grade of the merged diffs ({diff_range}) "
            f"against the version goal for the release qualifier"
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
                "task_type": "release_merge_grade",
                "diff_range": diff_range,
                "release_version_id": str(version_id),
                "merged_pr_count": len(merged_pr_numbers),
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
        parsed = await async_json_loads(artifact.logs)
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
                    Task.payloads[0]["type"].as_string() == "release_audit_challenge",
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
    status_val = (
        task.status.value if hasattr(task.status, "value") else str(task.status)
    )
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
    child_failures = await _collect_terminal_infra_child_events(
        session, qual, infra_error
    )
    signature = _infra_failure_signature(infra_error, child_failures)
    previous_signature = qual.get("previous_infra_failure_signature") or qual.get(
        "prior_infra_failure_signature"
    )
    previous_candidate_sha = qual.get("previous_infra_failure_candidate_sha")
    same_error_as_prior = bool(previous_signature and signature == previous_signature)
    same_error_different_candidate = bool(
        same_error_as_prior
        and previous_candidate_sha
        and qual.get("candidate_sha")
        and previous_candidate_sha != qual.get("candidate_sha")
    )
    prior_history = _bounded_infra_history(qual.get("infra_attempt_history"))
    attempt_entry = {
        "finalized_at": finalized_at,
        "error": infra_error,
        "signature": signature,
        "candidate_sha": qual.get("candidate_sha"),
        "failing_leg": _infer_infra_leg(infra_error),
        "child_failures": child_failures,
    }
    attempt_history = [*prior_history, attempt_entry][-_INFRA_ATTEMPT_HISTORY_LIMIT:]

    # Permanent gate-incapacity detection: count trailing same-signature entries
    # in attempt_history.  When a migration_apply_check_unavailable error repeats
    # with the exact same signature across _MIN_INFRA_ERROR_STREAK or more
    # consecutive attempts, the condition is structural and will not self-resolve
    # by retry — escalate with a different operator message.
    is_permanent_gate_incapacity = False
    same_signature_streak = 0
    if infra_error.startswith("migration_apply"):
        cur_sig = signature
        for entry in reversed(attempt_history):
            if entry.get("signature") == cur_sig:
                same_signature_streak += 1
            else:
                break
        is_permanent_gate_incapacity = (
            same_signature_streak >= _MIN_INFRA_ERROR_STREAK
        )

    infra_evidence = {
        "error": infra_error,
        "signature": signature,
        "previous_signature": previous_signature,
        "same_error_as_prior": same_error_as_prior,
        "same_error_different_candidate": same_error_different_candidate,
        "consecutive_infra_errors": current_streak,
        "is_permanent_gate_incapacity": is_permanent_gate_incapacity,
        "same_signature_streak": same_signature_streak,
        "attempt": attempt_entry,
        "attempt_history": attempt_history,
    }

    # Per-dimension failure isolation (Phase 1): infer WHICH dimension the infra
    # failure belongs to so only that (candidate_sha, dimension) ledger fact is
    # marked unavailable below — the other dimensions that already resolved for
    # this candidate tree keep their rows.
    failing_dimension = _infer_dimension_from_infra_error(infra_error)

    record: dict[str, Any] = {
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", True),
        "status": "infra_failure",
        "error": infra_error,
        # Which single dimension this infra failure isolated to (the other
        # dimensions' ledger facts survive) — surfaced for operator diagnosis.
        "failing_dimension": failing_dimension,
        "infra_failure_signature": signature,
        "previous_infra_failure_signature": previous_signature,
        "same_error_as_prior": same_error_as_prior,
        "same_infra_failure_as_previous": same_error_as_prior,
        "same_error_different_candidate": same_error_different_candidate,
        "same_infra_failure_different_candidate": same_error_different_candidate,
        "infra_failure_child_events": child_failures,
        "infra_attempt_history": attempt_history,
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

    # Per-dimension failure isolation (Phase 1): record ONLY the failing
    # dimension as 'unavailable' in the per-(candidate_sha, dimension) ledger,
    # tracking a PER-DIMENSION infra-error streak in the row's detail — so this
    # gate's flakiness cannot burn another gate's retry budget and the other
    # dimensions' already-resolved facts for this candidate tree are untouched.
    # Best-effort: a ledger write failure must never break the infra-failure hold.
    # Skip when failing_dimension is None (e.g. ledger-integrity codes) — there
    # is no single dimension to blame and writing 'audit' would overwrite a
    # legitimately-resolved audit fact.
    _rq = _release_qualifier_module()
    dimension_infra_streak = (
        await _rq._record_infra_dimension_unavailable(
            session,
            version,
            qual.get("candidate_sha"),
            failing_dimension,
            infra_error=infra_error,
            signature=signature,
        )
        if failing_dimension is not None
        else None
    )

    hotfix_preview: dict[str, Any] | None = None
    if is_permanent_gate_incapacity:
        # Make the sanctioned remediation concrete at the point incapacity is
        # declared.  This is read-only and deliberately best-effort: inability
        # to preview must not turn a held release into a promotion.
        try:
            hotfix_preview = await _release_qualifier_module().check_hotfix_scope_preview(
                session, version.project_id, candidate_sha=qual.get("candidate_sha")
            )
        except Exception:  # noqa: BLE001 — remediation guidance is best-effort
            logger.warning(
                "[RELEASE_QUALIFIER] hotfix scope preview failed for v%d",
                version.version_number,
                exc_info=True,
            )

    # Phase 1b read: compose already-resolved dimension facts from the per-dimension
    # ledger into the record and verdict sub_scores, so a passed audit/CI/etc. is
    # visible in the verdict instead of being discarded by a transient infra defer
    # on a DIFFERENT gate.  Best-effort — any read failure leaves resolved_dimensions
    # empty and the infra-failure hold proceeds exactly as before.
    resolved_dimensions: dict[str, Any] = {}
    _ledger_candidate_sha = qual.get("candidate_sha")
    if _ledger_candidate_sha:
        try:
            _prior = await _rq._load_prior_dimension_results(
                session, version, _ledger_candidate_sha
            )
            _resolved_statuses = _rq._LEDGER_RESOLVED_STATUSES
            for _dim, _row in _prior.items():
                if _row.status in _resolved_statuses:
                    resolved_dimensions[_dim] = {
                        "status": _row.status,
                        "sub_score": _row.sub_score,
                        "blocker": _row.blocker,
                    }
        except Exception:  # noqa: BLE001 — read is best-effort, never fatal
            resolved_dimensions = {}

    if resolved_dimensions:
        record["resolved_dimensions"] = resolved_dimensions
        flag_modified(version, "last_qualification")

    if (
        current_streak >= _MIN_INFRA_ERROR_STREAK
        and current_streak % _MIN_INFRA_ERROR_STREAK == 0
    ):
        if is_permanent_gate_incapacity:
            preview_text = (
                f"Eligible narrow diff: {hotfix_preview.get('changed_files', [])}. "
                if hotfix_preview and hotfix_preview.get("eligible")
                else "Hotfix scope preview unavailable or ineligible; inspect it before acting. "
            )
            alert_message = (
                f"The migration-apply gate for v{version.version_number} is "
                f"persistently incapable across {same_signature_streak} "
                f"same-signature attempts ({infra_error!r}) — the underlying "
                f"condition will not self-resolve by retry. Sanctioned "
                f"{preview_text}Sanctioned remediation: use the hotfix scope "
                f"preview and approved hotfix endpoint (POST /projects/{{id}}/promote/hotfix), "
                f"not whole-tree force-promotion. "
                f"Signature: {signature}."
            )
        else:
            alert_message = (
                f"The release qualifier for v{version.version_number} has "
                f"failed {current_streak} consecutive time(s) due to "
                f"infrastructure errors ({infra_error!r}) — the qualifier "
                f"agent VM or clone pipeline is broken. This is NOT a "
                f"quality finding about the release candidate. Inspect the "
                f"release_audit / qualifier-smoketest agent infrastructure "
                f"and re-queue to retry once the issue is resolved. "
                f"Signature: {signature}; same as previous: "
                f"{same_error_as_prior}."
            )
        session.add(
            Alert(
                project_id=version.project_id,
                severity=AlertSeverity.WARNING,
                title=(
                    f"Release qualifier v{version.version_number} infrastructure "
                    f"failure: {current_streak} consecutive errors"
                ),
                message=alert_message,
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
    if is_permanent_gate_incapacity:
        preview_text = (
            f" Eligible narrow diff: {hotfix_preview.get('changed_files', [])}."
            if hotfix_preview and hotfix_preview.get("eligible")
            else " Hotfix scope preview is unavailable or ineligible; do not force-promote."
        )
        notify_message = (
            f"v{version.version_number} migration-apply gate is persistently "
            f"incapable across {same_signature_streak} same-signature attempts "
            f"({infra_error.replace('_', ' ')}) — the underlying condition "
            f"will not self-resolve by retry. "
            f"Use the sanctioned hotfix scope preview and endpoint "
            f"(POST /projects/{{id}}/promote/hotfix), not whole-tree force-promotion."
            f"{preview_text}"
        )
    else:
        notify_message = (
            f"v{version.version_number} qualifier infrastructure failure — "
            f"{infra_error.replace('_', ' ')} (agent VM / clone failure, "
            f"not a quality finding). Returned to stabilize for "
            f"re-qualification. Consecutive infra errors: {current_streak}. "
            f"Signature: {signature}; same as previous: {same_error_as_prior}."
        )
    await rq._notify_operator(
        session,
        version,
        "release_infra_failure",
        notify_message,
        extra={
            "error": infra_error,
            "infra_failure_signature": signature,
            "previous_infra_failure_signature": previous_signature,
            "same_error_as_prior": same_error_as_prior,
            "same_error_different_candidate": same_error_different_candidate,
            "consecutive_infra_errors": current_streak,
            "is_permanent_gate_incapacity": is_permanent_gate_incapacity,
            "same_signature_streak": same_signature_streak,
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
                "infra_failure_signature": signature,
                "previous_infra_failure_signature": previous_signature,
                "same_error_as_prior": same_error_as_prior,
                "same_error_different_candidate": same_error_different_candidate,
                "consecutive_infra_errors": current_streak,
                "is_permanent_gate_incapacity": is_permanent_gate_incapacity,
                "same_signature_streak": same_signature_streak,
            },
        )
    )
    _INFRA_WEBHOOK_TASKS.add(_webhook_task)
    _webhook_task.add_done_callback(_INFRA_WEBHOOK_TASKS.discard)

    logger.warning(
        "[RELEASE_QUALIFIER] v%d infra failure: %s (streak=%d) — "
        "infra verdict recorded best-effort, version returned to stabilize",
        version.version_number,
        infra_error,
        current_streak,
    )
    promotion_enabled = resolve_project_promotion_enabled(version.project_id)
    _verdict_sub_scores: dict[str, Any] = {"infra_failure": infra_evidence}
    _verdict_sub_scores.update(resolved_dimensions)
    await _record_qualification_verdict(
        session,
        version,
        {**record, "sub_scores": _verdict_sub_scores},
        terminal_status="infra_failure",
        would_promote=False,
        promotion_enabled=promotion_enabled,
    )
    return {
        "status": "infra_failure",
        "error": infra_error,
        "infra_failure_signature": signature,
        "same_error_as_prior": same_error_as_prior,
        "same_error_different_candidate": same_error_different_candidate,
        "consecutive_infra_errors": current_streak,
        "is_permanent_gate_incapacity": is_permanent_gate_incapacity,
        "same_signature_streak": same_signature_streak,
        # Per-dimension isolation (Phase 1): which single dimension was marked
        # unavailable, and that dimension's own infra-error streak (distinct from
        # the attempt-level ``consecutive_infra_errors`` above).
        "failing_dimension": failing_dimension,
        "dimension_infra_streak": dimension_infra_streak,
    }


async def _handle_independent_blocker(
    session: AsyncSession,
    version: ReleaseVersion,
    qual: dict[str, Any],
    independent_blocker_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Finalize a qualifier run blocked by a challenger-confirmed independent finding.

    Unlike _handle_infra_failure, this IS a quality finding about the candidate.
    It records a durable not_promoted verdict, does not increment the infra-error
    streak, and does not raise an infra Alert.  The challenger-confirmed finding
    details are persisted in last_qualification so the strategic loop can target
    the actual issue.
    """
    finalized_at = _utcnow().isoformat()
    record: dict[str, Any] = {
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", True),
        "status": "not_promoted",
        "blocker": "audit_challenge_independent_blocker",
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "audit_challenge_task_id": qual.get("audit_challenge_task_id"),
        "audit_challenge_spawn_error": qual.get("audit_challenge_spawn_error"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
        "audit_findings": independent_blocker_findings,
        # Preserve the one-shot stale-keyless consumption marker so the
        # next sweep can distinguish "same HEAD, already re-qualified once"
        # from "new HEAD, re-qualification is warranted".
        "keyless_requalification_consumed_at": qual.get(
            "keyless_requalification_consumed_at"
        ),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: challenger confirmed an "
            f"independent >=HIGH severity issue — quality finding, returning to "
            f"stabilize for re-qualification"
        ),
    )
    session.add(version)

    promotion_enabled = resolve_project_promotion_enabled(version.project_id)
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=promotion_enabled,
    )

    blocker_detail = ""
    if independent_blocker_findings:
        f0 = independent_blocker_findings[0]
        sev = str(f0.get("severity") or "high").upper()
        summary = str(f0.get("summary") or "").strip()
        blocker_detail = f" ({sev}: {summary[:120]})" if summary else f" ({sev})"
    rq = _release_qualifier_module()
    await rq._notify_operator(
        session,
        version,
        "release_blocked",
        (
            f"v{version.version_number} blocked from promotion — audit challenger "
            f"confirmed an independent >=HIGH severity issue not present in primary "
            f"findings{blocker_detail}. Not promoted; staying in stabilize."
        ),
        extra={
            "blocker": "audit_challenge_independent_blocker",
            "audit_findings": independent_blocker_findings,
        },
    )
    logger.warning(
        "[RELEASE_QUALIFIER] v%d independent blocker: challenger confirmed >=HIGH "
        "issue outside primary findings — verdict=not_promoted (quality finding, "
        "not infra failure)",
        version.version_number,
    )
    return {
        "status": "not_promoted",
        "blocker": "audit_challenge_independent_blocker",
        "audit_findings": independent_blocker_findings,
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
