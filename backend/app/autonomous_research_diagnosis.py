"""Autonomous direction-diagnosis research spawning.

Extracted from :mod:`app.autonomous_research` to keep that module within its
size budget. The function is still re-exported from ``autonomous_research``
so existing callers (``direction_lifecycle`` and tests that import it from
there) continue to work unchanged.
"""

import logging
import subprocess
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    DeploymentState,
    Project,
    RegressionRemediationCase,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.strategic_completion import _is_code_failure_pause_reason
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Dedup window for unproductive-pause investigations. Shorter than the 48h
# direction cooldown so a direction that bounces between saturation/unproductive
# states within a week still gets one fresh diagnosis per pause event.
_UNPRODUCTIVE_DIAGNOSIS_DEDUP_HOURS = 24

_RECENT_CHANGES_COMMIT_CAP = 30
_RECENT_CHANGES_BYTES_CAP = 4096


async def _get_recent_changes_context(session: AsyncSession) -> dict:
    """Fetch deploy timestamp and recent git commits for investigation payloads.

    Serializes the data at spawn time so the investigation VM (which has no
    repo access) can reference what shipped recently.
    """
    deploy_row = (
        (await session.execute(select(DeploymentState).where(DeploymentState.id == 1)))
        .scalars()
        .first()
    )

    deployed_at_iso = deploy_row.deployed_at.isoformat() if deploy_row else None
    current_sha = (deploy_row.current_sha or "") if deploy_row else ""
    prior_sha = (
        (getattr(deploy_row, "prior_deploy_sha", None) or "") if deploy_row else ""
    )

    commits: list[str] = []
    if deployed_at_iso:
        # Prefer the explicit prior_deploy_sha..current_sha range when both shas
        # are recorded — it gives a precise commit list independent of the
        # checkout's wall-clock state. Fall back to --since=deployed_at when the
        # prior sha hasn't been captured yet (first deploy after the migration).
        if prior_sha and current_sha and prior_sha != current_sha:
            log_args = [
                "git",
                "log",
                "--oneline",
                f"-{_RECENT_CHANGES_COMMIT_CAP}",
                f"{prior_sha}..{current_sha}",
            ]
        else:
            log_args = [
                "git",
                "log",
                "--oneline",
                f"-{_RECENT_CHANGES_COMMIT_CAP}",
                f"--since={deployed_at_iso}",
            ]
        try:
            result = subprocess.run(
                log_args,
                capture_output=True,
                text=True,
                timeout=10,
            )
            raw = result.stdout.strip()
            if raw:
                # Trim to byte cap before splitting to avoid huge payloads.
                commits = raw[:_RECENT_CHANGES_BYTES_CAP].splitlines()
            elif result.returncode != 0:
                # Backend pods normally don't ship the git checkout; surface the
                # failure at WARNING so prod regressions in this signal are visible.
                logger.warning(
                    "[DIAGNOSIS] git log returned non-zero (rc=%s) — "
                    "recent_changes block will be empty: %s",
                    result.returncode,
                    (result.stderr or "").strip()[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DIAGNOSIS] git log failed; omitting commit list: %s",
                exc,
            )

    return {
        "deployed_at": deployed_at_iso,
        "current_sha": current_sha or None,
        "commits": commits,
        "commit_count": len(commits),
    }


async def _spawn_direction_diagnosis(
    project: Project,
    direction: dict,
    pause_reason: str,
    session: AsyncSession,
    retry_context: dict | None = None,
) -> bool:
    """Spawn a diagnosis research task for a freshly auto-paused direction.

    Diagnosis research investigates WHY the direction keeps failing — it looks at
    the direction's recent error signatures, failure classifications, and playbook
    entries to produce targeted fix findings that address the root cause before the
    direction's 48h cooldown elapses.

    When ``retry_context`` is supplied, the task is a re-spawn after a prior
    diagnosis stalled (reaped/timed-out). The payload carries the retry metadata
    so the agent takes a different investigation angle.

    Returns True if a new diagnosis task was created, False if skipped (already an
    active diagnosis task for this direction, or the spawn couldn't be completed).
    """
    did = direction.get("id")
    if not did:
        return False

    # Skip if a diagnosis research is already active for this direction —
    # one diagnosis per pause event is plenty.
    existing = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                    Task.context["diagnosis_for_direction"].as_string() == did,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        logger.info(
            f"[DIAGNOSIS] Skipping diagnosis spawn for direction {did}: "
            f"task {existing.id} already active"
        )
        return False

    # Gather recent failed-task signatures for this direction to inform the agent.
    recent_failed_rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.FAILED,
                    Task.context["direction_id"].as_string() == did,
                )
                .order_by(Task.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    failed_signatures: list[dict] = []
    taxonomy_counts: dict[str, int] = {}
    for t in recent_failed_rows:
        ctx = t.context or {}
        payload = t.first_payload or {}
        category = ctx.get("failure_category") or ""
        taxonomy = ctx.get("failure_taxonomy") or ""
        signature = {
            "task_id": str(t.id),
            "title": (t.title or "")[:200],
            "task_type": payload.get("type", ""),
            "failure_category": category,
            "failure_taxonomy": taxonomy,
            "status_message": (t.latest_status_message or "")[:400],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        fingerprint = ctx.get("failure_fingerprint") or ctx.get("failure_stage")
        if fingerprint:
            signature["failure_fingerprint"] = str(fingerprint)[:200]
        failed_signatures.append(signature)
        if taxonomy:
            taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
        elif category:
            taxonomy_counts[category] = taxonomy_counts.get(category, 0) + 1

    # Pull playbook entries (operational knowledge) for the agent to reference.
    # Filter out pending_source entries — those are mid-flight writes from
    # tasks that haven't completed yet; surfacing them poisons peers when the
    # writing task ultimately fails (see main.py update_playbook).
    doc = project.design_doc or {}
    playbook_entries: dict[str, str] = {}
    for k, v in doc.items():
        if k.startswith("playbook/") and isinstance(v, dict):
            if v.get("pending_source"):
                continue
            content = v.get("content") or ""
            if content:
                playbook_entries[k] = content[:2000]

    # Compact direction summary — just this direction, with recent history attached.
    raw_history = direction.get("research_history") or []
    _HISTORY_NUMERIC_FIELDS = {
        "tasks_created",
        "prs_merged",
        "tasks_failed",
        "tasks_failed_infra",
        "scope_breakdown",
    }
    compact_history = [
        {k: entry[k] for k in _HISTORY_NUMERIC_FIELDS if k in entry}
        for entry in raw_history[-5:]
    ]
    direction_summary: dict = {
        "id": did,
        "text": direction.get("text"),
        "priority": direction.get("priority"),
        "system_id": direction.get("system_id"),
        "paths": direction.get("paths") or [],
        "auto_pause_count": direction.get("auto_pause_count", 0),
    }
    if direction.get("category"):
        direction_summary["category"] = direction["category"]
    if compact_history:
        direction_summary["research_history"] = compact_history

    # Design doc summary — keys + playbook entries (same shape as standard research).
    design_doc_summary: dict = {
        "_keys": [
            k for k in doc if not k.startswith("_") and not k.startswith("playbook/")
        ],
    }
    design_doc_summary.update(playbook_entries)

    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = (
        _runtime_config.get("task_type_efforts", {}).get("research") or None
    )
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")

    remediation_history = {
        "remediation_attempts": int(direction.get("remediation_attempts") or 0),
        "last_remediation_result": direction.get("last_remediation_result"),
        "last_remediation_at": direction.get("last_remediation_at"),
        "last_remediation_task_id": direction.get("last_remediation_task_id"),
        "last_remediation_fingerprint": direction.get("last_remediation_fingerprint"),
        "last_remediation_failure_class": direction.get(
            "last_remediation_failure_class"
        ),
        "last_remediation_error_summary": direction.get(
            "last_remediation_error_summary"
        ),
    }

    research_payload: dict = {
        "type": "research",
        "research_mode": "diagnosis",
        "directions": [direction_summary],
        "current_direction_id": did,
        "design_doc": design_doc_summary,
        "base_branch": "main",
        "pause_reason": pause_reason,
        "failed_task_signatures": failed_signatures,
        "failure_taxonomy_counts": taxonomy_counts,
        "remediation_history": remediation_history,
        **({"model": research_model} if research_model else {}),
        **({"effort": research_effort} if research_effort else {}),
        **({"prompt_suffix": research_suffix} if research_suffix else {}),
        **({"retry_context": retry_context} if retry_context else {}),
    }
    if project.build_config:
        research_payload["build_config"] = project.build_config
    research_payload = validate_internal_payload(
        research_payload, "autonomous_research:diagnosis"
    )

    direction_text = (direction.get("text") or "direction")[:160]
    retry_attempt = retry_context.get("retry_attempt") if retry_context else None
    title_prefix = (
        f"Diagnosis (retry {retry_attempt})" if retry_attempt else "Diagnosis"
    )
    task_context: dict = {
        "diagnosis_for_direction": did,
        "direction_id": did,
        "pause_reason": pause_reason,
    }
    if retry_context:
        task_context["diagnosis_retry"] = True
        task_context["diagnosis_retry_attempt"] = retry_attempt
        if retry_context.get("prior_task_id"):
            task_context["prior_diagnosis_task_id"] = retry_context["prior_task_id"]
    new_task = Task(
        project_id=project.id,
        title=f"{title_prefix}: {direction_text}",
        description=(
            f"Auto-paused diagnosis for direction: {direction_text}"
            + (
                f" (retry {retry_attempt}: prior task {retry_context.get('prior_task_id')} "
                f"timed out)"
                if retry_context
                else ""
            )
        )[:500],
        payloads=[research_payload],
        context=task_context,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={
            "spawned_by": "AUTONOMOUS_LOOP",
            "trigger": "direction_auto_pause_diagnosis",
            "pause_reason": pause_reason,
            **({"retry_attempt": retry_attempt} if retry_attempt else {}),
        },
    )
    session.add(history)
    logger.info(
        f"[DIAGNOSIS] Spawned diagnosis research {new_task.id} for direction {did}: "
        f"{pause_reason} ({len(failed_signatures)} failed task signatures"
        + (f", retry {retry_attempt}" if retry_attempt else "")
        + ")"
    )
    return True


async def _spawn_unproductive_diagnosis(
    project: Project,
    direction: dict,
    pause_reason: str,
    session: AsyncSession,
) -> bool:
    """Spawn a lightweight investigate task for a non-code-failure pause.

    Saturation (Rule 1) and Unproductive (Rule 2) pauses won't benefit from the
    code-failure diagnosis prompt: the question isn't "what pattern keeps
    breaking?" but "why isn't this direction producing merged PRs?". This
    investigation examines scope calibration, finding relevance, saturation
    signals, and whether the direction's goal may already be met.

    Guards: pause reason must be non-code-failure; dedup against any
    unproductive-diagnosis task for this direction within the last 24h
    (active OR recently created).

    Returns True when a task was created, False when skipped.
    """
    if _is_code_failure_pause_reason(pause_reason):
        return False
    did = direction.get("id")
    if not did:
        return False

    cutoff = _utcnow() - timedelta(hours=_UNPRODUCTIVE_DIAGNOSIS_DEDUP_HOURS)
    existing = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == project.id,
                Task.context["unproductive_diagnosis_direction_id"].as_string()
                == str(did),
                Task.created_at >= cutoff,
            )
            .limit(1)
        )
    ).first()
    if existing is not None:
        logger.info(
            f"[DIAGNOSIS] Skipping unproductive diagnosis for direction {did}: "
            f"recent investigation already exists"
        )
        return False

    history = direction.get("research_history") or []
    recent_lines = (
        "\n".join(
            f"- created={e.get('tasks_created', 0)} completed={e.get('tasks_completed', 0)} "
            f"failed={e.get('tasks_failed', 0)} merged={e.get('prs_merged', 0)} "
            f"scope={e.get('scope_breakdown') or {}}"
            for e in history[-5:]
        )
        or "- (no research history)"
    )
    direction_text = (direction.get("text") or "")[:200]

    recent_changes = await _get_recent_changes_context(session)
    changes_lines = ""
    if recent_changes.get("deployed_at"):
        changes_lines = (
            f"\n\nRecent changes (since deploy at {recent_changes['deployed_at']}"
            + (
                f", sha {recent_changes['current_sha']}"
                if recent_changes.get("current_sha")
                else ""
            )
            + "):\n"
            + (
                "\n".join(f"  {c}" for c in recent_changes["commits"])
                if recent_changes["commits"]
                else "  (no commits found since last deploy)"
            )
        )

    prompt = (
        f"A research direction was auto-paused for non-code reasons.\n\n"
        f"Direction: {direction_text}\n"
        f"Pause reason: {pause_reason}\n"
        f"Auto-pause count: {direction.get('auto_pause_count', 0)}\n\n"
        f"Recent cycles (tasks_created / completed / failed / merged / scope):\n"
        f"{recent_lines}"
        f"{changes_lines}\n\n"
        f"Investigate why this direction is not producing merged PRs. Consider:\n"
        f"- Is the direction's goal already substantially met by existing code?\n"
        f"- Are spawned findings too narrow, too broad, or irrelevant to the goal?\n"
        f"- Is scope calibration mis-sized (too small to ship value, too large to pass review)?\n"
        f"- Is the direction saturated (all useful changes already shipped, now scraping edges)?\n"
        f"Recommend one of: retire direction, reshape goal text, adjust scope targeting, or resume."
    )
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": "main",
            "effort": "low",
            "recent_changes": recent_changes,
        },
        "autonomous_research_diagnosis:unproductive",
    )
    new_task = Task(
        project_id=project.id,
        title=f"Investigate unproductive direction: {direction_text[:60]}",
        description=f"Auto-pause reason: {pause_reason}"[:500],
        payloads=[payload],
        context={
            "direction_id": str(did),
            "pause_reason": pause_reason,
            "diagnosis_trigger": "direction_unproductive_pause",
            "unproductive_diagnosis_direction_id": str(did),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=60,
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "AUTONOMOUS_LOOP",
                "trigger": "direction_unproductive_pause",
                "pause_reason": pause_reason,
            },
        )
    )
    direction["unproductive_diagnosis_spawned_at"] = _utcnow().isoformat()
    logger.info(
        f"[DIAGNOSIS] Spawned unproductive investigate {new_task.id} for direction {did}: "
        f"{pause_reason[:60]}"
    )
    return True


_ACTIVE_TASK_STATUSES = frozenset(
    [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
)


async def _spawn_remediation_stall_diagnosis(
    project: Project,
    case: RegressionRemediationCase,
    session: AsyncSession,
) -> bool:
    """Spawn an enriched stall-diagnosis investigate for a stalled ACTIVE remediation.

    Richer companion to the dispatcher's inline auto-escalation: includes recent
    deployment changes and a full child-task inventory in the prompt so the
    investigating agent has maximum context about what is blocking convergence.

    Returns True if a new task was created, False if skipped (no open child).
    """
    if case.last_remediation_task_id is None:
        return False

    child = await session.get(Task, case.last_remediation_task_id)
    if child is None or child.status not in _ACTIVE_TASK_STATUSES:
        return False

    recent_changes = await _get_recent_changes_context(session)
    changes_block = ""
    if recent_changes.get("deployed_at"):
        sha_note = (
            f", sha {recent_changes['current_sha']}"
            if recent_changes.get("current_sha")
            else ""
        )
        commit_lines = (
            "\n".join(f"  {c}" for c in recent_changes["commits"])
            if recent_changes["commits"]
            else "  (no commits found since last deploy)"
        )
        changes_block = (
            f"\n\nRecent changes (since deploy at {recent_changes['deployed_at']}"
            f"{sha_note}):\n{commit_lines}"
        )

    child_summary = (
        f"  task_id={child.id} status={child.status} "
        f"message={(child.latest_status_message or '')[:200]!r}"
    )
    prompt = (
        f"A remediation for regression class '{case.regression_class}' has been "
        f"ACTIVE for >24h without resolving the underlying signal.\n\n"
        f"Existing children:\n{child_summary}\n\n"
        f"Diagnose the REMEDIATION BLOCKER (not the original signal): "
        f"merge conflict patterns, reviewer/CI disagreement on prior fix attempts, "
        f"infrastructure preconditions (toolchain, missing system libs), or "
        f"leader-election / orchestrator cascade issues are the most common "
        f"stall classes. Produce the structured diagnosis JSON "
        f"({{summary, root_cause, recommended_fix, severity, confidence, "
        f"affected_files}}). Do NOT re-investigate the original signal."
        f"{changes_block}"
    )
    payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": prompt,
            "base_branch": "main",
            "effort": "low",
            "recent_changes": recent_changes,
        },
        "autonomous_research_diagnosis:remediation_stall",
    )
    new_task = Task(
        project_id=project.id,
        title=f"Diagnose stalled remediation: {case.regression_class}"[:200],
        description=(
            "Auto-escalation: remediation case ACTIVE >24h with open child. "
            "Diagnosing the remediation blocker."
        )[:500],
        payloads=[payload],
        context={
            "spawned_by": "autonomous_research_diagnosis",
            "regression_class": case.regression_class,
            "signal_name": "remediation_stall_diagnosis",
            "auto_escalation": True,
            "prior_remediation_task_id": str(case.last_remediation_task_id),
            "remediation_case_id": str(case.id),
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
                "spawned_by": "AUTONOMOUS_RESEARCH_DIAGNOSIS",
                "regression_class": case.regression_class,
                "signal_name": "remediation_stall_diagnosis",
                "auto_escalation": True,
            },
        )
    )
    logger.info(
        "[DIAGNOSIS] Spawned remediation stall investigation %s for class '%s' "
        "(child=%s is still %s)",
        new_task.id,
        case.regression_class,
        case.last_remediation_task_id,
        child.status,
    )
    return True
