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
from app.branch_resolution import get_project_promotion_target
from app.database import resolve_dialect_name
from app.landed_signal import landed_signal_predicate
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
from app.qualification_dimensions import LEDGER_DIMENSION_KEYS
from app.release_promotion import _lts_branch_names, _record_qualification_verdict
from app.runner_eligibility import (
    RunnerDispatchSnapshot,
    _runner_accepting_new_work,
    _runner_active_task_count,
    _runner_agent_available,
    _runner_at_capacity,
    _runner_handles_task_type,
)
from app.runtime_config_state import resolve_project_promotion_enabled
from app.shared_state import runtime_config as _runtime_config
from app.task_capabilities import ExecutionSite, LOCAL_RUNNER_EXECUTION_SITE
from app.time_utils import _utcnow
from app.json_extract import async_json_loads
from app.deployed_sha import get_serving_sha

# ── merge-grade contract helpers ─────────────────────────────────────────────
# Canonically defined in app.release_scoring (where the RELEASE_MERGE_GRADE
# artifact is parsed); the direction-scoped objective grade reuses the same
# scope-agnostic evaluator and placeholder-goal predicate as the version path.
from app.release_scoring import (
    evaluate_merge_grade,
    is_contentless_goal,
)
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

_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    }
)


def _coerce_attempt_id(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


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


_CHILD_TAIL_MAX_CHARS = 500


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
        tail = failed_task.latest_status_message
        if tail:
            event["child_tail"] = tail[-_CHILD_TAIL_MAX_CHARS:]
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
    if infra_error.startswith("walkthrough_e2e") or infra_error.startswith(
        "qualification_probe"
    ):
        return "walkthrough_e2e"
    return "audit"


def _infer_dimension_from_infra_error(infra_error: str) -> str | None:
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
    if code.startswith("walkthrough_e2e") or code.startswith(
        "qualification_probe"
    ):
        # Product probes are attempt-scoped decision evidence, not reusable
        # dimensions in the cross-attempt fact ledger.
        return None
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

    migration gate look transient and prevented incapacity escalation.  The
    suffix after the first colon is diagnostic detail too (scratch database names,
    exception text, and other per-attempt values), so only the stable error code
    participates in the signature.
    """
    del child_failures
    stable_error_code = infra_error.partition(":")[0].strip()
    return f"{_infer_infra_leg(infra_error)}:unknown:{stable_error_code}"


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
    if qual.get("walkthrough_e2e_task_id"):
        terminal_ids.append(
            ("walkthrough_e2e", str(qual.get("walkthrough_e2e_task_id")))
        )

    seen_failed_ids = {
        str(row.get("failed_task_id"))
        for row in existing
        if row.get("failed_task_id") is not None
    }

    # Collect valid UUIDs not yet in seen_failed_ids, then batch-fetch in one
    # query instead of one session.get per leg.
    valid_uuids: list[uuid.UUID] = []
    for _leg, task_id in terminal_ids:
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
    qualification_attempt_id: uuid.UUID | str,
    audit_base_sha: str | None = None,
    version_goal: str | None = None,
    categories: list[dict[str, Any]] | None = None,
    rubric_fingerprint: str | None = None,
) -> Task:
    """Create the qualifier's single holistic final-review task."""
    payload: dict[str, Any] = {
        "type": "release_audit",
        "base_branch": base_branch,
        "diff_range": diff_range,
        # The FULL version delta (base_branch...candidate_sha), pinned
        # separately from ``diff_range`` so the agent can narrow the latter to
        # an incremental ``audit_base_sha..candidate`` window for code-quality
        # findings while STILL grading goal alignment and acceptance categories
        # over the full version delta the promotion PR would carry.  Goal and
        # category verdicts have no scoped carry-forward (unlike code-quality
        # findings, which merge unchanged paths from the prior audit fact), so
        # grading them against a shrinking incremental window ratchets every
        # requalification worse.  See dir task-7602202a.
        "goal_category_diff_range": diff_range,
        # Audit C4: pin the audit to the exact qualified commit.  The agent
        # reads this from the payload and checks out this SHA.
        "candidate_sha": candidate_sha,
        "qualification_attempt_id": str(qualification_attempt_id),
        # Explicitly pin to Opus — regression guard against silent downgrade.
        "quality_tier": "opus",
    }
    if version_goal:
        payload["version_goal"] = version_goal
    payload["categories"] = list(categories or [])
    payload["rubric_fingerprint"] = rubric_fingerprint
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
        title=f"Release review: {base_branch}...{candidate_sha[:8]} candidate",
        description=(
            f"Holistic quality and goal review of {diff_range} against the "
            f"release contract"
        ),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
            "qualification_attempt_id": str(qualification_attempt_id),
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
                "goal_category_diff_range": diff_range,
                "audit_base_sha": audit_base_sha,
                "rubric_fingerprint": rubric_fingerprint,
                "release_version_id": str(version_id),
                "qualification_attempt_id": str(qualification_attempt_id),
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
    qualification_attempt_id: uuid.UUID | str,
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
        "qualification_attempt_id": str(qualification_attempt_id),
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
            "qualification_attempt_id": str(qualification_attempt_id),
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
                "qualification_attempt_id": str(qualification_attempt_id),
                **(
                    {"primary_audit_task_id": primary_audit_task_id}
                    if primary_audit_task_id is not None
                    else {}
                ),
            },
        )
    )
    return task


_WALKTHROUGH_E2E_TASK_TYPE = "walkthrough_e2e"

# The supported walkthrough_e2e execution site: a LIVE LOCAL RUNNER proven for
# the candidate-stack site (docker info + Compose + Chromium), pinned at birth.
# The registry declares the SAME site (LOCAL_RUNNER_EXECUTION_SITE), so
# declaration, enforcement, and this consumption path share one typed vocabulary;
# generic dispatch treats the site as non-default and fails closed rather than
# claiming the type.  The annotation against ExecutionSite means a future untyped
# value fails static checking instead of silently diverging.
_WALKTHROUGH_E2E_EXECUTION_SITE: ExecutionSite = LOCAL_RUNNER_EXECUTION_SITE


class QualifierWalkthroughSiteUnavailableError(RuntimeError):
    """Raised (fail-closed) when no live, project-allowed runner can execute the
    qualify-time ``walkthrough_e2e`` candidate-stack walkthrough.

    ``run_qualification`` catches the spawn exception and records a
    ``spawn_failed`` ``last_qualification`` with ``detail`` set to the precise
    execution-site reason, so a misconfigured fleet surfaces immediately instead
    of wasting task-retry cycles on an execution site that can never boot the
    candidate stack (docker compose + Playwright/Chromium).
    """


def _walkthrough_e2e_site_eligible(runner: object) -> bool:
    """Whether a runner proves the candidate-stack walkthrough execution site.

    The candidate stack (``CandidateStackController``) boots via ``docker compose``
    and drives a Playwright/Chromium harness, so the site must prove BOTH the
    Docker runtime AND the Chromium harness:

    * ``isolation_backends.docker_usable is True`` — the heartbeat-proven
      ``docker info`` capability (reported by ``capabilities_inventory``); rejects
      ordinary bwrap-only and podman/apptainer-only boxes.
    * ``isolation_backends.playwright_chromium_usable is True`` — the
      heartbeat-proven Chromium capability (reported by ``capabilities_inventory``
      from the host's Playwright browser cache); rejects thin-docker boxes that
      cannot drive the walkthrough browser.

    Both capabilities are read from the runner's OWN inventory (the nested
    ``isolation_backends`` map the heartbeat emits) — a key that is only
    operator-claimed at the top level, or defaulted/missing, is unprovable and
    fails closed. A thin-docker box (Docker runtime but no Chromium harness) or a
    box that never declared ``walkthrough_e2e`` task coverage is rejected.
    """
    caps = getattr(runner, "capabilities", None) or {}
    backends = caps.get("isolation_backends")
    docker_ok = isinstance(backends, dict) and backends.get("docker_usable") is True
    chromium_ok = (
        isinstance(backends, dict)
        and backends.get("playwright_chromium_usable") is True
    )
    return docker_ok and chromium_ok


async def _select_walkthrough_e2e_execution_site(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> tuple[RunnerDispatchSnapshot | None, str]:
    """Resolve the live, project-allowed runner for the qualify-time walkthrough.

    Reuses the canonical runner-eligibility contracts — task-type coverage
    (``_runner_handles_task_type``), agent availability (``_runner_agent_available``),
    accepting-new-work (``_runner_accepting_new_work``), capacity
    (``_runner_at_capacity``), and the container-runtime/site gate
    (:func:`_walkthrough_e2e_site_eligible``) — over the canonical live-runner
    cache (:func:`app.orchestrator_runner_dispatch._load_active_runners_cache`).
    Selection is deterministic: least effective load, then lowest runner id.

    Returns ``(runner, "")`` when a site exists, else ``(None, reason)`` where
    ``reason`` names the precise execution-site gap so the fail-closed record is
    actionable.
    """
    # Imported lazily: ``app.orchestrator_runner_dispatch`` transitively imports
    # ``app.routers.admin_runners`` (via ``strategic_runtime_work``) and that
    # router chain reaches back to ``app.release_qualifier`` → this module, so a
    # top-level import here would be a load-time cycle. By the time this helper
    # runs, both modules are fully initialized.
    from app.orchestrator_runner_dispatch import _load_active_runners_cache

    active_runners_cache = await _load_active_runners_cache(session)
    candidates = active_runners_cache.get(project_id) or []
    if not candidates:
        return None, "no live project-allowed runner is registered"
    handles_type = [
        r
        for r in candidates
        if _runner_handles_task_type(r, _WALKTHROUGH_E2E_TASK_TYPE)
    ]
    if not handles_type:
        return (
            None,
            "no live project-allowed runner covers task type walkthrough_e2e",
        )
    base_eligible = [
        r
        for r in handles_type
        if _runner_accepting_new_work(r)
        and _runner_agent_available(r)
        and not _runner_at_capacity(r, 0, _WALKTHROUGH_E2E_TASK_TYPE)
    ]
    if not base_eligible:
        return (
            None,
            "walkthrough_e2e runners are draining, agent-unavailable, or at capacity",
        )
    site_eligible = [r for r in base_eligible if _walkthrough_e2e_site_eligible(r)]
    if not site_eligible:
        return (
            None,
            "no eligible walkthrough_e2e runner proves the candidate-stack site "
            "(isolation_backends.docker_usable and playwright_chromium_usable)",
        )
    selected = min(
        site_eligible,
        key=lambda r: (_runner_active_task_count(r), str(r.id)),
    )
    return selected, ""


async def _spawn_qualifier_walkthrough_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    candidate_sha: str,
    qualification_attempt_id: uuid.UUID | str,
) -> Task:
    """Create + dispatch a qualify-time ``walkthrough_e2e`` task on the candidate.

    The supported execution site is a LIVE LOCAL RUNNER (not a GCE VM): this
    resolves a runner proven for the candidate-stack site (docker info + Compose
    + Chromium via :func:`_walkthrough_e2e_site_eligible`) and pins the task to
    it AT BIRTH (``vm_instance_id='runner:{id}'`` + ``context['runner_target_id']``
    + PROVISIONING + ``vm_provisioned_at``, with ``started_at`` NULL so the runner
    stamps it on pull) — mirroring the orchestrator's own pin contract
    (``_try_runner_dispatch``) and the strategic-spawn pin-at-birth path. The
    task's ``execution_site`` context label records the actual dispatch target
    (:data:`_WALKTHROUGH_E2E_EXECUTION_SITE`), which the registry declares for the
    task type, so declaration, enforcement, and consumption share one vocabulary.
    When no eligible site exists the spawn FAILS CLOSED (raises
    :class:`QualifierWalkthroughSiteUnavailableError`) before any task row is
    created, so the caller records a ``spawn_failed`` qualification with the
    precise execution-site reason instead of queueing a child that can never boot
    the candidate stack.
    """
    runner, site_reason = await _select_walkthrough_e2e_execution_site(
        session, project_id
    )
    if runner is None:
        raise QualifierWalkthroughSiteUnavailableError(
            f"cannot dispatch qualifier walkthrough_e2e for candidate "
            f"{candidate_sha[:8]}: {site_reason}"
        )
    project = await session.get(Project, project_id)
    integration_branch, _promotion_target = _lts_branch_names(project)
    payload: dict[str, Any] = {
        "type": "walkthrough_e2e",
        "base_branch": integration_branch,
        "candidate_sha": candidate_sha,
        "qualification_attempt_id": str(qualification_attempt_id),
    }
    try:
        payload = validate_internal_payload(payload, "release_qualifier:walkthrough_e2e")
    except PayloadTooLargeError:
        raise

    task = Task(
        project_id=project_id,
        title=f"Qualifier walkthrough e2e: candidate {candidate_sha[:8]}",
        description=(
            f"Qualify-time live-stack walkthrough of release candidate {candidate_sha}"
        ),
        payloads=[payload],
        vm_instance_id=f"runner:{runner.id}",
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=_utcnow(),
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "qualifier_walkthrough_e2e": True,
            "candidate_sha": candidate_sha,
            "qualification_attempt_id": str(qualification_attempt_id),
            "execution_site": _WALKTHROUGH_E2E_EXECUTION_SITE,
            "runner_target_id": str(runner.id),
            "runner_hostname": runner.hostname,
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
                "task_type": "walkthrough_e2e",
                "qualifier_walkthrough_e2e": True,
                "execution_site": _WALKTHROUGH_E2E_EXECUTION_SITE,
                "release_version_id": str(version_id),
                "qualification_attempt_id": str(qualification_attempt_id),
                "runner_target_id": str(runner.id),
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
    qualification_attempt_id: uuid.UUID | str,
    version_goal: str,
    merged_pr_numbers: list[int],
    categories: list[dict[str, Any]] | None = None,
    rubric_fingerprint: str | None = None,
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
        "qualification_attempt_id": str(qualification_attempt_id),
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
        "rubric_fingerprint": rubric_fingerprint,
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
            "qualification_attempt_id": str(qualification_attempt_id),
            "rubric_fingerprint": rubric_fingerprint,
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
                "qualification_attempt_id": str(qualification_attempt_id),
                "merged_pr_count": len(merged_pr_numbers),
                "rubric_fingerprint": rubric_fingerprint,
            },
        )
    )
    return task


# ---------------------------------------------------------------------------
# Direction-scoped blind objective grade (dir d-4f944896)
# ---------------------------------------------------------------------------
# The version-scoped merge grade above is hard-wired to the one open
# ReleaseVersion.  A DIRECTION (campaign) has no version; its objective is the
# strategist-authored steer (``completion_criteria`` / ``context.success_evidence``)
# and its "integrated merged diff" is the union of the PRs its tasks landed.  The
# spawner below creates the same blind ``release_merge_grade`` task scoped to a
# direction, and :func:`record_direction_merge_grade` consumes the completed
# artifact into the DIRECTION-scoped category-verdict ledger (the version-scoped
# inline recording in ``release_qualifier.finalize_qualification`` is the exact
# analogue).  The BLINDNESS contract is preserved verbatim: the grader sees the
# direction objective and the merged diffs ONLY — never the strategist narrative.


def _resolve_direction_objective(direction: dict[str, Any]) -> str | None:
    """Resolve a direction's DECLARED OBJECTIVE text, or None.

    ``completion_criteria`` lives directly on the direction dict (see
    ``claim_task_helpers._build_direction_contract``); when it is unset the
    fallback is ``context["success_evidence"]`` — the success contract the
    campaign's objective is judged against.  First non-empty string wins.
    """
    if not isinstance(direction, dict):
        return None
    completion = direction.get("completion_criteria")
    if isinstance(completion, str) and completion.strip():
        return completion.strip()
    ctx = direction.get("context")
    if isinstance(ctx, dict):
        success = ctx.get("success_evidence")
        if isinstance(success, str) and success.strip():
            return success.strip()
    return None


def _task_pr_number(task: Task) -> int | None:
    """Extract the merged PR number a task shipped, or None.

    Reads the PR-reference keys the codebase uses across task contexts —
    ``context["pr_number"]``, ``context["rollup_pr_number"]``,
    ``context["review_loop"]["pr_number"]`` — with the legacy
    ``first_payload["pr_number"]`` fallback (mirrors branch_cleanup /
    llm_cost_attribution's read surface).
    """
    ctx = task.context or {}
    review_loop = ctx.get("review_loop")
    for raw in (
        ctx.get("pr_number"),
        ctx.get("rollup_pr_number"),
        review_loop.get("pr_number") if isinstance(review_loop, dict) else None,
        (task.first_payload or {}).get("pr_number"),
    ):
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


async def _collect_direction_merged_pr_numbers(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
) -> list[int]:
    """Collect the merged-PR window of a direction's INTEGRATED MERGED DIFF.

    Every COMPLETED task tagged ``context["direction_id"] == direction_id`` that
    truly landed on the integration branch (``landed_signal_predicate`` — the
    same landed-signal predicate the version acceptance-window uses) contributes
    its merged PR number, deduplicated and sorted ascending.  This is the window
    the blind grader fetches each PR's diff from to grade goal coverage.
    """
    from sqlalchemy import cast as _cast
    from sqlalchemy import func as _func
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB

    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        direction_filter = (
            _func.json_extract(Task.context, "$.direction_id") == direction_id
        )
    else:
        direction_filter = Task.context.op("@>")(
            _cast({"direction_id": direction_id}, _JSONB)
        )
    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            direction_filter,
            landed_signal_predicate(session),
        )
        .order_by(Task.id)
    )
    seen: set[int] = set()
    merged_prs: list[int] = []
    for _task in result.scalars().all():
        pr = _task_pr_number(_task)
        if pr is not None and pr not in seen:
            seen.add(pr)
            merged_prs.append(pr)
    # Deterministic payload: the merged-PR window is a SET of PR numbers, so sort
    # numerically regardless of task-id ordering (Task.id is a UUID, not a
    # creation counter).
    return sorted(merged_prs)


async def _spawn_direction_merge_grade_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    direction: dict[str, Any],
    candidate_sha: str,
    qualification_attempt_id: uuid.UUID | str,
    categories: list[dict[str, Any]] | None = None,
    rubric_fingerprint: str | None = None,
    base_branch: str | None = None,
    merged_pr_numbers: list[int] | None = None,
) -> Task | None:
    """Create + queue a blind ``release_merge_grade`` task for a DIRECTION.

    The direction analog of :func:`_spawn_release_merge_grade_task`.  The agent
    grades the direction's INTEGRATED MERGED DIFF (the ``merged_prs`` window
    derived from tasks tagged with this direction) against the direction's
    DECLARED OBJECTIVE and emits the same goal-coverage verdict shape.

      * OBJECTIVE RESOLUTION — ``completion_criteria`` then
        ``context.success_evidence`` (see :func:`_resolve_direction_objective`).
        FAILS CLOSED when no real objective exists: a missing or contentless
        objective returns ``None`` and no task row is minted (reuses
        ``release_scoring.is_contentless_goal`` — the same placeholder predicate
        the version path uses to skip spawning).
      * INTEGRATED MERGED DIFF — ``base_branch`` defaults to the project's
        PROMOTION TARGET (the stable ancestor the version path also pins its
        diff to); ``candidate_sha`` is the caller-supplied integrated-diff tip;
        ``merged_prs`` is derived from landed direction tasks (or supplied
        directly by the caller).  The promotion target is the base so a
        terminal rollup (candidate == integration-branch tip) still grades a
        NON-EMPTY three-dot range — pinning the integration branch there would
        yield ``automain...automain-tip`` = empty and the objective could never
        be graded.
      * SCOPE CARRIER — ``direction_id`` rides in BOTH the task payload and the
        task context so the agent can echo it and the backend can key results.

    BLINDNESS CONTRACT (dir d-c0fe439f) — preserved verbatim from the version
    spawner: the grader sees the frozen objective and the merged diffs ONLY; the
    ``assert`` tripwire fails loudly if a future edit ever leaks a running-update
    key into the payload.
    """
    direction_id = str(direction.get("id") or direction.get("direction_id") or "").strip()
    if not direction_id:
        logger.warning(
            "[RELEASE_QUALIFIER] _spawn_direction_merge_grade_task: direction "
            "carries no id/direction_id — refusing to spawn a grade",
        )
        return None
    objective = _resolve_direction_objective(direction)
    if not objective or is_contentless_goal(objective):
        logger.warning(
            "[RELEASE_QUALIFIER] _spawn_direction_merge_grade_task: direction %s "
            "has no gradeable objective (completion_criteria/context.success_evidence "
            "missing or contentless) — spawning nothing",
            direction_id,
        )
        return None
    if base_branch is None:
        _project = await session.get(Project, project_id)
        if _project is None:
            # Fail closed: without the project row there is no project-aware
            # promotion target to pin the diff range to — never fabricate a
            # hardcoded "main" fallback (branch-literal compliance invariant).
            logger.warning(
                "[RELEASE_QUALIFIER] _spawn_direction_merge_grade_task: project %s "
                "not found — refusing to spawn a grade without a promotion target",
                project_id,
            )
            return None
        # Pin the diff base to the STABLE promotion target (never the live
        # integration branch): for a terminal rollup the candidate_sha IS the
        # integration-branch tip, so an integration-branch base would produce an
        # EMPTY three-dot range and the objective could never be graded.  The
        # promotion target is always a stable ancestor of the candidate and
        # matches the version path's `promotion_target...<candidate_sha>`.
        base_branch = get_project_promotion_target(_project)
    if merged_pr_numbers is None:
        merged_pr_numbers = await _collect_direction_merged_pr_numbers(
            session, project_id, direction_id
        )
    payload: dict[str, Any] = {
        "type": "release_merge_grade",
        "base_branch": base_branch,
        "diff_range": f"{base_branch}...{candidate_sha}",
        # Pin the grade to the integrated-diff tip the caller resolved.
        "candidate_sha": candidate_sha,
        "qualification_attempt_id": str(qualification_attempt_id),
        # The ONLY narrative the grader is allowed to see: the frozen objective.
        "version_goal": objective,
        # The direction's merged-PR window (same contract the version grade uses);
        # the agent fetches each PR's diff to grade goal coverage.
        "merged_prs": list(merged_pr_numbers),
        # Goal-verification redesign: the ACTIVE acceptance-criterion CATEGORIES
        # (the direction's declared acceptance criteria), same contract as the
        # version spawner.
        "categories": list(categories or []),
        "rubric_fingerprint": rubric_fingerprint,
        # Direction scope discriminator — the agent echoes it and the backend
        # keys results into the direction-scoped ledger.
        "direction_id": direction_id,
        # Explicitly pin to Opus — regression guard against silent downgrade
        # (mirrors the version spawner).
        "quality_tier": "opus",
    }
    # Blindness guard — see the version spawner's contract note. No payload key
    # may carry the strategist running-update narrative into a grade that must be
    # blind to it under BOTH scopes.
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
        # A too-large merge-grade payload (an enormous objective or PR list) is a
        # real bug worth surfacing — let it raise to the spawn caller's handler.
        raise

    task = Task(
        project_id=project_id,
        title=f"Direction merge grade: {direction_id} ({base_branch}...{candidate_sha[:8]}) vs objective",
        description=(
            f"Blind goal-coverage grade of the direction's integrated merged "
            f"diffs ({len(merged_pr_numbers)} merged PRs) against its declared objective"
        ),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "direction_id": direction_id,
            "candidate_sha": candidate_sha,
            "qualification_attempt_id": str(qualification_attempt_id),
            "rubric_fingerprint": rubric_fingerprint,
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
                "direction_id": direction_id,
                "candidate_sha": candidate_sha,
                "qualification_attempt_id": str(qualification_attempt_id),
                "merged_pr_count": len(merged_pr_numbers),
                "rubric_fingerprint": rubric_fingerprint,
            },
        )
    )
    return task


async def record_direction_merge_grade(
    session: AsyncSession,
    *,
    direction_id: str,
    project_id: uuid.UUID,
    goal: str,
    artifact: dict[str, Any],
    active_categories: list[dict[str, Any]] | None = None,
    qualification_attempt_id: uuid.UUID | None = None,
    grader_run_id: Any = None,
) -> dict[str, Any]:
    """Consume a completed direction-scoped ``RELEASE_MERGE_GRADE`` artifact.

    The RESULT side of the direction grade: evaluates the artifact with
    ``release_scoring.evaluate_merge_grade`` (already scope-agnostic — goal +
    artifact + active_categories) and records the per-category verdicts + residual
    into the DIRECTION-scoped category-verdict ledger keyed on ``direction_id``
    (the exact counterpart of the version-scoped inline recording in
    ``release_qualifier.finalize_qualification``).  The ``objective_gap``
    completion-debt consequence rides along inside each ledger upsert, keyed on
    ``objective_gap:direction:<direction_id>:<category_key>``.  Ledger writes are
    best-effort and never raise — a ledger hiccup must not break grading.

    Returns the evaluated verdict dict (status/covers_goal/coverage/biggest_gap/
    category_verdicts/residual_uncaptured_gap) PLUS a ``ledger_writes`` boolean —
    True only when at least one category-verdict/residual row was actually
    persisted for this grade.  A consumer that only wants to re-stamp its
    direction when a grade was NEWLY RECORDED checks ``ledger_writes`` so an
    artifact with no gradable content (missing ``graded_candidate_sha``, no
    per-category verdicts) does not trigger a write.
    """
    cert = evaluate_merge_grade(goal, artifact, active_categories or None)
    candidate_sha = (
        str(artifact.get("graded_candidate_sha") or "").strip()
        if isinstance(artifact, dict)
        else ""
    ) or None
    fresh_verdicts = cert.get("category_verdicts")
    ledger_writes = False
    if fresh_verdicts and candidate_sha:
        try:
            from app import category_verdict_ledger as _ledger

            for _v in fresh_verdicts:
                await _ledger.upsert_category_verdict(
                    session,
                    direction_id=direction_id,
                    candidate_sha=candidate_sha,
                    category_key=_v.get("category_key"),
                    status=_v.get("status"),
                    tier=_v.get("tier", "judged"),
                    gap=_v.get("gap"),
                    citations=_v.get("citations") or [],
                    grader_run_id=grader_run_id,
                    qualification_attempt_id=qualification_attempt_id,
                    project_id=project_id,
                )
            await _ledger.upsert_residual(
                session,
                direction_id=direction_id,
                candidate_sha=candidate_sha,
                uncaptured_gap=cert.get("residual_uncaptured_gap"),
                qualification_attempt_id=qualification_attempt_id,
                project_id=project_id,
            )
            ledger_writes = True
        except Exception:  # noqa: BLE001 — ledger write failure must not break grading
            logger.warning(
                "[RELEASE_QUALIFIER] failed to persist direction-scoped "
                "merge-grade verdicts for direction %s — grading continues with "
                "the in-memory verdict",
                direction_id,
                exc_info=True,
            )
    return {**cert, "ledger_writes": ledger_writes}


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
    engine_sha = get_serving_sha()
    prior_history = _bounded_infra_history(qual.get("infra_attempt_history"))
    attempt_entry = {
        "finalized_at": finalized_at,
        "error": infra_error,
        "signature": signature,
        "candidate_sha": qual.get("candidate_sha"),
        "engine_sha": engine_sha,
        "failing_leg": _infer_infra_leg(infra_error),
        "child_failures": child_failures,
    }
    attempt_history = [*prior_history, attempt_entry][-_INFRA_ATTEMPT_HISTORY_LIMIT:]

    # Permanent gate-incapacity detection: count trailing same-signature entries
    # in attempt_history.  When ANY qualifier gate (smoketest, audit, challenge,
    # merge_grade, migration_apply) repeats with the exact same signature across
    # _MIN_INFRA_ERROR_STREAK or more consecutive attempts, the condition is
    # structural and will not self-resolve by retry — escalate with a different
    # operator message.
    is_permanent_gate_incapacity = False
    same_signature_streak = 0
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
        "qualification_attempt_id": qual.get("qualification_attempt_id"),
        "retry_episode": qual.get("retry_episode", 0),
        "retry_episode_cap": qual.get("retry_episode_cap"),
        "candidate_sha": qual.get("candidate_sha"),
        "engine_sha": engine_sha,
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", False),
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
        "requested_probes": qual.get("requested_probes") or [],
        # Durable record of a product probe dropped at request time
        # (blocker="incompatible_probe"), carried forward from the pending record
        # so the infra-failure record keeps the drop trace for the re-drive.
        "qualify_request_blocked": qual.get("qualify_request_blocked"),
        "qualify_request_blocked_at": qual.get("qualify_request_blocked_at"),
        "walkthrough_e2e_task_id": qual.get("walkthrough_e2e_task_id"),
        "walkthrough_e2e_result": qual.get("walkthrough_e2e_result"),
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
    dimension_infra_streak = (
        await _release_qualifier_module()._record_infra_dimension_unavailable(
            session,
            version,
            qual.get("candidate_sha"),
            failing_dimension,
            infra_error=infra_error,
            signature=signature,
            origin_attempt_id=qual.get("qualification_attempt_id"),
        )
        if failing_dimension is not None
        else None
    )

    failing_leg = _infer_infra_leg(infra_error)
    hotfix_preview: dict[str, Any] | None = None
    if is_permanent_gate_incapacity and infra_error.startswith("migration_apply"):
        # Make the sanctioned remediation concrete at the point incapacity is
        # declared.  This is read-only and deliberately best-effort: inability
        # to preview must not turn a held release into a promotion.
        # Only migration_apply has a meaningful hotfix-scope preview; other legs
        # (smoketest, audit, challenge) have no narrowed hotfix path.
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
        # Persist the hotfix eligibility preview into the qualification record so
        # post-mortem analysis can see the eligible changes without re-computing.
        if hotfix_preview and hotfix_preview.get("eligible"):
            infra_evidence["hotfix_preview"] = hotfix_preview
            record["hotfix_preview"] = hotfix_preview
            flag_modified(version, "last_qualification")

    # Phase 1b read: compose already-resolved dimension facts from the per-dimension
    # ledger into the record and verdict sub_scores, so a passed audit/CI/etc. is
    # visible in the verdict instead of being discarded by a transient infra defer
    # on a DIFFERENT gate.  Best-effort — any read failure leaves resolved_dimensions
    # empty and the infra-failure hold proceeds exactly as before.
    resolved_dimensions: dict[str, Any] = {}
    _ledger_candidate_sha = qual.get("candidate_sha")
    if _ledger_candidate_sha:
        try:
            _prior = await _release_qualifier_module()._load_prior_dimension_results(
                session, version, _ledger_candidate_sha
            )
            _resolved_statuses = _release_qualifier_module()._LEDGER_RESOLVED_STATUSES
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
                f"The {failing_leg} gate for v{version.version_number} is "
                f"persistently incapable across {same_signature_streak} "
                f"same-signature attempts ({infra_error!r}) — the underlying "
                f"condition will not self-resolve by retry. "
                f"{preview_text}Sanctioned remediation: use the hotfix scope "
                f"preview and approved hotfix endpoint (POST /projects/{{id}}/promote/hotfix), "
                f"not whole-tree force-promotion. "
                f"Signature: {signature}. Engine: {engine_sha or 'unknown'}."
            )
        else:
            alert_message = (
                f"The release qualifier for v{version.version_number} has "
                f"failed {current_streak} consecutive time(s) due to "
                f"infrastructure errors ({infra_error!r}) — the qualifier "
                f"agent VM or clone pipeline is broken. This is NOT a "
                f"quality finding about the release candidate. Inspect the "
                f"release-audit, challenge, and goal-grade agent infrastructure "
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
                event_type="release.qualification_crash_loop",
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
            f"v{version.version_number} {failing_leg} gate is persistently "
            f"incapable across {same_signature_streak} same-signature attempts "
            f"({infra_error.replace('_', ' ')}) — the underlying condition "
            f"will not self-resolve by retry. "
            f"Use the sanctioned hotfix scope preview and endpoint "
            f"(POST /projects/{{id}}/promote/hotfix), not whole-tree force-promotion."
            f"{preview_text} Engine: {engine_sha or 'unknown'}."
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
        _release_qualifier_module().dispatch_external_notification(
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
        "engine_sha": engine_sha,
        "infra_failure_signature": signature,
        "same_error_as_prior": same_error_as_prior,
        "same_error_different_candidate": same_error_different_candidate,
        "consecutive_infra_errors": current_streak,
        "is_permanent_gate_incapacity": is_permanent_gate_incapacity,
        "same_signature_streak": same_signature_streak,

        # Hotfix scope preview (present only when is_permanent_gate_incapacity
        # and the preview computation succeeded with an eligible result).
        "hotfix_preview": hotfix_preview,

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
        "qualification_attempt_id": qual.get("qualification_attempt_id"),
        "retry_episode": qual.get("retry_episode", 0),
        "retry_episode_cap": qual.get("retry_episode_cap"),
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", False),
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


async def required_children_terminal(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return whether every active qualifier child is terminal.

    Retired smoke children are deliberately ignored. Ledger-backed dimensions
    carried into the active attempt do not require a new child.
    """
    qual = version.last_qualification or {}
    resolved_dims: dict[str, dict[str, Any]] = {
        dim: fact
        for dim, fact in (qual.get("resolved_dimensions") or {}).items()
        if dim in LEDGER_DIMENSION_KEYS
    }
    audit_resolved = "audit" in resolved_dims and "test_erosion" in resolved_dims
    merge_grade_resolved = "merge_grade" in resolved_dims

    audit_task_id_raw = qual.get("audit_task_id")
    challenge_task_id_raw = qual.get("challenge_task_id")
    merge_grade_task_id_raw = qual.get("merge_grade_task_id")
    walkthrough_requested = "walkthrough_e2e" in set(
        qual.get("requested_probes") or []
    )
    walkthrough_task_id_raw = qual.get("walkthrough_e2e_task_id")
    challenge_required = bool(challenge_task_id_raw)
    merge_grade_required = bool(merge_grade_task_id_raw)
    # A present audit child is authoritative even when older code-quality facts
    # were carried forward. Contract drift deliberately spawns one fresh
    # holistic audit while retaining those facts for provenance; finalization
    # must wait for that child to finish.
    audit_required = bool(audit_task_id_raw) or not audit_resolved

    if not audit_task_id_raw and not audit_resolved:
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has pending "
            "qualification but no audit child or resolved audit facts",
            version.version_number,
        )
        return False

    try:
        audit_task_id = _coerce_attempt_id(audit_task_id_raw)
        challenge_task_id = (
            _coerce_attempt_id(challenge_task_id_raw) if challenge_required else None
        )
        merge_grade_task_id = (
            _coerce_attempt_id(merge_grade_task_id_raw)
            if merge_grade_required
            else None
        )
        walkthrough_task_id = (
            _coerce_attempt_id(walkthrough_task_id_raw)
            if walkthrough_requested and walkthrough_task_id_raw
            else None
        )
        if audit_task_id_raw is not None and audit_task_id is None:
            raise ValueError("invalid audit task id")
        if challenge_required and challenge_task_id is None:
            raise ValueError("invalid challenge task id")
        if merge_grade_required and merge_grade_task_id is None:
            raise ValueError("invalid merge-grade task id")
        if walkthrough_requested and walkthrough_task_id_raw and walkthrough_task_id is None:
            raise ValueError("invalid walkthrough task id")
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has unparseable "
            "active child task ids (audit=%r challenge=%r merge_grade=%r "
            "walkthrough=%r) -- not ready",
            version.version_number,
            audit_task_id_raw,
            challenge_task_id_raw,
            merge_grade_task_id_raw,
            walkthrough_task_id_raw,
        )
        return False

    audit_task = await session.get(Task, audit_task_id) if audit_task_id else None
    challenge_task = (
        await session.get(Task, challenge_task_id) if challenge_task_id else None
    )
    merge_grade_task = (
        await session.get(Task, merge_grade_task_id) if merge_grade_task_id else None
    )
    walkthrough_task = (
        await session.get(Task, walkthrough_task_id) if walkthrough_task_id else None
    )

    active_attempt_id = _coerce_attempt_id(qual.get("qualification_attempt_id"))
    if active_attempt_id is not None:
        for leg, task in (
            ("audit", audit_task),
            ("challenge", challenge_task),
            ("merge_grade", merge_grade_task),
            ("walkthrough_e2e", walkthrough_task),
        ):
            if task is None:
                continue
            child_attempt_id = _coerce_attempt_id(
                (task.context or {}).get("qualification_attempt_id")
            )
            if child_attempt_id != active_attempt_id:
                logger.warning(
                    "[RELEASE_QUALIFIER] finalize-gate: v%d %s child %s belongs "
                    "to attempt %s, active attempt is %s -- not ready",
                    version.version_number,
                    leg,
                    task.id,
                    child_attempt_id,
                    active_attempt_id,
                )
                return False

    if (
        (audit_required and audit_task is None)
        or (challenge_required and challenge_task is None)
        or (merge_grade_required and not merge_grade_resolved and merge_grade_task is None)
    ):
        return False
    # A MISSING walkthrough_e2e child (absent task id, or the row was deleted)
    # is deliberately NOT a non-terminal condition: there is no live child to
    # wait for, and _finalize_qualification holds an absent requested probe as
    # an infra failure (reason walkthrough_e2e_task_missing — a non-conclusive
    # hold that the sweep's finalize path re-drives, never a quality verdict).
    # Treating it as missing-and-terminal lets the sweep's finalize path process
    # the qualification instead of stranding it in pending until the 6h timeout.
    # A PRESENT, non-terminal walkthrough child still blocks below.

    active_children = (
        (audit_required, audit_task),
        (challenge_required, challenge_task),
        (merge_grade_required and not merge_grade_resolved, merge_grade_task),
        (walkthrough_requested, walkthrough_task),
    )
    if any(
        required and task is not None and task.status not in _TERMINAL_STATUSES
        for required, task in active_children
    ):
        return False

    logger.info(
        "[RELEASE_QUALIFIER] finalize-gate: active children terminal for v%d",
        version.version_number,
    )
    return True
