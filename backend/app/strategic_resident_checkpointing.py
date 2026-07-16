"""Canonical creation and initial checkpointing for a project's Resident."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Project,
    StrategicResident,
    StrategicResidentCheckpoint,
)


CHECKPOINT_NAMESPACE = uuid.UUID("33d6659c-727d-4dc8-b04c-fd30cc753ad1")
CHECKPOINT_MAX_BYTES = 128_000
_MAX_DEPTH = 10
_MAX_STRING_CHARS = 4_000
_MAX_DICT_ITEMS = 200
_MAX_LIST_ITEMS = 100
_MAX_NODES = 5_000
_LEGACY_MAX_DEPTH = 6
_LEGACY_SUMMARY_MAX_BYTES = 10_000
_LEGACY_DECISIONS_MAX_BYTES = 32_000
_LEGACY_CURSOR_MAX_CHARS = 2_000
_LEGACY_MAX_DICT_ITEMS = 50
_LEGACY_MAX_LIST_ITEMS = 50
_LEGACY_MAX_NODES = 1_000
_DIRECTION_FIELDS = (
    "id",
    "text",
    "status",
    "priority",
    "rationale",
    "recommended_mode",
    "gap_id",
    "success_evidence",
    "retirement_condition",
    "staged_for_version",
    "superseded_by",
)


def _bounded(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    if budget is None:
        budget = [_MAX_NODES]
    if budget[0] <= 0 or depth >= _MAX_DEPTH:
        return None
    budget[0] -= 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[:_MAX_DICT_ITEMS]:
            key = str(raw_key)
            if key.startswith("_"):
                continue
            result[key] = _bounded(
                value[raw_key],
                depth=depth + 1,
                budget=budget,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded(item, depth=depth + 1, budget=budget)
            for item in list(value)[:_MAX_LIST_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _directions(raw: Any, *, budget: list[int]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for direction in raw:
        if not isinstance(direction, Mapping):
            continue
        if str(direction.get("status") or "active") not in {
            "active",
            "paused",
            "proposed",
            "staged",
        }:
            continue
        item = {
            field: _bounded(direction[field], budget=budget)
            for field in _DIRECTION_FIELDS
            if field in direction
        }
        context = direction.get("context")
        if isinstance(context, Mapping):
            bounded_context = {
                key: _bounded(context[key], budget=budget)
                for key in (
                    "success_evidence",
                    "retirement_condition",
                    "recommended_mode",
                    "gap_id",
                )
                if key in context
            }
            if bounded_context:
                item["context"] = bounded_context
        result.append(item)
        if len(result) >= _MAX_LIST_ITEMS:
            break
    return result


def _memory(raw: Any, *, budget: list[int]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in raw[-100:]:
        if budget[0] <= 0:
            break
        if not isinstance(entry, Mapping):
            continue
        note = entry.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        budget[0] -= 1
        item = {"note": note.strip()[:_MAX_STRING_CHARS]}
        if entry.get("set_at") is not None:
            item["set_at"] = str(entry["set_at"])[:128]
        result.append(item)
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _bounded_legacy_value(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    """Bound selected legacy strategy state without retaining session history."""

    if budget is None:
        budget = [_LEGACY_MAX_NODES]
    if budget[0] <= 0 or depth >= _LEGACY_MAX_DEPTH:
        return None
    budget[0] -= 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[
            :_LEGACY_MAX_DICT_ITEMS
        ]:
            key = str(raw_key)
            if key.startswith("_"):
                continue
            result[key] = _bounded_legacy_value(
                value[raw_key],
                depth=depth + 1,
                budget=budget,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_legacy_value(item, depth=depth + 1, budget=budget)
            for item in list(value)[:_LEGACY_MAX_LIST_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_LEGACY_DECISIONS_MAX_BYTES]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_LEGACY_DECISIONS_MAX_BYTES]


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _legacy_strategic_projection(
    vision: Any,
) -> tuple[dict[str, Any], str | None]:
    """Project only durable strategic legacy fields from the session blob."""

    if not isinstance(vision, Mapping):
        return {}, None
    blob = vision.get("_resident_strategic_session")
    if not isinstance(blob, Mapping):
        return {}, None

    strategy: dict[str, Any] = {}
    summary = blob.get("_session_summary")
    if isinstance(summary, str) and summary:
        strategy["legacy_session_summary"] = _truncate_utf8(
            summary,
            _LEGACY_SUMMARY_MAX_BYTES,
        )

    decisions = blob.get("_decisions_reg")
    if isinstance(decisions, str):
        if len(decisions.encode("utf-8")) > _LEGACY_DECISIONS_MAX_BYTES:
            decisions = None
            strategy["legacy_decisions_register_malformed"] = True
        else:
            try:
                decisions = json.loads(decisions)
            except (TypeError, ValueError):
                decisions = None
                strategy["legacy_decisions_register_malformed"] = True
    if isinstance(decisions, Mapping):
        decisions = dict(decisions)
        for key in ("decisions", "entries"):
            if isinstance(decisions.get(key), list):
                decisions[key] = decisions[key][:_LEGACY_MAX_LIST_ITEMS]
        projected = _bounded_legacy_value(decisions)
        if len(_canonical(projected)) <= _LEGACY_DECISIONS_MAX_BYTES:
            strategy["legacy_decisions_register"] = projected
        else:
            strategy["legacy_decisions_register_malformed"] = True
    elif isinstance(decisions, list):
        projected = _bounded_legacy_value(decisions[:_LEGACY_MAX_LIST_ITEMS])
        if len(_canonical(projected)) <= _LEGACY_DECISIONS_MAX_BYTES:
            strategy["legacy_decisions_register"] = projected
        else:
            strategy["legacy_decisions_register_malformed"] = True

    cursor = blob.get("_message_cursor")
    legacy_cursor = (
        str(cursor)[:_LEGACY_CURSOR_MAX_CHARS] if cursor is not None else None
    )
    return strategy, legacy_cursor


def _small_mapping(raw: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    budget = [_MAX_NODES]
    return {
        key: _bounded(raw[key], budget=budget)
        for key in keys
        if key in raw and not key.startswith("_")
    }


def build_initial_strategic_checkpoint(
    project: Project | Mapping[str, Any],
    *,
    source: str = "runtime_initialization",
) -> tuple[dict[str, Any], str]:
    """Build the bounded v1 checkpoint shared by every new-project path."""

    def value(key: str, default: Any) -> Any:
        if isinstance(project, Mapping):
            return project.get(key, default)
        return getattr(project, key, default)

    problem_model = value("problem_model", {}) or {}
    vision = value("vision", {}) or {}
    directions = value("directions", []) or []
    memory = value("strategic_memory_notes", []) or []
    legacy_strategy, legacy_message_cursor = _legacy_strategic_projection(vision)
    budget = [_MAX_NODES]
    body: dict[str, Any] = {
        "schema_version": 1,
        "charter": _bounded(problem_model, budget=budget),
        "working_strategy": {
            "vision": _bounded(vision, budget=budget),
            "running_update": str(value("running_update", "") or "")[
                :_MAX_STRING_CHARS
            ],
            "directions": _directions(directions, budget=budget),
            "strategic_memory": _memory(memory, budget=budget),
            **legacy_strategy,
        },
        "cursors": {
            "handled_message_sequence": 0,
            "project_change": {},
            **(
                {"legacy_message_cursor": legacy_message_cursor}
                if legacy_message_cursor is not None
                else {}
            ),
        },
        "migration": {
            "source": str(source)[:64],
            "truncated": budget[0] <= 0,
        },
    }
    encoded = _canonical(body)
    if len(encoded) > CHECKPOINT_MAX_BYTES:
        body["charter"] = {
            "truncated": True,
            **_small_mapping(
                problem_model,
                (
                    "revision",
                    "purpose",
                    "target_user",
                    "problem",
                    "problems",
                    "goals",
                    "constraints",
                ),
            ),
        }
        strategy = body["working_strategy"]
        strategy["vision"] = _small_mapping(
            vision,
            ("revision", "purpose", "systems", "priorities", "context"),
        )
        strategy["directions"] = strategy["directions"][:25]
        strategy["strategic_memory"] = strategy["strategic_memory"][-20:]
        body["migration"]["truncated"] = True
        encoded = _canonical(body)
    if len(encoded) > CHECKPOINT_MAX_BYTES:
        strategy = body["working_strategy"]
        strategy["vision"] = _small_mapping(vision, ("revision", "purpose"))
        strategy["directions"] = [
            {
                key: value[:1_000] if isinstance(value, str) else value
                for key, value in list(direction.items())[:8]
            }
            for direction in strategy["directions"][:10]
        ]
        strategy["strategic_memory"] = [
            {
                "note": str(entry.get("note") or "")[:1_000],
                **(
                    {"set_at": str(entry.get("set_at"))[:128]}
                    if entry.get("set_at")
                    else {}
                ),
            }
            for entry in strategy["strategic_memory"][-10:]
        ]
        encoded = _canonical(body)
    if len(encoded) > CHECKPOINT_MAX_BYTES:
        raise ValueError("minimal strategic checkpoint exceeds maximum size")
    return body, hashlib.sha256(encoded).hexdigest()


async def initialize_strategic_resident(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    locked_project: Project | None = None,
) -> StrategicResident:
    """Idempotently create one Resident and its deterministic v1 checkpoint.

    The caller must either pass a newly-created/already-locked Project or let
    this helper acquire the canonical Project row lock. No commit is performed.
    """

    project = locked_project
    if project is None:
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
    if project is None or project.id != project_id:
        raise ValueError("Resident project does not exist or lock identity mismatched")

    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if resident is None:
        resident = StrategicResident(
            project_id=project_id,
            background_policy="paused" if project.paused else "active",
        )
        session.add(resident)
        await session.flush()

    checkpoint = (
        await session.execute(
            select(StrategicResidentCheckpoint)
            .where(
                StrategicResidentCheckpoint.project_id == project_id,
                StrategicResidentCheckpoint.version == 1,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        body, content_hash = build_initial_strategic_checkpoint(project)
        checkpoint = StrategicResidentCheckpoint(
            id=uuid.uuid5(CHECKPOINT_NAMESPACE, f"{project_id}:1"),
            project_id=project_id,
            version=1,
            schema_version=1,
            content_hash=content_hash,
            checkpoint=body,
            authority_generation=resident.authority_generation,
        )
        session.add(checkpoint)
        await session.flush()
    if resident.current_checkpoint_id is None:
        resident.current_checkpoint_id = checkpoint.id
        session.add(resident)
    return resident


__all__ = [
    "CHECKPOINT_MAX_BYTES",
    "CHECKPOINT_NAMESPACE",
    "build_initial_strategic_checkpoint",
    "initialize_strategic_resident",
]
