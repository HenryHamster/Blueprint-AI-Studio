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
import json
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
        "replay_effort_tier_split",
        "_replay_signals_for_cycle",
        "_extract_pre_spawn_signals",
        "_extract_operator_steered_from_phase1_context",
        "split_to_json_payload",
        "main",
    ):
        assert callable(getattr(mod, name, None)), f"missing {name}"


def test_split_to_json_payload_records_measured_routine_share():
    """--write-json payload: measured status, routine share, honest counts."""
    mod = _load_module()
    rows = [
        _row(id="c1", cycle_metadata={"effort_tier": "routine"}, directions_created=1),
        _row(id="c2", cycle_metadata={"effort_tier": "routine"}),
        _row(id="c3", cycle_metadata={"effort_tier": "consequential"}),
    ]
    agg = mod.aggregate_effort_tier_evidence(rows)
    as_of = datetime(2026, 8, 6, tzinfo=timezone.utc)
    payload = mod.split_to_json_payload(agg, None, None, None, as_of, "test provenance")

    assert payload["schema_version"] == 1
    assert payload["measurement_status"] == "measured"
    assert payload["routine_share"] == pytest.approx(2 / 3)
    assert payload["split"] == {
        "routine": pytest.approx(2 / 3),
        "consequential": pytest.approx(1 / 3),
        "pinned": 0.0,
        "other": 0.0,
    }
    assert payload["denominator_cycles"] == 3
    assert payload["measured_denominator_cycles"] == 3
    assert payload["counts"] == {"routine": 2, "consequential": 1}
    assert payload["completed_missing_tier_count"] == 0
    assert payload["non_completed_with_tier_count"] == 0
    assert payload["rows_considered"] == 3
    assert payload["measured_at"] == as_of.isoformat()
    assert payload["provenance"] == "test provenance"


def test_split_to_json_payload_reports_unmeasured_when_no_stamps():
    """No completed stamped cycles -> honest unmeasured payload (R = null)."""
    mod = _load_module()
    agg = mod.aggregate_effort_tier_evidence([])
    as_of = datetime(2026, 8, 6, tzinfo=timezone.utc)
    payload = mod.split_to_json_payload(agg, None, None, None, as_of, "test provenance")

    assert payload["measurement_status"] == "unmeasured"
    assert payload["routine_share"] is None
    assert payload["denominator_cycles"] == 0
    assert all(v is None for v in payload["split"].values())
    assert payload["counts"] == {}


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


def _zero_conflicts():
    return {
        "orphaned_gaps": 0,
        "ungrounded_directions": 0,
        "stale_active_moves": 0,
        "staffed_but_deferred": 0,
    }


def test_replayed_routine_share_is_a_labelled_fail_closed_lower_bound():
    """The REQUIRED named test: the replay path honours stamps, replays
    unstamped rows fail-closed, labels the payload, and stays a lower bound.

    Fixture (project p1, oldest first):
      c1 stamped routine WITH a clean integrity stamp (prior record verifiable);
      c2 unstamped, open_message_count=0, clean, phase1 context present and
         NOT operator-steered   -> ROUTINE;
      c3 unstamped, open_message_count NOT persisted       -> fail-closed CONSEQUENTIAL;
      c4 unstamped, open_message_count=2                   -> CONSEQUENTIAL.
    """
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"effort_tier": "routine", "integrity_conflict_counts": dict(zero)},
        ),
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
        ),
        _row(
            id="c3",
            project_id="p1",
            created_at="2026-08-03T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
        ),
        _row(
            id="c4",
            project_id="p1",
            created_at="2026-08-04T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 2, "operator_steered": False},
        ),
    ]

    replay = mod.replay_effort_tier_split(rows)

    # (a) stamped tier honoured as-is (c1); (b) c2 routine, c3 fail-closed to
    # consequential because open_message_count is unavailable, c4 consequential.
    assert replay["counts"] == {"routine": 2, "consequential": 2}
    assert replay["denominator_cycles"] == 4
    assert replay["source_counts"] == {"stamp": 1, "replay": 3, "unresolved": 0}
    assert replay["routine_share"] == pytest.approx(0.5)

    # (c) the payload is labelled replayed / fail_closed_lower_bound and carries
    # the signal_resolution map.
    agg = mod.aggregate_effort_tier_evidence(rows)
    as_of = datetime(2026, 8, 6, tzinfo=timezone.utc)
    payload = mod.split_to_json_payload(
        agg, None, None, None, as_of, "test provenance", replay=replay
    )
    assert payload["measurement_status"] == "replayed"
    assert payload["bound_kind"] == "fail_closed_lower_bound"
    assert payload["routine_share"] == pytest.approx(0.5)
    assert payload["denominator_cycles"] == 4
    assert payload["counts"] == {"routine": 2, "consequential": 2}
    assert payload["signal_resolution"]["open_message_count"]["defaulted-fail-closed"] == 1
    assert payload["signal_resolution"]["open_message_count"]["reconstructed-from-payload"] == 2
    assert payload["signal_resolution"]["integrity_conflict_counts"]["reconstructed-from-payload"] == 3

    # (c2) the STAMP-ONLY basis of the preserved measured ``split`` view is kept
    # separately, so a reader quoting split['routine'] can quote the denominator
    # that share is actually over (1 stamped cycle) rather than the replay's 4.
    assert payload["measured_denominator_cycles"] == 1
    assert payload["split"]["routine"] == pytest.approx(1.0)

    # (d) fail-closed lower bound: the replayed share (open_message_count missing
    # for c3) is at most the share when every signal is known (c3 is clean).
    rows_full = list(rows)
    rows_full[2] = dict(
        rows[2],
        pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
    )
    replay_full = mod.replay_effort_tier_split(rows_full)
    assert replay_full["counts"] == {"routine": 3, "consequential": 1}
    assert replay["routine_share"] <= replay_full["routine_share"]


def test_replay_uses_stamp_when_present():
    """A stamped completed cycle keeps its stamp even when a replay of its
    reconstructed signals would have produced a different tier."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        # Stamped routine with an unverifiable prior record and no persisted
        # open_message_count: a replay would fail closed to consequential, but
        # the stamp is honoured as-is.
        _row(id="c1", project_id="p1", cycle_metadata={"effort_tier": "routine"}),
        # Stamped consequential with clean signals: a replay would say routine,
        # but the stamp wins.
        _row(
            id="c2",
            project_id="p1",
            cycle_metadata={"effort_tier": "consequential", "integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"routine": 1, "consequential": 1}
    assert replay["source_counts"] == {"stamp": 2, "replay": 0, "unresolved": 0}


def test_replay_prior_cycle_dirty_derived_from_history():
    """The immediately-preceding completed cycle's non-zero integrity counts
    mark the current cycle a recovery decision -> CONSEQUENTIAL."""
    mod = _load_module()
    zero = _zero_conflicts()
    dirty = dict(zero)
    dirty["orphaned_gaps"] = 2
    rows = [
        # c1's own integrity is dirty -> consequential regardless of history.
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(dirty)},
            pre_spawn_signals={"open_message_count": 0},
        ),
        # c2 is clean itself but follows a dirty completed cycle -> consequential
        # via prior_cycle_dirty (derived-from-history, counts are reconstructible).
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"consequential": 2}
    assert replay["signal_resolution"]["prior_cycle_dirty"]["derived-from-history"] == 2


def test_replay_prior_cycle_dirty_fails_closed_when_unverifiable():
    """A prior completed cycle whose clean record is not stamped (unverifiable)
    fails closed: the current cycle is treated as a recovery decision."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        # c1 has NO integrity stamp (cleanliness unknown) but open messages are
        # clean and phase1 context is present + not operator-steered -> the
        # classifier's fail-open handling of a missing integrity stamp lets it
        # be routine.
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
        ),
        # c2 is clean and open-message-clean, but its prior completed cycle c1
        # has no integrity stamp -> prior_cycle_dirty defaulted-fail-closed.
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"routine": 1, "consequential": 1}
    assert replay["signal_resolution"]["prior_cycle_dirty"]["defaulted-fail-closed"] == 1


def test_replay_recent_cycle_failures_derived_from_history():
    """A failed/stalled outcome among the 3 prior cycles marks the current
    cycle a recovery decision -> CONSEQUENTIAL."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        # Non-completed failed cycle: contributes to history, never counted.
        _row(
            id="c1",
            project_id="p1",
            outcome="failed_phase2",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={},
        ),
        # c2 follows a failed cycle -> recent_cycle_failures=True.
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"consequential": 1}
    assert replay["denominator_cycles"] == 1
    assert replay["source_counts"] == {"stamp": 0, "replay": 1, "unresolved": 0}
    assert replay["signal_resolution"]["recent_cycle_failures"]["derived-from-history"] == 1


def test_replay_empty_row_set_reports_zero_honestly():
    """No rows -> zero denominator and zero share, never a fabricated number."""
    mod = _load_module()
    replay = mod.replay_effort_tier_split([])
    assert replay["denominator_cycles"] == 0
    assert replay["counts"] == {}
    assert replay["routine_share"] == 0.0


def test_extract_pre_spawn_signals_from_task_payload():
    """The phase2-task-payload enrichment extracts open_message_count and maps
    the payload's ``version_phase`` key to ``version_phase_block`` for the
    classifier. operator_steered is NOT packed in the phase2 payload (the spawn
    path persists it only on the phase1 task context), so it is not extracted
    here."""
    mod = _load_module()
    assert mod._extract_pre_spawn_signals(None) == {}
    assert mod._extract_pre_spawn_signals({"unrelated": 1}) == {}
    assert mod._extract_pre_spawn_signals({"open_message_count": 3}) == {"open_message_count": 3}
    assert mod._extract_pre_spawn_signals({"open_message_count": 0}) == {"open_message_count": 0}
    # version_phase (the payload's actual key) is mapped to version_phase_block.
    vp = {"qualification_state": {"state": "required_check_failed"}}
    assert mod._extract_pre_spawn_signals({"open_message_count": 0, "version_phase": vp}) == {
        "open_message_count": 0,
        "version_phase_block": vp,
    }
    # A None version_phase is not a block and is left out.
    assert mod._extract_pre_spawn_signals({"version_phase": None}) == {}
    # operator_steered in a payload is ignored -- it is never packed there; it is
    # recovered from the phase1 task context (test_operator_steered_from_phase1_context).
    assert mod._extract_pre_spawn_signals(
        {"open_message_count": 0, "operator_steered": True}
    ) == {"open_message_count": 0}


def test_operator_steered_from_phase1_context():
    """operator_steered is reconstructed from the phase1 design-doc task context:
    present-True -> True, present context without the key -> definite False, and
    no context (strategy-only cycle) -> None so the replay path fails closed."""
    mod = _load_module()
    assert mod._extract_operator_steered_from_phase1_context(
        {"operator_steered": True}
    ) is True
    assert mod._extract_operator_steered_from_phase1_context(
        {"operator_steered": False}
    ) is False
    # Absent key on a present context is a definite False (the spawn path only
    # stamps the key when operator-origin).
    assert mod._extract_operator_steered_from_phase1_context(
        {"strategic_cycle_id": "c1"}
    ) is False
    assert mod._extract_operator_steered_from_phase1_context({}) is False
    # No context at all -> genuinely unknown, the replay must fail closed.
    assert mod._extract_operator_steered_from_phase1_context(None) is None


def test_task_payload_enrichment_feeds_the_replay():
    """A row enriched from its phase2 task first payload contributes
    open_message_count, so a clean cycle with a persisted count and a phase1
    context that was NOT operator-steered is ROUTINE."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
        ),
    ]
    # Simulate the _load_cycle_rows enrichment step: the phase2 task payload
    # packs open_message_count as a critical key, and the phase1 task context
    # contributes operator_steered (definite False here).
    pre = mod._extract_pre_spawn_signals({"open_message_count": 0})
    pre["operator_steered"] = mod._extract_operator_steered_from_phase1_context(
        {"strategic_cycle_id": "c1"}
    )
    rows[0]["pre_spawn_signals"] = pre
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"routine": 1}

    # Without the enrichment the same row fails closed to consequential
    # (open_message_count unknown AND operator_steered unknown).
    rows_plain = [dict(rows[0], pre_spawn_signals={})]
    replay_plain = mod.replay_effort_tier_split(rows_plain)
    assert replay_plain["counts"] == {"consequential": 1}


def test_replay_operator_steered_with_no_open_messages_is_consequential():
    """An operator-steered cycle with zero open messages must NOT be replayed
    as ROUTINE: the classifier treats operator_steered as a standalone
    high-stakes signal, so a replay that defaulted it to falsy would inflate the
    routine share above the true fail-closed lower bound. operator_steered is
    persisted only on the phase1 task context (reconstructed here), never in
    the phase2 payload."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": True},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"consequential": 1}
    assert replay["routine_share"] == 0.0
    assert replay["signal_resolution"]["operator_steered"]["reconstructed-from-payload"] == 1


def test_replay_operator_steered_unknown_fails_closed():
    """A cycle with NO phase1 task (strategy-only) leaves operator_steered
    genuinely unavailable; the replay must fail closed to consequential rather
    than assume the ROUTINE-friendly falsy default."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"consequential": 1}
    assert replay["signal_resolution"]["operator_steered"]["defaulted-fail-closed"] == 1


def test_replay_version_phase_block_reconstructed_from_payload():
    """The phase2 payload carries the version-phase block under the
    ``version_phase`` key; the replay maps it to ``version_phase_block`` so an
    LTS-split cycle with a decision-pending qualification state is replayed
    CONSEQUENTIAL, while a settled version stays ROUTINE."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        # LTS split with a decision-pending qualification -> consequential.
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={
                "open_message_count": 0,
                "operator_steered": False,
                "version_phase_block": {
                    "qualification_state": {"state": "required_check_failed"}
                },
            },
        ),
        # Settled promoted version -> not a consequential signal by itself.
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={
                "open_message_count": 0,
                "operator_steered": False,
                "version_phase_block": {"qualification_state": {"state": "promoted"}},
            },
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"routine": 1, "consequential": 1}
    assert replay["signal_resolution"]["version_phase_block"]["reconstructed-from-payload"] == 2


def test_replay_absent_version_phase_block_is_a_valid_not_a_signal():
    """A missing ``version_phase`` key is a genuine not-a-signal, not an
    inflation: the strategic payload schema gives ``version_phase`` a floor
    callback, so a non-empty block ALWAYS ships in the packed payload -- a
    missing key means the classifier saw ``version_phase=None`` at spawn, which
    it explicitly treats as not-a-consequential-signal. So a clean non-LTS cycle
    replays ROUTINE and the bound stays a strict lower bound."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
        ),
    ]
    replay = mod.replay_effort_tier_split(rows)
    assert replay["counts"] == {"routine": 1}
    assert replay["signal_resolution"]["version_phase_block"]["defaulted-not-a-signal"] == 1


def test_main_cli_replay_writes_labelled_lower_bound_payload(monkeypatch, tmp_path):
    """--replay --write-json emits a payload labelled replayed / lower bound."""
    mod = _load_module()
    zero = _zero_conflicts()
    rows = [
        _row(
            id="c1",
            project_id="p1",
            created_at="2026-08-01T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
            pre_spawn_signals={"open_message_count": 0, "operator_steered": False},
        ),
        _row(
            id="c2",
            project_id="p1",
            created_at="2026-08-02T00:00:00+00:00",
            cycle_metadata={"integrity_conflict_counts": dict(zero)},
        ),
    ]

    async def _fake_load_cycle_rows():
        return rows

    monkeypatch.setattr(mod, "_load_cycle_rows", _fake_load_cycle_rows)
    out_path = tmp_path / "split.json"
    monkeypatch.setattr(
        "sys.argv",
        ["strategic_effort_tier_evidence.py", "--replay", "--write-json", str(out_path)],
    )
    mod.main()

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["measurement_status"] == "replayed"
    assert payload["bound_kind"] == "fail_closed_lower_bound"
    assert payload["denominator_cycles"] == 2
    assert payload["counts"] == {"routine": 1, "consequential": 1}
    assert "signal_resolution" in payload
