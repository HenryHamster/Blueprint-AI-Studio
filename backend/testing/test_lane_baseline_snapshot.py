"""Tests for lane_baseline_snapshot module."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.lane_baseline_snapshot import (
    DESIGN_DOC_KEY,
    _MAX_BASELINE_ENTRY_BYTES,
    _MAX_BASELINES_STORED,
    _compute_checkpoint_recovery_waste,
    _compute_relay_medians,
    _slim_baseline,
    backfill_missing_lane_baselines,
    persist_version_lane_baseline,
    read_version_lane_baseline,
)
from app.models import ReleaseVersion, TaskStatus
from app.project_telemetry import (
    read_version_lane_baseline as _read_lane_baseline_row,
    upsert_lane_baseline as _upsert_lane_baseline_row,
)
from app.review_fix_lane_pilot import LANE_RECOVERY_REASON_KEY
from app.time_utils import _utcnow
from testing.factories import make_project, make_task


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------


class TestComputeRelayMedians:
    def test_empty_returns_none_medians(self):
        result = _compute_relay_medians([])
        assert result["wall_clock_seconds"] is None
        assert result["active_seconds"] is None
        assert result["gap_seconds"] is None
        assert result["task_count"] is None
        assert result["pr_count"] == 0

    def test_single_pr_returns_that_pr_values(self):
        breakdown = [
            {
                "pr_number": "101",
                "task_count": 3,
                "review_seconds": 600.0,
                "fix_seconds": 1200.0,
                "gap_seconds": 300.0,
                "total_seconds": 2100.0,
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
        ]
        result = _compute_relay_medians(breakdown)
        assert result["wall_clock_seconds"] == 2100.0
        assert result["active_seconds"] == 1800.0  # 600 + 1200
        assert result["gap_seconds"] == 300.0
        assert result["task_count"] == 3.0
        assert result["pr_count"] == 1

    def test_odd_count_median(self):
        breakdown = [
            {
                "pr_number": str(i),
                "task_count": i,
                "review_seconds": float(i * 100),
                "fix_seconds": 0.0,
                "gap_seconds": float(i * 10),
                "total_seconds": float(i * 110),
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
            for i in [1, 2, 3]
        ]
        result = _compute_relay_medians(breakdown)
        # Median of [1,2,3] total_seconds (sorted) * 110 => 220
        assert result["wall_clock_seconds"] == 220.0

    def test_even_count_median_averages_middle_two(self):
        breakdown = [
            {
                "pr_number": str(i),
                "task_count": 1,
                "review_seconds": float(i * 100),
                "fix_seconds": 0.0,
                "gap_seconds": 0.0,
                "total_seconds": float(i * 100),
                "queue_seconds": 0.0,
                "provision_seconds": 0.0,
                "cold_start_seconds": 0.0,
                "execution_seconds": 0.0,
            }
            for i in [1, 2, 3, 4]
        ]
        result = _compute_relay_medians(breakdown)
        # Sorted: 100, 200, 300, 400 → median = (200+300)/2 = 250
        assert result["wall_clock_seconds"] == 250.0
        assert result["pr_count"] == 4


class TestReadVersionLaneBaseline:
    def test_returns_none_when_no_design_doc(self):
        project = make_project()
        project.design_doc = None
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_none_when_key_missing(self):
        project = make_project()
        project.design_doc = {"other_key": {}}
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_none_for_different_version(self):
        project = make_project()
        project.design_doc = {DESIGN_DOC_KEY: {"8": {"version_number": 8}}}
        assert read_version_lane_baseline(project, 9) is None

    def test_returns_stored_baseline_for_matching_version(self):
        baseline = {"version_number": 9, "pr_count": 5}
        project = make_project()
        project.design_doc = {DESIGN_DOC_KEY: {"9": baseline}}
        result = read_version_lane_baseline(project, 9)
        assert result == baseline

    def test_slimming_keeps_flat_convergence_scalars_without_lane_waste(self):
        baseline = {
            "version_number": 11,
            "outer_loop_convergence": {
                "cap_exhausted_direction_count": 2,
                "supersede_affected_reviews_to_converge_median": 3,
                "supersede_affected_reviews_to_converge_p95": 5,
            },
        }
        result = _slim_baseline(baseline)
        assert "lane_waste" not in result
        assert result["outer_loop_convergence"]["cap_exhausted_direction_count"] == 2
        assert result["outer_loop_convergence"]["supersede_affected_reviews_to_converge_median"] == 3
        assert result["outer_loop_convergence"]["supersede_affected_reviews_to_converge_p95"] == 5

    def test_slimming_keeps_retarget_convergence_counters(self):
        baseline = {
            "version_number": 11,
            "lane_waste": {
                "reviews_to_converge": {"count": 3, "median": 2, "p95": 5},
                "converged_review_counts": [1, 2, 3],
                "retarget_review_scope_counts": {"delta": 1, "full": 2},
                "stale_head_discard_count": 4,
                "chains": [{"task_count": 99}],
            },
        }

        result = _slim_baseline(baseline)

        lane_waste = result["lane_waste"]
        assert lane_waste["reviews_to_converge"]["median"] == 2
        assert "converged_review_counts" not in lane_waste
        assert lane_waste["retarget_review_scope_counts"] == {"delta": 1, "full": 2}
        assert lane_waste["stale_head_discard_count"] == 4
        assert "chains" not in lane_waste
        assert "dropped_fields" not in result

    def test_slim_retains_lane_waste_at_snapshot_chain_ceiling(self):
        """A 5,000-sample legacy payload cannot evict durable lane-waste data."""
        baseline = {
            "version_number": 12,
            "lane_waste": {
                "reviews_to_converge": {
                    "count": 5_000,
                    "median": 2_500.5,
                    "p95": 4_750,
                },
                "converged_review_counts": list(range(1, 5_001)),
                "retarget_review_scope_counts": {"delta": 1_234, "full": 567},
                "stale_head_discard_count": 89,
                "chains": [{"task_count": 99}] * 100,
            },
            "outer_loop_convergence": {
                "iteration_median": 3,
                "convergence_rate": 0.75,
            },
        }

        result = _slim_baseline(baseline)

        assert len(json.dumps(result).encode()) <= _MAX_BASELINE_ENTRY_BYTES
        lane_waste = result["lane_waste"]
        assert "converged_review_counts" not in lane_waste
        assert "chains" not in lane_waste
        assert lane_waste["reviews_to_converge"] == {
            "count": 5_000,
            "median": 2_500.5,
            "p95": 4_750,
        }
        assert lane_waste["retarget_review_scope_counts"] == {
            "delta": 1_234,
            "full": 567,
        }
        assert lane_waste["stale_head_discard_count"] == 89

    def test_overflow_shed_preserves_direction_grading_metrics(self):
        """Overflow records its eviction while retaining retarget evidence."""
        baseline = {
            "version_number": 13,
            "lane_waste": {
                "retarget_review_scope_counts": {"delta": 7, "full": 3},
                "stale_head_discard_count": 2,
                "future_detail": "x" * (_MAX_BASELINE_ENTRY_BYTES * 2),
            },
            "lane_iteration_distribution": {"review": {"p50_iterations": 2}},
        }

        result = _slim_baseline(baseline)

        assert len(json.dumps(result).encode()) <= _MAX_BASELINE_ENTRY_BYTES
        assert result["dropped_fields"] == [
            "lane_iteration_distribution",
            "lane_waste.future_detail",
        ]
        assert result["lane_waste"]["retarget_review_scope_counts"] == {
            "delta": 7,
            "full": 3,
        }
        assert result["lane_waste"]["stale_head_discard_count"] == 2

    def test_trend_provenance_distinguishes_evicted_absent_and_zero(self):
        """Trend consumers can tell eviction, no measurement, and a real zero apart."""
        from app.digest_metrics import compute_lane_baseline_trend

        latest = {
            "version_number": 13,
            "dropped_fields": ["lane_waste.retarget_review_scope_counts"],
            "lane_waste": {"stale_head_discard_count": 0},
        }
        prior = {
            "version_number": 12,
            "lane_waste": {
                "retarget_review_scope_counts": {"delta": 0, "full": 0},
                "stale_head_discard_count": 0,
            },
        }

        trend = compute_lane_baseline_trend([latest, prior])

        assert trend is not None
        delta = trend["retarget_convergence"]["retarget_review_scope_counts"]["delta"]
        assert delta["latest"] is None
        assert delta["prior"] == 0
        assert delta["availability"] == {
            "latest": "evicted",
            "prior": "available",
        }
        stale = trend["retarget_convergence"]["stale_head_discard_count"]
        assert stale["latest"] == 0
        assert stale["prior"] == 0
        assert stale["availability"] == {
            "latest": "available",
            "prior": "available",
        }
        recovery = trend["checkpoint_recovery_waste_total"]
        assert recovery["availability"] == {
            "latest": "unavailable",
            "prior": "unavailable",
        }


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


class TestPersistVersionLaneBaseline:
    @pytest.mark.asyncio
    async def test_persisted_convergence_reports_both_supersede_arms(
        self, db_session
    ):
        """Persisted convergence separates rollup supersede and control arms."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)
        original = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            created_at=opened - timedelta(days=1),
        )
        db_session.add(original)
        await db_session.flush()
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review", "pr_number": 902}],
                context={
                    "rollup_pr_number": 902,
                    "review_loop": {
                        "original_task_id": str(original.id),
                        "pr_number": 902,
                        "iteration": 1,
                        "superseded_pr_numbers": ["901"],
                    }
                },
                created_at=opened + timedelta(hours=1),
            )
        )
        unaffected_original = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True, "_is_rollup_review": True},
            created_at=opened - timedelta(days=1),
        )
        db_session.add(unaffected_original)
        await db_session.flush()
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review", "pr_number": 903}],
                context={
                    "rollup_pr_number": 903,
                    "review_loop": {
                        "original_task_id": str(unaffected_original.id),
                        "pr_number": 903,
                        "iteration": 1,
                    }
                },
                created_at=opened + timedelta(hours=2),
            )
        )
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        convergence = result["outer_loop_convergence"]
        assert convergence["supersede_affected_chain_count"] == 1
        assert convergence["supersede_affected_reviews_to_converge_count"] == 1
        assert convergence["supersede_affected_reviews_to_converge_median"] == 1
        assert convergence["rollup_chain_count"] == 2
        assert convergence["rollup_supersede_affected_chain_count"] == 1
        assert convergence["rollup_supersede_unaffected_chain_count"] == 1
        assert convergence["rollup_supersede_affected_reviews_to_converge_count"] == 1
        assert convergence["rollup_supersede_unaffected_reviews_to_converge_count"] == 1
        assert convergence["rollup_supersede_affected_reviews_to_converge_median"] == 1
        assert convergence["rollup_supersede_unaffected_reviews_to_converge_median"] == 1

    @pytest.mark.asyncio
    async def test_persist_keeps_supersede_percentiles_above_one_thousand_chains(
        self, db_session
    ):
        """The durable snapshot retains full-population convergence percentiles."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)
        tasks = []
        for chain_number in range(1_060):
            original = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
                created_at=opened + timedelta(hours=1),
            )
            tasks.append(original)
            review_count = 1 if chain_number < 1_000 else 10
            tasks.extend(
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "review", "pr_number": chain_number + 5_000}],
                    context={
                        "review_loop": {
                            "original_task_id": str(original.id),
                            "pr_number": chain_number + 5_000,
                            "iteration": iteration + 1,
                            "superseded_pr_numbers": [str(chain_number + 4_000)],
                        }
                    },
                    created_at=opened + timedelta(hours=2),
                )
                for iteration in range(review_count)
            )
        db_session.add_all(tasks)
        await db_session.commit()

        await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        stored = await _read_lane_baseline_row(db_session, project.id, 9)
        assert stored is not None
        convergence = stored["outer_loop_convergence"]
        assert convergence["supersede_affected_reviews_to_converge_count"] == 1_060
        assert convergence["supersede_affected_reviews_to_converge_sample_count"] == 1_060
        assert convergence["supersede_affected_reviews_to_converge_median"] == 1
        assert convergence["supersede_affected_reviews_to_converge_p95"] == 10

    @pytest.mark.asyncio
    async def test_version_baseline_persists_and_trends_convergence_counters(
        self, db_session
    ):
        """Closed-version snapshots retain canonical outer-loop counters for trend use."""
        from app.digest_metrics import compute_lane_baseline_trend

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        windows = [
            (9, now - timedelta(days=30), now - timedelta(days=20), 4, True),
            (10, now - timedelta(days=19), now - timedelta(days=10), 2, False),
        ]
        for version, opened, closed, merged_iteration, include_cap in windows:
            original = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={
                    "pr_merged": True,
                    "direction_id": f"direction-{version}",
                },
                created_at=opened + timedelta(hours=1),
            )
            db_session.add(original)
            await db_session.flush()
            iteration_tasks = [
                make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    payloads=[{"type": "review", "prompt": "baseline"}],
                    context={
                        "review_loop": {
                            "pr_number": (
                                str(version)
                                if iteration < merged_iteration
                                else f"{version}-replacement"
                            ),
                            "original_task_id": str(original.id),
                            "iteration": iteration,
                        }
                    },
                    created_at=opened + timedelta(hours=iteration + 1),
                )
                for iteration in range(1, merged_iteration + 1)
            ]
            db_session.add_all(iteration_tasks)
            if include_cap:
                cap_original = make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    context={"direction_id": f"direction-cap-{version}"},
                    created_at=opened + timedelta(hours=10),
                )
                db_session.add(cap_original)
                await db_session.flush()
                db_session.add(
                    make_task(
                        project_id=project.id,
                        status=TaskStatus.FAILED,
                        payloads=[{"type": "review", "prompt": "baseline"}],
                        context={
                            "review_loop": {
                                "pr_number": f"{version}-cap",
                                "original_task_id": str(cap_original.id),
                                "iteration": 16,
                            },
                            "failure_detail": "review_cap_exhausted",
                            "pr_rejected": True,
                        },
                        created_at=opened + timedelta(hours=11),
                    )
                )
            await db_session.commit()

            result = await persist_version_lane_baseline(
                db_session,
                project_id=project.id,
                version_number=version,
                opened_at=opened,
                closed_at=closed,
                terminal_status="promoted",
            )
            assert result is not None
            assert result["baseline_def_version"] == "v5"
            assert result["outer_loop_convergence"]["iteration_median"] is not None
            assert "chains" not in result["lane_waste"]

        latest = await _read_lane_baseline_row(db_session, project.id, 10)
        prior = await _read_lane_baseline_row(db_session, project.id, 9)
        assert latest is not None and prior is not None
        assert latest["outer_loop_convergence"]["iteration_median"] == 2
        assert prior["outer_loop_convergence"]["cap_exhausted_chain_count"] == 1
        assert prior["outer_loop_convergence"]["cap_exhausted_direction_count"] == 1
        assert prior["outer_loop_convergence"]["supersede_affected_chain_count"] == 1
        assert prior["outer_loop_convergence"]["supersede_affected_reviews_to_converge_count"] == 1
        assert prior["outer_loop_convergence"]["supersede_affected_reviews_to_converge_sample_count"] == 1
        assert prior["outer_loop_convergence"]["supersede_affected_reviews_to_converge_median"] == 4
        assert prior["outer_loop_convergence"]["supersede_affected_reviews_to_converge_p95"] == 4
        assert prior["outer_loop_convergence"]["terminal_outcome_counts"]["cap_exhausted"] == 1

        trend = compute_lane_baseline_trend([latest, prior])
        assert trend is not None
        assert trend["outer_loop_convergence"]["iteration_median"]["classification"] == "improving"
        assert trend["outer_loop_convergence"]["convergence_rate"]["latest"] == 1.0
        assert trend["outer_loop_convergence"]["convergence_rate"]["classification"] == "improving"
        assert trend["outer_loop_convergence"]["cap_exhausted_chain_count"]["delta"] == -1
        assert trend["outer_loop_convergence"]["cap_exhausted_direction_count"]["delta"] == -1
        assert trend["outer_loop_convergence"]["supersede_affected_reviews_to_converge_median"]["classification"] == "improving"

    @pytest.mark.asyncio
    async def test_persist_writes_baseline_to_table(self, db_session):
        """persist_version_lane_baseline stores a non-None dict in the dedicated
        project_lane_baseline table without adding a new design_doc copy."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Add a review-loop task in the window
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "review", "prompt": "x"}],
            context={"review_loop": {"pr_number": "101"}},
            created_at=opened + timedelta(days=1),
            started_at=opened + timedelta(days=1),
            status_log=[
                {
                    "s": "COMPLETED",
                    "t": (opened + timedelta(days=1, hours=1)).isoformat(),
                }
            ],
        )
        db_session.add(task)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        assert result["version_number"] == 9
        assert result["terminal_status"] == "promoted"
        assert "lane_iteration_distribution" in result
        assert "lane_waste" in result
        assert "relay_medians" in result
        assert "outer_loop_convergence" in result
        assert result["pr_count"] == 1
        # Bloat sub-blobs must not be stored
        assert "relay_counts_by_lane" not in result
        assert "per_pr_phase_breakdown" not in result
        assert "chains" not in result.get("lane_waste", {})

        # Verify it landed in the dedicated table (not design_doc).
        stored = await _read_lane_baseline_row(db_session, project.id, 9)
        assert stored is not None
        assert stored["pr_count"] == 1
        await db_session.refresh(project)
        assert DESIGN_DOC_KEY not in (project.design_doc or {})

    @pytest.mark.asyncio
    async def test_persist_first_write_wins(self, db_session):
        """A second persist call for the same version returns the original baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        first = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()
        first_captured_at = first["captured_at"]

        # Second call should return the already-stored entry unchanged
        second = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        assert second is not None
        assert second["captured_at"] == first_captured_at

    @pytest.mark.asyncio
    async def test_persist_no_tasks_produces_zero_counts(self, db_session):
        """A version window with no tasks stores a valid zero-count baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=5,
            opened_at=now - timedelta(days=30),
            closed_at=now - timedelta(days=23),
            terminal_status="abandoned",
        )
        await db_session.commit()

        assert result is not None
        assert result["pr_count"] == 0
        assert result["task_count"] == 0
        assert result["terminal_status"] == "abandoned"
        relay_medians = result["relay_medians"]
        assert relay_medians["pr_count"] == 0
        assert relay_medians["wall_clock_seconds"] is None

    @pytest.mark.asyncio
    async def test_persist_history_cap_evicts_oldest(self, db_session):
        """Storing more than _MAX_BASELINES_STORED evicts the oldest versions.

        Retention is now enforced by the dedicated project_lane_baseline table's
        accessor (_LANE_BASELINE_VERSIONS_MAX), not a design_doc dict cap.
        """
        from testing.conftest import USE_POSTGRES

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline retention uses Postgres ON CONFLICT")

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        # Pre-populate the table with _MAX_BASELINES_STORED versions directly.
        for i in range(1, _MAX_BASELINES_STORED + 1):
            await _upsert_lane_baseline_row(
                db_session,
                project.id,
                i,
                now,
                "v1",
                {"version_number": i, "captured_at": now.isoformat()},
            )
        await db_session.commit()

        # Adding one more should evict version 1 (the lowest version number).
        new_version = _MAX_BASELINES_STORED + 1
        await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=new_version,
            opened_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=1),
            terminal_status="promoted",
        )
        await db_session.commit()

        assert await _read_lane_baseline_row(db_session, project.id, 1) is None
        assert (
            await _read_lane_baseline_row(db_session, project.id, new_version)
            is not None
        )

    @pytest.mark.asyncio
    async def test_persist_for_nonexistent_project_returns_none(self, db_session):
        """Persisting for a project that doesn't exist returns None without error."""
        now = _utcnow()
        result = await persist_version_lane_baseline(
            db_session,
            project_id=uuid.uuid4(),
            version_number=1,
            opened_at=now - timedelta(days=7),
            closed_at=now,
            terminal_status="promoted",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_persist_bounds_entry_bytes_preserving_readback(self, db_session):
        """High-volume baseline stays under byte budget with all required fields intact."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Create many review-loop tasks across many PRs to produce large
        # per_pr_phase_breakdown and lane_waste chains before slimming.
        for pr_num in range(1, 51):
            for _ in range(3):
                task = make_task(
                    project_id=project.id,
                    status=TaskStatus.COMPLETED,
                    created_by="ORCHESTRATOR",
                    payloads=[{"type": "review", "prompt": "x" * 50}],
                    context={"review_loop": {"pr_number": str(pr_num)}},
                    created_at=opened + timedelta(hours=pr_num),
                    started_at=opened + timedelta(hours=pr_num),
                    status_log=[
                        {
                            "s": "COMPLETED",
                            "t": (
                                opened + timedelta(hours=pr_num, minutes=30)
                            ).isoformat(),
                        }
                    ],
                )
                db_session.add(task)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=12,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None

        # (a) per-entry byte budget
        entry_bytes = len(json.dumps(result, default=str).encode())
        assert entry_bytes <= _MAX_BASELINE_ENTRY_BYTES, (
            f"Stored entry is {entry_bytes} bytes, exceeds {_MAX_BASELINE_ENTRY_BYTES}"
        )

        # (b) the stored table row is under the per-entry byte budget
        stored_row = await _read_lane_baseline_row(db_session, project.id, 12)
        assert stored_row is not None
        row_bytes = len(json.dumps(stored_row, default=str).encode())
        assert row_bytes <= _MAX_BASELINE_ENTRY_BYTES, (
            f"Stored table row is {row_bytes} bytes, exceeds {_MAX_BASELINE_ENTRY_BYTES}"
        )

        # (c) all required readback fields present
        assert "relay_medians" in result
        assert "lane_iteration_distribution" in result
        assert "checkpoint_recovery_waste" in result
        assert "lane_waste" in result
        lw = result["lane_waste"]
        assert "waste_taxonomy" in lw
        assert "waste_task_count" in lw
        assert "version_number" in result
        assert "captured_at" in result
        assert "terminal_status" in result
        assert "pr_count" in result
        assert "task_count" in result

        # (d) bloat sub-blobs dropped
        assert "per_pr_phase_breakdown" not in result
        assert "relay_counts_by_lane" not in result
        assert "chains" not in lw

    # NOTE: the former test_persist_self_heals_existing_bloated_entries was
    # removed with the table-writer cutover: new baselines live one-per-row in
    # project_lane_baseline and each row is slimmed at write time, so there is
    # no shared design_doc dict to re-slim on subsequent writes. The per-write
    # slimming is still covered by
    # test_persist_bounds_entry_bytes_preserving_readback above.


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------


class TestBackfillMissingLaneBaselines:
    @pytest.mark.asyncio
    async def test_backfill_persists_missing_baselines(self, db_session):
        """backfill_missing_lane_baselines writes baselines for closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        v9_opened = now - timedelta(days=14)
        v9_promoted = now - timedelta(days=7)

        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            status="promoted",
            opened_at=v9_opened,
            promoted_at=v9_promoted,
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [9]

        stored = await _read_lane_baseline_row(db_session, project.id, 9)
        assert stored is not None
        assert stored["version_number"] == 9
        assert stored["terminal_status"] == "promoted"
        assert "relay_medians" in stored

    @pytest.mark.asyncio
    @pytest.mark.parametrize("definition", ["v5", "v6"])
    async def test_backfill_skips_already_stored_version(self, db_session, definition):
        """Current table definitions remain first-write-wins during backfill."""
        from testing.conftest import USE_POSTGRES

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline gap check uses Postgres ON CONFLICT")

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        # Seed the existing baseline directly into the table.
        await _upsert_lane_baseline_row(
            db_session,
            project.id,
            9,
            now,
            definition,
            {"version_number": 9, "captured_at": "2026-01-01T00:00:00"},
        )
        version = ReleaseVersion(
            project_id=project.id,
            version_number=9,
            status="promoted",
            opened_at=now - timedelta(days=14),
            promoted_at=now - timedelta(days=7),
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == []
        # Original entry must be unchanged (first-write-wins).
        stored = await _read_lane_baseline_row(db_session, project.id, 9)
        assert stored["captured_at"] == "2026-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_backfill_preserves_legacy_design_doc_version(self, db_session):
        """A refresh leaves the retained legacy fallback untouched."""
        legacy = {
            "version_number": 9,
            "captured_at": "2026-01-01T00:00:00",
            "terminal_status": "promoted",
        }
        project = make_project(design_doc={DESIGN_DOC_KEY: {"9": legacy}})
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        db_session.add(
            ReleaseVersion(
                project_id=project.id,
                version_number=9,
                status="promoted",
                opened_at=now - timedelta(days=14),
                promoted_at=now - timedelta(days=7),
            )
        )
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [9]
        refreshed = await _read_lane_baseline_row(db_session, project.id, 9)
        assert refreshed is not None
        assert refreshed["baseline_def_version"] == "v5"
        await db_session.refresh(project)
        assert project.design_doc[DESIGN_DOC_KEY]["9"] == legacy

    @pytest.mark.asyncio
    async def test_backfill_refreshes_stale_definition_for_rollup_arms(self, db_session):
        """Stale retained rows gain rollup arms and produce a usable trend."""
        from testing.conftest import USE_POSTGRES
        from app.digest_metrics import compute_lane_baseline_trend

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline refresh uses Postgres row metadata")

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        windows = [
            (9, now - timedelta(days=30), now - timedelta(days=20), 1),
            (10, now - timedelta(days=19), now - timedelta(days=10), 2),
        ]
        for version_number, opened, closed, review_count in windows:
            original = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
                created_at=opened - timedelta(hours=1),
            )
            db_session.add(original)
            await db_session.flush()
            db_session.add_all(
                [
                    make_task(
                        project_id=project.id,
                        status=TaskStatus.COMPLETED,
                        payloads=[
                            {"type": "review", "pr_number": str(version_number)}
                        ],
                        context={
                            "rollup_pr_number": version_number,
                            "review_loop": {
                                "original_task_id": str(original.id),
                                "pr_number": version_number,
                                "iteration": iteration,
                                "superseded_pr_numbers": [version_number - 1],
                            },
                        },
                        created_at=opened + timedelta(hours=iteration),
                    )
                    for iteration in range(1, review_count + 1)
                ]
            )
            db_session.add(
                ReleaseVersion(
                    project_id=project.id,
                    version_number=version_number,
                    status="promoted",
                    opened_at=opened,
                    promoted_at=closed,
                )
            )
            await _upsert_lane_baseline_row(
                db_session,
                project.id,
                version_number,
                now,
                "v4",
                {
                    "version_number": version_number,
                    "captured_at": "2026-01-01T00:00:00",
                    "baseline_def_version": "v4",
                },
            )
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [10, 9]
        latest = await _read_lane_baseline_row(db_session, project.id, 10)
        prior = await _read_lane_baseline_row(db_session, project.id, 9)
        assert latest is not None and prior is not None
        assert latest["baseline_def_version"] == "v5"
        assert prior["baseline_def_version"] == "v5"
        assert (
            latest["outer_loop_convergence"]
            ["rollup_supersede_affected_reviews_to_converge_median"]
            == 2
        )
        assert (
            prior["outer_loop_convergence"]
            ["rollup_supersede_affected_reviews_to_converge_median"]
            == 1
        )

        trend = compute_lane_baseline_trend([latest, prior])
        assert trend is not None
        assert (
            trend["outer_loop_convergence"]
            ["rollup_supersede_affected_reviews_to_converge_median"]["delta"]
            == 1
        )
        assert (
            trend["outer_loop_convergence"]
            ["rollup_supersede_affected_reviews_to_converge_p95"]["classification"]
            == "declining"
        )

    @pytest.mark.asyncio
    async def test_backfill_respects_max_versions_cap(self, db_session):
        """backfill only processes the most recent max_versions closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        for vn in range(1, 6):  # 5 closed versions
            v = ReleaseVersion(
                project_id=project.id,
                version_number=vn,
                status="promoted",
                opened_at=now - timedelta(days=10 + vn),
                promoted_at=now - timedelta(days=vn),
            )
            db_session.add(v)
        await db_session.commit()

        # Backfill with max_versions=3 — should only process the 3 most recent
        backfilled = await backfill_missing_lane_baselines(
            project_id=project.id, max_versions=3
        )

        assert len(backfilled) == 3
        assert set(backfilled) == {3, 4, 5}

    @pytest.mark.asyncio
    async def test_backfill_handles_abandoned_version_closed_at(self, db_session):
        """backfill derives closed_at from last_qualification["abandoned_at"] for abandoned versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        abandoned_iso = (now - timedelta(days=3)).isoformat()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=7,
            status="abandoned",
            opened_at=now - timedelta(days=10),
            last_qualification={"abandoned_at": abandoned_iso, "reason": "operator"},
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == [7]
        stored = await _read_lane_baseline_row(db_session, project.id, 7)
        assert stored is not None
        assert stored["terminal_status"] == "abandoned"

    @pytest.mark.asyncio
    async def test_backfill_no_closed_versions_is_noop(self, db_session):
        """backfill returns an empty list when the project has no closed versions."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        # Only an open version
        now = _utcnow()
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            status="open",
            opened_at=now - timedelta(days=3),
        )
        db_session.add(version)
        await db_session.commit()

        backfilled = await backfill_missing_lane_baselines(project_id=project.id)

        assert backfilled == []

    @pytest.mark.asyncio
    async def test_backfill_unknown_project_returns_empty(self, db_session):
        """backfill returns an empty list without error for a nonexistent project."""
        backfilled = await backfill_missing_lane_baselines(project_id=uuid.uuid4())
        assert backfilled == []


# ---------------------------------------------------------------------------
# Checkpoint recovery waste tests
# ---------------------------------------------------------------------------


class TestComputeCheckpointRecoveryWaste:
    def test_no_recovery_tasks_returns_zero_totals(self):
        """A version with no recovery tasks produces zero counts."""
        tasks = [make_task(context={"review_loop": {"pr_number": "1"}})]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result == {"by_reason": {}, "total": 0, "exhausted_count": 0}

    def test_single_recovery_task_counted(self):
        """A single recovery task is counted under its reason."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 1
        assert result["by_reason"] == {"checkpoint_missing": 1}
        assert result["exhausted_count"] == 0

    def test_multiple_tasks_same_reason_aggregated(self):
        """Multiple recovery tasks with the same reason are summed."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={"review_loop": {"pr_number": "99"}}),  # non-recovery
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 2
        assert result["by_reason"] == {"checkpoint_missing": 2}

    def test_multiple_reasons_grouped_separately(self):
        """Tasks with distinct reason values form separate by_reason buckets."""
        tasks = [
            make_task(context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"}),
            make_task(context={LANE_RECOVERY_REASON_KEY: "some_other_reason"}),
        ]
        result = _compute_checkpoint_recovery_waste(tasks)
        assert result["total"] == 2
        assert result["by_reason"]["checkpoint_missing"] == 1
        assert result["by_reason"]["some_other_reason"] == 1

    def test_empty_task_list_returns_zero(self):
        """Empty task list returns all-zero result."""
        result = _compute_checkpoint_recovery_waste([])
        assert result == {"by_reason": {}, "total": 0, "exhausted_count": 0}

    def test_task_with_none_context_skipped(self):
        """Tasks with None context are skipped without error."""
        task = make_task()
        task.context = None
        result = _compute_checkpoint_recovery_waste([task])
        assert result["total"] == 0

    def test_exhausted_count_from_lane_rows(self):
        """Exhausted checkpoint-recovery kills are counted from KILLED lane rows."""
        from app.models import ImprovementLane

        # Create mock lanes in KILLED status with exhausted recovery markers
        lane_exhausted = ImprovementLane(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="exhausted lane",
            goal="test",
            branch_name="test-branch",
            base_branch="main",
            status="KILLED",
            manifest={
                "recovery": {
                    "reason": "checkpoint_recovery_exhausted",
                    "attempt": 3,
                    "exhausted": True,
                    "at": _utcnow().isoformat(),
                }
            },
        )
        lane_not_exhausted = ImprovementLane(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="active lane",
            goal="test",
            branch_name="test-branch",
            base_branch="main",
            status="ACTIVE",
            manifest={
                "recovery": {
                    "reason": "checkpoint_missing",
                    "attempt": 1,
                    "exhausted": False,
                    "at": _utcnow().isoformat(),
                }
            },
        )
        lane_killed_no_recovery = ImprovementLane(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            title="killed lane no recovery",
            goal="test",
            branch_name="test-branch",
            base_branch="main",
            status="KILLED",
            manifest={},
        )

        result = _compute_checkpoint_recovery_waste(
            [], lanes=[lane_exhausted, lane_not_exhausted, lane_killed_no_recovery]
        )
        assert result["exhausted_count"] == 1
        assert result["total"] == 0
        assert result["by_reason"] == {}


class TestPersistCheckpointRecoveryWasteInBaseline:
    @pytest.mark.asyncio
    async def test_persist_includes_checkpoint_recovery_waste(self, db_session):
        """persist_version_lane_baseline folds checkpoint_recovery_waste into baseline."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Two recovery tasks in the version window
        for _ in range(2):
            task = make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[{"type": "fix", "prompt": "recover"}],
                context={LANE_RECOVERY_REASON_KEY: "checkpoint_missing"},
                created_at=opened + timedelta(hours=2),
                started_at=opened + timedelta(hours=2),
                status_log=[
                    {
                        "s": "COMPLETED",
                        "t": (opened + timedelta(hours=3)).isoformat(),
                    }
                ],
            )
            db_session.add(task)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=10,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        crw = result.get("checkpoint_recovery_waste")
        assert crw is not None
        assert crw["total"] == 2
        assert crw["by_reason"] == {"checkpoint_missing": 2}
        assert crw["exhausted_count"] == 0

    @pytest.mark.asyncio
    async def test_persist_no_recovery_tasks_zero_waste(self, db_session):
        """A version window with no recovery tasks produces a zero-waste block."""
        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=11,
            opened_at=now - timedelta(days=14),
            closed_at=now - timedelta(days=7),
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        crw = result.get("checkpoint_recovery_waste")
        assert crw is not None
        assert crw["total"] == 0
        assert crw["by_reason"] == {}
        assert crw["exhausted_count"] == 0

    @pytest.mark.asyncio
    async def test_persist_counts_exhausted_lane_kills(self, db_session):
        """persist_version_lane_baseline counts KILLED lanes with exhausted recovery markers."""
        from app.models import ImprovementLane

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        opened = now - timedelta(days=7)
        closed = now - timedelta(days=1)

        # Create a KILLED lane with exhausted recovery marker
        lane = ImprovementLane(
            project_id=project.id,
            title="exhausted lane",
            goal="test goal",
            branch_name="test-branch",
            base_branch="main",
            status="KILLED",
            created_at=opened + timedelta(hours=1),
            manifest={
                "recovery": {
                    "reason": "checkpoint_recovery_exhausted",
                    "attempt": 3,
                    "exhausted": True,
                    "at": (opened + timedelta(hours=2)).isoformat(),
                }
            },
        )
        db_session.add(lane)

        # Create another KILLED lane WITHOUT exhausted marker (should not count)
        lane_no_exhausted = ImprovementLane(
            project_id=project.id,
            title="killed lane no exhausted",
            goal="test goal",
            branch_name="other-branch",
            base_branch="main",
            status="KILLED",
            created_at=opened + timedelta(hours=3),
            manifest={},
        )
        db_session.add(lane_no_exhausted)
        await db_session.commit()

        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=12,
            opened_at=opened,
            closed_at=closed,
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        crw = result.get("checkpoint_recovery_waste")
        assert crw is not None
        assert crw["exhausted_count"] == 1
        assert crw["total"] == 0  # No recovery tasks, only exhausted kills from lanes
        assert crw["by_reason"] == {}


# ---------------------------------------------------------------------------
# Dual-write to project_lane_baseline (design_doc God-column decomposition)
# ---------------------------------------------------------------------------


class TestLaneBaselineDualWrite:
    @pytest.mark.asyncio
    async def test_persist_writes_only_to_lane_baseline_table(self, db_session):
        """persist_version_lane_baseline upserts a row into project_lane_baseline
        and no longer copies new rows into design_doc; historical fallback data
        remains readable until a future explicit migration retires it."""
        from testing.conftest import USE_POSTGRES

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline write uses Postgres ON CONFLICT")

        project = make_project()
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        result = await persist_version_lane_baseline(
            db_session,
            project_id=project.id,
            version_number=9,
            opened_at=now - timedelta(days=7),
            closed_at=now - timedelta(days=1),
            terminal_status="promoted",
        )
        await db_session.commit()

        assert result is not None
        # The dedicated table carries the slimmed blob keyed by version.
        table_blob = await _read_lane_baseline_row(db_session, project.id, 9)
        assert table_blob is not None
        assert table_blob["version_number"] == 9
        assert table_blob["terminal_status"] == "promoted"
        # design_doc must NOT carry it (the write is a move, not a dual-write).
        await db_session.refresh(project)
        assert DESIGN_DOC_KEY not in (project.design_doc or {})
        assert read_version_lane_baseline(project, 9) is None

    @pytest.mark.asyncio
    async def test_preferring_table_reads_table_only_baseline(self, db_session):
        """read_version_lane_baseline_preferring_table returns a table-only baseline."""
        from testing.conftest import USE_POSTGRES

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline dual-write uses Postgres ON CONFLICT")

        from app.lane_baseline_snapshot import (
            read_version_lane_baseline_preferring_table,
        )
        from app.project_telemetry import upsert_lane_baseline

        project = make_project()
        project.design_doc = {}  # design_doc has NO lane_baselines
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        await upsert_lane_baseline(
            db_session, project.id, 7, now, "v1", {"version_number": 7, "m": "table"}
        )
        await db_session.commit()
        await db_session.refresh(project)

        got = await read_version_lane_baseline_preferring_table(
            db_session, project, 7
        )
        assert got == {"version_number": 7, "m": "table"}

    @pytest.mark.asyncio
    async def test_preferring_table_falls_back_to_design_doc(self, db_session):
        """When the table has no row, the design_doc blob is returned."""
        project = make_project()
        project.design_doc = {
            DESIGN_DOC_KEY: {"3": {"version_number": 3, "m": "doc"}}
        }
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        from app.lane_baseline_snapshot import (
            read_version_lane_baseline_preferring_table,
        )

        got = await read_version_lane_baseline_preferring_table(
            db_session, project, 3
        )
        assert got == {"version_number": 3, "m": "doc"}
        # Absent version -> None.
        assert (
            await read_version_lane_baseline_preferring_table(db_session, project, 99)
            is None
        )

    @pytest.mark.asyncio
    async def test_read_all_preferring_table_merges_and_keys_per_version(
        self, db_session
    ):
        """read_all merges table rows over design_doc, keyed by version_number."""
        from testing.conftest import USE_POSTGRES

        if not USE_POSTGRES:
            pytest.skip("project_lane_baseline dual-write uses Postgres ON CONFLICT")

        from app.lane_baseline_snapshot import (
            read_all_lane_baselines_preferring_table,
        )
        from app.project_telemetry import upsert_lane_baseline

        project = make_project()
        # design_doc has v1 (doc-only) and v2 (also in table); table has v2, v3.
        design_doc = {
            DESIGN_DOC_KEY: {
                "1": {"version_number": 1, "src": "doc"},
                "2": {"version_number": 2, "src": "doc"},
            }
        }
        project.design_doc = design_doc
        db_session.add(project)
        await db_session.commit()

        now = _utcnow()
        await upsert_lane_baseline(
            db_session, project.id, 2, now, "v1", {"version_number": 2, "src": "table"}
        )
        await upsert_lane_baseline(
            db_session, project.id, 3, now, "v1", {"version_number": 3, "src": "table"}
        )
        await db_session.commit()

        merged = await read_all_lane_baselines_preferring_table(
            db_session, project.id, design_doc
        )
        # Per-version keying preserved; table wins for v2; doc-only v1 kept.
        assert set(merged.keys()) == {"1", "2", "3"}
        assert merged["1"]["src"] == "doc"
        assert merged["2"]["src"] == "table"
        assert merged["3"]["src"] == "table"

    @pytest.mark.asyncio
    async def test_read_all_preferring_table_empty_table_uses_design_doc(
        self, db_session
    ):
        """With an empty table, read_all returns exactly the design_doc versions."""
        from app.lane_baseline_snapshot import (
            read_all_lane_baselines_preferring_table,
        )

        project = make_project()
        design_doc = {DESIGN_DOC_KEY: {"5": {"version_number": 5, "src": "doc"}}}
        project.design_doc = design_doc
        db_session.add(project)
        await db_session.commit()

        merged = await read_all_lane_baselines_preferring_table(
            db_session, project.id, design_doc
        )
        assert merged == {"5": {"version_number": 5, "src": "doc"}}
