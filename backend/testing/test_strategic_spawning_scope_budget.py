"""strategic_spawning: payload scope and budget packing.

Covers the reconciled design-doc payload (symbol absent/indeterminate
downgrades), operator-override summaries, calibration factor, mode-aware
``system_maturity`` / ``drift_signals`` additions, ``pack_under_budget``
integration, and the ``version_phase`` promotion-readiness enrichment.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import json
import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, patch

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Artifact,
    ArtifactType,
    Task,
    TaskStatus,
)
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _STRATEGIC_REVIEW_SCHEMA,
)
from app.payload_schemas import pack_under_budget
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _lts_split_active,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
    _utcnow_naive,
)


def test_build_operator_overrides_summary_caps_and_truncates():
    """The summary respects the entry cap and char-bounds each value."""
    from app.strategic_spawning import (
        _build_operator_overrides_summary,
        _OPERATOR_OVERRIDES_MAX,
        _OVERRIDE_VALUE_MAXLEN,
    )

    ledger = [
        {
            "at": f"2026-06-{i:02d}T00:00:00Z",
            "field": "vision.g",
            "kind": "value_conflict",
            "user_value": "u",
            "agent_value": "x" * (_OVERRIDE_VALUE_MAXLEN + 50),
        }
        for i in range(1, _OPERATOR_OVERRIDES_MAX + 6)
    ]
    project = _make_project(vision={"_rejected_agent_edits": ledger})
    out = _build_operator_overrides_summary(project)

    assert len(out) == _OPERATOR_OVERRIDES_MAX
    # Truncated value carries the ellipsis marker and respects the cap.
    assert out[0]["agent_value"].endswith("…")
    assert len(out[0]["agent_value"]) == _OVERRIDE_VALUE_MAXLEN + 1


@pytest.mark.asyncio
async def test_design_doc_update_payload_omits_playbook_archive(db_session):
    """Integration: _spawn_design_doc_update must not ship playbook_archive."""
    project = _make_project(
        id=uuid.uuid4(),
        name="archive-strip-test",
        github_owner="test",
        github_repo="repo",
        autonomous_enabled=True,
        vision={"purpose": "test"},
        design_doc={
            "summary": "ok",
            "playbook_archive": [
                {"_archived_key": f"playbook/e{i}"} for i in range(100)
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _spawn_design_doc_update(project, db_session)
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # design_doc now ships as a compact navigation index (one entry per
    # non-internal section). playbook_archive is operator-only and excluded;
    # the top-level "summary" section is represented by its key.
    dd = payload["design_doc"]
    by_key = {e["key"]: e for e in dd["sections"]}
    assert "playbook_archive" not in by_key
    assert by_key["summary"]["summary"] == "ok"


@pytest.mark.asyncio
async def test_spawn_design_doc_update_payload_uses_reconciled_design_doc(
    db_session, mocker
):
    """Phase 1 payload ships the reconciled design doc built with GitHub context."""
    from app.strategic_spawning import _agent_view_design_doc

    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-123",
    }
    reconciled_design_doc = {
        "systems/api": {
            "status": "missing",
            "presence_warning": "reconciled against integration branch",
        }
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    reconcile_mock = mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(return_value=reconciled_design_doc),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # The reconciled status flows into the compact index, not a full inline doc.
    by_key = {e["key"]: e for e in payload["design_doc"]["sections"]}
    assert by_key["systems/api"]["status"] == "missing"
    assert (
        by_key["systems/api"]["presence_warning"]
        == "reconciled against integration branch"
    )
    reconcile_mock.assert_awaited_once_with(
        _agent_view_design_doc(project.design_doc), **verification_context
    )


@pytest.mark.asyncio
async def test_spawn_design_doc_update_payload_downgrades_symbol_absent_entry(
    db_session, mocker
):
    """A FILE-PRESENT but SYMBOL-ABSENT entry is downgraded to missing in the payload.

    Exercises the real symbol-aware ``ground_design_doc`` walker (NOT mocked):
    the backing file still exists on the branch (file-level pass leaves the entry
    alone), but the specifically-cited ``path:symbol`` was deleted, so the
    symbol-level pass downgrades the entry to ``missing`` with a presence_warning
    naming the absent symbol — the post-refactor facade case file-level grounding
    cannot see.
    """
    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-789",
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
                "description": "Serves requests via backend/app/api.py:deleted_func.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    # File still present on the branch → file-level pass does NOT downgrade.
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=True),
    )
    # Cited symbol is unambiguously absent from the present file → symbol-level
    # pass downgrades the entry.
    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head",
        new=AsyncMock(return_value=False),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    by_key = {e["key"]: e for e in tasks[0].payloads[0]["design_doc"]["sections"]}
    entry = by_key["systems/api"]
    assert entry["status"] == "missing"
    assert "backend/app/api.py:deleted_func" in entry["presence_warning"]
    # The "[reconciled: ...]" note appended to the section's description is
    # surfaced through the bounded index summary.
    assert "[reconciled:" in entry["summary"]


@pytest.mark.asyncio
async def test_spawn_design_doc_update_symbol_indeterminate_leaves_entry(
    db_session, mocker
):
    """An indeterminate symbol result never downgrades a file-present entry."""
    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "automain",
        "project_id": "proj-790",
    }
    project = _make_project(
        id=uuid.uuid4(),
        vision={"purpose": "test"},
        design_doc={
            "systems/api": {
                "status": "functional",
                "modules": ["backend/app/api.py"],
                "description": "Serves requests via backend/app/api.py:maybe_func.",
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_spawning._build_shipped_changes_manifest",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=True),
    )
    # Indeterminate (None) and present (True) symbol results must NOT downgrade.
    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head",
        new=AsyncMock(return_value=None),
    )

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    by_key = {e["key"]: e for e in tasks[0].payloads[0]["design_doc"]["sections"]}
    entry = by_key["systems/api"]
    assert entry["status"] == "functional"
    assert "presence_warning" not in entry


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_uses_reconciled_design_doc(
    db_session, mocker
):
    """Phase 2 payload ships the reconciled design doc built with GitHub context."""
    from app.strategic_spawning import _agent_view_design_doc

    verification_context = {
        "token": "gh-token",
        "owner": "acme",
        "repo": "studio",
        "branch": "main",
        "project_id": "proj-456",
    }
    reconciled_design_doc = {
        "capabilities/search": {
            "status": "missing",
            "presence_warning": "reconciled against integration branch",
        }
    }
    project = _make_project(
        id=uuid.uuid4(),
        integration_branch="main",
        promotion_target="main",
        vision={"purpose": "test"},
        directions=[
            {
                "id": "d-reconcile",
                "status": "active",
                "text": "Verify strategic payload grounding",
                "priority": "medium",
            }
        ],
        design_doc={
            "capabilities/search": {
                "status": "working",
                "modules": ["backend/app/search.py"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.strategic_cycle.recent_for_agent_context",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning._build_recent_interventions",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_spawning.resolve_branch_verification_context",
        new=AsyncMock(return_value=verification_context),
    )
    reconcile_mock = mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(return_value=reconciled_design_doc),
    )
    mocker.patch(
        "app.strategic_spawning._verify_capability_backing_paths",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.strategic_spawning._build_system_maturity_payload",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.drift_detection.compute_direction_drift",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.drift_detection.persist_drift_snapshot",
        new=AsyncMock(return_value=None),
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]
    # The reconciled status flows into the compact index, not a full inline doc.
    by_key = {e["key"]: e for e in payload["design_doc"]["sections"]}
    assert by_key["capabilities/search"]["status"] == "missing"
    assert (
        by_key["capabilities/search"]["presence_warning"]
        == "reconciled against integration branch"
    )
    reconcile_mock.assert_awaited_once_with(
        _agent_view_design_doc(project.design_doc), **verification_context
    )


@pytest.mark.asyncio
async def test_strategic_review_payload_includes_calibration_factor(db_session):
    """prediction_calibration_factor and note are always present in strategic payload."""
    project = _make_project(
        vision={"purpose": "test", "_strategic_cycle_history": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "prediction_calibration_factor" in payload
    assert "prediction_calibration_note" in payload
    assert isinstance(payload["prediction_calibration_factor"], float)
    assert isinstance(payload["prediction_calibration_note"], str)


@pytest.mark.asyncio
async def test_strategic_review_calibration_factor_reflects_history(db_session):
    """Factor < 1.0 when recent cycles consistently over-predicted merges."""
    directions = [
        {
            "id": f"d{i}",
            "status": "active",
            "text": f"direction {i}",
            "priority": "medium",
            "research_history": [{"prs_merged": 1}],
        }
        for i in range(3)
    ]
    cycles = [
        {
            "cycle_id": f"sc-{i}",
            "outcome": "completed",
            "created_direction_ids": [f"d{i}"],
            "created_predictions": {f"d{i}": {"similar_past_merges": 10}},
        }
        for i in range(3)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "test", "_strategic_cycle_history": cycles},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    factor = payload["prediction_calibration_factor"]
    assert factor < 1.0, (
        f"Expected factor < 1.0 for over-prediction history, got {factor}"
    )
    assert "optimistic" in payload["prediction_calibration_note"]


# ── Mode-aware additions: system_maturity + drift_signals ─────────────────────


@pytest.mark.asyncio
async def test_strategic_review_includes_system_maturity_when_rows_exist(db_session):
    """system_maturity payload field is present and non-empty when SystemMaturity
    rows exist; absent when no rows."""
    from app.models import SystemMaturity
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {
                "id": "d-sm01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-A",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        SystemMaturity(
            project_id=project.id,
            system_id="sys-A",
            score=72,
            components={"test_coverage": 0.5},
            recommended_mode="ship",
            top_issues=[{"description": "thin tests"}],
            last_audited_at=_utcnow(),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" in payload
    assert isinstance(payload["system_maturity"], list)
    assert any(sm["system_id"] == "sys-A" for sm in payload["system_maturity"])
    sm_row = next(sm for sm in payload["system_maturity"] if sm["system_id"] == "sys-A")
    assert sm_row["score"] == 72
    assert sm_row["recommended_mode"] == "ship"


@pytest.mark.asyncio
async def test_strategic_review_omits_system_maturity_when_no_rows(db_session):
    """Without SystemMaturity rows, the system_maturity field is omitted from the payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-sm02",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-B",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    assert "system_maturity" not in payload, (
        f"system_maturity should not appear when no rows; payload keys: {sorted(payload.keys())}"
    )


@pytest.mark.asyncio
async def test_strategic_review_includes_drift_signals_when_drift_present(db_session):
    """drift_signals payload field is present when at least one direction has
    flagged drift; the field shape includes by_system and generated_at."""
    from app.models import Task as _Task, TaskStatus as _TaskStatus

    project = _make_project(
        directions=[
            {
                "id": "d-drift01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-C",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 5 completed claim-tasks with no PR artifacts → stalled_despite_churn
    for i in range(5):
        db_session.add(
            _Task(
                project_id=project.id,
                title=f"claim {i}",
                description="",
                status=_TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {
                        "type": "create",
                        "prompt": "x",
                        "current_direction_id": "d-drift01",
                    }
                ],
                context={"direction_id": "d-drift01"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "drift_signals" in payload
    drift = payload["drift_signals"]
    assert "by_system" in drift
    assert "generated_at" in drift


@pytest.mark.asyncio
async def test_drift_signals_persist_snapshot_and_include_consecutive_cycles(
    db_session,
):
    """_spawn_strategic_review persists a drift snapshot and enriches the payload
    with consecutive_flagged_cycles when prior flagged snapshots exist."""
    from datetime import timedelta
    from app.drift_detection import DirectionDriftSnapshot
    from app.time_utils import _utcnow

    project = _make_project(
        directions=[
            {
                "id": "d-trend01",
                "status": "active",
                "text": "x",
                "priority": "medium",
                "system_id": "sys-T",
            },
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed two prior flagged snapshots to simulate previous cycles.
    now = _utcnow()
    for i in range(2, 0, -1):
        db_session.add(
            DirectionDriftSnapshot(
                project_id=project.id,
                direction_id="d-trend01",
                snapshot_at=now - timedelta(hours=i * 6),
                claimed_changes_count=5,
                actually_merged_prs=0,
                claim_shipped_consistency=0.0,
                over_claim_flag=False,
                stalled_despite_churn_flag=True,
            )
        )
    await db_session.commit()

    # Create 5 completed claim-tasks with unmerged PR artifacts.
    for i in range(5):
        task = Task(
            project_id=project.id,
            title=f"trend claim {i}",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[
                {
                    "type": "create",
                    "prompt": "x",
                    "current_direction_id": "d-trend01",
                }
            ],
            context={"direction_id": "d-trend01"},
            plan_output={"summary": "shipped"},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url=f"https://github.com/test/repo/pull/{task.id.hex[:8]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    sr_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert sr_tasks, "No strategic_review task spawned"
    payload = sr_tasks[0].payloads[0]
    assert "drift_signals" in payload, (
        f"drift_signals missing; keys: {sorted(payload.keys())}"
    )
    drift = payload["drift_signals"]
    assert "d-trend01" in drift.get("by_direction", {}), (
        f"d-trend01 not in by_direction: {list(drift.get('by_direction', {}).keys())}"
    )
    sig = drift["by_direction"]["d-trend01"]
    # 2 prior flagged + 1 current (written by persist_drift_snapshot) = 3 consecutive
    assert "consecutive_flagged_cycles" in sig, (
        f"consecutive_flagged_cycles missing from sig: {sig}"
    )
    assert sig["consecutive_flagged_cycles"] == 3

    # Verify snapshot was persisted to DB (one new row for the current cycle).
    from sqlmodel import select as _select

    snap_result = await db_session.execute(
        _select(DirectionDriftSnapshot).where(
            DirectionDriftSnapshot.project_id == project.id,
            DirectionDriftSnapshot.direction_id == "d-trend01",
        )
    )
    snaps = snap_result.scalars().all()
    assert len(snaps) == 3, (
        f"Expected 3 snapshots (2 seeded + 1 persisted), got {len(snaps)}"
    )


@pytest.mark.asyncio
async def test_strategic_review_payload_documents_recommended_mode(db_session):
    """The optional `recommended_mode` field is part of the StrategicReviewPayload
    schema (validated via validate_internal_payload). Absence in a typical
    payload should not cause validation failures."""
    from app.payload_schemas import StrategicReviewPayload

    fields = StrategicReviewPayload.model_fields
    assert "recommended_mode" in fields, (
        "recommended_mode should be part of StrategicReviewPayload schema"
    )
    # And: the existing strategic_review payload still validates.
    project = _make_project(
        directions=[
            {"id": "d-doc01", "status": "active", "text": "x", "priority": "medium"},
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True


# ---------------------------------------------------------------------------
# efficiency_snapshot in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_efficiency_snapshot_when_data_present(
    db_session,
):
    """efficiency_snapshot is present in the payload when baseline data exists."""
    from unittest.mock import AsyncMock

    project = _make_project(
        directions=[
            {"id": "d-eff01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _sample_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": 0.6,
                "baseline": 0.5,
                "drift_pct": 20.0,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": 500.0,
                "baseline": 600.0,
                "drift_pct": -16.67,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": 40.0,
                "baseline": 50.0,
                "drift_pct": -20.0,
                "history": [],
            },
            "median_review_iterations": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            },
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_sample_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" in payload
    snap = payload["efficiency_snapshot"]
    assert "axes" in snap
    assert "cost_per_merged_pr" in snap["axes"]
    assert snap["axes"]["cost_per_merged_pr"]["drift_pct"] == -25.0


@pytest.mark.asyncio
async def test_strategic_review_omits_efficiency_snapshot_when_no_data(db_session):
    """efficiency_snapshot is omitted when all current axis values are None."""
    from unittest.mock import AsyncMock

    project = _make_project(
        directions=[
            {"id": "d-eff02", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _empty_snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_reuse_rate": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "vm_cold_start_p50_ms": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_api_calls_per_task": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
            "median_review_iterations": {
                "current": None,
                "baseline": None,
                "drift_pct": None,
                "history": [],
            },
        },
        "baseline_captured_at": None,
        "last_snapshot_at": None,
    }

    with patch(
        "app.efficiency_snapshot.read_baseline_with_drift",
        new=AsyncMock(return_value=_empty_snapshot),
    ):
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]
    assert "efficiency_snapshot" not in payload


# ---------------------------------------------------------------------------
# parallelism_snapshot removed from strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_omits_parallelism_snapshot(db_session):
    """parallelism_snapshot is no longer built or shipped in the strategic payload.

    It was a Blueprint VM-pool infra metric that does not generalize to an
    arbitrary target repo. Seeds VM-timing rows that would previously have
    populated the snapshot and confirms neither the payload key nor the
    prompt_suffix observation note appears, and that it is gone from the schema.
    """
    project = _make_project(
        directions=[
            {"id": "d-par01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app.time_utils import _utcnow

    now = _utcnow()
    active_task = Task(
        project_id=project.id,
        title="active",
        description="",
        payloads=[{"type": "research"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_started_at=now - timedelta(minutes=30),
        context={"direction_id": "d-par01"},
    )
    db_session.add(active_task)
    await db_session.commit()

    prev_cap = _shared_state.runtime_config.get("max_concurrent_vms")
    _shared_state.runtime_config["max_concurrent_vms"] = 15
    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        if prev_cap is None:
            _shared_state.runtime_config.pop("max_concurrent_vms", None)
        else:
            _shared_state.runtime_config["max_concurrent_vms"] = prev_cap

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "parallelism_snapshot" not in payload
    suffix = payload.get("prompt_suffix") or ""
    assert "throughput depends on" not in suffix
    assert "pool capacity" not in suffix

    # Gone from the pack schema too.
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    assert "parallelism_snapshot" not in _STRATEGIC_REVIEW_SCHEMA


# ---------------------------------------------------------------------------
# held_directions block in strategic payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_review_includes_held_directions_block(db_session):
    """When a direction is HELD, the strategic_review payload carries a
    read-only held_directions block: the direction text, consecutive-failure
    count, dominant error fingerprint, and a few RAW recent failure messages.
    """
    from app.shared_state import record_direction_result, DIRECTION_HOLD_THRESHOLD

    _shared_state.direction_failure_streaks.clear()
    direction_id = "d-held01"
    project = _make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Decompose the giant module",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed three FAILED tasks on the direction so the block can surface raw
    # failure messages + a dominant fingerprint.
    for i in range(3):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"failed-{i}",
                description="",
                payloads=[{"type": "create"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.FAILED,
                created_by="ORCHESTRATOR",
                latest_status_message=f"ImportError: cannot import name foo (attempt {i})",
                context={"direction_id": direction_id},
            )
        )
    await db_session.commit()

    # Drive the direction into the HELD state.
    try:
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_id,
                success=False,
                failure_class="CODE_ERROR",
                project_id=str(project.id),
            )
        assert _shared_state.get_held_directions().get(direction_id)

        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" in payload
    held = payload["held_directions"]
    assert isinstance(held, list) and len(held) == 1
    entry = held[0]
    assert entry["direction_id"] == direction_id
    assert "Decompose the giant module" in entry["direction_text"]
    assert entry["consecutive_failures"] == DIRECTION_HOLD_THRESHOLD
    assert entry["last_failure_class"] == "CODE_ERROR"
    assert entry["dominant_error_fingerprint"]
    msgs = entry["recent_failure_messages"]
    assert isinstance(msgs, list) and msgs
    assert any("ImportError" in m for m in msgs)


@pytest.mark.asyncio
async def test_strategic_review_omits_held_directions_when_none_held(db_session):
    """When no direction is held, the held_directions block is absent."""
    _shared_state.direction_failure_streaks.clear()
    project = _make_project(
        directions=[
            {"id": "d-ok01", "status": "active", "text": "x", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    try:
        spawned = await _spawn_strategic_review(project, db_session)
    finally:
        _shared_state.direction_failure_streaks.clear()

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    strategic_tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert strategic_tasks
    payload = strategic_tasks[0].payloads[0]
    assert "held_directions" not in payload


@pytest.mark.asyncio
async def test_strategic_review_skips_query_when_held_directions_filtered(db_session):
    """When held directions exist but none belong to the current project,
    the held_directions block is empty and the recent FAILED tasks query
    is not executed (avoiding wasted DB work).
    """
    from app.shared_state import record_direction_result, DIRECTION_HOLD_THRESHOLD

    _shared_state.direction_failure_streaks.clear()
    direction_id_held = "d-held02"
    other_project_id = str(uuid.uuid4())
    project = _make_project(
        directions=[
            {"id": "d-active01", "status": "active", "text": "Active dir", "priority": "medium"}
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Record a held direction in shared state with a DIFFERENT project_id
    # (simulating a held direction from another project).
    try:
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_id_held,
                success=False,
                failure_class="CODE_ERROR",
                project_id=other_project_id,
            )
        assert _shared_state.get_held_directions().get(direction_id_held)

        spawned = await _spawn_strategic_review(project, db_session)
        assert spawned is True

        result = await db_session.execute(_select_agent_tasks(project.id))
        strategic_tasks = [
            t
            for t in result.scalars().all()
            if t.first_payload.get("type") == "strategic_review"
        ]
        assert strategic_tasks
        payload = strategic_tasks[0].payloads[0]
        # The held_directions block should be empty or absent since the held
        # direction belongs to a different project.
        held = payload.get("held_directions", [])
        assert isinstance(held, list) and len(held) == 0
    finally:
        _shared_state.direction_failure_streaks.clear()


# ---------------------------------------------------------------------------
# _build_failure_feedback_mitigation unit tests
# ---------------------------------------------------------------------------


def test_scope_related_failures_downscope_large_to_medium():
    """3 scope_related first-attempt rejections downscopes large→medium."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-abc123"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-abc123": {
                "scope_related": 3,
                "total": 3,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "large", "create", design_doc
    )
    assert scope == "medium"
    assert note is not None and "scope related" in note
    assert evidence["failure_downscope"]["count"] == 3
    assert evidence["failure_downscope"]["from"] == "large"
    assert evidence["failure_downscope"]["to"] == "medium"


def test_scope_related_failures_downscope_medium_to_small():
    """Dominant scope_related rejections downscope medium→small."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-def456"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-def456": {
                "scope_related": 5,
                "testing": 1,
                "total": 6,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "small"
    assert note is not None and "scope related" in note
    assert evidence["failure_downscope"]["category"] == "scope_related"


def test_non_scope_failure_category_coaches_without_downscoping():
    """Non-scope rejection categories (testing, code_defect) produce a coaching
    note naming the failure mode but never shrink the scope — shrinking a task
    doesn't fix a testing gap, it just fragments the work."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-def457"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-def457": {
                "testing": 5,
                "scope_related": 1,
                "total": 6,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert note is not None and "testing" in note
    assert "failure_downscope" not in evidence


def test_failure_feedback_no_downscope_below_threshold():
    """2 failures (< threshold) leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-ghi789"}
    design_doc = {
        "direction_first_attempt_feedback": {
            "d-ghi789": {
                "code_defect": 2,
                "total": 2,
                "last_updated": "2026-04-20T00:00:00",
            },
        }
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert note is None
    assert "failure_downscope" not in evidence


def test_success_pattern_never_biases_scope_smaller():
    """A strong small-scope success history must NOT shrink a medium proposal.

    The old success-bias arm squeezed every proposal into the smallest
    historically-merged scope, optimizing per-task merge rate while
    fragmenting contract-complete changes. What a task needs determines
    its scope, not what merged before.
    """
    from app.strategic_spawning import _build_failure_feedback_mitigation

    direction = {"id": "d-jkl012"}
    design_doc = {
        "playbook/success-create-small": {
            "source": "success-pattern",
            "success_count": 40,
        },
        "playbook/success-create-medium": {
            "source": "success-pattern",
            "success_count": 4,
        },
    }
    scope, note, evidence = _build_failure_feedback_mitigation(
        direction, "medium", "create", design_doc
    )
    assert scope == "medium"
    assert note is None
    assert "success_bias" not in evidence


def test_no_data_returns_proposed_scope_unchanged():
    """Empty design_doc leaves scope and note unchanged."""
    from app.strategic_spawning import _build_failure_feedback_mitigation

    scope, note, evidence = _build_failure_feedback_mitigation(
        {"id": "d-xyz"}, "medium", "create", {}
    )
    assert scope == "medium"
    assert note is None
    assert evidence["proposed_scope"] == "medium"
    assert evidence["final_scope"] == "medium"


# ── pack_under_budget integration ─────────────────────────────────────────────


def test_pack_under_budget_keeps_version_phase_floor_under_pressure():
    """Lifecycle context must never disappear just because sibling context is
    bloated. A compact version_phase floor should preserve the current goal,
    while _packer_meta records the degradation so the agent can fetch
    full lifecycle detail if needed."""

    large_finding = {
        "severity": "high",
        "summary": "Nested audit detail that should not be required inline.",
        "details": "audit-finding-body " * 300,
        "evidence": ["evidence " * 200 for _ in range(6)],
    }
    version_phase = {
        "version_number": 21,
        "phase": "build",
        "goal": "Ship the post-promotion observability milestone.",
        "tasks_merged_this_version": 9,
        "last_qualification": {
            "status": "not_promoted",
            "confidence": 0.74,
            "blocker": "audit_findings",
            "audit_findings": [large_finding for _ in range(30)],
        },
        "recent_versions": [
            {
                "version_number": 20 - i,
                "status": "promoted",
                "tasks_merged_this_version": 4 + i,
                "last_qualification": {
                    "status": "promoted",
                    "confidence": 0.91 - (i * 0.01),
                    "blocker": None,
                    "audit_findings": [large_finding for _ in range(12)],
                },
                "acceptance_aggregate": {
                    "merged": 4 + i,
                    "proven": 3 + i,
                    "proven_rate": 0.75,
                },
            }
            for i in range(5)
        ],
        "acceptance_aggregate": {"merged": 9, "proven": 7, "proven_rate": 0.7778},
    }
    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "Blueprint",
        "base_branch": "automain",
        "is_self_repo": True,
        "enable_self_improvement": True,
        "vision": {"purpose": "test lifecycle floor"},
        "direction_counts": {"active": 18, "paused": 0, "total": 18, "max_active": 20},
        "max_active_directions": 20,
        "version_phase": version_phase,
        "directions_summary": [
            {
                "id": f"d-vp-{i}",
                "status": "active",
                "priority": "medium",
                "text": "large direction " * 80,
                "research_context": "direction context " * 400,
            }
            for i in range(30)
        ],
        "conversation_thread": [
            {"role": "user", "content": "conversation bulk " * 300}
            for _ in range(20)
        ],
    }

    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=6000)

    assert "version_phase" in packed, (
        f"version_phase must floor instead of dropping; meta={packed['_packer_meta']}"
    )
    vp = packed["version_phase"]
    assert vp["version_number"] == 21
    assert vp["phase"] == "build"
    assert vp["goal"] == "Ship the post-promotion observability milestone."

    meta = packed["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    trimmed = [entry for entry in meta["trimmed"] if entry["key"] == "version_phase"]
    assert "version_phase" not in dropped_keys, meta
    assert trimmed, f"version_phase degradation must be visible in _packer_meta: {meta}"
    assert trimmed[0]["kind"] in {"summarized", "floored"}


@pytest.mark.asyncio
async def test_strategic_review_packer_meta_present_on_small_project(db_session):
    """Small project: packer ships everything cleanly, meta lists no trims."""
    project = _make_project(
        directions=[
            {
                "id": "d-small1",
                "status": "active",
                "text": "Small direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "_packer_meta" in payload, "Packer must always attach _packer_meta"
    meta = payload["_packer_meta"]
    assert meta["trimmed"] == [], (
        f"Small project should have no trims: {meta['trimmed']}"
    )
    assert meta["dropped"] == [], (
        f"Small project should have no drops: {meta['dropped']}"
    )
    assert meta["budget_used"] > 0
    assert meta["budget_used"] < meta["budget_total"]


@pytest.mark.asyncio
async def test_strategic_review_packer_keeps_payload_under_cap_on_bloated_project(
    db_session,
):
    """Bloated project (50 directions, heavy history): packer must keep payload
    under the 256 KB validate_internal_payload hard cap and surface trims/drops
    so the agent can fetch full state via the ./fetch tool."""
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # Synthesize 50 directions, each with bulky research_history
    directions = [
        {
            "id": f"d-bloat{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("filler text " * 30),
            "priority": "medium",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "bloated project test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
                for i in range(20)
            ],
            "archived_gaps": [{"id": f"ag{i}", "name": "archived"} for i in range(40)],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Hard requirement: must fit under the 256KB cap that crashed autonomy before
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES, (
        f"Packer failed to keep payload under cap: {serialized_size}B >= {_MAX_PAYLOAD_BYTES}B"
    )

    meta = payload["_packer_meta"]
    # On a 50-direction bloated project, directions_summary should have been
    # trimmed (cap is 12). archived_gaps drops via _agent_view_problem_model
    # before pack, so they don't appear in meta — the trim happened upstream.
    assert meta["budget_used"] <= meta["budget_total"]
    assert len(payload["directions_summary"]) <= 12, (
        f"Expected ≤12 directions after trim, got {len(payload['directions_summary'])}"
    )
    # The trim must be visible to the agent so it knows to fetch the rest.
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    assert "directions_summary" in trimmed_keys, (
        f"Trimmed list must include directions_summary; meta={meta}"
    )

    # Trim-proof anchor: even though directions_summary was trimmed to <=12, the
    # authoritative direction_counts + max ceiling must carry the TRUE active
    # count (50) so the agent views never under-report or render false-empty.
    assert "direction_counts" in payload, (
        "Authoritative direction_counts must survive budget pressure"
    )
    assert payload["direction_counts"]["active"] == 50, (
        f"direction_counts.active must be the true pre-trim count, got "
        f"{payload['direction_counts']['active']}"
    )
    assert payload["direction_counts"]["total"] == 50
    assert "max_active_directions" in payload, (
        "max_active ceiling must reach the agent (no hardcoded fallback)"
    )
    assert payload["direction_counts"]["max_active"] == payload["max_active_directions"]
    # Neither anchor key was trimmed or dropped — they are critical-tier.
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    assert "direction_counts" not in trimmed_keys | dropped_keys
    assert "max_active_directions" not in trimmed_keys | dropped_keys


@pytest.mark.asyncio
async def test_direction_counts_by_system_survives_directions_summary_trim(db_session):
    """The authoritative per-system ACTIVE map (direction_counts.by_system) is
    computed from the FULL direction set before packing, so it stays correct and
    complete even when directions_summary is trimmed under byte-budget pressure.
    This is what keeps the agent's per-system coverage-gap and concentration
    signals truthful when the per-direction detail list is trimmed."""
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # 50 active directions spread across 3 systems (plus a few with no system_id
    # which must NOT contribute to the map), all bulky enough to force a trim.
    systems = ["sys-a", "sys-b", "sys-c"]
    directions = []
    for i in range(50):
        d = {
            "id": f"d-sys{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("filler text " * 30),
            "priority": "medium",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        # Last 5 directions intentionally carry no system_id.
        if i < 45:
            d["system_id"] = systems[i % 3]
        directions.append(d)

    # Expected per-system active tally from the FULL set: 45 directions round
    # -robin across 3 systems → 15 each; the 5 system-less ones contribute 0.
    expected_by_system = {"sys-a": 15, "sys-b": 15, "sys-c": 15}

    project = _make_project(
        directions=directions,
        vision={"purpose": "per-system trim-proofing test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Payload must fit under the hard cap and the detail list must have trimmed.
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES
    assert len(payload["directions_summary"]) <= 12, (
        "directions_summary must be trimmed for this test to be meaningful"
    )
    meta = payload["_packer_meta"]
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    assert "directions_summary" in trimmed_keys

    # The per-system map is nested under the critical direction_counts field and
    # reflects the FULL active set (15/15/15), NOT the ≤12 trimmed shipped rows.
    by_system = payload["direction_counts"]["by_system"]
    assert by_system == expected_by_system, (
        f"by_system must be the true pre-trim per-system tally, got {by_system}"
    )
    assert sum(by_system.values()) == 45, (
        "system-less directions must not contribute to the per-system map"
    )
    # Inherits direction_counts' critical tier — never trimmed or dropped.
    assert "direction_counts" not in trimmed_keys | dropped_keys


@pytest.mark.asyncio
async def test_direction_counts_survive_problem_model_floor_pressure(db_session):
    """Authoritative direction_counts survive even when a huge floor-reserved
    problem_model drops directions_summary entirely.

    The problem_model has a packer floor reservation; under a tiny byte budget it
    consumes the budget and directions_summary is dropped wholesale. The critical
    direction_counts + max_active_directions anchor must still ship verbatim."""
    schema = _STRATEGIC_REVIEW_SCHEMA
    raw = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "P",
        "base_branch": "main",
        "is_self_repo": False,
        "enable_self_improvement": False,
        "vision": {"purpose": "x"},
        "problem_model": {"gaps": [{"id": f"g{i}", "d": "y" * 200} for i in range(40)]},
        "direction_counts": {"active": 7, "paused": 2, "total": 9, "max_active": 15},
        "max_active_directions": 15,
        "directions_summary": [
            {"id": f"d{i}", "status": "active", "text": "z" * 200} for i in range(20)
        ],
    }
    # Tiny budget: forces directions_summary to drop, problem_model to degrade.
    packed = pack_under_budget(raw, schema, budget=2000)
    assert packed["direction_counts"]["active"] == 7, (
        "direction_counts must ship verbatim under maximum budget pressure"
    )
    assert packed["is_self_repo"] is False
    assert packed["max_active_directions"] == 15
    meta = packed["_packer_meta"]
    affected = {e["key"] for e in meta["trimmed"]} | {e["key"] for e in meta["dropped"]}
    assert "direction_counts" not in affected
    assert "max_active_directions" not in affected
    # The detail list is legitimately droppable; only the counts are trim-proof.
    assert "directions_summary" not in packed or len(packed["directions_summary"]) < 20


def test_directions_summary_summarization_compacts_bulky_nested_context():
    """When directions_summary must summarize, bulky research_context and
    recent_findings_evidence should compact per entry instead of causing the
    whole direction summary to ship multi-KB rows or disappear."""

    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "Blueprint",
        "base_branch": "main",
        "is_self_repo": False,
        "enable_self_improvement": False,
        "vision": {"purpose": "test direction compacting"},
        "direction_counts": {"active": 24, "paused": 0, "total": 24, "max_active": 30},
        "max_active_directions": 30,
        "directions_summary": [
            {
                "id": f"d-heavy-{i:02d}",
                "status": "active",
                "priority": "high" if i < 3 else "medium",
                "text": "direction summary text " * 10,
                "total_cycles": 5,
                "prs_merged": i,
                "verified_prs_merged": i,
                "tasks_failed": 0,
                "research_context": "research-context-body " * 300,
                "recent_findings_evidence": [
                    {
                        "finding_id": f"f-{i}-{j}",
                        "summary": "finding summary " * 80,
                        "evidence": "finding evidence body " * 180,
                        "source": {"task_id": f"task-{i}-{j}", "logs": "log " * 250},
                    }
                    for j in range(4)
                ],
            }
            for i in range(24)
        ],
    }

    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=30000)

    assert "directions_summary" in packed, (
        f"directions_summary should compact, not drop; meta={packed['_packer_meta']}"
    )
    assert 0 < len(packed["directions_summary"]) <= 12
    for entry in packed["directions_summary"]:
        entry_bytes = len(json.dumps(entry, default=str).encode())
        assert entry_bytes < 2000, (
            f"direction summary entry stayed too bulky ({entry_bytes}B): {entry}"
        )
        if "research_context" in entry:
            assert len(entry["research_context"]) < 1000
        if "recent_findings_evidence" in entry:
            evidence_bytes = len(
                json.dumps(entry["recent_findings_evidence"], default=str).encode()
            )
            assert evidence_bytes < 1000

    serialized = json.dumps(packed["directions_summary"], default=str)
    assert "research-context-body " * 80 not in serialized
    assert "finding evidence body " * 80 not in serialized


@pytest.mark.asyncio
async def test_drift_signals_survives_budget_pressure(db_session):
    """drift_signals must not be dropped when low-priority fields are evicted.

    Constructs a bloated project that forces the packer to drop low-priority
    fields, then asserts drift_signals (now medium priority) is still present
    while at least one low-priority field is absent from the payload.
    """
    import json as _json
    from app.payload_schemas import _MAX_PAYLOAD_BYTES

    # 50 bulky directions to force budget pressure
    directions = [
        {
            "id": f"d-dp{i:02d}",
            "status": "active",
            "text": f"Direction {i} " + ("pressure filler " * 40),
            "priority": "medium",
            "system_id": "sys-dp",
            "research_history": [
                {
                    "prs_merged": 1,
                    "prs_rejected": 0,
                    "tasks_failed": 0,
                    "tasks_created": 1,
                    "key_finding_descriptions": [
                        f"finding {j} " * 20 for j in range(5)
                    ],
                    "failed_task_details": [],
                    "timestamp": "2026-04-01T00:00:00Z",
                }
                for _ in range(8)
            ],
            "research_context": "context narrative " * 200,
        }
        for i in range(50)
    ]
    project = _make_project(
        directions=directions,
        vision={"purpose": "budget pressure test"},
        problem_model={
            "intent": {"purpose": "test", "current_focus": "x"},
            "gaps": [
                {
                    "id": f"g{i}",
                    "name": f"gap {i}",
                    "description": "x" * 500,
                    "history": [f"h{j}" * 10 for j in range(20)],
                    "past_moves": [f"m{j}" * 10 for j in range(20)],
                    "evidence": [f"e{j}" * 10 for j in range(20)],
                }
                for i in range(20)
            ],
            "archived_gaps": [{"id": f"ag{i}", "name": "archived"} for i in range(40)],
            "friction": [],
            "capabilities": [],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # Add stalled tasks so drift_signals is non-empty and included in the payload
    for i in range(5):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"drift claim {i}",
                description="",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                payloads=[
                    {"type": "create", "prompt": "x", "current_direction_id": "d-dp00"}
                ],
                context={"direction_id": "d-dp00"},
                plan_output={"summary": "shipped"},
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    # Payload must stay under hard cap
    serialized_size = len(_json.dumps(payload, default=str).encode())
    assert serialized_size < _MAX_PAYLOAD_BYTES

    meta = payload["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta.get("dropped", [])}

    # drift_signals (medium priority) must survive budget pressure
    assert "drift_signals" in payload, (
        f"drift_signals was dropped under budget pressure; dropped={dropped_keys}"
    )

    # Confirm budget pressure existed (something was trimmed or dropped)
    trimmed_keys = {entry["key"] for entry in meta.get("trimmed", [])}
    assert dropped_keys or trimmed_keys, (
        f"Expected budget pressure (trims or drops); meta={meta}"
    )

    # drift_signals must not be among the dropped fields
    assert "drift_signals" not in dropped_keys, (
        f"drift_signals should not be dropped; dropped={dropped_keys}"
    )


# ── version_phase: promotion_readiness + recent_versions enrichment ───────────


async def _ensure_verdict_table(session):
    """Create qualificationverdict table idempotently (mirrors test_release_qualifier pattern)."""
    from app.models import QualificationVerdict as _QV

    conn = await session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await session.commit()


async def _ensure_releaseversion_table(session):
    """Create releaseversion table idempotently."""
    from app.models import ReleaseVersion as _RV

    conn = await session.connection()
    await conn.run_sync(lambda c: _RV.__table__.create(c, checkfirst=True))
    await session.commit()


def test_summarize_version_phase_preserves_acceptance_aggregate():
    """The budget-trim summarizer keeps the additive acceptance_aggregate
    top-level key (and the raw merged count) intact under trim pressure."""
    from app.strategic_spawning import _summarize_version_phase

    block = {
        "version_number": 4,
        "phase": "stabilize",
        "tasks_merged_this_version": 6,
        "acceptance_aggregate": {"merged": 6, "proven": 4, "proven_rate": 0.6667},
        "promotion_readiness": {
            "ready": True,
            "thresholds": {"min_verdicts": 5},
            "confidence": {"mean": 0.9, "stdev": 0.02, "min": 0.8, "max": 0.95},
        },
    }
    out = _summarize_version_phase(block)
    # The aggregate and raw merged count survive the trim verbatim.
    assert out["acceptance_aggregate"] == {
        "merged": 6,
        "proven": 4,
        "proven_rate": 0.6667,
    }
    assert out["tasks_merged_this_version"] == 6
    # The trim still drops verbose thresholds from promotion_readiness.
    assert "thresholds" not in out["promotion_readiness"]


def test_summarize_version_phase_strips_nested_audit_findings_but_keeps_facts():
    """The version_phase summarizer must keep the lifecycle facts that steer the
    strategist while stripping bulky nested audit findings from both the current
    qualification and recent-version history."""
    from app.strategic_spawning import _summarize_version_phase

    huge_finding = {
        "severity": "high",
        "summary": "large audit finding",
        "details": "finding detail " * 500,
        "evidence": ["evidence detail " * 200 for _ in range(4)],
    }
    block = {
        "version_number": 21,
        "phase": "stabilize",
        "goal": "Make promotion lifecycle context impossible to lose.",
        "tasks_merged_this_version": 11,
        "acceptance_aggregate": {"merged": 11, "proven": 9, "proven_rate": 0.8182},
        "last_qualification": {
            "status": "not_promoted",
            "confidence": 0.73,
            "blocker": "audit_findings",
            "candidate_sha": "abcdef1234567890",
            "audit_findings": [huge_finding for _ in range(12)],
            "sub_scores": {"audit": 0.42, "tests": 1.0},
        },
        "recent_versions": [
            {
                "version_number": 20 - i,
                "status": "promoted",
                "phase": "stabilize",
                "tasks_merged_this_version": 5 + i,
                "acceptance_aggregate": {
                    "merged": 5 + i,
                    "proven": 4 + i,
                    "proven_rate": 0.8,
                },
                "last_qualification": {
                    "status": "promoted" if i != 1 else "not_promoted",
                    "confidence": 0.92 - (i * 0.04),
                    "blocker": None if i != 1 else "ci_red",
                    "audit_findings": [huge_finding for _ in range(10)],
                },
            }
            for i in range(5)
        ],
    }

    out = _summarize_version_phase(block)

    assert out["version_number"] == 21
    assert out["phase"] == "stabilize"
    assert out["goal"] == "Make promotion lifecycle context impossible to lose."
    assert out["tasks_merged_this_version"] == 11
    assert out["acceptance_aggregate"] == {
        "merged": 11,
        "proven": 9,
        "proven_rate": 0.8182,
    }

    last = out["last_qualification"]
    assert last["status"] == "not_promoted"
    assert last["confidence"] == 0.73
    assert last["blocker"] == "audit_findings"
    assert "audit_findings" not in last
    assert last["sub_scores"] == {"audit": 0.42, "tests": 1.0}

    recent = out["recent_versions"]
    assert len(recent) == 3
    assert recent[0]["version_number"] == 20
    assert recent[0]["tasks_merged_this_version"] == 5
    assert recent[0]["acceptance_aggregate"] == {
        "merged": 5,
        "proven": 4,
        "proven_rate": 0.8,
    }
    recent_lq = recent[1]["last_qualification"]
    assert recent_lq["status"] == "not_promoted"
    assert recent_lq["confidence"] == 0.88
    assert recent_lq["blocker"] == "ci_red"

    serialized = json.dumps(out, default=str)
    assert "large audit finding" not in serialized
    assert "finding detail" not in serialized


@pytest.mark.asyncio
async def test_version_phase_includes_promotion_readiness(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and QualificationVerdict rows exist, the strategic
    payload's version_phase block carries promotion_readiness with ready,
    would_promote_rate, and recurring_blockers keys."""
    from app import release_qualifier as rq
    from app.models import ReleaseVersion, QualificationVerdict

    mocker.patch.dict(
        rq._runtime_config,
        {
            "promotion_threshold": 0.81,
            "promotion_weights": {
                "audit": 4 / 7,
                "test_erosion": 3 / 7,
            },
            "completion_debt_weight_mode": "observe",
        },
        clear=False,
    )

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open version in stabilize phase
    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        last_qualification={
            "status": "not_promoted",
            "promotion_policy_snapshot": {
                "threshold": 0.90,
                "weights": {
                    "audit": 0.5,
                    "test_erosion": 0.25,
                    "smoketest": 0.25,
                },
            },
            "promotion_policy_source": "yaml_file",
            "promotion_policy_fallback_error": None,
        },
    )
    db_session.add(version)
    await db_session.commit()

    await db_session.refresh(version)

    # Seed QualificationVerdict rows (promotion_enabled=False = dry-run regime)
    for i in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                would_promote=True,
                blocker=None,
                confidence=0.85 + i * 0.01,
                promotion_enabled=False,
            )
        )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present for is_self_repo=True"
    pr = vp.get("promotion_readiness")
    assert pr is not None, "promotion_readiness should be present when verdicts exist"
    assert "ready" in pr, "promotion_readiness must have 'ready' key"
    assert "would_promote_rate" in pr, (
        "promotion_readiness must have 'would_promote_rate' key"
    )
    assert "recurring_blockers" in pr, (
        "promotion_readiness must have 'recurring_blockers' key"
    )

    current_policy = payload["current_effective_policy"]
    attempt_policy = payload["active_or_last_attempt_policy"]
    assert current_policy["threshold"] == pytest.approx(0.81)
    assert current_policy["source"] == "runtime_config"
    assert attempt_policy["threshold"] == pytest.approx(0.90)
    assert attempt_policy["source"] == "yaml_file"
    assert current_policy["policy_hash"] != attempt_policy["policy_hash"]
    assert vp["current_effective_policy"] == current_policy
    assert vp["active_or_last_attempt_policy"] == attempt_policy


@pytest.mark.asyncio
async def test_version_phase_qualification_state_reconciles_finalized_verdict(
    db_session, mocker, _lts_split_active
):
    """The strategic payload's version_phase applies the finalized-verdict
    substitution (unified with the other open-version surfaces): a stale
    ``pending`` + ``promotion_pending`` record with an already-resolved verdict
    reads the terminal status (NOT ``promotion_check_wait``) and surfaces the
    verdict disagreement explicitly."""
    from app.models import QualificationVerdict, ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "e" * 40
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha,
            confidence=0.9,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=False,
            finalized_at=now,
            audit_findings=[{"severity": "LOW", "summary": "nit"}],
        )
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.9,
        "promotion_pending": {"pr_number": 7, "pr_url": "https://x/pull/7"},
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"

    qs = vp.get("qualification_state")
    assert qs is not None, "qualification_state must be present"
    assert qs["state"] != "promotion_check_wait", (
        f"finalized verdict must supersede the stale promotion_pending: {qs}"
    )
    assert qs["state"] == "promoted", qs
    dis = vp.get("qualification_state_disagreement")
    assert dis is not None, (
        "strategic payload must surface the finalized-verdict disagreement"
    )
    assert dis["last_qualification_status"] == "pending"
    assert dis["verdict_terminal_status"] == "promoted"


@pytest.mark.asyncio
async def test_version_phase_narrates_debt_enforce_preview(
    db_session, mocker, _lts_split_active
):
    """The strategic payload keeps the observe-mode debt-enforce preview on the
    compacted version_phase.last_qualification block."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=_utcnow_naive(),
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "d" * 40,
            "confidence": 0.72,
            "threshold": 0.8,
            "sub_scores": {"audit": 0.9, "test_erosion": 0.8, "smoketest": 0.95},
            "debt_enforce_confidence_preview": 0.61,
            "debt_enforce_weight_preview": 0.15,
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"
    qual = vp.get("last_qualification")
    assert qual is not None, "last_qualification should be present"
    assert qual["debt_enforce_confidence_preview"] == pytest.approx(0.61)
    assert qual["debt_enforce_weight_preview"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_version_phase_qualification_state_reconciles_crashed_audit(
    db_session, mocker, _lts_split_active
):
    """The strategic payload's version_phase applies the audit-crash reconciliation:
    a bare ``pending`` record whose version-scoped audit child crashed
    (terminal-failed, no report) reads ``audit_crashed`` instead of telling the
    strategist to wait on a dead audit."""
    from app.models import ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "f" * 40
    version.last_qualification = {"status": "pending", "candidate_sha": sha}
    flag_modified(version, "last_qualification")
    db_session.add(version)
    # Version-scoped release_audit task pinned to the evaluated candidate, FAILED
    # with no report artifact => a crashed audit child.
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "release_audit"}],
            vm_instance_id="agent-vm-audit-crash-strat",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id), "candidate_sha": sha},
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None, "version_phase block should be present"

    qs = vp.get("qualification_state")
    assert qs is not None and qs["state"] == "audit_crashed", qs


@pytest.mark.asyncio
async def test_version_phase_includes_recent_versions(
    db_session, mocker, _lts_split_active
):
    """When is_self_repo=True and closed ReleaseVersion rows exist, version_phase
    carries a recent_versions list."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()

    # One closed (promoted) version
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=2),
    )
    db_session.add(closed)

    # One open version
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
    )
    db_session.add(open_v)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None
    rv = vp.get("recent_versions")
    assert rv is not None, (
        "recent_versions should be present when closed versions exist"
    )
    assert isinstance(rv, list)
    assert len(rv) >= 1
    # Each entry should have version_number and status
    for entry in rv:
        assert "version_number" in entry
        assert "status" in entry


@pytest.mark.asyncio
async def test_version_phase_block_carries_goal_coverage_drift(
    db_session, mocker, _lts_split_active
):
    """The always-on version_phase block carries goal_coverage_drift when the
    helper returns a non-None dict, and the key survives _compact_version_phase
    at both normal and floor levels."""
    from app.models import ReleaseVersion, QualificationVerdict

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        goal="Ship feature X and Y",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Seed a QualificationVerdict with a merge_grade that flags a goal miss
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            merge_grade_status="fail",
            merge_grade_coverage=0.45,
            merge_grade_biggest_gap="feature Y not covered",
            goal_alignment="partial",
            finalized_at=now,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present"
    drift = vp.get("goal_coverage_drift")
    assert drift is not None, "goal_coverage_drift should be present in version_phase"
    assert "signal" in drift, "goal_coverage_drift must have 'signal' key"
    assert drift["signal"] == "drifting", (
        f"signal should be 'drifting' given stale merge grade, got {drift['signal']!r}"
    )
    assert "biggest_gap" in drift, "goal_coverage_drift must carry biggest_gap"

    # Confirm it survives compaction at both normal and floor levels.
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    normal = _summarize_version_phase(vp)
    assert "goal_coverage_drift" in normal, (
        "goal_coverage_drift must survive _summarize_version_phase"
    )
    assert normal["goal_coverage_drift"].get("signal") == "drifting"

    floor = _version_phase_floor(vp)
    assert "goal_coverage_drift" in floor, (
        "goal_coverage_drift must survive _version_phase_floor"
    )


@pytest.mark.asyncio
async def test_version_phase_block_carries_criterion_coverage(
    db_session, mocker, _lts_split_active
):
    """The initial strategic payload carries criterion_coverage for the open
    version and the signal survives version_phase compaction/floor trimming."""

    from app import acceptance_criteria as ac
    from app.models import ReleaseVersion
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="build",
        status="open",
        opened_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    c_covered = await ac.create_criterion(
        db_session, project.id, version.id, "Ship the new auth layer"
    )
    c_partial = await ac.create_criterion(
        db_session, project.id, version.id, "Harden CI pipeline"
    )
    c_uncovered = await ac.create_criterion(
        db_session, project.id, version.id, "Document the rollout"
    )
    await ac.record_evidence(
        db_session,
        criterion_id=c_covered.id,
        project_id=project.id,
        source_kind="operator_attestation",
        source_id="attest-1",
    )
    await ac.record_evidence(
        db_session,
        criterion_id=c_partial.id,
        project_id=project.id,
        source_kind="operator",
        source_id="note-7",
    )

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present"
    coverage = vp.get("criterion_coverage")
    assert coverage is not None, "criterion_coverage should be present in version_phase"
    assert coverage["total"] == 3
    assert coverage["covered"] == 0
    assert coverage["attested"] == 1
    assert coverage["partial"] == 1
    assert coverage["uncovered"] == 1
    by_key = {entry["criterion_key"]: entry for entry in coverage["criteria"]}
    assert by_key[c_covered.criterion_key]["coverage"] == "attested"
    assert by_key[c_partial.criterion_key]["coverage"] == "partial"
    assert by_key[c_uncovered.criterion_key]["coverage"] == "uncovered"

    normal = _summarize_version_phase(vp)
    assert normal["criterion_coverage"]["total"] == 3
    assert len(normal["criterion_coverage"]["criteria"]) == 3

    floor = _version_phase_floor(vp)
    assert floor["criterion_coverage"]["total"] == 3
    floor_criteria = floor["criterion_coverage"]["criteria"]
    assert len(floor_criteria) == 3
    assert {entry["criterion_key"] for entry in floor_criteria} == {
        c_covered.criterion_key,
        c_partial.criterion_key,
        c_uncovered.criterion_key,
    }

    # stuck_count survives compaction — the uncovered criterion has no direction
    # mapping so its stuck_reason is "no_mapping" and stuck_count is 1.
    assert coverage["stuck_count"] == 1, "stuck_count must be carried in the raw payload"
    assert normal["criterion_coverage"]["stuck_count"] == 1, "stuck_count must survive _summarize_version_phase"
    assert floor["criterion_coverage"]["stuck_count"] == 1, "stuck_count must survive _version_phase_floor"
    # Per-criterion stuck_reason is in the criteria list (not the floor path).
    stuck_entry = by_key[c_uncovered.criterion_key]
    assert stuck_entry["stuck_reason"] == "no_mapping", "uncovered criterion with no direction should be no_mapping"
    # Covered and partial criteria have stuck_reason=None.
    assert by_key[c_covered.criterion_key]["stuck_reason"] is None
    assert by_key[c_partial.criterion_key]["stuck_reason"] is None


