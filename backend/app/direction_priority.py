"""Shared mapping from direction priority vocabulary to ``Task.priority``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_DIRECTION_TASK_PRIORITY = {"high": 100, "medium": 50, "low": 0}


def direction_task_priority(direction: Mapping[str, Any] | None) -> int:
    """Return scheduler priority for work commissioned by ``direction``.

    Sparse and legacy directions retain the Task model's neutral priority (50).
    One mapping keeps Strategic direct work, autonomous research roots, and
    discovery successors from ranking the same direction differently.
    """

    raw = direction.get("priority") if isinstance(direction, Mapping) else None
    return _DIRECTION_TASK_PRIORITY.get(str(raw or "").lower(), 50)
