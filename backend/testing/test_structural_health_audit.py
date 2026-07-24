"""Tests for the structural-health audit lane (fat-row foundation).

Covers:
* the bounded DB-side measurement (ORDER BY size DESC LIMIT N, ids + byte size
  only — never loads the full context payload);
* mode gating — off enrolls nothing, observe records-but-enrolls-nothing,
  enforce enrolls;
* threshold trip enrolls EXACTLY ONE deduped finding, and a second cycle
  re-detecting the same row enrolls nothing (stable finding_key dedup);
* the leader-only recurring sweep's mode + cadence gating.
"""

import app.structural_health_audit as sha
from app.evergreen_backlog import enroll_backlog_item
from app.models import EvergreenBacklogItem, Project, Task
from app.shared_state import runtime_config
from app.structural_health_audit import (
    FAT_ROW_FINDING_KEY,
    STRUCTURAL_AUDIT_PROVENANCE,
    measure_largest_context_rows,
    run_fat_row_check,
    run_structural_health_audit,
)
from sqlmodel import select


async def _make_project(db_session, **kw) -> Project:
    project = Project(
        name=kw.pop("name", "structural-proj"), user_id="test-user-123", **kw
    )
    db_session.add(project)
    await db_session.flush()
    return project


async def _make_task(db_session, project, *, blob_bytes: int) -> Task:
    """Create a task whose context serializes to roughly ``blob_bytes`` bytes."""
    task = Task(project_id=project.id, context={"blob": "A" * max(0, blob_bytes)})
    db_session.add(task)
    await db_session.flush()
    return task


def setup_function(_func):
    # Each test gets a clean observability singleton + cadence gate + config.
    sha._reset_observability_for_tests()
    sha._last_audit_scan = None
    runtime_config.pop("structural_health_audit_mode", None)
    runtime_config.pop("structural_health_audit_interval_minutes", None)
    runtime_config.pop("structural_health_audit_fat_row_bytes", None)


async def _enrolled_fat_row_items(db_session, project_id) -> list[EvergreenBacklogItem]:
    return list(
        (
            await db_session.execute(
                select(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project_id,
                    EvergreenBacklogItem.finding_key == FAT_ROW_FINDING_KEY,
                )
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# Bounded DB-side measurement.
# --------------------------------------------------------------------------- #


async def test_measure_is_bounded_and_ordered_desc(db_session):
    project = await _make_project(db_session)
    # Create more rows than the limit, with strictly increasing context sizes.
    for n in (100, 500, 2000, 8000):
        await _make_task(db_session, project, blob_bytes=n)

    results = await measure_largest_context_rows(db_session, limit=2)
    # Bounded: never more than the limit, regardless of table size.
    assert len(results) == 2
    # Ordered biggest-first.
    assert results[0].bytes >= results[1].bytes
    # The largest row dominates the small ones.
    assert results[0].bytes >= 8000
    # Measurement carries ids + a byte size only — no context payload field.
    m = results[0]
    assert m.task_id and m.project_id and isinstance(m.bytes, int)
    assert not hasattr(m, "context")


async def test_measure_computes_real_byte_sizes(db_session):
    project = await _make_project(db_session)
    small = await _make_task(db_session, project, blob_bytes=10)
    big = await _make_task(db_session, project, blob_bytes=5000)

    by_id = {m.task_id: m.bytes for m in await measure_largest_context_rows(db_session)}
    assert by_id[str(big.id)] > by_id[str(small.id)]
    # The big row's measured size is in the right ballpark (>= the blob length).
    assert by_id[str(big.id)] >= 5000


# --------------------------------------------------------------------------- #
# Mode gating + threshold trip + dedup.
# --------------------------------------------------------------------------- #


async def test_enforce_threshold_trip_enrolls_exactly_one(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)

    decision = await run_fat_row_check(db_session, mode="enforce", threshold_bytes=1000)
    assert decision["offenders"] >= 1
    assert decision["enrolled"] == 1

    items = await _enrolled_fat_row_items(db_session, project.id)
    assert len(items) == 1
    assert items[0].finding_key == FAT_ROW_FINDING_KEY
    assert items[0].provenance == STRUCTURAL_AUDIT_PROVENANCE
    assert items[0].status == "enrolled"


async def test_enforce_second_cycle_does_not_reenroll(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)

    first = await run_fat_row_check(db_session, mode="enforce", threshold_bytes=1000)
    assert first["enrolled"] == 1

    second = await run_fat_row_check(db_session, mode="enforce", threshold_bytes=1000)
    # Re-detecting the SAME structural finding enrolls nothing (stable key dedup).
    assert second["offenders"] >= 1
    assert second["enrolled"] == 0
    assert len(await _enrolled_fat_row_items(db_session, project.id)) == 1


async def test_observe_records_but_enrolls_nothing(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)

    decision = await run_fat_row_check(db_session, mode="observe", threshold_bytes=1000)
    # It measured the would-be finding...
    assert decision["offenders"] >= 1
    # ...but enrolled nothing.
    assert decision["enrolled"] == 0
    assert await _enrolled_fat_row_items(db_session, project.id) == []
    # Observability recorded the measurement + decision.
    snap = sha.get_structural_health_observability()
    assert snap["cycles"] == 1
    assert snap["last_decision"]["offenders"] >= 1
    assert snap["last_decision"]["enrolled"] == 0
    assert snap["largest_rows"]


async def test_below_threshold_enrolls_nothing(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=50)

    decision = await run_fat_row_check(
        db_session, mode="enforce", threshold_bytes=1_000_000
    )
    assert decision["offenders"] == 0
    assert decision["enrolled"] == 0
    assert await _enrolled_fat_row_items(db_session, project.id) == []


async def test_enforce_one_finding_per_project_even_with_many_fat_rows(db_session):
    project = await _make_project(db_session)
    for _ in range(3):
        await _make_task(db_session, project, blob_bytes=5000)

    decision = await run_fat_row_check(db_session, mode="enforce", threshold_bytes=1000)
    assert decision["offenders"] >= 3
    # Deduped by (project_id, finding_key): exactly one ledger row for the class.
    assert decision["enrolled"] == 1
    assert len(await _enrolled_fat_row_items(db_session, project.id)) == 1


async def test_reopened_retired_finding_is_requeued(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)
    # Pre-existing retired finding for the same key.
    item = await enroll_backlog_item(
        db_session,
        project_id=project.id,
        finding_key=FAT_ROW_FINDING_KEY,
        title="old",
        provenance=STRUCTURAL_AUDIT_PROVENANCE,
    )
    item.status = "retired"
    await db_session.flush()

    decision = await run_fat_row_check(db_session, mode="enforce", threshold_bytes=1000)
    # A retired finding is NOT "already enrolled" — it is re-opened (requeued).
    assert decision["enrolled"] == 1
    items = await _enrolled_fat_row_items(db_session, project.id)
    assert len(items) == 1
    assert items[0].status == "enrolled"


# --------------------------------------------------------------------------- #
# Leader-only recurring sweep: mode + cadence gating.
# --------------------------------------------------------------------------- #


async def test_sweep_default_mode_is_observe(db_session):
    """When no runtime_config key is set, DEFAULT_AUDIT_MODE ('observe') is used.
    The sweep must run (return non-None), record observations, and enroll nothing."""
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)
    # setup_function already popped the key — no explicit mode set.
    runtime_config["structural_health_audit_fat_row_bytes"] = 1000

    result = await run_structural_health_audit(db_session)
    assert result is not None, "observe mode must run the sweep (not return None)"
    assert result["fat_row"]["enrolled"] == 0, "observe mode must not enroll findings"
    assert await _enrolled_fat_row_items(db_session, project.id) == []
    snap = sha.get_structural_health_observability()
    assert snap["cycles"] >= 1
    assert snap["last_decision"]["offenders"] >= 1


async def test_sweep_mode_off_is_inert(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)
    runtime_config["structural_health_audit_mode"] = "off"

    result = await run_structural_health_audit(db_session)
    assert result is None
    assert await _enrolled_fat_row_items(db_session, project.id) == []


async def test_sweep_enforce_enrolls(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)
    runtime_config["structural_health_audit_mode"] = "enforce"
    runtime_config["structural_health_audit_fat_row_bytes"] = 1000

    result = await run_structural_health_audit(db_session)
    assert result is not None
    # Assert on the fat_row sub-result: other checks (GIN overhead, etc.) may also
    # enroll in a fresh test DB (GIN index : table ratio is high for a single-row table),
    # so total_enrolled is environment-dependent.
    assert result["fat_row"]["enrolled"] == 1
    assert len(await _enrolled_fat_row_items(db_session, project.id)) == 1


async def test_sweep_cadence_gate_skips_within_interval(db_session):
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)
    runtime_config["structural_health_audit_mode"] = "observe"
    runtime_config["structural_health_audit_interval_minutes"] = 60

    first = await run_structural_health_audit(db_session)
    assert first is not None
    # A second immediate call is gated out by the cadence timestamp.
    second = await run_structural_health_audit(db_session)
    assert second is None


# --------------------------------------------------------------------------- #
# Default mode configuration.
# --------------------------------------------------------------------------- #


async def test_default_mode_is_observe(db_session):
    """When STRUCTURAL_HEALTH_AUDIT_MODE env var is unset, the default mode is
    'observe' and the observe-mode code path runs without errors and enrolls nothing."""
    project = await _make_project(db_session)
    await _make_task(db_session, project, blob_bytes=5000)

    # setup_function pops the mode from runtime_config, so we don't explicitly set it.
    # This test verifies that run_structural_health_audit defaults to 'observe' mode.
    # We set the threshold to detect the fat row we created above.
    runtime_config["structural_health_audit_fat_row_bytes"] = 1000
    result = await run_structural_health_audit(db_session)

    # With default observe mode, the audit runs and records the decision...
    assert result is not None
    assert result["fat_row"]["offenders"] >= 1
    # ...but enrolls nothing.
    assert result["fat_row"]["enrolled"] == 0
    assert await _enrolled_fat_row_items(db_session, project.id) == []
    # Observability recorded the measurement + decision.
    snap = sha.get_structural_health_observability()
    assert snap["cycles"] >= 1
    assert snap["last_decision"]["offenders"] >= 1
    assert snap["last_decision"]["enrolled"] == 0


# --------------------------------------------------------------------------- #
# Admin endpoint: structural-health-observability.
# --------------------------------------------------------------------------- #


async def test_structural_health_observability_endpoint(client, db_session):
    """GET /api/admin/evergreen/structural-health-observability returns 200 with
    the expected snapshot keys (matches the parallel fill-observability convention)."""
    # Run one observe cycle so the singleton has non-trivial data.
    project = await _make_project(db_session, name="obs-endpoint-proj")
    await _make_task(db_session, project, blob_bytes=5000)
    await db_session.commit()

    runtime_config["structural_health_audit_mode"] = "observe"
    runtime_config["structural_health_audit_fat_row_bytes"] = 1000
    await run_structural_health_audit(db_session)
    # Commit so the AuditLaneDecisionLog row is visible to the HTTP endpoint's session.
    await db_session.commit()

    resp = await client.get("/api/admin/evergreen/structural-health-observability")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "cycles" in body
    assert "enrolled_total" in body
    assert "last_decision" in body
    assert "largest_rows" in body
    assert "recent" in body
    assert body["cycles"] >= 1
    # Durable DB aggregate is included in the response.
    assert "observe_window_db" in body
    db_obs = body["observe_window_db"]
    assert db_obs["cycles"] >= 1
    assert "tripped_count" in db_obs


# --------------------------------------------------------------------------- #
# Durable DB writer + reader.
# --------------------------------------------------------------------------- #


async def test_log_audit_cycle_to_db_writes_row(db_session):
    """log_audit_cycle_to_db writes one AuditLaneDecisionLog row per call."""
    from app.models import AuditLaneDecisionLog
    from app.structural_health_audit import log_audit_cycle_to_db
    from app.time_utils import _utcnow

    now = _utcnow()
    await log_audit_cycle_to_db(
        db_session,
        ts=now,
        mode="observe",
        tripped=True,
        enrolled=0,
        payload={"fat_row_offenders": 3},
    )
    await db_session.flush()

    rows = list(
        (
            await db_session.execute(
                select(AuditLaneDecisionLog).where(
                    AuditLaneDecisionLog.lane == "structural_health"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].mode == "observe"
    assert rows[0].tripped is True
    assert rows[0].enrolled == 0
    assert rows[0].payload["fat_row_offenders"] == 3


async def test_get_structural_health_observability_from_db_aggregates(db_session):
    """get_structural_health_observability_from_db counts tripped vs total cycles."""
    from app.structural_health_audit import (
        get_structural_health_observability_from_db,
        log_audit_cycle_to_db,
    )
    from app.time_utils import _utcnow

    now = _utcnow()
    await log_audit_cycle_to_db(
        db_session, ts=now, mode="observe", tripped=True, enrolled=0, payload={}
    )
    await log_audit_cycle_to_db(
        db_session, ts=now, mode="observe", tripped=False, enrolled=0, payload={}
    )
    await log_audit_cycle_to_db(
        db_session, ts=now, mode="observe", tripped=True, enrolled=0, payload={}
    )
    await db_session.flush()

    result = await get_structural_health_observability_from_db(
        db_session, window_hours=24
    )
    assert result["cycles"] == 3
    assert result["tripped_count"] == 2
    assert result["enrolled_total"] == 0
    assert result["source"] == "db"


async def test_run_structural_health_audit_writes_db_row(db_session):
    """run_structural_health_audit writes a durable row even in observe mode."""
    from app.models import AuditLaneDecisionLog

    runtime_config["structural_health_audit_mode"] = "observe"
    runtime_config["structural_health_audit_fat_row_bytes"] = 1000
    await run_structural_health_audit(db_session)

    rows = list(
        (
            await db_session.execute(
                select(AuditLaneDecisionLog).where(
                    AuditLaneDecisionLog.lane == "structural_health"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].mode == "observe"
    assert rows[0].enrolled == 0
