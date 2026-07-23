"""Tests for the viewport-coverage snapshot producer (app.viewport_coverage).

``compute_viewport_coverage_snapshot`` and
``update_viewport_coverage_snapshot_from_diff`` are pure producer helpers that
parse a merged PR diff and persist a ``viewport_coverage_snapshot`` onto
``project.design_doc``. They live in ``app.viewport_coverage`` and are invoked
from the rollup merge path (``rollup_management``). These tests exercise the
producer directly and do not depend on the (removed) pipeline-regression
dispatcher signal that formerly consumed the snapshot.
"""

from __future__ import annotations

import pytest

from testing.factories import make_project


class TestComputeViewportCoverageSnapshot:
    """Unit tests for compute_viewport_coverage_snapshot (pure function)."""

    def _make_diff(
        self,
        *,
        removed_assertions: int = 0,
        added_assertions: int = 0,
        context_assertions: int = 0,
        escape_hatches: int = 0,
        deleted_component: str | None = None,
    ) -> str:
        lines = []
        if deleted_component:
            lines.append(f"diff --git a/{deleted_component} b/{deleted_component}")
            lines.append("deleted file mode 100644")
            lines.append(f"--- a/{deleted_component}")
            lines.append("+++ /dev/null")

        lines.append(
            "diff --git a/admin-ui/src/components/Foo.viewport.test.tsx"
            " b/admin-ui/src/components/Foo.viewport.test.tsx"
        )
        lines.append("--- a/admin-ui/src/components/Foo.viewport.test.tsx")
        lines.append("+++ b/admin-ui/src/components/Foo.viewport.test.tsx")
        lines.extend(" expectNoOverflowAt(container, 375);" for _ in range(context_assertions))
        lines.extend("-expectNoOverflowAt(container, 375);" for _ in range(removed_assertions))
        lines.extend("+expectNoTextTruncationAt(container, 375);" for _ in range(added_assertions))

        if escape_hatches:
            lines.append(
                "diff --git a/admin-ui/src/components/Bar.tsx"
                " b/admin-ui/src/components/Bar.tsx"
            )
            lines.append("--- a/admin-ui/src/components/Bar.tsx")
            lines.append("+++ b/admin-ui/src/components/Bar.tsx")
            lines.extend("+  <div data-viewport-allow-overflow>" for _ in range(escape_hatches))

        return "\n".join(lines)

    def test_happy_path_drop(self):
        from app.viewport_coverage import compute_viewport_coverage_snapshot

        diff = self._make_diff(
            context_assertions=5, removed_assertions=3, added_assertions=0
        )
        snap = compute_viewport_coverage_snapshot(diff, pr_number=42)
        # before = 5 context + 3 removed = 8
        assert snap["assertion_count_before"] == 8
        # after = 5 context + 0 added = 5
        assert snap["assertion_count_after"] == 5
        assert snap["new_escape_hatch_count"] == 0
        assert snap["removed_component_files"] == []
        assert snap["merged_pr"] == 42

    def test_escape_hatch_addition(self):
        from app.viewport_coverage import compute_viewport_coverage_snapshot

        diff = self._make_diff(
            context_assertions=4,
            removed_assertions=0,
            added_assertions=0,
            escape_hatches=2,
        )
        snap = compute_viewport_coverage_snapshot(diff, pr_number=99)
        assert snap["assertion_count_before"] == snap["assertion_count_after"] == 4
        assert snap["new_escape_hatch_count"] == 2

    def test_legitimate_component_removal_recorded(self):
        from app.viewport_coverage import compute_viewport_coverage_snapshot

        diff = self._make_diff(
            context_assertions=5,
            removed_assertions=3,
            deleted_component="admin-ui/src/components/OldWidget.tsx",
        )
        snap = compute_viewport_coverage_snapshot(diff, pr_number=77)
        assert snap["removed_component_files"] == [
            "admin-ui/src/components/OldWidget.tsx"
        ]

    def test_no_admin_ui_files_skipped(self):
        from app.viewport_coverage import compute_viewport_coverage_snapshot

        diff = "diff --git a/backend/app/foo.py b/backend/app/foo.py\n-old\n+new\n"
        snap = compute_viewport_coverage_snapshot(diff, pr_number=1)
        assert snap["assertion_count_before"] == 0
        assert snap["assertion_count_after"] == 0
        assert snap["new_escape_hatch_count"] == 0

    @pytest.mark.asyncio
    async def test_update_writes_to_dedicated_sr_analytics(self, db_session):
        """Viewport coverage writes only to the dedicated SR analytics row."""
        from app.models import ProjectSRAnalytics
        from app.viewport_coverage import update_viewport_coverage_snapshot_from_diff

        project = make_project()
        project.design_doc = {"architectural_context": {"keep": True}}
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        diff = self._make_diff(context_assertions=6, removed_assertions=2)
        await update_viewport_coverage_snapshot_from_diff(
            db_session, project, 123, diff
        )
        await db_session.commit()
        await db_session.refresh(project)

        row = await db_session.get(ProjectSRAnalytics, project.id)
        assert row is not None
        snap = row.data["viewport_coverage_snapshot"]
        assert project.design_doc == {"architectural_context": {"keep": True}}
        assert snap["assertion_count_before"] == 8
        assert snap["assertion_count_after"] == 6
        assert snap["merged_pr"] == 123

    @pytest.mark.asyncio
    async def test_update_skips_when_no_admin_ui(self, db_session):
        """update_viewport_coverage_snapshot_from_diff is a no-op when no admin-ui in diff."""
        from app.viewport_coverage import update_viewport_coverage_snapshot_from_diff

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        diff = "diff --git a/backend/app/foo.py b/backend/app/foo.py\n+pass\n"
        await update_viewport_coverage_snapshot_from_diff(db_session, project, 99, diff)
        await db_session.commit()
        await db_session.refresh(project)

        snap = (project.design_doc or {}).get("viewport_coverage_snapshot")
        assert snap is None
