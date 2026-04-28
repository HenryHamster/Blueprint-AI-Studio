"""Helpers for direction recommended_mode handling."""

from __future__ import annotations

from typing import Any


def normalized_recommended_mode(direction: dict[str, Any] | None) -> str | None:
    mode = (direction or {}).get("recommended_mode")
    if not isinstance(mode, str):
        return None
    mode = mode.strip().lower()
    return mode or None


def is_pause_mode(direction: dict[str, Any] | None) -> bool:
    return normalized_recommended_mode(direction) == "pause"


def append_prompt_suffix(payload: dict[str, Any], suffix: str) -> None:
    existing = payload.get("prompt_suffix", "")
    payload["prompt_suffix"] = f"{existing}\n{suffix}".strip() if existing else suffix


def mode_prompt_hint(mode: str | None) -> str | None:
    if mode == "audit":
        return (
            "Mode hint: this direction is in audit mode. Prioritize auditing and validation of the "
            "target system's health, drift, and claim-vs-shipped consistency over new feature work."
        )
    if mode == "fundamentals":
        return (
            "Mode hint: this direction is in fundamentals mode. Prioritize foundational reliability, "
            "architecture, tests, and maintainability over incremental product-facing features."
        )
    return None
