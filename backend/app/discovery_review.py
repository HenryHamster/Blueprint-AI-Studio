"""Durable outcome-decision boundary for bounded discovery cycles.

One normal ``discovery_review`` task converts a settled research-parent outcome
into exactly one actionable disposition.  The controller never creates a
second-opinion task and never closes a cycle into an undecided sink: an
``advance`` decision durably creates both its successor cycle and the request
that will run it, while every other successful decision is an explicit
park/stop/operator disposition.

All helpers mutate the caller's transaction and never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from typing import Any, Mapping
import uuid

from sqlalchemy import or_
from sqlmodel import select

from app.branch_resolution import get_project_integration_branch
from app.capability_registry import (
    annotate_capability_proof_status,
    registry_capability_evidence_refs_map,
    registry_capability_vocabulary,
)
from app.discovery_admission import (
    POLICY_BLOCKING_DISCOVERY_VERDICTS,
    direction_authority_matches,
    direction_authority_snapshot,
)
from app.discovery_authority_reconciliation import (
    reconcile_stalled_direction_authority as _reconcile_stalled_direction_authority,
)
from app.discovery_review_packet import (
    DISCOVERY_REVIEW_DECISION_SNAPSHOT_CHARS as _DECISION_SNAPSHOT_CHARS,
    DISCOVERY_REVIEW_MAX_PACKET_ITEMS as _MAX_PACKET_ITEMS,
    _canonical_json,
    _checksum,
    _enum_value,
    build_discovery_review_packet as _build_review_packet,
)
from app.direction_priority import direction_task_priority
from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
from app.models import (
    DiscoveryCycle,
    Project,
    ProjectMessage,
    StrategicSpawnRequest,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.notification_dispatch import post_event_message
from app.research_deferred_carry import (
    canonical_deferred_finding_hash,
    unacknowledged_deferred_findings,
)
from app.research_finding_policy import NON_SPAWNING_FINDING_TYPES
from app.shared_state import runtime_config as _runtime_config
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    acquire_automatic_task_creation_authority,
)
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)

DISCOVERY_REVIEW_TASK_TYPE = "discovery_review"
DISCOVERY_REVIEW_SWEEP_LIMIT = 40
DISCOVERY_REVIEW_ROUTINE_EFFORT = "medium"
DISCOVERY_EVIDENCE_ESCALATION_GRACE = timedelta(hours=2)
# Total decision-task attempts (original + respawns) before a cycle fails
# closed into review_blocked. Attempts are counted from the task ledger
# (terminal discovery_review tasks on the cycle), not from mutable cycle
# state, so a respawn wiping review_result cannot reset the budget.
DISCOVERY_REVIEW_MAX_DECISION_ATTEMPTS = 3

_ACTIVE_COHORT_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)
_SOURCE_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
_SPAWNABLE_CYCLE_STATES = frozenset(
    {"open", "admission_closed", "review_pending"}
)
_SWEEPABLE_CYCLE_STATES = _SPAWNABLE_CYCLE_STATES | {"reviewing", "review_blocked"}
_VALID_ACTIONS = frozenset({"advance", "park", "stop", "ask_operator"})
_VALID_NEXT_BET_MODES = frozenset({"research", "measure", "build"})
_LEGACY_REVIEW_PAYLOAD_KEYS = frozenset(
    {
        "review_round",
        "confirmation_proposal",
        "recommended_phase",
        "recommended_limits",
        "candidate_sha",
    }
)
_RESULT_KEYS = frozenset(
    {
        "cycle_id",
        "reviewed_revision",
        "action",
        "summary",
        "evidence_citations",
        "next_bet",
        "park_reason",
        "resume_condition",
        "operator_question",
    }
)
_NEXT_BET_KEYS = frozenset(
    {
        "mode",
        "claim",
        "decision_question",
        "success_evidence",
        "retirement_condition",
    }
)
@dataclass(frozen=True)
class DiscoveryReviewSpawnResult:
    cycle_id: uuid.UUID
    task_id: uuid.UUID | None
    spawned: bool
    ready: bool
    reason: str


class DiscoveryReviewResultError(ValueError):
    """The decision output cannot authoritatively transition its cycle."""


class DiscoveryCycleBusy(RuntimeError):
    """The latest resolvable cycle is locked by a concurrent decision."""


def _validate_frozen_direction_authority(
    cycle: DiscoveryCycle,
    direction: Mapping[str, Any],
    *,
    action: str,
) -> None:
    frozen = (cycle.bet_snapshot or {}).get("direction")
    if not isinstance(frozen, Mapping):
        raise DiscoveryReviewResultError(
            f"direction authority snapshot is missing before {action}"
        )
    if direction_authority_matches(frozen, direction) is not True:
        raise DiscoveryReviewResultError(
            f"direction authority changed before {action}"
        )


def _authoritative_successor_bet(
    current_bet: Mapping[str, Any],
    proposed_bet: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry the frozen direction proof contract into the successor bet."""

    successor_bet = dict(proposed_bet)
    current_direction = current_bet.get("direction")
    if not isinstance(current_direction, Mapping):
        return successor_bet
    successor_bet["direction"] = direction_authority_snapshot(current_direction)
    return successor_bet


async def resolve_discovery_cycle(
    session,
    *,
    project_id: uuid.UUID,
    direction_key: str,
    cycle_id: uuid.UUID | str,
    cycle_revision: int,
    action: str,
    next_bet: Mapping[str, Any] | None = None,
    note: str | None = None,
    authority_task: Task | None = None,
    skip_locked: bool = False,
) -> DiscoveryCycle:
    """Resolve an operator question without creating a stranded successor."""

    try:
        parsed_cycle_id = uuid.UUID(str(cycle_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("cycle_id must be a UUID") from None
    if isinstance(cycle_revision, bool) or not isinstance(cycle_revision, int):
        raise ValueError("cycle_revision must be an integer")
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"continue", "park", "close"}:
        raise ValueError("action must be continue, park, or close")
    if normalized_action == "continue":
        try:
            normalized_next_bet = _normalize_next_bet(next_bet)
        except DiscoveryReviewResultError as exc:
            raise ValueError(str(exc)) from exc
    else:
        if next_bet is not None:
            raise ValueError(f"{normalized_action} must not include next_bet")
        normalized_next_bet = None
    normalized_note = str(note or "")[:2_000] or None
    request_hash = _checksum(
        {
            "cycle_id": str(parsed_cycle_id),
            "cycle_revision": cycle_revision,
            "action": normalized_action,
            "next_bet": normalized_next_bet,
            "note": normalized_note,
        }
    )
    resolvable_states = {"reviewed", "review_blocked", "review_pending"}
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.project_id == project_id,
                DiscoveryCycle.direction_key == direction_key,
                DiscoveryCycle.id == parsed_cycle_id,
                DiscoveryCycle.state.in_(resolvable_states),
            )
            .with_for_update(skip_locked=skip_locked)
        )
    ).scalar_one_or_none()
    if cycle is None:
        if skip_locked:
            existing_cycle_id = (
                await session.execute(
                    select(DiscoveryCycle.id)
                    .where(
                        DiscoveryCycle.project_id == project_id,
                        DiscoveryCycle.direction_key == direction_key,
                        DiscoveryCycle.id == parsed_cycle_id,
                        DiscoveryCycle.state.in_(resolvable_states),
                    )
                )
            ).scalar_one_or_none()
            if existing_cycle_id is not None:
                raise DiscoveryCycleBusy("discovery cycle is being updated; retry")
        raise ValueError("no operator-resolvable discovery cycle exists")
    prior = dict(cycle.review_result or {})
    evidence_pending = (
        cycle.state == "review_pending"
        and prior.get("status") == "EVIDENCE_PENDING"
    )
    if cycle.state == "review_pending" and not evidence_pending:
        raise ValueError("latest discovery cycle is not operator-resolvable")
    existing_resolution = prior.get("operator_resolution")
    if isinstance(existing_resolution, Mapping):
        if existing_resolution.get("request_hash") != request_hash:
            raise ValueError("discovery cycle already has a conflicting resolution")
        next_cycle_id = prior.get("next_cycle_id")
        if normalized_action == "continue" and next_cycle_id:
            try:
                successor = (
                    await session.execute(
                        select(DiscoveryCycle).where(
                            DiscoveryCycle.id == uuid.UUID(str(next_cycle_id)),
                            DiscoveryCycle.project_id == cycle.project_id,
                        )
                    )
                ).scalar_one_or_none()
            except (TypeError, ValueError, AttributeError):
                successor = None
            if successor is not None:
                return successor
            raise ValueError("resolved discovery successor is missing")
        if normalized_action == "close":
            return cycle
        if normalized_action == "park":
            return cycle
    if int(cycle.revision or 0) != cycle_revision:
        raise ValueError("discovery cycle revision changed; refresh before resolving")
    if cycle.state == "reviewed" and cycle.verdict not in {
        "ask_operator",
        "park",
    }:
        raise ValueError("latest discovery cycle does not require operator resolution")

    resolution = {
        "action": normalized_action,
        "note": normalized_note,
        "expected_cycle_revision": cycle_revision,
        "request_hash": request_hash,
        "resolved_at": _utcnow().isoformat(),
    }
    cycle.updated_at = _utcnow()
    cycle.reviewed_at = cycle.updated_at
    cycle.revision = int(cycle.revision or 0) + 1
    if normalized_action == "close":
        await _apply_direction_disposition(
            session,
            cycle=cycle,
            result={
                "action": "stop",
                "summary": str(normalized_note or "Closed by operator")[:4_000],
            },
            now=cycle.updated_at,
            require_frozen_authority=False,
        )
        prior["operator_resolution"] = resolution
        cycle.review_result = prior
        cycle.verdict = "stop"
        cycle.state = "reviewed"
        session.add(cycle)
        return cycle
    if normalized_action == "park":
        missing = [
            str(item).strip()
            for item in prior.get("missing_required_evidence", [])
            if str(item).strip()
        ]
        reason = str(normalized_note or "Parked by operator pending stronger evidence")[:2_000]
        resume_condition = (
            "; ".join(missing)[:2_000]
            or "Operator or Resident supplies a new bounded next bet"
        )
        await _apply_direction_disposition(
            session,
            cycle=cycle,
            result={
                "action": "park",
                "summary": reason,
                "park_reason": reason,
                "resume_condition": resume_condition,
            },
            now=cycle.updated_at,
            require_frozen_authority=False,
        )
        prior["operator_resolution"] = resolution
        cycle.review_result = prior
        cycle.verdict = "park"
        cycle.state = "reviewed"
        session.add(cycle)
        return cycle
    assert normalized_next_bet is not None
    decision_task = (
        (
            await session.execute(
                select(Task).where(
                    Task.id == cycle.review_task_id,
                    Task.project_id == cycle.project_id,
                )
            )
        ).scalar_one_or_none()
        if cycle.review_task_id is not None
        else authority_task
    )
    if decision_task is None and authority_task is not None:
        decision_task = authority_task
    if decision_task is None and cycle.source_research_task_id is not None:
        decision_task = (
            await session.execute(
                select(Task).where(
                    Task.id == cycle.source_research_task_id,
                    Task.project_id == cycle.project_id,
                )
            )
        ).scalar_one_or_none()
    if decision_task is None:
        raise ValueError("operator resolution has no durable authority task")
    if decision_task.project_id != cycle.project_id:
        raise ValueError("operator authority task belongs to another project")
    resuming_park = cycle.verdict == "park"
    current_direction_authority = await _validate_direction_for_advance(
        session,
        cycle=cycle,
        now=cycle.updated_at,
        resume_park=resuming_park,
        require_frozen_authority=False,
    )
    current_bet = dict(cycle.bet_snapshot or {})
    current_bet["direction"] = current_direction_authority
    successor_bet = _authoritative_successor_bet(
        current_bet, normalized_next_bet
    )
    cycle.verdict = "advance"
    cycle.state = "reviewed"
    session.add(cycle)
    await session.flush()
    successor, request = await _create_successor_with_request(
        session,
        cycle=cycle,
        task=decision_task,
        result={"next_bet": normalized_next_bet},
        now=cycle.updated_at,
        direction_authority_override=current_direction_authority,
    )
    # The helper applies the frozen direction proof contract. Assert that the
    # separately-computed value stays equivalent so this operator path cannot
    # silently drift from normal decisions.
    if dict(successor.bet_snapshot or {}) != successor_bet:
        raise ValueError("operator successor proof contract changed unexpectedly")
    prior["operator_resolution"] = resolution
    prior["next_cycle_id"] = str(successor.id)
    prior["successor_spawn_request_id"] = str(request.id)
    cycle.review_result = prior
    session.add(cycle)
    return successor


async def _cycle_is_quiescent(
    session, cycle: DiscoveryCycle, *, now: datetime
) -> tuple[bool, str]:
    pending_request = (
        await session.execute(
            select(StrategicSpawnRequest.id)
            .where(
                StrategicSpawnRequest.project_id == cycle.project_id,
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
                StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                StrategicSpawnRequest.task_id.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if cycle.source_research_task_id is None:
        if pending_request is not None:
            return False, "source_research_task_pending"
        return False, "source_research_task_missing"
    source = (
        await session.execute(
            select(Task)
            .where(
                Task.id == cycle.source_research_task_id,
                Task.project_id == cycle.project_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None:
        if pending_request is not None:
            return False, "source_research_task_pending"
        return False, "source_research_task_missing"
    if source.status not in _SOURCE_TERMINAL_STATUSES:
        return False, "source_research_task_active"
    if (
        source.status == TaskStatus.COMPLETED
        and (source.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
    ):
        return False, "source_research_findings_pending"
    if source.post_commit_pending:
        return False, "source_research_post_commit_pending"
    active = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == cycle.project_id,
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
    post_commit_pending = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == cycle.project_id,
                Task.discovery_cycle_id == cycle.id,
                Task.post_commit_pending.is_(True),
                Task.id != cycle.review_task_id
                if cycle.review_task_id is not None
                else True,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if post_commit_pending is not None:
        return False, "cohort_post_commit_pending"
    if pending_request is not None:
        return False, "spawn_requests_pending"
    return True, "quiescent"


async def _emit_evidence_pending(
    session,
    *,
    cycle: DiscoveryCycle,
    missing: list[str],
    packet_checksum: str,
    wake_resident: bool = False,
) -> None:
    event_type = (
        "discovery.operator_question" if wake_resident else "discovery.evidence_pending"
    )
    operator_question = (
        "Required discovery outcome evidence is still missing. Provide a complete "
        "next bet to continue, park pending stronger evidence, or close the direction."
        if wake_resident
        else None
    )
    await post_event_message(
        session,
        cycle.project_id,
        event_type=event_type,
        summary=(
            f"Discovery outcome evidence pending for {cycle.direction_key}: "
            f"{', '.join(missing)[:240]}"
        ),
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": int(cycle.revision or 0),
            "direction_key": cycle.direction_key,
            "missing_required_evidence": missing,
            "policy_status": "EVIDENCE_PENDING",
            **({"resident_wake_barrier": True} if wake_resident else {}),
            **({"operator_question": operator_question} if operator_question else {}),
        },
        task_id=cycle.source_research_task_id,
        idempotency_key=(
            f"discovery-evidence-pending:{cycle.id}:{packet_checksum}:"
            f"{'operator' if wake_resident else 'pending'}"
        ),
    )


def _parsed_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _hold_for_evidence(
    session,
    *,
    cycle: DiscoveryCycle,
    missing: list[str],
    packet_checksum: str,
    now: datetime,
    force_wake: bool = False,
) -> str:
    prior = dict(cycle.review_result or {})
    same_gap = (
        prior.get("status") == "EVIDENCE_PENDING"
        and prior.get("packet_checksum") == packet_checksum
        and prior.get("missing_required_evidence") == missing
    )
    first_seen = (
        _parsed_timestamp(prior.get("first_seen_at")) if same_gap else None
    ) or now
    was_escalated = bool(prior.get("escalated_at")) if same_gap else False
    should_escalate = force_wake or now >= (
        first_seen + DISCOVERY_EVIDENCE_ESCALATION_GRACE
    )
    newly_escalated = should_escalate and not was_escalated
    cycle.state = "review_pending"
    cycle.review_result = {
        "status": "EVIDENCE_PENDING",
        "missing_required_evidence": missing,
        "packet_checksum": packet_checksum,
        "first_seen_at": first_seen.isoformat(),
        "checked_at": now.isoformat(),
        "escalated_at": (
            now.isoformat()
            if newly_escalated
            else prior.get("escalated_at") if same_gap else None
        ),
    }
    cycle.updated_at = now
    session.add(cycle)
    if not same_gap or newly_escalated:
        await _emit_evidence_pending(
            session,
            cycle=cycle,
            missing=missing,
            packet_checksum=packet_checksum,
            wake_resident=should_escalate,
        )
    if newly_escalated:
        return "evidence_pending_escalated"
    return "evidence_pending_unchanged" if same_gap else "evidence_pending"


async def _block_orphaned_review(
    session,
    *,
    cycle: DiscoveryCycle,
    reason: str,
    now: datetime,
) -> None:
    cycle.state = "review_blocked"
    cycle.review_result = {
        "status": "DECISION_FAILED",
        "error": reason,
        "review_task_id": str(cycle.review_task_id)
        if cycle.review_task_id
        else None,
    }
    cycle.revision = int(cycle.revision or 0) + 1
    cycle.updated_at = now
    session.add(cycle)
    await post_event_message(
        session,
        cycle.project_id,
        event_type="discovery.review_blocked",
        summary=f"Discovery decision blocked for {cycle.direction_key}: {reason}",
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": int(cycle.revision or 0),
            "direction_key": cycle.direction_key,
            "resident_wake_barrier": True,
            "policy_status": "DECISION_FAILED",
        },
        task_id=cycle.source_research_task_id,
        idempotency_key=f"discovery-review-orphaned:{cycle.id}:{cycle.revision}",
    )


async def _reconcile_review_reference(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> DiscoveryReviewSpawnResult | None:
    if cycle.review_task_id is None:
        if cycle.state == "reviewing":
            await _block_orphaned_review(
                session,
                cycle=cycle,
                reason="decision task reference is missing",
                now=now,
            )
            return DiscoveryReviewSpawnResult(
                cycle.id, None, False, False, "review_blocked"
            )
        return None
    review_task = (
        await session.execute(
            select(Task).where(
                Task.id == cycle.review_task_id,
                Task.project_id == cycle.project_id,
            )
        )
    ).scalar_one_or_none()
    if review_task is None:
        await _block_orphaned_review(
            session,
            cycle=cycle,
            reason="decision task was deleted",
            now=now,
        )
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, False, "review_blocked"
        )
    if review_task.status in _ACTIVE_COHORT_STATUSES:
        return DiscoveryReviewSpawnResult(
            cycle.id, review_task.id, False, True, "review_exists"
        )
    is_legacy = bool(_LEGACY_REVIEW_PAYLOAD_KEYS.intersection(review_task.first_payload))
    if is_legacy and cycle.state in {"review_pending", "reviewing"}:
        cycle.review_task_id = None
        cycle.review_result = {
            "status": "LEGACY_DECISION_REQUEUED",
            "legacy_review_task_id": str(review_task.id),
            "legacy_task_status": str(_enum_value(review_task.status)),
        }
        cycle.state = "review_pending"
        cycle.updated_at = now
        session.add(cycle)
        await session.flush()
        return None
    if cycle.state == "reviewing":
        await block_failed_discovery_review(
            session,
            task=review_task,
            reason=f"decision task ended {str(_enum_value(review_task.status))}",
        )
        if cycle.state == "review_pending":
            # Bounded retry accepted: fall through so this same sweep spawns
            # the replacement decision task instead of waiting a full interval.
            return None
        return DiscoveryReviewSpawnResult(
            cycle.id, review_task.id, False, False, "review_blocked"
        )
    return DiscoveryReviewSpawnResult(
        cycle.id, review_task.id, False, True, "review_exists"
    )


async def _notify_legacy_advisory_disposition(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> DiscoveryReviewSpawnResult | None:
    prior = dict(cycle.review_result or {})
    if (
        cycle.state != "reviewed"
        or cycle.verdict != "ask_operator"
        or prior.get("status") != "LEGACY_DISPOSITION_REQUIRES_RESOLUTION"
    ):
        return None
    legacy_verdict = str(prior.get("legacy_advisory_verdict") or "").strip()
    cycle.review_result = {
        **prior,
        "status": "LEGACY_DISPOSITION_AWAITING_RESOLUTION",
        "notified_at": now.isoformat(),
    }
    cycle.revision = int(cycle.revision or 0) + 1
    cycle.updated_at = now
    session.add(cycle)
    await post_event_message(
        session,
        cycle.project_id,
        event_type="discovery.operator_question",
        summary=(
            f"A legacy discovery review advised {legacy_verdict or 'a disposition'} "
            "but did not change direction authority. Continue with a complete "
            "next bet, park it explicitly, or close it."
        ),
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": cycle.revision,
            "direction_key": cycle.direction_key,
            "legacy_advisory_verdict": legacy_verdict or None,
            "operator_question": (
                "Should this direction continue with a complete bounded next bet, "
                "be parked explicitly, or be closed?"
            ),
            "resident_wake_barrier": True,
            "policy_status": "LEGACY_DISPOSITION_AWAITING_RESOLUTION",
        },
        task_id=cycle.review_task_id or cycle.source_research_task_id,
        idempotency_key=f"discovery-legacy-disposition:{cycle.id}",
    )
    return DiscoveryReviewSpawnResult(
        cycle.id,
        cycle.review_task_id,
        False,
        False,
        "legacy_disposition_needs_resolution",
    )


async def _reconcile_legacy_orphan_successor(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
) -> DiscoveryReviewSpawnResult | None:
    """Turn an old request-less successor into one explicit bounded decision.

    The previous controller created a live successor cycle but no Task or
    StrategicSpawnRequest. The new controller must not mistake that shape for
    missing outcome evidence or fabricate a modern next-bet contract from the
    legacy free-form snapshot.
    """

    if cycle.source_research_task_id is not None or cycle.generation <= 1:
        return None
    request_exists = (
        await session.execute(
            select(StrategicSpawnRequest.id)
            .where(
                StrategicSpawnRequest.project_id == cycle.project_id,
                StrategicSpawnRequest.discovery_cycle_id == cycle.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if request_exists is not None:
        return None
    predecessor = (
        await session.execute(
            select(DiscoveryCycle).where(
                DiscoveryCycle.project_id == cycle.project_id,
                DiscoveryCycle.direction_key == cycle.direction_key,
                DiscoveryCycle.generation == cycle.generation - 1,
                DiscoveryCycle.state.in_({"reviewed", "superseded"}),
                DiscoveryCycle.verdict == "advance",
            )
        )
    ).scalar_one_or_none()
    if predecessor is None:
        return None
    authority_task_id = (
        predecessor.review_task_id or predecessor.source_research_task_id
    )
    cycle.state = "reviewed"
    cycle.verdict = "ask_operator"
    cycle.review_task_id = authority_task_id
    cycle.review_result = {
        "status": "LEGACY_SUCCESSOR_NEEDS_NEXT_BET",
        "predecessor_cycle_id": str(predecessor.id),
        "legacy_bet_checksum": _checksum(dict(cycle.bet_snapshot or {})),
    }
    cycle.reviewed_at = now
    cycle.updated_at = now
    cycle.revision = int(cycle.revision or 0) + 1
    session.add(cycle)
    await post_event_message(
        session,
        cycle.project_id,
        event_type="discovery.operator_question",
        summary=(
            "A legacy discovery continuation has no dispatchable next-bet "
            "contract. Provide a complete next bet, park it, or close it."
        ),
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": cycle.revision,
            "direction_key": cycle.direction_key,
            "operator_question": (
                "What complete bounded next bet should replace this legacy continuation?"
            ),
            "resident_wake_barrier": True,
            "policy_status": "LEGACY_SUCCESSOR_NEEDS_NEXT_BET",
        },
        task_id=authority_task_id,
        idempotency_key=f"discovery-legacy-successor:{cycle.id}",
    )
    return DiscoveryReviewSpawnResult(
        cycle.id, authority_task_id, False, False, "legacy_successor_needs_next_bet"
    )


async def maybe_close_and_spawn_discovery_review(
    session,
    cycle_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> DiscoveryReviewSpawnResult:
    """Queue exactly one decision task once the research parent has settled."""

    parsed_cycle_id = (
        cycle_id if isinstance(cycle_id, uuid.UUID) else uuid.UUID(str(cycle_id))
    )
    # Resolve the parent project without a child lock, then take the universal
    # automatic-task admission lock before locking/mutating the DiscoveryCycle.
    # This preserves Project -> child order and makes pause linearizable with
    # the decision-task INSERT below.
    cycle_project_id = (
        await session.execute(
            select(DiscoveryCycle.project_id).where(
                DiscoveryCycle.id == parsed_cycle_id
            )
        )
    ).scalar_one_or_none()
    if cycle_project_id is None:
        return DiscoveryReviewSpawnResult(
            parsed_cycle_id, None, False, False, "cycle_missing"
        )
    admission = await acquire_automatic_task_creation_authority(
        session, cycle_project_id, skip_locked=True
    )
    if admission not in {
        AUTOMATIC_TASK_CREATION_ALLOWED,
        AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    }:
        return DiscoveryReviewSpawnResult(
            parsed_cycle_id, None, False, False, admission
        )
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.id == parsed_cycle_id,
                DiscoveryCycle.project_id == cycle_project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        return DiscoveryReviewSpawnResult(
            parsed_cycle_id, None, False, False, "cycle_missing"
        )
    now = now or _utcnow()
    authority_reconciliation = await _reconcile_stalled_direction_authority(
        session,
        cycle=cycle,
        now=now,
    )
    if authority_reconciliation in {
        "authority_reconciliation_deferred",
        "authority_superseded",
        "authority_proof_missing",
        "authority_proof_missing_unchanged",
        "authority_reconciliation_budget_exhausted",
    }:
        return DiscoveryReviewSpawnResult(
            cycle.id,
            cycle.review_task_id,
            False,
            False,
            authority_reconciliation,
        )
    legacy_disposition = await _notify_legacy_advisory_disposition(
        session,
        cycle=cycle,
        now=now,
    )
    if legacy_disposition is not None:
        return legacy_disposition
    existing_review = await _reconcile_review_reference(
        session,
        cycle=cycle,
        now=now,
    )
    if existing_review is not None:
        return existing_review
    if cycle.state not in _SPAWNABLE_CYCLE_STATES:
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, False, f"cycle_state_{cycle.state}"
        )

    legacy_successor = await _reconcile_legacy_orphan_successor(
        session,
        cycle=cycle,
        now=now,
    )
    if legacy_successor is not None:
        return legacy_successor

    if cycle.deadline_at is not None and now >= cycle.deadline_at:
        cycle.state = "admission_closed"
    ready, reason = await _cycle_is_quiescent(session, cycle, now=now)
    if not ready:
        if reason == "source_research_task_missing":
            missing = ["source research parent is missing"]
            pending_reason = await _hold_for_evidence(
                session,
                cycle=cycle,
                missing=missing,
                packet_checksum=_checksum(
                    {"cycle_id": str(cycle.id), "missing": missing}
                ),
                now=now,
                force_wake=True,
            )
            return DiscoveryReviewSpawnResult(
                cycle.id, None, False, False, pending_reason
            )
        if reason in {
            "cohort_active",
            "spawn_requests_pending",
            "source_research_task_pending",
        } and cycle.state == "open":
            cycle.state = "admission_closed"
        if cycle.state == "admission_closed":
            cycle.state = "review_pending"
        cycle.updated_at = now
        session.add(cycle)
        return DiscoveryReviewSpawnResult(cycle.id, None, False, False, reason)

    if admission == AUTOMATIC_TASK_CREATION_PROJECT_PAUSED:
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
    missing = [
        str(item)[:500]
        for item in packet_manifest.get("missing_required_evidence", [])
        if str(item).strip()
    ]
    if missing:
        pending_reason = await _hold_for_evidence(
            session,
            cycle=cycle,
            missing=missing,
            packet_checksum=packet_checksum,
            now=now,
        )
        return DiscoveryReviewSpawnResult(
            cycle.id, None, False, False, pending_reason
        )

    review_revision = int(cycle.revision or 0) + 1
    task = Task(
        project_id=cycle.project_id,
        discovery_cycle_id=cycle.id,
        title=f"Discovery decision: {cycle.direction_key}",
        description=(
            "Choose one actionable next disposition from the settled research "
            "parent's semantic outcome receipt."
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
                "evidence_packet": packet,
                "evidence_packet_manifest": packet_manifest,
                "packet_checksum": packet_checksum,
                "mechanism_fingerprint": cycle.mechanism_fingerprint,
                "effort": DISCOVERY_REVIEW_ROUTINE_EFFORT,
                "provider_preference": ["anthropic"],
            }
        ],
        context={
            "discovery_review": True,
            "discovery_cycle_id": str(cycle.id),
            "source_research_task_id": str(cycle.source_research_task_id),
            "artifact_prerequisites": artifact_prerequisites,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        idempotency_key=f"discovery-decision:{cycle.id}",
    )
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "DISCOVERY_DECISION_BOUNDARY",
                "cycle_id": str(cycle.id),
                "packet_checksum": packet_checksum,
            },
        )
    )
    await session.flush()
    cycle.review_task_id = task.id
    cycle.review_result = None
    cycle.state = "reviewing"
    cycle.revision = review_revision
    cycle.updated_at = now
    session.add(cycle)
    return DiscoveryReviewSpawnResult(cycle.id, task.id, True, True, "spawned")


def _required_text(result: Mapping[str, Any], key: str) -> str:
    value = result.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryReviewResultError(f"{key} must be a non-empty string")
    return value.strip()


def _normalize_next_bet(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(_NEXT_BET_KEYS):
        raise DiscoveryReviewResultError("advance requires an exact next_bet contract")
    next_bet = dict(value)
    if next_bet.get("mode") not in _VALID_NEXT_BET_MODES:
        raise DiscoveryReviewResultError("next_bet.mode is invalid")
    for key in _NEXT_BET_KEYS - {"mode"}:
        if not isinstance(next_bet.get(key), str) or not next_bet[key].strip():
            raise DiscoveryReviewResultError(
                f"next_bet.{key} must be a non-empty string"
            )
        next_bet[key] = next_bet[key].strip()
    if len(_canonical_json(next_bet)) > _DECISION_SNAPSHOT_CHARS:
        raise DiscoveryReviewResultError("next_bet exceeds the decision size limit")
    return next_bet


def _convert_escalation_to_evidence_bet(
    result: Mapping[str, Any],
    *,
    cycle: DiscoveryCycle,
    decision_receipt_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Dispatch a reviewer's unanswered evidence question instead of escalating.

    ``ask_operator`` exists for the questions only the operator can answer --
    preference, authority, scope, priority.  It is not an evidence-acquisition
    channel.  When the decision packet grants binding authority the reviewer
    already holds the power to commission the measurement it says is missing,
    so handing that question back parks a live direction behind a human for no
    reason: direction ``d-1210b978`` cycle ``81ba8010`` sat at
    ``discovery_ask_operator_awaiting_resolution`` for ~16 hours on 2026-08-24
    asking permission to re-dispatch a verification task it was already
    authorised to dispatch.

    The reviewer's epistemic caution is preserved exactly -- no pass/fail grade
    is fabricated for the bet it could not verify.  Its own question becomes
    the ``decision_question`` of one bounded ``measure`` successor, verbatim,
    and its own summary is kept and annotated rather than rewritten.

    Returns ``(result, conversion_record)``.  When the packet carries no parent
    outcome receipt to cite -- a shape the packet builder never emits -- there
    is no valid binding successor to author, so the escalation is returned
    untouched rather than fabricated into one.
    """

    if not decision_receipt_ids:
        return dict(result), None
    question = str(result.get("operator_question") or "").strip()
    if not question:
        return dict(result), None

    citations = [
        str(value) for value in result.get("evidence_citations") or []
    ]
    for receipt_id in sorted(decision_receipt_ids):
        if receipt_id not in citations:
            citations.append(receipt_id)
    citations = citations[:_MAX_PACKET_ITEMS]

    next_bet = _normalize_next_bet(
        {
            "mode": "measure",
            "claim": (
                "The evidence this decision packet lacks can be obtained by one "
                "bounded measurement run by this engine, without an operator "
                f"decision, on direction {cycle.direction_key}."
            ),
            "decision_question": question[:2_000],
            "success_evidence": (
                "A recorded measurement result -- command/test output, file "
                "contents, or an existing receipt -- that answers the decision "
                "question directly and is citable in the next decision packet."
            ),
            "retirement_condition": (
                "Retire this bet once the measurement is recorded, or if one "
                "bounded task cannot produce it, in which case park or stop the "
                "direction on that finding rather than escalating again."
            ),
        }
    )
    disposition = (
        "Engine disposition: this decision packet granted binding authority, so "
        "the unanswered evidence question was dispatched as a bounded measure "
        "bet instead of escalated to the operator. No pass/fail grade was "
        "recorded for the prior bet."
    )
    # Trim the reviewer's own words, never the disposition note: an audit that
    # cannot see why the verdict changed is worse than a shortened summary.
    reviewer_summary = str(result.get("summary") or "").strip()
    summary = (
        f"{reviewer_summary[: max(4_000 - len(disposition) - 2, 0)]}\n\n"
        f"{disposition}"
    ).strip()[:4_000]
    converted = {
        **dict(result),
        "action": "advance",
        "summary": summary,
        "evidence_citations": citations,
        "next_bet": next_bet,
        "park_reason": None,
        "resume_condition": None,
        "operator_question": None,
    }
    conversion_record = {
        "reason": "binding_decision_allowed",
        "original_action": "ask_operator",
        "original_operator_question": question[:2_000],
        "dispatched_bet_mode": "measure",
    }
    return converted, conversion_record


def parse_discovery_review_result(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate one total action without coercing ambiguous model output."""

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
    if set(result) != set(_RESULT_KEYS):
        raise DiscoveryReviewResultError(
            "review result must contain exactly the decision contract keys"
        )
    try:
        result["cycle_id"] = str(uuid.UUID(str(result.get("cycle_id"))))
    except (TypeError, ValueError, AttributeError):
        raise DiscoveryReviewResultError("cycle_id must be a UUID") from None
    revision = result.get("reviewed_revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise DiscoveryReviewResultError("reviewed_revision must be an integer")
    action = result.get("action")
    if action not in _VALID_ACTIONS:
        raise DiscoveryReviewResultError("action is missing or invalid")
    result["summary"] = _required_text(result, "summary")[:4_000]

    citations = result.get("evidence_citations")
    if not isinstance(citations, list) or len(citations) > _MAX_PACKET_ITEMS:
        raise DiscoveryReviewResultError("evidence_citations must be a bounded list")
    normalized: list[str] = []
    for value in citations:
        try:
            item = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise DiscoveryReviewResultError(
                "evidence_citations contains an invalid UUID"
            ) from None
        if item not in normalized:
            normalized.append(item)
    if action != "ask_operator" and not normalized:
        raise DiscoveryReviewResultError(
            "advance, park, and stop require evidence citations"
        )
    result["evidence_citations"] = normalized

    next_bet = result.get("next_bet")
    park_reason = result.get("park_reason")
    resume_condition = result.get("resume_condition")
    operator_question = result.get("operator_question")
    if action == "advance":
        result["next_bet"] = _normalize_next_bet(next_bet)
        if any(
            value is not None
            for value in (park_reason, resume_condition, operator_question)
        ):
            raise DiscoveryReviewResultError(
                "advance requires non-applicable fields to be null"
            )
    elif action == "park":
        result["park_reason"] = _required_text(result, "park_reason")[:2_000]
        result["resume_condition"] = _required_text(
            result, "resume_condition"
        )[:2_000]
        if next_bet is not None or operator_question is not None:
            raise DiscoveryReviewResultError(
                "park requires next_bet and operator_question to be null"
            )
    elif action == "ask_operator":
        result["operator_question"] = _required_text(
            result, "operator_question"
        )[:2_000]
        if any(
            value is not None
            for value in (next_bet, park_reason, resume_condition)
        ):
            raise DiscoveryReviewResultError(
                "ask_operator requires non-applicable fields to be null"
            )
    elif any(
        value is not None
        for value in (next_bet, park_reason, resume_condition, operator_question)
    ):
        raise DiscoveryReviewResultError(
            "stop requires non-applicable fields to be null"
        )
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
    return await post_event_message(
        session,
        cycle.project_id,
        event_type=event_type,
        summary=summary,
        context={
            "cycle_id": str(cycle.id),
            "cycle_revision": int(cycle.revision or 0),
            "review_task_id": str(task_id),
            "direction_key": cycle.direction_key,
            **({"resident_wake_barrier": True} if wake_resident else {}),
            **dict(extra or {}),
        },
        task_id=task_id,
        idempotency_key=f"discovery-review:{task_id}:{event_type}",
    )


async def _apply_direction_disposition(
    session,
    *,
    cycle: DiscoveryCycle,
    result: Mapping[str, Any],
    now: datetime,
    require_frozen_authority: bool = True,
) -> None:
    action = str(result["action"])
    if action not in {"park", "stop"}:
        return
    from app.direction_terminal_guard import (
        mark_auto_direction_terminal,
        validate_direction_terminal_transition,
    )
    from app.task_locking import with_directions_locked

    found = False

    def mutate(directions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal found
        for direction in directions:
            if str(direction.get("id") or "") != cycle.direction_key:
                continue
            found = True
            if require_frozen_authority:
                _validate_frozen_direction_authority(
                    cycle, direction, action=action
                )
            if action == "park":
                status = str(direction.get("status") or "active").strip().lower()
                explicit_park_refresh = (
                    not require_frozen_authority and status == "paused"
                )
                if status != "active" and not explicit_park_refresh:
                    raise DiscoveryReviewResultError(
                        f"direction authority changed to {status} before park"
                    )
                direction["status"] = "paused"
                direction["paused_reason"] = str(result["park_reason"])
                direction["paused_at"] = now.isoformat()
                direction["discovery_resume_condition"] = str(
                    result["resume_condition"]
                )
                direction["auto_paused"] = True
            else:
                status = str(direction.get("status") or "active").strip().lower()
                explicit_park_close = (
                    not require_frozen_authority and status == "paused"
                )
                if status != "active" and not explicit_park_close:
                    raise DiscoveryReviewResultError(
                        f"direction authority changed to {status} before stop"
                    )
                allowed, reason = validate_direction_terminal_transition(
                    direction,
                    rationale=str(result["summary"]),
                    terminal_reason="explicit_abandonment",
                )
                if not allowed:
                    raise DiscoveryReviewResultError(
                        f"direction retirement rejected: {reason}"
                    )
                mark_auto_direction_terminal(
                    direction,
                    outcome="stopped",
                    reason=str(result["summary"]),
                    completed_at=now.isoformat(),
                )
            direction["discovery_decision"] = {
                "cycle_id": str(cycle.id),
                "action": action,
                "summary": str(result["summary"]),
                "decided_at": now.isoformat(),
            }
            break
        return directions

    await with_directions_locked(session, cycle.project_id, mutate)
    if not found:
        raise DiscoveryReviewResultError(
            f"direction {cycle.direction_key} is missing from project authority"
        )


async def _validate_direction_for_advance(
    session,
    *,
    cycle: DiscoveryCycle,
    now: datetime,
    resume_park: bool = False,
    require_frozen_authority: bool = True,
) -> dict[str, Any]:
    """Lock current direction authority before commissioning successor work."""

    from app.task_locking import with_directions_locked

    found = False
    current_authority: dict[str, Any] | None = None

    def mutate(directions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal current_authority, found
        for direction in directions:
            if str(direction.get("id") or "") != cycle.direction_key:
                continue
            found = True
            if require_frozen_authority:
                _validate_frozen_direction_authority(
                    cycle, direction, action="advance"
                )
            status = str(direction.get("status") or "active").strip().lower()
            if resume_park:
                if status != "paused":
                    raise DiscoveryReviewResultError(
                        "parked direction is no longer paused and cannot be resumed"
                    )
                direction["status"] = "active"
                direction["auto_paused"] = False
                direction["resumed_at"] = now.isoformat()
                direction.pop("paused_reason", None)
                direction.pop("paused_at", None)
                direction.pop("discovery_resume_condition", None)
                direction["discovery_decision"] = {
                    "cycle_id": str(cycle.id),
                    "action": "resume",
                    "decided_at": now.isoformat(),
                }
            elif status != "active":
                raise DiscoveryReviewResultError(
                    f"direction authority changed to {status} before advance"
                )
            current_authority = direction_authority_snapshot(direction)
            break
        return directions

    await with_directions_locked(session, cycle.project_id, mutate)
    if not found:
        raise DiscoveryReviewResultError(
            f"direction {cycle.direction_key} is missing from project authority"
        )
    assert current_authority is not None
    return current_authority


def _successor_specification(
    *,
    cycle: DiscoveryCycle,
    successor: DiscoveryCycle,
    task: Task,
    project: Project,
    next_bet: Mapping[str, Any],
    prior_deferred_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    claim = str(next_bet["claim"])
    # Discovery successors used to bypass the canonical autonomous-research
    # envelope entirely. Most importantly, they dropped the capacity signal, so
    # generation 2+ silently reverted to the legacy 1-3 hard cap even after a
    # generation-1 root received real headroom. Keep the successor bounded to
    # its frozen bet while preserving the same execution ceiling and scheduler
    # priority as a normal direction-owned root. Carried metadata is useful
    # prompt context but cannot spawn a task, so it must not consume admission
    # headroom.
    from app.research_completion import effective_research_finding_headroom

    frozen_direction = (successor.bet_snapshot or {}).get("direction")
    if not isinstance(frozen_direction, Mapping):
        frozen_direction = None
    current_direction = next(
        (
            direction
            for direction in (project.directions or [])
            if isinstance(direction, Mapping)
            and str(direction.get("id") or "") == cycle.direction_key
        ),
        None,
    )
    direction_context = current_direction or frozen_direction
    raw_paths = direction_context.get("paths") if direction_context else None
    capacity_consuming_carry = sum(
        1
        for finding in prior_deferred_findings
        if str(finding.get("type") or "").strip().lower()
        not in NON_SPAWNING_FINDING_TYPES
    )
    payload: dict[str, Any] = {
        "type": "research",
        "current_direction_id": cycle.direction_key,
        "directions": [
            {
                "id": cycle.direction_key,
                "text": claim,
                "status": "active",
                "priority": (
                    direction_context.get("priority", "medium")
                    if direction_context
                    else "medium"
                ),
                "paths": (
                    [
                        path[:300]
                        for path in raw_paths[:30]
                        if isinstance(path, str) and path
                    ]
                    if isinstance(raw_paths, list)
                    else []
                ),
                "completion_criteria": next_bet["success_evidence"],
                "research_context": {
                    "mode": next_bet["mode"],
                    "decision_question": next_bet["decision_question"],
                    "retirement_condition": next_bet["retirement_condition"],
                },
            }
        ],
        "base_branch": get_project_integration_branch(project),
        "work_headroom": effective_research_finding_headroom(
            capacity_consuming_carry
        ),
        "discovery_generation": successor.generation,
        "discovery_phase": successor.phase,
        "discovery_intent_snapshot": dict(successor.intent_snapshot or {}),
        "discovery_bet_snapshot": dict(successor.bet_snapshot or {}),
    }
    if project.build_config:
        payload["build_config"] = project.build_config
    research_suffix = (_runtime_config.get("agent_prompt_suffixes", {}) or {}).get(
        "research"
    )
    if isinstance(research_suffix, str) and research_suffix.strip():
        payload["prompt_suffix"] = research_suffix.strip()
    available_capabilities = annotate_capability_proof_status(
        registry_capability_vocabulary(project.problem_model),
        project.capability_proof_records,
        registry_capability_evidence_refs_map(project.problem_model),
    )
    if available_capabilities:
        payload["available_capabilities"] = available_capabilities
    # Deliberately omit payload.model / payload.effort. The dispatcher resolves
    # those fields from unified task_type_routing; a copied legacy per-type value
    # would outrank and downgrade that canonical route.
    return {
        "task": {
            "title": f"Research: {claim}"[:200],
            "description": (
                f"Discovery successor ({next_bet['mode']}): "
                f"{next_bet['decision_question']}"
            )[:500],
            "payloads": [payload],
            "context": {
                "direction_id": cycle.direction_key,
                "discovery_decision_task_id": str(task.id),
                "discovery_decision_mode": next_bet["mode"],
                **(
                    {"prior_deferred_findings": prior_deferred_findings}
                    if prior_deferred_findings
                    else {}
                ),
            },
            "priority": direction_task_priority(direction_context),
            "watch": False,
        }
    }


def _successor_deferred_findings(
    cycle: DiscoveryCycle,
    source_task: Task | None,
) -> list[dict[str, Any]]:
    """Carry direction-scoped deferred work across a discovery generation."""

    if source_task is None:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in unacknowledged_deferred_findings(source_task):
        raw_direction_id = str(raw.get("direction_id") or "").strip()
        if raw_direction_id and raw_direction_id != cycle.direction_key:
            continue
        finding = dict(raw)
        # Completion treats a missing finding direction as the owning research
        # task's direction. Preserve that same meaning at the generation
        # boundary instead of dropping otherwise-valid parent-scoped work.
        finding["direction_id"] = cycle.direction_key
        # ``finding_hash`` is agent-authored routing lineage, not a carry
        # credential. Distinct findings must not collapse merely because an
        # artifact supplied the same raw hash for both.
        identity = canonical_deferred_finding_hash(finding)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(finding)

    def deferred_count(finding: Mapping[str, Any]) -> int:
        try:
            return int(finding.get("deferred_count") or 0)
        except (TypeError, ValueError):
            return 0

    result.sort(key=deferred_count, reverse=True)
    # Do not truncate before there is a durable consumed/overflow protocol.
    # The successor request is currently the only handoff from this cycle's
    # source task; anything omitted here becomes unreachable in generation N+1.
    # The completion handler owns the execution cap and persists its overflow
    # on the new source task for the following generation.
    return result


async def _create_successor_with_request(
    session,
    *,
    cycle: DiscoveryCycle,
    task: Task,
    result: Mapping[str, Any],
    now: datetime,
    direction_authority_override: Mapping[str, Any] | None = None,
) -> tuple[DiscoveryCycle, StrategicSpawnRequest]:
    if task.project_id != cycle.project_id:
        raise DiscoveryReviewResultError(
            "decision task belongs to another project"
        )
    project = await session.get(Project, cycle.project_id)
    if project is None:
        raise DiscoveryReviewResultError(
            f"project {cycle.project_id} disappeared before successor dispatch"
        )
    source_task = None
    if cycle.source_research_task_id is not None:
        source_task = (
            await session.execute(
                select(Task).where(
                    Task.id == cycle.source_research_task_id,
                    Task.project_id == cycle.project_id,
                )
            )
        ).scalar_one_or_none()
        if source_task is None:
            raise DiscoveryReviewResultError(
                "source research task is missing or belongs to another project"
            )

    current_bet = dict(cycle.bet_snapshot or {})
    if direction_authority_override is not None:
        current_bet["direction"] = dict(direction_authority_override)
    next_bet = _authoritative_successor_bet(current_bet, dict(result["next_bet"]))
    successor = DiscoveryCycle(
        project_id=cycle.project_id,
        direction_key=cycle.direction_key,
        generation=cycle.generation + 1,
        outcome_kind=cycle.outcome_kind,
        phase=cycle.phase,
        intent_snapshot=dict(cycle.intent_snapshot or {}),
        bet_snapshot=next_bet,
        mechanism_fingerprint=_checksum(next_bet),
        deadline_at=now + timedelta(hours=24),
    )
    session.add(successor)
    await session.flush()
    prior_deferred_findings = _successor_deferred_findings(cycle, source_task)
    specification = _successor_specification(
        cycle=cycle,
        successor=successor,
        task=task,
        project=project,
        next_bet=result["next_bet"],
        prior_deferred_findings=prior_deferred_findings,
    )
    from app.strategic_spawn_requests import (
        stable_specification_hash,
        stable_work_specification_hash,
        strategic_work_idempotency_key,
    )

    request_key = f"discovery-successor:{cycle.id}"
    work_specification_hash = stable_work_specification_hash(specification)
    active_task_idempotency_key = strategic_work_idempotency_key(
        project_id=cycle.project_id,
        direction_id=cycle.direction_key,
        request_key=request_key,
    )

    request = StrategicSpawnRequest(
        project_id=cycle.project_id,
        parent_task_id=task.id,
        idempotency_key=request_key,
        task_type="research",
        specification=specification,
        specification_hash=stable_specification_hash(specification),
        direction_id=cycle.direction_key,
        discovery_cycle_id=successor.id,
        state="deferred",
        admission_reason="discovery_decision_successor_queued",
        not_before=now,
        policy_snapshot={
            "source": "discovery_decision",
            "predecessor_cycle_id": str(cycle.id),
            "decision_task_id": str(task.id),
            "active_task_idempotency_key": active_task_idempotency_key,
            "work_specification_hash": work_specification_hash,
        },
    )
    session.add(request)
    await session.flush()
    return successor, request


async def complete_discovery_review(
    session,
    *,
    task: Task,
    raw_result: str | Mapping[str, Any],
) -> DiscoveryCycle:
    """Atomically apply one actionable decision or fail visibly."""

    if task.discovery_cycle_id is None:
        raise DiscoveryReviewResultError("review task has no discovery cycle")
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.id == task.discovery_cycle_id,
                DiscoveryCycle.project_id == task.project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise DiscoveryReviewResultError("discovery cycle no longer exists")
    if cycle.review_task_id != task.id:
        raise DiscoveryReviewResultError("review task is stale for this cycle")
    if cycle.state == "reviewed" and cycle.review_result is not None:
        return cycle

    payload = task.first_payload
    expected_revision = payload.get("expected_revision")
    now = _utcnow()
    escalation_conversion: dict[str, Any] | None = None
    try:
        if cycle.state != "reviewing":
            raise DiscoveryReviewResultError(
                f"cycle is {cycle.state}, not reviewing"
            )
        if (
            not isinstance(expected_revision, int)
            or expected_revision != cycle.revision
        ):
            raise DiscoveryReviewResultError(
                "cycle revision changed while review was running"
            )
        quiescent, quiescence_reason = await _cycle_is_quiescent(
            session, cycle, now=now
        )
        invalidation_reason = quiescence_reason if not quiescent else None
        if invalidation_reason is None:
            _, current_packet_checksum, _, _, _, _ = await _build_review_packet(
                session, cycle, now=now
            )
            if payload.get("packet_checksum") != current_packet_checksum:
                invalidation_reason = "review_packet_stale"
        if invalidation_reason is not None:
            stale_review_task_id = cycle.review_task_id
            cycle.state = "review_pending"
            cycle.review_task_id = None
            cycle.review_result = {
                "status": "REVIEW_PACKET_INVALIDATED",
                "reason": invalidation_reason,
                "stale_review_task_id": str(stale_review_task_id),
            }
            cycle.revision = int(cycle.revision or 0) + 1
            cycle.updated_at = now
            session.add(cycle)
            await _emit_review_event(
                session,
                cycle=cycle,
                task_id=task.id,
                event_type="discovery.review_packet_invalidated",
                summary=(
                    f"Discovery decision packet invalidated for {cycle.direction_key}: "
                    f"{invalidation_reason}"
                ),
                extra={
                    "reason": invalidation_reason,
                    "policy_status": "REVIEW_PACKET_INVALIDATED",
                },
            )
            return cycle
        result = parse_discovery_review_result(raw_result)
        if result["cycle_id"] != str(cycle.id):
            raise DiscoveryReviewResultError(
                "review cycle_id does not match the task cycle"
            )
        if result["reviewed_revision"] != expected_revision:
            raise DiscoveryReviewResultError(
                "reviewed_revision does not match the reviewed cycle snapshot"
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
        decision_receipt_ids = {
            str(item.get("evidence_id"))
            for item in payload.get("evidence_packet", [])
            if isinstance(item, Mapping)
            and item.get("semantic_role") == "parent_outcome"
            and item.get("evidence_id")
        }
        packet_manifest = payload.get("evidence_packet_manifest")
        binding_decision_allowed = not (
            isinstance(packet_manifest, Mapping)
            and (
                packet_manifest.get("binding_decision_allowed") is False
                or packet_manifest.get("required_evidence_omitted") is True
            )
        )
        if result["action"] != "ask_operator" and not binding_decision_allowed:
            raise DiscoveryReviewResultError(
                "incomplete packet semantics permit only ask_operator"
            )
        if result["action"] == "ask_operator" and binding_decision_allowed:
            # The reviewer had the authority to commission exactly the work it
            # asked permission for, so convert the escalation into the bounded
            # measurement bet that acquires the missing evidence. See
            # _convert_escalation_to_evidence_bet for the incident this closes.
            result, escalation_conversion = _convert_escalation_to_evidence_bet(
                result,
                cycle=cycle,
                decision_receipt_ids=decision_receipt_ids,
            )
            cited_ids = {
                str(uuid.UUID(value)) for value in result["evidence_citations"]
            }
        if (
            result["action"] != "ask_operator"
            and not cited_ids.intersection(decision_receipt_ids)
        ):
            raise DiscoveryReviewResultError(
                "binding decision must cite the current parent outcome receipt"
            )
        if result["action"] == "advance":
            await _validate_direction_for_advance(
                session,
                cycle=cycle,
                now=now,
            )
        elif result["action"] in {"park", "stop"}:
            await _apply_direction_disposition(
                session,
                cycle=cycle,
                result=result,
                now=now,
            )
    except DiscoveryReviewResultError as exc:
        if cycle.state == "reviewing":
            cycle.state = "review_blocked"
            cycle.review_result = {
                "status": "DECISION_FAILED",
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
                    f"Discovery decision blocked for {cycle.direction_key}: "
                    f"{str(exc)[:240]}"
                ),
                extra={"reason": str(exc)[:500], "policy_status": "DECISION_FAILED"},
                wake_resident=True,
            )
        raise

    action = str(result["action"])
    conversion_record = (
        {"converted_from_ask_operator": escalation_conversion}
        if escalation_conversion is not None
        else {}
    )
    cycle.verdict = action
    cycle.review_result = {**dict(result), **conversion_record}
    cycle.reviewed_at = now
    cycle.updated_at = now
    cycle.revision = int(cycle.revision or 0) + 1

    successor: DiscoveryCycle | None = None
    request: StrategicSpawnRequest | None = None
    if action == "advance":
        # Close the old live row inside this transaction before inserting the
        # successor, satisfying the unique-live-direction constraint without
        # exposing an intermediate state to other transactions.
        cycle.state = "reviewed"
        session.add(cycle)
        await session.flush()
        successor, request = await _create_successor_with_request(
            session,
            cycle=cycle,
            task=task,
            result=result,
            now=now,
        )
        cycle.review_result = {
            **dict(result),
            **conversion_record,
            "next_cycle_id": str(successor.id),
            "successor_spawn_request_id": str(request.id),
        }
    else:
        cycle.state = "reviewed"
    session.add(cycle)
    await session.flush()

    await _emit_review_event(
        session,
        cycle=cycle,
        task_id=task.id,
        event_type="discovery.review_completed",
        summary=(
            f"Discovery decision for {cycle.direction_key}: {action} "
            f"({result['summary'][:240]})"
        ),
        extra={
            "action": action,
            "review_result": cycle.review_result,
            "next_cycle_id": str(successor.id) if successor else None,
            "successor_spawn_request_id": str(request.id) if request else None,
            "policy_status": (
                "successor_requested" if action == "advance" else action
            ),
        },
        wake_resident=False,
    )
    if action == "ask_operator":
        await _emit_review_event(
            session,
            cycle=cycle,
            task_id=task.id,
            event_type="discovery.operator_question",
            summary=str(result["operator_question"]),
            extra={
                "action": action,
                "operator_question": str(result["operator_question"]),
                "policy_status": "ask_operator",
            },
            wake_resident=True,
        )
    elif action == "park":
        await _emit_review_event(
            session,
            cycle=cycle,
            task_id=task.id,
            event_type="discovery.direction_parked",
            summary=(
                f"Discovery direction {cycle.direction_key} was parked: "
                f"{result['park_reason'][:240]}"
            ),
            extra={
                "action": action,
                "resume_condition": str(result["resume_condition"]),
                "policy_status": "park",
            },
            wake_resident=True,
        )
    return cycle


async def block_failed_discovery_review(
    session,
    *,
    task: Task,
    reason: str,
) -> bool:
    """Fail visibly after the normal decision task exhausts its retries."""

    if task.discovery_cycle_id is None:
        return False
    cycle = (
        await session.execute(
            select(DiscoveryCycle)
            .where(
                DiscoveryCycle.id == task.discovery_cycle_id,
                DiscoveryCycle.project_id == task.project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if cycle is None or cycle.review_task_id != task.id or cycle.state != "reviewing":
        return False
    # A decision task that died without producing a decision (VM loss, runner
    # outage, provision failure) says nothing about the cycle's evidence — so
    # respawn a fresh decision task, bounded by the ledger-derived attempt
    # count, instead of failing the cycle closed on the first transient death.
    # Semantic failures (a completed task whose output cannot transition the
    # cycle) still fail closed via complete_discovery_review.
    from sqlalchemy import func as _sql_func

    prior_attempts = (
        await session.execute(
            select(_sql_func.count())
            .select_from(Task)
            .where(
                Task.project_id == cycle.project_id,
                Task.discovery_cycle_id == cycle.id,
                Task.payloads[0]["type"].as_string() == DISCOVERY_REVIEW_TASK_TYPE,
                Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
        )
    ).scalar() or 0
    attempts = max(int(prior_attempts), 1)
    if attempts < DISCOVERY_REVIEW_MAX_DECISION_ATTEMPTS:
        cycle.state = "review_pending"
        cycle.review_task_id = None
        cycle.review_result = {
            "status": "DECISION_RETRY",
            "error": reason[:2_000],
            "failed_review_task_id": str(task.id),
            "task_status": str(_enum_value(task.status)),
            "attempt": attempts,
            "max_attempts": DISCOVERY_REVIEW_MAX_DECISION_ATTEMPTS,
        }
        cycle.revision = int(cycle.revision or 0) + 1
        cycle.updated_at = _utcnow()
        session.add(cycle)
        await _emit_review_event(
            session,
            cycle=cycle,
            task_id=task.id,
            event_type="discovery.review_retry",
            summary=(
                f"Discovery decision task for {cycle.direction_key} ended "
                f"{_enum_value(task.status)!s} without a decision; respawning "
                f"(attempt {attempts}/{DISCOVERY_REVIEW_MAX_DECISION_ATTEMPTS})"
            ),
            extra={"reason": reason[:500], "policy_status": "DECISION_RETRY"},
        )
        return True
    cycle.state = "review_blocked"
    cycle.review_result = {
        "status": "DECISION_FAILED",
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
        summary=f"Discovery decision blocked for {cycle.direction_key}: {reason[:240]}",
        extra={"reason": reason[:500], "policy_status": "DECISION_FAILED"},
        wake_resident=True,
    )
    return True


async def _handle_discovery_review_completion(
    task: Task,
    logs: str,
    session,
) -> None:
    try:
        await complete_discovery_review(session, task=task, raw_result=logs)
    except DiscoveryReviewResultError:
        logger.warning(
            "[DISCOVERY_REVIEW] Decision task %s failed closed",
            task.id,
            exc_info=True,
        )


async def _sweep_discovery_cycle(session, cycle_id: uuid.UUID, *, now: datetime) -> bool:
    try:
        async with session.begin_nested():
            result = await maybe_close_and_spawn_discovery_review(
                session, cycle_id, now=now
            )
        changed = result.spawned or result.reason in {
            "authority_reconciled",
            "authority_superseded",
            "authority_proof_missing",
            "evidence_pending",
            "evidence_pending_escalated",
            "legacy_successor_needs_next_bet",
            "legacy_disposition_needs_resolution",
            "review_blocked",
        }
        await session.commit()
        return changed
    except Exception:
        await session.rollback()
        logger.exception("Discovery-decision sweep skipped malformed cycle %s", cycle_id)
        return False


async def sweep_discovery_reviews(
    session,
    now: datetime | None = None,
    *,
    limit: int = DISCOVERY_REVIEW_SWEEP_LIMIT,
) -> int:
    """Idempotent backstop for missed settled-parent decision triggers."""

    now = now or _utcnow()
    cycle_ids = list(
        (
            await session.execute(
                select(DiscoveryCycle.id)
                .where(
                    or_(
                        DiscoveryCycle.state.in_(_SWEEPABLE_CYCLE_STATES),
                        (
                            (DiscoveryCycle.state == "reviewed")
                            & (
                                DiscoveryCycle.verdict.in_(
                                    POLICY_BLOCKING_DISCOVERY_VERDICTS
                                )
                            )
                        ),
                    )
                )
                # Live-cycle convergence always wins the bounded batch.  Once
                # there is spare sweep capacity, policy-blocking reviewed cycles
                # are rechecked for true-authority supersession without an old
                # disposition starving current reviews.
                .order_by(
                    (DiscoveryCycle.state == "reviewed").asc(),
                    DiscoveryCycle.updated_at.asc(),
                    DiscoveryCycle.id.asc(),
                )
                .limit(max(1, min(limit, DISCOVERY_REVIEW_SWEEP_LIMIT)))
            )
        )
        .scalars()
        .all()
    )
    return sum(
        [
            await _sweep_discovery_cycle(session, cycle_id, now=now)
            for cycle_id in cycle_ids
        ]
    )
