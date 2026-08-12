"""Canonical truthful-state-layer for the v12 CLI/UI read path.

Assembles a SystemStateSnapshot from existing helpers with NO new business
logic. The single entry-point is compute_system_state(), which eliminates
the four-way serving-status divergence by using aggregate-first serving truth.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeploymentState, Project, Task, TaskStatus

_STALE_HEARTBEAT_MINUTES = 5


@dataclass
class DeployStateSnapshot:
    status: str
    current_sha: str
    current_revision: str
    deployed_at: datetime | None
    pause_reason: str | None
    rollback_reason: str | None
    rollback_count: int
    last_known_good_sha: str
    halted: bool


@dataclass
class ArtifactServingSnapshot:
    artifact_kind: str
    required: bool
    serving_status: str | None
    served_identifier: str | None
    served_at: datetime | None
    reason_code: str | None


@dataclass
class ServingSnapshot:
    status: str | None
    sha: str | None
    confirmed_at: datetime | None
    failure_reason: str | None
    fleet_freshness: dict[str, Any] | None = None
    artifacts: list[ArtifactServingSnapshot] = field(default_factory=list)


@dataclass
class VersionSnapshot:
    version_number: int
    phase: str
    status: str
    opened_at: datetime | None
    phase_changed_at: datetime | None
    goal: str | None
    merged_task_count: int
    in_flight_task_count: int
    qualification_state: dict[str, Any] | None
    serving: ServingSnapshot
    qualification_provenance: dict[str, Any] = field(default_factory=dict)
    dimension_states: list[dict[str, Any]] = field(default_factory=list)
    qualification_attempts: int = 0
    qualification_retry_episode: int = 0
    active_qualification_attempt_id: str | None = None


@dataclass
class FleetSnapshot:
    active: int
    queued: int
    evaluating: int
    parked: int
    stale_heartbeat_count: int


@dataclass
class SystemStateSnapshot:
    deploy: DeployStateSnapshot
    version: VersionSnapshot | None
    fleet: FleetSnapshot
    generated_at: str  # ISO-UTC string

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict, converting datetimes to ISO strings."""
        return _serialize(dataclasses.asdict(self))


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        s = value.isoformat()
        return s if s.endswith("Z") else s + "Z"
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


async def compute_system_state(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    *,
    include_artifacts: bool = True,
) -> SystemStateSnapshot:
    """Assemble the canonical system state from existing helpers.

    When project_id is supplied, includes version + project-scoped fleet.
    When project_id is None, version is None and fleet is fleet-wide.
    Contains NO new business logic — only reads and assembles existing data.
    """
    from app.time_utils import _utcnow
    from app import release_version as rv
    from app.release_artifacts import version_serving_summary as _version_serving_summary
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_read_model,
    )

    now = _utcnow()

    # --- DeploymentState singleton ---
    deploy_state = await session.get(DeploymentState, 1)
    if deploy_state is None:
        deploy_snap = DeployStateSnapshot(
            status="unknown",
            current_sha="",
            current_revision="",
            deployed_at=None,
            pause_reason=None,
            rollback_reason=None,
            rollback_count=0,
            last_known_good_sha="",
            halted=False,
        )
    else:
        deploy_snap = DeployStateSnapshot(
            status=deploy_state.status,
            current_sha=deploy_state.current_sha,
            current_revision=deploy_state.current_revision,
            deployed_at=deploy_state.deployed_at,
            pause_reason=deploy_state.pause_reason,
            rollback_reason=deploy_state.rollback_reason,
            rollback_count=deploy_state.rollback_count,
            last_known_good_sha=deploy_state.last_known_good_sha,
            halted=deploy_state.halted_at is not None,
        )

    # --- Version block (only when project_id is given) ---
    version_snap: VersionSnapshot | None = None
    if project_id is not None:
        project = await session.get(Project, project_id)
        version = await rv.current_open_version(
            session, project_id, include_artifacts=include_artifacts
        )
        if version is not None:
            agg_dict, evidence_list = _version_serving_summary(version, project)
            aggregate_serving_status: str | None = agg_dict.get("serving_status")
            aggregate_reason: str | None = agg_dict.get("reason_code")

            artifact_snaps = [
                ArtifactServingSnapshot(
                    artifact_kind=e["artifact_kind"],
                    required=e["required"],
                    serving_status=e["serving_status"],
                    served_identifier=e["served_identifier"],
                    served_at=(
                        datetime.fromisoformat(e["served_at"])
                        if e.get("served_at")
                        else None
                    ),
                    reason_code=e["reason_code"],
                )
                for e in evidence_list
            ]

            confirmed_at: datetime | None = None
            for e in evidence_list:
                if e.get("serving_status") == "serving_confirmed" and e.get(
                    "served_at"
                ):
                    try:
                        confirmed_at = datetime.fromisoformat(e["served_at"])
                    except (ValueError, TypeError):
                        pass
                    break

            # Derive serving SHA from confirmed artifact evidence rather than
            # the legacy scalar (version.served_sha), which is only synced from
            # backend_revision and stays blank/stale for external projects.
            serving_sha: str | None = next(
                (
                    e["served_identifier"]
                    for e in evidence_list
                    if e.get("serving_status") == "serving_confirmed"
                    and e.get("served_identifier") is not None
                ),
                version.served_sha,
            )
            serving_snap = ServingSnapshot(
                status=aggregate_serving_status,
                sha=serving_sha,
                confirmed_at=confirmed_at,
                failure_reason=(
                    aggregate_reason
                    if aggregate_serving_status == "deploy_failed"
                    else None
                ),
                fleet_freshness=agg_dict.get("fleet_freshness"),
                artifacts=artifact_snaps,
            )

            # Pass the AGGREGATE serving status — never the raw scalar.
            # summarize_qualification_state uses aggregate_serving_status when
            # artifact_evidence is provided, so the read path stays consistent.
            # Routed through the shared OPEN-version resolver so this snapshot
            # applies the SAME three enrichments (serving aggregate + version-
            # scoped audit child + finalized verdict) as the strategic payload,
            # the agent fetch verb, and the admin lifecycle endpoint — otherwise
            # the snapshot alone told a ``qualifying`` story in the stale-pending
            # and audit-crashed windows while the others had resolved. The open
            # version is the only one that needs the full set here.
            qualification_read = await resolve_open_version_qualification_read_model(
                session,
                version,
                project,
                serving=(agg_dict, evidence_list),
            )

            merged_count = await rv.count_merged_since(
                session, project_id, version.opened_at
            )
            in_flight_count = await rv.count_in_flight_tasks(session, project_id)

            version_snap = VersionSnapshot(
                version_number=version.version_number,
                phase=version.phase,
                status=version.status,
                opened_at=version.opened_at,
                phase_changed_at=version.phase_changed_at,
                goal=version.goal,
                merged_task_count=merged_count,
                in_flight_task_count=in_flight_count,
                qualification_state=qualification_read.qualification_state,
                qualification_provenance=qualification_read.provenance,
                dimension_states=qualification_read.dimension_states,
                qualification_attempts=version.qualification_attempts,
                qualification_retry_episode=version.qualification_retry_episode,
                active_qualification_attempt_id=(
                    str(version.active_qualification_attempt_id)
                    if version.active_qualification_attempt_id
                    else None
                ),
                serving=serving_snap,
            )

    # --- Fleet counts ---
    stale_threshold = now - timedelta(minutes=_STALE_HEARTBEAT_MINUTES)

    def _count_stmt(*statuses: TaskStatus) -> Any:
        stmt = select(func.count()).select_from(Task).where(Task.status.in_(statuses))
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        return stmt

    active = int(
        (await session.execute(_count_stmt(TaskStatus.IN_PROGRESS))).scalar() or 0
    )
    queued = int(
        (
            await session.execute(
                _count_stmt(TaskStatus.QUEUED, TaskStatus.PROVISIONING)
            )
        ).scalar()
        or 0
    )
    evaluating = int(
        (await session.execute(_count_stmt(TaskStatus.EVALUATING))).scalar() or 0
    )
    parked = int((await session.execute(_count_stmt(TaskStatus.PARKED))).scalar() or 0)

    stale_stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.status == TaskStatus.IN_PROGRESS,
            Task.last_heartbeat < stale_threshold,
        )
    )
    if project_id is not None:
        stale_stmt = stale_stmt.where(Task.project_id == project_id)
    stale_heartbeat_count = int((await session.execute(stale_stmt)).scalar() or 0)

    fleet_snap = FleetSnapshot(
        active=active,
        queued=queued,
        evaluating=evaluating,
        parked=parked,
        stale_heartbeat_count=stale_heartbeat_count,
    )

    return SystemStateSnapshot(
        deploy=deploy_snap,
        version=version_snap,
        fleet=fleet_snap,
        generated_at=now.isoformat() + "Z",
    )
