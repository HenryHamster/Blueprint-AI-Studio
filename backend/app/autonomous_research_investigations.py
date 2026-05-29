"""Failure-driven investigation & remediation spawning.

Extracted from ``autonomous_research`` (which grew past its size cap). This module
holds the cohesive cluster of spawners that each react to a failure pattern by
spawning a diagnostic ``investigate`` task or a ``create`` remediation task —
direction-hold investigations, task-failure investigations, review-convergence
investigations, fingerprint-audit-system inference, and residual-fingerprint
remediation — all sharing the ``_recent_investigation_exists`` dedup gate.

Every public symbol is re-exported from ``app.autonomous_research`` for backwards
compatibility with existing callers and tests that do
``from app.autonomous_research import ...``.
"""

import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Task,
    Project,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
import app.shared_state as _shared_state
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow
from app.self_audit_dispatch import _record_residual_audit_deferral

logger = logging.getLogger(__name__)


_MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION = 3


async def _recent_investigation_exists(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession,
    error_fingerprint: str | None = None,
) -> bool:
    """Return True if an investigate-type task already targets this direction.

    Unified dedup gate shared by ``_spawn_direction_diagnosis`` (auto-pause path)
    and ``_spawn_direction_hold_investigation`` (direction-hold path). Matches
    any investigate task for ``direction_id`` that is either in-flight
    (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING) or was created in the last 24h.

    When ``error_fingerprint`` is supplied, dedup is narrowed to investigations
    whose ``failure_fingerprints`` context array contains that fingerprint —
    distinct failure patterns within the same direction can each be investigated.
    Regardless of fingerprint, a per-direction concurrent cap of
    ``_MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION`` always applies to prevent
    resource exhaustion.
    """
    from sqlalchemy import or_

    active_statuses = (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    )
    cutoff = _utcnow() - timedelta(hours=24)
    rows = (
        await session.execute(
            select(Task.id, Task.context, Task.status).where(
                Task.project_id == project_id,
                Task.payloads[0]["type"].as_string() == "investigate",
                Task.context["direction_id"].as_string() == str(direction_id),
                or_(
                    Task.status.in_(active_statuses),
                    Task.created_at >= cutoff,
                ),
            )
        )
    ).all()

    active_count = sum(1 for _id, _ctx, status in rows if status in active_statuses)
    if active_count >= _MAX_ACTIVE_INVESTIGATIONS_PER_DIRECTION:
        return True

    if error_fingerprint is None:
        return len(rows) > 0

    for _id, ctx, _status in rows:
        fps = (ctx or {}).get("failure_fingerprints") or []
        if error_fingerprint in fps:
            return True
    return False


async def _spawn_direction_hold_investigation(
    project: Project, session: AsyncSession
) -> int:
    """Auto-spawn an investigate-type task for each held direction in the project.

    A direction enters the held state after 3 consecutive CODE_ERROR failures (see
    shared_state.record_direction_result). Without this spawn, the hold silently
    expires after DIRECTION_HOLD_TIMEOUT_HOURS and the direction resumes blindly
    retrying. Investigation reports are consumed by _handle_investigation_completion,
    which can then drive targeted remediation.
    """
    from app.anomaly_detection import _error_fingerprint

    held = _shared_state.get_held_directions()
    if not held:
        return 0

    project_id_str = str(project.id)
    direction_map = {d.get("id"): d for d in (project.directions or []) if d.get("id")}
    spawned = 0

    for direction_id, hold_info in held.items():
        if hold_info.get("project_id") != project_id_str:
            continue
        direction = direction_map.get(direction_id)
        if not direction:
            continue
        recent_failed = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.status == TaskStatus.FAILED,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        dir_failures = [
            t
            for t in recent_failed
            if (t.context or {}).get("direction_id") == direction_id
        ][:5]

        failure_lines = []
        fingerprints: list[str] = []
        for t in dir_failures:
            err = (t.latest_status_message or "(no message)")[:240]
            fp = _error_fingerprint(err)
            fingerprints.append(fp)
            failure_lines.append(f"- [{str(t.id)[:8]}] fp={fp} | {err}")
        failure_block = (
            "\n".join(failure_lines)
            if failure_lines
            else "(no recent failure details available)"
        )

        dominant_fp = fingerprints[0] if fingerprints else None
        if await _recent_investigation_exists(
            project.id,
            direction_id,
            session,
            error_fingerprint=dominant_fp,
        ):
            continue

        direction_text = (direction.get("text") or "?")[:300]
        fc = hold_info.get("last_failure_class") or "CODE_ERROR"
        streak_n = hold_info.get("consecutive_failures", "?")
        prompt = (
            f"Direction '{direction_text}' (id: {direction_id}) has been placed on hold "
            f"after {streak_n} consecutive {fc} failures. Diagnose the underlying root cause "
            f"so the direction can resume.\n\n"
            f"Recent failed tasks for this direction:\n{failure_block}\n\n"
            f"Produce the structured diagnosis JSON report. Focus on shared failure patterns "
            f"across the tasks above (same fingerprint = same root cause)."
        )

        payload = validate_internal_payload(
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": _get_project_integration_branch(project),
            },
            "autonomous_research:direction_hold_investigation",
        )
        new_task = Task(
            project_id=project.id,
            title=f"Investigate direction hold: {direction_text[:80]}",
            description=f"Auto-spawned on direction {direction_id[:8]} hold ({streak_n} {fc} failures)",
            payloads=[payload],
            context={
                "direction_id": direction_id,
                "spawned_by": "direction_hold_investigation",
                "hold_reason": hold_info.get("reason", ""),
                "last_failure_class": fc,
                "failure_fingerprints": fingerprints,
                **({"error_fingerprint": fingerprints[0]} if fingerprints else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        session.add(
            TaskHistory(
                task_id=new_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "DIRECTION_HOLD_INVESTIGATION",
                    "direction_id": direction_id,
                },
            )
        )
        logger.info(
            f"[DIRECTION HEALTH] Spawned investigation task {new_task.id} for held direction {direction_id}"
        )
        spawned += 1

    if spawned:
        await session.commit()
    return spawned


async def _spawn_task_failure_investigation(
    task: Task,
    session: AsyncSession,
) -> bool:
    """Spawn an investigate task when a directed task exhausts retries with
    a CODE_ERROR / code_defect failure.

    Returns True when a new investigation task is created, False otherwise.
    Canonical implementation — called by task_lifecycle post-failure processing
    once a task has been marked FAILED and its retry budget is exhausted.
    Gating is delegated to ``should_trigger_code_defect_investigation``; this
    function adds 24h dedup per direction and builds the investigation prompt.
    """
    from app.failure_classification import (
        AGENT_TIMEOUT,
        CODE_ERROR,
        REVIEW_QUALITY_STREAK_THRESHOLD,
        TAXONOMY_CODE_DEFECT,
        TAXONOMY_INFRA_RELATED,
        TAXONOMY_REVIEW_QUALITY,
        classify_failure_taxonomy,
        should_trigger_code_defect_investigation,
        should_trigger_review_quality_investigation,
        should_trigger_timeout_investigation,
    )

    is_code_defect = should_trigger_code_defect_investigation(task)
    is_timeout = (not is_code_defect) and should_trigger_timeout_investigation(task)
    is_review_quality = (
        (not is_code_defect)
        and (not is_timeout)
        and should_trigger_review_quality_investigation(task)
    )
    if not (is_code_defect or is_timeout or is_review_quality):
        return False

    ctx = task.context or {}
    direction_id = ctx.get("direction_id")

    # Investigation-recursion depth cap. The 2026-05-03 dogfood run showed
    # multi-level investigation chains burning compute without converging:
    #   original_task fails (depth 0) →
    #     investigation_1 (depth 1) → remediation_1 (depth 1) →
    #       investigation_2 (depth 2) → remediation_2 (depth 2) →
    #         investigation_3 (depth 3) → ...
    # Each investigation costs an agent VM (~$0.50-1.00 + tokens) and the
    # marginal value drops fast — by depth 3 the diagnoses tend to converge
    # on operator-judgment-required outcomes anyway. Cap at depth 2: failures
    # of deeper-level remediations skip the spawn and surface to operator
    # review via project message instead.
    MAX_INVESTIGATION_DEPTH = 2
    source_depth = int(ctx.get("investigation_depth") or 0)
    next_depth = source_depth + 1
    if next_depth > MAX_INVESTIGATION_DEPTH:
        logger.warning(
            f"[TASK FAILURE INVESTIGATION] Skip spawn for task {task.id} "
            f"(direction {direction_id}): investigation_depth would be "
            f"{next_depth} > MAX={MAX_INVESTIGATION_DEPTH}. Marking for "
            f"operator review."
        )
        try:
            from app.models import ProjectMessage, MessageType

            session.add(
                ProjectMessage(
                    project_id=task.project_id,
                    message_type=MessageType.NOTE,
                    from_type="orchestrator",
                    content=(
                        f"Investigation chain depth limit ({MAX_INVESTIGATION_DEPTH}) "
                        f"reached for direction {direction_id or 'undirected'}. "
                        f"Failed task: {task.id} ({(task.title or '?')[:80]}). "
                        f"Auto-remediation has tried multiple times without converging — "
                        f"operator review is needed before spawning further investigations."
                    ),
                )
            )
        except Exception as _msg_exc:  # noqa: BLE001
            logger.warning(
                f"[TASK FAILURE INVESTIGATION] Failed to post depth-cap message: {_msg_exc}"
            )
        return False

    if is_review_quality:
        # Frequency gate: fire once this direction shows a review-quality
        # pattern in its recent failures. Use a sliding window over the
        # most recent FAILED tasks on the direction (excluding the current
        # one) and require at least (THRESHOLD - 1) of the last WINDOW_SIZE
        # to be review_quality. This tolerates occasional non-review-quality
        # failures interspersed in an otherwise-clear review-quality pattern.
        WINDOW_SIZE = 5
        required_count = REVIEW_QUALITY_STREAK_THRESHOLD - 1
        # If a convergence investigation already exists for this direction
        # within the last 7 days, that's strong independent evidence of
        # chronic review-quality problems — lower the count requirement by
        # 1 so we trigger faster on repeat offenders.
        convergence_cutoff = _utcnow() - timedelta(days=7)
        convergence_recent = (
            (
                await session.execute(
                    select(Task).where(
                        Task.project_id == task.project_id,
                        Task.created_at >= convergence_cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        has_convergence = any(
            (t.context or {}).get("direction_id") == direction_id
            and (t.context or {}).get("spawned_by")
            == "review_convergence_investigation"
            for t in convergence_recent
        )
        if has_convergence:
            required_count = max(1, required_count - 1)

        recent = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == task.project_id,
                        Task.status == TaskStatus.FAILED,
                        Task.id != task.id,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(60)
                )
            )
            .scalars()
            .all()
        )
        dir_recent = [
            t for t in recent if (t.context or {}).get("direction_id") == direction_id
        ][:WINDOW_SIZE]
        if len(dir_recent) < required_count:
            return False
        review_quality_count = sum(
            1
            for t in dir_recent
            if classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY
        )
        if review_quality_count < required_count:
            return False

    last_failure_class = (
        CODE_ERROR if (is_code_defect or is_review_quality) else AGENT_TIMEOUT
    )
    if is_code_defect:
        failure_taxonomy = TAXONOMY_CODE_DEFECT
    elif is_review_quality:
        failure_taxonomy = TAXONOMY_REVIEW_QUALITY
    else:
        failure_taxonomy = TAXONOMY_INFRA_RELATED
    # Audit W1: previous_error_fingerprint is now a column.
    prev_fp = task.previous_error_fingerprint or ""
    if await _recent_investigation_exists(
        task.project_id,
        direction_id,
        session,
        error_fingerprint=prev_fp or None,
    ):
        return False

    if prev_fp:
        from app.task_completion import _find_sibling_fingerprint_investigation

        sibling_cutoff = _utcnow() - timedelta(hours=24)
        shared_id = await _find_sibling_fingerprint_investigation(
            task.project_id,
            prev_fp,
            sibling_cutoff,
            session,
        )
        if shared_id is not None:
            logger.info(
                f"[TASK FAILURE INVESTIGATION] Skip spawn for task {task.id} "
                f"(direction {direction_id}): shares fingerprint {prev_fp[:40]} "
                f"with existing investigate task {shared_id}"
            )
            return False

    direction: dict = {}
    _project = await session.get(Project, task.project_id)
    if direction_id and _project is not None:
        direction = (
            next(
                (d for d in (_project.directions or []) if d.get("id") == direction_id),
                {},
            )
            or {}
        )

    payload = task.first_payload or {}
    ttype = payload.get("type") or ""
    status_msg = (task.latest_status_message or "(no message)")[:400]
    tool_trace = ctx.get("_tool_trace") or ""
    if isinstance(tool_trace, list):
        tool_trace = "\n".join(str(x) for x in tool_trace)
    tool_trace = str(tool_trace)[:1500]
    failing_tests = ctx.get("failing_tests") or ctx.get("failed_tests") or []
    if isinstance(failing_tests, (list, tuple)):
        failing_tests_str = ", ".join(str(x) for x in failing_tests)[:400]
    else:
        failing_tests_str = str(failing_tests)[:400]

    prompt_lines = [
        f"Task {str(task.id)[:8]} ({ttype}) failed with {last_failure_class} / {failure_taxonomy} "
        f"after exhausting its retry budget. Diagnose the root cause.",
        "",
        f"Direction: {direction_id}",
        f"Status message: {status_msg}",
    ]
    if prev_fp:
        prompt_lines.append(f"Previous error fingerprint: {prev_fp}")
    if failing_tests_str:
        prompt_lines.append(f"Failing tests: {failing_tests_str}")
    if tool_trace:
        prompt_lines.append(f"\nRecent tool trace:\n{tool_trace}")
    if is_review_quality:
        prompt_lines.append(
            f"\nThis is a review-quality pattern investigation: this direction has "
            f"{REVIEW_QUALITY_STREAK_THRESHOLD}+ review-quality failures in its recent "
            f"task window (lint/type-check/test-suite). Look for systemic causes — a module that "
            f"consistently produces lint-failing code, a test configuration mismatch, "
            f"or a code pattern that always triggers type errors — rather than "
            f"diagnosing this one task's failure in isolation."
        )

    prior_diag = (direction.get("diagnosis_summary") or "").strip()
    if prior_diag:
        prompt_lines.append(f"\nPrior diagnosis summary:\n{prior_diag[:800]}")
    partial_summary = (direction.get("partial_diagnosis_summary") or "").strip()
    partial_root = (direction.get("partial_diagnosis_root_cause") or "").strip()
    partial_files = direction.get("partial_diagnosis_files") or []
    if partial_summary or partial_root or partial_files:
        partial_lines = ["\nPartial diagnosis from prior failed investigation:"]
        if partial_summary:
            partial_lines.append(f"- summary: {partial_summary[:400]}")
        if partial_root:
            partial_lines.append(f"- root cause: {partial_root[:400]}")
        if partial_files:
            files_str = ", ".join(str(f) for f in partial_files[:10])[:300]
            partial_lines.append(f"- affected files: {files_str}")
        prompt_lines.append("\n".join(partial_lines))
    rem_attempts = int(direction.get("remediation_attempts") or 0)
    last_rem_result = direction.get("last_remediation_result")
    last_rem_err = (direction.get("last_remediation_error_summary") or "")[:300]
    last_rem_fc = direction.get("last_remediation_failure_class")
    if rem_attempts or last_rem_result or last_rem_err:
        rem_lines = [f"\nRemediation history ({rem_attempts} prior attempt(s)):"]
        if last_rem_result:
            rem_lines.append(f"- last result: {last_rem_result}")
        if last_rem_fc:
            rem_lines.append(f"- last failure class: {last_rem_fc}")
        if last_rem_err:
            rem_lines.append(f"- last error: {last_rem_err}")
        prompt_lines.append("\n".join(rem_lines))

    prompt_lines.append(
        "\nProduce the structured diagnosis JSON report so remediation can be spawned. "
        "Build on prior findings above rather than re-diagnosing what is already known."
    )
    prompt = "\n".join(prompt_lines)

    inv_payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": _get_project_integration_branch(_project),
        },
        "autonomous_research:task_failure_investigation",
    )
    new_task = Task(
        project_id=task.project_id,
        title=f"Investigate task failure: {(task.title or '?')[:80]}",
        description=f"Auto-spawned after {ttype} task {str(task.id)[:8]} exhausted retries (CODE_ERROR/code_defect)",
        payloads=[inv_payload],
        context={
            "direction_id": direction_id,
            "spawned_by": "task_failure_investigation",
            "source_task_id": str(task.id),
            "last_failure_class": last_failure_class,
            "failure_taxonomy": failure_taxonomy,
            "failure_fingerprints": [prev_fp] if prev_fp else [],
            "investigation_depth": next_depth,
            **(
                {"diagnosis_fingerprint": prev_fp, "error_fingerprint": prev_fp}
                if prev_fp
                else {}
            ),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "TASK_FAILURE_INVESTIGATION",
                "direction_id": direction_id,
                "source_task_id": str(task.id),
            },
        )
    )
    logger.info(
        f"[TASK FAILURE INVESTIGATION] Spawned investigation {new_task.id} for "
        f"failed task {task.id} (direction {direction_id})"
    )
    return True


async def _spawn_review_convergence_investigation(
    orig_task: Task,
    session: AsyncSession,
    *,
    pr_number: int,
    iterations: int,
    detail: str,
    trigger: str,
    review_history: list | None = None,
) -> uuid.UUID | None:
    """Spawn an investigate task for review convergence failures.

    Fired from ``review_pipeline._handle_cap_exhaustion`` (trigger=
    ``review_cap_exhausted``) and the stuck-cycle path in
    ``_spawn_pipeline_task`` (trigger=``review_stuck_cycle``).

    Guards (mirroring _spawn_task_failure_investigation):
    - Skip when orig_task has no direction_id (no direction to diagnose).
    - Skip investigate/smoketest types to avoid self-referential loops.
    - Respect the shared 24h per-direction investigation dedup.
    """
    ttype = (orig_task.first_payload or {}).get("type") or ""
    if ttype in ("investigate", "smoketest"):
        return None

    direction_id = (orig_task.context or {}).get("direction_id")
    if not direction_id:
        return None

    if await _recent_investigation_exists(orig_task.project_id, direction_id, session):
        return None

    from app.anomaly_detection import _error_fingerprint
    from app.failure_classification import CODE_ERROR, TAXONOMY_REVIEW_QUALITY
    from app.task_completion import _find_sibling_fingerprint_investigation

    fp_source = (orig_task.latest_status_message or "").strip()
    if not fp_source:
        history_for_fp = (
            review_history
            or (orig_task.context or {}).get("review_loop", {}).get("review_history")
            or []
        )
        for entry in reversed(history_for_fp):
            for iss in entry.get("issues") or []:
                desc = (iss.get("description") or "").strip()
                if desc:
                    fp_source = desc
                    break
            if fp_source:
                break
            fb = entry.get("feedback")
            if fb:
                fp_source = str(fb).strip()
                break
    diagnosis_fingerprint = _error_fingerprint(fp_source) if fp_source else ""

    if diagnosis_fingerprint:
        sibling_cutoff = _utcnow() - timedelta(hours=24)
        shared_id = await _find_sibling_fingerprint_investigation(
            orig_task.project_id,
            diagnosis_fingerprint,
            sibling_cutoff,
            session,
        )
        if shared_id is not None:
            logger.info(
                f"[REVIEW CONVERGENCE] Skip spawn for PR #{pr_number} "
                f"(direction {direction_id}): shares fingerprint "
                f"{diagnosis_fingerprint[:40]} with existing investigate task {shared_id}"
            )
            return None

    _project = await session.get(Project, orig_task.project_id)

    history = (
        review_history
        or (orig_task.context or {}).get("review_loop", {}).get("review_history")
        or []
    )
    issues_lines: list[str] = []
    for entry in history[-3:]:
        it = entry.get("iteration", "?")
        issues = entry.get("issues") or []
        for iss in issues[:5]:
            sev = (iss.get("severity") or "").upper()
            desc = (iss.get("description") or "").strip()[:200]
            if desc:
                issues_lines.append(f"  - [iter {it}] [{sev}] {desc}")
        if not issues and entry.get("feedback"):
            issues_lines.append(f"  - [iter {it}] {str(entry.get('feedback'))[:200]}")
    issues_block = (
        "\n".join(issues_lines)
        if issues_lines
        else "(no structured review issues captured)"
    )

    trigger_label = (
        "cap exhausted" if trigger == "review_cap_exhausted" else "stuck cycle"
    )
    prompt = (
        f"Review-fix convergence failure on PR #{pr_number} ({trigger_label}) after "
        f"{iterations} iterations. Diagnose why the review loop could not converge.\n\n"
        f"Direction: {direction_id}\n"
        f"Source task: {str(orig_task.id)[:8]} ({ttype})\n"
        f"Detail: {detail}\n\n"
        f"Repeating review issues:\n{issues_block}\n\n"
        f"Consider: scope too large for one PR, architectural mismatch, wrong approach, "
        f"direction misalignment, or test-coverage/correctness gaps the fix agent cannot "
        f"close in scope. Produce the structured diagnosis JSON report."
    )

    inv_payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": _get_project_integration_branch(_project),
        },
        "autonomous_research:review_convergence_investigation",
    )
    new_task = Task(
        project_id=orig_task.project_id,
        title=f"Investigate review convergence: PR #{pr_number}",
        description=f"Auto-spawned after review {trigger_label} on PR #{pr_number} ({iterations} iterations)",
        payloads=[inv_payload],
        context={
            "direction_id": direction_id,
            "spawned_by": "review_convergence_investigation",
            "source_task_id": str(orig_task.id),
            "pr_number": pr_number,
            "review_iterations": iterations,
            "convergence_trigger": trigger,
            "convergence_detail": detail,
            "last_failure_class": CODE_ERROR,
            "failure_taxonomy": TAXONOMY_REVIEW_QUALITY,
            "failure_fingerprints": [diagnosis_fingerprint]
            if diagnosis_fingerprint
            else [],
            **(
                {"diagnosis_fingerprint": diagnosis_fingerprint}
                if diagnosis_fingerprint
                else {}
            ),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "REVIEW_CONVERGENCE_INVESTIGATION",
                "direction_id": direction_id,
                "source_task_id": str(orig_task.id),
                "pr_number": pr_number,
                "trigger": trigger,
            },
        )
    )
    logger.info(
        f"[REVIEW CONVERGENCE] Spawned investigation {new_task.id} for "
        f"PR #{pr_number} ({trigger}, direction {direction_id})"
    )
    return new_task.id


async def _infer_fingerprint_audit_system(
    project: Project,
    session: AsyncSession,
    task_ids: list,
) -> str | None:
    """Return system_id if fingerprint tasks all map to a single system in audit/fundamentals mode.

    Reuses the direction→system_id mapping from project.directions (same as self_audit_dispatch).
    Returns None when the mapping is ambiguous (no direction link, multiple systems, or unset mode).
    """
    from app.models import SystemMaturity

    dir_to_system: dict[str, str] = {
        d["id"]: d["system_id"]
        for d in (project.directions or [])
        if d.get("id") and d.get("system_id")
    }
    if not dir_to_system or not task_ids:
        return None
    uuids = []
    for tid in task_ids[:5]:
        with contextlib.suppress(ValueError, AttributeError):
            uuids.append(uuid.UUID(str(tid)))
    if not uuids:
        return None
    rows = (await session.execute(select(Task.context).where(Task.id.in_(uuids)))).all()
    system_ids: set[str] = set()
    for (ctx,) in rows:
        did = (ctx or {}).get("direction_id")
        if did and did in dir_to_system:
            system_ids.add(dir_to_system[did])
    if len(system_ids) != 1:
        return None
    sid = next(iter(system_ids))
    mat = (
        await session.execute(
            select(SystemMaturity).where(
                SystemMaturity.project_id == project.id,
                SystemMaturity.system_id == sid,
            )
        )
    ).scalar_one_or_none()
    return sid if mat and mat.recommended_mode in ("audit", "fundamentals") else None


async def _spawn_residual_fingerprint_remediation(
    project: Project, session: AsyncSession
) -> int:
    """Spawn a create-type remediation task for the top uncooled residual CODE_ERROR fingerprint.

    Scans get_top_residual_fingerprints() and spawns at most one create task per
    call — the highest-count fingerprint whose per-project cooldown has expired.
    The cooldown (default 24 h, configurable via residual_remediation_cooldown_hours)
    prevents re-spawning while a prior remediation is still live or recently merged.

    Global concurrency cap: if there are already `max_concurrent_auto_remediations`
    (default 2) auto-remediation tasks active or queued, skip spawning. Prevents
    the queue from ballooning when many distinct fingerprints accumulate, e.g.
    when an ambient bug on main causes every task to fail in a slightly
    different place and each surface-level error gets its own fingerprint.

    Returns the number of tasks spawned (0 or 1).
    """
    from app.failure_classification import get_top_residual_fingerprints
    from app.models import ResidualCaseStatus, ResidualRemediationCase
    from sqlalchemy import select

    fingerprints = get_top_residual_fingerprints(window_seconds=86400, top_n=10)
    if not fingerprints:
        return 0

    # Load case rows we need for spawn-side filters in one query:
    # IGNORED + SUSPENDED block re-spawn; REMEDIATING + last_spawned_at
    # drive the per-fingerprint cooldown. Row count for REMEDIATING
    # also drives the project-wide concurrency cap (see below).
    case_rows_result = await session.execute(
        select(
            ResidualRemediationCase.fingerprint,
            ResidualRemediationCase.status,
            ResidualRemediationCase.last_spawned_at,
        ).where(ResidualRemediationCase.project_id == project.id)
    )
    case_rows = case_rows_result.all()
    blocked_fingerprints: set[str] = {
        fp
        for fp, status, _ in case_rows
        if status in (ResidualCaseStatus.IGNORED, ResidualCaseStatus.SUSPENDED)
    }
    last_spawned_by_fp: dict[str, datetime] = {
        fp: ts for fp, _, ts in case_rows if ts is not None
    }

    # Project-wide concurrency cap. Count case rows currently in the
    # in-flight REMEDIATING state — independent of the CI auto-fix
    # quota since that lives on project.design_doc._auto_remediation.
    # The DB row count is authoritative; old in-memory dedup is gone.
    max_concurrent = int(_runtime_config.get("max_concurrent_auto_remediations", 2))
    active_count = sum(
        1 for _, status, _ in case_rows if status == ResidualCaseStatus.REMEDIATING
    )
    if active_count >= max_concurrent:
        logger.info(
            "[AUTO REMEDIATION] %d residual case(s) already REMEDIATING, "
            "at or above cap (%d); skipping spawn (project %s).",
            active_count,
            max_concurrent,
            project.id,
        )
        return 0

    cooldown_hours = _runtime_config.get("residual_remediation_cooldown_hours", 24)
    cooldown_seconds = cooldown_hours * 3600
    # Higher count threshold for fingerprints whose source isn't error-shaped
    # (latest_status_message was a routine heartbeat — Cloning, Using tool,
    # PROVISION_PROGRESS, etc. — and no error line surfaced from the logs).
    # A single one-off task dying with such a status is noise; many tasks
    # dying with the same heartbeat is a real signal worth investigating.
    no_error_min_count = int(
        _runtime_config.get("residual_remediation_no_error_min_count", 20)
    )
    now_dt = _utcnow()
    cooldown_delta = timedelta(seconds=cooldown_seconds)

    for fp_entry in fingerprints:
        fp = fp_entry["fingerprint"]
        if fp in blocked_fingerprints:
            logger.debug(
                "[AUTO REMEDIATION] Skipping IGNORED/SUSPENDED fingerprint %s (project %s).",
                fp[:32],
                project.id,
            )
            continue
        last_spawned = last_spawned_by_fp.get(fp)
        if last_spawned is not None and (now_dt - last_spawned) < cooldown_delta:
            continue

        # Defer if all sampled tasks map to a single system in audit/fundamentals mode.
        _audit_sid = await _infer_fingerprint_audit_system(
            project, session, fp_entry.get("task_ids") or []
        )
        if _audit_sid:
            logger.info(
                "[AUTO REMEDIATION] Deferring fingerprint %s: system %r is in "
                "audit/fundamentals mode (project %s).",
                fp[:32],
                _audit_sid,
                project.id,
            )
            await _record_residual_audit_deferral(session, project.id, fp, _audit_sid)
            continue

        # Require a high count before remediating non-error-shaped clusters.
        if (
            not fp_entry.get("is_error_shaped", True)
            and fp_entry.get("count", 0) < no_error_min_count
        ):
            logger.info(
                "[AUTO REMEDIATION] Skipping fingerprint %s: heartbeat-only cluster "
                "(no error-shaped contribution) at count=%d, threshold=%d. "
                "Sample: %.80s",
                fp[:32],
                fp_entry.get("count", 0),
                no_error_min_count,
                (fp_entry.get("sample_message") or "")[:80],
            )
            continue

        # Prefer the list-of-samples when present (added so fingerprints
        # accumulate distinct error-line variations across tasks); fall
        # back to the single legacy fields.
        sample_messages = fp_entry.get("sample_messages") or (
            [fp_entry["sample_message"]] if fp_entry.get("sample_message") else []
        )

        # Re-classify the primary sample against the current classifier.
        # Fingerprints can linger in the in-memory store for up to 24 h after
        # the underlying classify_failure fix is deployed (the store is
        # process-local; it doesn't clear on code push). If the sample now
        # routes to AGENT_TIMEOUT (or any non-CODE_ERROR class), the issue
        # has already been addressed and spawning another remediation task
        # would waste capacity and trigger an identical cycle 24 h later.
        if sample_messages:
            from app.failure_classification import (
                classify_failure as _rfc,
                CODE_ERROR as _CE,
            )
            import types as _types

            _probe = _types.SimpleNamespace(
                context={},
                latest_status_message=sample_messages[0],
                id=None,
            )
            if _rfc(_probe) != _CE:
                logger.info(
                    "[AUTO REMEDIATION] Skipping fingerprint %s: primary sample "
                    "no longer classifies as CODE_ERROR (underlying fix already deployed). "
                    "Sample: %.80s",
                    fp[:32],
                    sample_messages[0],
                )
                continue

        sample_statuses = fp_entry.get("sample_statuses") or (
            [fp_entry["sample_status"]] if fp_entry.get("sample_status") else []
        )
        task_ids = fp_entry.get("task_ids") or []
        task_ids_str = ", ".join(str(t) for t in task_ids[:5])

        # Render each bucket as a numbered list when we have >1 sample, or
        # a single block when we have one. Capped list already from the
        # residual store; just join.
        def _render_samples(label: str, samples: list[str]) -> str:
            samples = [s for s in samples if s]
            if not samples:
                return ""
            if len(samples) == 1:
                return f"\n\n{label}:\n{samples[0]}"
            bodies = "\n\n".join(f"[{i + 1}] {s}" for i, s in enumerate(samples))
            return f"\n\n{label} ({len(samples)} variations across affected tasks):\n{bodies}"

        samples_block = _render_samples("Sample error message(s)", sample_messages)
        # Status context — show only those that differ from the error lines.
        distinct_statuses = [
            s
            for s in sample_statuses
            if s and not any(s.strip() == m.strip() for m in sample_messages)
        ]
        status_block = _render_samples(
            "Agent status when these failures hit (context, not the error)",
            distinct_statuses,
        )

        prompt = (
            f"You are spawned as an auto-remediation task. A CODE_ERROR "
            f"fingerprint has been recurring on main for {fp_entry.get('count', '?')} "
            f"task(s) in the last 24 hours and nothing in the pipeline has "
            f"fixed it. Your job is to investigate and open a fix PR.\n\n"
            f"IMPORTANT — read this carefully:\n"
            f"- This bug exists on main TODAY. It is not caused by your "
            f"changes. Your job is to fix it.\n"
            f"- The default 'don't touch pre-existing failures' instinct "
            f"DOES NOT APPLY here. You are EXPLICITLY authorized (and "
            f"expected) to edit unrelated-looking code, test mocks, "
            f"fixtures, or imports when the error points there.\n"
            f"- Common causes: missing exports on test mocks, stale "
            f"fixtures, recent refactors that broke unrelated tests, "
            f"missing migration, imports pointing at a moved file.\n"
            f"- First, reproduce the error locally — run the relevant test "
            f"suite and confirm you see the failure before editing.\n"
            f"- Do NOT fix by silencing, skipping, weakening assertions, "
            f"or try/except-wrapping errors. Find and fix the real cause.\n\n"
            f"Fingerprint: '{fp}'"
            f"{samples_block}"
            f"{status_block}\n\n"
            f"Affected task IDs (up to 5): {task_ids_str or '(none recorded)'}\n\n"
            f"Open a fix PR that resolves the underlying code defect."
        )
        payload = validate_internal_payload(
            {
                "type": "create",
                "prompt": prompt,
                "base_branch": _get_project_integration_branch(project),
            },
            "autonomous_research:residual_fingerprint_remediation",
        )
        primary_sample = sample_messages[0] if sample_messages else ""
        # Compute investigation_depth for the remediation. We look up the
        # max depth among task_ids (the failed tasks that contributed to
        # this fingerprint) and add 1. If the deepest source was at depth 1
        # (already a remediation that failed), the new remediation lands at
        # depth 2 — and a subsequent failure won't trigger investigation #3
        # (gated by MAX_INVESTIGATION_DEPTH=2 in _spawn_task_failure_investigation).
        source_max_depth = 0
        if task_ids:
            try:
                rows = (
                    await session.execute(
                        select(Task.context).where(
                            Task.id.in_(
                                [
                                    uuid.UUID(t) if isinstance(t, str) else t
                                    for t in task_ids[:5]
                                ]
                            )
                        )
                    )
                ).all()
                for (rc,) in rows:
                    d = int((rc or {}).get("investigation_depth") or 0)
                    if d > source_max_depth:
                        source_max_depth = d
            except Exception as _depth_exc:  # noqa: BLE001
                logger.debug(
                    f"[AUTO REMEDIATION] depth lookup failed for {fp[:32]}: {_depth_exc}"
                )
        remediation_context = {
            "auto_remediation": True,
            "spawned_by": "residual_fingerprint_remediation",
            "error_fingerprint": fp,
            "sample_message": primary_sample[:500],
            "task_ids": task_ids,
            "investigation_depth": source_max_depth + 1,
        }
        new_task = Task(
            project_id=project.id,
            title=f"Remediate residual fingerprint: {fp[:60]}",
            description=f"Auto-spawned remediation for residual CODE_ERROR fingerprint {fp[:40]}",
            payloads=[payload],
            context=remediation_context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        session.add(
            TaskHistory(
                task_id=new_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "RESIDUAL_FINGERPRINT_REMEDIATION",
                    "error_fingerprint": fp,
                },
            )
        )
        # Stamp REMEDIATING + last_spawned_* on the case row and emit the
        # spawn message. Subsequent ticks read the same row to enforce the
        # per-fingerprint cooldown — no in-memory dedup dict.
        from app.notification_dispatch import post_residual_fix_spawned_message

        try:
            await post_residual_fix_spawned_message(
                session,
                project.id,
                fingerprint=fp,
                sample_message=primary_sample,
                task_id=new_task.id,
            )
        except Exception as _spawn_msg_exc:  # noqa: BLE001
            logger.warning(
                "[AUTO REMEDIATION] Failed to post spawned message for %s: %s",
                fp[:32],
                _spawn_msg_exc,
            )
        logger.info(
            f"[AUTO REMEDIATION] Spawned create task {new_task.id} for residual "
            f"fingerprint {fp[:40]} (project {project.id})"
        )
        await session.commit()
        return 1

    return 0
