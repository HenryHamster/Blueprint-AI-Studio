"""Residual-fingerprint create-fix lane.

This is the explicitly-PRESERVED working auto-remediation lane that the
self-repair-apparatus strip kept. It was previously housed in
``app.autonomous_research_investigations`` (deleted with the apparatus); the
three functions below are the only survivors and were re-homed here intact:

* ``_infer_fingerprint_audit_system`` — maps a fingerprint's contributing
  tasks back to a single SystemMaturity row in audit/fundamentals mode (so
  the lane can defer remediation when the system is intentionally being
  audited rather than auto-fixed).
* ``_record_residual_audit_deferral`` — stamps the deferral on the
  ResidualRemediationCase row and one-shot-escalates at the configured
  threshold (re-homed from the deleted ``self_audit_dispatch``).
* ``_spawn_residual_fingerprint_remediation`` — the lane itself: spawns at
  most one create-type fix task per call for the top uncooled residual
  CODE_ERROR fingerprint.

``app.autonomous_research`` re-exports ``_spawn_residual_fingerprint_remediation``
so existing callers / tests that import it from there keep working.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow
from app.strategic_authority_locks import commit_legacy_authorized

logger = logging.getLogger(__name__)


async def _infer_fingerprint_audit_system(
    project: Project,
    session: AsyncSession,
    task_ids: list,
) -> str | None:
    """Return system_id if fingerprint tasks all map to a single system in audit/fundamentals mode.

    Reuses the direction→system_id mapping from project.directions. Returns
    None when the mapping is ambiguous (no direction link, multiple systems,
    or the system's recommended mode is not audit/fundamentals).
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


async def _record_residual_audit_deferral(
    session: AsyncSession,
    project_id: uuid.UUID,
    fingerprint: str,
    system_id: str,
) -> None:
    """Stamp a deferral event on the ResidualRemediationCase for *fingerprint*.

    Increments ``audit_defer_count`` and records ``audit_deferred_system_id`` so
    operators can see via GET /residual-cases why a case is held.
    The caller (``_spawn_residual_fingerprint_remediation``) commits after return.

    Re-homed from the deleted ``app.self_audit_dispatch``.
    """
    from app.models import ResidualRemediationCase

    case = (
        await session.execute(
            select(ResidualRemediationCase).where(
                ResidualRemediationCase.project_id == project_id,
                ResidualRemediationCase.fingerprint == fingerprint,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if case is None:
        return
    case.audit_defer_count = (case.audit_defer_count or 0) + 1
    case.audit_deferred_system_id = system_id
    case.updated_at = _utcnow()
    session.add(case)

    # One-shot escalation: fire exactly when count first reaches the threshold
    # (== not >=) so the alert fires once per deferral streak. The streak
    # resets to 0 in post_residual_fix_spawned_message when a fix is spawned,
    # allowing re-escalation on a future audit cycle.
    from app.notification_dispatch import (  # deferred — avoids circular import
        _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        post_residual_fix_audit_deferred_escalation_message,
    )

    threshold = int(
        _runtime_config.get(
            "residual_audit_defer_escalation_threshold",
            _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        )
    )
    if case.audit_defer_count == threshold:
        await post_residual_fix_audit_deferred_escalation_message(
            session,
            project_id,
            fingerprint=fingerprint,
            defer_count=case.audit_defer_count,
        )


async def _spawn_residual_fingerprint_remediation(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
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

    # Guard: per-project spend ceiling via canonical helper (also emits approaching-ceiling alert)
    from app.autonomous_research import (
        _project_spend_ceiling_spawn_defer_reason,
    )  # deferred — avoids circular import

    _spend_defer = await _project_spend_ceiling_spawn_defer_reason(
        project,
        session,
        expected_legacy_generation=expected_legacy_generation,
    )
    if _spend_defer:
        logger.info(
            "[AUTO REMEDIATION] Skipping residual spawn for project %s: %s",
            project.id,
            _spend_defer,
        )
        return 0
    if expected_legacy_generation is not None:
        from app.notification_dispatch import begin_deferred_external_notifications

        begin_deferred_external_notifications(session)

    # Load case rows we need for spawn-side filters in one query:
    # IGNORED + SUSPENDED block re-spawn; REMEDIATING + last_spawned_at
    # drive the per-fingerprint cooldown. Row count for REMEDIATING
    # also drives the project-wide concurrency cap (see below).
    # suspended_at + recovery_attempts drive exponential-backoff auto-recovery.
    case_rows_result = await session.execute(
        select(
            ResidualRemediationCase.fingerprint,
            ResidualRemediationCase.status,
            ResidualRemediationCase.last_spawned_at,
            ResidualRemediationCase.suspended_at,
            ResidualRemediationCase.recovery_attempts,
        ).where(ResidualRemediationCase.project_id == project.id)
    )
    case_rows = case_rows_result.all()
    blocked_fingerprints: set[str] = {
        fp
        for fp, status, _, _sat, _ra in case_rows
        if status in (ResidualCaseStatus.IGNORED, ResidualCaseStatus.SUSPENDED)
    }
    last_spawned_by_fp: dict[str, datetime] = {
        fp: ts for fp, _, ts, _sat, _ra in case_rows if ts is not None
    }

    # Auto-recover SUSPENDED cases whose exponential-backoff cooldown has elapsed.
    # This brings residual cases to parity with branch suspension auto-recovery.
    from app.auto_remediation import (  # deferred — avoids top-level circular import
        _suspension_cooldown_elapsed,
    )
    from app.notification_dispatch import (
        post_residual_fix_auto_recovered_message,
    )

    now_dt = _utcnow()
    recovered_any = False
    for fp, status, _ts, suspended_at, recovery_attempts in case_rows:
        if status != ResidualCaseStatus.SUSPENDED:
            continue
        suspended_at_str = suspended_at.isoformat() if suspended_at else None
        meta = {"suspended_at": suspended_at_str, "recovery_attempts": recovery_attempts or 0}
        if not _suspension_cooldown_elapsed(meta, now_dt):
            continue
        # Cooldown elapsed and below max attempts — auto-recover this case.
        case_row = (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.project_id == project.id,
                    ResidualRemediationCase.fingerprint == fp,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if case_row is None:
            continue
        new_attempts = (case_row.recovery_attempts or 0) + 1
        case_row.status = ResidualCaseStatus.ACTIVE
        case_row.suspended_at = None
        case_row.recovery_attempts = new_attempts
        case_row.updated_at = now_dt
        session.add(case_row)
        recovered_any = True
        blocked_fingerprints.discard(fp)
        logger.info(
            "[AUTO REMEDIATION] Auto-recovering SUSPENDED fingerprint %s "
            "(project %s, recovery attempt %d).",
            fp[:32],
            project.id,
            new_attempts,
        )
        await post_residual_fix_auto_recovered_message(
            session,
            project.id,
            fingerprint=fp,
            recovery_attempts=new_attempts,
        )
    if recovered_any:
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        if expected_legacy_generation is not None:
            from app.notification_dispatch import begin_deferred_external_notifications

            begin_deferred_external_notifications(session)

    # Project-wide concurrency cap. Count case rows currently in the
    # in-flight REMEDIATING state — independent of the CI auto-fix
    # quota since that lives on project.design_doc._auto_remediation.
    # The DB row count is authoritative; old in-memory dedup is gone.
    max_concurrent = int(_runtime_config.get("max_concurrent_auto_remediations", 2))
    active_count = sum(
        1 for _, status, _ts, _sat, _ra in case_rows if status == ResidualCaseStatus.REMEDIATING
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
        # (gated by MAX_INVESTIGATION_DEPTH=2 in the prior investigation lane).
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
            auto_remediation=True,
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
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        return 1

    return 0


async def _spawn_viewport_regression_remediation(
    project: Project,
    session: AsyncSession,
    *,
    expected_legacy_generation: int | None = None,
) -> int:
    """Spawn a create-type remediation task for the top uncooled viewport regression fingerprint.

    Mirrors _spawn_residual_fingerprint_remediation but for VIEWPORT_REGRESSION
    fingerprints stored in get_top_viewport_residual_fingerprints(). Applies the
    same cooldown, concurrency cap, blocked-fingerprint, and spend-ceiling checks.
    Skips the CODE_ERROR re-classification step since the fingerprint directly
    encodes component+assertion_type.

    Returns the number of tasks spawned (0 or 1).
    """
    from app.failure_classification import get_top_viewport_residual_fingerprints
    from app.models import ResidualCaseStatus, ResidualRemediationCase
    from sqlalchemy import select

    fingerprints = get_top_viewport_residual_fingerprints(
        window_seconds=86400, top_n=10
    )
    if not fingerprints:
        return 0

    # Guard: per-project spend ceiling via canonical helper (also emits approaching-ceiling alert)
    from app.autonomous_research import (
        _project_spend_ceiling_spawn_defer_reason,
    )  # deferred — avoids circular import

    _spend_defer = await _project_spend_ceiling_spawn_defer_reason(
        project,
        session,
        expected_legacy_generation=expected_legacy_generation,
    )
    if _spend_defer:
        logger.info(
            "[VIEWPORT REMEDIATION] Skipping viewport spawn for project %s: %s",
            project.id,
            _spend_defer,
        )
        return 0
    if expected_legacy_generation is not None:
        from app.notification_dispatch import begin_deferred_external_notifications

        begin_deferred_external_notifications(session)

    # Load case rows for spawn-side filters in one query:
    # IGNORED + SUSPENDED block re-spawn; last_spawned_at drives per-fingerprint
    # cooldown; REMEDIATING count drives the project-wide concurrency cap.
    # suspended_at + recovery_attempts drive exponential-backoff auto-recovery.
    case_rows_result = await session.execute(
        select(
            ResidualRemediationCase.fingerprint,
            ResidualRemediationCase.status,
            ResidualRemediationCase.last_spawned_at,
            ResidualRemediationCase.suspended_at,
            ResidualRemediationCase.recovery_attempts,
        ).where(ResidualRemediationCase.project_id == project.id)
    )
    case_rows = case_rows_result.all()
    blocked_fingerprints: set[str] = {
        fp
        for fp, status, _, _sat, _ra in case_rows
        if status in (ResidualCaseStatus.IGNORED, ResidualCaseStatus.SUSPENDED)
    }
    last_spawned_by_fp: dict[str, datetime] = {
        fp: ts for fp, _, ts, _sat, _ra in case_rows if ts is not None
    }

    # Auto-recover SUSPENDED cases whose exponential-backoff cooldown has elapsed.
    from app.auto_remediation import (  # deferred — avoids top-level circular import
        _suspension_cooldown_elapsed,
    )
    from app.notification_dispatch import (
        post_residual_fix_auto_recovered_message,
    )

    now_dt = _utcnow()
    recovered_any = False
    for fp, status, _ts, suspended_at, recovery_attempts in case_rows:
        if status != ResidualCaseStatus.SUSPENDED:
            continue
        suspended_at_str = suspended_at.isoformat() if suspended_at else None
        meta = {"suspended_at": suspended_at_str, "recovery_attempts": recovery_attempts or 0}
        if not _suspension_cooldown_elapsed(meta, now_dt):
            continue
        case_row = (
            await session.execute(
                select(ResidualRemediationCase).where(
                    ResidualRemediationCase.project_id == project.id,
                    ResidualRemediationCase.fingerprint == fp,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if case_row is None:
            continue
        new_attempts = (case_row.recovery_attempts or 0) + 1
        case_row.status = ResidualCaseStatus.ACTIVE
        case_row.suspended_at = None
        case_row.recovery_attempts = new_attempts
        case_row.updated_at = now_dt
        session.add(case_row)
        recovered_any = True
        blocked_fingerprints.discard(fp)
        logger.info(
            "[VIEWPORT REMEDIATION] Auto-recovering SUSPENDED fingerprint %s "
            "(project %s, recovery attempt %d).",
            fp[:32],
            project.id,
            new_attempts,
        )
        await post_residual_fix_auto_recovered_message(
            session,
            project.id,
            fingerprint=fp,
            recovery_attempts=new_attempts,
        )
    if recovered_any:
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        if expected_legacy_generation is not None:
            from app.notification_dispatch import begin_deferred_external_notifications

            begin_deferred_external_notifications(session)

    # Project-wide concurrency cap (shared with CODE_ERROR lane; both write to the
    # same ResidualRemediationCase table so REMEDIATING rows from either lane count).
    max_concurrent = int(_runtime_config.get("max_concurrent_auto_remediations", 2))
    active_count = sum(
        1 for _, status, _ts, _sat, _ra in case_rows if status == ResidualCaseStatus.REMEDIATING
    )
    if active_count >= max_concurrent:
        logger.info(
            "[VIEWPORT REMEDIATION] %d residual case(s) already REMEDIATING, "
            "at or above cap (%d); skipping spawn (project %s).",
            active_count,
            max_concurrent,
            project.id,
        )
        return 0

    cooldown_hours = _runtime_config.get("residual_remediation_cooldown_hours", 24)
    cooldown_seconds = cooldown_hours * 3600
    now_dt = _utcnow()
    cooldown_delta = timedelta(seconds=cooldown_seconds)

    for fp_entry in fingerprints:
        fp = fp_entry["fingerprint"]
        if fp in blocked_fingerprints:
            logger.debug(
                "[VIEWPORT REMEDIATION] Skipping IGNORED/SUSPENDED fingerprint %s (project %s).",
                fp[:32],
                project.id,
            )
            continue
        last_spawned = last_spawned_by_fp.get(fp)
        if last_spawned is not None and (now_dt - last_spawned) < cooldown_delta:
            continue

        component = fp_entry.get("component", "unknown")
        assertion_type = fp_entry.get("assertion_type", "unknown")
        task_ids = fp_entry.get("task_ids") or []
        task_ids_str = ", ".join(str(t) for t in task_ids[:5])
        sample_message = fp_entry.get("sample_message", "")

        prompt = (
            f"You are spawned as an auto-remediation task. A VIEWPORT_REGRESSION "
            f"fingerprint has been recurring on main for {fp_entry.get('count', '?')} "
            f"task(s) in the last 24 hours and nothing in the pipeline has fixed it. "
            f"Your job is to investigate and open a fix PR.\n\n"
            f"Component: {component}\n"
            f"Assertion type: {assertion_type}\n"
            f"Fingerprint: '{fp}'\n\n"
            f"IMPORTANT — read this carefully:\n"
            f"- This visual regression exists on main TODAY. It is not caused by "
            f"your changes. Your job is to fix it.\n"
            f"- The default 'don't touch pre-existing failures' instinct DOES NOT "
            f"APPLY here. You are EXPLICITLY authorized (and expected) to edit "
            f"the component, its layout, test expectations, or Tailwind classes "
            f"when the regression points there.\n"
            f"- Common causes: a recent layout change introduced overflow, text "
            f"clipping, or touch-target violations in the component.\n"
            f"- First, run the viewport test suite and reproduce the failure "
            f"locally before editing.\n"
            f"- Do NOT fix by adding data-viewport-allow-* escape hatches or "
            f"disabling viewport assertions. Find and fix the real layout cause.\n\n"
            f"Sample message: {sample_message or '(none recorded)'}\n"
            f"Affected task IDs (up to 5): {task_ids_str or '(none recorded)'}\n\n"
            f"Open a fix PR that resolves the underlying layout defect."
        )
        payload = validate_internal_payload(
            {
                "type": "create",
                "prompt": prompt,
                "base_branch": _get_project_integration_branch(project),
            },
            "autonomous_research:viewport_regression_remediation",
        )
        remediation_context = {
            "spawned_by": "viewport_regression_remediation",
            "viewport_fingerprint": fp,
            "component": component,
            "assertion_type": assertion_type,
            "hit_count": fp_entry.get("count", 0),
            "task_ids": task_ids,
        }
        new_task = Task(
            project_id=project.id,
            title=f"Fix viewport regression: {component} ({assertion_type})",
            description=(
                f"Auto-spawned remediation for viewport regression fingerprint {fp[:60]}"
            ),
            payloads=[payload],
            context=remediation_context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            auto_remediation=True,
        )
        session.add(new_task)
        session.add(
            TaskHistory(
                task_id=new_task.id,
                action="SPAWNED",
                details={
                    "spawned_by": "VIEWPORT_REGRESSION_REMEDIATION",
                    "viewport_fingerprint": fp,
                },
            )
        )
        from app.notification_dispatch import post_residual_fix_spawned_message

        try:
            await post_residual_fix_spawned_message(
                session,
                project.id,
                fingerprint=fp,
                sample_message=sample_message,
                task_id=new_task.id,
            )
        except Exception as _spawn_msg_exc:  # noqa: BLE001
            logger.warning(
                "[VIEWPORT REMEDIATION] Failed to post spawned message for %s: %s",
                fp[:32],
                _spawn_msg_exc,
            )
        logger.info(
            f"[VIEWPORT REMEDIATION] Spawned create task {new_task.id} for viewport "
            f"fingerprint {fp[:60]} (project {project.id})"
        )
        if expected_legacy_generation is None:
            await session.commit()
        else:
            await commit_legacy_authorized(
                session, project.id, expected_legacy_generation
            )
        return 1

    return 0
