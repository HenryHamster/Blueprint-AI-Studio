"""Durable review boundary for bounded discovery cycles.

A normal ``discovery_review`` task judges a quiescent cycle from a server-built
evidence packet. Routine continuation stays within the cheap review loop;
consequential or blocked outcomes become durable semantic receipts that wake the
supported Resident session for strategic judgment.

All mutating helpers operate on the caller's transaction and never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from typing import Any, Mapping
import uuid

from sqlmodel import select

from app.direction_contract import DIRECTION_CONTRACT_FIELDS
from app.discovery_review_packet import (
    DISCOVERY_REVIEW_BLOCKED_GRACE as _BLOCKED_GRACE,
    DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS as _DECISION_SNAPSHOT_CHARS,
    DISCOVERY_REVIEW_MAX_PACKET_ITEMS as _MAX_PACKET_ITEMS,
    _canonical_json,
    _checksum,
    _enum_value,
    build_discovery_review_packet as _build_review_packet,
)
from app.models import (
    DiscoveryAdmission,
    DiscoveryCycle,
    Project,
    ProjectMessage,
    StrategicSpawnRequest,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.notification_dispatch import post_event_message
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)

DISCOVERY_REVIEW_TASK_TYPE = "discovery_review"
DISCOVERY_REVIEW_SWEEP_LIMIT = 40
MAX_AUTONOMOUS_DISCOVERY_GENERATIONS = 3
# Routine evidence synthesis belongs on the strong lower tier. Only a
# consequential proposal earns one independent frontier confirmation.
DISCOVERY_REVIEW_ROUTINE_EFFORT = "medium"
DISCOVERY_REVIEW_CONFIRMATION_EFFORT = "max"

_ACTIVE_COHORT_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    }
)
_SOURCE_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)
_SPAWNABLE_CYCLE_STATES = frozenset(
    {"open", "admission_closed", "review_pending"}
)
_VALID_VERDICTS = frozenset(
    {
        "renew",
        "pivot",
        "expand",
        "branch",
        "fallback_pending",
        "park",
        "stop",
        "ask_operator",
        "inconclusive",
    }
)
_VALID_PHASES = frozenset({"probe", "demonstrate", "productize", "release"})
_AUTONOMOUS_CONTINUATION_VERDICTS = frozenset({"renew", "inconclusive"})
_CONSEQUENTIAL_CONTINUATION_VERDICTS = frozenset(
    {"pivot", "expand", "branch", "fallback_pending"}
)
_CONFIRMATION_VERDICTS = frozenset(
    {*_CONSEQUENTIAL_CONTINUATION_VERDICTS, "park", "stop"}
)
_RECOMMENDED_LIMIT_KEYS = frozenset(
    {"task_credit_limit", "attempt_credit_limit", "concurrency_limit"}
)


@dataclass(frozen=True)
class DiscoveryReviewSpawnResult:
    cycle_id: uuid.UUID
    task_id: uuid.UUID | None
    spawned: bool
    ready: bool
    reason: str


class DiscoveryReviewResultError(ValueError):
    """The review output cannot authoritatively transition its cycle."""


def _authoritative_successor_bet(
    current_bet: Mapping[str, Any],
    proposed_bet: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry a cycle's frozen direction proof contract into its successor."""

    successor_bet = dict(proposed_bet)
    current_direction = current_bet.get("direction")
    if not isinstance(current_direction, Mapping):
        return successor_bet
    current_context = current_direction.get("context")
    if not isinstance(current_context, Mapping):
        return successor_bet
    proof_contract = {
        field: current_context[field]
        for field in DIRECTION_CONTRACT_FIELDS
        if field in current_context
    }
    if not proof_contract:
        return successor_bet

    # A next bet may change the mechanism within a direction. It cannot rewrite
    # the purpose and proof boundary against which that mechanism is reviewed.
    frozen_direction = dict(current_direction)
    frozen_direction["context"] = proof_contract
    successor_bet["direction"] = frozen_direction
    return successor_bet


async def resolve_discovery_cycle(
    session,
    *,
    project_id: uuid.UUID,
    direction_key: str,
    action: str,
    next_bet: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> DiscoveryCycle:
    """Apply an explicit operator decision without depending on Resident uptime."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"continue", "close"}:
        raise ValueError("action must be continue or close")
    if normalized_action == "continue":
        live_successor = (
            await session.execute(
                select(DiscoveryCycle)
                .where(
                    DiscoveryCycle.project_id == project_id,
                    DiscoveryCycle.direction_key == direction_key,
                    DiscoveryCycle.state == "open",
                )
                .order_by(DiscoveryCycle.generation.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if live_successor is not None:
            predecessor = (
                await session.execute(
                    select(DiscoveryCycle).where(
                        DiscoveryCycle.project_id == project_id,
                        DiscoveryCycle.direction_key == direction_key,
                        DiscoveryCycle.generation == live_successor.generation - 1,
                    )
                )
            ).scalar_one_or_none()
            prior_resolution = (
                (predecessor.review_result or {}).get("operator_resolution")
                if predecessor is not None
                and isinstance(predecessor.review_result, dict)
                else None
            )
            if (
                predecessor is not None
                and isinstance(prior_resolution, Mapping)
                and prior_resolution.get("action") == "continue"
                and dict(live_successor.bet_snapshot or {})
                == _authoritative_successor_bet(
                    dict(predecessor.bet_snapshot or {}),
                    dict(next_bet or {}),
                )
            ):
                return live_successor
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.project_id == project_id,
                DiscoveryCycle.direction_key == direction_key,
                DiscoveryCycle.state.in_({"reviewed", "review_blocked"}),
            )
            .order_by(DiscoveryCycle.generation.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise ValueError("no operator-resolvable discovery cycle exists")
    resolution = {
        "action": normalized_action,
        "note": str(note or "")[:2_000] or None,
        "resolved_at": _utcnow().isoformat(),
    }
    prior = dict(cycle.review_result or {})
    prior["operator_resolution"] = resolution
    cycle.review_result = prior
    cycle.updated_at = _utcnow()
    if normalized_action == "close":
        cycle.verdict = "stop"
        session.add(cycle)
        return cycle
    if not isinstance(next_bet, Mapping) or not next_bet:
        raise ValueError("continue requires a non-empty next_bet")
    cycle.state = "superseded"
    successor_bet = _authoritative_successor_bet(
        dict(cycle.bet_snapshot or {}),
        next_bet,
    )
    successor = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=cycle.generation + 1,
        outcome_kind=cycle.outcome_kind,
        phase=cycle.phase,
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=successor_bet,
        mechanism_fingerprint=_checksum(successor_bet),
        task_credit_limit=cycle.task_credit_limit,
        attempt_credit_limit=cycle.attempt_credit_limit,
        concurrency_limit=cycle.concurrency_limit,
        deadline_at=_utcnow() + timedelta(hours=24),
    )
    session.add(cycle)
    session.add(successor)
    await session.flush()
    return successor


async def _cycle_is_quiescent(
    session,
    cycle: DiscoveryCycle,
    *,
    now: datetime,
) -> tuple[bool, str]:
    if cycle.source_research_task_id is None:
        return False, "source_research_task_missing"
    source = await session.get(Task, cycle.source_research_task_id)
    if source is None:
        return False, "source_research_task_missing"
    if source.status not in _SOURCE_TERMINAL_STATUSES:
        return False, "source_research_task_active"

    active = (
        await session.execute(
            select(Task.id)
            .where(
                Task.discovery_cycle_id == cycle.id,
                Task.status.in_(_ACTIVE_COHORT_STATUSES),
                Task.id != cycle.review_task_id
                if cycle.review_task_id is not None
                else True,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return False, "cohort_active"
    blocked = (
        await session.execute(
            select(Task.id)
            .where(
                Task.discovery_cycle_id == cycle.id,
                Task.status == TaskStatus.BLOCKED,
                Task.id != cycle.review_task_id
                if cycle.review_task_id is not None
                else True,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if blocked is not None and (
        cycle.deadline_at is None
        or now < cycle.deadline_at + _BLOCKED_GRACE
    ):
        return False, "cohort_blocked_in_grace"
    pending_request = (
        await session.execute(
            select(StrategicSpawnRequest.id)
            .where(
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                StrategicSpawnRequest.task_id.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending_request is not None and (
        cycle.deadline_at is None
        or now < cycle.deadline_at + _BLOCKED_GRACE
    ):
        return False, "spawn_requests_in_grace"
    return True, "quiescent"


async def _cancel_overdue_spawn_requests(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> int:
    """Disposition deferred intents that outlived the envelope and grace period."""

    if (
        cycle.deadline_at is None
        or now < cycle.deadline_at + _BLOCKED_GRACE
    ):
        return 0
    requests = (
        await session.execute(
            select(StrategicSpawnRequest)
            .where(
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                StrategicSpawnRequest.task_id.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    if not requests:
        return 0
    request_ids = [request.id for request in requests]
    admissions = (
        await session.execute(
            select(DiscoveryAdmission).where(
                DiscoveryAdmission.cycle_id == cycle.id,
                DiscoveryAdmission.spawn_request_id.in_(request_ids),
                DiscoveryAdmission.state == "reserved",
            )
        )
    ).scalars().all()
    from app.discovery_admission import release_discovery_admission

    for admission in admissions:
        await release_discovery_admission(
            session,
            cycle_id=cycle.id,
            admission_key=admission.admission_key,
            now=now,
        )
    for request in requests:
        request.state = "cancelled"
        request.admission_reason = "discovery_cycle_review_boundary"
        request.not_before = None
        request.updated_at = now
        session.add(request)
    return len(requests)


async def maybe_close_and_spawn_discovery_review(
    session,
    cycle_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> DiscoveryReviewSpawnResult:
    """Close a terminal cohort and atomically queue exactly one review task."""

    parsed_cycle_id = (
        cycle_id if isinstance(cycle_id, uuid.UUID) else uuid.UUID(str(cycle_id))
    )
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == parsed_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return DiscoveryReviewSpawnResult(
            parsed_cycle_id, None, False, False, "cycle_missing"
        )
    project = await session.get(Project, cycle.project_id)
    stored_review = cycle.review_result or {}
    deferred_confirmation = (
        stored_review.get("confirmation_deferred") == "project_paused"
        if isinstance(stored_review, Mapping)
        else False
    )
    if deferred_confirmation:
        if project is not None and project.paused:
            return DiscoveryReviewSpawnResult(
                cycle.id,
                cycle.review_task_id,
                False,
                True,
                "project_paused",
            )
        first_result = stored_review.get("first_review")
        prior_task = (
            await session.get(Task, cycle.review_task_id)
            if cycle.review_task_id is not None
            else None
        )
        if not isinstance(first_result, Mapping) or prior_task is None:
            cycle.state = "review_blocked"
            cycle.review_result = {
                **dict(stored_review),
                "error": "deferred confirmation lost its first review task",
            }
            cycle.updated_at = _utcnow()
            session.add(cycle)
            return DiscoveryReviewSpawnResult(
                cycle.id, None, False, False, "deferred_confirmation_invalid"
            )
        confirmation = await _spawn_confirmation_review(
            session,
            cycle=cycle,
            prior_task=prior_task,
            first_result=first_result,
            now=now or _utcnow(),
        )
        return DiscoveryReviewSpawnResult(
            cycle.id,
            confirmation.id,
            True,
            True,
            "confirmation_spawned",
        )
    if cycle.review_task_id is not None:
        return DiscoveryReviewSpawnResult(
            cycle.id, cycle.review_task_id, False, True, "review_exists"
        )
    if cycle.state not in _SPAWNABLE_CYCLE_STATES:
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, False, f"cycle_state_{cycle.state}"
        )

    now = now or _utcnow()
    if cycle.deadline_at is not None and now >= cycle.deadline_at:
        cycle.state = "admission_closed"

    ready, reason = await _cycle_is_quiescent(session, cycle, now=now)
    if not ready:
        # Once the source is terminal no new descendant admissions should start,
        # but already-running/queued work remains intact and is allowed to drain.
        if reason in {
            "cohort_active",
            "cohort_blocked_in_grace",
            "spawn_requests_in_grace",
        } and cycle.state == "open":
            cycle.state = "admission_closed"
        if cycle.state == "admission_closed":
            cycle.state = "review_pending"
        session.add(cycle)
        return DiscoveryReviewSpawnResult(cycle.id, None, False, False, reason)

    if project is not None and project.paused:
        cycle.state = "review_pending"
        cycle.updated_at = now
        session.add(cycle)
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, True, "project_paused"
        )

    (
        packet,
        packet_checksum,
        artifact_prerequisites,
        packet_manifest,
        bounded_intent,
        bounded_bet,
    ) = await _build_review_packet(session, cycle, now=now)
    await _cancel_overdue_spawn_requests(session, cycle=cycle, now=now)
    review_revision = int(cycle.revision or 0) + 1
    task = Task(
        project_id=cycle.project_id,
        discovery_cycle_id=cycle.id,
        title=f"Discovery review: {cycle.direction_key}",
        description=(
            "Judge the completed discovery cycle against its frozen intent and bet "
            "using the server-authorized evidence packet."
        ),
        parent_task_id=None,
        payloads=[
            {
                "type": DISCOVERY_REVIEW_TASK_TYPE,
                "cycle_id": str(cycle.id),
                "expected_revision": review_revision,
                "direction_key": cycle.direction_key,
                "intent_snapshot": bounded_intent,
                "bet_snapshot": bounded_bet,
                "outcome_kind": cycle.outcome_kind,
                "phase": cycle.phase,
                "evidence_packet": packet,
                "evidence_packet_manifest": packet_manifest,
                "packet_checksum": packet_checksum,
                "candidate_sha": cycle.candidate_sha,
                "mechanism_fingerprint": cycle.mechanism_fingerprint,
                "review_round": 1,
                "prior_review_task_id": None,
                # Payload routing wins over the task's broad ``strategic``
                # category, preventing every ordinary cycle review from paying
                # the max-tier strategic overhead.
                "effort": DISCOVERY_REVIEW_ROUTINE_EFFORT,
            }
        ],
        context={
            "discovery_review": True,
            "discovery_cycle_id": str(cycle.id),
            "source_research_task_id": (
                str(cycle.source_research_task_id)
                if cycle.source_research_task_id
                else None
            ),
            "artifact_prerequisites": artifact_prerequisites,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        idempotency_key=f"discovery-review:{cycle.id}:round:1",
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "DISCOVERY_REVIEW_BOUNDARY",
                "cycle_id": str(cycle.id),
                "packet_checksum": packet_checksum,
            },
        )
    )
    await session.flush()

    cycle.review_task_id = task.id
    cycle.state = "reviewing"
    cycle.revision = review_revision
    cycle.updated_at = now
    session.add(cycle)
    return DiscoveryReviewSpawnResult(cycle.id, task.id, True, True, "spawned")


def parse_discovery_review_result(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate the model result without accepting prose or inferred defaults."""

    if isinstance(raw, Mapping):
        result = dict(raw)
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DiscoveryReviewResultError("review result is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise DiscoveryReviewResultError("review result must be a JSON object")
        result = parsed
    else:
        raise DiscoveryReviewResultError("review result must be an object")

    try:
        result["cycle_id"] = str(uuid.UUID(str(result.get("cycle_id"))))
    except (TypeError, ValueError, AttributeError):
        raise DiscoveryReviewResultError("cycle_id must be a UUID") from None
    reviewed_revision = result.get("reviewed_revision")
    if isinstance(reviewed_revision, bool) or not isinstance(reviewed_revision, int):
        raise DiscoveryReviewResultError("reviewed_revision must be an integer")

    verdict = result.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise DiscoveryReviewResultError("review verdict is missing or invalid")
    confidence = result.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise DiscoveryReviewResultError("confidence must be between 0 and 1")
    for key in ("decision_summary", "goal_progress_assessment"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DiscoveryReviewResultError(f"{key} must be a non-empty string")
    contradictions = result.get("contradictions")
    if (
        not isinstance(contradictions, list)
        or len(contradictions) > 50
        or any(not isinstance(value, str) for value in contradictions)
    ):
        raise DiscoveryReviewResultError(
            "contradictions must be a bounded list of strings"
        )
    next_bet = result.get("next_bet")
    if next_bet is not None and not isinstance(next_bet, dict):
        raise DiscoveryReviewResultError("next_bet must be an object or null")
    if (
        next_bet is not None
        and len(_canonical_json(next_bet))
        > _DECISION_SNAPSHOT_CHARS
    ):
        raise DiscoveryReviewResultError("next_bet exceeds the decision size limit")
    recommended_phase = result.get("recommended_phase")
    if recommended_phase is not None and recommended_phase not in _VALID_PHASES:
        raise DiscoveryReviewResultError("recommended_phase is invalid")
    recommended_limits = result.get("recommended_limits")
    if not isinstance(recommended_limits, dict) or set(recommended_limits) != set(
        _RECOMMENDED_LIMIT_KEYS
    ):
        raise DiscoveryReviewResultError(
            "recommended_limits must contain exactly the three cycle limit keys"
        )
    for key, value in recommended_limits.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise DiscoveryReviewResultError(
                f"recommended_limits.{key} must be a positive integer or null"
            )
    operator_question = result.get("operator_question")
    if operator_question is not None and (
        not isinstance(operator_question, str) or not operator_question.strip()
    ):
        raise DiscoveryReviewResultError(
            "operator_question must be a non-empty string or null"
        )
    if verdict == "ask_operator" and not operator_question:
        raise DiscoveryReviewResultError(
            "ask_operator verdict requires operator_question"
        )

    evidence_ids = result.get("evidence_citations")
    if (
        not isinstance(evidence_ids, list)
        or len(evidence_ids) > _MAX_PACKET_ITEMS
    ):
        raise DiscoveryReviewResultError(
            "evidence_citations must be a bounded list"
        )
    if not evidence_ids and verdict not in {"inconclusive", "ask_operator"}:
        raise DiscoveryReviewResultError(
            "substantive verdicts require at least one evidence citation"
        )
    normalized_ids: list[str] = []
    for value in evidence_ids:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise DiscoveryReviewResultError(
                "evidence_citations contains an invalid UUID"
            ) from None
        if normalized not in normalized_ids:
            normalized_ids.append(normalized)
    result["evidence_citations"] = normalized_ids
    result["confidence"] = float(confidence)
    return result


async def _emit_review_event(
    session,
    *,
    cycle: DiscoveryCycle,
    task_id: uuid.UUID,
    event_type: str,
    summary: str,
    extra: Mapping[str, Any] | None = None,
    wake_resident: bool = False,
) -> ProjectMessage | None:
    context = {
        "cycle_id": str(cycle.id),
        "review_task_id": str(task_id),
        "direction_key": cycle.direction_key,
        **({"resident_wake_barrier": True} if wake_resident else {}),
        **dict(extra or {}),
    }
    return await post_event_message(
        session,
        cycle.project_id,
        event_type=event_type,
        summary=summary,
        context=context,
        task_id=task_id,
        idempotency_key=f"discovery-review:{task_id}:{event_type}",
    )


async def _create_successor_cycle(
    session,
    *,
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
    now: datetime,
) -> DiscoveryCycle:
    """Apply a safe continuation verdict without requiring Resident uptime."""

    limits = dict(result["recommended_limits"])
    proposed_bet = (
        dict(result["next_bet"])
        if isinstance(result.get("next_bet"), Mapping)
        else dict(cycle.bet_snapshot or {})
    )
    next_bet = _authoritative_successor_bet(
        dict(cycle.bet_snapshot or {}),
        proposed_bet,
    )
    successor = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=cycle.generation + 1,
        outcome_kind=cycle.outcome_kind,
        phase=result.get("recommended_phase") or cycle.phase,
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=next_bet,
        mechanism_fingerprint=_checksum(next_bet),
        task_credit_limit=(
            limits["task_credit_limit"]
            if limits["task_credit_limit"] is not None
            else cycle.task_credit_limit
        ),
        attempt_credit_limit=(
            limits["attempt_credit_limit"]
            if limits["attempt_credit_limit"] is not None
            else cycle.attempt_credit_limit
        ),
        concurrency_limit=(
            limits["concurrency_limit"]
            if limits["concurrency_limit"] is not None
            else cycle.concurrency_limit
        ),
        deadline_at=now + timedelta(hours=24),
    )
    session.add(successor)
    await session.flush()
    return successor


def _decision_proposal(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verdict": result.get("verdict"),
        "next_bet": result.get("next_bet"),
        "recommended_phase": result.get("recommended_phase"),
        "recommended_limits": result.get("recommended_limits"),
    }


def _decision_consequence_reasons(
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
) -> tuple[str, ...]:
    """Derive consequence from effective state changes, not model labels."""

    # ask_operator already fails directly into Resident judgment; paying for a
    # frontier confirmation first would add cost without making it less human.
    if result.get("verdict") == "ask_operator":
        return ()

    reasons: list[str] = []
    if result.get("verdict") in _CONFIRMATION_VERDICTS:
        reasons.append("verdict")

    current_bet = dict(cycle.bet_snapshot or {})
    proposed_bet = result.get("next_bet")
    effective_bet = (
        dict(proposed_bet)
        if isinstance(proposed_bet, Mapping)
        else current_bet
    )
    if _canonical_json(effective_bet) != _canonical_json(current_bet):
        reasons.append("next_bet")

    effective_phase = result.get("recommended_phase") or cycle.phase
    if effective_phase != cycle.phase:
        reasons.append("phase")

    recommended_limits = result.get("recommended_limits")
    if isinstance(recommended_limits, Mapping):
        for key in sorted(_RECOMMENDED_LIMIT_KEYS):
            current_value = int(getattr(cycle, key))
            proposed_value = recommended_limits.get(key)
            effective_value = (
                proposed_value if proposed_value is not None else current_value
            )
            if effective_value != current_value:
                reasons.append(key)

    return tuple(reasons)


def _decision_requires_confirmation(
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
) -> bool:
    return bool(_decision_consequence_reasons(cycle, result))


async def _spawn_confirmation_review(
    session,
    *,
    cycle: DiscoveryCycle,
    prior_task: Task,
    first_result: Mapping[str, Any],
    now: datetime,
) -> Task:
    """Queue an independent second opinion before a consequential direction change."""

    next_revision = int(cycle.revision or 0) + 1
    payload = dict(prior_task.first_payload)
    payload.update(
        {
            "expected_revision": next_revision,
            "review_round": 2,
            "prior_review_task_id": str(prior_task.id),
            # Round two judges this server-canonical proposal against the same
            # frozen evidence. It can reproduce it exactly to confirm or emit a
            # different decision to reject/escalate.
            "confirmation_proposal": _decision_proposal(first_result),
            # This is the cycle's only frontier adjudication. Round two cannot
            # recursively create another confirmation task.
            "effort": DISCOVERY_REVIEW_CONFIRMATION_EFFORT,
        }
    )
    context = dict(prior_task.context or {})
    context["prior_review_task_id"] = str(prior_task.id)
    task = Task(
        project_id=cycle.project_id,
        discovery_cycle_id=cycle.id,
        title=f"Independent confirmation: {cycle.direction_key}",
        description=(
            "Independently judge the frozen discovery evidence. A consequential "
            "direction change requires two matching verdicts."
        ),
        parent_task_id=None,
        payloads=[payload],
        context=context,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        idempotency_key=f"discovery-review:{cycle.id}:round:2",
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "DISCOVERY_REVIEW_CONFIRMATION",
                "cycle_id": str(cycle.id),
                "prior_review_task_id": str(prior_task.id),
                "packet_checksum": payload.get("packet_checksum"),
            },
        )
    )
    await session.flush()
    cycle.review_task_id = task.id
    cycle.state = "reviewing"
    cycle.review_result = {
        "first_review": dict(first_result),
        "confirmation_task_id": str(task.id),
        "confirmation_reasons": list(
            _decision_consequence_reasons(cycle, first_result)
        ),
    }
    cycle.revision = next_revision
    cycle.updated_at = now
    session.add(cycle)
    return task


def _continuation_block_reason(
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
) -> str | None:
    if cycle.generation >= MAX_AUTONOMOUS_DISCOVERY_GENERATIONS:
        return "autonomous_generation_cap_reached"
    next_bet = result.get("next_bet")
    if not isinstance(next_bet, Mapping) or not next_bet:
        return "next_bet_required_for_autonomous_continuation"
    return None


def _validate_monotone_recommended_limits(
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
) -> None:
    recommended = result.get("recommended_limits")
    if not isinstance(recommended, Mapping):
        return
    current = {
        "task_credit_limit": int(cycle.task_credit_limit),
        "attempt_credit_limit": int(cycle.attempt_credit_limit),
        "concurrency_limit": int(cycle.concurrency_limit),
    }
    for key, current_value in current.items():
        proposed = recommended.get(key)
        if proposed is not None and proposed > current_value:
            raise DiscoveryReviewResultError(
                f"recommended_limits.{key} cannot increase autonomously "
                f"from {current_value} to {proposed}"
            )


async def complete_discovery_review(
    session,
    *,
    task: Task,
    raw_result: str | Mapping[str, Any],
) -> DiscoveryCycle:
    """Apply a valid, still-current verdict or fail closed as review_blocked."""

    if task.discovery_cycle_id is None:
        raise DiscoveryReviewResultError("review task has no discovery cycle")
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == task.discovery_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise DiscoveryReviewResultError("discovery cycle no longer exists")
    if cycle.review_task_id != task.id:
        raise DiscoveryReviewResultError("review task is stale for this cycle")
    if cycle.state == "reviewed" and cycle.review_result is not None:
        # Post-commit replay: the verdict and notification already landed.
        return cycle

    payload = task.first_payload
    expected_revision = payload.get("expected_revision")
    stale_reason: str | None = None
    if cycle.state != "reviewing":
        stale_reason = f"cycle is {cycle.state}, not reviewing"
    elif not isinstance(expected_revision, int) or expected_revision != cycle.revision:
        stale_reason = "cycle revision changed while review was running"

    try:
        result = parse_discovery_review_result(raw_result)
        if stale_reason:
            raise DiscoveryReviewResultError(stale_reason)
        _validate_monotone_recommended_limits(cycle, result)
        if result["cycle_id"] != str(cycle.id):
            raise DiscoveryReviewResultError(
                "review cycle_id does not match the task cycle"
            )
        if result["reviewed_revision"] != expected_revision:
            raise DiscoveryReviewResultError(
                "reviewed_revision does not match the reviewed cycle snapshot"
            )
        packet_manifest = payload.get("evidence_packet_manifest")
        if (
            isinstance(packet_manifest, Mapping)
            and packet_manifest.get("required_evidence_omitted") is True
            and result["verdict"] not in {"inconclusive", "ask_operator"}
        ):
            raise DiscoveryReviewResultError(
                "incomplete packet permits only inconclusive or ask_operator"
            )
        payload_candidate_sha = payload.get("candidate_sha")
        if cycle.candidate_sha and payload_candidate_sha != cycle.candidate_sha:
            raise DiscoveryReviewResultError(
                "task candidate_sha does not match the pinned cycle candidate"
            )
        cited_ids = {str(uuid.UUID(value)) for value in result["evidence_citations"]}
        packet_ids = {
            str(item.get("evidence_id"))
            for item in payload.get("evidence_packet", [])
            if isinstance(item, Mapping) and item.get("evidence_id")
        }
        if not cited_ids.issubset(packet_ids):
            raise DiscoveryReviewResultError(
                "review cited evidence outside its authorized packet"
            )
        review_round = payload.get("review_round", 1)
        if review_round not in {1, 2}:
            raise DiscoveryReviewResultError("review_round must be one or two")
        first_result: Mapping[str, Any] | None = None
        if review_round == 2:
            stored = cycle.review_result or {}
            candidate = (
                stored.get("first_review")
                if isinstance(stored, Mapping)
                else None
            )
            if not isinstance(candidate, Mapping):
                raise DiscoveryReviewResultError(
                    "confirmation review has no durable first-review result"
                )
            first_result = candidate
            proposal = payload.get("confirmation_proposal")
            if (
                not isinstance(proposal, Mapping)
                or _canonical_json(proposal)
                != _canonical_json(_decision_proposal(first_result))
            ):
                raise DiscoveryReviewResultError(
                    "confirmation proposal does not match the durable first review"
                )
    except DiscoveryReviewResultError as exc:
        if cycle.state == "reviewing":
            cycle.state = "review_blocked"
            cycle.review_result = {
                "error": str(exc),
                "review_task_id": str(task.id),
                "expected_revision": expected_revision,
            }
            cycle.revision = int(cycle.revision or 0) + 1
            cycle.updated_at = _utcnow()
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.review_blocked",
                summary=(
                    f"Discovery review blocked for {cycle.direction_key}: {str(exc)[:240]}"
                ),
                extra={"reason": str(exc)[:500]},
                wake_resident=True,
            )
        raise

    verdict = result["verdict"]
    consequence_reasons = _decision_consequence_reasons(cycle, result)
    if review_round == 1 and _decision_requires_confirmation(cycle, result):
        project = await session.get(Project, cycle.project_id)
        if project is not None and project.paused:
            cycle.state = "review_pending"
            cycle.review_result = {
                "first_review": dict(result),
                "prior_review_task_id": str(task.id),
                "confirmation_deferred": "project_paused",
                "confirmation_reasons": list(consequence_reasons),
            }
            cycle.revision = int(cycle.revision or 0) + 1
            cycle.updated_at = _utcnow()
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.review_completed",
                summary=(
                    f"Discovery review for {cycle.direction_key}: {verdict}; "
                    "confirmation deferred while the project is paused."
                ),
                extra={
                    "verdict": verdict,
                    "confidence": result["confidence"],
                    "review_result": result,
                    "confirmation_reasons": list(consequence_reasons),
                    "policy_status": "confirmation_deferred_project_paused",
                },
            )
            return cycle
        confirmation = await _spawn_confirmation_review(
            session,
            cycle=cycle,
            prior_task=task,
            first_result=result,
            now=_utcnow(),
        )
        await _emit_review_event(
            session,
            cycle=cycle,
            task_id=task.id,
            event_type="discovery.review_completed",
            summary=(
                f"Discovery review for {cycle.direction_key}: {verdict}; "
                "independent confirmation queued."
            ),
            extra={
                "verdict": verdict,
                "confidence": result["confidence"],
                "review_result": result,
                "confirmation_task_id": str(confirmation.id),
                "confirmation_reasons": list(consequence_reasons),
                "policy_status": "confirmation_queued",
            },
        )
        return cycle

    if review_round == 2:
        assert first_result is not None
        first_decision = _decision_proposal(first_result)
        confirmation_decision = _decision_proposal(result)
        if _canonical_json(first_decision) != _canonical_json(
            confirmation_decision
        ):
            cycle.state = "review_blocked"
            cycle.verdict = "ask_operator"
            cycle.review_result = {
                "first_review": dict(first_result),
                "confirmation_review": result,
                "error": "independent reviews disagree",
            }
            cycle.reviewed_at = _utcnow()
            cycle.updated_at = cycle.reviewed_at
            cycle.revision = int(cycle.revision or 0) + 1
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.review_completed",
                summary=(
                    f"Discovery reviews disagree for {cycle.direction_key}: "
                    "Resident adjudication is required."
                ),
                extra={
                    "first_review": dict(first_result),
                    "confirmation_review": result,
                    "policy_status": "resident_adjudication_required",
                },
                wake_resident=True,
            )
            return cycle

    cycle.verdict = verdict
    cycle.review_result = (
        {
            "first_review": dict(first_result),
            "confirmation_review": result,
            "confirmation_reasons": list(consequence_reasons),
        }
        if first_result is not None
        else result
    )
    cycle.reviewed_at = _utcnow()
    cycle.updated_at = cycle.reviewed_at
    cycle.state = "reviewed"
    cycle.revision = int(cycle.revision or 0) + 1
    session.add(cycle)
    await session.flush()

    successor: DiscoveryCycle | None = None
    continuation_requested = (
        verdict in _AUTONOMOUS_CONTINUATION_VERDICTS
        or (
            review_round == 2
            and verdict in _CONSEQUENTIAL_CONTINUATION_VERDICTS
        )
    )
    continuation_block_reason = (
        _continuation_block_reason(cycle, result)
        if continuation_requested
        else None
    )
    if continuation_requested and continuation_block_reason is None:
        successor = await _create_successor_cycle(
            session,
            cycle=cycle,
            result=result,
            now=cycle.reviewed_at,
        )

    await _emit_review_event(
        session,
        cycle=cycle,
        task_id=task.id,
        event_type="discovery.review_completed",
        summary=(
            f"Discovery review for {cycle.direction_key}: {verdict} "
            f"({result['decision_summary'][:240]})"
        ),
        extra={
            "verdict": verdict,
            "confidence": result["confidence"],
            "review_result": cycle.review_result,
            "next_cycle_id": str(successor.id) if successor is not None else None,
            "policy_status": (
                "continued"
                if successor is not None
                else (
                    continuation_block_reason
                    or (
                        "resident_review_required"
                        if verdict in {"ask_operator", "stop", "park"}
                        else "halted"
                    )
                )
            ),
        },
        wake_resident=(
            (review_round == 2 and bool(consequence_reasons))
            or verdict == "ask_operator"
            or continuation_block_reason is not None
        ),
    )
    return cycle


async def block_failed_discovery_review(
    session,
    *,
    task: Task,
    reason: str,
) -> bool:
    """Fail closed after the normal task exhausts its own retries."""

    if task.discovery_cycle_id is None:
        return False
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(DiscoveryCycle.id == task.discovery_cycle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        cycle is None
        or cycle.review_task_id != task.id
        or cycle.state != "reviewing"
    ):
        return False
    cycle.state = "review_blocked"
    cycle.review_result = {
        "error": reason[:2_000],
        "review_task_id": str(task.id),
        "task_status": str(_enum_value(task.status)),
    }
    cycle.revision = int(cycle.revision or 0) + 1
    cycle.updated_at = _utcnow()
    session.add(cycle)
    await _emit_review_event(
        session,
        cycle=cycle,
        task_id=task.id,
        event_type="discovery.review_blocked",
        summary=f"Discovery review blocked for {cycle.direction_key}: {reason[:240]}",
        extra={"reason": reason[:500]},
        wake_resident=True,
    )
    return True


async def _handle_discovery_review_completion(
    task: Task,
    logs: str,
    session,
) -> None:
    """Task-lifecycle adapter for a completed normal review task.

    The agent handler already performs one focused JSON repair.  Invalid output
    here is therefore a terminal review outcome, not a reason to reinterpret
    prose or fan out more work.
    """

    try:
        await complete_discovery_review(
            session,
            task=task,
            raw_result=logs,
        )
    except DiscoveryReviewResultError:
        logger.warning(
            "[DISCOVERY_REVIEW] Review task %s failed closed",
            task.id,
            exc_info=True,
        )


async def sweep_discovery_reviews(
    session,
    now: datetime | None = None,
    *,
    limit: int = DISCOVERY_REVIEW_SWEEP_LIMIT,
) -> int:
    """Bounded missed-trigger repair for expired or quiescent cycles."""

    now = now or _utcnow()
    cycle_ids = list(
        (
            await session.execute(
                select(DiscoveryCycle.id)
                .where(DiscoveryCycle.state.in_(_SPAWNABLE_CYCLE_STATES))
                .order_by(DiscoveryCycle.updated_at.asc(), DiscoveryCycle.id.asc())
                .limit(max(1, min(limit, DISCOVERY_REVIEW_SWEEP_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for cycle_id in cycle_ids:
        try:
            async with session.begin_nested():
                result = await maybe_close_and_spawn_discovery_review(
                    session, cycle_id, now=now
                )
            if result.spawned or result.reason in {
                "cohort_active",
                "cohort_blocked_in_grace",
                "spawn_requests_in_grace",
                "source_research_task_active",
            }:
                changed += 1
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Discovery-review sweep skipped malformed cycle %s",
                cycle_id,
            )
    return changed
