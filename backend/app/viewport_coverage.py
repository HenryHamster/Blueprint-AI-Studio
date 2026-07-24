"""Viewport coverage snapshot helpers for merge-time callers."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_VIEWPORT_ASSERTION_RE = re.compile(
    r"(expectNoOverflowAt|expectNoTextTruncationAt|expectMinTouchTargetAt)\s*\("
)
_VIEWPORT_ESCAPE_HATCH_RE = re.compile(
    r"data-viewport-allow-(overflow|truncation|small-target)"
)
_VIEWPORT_TEST_PATH_RE = re.compile(r"^admin-ui/src/.+\.viewport\.test\.[tj]sx?$")
_COMPONENT_PATH_RE = re.compile(r"^admin-ui/src/components/[^/]+\.tsx$")


def compute_viewport_coverage_snapshot(
    diff_text: str, pr_number: int | None = None
) -> dict:
    """Compute a viewport_coverage_snapshot dict from a PR unified diff."""
    assertion_before = 0
    assertion_after = 0
    escape_hatches = 0
    removed_components: list[str] = []

    in_viewport_test = False
    in_component = False
    current_path = ""

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            raw = parts[3] if len(parts) >= 4 else ""
            current_path = raw[2:] if raw.startswith("b/") else raw
            in_viewport_test = bool(_VIEWPORT_TEST_PATH_RE.match(current_path))
            in_component = bool(_COMPONENT_PATH_RE.match(current_path))
            continue

        if line.startswith("deleted file mode") and _COMPONENT_PATH_RE.match(
            current_path
        ):
            removed_components.append(current_path)
            continue

        if in_viewport_test:
            if line.startswith("-") and not line.startswith("---"):
                if _VIEWPORT_ASSERTION_RE.search(line):
                    assertion_before += 1
            elif line.startswith("+") and not line.startswith("+++"):
                if _VIEWPORT_ASSERTION_RE.search(line):
                    assertion_after += 1
            elif _VIEWPORT_ASSERTION_RE.search(line):
                assertion_before += 1
                assertion_after += 1

        if in_component and line.startswith("+") and not line.startswith("+++"):
            if _VIEWPORT_ESCAPE_HATCH_RE.search(line):
                escape_hatches += 1

    return {
        "assertion_count_before": assertion_before,
        "assertion_count_after": assertion_after,
        "new_escape_hatch_count": escape_hatches,
        "removed_component_files": removed_components[:50],
        "merged_pr": pr_number,
    }


async def update_viewport_coverage_snapshot_from_diff(
    session: AsyncSession,
    project: Any,
    pr_number: int,
    diff_text: str,
) -> None:
    """Write viewport_coverage_snapshot to project_sr_analytics."""
    from app.task_locking import with_sr_analytics_locked

    if "admin-ui/" not in diff_text:
        return

    snap = compute_viewport_coverage_snapshot(diff_text, pr_number=pr_number)

    def _mutate(data: dict) -> None:
        data["viewport_coverage_snapshot"] = snap

    await with_sr_analytics_locked(session, project.id, _mutate)
    logger.info(
        "[REGRESSION_DISPATCHER] viewport_coverage_snapshot updated "
        "project=%s pr=%s before=%d after=%d escape_hatches=%d removed=%d",
        project.id,
        pr_number,
        snap["assertion_count_before"],
        snap["assertion_count_after"],
        snap["new_escape_hatch_count"],
        len(snap["removed_component_files"]),
    )
