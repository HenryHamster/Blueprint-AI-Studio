"""Regression tests for the ProjectTelemetry dual-write path.

Verifies that:
1. set_project_telemetry/get_project_telemetry round-trip correctly.
2. Production-scale reserved blobs (efficiency_baseline ~99 KB,
   lane_baselines ~27 KB, cochange_index ~38 KB) fit in the table and
   are retrieved intact.
3. Agent design-doc writes succeed while relocated blobs hold large data
   (i.e. the blobs don't count toward the 500 KB agent cap).
4. design_doc_fallback is used when the telemetry row is absent.
"""

from __future__ import annotations

import json

import pytest

from app.models import Project
from app.project_telemetry import (
    TELEMETRY_KEYS,
    get_project_telemetry,
    set_project_telemetry,
)
from app.task_locking import DESIGN_DOC_MAX_BYTES, _RESERVED_DESIGN_DOC_KEYS


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
# basic round-trip
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
        doc[key] = {"data": "x" * 30_000}  # ~30 KB per key → >270 KB total reserved

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
    """An agent doc with 373 KB of reserved data + 400 KB of agent pages is
    within the 500 KB cap because only agent pages are measured."""
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
        "efficiency_baseline",
        "lane_baselines",
        "cochange_index",
        "cochange_applied_prs",
        "module_feedback",
        "sr_catch_rate_trends",
        "retired_diagnoses_archive",
        "playbook_archive",
        "proposed_changes",
    }
    assert TELEMETRY_KEYS == expected
