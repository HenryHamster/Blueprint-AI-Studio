"""Recover marker-less research finalization without re-running its agent.

The normal terminal-status path writes ``post_commit_pending`` before invoking
the completion handler.  A sidecar verdict, however, resumes its source task
directly.  If that process dies after research persisted
``_research_spawn_pending`` but before the finalized-findings receipt, the
source can be left COMPLETED with no pending marker and no future callback.

This sweep treats the raw persisted ``plan_output`` as the one-shot routing
input.  It never queues the source task and never creates a new research root.
Claims are serialized under the canonical Project -> Task lock pair and carry a
durable lease plus the exact task-attempt identity.  Paused projects are a
strict no-op: no claim and no retry-budget mutation occurs until unpause.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import Project, Task, TaskStatus, get_task_lifetime_attempt_ordinal
from app.schemas import AgentStatusPayload
from app.task_locking import lock_project_then_task
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

RESEARCH_SPAWN_RECOVERY_KEY = "_research_spawn_recovery"
RESEARCH_SPAWN_RECOVERY_BATCH_SIZE = 20
RESEARCH_SPAWN_RECOVERY_GRACE_S = 600
RESEARCH_SPAWN_RECOVERY_LEASE_S = 360
RESEARCH_SPAWN_RECOVERY_MAX_FAILURES = 20


def research_spawn_recovery_attempt_identity(task: Task) -> dict[str, Any]:
    """Return the stable identity of the exact agent attempt being recovered."""

    return {
        "attempt_ordinal": get_task_lifetime_attempt_ordinal(task),
        "attempt_count": int(task.attempt_count or 0),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "vm_instance_id": task.vm_instance_id,
    }


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _eligible_locked_task(task: Task, *, allow_progressed: bool) -> bool:
    context = task.context or {}
    if (
        task.archived
        or task.post_commit_pending
        or (task.first_payload or {}).get("type") != "research"
        or context.get("_research_spawn_pending") is not True
        or context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is True
        or not isinstance(task.plan_output, list)
    ):
        return False
    if task.status == TaskStatus.COMPLETED:
        return True
    return bool(
        allow_progressed
        and task.status == TaskStatus.EVALUATING
        and isinstance(context.get(RESEARCH_SPAWN_RECOVERY_KEY), dict)
    )


async def _claim_markerless_research_spawn(task_id) -> dict[str, Any] | None:
    """Claim one exact stranded attempt, or defer without mutation."""

    from app import services

    async with services.AsyncSessionLocal() as session:
        project_id = (
            await session.execute(select(Task.project_id).where(Task.id == task_id))
        ).scalar_one_or_none()
        if project_id is None:
            return None

        async with lock_project_then_task(
            session,
            project_id,
            task_id,
            skip_locked=True,
        ) as (project, task):
            if project is None or task is None:
                return None
            # Pause is checked while the Project lock is held.  Do not even
            # write a lease: pause -> unpause must retain the full retry budget.
            if project.paused or project.archived:
                return None

            context = dict(task.context or {})
            prior = dict(context.get(RESEARCH_SPAWN_RECOVERY_KEY) or {})
            if not _eligible_locked_task(task, allow_progressed=bool(prior)):
                return None

            identity = research_spawn_recovery_attempt_identity(task)
            if prior.get("attempt_identity") != identity:
                prior = {}

            now = _utcnow()
            if not prior and task.updated_at > now - timedelta(
                seconds=RESEARCH_SPAWN_RECOVERY_GRACE_S
            ):
                return None

            claimed_at = _parse_timestamp(prior.get("claimed_at"))
            if (
                prior.get("state") == "claimed"
                and claimed_at is not None
                and (now - claimed_at).total_seconds() < RESEARCH_SPAWN_RECOVERY_LEASE_S
            ):
                return None

            failures = int(prior.get("failure_attempts", 0) or 0)
            if failures >= RESEARCH_SPAWN_RECOVERY_MAX_FAILURES:
                if prior.get("state") != "exhausted":
                    prior.update(
                        {
                            "state": "exhausted",
                            "exhausted_at": now.isoformat(),
                            "attempt_identity": identity,
                        }
                    )
                    prior.pop("claimed_at", None)
                    prior.pop("lease_expires_at", None)
                    context[RESEARCH_SPAWN_RECOVERY_KEY] = prior
                    task.context = context
                    task.post_commit_failed = True
                    task.latest_status_message = (
                        "Research finalization recovery exhausted after "
                        f"{failures} failed same-lineage attempts"
                    )[:500]
                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                return None

            token = uuid.uuid4().hex
            lease = {
                **prior,
                "state": "claimed",
                "claim_token": token,
                "claimed_at": now.isoformat(),
                "lease_expires_at": (
                    now + timedelta(seconds=RESEARCH_SPAWN_RECOVERY_LEASE_S)
                ).isoformat(),
                "claim_count": int(prior.get("claim_count", 0) or 0) + 1,
                "failure_attempts": failures,
                "attempt_identity": identity,
            }
            context[RESEARCH_SPAWN_RECOVERY_KEY] = lease
            task.context = context
            flag_modified(task, "context")
            session.add(task)
            plan_output = json.loads(json.dumps(task.plan_output))
            await session.commit()
            return {
                "task_id": task.id,
                "claim_token": token,
                "attempt_identity": identity,
                "plan_output": plan_output,
            }


async def _finish_markerless_research_spawn_claim(
    task_id,
    claim_token: str,
    *,
    error: BaseException | None = None,
    deferred_reason: str | None = None,
) -> None:
    """Close one current lease without touching a newer lifecycle attempt."""

    from app import services

    async with services.AsyncSessionLocal() as session:
        project_id = (
            await session.execute(select(Task.project_id).where(Task.id == task_id))
        ).scalar_one_or_none()
        if project_id is None:
            return
        async with lock_project_then_task(
            session,
            project_id,
            task_id,
            skip_locked=False,
        ) as (project, task):
            if project is None or task is None:
                return
            context = dict(task.context or {})
            meta = dict(context.get(RESEARCH_SPAWN_RECOVERY_KEY) or {})
            if meta.get("claim_token") != claim_token or meta.get(
                "attempt_identity"
            ) != research_spawn_recovery_attempt_identity(task):
                return

            now = _utcnow().isoformat()
            finalized = bool(
                context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is True
                and context.get("_research_spawn_pending") is not True
            )
            if finalized:
                meta.update({"state": "recovered", "recovered_at": now})
                meta.pop("last_error", None)
                meta.pop("last_failure_at", None)
            elif (
                (deferred_reason == "project_paused" and project.paused)
                or project.paused
                or project.archived
            ):
                meta.update(
                    {
                        "state": "deferred",
                        "last_deferred_at": now,
                        "last_deferred_reason": (
                            "project_paused" if project.paused else "project_archived"
                        ),
                    }
                )
            elif error is not None and _eligible_locked_task(
                task, allow_progressed=True
            ):
                failures = int(meta.get("failure_attempts", 0) or 0) + 1
                meta.update(
                    {
                        "state": "retryable",
                        "failure_attempts": failures,
                        "last_failure_at": now,
                        "last_error": f"{type(error).__name__}: {error}"[:1000],
                    }
                )
                if getattr(error, "lock_scope", None):
                    meta["last_lock_scope"] = error.lock_scope
            else:
                # Cancellation, requeue, archival, or a newer attempt won.  The
                # exact-token ledger is no longer authoritative; do not dirty it.
                return

            meta.pop("claimed_at", None)
            meta.pop("lease_expires_at", None)
            context[RESEARCH_SPAWN_RECOVERY_KEY] = meta
            task.context = context
            flag_modified(task, "context")
            session.add(task)
            await session.commit()


async def _sweep_markerless_research_spawn_pending() -> None:
    """Replay bounded same-lineage research finalization from durable state."""

    from app import services
    from app.orchestrator import _mark_orchestrator_progress
    from app.routers.agent_status import _process_post_commit

    now = _utcnow()
    grace_cutoff = now - timedelta(seconds=RESEARCH_SPAWN_RECOVERY_GRACE_S)
    try:
        async with services.AsyncSessionLocal() as session:
            if resolve_dialect_name(session) == "postgresql":
                plan_is_array = func.jsonb_typeof(Task.plan_output) == "array"
            else:
                plan_is_array = func.json_type(Task.plan_output) == "array"
            recovery_state = Task.context[RESEARCH_SPAWN_RECOVERY_KEY][
                "state"
            ].as_string()
            has_recovery = (
                Task.context[RESEARCH_SPAWN_RECOVERY_KEY]["claim_token"]
                .as_string()
                .is_not(None)
            )
            candidate_ids = (
                (
                    await session.execute(
                        select(Task.id)
                        .join(Project, Project.id == Task.project_id)
                        .where(
                            Task.archived.is_(False),
                            Task.post_commit_pending.is_(False),
                            Project.archived.is_(False),
                            Project.paused.is_(False),
                            Task.payloads[0]["type"].as_string() == "research",
                            Task.context["_research_spawn_pending"]
                            .as_boolean()
                            .is_(True),
                            Task.context[FINALIZED_RESEARCH_FINDINGS_KEY]
                            .as_boolean()
                            .is_not(True),
                            Task.plan_output.is_not(None),
                            plan_is_array,
                            func.coalesce(recovery_state, "") != "exhausted",
                            or_(
                                Task.status == TaskStatus.COMPLETED,
                                (Task.status == TaskStatus.EVALUATING) & has_recovery,
                            ),
                            or_(Task.updated_at < grace_cutoff, has_recovery),
                        )
                        .order_by(Task.updated_at.asc())
                        .limit(RESEARCH_SPAWN_RECOVERY_BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )

        for task_id in candidate_ids:
            _mark_orchestrator_progress()
            claim = await _claim_markerless_research_spawn(task_id)
            if claim is None:
                continue
            token = claim["claim_token"]
            try:
                payload = AgentStatusPayload(
                    task_id=task_id,
                    status=TaskStatus.COMPLETED,
                    logs=json.dumps(claim["plan_output"]),
                )
                result = await services._bounded(
                    _process_post_commit(
                        task_id,
                        payload,
                        _expected_research_spawn_recovery_token=token,
                    ),
                    services._POST_COMMIT_CALL_TIMEOUT_S,
                    what=f"markerless research spawn {str(task_id)[:8]}",
                )
                result = result or {}
                deferred_reason = None
                if result.get("recovery_skipped"):
                    deferred_reason = result.get("recovery_reason")
                await _finish_markerless_research_spawn_claim(
                    task_id,
                    token,
                    deferred_reason=deferred_reason,
                    error=(
                        RuntimeError("completion returned without final receipt")
                        if not result.get("recovery_skipped")
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — durable retry ledger owns it
                await _finish_markerless_research_spawn_claim(
                    task_id,
                    token,
                    error=exc,
                )
                logger.warning(
                    "[POST_COMMIT SWEEP] Marker-less research recovery for %s "
                    "failed and remains same-lineage retryable: %s: %s",
                    str(task_id)[:8],
                    type(exc).__name__,
                    exc,
                )
    except Exception:
        logger.exception(
            "[POST_COMMIT SWEEP] Marker-less research recovery query failed"
        )
