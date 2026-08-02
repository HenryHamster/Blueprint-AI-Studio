"""Atomic admission accounting for bounded discovery cycles.

The functions in this module deliberately do not commit, roll back, or close
the supplied session.  A task constructor can therefore reserve a credit in
the same transaction that persists the task (or durable spawn request).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Literal, Mapping
import uuid

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.direction_contract import DIRECTION_CONTRACT_FIELDS
from app.models import (
    DiscoveryAdmission,
    DiscoveryCycle,
    Project,
    StrategicSpawnRequest,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow


DiscoveryAdmissionKind = Literal["logical_task", "attempt"]
DISCOVERY_ADMISSION_KINDS = frozenset({"logical_task", "attempt"})
DEFAULT_DISCOVERY_TASK_CREDIT_LIMIT = 8
DEFAULT_DISCOVERY_ATTEMPT_CREDIT_LIMIT = 4
DEFAULT_DISCOVERY_CONCURRENCY_LIMIT = 2
DEFAULT_DISCOVERY_DEADLINE_HOURS = 24
DISCOVERY_ADMITTING_STATES = frozenset({"open"})
DISCOVERY_ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
    }
)
class DiscoveryCycleUnavailable(RuntimeError):
    """A direction cannot open another root research task yet."""


@dataclass(frozen=True)
class DiscoveryAdmissionDecision:
    """Machine-readable result shared by admission and dispatch call sites."""

    allowed: bool
    reason: str
    cycle_id: uuid.UUID | None
    admission_id: uuid.UUID | None = None
    admission_kind: str | None = None
    idempotent: bool = False
    # A denied claim can either be transient (currently no concurrency slot) or
    # permanent for this task (the envelope closed before it was admitted).
    # Dispatch callers use this signal to avoid leaving permanently ineligible
    # QUEUED work stranded forever.
    terminal_denial: bool = False
    details: Mapping[str, Any] | None = None


def _as_uuid(value: Any, *, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{field_name} must be a UUID") from None


async def ensure_discovery_cycle_for_research(
    session: AsyncSession,
    *,
    project: Project,
    direction_key: str,
    direction: Mapping[str, Any],
    runtime_config: Mapping[str, Any] | None = None,
    candidate_sha: str | None = None,
    now: datetime | None = None,
    join_open_cycle: bool = False,
) -> DiscoveryCycle | None:
    """Return the live direction cycle, bootstrapping generation one if absent.

    The autonomous direction-spawn lock is the outer serialization boundary.
    The database's partial live-cycle uniqueness constraint remains the final
    race guard. Intent and bet inputs are frozen here, before research begins,
    so a later reviewer judges the work against the purpose that commissioned
    it rather than a subsequently rewritten Strategic narrative.
    """

    key = str(direction_key or "").strip()
    if not key or len(key) > 128:
        raise ValueError("direction_key must contain 1 to 128 characters")
    live = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.project_id == project.id,
                DiscoveryCycle.direction_key == key,
                DiscoveryCycle.state.in_(
                    {
                        "open",
                        "admission_closed",
                        "review_pending",
                        "reviewing",
                        "review_blocked",
                    }
                ),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if live is not None:
        if live.state == "open" and (
            live.source_research_task_id is None or join_open_cycle
        ):
            if candidate_sha:
                live.candidate_sha = candidate_sha
            return live
        raise DiscoveryCycleUnavailable(
            f"direction {key} already has cycle {live.id} in state {live.state}"
        )

    latest = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.project_id == project.id,
                DiscoveryCycle.direction_key == key,
            )
            .order_by(DiscoveryCycle.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and latest.verdict is not None:
        raise DiscoveryCycleUnavailable(
            f"direction {key} is awaiting policy resolution after "
            f"{latest.verdict}"
        )

    generation = int(
        (
            await session.execute(
                select(func.max(DiscoveryCycle.generation)).where(
                    DiscoveryCycle.project_id == project.id,
                    DiscoveryCycle.direction_key == key,
                )
            )
        ).scalar_one_or_none()
        or 0
    ) + 1
    config = runtime_config or {}
    opened_at = now or _utcnow()
    from app.problem_model import resolve_project_purpose
    from app.release_version import current_open_version

    problem_model = dict(project.problem_model or {})
    intent = dict(problem_model.get("intent") or {})
    release = await current_open_version(session, project.id)
    direction_snapshot = {
        field: direction.get(field)
        for field in (
            "id",
            "text",
            "category",
            "priority",
            "system_id",
            "completion_criteria",
            "research_context",
        )
        if direction.get(field) is not None
    }
    direction_context = direction.get("context")
    if isinstance(direction_context, Mapping):
        proof_contract = {
            field: direction_context[field]
            for field in DIRECTION_CONTRACT_FIELDS
            if field in direction_context
        }
        if proof_contract:
            # Only the canonical proof contract crosses this boundary. Other
            # mutable Strategic context must not silently become review truth.
            direction_snapshot["context"] = proof_contract
    bet_snapshot = {
        "direction": direction_snapshot,
        "claim": str(direction.get("text") or "")[:2_000],
        "decision_question": str(
            direction.get("completion_criteria")
            or f"Did work on {direction.get('text') or key} create "
            "goal-relevant, inspectable progress?"
        )[:4_000],
        "alternatives": list(direction.get("alternatives") or [])[:12],
    }
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=key,
        generation=generation,
        outcome_kind="mixed",
        phase="probe",
        intent_snapshot={
            "project_name": project.name,
            # Operator-owned user_purpose wins in resolve_project_purpose and
            # survives Strategic rewrites. The release goal is independently
            # frozen for its lifetime. Mutable narrative is context only.
            "user_purpose": resolve_project_purpose(project),
            "release_goal": release.goal if release is not None else None,
            "release_version_id": str(release.id) if release is not None else None,
            "purpose_source": intent.get("source"),
            "constraints": problem_model.get("constraints") or {},
            "project_context": problem_model.get("context") or {},
            "problem_model_revision": problem_model.get("revision"),
            "running_update_excerpt": str(project.running_update or "")[:2_000],
        },
        bet_snapshot=bet_snapshot,
        candidate_sha=candidate_sha,
        mechanism_fingerprint=hashlib.sha256(
            json.dumps(bet_snapshot, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        task_credit_limit=max(
            1,
            int(
                config.get(
                    "discovery_cycle_task_credit_limit",
                    DEFAULT_DISCOVERY_TASK_CREDIT_LIMIT,
                )
            ),
        ),
        attempt_credit_limit=max(
            1,
            int(
                config.get(
                    "discovery_cycle_attempt_credit_limit",
                    DEFAULT_DISCOVERY_ATTEMPT_CREDIT_LIMIT,
                )
            ),
        ),
        concurrency_limit=max(
            1,
            int(
                config.get(
                    "discovery_cycle_concurrency_limit",
                    DEFAULT_DISCOVERY_CONCURRENCY_LIMIT,
                )
            ),
        ),
        deadline_at=opened_at
        + timedelta(
            hours=max(
                1,
                int(
                    config.get(
                        "discovery_cycle_deadline_hours",
                        DEFAULT_DISCOVERY_DEADLINE_HOURS,
                    )
                ),
            )
        ),
    )
    session.add(cycle)
    await session.flush()
    return cycle


async def resolve_discovery_cycle_lineage(
    session: AsyncSession,
    *,
    explicit_cycle_id: uuid.UUID | str | None = None,
    task_id: uuid.UUID | str | None = None,
    parent_task_id: uuid.UUID | str | None = None,
    strategic_spawn_request_id: uuid.UUID | str | None = None,
    project_id: uuid.UUID | str | None = None,
    direction_key: str | None = None,
    max_parent_hops: int = 64,
) -> uuid.UUID | None:
    """Resolve the cycle inherited by a new task or spawn request.

    Explicit lineage is authoritative.  Otherwise a durable spawn request is
    checked first, followed by the task ancestry.  Walking ancestors makes the
    boundary robust while older constructors are still being migrated to stamp
    every direct child.
    """

    if explicit_cycle_id is not None:
        return _as_uuid(explicit_cycle_id, field_name="explicit_cycle_id")

    if strategic_spawn_request_id is not None:
        request_id = _as_uuid(
            strategic_spawn_request_id,
            field_name="strategic_spawn_request_id",
        )
        request_lineage = (
            await session.execute(
                select(
                    StrategicSpawnRequest.discovery_cycle_id,
                    StrategicSpawnRequest.parent_task_id,
                ).where(StrategicSpawnRequest.id == request_id)
            )
        ).one_or_none()
        if request_lineage is not None:
            request_cycle_id, request_parent_id = request_lineage
            if request_cycle_id is not None:
                return request_cycle_id
            if parent_task_id is None:
                parent_task_id = request_parent_id

    current_id = task_id if task_id is not None else parent_task_id
    if current_id is not None:
        current_id = _as_uuid(current_id, field_name="task_id")
        seen: set[uuid.UUID] = set()
        for _ in range(max_parent_hops):
            if current_id in seen:
                raise RuntimeError("cycle detected in task parent lineage")
            seen.add(current_id)
            row = (
                await session.execute(
                    select(Task.discovery_cycle_id, Task.parent_task_id).where(
                        Task.id == current_id
                    )
                )
            ).one_or_none()
            if row is None:
                break
            cycle_id, next_parent_id = row
            if cycle_id is not None:
                return cycle_id
            if next_parent_id is None:
                break
            current_id = next_parent_id
        else:
            raise RuntimeError("task parent lineage exceeds maximum depth")

    if project_id is None and direction_key is None:
        return None
    if project_id is None or not str(direction_key or "").strip():
        raise ValueError("project_id and direction_key must be supplied together")
    normalized_project_id = _as_uuid(project_id, field_name="project_id")
    return (
        await session.execute(
            select(DiscoveryCycle.id).where(
                DiscoveryCycle.project_id == normalized_project_id,
                DiscoveryCycle.direction_key == str(direction_key).strip(),
                DiscoveryCycle.state.in_(
                    {
                        "open",
                        "admission_closed",
                        "review_pending",
                        "reviewing",
                        "review_blocked",
                    }
                ),
            )
        )
    ).scalar_one_or_none()


def _policy_decision(
    *,
    cycle_id: uuid.UUID,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> DiscoveryAdmissionDecision:
    return DiscoveryAdmissionDecision(
        allowed=False,
        reason=reason,
        cycle_id=cycle_id,
        details=details,
    )


async def _active_cycle_task_count(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID,
    exclude_task_id: uuid.UUID | None = None,
) -> int:
    statement = select(func.count()).select_from(Task).where(
        Task.discovery_cycle_id == cycle_id,
        Task.status.in_(DISCOVERY_ACTIVE_TASK_STATUSES),
    )
    if exclude_task_id is not None:
        statement = statement.where(Task.id != exclude_task_id)
    return int((await session.execute(statement)).scalar_one())


async def _evaluate_cycle_policy(
    session: AsyncSession,
    *,
    cycle: DiscoveryCycle,
    admission_kind: DiscoveryAdmissionKind,
    units: int,
    now: datetime,
) -> DiscoveryAdmissionDecision | None:
    if cycle.state not in DISCOVERY_ADMITTING_STATES:
        return _policy_decision(
            cycle_id=cycle.id,
            reason="cycle_not_admitting",
            details={"state": cycle.state},
        )
    if cycle.deadline_at is not None and now >= cycle.deadline_at:
        return _policy_decision(
            cycle_id=cycle.id,
            reason="deadline_expired",
            details={"deadline_at": cycle.deadline_at.isoformat()},
        )

    if admission_kind == "logical_task":
        used = cycle.reserved_task_credits + cycle.consumed_task_credits
        limit = cycle.task_credit_limit
    else:
        used = cycle.reserved_attempt_credits + cycle.consumed_attempt_credits
        limit = cycle.attempt_credit_limit
    if limit is not None and used + units > limit:
        return _policy_decision(
            cycle_id=cycle.id,
            reason=f"{admission_kind}_credit_limit",
            details={"used": used, "requested": units, "limit": limit},
        )

    # Concurrency is an execution constraint, not an admission constraint.
    # Rejecting construction here drops durable findings instead of preserving
    # them as queued backlog until a slot opens.
    return None


async def reserve_discovery_admission(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | str,
    admission_key: str,
    admission_kind: DiscoveryAdmissionKind = "logical_task",
    # Compatibility alias for early integration callers.
    credit_kind: DiscoveryAdmissionKind | None = None,
    units: int = 1,
    task_id: uuid.UUID | str | None = None,
    spawn_request_id: uuid.UUID | str | None = None,
    metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> DiscoveryAdmissionDecision:
    """Atomically and idempotently reserve cycle credit.

    The cycle row is locked before the idempotency lookup and counter update,
    serializing all reservations for a cycle without requiring a retry loop
    around the unique ledger key. Policy and invariant violations fail closed.
    """
    normalized_cycle_id = _as_uuid(cycle_id, field_name="cycle_id")
    if credit_kind is not None:
        if admission_kind != "logical_task" and admission_kind != credit_kind:
            raise ValueError("admission_kind and credit_kind disagree")
        admission_kind = credit_kind
    if admission_kind not in DISCOVERY_ADMISSION_KINDS:
        raise ValueError("admission_kind must be logical_task or attempt")
    key = str(admission_key or "").strip()
    if not key or len(key) > 512:
        raise ValueError("admission_key must contain 1 to 512 characters")
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        raise ValueError("units must be a positive integer")
    normalized_task_id = (
        _as_uuid(task_id, field_name="task_id") if task_id is not None else None
    )
    normalized_request_id = (
        _as_uuid(spawn_request_id, field_name="spawn_request_id")
        if spawn_request_id is not None
        else None
    )

    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == normalized_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason="cycle_not_found",
            cycle_id=normalized_cycle_id,
        )
    if cycle.state in {"reviewing", "reviewed", "review_blocked", "cancelled"}:
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason="cycle_review_frozen",
            cycle_id=normalized_cycle_id,
            terminal_denial=True,
            details={"state": cycle.state},
        )

    existing = (
        await session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == normalized_cycle_id,
                DiscoveryAdmission.admission_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.admission_kind != admission_kind or existing.units != units:
            return DiscoveryAdmissionDecision(
                allowed=False,
                reason="idempotency_mismatch",
                cycle_id=normalized_cycle_id,
                admission_id=existing.id,
                admission_kind=existing.admission_kind,
                idempotent=True,
                details={
                    "existing_kind": existing.admission_kind,
                    "existing_units": existing.units,
                },
            )
        if (
            normalized_task_id is not None
            and existing.task_id is not None
            and existing.task_id != normalized_task_id
        ) or (
            normalized_request_id is not None
            and existing.spawn_request_id is not None
            and existing.spawn_request_id != normalized_request_id
        ):
            return DiscoveryAdmissionDecision(
                allowed=False,
                reason="idempotency_owner_mismatch",
                cycle_id=normalized_cycle_id,
                admission_id=existing.id,
                admission_kind=existing.admission_kind,
                idempotent=True,
            )
        if existing.task_id is None and normalized_task_id is not None:
            existing.task_id = normalized_task_id
        if existing.spawn_request_id is None and normalized_request_id is not None:
            existing.spawn_request_id = normalized_request_id
        if existing.state == "released":
            return DiscoveryAdmissionDecision(
                allowed=False,
                reason="admission_released",
                cycle_id=normalized_cycle_id,
                admission_id=existing.id,
                admission_kind=existing.admission_kind,
                idempotent=True,
            )
        return DiscoveryAdmissionDecision(
            allowed=True,
            reason=f"already_{existing.state}",
            cycle_id=normalized_cycle_id,
            admission_id=existing.id,
            admission_kind=existing.admission_kind,
            idempotent=True,
        )

    policy = await _evaluate_cycle_policy(
        session,
        cycle=cycle,
        admission_kind=admission_kind,
        units=units,
        now=now or _utcnow(),
    )
    if policy is not None and not policy.allowed:
        return policy

    admission = DiscoveryAdmission(
        cycle_id=normalized_cycle_id,
        admission_key=key,
        admission_kind=admission_kind,
        task_id=normalized_task_id,
        spawn_request_id=normalized_request_id,
        units=units,
        admission_metadata=dict(metadata or {}),
    )
    session.add(admission)
    if admission_kind == "logical_task":
        cycle.reserved_task_credits += units
    else:
        cycle.reserved_attempt_credits += units
    cycle.revision += 1
    cycle.updated_at = _utcnow()
    session.add(cycle)
    await session.flush()
    return DiscoveryAdmissionDecision(
        allowed=True,
        reason="reserved",
        cycle_id=normalized_cycle_id,
        admission_id=admission.id,
        admission_kind=admission_kind,
    )


async def _settle_discovery_admission(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | str,
    admission_key: str,
    target_state: Literal["consumed", "released"],
    now: datetime | None,
) -> bool:
    normalized_cycle_id = _as_uuid(cycle_id, field_name="cycle_id")
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == normalized_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return False
    admission = (
        await session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == normalized_cycle_id,
                DiscoveryAdmission.admission_key == admission_key,
            )
        )
    ).scalar_one_or_none()
    if admission is None:
        return False
    if admission.state == target_state:
        return True
    if admission.state != "reserved":
        return False

    if admission.admission_kind == "logical_task":
        if cycle.reserved_task_credits < admission.units:
            raise RuntimeError("discovery task reservation counter underflow")
        cycle.reserved_task_credits -= admission.units
        if target_state == "consumed":
            cycle.consumed_task_credits += admission.units
    else:
        if cycle.reserved_attempt_credits < admission.units:
            raise RuntimeError("discovery attempt reservation counter underflow")
        cycle.reserved_attempt_credits -= admission.units
        if target_state == "consumed":
            cycle.consumed_attempt_credits += admission.units
    admission.state = target_state
    admission.updated_at = now or _utcnow()
    cycle.revision += 1
    cycle.updated_at = now or _utcnow()
    session.add(admission)
    session.add(cycle)
    await session.flush()
    return True


async def consume_discovery_admission(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | str,
    admission_key: str,
    now: datetime | None = None,
) -> bool:
    """Consume a reservation exactly once; return false if absent or released."""

    return await _settle_discovery_admission(
        session,
        cycle_id=cycle_id,
        admission_key=admission_key,
        target_state="consumed",
        now=now,
    )


async def release_discovery_admission(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | str,
    admission_key: str,
    now: datetime | None = None,
) -> bool:
    """Release an unused reservation exactly once."""

    return await _settle_discovery_admission(
        session,
        cycle_id=cycle_id,
        admission_key=admission_key,
        target_state="released",
        now=now,
    )


async def validate_discovery_dispatch_authority(
    session: AsyncSession,
    *,
    cycle_id: uuid.UUID | str,
    admission_key: str | None = None,
    task_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
) -> DiscoveryAdmissionDecision:
    """Validate dispatch authority without charging or mutating admission state."""

    normalized_cycle_id = _as_uuid(cycle_id, field_name="cycle_id")
    normalized_task_id = (
        _as_uuid(task_id, field_name="task_id") if task_id is not None else None
    )
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == normalized_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason="cycle_not_found",
            cycle_id=normalized_cycle_id,
        )
    if cycle.state in {"reviewing", "reviewed", "review_blocked", "cancelled"}:
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason="cycle_review_frozen",
            cycle_id=normalized_cycle_id,
            terminal_denial=True,
            details={"state": cycle.state},
        )

    admission: DiscoveryAdmission | None = None
    if admission_key is not None:
        admission = (
            await session.execute(
                select(DiscoveryAdmission).where(
                    DiscoveryAdmission.cycle_id == normalized_cycle_id,
                    DiscoveryAdmission.admission_key == admission_key,
                )
            )
        ).scalar_one_or_none()
        if admission is None or admission.state == "released":
            policy = _policy_decision(
                cycle_id=normalized_cycle_id,
                reason=(
                    "admission_missing"
                    if admission is None
                    else "admission_released"
                ),
            )
            return policy
        if (
            normalized_task_id is not None
            and admission.task_id is not None
            and admission.task_id != normalized_task_id
        ):
            return DiscoveryAdmissionDecision(
                allowed=False,
                reason="admission_owner_mismatch",
                cycle_id=normalized_cycle_id,
                admission_id=admission.id,
                admission_kind=admission.admission_kind,
            )
    elif normalized_task_id is not None:
        admission = (
            await session.execute(
                select(DiscoveryAdmission)
                .where(
                    DiscoveryAdmission.cycle_id == normalized_cycle_id,
                    DiscoveryAdmission.task_id == normalized_task_id,
                )
                .order_by(DiscoveryAdmission.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if admission is None or admission.state == "released":
            return _policy_decision(
                cycle_id=normalized_cycle_id,
                reason=(
                    "admission_missing"
                    if admission is None
                    else "admission_released"
                ),
            )

    # Closing an envelope stops *new* descendant work. Already-reserved work
    # keeps dispatch authority so it can drain instead of being stranded.
    if admission is None:
        if cycle.state not in DISCOVERY_ADMITTING_STATES:
            return _policy_decision(
                cycle_id=normalized_cycle_id,
                reason="cycle_not_admitting",
                details={"state": cycle.state},
            )
        check_time = now or _utcnow()
        if cycle.deadline_at is not None and check_time >= cycle.deadline_at:
            return _policy_decision(
                cycle_id=normalized_cycle_id,
                reason="deadline_expired",
                details={"deadline_at": cycle.deadline_at.isoformat()},
            )
    if cycle.concurrency_limit is not None:
        active = await _active_cycle_task_count(
            session,
            cycle_id=normalized_cycle_id,
            exclude_task_id=normalized_task_id,
        )
        if active >= cycle.concurrency_limit:
            return _policy_decision(
                cycle_id=normalized_cycle_id,
                reason="concurrency_limit",
                details={"active": active, "limit": cycle.concurrency_limit},
            )
    return DiscoveryAdmissionDecision(
        allowed=True,
        reason="authority_valid",
        cycle_id=normalized_cycle_id,
        admission_id=admission.id if admission is not None else None,
        admission_kind=(
            admission.admission_kind if admission is not None else None
        ),
    )


async def authorize_discovery_task_execution(
    session: AsyncSession,
    *,
    task: Task,
    now: datetime | None = None,
) -> DiscoveryAdmissionDecision:
    """Resolve, repair, admit, and validate discovery execution authority.

    This is the central backstop for execution boundaries.  Constructors should
    still stamp lineage and reserve credits eagerly, but a missed constructor
    cannot escape the envelope: ancestry is resolved immediately before claim,
    the task is stamped, and a task-id-keyed reservation is attempted exactly
    once.  Existing reservations retain authority after close/deadline so
    already accepted work drains normally.
    """

    cycle_id = task.discovery_cycle_id
    if cycle_id is None:
        cycle_id = await resolve_discovery_cycle_lineage(
            session,
            task_id=task.id,
        )
        if cycle_id is None:
            return DiscoveryAdmissionDecision(
                allowed=True,
                reason="not_discovery_scoped",
                cycle_id=None,
            )
        task.discovery_cycle_id = cycle_id
        session.add(task)
        await session.flush()

    # A discovery review is the server-owned control task that closes the
    # envelope; it is not a descendant bet and must not consume descendant
    # credits.  Keep this exemption fail-closed: only the exact task named by
    # the locked cycle row, while that cycle is actively reviewing, qualifies.
    if task.first_payload.get("type") == "discovery_review":
        review_cycle = (
            await session.execute(
                select(DiscoveryCycle)
                .where(DiscoveryCycle.id == cycle_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            review_cycle is not None
            and review_cycle.state == "reviewing"
            and review_cycle.review_task_id == task.id
        ):
            return DiscoveryAdmissionDecision(
                allowed=True,
                reason="discovery_review_control_task",
                cycle_id=review_cycle.id,
                details={"charged": False},
            )

    # A task can cross this boundary more than once before the orchestrator
    # commits its claim, so the key must identify the execution incarnation,
    # not the authorization call. Constructors reserve the initial logical-task
    # credit under their own key. Every later fresh incarnation is stamped by
    # ``reset_task_for_fresh_attempt`` (or the few low-level recovery paths)
    # and charged here as an attempt only when it is actually dispatched.
    context = task.context if isinstance(task.context, dict) else {}
    raw_retry_ordinal = context.get("_discovery_attempt_ordinal")
    retry_ordinal: int | None = None
    if raw_retry_ordinal is not None:
        try:
            parsed_retry_ordinal = int(raw_retry_ordinal)
        except (TypeError, ValueError):
            parsed_retry_ordinal = 0
        if parsed_retry_ordinal > 0:
            retry_ordinal = parsed_retry_ordinal
    # Backward-compatible repair for rows queued by an older process before the
    # ordinal stamp existed. This timestamp is written only by fresh-attempt
    # transitions, never by an initial constructor.
    if retry_ordinal is None and context.get("_fresh_attempt_started_at"):
        retry_ordinal = 1

    if retry_ordinal is not None:
        admission_kind: DiscoveryAdmissionKind = "attempt"
        admission_key = f"attempt:{task.id}:{retry_ordinal}"
        existing_statement = select(DiscoveryAdmission).where(
            DiscoveryAdmission.cycle_id == cycle_id,
            DiscoveryAdmission.admission_key == admission_key,
        )
    else:
        admission_kind = "logical_task"
        admission_key = f"task:{task.id}"
        # Eager constructors use intent-specific idempotency keys. Preserve that
        # reservation rather than creating a second logical-task charge.
        existing_statement = (
            select(DiscoveryAdmission)
            .where(
                DiscoveryAdmission.cycle_id == cycle_id,
                DiscoveryAdmission.task_id == task.id,
                DiscoveryAdmission.admission_kind == "logical_task",
                DiscoveryAdmission.state != "released",
            )
            .order_by(DiscoveryAdmission.created_at.desc())
            .limit(1)
        )
    existing = (await session.execute(existing_statement)).scalar_one_or_none()
    late: DiscoveryAdmissionDecision | None = None
    if existing is None:
        late = await reserve_discovery_admission(
            session,
            cycle_id=cycle_id,
            admission_key=admission_key,
            admission_kind=admission_kind,
            task_id=task.id,
            metadata={
                "source": "execution_authority_backstop",
                **(
                    {"retry_ordinal": retry_ordinal}
                    if retry_ordinal is not None
                    else {}
                ),
            },
            now=now,
        )
        if not late.allowed:
            return DiscoveryAdmissionDecision(
                allowed=False,
                reason=late.reason,
                cycle_id=late.cycle_id,
                admission_id=late.admission_id,
                admission_kind=late.admission_kind,
                idempotent=late.idempotent,
                terminal_denial=late.reason != "concurrency_limit",
                details=late.details,
            )

    # The task already exists, so a successful backstop reservation is no
    # longer provisional. Settle it before the concurrency check, matching eager
    # constructors: concurrency may defer execution, but must not leak reserved
    # credits while durable backlog waits for a slot.
    if existing is not None:
        admission_key_to_consume = existing.admission_key
        settlement_admission_id = existing.id
        settlement_admission_kind = existing.admission_kind
    else:
        assert late is not None
        admission_key_to_consume = admission_key
        settlement_admission_id = late.admission_id
        settlement_admission_kind = late.admission_kind
    if not await consume_discovery_admission(
        session,
        cycle_id=cycle_id,
        admission_key=admission_key_to_consume,
        now=now,
    ):
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason="admission_settlement_failed",
            cycle_id=cycle_id,
            admission_id=settlement_admission_id,
            admission_kind=settlement_admission_kind,
            terminal_denial=True,
        )

    decision = await validate_discovery_dispatch_authority(
        session,
        cycle_id=cycle_id,
        admission_key=admission_key_to_consume,
        task_id=task.id,
        now=now,
    )
    if not decision.allowed:
        return DiscoveryAdmissionDecision(
            allowed=False,
            reason=decision.reason,
            cycle_id=decision.cycle_id,
            admission_id=decision.admission_id,
            admission_kind=decision.admission_kind,
            idempotent=decision.idempotent,
            terminal_denial=decision.reason != "concurrency_limit",
            details=decision.details,
        )
    return decision
