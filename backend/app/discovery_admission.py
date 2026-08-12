"""Discovery-cycle bootstrap and provenance lineage.

``discovery_cycle_id`` groups a research parent and its descendants into one
evidence cohort. It is deliberately not execution authority: once work is
created, ordinary scheduler, retry, review, spend, and approval policy governs
whether it runs.
"""

from __future__ import annotations

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


class DiscoveryCycleUnavailable(RuntimeError):
    """A direction cannot open another root research task yet."""


def direction_authority_snapshot(direction: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical direction fields that authorize discovery work."""

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
    if latest is not None and latest.verdict in {"park", "stop", "ask_operator"}:
        raise DiscoveryCycleUnavailable(
            f"direction {key} is awaiting policy resolution after {latest.verdict}"
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
