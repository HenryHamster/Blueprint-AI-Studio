"""Bounded operator health/readiness projection for Resident rollout.

The projection derives readiness only from durable project-scoped state. It
never wakes the Resident or calls a provider, so polling cannot change the
system being observed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Project,
    ProjectMessage,
    StrategicCycle,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_resident_failure_recovery import UNSETTLED_RECEIPT_STATUSES
from app.strategic_resident_legacy_projection import (
    LEGACY_CONTINUATION_KEY,
    LEGACY_SESSION_KEY,
)
from app.strategic_resident_ownership import assert_resident_pointer_lineage
from app.strategic_resident_transition_locks import (
    ACTIVE_LEGACY_CYCLE_OUTCOMES,
    LEGACY_CYCLE_TASK_TYPES,
    LEGACY_RESIDENT_SESSION_ID_CONTEXT_KEY,
    LEGACY_RESIDENT_TASK_CONTEXT_KEY,
    ResidentOwnershipStateError,
    TERMINAL_TASK_STATUSES,
)
from app.strategic_resident_turns import STRATEGIC_RESIDENT_TURN_TASK_TYPE
from app.time_utils import _utcnow


RESIDENT_SOAK_MINIMUM = timedelta(days=7)
RESIDENT_SHADOW_PROOF_MAX_AGE = timedelta(hours=24)
_MAX_MISMATCH_REASONS = 20
_MAX_REASON_CHARS = 160


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _naive_utc(value).replace(tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _naive_utc(parsed)


def _canonical_status(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _task_projection(task: Task | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "task_id": str(task.id),
        "status": _canonical_status(task.status),
        "updated_at": _iso(task.updated_at),
    }


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _bounded_range(value: Any) -> list[int] | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        return [int(value[0]), int(value[1])]
    return None


def _bounded_comparison(value: Any) -> tuple[dict[str, Any] | None, list[str], bool]:
    if not isinstance(value, Mapping):
        return None, [], False
    raw_reasons = value.get("mismatch_reasons")
    if not isinstance(raw_reasons, list):
        return None, [], False
    reasons = [str(item)[:_MAX_REASON_CHARS] for item in raw_reasons[:_MAX_MISMATCH_REASONS]]
    requested_range = _bounded_range(value.get("requested_message_range"))
    represented_range = _bounded_range(value.get("represented_message_range"))
    raw_requested_chips = value.get("requested_chip_count")
    raw_represented_chips = value.get("represented_chip_count")
    requested_chips = (
        int(raw_requested_chips)
        if isinstance(raw_requested_chips, int)
        and not isinstance(raw_requested_chips, bool)
        and raw_requested_chips >= 0
        else None
    )
    represented_chips = (
        int(raw_represented_chips)
        if isinstance(raw_represented_chips, int)
        and not isinstance(raw_represented_chips, bool)
        and raw_represented_chips >= 0
        else None
    )
    valid = (
        len(raw_reasons) <= _MAX_MISMATCH_REASONS
        and all(isinstance(item, str) for item in raw_reasons)
        and value.get("checkpoint_lineage_matches") is True
        and requested_range == represented_range
        and requested_chips is not None
        and represented_chips is not None
        and requested_chips == represented_chips
    )
    projection = {
        "checkpoint_lineage_matches": value.get("checkpoint_lineage_matches") is True,
        "requested_message_range": requested_range,
        "represented_message_range": represented_range,
        "requested_chip_count": requested_chips,
        "represented_chip_count": represented_chips,
        "shadow_scheduling_decision": (
            str(value.get("shadow_scheduling_decision"))[:80]
            if value.get("shadow_scheduling_decision") is not None
            else None
        ),
        "mismatch_reasons": reasons,
        "truncated": len(raw_reasons) > len(reasons),
    }
    return projection, reasons, valid


def _checkpoint_is_valid(
    resident: StrategicResident,
    checkpoint: StrategicResidentCheckpoint | None,
    latest: StrategicResidentCheckpoint | None,
) -> bool:
    if (
        checkpoint is None
        or latest is None
        or checkpoint.project_id != resident.project_id
        or resident.current_checkpoint_id != checkpoint.id
        or latest.id != checkpoint.id
        or int(latest.version) != int(checkpoint.version)
        or int(checkpoint.schema_version) < 1
        or int(checkpoint.authority_generation) > int(resident.authority_generation)
        or not isinstance(checkpoint.checkpoint, dict)
        or checkpoint.checkpoint.get("schema_version") != checkpoint.schema_version
    ):
        return False
    canonical = json.dumps(
        checkpoint.checkpoint,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hmac.compare_digest(
        hashlib.sha256(canonical).hexdigest(), str(checkpoint.content_hash)
    )


def _shadow_task_matches_checkpoint(
    task: Task | None,
    checkpoint: StrategicResidentCheckpoint | None,
) -> bool:
    if (
        task is None
        or checkpoint is None
        or task.status != TaskStatus.COMPLETED
        or task.project_id != checkpoint.project_id
    ):
        return False
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    if (
        payload.get("type") != STRATEGIC_RESIDENT_TURN_TASK_TYPE
        or payload.get("shadow") is not True
    ):
        return False
    output = task.plan_output if isinstance(task.plan_output, dict) else {}
    shadow = output.get("resident_shadow")
    manifest = shadow.get("manifest") if isinstance(shadow, Mapping) else None
    manifest_checkpoint = (
        manifest.get("checkpoint") if isinstance(manifest, Mapping) else None
    )
    if not isinstance(manifest_checkpoint, Mapping):
        return False
    return bool(
        _nonnegative_int(manifest_checkpoint.get("version"))
        == int(checkpoint.version)
        and str(manifest_checkpoint.get("content_hash") or "")
        == str(checkpoint.content_hash)
    )


async def build_resident_rollout_health(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded, side-effect-free rollout and soak readiness report."""

    observed_at = _naive_utc(now or _utcnow())
    resident = (
        await session.execute(
            select(StrategicResident).where(StrategicResident.project_id == project_id)
        )
    ).scalar_one_or_none()
    if resident is None:
        blockers = ["resident_not_initialized"]
        return {
            "project_id": str(project_id),
            "observed_at": _iso(observed_at),
            "resident_initialized": False,
            "cutover_ready": False,
            "rollback_ready": False,
            "legacy_retirement_ready": False,
            "blockers": blockers,
            "cutover_blockers": blockers,
            "rollback_blockers": blockers,
            "retirement_blockers": blockers,
        }

    project = await session.get(Project, project_id)
    checkpoint = (
        await session.get(StrategicResidentCheckpoint, resident.current_checkpoint_id)
        if resident.current_checkpoint_id is not None
        else None
    )
    latest_checkpoint = (
        (
            await session.execute(
                select(StrategicResidentCheckpoint)
                .where(StrategicResidentCheckpoint.project_id == project_id)
                .order_by(
                    StrategicResidentCheckpoint.version.desc(),
                    StrategicResidentCheckpoint.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    )
    checkpoint_valid = _checkpoint_is_valid(resident, checkpoint, latest_checkpoint)

    scheduling = (
        resident.scheduling_state if isinstance(resident.scheduling_state, dict) else {}
    )
    rollout = resident.rollout_state if isinstance(resident.rollout_state, dict) else {}
    shadow_task_id = _uuid_or_none(scheduling.get("shadow_last_completed_task_id"))
    pointer_ids = {
        pointer
        for pointer in (
            resident.active_turn_task_id,
            resident.interactive_successor_task_id,
            resident.background_successor_task_id,
            shadow_task_id,
        )
        if pointer is not None
    }
    pointer_rows = (
        (
            await session.execute(
                select(Task).where(Task.project_id == project_id, Task.id.in_(pointer_ids))
            )
        )
        .scalars()
        .all()
        if pointer_ids
        else []
    )
    tasks = {row.id: row for row in pointer_rows}
    pointer_lineage_valid = True
    if checkpoint_valid and checkpoint is not None:
        try:
            assert_resident_pointer_lineage(
                resident,
                list(tasks.values()),
                owner=_canonical_status(resident.engine_owner),
                checkpoint=checkpoint,
            )
        except (KeyError, TypeError, ValueError, ResidentOwnershipStateError):
            pointer_lineage_valid = False

    latest_user_sequence, latest_unhandled_user_sequence = (
        await session.execute(
            select(
                func.max(ProjectMessage.sequence),
                func.max(
                    case(
                        (
                            ProjectMessage.handling_state.in_(("pending", "claimed")),
                            ProjectMessage.sequence,
                        ),
                        else_=None,
                    )
                ),
            ).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.surface == "conversation",
                ProjectMessage.from_type == "user",
            )
        )
    ).one()
    unsettled_receipt_count = int(
        (
            await session.execute(
                select(func.count(StrategicOperationReceipt.id)).where(
                    StrategicOperationReceipt.project_id == project_id,
                    StrategicOperationReceipt.status.in_(
                        tuple(UNSETTLED_RECEIPT_STATUSES)
                    ),
                )
            )
        ).scalar_one()
        or 0
    )

    active_cycle_count = int(
        (
            await session.execute(
                select(func.count(StrategicCycle.id)).where(
                    StrategicCycle.project_id == project_id,
                    StrategicCycle.outcome.in_(ACTIVE_LEGACY_CYCLE_OUTCOMES),
                )
            )
        ).scalar_one()
        or 0
    )
    active_legacy_task_count = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.status.not_in(list(TERMINAL_TASK_STATUSES)),
                    or_(
                        Task.context[LEGACY_RESIDENT_TASK_CONTEXT_KEY]
                        .as_boolean()
                        .is_(True),
                        and_(
                            Task.payloads[0]["type"].as_string().in_(
                                LEGACY_CYCLE_TASK_TYPES
                            ),
                            Task.context["strategic_cycle_id"]
                            .as_string()
                            .is_not(None),
                        ),
                    ),
                )
            )
        ).scalar_one()
        or 0
    )

    comparison, mismatch_reasons, comparison_valid = _bounded_comparison(
        scheduling.get("shadow_last_comparison")
    )
    shadow_handled_sequence = _nonnegative_int(
        scheduling.get("shadow_handled_message_sequence")
    )
    shadow_completed_at = _parse_timestamp(scheduling.get("shadow_last_completed_at"))
    shadow_proof_fresh = bool(
        shadow_completed_at is not None
        and shadow_completed_at <= observed_at
        and observed_at - shadow_completed_at <= RESIDENT_SHADOW_PROOF_MAX_AGE
    )
    shadow_task_current = _shadow_task_matches_checkpoint(
        tasks.get(shadow_task_id), checkpoint
    )

    common_blockers: list[str] = []
    if not checkpoint_valid:
        common_blockers.append("checkpoint_integrity_invalid")
    if unsettled_receipt_count:
        common_blockers.append("unsettled_operation_receipts")
    if resident.health_status == "blocked":
        common_blockers.append("resident_health_blocked")
    if not pointer_lineage_valid:
        common_blockers.append("pointer_lineage_invalid")

    cutover_blockers = list(common_blockers)
    if resident.engine_owner != "legacy":
        cutover_blockers.append("already_cut_over")
    if scheduling.get("shadow_projection_state") != "healthy":
        cutover_blockers.append("shadow_projection_not_healthy")
    if not comparison_valid or not shadow_task_current:
        cutover_blockers.append("shadow_comparison_missing_or_stale")
    if not shadow_proof_fresh:
        cutover_blockers.append("shadow_proof_expired")
    if mismatch_reasons:
        cutover_blockers.append("shadow_comparison_mismatch")
    if latest_user_sequence is not None and shadow_handled_sequence < int(
        latest_user_sequence
    ):
        cutover_blockers.append("shadow_message_backlog")

    vision = (
        project.vision
        if project is not None and isinstance(project.vision, dict)
        else {}
    )
    legacy_session = vision.get(LEGACY_SESSION_KEY)
    legacy_session_integrity_valid = True
    if isinstance(legacy_session, Mapping) and legacy_session.get("task_id"):
        legacy_task_id = _uuid_or_none(legacy_session.get("task_id"))
        legacy_task = (
            await session.get(Task, legacy_task_id)
            if legacy_task_id is not None
            else None
        )
        legacy_context = (
            legacy_task.context
            if legacy_task is not None and isinstance(legacy_task.context, dict)
            else {}
        )
        expected_session_id = str(legacy_session.get("session_id") or "")
        legacy_session_integrity_valid = bool(
            legacy_task is not None
            and legacy_task.project_id == project_id
            and legacy_context.get(LEGACY_RESIDENT_TASK_CONTEXT_KEY) is True
            and (
                not expected_session_id
                or str(
                    legacy_context.get(LEGACY_RESIDENT_SESSION_ID_CONTEXT_KEY) or ""
                )
                == expected_session_id
            )
        )
    if vision.get(LEGACY_CONTINUATION_KEY) is not None:
        cutover_blockers.append("legacy_continuation_active")
    if isinstance(legacy_session, Mapping) and (
        legacy_session.get("status") != "ended"
        or legacy_session.get("stop_requested") is not True
    ):
        cutover_blockers.append("legacy_session_not_drained")
    if not legacy_session_integrity_valid:
        cutover_blockers.append("legacy_session_integrity_invalid")
    if active_cycle_count:
        cutover_blockers.append("legacy_cycle_active")
    if active_legacy_task_count:
        cutover_blockers.append("legacy_task_active")

    rollback_blockers = list(common_blockers)
    if resident.engine_owner != "strategic_resident":
        rollback_blockers.append("not_cut_over")

    cutover_at = _parse_timestamp(rollout.get("cutover_completed_at"))
    rollback_at = _parse_timestamp(rollout.get("rollback_completed_at"))
    soak_seconds = (
        max(0, int((observed_at - cutover_at).total_seconds()))
        if cutover_at is not None and cutover_at <= observed_at
        else 0
    )
    failed_turn_count = 0
    if cutover_at is not None:
        failed_turn_count = int(
            (
                await session.execute(
                    select(func.count(Task.id)).where(
                        Task.project_id == project_id,
                        Task.payloads[0]["type"].as_string()
                        == STRATEGIC_RESIDENT_TURN_TASK_TYPE,
                        Task.status == TaskStatus.FAILED,
                        Task.updated_at >= cutover_at,
                    )
                )
            ).scalar_one()
            or 0
        )

    retirement_blockers = list(common_blockers)
    if resident.engine_owner != "strategic_resident":
        retirement_blockers.append("strategic_resident_not_authoritative")
    if cutover_at is None or soak_seconds < int(RESIDENT_SOAK_MINIMUM.total_seconds()):
        retirement_blockers.append("seven_day_soak_incomplete")
    if rollback_at is None:
        retirement_blockers.append("rollback_rehearsal_missing_or_invalid")
    elif (
        cutover_at is None
        or rollback_at > cutover_at
        or rollback_at > observed_at
    ):
        retirement_blockers.append("rollback_rehearsal_not_bound_to_rollout")
    if resident.health_status != "healthy":
        retirement_blockers.append("resident_not_healthy")
    if (
        cutover_at is not None
        and resident.last_error_at is not None
        and _naive_utc(resident.last_error_at) >= cutover_at
    ):
        retirement_blockers.append("resident_error_during_soak")
    if failed_turn_count:
        retirement_blockers.append("failed_resident_turn_during_soak")
    if (
        cutover_at is not None
        and (
            resident.last_healthy_at is None
            or _naive_utc(resident.last_healthy_at) < cutover_at
        )
    ):
        retirement_blockers.append("healthy_observation_missing")

    cutover_blockers = sorted(set(cutover_blockers))
    rollback_blockers = sorted(set(rollback_blockers))
    retirement_blockers = sorted(set(retirement_blockers))
    return {
        "project_id": str(project_id),
        "observed_at": _iso(observed_at),
        "resident_initialized": True,
        "engine_owner": str(resident.engine_owner),
        "engine_version": resident.engine_version,
        "background_policy": str(resident.background_policy),
        "authority_generation": int(resident.authority_generation),
        "health_status": str(resident.health_status),
        "last_healthy_at": _iso(resident.last_healthy_at),
        "last_error_at": _iso(resident.last_error_at),
        "checkpoint": (
            {
                "checkpoint_id": str(checkpoint.id),
                "version": int(checkpoint.version),
                "content_hash": checkpoint.content_hash,
                "integrity_valid": True,
            }
            if checkpoint_valid and checkpoint is not None
            else {
                "checkpoint_id": (
                    str(resident.current_checkpoint_id)
                    if resident.current_checkpoint_id is not None
                    else None
                ),
                "integrity_valid": False,
            }
        ),
        "turns": {
            "active": _task_projection(tasks.get(resident.active_turn_task_id)),
            "interactive_successor": _task_projection(
                tasks.get(resident.interactive_successor_task_id)
            ),
            "background_successor": _task_projection(
                tasks.get(resident.background_successor_task_id)
            ),
        },
        "conversation": {
            "handled_sequence": int(resident.handled_message_sequence),
            "shadow_handled_sequence": shadow_handled_sequence,
            "latest_user_sequence": latest_user_sequence,
            "latest_unhandled_user_sequence": latest_unhandled_user_sequence,
        },
        "operations": {"unsettled_receipt_count": unsettled_receipt_count},
        "legacy_preflight": {
            "active_cycle_count": active_cycle_count,
            "active_task_count": active_legacy_task_count,
            "continuation_active": vision.get(LEGACY_CONTINUATION_KEY) is not None,
            "session_integrity_valid": legacy_session_integrity_valid,
            "session_drained": not isinstance(legacy_session, Mapping)
            or (
                legacy_session.get("status") == "ended"
                and legacy_session.get("stop_requested") is True
            ),
        },
        "shadow": {
            "state": str(scheduling.get("shadow_projection_state") or "")[:40] or None,
            "last_completed_at": _iso(shadow_completed_at),
            "proof_max_age_seconds": int(
                RESIDENT_SHADOW_PROOF_MAX_AGE.total_seconds()
            ),
            "proof_fresh": shadow_proof_fresh,
            "checkpoint_current": shadow_task_current,
            "last_projection_latency_ms": (
                max(0, int(scheduling["shadow_last_projection_latency_ms"]))
                if isinstance(
                    scheduling.get("shadow_last_projection_latency_ms"), int
                )
                and not isinstance(
                    scheduling.get("shadow_last_projection_latency_ms"), bool
                )
                else None
            ),
            "last_comparison": comparison,
        },
        "soak": {
            "cutover_completed_at": _iso(cutover_at),
            "observed_seconds": soak_seconds,
            "required_seconds": int(RESIDENT_SOAK_MINIMUM.total_seconds()),
            "rollback_rehearsed_at": _iso(rollback_at),
            "failed_turn_count": failed_turn_count,
            "error_free": not (
                cutover_at is not None
                and resident.last_error_at is not None
                and _naive_utc(resident.last_error_at) >= cutover_at
            )
            and failed_turn_count == 0,
        },
        "cutover_ready": not cutover_blockers,
        "rollback_ready": not rollback_blockers,
        "legacy_retirement_ready": not retirement_blockers,
        "blockers": cutover_blockers,
        "cutover_blockers": cutover_blockers,
        "rollback_blockers": rollback_blockers,
        "retirement_blockers": retirement_blockers,
    }


__all__ = [
    "RESIDENT_SHADOW_PROOF_MAX_AGE",
    "RESIDENT_SOAK_MINIMUM",
    "build_resident_rollout_health",
]
