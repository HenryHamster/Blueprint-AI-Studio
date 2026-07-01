"""Canonical operator-workbench target and refetch vocabulary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TARGET_SURFACE_MESSAGES = "messages"
TARGET_SURFACE_PIPELINE = "pipeline"
TARGET_SURFACE_PROJECT = "project"
TARGET_SURFACE_RUNTIME = "runtime"
TARGET_SURFACE_TASK = "task"
TARGET_SURFACE_KNOWLEDGE = "knowledge"

TARGET_SURFACES = frozenset(
    (
        TARGET_SURFACE_MESSAGES,
        TARGET_SURFACE_PIPELINE,
        TARGET_SURFACE_PROJECT,
        TARGET_SURFACE_RUNTIME,
        TARGET_SURFACE_TASK,
        TARGET_SURFACE_KNOWLEDGE,
    )
)

REFETCH_SURFACE_MESSAGES = "messages"
REFETCH_SURFACE_STRATEGIC_SESSION = "strategic_session"
REFETCH_SURFACE_DIRECTIONS = "directions"
REFETCH_SURFACE_PIPELINE = "pipeline"
REFETCH_SURFACE_RUNTIME = "runtime"

REFETCH_SURFACES = frozenset(
    (
        REFETCH_SURFACE_MESSAGES,
        REFETCH_SURFACE_STRATEGIC_SESSION,
        REFETCH_SURFACE_DIRECTIONS,
        REFETCH_SURFACE_PIPELINE,
        REFETCH_SURFACE_RUNTIME,
    )
)

DEFAULT_WORKBENCH_REFETCH_SURFACES = (REFETCH_SURFACE_STRATEGIC_SESSION,)

TARGET_KEYS = (
    "surface",
    "project_id",
    "section",
    "task_id",
    "message_id",
    "direction_id",
    "alert_id",
    "action",
    "document_id",
    "artifact_id",
    "cycle_id",
    "runner_id",
)

PROJECT_TARGET_SECTIONS = frozenset(
    (
        "running-update",
        "directions",
        "problem-model",
        "design-doc",
        "manage",
        "selected_refs",
        "overview",
        "build-config",
        "branches",
        "auto-review",
        "resource-envelope",
        "auto-remediation",
        "issue-implementation",
        "alert-domains",
        "access",
    )
)

PIPELINE_TARGET_SECTIONS = frozenset(("flow", "board", "archive"))

RUNTIME_TARGET_SECTIONS = frozenset(
    (
        "health",
        "devices",
        "compute",
        "accounts",
        "settings",
        "analytics",
        "general",
        "orchestrator",
        "deploy",
        "provider-routing",
        "provider-settings",
        "notifications",
        "advanced",
    )
)

TARGET_SECTIONS_BY_SURFACE = {
    TARGET_SURFACE_PROJECT: PROJECT_TARGET_SECTIONS,
    TARGET_SURFACE_PIPELINE: PIPELINE_TARGET_SECTIONS,
    TARGET_SURFACE_RUNTIME: RUNTIME_TARGET_SECTIONS,
}

TARGET_SECTION_ALIASES_BY_SURFACE = {
    TARGET_SURFACE_RUNTIME: {
        "runners": "devices",
    },
}


def compact_workbench_text(value: Any, *, max_chars: int = 180) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[: max_chars - 3]}..."


def compact_workbench_target(
    target: Mapping[str, Any] | None,
    *,
    max_chars: int = 160,
) -> dict[str, Any] | None:
    if not isinstance(target, Mapping):
        return None
    surface = compact_workbench_text(target.get("surface"), max_chars=80)
    if surface not in TARGET_SURFACES:
        return None

    compact: dict[str, Any] = {"surface": surface}
    valid_sections = TARGET_SECTIONS_BY_SURFACE.get(surface)
    section_aliases = TARGET_SECTION_ALIASES_BY_SURFACE.get(surface, {})
    for key in TARGET_KEYS:
        if key == "surface":
            continue
        value = compact_workbench_text(target.get(key), max_chars=max_chars)
        if not value:
            continue
        if key == "section":
            value = section_aliases.get(value, value)
        if key == "section" and valid_sections is not None and value not in valid_sections:
            continue
        compact[key] = value
    return compact


def bounded_workbench_refetch_surfaces(
    values: Sequence[str] | None,
    *,
    default: Sequence[str] | None = None,
    limit: int = 8,
) -> list[str]:
    surfaces: list[str] = []
    seen: set[str] = set()
    source_values = values if values is not None else default or ()
    for raw in source_values:
        surface = compact_workbench_text(raw, max_chars=80)
        if not surface or surface in seen or surface not in REFETCH_SURFACES:
            continue
        surfaces.append(surface)
        seen.add(surface)
        if len(surfaces) >= limit:
            break
    if surfaces:
        return surfaces
    if not default:
        return []
    return bounded_workbench_refetch_surfaces(default, default=None, limit=limit)
