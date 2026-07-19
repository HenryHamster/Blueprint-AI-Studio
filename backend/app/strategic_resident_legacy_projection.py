"""Bounded projection of a Strategic Resident checkpoint into legacy inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.models import Project, StrategicResidentCheckpoint

LEGACY_SESSION_KEY = "_resident_strategic_session"
LEGACY_CONTINUATION_KEY = "_strategic_local_continuation"

_LEGACY_SUMMARY_MAX_BYTES = 10_000
_LEGACY_DECISIONS_MAX_BYTES = 32_000
_LEGACY_CURSOR_MAX_CHARS = 2_000
_LEGACY_RUNNING_UPDATE_MAX_BYTES = 20_000
_PROJECTION_NODE_MAX = 2_000
_PROJECTION_DEPTH_MAX = 8
_PROJECTION_STRING_MAX = 8_000


class LegacyCheckpointProjectionError(ValueError):
    """Checkpoint data cannot be represented safely in legacy inputs."""


def _bounded_projection(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    remaining = budget if budget is not None else [_PROJECTION_NODE_MAX]
    if remaining[0] <= 0 or depth >= _PROJECTION_DEPTH_MAX:
        return None
    remaining[0] -= 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_PROJECTION_STRING_MAX]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item))[:200]:
            key = str(raw_key)[:160]
            result[key] = _bounded_projection(
                value[raw_key],
                depth=depth + 1,
                budget=remaining,
            )
            if remaining[0] <= 0:
                break
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_projection(item, depth=depth + 1, budget=remaining)
            for item in list(value)[:200]
            if remaining[0] > 0
        ]
    return str(value)[:_PROJECTION_STRING_MAX]


def _utf8_prefix(value: Any, max_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _bounded_json_text(value: Any, max_bytes: int) -> str:
    bounded = _bounded_projection(value)
    encoded = json.dumps(
        bounded,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return json.dumps(
        {
            "truncated": True,
            "sha256": digest,
            "reason": "checkpoint projection exceeded legacy byte limit",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def merge_public_with_private(current: Any, projected: Any) -> dict[str, Any]:
    """Replace public state while preserving newer private runtime keys."""

    existing = current if isinstance(current, dict) else {}
    public = (
        {
            str(key): deepcopy(value)
            for key, value in projected.items()
            if not str(key).startswith("_")
        }
        if isinstance(projected, dict)
        else {}
    )
    private = {
        str(key): deepcopy(value)
        for key, value in existing.items()
        if str(key).startswith("_")
    }
    return {**public, **private}


def project_checkpoint_to_legacy(
    project: Project,
    checkpoint: StrategicResidentCheckpoint,
    *,
    now: datetime,
) -> None:
    # Caller (strategic_resident_ownership.cutover_to_strategic_resident) holds the
    # project row lock via with_for_update() from _lock_project_resident_tasks, so
    # direct writes to project.vision / project.directions / project.running_update
    # are serialized and safe. This function is intentionally synchronous to avoid
    # requiring an async session inside what is already a locked transaction.
    body = checkpoint.checkpoint if isinstance(checkpoint.checkpoint, dict) else {}
    working = body.get("working_strategy")
    if not isinstance(body.get("charter"), dict) or not isinstance(working, dict):
        raise LegacyCheckpointProjectionError(
            "Resident checkpoint lacks bounded charter or working strategy"
        )
    if not isinstance(working.get("vision"), dict):
        raise LegacyCheckpointProjectionError(
            "Resident checkpoint legacy vision projection is malformed"
        )
    if not isinstance(working.get("directions", []), list) or not isinstance(
        working.get("strategic_memory", []), list
    ):
        raise LegacyCheckpointProjectionError(
            "Resident checkpoint legacy list projection is malformed"
        )
    charter = _bounded_projection(body["charter"])
    projected_vision = _bounded_projection(working.get("vision") or {})
    project.problem_model = merge_public_with_private(project.problem_model, charter)
    vision = merge_public_with_private(project.vision, projected_vision)

    summary_source = working.get("legacy_session_summary")
    if not isinstance(summary_source, str):
        summary_source = _bounded_json_text(
            {
                key: working.get(key)
                for key in (
                    "vision",
                    "running_update",
                    "directions",
                    "strategic_memory",
                    "decisions",
                    "commitments",
                    "open_loops",
                )
                if key in working
            },
            _LEGACY_SUMMARY_MAX_BYTES,
        )
    decisions = working.get(
        "decisions",
        working.get(
            "legacy_decisions_register",
            {"version": 1, "decisions": []},
        ),
    )
    cursors = body.get("cursors") if isinstance(body.get("cursors"), dict) else {}
    cursor = cursors.get("legacy_message_cursor")
    if cursor is None:
        cursor = cursors.get("handled_message_sequence")
    if cursor is None:
        cursor = 0
    vision[LEGACY_SESSION_KEY] = {
        "status": "ended",
        "stop_requested": True,
        "ended_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "_session_summary": _utf8_prefix(
            summary_source,
            _LEGACY_SUMMARY_MAX_BYTES,
        ),
        "_decisions_reg": _bounded_json_text(
            decisions,
            _LEGACY_DECISIONS_MAX_BYTES,
        ),
        "_message_cursor": str(cursor)[:_LEGACY_CURSOR_MAX_CHARS],
        "rollback_checkpoint_id": str(checkpoint.id),
        "rollback_checkpoint_version": int(checkpoint.version),
        "rollback_checkpoint_hash": checkpoint.content_hash,
    }
    vision.pop(LEGACY_CONTINUATION_KEY, None)
    project.vision = vision
    project.directions = _bounded_projection(working.get("directions") or [])
    project.strategic_memory_notes = _bounded_projection(
        working.get("strategic_memory") or []
    )
    project.running_update = _utf8_prefix(
        working.get("running_update") or "",
        _LEGACY_RUNNING_UPDATE_MAX_BYTES,
    )


__all__ = [
    "LEGACY_CONTINUATION_KEY",
    "LEGACY_SESSION_KEY",
    "LegacyCheckpointProjectionError",
    "merge_public_with_private",
    "project_checkpoint_to_legacy",
]
