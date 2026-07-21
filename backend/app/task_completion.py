"""Task completion handlers and their supporting functions.

Extracted from app.main to keep main.py focused on routing and orchestration.
These handlers are called from _process_post_commit() in main.py based on task type.

Anomaly detection, research completion, and strategic completion handlers have
been further extracted into their own modules (anomaly_detection.py,
research_completion.py, strategic_completion.py).
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.github import (
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,  # noqa: F401  (re-exported for tests / downstream)
    build_blueprint_pr_merge_summary_comment,  # noqa: F401  (re-exported for tests / downstream)
    compare_ref_status,
    create_branch,
    create_pr_comment,
    delete_branch,
    get_branch_sha,
    get_open_pull_requests,
    submit_pr_review,
)
from app.models import (
    Artifact,
    ComputeJob,
    ComputeJobStatus,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.deploy_identity import project_is_deploy_repo
from app.repo_bootstrap import (
    BOOTSTRAP_RECOVERY_MAX_ATTEMPTS,
    coerce_recovery_attempts,
    next_recovery_backoff,
)
from app.compute_jobs import (
    _is_durable_artifact_uri,
    get_compute_job_for_task,
    stamp_compute_job_result_reference,
)
from app.artifact_prerequisites import (
    ARTIFACT_PREREQUISITES_KEY,
    _artifact_pointer,
    deliver_artifact_to_dependents,
    stamp_artifact_logical_key,
)
from app.compute_experiment_contract import (
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY,
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY,
)
from app.task_capabilities import WRITE_TASK_TYPES
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from app.task_evaluators import (
    BLIND_REVIEW_SOURCE_KEY,
    REVIEW_SECOND_OPINION_AWAITING_KEY,
    REVIEW_SECOND_OPINION_PARKED_KEY,
    REVIEW_SECOND_OPINION_SOURCE_KEY,
    EvaluationVerdict,
    _extract_compute_measurement,
    describe_emitted_compute_metric_keys,
    evaluate_compute_success_criterion,
    evaluate_terminal_output,
    record_failed_gate,
)
from app.plan_completion import (
    # Re-exports — task_completion is the canonical completion-API import surface
    # for downstream modules (task_lifecycle, research_completion, tests). The
    # implementations live in plan_completion.py, but a 2026-05-03 ruff --fix
    # quietly removed these as "unused" within task_completion.py and silently
    # broke every downstream importer. noqa pins them so a future --fix sweep
    # can't repeat the regression.
    _handle_plan_completion,  # noqa: F401
    _inject_build_on_deps,  # noqa: F401
    _inject_file_overlap_deps,  # noqa: F401
    _log_file_overlaps,  # noqa: F401
    _parse_plan_output,  # noqa: F401
    _validate_plan_dependencies,  # noqa: F401
)
from app.routers.agent import get_github_token_for_task
from app.shared_state import (
    get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.payload_schemas import read_acceptance_evidence, validate_internal_payload
from app.broadcast import broadcast_to_project
from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX
from app.time_utils import _utcnow
from app.task_locking import (
    project_lock,
    with_design_doc_locked,
    with_directions_locked,
)
from app.vm_cost_rates import get_vm_rate_per_second
from app.json_extract import (  # noqa: F401  (re-exported; callers import from here)
    _extract_json_array,
    _extract_last_json_array,
    _extract_json_object,
    async_json_loads,
)

logger = logging.getLogger(__name__)


_COMPUTE_WAKE_POST_COMMIT_META_KEY = "_compute_wake_post_commit_meta"
_COMPUTE_WAKE_NEXT_RETRY_KEY = "_compute_wake_next_retry_after"
_COMPUTE_WAKE_MAX_ATTEMPTS = 12


# Shared marker used by both the TypeScript agent handler (review-fix.ts
# buildBlueprintPrFooter) and this Python helper so either side can update
# the same block without leaving duplicates.
BLUEPRINT_PR_META_MARKER = "<!-- blueprint:meta -->"


async def _persist_direction_field_update(
    session: AsyncSession,
    project: Project,
    direction: Dict[str, Any],
    *,
    deleted_keys: frozenset = frozenset(),
) -> None:
    """Serialize one mutated direction dict back into ``project.directions``.

    The caller has already applied every field mutation to ``direction`` — an
    element of a directions list that was loaded *outside* any lock. Writing the
    whole stale list back (the legacy ``project.directions = directions``
    pattern) loses a concurrent same-project completion's write to a *different*
    direction. This helper instead re-acquires the project row lock via
    ``with_directions_locked``, re-reads the directions list fresh under that
    lock, and merges only the field delta onto the matching entry — every other
    direction, and every field not touched by the caller, is left exactly as the
    fresh read found it.

    ``deleted_keys`` must enumerate any keys that the caller explicitly removed
    from ``direction`` (via ``pop``). Because we no longer clear the fresh entry
    before merging, those keys would otherwise survive in the DB from a previous
    write. Callers that only add/update fields may omit this parameter.

    ``session.expire`` is required because the app runs ``expire_on_commit=False``:
    without it, ``with_directions_locked``'s ``SELECT ... FOR UPDATE`` returns the
    already-cached (stale) Project instance rather than the freshly-committed row
    data, defeating the lock. Expiring the attribute forces a fresh load inside
    the lock.
    """
    dir_id = direction.get("id")
    session.expire(project, ["directions"])

    def _apply(fresh_dirs: List[Dict[str, Any]]):
        target = next((d for d in fresh_dirs if d.get("id") == dir_id), None)
        if target is not None:
            target.update(direction)
            for k in deleted_keys:
                target.pop(k, None)
        return None

    await with_directions_locked(session, project.id, _apply)


def _estimated_task_cost_usd(task: Task) -> Optional[float]:
    """Return the VM cost for a task, or None when timing data is incomplete."""
    if not task.vm_provisioned_at or not task.vm_stopped_at:
        return None
    rate = get_vm_rate_per_second(machine_type=task.vm_machine_type, spot=task.vm_spot)
    return (task.vm_stopped_at - task.vm_provisioned_at).total_seconds() * rate


def _extract_compute_result_reference(logs: str) -> Optional[str]:
    try:
        payload = json.loads(logs)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    reference = payload.get("result_reference")
    if isinstance(reference, str):
        reference = reference.strip()
        return reference or None
    return None


def _extract_compute_job_id(logs: str) -> Optional[uuid.UUID]:
    try:
        payload = json.loads(logs)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_job_id = payload.get("job_id")
    if raw_job_id is None:
        return None
    try:
        return uuid.UUID(str(raw_job_id))
    except (TypeError, ValueError):
        return None


async def _stamp_compute_result_resource(
    session: AsyncSession, task: Task, logs: str
) -> dict[str, Any]:
    """Stamp and return durable resource identity for a compute result."""
    resource: dict[str, Any] = {}
    result_reference = _extract_compute_result_reference(logs)
    if result_reference is not None:
        resource["result_reference"] = result_reference
    job_id = _extract_compute_job_id(logs)
    if job_id is None:
        return resource

    if result_reference is not None:
        job = await stamp_compute_job_result_reference(
            session,
            task_id=task.id,
            job_id=job_id,
            result_reference=result_reference,
        )
    else:
        job = await get_compute_job_for_task(session, job_id=job_id, task_id=task.id)
    if job is None:
        return resource
    if job.result_reference:
        resource["result_reference"] = job.result_reference
    runner_artifact = (job.polling_metadata or {}).get("runner_artifact")
    transfer = (job.polling_metadata or {}).get("runner_artifact_transfer")
    if isinstance(runner_artifact, dict):
        resource["artifact_delivery_report"] = {
            "status": "verified" if runner_artifact.get("verified") is True else "legacy",
            "verified": runner_artifact.get("verified") is True,
            "size": runner_artifact.get("size"),
            "crc32c": runner_artifact.get("crc32c"),
            "generation": runner_artifact.get("generation"),
        }
    elif isinstance(transfer, dict):
        resource["artifact_delivery_report"] = {
            "status": transfer.get("status", "attempted"),
            "verified": False,
        }
    if job.artifact_id is None:
        return resource

    artifact = await session.get(Artifact, job.artifact_id)
    resource["artifact_id"] = str(job.artifact_id)
    if artifact is not None and artifact.storage_uri:
        resource["storage_uri"] = artifact.storage_uri
    return resource


async def _load_release_echo_for_criterion(
    session: AsyncSession, task: Task, logs: str | None
) -> tuple[str | None, str | None]:
    """Return (echo_sha, expected_sha) from the task's compute job for release-echo criteria.

    Tries the job_id from the COMPUTE_RESULT logs first; falls back to a task-level
    query for terminal jobs when the command emitted no artifact (e.g. TIMED_OUT
    before writing anything). Returns (None, None) when no job can be found.
    """
    job: ComputeJob | None = None
    job_id = _extract_compute_job_id(logs or "")
    if job_id is not None:
        job = await get_compute_job_for_task(session, job_id=job_id, task_id=task.id)
    if job is None:
        result = await session.execute(
            select(ComputeJob)
            .where(ComputeJob.task_id == task.id)
            .where(
                ComputeJob.status.in_(
                    [
                        ComputeJobStatus.SUCCEEDED,
                        ComputeJobStatus.FAILED,
                        ComputeJobStatus.CANCELLED,
                        ComputeJobStatus.TIMED_OUT,
                    ]
                )
            )
            .order_by(ComputeJob.created_at.desc())
        )
        job = result.scalars().first()
    if job is None or not isinstance(job.polling_metadata, dict):
        return None, None
    echo_sha: str | None = job.polling_metadata.get("release_echo_sha") or None
    evidence = job.polling_metadata.get("live_release_evidence")
    expected_sha: str | None = (
        evidence.get("promoted_sha") or None if isinstance(evidence, dict) else None
    )
    return echo_sha, expected_sha


def _summarize_progress_records(
    progress: Any, criterion: dict | None
) -> dict[str, Any] | None:
    """Compact trajectory summary from a job's ``polling_metadata["progress"]``.

    Returns ``{metric, count, first, last, best, best_direction}`` (each record
    compacted to ``{value, step?, ts?}``) or None when no usable records exist.
    The summary is scoped to the metric of the LAST record (mixed-metric files
    summarize the most recent series). ``best`` direction comes from the
    declared success-criterion operator when it names the same metric
    (``<``/``<=`` ⇒ minimize); otherwise it defaults to maximize and says so
    via ``best_direction`` — a hint, not a verdict. Records were validated at
    the report endpoint but are re-checked here (fail-soft) since
    polling_metadata is plain JSONB.
    """
    if not isinstance(progress, dict):
        return None
    records = progress.get("records")
    if not isinstance(records, list):
        return None

    def _valid(rec: Any) -> bool:
        if not isinstance(rec, dict):
            return False
        value = rec.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if value != value or value in (float("inf"), float("-inf")):
            return False
        return isinstance(rec.get("metric"), str) and bool(rec.get("metric"))

    valid = [rec for rec in records if _valid(rec)]
    if not valid:
        return None
    metric = valid[-1]["metric"]
    series = [rec for rec in valid if rec.get("metric") == metric]

    minimize = False
    if isinstance(criterion, dict) and criterion.get("metric") == metric:
        minimize = criterion.get("operator") in ("<", "<=")
    best = (
        min(series, key=lambda rec: rec["value"])
        if minimize
        else max(series, key=lambda rec: rec["value"])
    )

    def _compact(rec: dict) -> dict[str, Any]:
        out: dict[str, Any] = {"value": rec["value"]}
        if isinstance(rec.get("step"), int) and not isinstance(rec.get("step"), bool):
            out["step"] = rec["step"]
        if isinstance(rec.get("ts"), str) and rec.get("ts"):
            out["ts"] = rec["ts"]
        return out

    raw_count = progress.get("count")
    count = (
        raw_count
        if isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and raw_count >= len(valid)
        else len(valid)
    )
    return {
        "metric": metric,
        "count": count,
        "first": _compact(series[0]),
        "last": _compact(series[-1]),
        "best": _compact(best),
        "best_direction": "min" if minimize else "max",
    }


async def _build_compute_progress_summary(
    session: AsyncSession, task: Task, logs: str, criterion: dict | None
) -> dict[str, Any] | None:
    """Load the task's compute job and summarize its interim-progress trail.

    Gives follow-on agents the trained trajectory ("loss plateaued at 2.31
    after step 1400"), not just the terminal state. Fail-soft by contract: no
    job, no progress records (old runner / GCP backend / job that never wrote
    the file), or any load error returns None and the compute_result simply
    omits ``progress_summary``.
    """
    try:
        job: ComputeJob | None = None
        job_id = _extract_compute_job_id(logs or "")
        if job_id is not None:
            job = await get_compute_job_for_task(
                session, job_id=job_id, task_id=task.id
            )
        if job is None:
            result = await session.execute(
                select(ComputeJob)
                .where(ComputeJob.task_id == task.id)
                .order_by(ComputeJob.created_at.desc())
            )
            job = result.scalars().first()
        if job is None or not isinstance(job.polling_metadata, dict):
            return None
        return _summarize_progress_records(
            job.polling_metadata.get("progress"), criterion
        )
    except Exception:  # noqa: BLE001 — fail-soft: never block terminal completion
        logger.warning(
            "Progress-summary extraction failed for task %s", task.id, exc_info=True
        )
        return None


def _derive_artifact_delivery_record(resource: dict) -> dict:
    """Derive artifact delivery classification from a stamped compute resource dict.

    Returns a dict with:
      delivered: True iff a durable-class URI is present in storage_uri or result_reference
      durability: 'durable' | 'transient' | 'none'
      artifact_id: str | None
      result_reference: str | None
    """
    storage_uri = resource.get("storage_uri")
    result_reference = resource.get("result_reference")
    artifact_id = resource.get("artifact_id")
    # Both storage_uri and result_reference must be validated against the durable-URI
    # predicate: Artifact.storage_uri can be https:// (agent-supplied signed URLs) and
    # is not guaranteed durable.
    if (storage_uri and _is_durable_artifact_uri(storage_uri)) or (
        result_reference and _is_durable_artifact_uri(result_reference)
    ):
        durability = "durable"
        delivered = True
    elif result_reference:
        durability = "transient"
        delivered = False
    else:
        durability = "none"
        delivered = False
    record: dict = {"delivered": delivered, "durability": durability}
    if artifact_id is not None:
        record["artifact_id"] = artifact_id
    if result_reference is not None:
        record["result_reference"] = result_reference
    report = resource.get("artifact_delivery_report")
    if isinstance(report, dict):
        record["transfer_status"] = report.get("status")
        record["verified"] = report.get("verified") is True
        for key in ("size", "crc32c", "generation"):
            if report.get(key) is not None:
                record[key] = report[key]
    return record


def build_blueprint_pr_footer(
    task: Task,
    *,
    iteration_count: Optional[int] = None,
    direction_title: Optional[str] = None,
) -> str:
    """Build a badge-style footer string for a Blueprint PR body.

    Omits any field whose data is absent so callers need not pre-filter.
    Uses BLUEPRINT_PR_META_MARKER so the block can be replaced idempotently.
    """
    ctx = task.context or {}
    parts: list[str] = []
    review_loop = ctx.get("review_loop") or {}
    if iteration_count is None and isinstance(review_loop.get("iteration"), int):
        iteration_count = int(review_loop["iteration"])
    if not direction_title and isinstance(ctx.get("direction_title"), str):
        direction_title = ctx.get("direction_title")

    cost = _estimated_task_cost_usd(task)
    if cost is not None:
        parts.append(f"${cost:.2f}")

    if iteration_count and iteration_count > 1:
        parts.append(f"{iteration_count} iterations")

    direction_id = ctx.get("direction_id")
    if direction_id:
        label = (
            f"{direction_title[:60]} ({direction_id})"
            if direction_title
            else str(direction_id)
        )
        parts.append(f"direction: {label}")

    if not parts:
        return ""
    return f"{BLUEPRINT_PR_META_MARKER}\n*{' · '.join(parts)}*"


async def patch_pr_body_with_blueprint_meta(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    footer: str,
) -> None:
    """Best-effort: append or replace the blueprint:meta footer in a GitHub PR body."""
    import httpx  # noqa: PLC0415

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        if not resp.is_success:
            return
        current = resp.json().get("body") or ""
        idx = current.find(BLUEPRINT_PR_META_MARKER)
        updated = (current[:idx] + footer) if idx >= 0 else f"{current}\n\n{footer}"
        await client.patch(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={**headers, "Content-Type": "application/json"},
            json={"body": updated},
        )


def _compute_first_attempt_succeeded(
    attempt_count: int,
    status: "TaskStatus",
    merge_verification: "str | None" = None,
) -> bool:
    """Return True if the task completed successfully on its first attempt.

    A degraded-unverified merge (environment/setup failure blocked real
    verification) does NOT count as a first-attempt success even if
    attempt_count <= 1 and status == COMPLETED — the claim was never proven on
    HEAD.  Pass ``merge_verification`` as the
    :func:`~app.landed_signal.merge_verification_status` result at write time
    so the flag is accurate when the merge context is already available.
    """
    if merge_verification == "unverified_degraded":
        return False
    return attempt_count <= 1 and status == TaskStatus.COMPLETED


def _stamp_orphan_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    direction_id: str,
) -> None:
    """Record an orphan remediation outcome on the task itself when the linked
    direction no longer exists. Writes to ``task.context['orphan_remediation_outcome']``
    so diagnosis/operator tooling can still locate the result."""
    ctx = dict(task.context or {})
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
    outcome: Dict[str, Any] = {
        "result": "merged" if merged else "failed",
        "timestamp": _utcnow().isoformat(),
        "missing_direction_id": direction_id,
    }
    if fingerprint:
        outcome["fingerprint"] = str(fingerprint)
    if not merged:
        from app.failure_classification import classify_failure  # noqa: PLC0415

        outcome["failure_class"] = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )
        outcome["error_summary"] = (task.latest_status_message or "")[:200]
    ctx["orphan_remediation_outcome"] = outcome
    task.context = ctx
    flag_modified(task, "context")


def _stamp_remediation_spawn_on_direction(
    direction: Dict[str, Any], task: Task
) -> None:
    """Record that a remediation task is in flight without declaring its outcome."""
    ctx = task.context or {}
    direction["last_remediation_at"] = _utcnow().isoformat()
    direction["last_remediation_task_id"] = str(task.id)
    direction["remediation_task_id"] = str(task.id)
    diagnosis_task_id = ctx.get("diagnosis_task_id")
    if diagnosis_task_id:
        direction["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
    fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
    if fingerprint:
        direction["last_remediation_fingerprint"] = str(fingerprint)


def _phase_runner_affinity(task: Task) -> str | None:
    """Return the local-runner id a completed phase should prefer next."""
    ctx = task.context or {}
    runner_target_id = ctx.get("runner_target_id")
    if isinstance(runner_target_id, str) and runner_target_id.strip():
        return runner_target_id.strip()
    vm_instance_id = task.vm_instance_id
    if isinstance(vm_instance_id, str) and vm_instance_id.startswith("runner:"):
        runner_id = vm_instance_id.removeprefix("runner:").strip()
        return runner_id or None
    preferred_runner_target_id = ctx.get("preferred_runner_target_id")
    if (
        isinstance(preferred_runner_target_id, str)
        and preferred_runner_target_id.strip()
    ):
        return preferred_runner_target_id.strip()
    return None


def _phase_local_runner_context(task: Task) -> dict[str, Any]:
    """Carry hard local Strategic intent from architecture phase to review."""

    ctx = task.context or {}
    if not ctx.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY):
        return {}
    reason = ctx.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
    return {
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: (
            reason if isinstance(reason, str) and reason else "workbench_continuous"
        ),
    }


# Cap on the per-direction remediation-outcome history list length.
_REMEDIATION_HISTORY_MAX = 5


async def _record_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    session: AsyncSession,
) -> bool:
    """Record remediation outcome on the direction the remediation targeted.

    A remediation task is spawned by the autonomous diagnosis pipeline with
    `diagnosis_task_id` in its context. When such a task reaches a terminal
    outcome (merged or failed), stamp the linked direction so effectiveness
    monitoring and escalation logic (direction_lifecycle.py, anomaly_detection.py)
    can observe attempts, last result, timestamp, and the diagnosis fingerprint.

    Returns True if direction state was updated, False otherwise.
    """
    ctx = task.context or {}
    diagnosis_task_id = ctx.get("diagnosis_task_id")
    direction_id = ctx.get("direction_id")
    # diagnosis_task_id is optional — CI auto-remediation fixes may target a
    # direction (by fingerprint match) without a prior investigation linkage.
    # direction_id alone is sufficient to stamp the outcome.
    if not direction_id:
        return False
    # Serialize the read-modify-write of project.directions through the
    # canonical row lock: the mutate_fn re-locates the target direction inside
    # the lock against the freshly-loaded list and applies all mutations there,
    # so a concurrent same-project completion mutating a *different* direction is
    # not clobbered. Sibling propagation also runs inside the lock against the
    # fresh list (``find_directions_sharing_fingerprint`` only reads
    # ``project.directions``, so a lightweight shim suffices).
    recorded = {"v": False}

    def _mutate(directions: List[Dict[str, Any]]):
        target = next((d for d in directions if d.get("id") == direction_id), None)
        if target is None:
            # Direction was retired or removed before the remediation task
            # reached a terminal state. Stamp the outcome on the task itself so
            # the fix outcome is not silently discarded — operators and the
            # diagnosis pipeline can still find what happened via task.context.
            _stamp_orphan_remediation_outcome(
                task,
                merged=merged,
                direction_id=str(direction_id),
            )
            return None
        # Dedup: the rollup merge flow calls this function twice for the same
        # task (intermediate merge + rollup landing). Skip any repeat recording
        # for the same task_id to prevent inflated attempt counts and duplicate
        # history entries.
        task_id_str = str(task.id)
        if any(
            e.get("task_id") == task_id_str
            for e in (target.get("remediation_history") or [])
        ):
            recorded["v"] = True
            return None
        proj_shim = SimpleNamespace(directions=directions)
        fingerprint = ctx.get("diagnosis_fingerprint") or ctx.get("error_fingerprint")
        prior_fingerprint = target.get("last_remediation_fingerprint")
        prior_attempts = int(target.get("remediation_attempts") or 0)
        if (
            fingerprint
            and prior_fingerprint
            and str(fingerprint) != str(prior_fingerprint)
        ):
            target["remediation_attempts"] = 1
        else:
            target["remediation_attempts"] = prior_attempts + 1
        now_iso = _utcnow().isoformat()
        target["last_remediation_result"] = "merged" if merged else "failed"
        target["last_remediation_at"] = now_iso
        if diagnosis_task_id:
            target["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
        target["last_remediation_task_id"] = str(task.id)
        if fingerprint:
            target["last_remediation_fingerprint"] = str(fingerprint)
        history_entry: Dict[str, Any] = {
            "task_id": str(task.id),
            "result": "merged" if merged else "failed",
            "timestamp": now_iso,
            "prompt_snippet": ((task.first_payload or {}).get("prompt", "") or "")[
                :200
            ],
        }
        if fingerprint:
            history_entry["fingerprint"] = str(fingerprint)
        trigger_failure_class = ctx.get("trigger_failure_class")
        if trigger_failure_class:
            history_entry["trigger_failure_class"] = trigger_failure_class
        ci_failure_type = ctx.get("ci_failure_type")
        if ci_failure_type:
            history_entry["ci_failure_type"] = ci_failure_type
        if merged:
            target.pop("last_remediation_failure_class", None)
            target.pop("last_remediation_error_summary", None)
            if fingerprint:
                try:
                    _propagate_remediation_success_to_siblings(
                        project=proj_shim,
                        directions=directions,
                        fingerprint=str(fingerprint),
                        successful_direction_id=str(direction_id),
                        task=task,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[DIAGNOSIS] Failed to propagate remediation success to "
                        f"siblings for direction {direction_id}: {exc!r}"
                    )
        else:
            from app.failure_classification import classify_failure  # noqa: PLC0415

            target["last_remediation_failure_class"] = classify_failure(
                task, at_time=task.last_heartbeat or task.started_at
            )
            target["last_remediation_error_summary"] = (
                task.latest_status_message or ""
            )[:200]
            history_entry["failure_class"] = target["last_remediation_failure_class"]
            history_entry["error_summary"] = target["last_remediation_error_summary"]
            if fingerprint:
                try:
                    _propagate_remediation_failure_to_siblings(
                        project=proj_shim,
                        directions=directions,
                        fingerprint=str(fingerprint),
                        failed_direction_id=str(direction_id),
                        task=task,
                        failure_class=target["last_remediation_failure_class"],
                        error_summary=target["last_remediation_error_summary"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[DIAGNOSIS] Failed to propagate remediation failure to "
                        f"siblings for direction {direction_id}: {exc!r}"
                    )
        history_list = list(target.get("remediation_history") or [])
        history_list.append(history_entry)
        target["remediation_history"] = history_list[-_REMEDIATION_HISTORY_MAX:]
        recorded["v"] = True
        return None

    # expire_on_commit=False leaves a stale directions snapshot in the identity
    # map; drop it so with_directions_locked re-reads the row fresh under the
    # lock (see _persist_direction_field_update docstring).
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["directions"])
    await with_directions_locked(session, task.project_id, _mutate)
    return recorded["v"]


def _propagate_remediation_success_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    successful_direction_id: Optional[str],
    task: Task,
) -> None:
    """Un-pause sibling directions sharing ``fingerprint`` and stamp prior-art context.

    When a remediation merges, other auto-paused directions holding on the same
    exact error fingerprint almost certainly share the root cause that was just
    fixed. Rather than making each one wait out its own 48h cooldown and run an
    independent diagnosis, clear their pause state, stamp the successful task as
    prior art (so the diagnosis pipeline can reference what worked if they fail
    again), and append an audit note. The match relies on
    ``find_directions_sharing_fingerprint`` which enforces exact equality plus
    auto-paused + recency filters, guarding against false positives.

    Mutates ``directions`` in place; the caller persists via ``flag_modified``.
    """
    from app.anomaly_detection import find_directions_sharing_fingerprint  # noqa: PLC0415

    siblings = find_directions_sharing_fingerprint(
        project,
        fingerprint,
        exclude_direction_id=successful_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    if successful_direction_id:
        note_text = (
            f"Sibling remediation merged ({now_iso[:10]}) for direction "
            f"{successful_direction_id} sharing the same error fingerprint. "
            f"Task: {task.id}."
        )
    else:
        note_text = (
            f"Unlinked CI remediation merged ({now_iso[:10]}) sharing the "
            f"same error fingerprint. Task: {task.id}."
        )
    if prompt_snippet:
        note_text += f" Approach: {prompt_snippet}"
    note_text = note_text[:STRATEGIC_RATIONALE_MAX]
    sibling_ids = {s.get("id") for s in siblings if s.get("id")}
    diagnosis_task_id = (task.context or {}).get("diagnosis_task_id")
    for direction in directions:
        if direction.get("id") not in sibling_ids:
            continue
        # Clear the auto-pause so the direction can resume on the next lifecycle
        # sweep without waiting out its own cooldown.
        _prior_status = direction.get("status")
        direction["status"] = "active"
        for field in (
            "auto_paused",
            "paused_reason",
            "paused_at",
            "needs_manual_intervention",
            "escalation_notification_pending",
        ):
            direction.pop(field, None)
        # Grace period: when this resumes a paused direction, stamp the recovery
        # marker consumed by _check_direction_health so the same frozen
        # research_history that drove the pause cannot immediately re-pause or
        # zombie-retire the direction before a new research cycle is appended —
        # matching cooldown auto-recovery and the operator/strategist surfaces.
        if _prior_status == "paused":
            direction["auto_recovered_at"] = now_iso
        # Stamp prior art so autonomous_research_diagnosis can surface what
        # worked if the same fingerprint resurfaces on this direction.
        direction["last_remediation_task_id"] = str(task.id)
        if diagnosis_task_id:
            direction["last_remediation_diagnosis_task_id"] = str(diagnosis_task_id)
        direction["last_remediation_fingerprint"] = str(fingerprint)
        direction["last_remediation_result"] = "merged"
        direction["last_remediation_at"] = now_iso
        if successful_direction_id:
            direction["cross_direction_remediation_source"] = str(
                successful_direction_id
            )
        notes = list(direction.get("notes") or [])
        notes.append(
            {
                "text": note_text,
                "added_at": now_iso,
                "added_by_task": str(task.id),
                "source": "sibling_remediation_success",
            }
        )
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


def _propagate_remediation_failure_to_siblings(
    *,
    project: Project,
    directions: List[Dict[str, Any]],
    fingerprint: str,
    failed_direction_id: Optional[str],
    task: Task,
    failure_class: Optional[str],
    error_summary: Optional[str],
) -> None:
    """Inject a 'do not repeat' note into sibling directions sharing ``fingerprint``.

    When a remediation fails, other directions holding on the same error
    fingerprint should not re-attempt the same approach. Surface the failed
    prompt, failure class, and error summary so sibling investigations can
    propose a different fix.

    Mutates ``directions`` in place; the caller persists via ``flag_modified``.
    """
    from app.anomaly_detection import find_directions_sharing_fingerprint  # noqa: PLC0415

    siblings = find_directions_sharing_fingerprint(
        project,
        fingerprint,
        exclude_direction_id=failed_direction_id,
    )
    if not siblings:
        return
    prompt_snippet = (task.first_payload.get("prompt") or "").strip()[:200]
    now_iso = _utcnow().isoformat()
    if failed_direction_id:
        note_text = (
            f"Sibling remediation FAILED ({now_iso[:10]}) for direction "
            f"{failed_direction_id} sharing the same error fingerprint. "
            f"Task: {task.id}. Do not repeat this approach."
        )
    else:
        note_text = (
            f"Unlinked CI remediation FAILED ({now_iso[:10]}) sharing the "
            f"same error fingerprint. Task: {task.id}. Do not repeat this approach."
        )
    if failure_class:
        note_text += f" Failure: {failure_class}."
    if error_summary:
        note_text += f" Error: {error_summary}"
    if prompt_snippet:
        note_text += f" Failed approach: {prompt_snippet}"
    note_text = note_text[:STRATEGIC_RATIONALE_MAX]
    sibling_ids = {s.get("id") for s in siblings if s.get("id")}
    for direction in directions:
        if direction.get("id") not in sibling_ids:
            continue
        notes = list(direction.get("notes") or [])
        notes.append(
            {
                "text": note_text,
                "added_at": now_iso,
                "added_by_task": str(task.id),
                "source": "sibling_remediation_failure",
            }
        )
        direction["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]


_PROJECT_REMEDIATION_HISTORY_MAX = 20


async def _record_project_remediation_outcome(
    task: Task,
    *,
    merged: bool,
    session: AsyncSession,
) -> bool:
    """Record a CI auto-remediation outcome at the project level when no
    direction_id is linked, so unlinked-fix effectiveness is still trackable
    via Project.remediation_tracking."""
    project = await session.get(Project, task.project_id)
    if not project:
        return False
    ctx = task.context or {}
    # Audit W1: previous_error_fingerprint is now a column.
    fingerprint = (
        ctx.get("diagnosis_fingerprint")
        or ctx.get("error_fingerprint")
        or task.previous_error_fingerprint
    )
    state = dict(project.remediation_tracking or {})
    now_iso = _utcnow().isoformat()
    entry: Dict[str, Any] = {
        "task_id": str(task.id),
        "result": "merged" if merged else "failed",
        "timestamp": now_iso,
    }
    if fingerprint:
        entry["fingerprint"] = str(fingerprint)
    failure_class: Optional[str] = None
    error_summary: Optional[str] = None
    if not merged:
        from app.failure_classification import classify_failure  # noqa: PLC0415

        failure_class = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )
        error_summary = (task.latest_status_message or "")[:200]
        entry["failure_class"] = failure_class
        entry["error_summary"] = error_summary
    history = list(state.get("history") or [])
    # Dedup: skip if this task_id was already recorded in the project history.
    task_id_str = str(task.id)
    if any(e.get("task_id") == task_id_str for e in history):
        return True
    history.append(entry)
    state["history"] = history[-_PROJECT_REMEDIATION_HISTORY_MAX:]
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_result"] = entry["result"]
    state["last_at"] = now_iso
    if fingerprint:
        state["last_fingerprint"] = str(fingerprint)
    project.remediation_tracking = state
    flag_modified(project, "remediation_tracking")

    # Fingerprint-based sibling propagation: although this remediation is
    # unlinked (no direction_id), other auto-paused directions may be holding
    # on the same exact error fingerprint. Apply the same sibling notes /
    # un-pause logic as the linked path so unlinked CI fixes don't leave
    # siblings stuck.
    if fingerprint:
        # Serialize the sibling-directions write through the canonical row lock
        # so a concurrent same-project completion mutating a different direction
        # is not clobbered. Drop the cached directions snapshot first
        # (expire_on_commit=False) so the helper reads the row fresh under the
        # lock; the remediation_tracking write above is left pending and flushed
        # within the same transaction. remediation_tracking itself has no
        # canonical helper and is left as a plain write (out of scope here).
        session.expire(project, ["directions"])

        def _propagate(directions: List[Dict[str, Any]]):
            proj_shim = SimpleNamespace(directions=directions)
            if merged:
                _propagate_remediation_success_to_siblings(
                    project=proj_shim,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    successful_direction_id=None,
                    task=task,
                )
            else:
                _propagate_remediation_failure_to_siblings(
                    project=proj_shim,
                    directions=directions,
                    fingerprint=str(fingerprint),
                    failed_direction_id=None,
                    task=task,
                    failure_class=failure_class,
                    error_summary=error_summary,
                )
            return None

        try:
            await with_directions_locked(session, task.project_id, _propagate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[DIAGNOSIS] Failed to propagate unlinked remediation outcome "
                f"to siblings for project {project.id}: {exc!r}"
            )
    session.add(project)
    return True


_MIGRATION_FINALIZATION_CONTEXT_KEY = "_migration_finalization"


def _migration_finalized_revision_from_context(task: Task | None) -> Optional[str]:
    if task is None:
        return None
    finalization = (task.context or {}).get(_MIGRATION_FINALIZATION_CONTEXT_KEY)
    if not isinstance(finalization, dict):
        return None
    revision = finalization.get("finalized_revision")
    if not isinstance(revision, str):
        return None
    revision = revision.strip()
    return revision or None


def _persist_migration_finalization_for_downstream(
    task: Task, source_task: Task | None
) -> bool:
    """Carry auto-finalization rewrite metadata onto the reservation-owning task.

    The review/CI auto-finalization paths store the rewritten Alembic revision
    under ``_migration_finalization`` on the merging CI/review task's context
    (``source_task``), not on the reservation-owning task. When a non-terminal
    (e.g. staged-rollup) merge keeps the reservation ACTIVE, that metadata must
    be copied onto ``task`` so the eventual downstream terminal landing — whose
    own ``source_task`` is a *different* CI task that never saw the rewrite — can
    still record the correct ``finalized_revision`` instead of silently falling
    back to ``reservation.provisional_revision`` for a renumbered migration.

    Returns True when metadata was persisted onto ``task``.
    """
    if source_task is None or source_task is task:
        return False
    finalization = (source_task.context or {}).get(_MIGRATION_FINALIZATION_CONTEXT_KEY)
    if not isinstance(finalization, dict):
        return False
    existing = (task.context or {}).get(_MIGRATION_FINALIZATION_CONTEXT_KEY)
    if isinstance(existing, dict) and existing.get("finalized_revision"):
        # Owning task already carries a rewrite — don't clobber it.
        return False
    task.context = {
        **(task.context or {}),
        _MIGRATION_FINALIZATION_CONTEXT_KEY: finalization,
    }
    flag_modified(task, "context")
    return True


def _reset_review_loop_pr_head_cache(review_loop: dict | None) -> dict:
    """Force the next CI sweep to refetch the PR head after a branch rewrite."""

    refreshed = dict(review_loop or {})
    refreshed["head_sha"] = ""
    refreshed["polls_since_pr_fetch"] = 999
    for key in ("mergeable_state", "last_merge_msg", "_behind_update_retries"):
        refreshed.pop(key, None)
    return refreshed


async def _finalize_migration_reservation_after_merge(
    task: Task,
    session: AsyncSession,
    *,
    source_task: Task | None = None,
    terminal_merge: bool = True,
) -> None:
    """Finalize the task's ACTIVE migration reservation after a terminal PR merge."""
    from app.migration_reservations import (  # noqa: PLC0415
        finalize_migration_reservation,
        get_migration_reservation_for_task,
    )

    if not terminal_merge:
        preserved = _persist_migration_finalization_for_downstream(task, source_task)
        if preserved:
            session.add(task)
        logger.info(
            "[MIGRATION] keeping reservation ACTIVE for task %s after non-terminal "
            "merge; awaiting downstream landing%s",
            task.id,
            " (preserved rewrite metadata for downstream landing)" if preserved else "",
        )
        return

    finalized_revision = _migration_finalized_revision_from_context(source_task)
    if finalized_revision is None:
        finalized_revision = _migration_finalized_revision_from_context(task)
    if finalized_revision is None:
        reservation = await get_migration_reservation_for_task(session, task_id=task.id)
        if reservation is None:
            return
        finalized_revision = reservation.provisional_revision
    finalized = await finalize_migration_reservation(
        session,
        task_id=task.id,
        finalized_revision=finalized_revision,
    )
    if finalized is not None:
        logger.info(
            "[MIGRATION] finalized reservation for task %s at revision %s",
            task.id,
            finalized_revision,
        )


async def _expire_migration_reservation_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Expire an ACTIVE reservation once its owning task terminally fails."""
    if status not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED):
        return
    from app.migration_reservations import expire_migration_reservation  # noqa: PLC0415

    expired = await expire_migration_reservation(session, task_id=task.id)
    if expired is not None:
        logger.info(
            "[MIGRATION] expired reservation for terminal task %s (%s)",
            task.id,
            getattr(status, "value", status),
        )


async def _release_pool_member_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> Optional[str]:
    """Return or retire the warm-pool member that served a now-terminal task.

    A leased :class:`~app.models.PoolVm` row stays ``LEASED`` — counting against
    the per-project pool target and the fleet budget (blocking replenishment) —
    until the reaper's orphan-lease sweep eventually reclaims and DELETEs it,
    making every member single-use. Reconcile the registry promptly instead,
    matching what the pool agent does next:

      * ``COMPLETED`` — the agent loops back to ``/next-role``
        (``bootstrapPoolMode`` -> ``pollForNextRole``), so return the member to
        IDLE via :func:`~app.warm_pool.release_pool_vm` (a fresh idle window
        starts; the idle-TTL reaper / stale-image recycling / cost guard still
        decide when it is ultimately retired).
      * ``FAILED`` / ``CANCELLED`` — the agent re-throws and exits without
        polling, so it cannot be reused: mark it ``RETIRING`` via
        :func:`~app.warm_pool.mark_pool_vm_retiring`.

    No-op (returns ``None``) for non-terminal states, when the pool is disabled,
    or when no LEASED member is bound to this task (the common cold-provisioned
    case + idempotency guard). Returns the outcome label when it acted; the
    caller owns the commit.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return None
    # Deferred imports: keep the module-level surface stable and avoid importing
    # the warm-pool registry on the hot completion path when the pool is dark.
    from app.models import PoolVmState  # noqa: PLC0415
    from app.warm_pool import (  # noqa: PLC0415
        get_pool_vm,
        is_warm_pool_enabled,
        mark_pool_vm_retiring,
        release_pool_vm,
    )

    if not is_warm_pool_enabled():
        return None
    vm_id = task.vm_instance_id
    if not vm_id:
        return None
    member = await get_pool_vm(session, vm_id)
    if member is None or member.state != PoolVmState.LEASED:
        # Not a pool member, or already reconciled / retiring — nothing to do.
        return None
    # Only reclaim the member actually bound to THIS task: a vm_instance_id
    # reused across tasks must not let one task's terminal state pull back a
    # member leased to a different task.
    if member.leased_task_id != task.id:
        return None
    if status == TaskStatus.COMPLETED:
        await release_pool_vm(session, vm_id, owning_task_id=task.id)
        outcome = "idle"
    else:
        await mark_pool_vm_retiring(session, vm_id, source_states=(PoolVmState.LEASED,))
        outcome = "retiring"
    logger.info(
        "[WARM-POOL] task %s terminal (%s) — pool member %s -> %s",
        task.id,
        getattr(status, "value", status),
        vm_id,
        outcome,
    )
    return outcome


async def _record_remediation_if_terminal(
    task: Task,
    status: "TaskStatus",
    session: AsyncSession,
) -> None:
    """Stamp remediation outcome on the linked direction when a diagnosis-spawned task terminates.

    Records the merged/failed outcome on the targeted direction (and falls back
    to project-level tracking when no direction linkage survives) so the
    direction-result ladder sees terminal remediation outcomes.
    """
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return
    # Reconcile the warm-pool member that served this task BEFORE the
    # remediation-specific early returns below, so every terminal task — not
    # just diagnosis-linked ones — promptly returns/retires its pool member
    # instead of stranding it LEASED against the pool budget. Best-effort: a
    # failure here must not block remediation recording (the orphan-lease reaper
    # remains the backstop).
    try:
        await _release_pool_member_if_terminal(task, status, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[WARM-POOL] Failed to reconcile pool member for terminal task {task.id}: {exc!r}"
        )
    ctx = task.context or {}
    has_direction_linkage = bool(
        ctx.get("diagnosis_task_id") and ctx.get("direction_id")
    )
    # Audit W1: auto_remediation is now a column.
    is_auto_remediation = bool(getattr(task, "auto_remediation", False))
    if not has_direction_linkage and not is_auto_remediation:
        return
    # Tasks that produce a PR enter the review pipeline — their true outcome
    # (merged vs. rejected) is only known after the review task completes or the
    # pipeline exhausts its iteration cap. Recording on the PR-creating task's
    # COMPLETED would prematurely stamp merged=True before review/CI has run.
    # FAILED/CANCELLED still record immediately since no viable PR exists.
    # Every write task type opens a PR and thus enters the review pipeline; the
    # `review` task itself also operates on that PR. Derive from the canonical
    # registry so a newly-added write type is honored automatically — `review` is
    # the only non-write member and is listed explicitly. See
    # app/task_capabilities.py:WRITE_TASK_TYPES.
    _REVIEW_PIPELINE_TYPES = WRITE_TASK_TYPES | {"review"}
    if status == TaskStatus.COMPLETED:
        task_type = (task.first_payload or {}).get("type")
        if task_type in _REVIEW_PIPELINE_TYPES:
            return
    try:
        recorded = await _record_remediation_outcome(
            task,
            merged=(status == TaskStatus.COMPLETED),
            session=session,
        )
        if not recorded:
            # Fall back to project-level tracking whenever the direction lookup
            # failed (either no direction_id at all on an auto_remediation, or
            # the linked direction was retired before terminal state). This
            # guarantees remediation outcomes are never silently discarded.
            had_direction_linkage = bool(ctx.get("direction_id"))
            if is_auto_remediation or had_direction_linkage:
                await _record_project_remediation_outcome(
                    task,
                    merged=(status == TaskStatus.COMPLETED),
                    session=session,
                )
    except Exception as exc:
        logger.warning(
            f"[DIAGNOSIS] Failed to record remediation outcome for task {task.id}: {exc!r}"
        )


# ---------------------------------------------------------------------------
# Parser / Validator helpers
# ---------------------------------------------------------------------------


def _normalize_findings_array(parsed: Any) -> list[dict]:
    """Filter a parsed findings array to dict-only elements, defensive normalization.

    When _extract_json_array returns a bracket-balanced span containing the
    required keys, a malformed-but-parseable JSON like [{"severity":"high"}, "stray"],
    passes isinstance(parsed, list) but contains non-dict elements. Consumers
    throughout the handlers iterate findings and call .get() on each element,
    so a single non-dict entry would raise AttributeError and fail the entire
    task. This helper normalizes at parse time so bad elements are silently
    skipped rather than crashing the handler.

    Mirrors the pattern already in use by task_outcomes._findings_list and
    research_completion.py for defensive filtering.
    """
    if not isinstance(parsed, list):
        return []
    return [f for f in parsed if isinstance(f, dict)]


# Round 4 W1 (audit C3): content snapshot used by ``_ensure_branch`` for
# lock-window-break validation. Heartbeat-driven ``updated_at`` advances
# every 30s and would false-defer under load. Validate against the fields
# the branch-creation window protects.
def _capture_branch_creation_snapshot(task: Task) -> dict:
    """Snapshot the fields the branch-creation lock-window-break protects.

    The window between commit-release and re-acquire is meant to detect
    peer mutation of the post-plan / post-research status flow. The
    relevant fields are:
      - status: must remain EVALUATING (the value the caller advanced to
        before calling _ensure_branch); a peer transition would invalidate
        the branch-creation outcome
      - context.plan_branch / context.research_branch: a peer may have
        already created the staging branch; we must not overwrite
      - auto_remediation: snapshotted at line 1615 and read on the
        failure path at line 1710 (record_task_result). If a peer flips
        this between snapshot and re-acquire, the failure record would
        carry a stale flag (R5 audit MEDIUM-C).
      - requires_approval: read post-window in places that consume the
        relocked task; included so peer flips trip the validator.
    Heartbeat-only updates don't touch any of these.
    """
    ctx = task.context or {}
    return {
        "status": task.status,
        "plan_branch": ctx.get("plan_branch"),
        "research_branch": ctx.get("research_branch"),
        "auto_remediation": task.auto_remediation,
        "requires_approval": task.requires_approval,
    }


class _StaleBranchError(RuntimeError):
    """The branch we must branch FROM, or the branch we would adopt, is unverified.

    Raised inside ``_ensure_branch``'s attempt loop so the attempt is retried
    with backoff. Two cases:

    * a pre-existing branch sits at a stale tip we could not reset, or
    * the source tip itself could not be resolved (null anchor).

    Distinct from a generic failure because the "does the branch exist?"
    verify-fallback must NOT run for either — existence is precisely what is not
    sufficient here.
    """


async def _reconcile_reused_branch(
    gh_token: str,
    owner: str,
    repo: str,
    branch_name: str,
    existing_sha: str | None,
    source_sha: str | None,
    label: str,
) -> bool:
    """Decide whether a PRE-EXISTING ``branch_name`` may be adopted as the base.

    Returns True when the branch is safe to use, False when it is a stale tip that
    could not be repaired (the caller retries / eventually fails the task).

    ``create_branch`` reports reuse (``reused=True``) on GitHub's 422; with
    ``sha_matches=False`` the ref points somewhere other than the tip we asked for.
    What to do then turns ENTIRELY on the RAW compare status of
    ``source_sha...existing_sha`` — see :func:`app.github_refs.compare_ref_status`
    for why a boolean ancestry test is NOT sufficient (it collapses ``"behind"``
    and ``"diverged"``, which have OPPOSITE answers here).

    DESTROY only when we have positively established there is nothing to destroy:

    * ``"behind"`` — existing branch has NO commits of its own (crashed-prior-run
      leftover cut from an older base). Reset is a FAST-FORWARD; nothing is lost.
      This is the ONLY delete + recreate case — the same repair
      ``release_promotion.promote()`` performs for a stale release branch.
    * everything else (``identical`` / ``ahead`` / ``diverged`` / ``None``) — ADOPT.
      ``"diverged"`` in particular is the ROUTINE state of an inherited staging
      branch: ``plan_completion`` / ``research_completion`` hand us the head branch
      of an OPEN rollup PR with CHILDREN ALREADY MERGED INTO IT, while ``parent_base``
      (automain) keeps advancing underneath. Deleting it would close the PR and
      destroy the merged child commits. ``None`` means a transient API error must
      never be able to destroy a ref.
    """
    if not existing_sha:
        return False
    if not source_sha:
        # We could not resolve the tip we were supposed to branch FROM. Adopting
        # here would re-open the stale-tip bug on an unverified read (we have no
        # idea whether the existing branch is current), and deleting would be
        # destroying a ref we cannot justify destroying. Neither — signal failure
        # so the caller RETRIES with a fresh source-sha read.
        logger.warning(
            f"[{label}] Branch {branch_name} exists at {existing_sha[:8]} but the "
            f"SOURCE tip could not be resolved — refusing to adopt on an unverified "
            f"read; retrying"
        )
        return False
    if existing_sha == source_sha:
        # Already exactly where we want it.
        return True

    compare_status = await compare_ref_status(
        gh_token, owner, repo, source_sha, existing_sha
    )
    if compare_status == "behind":
        logger.warning(
            f"[{label}] Branch {branch_name} exists at {existing_sha[:8]}, which is "
            f"strictly BEHIND source {source_sha[:8]} (no unique commits — stale "
            f"branch from a crashed prior run) — resetting it to the source tip "
            f"(fast-forward; nothing is destroyed)"
        )
    else:
        # identical / ahead / diverged / None — every one of these either already
        # contains the source tip or holds commits of its own that a delete would
        # destroy. Adopt.
        logger.info(
            f"[{label}] Branch {branch_name} exists at {existing_sha[:8]} with compare "
            f"status {compare_status!r} vs source {source_sha[:8]} — ADOPTING. It is "
            f"not strictly behind the source tip, so it either already contains it or "
            f"carries unique commits (e.g. children merged into an inherited staging "
            f"branch that is the head of an open rollup PR). Deleting it here would "
            f"destroy landed work."
        )
        return True

    await delete_branch(gh_token, owner, repo, branch_name)
    recreated = await create_branch(gh_token, owner, repo, branch_name, source_sha)
    recreated = recreated or {}
    if recreated.get("reused") and not recreated.get("sha_matches"):
        logger.error(
            f"[{label}] Branch {branch_name} STILL exists at "
            f"{recreated.get('existing_sha')} after delete+recreate — not adopting"
        )
        return False
    return True


async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str, label: str, session: AsyncSession
) -> Task | None:
    """Create a branch, or verify it already exists.

    Returns:
        - The freshly-locked Task on success (caller continues mutating
          under the new lock).
        - None on failure (task set to FAILED) OR when a peer mutated the
          row during the unlocked window and we deferred. In both cases
          the caller should ``return`` without further mutation.

    Round 3 H5 (lock-window break): callers (_handle_plan_completion,
    _handle_research_completion) run inside _process_post_commit while
    the task is FOR UPDATE-locked. The retry loop below sleeps 10-40s
    (with jitter, up to ~150s combined across 5 attempts) and makes
    GitHub API calls that can hang for tens of seconds during rate-limit.
    Holding the task lock across that window starves concurrent
    heartbeats and false-orphans the row. We mirror the
    rollup_management.py ``_release_parent_lock`` / ``_relock_parent``
    pattern: capture a content snapshot, commit-release before the loop,
    run GitHub calls + sleeps without the lock, then re-acquire with
    snapshot validation.

    Round 4 W1 (audit C1, C2, C3):
      - C1 (cache): ``populate_existing=True`` on the re-SELECT bypasses
        the identity-map cache that previously made the staleness
        validation a no-op under ``expire_on_commit=False``.
      - C2 (success path): the prior implementation returned True on
        success WITHOUT re-acquiring the lock. Callers
        (_handle_plan_completion, _handle_research_completion) then
        mutated ``task.context`` and committed *as if* locked — but
        they were not. Fix: success path now also re-acquires FOR UPDATE
        and returns the fresh Task. Caller swaps its local ``task``
        binding to the returned instance and proceeds under the new
        lock. If re-acquire fails (peer holds), return None and let the
        caller defer (the branch was created, but we couldn't re-lock —
        the next sweep observes the branch artifact and proceeds).
      - C3 (heartbeat): the prior ``updated_at`` validation tripped on
        every overlapping heartbeat. Fix: validate against
        ``_capture_branch_creation_snapshot`` — content fields only.
    """
    # Deferred import to avoid circular dependency
    from app.alert_dispatch import _maybe_create_failure_alert  # noqa: PLC0415

    # Round 4 W1: capture snapshot BEFORE the release-commit so the
    # snapshot reflects the pre-commit content, not the post-commit
    # ``updated_at`` (which the prior implementation needed but the new
    # content-based validator does not). ``parent_id_local`` mirrors the
    # snapshot-stale convention: between release and re-acquire, the
    # in-memory ``task`` is detached and only stable ids + the captured
    # snapshot are safe to read.
    parent_id_local = task.id
    parent_task_type = task.first_payload.get("type", "")
    parent_auto_remediation = task.auto_remediation
    parent_context_snapshot = dict(task.context or {})
    pre_branch_snapshot = _capture_branch_creation_snapshot(task)
    session.add(task)
    await session.commit()  # release task FOR UPDATE lock; SQLA returns
    # the connection to the pool (audit H6).

    max_retries = 5
    gh_token = owner = repo = None
    last_err = None
    branch_created = False
    for _attempt in range(max_retries):
        # The source tip we intend this branch to be cut from. Re-read every
        # attempt (it can advance between retries) and reset per-iteration so the
        # verify-fallback below never compares against a previous attempt's tip.
        sha: str | None = None
        try:
            gh_token, owner, repo = await get_github_token_for_task(
                parent_id_local, session, force_refresh=(_attempt > 0)
            )
            sha = await get_branch_sha(gh_token, owner, repo, source_branch, live=True)
            if not sha:
                # ``get_branch_sha`` is str-or-raise, but a falsy return would be
                # far worse than a raise: ``create_branch`` would anchor on a null
                # sha and, on the 422/reuse path, ``_reconcile_reused_branch`` has
                # no source tip to compare against. RETRY (fresh read, fresh
                # token) — never adopt a branch whose correctness we cannot check.
                raise _StaleBranchError(
                    f"could not resolve the source tip of {source_branch}; "
                    f"refusing to create or adopt {branch_name} on a null anchor"
                )
            ref = await create_branch(gh_token, owner, repo, branch_name, sha) or {}
            # create_branch reports reuse instead of silently adopting: a 422
            # ("already exists") can be our own retried write OR a stale branch
            # left by a crashed prior run, sitting at a completely different SHA.
            if (
                ref.get("reused")
                and not ref.get("sha_matches")
                and not await _reconcile_reused_branch(
                    gh_token,
                    owner,
                    repo,
                    branch_name,
                    ref.get("existing_sha"),
                    sha,
                    label,
                )
            ):
                raise _StaleBranchError(
                    f"branch {branch_name} exists at a divergent tip that could "
                    f"not be reset to {sha[:8] if sha else '?'}"
                )
            logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
            branch_created = True
            break
        except Exception as e:
            last_err = e
            # A _StaleBranchError already PROVED the branch exists and is
            # unusable — re-running the existence check below would only
            # re-adopt it. Fall straight through to backoff/retry.
            if gh_token and not isinstance(e, _StaleBranchError):
                try:
                    # live=True: this read is a decision input for adopt-vs-reset,
                    # so it must observe the current tip, not a <=60s cached one.
                    existing_sha = await get_branch_sha(
                        gh_token, owner, repo, branch_name, live=True
                    )
                except HTTPException as verify_exc:
                    if verify_exc.status_code == 404:
                        # Branch genuinely doesn't exist — count against attempt budget
                        pass
                    else:
                        # Transient server error during verification (e.g. 502) — don't
                        # conclude the branch is missing; continue retrying.
                        logger.warning(
                            f"[{label}] Transient error verifying branch {branch_name} ({verify_exc.status_code}), will retry"
                        )
                except Exception:
                    logger.exception(
                        f"[{label}] Non-HTTP error verifying branch {branch_name}"
                    )
                else:
                    # The branch exists — but "exists" is NOT "usable". Adopt it
                    # only when it is at (or ahead of) the source tip; a divergent
                    # stale tip gets reset first.
                    if await _reconcile_reused_branch(
                        gh_token, owner, repo, branch_name, existing_sha, sha, label
                    ):
                        logger.info(
                            f"[{label}] Branch {branch_name} already exists — reusing"
                        )
                        branch_created = True
                        break
                    logger.warning(
                        f"[{label}] Branch {branch_name} exists but is a stale, "
                        f"divergent tip that could not be reset — retrying"
                    )
            if _attempt < max_retries - 1:
                backoff = (
                    10 * (2**_attempt) * (0.75 + 0.5 * random.random())
                )  # jitter: 10-20s, 20-40s, …
                logger.warning(
                    f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {backoff:.1f}s: {e!r}"
                )
                await asyncio.sleep(backoff)
            else:
                if gh_token:
                    logger.exception(
                        f"[{label}] Branch {branch_name} creation failed after {max_retries} attempts"
                    )
                else:
                    logger.exception(
                        f"[{label}] Cannot create branch — GitHub token unavailable"
                    )

    # Round 4 W1 (C1, C2, C3): re-acquire with snapshot validation on
    # BOTH the success path (C2 — caller will mutate task.context) AND
    # the failure path (mark FAILED). Content snapshot replaces
    # updated_at. To bypass the identity-map cache, ``session.expire``
    # the in-memory instance AND set ``populate_existing=True`` on the
    # SELECT (the canonical reference is now
    # ``rollup_management._relock_parent``, ~line 155 — populate_existing
    # alone is unreliable with ``with_for_update(skip_locked=True)`` on
    # async sessions).
    #
    # Incident 2026-05-04 (b9456e05 / PRs #3606/3607/3612/3613): a
    # one-shot ``skip_locked=True`` silently stranded the parent when
    # the row happened to be locked at the wrong instant — the prior
    # comment claimed "next sweep observes the branch artifact and
    # proceeds" but no such sweep exists. Bounded retry (~9s) makes
    # now-this-instant contention recoverable; on exhaustion we raise
    # so the caller's exception path can persist post_commit_pending
    # for the existing sweep to retry, instead of silently returning
    # None.
    fresh = None
    _max_relock_attempts = 6
    for _relock_attempt in range(_max_relock_attempts):
        session.expire(task)
        fresh_stmt = (
            select(Task)
            .where(Task.id == parent_id_local)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        fresh = (await session.execute(fresh_stmt)).scalars().first()
        if fresh is not None:
            break
        if _relock_attempt < _max_relock_attempts - 1:
            # 1.0, 1.4, 1.8, 2.2, 2.6 → ~9s total before final attempt
            await asyncio.sleep(1.0 + _relock_attempt * 0.4)
    _defer_kind = "post-success context" if branch_created else "failure"
    if fresh is None:
        # All retries exhausted. Word "timeout" in the message routes
        # the caller's transient-error heuristic at task_lifecycle.py
        # (~line 2710) to requeue the task instead of failing it
        # permanently. A fresh agent run regenerates the plan output;
        # post-commit then re-runs and the relock typically succeeds
        # once the contending peer is gone.
        raise RuntimeError(
            f"[{label}] Task {parent_id_local} relock timeout after "
            f"{_max_relock_attempts} retries (~9s) — could not "
            f"persist {_defer_kind} mutation. Branch {branch_name} "
            f"{'was created on GitHub' if branch_created else 'creation also failed'}."
        )
    fresh_snapshot = _capture_branch_creation_snapshot(fresh)
    if fresh_snapshot != pre_branch_snapshot:
        # Genuine peer-content mutation — a different writer modified
        # status/payloads/context during our unlock window. Re-running
        # the agent would just race again. Raise with a non-transient
        # message so the caller marks the task FAILED for operator
        # review rather than silently stranding it.
        raise RuntimeError(
            f"[{label}] Task {parent_id_local} mutated during branch-creation "
            f"window (expected snapshot={pre_branch_snapshot}, found "
            f"{fresh_snapshot}) — could not persist {_defer_kind} mutation. "
            f"Branch {branch_name} "
            f"{'was created on GitHub' if branch_created else 'creation also failed'}. "
            f"Manual investigation required: identify the peer writer."
        )

    if branch_created:
        # C2: success — return the relocked task so caller mutates under
        # the new lock. Caller MUST swap its ``task`` reference to the
        # returned instance.
        return fresh

    # Failure path
    e = last_err
    task = fresh
    task.status = TaskStatus.FAILED
    task.latest_status_message = (
        f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    )
    from app.failure_classification import classify_failure, persist_failure_category  # noqa: PLC0415
    from app.routers.agent_status import record_task_result  # noqa: PLC0415

    _fc = classify_failure(task, at_time=task.last_heartbeat or task.started_at)
    persist_failure_category(task, _fc)
    record_task_result(
        success=False,
        failure_class=_fc,
        task_id=str(task.id),
        task_type=parent_task_type,
        task_context=parent_context_snapshot,
        auto_remediation=parent_auto_remediation,
    )
    session.add(task)
    await _maybe_create_failure_alert(
        task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200]
    )
    await session.commit()
    return None


# ---------------------------------------------------------------------------
# Completion handlers
# ---------------------------------------------------------------------------


async def _maybe_release_safeguard_gate(
    safeguard_task: Task, findings: list | None, session: AsyncSession
) -> None:
    """Release or maintain the cross-cutting merge gate when a safeguard audit terminates.

    No-ops immediately if the task is not a safeguard (no safeguard_for_task_id in context).
    Releases the gate (clears orig.requires_approval) when no critical/high findings exist.
    Called on every TERMINAL safeguard path — COMPLETED (with parsed findings) AND
    FAILED/reaped (findings=None) — so the gate cannot be permanently stranded (cluster C5).

    Re-arm behaviour after clearing the flag:
      - orig EVALUATING (review approved while safeguard was pending): set
        review_loop.ci_waiting=True so the orchestrator's CI loop attempts the merge.
      - orig already COMPLETED (the safeguard outlived orig's lifecycle): clearing
        the flag alone leaves the open PR stranded forever — nothing re-attempts the
        merge. Fire the merge directly here, guarded by an idempotency flag
        (_safeguard_merge_fired) so a re-run / concurrent path cannot double-merge.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    safeguard_for_id = (safeguard_task.context or {}).get("safeguard_for_task_id")
    if not safeguard_for_id:
        return
    has_blocker = bool(
        findings and any(f.get("severity") in ("critical", "high") for f in findings)
    )
    try:
        orig = await session.get(Task, uuid.UUID(safeguard_for_id))
    except Exception:
        orig = None
    if orig is None:
        logger.warning(
            f"[SAFEGUARD] orig {safeguard_for_id[:8]} not found — gate not updated"
        )
        return

    # No challenge lane: critical/high findings conservatively hold the gate.
    if has_blocker:
        logger.info(
            "[SAFEGUARD] Gate maintained on %s — critical/high findings",
            safeguard_for_id[:8],
        )
        return
    orig.requires_approval = False
    pr_num = (safeguard_task.context or {}).get("safeguard_pr_number")
    orig_ctx = dict(orig.context or {})
    _fire_completed_orig_merge = False
    if orig.status == TaskStatus.EVALUATING and pr_num:
        # Review already approved while safeguard was pending; hand off to CI
        # polling loop. arm_ci_wait co-stamps pr_number + a fresh ci_wait_started
        # so this re-arm never parks the identity-less {ci_waiting} shape that the
        # merge poll would skip / that would silently disable the CI-wait timeout.
        from app.ci_merge_helpers import arm_ci_wait  # noqa: PLC0415

        _rearm_loop = dict(orig_ctx.get("review_loop") or {})
        arm_ci_wait(
            _rearm_loop,
            pr_number=pr_num,
            now_iso=_utcnow().isoformat(),
        )
        orig_ctx["review_loop"] = _rearm_loop
        orig.context = orig_ctx
        flag_modified(orig, "context")
    elif (
        orig.status == TaskStatus.COMPLETED
        and pr_num
        and not orig_ctx.get("_safeguard_merge_fired")
    ):
        # orig already terminalized — clearing the flag re-arms nothing, so the
        # open PR would strand forever. Mark the idempotency flag now (persisted
        # with the gate-clear commit) and fire the merge AFTER the commit, below.
        orig_ctx["_safeguard_merge_fired"] = True
        orig.context = orig_ctx
        flag_modified(orig, "context")
        _fire_completed_orig_merge = True
    orig.latest_status_message = (
        f"Cross-cutting safeguard passed — merge gate released (PR #{pr_num})"
    )
    session.add(orig)
    await session.commit()
    logger.info(f"[SAFEGUARD] Gate released on {safeguard_for_id[:8]} for PR #{pr_num}")

    if _fire_completed_orig_merge:
        # Best-effort: fire the held merge for the already-COMPLETED orig. The
        # idempotency flag was committed above, so even if this raises the merge
        # is never re-attempted from here (avoids a double-merge loop); a manual
        # re-arm is then the operator's call. Merge only if the PR is still open.
        from app.github import (  # noqa: PLC0415
            get_pull_request,
            merge_pull_request,
        )

        try:
            gh_token, owner, repo = await get_github_token_for_task(orig.id, session)
            _pr = await get_pull_request(gh_token, owner, repo, int(pr_num))
            if _pr.get("merged"):
                logger.info(
                    f"[SAFEGUARD] PR #{pr_num} already merged — no action (orig "
                    f"{safeguard_for_id[:8]})"
                )
            elif (_pr.get("state") or "open") != "open":
                logger.info(
                    f"[SAFEGUARD] PR #{pr_num} not open (state={_pr.get('state')}) "
                    f"— skipping post-gate merge (orig {safeguard_for_id[:8]})"
                )
            else:
                # Pre-merge Alembic dual-head guard (parity with the CI-completion
                # and inline task-completion merge paths). The held merge can target
                # the integration or promotion branch; without this check a PR that
                # added 'the next' migration while the safeguard ran could land a
                # duplicate revision / dual head. Only guard integration/promotion
                # targets; on a block-or-defer verdict skip the merge and leave the
                # PR open so the guarded retry/resolver flow can handle it.
                from app.shared_state import (  # noqa: PLC0415
                    get_project_promotion_target,
                )

                _sg_project = (
                    await session.get(Project, orig.project_id)
                    if orig.project_id
                    else None
                )
                _sg_base = (_pr.get("base") or {}).get("ref", "")
                _sg_gated = _sg_base in (
                    get_project_integration_branch(_sg_project),
                    get_project_promotion_target(_sg_project),
                )
                _sg_ok = True
                if _sg_gated:
                    from app.rollup_migration_validation import (  # noqa: PLC0415
                        enforce_premerge_migration_guard,
                    )

                    _sg_ok, _sg_reason = await enforce_premerge_migration_guard(
                        gh_token,
                        owner,
                        repo,
                        int(pr_num),
                        _sg_base,
                        promotion_target=get_project_promotion_target(_sg_project),
                    )
                if not _sg_ok:
                    logger.warning(
                        f"[SAFEGUARD] Post-gate merge of PR #{pr_num} held — "
                        f"Alembic dual-head guard: {_sg_reason} (orig "
                        f"{safeguard_for_id[:8]}) — leaving PR open for the "
                        f"guarded retry/resolver flow"
                    )
                else:
                    _res = await merge_pull_request(gh_token, owner, repo, int(pr_num))
                    if _res.get("merged"):
                        try:
                            await _finalize_migration_reservation_after_merge(
                                orig, session
                            )
                        except Exception as _mr_exc:  # noqa: BLE001
                            logger.warning(
                                "[MIGRATION] Failed to finalize reservation for "
                                "merged task %s: %r",
                                orig.id,
                                _mr_exc,
                            )
                        logger.info(
                            f"[SAFEGUARD] Fired held merge for COMPLETED orig "
                            f"{safeguard_for_id[:8]} PR #{pr_num} after gate release"
                        )
                    else:
                        logger.warning(
                            f"[SAFEGUARD] Post-gate merge of PR #{pr_num} (orig "
                            f"{safeguard_for_id[:8]}) did not merge: "
                            f"{_res.get('message', 'unknown')}"
                        )
        except Exception as _merge_exc:  # noqa: BLE001
            logger.warning(
                f"[SAFEGUARD] Post-gate merge attempt for PR #{pr_num} (orig "
                f"{safeguard_for_id[:8]}) failed: {_merge_exc}",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Cheap always-on acceptance probes for findings-producing task types
# (security_audit, perf). Mirrors the topic-responsiveness and grounding
# probes in knowledge_completion.py: deterministic, no external calls, errs
# toward acceptance — only a CLEAR objective miss or complete lack of file
# references fails here.
# ---------------------------------------------------------------------------

_FINDINGS_STOPWORDS = frozenset(
    {
        "about",
        "code",
        "does",
        "from",
        "function",
        "have",
        "into",
        "make",
        "made",
        "method",
        "over",
        "prompt",
        "report",
        "should",
        "state",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "topic",
        "under",
        "used",
        "using",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
        "your",
    }
)
_FINDINGS_MIN_KWS = 3
_FINDINGS_MIN_OVERLAP = 0.1


def _findings_kws(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in _FINDINGS_STOPWORDS
    }


def _findings_responsiveness_miss(task: Task, findings: list) -> str | None:
    """Return a reason when findings clearly don't match the requested target, else None."""
    payload = task.first_payload or {}
    req = _findings_kws(
        " ".join(str(payload.get(k) or "") for k in ("prompt", "topic", "target"))
    )
    if len(req) < _FINDINGS_MIN_KWS:
        return None
    doc = _findings_kws(
        " ".join(
            " ".join(str(f.get(k) or "") for k in ("title", "description", "area"))
            for f in findings
        )
    )
    overlap = req & doc
    if len(overlap) / len(req) >= _FINDINGS_MIN_OVERLAP:
        return None
    return f"findings not responsive to target ({len(overlap)}/{len(req)} keyword(s) matched in titles/descriptions)"


def _findings_grounding_miss(findings: list) -> str | None:
    """Return a reason when NO finding references a file path (completely ungrounded)."""
    if not findings or any(str(f.get("file_path") or "").strip() for f in findings):
        return None
    return f"findings not grounded in repository: none of {len(findings)} finding(s) cite a file path"


def _findings_probe_detail(task: Task, findings: list) -> dict:
    """Structured probe detail persisted in plan_output for operator visibility."""
    payload = task.first_payload or {}
    req = _findings_kws(
        " ".join(str(payload.get(k) or "") for k in ("prompt", "topic", "target"))
    )
    doc = _findings_kws(
        " ".join(
            " ".join(str(f.get(k) or "") for k in ("title", "description", "area"))
            for f in findings
        )
    )
    grounded = sum(1 for f in findings if str(f.get("file_path") or "").strip())
    return {
        "topic_responsiveness": {
            "passed": _findings_responsiveness_miss(task, findings) is None,
            "overlap": len(req & doc),
            "total": len(req),
        },
        "grounding": {
            "passed": _findings_grounding_miss(findings) is None,
            "grounded": grounded,
            "total": len(findings),
        },
    }


async def _handle_security_audit_completion(
    task: Task, logs: str, session: AsyncSession
):
    """Parse security audit findings and spawn create tasks for critical/high severity issues.

    Forward-quality gate (escalated path, mirrors _handle_research_completion):
    a critical/high audit finding spawns code-change (create) PRs — the same
    blast radius as a research finding — so when the security-audit quality
    grader is enabled and the audit would spawn implementation work, this
    handler does NOT spawn children on the first pass. It stands up an
    INDEPENDENT grader and parks itself EVALUATING. The grader's verdict returns
    via the re-entrant completion path (task_lifecycle._resolve_quality_grader_completion),
    which re-invokes this handler with ``context['quality_grader']`` set:
      * verdict PASS -> proceed to the normal spawn-on-findings path;
      * verdict FAIL -> mark FAILED and spawn NOTHING (an ungrounded or
        hallucinated audit must never launch fix PRs on its unverified say-so).
    Audits with no critical/high findings spawn nothing and never escalate — the
    gate is proportional to blast radius.
    """
    from app.task_evaluators import (
        QUALITY_GRADER_VERDICT_KEY,
        spawn_forward_quality_grader,
    )

    # Re-entry signal: the independent grader has returned its verdict.
    _grader_verdict = (task.context or {}).get(QUALITY_GRADER_VERDICT_KEY)
    if _grader_verdict is not None and not _grader_verdict.get("passed", False):
        # FAILED forward-quality gate: fail closed — do NOT spawn fix tasks and
        # do NOT complete clean. Record the verdict for observability +
        # direction-health accounting, release any safeguard gate, end terminal.
        record_failed_gate(
            task,
            EvaluationVerdict(
                False,
                str(_grader_verdict.get("reason") or "forward-quality grade failed"),
                "forward-quality",
            ),
        )
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, None, session)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            "[SECURITY AUDIT] Task %s failed the forward-quality gate; spawned "
            "no fix tasks: %s",
            task.id,
            _grader_verdict.get("reason"),
        )
        return

    # Parse the JSON findings array from logs
    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = await async_json_loads(json_str)
            findings = _normalize_findings_array(parsed)
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = (
            "Security audit completed with no parseable findings"
        )
        task.plan_output = {"findings": []}
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, findings, session)
        return

    # Store all findings + acceptance-probe detail for operator visibility.
    # Wrapped in a dict so query_findings_index (which only processes list
    # plan_output) does not treat security audit findings as research findings.
    task.plan_output = {
        "findings": findings,
        "acceptance_probe": _findings_probe_detail(task, findings),
    }
    session.add(task)
    await session.commit()

    # Goal-level acceptance probe: cheap deterministic check that findings are
    # responsive to the audited target and grounded in the repository. Runs
    # always, regardless of whether the optional quality grader is enabled.
    # Errs toward acceptance — only a CLEAR miss fails here, preventing an
    # obviously off-objective or ungrounded audit from spawning fix PRs.
    _probe_miss = _findings_responsiveness_miss(
        task, findings
    ) or _findings_grounding_miss(findings)
    if _probe_miss is not None:
        record_failed_gate(
            task,
            EvaluationVerdict(
                False, f"security_audit {_probe_miss}", "forward-quality"
            ),
        )
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, None, session)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            "[SECURITY AUDIT] Task %s failed acceptance probe: %s", task.id, _probe_miss
        )
        return

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload).get(
        "base_branch", get_project_integration_branch(project)
    )

    # Spawn create tasks for critical and high severity findings
    actionable = [f for f in findings if f.get("severity") in ("critical", "high")]
    if len(actionable) > 10:
        actionable = actionable[:10]
    if not actionable:
        task.latest_status_message = f"Security audit found {len(findings)} finding(s), none critical/high — no tasks spawned"
        session.add(task)
        await session.commit()
        await _maybe_release_safeguard_gate(task, findings, session)
        return

    # Forward-quality ESCALATION gate (blast-radius-proportional). When enabled
    # and these critical/high findings would spawn downstream fix PRs, do not
    # spawn anything yet: stand up an INDEPENDENT grader and park EVALUATING. The
    # grader's verdict returns via the re-entrant completion path, which
    # re-invokes this handler with the verdict in context (the `_grader_verdict
    # is None` guard lets the resume pass fall through to the spawn loop instead
    # of escalating again).
    if _grader_verdict is None and _runtime_config.get(
        "security_audit_quality_grader_enabled", False
    ):
        grader = await spawn_forward_quality_grader(task, json.dumps(findings), session)
        if grader is not None:
            logger.info(
                "[SECURITY AUDIT] Task %s parked awaiting forward-quality grade "
                "(grader %s) before spawning fix tasks",
                task.id,
                grader.id,
            )
            return

    # Already-spawned guard: if a prior attempt committed children but died before
    # flipping the parent to EVALUATING, a replay would re-parse the same findings
    # and spawn duplicates. Detect that case and skip spawning.
    existing_children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    already_spawned = [
        ct
        for ct in existing_children
        if (ct.context or {}).get("security_audit_task_id") == str(task.id)
    ]

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    child_tasks = []
    if already_spawned:
        # Replay path: children already committed in a prior attempt.
        # Skip spawning; use existing children to complete remaining effects.
        child_tasks = already_spawned
        logger.info(
            "[SECURITY AUDIT] Task %s replay: found %d already-spawned children, skipping re-spawn",
            task.id,
            len(child_tasks),
        )
    else:
        for i, finding in enumerate(actionable):
            fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
            if not fix_prompt:
                continue
            title = finding.get("title", f"Security fix #{i + 1}")
            severity = finding.get("severity", "high")

            new_task = Task(
                project_id=task.project_id,
                title=f"[{severity.upper()}] {title}",
                description=finding.get("description", fix_prompt[:200]),
                parent_task_id=task.id,
                payloads=[
                    validate_internal_payload(
                        {
                            "type": "create",
                            "prompt": fix_prompt,
                            "base_branch": base_branch,
                            **(
                                {"prompt_suffix": create_suffix}
                                if create_suffix
                                else {}
                            ),
                        },
                        "task_completion:security_finding",
                    )
                ],
                context={
                    "security_audit_task_id": str(task.id),
                    "finding_index": i,
                    "severity": severity,
                    "file_path": finding.get("file_path", ""),
                    "line_number": finding.get("line_number", 0),
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                requires_approval=False,  # review pipeline handles quality; only merge to main gates
                depends_on=[],
            )
            session.add(new_task)
            child_tasks.append(new_task)

        await session.flush()
        for ct in child_tasks:
            history = TaskHistory(
                task_id=ct.id,
                action="SPAWNED",
                details={
                    "spawned_by": "SECURITY_AUDIT",
                    "parent_audit_id": str(task.id),
                },
            )
            session.add(history)

    pending_broadcasts: list[tuple[str, str, dict]] = []
    for ct in child_tasks:
        pending_broadcasts.append(
            (
                str(task.project_id),
                "task_updated",
                {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
            )
        )

    # Post PR comment for medium/low findings that don't get auto-fixed
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    if informational:
        rows = "\n".join(
            "| {severity} | {title} | {loc} | {desc} |".format(
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                loc=(
                    f.get("file_path", "")
                    + (f":{f['line_number']}" if f.get("line_number") else "")
                ),
                desc=f.get("description", ""),
            )
            for f in informational
        )
        comment_body = (
            "## Security Audit Findings (Informational)\n\n"
            "The following medium/low severity findings were identified. "
            "They have not been auto-fixed and require manual review:\n\n"
            "| Severity | Title | File | Description |\n"
            "|----------|-------|------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(
                    f"[SECURITY AUDIT] Posted informational findings comment on PR #{pr_number}"
                )
        except Exception as e:
            logger.warning(
                f"[SECURITY AUDIT] Failed to post PR comment for informational findings: {e}",
                exc_info=True,
            )

    if child_tasks:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = f"{len(child_tasks)} critical/high fixed, {len(informational)} medium/low surfaced as PR comment"
    else:
        # No critical/high fix tasks spawned. Like the perf handler below, a
        # security_audit opens no PR of its own, so a childless EVALUATING task
        # has no lifecycle path to COMPLETED (no rollup trigger; reaper Step 4
        # skips childless parents) and would hang forever, pinning any parent.
        # Mark it terminal directly.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = f"Security audit complete: no critical/high findings; {len(informational)} medium/low surfaced as PR comment"
    session.add(task)
    # Single atomic commit: children INSERTs + parent status flip land together.
    # A crash before this point leaves no children (nothing was committed), so
    # the replay guard above fires only when children truly exist from a prior
    # committed attempt — making replay safe either way.
    await session.commit()

    for project_id, event_name, payload in pending_broadcasts:
        try:
            await broadcast_to_project(project_id, event_name, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[SECURITY AUDIT] Failed to broadcast {event_name} for {payload.get('task_id')}: {exc}"
            )

    await broadcast_to_project(
        str(task.project_id),
        "autonomous_activity",
        {"security_audit_task_id": str(task.id), "tasks_created": len(child_tasks)},
    )
    logger.info(
        f"[SECURITY AUDIT] Created {len(child_tasks)} fix tasks from audit {task.id}"
    )
    await _maybe_release_safeguard_gate(task, findings, session)


async def _handle_perf_completion(task: Task, logs: str, session: AsyncSession):
    """Parse perf findings and spawn a bundled create task for all high-severity issues.

    Bundles all high-severity findings into a single child task to eliminate the fan-out
    (which previously spawned one task per finding, each with its own VM, PR, and review).
    Medium/low findings are surfaced as an informational PR comment.
    """
    # Forward evaluation gate (registry-resolved 'metric' strategy): a perf
    # report that produced no parseable structured measurement must NOT complete
    # clean. Mark it FAILED (terminal) — a perf task spawns no children and opens
    # no PR of its own, so EVALUATING would hang forever. A measurement that
    # legitimately found zero high-severity issues is still a parseable result
    # and passes the gate; only a missing/unparseable measurement fails.
    verdict = evaluate_terminal_output(task, logs)
    if not verdict.passed:
        record_failed_gate(task, verdict)
        task.plan_output = {"findings": []}
        session.add(task)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            "[PERF] Task %s did not clear the metric gate: %s",
            task.id,
            verdict.reason,
        )
        return

    findings = None
    try:
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = await async_json_loads(json_str)
            findings = _normalize_findings_array(parsed)
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        # The metric gate passed, so a structured measurement exists; this
        # branch now only covers the defensive empty-list edge. Keep the
        # terminal-COMPLETED behavior for a genuinely empty parseable result.
        task.latest_status_message = (
            "Perf analysis completed with no parseable findings"
        )
        task.plan_output = {"findings": []}
        session.add(task)
        await session.commit()
        return

    # Store all findings + acceptance-probe detail for operator visibility.
    # Wrapped in a dict so query_findings_index (which only processes list
    # plan_output) does not treat perf audit findings as research findings.
    task.plan_output = {
        "findings": findings,
        "acceptance_probe": _findings_probe_detail(task, findings),
    }
    session.add(task)
    await session.commit()

    # Goal-level acceptance probe: cheap deterministic check that findings are
    # responsive to the audited target. Runs always (after the metric structural
    # gate), regardless of the optional quality grader. Errs toward acceptance —
    # only a CLEAR miss fails here. Grounding (file_path presence) is NOT
    # applied here: perf findings are legitimately area-based without file refs.
    _probe_miss = _findings_responsiveness_miss(task, findings)
    if _probe_miss is not None:
        record_failed_gate(
            task, EvaluationVerdict(False, f"perf {_probe_miss}", "forward-quality")
        )
        session.add(task)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info("[PERF] Task %s failed acceptance probe: %s", task.id, _probe_miss)
        return

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload).get(
        "base_branch", get_project_integration_branch(project)
    )

    # Collect high-severity findings (capped at 10 as before)
    actionable = [f for f in findings if f.get("severity") == "high"]
    if len(actionable) > 10:
        actionable = actionable[:10]

    child_tasks = []

    # Already-spawned guard: if a prior attempt committed a child but died before
    # flipping the parent to EVALUATING, a replay would re-parse the same findings
    # and spawn a duplicate. Detect that case and skip spawning.
    existing_children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    already_spawned = [
        ct
        for ct in existing_children
        if (ct.context or {}).get("perf_task_id") == str(task.id)
    ]

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")

    if already_spawned:
        # Replay path: child already committed in a prior attempt.
        # Skip spawning; use existing child to complete remaining effects.
        child_tasks = already_spawned
        logger.info(
            "[PERF] Task %s replay: found already-spawned child, skipping re-spawn",
            task.id,
        )
    else:
        # Bundle all high-severity findings into a single child task
        if actionable:
            # Enumerate findings in the prompt so the agent addresses them all
            findings_text = "\n".join(
                f"{i + 1}. [{finding.get('area', 'general')}] {finding.get('title', f'Finding {i + 1}')}\n"
                f"   Description: {finding.get('description', '')}\n"
                f"   Suggested approach: {finding.get('fix_prompt', '')}"
                for i, finding in enumerate(actionable)
            )

            bundled_prompt = (
                f"Performance audit identified {len(actionable)} high-severity findings:\n\n"
                f"{findings_text}\n\n"
                f"Address each finding independently in a single PR. You may skip individual "
                f"findings that prove risky or out of scope rather than failing the entire task. "
                f"Aim to fix as many as feasible without coupling unrelated changes."
            )

            new_task = Task(
                project_id=task.project_id,
                title=f"[PERF] Fix {len(actionable)} high-severity performance findings",
                description=f"Bundle of {len(actionable)} performance findings from audit",
                parent_task_id=task.id,
                payloads=[
                    validate_internal_payload(
                        {
                            "type": "create",
                            "prompt": bundled_prompt,
                            "base_branch": base_branch,
                            **(
                                {"prompt_suffix": create_suffix}
                                if create_suffix
                                else {}
                            ),
                        },
                        "task_completion:perf_findings_bundle",
                    )
                ],
                context={
                    "perf_task_id": str(task.id),
                    "finding_count": len(actionable),
                    "severity": "high",
                    "findings_areas": list(
                        set(f.get("area", "") for f in actionable if f.get("area"))
                    ),
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                requires_approval=False,
                depends_on=[],
            )
            session.add(new_task)
            child_tasks.append(new_task)

        await session.flush()
        for ct in child_tasks:
            history = TaskHistory(
                task_id=ct.id,
                action="SPAWNED",
                details={"spawned_by": "PERF_AUDIT", "parent_perf_id": str(task.id)},
            )
            session.add(history)

    pending_broadcasts: list[tuple[str, str, dict]] = []
    for ct in child_tasks:
        pending_broadcasts.append(
            (
                str(task.project_id),
                "task_updated",
                {"task_id": str(ct.id), "status": TaskStatus.QUEUED},
            )
        )

    # Post a summary comment on open PRs for medium/low findings
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    all_surfaced = informational + [
        f for f in findings if f.get("severity") == "high" and f not in actionable
    ]
    if all_surfaced or findings:
        rows = "\n".join(
            "| {area} | {severity} | {title} | {desc} |".format(
                area=f.get("area", ""),
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                desc=f.get("description", "")[:120],
            )
            for f in findings
        )
        comment_body = (
            "## Performance Analysis Findings\n\n"
            f"Analyzed codebase for performance bottlenecks. "
            f"{len(child_tasks)} high-severity fix task(s) spawned automatically.\n\n"
            "| Area | Severity | Title | Description |\n"
            "|------|----------|-------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(
                    f"[PERF] Posted findings summary comment on PR #{pr_number}"
                )
        except Exception as e:
            logger.warning(f"[PERF] Failed to post PR comment: {e}", exc_info=True)

    if child_tasks:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = (
            f"1 high-severity fix task spawned for {len(actionable)} findings, "
            f"{len(informational)} medium/low finding(s) surfaced as PR comment"
        )
    else:
        # No high-severity fix tasks spawned. A perf task opens no PR of its
        # own, so with zero children there is no COMPLETED child to trigger
        # _check_and_trigger_rollup and no children for the reaper's EVALUATING
        # sweep to act on (reaper Step 4 ``if not children_snap: continue``).
        # An EVALUATING perf task here would hang forever — and pin its
        # research/strategic parent in EVALUATING too. Mark it terminal
        # directly, the same workaround the memo handler documents for its
        # childless, PR-less case.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            "Perf audit complete: no high-severity findings; "
            f"{len(informational)} medium/low finding(s) surfaced as PR comment"
        )
    session.add(task)
    await session.commit()

    for project_id, event_name, payload in pending_broadcasts:
        try:
            await broadcast_to_project(project_id, event_name, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[PERF] Failed to broadcast {event_name} for {payload.get('task_id')}: {exc}"
            )

    try:
        await broadcast_to_project(
            str(task.project_id),
            "autonomous_activity",
            {"perf_task_id": str(task.id), "tasks_created": len(child_tasks)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PERF] Failed to broadcast autonomous activity: {exc}")
    logger.info(
        f"[PERF] Created 1 bundled fix task from perf analysis {task.id} "
        f"(findings={len(actionable)})"
    )


# ---------------------------------------------------------------------------
# Compute-experiment completion — terminal metric gate, no PR, no children
# ---------------------------------------------------------------------------


async def _persist_compute_experiment_knowledge_best_effort(
    task: Task,
    logs: str,
    criterion: Optional[dict],
    session: AsyncSession,
) -> bool:
    """Persist a compute_experiment's measured verdict as durable knowledge.

    Best-effort, additive, and self-contained: it runs only when the experiment
    produced a measurement (terminal ``outcome`` + measured ``cost``) and records
    the verdict — including an honest negative result when the declared criterion
    was NOT met — as a queryable ``KnowledgeDocument`` discoverable as prior
    knowledge by future research/strategist cycles. The deterministic capture is
    faithful (it records the real reported metrics and any A/B arms; see
    :func:`knowledge_completion.persist_compute_experiment_knowledge`) and applies
    NO interpretation — judgment is the opt-in agent's job. Wrapped so
    durable-knowledge persistence can NEVER break the compute task's completion
    semantics.

    Returns ``True`` when the deterministic document is durably persisted (or
    there was no measurement to record), ``False`` when persistence failed and a
    bounded retry through the wake sweep is warranted.
    """
    measurement = _extract_compute_measurement(logs)
    if measurement is None:
        # No measured verdict (crash / no terminal outcome) — nothing durable to record.
        return True
    outcome, cost = measurement
    try:
        from app.knowledge_completion import persist_compute_experiment_knowledge

        await persist_compute_experiment_knowledge(
            task,
            outcome=outcome,
            cost=cost,
            criterion=criterion,
            result_reference=_extract_compute_result_reference(logs),
            session=session,
            logs=logs,
        )
    except Exception:  # noqa: BLE001 — knowledge persistence must never break completion
        logger.warning(
            "[COMPUTE] Failed to persist knowledge document for task %s",
            task.id,
            exc_info=True,
        )
        # Record the failure on plan_output so the acceptance criterion
        # (KnowledgeDocument must exist) is verifiable from the task record.
        # A flush/commit failure inside persist_compute_experiment_knowledge
        # leaves the session in pending-rollback state; roll back first so the
        # fallback write starts from a clean transaction.
        try:
            await session.rollback()
            # rollback expired `task`; re-materialise it so the fallback
            # breadcrumb read+write below runs on a live instance instead of
            # firing a sync lazy-load -> MissingGreenlet (swallowed by the
            # except, silently dropping the {"persisted": False} breadcrumb).
            await session.refresh(task)
            plan_output = dict(task.plan_output or {})
            plan_output["knowledge_document"] = {"persisted": False}
            task.plan_output = plan_output
            flag_modified(task, "plan_output")
            session.add(task)
            await session.commit()
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


async def _stamp_compute_terminal_diagnostics(
    session: AsyncSession,
    task: "Task",
    logs: str,
) -> None:
    """Load the owning compute job, extract terminal diagnostics, classify, and persist.

    Called from both pre-metric failure branches in
    _handle_compute_experiment_completion.  Stamps:
    - task.context["failure_category"] via persist_failure_category
    - task.context["compute_terminal_diagnostics"] with the typed reason/phase/etc.
    - task.latest_status_message with a human-readable terminal_reason summary

    Never raises — a missing job or classification error must not block the
    FAILED finalization.
    """
    from app.failure_classification import (  # noqa: PLC0415
        classify_compute_terminal_reason,
        persist_failure_category,
    )
    from app.knowledge_completion import _extract_job_terminal_diagnostics  # noqa: PLC0415

    try:
        job_id = _extract_compute_job_id(logs)
        job = None
        if job_id:
            job = await get_compute_job_for_task(
                session, job_id=job_id, task_id=task.id
            )
        diag = _extract_job_terminal_diagnostics(job)
        terminal_reason = (diag or {}).get("terminal_reason") if diag else None
        vm_phase = (diag or {}).get("vm_phase") if diag else None
        failure_class = classify_compute_terminal_reason(terminal_reason, vm_phase)
        persist_failure_category(task, failure_class)
        if diag:
            ctx = dict(task.context or {})
            ctx["compute_terminal_diagnostics"] = diag
            task.context = ctx
            flag_modified(task, "context")
        if terminal_reason:
            phase_label = f" ({vm_phase})" if vm_phase else ""
            task.latest_status_message = (
                f"Compute job failed{phase_label}: {terminal_reason}"
            )[:500]
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPUTE] Failed to stamp terminal diagnostics for task %s",
            task.id,
            exc_info=True,
        )


async def _finalize_compute_experiment(
    session: AsyncSession,
    task: Task,
    status: TaskStatus,
    logs: str,
    criterion: Optional[dict],
    *,
    owns_commit: bool,
) -> None:
    """Persist a compute_experiment's terminal mutation, committing only if owned.

    The single seam every terminal branch of ``_handle_compute_experiment_completion``
    funnels through so commit ownership is consistent:

    * ``owns_commit=True`` (the direct post-commit dispatch path): commit the
      task mutation, broadcast the status change, and persist durable knowledge —
      today's behaviour, unchanged.
    * ``owns_commit=False`` (the wake-delivery path): mutate only. The caller —
      ``task_lifecycle.deliver_wake`` via ``reaper_sweeps.sweep_deliver_task_wakes``
      — owns the single commit that covers BOTH the FIRED→DELIVERED flip and this
      task advance, and runs ``run_compute_wake_post_commit`` for the broadcast +
      knowledge AFTER it commits (so the KnowledgeDocument's own internal commit
      cannot release the delivery row lock mid-batch).
    """
    session.add(task)
    if not owns_commit:
        return
    await session.commit()
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": status},
    )
    await _persist_compute_experiment_knowledge_best_effort(
        task, logs, criterion, session
    )


async def _clear_post_commit_marker(session: AsyncSession, registration) -> None:
    """Remove post-commit retry keys from source_metadata and commit."""
    marker = dict(registration.source_metadata or {})
    marker.pop(_COMPUTE_WAKE_POST_COMMIT_META_KEY, None)
    marker.pop(_COMPUTE_WAKE_NEXT_RETRY_KEY, None)
    registration.source_metadata = marker
    flag_modified(registration, "source_metadata")
    session.add(registration)
    await session.commit()


async def _schedule_post_commit_backoff(
    session: AsyncSession, registration, attempts: int
) -> None:
    """Write a next_retry_after backoff timestamp into source_metadata and commit."""
    backoff_delta = timedelta(seconds=min(60 * 2 ** (attempts - 1), 3600))
    next_retry_after = (_utcnow() + backoff_delta).isoformat()
    marker = dict(registration.source_metadata or {})
    marker[_COMPUTE_WAKE_NEXT_RETRY_KEY] = next_retry_after
    registration.source_metadata = marker
    flag_modified(registration, "source_metadata")
    session.add(registration)
    await session.commit()


async def run_compute_wake_post_commit(session: AsyncSession, registration) -> bool:
    """Post-commit broadcast + durable knowledge for a wake-finalized compute task.

    Invoked by ``reaper_sweeps.sweep_deliver_task_wakes`` AFTER it commits a
    delivered ``compute_job_terminal`` registration. Mirrors the side effects the
    direct completion path performs inside ``_finalize_compute_experiment`` with
    ``owns_commit=True`` — but deferred until after the delivery commit so the
    KnowledgeDocument persistence (which commits internally) can never release the
    delivery row lock before its sibling registrations are processed.

    Strictly best-effort and fully self-contained: a no-op for any non-compute
    wake kind, and any failure is reported back as ``False`` so the sweep can
    retry a delivered registration carrying the durable marker. Transient
    failures write a ``_COMPUTE_WAKE_NEXT_RETRY_KEY`` backoff timestamp so the
    retry sub-pass skips this row until the window expires, preventing a failing
    batch from starving newer marked rows. After ``_COMPUTE_WAKE_MAX_ATTEMPTS``
    the marker is cleared and a WARNING is emitted.
    """
    from app.compute_jobs import (
        COMPUTE_JOB_TERMINAL_WAKE_KIND,
        build_compute_result_logs_async,
    )
    from app.models import ComputeJob

    if registration.wake_source_kind != COMPUTE_JOB_TERMINAL_WAKE_KIND:
        return True
    try:
        task = await session.get(Task, registration.task_id)
        if task is None:
            # Permanent failure: task is gone; clear the marker so the row is
            # not retried indefinitely.
            with contextlib.suppress(Exception):
                await _clear_post_commit_marker(session, registration)
            return False
        marker = dict(registration.source_metadata or {})
        marker_meta = dict(marker.get(_COMPUTE_WAKE_POST_COMMIT_META_KEY) or {})
        attempts = int(marker_meta.get("attempts", 0) or 0) + 1
        if attempts >= _COMPUTE_WAKE_MAX_ATTEMPTS:
            logger.warning(
                "[COMPUTE] Post-commit wake max attempts (%d) reached for task %s; clearing marker",
                _COMPUTE_WAKE_MAX_ATTEMPTS,
                registration.task_id,
            )
            with contextlib.suppress(Exception):
                await _clear_post_commit_marker(session, registration)
            return False
        marker_meta["attempts"] = attempts
        marker_meta["updated_at"] = _utcnow().isoformat()
        if "started_at" not in marker_meta:
            marker_meta["started_at"] = marker_meta["updated_at"]
        marker[_COMPUTE_WAKE_POST_COMMIT_META_KEY] = marker_meta
        # Clear any stale next_retry_after now that we are actively attempting.
        marker.pop(_COMPUTE_WAKE_NEXT_RETRY_KEY, None)
        registration.source_metadata = marker
        flag_modified(registration, "source_metadata")
        session.add(registration)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": task.status},
        )
        from app.cost_ceiling import check_daily_cost_ceiling

        await check_daily_cost_ceiling(task, session)
        from app.monthly_budget_alerts import check_monthly_budget

        await check_monthly_budget(session)
        job = None
        if registration.wake_source_ref:
            with contextlib.suppress(ValueError, TypeError):
                job = await session.get(
                    ComputeJob, uuid.UUID(str(registration.wake_source_ref))
                )
        if job is None:
            # Permanent failure: job is gone; clear the marker.
            with contextlib.suppress(Exception):
                await _clear_post_commit_marker(session, registration)
            return False
        logs = await build_compute_result_logs_async(job)
        criterion = (task.context or {}).get("compute_success_criterion")
        persisted = await _persist_compute_experiment_knowledge_best_effort(
            task, logs, criterion, session
        )
        if not persisted:
            # Durable knowledge did not persist. Keep the durable marker and
            # schedule a bounded backoff retry so Pass 0 re-attempts. Bounded by
            # _COMPUTE_WAKE_MAX_ATTEMPTS so a stuck job never retries forever.
            with contextlib.suppress(Exception):
                await _schedule_post_commit_backoff(session, registration, attempts)
            return False
        marker = dict(registration.source_metadata or {})
        marker.pop(_COMPUTE_WAKE_POST_COMMIT_META_KEY, None)
        marker.pop(_COMPUTE_WAKE_NEXT_RETRY_KEY, None)
        registration.source_metadata = marker
        flag_modified(registration, "source_metadata")
        session.add(registration)
        await session.commit()
        return True
    except Exception:  # noqa: BLE001 — post-commit effects must never break the sweep
        logger.warning(
            "[COMPUTE] Post-commit wake side effects failed for task %s",
            getattr(registration, "task_id", None),
            exc_info=True,
        )
        # Best-effort: rollback and schedule a backoff retry so the row is not
        # retried every cycle, which would starve newer marked rows.
        with contextlib.suppress(Exception):
            await session.rollback()
            # rollback expired `registration`; re-materialise it so the backoff
            # read/schedule below runs on a live instance instead of firing a
            # sync lazy-load -> MissingGreenlet (swallowed by the suppress,
            # silently skipping the anti-starvation backoff).
            await session.refresh(registration)
            marker = dict(registration.source_metadata or {})
            marker_meta = dict(marker.get(_COMPUTE_WAKE_POST_COMMIT_META_KEY) or {})
            attempts = int(marker_meta.get("attempts", 0) or 0)
            await _schedule_post_commit_backoff(session, registration, attempts)
        return False


# Terminal outcomes that are INTENTIONAL kills (the substrate deliberately stopped
# the job by budget/deadline, or it was cancelled). An unmeasured declared metric
# under one of these is EXPECTED — the experiment never got to finish — so it must
# NOT trip the loud unmeasured-criterion failure; it stays a clean terminal outcome
# exactly as today. A genuine SUCCESS (or FAILURE) with an unmeasured declared
# metric is the misleading state the loud failure exists to catch.
_COMPUTE_INTENTIONAL_KILL_OUTCOMES: frozenset[str] = frozenset(
    {"SPEND_CAP_EXCEEDED", "TIMED_OUT", "CANCELLED"}
)


def _is_unmeasured_declared_metric(task: Task, criterion: Optional[dict]) -> bool:
    """True when ``task`` declared a kind=metric criterion that produced NO value.

    The loud unmeasured-criterion failure applies ONLY to ``kind="metric"``
    criteria that came back unmeasurable (no numeric value in the terminal
    result). It explicitly excludes:

      * ``kind="artifact_produced"`` criteria — they gate on ``durable_file_count``
        through the artifact-delivery path, not a reported numeric metric;
      * a measured criterion (real numeric ``value``) that merely missed its
        threshold — that is faithfully recorded negative evidence, not a failure;
      * the no-criterion case — presence-based behavior is preserved.
    """
    if not isinstance(criterion, dict):
        return False
    # measurable is False ONLY when the metric was absent/non-numeric in the
    # result. A measured-but-missed criterion has measurable True and is evidence.
    if criterion.get("measurable") is not False:
        return False
    payload = task.first_payload or {}
    raw = payload.get("success_criterion") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return False
    kind = str(raw.get("kind") or "metric")
    return kind == "metric"


# Context key stamped on a spawned reason-and-ship follow-on identifying its
# originating compute_experiment. Doubles as the replay guard so a re-dispatched
# completion never spawns a duplicate follow-on — mirroring security_audit's
# ``security_audit_task_id`` marker.
_ACT_ON_VERDICT_SOURCE_KEY = "act_on_verdict_source_task_id"

# Cap on the raw faithful-capture bytes embedded in the follow-on prompt so the
# spawned create task's payload stays comfortably under the payload byte budget
# (_MAX_PAYLOAD_BYTES). The structured compute_result is always included in full;
# only the raw result record is truncated.
_ACT_ON_VERDICT_LOG_CHARS = 16000


def _parse_compute_result_record(logs: str) -> dict[str, Any] | None:
    """Best-effort parse of the COMPUTE_RESULT record (``build_compute_result_logs``).

    Returns the record dict — carrying ``terminal_reason`` and the readable
    ``command_log_text`` — or ``None`` when ``logs`` is not the JSON record
    (legacy free-form logs, raw crash output).
    """
    try:
        parsed = json.loads(logs) if logs else None
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _compute_terminal_reason_of(
    compute_result: dict[str, Any] | None, logs: str
) -> str | None:
    """The job's ``terminal_reason``, from the handler result or the raw record."""
    reason = (compute_result or {}).get("terminal_reason")
    if not (isinstance(reason, str) and reason.strip()):
        reason = (_parse_compute_result_record(logs) or {}).get("terminal_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _build_act_on_verdict_prompt(
    task: Task,
    compute_result: dict[str, Any],
    logs: str,
    *,
    failure_mode: bool = False,
) -> str:
    """Author the fresh disposition follow-on agent's prompt.

    Two framings, selected by ``failure_mode``:

    * Success (``failure_mode=False``): a reason-and-ship prompt. Embeds the
      experiment identity, the ORIGINAL hypothesis/goal/criterion, and the FULL
      faithful results (both arms + real numbers) so a fresh, independent agent
      can judge whether the result warrants a change. No success rubric and no
      A-vs-B verdict is hardcoded — that judgment is the agent's job.

    * Failure (``failure_mode=True``): a diagnose-and-fix prompt. The experiment
      crashed, timed out before measuring, or never delivered the artifact/metric
      it declared — its traceback is otherwise stranded in a GCS bundle invisible
      to the strategist. This framing surfaces the captured stderr/traceback tail
      PROMINENTLY and asks the agent to diagnose root cause, fix it, add a
      regression guard, and open a PR (or record a reasoned no-op for a transient
      infra blip or a legitimately expected miss). The COMPUTE_RESULT artifact
      pointer (when one exists) is still forwarded via the spawned task's context,
      exactly as on the success path.
    """
    payload = task.first_payload or {}
    experiment = payload.get("experiment") or "(unnamed experiment)"
    benchmark = payload.get("benchmark")
    hypothesis = payload.get("hypothesis") or "(no explicit hypothesis declared)"
    metric = payload.get("metric")
    success_criterion = payload.get("success_criterion")

    if failure_mode:
        lines = [
            "You are a fresh, independent engineer acting on a FAILED compute "
            "experiment. The experiment did NOT produce a clean, measured result: "
            "it crashed, timed out before emitting its metric, or never delivered "
            "the artifact/metric it declared. Diagnose the root cause and FIX it "
            "in this same task.",
            "",
            f"Experiment: {experiment}",
        ]
    else:
        lines = [
            "You are a fresh, independent engineer acting on a COMPLETED compute "
            "experiment. Reason over its results and SHIP in this same task.",
            "",
            f"Experiment: {experiment}",
        ]
    if benchmark:
        lines.append(f"Benchmark: {benchmark}")
    lines.append(f"Originating experiment task id: {task.id}")
    lines.append("")
    lines.append(f"Original hypothesis / goal:\n{hypothesis}")
    if metric:
        lines.append(f"\nDeclared metric: {metric}")
    if success_criterion is not None:
        lines.append(
            "\nDeclared success criterion:\n"
            + json.dumps(success_criterion, indent=2, default=str)
        )
    lines.append("")
    if failure_mode:
        record = _parse_compute_result_record(logs)
        terminal_reason = _compute_terminal_reason_of(compute_result, logs)
        log_tail = (record or {}).get("command_log_text")
        log_tail = (
            log_tail.strip() if isinstance(log_tail, str) and log_tail.strip() else None
        )
        # Reason first: WHY the job went terminal (deadline kill, spend cap,
        # provisioning timeout, runner failure) — not just that it FAILED.
        if terminal_reason:
            lines.append(f"Terminal reason (why the job ended): {terminal_reason}")
            lines.append("")
        # The traceback/stderr tail is the key visibility fix — show it FIRST and
        # prominently so the diagnosing agent does not have to fetch the GCS
        # bundle. When ``logs`` is the structured record, its command_log_text
        # (unescaped) IS the captured output; otherwise fall back to the raw
        # logs (crash traceback / legacy free-form output).
        raw = (log_tail or "") if record is not None else (logs or "")
        raw = raw.strip()
        if raw:
            if len(raw) > _ACT_ON_VERDICT_LOG_CHARS:
                raw = raw[-_ACT_ON_VERDICT_LOG_CHARS:]
                raw = "…(truncated; showing tail)…\n" + raw
            lines.append(
                "Captured stderr / traceback tail (the experiment's failure "
                "output — diagnose from this):\n" + raw
            )
        else:
            lines.append(
                "No stderr/traceback was captured for this failure — the "
                "experiment may have died before emitting any output."
            )
        # Merge the parsed terminal record (minus the log text shown above)
        # under the handler-derived compute_result so gate-fail spawns — which
        # pass compute_result={} — still forward outcome/cost/terminal metadata.
        record_view = {
            k: v
            for k, v in (record or {}).items()
            if k not in ("command_log_text", "command_log_truncated")
        }
        merged_record = {**record_view, **(compute_result or {})}
        if merged_record:
            lines.append(
                "\nPartial / terminal result record (may be incomplete):\n"
                + json.dumps(merged_record, indent=2, default=str)
            )
        lines.append("")
        lines.append(
            "Diagnose the root cause of the failure, then: implement the fix, add "
            "a regression guard (a test or assertion that would have caught this), "
            "and open a PR. If the failure is a transient infra blip or a "
            "legitimately expected miss (not a code defect), conclude instead with "
            "a recorded no-op that explains why no change is warranted."
        )
        return "\n".join(lines)

    lines.append(
        "Full experiment results (faithful capture — both A/B arms and the real "
        "numbers):\n" + json.dumps(compute_result, indent=2, default=str)
    )
    raw = (logs or "").strip()
    if raw:
        if len(raw) > _ACT_ON_VERDICT_LOG_CHARS:
            raw = raw[:_ACT_ON_VERDICT_LOG_CHARS] + "\n…(truncated)…"
        lines.append("\nRaw result record:\n" + raw)
    lines.append("")
    lines.append(
        "Reason about whether this result warrants a change to the codebase. "
        "Compare the arms yourself — do NOT assume a success rubric. If the "
        "result warrants a change, implement it and open a PR. If it does not, "
        "conclude with a recorded no-op that explains why the result does not "
        "warrant a change."
    )
    return "\n".join(lines)


async def _spawn_act_on_verdict_followon(
    session: AsyncSession,
    task: Task,
    compute_result: dict[str, Any],
    logs: str,
    *,
    failure_mode: bool = False,
) -> Task | None:
    """Spawn ONE write-capable disposition follow-on for a terminal experiment.

    Mirrors the security_audit completion→spawn-create pattern: builds a
    ``type=create`` child (``created_by="ORCHESTRATOR"``, ``QUEUED``,
    ``requires_approval=False``, ``parent_task_id=task.id``), records a SPAWNED
    ``TaskHistory`` row, and — when the experiment produced a durable
    COMPUTE_RESULT artifact — wires that artifact forward into the child's
    ``context[artifact_prerequisites]`` (Pillar 5 spawn-forward) using the
    canonical artifact-pointer shape, so the follow-on arrives with a fetchable
    pointer it can resolve via ``GET /api/agent/artifacts/download-url``.

    ``failure_mode`` selects the diagnose-and-fix prompt/title variant used for
    the FAILURE branches (crash / timed-out-before-measure / undelivered
    artifact) instead of the success-path reason-and-ship framing.

    The ORCHESTRATOR does this spawn; the strategist allowlist
    (``task_capabilities.strategist_spawnable``) is deliberately NOT touched.

    Idempotent: if a follow-on for this task already exists (a prior completion
    attempt committed it before dying), returns it without spawning a duplicate.
    The caller adds nothing else to the session before its single commit, so the
    child INSERT + parent advance land atomically.

    Concurrency-bounded: when the project already has at least
    ``max_concurrent_act_on_verdict_followons`` (default 4) non-terminal
    disposition follow-ons in flight, the spawn is SKIPPED (returns ``None``)
    without stamping the dedup key, so the next re-dispatch retries once the
    fleet drains.
    """
    existing_children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    for ct in existing_children:
        if (ct.context or {}).get(_ACT_ON_VERDICT_SOURCE_KEY) == str(task.id):
            logger.info(
                "[COMPUTE] act_on_verdict follow-on already spawned for task %s; "
                "skipping duplicate",
                task.id,
            )
            return ct

    # Concurrency cap: count this project's in-flight disposition follow-ons
    # (non-terminal type=create children carrying the source key, across ALL
    # experiments). At/above the cap, skip WITHOUT stamping the dedup key so a
    # later re-dispatch retries once the fleet drains.
    try:
        cap = int(_runtime_config.get("max_concurrent_act_on_verdict_followons", 4))
    except (TypeError, ValueError):
        cap = 4
    if cap > 0:
        in_flight_rows = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == task.project_id,
                        Task.status.in_((TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)),
                    )
                )
            )
            .scalars()
            .all()
        )
        in_flight = sum(
            1
            for t in in_flight_rows
            if (t.context or {}).get(_ACT_ON_VERDICT_SOURCE_KEY)
        )
        if in_flight >= cap:
            logger.warning(
                "[COMPUTE] act_on_verdict follow-on cap reached for project %s "
                "(%d in-flight >= cap %d); skipping spawn for task %s — will "
                "retry on re-dispatch",
                task.project_id,
                in_flight,
                cap,
                task.id,
            )
            return None

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload or {}).get(
        "base_branch"
    ) or get_project_integration_branch(project)
    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    prompt = _build_act_on_verdict_prompt(
        task, compute_result, logs, failure_mode=failure_mode
    )

    context: dict[str, Any] = {_ACT_ON_VERDICT_SOURCE_KEY: str(task.id)}
    # Machine-readable WHY for the follow-on: forward the job's terminal_reason
    # (the existing vocabulary — deadline kill, spend cap, provisioning timeout,
    # runner failure) so iteration agents and tooling read the reason without
    # re-parsing the prompt.
    _terminal_reason = _compute_terminal_reason_of(compute_result, logs)
    if _terminal_reason:
        context["compute_terminal_reason"] = _terminal_reason[:500]
    # Pillar 5 spawn-forward: when a durable COMPUTE_RESULT artifact exists, stamp
    # its canonical pointer onto the follow-on so the fresh agent can mint a signed
    # URL. Best-effort: a missing/unresolvable artifact must not block the spawn.
    artifact_id = compute_result.get("artifact_id")
    if artifact_id:
        try:
            artifact = await session.get(Artifact, uuid.UUID(str(artifact_id)))
        except (ValueError, TypeError):
            artifact = None
        if artifact is not None:
            context[ARTIFACT_PREREQUISITES_KEY] = [
                _artifact_pointer(artifact, str(task.id))
            ]

    experiment_label = (task.first_payload or {}).get(
        "experiment"
    ) or "compute experiment"
    if failure_mode:
        title = f"diagnose-and-fix: {experiment_label}"[:200]
        description = (
            "Diagnose a failed compute experiment, fix the root cause, and ship a guard"
        )
    else:
        title = f"act-on-verdict: {experiment_label}"[:200]
        description = (
            "Reason over a completed compute experiment's results and ship if warranted"
        )
    child = Task(
        project_id=task.project_id,
        title=title,
        description=description,
        parent_task_id=task.id,
        payloads=[
            validate_internal_payload(
                {
                    "type": "create",
                    "prompt": prompt,
                    "base_branch": base_branch,
                    **({"prompt_suffix": create_suffix} if create_suffix else {}),
                },
                "task_completion:act_on_verdict",
            )
        ],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        requires_approval=False,  # review/CI gate handles quality on its own PR
        depends_on=[],
    )
    session.add(child)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=child.id,
            action="SPAWNED",
            details={
                "spawned_by": "ACT_ON_VERDICT",
                "parent_experiment_id": str(task.id),
            },
        )
    )
    logger.info(
        "[COMPUTE] Spawned act_on_verdict %s follow-on %s for experiment %s",
        "diagnose-and-fix" if failure_mode else "reason-and-ship",
        child.id,
        task.id,
    )
    return child


# Context key stamped on a sweep aggregate follow-on to distinguish it from
# single-arm follow-ons (which carry _ACT_ON_VERDICT_SOURCE_KEY instead).
_ACT_ON_VERDICT_SWEEP_KEY = "act_on_verdict_sweep_id"

# Terminal states — a sweep arm in one of these is done contributing results.
_TERMINAL_TASK_STATUSES = frozenset(
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED]
)

# Best-effort deadline: if this many hours have elapsed since the oldest arm
# was created and some arms are still non-terminal (wedged), finalize the
# aggregate on partial results rather than deferring indefinitely.
_SWEEP_AGGREGATE_BEST_EFFORT_DEADLINE_HOURS = 24


def _build_sweep_aggregate_prompt(
    tasks: list,
    sweep_id: str,
    *,
    failure_mode: bool = False,
) -> str:
    """Build the aggregate follow-on prompt covering all arms of a sweep."""
    if failure_mode:
        lines = [
            "You are a fresh, independent engineer acting on a FAILED compute-experiment "
            "sweep. One or more arms of the sweep did NOT produce a clean, measured result. "
            "Diagnose the root causes and FIX them in this same task.",
            "",
            f"Sweep id: {sweep_id}",
            f"Arms ({len(tasks)} total):",
        ]
    else:
        lines = [
            "You are a fresh, independent engineer acting on a COMPLETED compute-experiment "
            "sweep. Reason over the aggregate results across all arms and SHIP in this same task.",
            "",
            f"Sweep id: {sweep_id}",
            f"Arms ({len(tasks)} total):",
        ]
    for arm in tasks:
        payload = arm.first_payload or {}
        experiment = payload.get("experiment") or "(unnamed)"
        hypothesis = payload.get("hypothesis") or "(no hypothesis)"
        compute_result = (
            (arm.plan_output or {}).get("compute_result")
            if isinstance(arm.plan_output, dict)
            else None
        )
        lines.append(f"\n  Arm task id: {arm.id}")
        lines.append(f"  Experiment: {experiment}")
        lines.append(f"  Hypothesis: {hypothesis}")
        if compute_result:
            lines.append(
                "  Result:\n"
                + "\n".join(
                    "    " + ln
                    for ln in json.dumps(
                        compute_result, indent=2, default=str
                    ).splitlines()
                )
            )
        else:
            lines.append(f"  Result: (none — arm status={arm.status})")
    lines.append("")
    if failure_mode:
        lines.append(
            "Diagnose the root cause(s) of the failure(s), then: implement fixes, "
            "add regression guards, and open a PR. If a failure is a transient infra "
            "blip or legitimately expected miss, record a no-op explaining why."
        )
    else:
        lines.append(
            "Reason about whether these aggregate results warrant a change to the "
            "codebase. Compare arms where applicable. If warranted, implement the "
            "change and open a PR. Otherwise, record a no-op explaining why."
        )
    return "\n".join(lines)


async def _maybe_spawn_sweep_aggregate_followon(
    session: AsyncSession,
    task: Task,
    sweep_id: str,
    compute_result: dict[str, Any],
    logs: str,
    *,
    failure_mode: bool = False,
) -> Task | None:
    """Spawn ONE aggregate disposition follow-on when ALL sweep arms are terminal.

    If any sibling arm is still non-terminal, logs and returns None — a later
    arm completion will re-evaluate. When all arms are terminal, spawns one
    aggregate ``type=create`` child whose prompt covers every arm's result.

    Idempotent: guards against double-spawn via ``_ACT_ON_VERDICT_SWEEP_KEY``
    stamped in the child's context.
    """
    # Load all sibling tasks sharing this sweep_id on the same project.
    sibling_rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    Task.payloads[0]["sweep_id"].as_string() == sweep_id,
                    Task.payloads[0]["type"].as_string() == "compute_experiment",
                )
            )
        )
        .scalars()
        .all()
    )

    if not sibling_rows:
        # Defensive: treat self-only as a single-arm sweep and proceed.
        sibling_rows = [task]

    # The current task is being finalized right now (status not yet committed);
    # exclude it from the non-terminal check so the last arm can trigger the spawn.
    non_terminal = [
        t
        for t in sibling_rows
        if t.status not in _TERMINAL_TASK_STATUSES and t.id != task.id
    ]
    if non_terminal:
        # Check whether the best-effort deadline has elapsed.  If the oldest arm
        # was created more than _SWEEP_AGGREGATE_BEST_EFFORT_DEADLINE_HOURS ago,
        # finalize on partial results rather than waiting for wedged arms.
        oldest_created = min(
            (t.created_at for t in sibling_rows if t.created_at is not None),
            default=None,
        )
        past_deadline = oldest_created is not None and (
            _utcnow() - oldest_created
        ) > timedelta(hours=_SWEEP_AGGREGATE_BEST_EFFORT_DEADLINE_HOURS)
        if not past_deadline:
            logger.info(
                "[COMPUTE] Sweep %s: arm %s terminal but %d sibling(s) still non-terminal; "
                "deferring aggregate follow-on",
                sweep_id,
                task.id,
                len(non_terminal),
            )
            return None
        # Deadline elapsed — finalize best-effort; treat wedged arms as failures.
        logger.warning(
            "[COMPUTE] Sweep %s: %d arm(s) wedged past %dh deadline — "
            "finalizing best-effort aggregate on %d/%d terminal arm(s)",
            sweep_id,
            len(non_terminal),
            _SWEEP_AGGREGATE_BEST_EFFORT_DEADLINE_HOURS,
            len(sibling_rows) - len(non_terminal),
            len(sibling_rows),
        )
        failure_mode = True

    # All arms are terminal — check whether an aggregate follow-on already exists.
    sibling_ids = [s.id for s in sibling_rows]
    all_children = (
        (
            await session.execute(
                select(Task).where(Task.parent_task_id.in_(sibling_ids))
            )
        )
        .scalars()
        .all()
    )
    for ct in all_children:
        if (ct.context or {}).get(_ACT_ON_VERDICT_SWEEP_KEY) == sweep_id:
            logger.info(
                "[COMPUTE] Sweep %s: aggregate follow-on %s already exists; "
                "skipping duplicate spawn",
                sweep_id,
                ct.id,
            )
            return ct

    # Determine overall failure_mode: True when ANY arm failed non-intentionally.
    any_failure = any(t.status == TaskStatus.FAILED for t in sibling_rows)
    effective_failure_mode = failure_mode or any_failure

    project = await session.get(Project, task.project_id)
    base_branch = (task.first_payload or {}).get(
        "base_branch"
    ) or get_project_integration_branch(project)
    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")

    prompt = _build_sweep_aggregate_prompt(
        list(sibling_rows), sweep_id, failure_mode=effective_failure_mode
    )

    # Collect durable artifact pointers from all arms (Pillar 5 spawn-forward).
    # Batch-load the arms' artifacts once (was one session.get per sibling) and
    # look them up in-memory; the per-sibling UUID parse + None-handling below is
    # preserved exactly so invalid/missing ids skip just as before.
    _sweep_artifact_uuids: set[uuid.UUID] = set()
    for sibling in sibling_rows:
        sibling_result = (
            (sibling.plan_output or {}).get("compute_result")
            if isinstance(sibling.plan_output, dict)
            else None
        )
        if sibling_result:
            artifact_id = sibling_result.get("artifact_id")
            if artifact_id:
                try:
                    _sweep_artifact_uuids.add(uuid.UUID(str(artifact_id)))
                except (ValueError, TypeError):
                    pass
    artifacts_by_id: dict[uuid.UUID, Artifact] = {}
    if _sweep_artifact_uuids:
        _artifact_rows = (
            (
                await session.execute(
                    select(Artifact).where(Artifact.id.in_(_sweep_artifact_uuids))
                )
            )
            .scalars()
            .all()
        )
        artifacts_by_id = {a.id: a for a in _artifact_rows}

    artifact_prereqs: list[dict] = []
    for sibling in sibling_rows:
        sibling_result = (
            (sibling.plan_output or {}).get("compute_result")
            if isinstance(sibling.plan_output, dict)
            else None
        )
        if sibling_result:
            artifact_id = sibling_result.get("artifact_id")
            if artifact_id:
                try:
                    artifact = artifacts_by_id.get(uuid.UUID(str(artifact_id)))
                except (ValueError, TypeError):
                    artifact = None
                if artifact is not None:
                    artifact_prereqs.append(
                        _artifact_pointer(artifact, str(sibling.id))
                    )

    context: dict[str, Any] = {
        _ACT_ON_VERDICT_SWEEP_KEY: sweep_id,
        "sweep_arm_task_ids": [str(t.id) for t in sibling_rows],
    }
    if artifact_prereqs:
        context[ARTIFACT_PREREQUISITES_KEY] = artifact_prereqs

    if effective_failure_mode:
        title = f"diagnose-and-fix: sweep {sweep_id[:8]}"[:200]
        description = "Diagnose failed sweep arms, fix root causes, and ship a guard"
    else:
        title = f"act-on-verdict: sweep {sweep_id[:8]}"[:200]
        description = "Reason over a completed compute sweep's aggregate results and ship if warranted"

    child = Task(
        project_id=task.project_id,
        title=title,
        description=description,
        parent_task_id=task.id,
        payloads=[
            validate_internal_payload(
                {
                    "type": "create",
                    "prompt": prompt,
                    "base_branch": base_branch,
                    **({"prompt_suffix": create_suffix} if create_suffix else {}),
                },
                "task_completion:act_on_verdict_sweep",
            )
        ],
        context=context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        depends_on=[],
    )
    session.add(child)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=child.id,
            action="SPAWNED",
            details={
                "spawned_by": "ACT_ON_VERDICT_SWEEP",
                "sweep_id": sweep_id,
                "arm_task_ids": [str(t.id) for t in sibling_rows],
            },
        )
    )
    logger.info(
        "[COMPUTE] Sweep %s: spawned aggregate %s follow-on %s covering %d arm(s)",
        sweep_id,
        "diagnose-and-fix" if effective_failure_mode else "reason-and-ship",
        child.id,
        len(sibling_rows),
    )
    return child


def _parse_capability_proof_trial_result(task: Task, logs: str) -> dict | None:
    """Extract a ``capability_proof_trial`` agent-reported result dict.

    The agent's bespoke handler reports
    ``{cap, head_sha, test_refs, ran, passed, skipped_summary, could_not_run}``.
    We accept it from either the durable ``task.plan_output`` (a dict) or the raw
    reported ``logs`` (JSON, optionally wrapped under a
    ``capability_proof_trial_result`` key). Returns the result dict, or ``None``
    when nothing parseable is present. Never raises.
    """
    po = getattr(task, "plan_output", None)
    if isinstance(po, dict) and ("ran" in po or po.get("type") == "capability_proof_trial"):
        return po
    if isinstance(logs, str) and logs.strip():
        try:
            parsed = json.loads(logs)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed.get("capability_proof_trial_result")
            if isinstance(inner, dict):
                return inner
            if "ran" in parsed or parsed.get("type") == "capability_proof_trial":
                return parsed
    return None


async def _handle_capability_proof_trial_completion(
    task: Task, logs: str, session: AsyncSession
) -> None:
    """Ingest a ``capability_proof_trial``'s executed-test result into the ledger.

    Slice 2 of the machine-verified capability-proof earn path (design 84021e2d).
    The agent handler runs ONLY a capability's declared convention-named covering
    tests as a targeted runner job and reports
    ``{cap, head_sha, test_refs, ran, passed, skipped_summary, could_not_run}``.
    This handler is the INGESTION half:

      * On a GREEN result (:func:`ci_resolution.capability_proof_trial_is_green` —
        the tests actually ran and passed and the suite verified something), it
        mints the genuine ``proven`` named_test proof via
        :func:`record_capability_named_test_execution_proof`.
      * On a RED / skipped / could-not-run result, per design §7 it NEVER demotes:
        no proof is recorded; the contradiction is stamped on ``task.context`` for
        strategist visibility only (it does NOT touch the capability's
        proof-scoring evidence_refs, so an existing ``proven`` is untouched).

    **Receipt completeness requirement**: a GREEN receipt must identify the
    *complete* enrolled test suite (``test_refs`` must equal the enrolled set
    exactly — neither a strict subset nor a superset) and the *actual checkout
    revision* (``head_sha`` must match the enrolled SHA when one was specified).
    Any mismatch records NO proof.  The recorded ``commit_sha`` uses the
    agent-reported ``head_sha`` (actual executed revision) in preference to the
    enrollment-time estimate, falling back to the enrolled SHA only when the
    agent omitted it.

    Operator-disableable + fail-open: the
    ``capability_proof_execution_trials_enabled`` runtime flag defaults to True,
    and any parse/lookup/record error leaves the capability at its existing proof,
    never worse. Non-raising (the post-commit dispatcher already guards, but the
    contract here is explicitly no-raise so a malformed trial can never strand the
    surrounding commit).
    """
    try:
        if not _runtime_config.get("capability_proof_execution_trials_enabled", False):
            logger.info(
                "[CAP_TRIAL] flag off — ingestion skipped for task %s", task.id
            )
            return

        from app.capability_proof import (  # noqa: PLC0415
            record_capability_named_test_execution_proof,
        )
        from app.ci_resolution import (  # noqa: PLC0415
            capability_proof_trial_is_green,
        )

        result = _parse_capability_proof_trial_result(task, logs)
        if result is None:
            logger.warning(
                "[CAP_TRIAL] no parseable trial result for task %s — ingesting nothing",
                task.id,
            )
            return

        cap = result.get("cap")
        if not isinstance(cap, str) or not cap.strip():
            logger.warning(
                "[CAP_TRIAL] trial result for task %s missing 'cap' — ingesting nothing",
                task.id,
            )
            return
        head_sha = result.get("head_sha")
        refs = [
            r for r in (result.get("test_refs") or []) if isinstance(r, str) and r.strip()
        ]

        project = await session.get(Project, task.project_id)
        if project is None:
            logger.warning(
                "[CAP_TRIAL] project %s gone for trial task %s — ingesting nothing",
                task.project_id,
                task.id,
            )
            return

        async def _record_receipt_contradiction(reason: str) -> None:
            """Persist an auditable rejection without changing proof state.

            A receipt-contract failure is neither a test failure nor authority to
            revoke an existing proof.  It must nevertheless be visible to the
            stalled-trial health surface rather than disappearing into logs.
            """
            ctx = dict(task.context or {})
            ctx["capability_proof_trial_contradiction"] = {
                "reason": reason,
                "ran": bool(result.get("ran")),
                "passed": bool(result.get("passed")),
                "could_not_run": bool(result.get("could_not_run")),
                "recorded_at": _utcnow().isoformat(),
            }
            task.context = ctx
            flag_modified(task, "context")
            await session.commit()

        # --- Validate agent-reported result against enrollment contract ---
        # The enrollment (task.first_payload) is the orchestrator-authored contract
        # written by enroll_capability_proof_trial. The agent must not be able to
        # mint proof for a different cap, unenrolled test_refs, or a revision that
        # was never enrolled. On ANY mismatch, record NO proof and surface the
        # contradiction on the task context for strategist visibility (mirroring the
        # non-green fail-open branch).
        from app.capability_registry import normalize_capability_name as _norm_cap  # noqa: PLC0415

        enrolled = task.first_payload
        if not isinstance(enrolled, dict):
            logger.warning(
                "[CAP_TRIAL] no enrollment payload for task %s — ingesting nothing",
                task.id,
            )
            return

        cap_enrolled = _norm_cap(enrolled.get("cap"))
        if not cap_enrolled:
            logger.warning(
                "[CAP_TRIAL] enrollment for task %s missing valid 'cap' — ingesting nothing",
                task.id,
            )
            return

        refs_enrolled = [
            r for r in (enrolled.get("test_refs") or []) if isinstance(r, str) and r.strip()
        ]
        head_sha_enrolled = (
            enrolled.get("head_sha") if isinstance(enrolled.get("head_sha"), str) else None
        )
        env_fingerprint_enrolled = (
            enrolled.get("env_fingerprint")
            if isinstance(enrolled.get("env_fingerprint"), str) and enrolled.get("env_fingerprint")
            else None
        )

        # (1) Capability name must normalize-equal the enrolled name.
        cap_normalized = _norm_cap(cap)
        if not cap_normalized or cap_normalized != cap_enrolled:
            logger.warning(
                "[CAP_TRIAL] capability mismatch for task %s — "
                "reported=%r normalized=%r enrolled=%r — ingesting nothing",
                task.id,
                cap,
                cap_normalized,
                cap_enrolled,
            )
            await _record_receipt_contradiction("capability_mismatch")
            return

        # (2) The receipt must name exactly the enrolled target set.  A subset
        #     would let a compromised runner omit a hard test, then have its
        #     green aggregate recorded as though the complete suite had run.
        #     Belt-and-suspenders: guard empty refs explicitly.
        reported_refs_set = set(refs)
        enrolled_refs_set = set(refs_enrolled)
        if not refs or reported_refs_set != enrolled_refs_set:
            extra = sorted(reported_refs_set - enrolled_refs_set)
            missing = sorted(enrolled_refs_set - reported_refs_set)
            logger.warning(
                "[CAP_TRIAL] test_refs mismatch for task %s — "
                "extra=%s missing=%s — ingesting nothing",
                task.id,
                extra,
                missing,
            )
            await _record_receipt_contradiction("test_refs_mismatch")
            return

        # (3) Reported head_sha must exactly match the enrolled revision.  In
        #     particular, an empty/missing reported SHA cannot turn an enrolled
        #     revision-bound trial into an unbound proof receipt.
        reported_sha = head_sha if isinstance(head_sha, str) else None
        if head_sha_enrolled and reported_sha != head_sha_enrolled:
            logger.warning(
                "[CAP_TRIAL] head_sha mismatch for task %s — "
                "reported=%s, enrolled=%s — ingesting nothing",
                task.id,
                reported_sha,
                head_sha_enrolled,
            )
            await _record_receipt_contradiction("head_sha_mismatch")
            return

        # --- Enrollment validated — use enrolled values for recording ---
        # Prefer reported_sha (the actual executed revision) over the enrollment-time
        # estimate; fall back to head_sha_enrolled when the agent omits head_sha.
        commit_sha_to_record = reported_sha or head_sha_enrolled
        if capability_proof_trial_is_green(result):
            recorded = record_capability_named_test_execution_proof(
                project,
                cap_enrolled,
                refs_enrolled,
                commit_sha_to_record,
                suite_verifying=True,
                env_fingerprint=env_fingerprint_enrolled,
            )
            if recorded:
                await session.commit()
                logger.info(
                    "[CAP_TRIAL] GREEN trial — recorded proven named_test proof for "
                    "cap=%s (task %s, sha=%s, refs=%s)",
                    cap_enrolled,
                    task.id,
                    commit_sha_to_record,
                    refs_enrolled,
                )
                # The proof is durable before waking waiters. The shared resolver
                # locks a bounded project slice and resumes only all-needs-satisfied
                # tasks; its event wrapper is fail-open so proof ingestion stays
                # authoritative even if an immediate wake cannot run.
                from app.reaper_sweeps import (
                    event_driven_unblock_on_capability_proof,
                )

                await event_driven_unblock_on_capability_proof(task.project_id, session)
            else:
                logger.info(
                    "[CAP_TRIAL] GREEN trial for cap=%s (task %s) but recorder minted "
                    "nothing (empty refs / unnormalizable / vacuous) — no proof",
                    cap_enrolled,
                    task.id,
                )
            return

        # Non-green: NEVER demote on ambiguity (design §7). Surface the
        # contradiction on the task context for the strategist. A definitive RED
        # (tests actually ran and failed, not an infrastructure failure) also
        # REVOKES any stale executed-proof ledger entry so a capability known
        # broken at HEAD cannot keep clearing the enforce gate until the 30-day
        # TTL expires.
        is_definitive_red = (
            bool(result.get("ran"))
            and not bool(result.get("could_not_run"))
            and not bool(result.get("passed"))
        )
        revoked = False
        if is_definitive_red:
            from app.capability_proof import (
                revoke_stale_execution_proof,
            )
            revoked = revoke_stale_execution_proof(project, cap_enrolled)

        ctx = dict(task.context or {})
        ctx["capability_proof_trial_contradiction"] = {
            "cap": cap_enrolled,
            "head_sha": head_sha_enrolled,
            "test_refs": refs_enrolled,
            "ran": bool(result.get("ran")),
            "passed": bool(result.get("passed")),
            "could_not_run": bool(result.get("could_not_run")),
            "proof_revoked": revoked,
            "recorded_at": _utcnow().isoformat(),
        }
        task.context = ctx
        flag_modified(task, "context")
        await session.commit()
        logger.info(
            "[CAP_TRIAL] non-green trial for cap=%s (task %s) — "
            "definitive_red=%s proof_revoked=%s",
            cap_enrolled,
            task.id,
            is_definitive_red,
            revoked,
        )
    except Exception:  # noqa: BLE001 — ingestion must fail open, never raise
        logger.warning(
            "[CAP_TRIAL] ingestion failed for task %s (fail-open — capability left "
            "at existing proof)",
            getattr(task, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[CAP_TRIAL] rollback after ingestion failure also failed",
                exc_info=True,
            )


async def _handle_compute_experiment_completion(
    task: Task, logs: str, session: AsyncSession, *, owns_commit: bool = True
):
    """Gate and persist a compute_experiment task's terminal job result.

    The agent's bespoke compute-experiment handler launches a budgeted,
    auto-torn-down compute job, polls it to terminal state, and emits a
    COMPUTE_RESULT artifact: a JSON object carrying the terminal job
    ``outcome`` plus ``final_cost_usd``/``accrued_cost_usd``. This handler is a
    thin terminal gate (no children, no PR), modelled on ``_handle_perf_completion``:

      * Forward metric gate (registry-resolved 'metric' strategy, made
        compute-shape-aware): an experiment that produced no terminal job
        outcome OR no measured cost must NOT complete clean. Mark it FAILED
        (terminal) — a compute_experiment spawns no children and opens no PR,
        so EVALUATING would hang forever and pin its parent.
      * On pass: store the result on ``task.plan_output`` and mark COMPLETED.
        Budget/deadline kills carry an honest terminal outcome and a cost, so
        they pass the gate and surface through the task as COMPLETED with the
        kill outcome recorded — they are intentional terminal outcomes, not
        crashes.

    Commit ownership (``owns_commit``): every terminal branch funnels through
    :func:`_finalize_compute_experiment`.

      * ``owns_commit=True`` (default — the direct ``_process_post_commit``
        dispatch path): commit the mutation, broadcast, and persist knowledge
        here, as before.
      * ``owns_commit=False`` (the wake-delivery path, called from
        ``task_lifecycle.deliver_wake``): mutate only, no internal commit — the
        caller's single commit covers both the wake FIRED→DELIVERED flip and this
        task advance atomically, and runs ``run_compute_wake_post_commit`` for the
        broadcast + knowledge AFTER it commits.
    """
    verdict = evaluate_terminal_output(task, logs)
    sweep_id = (task.first_payload or {}).get("sweep_id") or None

    # Record the declared success-criterion verdict DURABLY on the task so the
    # operator and strategic consumers can read whether the experiment's goal
    # was actually met — metric/operator/threshold, observed value, met/unmet,
    # and whether it was reported (measurable) — for met, unmet, and
    # not-reported cases alike, independent of the gate's pass/fail. Written
    # before record_failed_gate so the FAIL path preserves it alongside the
    # evaluation_gate record.
    #
    # Pre-load release-echo data from the job row so that release_echo_present /
    # release_echo_verified criteria are always evaluable (numeric 0.0/1.0) even
    # when the experiment command timed out before emitting a COMPUTE_RESULT.
    _echo_sha, _expected_sha = await _load_release_echo_for_criterion(
        session, task, logs
    )
    criterion = evaluate_compute_success_criterion(
        task, logs, echo_sha=_echo_sha, expected_sha=_expected_sha
    )
    if criterion is not None:
        crit_context = dict(task.context or {})
        crit_context["compute_success_criterion"] = criterion
        task.context = crit_context
        flag_modified(task, "context")

    # Fast-path: SMOKE_FAILED must never reach the gate as a "measured" result.
    # _extract_compute_measurement returns ("SMOKE_FAILED", 0.0) for a zero-cost
    # smoke-failed job — which would incorrectly pass the gate.  Intercept here
    # and route to the failure path before the gate logic runs.
    _early_meas = _extract_compute_measurement(logs)
    if _early_meas is not None and _early_meas[0] == "SMOKE_FAILED":
        from app.failure_classification import (  # noqa: PLC0415
            COMPUTE_PREFLIGHT_SMOKE_FAILED,
            persist_failure_category,
        )

        _smoke_reason = (
            "compute_preflight_smoke_failed: command failed pre-flight smoke "
            "on the DB-less substrate — no full VM was provisioned"
        )
        record_failed_gate(
            task,
            EvaluationVerdict(False, _smoke_reason, "compute-preflight-smoke"),
        )
        persist_failure_category(task, COMPUTE_PREFLIGHT_SMOKE_FAILED)
        logger.info(
            "[COMPUTE] Task %s FAILED at pre-flight smoke (no full VM provisioned)",
            task.id,
        )
        await _spawn_act_on_verdict_followon(session, task, {}, logs, failure_mode=True)
        await _finalize_compute_experiment(
            session, task, TaskStatus.FAILED, logs, criterion, owns_commit=owns_commit
        )
        return

    acceptance_evidence = read_acceptance_evidence(task.first_payload)
    requires_durable_artifact = bool(
        (task.first_payload or {}).get("require_durable_artifact")
    ) or (
        acceptance_evidence is not None
        and acceptance_evidence.kind == "downstream_artifact"
    )

    prerequisite_mode = (task.context or {}).get(
        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY
    ) == COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY

    if prerequisite_mode:
        measurement = _extract_compute_measurement(logs)
        if measurement is None:
            record_failed_gate(task, verdict)
            await _stamp_compute_terminal_diagnostics(session, task, logs)
            logger.info(
                "[COMPUTE] Prerequisite-mode task %s produced no measured verdict: %s",
                task.id,
                verdict.reason,
            )
            await _finalize_compute_experiment(
                session,
                task,
                TaskStatus.FAILED,
                logs,
                criterion,
                owns_commit=owns_commit,
            )
            return

        outcome, cost = measurement
        plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
        plan_output = dict(plan_output)
        compute_result: dict[str, Any] = {"outcome": outcome, "final_cost_usd": cost}
        if criterion is not None:
            compute_result["criterion"] = criterion
        # Non-success terminals carry WHY (the job's terminal_reason) so
        # plan_output.compute_result — the durable record downstream prompts
        # read — surfaces the reason, not just the outcome state.
        if outcome != "SUCCESS":
            _term_reason = _compute_terminal_reason_of(None, logs)
            if _term_reason:
                compute_result["terminal_reason"] = _term_reason
        # Interim-progress trajectory (first/last/best + count) when the job
        # self-reported progress records — follow-on agents see the CURVE, not
        # just the terminal number.
        _progress_summary = await _build_compute_progress_summary(
            session, task, logs, criterion
        )
        if _progress_summary is not None:
            compute_result["progress_summary"] = _progress_summary
        resource = await _stamp_compute_result_resource(session, task, logs)
        compute_result.update(resource)
        compute_result["artifact_delivery"] = _derive_artifact_delivery_record(resource)
        plan_output["compute_result"] = compute_result
        task.plan_output = plan_output
        flag_modified(task, "plan_output")
        delivery = compute_result["artifact_delivery"]
        # Tier-2 strict gate: explicit require_durable_artifact and
        # downstream_artifact acceptance evidence both require a durable
        # artifact delivery before the task may complete. Ordinary
        # prerequisites (no flag, no downstream_artifact evidence) stay
        # fail-soft and complete on measurement alone.
        if requires_durable_artifact and not delivery["delivered"]:
            miss_reason = (
                f"require_durable_artifact: no durable artifact delivered "
                f"(durability={delivery['durability']})"
            )
            record_failed_gate(
                task,
                EvaluationVerdict(False, miss_reason, "artifact-delivery"),
            )
            logger.info(
                "[COMPUTE] Prerequisite-mode task %s failed Tier-2 "
                "durable-artifact gate: %s",
                task.id,
                miss_reason,
            )
            await _finalize_compute_experiment(
                session,
                task,
                TaskStatus.FAILED,
                logs,
                criterion,
                owns_commit=owns_commit,
            )
            return
        task.latest_status_message = (
            f"Compute prerequisite measured: outcome={outcome}, final_cost=${cost}"
            f", artifact_delivery={delivery['durability']}"
        )[:500]
        task.status = TaskStatus.COMPLETED
        logger.info(
            "[COMPUTE] Prerequisite-mode task %s completed with measured verdict",
            task.id,
        )
        await _finalize_compute_experiment(
            session,
            task,
            TaskStatus.COMPLETED,
            logs,
            criterion,
            owns_commit=owns_commit,
        )
        return

    if not verdict.passed:
        record_failed_gate(task, verdict)
        await _stamp_compute_terminal_diagnostics(session, task, logs)
        logger.info(
            "[COMPUTE] Task %s did not clear the artifact-delivery gate: %s",
            task.id,
            verdict.reason,
        )
        # Crash-visibility fix: the gate failed before any compute_result was
        # extracted, so the experiment's traceback is otherwise stranded in a
        # GCS bundle. No intentional kill reaches this branch (kills carry an
        # outcome + cost and pass the gate), so always spawn a tracked
        # diagnose-and-fix follow-on. The experiment itself stays FAILED.
        if sweep_id:
            await _maybe_spawn_sweep_aggregate_followon(
                session, task, sweep_id, {}, logs, failure_mode=True
            )
        else:
            await _spawn_act_on_verdict_followon(
                session, task, {}, logs, failure_mode=True
            )
        await _finalize_compute_experiment(
            session,
            task,
            TaskStatus.FAILED,
            logs,
            criterion,
            owns_commit=owns_commit,
        )
        return

    # The gate passed, so a terminal, costed result is present. Re-extract it
    # for display/telemetry (outcome + cost). _extract_compute_measurement is
    # the same parser the gate used, so the two can never disagree.
    measurement = _extract_compute_measurement(logs)
    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    if measurement is not None:
        outcome, cost = measurement
        compute_result: dict = {"outcome": outcome, "final_cost_usd": cost}
        if criterion is not None:
            compute_result["criterion"] = criterion
        # Mirror the prerequisite path: non-success terminals carry WHY so the
        # follow-on spawn and the sweep aggregate prompt (which read
        # plan_output.compute_result) surface the reason, not just the state.
        if outcome != "SUCCESS":
            _term_reason = _compute_terminal_reason_of(None, logs)
            if _term_reason:
                compute_result["terminal_reason"] = _term_reason
        # Mirror the prerequisite path: attach the self-reported progress
        # trajectory so the act-on-verdict follow-on reads the curve too.
        _progress_summary = await _build_compute_progress_summary(
            session, task, logs, criterion
        )
        if _progress_summary is not None:
            compute_result["progress_summary"] = _progress_summary
        resource = await _stamp_compute_result_resource(session, task, logs)
        compute_result.update(resource)
        delivery = _derive_artifact_delivery_record(resource)
        compute_result["artifact_delivery"] = delivery
        plan_output["compute_result"] = compute_result
        task.plan_output = plan_output
        flag_modified(task, "plan_output")
        # Tier 2: strict gate — fail if the task declared it needs a durable
        # artifact, either explicitly or via downstream_artifact evidence.
        if requires_durable_artifact and not delivery["delivered"]:
            miss_reason = (
                f"require_durable_artifact: no durable artifact delivered "
                f"(durability={delivery['durability']})"
            )
            record_failed_gate(
                task,
                EvaluationVerdict(False, miss_reason, "artifact-delivery"),
            )
            logger.info(
                "[COMPUTE] Task %s failed Tier-2 durable-artifact gate: %s",
                task.id,
                miss_reason,
            )
            # Spawn a diagnose-and-fix follow-on (forwarding the partial
            # compute_result + any durable artifact pointer) UNLESS the outcome
            # is an intentional kill — those are honest terminal states, not a
            # defect to investigate. Experiment stays FAILED.
            if outcome not in _COMPUTE_INTENTIONAL_KILL_OUTCOMES:
                if sweep_id:
                    await _maybe_spawn_sweep_aggregate_followon(
                        session, task, sweep_id, compute_result, logs, failure_mode=True
                    )
                else:
                    await _spawn_act_on_verdict_followon(
                        session, task, compute_result, logs, failure_mode=True
                    )
            await _finalize_compute_experiment(
                session,
                task,
                TaskStatus.FAILED,
                logs,
                criterion,
                owns_commit=owns_commit,
            )
            return
        # LOUD: a declared kind=metric success_criterion that produced no numeric
        # value must terminate as a measurable FAILURE — never complete clean as
        # the misleading 'outcome=SUCCESS / goal missed (value=null)'. The
        # criterion evidence is already recorded on plan_output above. Excludes
        # kind=artifact_produced (gated on durable_file_count) and intentional
        # budget/deadline/cancel kills (those stay exactly as today). A
        # measured-but-missed criterion is evidence, not this failure.
        if (
            _is_unmeasured_declared_metric(task, criterion)
            and outcome not in _COMPUTE_INTENTIONAL_KILL_OUTCOMES
        ):
            from app.failure_classification import (  # noqa: PLC0415
                COMPUTE_UNMEASURED_CRITERION,
                persist_failure_category,
            )

            metric_name = (criterion or {}).get("metric")
            emitted_diag = describe_emitted_compute_metric_keys(logs)
            miss_reason = (
                f"compute_unmeasured_criterion: declared metric {metric_name!r} "
                f"produced no numeric value (outcome={outcome}); the experiment "
                f"ran but the declared hypothesis was never measured — {emitted_diag}"
            )
            record_failed_gate(
                task,
                EvaluationVerdict(False, miss_reason, "compute-unmeasured-criterion"),
            )
            persist_failure_category(task, COMPUTE_UNMEASURED_CRITERION)
            logger.info(
                "[COMPUTE] Task %s failed loud (unmeasured declared metric): %s",
                task.id,
                miss_reason,
            )
            # Crash-visibility fix: a SUCCESS/FAILURE outcome whose declared metric
            # never materialized is the classic "ran but crashed before emitting
            # the number" case. Intentional kills are already excluded by the
            # guard above, so spawn a tracked diagnose-and-fix follow-on
            # (forwarding the partial result + artifact pointer). Experiment stays
            # FAILED.
            if sweep_id:
                await _maybe_spawn_sweep_aggregate_followon(
                    session, task, sweep_id, compute_result, logs, failure_mode=True
                )
            else:
                await _spawn_act_on_verdict_followon(
                    session, task, compute_result, logs, failure_mode=True
                )
            await _finalize_compute_experiment(
                session,
                task,
                TaskStatus.FAILED,
                logs,
                criterion,
                owns_commit=owns_commit,
            )
            return
        task.latest_status_message = (
            f"Compute experiment terminal: outcome={outcome}, "
            f"final_cost=${cost}, artifact_delivery={delivery['durability']}"
        )[:500]
    else:  # pragma: no cover - defensive; the gate guarantees a measurement
        task.latest_status_message = "Compute experiment completed"
    task.plan_output = plan_output
    flag_modified(task, "plan_output")

    # Always-dispose (axis B): EVERY measured, non-prerequisite compute
    # experiment now spawns ONE write-capable reason-and-ship follow-on over its
    # faithful result; the parent advances to EVALUATING to await it (mirroring
    # the security_audit completion→spawn-create pattern). The authoring-time
    # ``act_on_verdict`` opt-in is retired — disposition is decided here, not at
    # spawn. The spawn is concurrency-bounded
    # (``max_concurrent_act_on_verdict_followons``): when the cap is reached no
    # child spawns and the experiment completes clean, so the dispose is
    # best-effort, never a hard blocker. Faithful capture (axis A) always runs.
    #
    # Intentional kills (budget/deadline/cancel) are EXCLUDED: they are honest,
    # intended terminal outcomes whose experiment never produced a result to
    # reason over, so there is nothing to dispose — exactly as the failure
    # branches above exclude them. They complete clean with full capture.
    #
    # spawn_followup gate: operator-authored tasks (created_by="USER") suppress
    # the follow-on by default (opt-in via spawn_followup=True in the payload).
    # Engine-authored tasks (ORCHESTRATOR / STRATEGIC_AGENT) spawn by default
    # unless spawn_followup=False is explicitly set. This prevents validation
    # smokes from burning agent budget on throwaway metrics.
    _payload_spawn_followup = (task.first_payload or {}).get("spawn_followup")
    _should_spawn_followup = (
        _payload_spawn_followup is True
        if task.created_by == "USER"
        else _payload_spawn_followup is not False
    )
    final_status = TaskStatus.COMPLETED
    result_outcome = (plan_output.get("compute_result") or {}).get("outcome")
    if (
        result_outcome not in _COMPUTE_INTENTIONAL_KILL_OUTCOMES
        and _should_spawn_followup
    ):
        if sweep_id:
            child = await _maybe_spawn_sweep_aggregate_followon(
                session, task, sweep_id, plan_output.get("compute_result") or {}, logs
            )
        else:
            child = await _spawn_act_on_verdict_followon(
                session, task, plan_output.get("compute_result") or {}, logs
            )
        if child is not None:
            final_status = TaskStatus.EVALUATING
            task.latest_status_message = (
                f"Compute experiment terminal; spawned reason-and-ship "
                f"follow-on {child.id}"
            )[:500]

    task.status = final_status
    logger.info(
        "[COMPUTE] Task %s delivered a compute artifact (status=%s)",
        task.id,
        final_status,
    )
    # Persist the measured verdict as durable, queryable knowledge so future
    # research/strategist cycles can discover that this experiment already ran
    # and what it found, the same way web_research/design results survive.
    await _finalize_compute_experiment(
        session,
        task,
        final_status,
        logs,
        criterion,
        owns_commit=owns_commit,
    )


# ---------------------------------------------------------------------------
# Panel-review completion — persist a structured verdict artifact, no PR, no children
# ---------------------------------------------------------------------------


_PANEL_REVIEW_REQUIRED_LENSES = (
    "correctness",
    "security",
    "code_quality",
    "test_coverage",
)
_PANEL_REVIEW_ARTIFACT_TYPE = "PANEL_REVIEW_VERDICT"


def _normalize_panel_review_verdict(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a panel-review verdict.

    ``overall_verdict`` (APPROVE / REQUEST_CHANGES) is the only required field.
    ``lenses`` is optional telemetry: when a well-formed lenses object is present
    it is captured and preserved; when missing or malformed the overall verdict
    resolution still proceeds so a correctly-decided review is never discarded on
    a structural check for a field nothing downstream consumes.
    """
    overall_verdict = str(raw.get("overall_verdict") or "").strip()
    if overall_verdict not in {"APPROVE", "REQUEST_CHANGES"}:
        return None

    raw_lenses = raw.get("lenses")
    lens_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_lenses, dict):
        for name, payload in raw_lenses.items():
            if isinstance(name, str) and isinstance(payload, dict):
                lens_map[name] = payload
    elif isinstance(raw_lenses, list):
        for item in raw_lenses:
            if not isinstance(item, dict):
                continue
            name = item.get("lens") or item.get("name") or item.get("id")
            if isinstance(name, str):
                lens_map[name] = item

    # Normalize per-lens telemetry where available; skip lenses that are
    # missing or malformed — never gate the overall verdict on their presence.
    normalized_lenses: Dict[str, Dict[str, Any]] = {}
    for lens in _PANEL_REVIEW_REQUIRED_LENSES:
        payload = lens_map.get(lens)
        if not isinstance(payload, dict):
            continue
        lens_verdict = str(payload.get("verdict") or "").strip()
        if not lens_verdict:
            continue
        summary = str(
            payload.get("summary")
            or payload.get("feedback")
            or payload.get("reasoning")
            or ""
        ).strip()
        if not summary:
            continue
        issues = payload.get("issues")
        normalized_lenses[lens] = {
            "verdict": lens_verdict,
            "summary": summary,
            "issues": issues if isinstance(issues, list) else [],
        }

    normalized: Dict[str, Any] = {
        "overall_verdict": overall_verdict,
        "summary": str(raw.get("summary") or raw.get("feedback") or "").strip(),
    }
    if normalized_lenses:
        normalized["lenses"] = normalized_lenses
    overall_issues = raw.get("issues")
    if isinstance(overall_issues, list):
        normalized["issues"] = overall_issues
    investigated_files = raw.get("investigated_files")
    if isinstance(investigated_files, list):
        normalized["investigated_files"] = [
            str(path).strip() for path in investigated_files if str(path).strip()
        ]
    return normalized


def _parse_panel_review_output(text: str | None) -> Optional[Dict[str, Any]]:
    """Best-effort extract the structured panel-review verdict from agent output.

    Uses the same three-method cascade as the primary review path so the two
    paths cannot diverge in strictness:

    Method 1 — top-level JSON with ``overall_verdict``.
    Method 2 — balanced-bracket JSON scan for ``overall_verdict`` or ``lenses``
               embedded in prose.
    Method 3 — anchored-prose floor via ``_resolve_fallback_verdict`` (same
               safety gate as the primary review); returns None only when no
               APPROVE/REQUEST_CHANGES signal is present anywhere.
    """
    if not text:
        return None
    snippet = text[:500_000]
    candidates: List[str] = []
    try:
        parsed = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "overall_verdict" in parsed:
        candidates.append(snippet)

    # Scan for embedded JSON objects carrying either key.
    for search_key in ("overall_verdict", "lenses"):
        candidate = _extract_json_object(snippet, search_key)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate_text in candidates:
        try:
            obj = json.loads(candidate_text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            normalized = _normalize_panel_review_verdict(obj)
            if normalized is not None:
                return normalized

    # Method 3: anchored-prose floor — the same safety floor the primary review
    # uses. Returns None (→ fail-closed escalation) when no anchored
    # APPROVE/REQUEST_CHANGES signal is present, so genuinely absent verdicts
    # still escalate to manual review.
    from app.task_lifecycle import _resolve_fallback_verdict  # noqa: PLC0415

    verdict = _resolve_fallback_verdict(snippet)
    if verdict is not None:
        return {"overall_verdict": verdict, "summary": ""}
    return None


# ── Review second-opinion reconciliation ──────────────────────────────────
# Context key on the REVIEW task holding the durable reconciliation record of a
# primary verdict against its independent panel-review challenge. Exactly one of
# four states (see ``reconcile_review_second_opinion``):
#   unchallenged          gate off / not high-stakes (recorded inline, never here)
#   challenged_confirmed  challenge upholds the verdict AND the surviving issues
#   challenged_revised    same verdict direction, surviving issue set changed
#   challenged_rejected   challenge overturns the verdict direction
REVIEW_SECOND_OPINION_RECONCILIATION_KEY = "review_second_opinion"

# The whole second-opinion key family is task-local state on a single review
# task (or its challenger). None of it may ride the review→fix→review context
# copy in review_pipeline._spawn_pipeline_task: an inherited reconciliation
# record fires the single-shot re-entry override on the NEXT review task,
# clobbering its fresh verdict with iteration-1's verdict + issues — the
# identical issue set then trips the stuck-cycle detector and falsely parks
# the chain (PR #9243). Spawn paths that copy context must exclude these.
REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS = (
    REVIEW_SECOND_OPINION_RECONCILIATION_KEY,
    REVIEW_SECOND_OPINION_AWAITING_KEY,
    REVIEW_SECOND_OPINION_PARKED_KEY,
    REVIEW_SECOND_OPINION_SOURCE_KEY,
    BLIND_REVIEW_SOURCE_KEY,
)


def _review_issue_signature(issue: Any) -> str:
    """Stable identity for a review issue, for set comparison across reviewers."""
    if not isinstance(issue, dict):
        return str(issue).strip().lower()
    desc = str(issue.get("description") or "").strip().lower()
    file = str(issue.get("file") or "").strip().lower()
    line = str(issue.get("line") or "").strip()
    return f"{file}:{line}:{desc}"


def reconcile_review_second_opinion(
    primary_verdict: str,
    primary_issues: List[Dict[str, Any]] | None,
    panel: Dict[str, Any],
) -> Dict[str, Any]:
    """Reconcile a primary review verdict against a normalized panel challenge.

    ``panel`` is the output of :func:`_parse_panel_review_output` (already
    validated to carry ``overall_verdict`` ∈ {APPROVE, REQUEST_CHANGES}).

    Returns a record with exactly one of three challenge states
    (``challenged_confirmed`` / ``challenged_revised`` / ``challenged_rejected``
    — the fourth, ``unchallenged``, is recorded inline by the gate and never
    reaches this function) plus the reconciled verdict and the surviving /
    agreed issue set downstream consumers act on.
    """
    primary_verdict = (
        primary_verdict
        if primary_verdict in ("APPROVE", "REQUEST_CHANGES")
        else "REQUEST_CHANGES"
    )
    challenge_verdict = panel.get("overall_verdict")
    if challenge_verdict not in ("APPROVE", "REQUEST_CHANGES"):
        # Defensive: parser guarantees one of the two, but fail toward
        # confirming the primary rather than inventing an overturn.
        challenge_verdict = primary_verdict
    challenge_issues = panel.get("issues")
    challenge_issues = challenge_issues if isinstance(challenge_issues, list) else []

    if challenge_verdict != primary_verdict:
        # Challenge overturns the verdict direction.
        state = "challenged_rejected"
        reconciled_verdict = challenge_verdict
        surviving = challenge_issues if challenge_verdict == "REQUEST_CHANGES" else []
    elif primary_verdict == "APPROVE":
        # Both APPROVE — no blocking issue set to compare.
        state = "challenged_confirmed"
        reconciled_verdict = "APPROVE"
        surviving = []
    else:
        # Both REQUEST_CHANGES — compare the surviving issue set.
        primary_sigs = {_review_issue_signature(i) for i in (primary_issues or [])}
        challenge_sigs = {_review_issue_signature(i) for i in challenge_issues}
        reconciled_verdict = "REQUEST_CHANGES"
        if primary_sigs == challenge_sigs:
            state = "challenged_confirmed"
            surviving = list(primary_issues or [])
        else:
            # Surviving set changed: keep the primary issues the challenge still
            # upholds (intersection); fall back to the challenge's own set, then
            # the primary's, so the fix is never spawned with an empty issue set.
            agreed = [
                i
                for i in (primary_issues or [])
                if _review_issue_signature(i) in challenge_sigs
            ]
            surviving = agreed or challenge_issues or list(primary_issues or [])
            state = "challenged_revised"

    return {
        "state": state,
        "primary_verdict": primary_verdict,
        "challenge_verdict": challenge_verdict,
        "reconciled_verdict": reconciled_verdict,
        "surviving_issues": surviving,
        "challenge_summary": str(panel.get("summary") or "")[:2000],
    }


async def _handle_panel_review_completion(task: Task, logs: str, session: AsyncSession):
    """Persist a terminal structured verdict for ``panel_review`` tasks."""
    from app.models import Artifact  # noqa: PLC0415

    verdict = evaluate_terminal_output(task, logs)
    if not verdict.passed:
        record_failed_gate(task, verdict)
        session.add(task)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            "[PANEL_REVIEW] Task %s did not clear the structured-verdict gate: %s",
            task.id,
            verdict.reason,
        )
        return

    parsed = _parse_panel_review_output(logs)
    if parsed is None:
        record_failed_gate(
            task,
            EvaluationVerdict(
                False,
                "structured verdict output not parseable after gate",
                "structured-verdict",
            ),
        )
        session.add(task)
        await session.commit()
        return

    artifact = Artifact(
        task_id=task.id,
        artifact_type=_PANEL_REVIEW_ARTIFACT_TYPE,
        logs=json.dumps(parsed),
    )
    stamp_artifact_logical_key(artifact, task)
    session.add(artifact)

    task.plan_output = parsed
    flag_modified(task, "plan_output")
    task.status = TaskStatus.COMPLETED
    task.latest_status_message = (
        f"Panel review verdict recorded ({parsed['overall_verdict']}; "
        f"{len(parsed.get('lenses') or {})} lenses)"
    )
    session.add(task)
    await session.commit()
    await session.refresh(artifact)

    await deliver_artifact_to_dependents(task, artifact, session, commit=True)

    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.COMPLETED},
    )
    logger.info("[PANEL_REVIEW] Persisted verdict artifact for task %s", task.id)

    # Surface the verdict on the PR (and raise an operator signal for
    # REQUEST_CHANGES). Best-effort — the durable artifact and COMPLETED
    # status above are the source of truth and must never be undone by a
    # transient posting failure.
    await _post_panel_review_to_pr(task, parsed, session)


def _build_panel_review_pr_body(parsed: Dict[str, Any]) -> str:
    """Render a panel-review verdict into a Markdown PR-review body.

    Clearly labelled "Blueprint Panel Review" so it does not read as a
    duplicate of the ordinary single-reviewer "Blueprint Code Review"
    posted by the ``review`` task type.
    """

    def _render_issues(issues: List[Any]) -> List[str]:
        rendered: List[str] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            sev = str(issue.get("severity", "minor")).upper()
            desc = issue.get("description") or issue.get("summary") or ""
            loc = ""
            if issue.get("file"):
                loc = f" (`{issue['file']}`"
                if issue.get("line"):
                    loc += f":{issue['line']}"
                loc += ")"
            rendered.append(f"- [{sev}] {desc}{loc}")
        return rendered

    overall = parsed.get("overall_verdict", "")
    lines: List[str] = [f"## Blueprint Panel Review — {overall}", ""]
    summary = str(parsed.get("summary") or "").strip()
    if summary:
        lines.extend([summary, ""])

    lenses = parsed.get("lenses") or {}
    for lens, data in lenses.items():
        if not isinstance(data, dict):
            continue
        label = lens.replace("_", " ").title()
        lines.append(f"### {label} — {data.get('verdict', '')}")
        lens_summary = str(data.get("summary") or "").strip()
        if lens_summary:
            lines.append(lens_summary)
        lines.extend(_render_issues(data.get("issues") or []))
        lines.append("")

    top_issues = parsed.get("issues") or []
    if top_issues:
        lines.append("### Cross-cutting issues")
        lines.extend(_render_issues(top_issues))
        lines.append("")

    return "\n".join(lines).strip()


async def _post_panel_review_to_pr(
    task: Task, parsed: Dict[str, Any], session: AsyncSession
) -> None:
    """Best-effort: surface a completed panel-review verdict on its PR.

    Posts the verdict as a real GitHub review (parity with the ordinary
    ``review`` task type, reusing ``submit_pr_review``) and, for a
    REQUEST_CHANGES overall verdict, raises an operator ``Alert`` so a
    blocking panel outcome is visible rather than an indistinguishable
    COMPLETED. Every failure is swallowed and logged — the durable
    artifact and COMPLETED status are authoritative and must survive any
    GitHub/posting error.
    """
    from app.models import Alert, AlertSeverity  # noqa: PLC0415

    # Greenlet-safe PR-number fetch. ``task.first_payload`` reads
    # ``task.payloads``, which is a deferred-loadable column in some dispatch
    # paths (recovery sweep / load_only projections); touching it implicitly
    # here would trigger lazy IO outside a greenlet and raise MissingGreenlet
    # (which previously crashed the reaper). An explicit awaited query is
    # safe regardless of how the task was loaded.
    try:
        row = (
            await session.execute(select(Task.payloads).where(Task.id == task.id))
        ).first()
    except Exception as exc:  # noqa: BLE001 — best-effort surfacing
        logger.warning(
            "[PANEL_REVIEW] could not load payload for task %s: %s", task.id, exc
        )
        return

    payloads = row[0] if row else None
    first = (payloads or [{}])[0] if isinstance(payloads, list) and payloads else {}
    pr_number = first.get("pr_number") if isinstance(first, dict) else None
    if not pr_number:
        logger.info(
            "[PANEL_REVIEW] task %s has no pr_number; skipping PR post", task.id
        )
        return

    overall = parsed.get("overall_verdict")
    event = "APPROVE" if overall == "APPROVE" else "REQUEST_CHANGES"

    # Split located issues into inline comments vs the review body, mirroring
    # the `review` task type's posting path. Both per-lens issues and any
    # top-level issues are considered.
    all_issues: List[Dict[str, Any]] = []
    for lens_data in (parsed.get("lenses") or {}).values():
        if isinstance(lens_data, dict):
            all_issues.extend(
                i for i in (lens_data.get("issues") or []) if isinstance(i, dict)
            )
    all_issues.extend(i for i in (parsed.get("issues") or []) if isinstance(i, dict))
    inline_comments: List[Dict[str, Any]] = []
    for issue in all_issues:
        if issue.get("file") and issue.get("line"):
            sev = str(issue.get("severity", "minor")).upper()
            desc = issue.get("description") or issue.get("summary") or ""
            inline_comments.append(
                {
                    "path": issue["file"],
                    "line": issue["line"],
                    "body": f"[{sev}] {desc}",
                }
            )

    body = _build_panel_review_pr_body(parsed)

    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        await submit_pr_review(
            gh_token, owner, repo, int(pr_number), event, body, inline_comments
        )
        logger.info(
            "[PANEL_REVIEW] posted %s review to PR #%s for task %s",
            event,
            pr_number,
            task.id,
        )
    except Exception as exc:  # noqa: BLE001 — posting is best-effort
        logger.warning(
            "[PANEL_REVIEW] failed to post review to PR #%s for task %s: %s",
            pr_number,
            task.id,
            exc,
        )

    if event != "REQUEST_CHANGES":
        return

    # REQUEST_CHANGES → visible, actionable operator signal (parity with other
    # blocking review outcomes, which raise a WARNING Alert).
    try:
        alert = Alert(
            project_id=task.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Panel review requested changes on PR #{pr_number}",
            message=(
                f"The multi-lens panel review for PR #{pr_number} returned "
                "REQUEST_CHANGES. Review the per-lens findings in the task "
                "detail drawer and on the PR before merging."
            ),
            task_id=task.id,
            event_type="review.escalated",
        )
        session.add(alert)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": "WARNING",
                "title": alert.title,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surfacing is best-effort
        logger.warning(
            "[PANEL_REVIEW] failed to raise REQUEST_CHANGES alert for task %s: %s",
            task.id,
            exc,
        )


# ---------------------------------------------------------------------------
# Memo completion — persist a terminal reasoning artifact, no PR, no children
# ---------------------------------------------------------------------------


_MEMO_REQUIRED_KEYS = ("summary", "reasoning", "citations", "tags")


def _parse_memo_output(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extract the structured memo object from agent ``logs``.

    The agent's contract is to emit a JSON object with the keys
    ``summary``, ``reasoning``, ``citations`` (list[str]), ``tags``
    (list[str]). We accept either a bare JSON object as the entire
    payload, or an object embedded in surrounding text.
    """
    if not text:
        return None
    snippet = text[:500_000]
    candidates: List[str] = []
    # Direct decode — common case where the agent passes a clean JSON
    # string up as the artifact body. Only accept the bare decode when
    # the required schema keys are present; otherwise fall through to
    # the scan so we don't surface unrelated objects as memos.
    try:
        parsed = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "summary" in parsed and "reasoning" in parsed:
        candidates.append(snippet)

    # Fallback: scan for a balanced JSON object containing "summary"
    # and "reasoning" keys.
    if not candidates:
        candidate = _extract_json_object(snippet, "summary", "reasoning")
        if candidate:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _normalize_memo(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce raw memo fields into a stable shape for persistence."""
    summary = obj.get("summary")
    reasoning = obj.get("reasoning")
    citations = obj.get("citations") or []
    tags = obj.get("tags") or []
    if not isinstance(citations, list):
        citations = []
    if not isinstance(tags, list):
        tags = []
    return {
        "summary": str(summary) if summary is not None else "",
        "reasoning": str(reasoning) if reasoning is not None else "",
        "citations": [str(c) for c in citations[:50]],
        "tags": [str(t) for t in tags[:20]],
    }


def _apply_synthesized_memo(
    memo_task: Task,
    memo: Dict[str, Any],
    session: AsyncSession,
) -> None:
    """Persist a synthesized memo artifact and mark the task COMPLETED immediately.

    Called at spawn time for dispatcher memos whose full reasoning is already
    known — avoids provisioning a VM solely to reformat known text.
    """
    from app.models import Artifact, ArtifactType  # noqa: PLC0415

    normalized = _normalize_memo(memo)
    artifact = Artifact(
        task_id=memo_task.id,
        artifact_type=ArtifactType.MEMO,
        logs=json.dumps(normalized),
    )
    stamp_artifact_logical_key(artifact, memo_task)
    session.add(artifact)
    po = dict(memo_task.plan_output) if isinstance(memo_task.plan_output, dict) else {}
    po["memo"] = normalized
    memo_task.plan_output = po
    flag_modified(memo_task, "plan_output")
    memo_task.vm_instance_id = None
    memo_task.vm_provisioned_at = None
    memo_task.status = TaskStatus.COMPLETED
    memo_task.latest_status_message = (
        f"Memo recorded ({len(normalized['citations'])} citation(s), "
        f"{len(normalized['tags'])} tag(s))"
    )


async def _handle_memo_completion(task: Task, logs: str, session: AsyncSession):
    """Persist a memo task's reasoning artifact.

    Memo tasks are terminal — they produce a single ``Artifact`` row of
    type ``MEMO`` and do NOT spawn child tasks or open a PR. Strategic
    review and downstream reasoning consumers can cite the artifact by id.

    Behaviour:
      * On parseable output: create one ``Artifact`` (artifact_type=MEMO,
        task_id=task.id, logs=serialized JSON), mirror the parsed object
        into ``task.plan_output['memo']``, and mark the task ``COMPLETED``.
        Memo tasks spawn no children and open no PR, so no lifecycle path
        promotes a childless, PR-less ``EVALUATING`` memo to ``COMPLETED`` —
        it must be marked terminal here directly, like the other terminal
        read-only handlers. ``query_recent_memo_citations`` reads only
        ``COMPLETED`` memos, so this is also the reader/writer contract.
      * On unparseable output: leave the task without an artifact, set
        ``latest_status_message`` so the operator sees why, and mark it
        ``COMPLETED`` (terminal, uncitable) rather than raising — failing
        here would re-queue an inherently invalid agent output.
    """
    # Local import keeps the module-level import surface stable and
    # avoids pulling Artifact into the strategic-completion re-export
    # chain (other handlers use the same lazy-import pattern).
    from app.models import Artifact, ArtifactType  # noqa: PLC0415

    # Forward evaluation gate (registry-resolved 'forward-quality' strategy):
    # an unparseable or empty memo must NOT complete clean. Marking it FAILED
    # (terminal) is the honest outcome — a memo spawns no children and opens no
    # PR, so EVALUATING would hang forever. The FAILED-memo error-log recovery
    # in task_lifecycle still gets a chance on the FAILED payload path; this
    # gate only catches the case where the COMPLETED-reported output is itself
    # not a valid memo.
    verdict = evaluate_terminal_output(task, logs)
    if not verdict.passed:
        record_failed_gate(task, verdict)
        session.add(task)
        await session.commit()
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            "[MEMO] Task %s did not clear the forward-quality gate: %s",
            task.id,
            verdict.reason,
        )
        return

    parsed = _parse_memo_output(logs)
    if parsed is None:
        # Defensive: the gate passed, so this should be unreachable. Treat any
        # residual parse failure as a gate failure rather than completing clean.
        record_failed_gate(
            task,
            EvaluationVerdict(
                False, "memo output not parseable after gate", "forward-quality"
            ),
        )
        session.add(task)
        await session.commit()
        return

    memo = _normalize_memo(parsed)
    serialized = json.dumps(memo)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.MEMO,
        logs=serialized,
    )
    stamp_artifact_logical_key(artifact, task)
    session.add(artifact)

    plan_output = task.plan_output if isinstance(task.plan_output, dict) else {}
    plan_output = dict(plan_output)
    plan_output["memo"] = memo
    task.plan_output = plan_output
    flag_modified(task, "plan_output")
    task.status = TaskStatus.COMPLETED
    task.latest_status_message = (
        f"Memo recorded ({len(memo['citations'])} citation(s), "
        f"{len(memo['tags'])} tag(s))"
    )
    session.add(task)
    await session.commit()
    await session.refresh(artifact)

    await deliver_artifact_to_dependents(task, artifact, session, commit=True)

    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.COMPLETED},
    )
    logger.info(f"[MEMO] Persisted memo artifact {artifact.id} for task {task.id}")


_RECOVERED_AGENT_MESSAGE_MARKER = "--- recovered-agent-message-text ---"


async def _try_recover_memo_from_parse_failure(
    task: Task, logs: str, session: AsyncSession
) -> bool:
    """Scan a JSONL error log for a valid memo JSON buried in an agent_message envelope.

    When a memo task fails with failure_category='parse_failed' the Codex agent
    may have already emitted a schema-valid memo JSON inside an item.completed /
    item.updated event that the deployed agent handler failed to extract.  Tries
    three strategies in order:

    1. ``--- recovered-agent-message-text ---`` marker blocks appended by the
       agent handler's failure path — each block contains the full raw text of a
       candidate agent_message, which may be longer than what fits in the truncated
       JSONL tail.
    2. Line-by-line JSONL scan for item.completed / item.updated agent_message
       envelopes (original strategy, handles the intact-line case).
    3. Escaped-JSON fallback: if the logs contain ``\\"summary\\"`` and
       ``\\"reasoning\\"`` (the escaped forms that appear when a JSONL line is
       only partially captured), unescape the substring following the nearest
       ``"text":"`` and call ``_parse_memo_output`` on the result.

    Returns True on successful recovery, False if no valid memo is found (leaving
    the task status unchanged so the caller can decide how to proceed).
    """
    if not logs:
        return False

    # Strategy 1: recovered-agent-message-text marker blocks.
    if _RECOVERED_AGENT_MESSAGE_MARKER in logs:
        parts = logs.split(_RECOVERED_AGENT_MESSAGE_MARKER)
        for part in parts[1:]:  # skip the content before the first marker
            # Text up to the next '---' marker or end of string
            next_sep = part.find("\n---")
            candidate_text = part[:next_sep].strip() if next_sep != -1 else part.strip()
            if not candidate_text:
                continue
            parsed = _parse_memo_output(candidate_text)
            if parsed is not None and all(k in parsed for k in _MEMO_REQUIRED_KEYS):
                logger.info(
                    "[MEMO] Recovery: found valid memo JSON in recovered-agent-message-text "
                    "block for task %s",
                    task.id,
                )
                await _handle_memo_completion(task, candidate_text, session)
                return True

    # Strategy 2: line-by-line JSONL scan (original path).
    for raw_line in logs.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(event, dict):
            continue
        if event.get("type") not in ("item.completed", "item.updated"):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue

        # text may be a plain string or a list of content blocks
        raw_text = item.get("text") or item.get("content")
        if isinstance(raw_text, list):
            text = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw_text
            )
        elif isinstance(raw_text, str):
            text = raw_text
        else:
            continue

        parsed = _parse_memo_output(text)
        if parsed is None:
            continue
        if not all(k in parsed for k in _MEMO_REQUIRED_KEYS):
            continue

        logger.info(
            "[MEMO] Recovery: found valid memo JSON in agent_message envelope for task %s",
            task.id,
        )
        await _handle_memo_completion(task, text, session)
        return True

    # Strategy 3: escaped-JSON fallback for truncated JSONL tails.
    # The escaped forms \"summary\" and \"reasoning\" appear when the JSONL line
    # was cut and only the interior of the JSON-escaped text value is visible.
    if '\\"summary\\"' in logs and '\\"reasoning\\"' in logs:
        text_key = '"text":"'
        idx = logs.rfind(text_key)
        if idx != -1:
            raw_escaped = logs[idx + len(text_key) :]
            # Unescape JSON string escapes so _parse_memo_output can find the object.
            unescaped = (
                raw_escaped.replace('\\"', '"')
                .replace("\\\\", "\\")
                .replace("\\n", "\n")
            )
            parsed = _parse_memo_output(unescaped)
            if parsed is not None and all(k in parsed for k in _MEMO_REQUIRED_KEYS):
                logger.info(
                    "[MEMO] Recovery: found valid memo JSON via escaped-tail fallback "
                    "for task %s",
                    task.id,
                )
                await _handle_memo_completion(task, unescaped, session)
                return True

    return False


# ---------------------------------------------------------------------------
# Self audit completion — moved to task_completion_self_audit.
# Importers should keep using `from app.task_completion import ...`; the
# re-export block at the bottom of this module preserves the facade.
# ---------------------------------------------------------------------------
from app.task_completion_self_audit import (  # noqa: E402,F401
    _AUDIT_CLEARABLE_MODES,
    _MODE_STRICTNESS,
    _PROPAGATION_RECORD_MAX_ENTRIES,
    _SELF_AUDIT_COMPONENT_KEYS,
    _SELF_AUDIT_PROPAGATED_MODES,
    _SHIP_RECOVERY_THRESHOLD,
    _VALID_SELF_AUDIT_MODES,
    SELF_AUDIT_PROPAGATION_RECORD_KEY,
    _handle_self_audit_completion,
    _is_recent_strategic_mode_lock,
    _maybe_auto_clear_direction_mode,
    _normalize_self_audit_finding,
    _parse_self_audit_findings,
)


async def _handle_design_doc_completion(
    task: Task,
    logs: str,
    session: AsyncSession,
    *,
    project: Project | None = None,
):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.

    Phase C3 / Round 3 C3 + C4: callers in the post-commit chain pre-acquire
    the project lock at the top of ``_process_post_commit`` via the
    canonical pair-locker ``lock_project_then_task`` (Project before Task)
    and pass the locked Project as ``project=`` so this handler does NOT
    re-acquire. When ``project=None`` (legacy / direct callers), we fall
    back to acquiring the lock here ourselves via
    ``lock_project_for_strategic_transition`` with the same bounded-retry
    semantics. The canonical order is Project before Task — see
    ``docs/concurrency.md``.
    """
    # Deferred imports to avoid circular dependency
    from app.strategic_spawning import _spawn_strategic_review  # noqa: PLC0415
    from app.strategic_spawning import (  # noqa: PLC0415
        STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY,
    )
    from app.alert_dispatch import _create_strategic_alert  # noqa: PLC0415
    from app.task_locking import lock_project_for_strategic_transition  # noqa: PLC0415
    from app.strategic_events import emit_strategic_cycle_completed  # noqa: PLC0415

    # Phase 1 emitted strategic_cycle_started at spawn time. Every path out of
    # this handler that does NOT chain to Phase 2 must pair it with a
    # strategic_cycle_completed so the frontend "Strategist working…" indicator
    # never sticks on. Only a successful _spawn_strategic_review (which emits its
    # own Phase-2 started) is exempt — that keeps the cycle visibly active.
    phase2_spawned = False
    phase1_cycle_id = task.strategic_cycle_id or (task.context or {}).get(
        "strategic_cycle_id"
    )
    # Pre-capture identifiers while the instance is known-loaded. A failed
    # _spawn_strategic_review can roll back / expire the Task instance, and a
    # lazy-load of task.id inside the except block raises MissingGreenlet —
    # which REPLACES chain_exc, masks the real failure, skips the retry
    # marker, and fails the whole completion (task a9bf1317, 2026-06-11).
    task_id_str = str(task.id)
    task_id_value = task.id
    task_project_id = task.project_id

    if project is None:
        # Legacy / direct-caller path: acquire the lock here. Post-commit
        # chain callers (task_lifecycle._process_post_commit) are expected
        # to pass ``project=`` to share the single acquisition across the
        # strategic-completion chain — see Phase C3 in the audit plan.
        project = await lock_project_for_strategic_transition(session, task.project_id)
    if project is None:
        # Distinguish "missing" from "held past retries" — the helper
        # returns None for both. We re-probe non-lockingly to choose the
        # right alert message; a missing project is a hard error, while
        # contention defers to the next cycle.
        exists = (
            await session.execute(
                select(Project.id).where(Project.id == task.project_id)
            )
        ).first()
        if exists is None:
            logger.error(
                f"[DESIGN DOC] Task {task.id}: project {task.project_id} not found"
            )
            await _create_strategic_alert(
                task.project_id,
                session,
                f"Design doc update failed: project not found (task {str(task.id)[:8]})",
            )
            # Phase 1 ended without chaining — close the cycle for the UI.
            await emit_strategic_cycle_completed(
                task.project_id,
                task.id,
                1,
                cycle_id=phase1_cycle_id,
                phase_state="failed",
                completion={
                    "status": "failed",
                    "failure_reason": "project not found",
                },
            )
            return
        logger.warning(
            f"[DESIGN DOC] Task {task.id}: project {task.project_id} held under contention "
            f"after retries; deferring strategic chain"
        )
        await _create_strategic_alert(
            task.project_id,
            session,
            f"Design doc update: project row contended (task {str(task.id)[:8]}) — strategic chain deferred",
        )
        # Phase 1 ended without chaining — close the cycle for the UI.
        await emit_strategic_cycle_completed(
            task.project_id,
            task.id,
            1,
            cycle_id=phase1_cycle_id,
            phase_state="deferred",
            completion={
                "status": "deferred",
                "status_message": "strategic chain deferred: project row contended",
            },
        )
        return

    doc = project.design_doc or {}
    rev = doc.get("_meta", {}).get("revision", 0)
    page_count = sum(1 for k in doc if not k.startswith("_"))
    logger.info(f"[DESIGN DOC] Task {task.id} completed: {page_count} pages, rev {rev}")
    # Snapshot the vision while ``project`` is known-loaded. _spawn_strategic_review
    # never writes project.vision, but it can roll back / expire the instance on
    # its failure paths; building the retry marker from this captured base
    # (instead of re-reading ``project.vision`` on the deferred/except paths below)
    # avoids a MissingGreenlet lazy-load. ``status_message`` is captured for the
    # same reason so the post-commit emit never reads it back off a possibly
    # expired ``task``.
    base_vision = dict(project.vision or {})
    status_message: str | None = None

    if page_count == 0:
        await _create_strategic_alert(
            task.project_id,
            session,
            f"Design doc update produced 0 pages (task {str(task.id)[:8]})",
        )

    # Chain: spawn Phase 2 (strategic review)
    # Do this BEFORE committing task status so a failed spawn doesn't leave
    # the task as COMPLETED with no Phase 2 follow-up.
    cycle_id = phase1_cycle_id
    apply_spawn_budget_guard = not bool(
        (task.context or {}).get(STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY)
    )
    # Architecture-cycle operator steering: the pending refresh marker was
    # consumed when Phase 1 (this design_doc task) was spawned, so it no longer
    # reads as operator-origin in vision. The flag was stamped on the task
    # context at spawn time — thread it into Phase 2 so the operator steering
    # directive still reaches the strategist.
    operator_steered = bool((task.context or {}).get("operator_steered"))
    # The operator steering text was persisted on the Phase 1 task context (the
    # refresh marker — and its excerpt — was consumed at Phase 1 spawn). Thread
    # it into Phase 2 so the steering directive survives even when the operator
    # message has aged out of the live conversation-thread snapshot.
    steering_excerpt = (task.context or {}).get("steering_excerpt")
    preferred_runner_target_id = _phase_runner_affinity(task)
    local_runner_context = _phase_local_runner_context(task)
    spawned = None
    try:
        # Wave F4 H9: removed `await session.refresh(project)`. The caller
        # (post-commit chain via task_lifecycle._process_post_commit) already
        # holds the project lock acquired via lock_project_for_strategic_transition;
        # the row is fresh-read at lock time, so a refresh here is a redundant
        # round-trip that defeats the lock-share optimization between
        # strategic-completion handlers.
        spawned = await _spawn_strategic_review(
            project,
            session,
            apply_spawn_budget_guard=apply_spawn_budget_guard,
            cycle_id=cycle_id,
            operator_steered=operator_steered,
            steering_excerpt=steering_excerpt,
            preferred_runner_target_id=preferred_runner_target_id,
            local_runner_context=local_runner_context,
        )
        if spawned:
            phase2_spawned = True
            task.latest_status_message = status_message = (
                f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
            )
        else:
            # Spawn silently skipped (active strategic task) — create retry marker
            logger.warning(
                f"[DESIGN DOC] Phase 2 spawn skipped for task {task_id_str} (active strategic task exists)"
            )
            task.latest_status_message = status_message = (
                f"Design doc: {page_count} pages, rev {rev} — strategic review deferred (active task exists)"
            )
            project.vision = {
                **base_vision,
                "_pending_strategic_review": {
                    "design_doc_task_id": task_id_str,
                    "failed_at": _utcnow().isoformat(),
                    "error": "skipped: active strategic task exists",
                    "cycle_id": cycle_id,
                    "operator_steered": operator_steered,
                    "steering_excerpt": steering_excerpt,
                    **(
                        {"preferred_runner_target_id": preferred_runner_target_id}
                        if preferred_runner_target_id
                        else {}
                    ),
                    **local_runner_context,
                },
            }
            flag_modified(project, "vision")
            session.add(project)
    except Exception as chain_exc:
        # Only pre-captured identifiers in here — task may be expired (see above).
        logger.error(
            f"[DESIGN DOC] Failed to chain Phase 2 for task {task_id_str}: {chain_exc}",
            exc_info=True,
        )
        task.latest_status_message = status_message = (
            f"Design doc: {page_count} pages, rev {rev} — FAILED to spawn strategic review: {chain_exc!r}"[
                :500
            ]
        )
        await _create_strategic_alert(
            task_project_id,
            session,
            f"Strategic review failed to spawn after design doc update (task {task_id_str[:8]}): {chain_exc!r}",
        )
        # Save a retry marker so the next research spawn can retry the strategic review
        project.vision = {
            **base_vision,
            "_pending_strategic_review": {
                "design_doc_task_id": task_id_str,
                "failed_at": _utcnow().isoformat(),
                "error": str(chain_exc)[:200],
                "cycle_id": cycle_id,
                "operator_steered": operator_steered,
                "steering_excerpt": steering_excerpt,
                **(
                    {"preferred_runner_target_id": preferred_runner_target_id}
                    if preferred_runner_target_id
                    else {}
                ),
                **local_runner_context,
            },
        }
        if cycle_id:
            from app import strategic_cycle as _sc  # noqa: PLC0415

            await _sc.mark_failure(
                session,
                cycle_id,
                phase="phase2_spawn",
                reason=str(chain_exc)[:200],
            )
        flag_modified(project, "vision")
        session.add(project)
    # Previously emitted a "Architecture analysis completed, strategic review
    # starting" phase-transition message. Removed as part of the auto-status
    # cleanup — the phase transition is visible in the task timeline and the
    # eventual strategic cycle_summary carries the user-facing narrative.
    session.add(task)
    await session.commit()
    # Post-commit: when Phase 2 was NOT spawned (deferred because an active
    # strategic task exists, or the spawn raised), the Phase-1 started event is
    # otherwise never paired. Emit completed so the indicator clears. A
    # successful spawn already emitted a Phase-2 started, so it is skipped.
    if not phase2_spawned:
        # Pre-captured identifiers + captured status string: ``task`` may have
        # been expired by a rollback inside _spawn_strategic_review, so reading
        # task.project_id / task.id / task.latest_status_message here would
        # lazy-load and raise MissingGreenlet (the site that failed task
        # 94594516, 2026-06-11, after the a9bf1317 partial fix).
        await emit_strategic_cycle_completed(
            task_project_id,
            task_id_value,
            1,
            cycle_id=cycle_id,
            phase_state="deferred",
            completion={
                "status": "deferred",
                "status_message": status_message,
            },
        )


# Confirmed-command field names written back by the agent build-config endpoint.
_CONFIRMED_COMMAND_KEYS = (
    "confirmed_lint_cmd",
    "confirmed_test_cmd",
    "confirmed_build_cmd",
    "confirmed_install_cmd",
)
# Command-memory statuses that mean "overview may be fine but command write-back
# is NOT complete" — i.e. a partial / degraded command-memory state.
_PARTIAL_COMMAND_MEMORY_STATUSES = frozenset(
    {"missing_commands", "writeback_failed", "unknown"}
)


def _has_command_writeback_evidence(build_config: Dict[str, Any], task_id: str) -> bool:
    """Return True when *this* task durably persisted ≥1 confirmed command.

    Primary evidence is the top-level write-back marker stamped by
    ``agent_write_back_build_config_commands``; the per-directory
    ``confirming_task_id`` stamps are a robustness fallback (e.g. a marker
    written by an older endpoint build that predates the top-level field).
    """
    if build_config.get(
        "last_command_writeback_task_id"
    ) == task_id and build_config.get("last_command_writeback_paths"):
        return True
    for entry in build_config.get("directories") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("confirming_task_id")) != task_id:
            continue
        if any(
            isinstance(entry.get(k), str) and entry.get(k).strip()
            for k in _CONFIRMED_COMMAND_KEYS
        ):
            return True
    return False


async def _seed_bootstrap_playbook_commands(
    project: Any,
    build_config: Dict[str, Any],
    task: Task,
    session: AsyncSession,
    now_iso: str,
) -> None:
    """Seed confirmed build/test/lint commands into project playbook after bootstrap.

    Called from within a project_lock block; uses with_design_doc_locked for the
    write so the invariant guard stays satisfied. Idempotent: skips keys that
    already exist. Only runs for external (non deploy-repo) projects with
    confirmed command memory.
    """
    if project_is_deploy_repo(project):
        return
    if not build_config.get("bootstrap_command_memory_complete"):
        return

    _CMD_PLAYBOOK_KEYS = {
        "test-detected": "confirmed_test_cmd",
        "build-detected": "confirmed_build_cmd",
        "lint-detected": "confirmed_lint_cmd",
    }

    cmds_by_key: Dict[str, list] = {k: [] for k in _CMD_PLAYBOOK_KEYS}
    for entry in build_config.get("directories") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path") or "."
        for playbook_key, field in _CMD_PLAYBOOK_KEYS.items():
            cmd = entry.get(field)
            if isinstance(cmd, str) and cmd.strip():
                cmds_by_key[playbook_key].append((path, cmd.strip()))

    to_seed: Dict[str, str] = {}
    for playbook_key, cmds in cmds_by_key.items():
        if not cmds:
            continue
        parts = []
        for path, cmd in cmds:
            if path and path not in (".", ""):
                parts.append(f"`cd {path} && {cmd}`")
            else:
                parts.append(f"`{cmd}`")
        to_seed[playbook_key] = " | ".join(parts)

    if not to_seed:
        return

    task_id_str = str(task.id)
    project_id = project.id
    added_count: Dict[str, int] = {"v": 0}

    def _mutate(doc: Dict[str, Any]) -> None:
        for key, content in to_seed.items():
            full_key = f"playbook/{key}"
            if full_key in doc:
                continue
            doc[full_key] = {
                "content": content,
                "source_task_id": task_id_str,
                "updated_at": now_iso,
                "confirmed_count": 0,
            }
            added_count["v"] += 1
        if added_count["v"]:
            meta = dict(doc.get("_meta") or {"revision": 0})
            meta["revision"] = meta.get("revision", 0) + 1
            meta["last_updated"] = now_iso
            doc["_meta"] = meta

    await with_design_doc_locked(session, project_id, _mutate)
    if added_count["v"]:
        logger.info(
            "[BOOTSTRAP] Seeded %d playbook command entries for project %s (task %s)",
            added_count["v"],
            project_id,
            task.id,
        )


def _stamp_command_memory_status(build_config: Dict[str, Any], task: Task) -> None:
    """Record a truthful, distinguishable command-memory marker on build_config.

    Reconciles the agent's self-reported ``command_memory`` outcome (persisted
    into ``task.context``) against durable write-back evidence so a valid
    repo_overview can no longer mask incomplete command memory. Mutates
    ``build_config`` in place; never raises on malformed input.
    """
    task_id = str(task.id)
    reported = (task.context or {}).get("command_memory")
    reported_status: str | None = None
    reported_detail: str | None = None
    reported_paths: list[str] | None = None
    if isinstance(reported, dict):
        rs = reported.get("status")
        if isinstance(rs, str):
            reported_status = rs
        rd = reported.get("detail")
        if isinstance(rd, str) and rd.strip():
            reported_detail = rd
        rp = reported.get("updated_paths")
        if isinstance(rp, list):
            reported_paths = [p for p in rp if isinstance(p, str) and p.strip()]

    evidence = _has_command_writeback_evidence(build_config, task_id)

    detail = reported_detail
    if evidence:
        status = "confirmed"
    elif reported_status == "missing_commands":
        status = "missing_commands"
    elif reported_status == "writeback_failed":
        status = "writeback_failed"
    elif reported_status == "success":
        # Agent claimed success but nothing landed — degrade truthfully so the
        # marker reflects reality, not the unverified claim.
        status = "writeback_failed"
        detail = detail or "reported success without write-back evidence"
    else:
        # Older agent omitted the payload entirely.
        status = "unknown"

    build_config["bootstrap_command_memory_status"] = status
    build_config["bootstrap_command_memory_complete"] = status == "confirmed"
    build_config["bootstrap_command_memory_updated_at"] = _utcnow().isoformat()
    if detail:
        build_config["bootstrap_command_memory_detail"] = detail
    else:
        build_config.pop("bootstrap_command_memory_detail", None)

    paths = reported_paths or (
        list(build_config.get("last_command_writeback_paths") or [])
        if build_config.get("last_command_writeback_task_id") == task_id
        else []
    )
    if paths:
        build_config["bootstrap_command_memory_paths"] = paths
    else:
        build_config.pop("bootstrap_command_memory_paths", None)


async def _upsert_domain_knowledge_document(
    session: AsyncSession,
    task: Task,
    github_owner: str,
    github_repo: str,
    now_iso: str,
) -> None:
    """Upsert a KnowledgeDocument for product-domain knowledge from bootstrap.

    Reads domain_overview from task.context and persists it as a singleton
    KnowledgeDocument (source_kind='repo_domain', topic='domain-knowledge').
    Skipped silently if domain_overview is absent or lacks a meaningful purpose.
    Never raises — domain capture is best-effort and must not block bootstrap.
    """
    from app.models import KnowledgeDocument, SourceKind

    try:
        ctx_domain = (task.context or {}).get("domain_overview")
        if not isinstance(ctx_domain, dict):
            return
        purpose = ctx_domain.get("purpose") or ""
        if not isinstance(purpose, str) or len(purpose.strip()) < 20:
            return

        key_concepts = ctx_domain.get("key_concepts") or {}
        critical_user_flows = ctx_domain.get("critical_user_flows") or []
        architectural_gotchas = ctx_domain.get("architectural_gotchas") or []
        source = ctx_domain.get("source") or "none-found"

        lines: list[str] = [f"## Product Purpose\n{purpose}\n"]
        if key_concepts:
            lines.append("## Key Domain Concepts")
            for term, definition in key_concepts.items():
                lines.append(f"- **{term}**: {definition}")
            lines.append("")
        if critical_user_flows:
            lines.append("## Critical User Flows")
            for flow in critical_user_flows:
                lines.append(f"- {flow}")
            lines.append("")
        if architectural_gotchas:
            lines.append("## Architectural Gotchas")
            for gotcha in architectural_gotchas:
                lines.append(f"- {gotcha}")
            lines.append("")
        lines.append(
            f"*Source: {source} | {github_owner}/{github_repo} | Captured {now_iso}*"
        )
        body = "\n".join(lines)

        title = f"{github_owner}/{github_repo} — Product Domain Knowledge"
        first_sentence = purpose.split(".")[0] if "." in purpose else purpose
        summary = first_sentence.strip()[:500] or purpose[:500]

        result = await session.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.project_id == task.project_id,
                KnowledgeDocument.topic == "domain-knowledge",
                KnowledgeDocument.source_kind == SourceKind.REPO_DOMAIN.value,
            )
            .order_by(KnowledgeDocument.updated_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

        source_citations = [f"file:{source}"] if source != "none-found" else []
        if existing is not None:
            existing.title = title
            existing.summary = summary
            existing.body = body
            existing.source_citations = source_citations
            existing.source_task_id = task.id
            session.add(existing)
        else:
            doc = KnowledgeDocument(
                project_id=task.project_id,
                source_task_id=task.id,
                source_kind=SourceKind.REPO_DOMAIN.value,
                title=title,
                summary=summary,
                body=body,
                topic="domain-knowledge",
                tags=["bootstrap", "domain", "product"],
                source_citations=source_citations,
            )
            session.add(doc)
        logger.info(
            "[BOOTSTRAP] Upserted domain KnowledgeDocument for project %s (task %s)",
            task.project_id,
            task.id,
        )
    except Exception:
        logger.exception(
            "[BOOTSTRAP] Failed to upsert domain KnowledgeDocument for project %s "
            "(task %s) — continuing without domain document",
            task.project_id,
            task.id,
        )


async def _handle_repo_bootstrap_completion(
    task: Task, logs: str, session: AsyncSession
) -> None:
    """Handle repo_bootstrap task completion: persist repo overview and stamp build_config.

    Called when a repo_bootstrap task reaches a terminal COMPLETED state.

    If the agent produced a repo overview JSON (tech_stack / directory_layout /
    first_steps_summary) — delivered via the structured completion payload and
    mirrored into task.context["repo_overview"] — it is persisted into
    project.design_doc["repo_overview"] so the first strategic cycle can
    consume it via _design_doc_index / _agent_view_design_doc. A clean success
    also stamps bootstrap_completed_at on build_config.

    If no parseable overview is present (degraded path), the handler records
    bootstrap_degraded_reason in build_config instead of silently claiming
    clean success. Either path marks the task terminal so the strategic spawn
    gate can proceed (the gate only defers while bootstrap is IN_PROGRESS/QUEUED).
    """
    _OVERVIEW_KEYS = ("tech_stack", "directory_layout", "first_steps_summary")
    _OVERVIEW_OPTIONAL_KEYS = (
        "entry_points",
        "test_layout",
        "key_conventions",
        "environment_requirements",
    )
    _REVISION_KEYS = ("source_revision", "revision", "commit", "commit_sha")
    overview: dict[str, str] | None = None
    source_revision: str | None = None

    def _extract_revision(src: dict[str, Any]) -> str | None:
        for key in _REVISION_KEYS:
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _build_overview(src: dict[str, Any]) -> dict[str, str]:
        result = {k: str(src.get(k, "")) for k in _OVERVIEW_KEYS}
        for k in _OVERVIEW_OPTIONAL_KEYS:
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                result[k] = v
        return result

    def _is_overview_complete(src: dict[str, Any]) -> bool:
        """Check if overview has all required fields present and non-empty."""
        for key in _OVERVIEW_KEYS:
            value = src.get(key)
            if not isinstance(value, str) or not value.strip():
                return False
        return True

    ctx_overview = (task.context or {}).get("repo_overview")
    if isinstance(ctx_overview, dict) and _is_overview_complete(ctx_overview):
        overview = _build_overview(ctx_overview)
        source_revision = _extract_revision(ctx_overview)
    elif logs:
        try:
            parsed = await async_json_loads(logs)
            if isinstance(parsed, dict) and _is_overview_complete(parsed):
                overview = _build_overview(parsed)
                source_revision = _extract_revision(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    # Provenance derives from the bootstrap task payload (branch) plus any
    # revision the agent reported (optional). The branch the overview describes
    # is the integration_branch the bootstrap ran against.
    source_branch = (task.first_payload or {}).get("integration_branch") or None

    async with project_lock(session, task.project_id, skip_locked=False) as project:
        if project is None:
            logger.warning(
                "[BOOTSTRAP] Project %s not found for bootstrap task %s — skipping",
                task.project_id,
                task.id,
            )
            return

        build_config = dict(project.build_config or {})
        now = _utcnow()
        now_iso = now.isoformat()

        if overview:
            # Provenance carried alongside the overview so downstream agents see
            # freshness (refreshed time + the branch/revision the overview
            # describes). Nested under the repo_overview block so a refresh
            # replaces both the content and its provenance atomically.
            provenance: Dict[str, Any] = {
                "refreshed_at": now_iso,
                "source_branch": source_branch,
                "source_revision": source_revision,
            }
            overview_block: Dict[str, Any] = {**overview, "provenance": provenance}

            def _mutate_design_doc(doc: Dict[str, Any]) -> None:
                existing_overview = doc.get("repo_overview")
                if existing_overview != overview_block:
                    meta = dict(doc.get("_meta") or {"revision": 0})
                    doc["repo_overview"] = overview_block
                    meta["revision"] = meta.get("revision", 0) + 1
                    meta["last_updated"] = now_iso
                    doc["_meta"] = meta

            session.expire(project, ["design_doc"])
            await with_design_doc_locked(session, task.project_id, _mutate_design_doc)

            if not build_config.get("bootstrap_completed_at"):
                build_config["bootstrap_completed_at"] = now_iso
            # Mirror provenance into build_config so the admin UI (which reads
            # project.build_config) can surface overview freshness.
            build_config["bootstrap_overview_refreshed_at"] = now_iso
            build_config["bootstrap_overview_source_branch"] = source_branch
            build_config["bootstrap_overview_source_revision"] = source_revision
            build_config.pop("bootstrap_degraded_reason", None)
            build_config.pop("bootstrap_recovery_next_at", None)
            build_config.pop("bootstrap_recovery_exhausted_at", None)
            build_config.pop("bootstrap_recovery_last_degraded_at", None)
            build_config["bootstrap_recovery_attempts"] = 0
            build_config["bootstrap_recovery_state"] = "completed"
            logger.info(
                "[BOOTSTRAP] Persisted repo_overview and stamped bootstrap_completed_at "
                "for project %s (task %s)",
                task.project_id,
                task.id,
            )
        else:
            if not build_config.get("bootstrap_completed_at"):
                _last_degraded_task_id = build_config.get(
                    "bootstrap_recovery_last_task_id"
                )
                if _last_degraded_task_id != str(task.id):
                    # New degraded occurrence — increment counter and record state.
                    attempts = (
                        coerce_recovery_attempts(
                            build_config.get("bootstrap_recovery_attempts")
                        )
                        + 1
                    )
                    build_config["bootstrap_degraded_reason"] = "overview_missing"
                    build_config["bootstrap_recovery_attempts"] = attempts
                    build_config["bootstrap_recovery_last_task_id"] = str(task.id)
                    build_config["bootstrap_recovery_last_degraded_at"] = (
                        now.isoformat()
                    )
                    if attempts >= BOOTSTRAP_RECOVERY_MAX_ATTEMPTS:
                        build_config["bootstrap_recovery_state"] = "exhausted"
                        build_config["bootstrap_recovery_exhausted_at"] = (
                            now.isoformat()
                        )
                        build_config.pop("bootstrap_recovery_next_at", None)
                    else:
                        build_config["bootstrap_recovery_state"] = "backoff"
                        build_config["bootstrap_recovery_next_at"] = (
                            now + next_recovery_backoff(attempts)
                        ).isoformat()
                        build_config.pop("bootstrap_recovery_exhausted_at", None)
                    logger.warning(
                        "[BOOTSTRAP] No repo_overview in completion payload for project %s "
                        "(task %s) — recording degraded marker",
                        task.project_id,
                        task.id,
                    )

        # ── Command-memory status (independent of overview persistence) ──────
        # A valid overview no longer implies complete command memory. The agent
        # reports the write-back outcome separately (success / missing_commands
        # / writeback_failed), and the write-back endpoint stamps top-level
        # evidence (last_command_writeback_task_id) plus per-directory
        # confirming_task_id. Completion reconciles the two so build_config
        # exposes a truthful, distinguishable command-memory marker:
        #   confirmed       — this task persisted ≥1 confirmed command
        #   missing_commands— agent found nothing runnable to confirm
        #   writeback_failed— agent's write-back failed (or it claimed success
        #                     without leaving any evidence)
        #   unknown         — older agent omitted the payload entirely; degrade
        #                     truthfully rather than falsely claim success
        _stamp_command_memory_status(build_config, task)
        await _seed_bootstrap_playbook_commands(
            project, build_config, task, session, now_iso
        )

        # Mirror domain-knowledge capture timestamp into build_config so the
        # admin UI can surface freshness independently of overview provenance.
        if isinstance((task.context or {}).get("domain_overview"), dict):
            build_config["bootstrap_domain_knowledge_refreshed_at"] = now_iso

        project.build_config = build_config
        flag_modified(project, "build_config")
        session.add(project)

        # Upsert product-domain KnowledgeDocument (best-effort; never blocks bootstrap).
        _bootstrap_payload = task.first_payload or {}
        await _upsert_domain_knowledge_document(
            session,
            task,
            github_owner=_bootstrap_payload.get("github_owner") or "",
            github_repo=_bootstrap_payload.get("github_repo") or "",
            now_iso=now_iso,
        )

        # Stamp task.completed_at so the row is queryable by completion time.
        # repo_bootstrap tasks reach COMPLETED status via the normal agent status
        # path (which sets task.status) but that path never stamped completed_at,
        # leaving rows with status=COMPLETED and completed_at=null.
        if task.completed_at is None:
            task.completed_at = _utcnow()
            session.add(task)

        await session.commit()


async def confirm_applied_playbook_entries(task: Task, session: AsyncSession) -> None:
    """Increment confirmed_count for playbook entries that guided this task's prompt.

    Called after a create/fix/refactor task merges. Reads playbook_applied_entry_ids
    from task.context (stamped at spawn time by research_completion) and bumps
    confirmed_count on each live entry in project.design_doc so the archival ranker
    can distinguish actively-effective patterns from stale ones.
    """
    entry_ids = (task.context or {}).get("playbook_applied_entry_ids") or []
    if not entry_ids:
        return
    now_iso = _utcnow().isoformat()

    # Serialize the design_doc read-modify-write through the canonical row lock
    # so a concurrent same-project completion bumping a different design_doc key
    # is not clobbered. The mutate_fn bumps confirmed_count on live playbook
    # entries inside the lock against the freshly-loaded doc.
    confirmed = {"v": 0}

    def _mutate(doc: Dict[str, Any]) -> None:
        for entry_id in entry_ids:
            full_key = (
                entry_id if entry_id.startswith("playbook/") else f"playbook/{entry_id}"
            )
            entry = doc.get(full_key)
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1
            entry["last_confirmed_at"] = now_iso
            doc[full_key] = entry
            confirmed["v"] += 1

    # expire_on_commit=False means a Project already identity-mapped by the
    # surrounding completion flow carries a stale design_doc; expire it so the
    # lock's SELECT FOR UPDATE re-reads the freshly-committed row (canonical
    # pattern documented at the top of this module, ~L103-110).
    project = await session.get(Project, task.project_id)
    if project is not None:
        session.expire(project, ["design_doc"])
    await with_design_doc_locked(session, task.project_id, _mutate)
    if confirmed["v"]:
        logger.info(
            "[PLAYBOOK] Confirmed %d entries for task %s", confirmed["v"], task.id
        )
