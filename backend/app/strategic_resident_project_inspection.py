"""Bounded project pipeline and qualification evidence for Resident turns."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import resolve_async_engine
from app.models import Project, Task
from app.strategic_resident_agent_binding import ResidentAgentBinding
from app.strategic_resident_external_inspection import (
    ExternalInspectionPlan,
    execute_external_inspection,
)
from app.strategic_resident_task_inspection import (
    MAX_EVIDENCE_DATA_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    _bounded_evidence_data,
    _canonical_json,
)
from app.system_state import compute_system_state


ProjectInspectionKind = Literal["pipeline", "qualification"]


class ResidentProjectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProjectInspectionKind
    project_id: uuid.UUID
    source_id: str
    revision: str
    checksum: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    data: dict[str, Any]


class ResidentProjectNotFound(LookupError):
    pass


def _evidence(
    *,
    project_id: uuid.UUID,
    kind: ProjectInspectionKind,
    data: dict[str, Any],
    observed_at: datetime,
) -> ResidentProjectEvidence:
    def build(data_budget: int) -> tuple[ResidentProjectEvidence, bytes]:
        bounded, encoded = _bounded_evidence_data(data, budget=data_budget)
        checksum = hashlib.sha256(encoded).hexdigest()
        return (
            ResidentProjectEvidence(
                kind=kind,
                project_id=project_id,
                source_id=str(project_id),
                revision=f"{kind}:{checksum}",
                checksum=checksum,
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


async def _project_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    if await session.get(Project, project_id) is None:
        raise ResidentProjectNotFound("project not found")
    snapshot = await compute_system_state(
        session,
        project_id,
        include_artifacts=False,
    )
    state = snapshot.to_dict()
    state.pop("generated_at", None)
    return state


async def inspect_project_pipeline(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    observed_at: datetime | None = None,
) -> ResidentProjectEvidence:
    """Inspect canonical pipeline, deploy, version, and fleet state."""

    state = await _project_state(session, project_id=project_id)
    return _evidence(
        project_id=project_id,
        kind="pipeline",
        data={
            "deploy": state["deploy"],
            "version": state["version"],
            "fleet": state["fleet"],
        },
        observed_at=observed_at or datetime.now(timezone.utc),
    )


async def inspect_project_qualification(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    observed_at: datetime | None = None,
) -> ResidentProjectEvidence:
    """Inspect the exact open-version qualification and serving state."""

    state = await _project_state(session, project_id=project_id)
    version = state["version"]
    data: dict[str, Any] = {"open_version": None}
    if isinstance(version, dict):
        data["open_version"] = {
            key: version.get(key)
            for key in (
                "version_number",
                "phase",
                "status",
                "phase_changed_at",
                "in_flight_task_count",
                "qualification_state",
                "qualification_provenance",
                "dimension_states",
                "qualification_attempts",
                "qualification_retry_episode",
                "active_qualification_attempt_id",
                "serving",
            )
        }
    return _evidence(
        project_id=project_id,
        kind="qualification",
        data=data,
        observed_at=observed_at or datetime.now(timezone.utc),
    )


async def _prepare_project_inspection(
    session: AsyncSession,
    task: Task,
    binding: ResidentAgentBinding,
    parameters: Mapping[str, Any],
) -> ExternalInspectionPlan:
    del parameters
    exists = await session.scalar(
        select(Project.id)
        .where(Project.id == binding.project_id)
        .with_for_update(key_share=True)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project_id = str(task.project_id)
    return ExternalInspectionPlan(
        expected_revisions={"project_id": project_id},
        reservation={"project_id": project_id},
        external_scope={"project_id": project_id},
    )


async def invoke_project_inspection(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    capability_id: Literal["pipeline.inspect", "qualification.inspect"],
    inspector: Callable[..., Awaitable[ResidentProjectEvidence]],
) -> dict[str, Any]:
    """Run a DB-heavy read outside the live turn's authority lock."""

    session_factory = async_sessionmaker(
        resolve_async_engine(session),
        expire_on_commit=False,
        autoflush=False,
    )

    async def perform(plan: ExternalInspectionPlan) -> ResidentProjectEvidence:
        async with session_factory() as inspection_session:
            try:
                return await inspector(
                    inspection_session,
                    project_id=uuid.UUID(str(plan.external_scope["project_id"])),
                )
            except ResidentProjectNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    return await execute_external_inspection(
        session,
        binding=binding,
        capability_id=capability_id,
        parameters={},
        prepare_locked=_prepare_project_inspection,
        perform=perform,
        authority_change_detail="project scope changed during inspection",
    )


__all__ = [
    "ResidentProjectEvidence",
    "ResidentProjectNotFound",
    "inspect_project_pipeline",
    "inspect_project_qualification",
    "invoke_project_inspection",
]
