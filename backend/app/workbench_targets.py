"""Compatibility aliases for the canonical operator-workbench contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.workbench_contracts import (
    DEFAULT_REFETCH_SURFACES,
    PROJECT_SECTIONS,
    REFETCH_SURFACES,
    RUNTIME_SECTIONS,
    SURFACE_DIRECTIONS,
    SURFACE_KNOWLEDGE,
    SURFACE_MESSAGES,
    SURFACE_PIPELINE,
    SURFACE_PROJECT,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    SURFACE_TASK,
    TARGET_KEYS,
    TARGET_SURFACES,
    compact_text,
    normalize_refetch_surfaces,
    normalize_target,
)

TARGET_SURFACE_MESSAGES = SURFACE_MESSAGES
TARGET_SURFACE_PIPELINE = SURFACE_PIPELINE
TARGET_SURFACE_PROJECT = SURFACE_PROJECT
TARGET_SURFACE_RUNTIME = SURFACE_RUNTIME
TARGET_SURFACE_TASK = SURFACE_TASK
TARGET_SURFACE_KNOWLEDGE = SURFACE_KNOWLEDGE

REFETCH_SURFACE_MESSAGES = SURFACE_MESSAGES
REFETCH_SURFACE_STRATEGIC_SESSION = SURFACE_STRATEGIC_SESSION
REFETCH_SURFACE_DIRECTIONS = SURFACE_DIRECTIONS
REFETCH_SURFACE_PIPELINE = SURFACE_PIPELINE
REFETCH_SURFACE_RUNTIME = SURFACE_RUNTIME

DEFAULT_WORKBENCH_REFETCH_SURFACES = DEFAULT_REFETCH_SURFACES

PROJECT_TARGET_SECTIONS = PROJECT_SECTIONS
PIPELINE_TARGET_SECTIONS = frozenset(("flow", "board", "archive"))
RUNTIME_TARGET_SECTIONS = RUNTIME_SECTIONS

TARGET_SECTIONS_BY_SURFACE = {
    TARGET_SURFACE_PROJECT: PROJECT_TARGET_SECTIONS,
    TARGET_SURFACE_PIPELINE: PIPELINE_TARGET_SECTIONS,
    TARGET_SURFACE_RUNTIME: RUNTIME_TARGET_SECTIONS,
}


def compact_workbench_text(value: Any, *, max_chars: int = 180) -> str | None:
    return compact_text(value, max_chars=max_chars)


def compact_workbench_target(
    target: Mapping[str, Any] | None,
    *,
    max_chars: int = 160,
) -> dict[str, Any] | None:
    del max_chars
    return normalize_target(target)


def bounded_workbench_refetch_surfaces(
    values: Sequence[str] | None,
    *,
    default: Sequence[str] | None = None,
    limit: int = 8,
) -> list[str]:
    return normalize_refetch_surfaces(values, default=default, limit=limit)
