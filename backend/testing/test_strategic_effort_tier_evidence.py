"""Tests for backend/scripts/strategic_effort_tier_evidence.py.

The required named test ``test_realised_tier_split_and_course_changing_findings``
feeds controlled fixture rows to the script's aggregation and asserts BOTH the
realised routine/consequential split (counts + proportions) and the
course-changing-findings result per tier.

Most tests are standalone (stdlib-only module load via importlib, no DB),
mirroring the accepted evidence precedent
``testing/test_strategic_cycle_cost_projection.py``. One DB-backed test
round-trips real ``StrategicCycle`` rows through the script's ORM->dict
reduction so the production read path is exercised too.

Run with:
    bash scripts/test-backend.sh testing/test_strategic_effort_tier_evidence.py
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "strategic_effort_tier_evidence.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "strategic_effort_tier_evidence", _SCRIPT_PATH
    )
    assert spec is not None, f"Could not locate script at {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _row(**overrides):
    base = {
        "id": "cycle-x",
        "project_id": "p",
        "outcome": "completed",
        "directions_created": 0,
        "directions_retired": 0,
        "tasks_spawned": 0,
        "vision_updated": False,
        "cycle_metadata": {"effort_tier": "routine", "effort_tier_reason": "clean"},
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_realised_tier_split_and_course_changing_findings():
    """The aggregation reports the realised split AND the course-changing
    findings on controlled fixture rows.

    Fixture: 6 completed tiered cycles --
      routine c1 (opened a direction -> course-changing)
      routine c2 (retired a direction -> course-changing)
      routine c3 (no decision signal -> not course-changing)
      consequential c4 (vision updated -> course-changing)
      consequential c5 (no signal -> not course-changing)
      pinned c6 (only tasks_spawned=3 -> NOT course-changing)
    """
    mod = _load_module()
    rows = [
        _row(id="c1", cycle_metadata={"effort_tier": "routine"}, directions_created=1),
        _row(id="c2", cycle_metadata={"effort_tier": "routine"}, directions_retired=1),
        _row(id="c3", cycle_metadata={"effort_tier": "routine"}),
        _row(id="c4", cycle_metadata={"effort_tier": "consequential"}, vision_updated=True),
        _row(id="c5", cycle_metadata={"effort_tier": "consequential"}),
        _row(id="c6", cycle_metadata={"effort_tier": "pinned"}, tasks_spawned=3),
    ]
    agg = mod.aggregate_effort_tier_evidence(rows)

    split = agg["split"]
    assert split["total_completed_with_tier"] == 6
    assert split["counts"] == {"routine": 3, "consequential": 2, "pinned": 1}
    assert split["proportions"]["routine"] == 3 / 6
    assert split["proportions"]["consequential"] == 2 / 6
    assert split["proportions"]["pinned"] == 1 / 6
    assert split["completed_missing_tier_count"] == 0
    assert split["non_completed_with_tier_count"] == 0

    cc = agg["course_changing"]
    assert cc["by_tier"]["routine"]["course_changing"] == 2
    assert cc["by_tier"]["routine"]["course_changing_rate"] == 2 / 3
    assert cc["by_tier"]["consequential"]["course_changing"] == 1
    assert cc["by_tier"]["consequential"]["course_changing_rate"] == 1 / 2
    # tasks_spawned alone is a consequence, not a finding -- pinned c6 is NOT
    # course-changing under the stated definition.
    assert cc["by_tier"]["pinned"]["course_changing"] == 0
    assert cc["routine_course_changing_cycles"] == 2
    assert cc["routine_course_changing_rate"] == 2 / 3


def test_missing_tier_rows_are_reported_not_filtered():
    """A completed cycle without a stamped tier is a finding, not a drop."""
    mod = _load_module()
    rows = [
        _row(id="stamped", cycle_metadata={"effort_tier": "routine"}),
        _row(id="empty-meta", cycle_metadata={}),
        _row(id="no-meta", cycle_metadata=None),
    ]
    agg = mod.aggregate_effort_tier_evidence(rows)

    split = agg["split"]
    assert split["total_completed_with_tier"] == 1
    assert split["counts"] == {"routine": 1}
    assert split["completed_missing_tier_count"] == 2
    assert set(split["completed_missing_tier_ids"]) == {"empty-meta", "no-meta"}


def test_non_completed_rows_with_tier_are_reported_separately():
    """In-flight/failed rows with a tier are surfaced, not counted as realised."""
    mod = _load_module()
    rows = [
        _row(id="done", cycle_metadata={"effort_tier": "consequential"}),
        _row(
            id="inflight",
            outcome="in_progress",
            cycle_metadata={"effort_tier": "consequential"},
        ),
        _row(
            id="failed",
            outcome="failed_phase2",
            cycle_metadata={"effort_tier": "routine"},
        ),
    ]
    agg = mod.aggregate_effort_tier_evidence(rows)

    split = agg["split"]
    assert split["total_completed_with_tier"] == 1
    assert split["counts"] == {"consequential": 1}
    assert split["non_completed_with_tier_count"] == 2
    # The course-changing breakdown only grades completed cycles.
    assert agg["course_changing"]["by_tier"]["routine"]["cycles"] == 0


def test_unknown_tier_string_lands_in_other_not_silently_dropped():
    """An unrecognised tier value is surfaced in the ``other`` bucket."""
    mod = _load_module()
    rows = [
        _row(id="weird", cycle_metadata={"effort_tier": "nuclear"}),
        _row(id="ok", cycle_metadata={"effort_tier": "routine"}),
    ]
    agg = mod.aggregate_effort_tier_evidence(rows)

    split = agg["split"]
    assert split["counts"] == {"routine": 1, "other": 1}
    assert split["proportions"]["other"] == 1 / 2
    assert agg["course_changing"]["by_tier"]["other"]["cycles"] == 1


def test_empty_row_set_reports_zero_split_honestly():
    """No rows -> zero denominator and zero rates, never a fabricated number."""
    mod = _load_module()
    agg = mod.aggregate_effort_tier_evidence([])

    split = agg["split"]
    assert split["total_completed_with_tier"] == 0
    assert split["counts"] == {}
    assert split["proportions"] == {}
    assert split["completed_missing_tier_count"] == 0
    assert agg["course_changing"]["by_tier"]["routine"]["course_changing"] == 0
    assert agg["course_changing"]["routine_course_changing_rate"] == 0.0


def test_window_filtering_restricts_by_created_at():
    """--days N keeps only rows created within the window and reports the
    observed boundaries."""
    mod = _load_module()
    rows = [
        _row(id="old", created_at="2026-07-01T00:00:00+00:00"),
        _row(id="new", created_at="2026-08-05T00:00:00+00:00"),
    ]
    as_of = datetime(2026, 8, 6, tzinfo=timezone.utc)
    kept, start, end = mod.filter_rows_by_window(rows, 7, as_of)
    assert [r["id"] for r in kept] == ["new"]
    assert start == datetime(2026, 8, 5, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 5, tzinfo=timezone.utc)

    kept_all, _, _ = mod.filter_rows_by_window(rows, None, as_of)
    assert {r["id"] for r in kept_all} == {"old", "new"}


def test_script_exposes_the_aggregation_surface():
    """The script must load clean (stdlib only, no DB) and expose the model."""
    mod = _load_module()
    for name in (
        "row_tier",
        "is_course_changing",
        "realised_tier_split",
        "course_changing_breakdown",
        "aggregate_effort_tier_evidence",
        "row_to_dict",
        "filter_rows_by_window",
        "main",
    ):
        assert callable(getattr(mod, name, None)), f"missing {name}"


@pytest.mark.asyncio
async def test_db_rows_round_trip_through_row_to_dict_and_aggregation(db_session):
    """Real StrategicCycle rows reduce to dicts and aggregate end-to-end."""
    from app.models import Project, StrategicCycle  # lazy: keeps module stdlib-only

    project = Project(name="Effort Tier Evidence", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycles = [
        StrategicCycle(
            id="db-cycle-1",
            project_id=project.id,
            outcome="completed",
            directions_created=1,
            vision_updated=True,
            cycle_metadata={"effort_tier": "routine", "effort_tier_reason": "clean"},
        ),
        StrategicCycle(
            id="db-cycle-2",
            project_id=project.id,
            outcome="completed",
            cycle_metadata={"effort_tier": "consequential", "effort_tier_reason": "open messages"},
        ),
        StrategicCycle(
            id="db-cycle-3",
            project_id=project.id,
            outcome="completed",
            cycle_metadata={},
        ),
    ]
    for cycle in cycles:
        db_session.add(cycle)
    await db_session.commit()

    mod = _load_module()
    rows = [mod.row_to_dict(cycle) for cycle in cycles]
    agg = mod.aggregate_effort_tier_evidence(rows)

    assert agg["rows_considered"] == 3
    assert agg["split"]["total_completed_with_tier"] == 2
    assert agg["split"]["counts"] == {"routine": 1, "consequential": 1}
    assert agg["split"]["completed_missing_tier_ids"] == ["db-cycle-3"]
    assert agg["split"]["completed_missing_tier_count"] == 1
    assert agg["course_changing"]["by_tier"]["routine"]["course_changing"] == 1
    assert agg["course_changing"]["by_tier"]["consequential"]["course_changing"] == 0
    assert agg["course_changing"]["routine_course_changing_rate"] == 1.0


def test_main_output_reports_the_split_and_missing_tier_rows_without_a_db():
    """The CLI printer surfaces the realised split and the missing-tier finding
    (the artifact a reader checks criterion c-a6b328b9 against)."""
    import contextlib
    import io

    mod = _load_module()
    rows = [
        _row(id="c1", cycle_metadata={"effort_tier": "routine"}, directions_created=1),
        _row(id="c2", cycle_metadata={}),
    ]
    as_of = datetime(2026, 8, 6, tzinfo=timezone.utc)
    kept, start, end = mod.filter_rows_by_window(rows, None, as_of)
    agg = mod.aggregate_effort_tier_evidence(kept)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod._print_inputs_and_results(kept, start, end, None, as_of, agg)
    out = buf.getvalue()
    assert "REALISED routine/consequential split" in out
    assert "Completed cycles MISSING tier  : 1" in out
    assert "did NOT silence all course-changing findings" in out


def test_row_tier_normalization_uses_the_metadata_key():
    """row_tier reads cycle_metadata['effort_tier'] and ignores empty strings."""
    mod = _load_module()
    assert mod.row_tier({"cycle_metadata": {"effort_tier": "routine"}}) == "routine"
    assert mod.row_tier({"cycle_metadata": {"effort_tier": ""}}) is None
    assert mod.row_tier({"cycle_metadata": {}}) is None
    assert mod.row_tier({}) is None
    assert mod.row_tier({"cycle_metadata": {"effort_tier": 7}}) is None
