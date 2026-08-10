"""Pure changed-file provenance helpers for the task acceptance gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PARENT_DERIVED_CHANGED_FILES_KEY = "_pr_changed_files_from_rollup_parent"
COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY = "_pr_changed_files_from_rollup_commit_range"


def stamp_parent_derived_changed_files_signal(
    paths: list[str],
    *,
    stamp_changed_files_signal: Callable[..., dict[str, Any]],
    parent_derived_key: str,
) -> dict[str, Any]:
    """Stamp a rollup child's inherited parent-union provenance."""
    signal = stamp_changed_files_signal(paths, github_verified=False)
    signal[parent_derived_key] = True
    signal["_pr_changed_files_provenance"] = "parent_derived"
    return signal


def stamp_commit_range_derived_changed_files_signal(
    paths: list[str],
    *,
    stamp_changed_files_signal: Callable[..., dict[str, Any]],
    parent_derived_key: str,
) -> dict[str, Any]:
    """Stamp an exact rollup-child PR range as authoritative provenance."""
    signal = stamp_changed_files_signal(paths, github_verified=True)
    signal.pop(parent_derived_key, None)
    signal[COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY] = True
    signal["_pr_changed_files_provenance"] = "commit_range_derived"
    return signal
