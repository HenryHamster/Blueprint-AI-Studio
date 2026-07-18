"""Bounded, project-scoped runner evidence for Strategic Resident turns."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import LocalRunner, RunnerHeartbeatSnapshot
from app.runner_health_monitors import (
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
)
from app.runner_project_binding import runner_allowed_for_project
from app.schemas import redact_command_log_text
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    _bounded_evidence_data,
    _canonical_json,
)
from app.time_utils import _strip_tz_assume_utc, _utcnow


RunnerInspectionKind = Literal["runner", "runner_capacity"]
MAX_RUNNERS_PER_INSPECTION = 10
MAX_RUNNER_LABEL_CHARS = 255
MAX_RUNNER_LIST_DATA_BYTES = 4_300


class ResidentRunnerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RunnerInspectionKind
    project_id: uuid.UUID
    source_id: str
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    data: dict[str, Any]


class ResidentRunnerNotFound(LookupError):
    pass


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _safe_positive_int(value: Any) -> int | None:
    parsed = _safe_non_negative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _runner_projection(row: Any, *, observed_at: datetime) -> dict[str, Any]:
    max_concurrent = _safe_positive_int(row.max_concurrent)
    max_compute = _safe_positive_int(row.max_concurrent_compute)
    if max_compute is None:
        max_compute = max_concurrent
    snapshot_at = row.snapshot_captured_at
    active_agent_tasks = (
        _safe_non_negative_int(row.snapshot_active_task_count)
        if snapshot_at is not None
        else None
    )
    active_compute_jobs = (
        _safe_non_negative_int(row.snapshot_active_compute_job_count)
        if snapshot_at is not None
        else None
    )
    active_work = (
        active_agent_tasks + active_compute_jobs
        if active_agent_tasks is not None and active_compute_jobs is not None
        else None
    )
    last_seen = row.last_seen_at
    comparison_time = _strip_tz_assume_utc(observed_at)
    seconds_since_seen = (
        max(0, int((comparison_time - last_seen).total_seconds()))
        if last_seen is not None and comparison_time is not None
        else None
    )
    live = bool(
        row.status == "active"
        and seconds_since_seen is not None
        and seconds_since_seen < _RUNNER_LIVENESS_SECONDS
    )
    consecutive_failures = max(0, int(row.consecutive_job_failures or 0))
    return {
        "runner_id": str(row.id),
        "hostname": (
            redact_command_log_text(str(row.hostname))[:MAX_RUNNER_LABEL_CHARS]
            if row.hostname
            else None
        ),
        "version": (
            redact_command_log_text(str(row.version))[:64] if row.version else None
        ),
        "status": str(row.status),
        "is_live": live,
        "last_seen_at": last_seen.isoformat() if last_seen is not None else None,
        "last_successful_job_at": (
            row.last_successful_job_at.isoformat()
            if row.last_successful_job_at is not None
            else None
        ),
        "consecutive_job_failures": consecutive_failures,
        "degraded": live
        and consecutive_failures >= _RUNNER_DEGRADED_FAILURE_THRESHOLD,
        # Heartbeat counts are intentionally advisory. They expose no cross-project
        # task/job identifiers when a physical runner is shared between projects.
        "capacity": {
            "source": (
                "heartbeat_snapshot_advisory"
                if snapshot_at is not None
                else "unknown"
            ),
            "snapshot_captured_at": (
                snapshot_at.isoformat() if snapshot_at is not None else None
            ),
            "agent_available": (
                bool(row.snapshot_agent_available)
                if row.snapshot_agent_available is not None
                else None
            ),
            "max_concurrent": max_concurrent,
            "active_work_count": active_work,
            "available_slots": (
                max(0, max_concurrent - active_work)
                if max_concurrent is not None and active_work is not None
                else None
            ),
            "max_concurrent_compute": max_compute,
            "active_compute_job_count": active_compute_jobs,
            "available_compute_slots": (
                max(0, max_compute - active_compute_jobs)
                if max_compute is not None and active_compute_jobs is not None
                else None
            ),
        },
        "hardware": {
            "has_gpu": _safe_bool(row.has_gpu),
            "gpu_count": _safe_non_negative_int(row.gpu_count),
        },
    }


def _evidence(
    *,
    kind: RunnerInspectionKind,
    project_id: uuid.UUID,
    source_id: str,
    data: dict[str, Any],
    observed_at: datetime,
) -> ResidentRunnerEvidence:
    def build(data_budget: int) -> tuple[ResidentRunnerEvidence, bytes]:
        bounded, encoded = _bounded_evidence_data(data, budget=data_budget)
        digest = hashlib.sha256(encoded).hexdigest()
        return (
            ResidentRunnerEvidence(
                kind=kind,
                project_id=project_id,
                source_id=source_id,
                revision=f"{kind}:{source_id}:{digest}",
                checksum=digest,
                observed_at=observed_at,
                data=bounded,
            ),
            encoded,
        )

    evidence, encoded = build(MAX_EVIDENCE_DATA_BYTES)
    complete = _canonical_json(evidence.model_dump(mode="json"))
    if len(complete) > MAX_EVIDENCE_RESPONSE_BYTES:
        envelope = len(complete) - len(encoded)
        evidence, _ = build(max(0, MAX_EVIDENCE_RESPONSE_BYTES - envelope))
    return evidence


def _latest_snapshot_scalar(column: Any, label: str) -> Any:
    """One indexed correlated scalar from the runner's latest heartbeat row."""

    return (
        select(column)
        .where(RunnerHeartbeatSnapshot.runner_id == LocalRunner.id)
        .order_by(
            RunnerHeartbeatSnapshot.captured_at.desc(),
            RunnerHeartbeatSnapshot.id.desc(),
        )
        .limit(1)
        .correlate(LocalRunner)
        .scalar_subquery()
        .label(label)
    )


def _runner_columns() -> tuple[Any, ...]:
    """Project bounded scalars; never select token_hash or the full JSONB blob."""

    return (
        LocalRunner.id,
        LocalRunner.hostname,
        LocalRunner.version,
        LocalRunner.status,
        LocalRunner.capabilities["max_concurrent"].as_string().label(
            "max_concurrent"
        ),
        LocalRunner.capabilities["max_concurrent_compute"].as_string().label(
            "max_concurrent_compute"
        ),
        LocalRunner.capabilities["has_gpu"].as_string().label("has_gpu"),
        LocalRunner.capabilities["gpu_count"].as_string().label("gpu_count"),
        LocalRunner.last_seen_at,
        LocalRunner.last_successful_job_at,
        LocalRunner.consecutive_job_failures,
        LocalRunner.created_at,
        _latest_snapshot_scalar(
            RunnerHeartbeatSnapshot.captured_at, "snapshot_captured_at"
        ),
        _latest_snapshot_scalar(
            RunnerHeartbeatSnapshot.active_task_count,
            "snapshot_active_task_count",
        ),
        _latest_snapshot_scalar(
            RunnerHeartbeatSnapshot.active_compute_job_count,
            "snapshot_active_compute_job_count",
        ),
        _latest_snapshot_scalar(
            RunnerHeartbeatSnapshot.agent_available, "snapshot_agent_available"
        ),
    )


async def inspect_project_runner(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    runner_id: str,
    observed_at: datetime | None = None,
) -> ResidentRunnerEvidence:
    """Inspect one exact non-deleted runner on the project's allow-list."""

    try:
        parsed_runner_id = uuid.UUID(runner_id)
    except ValueError:
        raise ResidentRunnerNotFound("runner not found in project") from None
    row = (
        await session.execute(
            select(*_runner_columns()).where(
                LocalRunner.id == parsed_runner_id,
                runner_allowed_for_project(project_id),
                LocalRunner.status != "deleted",
            )
        )
    ).one_or_none()
    if row is None:
        raise ResidentRunnerNotFound("runner not found in project")
    now = observed_at or _utcnow()
    data = _runner_projection(row, observed_at=now)
    return _evidence(
        kind="runner",
        project_id=project_id,
        source_id=str(row.id),
        data=data,
        observed_at=now,
    )


async def inspect_project_runners(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    observed_at: datetime | None = None,
) -> ResidentRunnerEvidence:
    """Return a stable bounded runner-capacity page for one project."""

    rows = (
        await session.execute(
            select(*_runner_columns())
            .where(
                runner_allowed_for_project(project_id),
                LocalRunner.status != "deleted",
            )
            .order_by(LocalRunner.created_at.desc(), LocalRunner.id.asc())
            .limit(MAX_RUNNERS_PER_INSPECTION + 1)
        )
    ).all()
    now = observed_at or _utcnow()
    projected = [
        _runner_projection(row, observed_at=now)
        for row in rows[:MAX_RUNNERS_PER_INSPECTION]
    ]
    runners: list[dict[str, Any]] = []
    for item in projected:
        candidate = {
            "runners": [*runners, item],
            "returned_count": len(runners) + 1,
            "truncated": True,
            "capacity_is_advisory": True,
        }
        if len(_canonical_json(candidate)) > MAX_RUNNER_LIST_DATA_BYTES:
            break
        runners.append(item)
    data = {
        "runners": runners,
        "returned_count": len(runners),
        "truncated": len(rows) > len(runners),
        "capacity_is_advisory": True,
    }
    return _evidence(
        kind="runner_capacity",
        project_id=project_id,
        source_id=f"project:{project_id}:runners",
        data=data,
        observed_at=now,
    )


__all__ = [
    "MAX_RUNNERS_PER_INSPECTION",
    "ResidentRunnerEvidence",
    "ResidentRunnerNotFound",
    "inspect_project_runner",
    "inspect_project_runners",
]
