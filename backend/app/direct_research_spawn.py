"""Direct Strategic research spawn contracts.

This leaf owns the research-specific payload envelope and the single-source
discovery-cycle admission fence used by ``/api/agent/strategic``. Keeping both
pieces together makes the task's advertised budget/scope and the authority to
occupy that budget one contract instead of two router-local implementations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlmodel import select

from app.capability_registry import (
    annotate_capability_proof_status,
    registry_capability_evidence_refs_map,
    registry_capability_vocabulary,
)
from app.models import DiscoveryCycle, Project, StrategicSpawnRequest, Task
from app.strategic_spawn_requests import (
    default_idempotency_key,
    stable_work_specification_hash,
    strategic_work_idempotency_key,
)


def enrich_direct_research_payload(
    spawn_payload: dict[str, Any],
    *,
    project: Project,
    direction_entry: Mapping[str, Any] | None,
    direction_id: object | None,
    direct_cycle: DiscoveryCycle | None,
    runtime_config: Mapping[str, Any],
) -> None:
    """Attach the canonical agent-facing envelope to a direct research root."""

    if spawn_payload.get("type") != "research":
        return

    from app.research_completion import effective_research_finding_headroom

    raw_headroom = spawn_payload.get("work_headroom")
    if (
        isinstance(raw_headroom, bool)
        or not isinstance(raw_headroom, (int, float))
        or not math.isfinite(raw_headroom)
        or raw_headroom < 0
    ):
        # Direction-free/manual roots also own the full fresh-work ceiling.
        # Keep an explicit valid bound: callers may intentionally commission a
        # narrower root, including an honest zero-headroom metadata pass.
        spawn_payload["work_headroom"] = effective_research_finding_headroom(0)

    if direction_entry is not None:
        from app.discovery_admission import direction_authority_snapshot

        spawn_payload["directions"] = [
            direction_authority_snapshot(dict(direction_entry))
        ]
        spawn_payload["current_direction_id"] = str(direction_id)
        if direct_cycle is not None:
            spawn_payload.update(
                {
                    "discovery_generation": direct_cycle.generation,
                    "discovery_phase": direct_cycle.phase,
                    "discovery_intent_snapshot": dict(
                        direct_cycle.intent_snapshot or {}
                    ),
                    "discovery_bet_snapshot": dict(direct_cycle.bet_snapshot or {}),
                }
            )

    # Capability vocabulary belongs to every research root, including bounded
    # direction-free work. It is a spawn-time snapshot of the strategist-owned
    # registry and durable proof ledger.
    available_capabilities = annotate_capability_proof_status(
        registry_capability_vocabulary(project.problem_model),
        getattr(project, "capability_proof_records", None),
        registry_capability_evidence_refs_map(project.problem_model),
    )
    if available_capabilities and not isinstance(
        spawn_payload.get("available_capabilities"), list
    ):
        spawn_payload["available_capabilities"] = available_capabilities

    suffixes = runtime_config.get("agent_prompt_suffixes")
    research_suffix = suffixes.get("research") if isinstance(suffixes, Mapping) else None
    existing_suffix = spawn_payload.get("prompt_suffix")
    if (
        isinstance(research_suffix, str)
        and research_suffix.strip()
        and not (isinstance(existing_suffix, str) and existing_suffix.strip())
    ):
        spawn_payload["prompt_suffix"] = research_suffix.strip()


async def enforce_direct_research_cycle_occupancy(
    session,
    *,
    project: Project,
    parent_task: Task,
    direct_cycle: DiscoveryCycle | None,
    direction_id: object | None,
    request_data: Mapping[str, Any],
    normalized_specification: Mapping[str, Any],
) -> None:
    """Reject a second full-headroom parent for an occupied discovery cycle.

    Exact lost-response replays remain accepted. All Task and request evidence
    is project-scoped defensively: a corrupt cross-project source/request edge
    cannot authorize, reserve, or block another project's research cycle.
    """

    if direct_cycle is None:
        return

    requested_work_hash = stable_work_specification_hash(normalized_specification)
    normalized_request_key = (
        str(request_data.get("idempotency_key") or "").strip()[:128]
        or default_idempotency_key(requested_work_hash)
    )
    requested_active_key = strategic_work_idempotency_key(
        project_id=project.id,
        direction_id=str(direction_id) if direction_id else None,
        request_key=normalized_request_key,
    )

    if direct_cycle.source_research_task_id is None:
        occupying_requests = (
            await session.execute(
                select(StrategicSpawnRequest).where(
                    StrategicSpawnRequest.project_id == project.id,
                    StrategicSpawnRequest.discovery_cycle_id == direct_cycle.id,
                    StrategicSpawnRequest.task_type.in_({"research", "web_research"}),
                    StrategicSpawnRequest.state.in_({"accepted", "deferred"}),
                )
            )
        ).scalars().all()
        conflicting_request = next(
            (
                request
                for request in occupying_requests
                if not (
                    request.parent_task_id == parent_task.id
                    and request.idempotency_key == normalized_request_key
                )
            ),
            None,
        )
        if conflicting_request is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"direction {direction_id} already has pending research "
                    f"request {conflicting_request.id} for discovery cycle "
                    f"{direct_cycle.id}; refusing to reserve a second parent"
                ),
            )
        return

    source_task = (
        await session.execute(
            select(Task).where(
                Task.id == direct_cycle.source_research_task_id,
                Task.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    source_matches_request = bool(
        source_task is not None
        and source_task.idempotency_key == requested_active_key
        and str(
            (source_task.context or {}).get("_strategic_work_specification_hash")
            or ""
        )
        == requested_work_hash
    )
    exact_request_replay = False
    if source_matches_request:
        exact_request_replay = (
            await session.execute(
                select(StrategicSpawnRequest.id).where(
                    StrategicSpawnRequest.project_id == project.id,
                    StrategicSpawnRequest.discovery_cycle_id == direct_cycle.id,
                    StrategicSpawnRequest.parent_task_id == parent_task.id,
                    StrategicSpawnRequest.idempotency_key == normalized_request_key,
                    StrategicSpawnRequest.task_id == source_task.id,
                )
            )
        ).scalar_one_or_none() is not None
    # Matching a task's active key/hash is not enough: those values are not a
    # caller receipt. Only the parent-bound StrategicSpawnRequest proves this
    # call is an exact replay of the request that created the cycle source.
    if not source_matches_request or not exact_request_replay:
        raise HTTPException(
            status_code=409,
            detail=(
                f"direction {direction_id} already has research source "
                f"{direct_cycle.source_research_task_id} in discovery cycle "
                f"{direct_cycle.id}; refusing to attach a second parent to the "
                "occupied cycle"
            ),
        )
