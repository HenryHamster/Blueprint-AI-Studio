"""Regression tests for the ProjectTelemetry accessor API.

Verifies that:
1. set_project_telemetry/get_project_telemetry round-trip correctly.
2. Production-scale reserved blobs (efficiency_baseline ~99 KB,
   lane_baselines ~27 KB, cochange_index ~38 KB) fit in the table and
   are retrieved intact.
3. Agent design-doc writes succeed while relocated blobs hold large data
   (i.e. the blobs don't count toward the 500 KB agent cap).
4. design_doc_fallback is used when the telemetry row is absent.
5. The dedicated-table accessors (upsert_telemetry, insert_efficiency_snapshot,
   upsert_lane_baseline) round-trip correctly and enforce retention caps.

The accessors use PostgreSQL-specific constructs — ``INSERT ... ON CONFLICT DO
UPDATE`` on a named constraint and ``key = ANY(:array)`` — so these exercises
require the real Postgres backend (``USE_TEST_POSTGRES=true``). Under SQLite the
``postgresql.insert`` construct does not compile, so the suite is skipped there
rather than asserting against a dialect the production code never runs on.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.models import Project
from app.project_telemetry import (
    _EFFICIENCY_SNAPSHOT_HISTORY_MAX,
    _LANE_BASELINE_VERSIONS_MAX,
    TELEMETRY_KEYS,
    get_project_telemetry,
    insert_efficiency_snapshot,
    read_recent_efficiency_snapshots,
    read_telemetry,
    read_telemetry_many,
    read_version_lane_baseline,
    set_project_telemetry,
    upsert_lane_baseline,
    upsert_telemetry,
)
from app.task_locking import DESIGN_DOC_MAX_BYTES, _RESERVED_DESIGN_DOC_KEYS
from app.time_utils import _utcnow
from testing.conftest import USE_POSTGRES
from testing.factories import make_project

pytestmark = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="project_telemetry accessors use Postgres ON CONFLICT / ARRAY ANY",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_large_efficiency_baseline(target_bytes: int = 99_000) -> dict:
    """Synthetic efficiency_baseline blob at roughly production size."""
    entry = {
        "baseline": {k: {"value": 1.23, "ts": "2026-01-01T00:00:00"} for k in range(20)},
        "snapshots": [
            {"ts": f"2026-01-{i:02d}T00:00:00", "axes": {k: i * 0.1 for k in range(20)}}
            for i in range(1, 30)
        ],
        "last_run_at": "2026-06-01T00:00:00",
        "last_alerted_payoff_verdict": None,
        "last_alerted_unpriced_coverage": "healthy",
        "last_alerted_self_repair_burn_share": "healthy",
        "last_alerted_routing_verdict": None,
        "_padding": "x" * max(0, target_bytes - 2000),
    }
    return entry


def _make_large_lane_baselines(target_bytes: int = 27_000) -> dict:
    """Synthetic lane_baselines blob at roughly production size."""
    baselines = {}
    for version in range(1, 40):
        baselines[str(version)] = {
            "opened_at": f"2026-01-{version % 28 + 1:02d}T00:00:00",
            "closed_at": "2026-02-01T00:00:00",
            "pr_count": version * 3,
            "task_count": version * 5,
            "_pad": "y" * 200,
        }
    # Pad to rough target size
    baselines["_padding"] = "z" * max(0, target_bytes - len(json.dumps(baselines)))
    return baselines


def _make_large_cochange_index(target_bytes: int = 38_000) -> dict:
    """Synthetic cochange_index blob at roughly production size."""
    index = {}
    for i in range(200):
        file_path = f"src/module_{i:03d}/file.py"
        index[file_path] = [f"src/module_{j:03d}/file.py" for j in range(i, min(i + 5, 200))]
    # Pad to target
    index["_padding"] = "w" * max(0, target_bytes - len(json.dumps(index)))
    return index


# ---------------------------------------------------------------------------
# HEAD-style round-trip (set/get_project_telemetry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_project_telemetry(db_session):
    project = Project(name="telemetry-test", user_id="user-1")
    db_session.add(project)
    await db_session.commit()

    value = {"foo": "bar", "count": 42}
    await set_project_telemetry(db_session, project.id, "efficiency_baseline", value)
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "efficiency_baseline")
    assert result == value


@pytest.mark.asyncio
async def test_set_project_telemetry_upserts(db_session):
    project = Project(name="telemetry-upsert", user_id="user-2")
    db_session.add(project)
    await db_session.commit()

    await set_project_telemetry(db_session, project.id, "lane_baselines", {"v": 1})
    await db_session.commit()
    await set_project_telemetry(db_session, project.id, "lane_baselines", {"v": 2})
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "lane_baselines")
    assert result == {"v": 2}


@pytest.mark.asyncio
async def test_get_project_telemetry_returns_none_when_absent(db_session):
    project = Project(name="telemetry-absent", user_id="user-3")
    db_session.add(project)
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "efficiency_baseline")
    assert result is None


@pytest.mark.asyncio
async def test_get_project_telemetry_fallback(db_session):
    project = Project(name="telemetry-fallback", user_id="user-4")
    db_session.add(project)
    await db_session.commit()

    fallback_doc = {"efficiency_baseline": {"from": "design_doc"}, "other_key": 123}
    result = await get_project_telemetry(
        db_session,
        project.id,
        "efficiency_baseline",
        design_doc_fallback=fallback_doc,
    )
    assert result == {"from": "design_doc"}


@pytest.mark.asyncio
async def test_get_project_telemetry_prefers_table_over_fallback(db_session):
    project = Project(name="telemetry-prefer-table", user_id="user-5")
    db_session.add(project)
    await db_session.commit()

    await set_project_telemetry(db_session, project.id, "efficiency_baseline", {"from": "table"})
    await db_session.commit()

    fallback_doc = {"efficiency_baseline": {"from": "design_doc"}}
    result = await get_project_telemetry(
        db_session,
        project.id,
        "efficiency_baseline",
        design_doc_fallback=fallback_doc,
    )
    assert result == {"from": "table"}


# ---------------------------------------------------------------------------
# production-scale blobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_production_scale_efficiency_baseline_round_trips(db_session):
    """~99 KB efficiency_baseline survives a set/get round-trip."""
    project = Project(name="telemetry-large-efficiency", user_id="user-6")
    db_session.add(project)
    await db_session.commit()

    blob = _make_large_efficiency_baseline(target_bytes=99_000)
    raw = json.dumps(blob)
    assert len(raw.encode()) >= 50_000, "blob should be substantial"

    await set_project_telemetry(db_session, project.id, "efficiency_baseline", blob)
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "efficiency_baseline")
    assert result is not None
    assert result.get("last_run_at") == blob["last_run_at"]
    assert len(result.get("snapshots", [])) == len(blob["snapshots"])


@pytest.mark.asyncio
async def test_production_scale_lane_baselines_round_trips(db_session):
    """~27 KB lane_baselines survives a set/get round-trip."""
    project = Project(name="telemetry-large-lanes", user_id="user-7")
    db_session.add(project)
    await db_session.commit()

    blob = _make_large_lane_baselines(target_bytes=27_000)
    await set_project_telemetry(db_session, project.id, "lane_baselines", blob)
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "lane_baselines")
    assert result is not None
    # All version keys survive
    for k in [str(v) for v in range(1, 40)]:
        assert k in result


@pytest.mark.asyncio
async def test_production_scale_cochange_index_round_trips(db_session):
    """~38 KB cochange_index survives a set/get round-trip."""
    project = Project(name="telemetry-large-cochange", user_id="user-8")
    db_session.add(project)
    await db_session.commit()

    blob = _make_large_cochange_index(target_bytes=38_000)
    await set_project_telemetry(db_session, project.id, "cochange_index", blob)
    await db_session.commit()

    result = await get_project_telemetry(db_session, project.id, "cochange_index")
    assert result is not None
    assert "src/module_000/file.py" in result


# ---------------------------------------------------------------------------
# agent cap: reserved blobs must not count toward 500 KB
# ---------------------------------------------------------------------------


def _measure_agent_owned(doc: dict) -> int:
    """Replicate task_locking._agent_owned_size (nested function, not importable)."""
    subset = {k: v for k, v in doc.items() if k not in _RESERVED_DESIGN_DOC_KEYS}
    return len(json.dumps(subset).encode())


def test_reserved_keys_excluded_from_agent_owned_size():
    """All TELEMETRY_KEYS are in _RESERVED_DESIGN_DOC_KEYS and are excluded
    from the agent-owned byte measurement."""
    doc: dict = {}
    for key in TELEMETRY_KEYS:
        doc[key] = {"data": "x" * 30_000}  # ~30 KB per key

    agent_page = "y" * 1_000  # 1 KB agent-written content
    doc["architecture"] = agent_page

    measured = _measure_agent_owned(doc)
    assert measured < 10_000, (
        f"reserved keys should be excluded; got {measured} bytes "
        f"(total doc ~{len(json.dumps(doc))} bytes)"
    )


def test_telemetry_keys_are_subset_of_reserved_keys():
    """TELEMETRY_KEYS must all appear in _RESERVED_DESIGN_DOC_KEYS."""
    missing = TELEMETRY_KEYS - _RESERVED_DESIGN_DOC_KEYS
    assert not missing, f"TELEMETRY_KEYS not in _RESERVED_DESIGN_DOC_KEYS: {missing}"


def test_agent_cap_not_breached_with_large_reserved_blobs():
    """An agent doc with reserved data + agent pages is within the 500 KB cap
    because only agent pages are measured."""
    reserved_data = {key: {"x": "z" * 40_000} for key in TELEMETRY_KEYS}
    agent_pages = {f"page_{i}": "a" * 40_000 for i in range(10)}  # 400 KB
    doc = {**reserved_data, **agent_pages}

    measured = _measure_agent_owned(doc)
    assert measured < DESIGN_DOC_MAX_BYTES, (
        f"400 KB of agent pages should be under the {DESIGN_DOC_MAX_BYTES} cap; "
        f"got {measured}"
    )


# ---------------------------------------------------------------------------
# TELEMETRY_KEYS completeness
# ---------------------------------------------------------------------------


def test_telemetry_keys_set():
    expected = {
        "cochange_index",
        "cochange_applied_prs",
        "module_feedback",
        "retired_diagnoses_archive",
        "playbook_archive",
        "proposed_changes",
        "direction_first_attempt_feedback",
        "systems/completion-debt-ledger",
        # Final God-column tranche: dual-written generic-KV blobs.
        "repo_overview",
        "_broadcast_limit",
        "_auto_remediation",
    }
    assert TELEMETRY_KEYS == expected


# ---------------------------------------------------------------------------
# Branch-style accessors (upsert/read_telemetry, snapshots, lane baselines)
# ---------------------------------------------------------------------------


async def _make_project(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def test_upsert_telemetry_roundtrip(db_session):
    """upsert then read returns the stored blob; a second upsert overwrites."""
    project = await _make_project(db_session)

    await upsert_telemetry(db_session, project.id, "catch_rate", {"v": 1, "n": 10})
    await db_session.commit()

    got = await read_telemetry(db_session, project.id, "catch_rate")
    assert got == {"v": 1, "n": 10}

    # Missing key -> None.
    assert await read_telemetry(db_session, project.id, "nope") is None

    # Second upsert on the same (project_id, key) OVERWRITES, does not duplicate.
    await upsert_telemetry(db_session, project.id, "catch_rate", {"v": 2, "n": 20})
    await db_session.commit()

    got = await read_telemetry(db_session, project.id, "catch_rate")
    assert got == {"v": 2, "n": 20}

    # Exactly one row survives for that key.
    from sqlalchemy import func, select

    from app.models import ProjectTelemetry

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(ProjectTelemetry)
            .where(
                ProjectTelemetry.project_id == project.id,
                ProjectTelemetry.key == "catch_rate",
            )
        )
    ).scalar_one()
    assert count == 1


async def test_read_telemetry_many_batch(db_session):
    """Batch read returns only existing keys, scoped to the project."""
    project = await _make_project(db_session)
    other = await _make_project(db_session)

    await upsert_telemetry(db_session, project.id, "a", {"x": 1})
    await upsert_telemetry(db_session, project.id, "b", {"x": 2})
    # A key on a DIFFERENT project must not leak into the result.
    await upsert_telemetry(db_session, other.id, "c", {"x": 99})
    await db_session.commit()

    got = await read_telemetry_many(db_session, project.id, ["a", "b", "c"])
    assert got == {"a": {"x": 1}, "b": {"x": 2}}

    # Empty key list short-circuits to {}.
    assert await read_telemetry_many(db_session, project.id, []) == {}


async def test_efficiency_snapshot_retention_prune(db_session):
    """Retention keeps only the newest _EFFICIENCY_SNAPSHOT_HISTORY_MAX rows."""
    project = await _make_project(db_session)

    base = _utcnow()
    total = _EFFICIENCY_SNAPSHOT_HISTORY_MAX + 5
    for i in range(total):
        await insert_efficiency_snapshot(
            db_session,
            project.id,
            captured_at=base + timedelta(minutes=i),
            metric_def_version="v1",
            blob={"i": i},
        )
    await db_session.commit()

    recent = await read_recent_efficiency_snapshots(db_session, project.id)
    assert len(recent) == _EFFICIENCY_SNAPSHOT_HISTORY_MAX
    # Newest-first ordering, and only the newest window is retained.
    kept_is = [snap.blob["i"] for snap in recent]
    expected = list(range(total - 1, total - 1 - _EFFICIENCY_SNAPSHOT_HISTORY_MAX, -1))
    assert kept_is == expected


async def test_efficiency_snapshot_upsert_same_captured_at(db_session):
    """A second insert at the same captured_at overwrites, not duplicates."""
    project = await _make_project(db_session)
    ts = _utcnow()

    await insert_efficiency_snapshot(
        db_session, project.id, ts, "v1", {"score": 1}
    )
    await insert_efficiency_snapshot(
        db_session, project.id, ts, "v2", {"score": 2}
    )
    await db_session.commit()

    recent = await read_recent_efficiency_snapshots(db_session, project.id)
    assert len(recent) == 1
    assert recent[0].blob == {"score": 2}
    assert recent[0].metric_def_version == "v2"


async def test_lane_baseline_per_version_keying_and_prune(db_session):
    """Lane baselines are keyed per version; prune keeps the newest N versions."""
    project = await _make_project(db_session)
    now = _utcnow()

    # Upsert version 5, then read it back.
    await upsert_lane_baseline(db_session, project.id, 5, now, "b1", {"m": 5})
    await db_session.commit()
    assert await read_version_lane_baseline(db_session, project.id, 5) == {"m": 5}

    # Re-upserting the SAME version overwrites in place.
    await upsert_lane_baseline(db_session, project.id, 5, now, "b2", {"m": 55})
    await db_session.commit()
    assert await read_version_lane_baseline(db_session, project.id, 5) == {"m": 55}

    # Missing version -> None.
    assert await read_version_lane_baseline(db_session, project.id, 999) is None

    # Fill past the cap; only the most recent _LANE_BASELINE_VERSIONS_MAX
    # versions survive.
    total = _LANE_BASELINE_VERSIONS_MAX + 5
    for v in range(1, total + 1):
        await upsert_lane_baseline(db_session, project.id, v, now, "b", {"m": v})
    await db_session.commit()

    from sqlalchemy import func, select

    from app.models import ProjectLaneBaseline

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(ProjectLaneBaseline)
            .where(ProjectLaneBaseline.project_id == project.id)
        )
    ).scalar_one()
    assert count == _LANE_BASELINE_VERSIONS_MAX

    # The lowest versions were pruned; the highest survive.
    assert await read_version_lane_baseline(db_session, project.id, 1) is None
    assert await read_version_lane_baseline(db_session, project.id, total) == {
        "m": total
    }


@pytest.mark.asyncio
async def test_project_telemetry_lock_seeds_and_preserves_legacy_container(
    db_session,
):
    """The first keyed mutation retains data that exists only in design_doc."""
    from app.task_locking import with_project_telemetry_locked

    legacy = {
        "baseline": {"cost_per_merged_pr": 3.5},
        "snapshots": [{"captured_at": "2026-07-21T00:00:00+00:00"}],
    }
    project = Project(
        name="telemetry-lock-fallback",
        user_id="user-lock",
        design_doc={"efficiency_baseline": legacy},
    )
    db_session.add(project)
    await db_session.commit()

    def _mark(container):
        container["last_alerted_payoff_verdict"] = "COST_REGRESSION"

    assert await with_project_telemetry_locked(
        db_session,
        project.id,
        "efficiency_baseline",
        _mark,
        design_doc_fallback=project.design_doc,
        skip_locked=False,
    )
    await db_session.commit()

    stored = await get_project_telemetry(
        db_session, project.id, "efficiency_baseline"
    )
    assert stored["baseline"] == legacy["baseline"]
    assert stored["snapshots"] == legacy["snapshots"]
    assert stored["last_alerted_payoff_verdict"] == "COST_REGRESSION"
