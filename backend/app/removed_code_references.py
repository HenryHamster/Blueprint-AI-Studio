"""Registry of removed code paths and components that long-lived documentation
must not reference.

This module provides a canonical list of retired UI components, modules, and code
paths that have been removed from the codebase. Documentation (including design
docs and plan documents) must not reference these removed items, as they can
mislead future implementers.

Each entry includes:
- name: human-readable name of the removed item
- patterns: list of string patterns to search for in documentation

The test_long_lived_docs_do_not_reference_removed_code_paths test scans all .md
files in /docs and /docs/plans to ensure no stale references exist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemovedCodeReference:
    """Single removed code item that documentation must not reference.

    Attributes:
        name: Human-readable name (e.g., "MetricsBar component")
        patterns: List of patterns to search for (exact string matches)
        rationale: Brief explanation of why it was removed
    """

    name: str
    patterns: list[str]
    rationale: str


# Registry of removed code references
# When UI components or modules are removed, add an entry here with patterns
# that docs should not reference. The test will ensure these patterns don't
# appear in .md files in /docs.
REMOVED_CODE_REFERENCES: tuple[RemovedCodeReference, ...] = (
    RemovedCodeReference(
        name="BoardSavedViewsBar component",
        patterns=["BoardSavedViewsBar"],
        rationale="Removed in rollup #12026 — board view filtering is now in KanbanColumn",
    ),
    RemovedCodeReference(
        name="QuickFilterPresets component",
        patterns=["QuickFilterPresets"],
        rationale="Removed in rollup #12026 — quick filters consolidated into main filter panel",
    ),
    RemovedCodeReference(
        name="TaskLaunchFAB component",
        patterns=["TaskLaunchFAB"],
        rationale="Removed — task launch moved to command palette and NewTaskModal",
    ),
    RemovedCodeReference(
        name="MetricsBar component",
        patterns=["MetricsBar"],
        rationale="Removed in rollup #12026 — metrics rolled into VersionSummaryPanel",
    ),
)


def get_removed_references() -> tuple[RemovedCodeReference, ...]:
    """Return the registry of removed code references."""
    return REMOVED_CODE_REFERENCES
