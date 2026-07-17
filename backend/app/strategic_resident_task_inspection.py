"""Exact, bounded project-scoped Task evidence for Resident capabilities."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.models import Task
from app.schemas import redact_command_log_text


TaskInspectionKind = Literal["task", "lineage", "logs", "failure", "provider"]
# Per-response evidence caps. One inspection response must fit both its projected
# ``data`` payload and its complete serialized envelope. The router additionally
# caps a turn to ``MAX_CAPABILITY_REQUESTS_PER_TURN`` (8) distinct inspections, so
# the evidence a single turn can accrue stays within ``MAX_TURN_EVIDENCE_BYTES``
# (8 * 5_500 = 44_000 <= 48_000) without threading a running total through the
# request path.
MAX_EVIDENCE_DATA_BYTES = 4_800
MAX_EVIDENCE_RESPONSE_BYTES = 5_500
MAX_TURN_EVIDENCE_BYTES = 48_000
# Field-level projection budgets. These bound individual fields but are NOT
# individually guaranteed to sum under MAX_EVIDENCE_DATA_BYTES, so a task with
# several large fields still collapses to a checksummed preview via
# ``_bounded_evidence_data`` (the hard backstop) rather than exceeding the cap.
MAX_STATUS_LOG_BYTES = 4_000
MAX_LOG_ENTRY_BYTES = 4_000
MAX_TEXT_CHARS = 4_000
MAX_TASK_LABEL_CHARS = 512
_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "client_secret",
    "credential",
    "api_key",
    "password",
    "private_key",
    "refresh_token",
    "access_token",
    "id_token",
    "secret",
    "token",
    "cookie",
)


class ResidentTaskEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TaskInspectionKind
    project_id: uuid.UUID
    source_id: str
    revision: str
    checksum: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    data: dict[str, Any]


class ResidentTaskNotFound(LookupError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _sensitive_key(value: Any) -> bool:
    normalized = "".join(character for character in str(value).casefold() if character.isalnum())
    return any(
        "".join(character for character in marker if character.isalnum()) in normalized
        for marker in _SENSITIVE_KEY_MARKERS
    )


def _bounded_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    remaining = budget if budget is not None else [500]
    if remaining[0] <= 0 or depth >= 6:
        return "[truncated]"
    remaining[0] -= 1
    if isinstance(value, str):
        return redact_command_log_text(value)[:MAX_TEXT_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value, key=str)[:50]:
            bounded_key = redact_command_log_text(str(key))[:160]
            result[bounded_key] = (
                "[redacted]"
                if _sensitive_key(key)
                else _bounded_value(value[key], depth=depth + 1, budget=remaining)
            )
            if remaining[0] <= 0:
                break
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_value(item, depth=depth + 1, budget=remaining)
            for item in value[:50]
            if remaining[0] > 0
        ]
    return str(_jsonable(value))[:MAX_TEXT_CHARS]


def _bounded_label(value: Any) -> Any:
    """Bound scalar task metadata that is repeated across lineage entries."""

    bounded = _bounded_value(value)
    if isinstance(bounded, str):
        return bounded[:MAX_TASK_LABEL_CHARS]
    return bounded


def _bounded_log_entry(value: Any) -> Any:
    bounded = _bounded_value(value)
    encoded = json.dumps(
        bounded, sort_keys=True, separators=(",", ":"), default=_jsonable
    ).encode()
    if len(encoded) <= MAX_LOG_ENTRY_BYTES:
        return bounded
    return {
        "truncated": True,
        "content_checksum": hashlib.sha256(encoded).hexdigest(),
        "preview": encoded[: MAX_LOG_ENTRY_BYTES - 256].decode(
            "utf-8", errors="ignore"
        ),
    }


def _bounded_status_log(values: Any, *, limit: int) -> tuple[list[Any], bool]:
    source_values = values if isinstance(values, list) else []
    source = source_values[-limit:]
    kept_reversed: list[Any] = []
    used = 2
    for raw in reversed(source):
        item = _bounded_log_entry(raw)
        item_size = len(json.dumps(item, default=_jsonable).encode()) + 1
        if kept_reversed and used + item_size > MAX_STATUS_LOG_BYTES:
            break
        kept_reversed.append(item)
        used += item_size
    kept_reversed.reverse()
    return kept_reversed, (
        len(source_values) > len(source) or len(kept_reversed) < len(source)
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_jsonable,
    ).encode("utf-8")


def _bounded_evidence_data(
    data: dict[str, Any],
    *,
    budget: int = MAX_EVIDENCE_DATA_BYTES,
) -> tuple[dict[str, Any], bytes]:
    """Hard-cap one evidence payload after all field-level projection."""

    encoded = _canonical_json(data)
    if len(encoded) <= budget:
        return data, encoded

    original_checksum = hashlib.sha256(encoded).hexdigest()
    preserved = {
        key: data[key]
        for key in ("task_id", "requested_task_id")
        if isinstance(data.get(key), str)
    }
    preview_source = encoded.decode("utf-8")

    def candidate(preview_chars: int) -> dict[str, Any]:
        return {
            **preserved,
            "evidence_truncated": True,
            "content_checksum": original_checksum,
            "preview": preview_source[:preview_chars],
        }

    best = candidate(0)
    if len(_canonical_json(best)) > budget:
        # Degenerate floor: even the empty-preview marker exceeds the budget. Drop
        # the preserved identity keys so the hard cap always holds unconditionally.
        best = {"evidence_truncated": True, "content_checksum": original_checksum}
        return best, _canonical_json(best)

    low = 0
    high = len(preview_source)
    while low <= high:
        midpoint = (low + high) // 2
        proposed = candidate(midpoint)
        proposed_encoded = _canonical_json(proposed)
        if len(proposed_encoded) <= budget:
            best = proposed
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best, _canonical_json(best)


def _bounded_lineage_tasks(
    tasks: list[Task],
    *,
    root_id: uuid.UUID,
    budget: int,
) -> tuple[list[dict[str, Any]], list[Task], bool]:
    """Project lineage members under a byte budget, always keeping the root.

    The requested root's entry is reserved first — wherever it sits in the
    ancestor -> root -> descendant order — so a large lineage that overflows the
    byte budget never truncates away the exact task the Resident asked about.
    Remaining members fill the budget in lineage order; once it is exhausted the
    tail is dropped and ``byte_truncated`` is set. Returns the kept entries and
    the corresponding kept tasks (both in lineage order) plus the truncation flag.
    """

    projected = [(task, _task_data(task, "lineage", log_limit=1)) for task in tasks]
    root_reserved = 0
    for task, entry in projected:
        if task.id == root_id:
            root_reserved = len(_canonical_json(entry)) + 1
            break

    used = 2  # surrounding ``[]``
    non_root_budget = budget - root_reserved
    byte_truncated = False
    exhausted = False
    keep_flags: list[bool] = []
    for task, entry in projected:
        if task.id == root_id:
            keep_flags.append(True)
            continue
        size = len(_canonical_json(entry)) + 1
        if exhausted or used + size > non_root_budget:
            byte_truncated = True
            exhausted = True
            keep_flags.append(False)
            continue
        used += size
        keep_flags.append(True)

    kept_entries = [
        entry for (_, entry), keep in zip(projected, keep_flags, strict=True) if keep
    ]
    kept_tasks = [
        task for (task, _), keep in zip(projected, keep_flags, strict=True) if keep
    ]
    return kept_entries, kept_tasks, byte_truncated


def _task_data(task: Task, kind: TaskInspectionKind, *, log_limit: int) -> dict[str, Any]:
    payload = task.first_payload
    common = {
        "task_id": str(task.id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "title": _bounded_label(task.title),
        "type": _bounded_label(payload.get("type")),
        "status": _jsonable(task.status),
        "version": int(task.version),
        "priority": int(task.priority),
        "attempt_count": int(task.attempt_count),
        "created_at": _jsonable(task.created_at),
        "updated_at": _jsonable(task.updated_at),
    }
    if kind == "task":
        depends_on = [str(item)[:160] for item in list(task.depends_on or [])[:50]]
        return {
            **common,
            "depends_on": depends_on,
            "depends_on_truncated": len(task.depends_on or []) > len(depends_on),
            "requires_approval": bool(task.requires_approval),
            "branch": _bounded_label(
                payload.get("branch") or (task.context or {}).get("branch")
            ),
            "pr_number": _bounded_label(
                payload.get("pr_number") or (task.context or {}).get("pr_number")
            ),
            "latest_status_message": _bounded_value(task.latest_status_message),
        }
    if kind == "logs":
        status_log, log_truncated = _bounded_status_log(
            task.status_log, limit=log_limit
        )
        return {
            **common,
            "latest_status_message": _bounded_value(task.latest_status_message),
            "status_log": status_log,
            "status_log_truncated": log_truncated,
        }
    if kind == "failure":
        context = task.context or {}
        return {
            **common,
            "latest_status_message": _bounded_value(task.latest_status_message),
            "blocker_kind": _bounded_label(task.blocker_kind),
            "blocked_reason": _bounded_value(task.blocked_reason),
            "park_reason": _bounded_value(task.park_reason),
            "error": _bounded_value(context.get("error") or context.get("failure_reason")),
            "error_fingerprint": _bounded_value(context.get("error_fingerprint") or task.previous_error_fingerprint),
            "failure_category": _bounded_value(context.get("failure_category")),
        }
    if kind == "provider":
        context = task.context or {}
        return {
            **common,
            "provider": _bounded_label(task.provider or context.get("llm_provider")),
            "model": _bounded_label(task.model or context.get("llm_model")),
            "account_id": _bounded_label(task.account_id or task.llm_account_id),
            "vm_instance_id": _bounded_label(task.vm_instance_id),
            "vm_machine_type": _bounded_label(task.vm_machine_type),
            "last_heartbeat": _jsonable(task.last_heartbeat),
        }
    return common


def _evidence(
    task: Task,
    kind: TaskInspectionKind,
    data: dict[str, Any],
    *,
    observed_at: datetime,
    revision: str | None = None,
) -> ResidentTaskEvidence:
    resolved_revision = (
        revision
        if revision is not None
        else f"task:{task.id}:v{int(task.version)}:{_jsonable(task.updated_at)}"
    )

    def build(data_budget: int) -> tuple[ResidentTaskEvidence, bytes]:
        bounded, encoded = _bounded_evidence_data(data, budget=data_budget)
        return (
            ResidentTaskEvidence(
                kind=kind,
                project_id=task.project_id,
                source_id=str(task.id),
                revision=resolved_revision,
                checksum=hashlib.sha256(encoded).hexdigest(),
                observed_at=observed_at,
                data=bounded,
            ),
            encoded,
        )

    evidence, encoded = build(MAX_EVIDENCE_DATA_BYTES)
    complete = _canonical_json(evidence.model_dump(mode="json"))
    if len(complete) > MAX_EVIDENCE_RESPONSE_BYTES:
        # The fixed envelope (kind/ids/revision/64-hex checksum/timestamp) does not
        # depend on the ``data`` content, so one re-bound against the response
        # budget minus that envelope guarantees the complete response fits.
        envelope = len(complete) - len(encoded)
        evidence, _ = build(max(0, MAX_EVIDENCE_RESPONSE_BYTES - envelope))
    return evidence


async def inspect_project_task(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    kind: Literal["task", "logs", "failure", "provider"] = "task",
    log_limit: int = 50,
    observed_at: datetime | None = None,
) -> ResidentTaskEvidence:
    """Inspect one exact task; the project predicate is the tenant boundary."""

    if not 1 <= log_limit <= 90:
        raise ValueError("log_limit must be between 1 and 90")
    task = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.project_id == project_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise ResidentTaskNotFound("task not found in project")
    now = observed_at or datetime.now(timezone.utc)
    return _evidence(task, kind, _task_data(task, kind, log_limit=log_limit), observed_at=now)


async def inspect_project_task_lineage(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    max_tasks: int = 50,
    observed_at: datetime | None = None,
) -> ResidentTaskEvidence:
    """Return ancestors plus descendants in stable order, bounded to one project."""

    if not 1 <= max_tasks <= 100:
        raise ValueError("max_tasks must be between 1 and 100")
    root = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.project_id == project_id)
        )
    ).scalar_one_or_none()
    if root is None:
        raise ResidentTaskNotFound("task not found in project")
    ancestor_task = aliased(Task)
    ancestors = select(
        Task.id.label("id"), Task.parent_task_id.label("parent_id"), literal(0).label("depth")
    ).where(Task.id == task_id, Task.project_id == project_id).cte(
        "resident_task_ancestors", recursive=True
    )
    ancestors = ancestors.union_all(
        select(
            ancestor_task.id,
            ancestor_task.parent_task_id,
            (ancestors.c.depth + 1).label("depth"),
        ).where(
            ancestor_task.id == ancestors.c.parent_id,
            ancestor_task.project_id == project_id,
            ancestors.c.depth < max_tasks,
        )
    )
    ancestor_rows = (
        await session.execute(
            select(Task, ancestors.c.depth)
            .join(ancestors, Task.id == ancestors.c.id)
            .where(ancestors.c.depth > 0)
            .order_by(ancestors.c.depth.asc())
            .limit(max_tasks)
        )
    ).all()

    # The requested root always survives: ancestors take at most ``max_tasks - 1``
    # slots so the root's own entry is never truncated away, and ``seen`` dedups a
    # node that a parent cycle would otherwise list as both ancestor and root.
    ancestors_near_first = [row[0] for row in ancestor_rows]
    ancestor_truncated = len(ancestors_near_first) >= max_tasks
    selected_ancestors = ancestors_near_first[: max_tasks - 1]
    selected_ancestors.reverse()
    seen: set[uuid.UUID] = set()
    ordered_tasks: list[Task] = []
    for candidate_task in (*selected_ancestors, root):
        if candidate_task.id in seen:
            continue
        seen.add(candidate_task.id)
        ordered_tasks.append(candidate_task)

    # Descendant BFS. Each level fetches the earliest children by (created_at, id)
    # with ``LIMIT remaining + 1``. ``ORDER BY ... LIMIT`` is a bounded top-N in
    # Postgres — a heap of at most ``remaining + 1`` rows, not a full sibling-set
    # materialization — so it stays bounded while returning a DETERMINISTIC subset.
    # Determinism is required: an unchanged lineage that truncates must still yield
    # a stable revision/checksum (the trusted-evidence index keys on them). ``seen``
    # makes descent cycle-safe; truncation is detected from a genuinely dropped
    # fresh child, never from a raw pre-dedup row count.
    descendant_truncated = False
    frontier = [root.id]
    while frontier and len(ordered_tasks) < max_tasks:
        remaining = max_tasks - len(ordered_tasks)
        rows = (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.parent_task_id.in_(frontier),
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
                .limit(remaining + 1)
            )
        ).scalars().all()
        next_frontier: list[uuid.UUID] = []
        for child in rows:
            if child.id in seen:
                continue
            if len(ordered_tasks) >= max_tasks:
                descendant_truncated = True
                break
            seen.add(child.id)
            ordered_tasks.append(child)
            next_frontier.append(child.id)
        frontier = next_frontier

    # If descent stopped at the count budget while unexpanded children remain —
    # including the case where ancestors + root already filled ``max_tasks`` and the
    # loop never ran — report truncation with one bounded existence probe.
    if not descendant_truncated and len(ordered_tasks) >= max_tasks and frontier:
        has_more = (
            await session.execute(
                select(Task.id)
                .where(
                    Task.project_id == project_id,
                    Task.parent_task_id.in_(frontier),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        descendant_truncated = has_more is not None

    projected_tasks, represented_tasks, tasks_byte_truncated = _bounded_lineage_tasks(
        ordered_tasks, root_id=root.id, budget=MAX_EVIDENCE_DATA_BYTES - 256
    )
    truncated = ancestor_truncated or descendant_truncated or tasks_byte_truncated
    data = {
        "requested_task_id": str(task_id),
        "truncated": truncated,
        "tasks": projected_tasks,
    }
    # Bind the lineage revision to every member actually represented (id, version,
    # updated_at) and to the truncation state, so the revision changes whenever the
    # included ancestor/descendant evidence changes — not only when the root does.
    member_fingerprint = _canonical_json(
        {
            "truncated": truncated,
            "members": [
                [str(item.id), int(item.version), _jsonable(item.updated_at)]
                for item in represented_tasks
            ],
        }
    )
    revision = (
        f"lineage:{root.id}:{hashlib.sha256(member_fingerprint).hexdigest()}"
    )
    return _evidence(
        root,
        "lineage",
        data,
        observed_at=observed_at or datetime.now(timezone.utc),
        revision=revision,
    )


__all__ = [
    "MAX_EVIDENCE_DATA_BYTES",
    "MAX_EVIDENCE_RESPONSE_BYTES",
    "MAX_TURN_EVIDENCE_BYTES",
    "ResidentTaskEvidence",
    "ResidentTaskNotFound",
    "inspect_project_task",
    "inspect_project_task_lineage",
]
