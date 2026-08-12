"""Pure changed-file provenance helpers for the task acceptance gate."""

from __future__ import annotations


import enum
from collections.abc import Callable
from typing import Any
_MODULE_SIZE_GOVERNANCE_CAP: int = 67

PARENT_DERIVED_CHANGED_FILES_KEY = "_pr_changed_files_from_rollup_parent"
COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY = "_pr_changed_files_from_rollup_commit_range"
PR_CHANGED_FILES_STAMP_SKIPPED_KEY = "_pr_changed_files_stamp_skipped"


class ChangedFilesStampSkipReason(str, enum.Enum):
    """Stable causes for a merge-time changed-files stamp not producing proof."""

    already_stamped = "already_stamped"
    child_pr_range_unavailable = "child_pr_range_unavailable"
    github_credentials_unavailable = "github_credentials_unavailable"
    pr_number_unavailable = "pr_number_unavailable"
    diff_fetch_failed = "diff_fetch_failed"
    no_diff_returned = "no_diff_returned"
    empty_diff = "empty_diff"
    rollup_parent_pr = "rollup_parent_pr"


PR_CHANGED_FILES_STAMP_SKIP_REASONS: frozenset[str] = frozenset(
    reason.value for reason in ChangedFilesStampSkipReason
)


def stamp_changed_files_skip_reason(
    context: dict[str, Any], reason: ChangedFilesStampSkipReason
) -> None:
    """Record one bounded, machine-readable changed-files stamp outcome."""
    context[PR_CHANGED_FILES_STAMP_SKIPPED_KEY] = reason.value


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
    stamp_changed_files_skip_reason(signal, ChangedFilesStampSkipReason.rollup_parent_pr)
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
    signal.pop(PR_CHANGED_FILES_STAMP_SKIPPED_KEY, None)
    signal[COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY] = True
    signal["_pr_changed_files_provenance"] = "commit_range_derived"
    return signal
