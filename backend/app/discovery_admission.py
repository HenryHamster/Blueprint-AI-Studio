"""Discovery-cycle bootstrap and provenance lineage.

``discovery_cycle_id`` groups a research parent and its descendants into one
evidence cohort. It is deliberately not execution authority: once work is
created, ordinary scheduler, retry, review, spend, and approval policy governs
whether it runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.direction_contract import DIRECTION_CONTRACT_FIELDS
from app.models import DiscoveryCycle, Project, StrategicSpawnRequest, Task
from app.time_utils import _utcnow


DEFAULT_DISCOVERY_DEADLINE_HOURS = 24
LIVE_DISCOVERY_CYCLE_STATES = frozenset(
    {
        "open",
        "admission_closed",
        "review_pending",
        "reviewing",
        "review_blocked",
    }
)
POLICY_BLOCKING_DISCOVERY_VERDICTS = frozenset({"park", "stop", "ask_operator"})


class DiscoveryCycleUnavailable(RuntimeError):
    """A direction cannot open another root research task yet."""


@dataclass(frozen=True)
class DirectionDiscoveryAdmissionDecision:
    """One canonical answer for direction-root discovery admission.

    ``can_admit_root`` is deliberately narrower than "the direction pipeline
    has work to do".  A pending/recoverable decision is not allowed to open a
    second research root; its reason and next action tell the scheduler and
    operator which existing stage must move instead.
    """

    direction_key: str
    can_admit_root: bool
    reason_code: str
    reason: str
    next_action: str
    cycle_id: uuid.UUID | None = None
    cycle_state: str | None = None
    cycle_generation: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "can_admit_root": self.can_admit_root,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "next_action": self.next_action,
            "cycle_id": str(self.cycle_id) if self.cycle_id is not None else None,
            "cycle_state": self.cycle_state,
            "cycle_generation": self.cycle_generation,
        }


def direction_authority_snapshot(direction: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded direction envelope frozen into discovery work."""

    snapshot = {
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
            snapshot["context"] = proof_contract
    direction_paths = direction.get("paths")
    if isinstance(direction_paths, list):
        snapshot["paths"] = [
            path.strip()[:500]
            for path in direction_paths[:50]
            if isinstance(path, str) and path.strip()
        ]
    return snapshot


def direction_authority_fence_snapshot(
    direction: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only immutable fields used by the exact authority fence.

    ``research_context`` remains in the frozen commission envelope because it
    grounds successor prompts.  It is accumulated outcome memory, however,
    and research completion is expected to rewrite it.  Comparing that field
    made a clean cycle invalidate itself before decision (production d-121).
    """

    snapshot = direction_authority_snapshot(direction)
    snapshot.pop("research_context", None)
    return snapshot


def direction_authority_matches(
    frozen_direction: Mapping[str, Any] | None,
    current_direction: Mapping[str, Any] | None,
) -> bool | None:
    """Compare true direction authority, or return ``None`` if proof is absent."""

    if not isinstance(frozen_direction, Mapping) or not isinstance(
        current_direction, Mapping
    ):
        return None
    frozen = direction_authority_fence_snapshot(frozen_direction)
    current = direction_authority_fence_snapshot(current_direction)
    return hashlib.sha256(
        json.dumps(frozen, sort_keys=True, default=str).encode("utf-8")
    ).digest() == hashlib.sha256(
        json.dumps(current, sort_keys=True, default=str).encode("utf-8")
    ).digest()


def direction_discovery_admission_decision(
    direction: Mapping[str, Any],
    cycle: DiscoveryCycle | None,
) -> DirectionDiscoveryAdmissionDecision:
    """Return the shared scheduler/debug decision for one direction cycle."""

    key = str(direction.get("id") or (cycle.direction_key if cycle else "")).strip()

    def decision(
        can_admit_root: bool,
        reason_code: str,
        reason: str,
        next_action: str,
    ) -> DirectionDiscoveryAdmissionDecision:
        return DirectionDiscoveryAdmissionDecision(
            direction_key=key,
            can_admit_root=can_admit_root,
            reason_code=reason_code,
            reason=reason,
            next_action=next_action,
            cycle_id=cycle.id if cycle is not None else None,
            cycle_state=cycle.state if cycle is not None else None,
            cycle_generation=cycle.generation if cycle is not None else None,
        )

    if cycle is None:
        return decision(
            True,
            "no_discovery_cycle",
            "No live or policy-blocking discovery cycle exists.",
            "Commission a new bounded research root.",
        )

    frozen = (cycle.bet_snapshot or {}).get("direction")
    authority_match = direction_authority_matches(frozen, direction)
    authority_reconcilable = cycle.state in {"review_pending", "review_blocked"} or (
        cycle.state == "reviewed"
        and cycle.verdict in POLICY_BLOCKING_DISCOVERY_VERDICTS
    )
    if authority_reconcilable and authority_match is False:
        return decision(
            False,
            "direction_authority_reconciliation_pending",
            "The cycle was commissioned under different direction authority.",
            "Supersede the stale cycle, then commission the current direction.",
        )
    if authority_reconcilable and authority_match is None:
        return decision(
            False,
            "direction_authority_proof_missing",
            "The cycle has no comparable frozen direction-authority proof.",
            "Resolve, park, or close the cycle explicitly before recommissioning.",
        )

    if cycle.state == "open":
        if cycle.source_research_task_id is None:
            return decision(
                True,
                "open_cycle_reservation",
                "An unbound live cycle reservation can admit this research root.",
                "Bind the research root to the existing reservation.",
            )
        return decision(
            False,
            "research_cycle_active",
            "The live discovery cycle already owns a research root.",
            "Wait for the source research cohort to settle.",
        )
    if cycle.state == "admission_closed":
        return decision(
            False,
            "discovery_admission_closed",
            "The discovery cohort is closed to new roots while it settles.",
            "Settle the cohort and advance it to discovery review.",
        )
    if cycle.state == "review_pending":
        return decision(
            False,
            "discovery_review_pending",
            "The settled cycle is waiting for its decision boundary.",
            "Spawn or recover the bounded discovery-review task.",
        )
    if cycle.state == "reviewing":
        return decision(
            False,
            "discovery_review_active",
            "A discovery decision task currently owns the cycle.",
            "Wait for that decision or recover its durable task reference.",
        )
    if cycle.state == "review_blocked":
        prior = dict(cycle.review_result or {})
        error = str(prior.get("error") or "").strip()
        if authority_match is True and error.startswith(
            "direction authority changed before "
        ):
            return decision(
                False,
                "false_authority_block_reconciliation_pending",
                "The review was blocked by evidence-only drift, while true authority is unchanged.",
                "Resume the bounded review from review_pending.",
            )
        return decision(
            False,
            "discovery_review_blocked",
            error or "The discovery decision failed closed.",
            "Resolve the blocked cycle explicitly, or repair its decision prerequisite.",
        )
    if cycle.state == "reviewed" and cycle.verdict in POLICY_BLOCKING_DISCOVERY_VERDICTS:
        verdict = str(cycle.verdict)
        return decision(
            False,
            f"discovery_{verdict}_awaiting_resolution",
            f"The latest discovery disposition is {verdict}.",
            "Continue with a bounded next bet, park it, or close it explicitly."
            if verdict == "ask_operator"
            else "Resolve the disposition explicitly before recommissioning.",
        )
    return decision(
        True,
        "prior_cycle_settled",
        "The latest discovery cycle is terminal and does not block a successor.",
        "Commission the next bounded research root.",
    )


async def load_latest_direction_discovery_cycles(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, DiscoveryCycle]:
    """Load exactly the latest cycle per direction for shared admission reads."""

    latest_generation = (
        select(
            DiscoveryCycle.direction_key.label("direction_key"),
            func.max(DiscoveryCycle.generation).label("generation"),
        )
        .where(DiscoveryCycle.project_id == project_id)
        .group_by(DiscoveryCycle.direction_key)
        .subquery()
    )
    cycles = (
        (
            await session.execute(
                select(DiscoveryCycle).join(
                    latest_generation,
                    (DiscoveryCycle.direction_key == latest_generation.c.direction_key)
                    & (DiscoveryCycle.generation == latest_generation.c.generation),
                )
                .where(DiscoveryCycle.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    return {cycle.direction_key: cycle for cycle in cycles}


async def load_direction_discovery_admission_decisions(
    session: AsyncSession,
    project_id: uuid.UUID,
    directions: list[Mapping[str, Any]],
) -> dict[str, DirectionDiscoveryAdmissionDecision]:
    """Evaluate all supplied directions against one consistent cycle snapshot."""

    cycles = await load_latest_direction_discovery_cycles(session, project_id)
    decisions: dict[str, DirectionDiscoveryAdmissionDecision] = {}
    for direction in directions:
        key = str(direction.get("id") or "").strip()
        if not key:
            continue
        decisions[key] = direction_discovery_admission_decision(
            direction, cycles.get(key)
        )
    return decisions


def _discovery_bet_snapshot(
    direction: Mapping[str, Any], direction_key: str, commissioned_question: str | None
) -> dict[str, Any]:
    """Build the immutable decision contract for one discovery commission."""

    bounded_commission = str(commissioned_question or "").strip()
    return {
        "direction": direction_authority_snapshot(direction),
        "claim": (bounded_commission or str(direction.get("text") or ""))[:2_000],
        "decision_question": str(
            bounded_commission
            or direction.get("completion_criteria")
            or f"Did work on {direction.get('text') or direction_key} create "
            "goal-relevant, inspectable progress?"
        )[:4_000],
        "alternatives": list(direction.get("alternatives") or [])[:12],
    }


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
    commissioned_question: str | None = None,
    now: datetime | None = None,
    join_open_cycle: bool = False,
) -> DiscoveryCycle | None:
    """Return the live direction cycle, bootstrapping generation one if absent.

    The direction-spawn lock and the database's partial live-cycle uniqueness
    constraint serialize roots. Intent and bet inputs are frozen before work
    begins so later review is grounded in the purpose that commissioned it.
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
                DiscoveryCycle.state.in_(LIVE_DISCOVERY_CYCLE_STATES),
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
            # A source-free cycle is only a reservation. When a direct
            # commission legitimately takes that reservation, freeze the
            # question it will actually ask. A conflict later in the same
            # transaction rolls this mutation back; an occupied source keeps
            # its original immutable bet for exact replay.
            pending_request_id = None
            bounded_commission = str(commissioned_question or "").strip()
            if live.source_research_task_id is None and bounded_commission:
                pending_request_id = (
                    await session.execute(
                        select(StrategicSpawnRequest.id)
                        .where(
                            StrategicSpawnRequest.project_id == project.id,
                            StrategicSpawnRequest.discovery_cycle_id == live.id,
                            StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if (
                live.source_research_task_id is None
                and pending_request_id is None
                and bounded_commission
            ):
                live.bet_snapshot = _discovery_bet_snapshot(
                    direction, key, commissioned_question
                )
                live.mechanism_fingerprint = hashlib.sha256(
                    json.dumps(live.bet_snapshot, sort_keys=True, default=str).encode(
                        "utf-8"
                    )
                ).hexdigest()
            return live
        admission = direction_discovery_admission_decision(direction, live)
        raise DiscoveryCycleUnavailable(
            f"direction {key} already has cycle {live.id} in state {live.state}: "
            f"{admission.reason} Next action: {admission.next_action}"
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
    if (
        latest is not None
        and latest.state != "superseded"
        and latest.verdict in POLICY_BLOCKING_DISCOVERY_VERDICTS
    ):
        admission = direction_discovery_admission_decision(direction, latest)
        raise DiscoveryCycleUnavailable(
            f"direction {key} is awaiting policy resolution after {latest.verdict}: "
            f"{admission.reason} Next action: {admission.next_action}"
        )

    generation = (
        int(
            (
                await session.execute(
                    select(func.max(DiscoveryCycle.generation)).where(
                        DiscoveryCycle.project_id == project.id,
                        DiscoveryCycle.direction_key == key,
                    )
                )
            ).scalar_one_or_none()
            or 0
        )
        + 1
    )
    config = runtime_config or {}
    opened_at = now or _utcnow()
    from app.problem_model import resolve_project_purpose
    from app.release_version import current_open_version

    problem_model = dict(project.problem_model or {})
    intent = dict(problem_model.get("intent") or {})
    release = await current_open_version(session, project.id)
    bet_snapshot = _discovery_bet_snapshot(direction, key, commissioned_question)
    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key=key,
        generation=generation,
        outcome_kind="mixed",
        phase="probe",
        intent_snapshot={
            "project_name": project.name,
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
    """Resolve the provenance cycle inherited by a task or spawn request."""

    normalized_project_id = (
        _as_uuid(project_id, field_name="project_id")
        if project_id is not None
        else None
    )

    if explicit_cycle_id is not None:
        return _as_uuid(explicit_cycle_id, field_name="explicit_cycle_id")

    if strategic_spawn_request_id is not None:
        request_id = _as_uuid(
            strategic_spawn_request_id,
            field_name="strategic_spawn_request_id",
        )
        request_filters = [StrategicSpawnRequest.id == request_id]
        if normalized_project_id is not None:
            request_filters.append(
                StrategicSpawnRequest.project_id == normalized_project_id
            )
        request_lineage = (
            await session.execute(
                select(
                    StrategicSpawnRequest.discovery_cycle_id,
                    StrategicSpawnRequest.parent_task_id,
                ).where(*request_filters)
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
            task_filters = [Task.id == current_id]
            if normalized_project_id is not None:
                task_filters.append(Task.project_id == normalized_project_id)
            row = (
                await session.execute(
                    select(Task.discovery_cycle_id, Task.parent_task_id).where(
                        *task_filters
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

    if direction_key is None:
        return None
    if project_id is None or not str(direction_key).strip():
        raise ValueError("project_id and direction_key must be supplied together")
    assert normalized_project_id is not None
    return (
        await session.execute(
            select(DiscoveryCycle.id).where(
                DiscoveryCycle.project_id == normalized_project_id,
                DiscoveryCycle.direction_key == str(direction_key).strip(),
                DiscoveryCycle.state.in_(LIVE_DISCOVERY_CYCLE_STATES),
            )
        )
    ).scalar_one_or_none()
