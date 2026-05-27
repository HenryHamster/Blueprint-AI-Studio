"""Tests for the viewport-regression subsystem of the pipeline regression dispatcher.

Covers:
  - _collect_viewport_failures / _evaluate_viewport_regression_signal (TestViewportRegressionEvaluator)
  - compute_viewport_coverage_snapshot / update_viewport_coverage_snapshot_from_diff (TestComputeViewportCoverageSnapshot)
  - viewport_coverage_drop signal (standalone integration tests)
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import Task, TaskStatus
from app.pipeline_regression_dispatcher import run_once
from app.time_utils import _utcnow

from testing.factories import make_project, make_task


def _make_viewport_task(
    project_id,
    *,
    status_message: str,
    title: str = "Test Task",
    updated_at_offset_hours: int = 1,
) -> Task:
    """Create a FAILED Task whose status_message triggers VIEWPORT_REGRESSION classification."""
    now = _utcnow()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        title=title,
        created_at=now - timedelta(hours=updated_at_offset_hours),
        updated_at=now - timedelta(hours=updated_at_offset_hours),
    )
    task.latest_status_message = status_message
    return task


class TestViewportRegressionEvaluator:
    """Tests for _collect_viewport_failures + _evaluate_viewport_regression_signal."""

    @pytest.mark.asyncio
    async def test_viewport_failure_trips_and_spawns(self, db_session):
        """Two FAILED tasks with viewport errors on same component+width spawn a case."""
        from app.models import RegressionRemediationCase
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
            _evaluate_viewport_regression_signal,
        )

        assert _VIEWPORT_REGRESSION_MIN_FAILURES >= 2, "threshold sanity"

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow at width=375px:\n"
            "  <div> right=400px (limit 376px)\n"
            "Found in MessagesPanel.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "MessagesPanel"
        assert trips[0]["width"] == "375"
        assert trips[0]["count"] >= _VIEWPORT_REGRESSION_MIN_FAILURES

        spawned = await _evaluate_viewport_regression_signal(
            db_session, project.id, spawn_budget_left=2
        )
        assert spawned == 1
        await db_session.commit()

        cases = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class
                        == "viewport_regression:MessagesPanel:overflow@375",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_spawn(self, db_session):
        """A single viewport failure (below min) does not spawn."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px touch target\n"
            "Found in AlertsBanner.viewport.test.tsx"
        )
        # Add one fewer than the minimum
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES - 1):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 0

    @pytest.mark.asyncio
    async def test_unknown_component_fallback(self, db_session):
        """When the file reference is absent, component falls back to 'unknown'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = "expectNoOverflowAt(375): document.documentElement.scrollWidth=400px > 376px"
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "unknown"
        assert trips[0]["width"] == "375"

    @pytest.mark.asyncio
    async def test_unknown_width_fallback(self, db_session):
        """When width cannot be extracted, width falls back to '0'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectnooverflowat( ): some viewport overflow detected\n"
            "Found in TaskDetailDrawerMeta.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "TaskDetailDrawerMeta"
        assert trips[0]["width"] == "0"

    @pytest.mark.asyncio
    async def test_dedup_same_component_width_updates_not_duplicates(self, db_session):
        """A second run_once with the same viewport failure class updates the case, not duplicates."""
        from app.models import RegressionRemediationCase
        from app.pipeline_regression_dispatcher import run_once

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 1 element(s) overflow\n"
            "Found in SearchFilterBar.viewport.test.tsx"
        )
        for _ in range(2):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        first = await run_once(db_session, project.id)
        assert first == 1
        await db_session.commit()

        # Second run: in-flight case should block re-spawn
        second = await run_once(db_session, project.id)
        assert second == 0

        cases = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class.like(
                            "viewport_regression:%"
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_overflow_only_failure_type(self, db_session):
        """Failures from expectNoOverflowAt classify as 'overflow'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow at width=375px\n"
            "Found in MessagesPanel.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "overflow"

    @pytest.mark.asyncio
    async def test_touch_target_only_failure_type(self, db_session):
        """Failures from expectMinTouchTargetAt classify as 'touch_target'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px touch target\n"
            "Found in AlertsBanner.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "touch_target"

    @pytest.mark.asyncio
    async def test_mixed_failure_types(self, db_session):
        """When a (component, width) bucket has both overflow and touch_target failures, classify as 'mixed'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        overflow_msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow\n"
            "Found in TestComponent.viewport.test.tsx"
        )
        touch_target_msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px\n"
            "Found in TestComponent.viewport.test.tsx"
        )
        # Add at least _VIEWPORT_REGRESSION_MIN_FAILURES failures
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES - 1):
            db_session.add(_make_viewport_task(project.id, status_message=overflow_msg))
        db_session.add(_make_viewport_task(project.id, status_message=touch_target_msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "mixed"

    def test_extract_failure_type_text_truncation(self):
        """_extract_failure_type returns 'text_truncation' for expectNoTextTruncationAt messages."""
        from app.pipeline_regression_dispatcher import _extract_failure_type

        msg = "expectNoTextTruncationAt(375): 1 element(s) truncated at width=375px"
        assert _extract_failure_type(msg) == "text_truncation"

    def test_extract_failure_type_overflow(self):
        """_extract_failure_type returns 'overflow' for expectNoOverflowAt messages."""
        from app.pipeline_regression_dispatcher import _extract_failure_type

        msg = "expectNoOverflowAt(375): 2 element(s) overflow at width=375px"
        assert _extract_failure_type(msg) == "overflow"

    def test_extract_failure_type_unknown(self):
        """_extract_failure_type returns 'unknown' when no known assertion is in the message."""
        from app.pipeline_regression_dispatcher import _extract_failure_type

        assert _extract_failure_type("some generic failure") == "unknown"

    @pytest.mark.asyncio
    async def test_regression_class_includes_assertion_type(self, db_session):
        """Emitted regression_class includes assertion type: viewport_regression:<C>:<type>@<w>."""
        from app.models import RegressionRemediationCase
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _evaluate_viewport_regression_signal,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px touch target\n"
            "Found in TaskCard.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        spawned = await _evaluate_viewport_regression_signal(
            db_session, project.id, spawn_budget_left=2
        )
        assert spawned == 1
        await db_session.commit()

        cases = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class
                        == "viewport_regression:TaskCard:touch_target@375",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_introducing_pr_number_in_trip_dict(self, db_session):
        """Trip dict includes introducing_pr_number when earliest task has pr_number in payload."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): overflow detected\n"
            "Found in MessagesPanel.viewport.test.tsx"
        )
        now = _utcnow()
        for i in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            task = make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "review", "pr_number": 1234}],
                created_at=now - timedelta(hours=i + 1),
                updated_at=now - timedelta(hours=i + 1),
            )
            task.latest_status_message = msg
            db_session.add(task)
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["introducing_pr_number"] == 1234
        assert trips[0]["introducing_commit_sha"] is None

    @pytest.mark.asyncio
    async def test_introducing_commit_sha_in_trip_dict_when_no_pr(self, db_session):
        """Trip dict includes introducing_commit_sha from context head_sha when no pr_number."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): overflow detected\n"
            "Found in KanbanBoard.viewport.test.tsx"
        )
        sha = "abc123def456789"
        now = _utcnow()
        for i in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            task = make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                context={"head_sha": sha},
                created_at=now - timedelta(hours=i + 1),
                updated_at=now - timedelta(hours=i + 1),
            )
            task.latest_status_message = msg
            db_session.add(task)
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["introducing_pr_number"] is None
        assert trips[0]["introducing_commit_sha"] == sha

    @pytest.mark.asyncio
    async def test_introducing_attribution_none_when_no_signals(self, db_session):
        """Trip dict has both attribution fields as None when task has no pr_number or head_sha."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): overflow detected\n"
            "Found in DirectionsPanel.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["introducing_pr_number"] is None
        assert trips[0]["introducing_commit_sha"] is None

    @pytest.mark.asyncio
    async def test_earliest_task_used_for_attribution(self, db_session):
        """The earliest failing task's pr_number is used, not the most recent."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): overflow detected\n"
            "Found in ResearchBrowser.viewport.test.tsx"
        )
        now = _utcnow()
        # Create tasks from newest to oldest; the oldest (highest offset) has pr_number=100.
        # All other (newer) tasks have pr_number=999.
        for i in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            pr_num = 100 if i == _VIEWPORT_REGRESSION_MIN_FAILURES - 1 else 999
            task = make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "review", "pr_number": pr_num}],
                created_at=now - timedelta(hours=i + 1),
                updated_at=now - timedelta(hours=i + 1),
            )
            task.latest_status_message = msg
            db_session.add(task)
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        # The oldest task (pr_number=100) should determine attribution
        assert trips[0]["introducing_pr_number"] == 100

    @pytest.mark.asyncio
    async def test_hyphenated_composed_filename_extracted_correctly(self, db_session):
        """Hyphenated composed-page filenames are preserved in full, not truncated."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        composed_cases = [
            (
                "operator-home-composed",
                "expectNoOverflowAt(375): overflow at width=375px\nFound in operator-home-composed.viewport.test.tsx",
            ),
            (
                "kanban-composed",
                "expectNoOverflowAt(375): overflow at width=375px\nFound in kanban-composed.viewport.test.tsx",
            ),
            (
                "pipeline-composed",
                "expectNoOverflowAt(375): overflow at width=375px\nFound in pipeline-composed.viewport.test.tsx",
            ),
        ]

        for _component, msg in composed_cases:
            for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
                db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        components = {t["component"] for t in trips}

        # Each composed-page test must produce a distinct, fully-named component
        assert "operator-home-composed" in components, (
            f"expected operator-home-composed in {components}"
        )
        assert "kanban-composed" in components, (
            f"expected kanban-composed in {components}"
        )
        assert "pipeline-composed" in components, (
            f"expected pipeline-composed in {components}"
        )
        # Must be three distinct buckets, not collapsed to "composed"
        assert len(components) == 3, f"expected 3 distinct components, got {components}"
        assert "composed" not in components, "bare 'composed' token must not appear"


# ---------------------------------------------------------------------------
# viewport_coverage_drop signal tests
# ---------------------------------------------------------------------------


def _design_doc_with_coverage_drop(
    *,
    before: int = 10,
    after: int = 7,
    escape_hatches: int = 0,
    removed_components: list | None = None,
) -> dict:
    return {
        "viewport_coverage_snapshot": {
            "assertion_count_before": before,
            "assertion_count_after": after,
            "new_escape_hatch_count": escape_hatches,
            "removed_component_files": removed_components or [],
            "merged_pr": 1234,
        }
    }


@pytest.mark.asyncio
async def test_viewport_coverage_drop_test_deletion(db_session):
    """Signal fires when assertions are deleted (post count < pre count)."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(before=10, after=7)
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"] == "viewport_coverage_drop"
    assert ctx["signal_name"] == "viewport_coverage_drop"
    payload = task.first_payload or {}
    assert "viewport_coverage_drop" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_viewport_coverage_drop_skip_insertion(db_session):
    """Signal fires when .skip() insertion reduces effective assertion count."""
    project = make_project(design_doc=_design_doc_with_coverage_drop(before=8, after=5))
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    assert (task.context or {}).get("regression_class") == "viewport_coverage_drop"


@pytest.mark.asyncio
async def test_viewport_coverage_drop_escape_hatch_attribute(db_session):
    """Signal fires when new escape-hatch attributes are added (count stable but coverage eroded)."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(before=10, after=10, escape_hatches=2)
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"] == "viewport_coverage_drop"
    payload = task.first_payload or {}
    assert "escape-hatch" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_viewport_coverage_drop_no_signal_on_component_removal(db_session):
    """Signal does NOT fire when a component file was legitimately removed."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(
            before=10,
            after=7,
            removed_components=["admin-ui/src/components/OldWidget.tsx"],
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert tasks == []


# ---------------------------------------------------------------------------
# viewport_coverage_snapshot producer tests
# ---------------------------------------------------------------------------


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
        for _ in range(context_assertions):
            lines.append(" expectNoOverflowAt(container, 375);")
        for _ in range(removed_assertions):
            lines.append("-expectNoOverflowAt(container, 375);")
        for _ in range(added_assertions):
            lines.append("+expectNoTextTruncationAt(container, 375);")

        if escape_hatches:
            lines.append(
                "diff --git a/admin-ui/src/components/Bar.tsx"
                " b/admin-ui/src/components/Bar.tsx"
            )
            lines.append("--- a/admin-ui/src/components/Bar.tsx")
            lines.append("+++ b/admin-ui/src/components/Bar.tsx")
            for _ in range(escape_hatches):
                lines.append("+  <div data-viewport-allow-overflow>")

        return "\n".join(lines)

    def test_happy_path_drop(self):
        from app.pipeline_regression_dispatcher import (
            compute_viewport_coverage_snapshot,
        )

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
        from app.pipeline_regression_dispatcher import (
            compute_viewport_coverage_snapshot,
        )

        diff = self._make_diff(
            context_assertions=4,
            removed_assertions=0,
            added_assertions=0,
            escape_hatches=2,
        )
        snap = compute_viewport_coverage_snapshot(diff, pr_number=99)
        assert snap["assertion_count_before"] == snap["assertion_count_after"] == 4
        assert snap["new_escape_hatch_count"] == 2

    def test_legitimate_component_removal_suppresses(self):
        from app.pipeline_regression_dispatcher import (
            compute_viewport_coverage_snapshot,
            _probe_viewport_coverage_drop,
        )

        diff = self._make_diff(
            context_assertions=5,
            removed_assertions=3,
            deleted_component="admin-ui/src/components/OldWidget.tsx",
        )
        snap = compute_viewport_coverage_snapshot(diff, pr_number=77)
        assert snap["removed_component_files"] == [
            "admin-ui/src/components/OldWidget.tsx"
        ]
        # Probe should suppress when a component was legitimately removed
        assert not _probe_viewport_coverage_drop({"viewport_coverage_snapshot": snap})

    def test_no_admin_ui_files_skipped(self):
        from app.pipeline_regression_dispatcher import (
            compute_viewport_coverage_snapshot,
        )

        diff = "diff --git a/backend/app/foo.py b/backend/app/foo.py\n-old\n+new\n"
        snap = compute_viewport_coverage_snapshot(diff, pr_number=1)
        assert snap["assertion_count_before"] == 0
        assert snap["assertion_count_after"] == 0
        assert snap["new_escape_hatch_count"] == 0

    @pytest.mark.asyncio
    async def test_update_writes_to_design_doc(self, db_session):
        """update_viewport_coverage_snapshot_from_diff writes to project.design_doc."""
        from app.pipeline_regression_dispatcher import (
            update_viewport_coverage_snapshot_from_diff,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        diff = self._make_diff(context_assertions=6, removed_assertions=2)
        await update_viewport_coverage_snapshot_from_diff(
            db_session, project, 123, diff
        )
        await db_session.commit()
        await db_session.refresh(project)

        snap = (project.design_doc or {}).get("viewport_coverage_snapshot")
        assert snap is not None
        assert snap["assertion_count_before"] == 8
        assert snap["assertion_count_after"] == 6
        assert snap["merged_pr"] == 123

    @pytest.mark.asyncio
    async def test_update_skips_when_no_admin_ui(self, db_session):
        """update_viewport_coverage_snapshot_from_diff is a no-op when no admin-ui in diff."""
        from app.pipeline_regression_dispatcher import (
            update_viewport_coverage_snapshot_from_diff,
        )

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

    @pytest.mark.asyncio
    async def test_round_trip_triggers_dispatcher(self, db_session):
        """Producer write + dispatcher read + run_once spawns a case end-to-end."""
        from app.pipeline_regression_dispatcher import (
            update_viewport_coverage_snapshot_from_diff,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Diff that drops 3 assertions: before=8, after=5
        diff = self._make_diff(context_assertions=5, removed_assertions=3)
        await update_viewport_coverage_snapshot_from_diff(
            db_session, project, 555, diff
        )
        await db_session.commit()
        await db_session.refresh(project)

        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        task = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        assert (task.context or {}).get("regression_class") == "viewport_coverage_drop"
