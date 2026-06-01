"""Integration tests for /api/admin/digest and /api/admin/activity endpoints."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlmodel import select

from app.main import app
from app.models import (
    Task,
    TaskStatus,
    ResidualRemediationCase,
    ResidualCaseStatus,
    ReleaseVersion,
)
from app.routers.admin_analytics import _derive_completed_at
from app.digest_metrics import (
    _FAILURE_CLASS_LABELS,
    compute_digest_cost_economics,
    compute_digest_trend_history,
    compute_failure_grouping,
    compute_narrative_summary,
    compute_recurring_error_clusters,
    compute_release_qualifier_infra_metrics,
    compute_remediation_and_patterns,
    compute_remediation_funnel,
    compute_retry_analytics,
    compute_scope_mismatch_summary,
)
from app.failure_classification import ALL_FAILURE_CLASSES
from app.sr_review_quality_metrics import (
    _stash_projected_task_fields,
    compute_review_quality_metrics,
    compute_sr_prediction_accuracy,
)
from app.sr_blind_spots import aggregate_sr_reviewer_disagreements
from testing.factories import make_project, make_task, make_membership, _utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def unauthenticated_client(override_get_session):
    """Client with no auth override — useful for testing 401/403 responses."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# /api/admin/digest tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_empty_window(client: AsyncClient):
    """No tasks in the DB → all counts are 0 and lists are empty."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["window_hours"] == 24
    assert data["tasks_truncated"] is False  # no tasks → well below the 5000-row cap
    assert data["completed_count"] == 0
    assert data["failed_count"] == 0
    assert data["pending_approval_count"] == 0
    assert data["research_cycles"] == 0
    assert data["prs_opened"] == 0
    assert data["active_count"] == 0
    assert data["active_tasks"] == []
    assert data["needs_attention"] == []
    assert data["recent_failures"] == []
    assert data["recent_completions"] == []


@pytest.mark.asyncio
async def test_digest_with_completed_tasks(client: AsyncClient, db_session):
    """Completed tasks inside the window appear in recent_completions with correct count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(
        project_id=project.id,
        title="Completed Task 1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    task2 = make_task(
        project_id=project.id,
        title="Completed Task 2",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 2
    assert data["failed_count"] == 0
    assert data["research_cycles"] == 1  # only the research-type task

    titles = {c["title"] for c in data["recent_completions"]}
    assert "Completed Task 1" in titles
    assert "Completed Task 2" in titles


@pytest.mark.asyncio
async def test_digest_recent_completions_include_scope_and_review_iterations(
    client: AsyncClient, db_session
):
    """recent_completions entries expose scope and review_iterations from task.context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Scoped Completed Task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"scope": "medium", "review_loop": {"iteration": 3}},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    entries = [
        c for c in data["recent_completions"] if c["title"] == "Scoped Completed Task"
    ]
    assert len(entries) == 1
    assert entries[0]["scope"] == "medium"
    assert entries[0]["review_iterations"] == 3


@pytest.mark.asyncio
async def test_digest_with_failed_tasks(client: AsyncClient, db_session):
    """Failed tasks appear in both needs_attention (reason='failed') and recent_failures."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failed_count"] == 1
    assert data["completed_count"] == 0

    assert len(data["needs_attention"]) == 1
    attention = data["needs_attention"][0]
    assert attention["task_id"] == str(task.id)
    assert attention["title"] == "Failed Task"
    assert attention["reason"] == "failed"

    assert len(data["recent_failures"]) == 1
    failure = data["recent_failures"][0]
    assert failure["task_id"] == str(task.id)
    assert failure["title"] == "Failed Task"
    assert "failed_at" in failure


@pytest.mark.asyncio
async def test_digest_migration_health(client: AsyncClient, db_session):
    """migration_health surfaces migrate-task counts, MIGRATION_ERROR failures, and auto-generated migrations."""
    from app.main import EXPECTED_SCHEMA_REVISION

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "migrate"}],
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "migrate"}],
                context={"failure_class": "MIGRATION_ERROR"},
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "related_files": [
                        "backend/alembic/versions/0099_x.py",
                        "backend/app/models.py",
                    ]
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    mh = response.json()["migration_health"]
    assert mh["current_schema_revision"] == EXPECTED_SCHEMA_REVISION
    assert mh["migration_tasks_in_window"] == 2
    assert mh["migration_tasks_succeeded"] == 1
    assert mh["migration_tasks_failed"] == 1
    assert mh["migration_errors_in_window"] == 1
    assert mh["auto_generated_migrations"] == 1


@pytest.mark.asyncio
async def test_digest_aggregates_merge_retries_saved(client: AsyncClient, db_session):
    """total_merge_retries_saved sums _merge_retries_saved across completed+failed tasks;
    merge_early_exit_count counts tasks with a positive saved value."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        context={"_merge_retries_saved": 3},
    )
    t2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"_merge_retries_saved": 2},
    )
    t3 = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        context={"_merge_retries_saved": 0},
    )
    t4 = make_task(project_id=project.id, status=TaskStatus.FAILED, context={})
    for t in (t1, t2, t3, t4):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total_merge_retries_saved"] == 5
    assert data["merge_early_exit_count"] == 2


@pytest.mark.asyncio
async def test_digest_failed_task_direction_attribution(
    client: AsyncClient, db_session
):
    """Failed tasks with direction_id context expose direction_id/direction_text in needs_attention, recent_failures, and failure_groups."""
    project = make_project(
        directions=[{"id": "dir-f1", "text": "Harden reliability", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-f1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    attention = data["needs_attention"][0]
    assert attention["direction_id"] == "dir-f1"
    assert attention["direction_text"] == "Harden reliability"

    failure = data["recent_failures"][0]
    assert failure["direction_id"] == "dir-f1"
    assert failure["direction_text"] == "Harden reliability"
    assert failure["task_type"] == "create"

    assert data["failure_groups"]
    group_task = data["failure_groups"][0]["tasks"][0]
    assert group_task["direction_id"] == "dir-f1"
    assert group_task["direction_text"] == "Harden reliability"


@pytest.mark.asyncio
async def test_digest_retry_waste_by_direction(client: AsyncClient, db_session):
    """retry_waste_by_direction groups retry-overhead cost by direction_id, sorted descending."""
    project = make_project(
        directions=[
            {"id": "dir-A", "text": "Direction Alpha", "status": "active"},
            {"id": "dir-B", "text": "Direction Beta", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # dir-A: one retry, 2h on-demand → expensive
    t_a = make_task(
        project_id=project.id,
        title="A retry",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-A", "_attempt_number": 2},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    # dir-B: two retries, each 30min spot → cheaper
    t_b1 = make_task(
        project_id=project.id,
        title="B retry 1",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-B", "_attempt_number": 2},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    t_b2 = make_task(
        project_id=project.id,
        title="B retry 2",
        status=TaskStatus.FAILED,
        context={"direction_id": "dir-B", "_attempt_number": 3},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    db_session.add_all([t_a, t_b1, t_b2])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    breakdown = data["retry_waste_by_direction"]
    assert len(breakdown) == 2
    # Sorted descending by cost — dir-A (on-demand, 2h) > dir-B (spot, 2x30min)
    assert breakdown[0]["direction_id"] == "dir-A"
    assert breakdown[0]["direction_text"] == "Direction Alpha"
    assert breakdown[0]["retry_task_count"] == 1
    assert breakdown[0]["retry_cost_usd"] > 0

    assert breakdown[1]["direction_id"] == "dir-B"
    assert breakdown[1]["direction_text"] == "Direction Beta"
    assert breakdown[1]["retry_task_count"] == 2
    assert breakdown[1]["retry_cost_usd"] > 0
    assert breakdown[0]["retry_cost_usd"] > breakdown[1]["retry_cost_usd"]


@pytest.mark.asyncio
async def test_digest_provision_waste_metrics(client: AsyncClient, db_session):
    """provision_waste_cost_usd sums VM cost of PROVISION_FAILURE tasks;
    provision_waste_by_direction groups and sorts by cost descending."""
    project = make_project(
        directions=[
            {"id": "dir-A", "text": "Direction Alpha", "status": "active"},
            {"id": "dir-B", "text": "Direction Beta", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # dir-A: 2h on-demand provision failure (expensive)
    pf_a = make_task(
        project_id=project.id,
        title="A prov fail",
        status=TaskStatus.FAILED,
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        context={"direction_id": "dir-A"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    # dir-B: 30min spot provision failure
    pf_b = make_task(
        project_id=project.id,
        title="B prov fail",
        status=TaskStatus.FAILED,
        latest_status_message="IN_USE_ADDRESSES quota exceeded for zone us-central1-a",
        context={"direction_id": "dir-B"},
        vm_provisioned_at=now - timedelta(minutes=35),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    # Non-provision failure — should NOT contribute
    other = make_task(
        project_id=project.id,
        title="Code error",
        status=TaskStatus.FAILED,
        latest_status_message="AssertionError: thing broke",
        context={"direction_id": "dir-A"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    db_session.add_all([pf_a, pf_b, other])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "provision_waste_cost_usd" in data
    assert data["provision_waste_cost_usd"] > 0
    # Should match cost_by_failure_class["PROVISION_FAILURE"]
    assert data["provision_waste_cost_usd"] == pytest.approx(
        data["cost_by_failure_class"].get("PROVISION_FAILURE", 0.0)
    )

    breakdown = data["provision_waste_by_direction"]
    assert len(breakdown) == 2
    assert breakdown[0]["direction_id"] == "dir-A"
    assert breakdown[0]["direction_text"] == "Direction Alpha"
    assert breakdown[0]["provision_task_count"] == 1
    assert breakdown[0]["provision_cost_usd"] > breakdown[1]["provision_cost_usd"]
    assert breakdown[1]["direction_id"] == "dir-B"
    assert breakdown[1]["provision_task_count"] == 1


@pytest.mark.asyncio
async def test_digest_analysis_overflow_summary(client: AsyncClient, db_session):
    """analysis_overflow_summary counts failed tasks with scope_gate_triggered=analysis_overflow,
    breaks down by scope, and sums VM cost using the standard rate calculation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    overflow_small = make_task(
        project_id=project.id,
        title="overflow small",
        status=TaskStatus.FAILED,
        context={"scope_gate_triggered": "analysis_overflow", "scope": "small"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    overflow_medium = make_task(
        project_id=project.id,
        title="overflow medium",
        status=TaskStatus.FAILED,
        context={"scope_gate_triggered": "analysis_overflow", "scope": "medium"},
        vm_provisioned_at=now - timedelta(minutes=30),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=True,
    )
    # Non-overflow failure — should NOT contribute
    other = make_task(
        project_id=project.id,
        title="unrelated",
        status=TaskStatus.FAILED,
        context={"scope": "small"},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=5),
        vm_spot=False,
    )
    db_session.add_all([overflow_small, overflow_medium, other])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    summary = data["analysis_overflow_summary"]
    assert summary["total_count"] == 2
    assert summary["by_scope"] == {"small": 1, "medium": 1}
    assert summary["cost_usd"] > 0


@pytest.mark.asyncio
async def test_digest_recent_failures_includes_failure_class_and_scope(
    client: AsyncClient, db_session
):
    """recent_failures entries include failure_class and scope fields."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"scope": "small"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert len(data["recent_failures"]) == 1
    failure = data["recent_failures"][0]
    assert "failure_class" in failure
    assert failure["failure_class"] is not None
    assert failure["scope"] == "small"


@pytest.mark.asyncio
async def test_digest_recurring_error_clusters(client: AsyncClient, db_session):
    """Failed tasks sharing a normalized error are grouped into a recurring cluster (count >= 2)."""
    project = make_project(
        directions=[{"id": "dir-rc", "text": "Reliability", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two failures with the same root cause but differing PR numbers — should
    # normalize to the same fingerprint.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail A",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-rc"},
            latest_status_message="Merge conflict on PR #1234",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail B",
            status=TaskStatus.FAILED,
            payloads=[{"type": "fix"}],
            context={"direction_id": "dir-rc"},
            latest_status_message="Merge conflict on PR #5678",
        )
    )
    # Singleton — should be excluded from clusters.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Fail C",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            latest_status_message="Unique failure: disk full",
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    clusters = data["recurring_error_clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["count"] == 2
    assert len(cluster["fingerprint"]) == 12
    assert "Merge conflict" in cluster["error_snippet"]
    assert cluster["direction_ids"] == ["dir-rc"]
    assert sorted(cluster["task_types"]) == ["create", "fix"]


@pytest.mark.asyncio
async def test_digest_recurring_error_clusters_empty_when_all_unique(
    client: AsyncClient, db_session
):
    """If every failed task has a distinct fingerprint, recurring_error_clusters is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i, msg in enumerate(
        [
            "TypeError: foo is undefined",
            "Rate limit exceeded for region us-west",
            "SyntaxError: unexpected token in parser",
        ]
    ):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Unique {i}",
                status=TaskStatus.FAILED,
                latest_status_message=msg,
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["recurring_error_clusters"] == []


@pytest.mark.asyncio
async def test_digest_recurring_patterns_from_persistent_anomalies(
    client: AsyncClient, db_session
):
    """persistent_anomalies entries with occurrence_count >= 2 appear in recurring_patterns."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-rec-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "Fingerprint fp-rec-001 affects 2 directions.",
        },
        "CODE_ERROR": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 1,  # below threshold, should be filtered out
            "severity": "medium",
            "latest_details": "low-count anomaly",
        },
    }
    try:
        # Failed task in last 48h with same fingerprint and a direction, so
        # affected_direction_count > 0 and remediation_status is unaddressed.
        db_session.add(
            make_task(
                project_id=project.id,
                title="Recurring fail",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={"direction_id": "dir-recur"},
                latest_status_message="boom: same root cause",
            )
        )
        await db_session.commit()

        # Seed persistent_anomalies key with the actual fingerprint of the message.
        from app.anomaly_detection import _error_fingerprint

        actual_fp = _error_fingerprint("boom: same root cause")
        _ss.persistent_anomalies[proj_key][f"CrossDirectionFingerprint_{actual_fp}"] = {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "boom affects directions",
        }

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()

        patterns = data["recurring_patterns"]
        assert len(patterns) == 2  # two with occurrence_count >= 2, one filtered
        fps = {p["fingerprint"] for p in patterns}
        assert "fp-rec-001" in fps
        assert actual_fp in fps
        # Sorted by occurrence_count desc — fp-rec-001 (4) first.
        assert patterns[0]["fingerprint"] == "fp-rec-001"
        assert patterns[0]["occurrence_count"] == 4
        assert patterns[0]["severity"] == "high"
        assert patterns[0]["remediation_status"] == "unaddressed"
        assert patterns[0]["proactive"] is False
        # actual_fp entry should count the matching failed task's direction.
        actual_entry = next(p for p in patterns if p["fingerprint"] == actual_fp)
        assert actual_entry["affected_direction_count"] == 1
        assert actual_entry["affected_directions"] == [
            {"direction_id": "dir-recur", "direction_text": ""}
        ]
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_recurring_patterns_remediation_status_investigating(
    client: AsyncClient, db_session
):
    """An in-flight investigate task with matching diagnosis_fingerprint marks status=investigating."""
    from app import shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    _ss.persistent_anomalies[proj_key] = {
        "CrossDirectionFingerprint_fp-inv-001": {
            "first_seen": "2026-04-18T00:00:00+00:00",
            "last_seen": "2026-04-19T00:00:00+00:00",
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "fp-inv-001 pattern",
        },
    }
    try:
        db_session.add(
            make_task(
                project_id=project.id,
                title="Active investigation",
                status=TaskStatus.IN_PROGRESS,
                payloads=[{"type": "investigate"}],
                context={"diagnosis_fingerprint": "fp-inv-001", "proactive": True},
            )
        )
        await db_session.commit()

        response = await client.get(f"/api/admin/digest?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        patterns = data["recurring_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["remediation_status"] == "investigating"
        assert patterns[0]["proactive"] is True
    finally:
        _ss.persistent_anomalies.pop(proj_key, None)


@pytest.mark.asyncio
async def test_digest_approval_task_direction_attribution(
    client: AsyncClient, db_session
):
    """Approval-pending tasks with direction_id context expose direction attribution in needs_attention."""
    project = make_project(
        directions=[{"id": "dir-a1", "text": "Approval direction", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
        context={"direction_id": "dir-a1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    approval_items = [
        a for a in data["needs_attention"] if a["reason"] == "approval_required"
    ]
    assert approval_items
    assert approval_items[0]["direction_id"] == "dir-a1"
    assert approval_items[0]["direction_text"] == "Approval direction"


@pytest.mark.asyncio
async def test_digest_with_pending_approval(client: AsyncClient, db_session):
    """Tasks requiring approval (non-terminal) appear in needs_attention with reason='approval_required'."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["pending_approval_count"] == 1

    attention_reasons = {a["reason"] for a in data["needs_attention"]}
    assert "approval_required" in attention_reasons

    approval_items = [
        a for a in data["needs_attention"] if a["reason"] == "approval_required"
    ]
    assert len(approval_items) == 1
    assert approval_items[0]["task_id"] == str(task.id)
    assert approval_items[0]["title"] == "Approval Task"


@pytest.mark.asyncio
async def test_digest_time_window_filtering(client: AsyncClient, db_session):
    """Tasks with created_at outside the requested hours window are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task inside the 24-hour window
    inside_task = make_task(
        project_id=project.id,
        title="Inside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=12),
    )
    # Task outside the 24-hour window
    outside_task = make_task(
        project_id=project.id,
        title="Outside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=25),
    )
    db_session.add(inside_task)
    db_session.add(outside_task)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Inside Window" in titles
    assert "Outside Window" not in titles


@pytest.mark.asyncio
async def test_digest_project_filter(client: AsyncClient, db_session):
    """project_id query param filters tasks to only the specified project."""
    project_a = make_project(name="Project A")
    project_b = make_project(name="Project B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = make_task(
        project_id=project_a.id,
        title="Task A",
        status=TaskStatus.COMPLETED,
    )
    task_b = make_task(
        project_id=project_b.id,
        title="Task B",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project_a.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Task A" in titles
    assert "Task B" not in titles


def test_compute_failure_grouping_respects_persisted_verdict():
    """Persisted authoritative verdict is returned by compute_failure_grouping even
    without at_time, preventing RATE_LIMIT→CODE_ERROR drift for analytics paths."""
    from app.failure_classification import (
        RATE_LIMIT,
        persist_authoritative_verdict,
    )

    now = _utcnow()
    heartbeat = now - timedelta(hours=2)
    window_start = now - timedelta(hours=24)

    task = make_task(
        status=TaskStatus.FAILED,
        latest_status_message="claude_code_failed",
        context={"failure_category": "claude_code_failed"},
        last_heartbeat=heartbeat,
    )
    # Simulate the reaper persisting RATE_LIMIT at the failure instant.
    persist_authoritative_verdict(task, RATE_LIMIT)

    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=window_start,
        now=now,
    )
    groups = {g["failure_class"]: g for g in result["failure_groups"]}
    assert RATE_LIMIT in groups, (
        f"Expected RATE_LIMIT group from persisted verdict, got: {list(groups)}"
    )
    assert "CODE_ERROR" not in groups, (
        "classify_failure must not flip RATE_LIMIT→CODE_ERROR in analytics path"
    )


@pytest.mark.asyncio
async def test_digest_active_count(client: AsyncClient, db_session):
    """In-progress tasks appear in active_count/active_tasks and not in completed_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_task = make_task(
        project_id=project.id,
        title="Active Task",
        status=TaskStatus.IN_PROGRESS,
    )
    completed_task = make_task(
        project_id=project.id,
        title="Done Task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(active_task)
    db_session.add(completed_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["active_count"] >= 1
    assert data["completed_count"] == 1

    active_ids = {t["task_id"] for t in data["active_tasks"]}
    assert str(active_task.id) in active_ids

    # active task should not bleed into completed_count
    completed_titles = [c["title"] for c in data["recent_completions"]]
    assert "Active Task" not in completed_titles


@pytest.mark.asyncio
async def test_digest_active_tasks_direction_and_cost(client: AsyncClient, db_session):
    """active_tasks entries expose direction_id, direction_text and running_cost_usd."""
    project = make_project(
        directions=[{"id": "d-1", "text": "Improve perf", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provisioned_at = _utcnow() - timedelta(hours=1)
    active_task = make_task(
        project_id=project.id,
        title="Directed Active",
        status=TaskStatus.IN_PROGRESS,
        context={"direction_id": "d-1"},
        vm_provisioned_at=provisioned_at,
        vm_spot=True,
    )
    bare_task = make_task(
        project_id=project.id,
        title="Bare Active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(active_task)
    db_session.add(bare_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    directed = by_id[str(active_task.id)]
    assert directed["direction_id"] == "d-1"
    assert directed["direction_text"] == "Improve perf"
    assert directed["running_cost_usd"] is not None
    assert directed["running_cost_usd"] > 0

    bare = by_id[str(bare_task.id)]
    assert bare["direction_id"] is None
    assert bare["direction_text"] is None
    assert bare["running_cost_usd"] is None


@pytest.mark.asyncio
async def test_digest_active_tasks_includes_scope(client: AsyncClient, db_session):
    """active_tasks entries expose the scope field from task context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    scoped_task = make_task(
        project_id=project.id,
        title="Scoped Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "large"},
    )
    bare_task = make_task(
        project_id=project.id,
        title="Bare Active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(scoped_task)
    db_session.add(bare_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    assert by_id[str(scoped_task.id)]["scope"] == "large"
    assert by_id[str(bare_task.id)]["scope"] is None


@pytest.mark.asyncio
async def test_digest_active_tasks_includes_review_iteration(
    client: AsyncClient, db_session
):
    """active_tasks entries expose review_iteration and review_cap from task.context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    iter_task = make_task(
        project_id=project.id,
        title="In-review Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "medium", "review_loop": {"iteration": 4}},
    )
    fresh_task = make_task(
        project_id=project.id,
        title="Fresh Active",
        status=TaskStatus.IN_PROGRESS,
        context={"scope": "small"},
    )
    db_session.add(iter_task)
    db_session.add(fresh_task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {t["task_id"]: t for t in data["active_tasks"]}
    assert by_id[str(iter_task.id)]["review_iteration"] == 4
    assert by_id[str(iter_task.id)]["review_cap"] == 12
    assert by_id[str(fresh_task.id)]["review_iteration"] == 0
    assert by_id[str(fresh_task.id)]["review_cap"] == 10


@pytest.mark.asyncio
async def test_digest_includes_long_running_completed_task(
    client: AsyncClient, db_session
):
    """A task created before the window but completed within it appears in completed_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task created 30 hours ago (outside 24h window) but completed 1 hour ago (inside window)
    completion_time = _utcnow() - timedelta(hours=1)
    long_running = make_task(
        project_id=project.id,
        title="Long Running Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=30),
        status_log=[{"t": completion_time.isoformat(), "status": "completed"}],
    )
    db_session.add(long_running)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Long Running Task" in titles


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_digest_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/digest",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/admin/activity tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_basic(client: AsyncClient, db_session):
    """Completed and failed tasks generate TASK_COMPLETED and TASK_FAILED events."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    completed = make_task(
        project_id=project.id,
        title="Completed Task",
        status=TaskStatus.COMPLETED,
    )
    failed = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(completed)
    db_session.add(failed)
    await db_session.commit()

    response = await client.get("/api/admin/activity")
    assert response.status_code == 200
    data = response.json()

    assert "events" in data
    assert "total" in data
    assert data["total"] >= 2

    event_types = {e["type"] for e in data["events"]}
    assert "TASK_COMPLETED" in event_types
    assert "TASK_FAILED" in event_types


@pytest.mark.asyncio
async def test_activity_since_filter(client: AsyncClient, db_session):
    """The since parameter filters out events before the cutoff timestamp."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    old_task = make_task(
        project_id=project.id,
        title="Old Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(days=10),
    )
    new_task = make_task(
        project_id=project.id,
        title="New Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(old_task)
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(old_task)
    await db_session.refresh(new_task)

    # Use a cutoff that excludes the old task
    since = (_utcnow() - timedelta(days=5)).isoformat()
    response = await client.get(f"/api/admin/activity?since={since}")
    assert response.status_code == 200
    data = response.json()

    task_ids = [e["task_id"] for e in data["events"] if e.get("task_id")]
    assert str(new_task.id) in task_ids
    assert str(old_task.id) not in task_ids


@pytest.mark.asyncio
async def test_activity_limit(client: AsyncClient, db_session):
    """The limit parameter caps the number of events returned."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    for i in range(5):
        task = make_task(
            project_id=project.id,
            title=f"Task {i}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/activity?limit=2")
    assert response.status_code == 200
    data = response.json()

    assert len(data["events"]) <= 2
    # total reflects all events found before limiting
    assert data["total"] >= 5


@pytest.mark.asyncio
async def test_activity_failed_includes_failure_class_and_taxonomy(
    client: AsyncClient, db_session
):
    """TASK_FAILED events expose failure_class and taxonomy from task context."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    failed = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
        context={"failure_category": "RATE_LIMIT"},
    )
    db_session.add(failed)
    await db_session.commit()

    response = await client.get("/api/admin/activity")
    assert response.status_code == 200
    data = response.json()
    failed_events = [e for e in data["events"] if e["type"] == "TASK_FAILED"]
    assert len(failed_events) >= 1
    ev = next(e for e in failed_events if e["task_id"] == str(failed.id))
    assert ev["failure_class"] == "RATE_LIMIT"
    assert ev["taxonomy"] == "infra_related"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_activity_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/activity",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# direction_progress tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_direction_progress_basic(client: AsyncClient, db_session):
    """Active direction with completed research tasks appears in direction_progress."""
    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Improve test coverage",
                "status": "active",
                "priority": "high",
                "possibly_saturated": False,
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Research task for that direction, completed within the window
    task = make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "direction_progress" in data
    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    assert dp["direction_id"] == "dir-1"
    assert dp["direction_text"] == "Improve test coverage"
    assert dp["priority"] == "high"
    assert dp["cycles_in_window"] == 1
    assert dp["tasks_created"] >= 1
    assert dp["possibly_saturated"] is False


@pytest.mark.asyncio
async def test_digest_direction_progress_excludes_inactive(
    client: AsyncClient, db_session
):
    """Inactive directions are not included in direction_progress even if tasks exist."""
    project = make_project(
        directions=[
            {"id": "dir-active", "text": "Active direction", "status": "active"},
            {"id": "dir-inactive", "text": "Inactive direction", "status": "inactive"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-active", "dir-inactive"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task for {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert "dir-active" in ids
    assert "dir-inactive" not in ids


@pytest.mark.asyncio
async def test_digest_direction_progress_includes_held_and_paused(
    client: AsyncClient, db_session
):
    """Held and auto_paused directions with recent task activity appear in direction_progress."""
    project = make_project(
        directions=[
            {"id": "dir-held", "text": "Held direction", "status": "held"},
            {"id": "dir-paused", "text": "Paused direction", "status": "auto_paused"},
            {"id": "dir-inactive", "text": "Inactive direction", "status": "inactive"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-held", "dir-paused", "dir-inactive"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task for {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert "dir-held" in by_id
    assert by_id["dir-held"]["status"] == "held"
    assert "dir-paused" in by_id
    assert by_id["dir-paused"]["status"] == "auto_paused"
    assert "dir-inactive" not in by_id


@pytest.mark.asyncio
async def test_digest_direction_progress_excludes_no_activity(
    client: AsyncClient, db_session
):
    """Active directions with no tasks in the window are omitted from direction_progress."""
    project = make_project(
        directions=[
            {"id": "dir-busy", "text": "Busy direction", "status": "active"},
            {"id": "dir-idle", "text": "Idle direction", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Only create a task for dir-busy
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task for busy",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-busy"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert "dir-busy" in ids
    assert "dir-idle" not in ids


@pytest.mark.asyncio
async def test_digest_direction_progress_prs_merged(client: AsyncClient, db_session):
    """prs_merged is summed from research_history entries within the time window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction with PRs",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 3},  # inside window
                    {"timestamp": old_ts, "prs_merged": 10},  # outside window
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    assert data["direction_progress"][0]["prs_merged"] == 3  # only the recent entry


@pytest.mark.asyncio
async def test_digest_direction_progress_prompt_quality_hint(
    client: AsyncClient, db_session
):
    """prompt_quality_hint is synthesized from research_history prompt_telemetry when available."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    telemetry_cycle = {
        "timestamp": recent_ts,
        "prompt_telemetry": {
            "merged": {
                "avg_char_count": 800,
                "avg_related_file_count": 3,
                "frac_mentions_tests": 0.8,
                "frac_mentions_symbols": 0.8,
            },
            "failed": {
                "avg_char_count": 300,
                "avg_related_file_count": 1,
                "frac_mentions_tests": 0.2,
                "frac_mentions_symbols": 0.2,
            },
        },
    }
    project = make_project(
        directions=[
            {
                "id": "dir-pq",
                "text": "Direction with telemetry",
                "status": "active",
                "research_history": [telemetry_cycle, dict(telemetry_cycle)],
            },
            {
                "id": "dir-no-pq",
                "text": "Direction without telemetry",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-pq", "dir-no-pq"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Research {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}
    assert isinstance(by_id["dir-pq"]["prompt_quality_hint"], str)
    assert by_id["dir-pq"]["prompt_quality_hint"]
    assert by_id["dir-no-pq"]["prompt_quality_hint"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_quality_score(client: AsyncClient, db_session):
    """quality_score appears in direction_progress for directions with research history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-q",
                "text": "Quality direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "tasks_created": 4, "prs_merged": 3},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-q"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert "quality_score" in dp
    assert isinstance(dp["quality_score"], float)
    assert 0.0 <= dp["quality_score"] <= 1.0


@pytest.mark.asyncio
async def test_digest_direction_progress_review_quality(
    client: AsyncClient, db_session
):
    """review_quality aggregates reviews_dismissed/overridden/user_issues_caught across in-window entries."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-rq",
                "text": "Review quality direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "reviews_dismissed": 2,
                        "reviews_overridden": 1,
                        "user_issues_caught": 3,
                    },
                    {
                        "timestamp": recent_ts2,
                        "reviews_dismissed": 1,
                        "reviews_overridden": 0,
                        "user_issues_caught": 2,
                    },
                    {
                        "timestamp": old_ts,
                        "reviews_dismissed": 99,
                        "reviews_overridden": 99,
                        "user_issues_caught": 99,
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-rq"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["review_quality"] == {
        "reviews_dismissed": 3,
        "reviews_overridden": 1,
        "user_issues_caught": 5,
    }


@pytest.mark.asyncio
async def test_digest_direction_progress_lifetime_metrics(
    client: AsyncClient, db_session
):
    """Lifetime fields combine research_history with history_aggregate evicted totals."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-life",
                "text": "Lifetime direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "prs_rejected": 1,
                        "tasks_created": 5,
                        "tasks_failed": 1,
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 1,
                        "prs_rejected": 0,
                        "tasks_created": 3,
                        "tasks_failed": 2,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 7,
                    "total_tasks_created": 20,
                    "total_prs_merged": 8,
                    "total_prs_rejected": 4,
                    "total_tasks_failed": 3,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-life"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["lifetime_cycles"] == 9  # 2 history entries + 7 aggregated
    assert dp["lifetime_prs_merged"] == 11  # 2 + 1 + 8
    assert dp["lifetime_prs_rejected"] == 5  # 1 + 0 + 4
    assert dp["lifetime_tasks_failed"] == 6  # 1 + 2 + 3
    assert dp["lifetime_conversion_rate"] == round(
        11 / 28, 3
    )  # 11 merged / (5 + 3 + 20) created


@pytest.mark.asyncio
async def test_digest_direction_progress_retirement_eligibility(
    client: AsyncClient, db_session
):
    """lifetime_autonomous_prs_merged, lifetime_operator_actions, and retirement_eligible are computed correctly."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-eligible",
                "text": "Eligible direction",
                "status": "active",
                # 4 autonomous from history (2 explicit + legacy fallback 2), aggregate adds 7 → total 11
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 2,
                        "prs_merged": 2,
                        "operator_action_count": 0,
                        "tasks_created": 3,
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 2,
                        "tasks_created": 2,
                    },  # legacy: no autonomous_prs_merged, falls back to prs_merged
                ],
                "history_aggregate": {
                    "total_cycles": 5,
                    "total_tasks_created": 10,
                    "total_prs_merged": 7,
                    "total_autonomous_prs_merged": 7,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-ineligible-ops",
                "text": "Ineligible: has operator actions",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 10,
                        "prs_merged": 10,
                        "operator_action_count": 1,
                        "tasks_created": 10,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 0,
                    "total_prs_merged": 0,
                    "total_autonomous_prs_merged": 0,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-ineligible-count",
                "text": "Ineligible: not enough autonomous merges",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 5,
                        "prs_merged": 5,
                        "operator_action_count": 0,
                        "tasks_created": 5,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 0,
                    "total_prs_merged": 0,
                    "total_autonomous_prs_merged": 4,
                    "total_operator_actions": 0,
                },
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-eligible", "dir-ineligible-ops", "dir-ineligible-count"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    eligible = by_id["dir-eligible"]
    assert eligible["lifetime_autonomous_prs_merged"] == 11  # 2 + 2 (fallback) + 7
    assert eligible["lifetime_operator_actions"] == 0
    assert eligible["retirement_eligible"] is True

    ineligible_ops = by_id["dir-ineligible-ops"]
    assert ineligible_ops["lifetime_operator_actions"] == 1
    assert ineligible_ops["retirement_eligible"] is False

    ineligible_count = by_id["dir-ineligible-count"]
    assert ineligible_count["lifetime_autonomous_prs_merged"] == 9  # 5 + 4
    assert ineligible_count["retirement_eligible"] is False


@pytest.mark.asyncio
async def test_digest_retirement_eligibility_boundary_cases(
    client: AsyncClient, db_session
):
    """At-threshold (exactly 10, 0 ops → True) and above-threshold + operator action (12, 1 op → False)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-at-threshold",
                "text": "Exactly at threshold",
                "status": "active",
                # 4 from history + 6 from aggregate = 10 total, 0 operator actions
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 4,
                        "prs_merged": 4,
                        "operator_action_count": 0,
                        "tasks_created": 4,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 3,
                    "total_tasks_created": 6,
                    "total_prs_merged": 6,
                    "total_autonomous_prs_merged": 6,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-above-with-ops",
                "text": "Above threshold but has operator action",
                "status": "active",
                # 12 autonomous merges total, but 1 operator action → zero-tolerance blocks eligibility
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 6,
                        "prs_merged": 6,
                        "operator_action_count": 1,
                        "tasks_created": 6,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 3,
                    "total_tasks_created": 6,
                    "total_prs_merged": 6,
                    "total_autonomous_prs_merged": 6,
                    "total_operator_actions": 0,
                },
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-at-threshold", "dir-above-with-ops"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    at_threshold = by_id["dir-at-threshold"]
    assert at_threshold["lifetime_autonomous_prs_merged"] == 10  # 4 + 6
    assert at_threshold["lifetime_operator_actions"] == 0
    assert at_threshold["retirement_eligible"] is True

    above_with_ops = by_id["dir-above-with-ops"]
    assert above_with_ops["lifetime_autonomous_prs_merged"] == 12  # 6 + 6
    assert above_with_ops["lifetime_operator_actions"] == 1
    assert (
        above_with_ops["retirement_eligible"] is False
    )  # zero-tolerance: any operator action blocks


@pytest.mark.asyncio
async def test_digest_direction_progress_evicted_findings_count(
    client: AsyncClient, db_session
):
    """evicted_findings_count surfaces len(history_aggregate.evicted_findings) per direction."""
    project = make_project(
        directions=[
            {
                "id": "dir-with-evicted",
                "text": "Direction with evicted findings",
                "status": "active",
                "history_aggregate": {
                    "evicted_findings": [
                        {"title": "evicted 1"},
                        {"title": "evicted 2"},
                        {"title": "evicted 3"},
                    ],
                },
            },
            {
                "id": "dir-no-evicted",
                "text": "Direction with empty evicted list",
                "status": "active",
                "history_aggregate": {"evicted_findings": []},
            },
            {
                "id": "dir-no-aggregate",
                "text": "Direction with no history_aggregate",
                "status": "active",
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-with-evicted", "dir-no-evicted", "dir-no-aggregate"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    assert by_id["dir-with-evicted"]["evicted_findings_count"] == 3
    assert by_id["dir-no-evicted"]["evicted_findings_count"] == 0
    assert by_id["dir-no-aggregate"]["evicted_findings_count"] == 0


@pytest.mark.asyncio
async def test_digest_direction_progress_sorted_by_cycles(
    client: AsyncClient, db_session
):
    """direction_progress list is sorted by cycles_in_window descending."""
    project = make_project(
        directions=[
            {"id": "dir-few", "text": "Few cycles", "status": "active"},
            {"id": "dir-many", "text": "Many cycles", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 1 research task for dir-few, 3 for dir-many
    db_session.add(
        make_task(
            project_id=project.id,
            title="Few task 1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-few"},
        )
    )
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Many task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": "dir-many"},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert ids.index("dir-many") < ids.index("dir-few")


@pytest.mark.asyncio
async def test_digest_direction_progress_absent_without_project_id(
    client: AsyncClient, db_session
):
    """direction_progress is empty when no project_id is supplied."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["direction_progress"] == []


@pytest.mark.asyncio
async def test_digest_direction_progress_taxonomy_and_failure_titles(
    client: AsyncClient, db_session
):
    """direction_progress aggregates taxonomy_summary and recent_failure_titles across research_history entries in-window."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-tax",
                "text": "Taxonomy direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "taxonomy_summary": {"scope_related": 2, "infra_related": 1},
                        "failed_task_details": [
                            {"title": "failed task A"},
                            {"title": "failed task B"},
                        ],
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "taxonomy_summary": {"scope_related": 1, "code_defect": 3},
                        "failed_task_details": [
                            {"title": "failed task C"},
                            {"title": "failed task D"},
                        ],
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "taxonomy_summary": {"scope_related": 99},
                        "failed_task_details": [{"title": "old failed task"}],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tax"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-tax"

    # taxonomy counts summed across both in-window entries, old entry excluded
    assert dp["taxonomy_summary"] == {
        "scope_related": 3,
        "infra_related": 1,
        "code_defect": 3,
    }

    # recent_failure_titles: last 3 titles from in-window failed_task_details
    titles = dp["recent_failure_titles"]
    assert titles is not None
    assert len(titles) == 3
    assert "old failed task" not in titles
    assert titles[-1] == "failed task D"


@pytest.mark.asyncio
async def test_digest_direction_progress_type_breakdown(
    client: AsyncClient, db_session
):
    """direction_progress aggregates type_breakdown across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-tb",
                "text": "Type breakdown direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "type_breakdown": {
                            "create": {"created": 2, "merged": 1, "failed": 1},
                            "fix": {"created": 1, "merged": 0, "failed": 0},
                        },
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "type_breakdown": {
                            "create": {"created": 1, "merged": 1, "failed": 0},
                            "review": {"created": 2, "merged": 0, "failed": 2},
                        },
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "type_breakdown": {
                            "create": {"created": 99, "merged": 99, "failed": 99},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tb"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-tb"

    tb = dp["type_breakdown"]
    assert tb is not None
    assert tb["create"] == {"created": 3, "merged": 2, "failed": 1}
    assert tb["fix"] == {"created": 1, "merged": 0, "failed": 0}
    assert tb["review"] == {"created": 2, "merged": 0, "failed": 2}


@pytest.mark.asyncio
async def test_digest_direction_progress_decomposition_summary(
    client: AsyncClient, db_session
):
    """decomposition_summary aggregates decomposition_quality across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-dq",
                "text": "Decomp direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p1",
                                "subtask_count": 3,
                                "subtask_merge_rate": 0.5,
                                "cross_module_count": 1,
                            },
                            {
                                "plan_task_id": "p2",
                                "subtask_count": 5,
                                "subtask_merge_rate": None,
                                "cross_module_count": 0,
                            },
                        ],
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 1,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p3",
                                "subtask_count": 2,
                                "subtask_merge_rate": 1.0,
                                "cross_module_count": 3,
                            },
                        ],
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 0,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p_old",
                                "subtask_count": 99,
                                "subtask_merge_rate": 0.0,
                                "cross_module_count": 99,
                            },
                        ],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-dq"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    summary = dp["decomposition_summary"]
    assert summary is not None
    assert summary["plans_assessed"] == 3
    # merge rate averaged over the 2 non-None values (0.5, 1.0) => 0.75
    assert summary["avg_subtask_merge_rate"] == 0.75
    # subtask_count across 3 plans: (3+5+2)/3 ≈ 3.33
    assert summary["avg_subtask_count"] == 3.33
    # cross_module across 3 plans: (1+0+3)/3 ≈ 1.33
    assert summary["avg_cross_module_count"] == 1.33


@pytest.mark.asyncio
async def test_digest_direction_progress_taxonomy_absent_when_no_entries(
    client: AsyncClient, db_session
):
    """taxonomy_summary and recent_failure_titles are null when research_history entries omit those fields."""
    recent_ts = (_utcnow() - timedelta(hours=5)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-plain",
                "text": "No taxonomy direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 1, "tasks_created": 2},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-plain"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    assert dp_list[0]["taxonomy_summary"] is None
    assert dp_list[0]["recent_failure_titles"] is None
    assert dp_list[0]["stage_dropout"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_stage_dropout(client: AsyncClient, db_session):
    """direction_progress aggregates stage_dropout across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-drop",
                "text": "Dropout direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "stage_dropout": {"review": 2, "ci": 1},
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "stage_dropout": {"review": 1, "agent": 4},
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "stage_dropout": {"review": 99},
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-drop"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-drop"
    # Stage counts summed across in-window entries only; old entry's {"review": 99} excluded.
    assert dp["stage_dropout"] == {"review": 3, "ci": 1, "agent": 4}


# ---------------------------------------------------------------------------
# Narrative content assertion tests
# ---------------------------------------------------------------------------


def _curr_ts():
    """Timestamp ~1 hour ago — within the default 24h window."""
    return _utcnow() - timedelta(hours=1)


def _prev_ts():
    """Timestamp ~36 hours ago — in the previous 24h window (24–48h ago)."""
    return _utcnow() - timedelta(hours=36)


@pytest.mark.asyncio
async def test_narrative_good_progress_prefix(client: AsyncClient, db_session):
    """Narrative starts with 'Good progress' when success rate improves by > 0.05 vs previous window."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 1 completed + 3 failed = 0.25 success rate
    prev = _prev_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        )
    )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 3 completed + 1 failed = 0.75 success rate
    # delta = 0.75 - 0.25 = 0.50 > 0.05 → trend="improving" → "Good progress —"
    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Good progress" in data["narrative_summary"]
    assert data["trend"] == "improving"


@pytest.mark.asyncio
async def test_narrative_declining_prefix(client: AsyncClient, db_session):
    """Narrative starts with 'Pipeline health declining' when success rate drops by > 0.05 vs previous."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 3 completed + 1 failed = 0.75 success rate
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    # Current window: 1 completed + 3 failed = 0.25 success rate
    # delta = 0.25 - 0.75 = -0.50 < -0.05 → trend="declining" → "Pipeline health declining —"
    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Pipeline health declining" in data["narrative_summary"]
    assert data["trend"] == "declining"


@pytest.mark.asyncio
async def test_narrative_no_trend_prefix_when_stable(client: AsyncClient, db_session):
    """Narrative has no trend prefix when success rate delta is within ±0.05 (stable)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Both windows: 3 completed + 1 failed = 0.75 success rate → delta = 0.0 → stable
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "Good progress" not in data["narrative_summary"]
    assert "Pipeline health declining" not in data["narrative_summary"]
    assert data["trend"] == "stable"


@pytest.mark.asyncio
async def test_narrative_up_from_x(client: AsyncClient, db_session):
    """completed_delta is correct when current window has more completions than previous.

    With only completions and no failures/approvals, the narrative takes the 'All clear'
    path and does not include the delta phrase — but completed_delta is still reported.
    Add a failure to force the standard path and verify 'up from X' appears there.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 1 completed
    prev = _prev_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        )
    )

    # Current window: 2 completed + 1 failed → forces standard narrative path
    # completed_delta = 2 - 1 = 1 > 0 → "(up from 1 last period)"
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "up from 1" in data["narrative_summary"]
    assert data["completed_delta"] == 1


@pytest.mark.asyncio
async def test_narrative_all_quiet_fallback(client: AsyncClient, db_session):
    """Narrative is 'All quiet' when no tasks exist in the window."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["narrative_summary"], str)
    assert len(data["narrative_summary"]) > 0
    assert "All quiet" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_dominant_category(
    client: AsyncClient, db_session
):
    """When a taxonomy category dominates failures (>=50%), narrative appends an actionable hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Failed Task {i}",
                status=TaskStatus.FAILED,
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "infra_related"
    assert "Infrastructure issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_review_quality(
    client: AsyncClient, db_session
):
    """CODE_ERROR failures with test/lint keywords → review_quality narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Lint Fail {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed"},
                latest_status_message="pytest failed on test_foo",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert (
        data["failure_taxonomy_distribution"]["dominant_category"] == "review_quality"
    )
    assert "Review quality issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_scope_related(
    client: AsyncClient, db_session
):
    """CODE_ERROR failures with expansion_ratio > 1.5 → scope_related narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Scope Fail {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed", "expansion_ratio": 2.5},
                latest_status_message="assertion error in handler",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "scope_related"
    assert "Scope mismatch is the top failure pattern" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_code_defect(client: AsyncClient, db_session):
    """CODE_ERROR failures without review/scope signals → code_defect narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Defect {i}",
                status=TaskStatus.FAILED,
                context={"failure_category": "test_failed"},
                latest_status_message="null pointer exception in handler",
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "code_defect"
    assert "Code defects dominate failures" in data["narrative_summary"]


def _build_narrative_summary(**overrides):
    base = dict(
        hours=24,
        project_id=None,
        completed_count=0,
        failed_count=0,
        pending_approval_count=0,
        needs_attention=[],
        failure_groups=[],
        failure_taxonomy_distribution={
            "categories": {},
            "total_failures": 0,
            "dominant_category": None,
        },
        trend=None,
        completed_delta=0,
        prev_completed_count=0,
        sorted_completions=[],
        sorted_failures=[],
        direction_progress_list=[],
        running_count=0,
        queued_count=0,
        pr_count=0,
        error_log_map={},
        failed_tasks=[],
    )
    base.update(overrides)
    return compute_narrative_summary(**base)


def test_compute_narrative_summary_all_clear():
    completed = [make_task(title="Completed Alpha", status=TaskStatus.COMPLETED)]
    assert (
        _build_narrative_summary(
            completed_count=1,
            sorted_completions=completed,
            pr_count=1,
        )
        == "All clear — 1 task completed with 1 PR merged, no action needed."
    )


def test_compute_narrative_summary_approval_only():
    assert (
        _build_narrative_summary(
            pending_approval_count=1,
            needs_attention=[{"reason": "approval_required", "waiting_hours": 2}],
        )
        == "1 task awaiting your approval (2h)."
    )


def test_compute_narrative_summary_dominant_code_error_refined():
    failed_tasks = [
        make_task(
            title="Lint Fail 1",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_foo",
        ),
        make_task(
            title="Lint Fail 2",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_bar",
        ),
        make_task(
            title="Lint Fail 3",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_baz",
        ),
    ]
    assert (
        _build_narrative_summary(
            failed_count=5,
            failure_groups=[{"failure_class": "CODE_ERROR", "count": 3}],
            failure_taxonomy_distribution={
                "categories": {"review_quality": {"count": 1}},
                "total_failures": 1,
                "dominant_category": "review_quality",
            },
            failed_tasks=failed_tasks,
        )
        == "Most failures (3/5) are code errors from test/lint failures — strengthen self-review before PR creation."
    )


def test_compute_narrative_summary_no_dominant_failure_class():
    failed_tasks = [
        make_task(title="Fail A", status=TaskStatus.FAILED),
        make_task(title="Fail B", status=TaskStatus.FAILED),
    ]
    assert (
        _build_narrative_summary(
            failed_count=2,
            failure_groups=[{"failure_class": "CODE_ERROR", "count": 1}],
            failure_taxonomy_distribution={
                "categories": {"code_defect": {"count": 1}},
                "total_failures": 1,
                "dominant_category": "code_defect",
            },
            sorted_failures=failed_tasks,
            failed_tasks=failed_tasks,
        )
        == "2 tasks failed (Fail A, Fail B)."
    )


def test_compute_narrative_summary_completion_trend_prefix():
    completed = [
        make_task(title="Completed Alpha", status=TaskStatus.COMPLETED),
        make_task(title="Completed Beta", status=TaskStatus.COMPLETED),
    ]
    failed_tasks = [make_task(title="Failed Task", status=TaskStatus.FAILED)]
    assert _build_narrative_summary(
        completed_count=2,
        failed_count=1,
        failure_groups=[{"failure_class": "CODE_ERROR", "count": 1}],
        failure_taxonomy_distribution={
            "categories": {"code_defect": {"count": 1}},
            "total_failures": 1,
            "dominant_category": "code_defect",
        },
        trend="improving",
        completed_delta=1,
        prev_completed_count=1,
        sorted_completions=completed,
        sorted_failures=failed_tasks,
        failed_tasks=failed_tasks,
    ) == (
        "1 task failed (Failed Task). "
        "Good progress — 2 tasks completed including Completed Alpha, Completed Beta (up from 1 last period)."
    )


def test_compute_narrative_summary_direction_taxonomy_hint():
    assert (
        _build_narrative_summary(
            project_id=uuid4(),
            direction_progress_list=[
                {
                    "direction_text": "Team Alpha",
                    "prs_merged": 2,
                    "possibly_saturated": False,
                    "tasks_created": 0,
                    "taxonomy_summary": {"review_quality": 2, "code_defect": 1},
                }
            ],
        )
        == "Research: Team Alpha: 2 PRs merged (mostly review-quality failures)."
    )


def test_compute_narrative_summary_empty_window_fallback():
    assert _build_narrative_summary() == "All quiet — no activity in the last 24h."


# ---------------------------------------------------------------------------
# Trend boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trend_improving(client: AsyncClient, db_session):
    """trend='improving' when success_rate_delta > 0.05 (prev=0.5, current=0.6, delta=0.1)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 5 completed + 5 failed = 0.50 success rate
    prev = _prev_ts()
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 6 completed + 4 failed = 0.60 success rate → delta = 0.10 > 0.05
    curr = _curr_ts()
    for _ in range(6):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "improving"
    assert data["prev_success_rate"] == 0.5
    assert data["success_rate"] == 0.6
    assert data["success_rate_delta"] == 0.1


@pytest.mark.asyncio
async def test_trend_declining(client: AsyncClient, db_session):
    """trend='declining' when success_rate_delta < -0.05 (prev=0.8, current=0.7, delta=-0.1)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Previous window: 8 completed + 2 failed = 0.80 success rate
    prev = _prev_ts()
    for _ in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
            )
        )

    # Current window: 7 completed + 3 failed = 0.70 success rate → delta = -0.10 < -0.05
    curr = _curr_ts()
    for _ in range(7):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "declining"
    assert data["prev_success_rate"] == 0.8
    assert data["success_rate"] == 0.7
    assert data["success_rate_delta"] == -0.1


@pytest.mark.asyncio
async def test_trend_stable(client: AsyncClient, db_session):
    """trend='stable' when |success_rate_delta| <= 0.05 (identical rates, delta=0.0)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Both windows: 3 completed + 1 failed = 0.75 success rate → delta = 0.0
    prev = _prev_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=prev,
            status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        )
    )

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "stable"
    assert data["success_rate_delta"] == 0.0


@pytest.mark.asyncio
async def test_trend_none_when_no_previous_terminal_tasks(
    client: AsyncClient, db_session
):
    """trend=None when the previous window has zero terminal tasks (can't compute prev_success_rate)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Current window only: 2 completed — no tasks in previous window
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["prev_success_rate"] is None
    assert data["success_rate_delta"] is None
    assert data["trend"] is None


# ---------------------------------------------------------------------------
# Failure classification in digest tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_group_rate_limit(client: AsyncClient, db_session):
    """Task with an exact Claude rate-limit message is classified into RATE_LIMIT failure group."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Rate Limited Task",
            status=TaskStatus.FAILED,
            latest_status_message="API Error: Rate limit reached",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "RATE_LIMIT" in failure_classes

    rate_limit_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "RATE_LIMIT"
    )
    assert rate_limit_group["count"] == 1
    assert any(
        "Rate Limited Task" in t.get("title", "") for t in rate_limit_group["tasks"]
    )


@pytest.mark.asyncio
async def test_failure_group_provision_failure(client: AsyncClient, db_session):
    """Task with 'quota' in status message is classified into PROVISION_FAILURE failure group."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Quota Exceeded Task",
            status=TaskStatus.FAILED,
            latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "PROVISION_FAILURE" in failure_classes

    provision_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "PROVISION_FAILURE"
    )
    assert provision_group["count"] == 1


@pytest.mark.asyncio
async def test_failure_group_code_error_default(client: AsyncClient, db_session):
    """Task with a generic error message is classified into CODE_ERROR failure group (default)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Generic Error Task",
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: cannot read property 'foo' of undefined",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "CODE_ERROR" in failure_classes

    code_error_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR"
    )
    assert code_error_group["count"] == 1
    task_entry = code_error_group["tasks"][0]
    assert "taxonomy" in task_entry
    assert task_entry["taxonomy"] in {
        "scope_related",
        "review_quality",
        "code_defect",
        "infra_related",
    }


@pytest.mark.asyncio
async def test_failure_groups_sorted_by_count_descending(
    client: AsyncClient, db_session
):
    """failure_groups are sorted by count descending (most frequent failure class first)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 CODE_ERROR tasks (generic messages)
    for i in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Code Error Task {i}",
                status=TaskStatus.FAILED,
                latest_status_message="SyntaxError: unexpected token",
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    # 1 RATE_LIMIT task
    db_session.add(
        make_task(
            project_id=project.id,
            title="Rate Limit Task",
            status=TaskStatus.FAILED,
            latest_status_message="API Error: Rate limit reached",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert len(data["failure_groups"]) >= 2
    # First group should have the highest count
    counts = [g["count"] for g in data["failure_groups"]]
    assert counts == sorted(counts, reverse=True)
    assert data["failure_groups"][0]["failure_class"] == "CODE_ERROR"
    assert data["failure_groups"][0]["count"] == 2


@pytest.mark.asyncio
async def test_failure_group_agent_timeout_subtypes(client: AsyncClient, db_session):
    """AGENT_TIMEOUT failures expose timeout_subtype in recent_failures and failure_groups."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    cases = [
        ("Heartbeat Task", "No heartbeat received after 300s", "heartbeat_lost"),
        ("OOM Task", "exit code 137 out of memory", "oom"),
        ("Exec Task", "execution timed out after 1800s", "execution_timeout"),
        (
            "Cascade Task",
            "Cancelled: parent evaluation timeout",
            "cascade_cancellation",
        ),
        (
            "Iter Task",
            "merge blocked, fix not spawned (iteration cap)",
            "iteration_limit",
        ),
        ("Crash Task", "[FATAL:uncaughtException] boom", "process_crash"),
    ]
    for title, msg, _ in cases:
        db_session.add(
            make_task(
                project_id=project.id,
                title=title,
                status=TaskStatus.FAILED,
                latest_status_message=msg,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    timeout_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "AGENT_TIMEOUT"
    )
    title_to_subtype = {
        t["title"]: t["timeout_subtype"] for t in timeout_group["tasks"]
    }
    for title, _, expected_subtype in cases:
        assert title_to_subtype[title] == expected_subtype, (
            title,
            title_to_subtype[title],
        )

    rf_by_title = {f["title"]: f for f in data["recent_failures"]}
    for title, _, expected_subtype in cases:
        assert rf_by_title[title]["timeout_subtype"] == expected_subtype


@pytest.mark.asyncio
async def test_non_timeout_failures_have_null_timeout_subtype(
    client: AsyncClient, db_session
):
    """Non-AGENT_TIMEOUT failures expose timeout_subtype=None."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            title="Code Error Task",
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: something broke",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    code_group = next(
        g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR"
    )
    assert code_group["tasks"][0]["timeout_subtype"] is None
    assert data["recent_failures"][0]["timeout_subtype"] is None


# ---------------------------------------------------------------------------
# Narrative ordering tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrative_approval_before_completion(client: AsyncClient, db_session):
    """Approval message appears before completion stats in the narrative (action-first ordering)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # One completed task
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    # One task awaiting approval
    db_session.add(
        make_task(
            project_id=project.id,
            title="Approval Task",
            status=TaskStatus.IN_PROGRESS,
            requires_approval=True,
            created_at=curr,
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    narrative = data["narrative_summary"]
    assert isinstance(narrative, str)
    assert "awaiting your approval" in narrative
    assert "completed" in narrative
    # Approval message must appear before completion stats
    assert narrative.index("awaiting your approval") < narrative.index("completed")


@pytest.mark.asyncio
async def test_narrative_all_clear_when_no_attention_needed(
    client: AsyncClient, db_session
):
    """Narrative is a single 'All clear' sentence when only completions exist (no failures, no approvals)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 3
    assert data["failed_count"] == 0
    assert data["pending_approval_count"] == 0

    narrative = data["narrative_summary"]
    assert "All clear" in narrative
    assert "no action needed" in narrative
    assert "3 tasks completed" in narrative
    # No trend prefix or failure language
    assert "Good progress" not in narrative
    assert "Pipeline health declining" not in narrative
    assert "failed" not in narrative


@pytest.mark.asyncio
async def test_trend_history_includes_first_attempt_rate(
    client: AsyncClient, db_session
):
    """trend_history entries include first_attempt_success_rate and first_attempt_sample_size."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add two completed tasks in the current window with _first_attempt_succeeded context
    curr = _curr_ts()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            context={"_first_attempt_succeeded": True},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            context={"_first_attempt_succeeded": False},
        )
    )
    # A task without the metric — should not count toward sample
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    trend_history = data["trend_history"]
    assert isinstance(trend_history, list)
    assert len(trend_history) == 7

    # All entries must have the new fields
    for entry in trend_history:
        assert "first_attempt_success_rate" in entry
        assert "first_attempt_sample_size" in entry

    # The last entry (current period) should reflect our two measured tasks
    last_entry = trend_history[-1]
    assert last_entry["first_attempt_sample_size"] == 2
    assert last_entry["first_attempt_success_rate"] == 0.5


@pytest.mark.asyncio
async def test_first_attempt_success_rate_breakdowns(client: AsyncClient, db_session):
    """first_attempt_success_rate includes by_type, by_scope, trend, and first_attempt_by_review_category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    prev = _prev_ts()

    # Current window: create/small success, refactor/medium failure w/ review cats
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": True, "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "refactor"}],
            context={
                "_first_attempt_succeeded": False,
                "scope": "medium",
                "_first_attempt_rejection_categories": {
                    "architecture": 1,
                    "correctness": 1,
                },
            },
        )
    )
    # Previous window: all failures — to produce an 'improving' trend
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
                payloads=[{"type": "create"}],
                context={"_first_attempt_succeeded": False, "scope": "small"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fa = response.json()["first_attempt_success_rate"]

    assert fa["by_type"] == {"create": 1.0, "refactor": 0.0}
    assert fa["by_scope"] == {"small": 1.0, "medium": 0.0}
    assert fa["trend"] == "improving"
    assert fa["first_attempt_by_review_category"] == {
        "architecture": 1.0,
        "correctness": 1.0,
    }


@pytest.mark.asyncio
async def test_first_attempt_by_review_category_includes_regression_risk(
    client: AsyncClient, db_session
):
    """regression_risk in _first_attempt_rejection_categories surfaces in the digest's
    first_attempt_by_review_category breakdown without being silently dropped."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    prev = _prev_ts()

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": True, "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_succeeded": False,
                "scope": "small",
                "_first_attempt_rejection_categories": {"regression_risk": 1},
            },
        )
    )
    # Previous window: needed so trend computes without errors.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                created_at=prev,
                status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
                payloads=[{"type": "create"}],
                context={"_first_attempt_succeeded": False, "scope": "small"},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fa = response.json()["first_attempt_success_rate"]

    by_cat = fa["first_attempt_by_review_category"]
    assert "regression_risk" in by_cat, (
        "regression_risk must surface in first_attempt_by_review_category"
    )
    # by_review_category is the fraction of first-attempt-rejected tasks that
    # flagged this category. We have one such task and it flagged regression_risk,
    # so the share is 1.0 — the value isn't the interesting bit; the key being
    # present (no silent drop) is.
    assert by_cat["regression_risk"] == 1.0


@pytest.mark.asyncio
async def test_heuristic_regression_risk_not_skipped_in_sr_skip_rate(
    client: AsyncClient, db_session
):
    """Heuristic-derived regression_risk entries (issue text tagged [heuristic-derived])
    are treated as valid SR outcomes — not as 'skipped' fallback envelopes — so they
    contribute to catch-rate metrics the same way baseline-derived entries do."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()

    def _make_sr(issue: str, passed: bool = False) -> dict:
        return {
            "verdict": "fail" if not passed else "pass",
            "fixes_applied": False,
            "check_results": {
                "regression_risk": {"passed": passed, "issue": issue},
            },
            "blind_spots": [],
        }

    # 3 fix tasks with heuristic-derived regression_risk (the new signal)
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _make_sr(
                        "[heuristic-derived] +120 source lines / 0 test lines"
                    )
                },
            )
        )
    # 2 fix tasks with baseline-style regression_risk (no heuristic tag)
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _make_sr(
                        "test_foo.py::test_bar now fails after change", passed=True
                    )
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    skip_rate = data.get("self_review_skip_rate")
    assert skip_rate is not None, (
        "self_review_skip_rate must be present (≥5 code-change tasks)"
    )
    # All 5 tasks have valid SR outcomes — none should be classified as skipped
    assert skip_rate["skipped_count"] == 0, (
        "heuristic-derived regression_risk entries must not count as skipped SR outcomes — "
        "aggregation should key on category presence, not issue text"
    )
    assert skip_rate["sample_size"] == 5

    # self_review_effectiveness sample_size must include both heuristic and baseline tasks
    sre = data.get("self_review_effectiveness")
    assert sre is not None, (
        "self_review_effectiveness must be present (≥3 tasks with SR outcomes)"
    )
    assert sre["sample_size"] == 5


@pytest.mark.asyncio
async def test_trend_history_includes_failed_by_taxonomy(
    client: AsyncClient, db_session
):
    """trend_history entries include failed_by_taxonomy counts parallel to failed_by_class."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # A review_quality failure (status message contains a keyword)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="pytest tests failed",
        )
    )
    # A scope_related failure (positive evidence: status-msg scope keyword)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="scope too large for task",
            context={"scope": "medium", "scope_calibrated_from": "small"},
        )
    )
    # A residual code_defect failure (no scope, no review keywords)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
            latest_status_message="unexpected exception",
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    trend_history = data["trend_history"]
    assert len(trend_history) == 7
    for entry in trend_history:
        assert "failed_by_taxonomy" in entry
        assert isinstance(entry["failed_by_taxonomy"], dict)

    last_entry = trend_history[-1]
    assert last_entry["failed"] == 3
    tax = last_entry["failed_by_taxonomy"]
    assert tax.get("review_quality") == 1
    assert tax.get("scope_related") == 1
    assert tax.get("code_defect") == 1
    assert sum(tax.values()) == 3


# ---------------------------------------------------------------------------
# Scope merge rate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_scope_conversion_rates_aggregated(
    client: AsyncClient, db_session
):
    """scope_conversion_rates aggregates scope_breakdown from research_history within the window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 5,
                        "tasks_created": 6,
                        "scope_breakdown": {
                            "small": {"created": 4, "merged": 3, "failed": 1},
                            "medium": {"created": 2, "merged": 1, "failed": 0},
                        },
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 10,
                        "tasks_created": 10,
                        "scope_breakdown": {
                            "small": {"created": 10, "merged": 10, "failed": 0},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    # scope_conversion_rates should aggregate only the recent entry (not the old one outside window)
    rates = data["scope_conversion_rates"]
    assert rates is not None
    assert rates["small"]["created"] == 4
    assert rates["small"]["merged"] == 3
    assert rates["medium"]["created"] == 2
    assert rates["medium"]["merged"] == 1

    # scope_breakdown should appear on the direction_progress entry
    dp = data["direction_progress"]
    assert len(dp) == 1
    sb = dp[0]["scope_breakdown"]
    assert sb is not None
    assert sb["small"]["created"] == 4
    assert sb["small"]["merged"] == 3
    assert sb["small"]["failed"] == 1
    assert sb["medium"]["created"] == 2


@pytest.mark.asyncio
async def test_digest_scope_breakdown_preserves_failure_sub_counts(
    client: AsyncClient, db_session
):
    """scope_breakdown in direction_progress aggregates failed_infra/failed_code_error/failed_scope_overflow."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 1,
                        "tasks_created": 5,
                        "scope_breakdown": {
                            "small": {
                                "created": 5,
                                "merged": 1,
                                "failed": 4,
                                "failed_infra": 1,
                                "failed_code_error": 2,
                                "failed_scope_overflow": 1,
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["small"]
    assert sb["failed_infra"] == 1
    assert sb["failed_code_error"] == 2
    assert sb["failed_scope_overflow"] == 1


@pytest.mark.asyncio
async def test_digest_scope_breakdown_includes_cross_module_and_failure_stages(
    client: AsyncClient, db_session
):
    """scope_breakdown aggregates merged_cross_module and failure_stages per scope."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-xmod",
                "text": "Direction xmod",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "medium": {
                                "created": 3,
                                "merged": 2,
                                "merged_cross_module": 1,
                                "failed": 1,
                                "failure_stages": {"test": 1},
                            },
                        },
                    },
                    {
                        "timestamp": recent_ts2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "scope_breakdown": {
                            "medium": {
                                "created": 2,
                                "merged": 1,
                                "merged_cross_module": 1,
                                "failed": 1,
                                "failure_stages": {"test": 1, "merge": 1},
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-xmod"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["medium"]
    assert sb["merged"] == 3
    assert sb["merged_cross_module"] == 2
    assert sb["failure_stages"] == {"test": 2, "merge": 1}


@pytest.mark.asyncio
async def test_digest_scope_breakdown_includes_efficiency_predictors(
    client: AsyncClient, db_session
):
    """scope_breakdown aggregates weighted expansion/complexity averages and effort_distribution totals."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "D1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "small": {
                                "created": 3,
                                "merged": 2,
                                "failed": 1,
                                "avg_expansion_ratio_merged": 1.0,
                                "avg_expansion_ratio_failed": 3.0,
                                "avg_complexity_merged": 2.0,
                                "avg_complexity_failed": 4.0,
                                "effort_distribution": {
                                    "low": {"created": 2, "merged": 2, "failed": 0},
                                    "high": {"created": 1, "merged": 0, "failed": 1},
                                },
                            },
                        },
                    },
                    {
                        "timestamp": recent_ts2,
                        "prs_merged": 2,
                        "tasks_created": 3,
                        "scope_breakdown": {
                            "small": {
                                "created": 3,
                                "merged": 2,
                                "failed": 1,
                                "avg_expansion_ratio_merged": 2.0,
                                "avg_complexity_merged": 3.0,
                                "effort_distribution": {
                                    "low": {"created": 3, "merged": 2, "failed": 1},
                                },
                            },
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["small"]
    # Weighted by merged counts (2 each cycle): (1.0*2 + 2.0*2)/4 = 1.5
    assert sb["avg_expansion_ratio_merged"] == 1.5
    assert sb["avg_expansion_ratio"] == 1.5
    # Only one cycle had failed expansion ratio: weight=1, value=3.0
    assert sb["avg_expansion_ratio_failed"] == 3.0
    # Complexity merged weighted: (2.0*2 + 3.0*2)/4 = 2.5
    assert sb["avg_complexity_merged"] == 2.5
    assert sb["avg_complexity_failed"] == 4.0
    ed = sb["effort_distribution"]
    assert ed["low"] == {"created": 5, "merged": 4, "failed": 1}
    assert ed["high"] == {"created": 1, "merged": 0, "failed": 1}


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_scope_and_status(
    client: AsyncClient, db_session
):
    """direction_progress entries include cost_by_scope and cost_by_status breakdowns."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: small/completed (on-demand, 3600s) and medium/failed (spot, 1800s)
    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "medium"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_by_scope"] is not None
    assert "small" in dp["cost_by_scope"]
    assert "medium" in dp["cost_by_scope"]
    assert dp["cost_by_scope"]["small"] > 0
    assert dp["cost_by_status"] is not None
    assert "COMPLETED" in dp["cost_by_status"]
    assert "FAILED" in dp["cost_by_status"]
    total = round(dp["cost_by_scope"]["small"] + dp["cost_by_scope"]["medium"], 2)
    assert total == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_budget_fields(
    client: AsyncClient, db_session, mocker
):
    """direction_progress exposes budget utilization and fires alert when >=80%."""
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    now = _utcnow()
    project = make_project(
        directions=[
            {
                "id": "dir-b",
                "text": "Budgeted dir",
                "status": "active",
                "cost_budget_usd": 0.01,  # 1 hour spot = $0.02 -> 200% utilization
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-b", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_budget_usd"] == 0.01
    assert dp["budget_utilization_pct"] is not None
    assert dp["budget_utilization_pct"] >= 80
    assert dp["budget_exceeded"] is True

    # Allow the scheduled ensure_future task to run.
    import asyncio as _a

    await _a.sleep(0)
    assert dispatch_mock.await_count == 1
    args, _kwargs = dispatch_mock.call_args
    assert args[1] == "BUDGET_EXCEEDED"
    assert args[2]["direction_id"] == "dir-b"


@pytest.mark.asyncio
async def test_digest_direction_progress_no_budget_fields(
    client: AsyncClient, db_session
):
    """When no cost_budget_usd is set, estimated_cost_usd is populated but utilization fields are null."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-nb", "text": "No budget dir", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-nb", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_budget_usd"] is None
    assert dp["budget_utilization_pct"] is None
    assert dp["budget_exceeded"] is False
    assert dp["estimated_cost_usd"] is not None
    assert dp["estimated_cost_usd"] > 0


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_provider_model(
    client: AsyncClient, db_session
):
    """direction_progress entries include cost_by_provider_model breakdown
    keyed by `<provider>|<model>`; per-provider sums match estimated_cost_usd."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-pm", "text": "PM split", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks split across (provider, model). One missing both fields buckets
    # under "unknown|unknown".
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-anth",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            provider="anthropic",
            model="claude-opus-4-7",
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-oai",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            provider="openai",
            model="gpt-5.4",
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-legacy",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            # No provider/model — legacy row should bucket under "unknown|unknown".
            vm_provisioned_at=now - timedelta(minutes=20),
            vm_stopped_at=now - timedelta(minutes=10),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    by_pm = dp["cost_by_provider_model"]
    assert by_pm is not None
    assert "anthropic|claude-opus-4-7" in by_pm
    assert "openai|gpt-5.4" in by_pm
    assert "unknown|unknown" in by_pm
    for v in by_pm.values():
        assert v > 0
    # Sum of provider/model breakdown matches the direction's overall cost.
    assert round(sum(by_pm.values()), 2) == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_type(client: AsyncClient, db_session):
    """direction_progress entries include cost_by_type breakdown keyed by task type."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_by_type"] is not None
    assert "research" in dp["cost_by_type"]
    assert "create" in dp["cost_by_type"]
    assert dp["cost_by_type"]["research"] > 0
    assert dp["cost_by_type"]["create"] > 0
    total = round(dp["cost_by_type"]["research"] + dp["cost_by_type"]["create"], 2)
    assert total == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_spot_vs_on_demand_cost_split(client: AsyncClient, db_session):
    """direction_progress and digest response expose spot/on-demand cost split."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Directed: one on-demand, one spot
    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "medium"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    # Undirected spot task contributes to top-level split only
    db_session.add(
        make_task(
            project_id=project.id,
            title="t3",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={},
            vm_provisioned_at=now - timedelta(hours=3),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()
    dp = body["direction_progress"][0]
    assert "spot_cost_usd" in dp
    assert "on_demand_cost_usd" in dp
    assert dp["spot_cost_usd"] > 0
    assert dp["on_demand_cost_usd"] > 0
    assert (
        round(dp["spot_cost_usd"] + dp["on_demand_cost_usd"], 2)
        == dp["estimated_cost_usd"]
    )

    assert "spot_cost_usd" in body
    assert "on_demand_cost_usd" in body
    # Top-level spot includes both directed spot task and undirected spot task
    assert body["spot_cost_usd"] > dp["spot_cost_usd"]
    assert body["on_demand_cost_usd"] == dp["on_demand_cost_usd"]


@pytest.mark.asyncio
async def test_digest_cost_by_failure_class(client: AsyncClient, db_session):
    """Digest response exposes per-failure-class cost breakdown and total failed cost."""
    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="timeout task",
            status=TaskStatus.FAILED,
            latest_status_message="",
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="external api task",
            status=TaskStatus.FAILED,
            latest_status_message="could not connect to server",
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert "cost_by_failure_class" in body
    assert "total_failed_cost_usd" in body
    cbfc = body["cost_by_failure_class"]
    assert "AGENT_TIMEOUT" in cbfc
    assert "EXTERNAL_API" in cbfc
    assert cbfc["AGENT_TIMEOUT"] > 0
    assert cbfc["EXTERNAL_API"] > 0
    assert round(sum(cbfc.values()), 4) == body["total_failed_cost_usd"]


@pytest.mark.asyncio
async def test_digest_scope_conversion_rates_absent_without_project(
    client: AsyncClient, db_session
):
    """scope_conversion_rates is null when no project_id is provided."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["scope_conversion_rates"] is None
    assert data["prev_scope_conversion_rates"] is None


@pytest.mark.asyncio
async def test_digest_prev_scope_conversion_rates(client: AsyncClient, db_session):
    """prev_scope_conversion_rates aggregates scope_breakdown from the previous window."""
    curr_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    prev_ts = (
        _utcnow() - timedelta(hours=36)
    ).isoformat()  # in prev 24h window (24–48h ago)

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": curr_ts,
                        "prs_merged": 2,
                        "tasks_created": 4,
                        "scope_breakdown": {
                            "medium": {"created": 4, "merged": 2, "failed": 0},
                        },
                    },
                    {
                        "timestamp": prev_ts,
                        "prs_merged": 1,
                        "tasks_created": 4,
                        "scope_breakdown": {
                            "medium": {"created": 4, "merged": 1, "failed": 0},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    # Current window rates
    assert data["scope_conversion_rates"]["medium"]["created"] == 4
    assert data["scope_conversion_rates"]["medium"]["merged"] == 2

    # Previous window rates (for trend comparison)
    prev_rates = data["prev_scope_conversion_rates"]
    assert prev_rates is not None
    assert prev_rates["medium"]["created"] == 4
    assert prev_rates["medium"]["merged"] == 1


@pytest.mark.asyncio
async def test_digest_direction_progress_filter_funnel(client: AsyncClient, db_session):
    """filter_funnel aggregates findings_produced, filter_breakdown, and batch_cap from research_history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 3,
                        "findings_produced": 10,
                        "batch_cap_applied": 2,
                        "filter_breakdown": {
                            "hash_dedup": 1,
                            "word_overlap_dedup": 2,
                            "file_overlap_dedup": 1,
                            "type_filtered": 0,
                            "empty_prompt": 1,
                        },
                    },
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 2,
                        "findings_produced": 4,
                        "batch_cap_applied": 0,
                        "filter_breakdown": {
                            "hash_dedup": 1,
                            "word_overlap_dedup": 0,
                            "file_overlap_dedup": 0,
                            "type_filtered": 1,
                            "empty_prompt": 0,
                        },
                    },
                    {
                        # Outside window — must not contribute
                        "timestamp": old_ts,
                        "tasks_created": 99,
                        "findings_produced": 99,
                        "batch_cap_applied": 99,
                        "filter_breakdown": {
                            "hash_dedup": 99,
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    ff = data["direction_progress"][0]["filter_funnel"]
    assert ff is not None
    assert ff["findings_produced"] == 14
    assert ff["hash_dedup"] == 2
    assert ff["word_overlap"] == 2
    assert ff["file_overlap"] == 1
    assert ff["type_filtered"] == 1
    assert ff["empty_prompt"] == 1
    assert ff["batch_cap"] == 2
    assert ff["tasks_created"] == 5

    # Pipeline-level aggregate mirrors per-direction totals when only one direction has activity
    pff = data["pipeline_filter_funnel"]
    assert pff is not None
    assert pff["findings_produced"] == 14
    assert pff["hash_dedup"] == 2
    assert pff["batch_cap"] == 2
    assert pff["tasks_created"] == 5


@pytest.mark.asyncio
async def test_digest_pipeline_filter_funnel_sums_across_directions(
    client: AsyncClient, db_session
):
    """pipeline_filter_funnel sums filter_funnel counters across all directions."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-a",
                "text": "Direction A",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 2,
                        "findings_produced": 5,
                        "batch_cap_applied": 1,
                        "filter_breakdown": {"hash_dedup": 1, "word_overlap_dedup": 1},
                    },
                ],
            },
            {
                "id": "dir-b",
                "text": "Direction B",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 3,
                        "findings_produced": 7,
                        "batch_cap_applied": 2,
                        "filter_breakdown": {"hash_dedup": 2, "type_filtered": 1},
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-a", "dir-b"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Research {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    pff = data["pipeline_filter_funnel"]
    assert pff is not None
    assert pff["findings_produced"] == 12
    assert pff["hash_dedup"] == 3
    assert pff["word_overlap"] == 1
    assert pff["type_filtered"] == 1
    assert pff["batch_cap"] == 3
    assert pff["tasks_created"] == 5


@pytest.mark.asyncio
async def test_digest_review_rejection_summary(client: AsyncClient, db_session):
    """rejection_reasons per direction and aggregate rejection_pattern_summary surface."""
    project = make_project(
        directions=[
            {"id": "dir-rej", "text": "Rejection dir", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Two rejected tasks with identical notes (after normalization) + one unique
    for i, (title, note) in enumerate(
        [
            ("Task A", "Scope too broad; split into smaller PRs"),
            ("Task B", "scope too broad;   split into smaller prs"),
            ("Task C", "Missing tests for new logic"),
        ]
    ):
        db_session.add(
            make_task(
                project_id=project.id,
                title=title,
                status=TaskStatus.FAILED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "dir-rej",
                    "pr_rejected": True,
                    "pr_rejection_note": note,
                },
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(minutes=30 + i),
            )
        )
    # Non-rejected failure should not count
    db_session.add(
        make_task(
            project_id=project.id,
            title="Unrelated failure",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-rej"},
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=10),
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    rps = data["rejection_pattern_summary"]
    assert rps["total_rejected"] == 3
    top = {n["snippet"]: n["count"] for n in rps["top_notes"]}
    assert top.get("scope too broad; split into smaller prs") == 2
    assert top.get("missing tests for new logic") == 1

    dp = next(d for d in data["direction_progress"] if d["direction_id"] == "dir-rej")
    reasons = dp["rejection_reasons"]
    assert reasons is not None
    assert len(reasons) == 3
    titles = {r["title"] for r in reasons}
    assert titles == {"Task A", "Task B", "Task C"}
    for r in reasons:
        assert "task_id" in r
        assert "rejection_note" in r


@pytest.mark.asyncio
async def test_digest_scope_mismatch_summary(client: AsyncClient, db_session):
    """scope_mismatch_summary aggregates calibrated tasks by original scope and direction."""
    project = make_project(
        directions=[
            {"id": "dir-sm1", "text": "Dir 1", "status": "active"},
            {"id": "dir-sm2", "text": "Dir 2", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm1",
                "scope": "small",
                "scope_calibrated_from": "medium",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm1",
                "scope": "small",
                "scope_calibrated_from": "medium:cross_module",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c3",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-sm1", "scope": "small"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c4",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "direction_id": "dir-sm2",
                "scope": "medium",
                "scope_calibrated_from": "large",
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    summary = data["scope_mismatch_summary"]
    assert summary is not None
    assert summary["total_calibrated"] == 3
    assert summary["calibrated_success_rate"] == round(2 / 3, 3)

    by_orig = summary["by_original_scope"]
    assert by_orig["medium"]["total"] == 2
    assert by_orig["medium"]["completed"] == 1
    assert by_orig["medium"]["failed"] == 1
    assert by_orig["large"]["total"] == 1
    assert by_orig["large"]["completed"] == 1

    top = summary["top_directions"]
    assert top[0]["direction_id"] == "dir-sm2"
    assert top[0]["calibration_rate"] == 1.0
    assert top[1]["direction_id"] == "dir-sm1"
    assert top[1]["calibrated_count"] == 2
    assert top[1]["total_tasks"] == 3


@pytest.mark.asyncio
async def test_digest_scope_mismatch_summary_empty(client: AsyncClient, db_session):
    """scope_mismatch_summary is None when no tasks have scope_calibrated_from."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="nc",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"scope": "small"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    assert response.json()["scope_mismatch_summary"] is None


@pytest.mark.asyncio
async def test_digest_retry_pattern_summary(client: AsyncClient, db_session):
    """retry_pattern_summary aggregates _failure_class_history across failed tasks."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks with identical repeating pattern (retry didn't help)
    db_session.add(
        make_task(
            project_id=project.id,
            title="r1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="r2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    # One task where retry changed class (not a repeat)
    db_session.add(
        make_task(
            project_id=project.id,
            title="r3",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["PROVISION_FAILURE", "CODE_ERROR"]},
        )
    )
    # Single-entry history — should be ignored
    db_session.add(
        make_task(
            project_id=project.id,
            title="r4",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR"]},
        )
    )
    # Completed task — should be ignored even with history
    db_session.add(
        make_task(
            project_id=project.id,
            title="r5",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    rps = response.json()["retry_pattern_summary"]
    assert rps["total_multi_attempt_failures"] == 3
    assert rps["repeat_class_count"] == 2
    top = rps["top_patterns"]
    assert top[0]["sequence"] == "CODE_ERROR -> CODE_ERROR"
    assert top[0]["count"] == 2
    sequences = {p["sequence"] for p in top}
    assert "PROVISION_FAILURE -> CODE_ERROR" in sequences


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary(client: AsyncClient, db_session):
    """review_cap_exhaustion_summary counts FAILED tasks with failure_detail=review_cap_exhausted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 16,
                "direction_id": "d-aaa",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 12,
                "direction_id": "d-aaa",
            },
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="c3",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 8,
                "direction_id": "d-bbb",
            },
        )
    )
    # Unrelated FAILED task — should NOT be counted
    db_session.add(
        make_task(
            project_id=project.id,
            title="other",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={"failure_detail": "something_else", "direction_id": "d-ccc"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    assert summary["count"] == 3
    assert summary["avg_iterations"] == pytest.approx((16 + 12 + 8) / 3)
    top = summary["top_directions"]
    assert top[0] == {"direction_id": "d-aaa", "count": 2}
    dirs = {d["direction_id"] for d in top}
    assert "d-bbb" in dirs
    assert "d-ccc" not in dirs
    assert "total_cost_usd" in summary
    assert "avg_cost_usd" in summary


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary_cost(
    client: AsyncClient, db_session
):
    """review_cap_exhaustion_summary.total_cost_usd sums VM cost for cap-exhausted tasks."""
    from app.routers.admin_analytics import SPOT_RATE, ON_DEMAND_RATE
    from datetime import datetime, timezone

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 1 hour spot task
    db_session.add(
        make_task(
            project_id=project.id,
            title="c1",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 16,
                "direction_id": "d-aaa",
            },
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    # 30 min on-demand task
    db_session.add(
        make_task(
            project_id=project.id,
            title="c2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "create"}],
            context={
                "failure_detail": "review_cap_exhausted",
                "review_cap_iterations": 12,
                "direction_id": "d-aaa",
            },
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    expected = SPOT_RATE * 3600 + ON_DEMAND_RATE * 1800
    assert summary["total_cost_usd"] == pytest.approx(round(expected, 4))
    assert summary["avg_cost_usd"] == pytest.approx(round(expected / 2, 4))


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary_empty(
    client: AsyncClient, db_session
):
    """review_cap_exhaustion_summary returns zero count and None avg when no cap exhaustion."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="ok",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    assert summary["count"] == 0
    assert summary["avg_iterations"] is None
    assert summary["top_directions"] == []
    assert summary["total_cost_usd"] == 0.0
    assert summary["avg_cost_usd"] is None


@pytest.mark.asyncio
async def test_failure_stage_distribution_aggregates_stages(
    client: AsyncClient, db_session
):
    """failure_stage_distribution tallies _previous_failure_stage and falls back to _derive_failure_stage."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="ctx stage",
            status=TaskStatus.FAILED,
            context={"_previous_failure_stage": "test"},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="derived lint",
            status=TaskStatus.FAILED,
            latest_status_message="ruff lint error in handler",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="derived lint 2",
            status=TaskStatus.FAILED,
            latest_status_message="eslint failure",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="provision",
            status=TaskStatus.FAILED,
            context={"failure_category": "clone_failed"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    dist = data["failure_stage_distribution"]
    assert dist["stages"]["test"] == 1
    assert dist["stages"]["lint"] == 2
    assert dist["stages"]["provision"] == 1
    assert dist["total_classified"] == 4
    assert dist["dominant_stage"] == "lint"


@pytest.mark.asyncio
async def test_digest_retry_overhead_cost_computation(client: AsyncClient, db_session):
    """Digest response surfaces retry_overhead_cost_usd, retry_task_count,
    and avg_retries_before_failure for failed tasks with _attempt_number > 1."""
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Retry on-demand: attempt 2, 1h of VM time (in-window)
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry on-demand",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 2},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    # Retry spot: attempt 3, 30m of VM time (in-window)
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry spot",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 3},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    # First-attempt failure: must be excluded from retry counts
    db_session.add(
        make_task(
            project_id=project.id,
            title="first attempt",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 1},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    # Retry with no VM timing: counts toward retry_task_count but not cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="retry no-vm",
            status=TaskStatus.FAILED,
            context={"_attempt_number": 4},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert body["retry_task_count"] == 3  # attempts 2, 3, 4 (attempt 1 excluded)
    # avg = (2 + 3 + 4) / 3 = 3.0
    assert body["avg_retries_before_failure"] == pytest.approx(3.0)

    expected_cost = ON_DEMAND_RATE * 3600 + SPOT_RATE * 1800
    assert body["retry_overhead_cost_usd"] == pytest.approx(round(expected_cost, 4))


@pytest.mark.asyncio
async def test_digest_retry_recovery_cost_from_prior_attempts(
    client: AsyncClient, db_session
):
    """retry_recovery_cost_usd sums _prior_attempts_cost across completed tasks
    with _attempt_number > 1 — the compute burned on failed attempts that
    eventually succeeded via retry."""
    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Successful retry with recorded prior-attempt cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="succeeded on retry",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 2, "_prior_attempts_cost": 0.50},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    # Successful retry with multi-attempt accumulated cost
    db_session.add(
        make_task(
            project_id=project.id,
            title="succeeded on 3rd",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 3, "_prior_attempts_cost": 1.25},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=15),
            vm_spot=True,
        )
    )
    # First-attempt success — excluded (no prior cost)
    db_session.add(
        make_task(
            project_id=project.id,
            title="first attempt success",
            status=TaskStatus.COMPLETED,
            context={"_attempt_number": 1, "_prior_attempts_cost": 9.0},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()

    assert body["retry_recovery_cost_usd"] == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_digest_escalation_outcome_summary(client: AsyncClient, db_session):
    """escalation_outcome_summary aggregates escalation_outcomes across directions and recent history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-e1",
                "text": "Dir 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 4,
                                "merged": 2,
                                "failed": 2,
                            },
                            "medium_to_large": {
                                "attempted": 1,
                                "merged": 0,
                                "failed": 1,
                            },
                        },
                    },
                    {
                        "timestamp": old_ts,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 99,
                                "merged": 99,
                                "failed": 0,
                            },
                        },
                    },
                ],
            },
            {
                "id": "dir-e2",
                "text": "Dir 2",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts2,
                        "escalation_outcomes": {
                            "small_to_medium": {
                                "attempted": 2,
                                "merged": 1,
                                "failed": 1,
                            },
                            "small_to_large": {
                                "attempted": 1,
                                "merged": 1,
                                "failed": 0,
                            },
                        },
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["escalation_outcome_summary"]
    assert summary is not None
    assert summary["total_attempted"] == 8
    assert summary["total_merged"] == 4
    assert summary["total_failed"] == 4
    assert summary["overall_success_rate"] == 0.5

    by_path = summary["by_path"]
    assert by_path["small_to_medium"] == {
        "attempted": 6,
        "merged": 3,
        "failed": 3,
        "success_rate": 0.5,
    }
    assert by_path["small_to_large"]["attempted"] == 1
    assert by_path["small_to_large"]["success_rate"] == 1.0
    assert by_path["medium_to_large"]["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_digest_remediation_funnel(client: AsyncClient, db_session):
    """investigate + remediation tasks surface in digest.remediation_funnel."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    inv_created = now - timedelta(hours=3)
    rem_completed = now - timedelta(hours=1)

    # Completed investigation with a parseable diagnosis summary
    inv_done = make_task(
        project_id=project.id,
        title="investigate A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=inv_created,
        plan_output={"summary": "root cause X"},
    )
    # In-flight investigation (counts as active)
    inv_active = make_task(
        project_id=project.id,
        title="investigate B",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
    )
    # Completed investigation without summary (counts but not with_diagnosis)
    inv_no_summary = make_task(
        project_id=project.id,
        title="investigate C",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=4),
        plan_output={"raw": "logs"},
    )
    # Investigation used to validate the fallback path (rem_merged2 has no
    # trigger_failure_class in its own context, so digest should fall back to
    # this investigation's context.last_failure_class).
    inv_fallback = make_task(
        project_id=project.id,
        title="investigate D",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={"summary": "root cause Y"},
        context={"last_failure_class": "LINT_ERROR"},
    )
    db_session.add_all([inv_done, inv_active, inv_no_summary, inv_fallback])
    await db_session.commit()
    await db_session.refresh(inv_done)
    await db_session.refresh(inv_fallback)

    rem_merged = make_task(
        project_id=project.id,
        title="remediation A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_done.id),
            "direction_id": "d1",
            "trigger_failure_class": "CODE_ERROR",
        },
        status_log=[{"t": rem_completed.isoformat(), "status": "completed"}],
    )
    # Second merged remediation: no trigger_failure_class in its own context,
    # but its linked investigation has last_failure_class — exercises fallback.
    rem_merged_fallback = make_task(
        project_id=project.id,
        title="remediation C",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={"diagnosis_task_id": str(inv_fallback.id), "direction_id": "d1"},
        status_log=[{"t": rem_completed.isoformat(), "status": "completed"}],
    )
    rem_failed = make_task(
        project_id=project.id,
        title="remediation B",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={"diagnosis_task_id": str(inv_done.id), "direction_id": "d1"},
    )
    db_session.add_all([rem_merged, rem_merged_fallback, rem_failed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    funnel = response.json()["remediation_funnel"]

    assert funnel["investigations_spawned"] == 4
    assert funnel["investigations_completed"] == 3
    assert funnel["investigations_with_diagnosis"] == 2
    assert funnel["active_investigations"] == 1
    assert funnel["remediations_spawned"] == 3
    assert funnel["remediations_merged"] == 2
    assert funnel["remediations_failed"] == 1
    # inv_done created 3h ago, rem_merged completed 1h ago → ~2h
    assert funnel["avg_time_to_remediation_hours"] is not None
    assert 1.5 <= funnel["avg_time_to_remediation_hours"] <= 2.5
    # rem_merged has trigger_failure_class=CODE_ERROR directly;
    # rem_merged_fallback has no trigger_failure_class but its linked
    # investigation has context.last_failure_class=LINT_ERROR.
    assert funnel["merged_remediations_by_trigger_class"] == {
        "CODE_ERROR": 1,
        "LINT_ERROR": 1,
    }
    # Per-class investigation stage counts: inv_fallback has
    # context.last_failure_class=LINT_ERROR; the other three investigations
    # have no class set and bucket into "unknown".
    assert funnel["investigations_spawned_by_class"] == {
        "LINT_ERROR": 1,
        "unknown": 3,
    }
    # Two completed with no class (inv_done, inv_no_summary) and one completed
    # with LINT_ERROR (inv_fallback). inv_active is IN_PROGRESS so excluded.
    assert funnel["investigations_completed_by_class"] == {
        "LINT_ERROR": 1,
        "unknown": 2,
    }
    # rem_merged has trigger CODE_ERROR; the other two remediations have
    # no trigger class set → "unknown".
    assert funnel["remediations_spawned_by_class"] == {
        "CODE_ERROR": 1,
        "unknown": 2,
    }


@pytest.mark.asyncio
async def test_compute_remediation_funnel_helper_direct(db_session):
    """compute_remediation_funnel computes the funnel directly off a session.

    Exercises the extracted helper in isolation (no FastAPI layer) and confirms
    the residual_remediation_funnel passed in lands verbatim in the returned dict
    at its expected key.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)

    inv_done = make_task(
        project_id=project.id,
        title="investigate A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={
            "summary": "root cause X",
            "root_cause": "null payload field",
            "recommended_fix": "add a null guard before the access",
            "confidence": "high",
        },
        context={"last_failure_class": "CODE_ERROR"},
    )
    inv_active = make_task(
        project_id=project.id,
        title="investigate B",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
    )
    db_session.add_all([inv_done, inv_active])
    await db_session.commit()
    await db_session.refresh(inv_done)

    rem_merged = make_task(
        project_id=project.id,
        title="remediation A",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_done.id),
            "direction_id": "d1",
            "trigger_failure_class": "CODE_ERROR",
        },
        status_log=[
            {"t": (now - timedelta(hours=1)).isoformat(), "status": "completed"}
        ],
    )
    db_session.add(rem_merged)
    await db_session.commit()

    sentinel_residual = {"spawned": 7, "marker": "residual-stays-here"}
    funnel, investigation_conversion = await compute_remediation_funnel(
        db_session,
        project_id=project.id,
        window_start=window_start,
        residual_remediation_funnel=sentinel_residual,
    )

    assert funnel["investigations_spawned"] == 2
    assert funnel["investigations_completed"] == 1
    assert funnel["investigations_with_diagnosis"] == 1
    assert funnel["active_investigations"] == 1
    assert funnel["remediations_spawned"] == 1
    assert funnel["remediations_merged"] == 1
    assert funnel["remediations_failed"] == 0
    assert funnel["merged_remediations_by_trigger_class"] == {"CODE_ERROR": 1}
    assert funnel["investigations_spawned_by_class"] == {
        "CODE_ERROR": 1,
        "unknown": 1,
    }
    # The caller-supplied residual funnel is spliced through unchanged.
    assert funnel["residual_remediation_funnel"] is sentinel_residual
    # Active-investigation detail surfaces the in-flight investigation only.
    assert [d["task_id"] for d in funnel["active_investigations_detail"]] == [
        str(inv_active.id)
    ]
    # investigation_conversion is returned separately as a top-level digest key.
    assert investigation_conversion["total_completed"] == 1
    assert investigation_conversion["parseable"] == 1
    assert investigation_conversion["actionable"] == 1
    assert investigation_conversion["remediation_spawned"] == 1


@pytest.mark.asyncio
async def test_compute_remediation_and_patterns_helper_direct(db_session):
    """compute_remediation_and_patterns returns all four expected top-level keys.

    Exercises the extracted helper in isolation (no FastAPI layer) and confirms
    it returns a dict with remediation_funnel, investigation_conversion,
    recurring_patterns, and early_warnings.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)

    result = await compute_remediation_and_patterns(
        session=db_session,
        project_id=project.id,
        window_start=window_start,
        failed_tasks=[],
        all_tasks=[],
        active_tasks_all=[],
        dir_id_to_text={},
    )

    assert set(result.keys()) == {
        "remediation_funnel",
        "investigation_conversion",
        "recurring_patterns",
        "early_warnings",
    }
    assert isinstance(result["recurring_patterns"], list)
    assert isinstance(result["early_warnings"], list)
    assert isinstance(result["remediation_funnel"], dict)
    assert isinstance(result["investigation_conversion"], dict)


@pytest.mark.asyncio
async def test_digest_cumulative_merged_by_trigger_class(
    client: AsyncClient, db_session
):
    """cumulative_merged_by_trigger_class counts across all time; per-window counts only in-window."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    inv_in = make_task(
        project_id=project.id,
        title="investigate in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={"summary": "rc"},
    )
    db_session.add(inv_in)
    await db_session.commit()
    await db_session.refresh(inv_in)

    # In-window diagnosis-driven remediation
    rem_in = make_task(
        project_id=project.id,
        title="rem in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "diagnosis_task_id": str(inv_in.id),
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
        },
    )
    # Out-of-window diagnosis-driven remediation
    rem_old = make_task(
        project_id=project.id,
        title="rem out-of-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(days=5),
        context={
            "diagnosis_task_id": "00000000-0000-0000-0000-000000000001",
            "trigger_failure_class": "CODE_ERROR",
            "pr_merged": True,
        },
    )
    # In-window CI auto-remediation
    ci_in = make_task(
        project_id=project.id,
        title="ci in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "LINT_ERROR",
            "pr_merged": True,
        },
    )
    # Out-of-window CI auto-remediation
    ci_old = make_task(
        project_id=project.id,
        title="ci out-of-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(days=10),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "LINT_ERROR",
            "pr_merged": True,
        },
    )
    # COMPLETED CI auto-remediation WITHOUT pr_merged — should NOT be counted
    ci_completed_not_merged = make_task(
        project_id=project.id,
        title="ci completed not merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={"auto_remediation": True, "trigger_failure_class": "LINT_ERROR"},
    )
    db_session.add_all([rem_in, rem_old, ci_in, ci_old, ci_completed_not_merged])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    funnel = response.json()["remediation_funnel"]

    # Per-window only sees rem_in (diagnosis-driven, in-window)
    assert funnel["merged_remediations_by_trigger_class"] == {"CODE_ERROR": 1}
    # Cumulative includes both diagnosis-driven and CI auto-remediation, all time
    assert funnel["cumulative_merged_by_trigger_class"] == {
        "CODE_ERROR": 2,
        "LINT_ERROR": 2,
    }


@pytest.mark.asyncio
async def test_digest_ci_auto_remediation(client: AsyncClient, db_session):
    """CI auto-remediation tasks (no diagnosis_task_id) surface in ci_auto_remediation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    ci_merged = make_task(
        project_id=project.id,
        title="ci auto-remediation merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "test",
            "pr_merged": True,
        },
    )
    ci_completed_not_merged = make_task(
        project_id=project.id,
        title="ci auto-remediation completed but not merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "test",
        },
    )
    ci_failed = make_task(
        project_id=project.id,
        title="ci auto-remediation failed",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "trigger_failure_class": "CODE_ERROR",
            "ci_failure_type": "build",
        },
    )
    db_session.add_all([ci_merged, ci_completed_not_merged, ci_failed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    ci = response.json()["remediation_funnel"]["ci_auto_remediation"]

    assert ci["spawned"] == 3
    assert ci["merged"] == 1
    assert ci["failed"] == 1
    assert ci["by_ci_failure_type"]["test"] == {"spawned": 2, "merged": 1, "failed": 0}
    assert ci["by_ci_failure_type"]["build"] == {"spawned": 1, "merged": 0, "failed": 1}


@pytest.mark.asyncio
async def test_digest_includes_first_review_approval_rate(
    client: AsyncClient, db_session
):
    """/api/admin/digest returns first_review_approval_rate with expected structure."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Merged create task approved on first review (no rejection categories).
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "pr_merged": True,
                "scope": "small",
                "_had_playbook": True,
                "_had_review_measurements": True,
            },
        )
    )
    # Merged refactor task rejected on first review.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "refactor"}],
            context={
                "pr_merged": True,
                "scope": "medium",
                "_had_playbook": False,
                "_had_review_measurements": False,
                "_first_attempt_rejection_categories": ["correctness"],
            },
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    fra = data["first_review_approval_rate"]
    assert set(fra.keys()) >= {
        "overall",
        "sample_size",
        "by_type",
        "by_scope",
        "with_playbook",
        "without_playbook",
        "with_measurements",
        "without_measurements",
    }
    assert fra["sample_size"] == 2
    assert fra["overall"] == 0.5
    assert fra["by_type"]["create"] == 1.0
    assert fra["by_type"]["refactor"] == 0.0
    assert fra["by_scope"]["small"] == 1.0
    assert fra["by_scope"]["medium"] == 0.0
    assert fra["with_playbook"] == 1.0
    assert fra["without_playbook"] == 0.0
    assert fra["with_measurements"] == 1.0
    assert fra["without_measurements"] == 0.0


@pytest.mark.asyncio
async def test_digest_prior_investigation_reuse(client: AsyncClient, db_session):
    """CI auto-remediation tasks with prior_diagnosis_task_id surface in prior_investigation_reuse."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    with_prior_merged = make_task(
        project_id=project.id,
        title="ci rem with prior merged",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=3),
        context={
            "auto_remediation": True,
            "prior_diagnosis_task_id": "inv-1",
            "ci_failure_type": "test",
        },
    )
    with_prior_failed = make_task(
        project_id=project.id,
        title="ci rem with prior failed",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={
            "auto_remediation": True,
            "prior_diagnosis_task_id": "inv-2",
            "ci_failure_type": "build",
        },
    )
    without_prior = make_task(
        project_id=project.id,
        title="ci rem no prior",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={
            "auto_remediation": True,
            "ci_failure_type": "test",
        },
    )
    db_session.add_all([with_prior_merged, with_prior_failed, without_prior])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    reuse = response.json()["remediation_funnel"]["prior_investigation_reuse"]

    assert reuse["total_with_prior"] == 2
    assert reuse["merged_with_prior"] == 1
    assert reuse["failed_with_prior"] == 1
    assert reuse["merge_rate"] == 0.5


@pytest.mark.asyncio
async def test_digest_investigation_conversion_breakdown(
    client: AsyncClient, db_session
):
    """investigate tasks categorize into structural/low/actionable/unparseable buckets."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    inv_structural = make_task(
        project_id=project.id,
        title="inv structural",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=5),
        plan_output={
            "summary": "scope problem",
            "root_cause": "direction scope is too broad",
            "recommended_fix": "narrow the direction",
            "confidence": "high",
        },
    )
    inv_low = make_task(
        project_id=project.id,
        title="inv low confidence",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=4),
        plan_output={
            "summary": "unclear",
            "root_cause": "flaky test",
            "recommended_fix": "retry",
            "confidence": "low",
        },
    )
    inv_actionable = make_task(
        project_id=project.id,
        title="inv actionable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=3),
        plan_output={
            "summary": "null deref",
            "root_cause": "missing null check",
            "recommended_fix": "add guard clause in foo()",
            "confidence": "high",
        },
    )
    inv_unparseable = make_task(
        project_id=project.id,
        title="inv unparseable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=2),
        plan_output={"raw": "garbage logs"},
    )
    inv_nofix = make_task(
        project_id=project.id,
        title="inv no recommended fix",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        created_at=now - timedelta(hours=1),
        plan_output={
            "summary": "something",
            "root_cause": "unknown",
            "recommended_fix": "",
            "confidence": "medium",
        },
    )
    db_session.add_all(
        [inv_structural, inv_low, inv_actionable, inv_unparseable, inv_nofix]
    )
    await db_session.commit()
    await db_session.refresh(inv_actionable)

    rem_child = make_task(
        project_id=project.id,
        title="remediation of actionable",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=2),
        context={"diagnosis_task_id": str(inv_actionable.id), "direction_id": "d1"},
    )
    db_session.add(rem_child)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    ic = response.json()["investigation_conversion"]

    assert ic["total_completed"] == 5
    assert ic["parseable"] == 4
    assert ic["unparseable"] == 1
    assert ic["structural"] == 1
    assert ic["low_confidence"] == 1
    assert ic["actionable"] == 1
    assert ic["remediation_spawned"] == 1

    drop_off = response.json()["remediation_funnel"]["diagnosis_drop_off_by_reason"]
    assert drop_off.get("structural") == 1
    assert drop_off.get("low_confidence") == 1
    assert drop_off.get("no_recommended_fix") == 1
    assert "no_direction" not in drop_off


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_positive(client: AsyncClient, db_session):
    """prevention_efficacy reports positive when preventive merge rate > overall."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p1",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 10,
                        "prs_merged": 3,
                        "preventive_context_tasks_created": 4,
                        "preventive_context_tasks_merged": 3,
                        "preventive_context_tasks_failed": 1,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_tasks_created"] == 4
    assert eff["preventive_tasks_merged"] == 3
    assert eff["preventive_tasks_failed"] == 1
    assert eff["preventive_merge_rate"] == 0.75
    assert eff["overall_merge_rate"] == 0.3
    assert eff["assessment"] == "positive"


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_neutral(client: AsyncClient, db_session):
    """prevention_efficacy reports neutral when rates are similar."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p2",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 10,
                        "prs_merged": 5,
                        "preventive_context_tasks_created": 4,
                        "preventive_context_tasks_merged": 2,
                        "preventive_context_tasks_failed": 2,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_merge_rate"] == 0.5
    assert eff["overall_merge_rate"] == 0.5
    assert eff["assessment"] == "neutral"


@pytest.mark.asyncio
async def test_digest_prevention_efficacy_no_data(client: AsyncClient, db_session):
    """prevention_efficacy returns no_data assessment when zero preventive tasks."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-p3",
                "text": "D",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 5,
                        "prs_merged": 2,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    eff = response.json()["prevention_efficacy"]
    assert eff["preventive_tasks_created"] == 0
    assert eff["preventive_merge_rate"] is None
    assert eff["overall_merge_rate"] == 0.4
    assert eff["assessment"] == "no_data"


# ---------------------------------------------------------------------------
# Anomaly adjustment snapshot in digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_anomaly_adjustment_inactive_by_default(client: AsyncClient):
    """When no anomaly throttle is active, digest exposes an inactive snapshot."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    adj = response.json()["anomaly_adjustment"]
    assert adj["active"] is False
    assert adj["trigger_anomalies"] == []


@pytest.mark.asyncio
async def test_digest_includes_anomaly_adjustment_when_active(client: AsyncClient):
    """When anomaly_adjustments is active, digest reflects trigger + reduction info."""
    from app import shared_state

    prev = dict(shared_state.anomaly_adjustments)
    try:
        shared_state.anomaly_adjustments.update(
            {
                "active": True,
                "adjusted_at": "2026-04-20T12:00:00",
                "trigger_anomalies": ["PROVISION_FAILURE_SURGE", "EXTERNAL_API_SURGE"],
                "pre_adjustment_max_vms": 20,
                "adjusted_max_vms": 8,
                "failure_gate_threshold": 0.6,
                "pre_adjustment_failure_gate": 0.8,
                "forced_restoration_count": 1,
                "forced_restored_at": "2026-04-20T11:00:00",
            }
        )
        response = await client.get("/api/admin/digest")
        assert response.status_code == 200
        adj = response.json()["anomaly_adjustment"]
        assert adj["active"] is True
        assert adj["adjusted_at"] == "2026-04-20T12:00:00"
        assert adj["trigger_anomalies"] == [
            "PROVISION_FAILURE_SURGE",
            "EXTERNAL_API_SURGE",
        ]
        assert adj["pre_adjustment_max_vms"] == 20
        assert adj["adjusted_max_vms"] == 8
        assert adj["failure_gate_threshold"] == 0.6
        assert adj["pre_adjustment_failure_gate"] == 0.8
        assert adj["forced_restoration_count"] == 1
    finally:
        shared_state.anomaly_adjustments.clear()
        shared_state.anomaly_adjustments.update(prev)


# ---------------------------------------------------------------------------
# JSONB projection tests (OOM mitigation — see admin_digest.py tasks_result)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_uses_first_payload_projection(client: AsyncClient, db_session):
    """Digest query projects payloads[0] SQL-side instead of loading the full array.

    Regression guard for the OOM fix: even with a multi-element payloads array,
    only payloads[0] should drive first_payload-keyed metrics (research_cycles,
    task_type classification), matching the Task.first_payload property.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Multi-payload research task",
        status=TaskStatus.COMPLETED,
        payloads=[
            {"type": "research"},
            {"type": "create"},
            {"type": "create"},
        ],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["research_cycles"] == 1
    assert data["completed_count"] == 1


@pytest.mark.asyncio
async def test_digest_derives_completed_at_from_status_log_projection(
    client: AsyncClient,
    db_session,
):
    """_derive_completed_at reads the SQL-projected status_log[-1]->>'t' timestamp.

    Place a task's status_log completion timestamp OUTSIDE the default 24h window
    but its created_at INSIDE the extended lookback; the digest should still
    include it in recent_completions if projection is wired correctly and exclude
    it if the completion timestamp actually fell outside the window.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Completion timestamp 12h ago — well within the default 24h window.
    recent_complete = (now - timedelta(hours=12)).isoformat()
    task_in_window = make_task(
        project_id=project.id,
        title="Completed in window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=24),
        status_log=[{"t": recent_complete, "status": "COMPLETED"}],
    )
    # Completion timestamp 40h ago — outside the 24h window, should not count.
    old_complete = (now - timedelta(hours=40)).isoformat()
    task_out_of_window = make_task(
        project_id=project.id,
        title="Completed out of window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=48),
        status_log=[{"t": old_complete, "status": "COMPLETED"}],
    )
    db_session.add(task_in_window)
    db_session.add(task_out_of_window)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()
    titles = {c["title"] for c in data["recent_completions"]}
    assert "Completed in window" in titles
    assert "Completed out of window" not in titles


@pytest.mark.asyncio
async def test_digest_handles_empty_payloads_and_status_log(
    client: AsyncClient, db_session
):
    """Empty payloads and status_log must not crash the projection path.

    Edge case: SQL `payloads[0]` returns NULL for empty array; `status_log[-1]->>'t'`
    returns NULL too. Downstream must fall back gracefully.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Empty JSONB task",
        status=TaskStatus.COMPLETED,
        payloads=[],
        status_log=[],
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_count"] == 1


@pytest.mark.asyncio
async def test_stash_projected_task_fields_works_for_orm_loaded_tasks(db_session):
    """Transient projection fields must attach safely to ORM-loaded SQLModel rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Projected task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        status_log=[],
    )
    db_session.add(task)
    await db_session.commit()

    loaded = (
        await db_session.execute(select(Task).where(Task.id == task.id))
    ).scalar_one()
    projected_completed_at = (_utcnow() - timedelta(hours=1)).isoformat()

    _stash_projected_task_fields(
        loaded,
        first_payload={"type": "research"},
        completed_at_ts=projected_completed_at,
    )

    assert loaded.first_payload == {"type": "research"}
    assert _derive_completed_at(loaded) == datetime.fromisoformat(
        projected_completed_at
    )


# ---------------------------------------------------------------------------
# /api/admin/residual-fingerprints tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_fingerprints_empty(client: AsyncClient):
    """No tasks → empty residuals list."""
    response = await client.get("/api/admin/residual-fingerprints")
    assert response.status_code == 200
    data = response.json()
    assert data["total_code_error_count"] == 0
    assert data["residual_count"] == 0
    assert data["top_residuals"] == []


@pytest.mark.asyncio
async def test_residual_fingerprints_clusters_code_errors(
    client: AsyncClient, db_session
):
    """FAILED tasks without failure_category cluster by fingerprint; excludes non-CODE_ERROR."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks with nearly identical message — same fingerprint cluster
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                latest_status_message="compilation failed: unexpected token in expression",
            )
        )
    # One task with a different message — second fingerprint
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="assertion error: value mismatch in test suite",
        )
    )
    # Task with failure_category set — excluded from residuals
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="compilation failed: unexpected token in expression",
            context={"failure_category": "CODE_ERROR"},
        )
    )
    # PROVISION_FAILURE — classify_failure returns non-CODE_ERROR, excluded
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="quota exceeded: cpus_all_regions limit reached",
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/residual-fingerprints")
    assert response.status_code == 200
    data = response.json()

    assert (
        data["total_code_error_count"] == 3
    )  # 2 + 1, excludes categorized + provision
    assert data["residual_count"] == 2
    top = data["top_residuals"]
    assert len(top) == 2
    # First entry has the highest count
    assert top[0]["count"] == 2
    assert top[1]["count"] == 1
    entry = top[0]
    assert "fingerprint" in entry
    assert "most_recent_at" in entry
    assert "sample_message" in entry
    assert isinstance(entry["example_task_ids"], list)
    assert len(entry["example_task_ids"]) <= 3


@pytest.mark.asyncio
async def test_residual_fingerprints_requires_auth(unauthenticated_client: AsyncClient):
    """Endpoint requires admin auth."""
    response = await unauthenticated_client.get("/api/admin/residual-fingerprints")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_clear_residual_fingerprints_empties_store(client: AsyncClient):
    """POST /residual-fingerprints/clear wipes the in-memory store and seen-task set."""
    from app.failure_classification import _residual_store, _residual_seen_task_ids

    _residual_store.clear()
    _residual_seen_task_ids.clear()
    _residual_store["fp-aaa"] = {"count": 2, "sample_message": "error: foo"}
    _residual_store["fp-bbb"] = {"count": 1, "sample_message": "error: bar"}
    _residual_seen_task_ids.add("task-id-x")

    response = await client.post("/api/admin/residual-fingerprints/clear")
    assert response.status_code == 200
    data = response.json()
    assert data["cleared"] == 2
    assert len(_residual_store) == 0
    assert len(_residual_seen_task_ids) == 0


@pytest.mark.asyncio
async def test_clear_residual_fingerprints_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Clear endpoint requires admin auth."""
    response = await unauthenticated_client.post(
        "/api/admin/residual-fingerprints/clear"
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_ignore_residual_case_creates_db_row(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/ignore creates an IGNORED row and returns it."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-abc"
    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/ignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "IGNORED"
    assert "id" in data


@pytest.mark.asyncio
async def test_ignore_residual_case_idempotent(client: AsyncClient, db_session):
    """Calling ignore twice for the same fingerprint/project is idempotent."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-dup"
    for _ in range(2):
        response = await client.post(
            f"/api/admin/residual-cases/{fingerprint}/ignore",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "IGNORED"


@pytest.mark.asyncio
async def test_ignore_residual_case_requires_auth(unauthenticated_client: AsyncClient):
    """Ignore endpoint requires admin auth."""
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/ignore",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unignore_residual_case_reactivates(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/unignore sets an IGNORED case back to ACTIVE."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-unignore"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.IGNORED,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "ACTIVE"
    assert "id" in data


@pytest.mark.asyncio
async def test_unignore_residual_case_404_when_missing(client: AsyncClient, db_session):
    """POST /residual-cases/{fp}/unignore returns 404 when no case row exists."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/residual-cases/nonexistent-fp/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unignore_residual_case_404_when_not_ignored(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unignore returns 404 when case is not IGNORED."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fingerprint-active"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.ACTIVE,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unignore",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unignore_residual_case_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Unignore endpoint requires admin auth."""
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/unignore",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code in (401, 403)


# ── POST /residual-cases/{fp}/unsuspend ──────────────────────────────────────


@pytest.mark.asyncio
async def test_unsuspend_residual_case_clears_state(
    client: AsyncClient, db_session, mocker
):
    """POST /residual-cases/{fp}/unsuspend transitions SUSPENDED→ACTIVE and clears fields."""
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-unsuspend"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.SUSPENDED,
        suspended_at=_utcnow(),
        consecutive_failure_count=3,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["fingerprint"] == fingerprint
    assert data["status"] == "ACTIVE"
    assert data["consecutive_failure_count"] == 0
    assert data["suspended_at"] is None
    assert "id" in data

    # Verify DB row was updated
    await db_session.refresh(case)
    assert case.status == ResidualCaseStatus.ACTIVE
    assert case.consecutive_failure_count == 0
    assert case.suspended_at is None


@pytest.mark.asyncio
async def test_unsuspend_residual_case_404_when_missing(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unsuspend returns 404 when no case row exists."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/residual-cases/nonexistent-fp/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsuspend_residual_case_404_when_not_suspended(
    client: AsyncClient, db_session
):
    """POST /residual-cases/{fp}/unsuspend returns 404 when case is ACTIVE (not SUSPENDED)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-not-suspended"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.ACTIVE,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unsuspend_residual_case_emits_message_and_webhook(
    client: AsyncClient, db_session, mocker
):
    """POST /residual-cases/{fp}/unsuspend creates a ProjectMessage and fires the webhook."""
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "err-fp-webhook-test"
    case = ResidualRemediationCase(
        project_id=project.id,
        fingerprint=fingerprint,
        status=ResidualCaseStatus.SUSPENDED,
        suspended_at=_utcnow(),
        consecutive_failure_count=3,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/residual-cases/{fingerprint}/unsuspend",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200

    # Verify a ProjectMessage with the correct event_type was created
    from app.models import ProjectMessage

    result = await db_session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
        )
    )
    messages = result.scalars().all()
    unsuspend_msgs = [
        m
        for m in messages
        if (m.context or {}).get("event_type") == "residual_fix_unsuspended"
    ]
    assert len(unsuspend_msgs) == 1
    assert fingerprint in unsuspend_msgs[0].content

    # Verify dispatch was called with RESIDUAL_FIX_UNSUSPENDED
    assert dispatch_mock.called
    call_args = dispatch_mock.call_args_list
    event_types = [
        c.args[1] if c.args else c.kwargs.get("event_type") for c in call_args
    ]
    assert "RESIDUAL_FIX_UNSUSPENDED" in event_types


@pytest.mark.asyncio
async def test_unsuspend_residual_case_requires_auth(
    unauthenticated_client: AsyncClient,
):
    """Unsuspend endpoint requires admin auth."""
    response = await unauthenticated_client.post(
        "/api/admin/residual-cases/some-fp/unsuspend",
        params={"project_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code in (401, 403)


# ── GET /residual-cases (list endpoint for ops.py fingerprints) ──────────────


@pytest.mark.asyncio
async def test_list_residual_cases_default_returns_open_states(
    client: AsyncClient, db_session
):
    """Default response includes ACTIVE/REMEDIATING/SUSPENDED, excludes others."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for fp, status in [
        ("active-fp", ResidualCaseStatus.ACTIVE),
        ("remediating-fp", ResidualCaseStatus.REMEDIATING),
        ("suspended-fp", ResidualCaseStatus.SUSPENDED),
        ("ignored-fp", ResidualCaseStatus.IGNORED),
        ("resolved-fp", ResidualCaseStatus.RESOLVED),
    ]:
        db_session.add(
            ResidualRemediationCase(
                project_id=project.id,
                fingerprint=fp,
                status=status,
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/residual-cases")
    assert response.status_code == 200
    data = response.json()
    fps = {c["fingerprint"] for c in data["cases"]}
    assert fps == {"active-fp", "remediating-fp", "suspended-fp"}
    assert sorted(data["status_filter"]) == ["ACTIVE", "REMEDIATING", "SUSPENDED"]


@pytest.mark.asyncio
async def test_list_residual_cases_explicit_status_filter(
    client: AsyncClient, db_session
):
    """Passing ?status=IGNORED returns only IGNORED cases."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="ignored-only",
            status=ResidualCaseStatus.IGNORED,
        )
    )
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="active-other",
            status=ResidualCaseStatus.ACTIVE,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/residual-cases", params={"status": "IGNORED"}
    )
    assert response.status_code == 200
    data = response.json()
    fps = {c["fingerprint"] for c in data["cases"]}
    assert fps == {"ignored-only"}


@pytest.mark.asyncio
async def test_list_residual_cases_rejects_invalid_status(client: AsyncClient):
    """Unknown status values must 400 — typos shouldn't silently return everything."""
    response = await client.get(
        "/api/admin/residual-cases", params={"status": "BOGUS_STATUS"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_residual_cases_orders_by_last_seen_desc(
    client: AsyncClient, db_session
):
    """Most recently-seen cases should surface first so the operator sees pressure."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from datetime import datetime, timedelta

    now = datetime.utcnow()
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="older",
            status=ResidualCaseStatus.ACTIVE,
            last_seen_at=now - timedelta(hours=12),
        )
    )
    db_session.add(
        ResidualRemediationCase(
            project_id=project.id,
            fingerprint="newer",
            status=ResidualCaseStatus.ACTIVE,
            last_seen_at=now - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/residual-cases")
    assert response.status_code == 200
    fps = [c["fingerprint"] for c in response.json()["cases"]]
    assert fps[0] == "newer"
    assert fps[1] == "older"


@pytest.mark.asyncio
async def test_list_residual_cases_requires_auth(unauthenticated_client: AsyncClient):
    """List endpoint requires admin auth."""
    response = await unauthenticated_client.get("/api/admin/residual-cases")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# ResidualRemediationCase lifecycle status enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_no_case(
    client: AsyncClient, db_session
):
    """Cluster with no matching case row has no case_status field."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            # Use structural error shape per fc071105's tightened regex
            # patterns (`^\s*(error|fail(ed)?)\s*:`); plain prose like
            # "syntax error at token..." no longer matches.
            latest_status_message="SyntaxError: unexpected end of file",
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert "case_status" not in top[0]


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_branches(
    client: AsyncClient, db_session
):
    """Each lifecycle status is surfaced in the corresponding cluster entry."""
    from app.anomaly_detection import _error_fingerprint

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One failed task per status variant so each gets its own fingerprint cluster.
    # Messages must match the structural error patterns added in fc071105
    # (`^\s*(error|fail(ed)?)\s*:` or exception-class line-start) — bare prose
    # like "test failure: active case assertion failed" no longer qualifies.
    messages = {
        ResidualCaseStatus.ACTIVE: "AssertionError: active case 1 == 2 failed",
        ResidualCaseStatus.REMEDIATING: "ImportError: remediating module not found",
        ResidualCaseStatus.SUSPENDED: "TypeError: suspended case got int, expected str",
        ResidualCaseStatus.MERGED: "ValueError: merged case got unexpected None",
        ResidualCaseStatus.FAILED: "KeyError: 'failed_case_lookup_key'",
        ResidualCaseStatus.IGNORED: "AttributeError: ignored case 'foo' missing 'bar'",
    }
    fps = {status: _error_fingerprint(msg) for status, msg in messages.items()}

    for status, msg in messages.items():
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                latest_status_message=msg,
            )
        )
    await db_session.commit()

    # Insert one case row per status
    for status, fp in fps.items():
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=status,
            last_spawned_pr_number=42
            if status == ResidualCaseStatus.REMEDIATING
            else None,
            consecutive_failure_count=3
            if status == ResidualCaseStatus.SUSPENDED
            else 0,
        )
        db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}&top_n=10"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]

    by_fp = {r["fingerprint"]: r for r in top}

    for status, fp in fps.items():
        assert fp in by_fp, f"fingerprint for {status} not in response"
        entry = by_fp[fp]
        assert entry["case_status"] == status, f"{status}: case_status mismatch"

    # REMEDIATING entry carries PR number
    rem_fp = fps[ResidualCaseStatus.REMEDIATING]
    assert by_fp[rem_fp]["last_spawned_pr_number"] == 42

    # SUSPENDED entry carries consecutive_failure_count
    sus_fp = fps[ResidualCaseStatus.SUSPENDED]
    assert by_fp[sus_fp]["consecutive_failure_count"] == 3

    # Entry with no case row should have no case_status — tested in separate test above
    # MERGED/FAILED/IGNORED entries should not carry PR or failure-count noise
    for st in (
        ResidualCaseStatus.MERGED,
        ResidualCaseStatus.FAILED,
        ResidualCaseStatus.IGNORED,
    ):
        assert by_fp[fps[st]].get("last_spawned_pr_number") is None


@pytest.mark.asyncio
async def test_residual_fingerprints_case_status_project_scoped(
    client: AsyncClient, db_session
):
    """Case rows from a different project do not pollute the response."""
    from app.anomaly_detection import _error_fingerprint

    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    msg = "TypeError: cross-project test got int, expected str"
    fp = _error_fingerprint(msg)

    db_session.add(
        make_task(
            project_id=project_a.id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
        )
    )
    # Case row belongs to project_b — should NOT be returned for project_a query
    case = ResidualRemediationCase(
        project_id=project_b.id,
        fingerprint=fp,
        status=ResidualCaseStatus.SUSPENDED,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project_a.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert "case_status" not in top[0]


@pytest.mark.asyncio
async def test_residual_fingerprints_includes_audit_deferral_fields(
    client: AsyncClient, db_session
):
    """audit_defer_count and audit_deferred_system_id are included when a case exists."""
    from app.anomaly_detection import _error_fingerprint

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    msg = "RuntimeError: nil pointer dereference in module core"
    fp = _error_fingerprint(msg)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
        )
    )
    case = ResidualRemediationCase(
        fingerprint=fp,
        project_id=project.id,
        status=ResidualCaseStatus.ACTIVE,
        audit_defer_count=3,
        audit_deferred_system_id="sys-payments",
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/residual-fingerprints?project_id={project.id}"
    )
    assert response.status_code == 200
    top = response.json()["top_residuals"]
    assert len(top) == 1
    assert top[0]["audit_defer_count"] == 3
    assert top[0]["audit_deferred_system_id"] == "sys-payments"


# ---------------------------------------------------------------------------
# cost_per_merge_exceeds_threshold / median_cost_per_merged_pr tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_cpm_threshold_flags_expensive_direction(
    client: AsyncClient, db_session
):
    """Directions whose cost_per_merged_pr exceeds median×3 get cost_per_merge_exceeds_threshold=True."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    now = _utcnow()
    recent_ts = (now - timedelta(hours=1)).isoformat()

    # Three directions: cheap (1h / 3 merges), medium (2h / 2 merges), pricey (8h / 1 merge).
    # CPMs: ~0.0223, 0.067, 0.536.  Median = 0.067.  Threshold = 0.067 * 3 = 0.201.
    # Only pricey exceeds the threshold.
    project = make_project(
        directions=[
            {
                "id": "dir-cheap",
                "text": "Cheap direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 3}],
            },
            {
                "id": "dir-med",
                "text": "Medium direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 2}],
            },
            {
                "id": "dir-pricey",
                "text": "Pricey direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id, hours in [("dir-cheap", 1), ("dir-med", 2), ("dir-pricey", 8)]:
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"task-{dir_id}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
                vm_provisioned_at=now - timedelta(hours=hours + 1),
                vm_stopped_at=now - timedelta(hours=1),
                vm_spot=False,
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert set(by_id) == {"dir-cheap", "dir-med", "dir-pricey"}

    assert by_id["dir-cheap"]["cost_per_merge_exceeds_threshold"] is False
    assert by_id["dir-med"]["cost_per_merge_exceeds_threshold"] is False
    assert by_id["dir-pricey"]["cost_per_merge_exceeds_threshold"] is True

    median = data["median_cost_per_merged_pr"]
    assert median is not None
    assert median > 0
    # median CPM ≈ dir-med's CPM (middle value when sorted)
    expected_med_cost = round(ON_DEMAND_RATE * 2 * 3600, 4)
    expected_med_cpm = round(expected_med_cost / 2, 4)
    assert abs(median - expected_med_cpm) < 0.001


@pytest.mark.asyncio
async def test_digest_cpm_threshold_false_when_no_merges(
    client: AsyncClient, db_session
):
    """When no directions have merged PRs, median is None and threshold flag is False for all."""
    now = _utcnow()
    recent_ts = (now - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "No merges direction",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 0}],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="task-1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["median_cost_per_merged_pr"] is None
    dp = data["direction_progress"][0]
    assert dp["cost_per_merge_exceeds_threshold"] is False


@pytest.mark.asyncio
async def test_digest_direction_progress_cpm_threshold(client: AsyncClient, db_session):
    """median_cost_per_merged_pr is calculated correctly and directions exceeding 3x median are flagged."""
    from app.routers.admin_analytics import SPOT_RATE

    dir_configs = [
        ("dir-a", 10.0),
        ("dir-b", 20.0),
        ("dir-c", 100.0),
    ]

    recent_ts = (_utcnow() - timedelta(minutes=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": did,
                "text": did,
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            }
            for did, cost in dir_configs
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did, cost in dir_configs:
        # t_cost = SPOT_RATE * duration_sec => duration_sec = t_cost / SPOT_RATE
        duration_sec = cost / SPOT_RATE
        now = _utcnow()
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_spot=True,
            vm_provisioned_at=now - timedelta(seconds=duration_sec),
            vm_started_at=now - timedelta(seconds=duration_sec),
            vm_stopped_at=now,
            context={
                "direction_id": did,
                "pr_merged": True,
            },
            payloads=[{"type": "research"}],
        )
        db_session.add(t)

    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["median_cost_per_merged_pr"] == 20.0

    dp_by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert dp_by_id["dir-a"]["cost_per_merged_pr"] == 10.0
    assert dp_by_id["dir-a"]["cost_per_merge_exceeds_threshold"] is False

    assert dp_by_id["dir-b"]["cost_per_merged_pr"] == 20.0
    assert dp_by_id["dir-b"]["cost_per_merge_exceeds_threshold"] is False

    assert dp_by_id["dir-c"]["cost_per_merged_pr"] == 100.0
    assert dp_by_id["dir-c"]["cost_per_merge_exceeds_threshold"] is True


# ---------------------------------------------------------------------------
# /api/admin/drift-snapshot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_snapshot_missing_project_returns_empty(client: AsyncClient):
    """Unknown project_id → empty snapshot with correct shape, not a 4xx."""
    import uuid as _uuid

    project_id = _uuid.uuid4()
    response = await client.get(f"/api/admin/drift-snapshot?project_id={project_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 0
    assert data["flagged_directions"] == []
    assert data["total_directions"] == 0
    assert data["any_system_flagged"] is False
    assert "generated_at" in data
    assert data["window_days"] == 7


@pytest.mark.asyncio
async def test_drift_snapshot_no_flags_when_healthy(client: AsyncClient, db_session):
    """Project with directions but no drift activity → zero flagged directions."""
    project = make_project(
        directions=[
            {
                "id": "d-healthy",
                "text": "Keep CI green",
                "status": "active",
                "priority": "high",
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 0
    assert data["flagged_directions"] == []
    assert data["total_directions"] == 1


@pytest.mark.asyncio
async def test_drift_snapshot_over_claim_direction_flagged(
    client: AsyncClient, db_session
):
    """Direction with many claims but zero merges appears in flagged_directions."""
    from app.models import Task, TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-over-claim"
    project = make_project(
        directions=[
            {
                "id": dir_id,
                "text": "Reduce flakiness",
                "status": "active",
                "priority": "high",
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add enough COMPLETED tasks with plan_output to trip over_claim threshold (>=3 claims, <40% consistency)
    for _ in range(4):
        task = Task(
            project_id=project.id,
            title="Claim task",
            description="",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            payloads=[{"type": "create", "current_direction_id": dir_id}],
            context={"direction_id": dir_id},
            plan_output={"summary": "claimed a change"},
            vm_instance_id=f"vm-{_now().timestamp()}",
            created_at=_now() - timedelta(hours=2),
        )
        db_session.add(task)

    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 1
    assert len(data["flagged_directions"]) == 1
    flagged = data["flagged_directions"][0]
    assert flagged["direction_id"] == dir_id
    assert flagged["direction_text"] == "Reduce flakiness"
    assert flagged["over_claim_flag"] is True
    assert flagged["claimed_changes_count"] == 4
    assert flagged["actually_merged_prs"] == 0
    assert "claim_shipped_consistency" in flagged
    assert "window_days" in data
    assert "generated_at" in data
    # drift_history field present; empty when no stored snapshots exist yet
    assert "drift_history" in flagged
    assert isinstance(flagged["drift_history"], list)


@pytest.mark.asyncio
async def test_drift_snapshot_history_series_returned(client: AsyncClient, db_session):
    """Flagged direction includes history sparkline from stored DirectionDriftSnapshot rows."""
    from app.drift_detection import DirectionDriftSnapshot
    from app.models import TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-trend"
    project = make_project(
        directions=[
            {"id": dir_id, "text": "Trend test", "status": "active", "priority": "high"}
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 3 historical drift snapshots (oldest → newest)
    base_time = _now() - timedelta(hours=6)
    for i in range(3):
        snap = DirectionDriftSnapshot(
            project_id=project.id,
            direction_id=dir_id,
            snapshot_at=base_time + timedelta(hours=i),
            claimed_changes_count=4,
            actually_merged_prs=0,
            claim_shipped_consistency=0.0,
            over_claim_flag=True,
            stalled_despite_churn_flag=False,
        )
        db_session.add(snap)

    # Add enough tasks to trigger the over_claim_flag in the live compute
    now = _now()
    for _ in range(4):
        task = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"direction_id": dir_id, "shipped": True},
            created_at=now - timedelta(hours=1),
        )
        db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["flagged_count"] == 1
    flagged = data["flagged_directions"][0]
    assert flagged["direction_id"] == dir_id
    history = flagged["drift_history"]
    assert isinstance(history, list)
    assert len(history) == 3
    # history is oldest-first (chronological order for sparkline)
    for entry in history:
        assert "snapshot_at" in entry
        assert "claim_shipped_consistency" in entry
        assert "over_claim_flag" in entry
        assert "stalled_despite_churn_flag" in entry
    # chronological order: each snapshot_at >= previous
    times = [entry["snapshot_at"] for entry in history]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_drift_snapshot_response_shape_pinned(client: AsyncClient, db_session):
    """Pin the exact key set on flagged_directions[i] and the per-direction history
    entries so a future rename of either side fails CI immediately.

    The asserted key set MUST mirror the frontend TypeScript interfaces
    DriftDirection and DriftHistoryPoint in
    admin-ui/src/components/DriftSignalsCard.tsx. If you change either side,
    update both together — silent drift between server and client is the bug
    class this test exists to catch.
    """
    from app.drift_detection import DirectionDriftSnapshot
    from app.models import TaskStatus
    from app.time_utils import _utcnow as _now
    from datetime import timedelta

    dir_id = "d-shape"
    project = make_project(
        directions=[
            {"id": dir_id, "text": "Shape pin", "status": "active", "priority": "high"}
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base_time = _now() - timedelta(hours=3)
    db_session.add(
        DirectionDriftSnapshot(
            project_id=project.id,
            direction_id=dir_id,
            snapshot_at=base_time,
            claimed_changes_count=4,
            actually_merged_prs=0,
            claim_shipped_consistency=0.0,
            over_claim_flag=True,
            stalled_despite_churn_flag=False,
        )
    )
    now = _now()
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"direction_id": dir_id, "shipped": True},
                created_at=now - timedelta(hours=1),
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/drift-snapshot?project_id={project.id}")
    assert response.status_code == 200
    flagged = response.json()["flagged_directions"][0]

    # Mirrors DriftDirection (extends DriftSignal) in DriftSignalsCard.tsx.
    assert set(flagged.keys()) == {
        "direction_id",
        "direction_text",
        "over_claim_flag",
        "stalled_despite_churn_flag",
        "claimed_changes_count",
        "actually_merged_prs",
        "claim_shipped_consistency",
        "drift_history",
    }
    assert flagged["drift_history"], (
        "expected at least one history entry from seeded snapshot"
    )
    # Mirrors DriftHistoryPoint in DriftSignalsCard.tsx.
    assert set(flagged["drift_history"][0].keys()) == {
        "snapshot_at",
        "claim_shipped_consistency",
        "over_claim_flag",
        "stalled_despite_churn_flag",
    }


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_aggregation(client: AsyncClient, db_session):
    """cost_per_merge_by_type groups cost and merge counts by task type."""
    from app.routers.admin_analytics import SPOT_RATE

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # create task: 10s, pr_merged=True
    t1 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=10),
        vm_started_at=now - timedelta(seconds=10),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    # fix task: 20s, pr_merged=True
    t2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=20),
        vm_started_at=now - timedelta(seconds=20),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "fix"}],
    )
    # research task: 5s, no merge
    t3 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=5),
        vm_started_at=now - timedelta(seconds=5),
        vm_stopped_at=now,
        context={},
        payloads=[{"type": "research"}],
    )
    for t in (t1, t2, t3):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    cpm_by_type = data["cost_per_merge_by_type"]
    assert "create" in cpm_by_type
    assert "fix" in cpm_by_type
    assert "research" in cpm_by_type

    assert cpm_by_type["create"]["merged_count"] == 1
    assert (
        abs(cpm_by_type["create"]["total_cost_usd"] - round(SPOT_RATE * 10, 4)) < 1e-6
    )
    assert cpm_by_type["create"]["cost_per_merge"] is not None

    assert cpm_by_type["fix"]["merged_count"] == 1
    assert abs(cpm_by_type["fix"]["total_cost_usd"] - round(SPOT_RATE * 20, 4)) < 1e-6
    assert cpm_by_type["fix"]["cost_per_merge"] is not None

    assert cpm_by_type["research"]["merged_count"] == 0
    assert cpm_by_type["research"]["cost_per_merge"] is None


@pytest.mark.asyncio
async def test_digest_residual_remediation_funnel(client: AsyncClient, db_session):
    """Residual remediation funnel sourced from ResidualRemediationCase rows."""
    from app.models import ResidualRemediationCase, ResidualCaseStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    in_window = now - timedelta(hours=6)
    out_of_window = now - timedelta(hours=30)

    def make_case(status, spawned_at):
        import uuid as _uuid

        return ResidualRemediationCase(
            project_id=project.id,
            fingerprint=str(_uuid.uuid4()),
            status=status,
            last_spawned_at=spawned_at,
        )

    # In-window cases: 1 merged, 1 failed, 1 suspended (escalated), 1 remediating
    db_session.add(make_case(ResidualCaseStatus.MERGED, in_window))
    db_session.add(make_case(ResidualCaseStatus.FAILED, in_window))
    db_session.add(make_case(ResidualCaseStatus.SUSPENDED, in_window))
    db_session.add(make_case(ResidualCaseStatus.REMEDIATING, in_window))
    # Out-of-window case: excluded from spawned counts; REMEDIATING counts in live total
    db_session.add(make_case(ResidualCaseStatus.ACTIVE, out_of_window))
    db_session.add(make_case(ResidualCaseStatus.REMEDIATING, out_of_window))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    rr = response.json()["remediation_funnel"]["residual_remediation_funnel"]

    assert rr["spawned"] == 4  # 4 cases with last_spawned_at in 24h window
    assert rr["merged"] == 1
    assert rr["failed"] == 2  # FAILED + SUSPENDED
    assert (
        rr["remediating"] == 2
    )  # live count: both REMEDIATING cases regardless of window
    groups = rr["open_cases_by_error_class"]
    assert groups and groups[0]["error_class"] == "CODE_ERROR"
    assert groups[0]["count"] == 4
    assert len(groups[0]["cases"]) == 4


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_no_vm_tasks(client: AsyncClient, db_session):
    """When no tasks have VM billing data, cost_per_merge_by_type is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["cost_per_merge_by_type"] == {}
    assert data["median_cost_per_merge_by_type"] is None


@pytest.mark.asyncio
async def test_cost_per_merge_by_type_threshold(client: AsyncClient, db_session):
    """exceeds_threshold is True when a type's CPM exceeds median * 3.0.

    Median requires >=3 types to produce a stable low reference point so the
    expensive outlier can exceed 3× median.  With create=10s, fix=20s, debug=1000s
    the median is 20 and 3× threshold is 60; debug (1000s) clearly exceeds it.
    """

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # create: 10s per merge (cheap)
    t_create = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=10),
        vm_started_at=now - timedelta(seconds=10),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
    )
    # fix: 20s per merge (medium)
    t_fix = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=20),
        vm_started_at=now - timedelta(seconds=20),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "fix"}],
    )
    # debug: 1000s per merge (far above 3x median=20 → threshold=60)
    t_debug = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_spot=True,
        vm_provisioned_at=now - timedelta(seconds=1000),
        vm_started_at=now - timedelta(seconds=1000),
        vm_stopped_at=now,
        context={"pr_merged": True},
        payloads=[{"type": "debug"}],
    )
    for t in (t_create, t_fix, t_debug):
        db_session.add(t)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    cpm_by_type = data["cost_per_merge_by_type"]
    median_cpm = data["median_cost_per_merge_by_type"]
    assert median_cpm is not None

    assert cpm_by_type["create"]["cost_per_merge"] is not None
    assert cpm_by_type["fix"]["cost_per_merge"] is not None
    assert cpm_by_type["debug"]["cost_per_merge"] is not None

    # debug CPM (1000s) >> 3 × median (20s); create and fix are below threshold
    assert cpm_by_type["debug"]["exceeds_threshold"] is True
    assert cpm_by_type["create"]["exceeds_threshold"] is False
    assert cpm_by_type["fix"]["exceeds_threshold"] is False


# ---------------------------------------------------------------------------
# prediction_calibration fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_calibration_fields_present_when_project_selected(
    client: AsyncClient, db_session
):
    """Digest includes prediction_calibration_* fields when project_id is provided.

    With no StrategicCycle rows the factor defaults to 1.0 and sample_count to 0.
    """
    project = make_project(directions=[])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "prediction_calibration_factor" in data
    assert "prediction_calibration_note" in data
    assert "prediction_calibration_sample_count" in data
    assert data["prediction_calibration_factor"] == 1.0
    assert data["prediction_calibration_sample_count"] == 0
    assert "well-calibrated" in (data["prediction_calibration_note"] or "")


@pytest.mark.asyncio
async def test_digest_calibration_fields_null_without_project(client: AsyncClient):
    """Digest omits calibration values when no project_id is provided."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["prediction_calibration_factor"] is None
    assert data["prediction_calibration_note"] is None
    assert data["prediction_calibration_sample_count"] is None


# ---------------------------------------------------------------------------
# Schema contract / ordering tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_top_level_schema_keys(client: AsyncClient):
    """All dashboard-facing top-level keys are present; tasks_truncated is a boolean."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    required = {
        "window_hours",
        "tasks_truncated",
        "completed_count",
        "failed_count",
        "pending_approval_count",
        "research_cycles",
        "prs_opened",
        "active_count",
        "running_count",
        "queued_count",
        "active_tasks",
        "needs_attention",
        "recent_failures",
        "recent_completions",
        "success_rate",
        "trend",
        "narrative_summary",
        "total_estimated_cost_usd",
        "migration_health",
        "failure_groups",
        "recurring_error_clusters",
        "recurring_patterns",
        "direction_progress",
        "retry_waste_by_direction",
        "provision_waste_cost_usd",
        "provision_waste_by_direction",
        "cost_by_failure_class",
        "analysis_overflow_summary",
        "total_merge_retries_saved",
        "merge_early_exit_count",
        "prediction_calibration_factor",
        "prediction_calibration_note",
        "prediction_calibration_sample_count",
    }
    missing = required - data.keys()
    assert not missing, f"Missing dashboard keys: {missing}"
    assert isinstance(data["tasks_truncated"], bool)


@pytest.mark.asyncio
async def test_digest_needs_attention_approval_before_failed(
    client: AsyncClient, db_session
):
    """approval_required items precede failed items in needs_attention."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Failed Task",
            status=TaskStatus.FAILED,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="Approval Task",
            status=TaskStatus.IN_PROGRESS,
            requires_approval=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    items = response.json()["needs_attention"]
    assert len(items) == 2
    assert items[0]["reason"] == "approval_required"
    assert items[1]["reason"] == "failed"


@pytest.mark.asyncio
async def test_digest_direction_progress_operator_action_count(
    client: AsyncClient, db_session
):
    """operator_action_count is summed from research_history entries within the window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction A",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "autonomous_prs_merged": 1,
                        "assisted_prs_merged": 1,
                        "operator_action_count": 3,
                    },
                    {"timestamp": old_ts, "prs_merged": 5, "operator_action_count": 10},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = data["direction_progress"][0]
    assert dp["operator_action_count"] == 3  # only recent entry
    assert dp["autonomous_prs_merged"] == 1
    assert dp["assisted_prs_merged"] == 1


@pytest.mark.asyncio
async def test_digest_pipeline_autonomy_totals(client: AsyncClient, db_session):
    """pipeline_autonomous_prs_merged, pipeline_assisted_prs_merged, pipeline_operator_action_count
    are sums across active directions in the window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-a",
                "text": "Direction A",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "autonomous_prs_merged": 2,
                        "assisted_prs_merged": 0,
                        "operator_action_count": 1,
                    },
                ],
            },
            {
                "id": "dir-b",
                "text": "Direction B",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 1,
                        "autonomous_prs_merged": 0,
                        "assisted_prs_merged": 1,
                        "operator_action_count": 2,
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ["dir-a", "dir-b"]:
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["pipeline_autonomous_prs_merged"] == 2
    assert data["pipeline_assisted_prs_merged"] == 1
    assert data["pipeline_operator_action_count"] == 3


@pytest.mark.asyncio
async def test_digest_pipeline_autonomy_legacy_fallback(
    client: AsyncClient, db_session
):
    """Legacy entries lacking autonomous_prs_merged are counted as autonomous (fallback)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-legacy",
                "text": "Legacy direction",
                "status": "active",
                "research_history": [
                    # No autonomous_prs_merged key — legacy pre-attribution entry
                    {"timestamp": recent_ts, "prs_merged": 4},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Legacy task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-legacy"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = data["direction_progress"][0]
    # Legacy fallback: autonomous = prs_merged, assisted = 0
    assert dp["autonomous_prs_merged"] == 4
    assert dp["assisted_prs_merged"] == 0
    assert dp["operator_action_count"] == 0
    assert data["pipeline_autonomous_prs_merged"] == 4
    assert data["pipeline_assisted_prs_merged"] == 0
    assert data["pipeline_operator_action_count"] == 0


@pytest.mark.asyncio
async def test_digest_active_tasks_ordered_oldest_first(
    client: AsyncClient, db_session
):
    """active_tasks are ordered oldest started_at first."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    older = make_task(
        project_id=project.id,
        title="Older Task",
        status=TaskStatus.IN_PROGRESS,
        started_at=_utcnow() - timedelta(hours=3),
    )
    newer = make_task(
        project_id=project.id,
        title="Newer Task",
        status=TaskStatus.IN_PROGRESS,
        started_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    active = response.json()["active_tasks"]
    ids = [t["task_id"] for t in active]
    assert ids.index(str(older.id)) < ids.index(str(newer.id))


@pytest.mark.asyncio
async def test_digest_direction_progress_legacy_autonomous_sum(
    client: AsyncClient, db_session
):
    """Multiple legacy research_history entries (prs_merged only, no autonomous_prs_merged)
    within the window: autonomous_prs_merged equals the sum of all prs_merged values."""
    recent_ts1 = (_utcnow() - timedelta(hours=2)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-legacy-sum",
                "text": "Legacy sum direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts1, "prs_merged": 3},
                    {"timestamp": recent_ts2, "prs_merged": 5},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-legacy-sum"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    # Both entries lack autonomous_prs_merged → fallback sums prs_merged values (3 + 5 = 8)
    assert dp["autonomous_prs_merged"] == 8
    assert dp["assisted_prs_merged"] == 0
    assert dp["prs_merged"] == 8


@pytest.mark.asyncio
async def test_digest_direction_progress_empty_history_sane_defaults(
    client: AsyncClient, db_session
):
    """Direction with a task created in the window but empty research_history appears in
    direction_progress with autonomous_prs_merged=0 and cost_per_merged_pr=None."""
    project = make_project(
        directions=[
            {
                "id": "dir-empty",
                "text": "Empty history direction",
                "status": "active",
                "research_history": [],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Create task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-empty"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp_list = response.json()["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["autonomous_prs_merged"] == 0
    assert dp["cost_per_merged_pr"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_zero_autonomous_with_operator_actions(
    client: AsyncClient, db_session
):
    """When autonomous_prs_merged=0 but operator_action_count>0, cost_per_merged_pr is None
    (not zero, not infinity — division is guarded on autonomous_prs_merged)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-op",
                "text": "Operator-assisted direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 0,
                        "autonomous_prs_merged": 0,
                        "assisted_prs_merged": 2,
                        "operator_action_count": 3,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-op"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["autonomous_prs_merged"] == 0
    assert dp["operator_action_count"] == 3
    assert dp["cost_per_merged_pr"] is None


# ── Timezone-aware research_history timestamps ───────────────────────────────


@pytest.mark.asyncio
async def test_digest_direction_progress_tz_aware_timestamp_counted(
    client: AsyncClient, db_session
):
    """research_history entries with tz-aware (UTC+Z) timestamps are counted, not dropped or raising TypeError."""
    project = make_project(
        directions=[
            {
                "id": "dir-tz-aware",
                "text": "Direction with tz-aware timestamp",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (
                            datetime.now(timezone.utc) - timedelta(hours=5)
                        ).isoformat(),
                        "prs_merged": 3,
                        "tasks_created": 6,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tz-aware"},
        )
    )
    await db_session.commit()

    # Must not raise TypeError for tz-aware vs naive comparison
    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    # prs_merged/tasks_created should be counted from the tz-aware entry
    assert dp["prs_merged"] == 3
    assert dp["conversion_rate"] == 0.5  # 3 merged / 6 created


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/retirement-critiques
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retirement_critiques_returns_critiques(client: AsyncClient, db_session):
    """Critiques stored in StrategicCycle.cycle_metadata are returned, newest cycle first."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(
        id="cycle-audit-1",
        project_id=project.id,
        cycle_metadata={
            "retirement_critiques": [
                {
                    "direction_id": "dir-abc",
                    "what_it_addressed": "Reduce DB queries",
                    "pattern_that_produced_it": "Overly broad direction",
                    "lesson": "Scope directions tightly",
                },
                {
                    "direction_id": "dir-def",
                    "what_it_addressed": "Improve test coverage",
                    "pattern_that_produced_it": "Coverage gaps",
                    "lesson": "Target specific modules",
                },
            ]
        },
    )
    db_session.add(cycle)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    critiques = data["critiques"]
    assert len(critiques) == 2
    assert critiques[0]["direction_id"] == "dir-abc"
    assert critiques[0]["what_it_addressed"] == "Reduce DB queries"
    assert critiques[0]["lesson"] == "Scope directions tightly"
    assert critiques[0]["cycle_id"] == "cycle-audit-1"
    assert critiques[1]["direction_id"] == "dir-def"


@pytest.mark.asyncio
async def test_retirement_critiques_empty_when_no_cycles(
    client: AsyncClient, db_session
):
    """Project with no strategic cycles returns empty critiques list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["critiques"] == []


@pytest.mark.asyncio
async def test_retirement_critiques_skips_sentinel_entries(
    client: AsyncClient, db_session
):
    """Entries with no_eligible_retirements=True are excluded from results."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(
        id="cycle-audit-sentinel",
        project_id=project.id,
        cycle_metadata={
            "retirement_critiques": [
                {"no_eligible_retirements": True},
                {
                    "direction_id": "dir-real",
                    "what_it_addressed": "Real work",
                    "pattern_that_produced_it": "Pattern",
                    "lesson": "Lesson",
                },
            ]
        },
    )
    db_session.add(cycle)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["critiques"][0]["direction_id"] == "dir-real"


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/research-actionability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_actionability_grade_distribution(
    client: AsyncClient, db_session
):
    """Grade distribution is computed from research task plan_output findings."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(id="cycle-action-1", project_id=project.id)
    db_session.add(cycle)

    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"_actionability_structural_downgrades": 2},
        plan_output=[
            {
                "title": "Finding A",
                "actionability_grade": "HIGH",
                "direction_id": "dir-h1",
            },
            {
                "title": "Finding B",
                "actionability_grade": "HIGH",
                "direction_id": "dir-h2",
            },
            {
                "title": "Finding C",
                "actionability_grade": "MEDIUM",
                "direction_id": "dir-m1",
            },
            {
                "title": "Finding D",
                "actionability_grade": "LOW",
                "direction_id": "dir-l1",
            },
        ],
    )
    db_session.add(research_task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 4
    assert data["grade_distribution"]["HIGH"] == 2
    assert data["grade_distribution"]["MEDIUM"] == 1
    assert data["grade_distribution"]["LOW"] == 1
    assert data["cycle_history"][0]["structural_downgrades"] == 2


@pytest.mark.asyncio
async def test_research_actionability_empty_project(client: AsyncClient, db_session):
    """Project with no research tasks returns zero counts."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 0
    assert data["grade_distribution"] == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


@pytest.mark.asyncio
async def test_research_actionability_excludes_non_research_plan_output(
    client: AsyncClient, db_session
):
    """plan_output on non-research task types must NOT leak into the grade distribution."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-action-typefilter", project_id=project.id))

    # Real research task — counted.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            plan_output=[
                {
                    "title": "Real finding",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-real",
                },
            ],
        )
    )
    # Non-research task that also wrote plan_output — must be excluded.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            plan_output=[
                {
                    "title": "Bogus finding",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-bogus",
                },
                {
                    "title": "Bogus 2",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-bogus2",
                },
            ],
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "planning"}],
            plan_output=[
                {
                    "title": "Planner output",
                    "actionability_grade": "LOW",
                    "direction_id": "dir-plan",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_findings"] == 1
    assert data["grade_distribution"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 0}


@pytest.mark.asyncio
async def test_research_actionability_research_task_does_not_self_attribute(
    client: AsyncClient, db_session
):
    """A research task whose context carries direction_id must not count as its own follow-on."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-action-self", project_id=project.id))

    # Research task with direction_id in context (canonical pattern from
    # research_completion.py:1721) and pr_merged accidentally truthy in
    # context. Without the self-exclusion, this would mark the direction as
    # both tasked and merged.
    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-self", "pr_merged": True},
        plan_output=[
            {
                "title": "Self-attribution probe",
                "actionability_grade": "HIGH",
                "direction_id": "dir-self",
            },
        ],
    )
    db_session.add(research_task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    # Distribution still records the finding...
    assert data["grade_distribution"]["HIGH"] == 1
    # ...but the research task itself must not register as the follow-on.
    assert data["grade_tasked"]["HIGH"] == 0
    assert data["grade_merged"]["HIGH"] == 0


@pytest.mark.asyncio
async def test_research_actionability_merge_requires_pr_merged_flag(
    client: AsyncClient, db_session
):
    """Merge attribution requires `context['pr_merged'] is True`, not just COMPLETED."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    research_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-action-merge",
            project_id=project.id,
            created_at=research_at - _td(hours=2),
        )
    )
    research_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        created_at=research_at,
        plan_output=[
            {
                "title": "Merged path",
                "actionability_grade": "HIGH",
                "direction_id": "dir-merged",
            },
            {
                "title": "Tasked-but-not-merged",
                "actionability_grade": "HIGH",
                "direction_id": "dir-tasked",
            },
            {
                "title": "Untouched",
                "actionability_grade": "MEDIUM",
                "direction_id": "dir-cold",
            },
        ],
    )
    db_session.add(research_task)

    # Follow-on for dir-merged: COMPLETED + pr_merged=True → merged.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=30),
            context={"direction_id": "dir-merged", "pr_merged": True},
        )
    )
    # Follow-on for dir-tasked: COMPLETED but NO pr_merged → tasked only.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=30),
            context={"direction_id": "dir-tasked"},
        )
    )
    # Follow-on for dir-merged created BEFORE the research task — must not count.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at - _td(hours=1),
            context={"direction_id": "dir-cold", "pr_merged": True},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_tasked"]["HIGH"] == 2  # dir-merged + dir-tasked
    assert data["grade_merged"]["HIGH"] == 1  # only dir-merged
    # dir-cold's pre-research follow-on must not register.
    assert data["grade_tasked"]["MEDIUM"] == 0
    assert data["grade_merged"]["MEDIUM"] == 0


@pytest.mark.asyncio
async def test_research_actionability_grade_merge_rate_and_calibration(
    client: AsyncClient, db_session
):
    """grade_merge_rate = merged/findings; calibration_score = HIGH_rate - LOW_rate."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    research_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-mr-1",
            project_id=project.id,
            created_at=research_at - _td(hours=1),
        )
    )
    # 3 HIGH findings (2 will merge, 1 just tasked) → HIGH rate = 2/3
    # 2 MEDIUM findings (1 will merge) → MEDIUM rate = 1/2
    # 2 LOW findings (0 will merge) → LOW rate = 0/2 = 0.0
    plan_output = [
        {"title": "H1", "actionability_grade": "HIGH", "direction_id": "dir-h1"},
        {"title": "H2", "actionability_grade": "HIGH", "direction_id": "dir-h2"},
        {"title": "H3", "actionability_grade": "HIGH", "direction_id": "dir-h3"},
        {"title": "M1", "actionability_grade": "MEDIUM", "direction_id": "dir-m1"},
        {"title": "M2", "actionability_grade": "MEDIUM", "direction_id": "dir-m2"},
        {"title": "L1", "actionability_grade": "LOW", "direction_id": "dir-l1"},
        {"title": "L2", "actionability_grade": "LOW", "direction_id": "dir-l2"},
    ]
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_at,
            plan_output=plan_output,
        )
    )
    # Merged follow-ons.
    for d in ("dir-h1", "dir-h2", "dir-m1"):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                created_at=research_at + _td(minutes=10),
                context={"direction_id": d, "pr_merged": True},
            )
        )
    # Tasked-only follow-on for HIGH.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_at + _td(minutes=10),
            context={"direction_id": "dir-h3"},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_merge_rate"]["HIGH"] == pytest.approx(2 / 3)
    assert data["grade_merge_rate"]["MEDIUM"] == pytest.approx(1 / 2)
    assert data["grade_merge_rate"]["LOW"] == 0.0
    # of-tasked: HIGH = 2 merged / 3 tasked, MEDIUM = 1/1, LOW = None (0 tasked)
    assert data["grade_merge_rate_of_tasked"]["HIGH"] == pytest.approx(2 / 3)
    assert data["grade_merge_rate_of_tasked"]["MEDIUM"] == pytest.approx(1.0)
    assert data["grade_merge_rate_of_tasked"]["LOW"] is None
    assert data["calibration_score"] == pytest.approx(2 / 3 - 0.0)


@pytest.mark.asyncio
async def test_research_actionability_calibration_null_when_no_findings(
    client: AsyncClient, db_session
):
    """calibration_score is null when HIGH or LOW has zero findings; merge_rate
    uses null (not 0.0) when grade_distribution is 0."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    db_session.add(StrategicCycle(id="cycle-mr-empty", project_id=project.id))
    # Only MEDIUM findings — both HIGH and LOW are absent.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            plan_output=[
                {
                    "title": "M1",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-m1",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["grade_merge_rate"]["HIGH"] is None
    assert data["grade_merge_rate"]["LOW"] is None
    assert data["grade_merge_rate"]["MEDIUM"] == 0.0  # 0 merged / 1 finding
    assert data["calibration_score"] is None


@pytest.mark.asyncio
async def test_research_actionability_cycle_history_chronological(
    client: AsyncClient, db_session
):
    """cycle_history has one entry per scanned cycle, oldest→newest, with per-cycle rates."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    now = _utcnow()
    cycle_old_at = now - _td(hours=10)
    cycle_new_at = now - _td(hours=4)
    db_session.add(
        StrategicCycle(
            id="cycle-old",
            project_id=project.id,
            created_at=cycle_old_at,
        )
    )
    db_session.add(
        StrategicCycle(
            id="cycle-new",
            project_id=project.id,
            created_at=cycle_new_at,
        )
    )

    # Research task in OLD cycle window: 1 HIGH (will merge).
    research_old_at = cycle_old_at + _td(minutes=30)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_old_at,
            plan_output=[
                {
                    "title": "Old HIGH",
                    "actionability_grade": "HIGH",
                    "direction_id": "dir-old-h",
                },
            ],
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=research_old_at + _td(minutes=5),
            context={"direction_id": "dir-old-h", "pr_merged": True},
        )
    )

    # Research task in NEW cycle window: 1 LOW (no follow-on).
    research_new_at = cycle_new_at + _td(minutes=30)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=research_new_at,
            plan_output=[
                {
                    "title": "New LOW",
                    "actionability_grade": "LOW",
                    "direction_id": "dir-new-l",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert "cycle_history" in data
    history = data["cycle_history"]
    assert len(history) == 2
    # Oldest → newest order.
    assert history[0]["cycle_id"] == "cycle-old"
    assert history[1]["cycle_id"] == "cycle-new"
    # Old bucket: 1 HIGH finding, merged. HIGH rate = 1.0, LOW absent → None.
    assert history[0]["total_findings"] == 1
    assert history[0]["grade_distribution"]["HIGH"] == 1
    assert history[0]["grade_merged"]["HIGH"] == 1
    assert history[0]["grade_merge_rate"]["HIGH"] == 1.0
    assert history[0]["grade_merge_rate"]["LOW"] is None
    assert history[0]["calibration_score"] is None  # LOW rate is None
    # New bucket: 1 LOW, not merged. LOW rate = 0.0, HIGH absent → None.
    assert history[1]["total_findings"] == 1
    assert history[1]["grade_distribution"]["LOW"] == 1
    assert history[1]["grade_merge_rate"]["LOW"] == 0.0
    assert history[1]["grade_merge_rate"]["HIGH"] is None
    assert history[1]["calibration_score"] is None


@pytest.mark.asyncio
async def test_research_actionability_cycle_history_null_rate_for_zero_findings(
    client: AsyncClient, db_session
):
    """A cycle with zero findings of a grade returns null (not 0.0) for that grade's merge_rate."""
    from app.models import StrategicCycle
    from datetime import timedelta as _td

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_membership(project.id))
    cyc_at = _utcnow() - _td(hours=2)
    db_session.add(
        StrategicCycle(
            id="cycle-only-medium",
            project_id=project.id,
            created_at=cyc_at,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            created_at=cyc_at + _td(minutes=10),
            plan_output=[
                {
                    "title": "M-only",
                    "actionability_grade": "MEDIUM",
                    "direction_id": "dir-m-only",
                },
            ],
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["cycle_history"]) == 1
    entry = data["cycle_history"][0]
    assert entry["grade_merge_rate"]["HIGH"] is None
    assert entry["grade_merge_rate"]["LOW"] is None
    assert entry["grade_merge_rate"]["MEDIUM"] == 0.0
    assert entry["calibration_score"] is None


# Self-review skip-rate metric
# ---------------------------------------------------------------------------


def _sr_skipped_envelope_unknown_verdict() -> dict:
    return {
        "verdict": "unknown",
        "fixes_applied": False,
        "check_results": {},
        "blind_spots": [],
    }


def _sr_skipped_envelope_blind_spot_marker(marker: str) -> dict:
    return {
        "verdict": "unknown",
        "fixes_applied": False,
        "check_results": {},
        "blind_spots": [marker],
    }


def _sr_valid_outcome() -> dict:
    return {
        "verdict": "pass",
        "fixes_applied": False,
        "check_results": {"correctness": {"passed": True}},
        "blind_spots": [],
    }


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_detects_skipped_envelope(
    client: AsyncClient, db_session
):
    """An unknown-verdict outcome with empty check_results counts as a skip."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 skipped (unknown verdict, empty check_results) + 2 valid → 5 code-change tasks
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["skipped_count"] == 3
    assert skip_rate["sample_size"] == 5
    assert skip_rate["overall_rate"] == 0.6


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_detects_blind_spot_marker(
    client: AsyncClient, db_session
):
    """blind_spots entries containing the fallback markers count as skips."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 parse-failure + 2 missing-file + 1 valid → 5 code-change tasks, 4 skipped
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "refactor"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                        "self-review JSON parsing failed"
                    )
                },
            )
        )
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "migrate"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                        "self-review skipped — investigate"
                    )
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_valid_outcome()},
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["skipped_count"] == 4
    assert skip_rate["sample_size"] == 5
    assert skip_rate["overall_rate"] == 0.8


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_excludes_non_code_change_types(
    client: AsyncClient,
    db_session,
):
    """Review/research tasks are excluded from the denominator."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 5 code-change tasks: 1 skipped, 4 valid
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    # Non-code-change tasks with skipped envelopes — must NOT affect the metric
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["sample_size"] == 5
    assert skip_rate["skipped_count"] == 1
    assert skip_rate["overall_rate"] == 0.2


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_returns_none_below_min_sample(
    client: AsyncClient,
    db_session,
):
    """Returns None when fewer than 5 code-change tasks are available."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 4 code-change tasks total → below threshold
    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["self_review_skip_rate"] is None


@pytest.mark.asyncio
async def test_digest_self_review_skip_rate_by_task_type_breakdown(
    client: AsyncClient,
    db_session,
):
    """by_task_type reports per-type rates only when that type has >=5 samples."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 5 create tasks: 2 skipped + 3 valid → eligible for by_task_type
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={"_self_review_outcome": _sr_valid_outcome()},
            )
        )
    # 2 fix tasks → below per-type threshold; included in overall but not in by_task_type
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    skip_rate = response.json()["self_review_skip_rate"]
    assert skip_rate is not None
    assert skip_rate["sample_size"] == 7
    assert skip_rate["skipped_count"] == 4
    assert "create" in skip_rate["by_task_type"]
    assert skip_rate["by_task_type"]["create"]["sample_size"] == 5
    assert skip_rate["by_task_type"]["create"]["skipped_count"] == 2
    assert skip_rate["by_task_type"]["create"]["rate"] == 0.4
    assert "fix" not in skip_rate["by_task_type"]


# ---------------------------------------------------------------------------
# SR disallowed blind-spot rate metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_sr_disallowed_blind_spot_rate_below_min_sample(
    client: AsyncClient, db_session
):
    """Fewer than 5 code-change tasks → sr_disallowed_blind_spot_rate is None."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(4):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 1,
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["sr_disallowed_blind_spot_rate"] is None


@pytest.mark.asyncio
async def test_digest_sr_disallowed_blind_spot_rate_basic(
    client: AsyncClient, db_session
):
    """Tasks with _filtered_blind_spots_count > 0 produce a correct violation rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 violators + 3 clean = 5 code-change tasks → rate = 0.4
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 2,
                    }
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        **_sr_valid_outcome(),
                        "_filtered_blind_spots_count": 0,
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    rate = response.json()["sr_disallowed_blind_spot_rate"]
    assert rate is not None
    assert rate["violation_count"] == 2
    assert rate["sample_size"] == 5
    assert rate["overall_rate"] == 0.4


# ---------------------------------------------------------------------------
# Fresh-reviewer effectiveness metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_empty(client: AsyncClient):
    """No tasks → fresh_reviewer_effectiveness present with zero sample_size and null rates."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre is not None
    assert fre["fresh_reviewer_sample_size"] == 0
    assert fre["fresh_reviewer_first_attempt_approval_rate"] is None
    assert fre["baseline_first_attempt_approval_rate"] is None
    assert fre["per_category_hit_rate"] == {}
    assert fre["reviewer_finding_added_value"] == {}
    assert fre["prediction_sample_size"] == 0


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_approval_rate_lift(
    client: AsyncClient, db_session
):
    """Tasks with reviewer_findings have a separate approval rate from those without."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 tasks with fresh-reviewer findings: both approved on first attempt
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "x",
                            }
                        ],
                    },
                },
            )
        )
    # 2 tasks without fresh-reviewer: both rejected on first attempt
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 2
    assert fre["fresh_reviewer_first_attempt_approval_rate"] == 1.0
    assert fre["baseline_first_attempt_approval_rate"] == 0.0


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_per_category_hit_rate(
    client: AsyncClient, db_session
):
    """per_category_hit_rate reports confirmed fraction; reviewer_finding_added_value
    counts categories fresh-reviewer caught that the agent missed and human confirmed."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks where fresh-reviewer flagged "correctness" and human confirmed it too.
    # Agent self-review passed correctness (so it counts as added value).
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "fail",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "CRITICAL",
                                "description": "y",
                            }
                        ],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 3
    assert fre["prediction_sample_size"] == 3
    # 3/3 correctness findings confirmed → hit rate 1.0
    assert fre["per_category_hit_rate"]["correctness"] == 1.0
    # agent passed correctness, reviewer flagged it, human confirmed → added value
    assert fre["reviewer_finding_added_value"].get("correctness", 0) == 3


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_effectiveness_excludes_reviewer_origin_catches(
    client: AsyncClient, db_session
):
    """Reviewer-stamped passed=False entries still count as reviewer added value."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "fail",
                        "check_results": {
                            "correctness": {
                                "passed": False,
                                "origin": "reviewer_disagreement",
                            }
                        },
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "CRITICAL",
                                "description": "caught by reviewer",
                            }
                        ],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    fre = response.json()["fresh_reviewer_effectiveness"]
    assert fre["fresh_reviewer_sample_size"] == 3
    assert fre["prediction_sample_size"] == 3
    assert fre["per_category_hit_rate"]["correctness"] == 1.0
    assert fre["reviewer_finding_added_value"].get("correctness", 0) == 3


# ---------------------------------------------------------------------------
# SR blind-spots aggregation endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_blind_spots_aggregates_themes(client: AsyncClient, db_session):
    """Top themes are counted and skipped/fallback envelopes are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks with the same first-6-token theme (should merge into count=3)
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [
                            f"did not verify file path number {i} thoroughly"
                        ],
                    }
                },
            )
        )
    # 1 task with a distinct blind-spot theme
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["async error propagation not checked"],
                }
            },
        )
    )
    # 1 skipped envelope — must NOT contribute to themes
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    themes_by_name = {t["theme"]: t for t in body["themes"]}
    # The 3 identical long strings normalise to the same 6-token prefix
    assert themes_by_name["did not verify file path number"]["count"] == 3
    assert themes_by_name["async error propagation not checked"]["count"] == 1
    # Skipped envelope contributes no entries
    assert body["total_distinct_themes"] == 2
    # Each theme entry must carry an examples list
    for entry in body["themes"]:
        assert "examples" in entry
        for ex in entry["examples"]:
            assert "text" in ex and "task_id" in ex and "task_type" in ex
    # The repeated-theme has examples capped and preserves verbatim text
    repeated = themes_by_name["did not verify file path number"]
    assert len(repeated["examples"]) == 3
    for ex in repeated["examples"]:
        assert ex["text"].startswith("did not verify file path number")
        assert ex["task_type"] == "create"


@pytest.mark.asyncio
async def test_sr_blind_spots_examples_capped_at_limit(client: AsyncClient, db_session):
    """Examples are capped at _BLIND_SPOT_EXAMPLES_PER_THEME even when count exceeds it."""
    from app.sr_blind_spots import _BLIND_SPOT_EXAMPLES_PER_THEME

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create more tasks than the example cap for a single theme. The blind-spot
    # string must NOT match DISALLOWED_BLIND_SPOT_PATTERNS — intended-skip
    # phrasings ("did not run the full ... suite") are filtered before themeing.
    for i in range(_BLIND_SPOT_EXAMPLES_PER_THEME + 2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [f"did not verify file path number {i}"],
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    theme = body["themes"][0]
    assert theme["count"] == _BLIND_SPOT_EXAMPLES_PER_THEME + 2
    assert len(theme["examples"]) == _BLIND_SPOT_EXAMPLES_PER_THEME


@pytest.mark.asyncio
async def test_sr_blind_spots_missing_task_type_produces_empty_string(
    client: AsyncClient, db_session
):
    """task_type is '' when first_payload has no 'type' key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{}],  # no 'type' key
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["async propagation not verified at all"],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"]
    ex = body["themes"][0]["examples"][0]
    assert ex["task_type"] == ""


@pytest.mark.asyncio
async def test_sr_blind_spots_skipped_envelopes_contribute_zero_examples(
    client: AsyncClient, db_session
):
    """Skipped/fallback SR envelopes produce no theme entries and no examples."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_skipped_envelope_unknown_verdict()},
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": _sr_skipped_envelope_blind_spot_marker(
                    "self-review skipped"
                )
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"] == []
    assert body["total_distinct_themes"] == 0


@pytest.mark.asyncio
async def test_sr_blind_spots_example_task_id_matches_source_task(
    client: AsyncClient, db_session
):
    """Each example carries the task_id of the task that emitted that blind_spot."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor"}],
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {},
                "blind_spots": ["did not check caller compatibility thoroughly here"],
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-blind-spots?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["themes"]
    ex = body["themes"][0]["examples"][0]
    assert ex["task_id"] == str(task.id)
    assert ex["task_type"] == "refactor"


@pytest.mark.asyncio
async def test_compute_sr_blind_spot_themes_stamps_design_doc(db_session):
    """compute_sr_blind_spot_themes stamps sr_blind_spot_signal_last_computed_at."""
    from app.sr_blind_spots import compute_sr_blind_spot_themes

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (project.design_doc or {}).get(
        "sr_blind_spot_signal_last_computed_at"
    ) is None

    fixed_now = _utcnow().replace(microsecond=0)
    await compute_sr_blind_spot_themes(db_session, project.id, now=fixed_now)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        project.design_doc["sr_blind_spot_signal_last_computed_at"]
        == fixed_now.isoformat()
    )


# ---------------------------------------------------------------------------
# aggregate_sr_regression_risk_test_signal (synchronous helper)
# ---------------------------------------------------------------------------


def _make_rr_task(project_id, *, sr_passed, test_attempts, task_type="create"):
    """Build a Task-like object with SR regression_risk + fix_loop_history.test."""
    check_results: dict = {}
    if sr_passed is not None:
        check_results["regression_risk"] = {"passed": sr_passed}
    history: dict = {}
    if test_attempts is not None:
        history["test"] = test_attempts
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context={"_self_review_outcome": {"check_results": check_results}},
        fix_loop_history=history,
    )


def test_aggregate_sr_regression_risk_test_signal_below_min_sample_returns_none():
    """Fewer than _SR_MIN_SAMPLE qualifying tasks → None."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(4)]
    assert aggregate_sr_regression_risk_test_signal(tasks) is None


def test_aggregate_sr_regression_risk_test_signal_passed_no_signal_is_not_a_miss():
    """SR passed + no test loop activity → true-pass, miss_by_test_signal_rate == 0."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(5)]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_passed_no_signal"] == 5
    assert result["sr_passed_with_signal"] == 0
    assert result["miss_by_test_signal_rate"] == 0.0
    assert result["numerator"] == 0
    assert result["denominator"] == 5


def test_aggregate_sr_regression_risk_test_signal_passed_with_testfix_counts_as_miss():
    """SR passed + testFix loop ran ≥1 non-trivial iteration → miss-by-test-signal."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    miss_attempts = [
        {"iteration": 1, "exit_reason": "in_progress"},
        {"iteration": 2, "exit_reason": "passed"},
    ]
    tasks = [
        _make_rr_task(pid, sr_passed=True, test_attempts=miss_attempts)
        for _ in range(3)
    ] + [_make_rr_task(pid, sr_passed=True, test_attempts=[]) for _ in range(2)]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_passed_with_signal"] == 3
    assert result["sr_passed_no_signal"] == 2
    assert result["miss_by_test_signal_rate"] == round(3 / 5, 3)


def test_aggregate_sr_regression_risk_test_signal_failed_with_signal_counts_as_hit():
    """SR failed regression_risk + testFix loop ran → hit-by-test-signal (not a miss)."""
    from app.sr_blind_spots import aggregate_sr_regression_risk_test_signal
    import uuid

    pid = uuid.uuid4()
    hit_attempts = [{"iteration": 1, "exit_reason": "divergence_detected"}]
    tasks = [
        _make_rr_task(pid, sr_passed=False, test_attempts=hit_attempts)
        for _ in range(5)
    ]
    result = aggregate_sr_regression_risk_test_signal(tasks)
    assert result is not None
    assert result["sr_failed_with_signal"] == 5
    assert result["sr_passed_with_signal"] == 0
    # No SR-passed tasks → denominator is 0
    assert result["denominator"] == 0
    assert result["miss_by_test_signal_rate"] is None


# ---------------------------------------------------------------------------
# aggregate_sr_scope_drift_vs_expansion (synchronous helper)
# ---------------------------------------------------------------------------


def _make_sd_task(project_id, *, sr_passed, expansion_ratio, task_type="create"):
    """Build a Task-like object with SR scope-drift check + expansion_ratio in context."""
    ctx = {
        "_self_review_outcome": {
            "check_results": {"scope-drift": {"passed": sr_passed}}
        },
        "expansion_ratio": expansion_ratio,
    }
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context=ctx,
    )


def test_aggregate_sr_scope_drift_vs_expansion_below_min_sample_returns_none():
    """Fewer than _SR_MIN_SAMPLE qualifying tasks → None."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.0) for _ in range(4)]
    assert aggregate_sr_scope_drift_vs_expansion(tasks) is None


def test_aggregate_sr_scope_drift_vs_expansion_consistent_bucket():
    """SR passed + ratio ≤1.5 → consistent, miss rate == 0."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.2) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["consistent"] == 5
    assert result["miss_by_expansion"] == 0
    assert result["miss_by_expansion_rate"] == 0.0
    assert result["numerator"] == 0
    assert result["denominator"] == 5


def test_aggregate_sr_scope_drift_vs_expansion_miss_bucket():
    """SR passed + ratio ≥2.0 → miss_by_expansion; rate = misses / (consistent+misses)."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [
        _make_sd_task(pid, sr_passed=True, expansion_ratio=2.5) for _ in range(3)
    ] + [_make_sd_task(pid, sr_passed=True, expansion_ratio=1.0) for _ in range(2)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["miss_by_expansion"] == 3
    assert result["consistent"] == 2
    assert result["miss_by_expansion_rate"] == round(3 / 5, 3)


def test_aggregate_sr_scope_drift_vs_expansion_hit_bucket():
    """SR failed scope-drift + ratio ≥1.5 → hit_by_expansion; denominator is 0."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=False, expansion_ratio=2.0) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["hit_by_expansion"] == 5
    assert result["miss_by_expansion"] == 0
    assert result["denominator"] == 0
    assert result["miss_by_expansion_rate"] is None


def test_aggregate_sr_scope_drift_vs_expansion_fp_candidate_bucket():
    """SR failed scope-drift + ratio ≤1.0 → fp_candidate."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    tasks = [_make_sd_task(pid, sr_passed=False, expansion_ratio=1.0) for _ in range(5)]
    result = aggregate_sr_scope_drift_vs_expansion(tasks)
    assert result is not None
    assert result["fp_candidate"] == 5
    assert result["hit_by_expansion"] == 0
    assert result["miss_by_expansion"] == 0
    assert result["miss_by_expansion_rate"] is None


def test_aggregate_sr_scope_drift_vs_expansion_skip_tasks_without_expansion_ratio():
    """Tasks without expansion_ratio in context are excluded from qualification."""
    from app.sr_blind_spots import aggregate_sr_scope_drift_vs_expansion
    import uuid

    pid = uuid.uuid4()
    ctx = {"_self_review_outcome": {"check_results": {"scope-drift": {"passed": True}}}}
    no_ratio_task = make_task(
        project_id=pid,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        context=ctx,
    )
    # 10 tasks but all lack expansion_ratio → fewer than min_sample qualify
    assert aggregate_sr_scope_drift_vs_expansion([no_ratio_task] * 10) is None


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/prompt-fingerprint-analytics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_groups_by_fingerprint(
    client: AsyncClient, db_session
):
    """Code-change tasks bucket by prompt_fingerprint with correct success/skip rates."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # Fingerprint A: 3 tasks, 2 first-attempt successes, 0 SR skips
    for succ in (True, True, False):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_first_attempt_succeeded": succ,
                    "_self_review_outcome": _sr_valid_outcome(),
                    **(
                        {}
                        if succ
                        else {"_first_attempt_rejection_categories": ["correctness"]}
                    ),
                },
            )
        )
    # Fingerprint B: 3 tasks, 0 first-attempt successes, 1 SR skip
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_first_attempt_succeeded": False,
                    "_first_attempt_rejection_categories": [
                        "correctness",
                        "performance",
                    ],
                    "_self_review_outcome": (
                        _sr_skipped_envelope_unknown_verdict()
                        if i == 0
                        else _sr_valid_outcome()
                    ),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    assert body["sample_size"] == 6

    by_fp = {fp["fingerprint"]: fp for fp in body["fingerprints"]}
    assert set(by_fp) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}

    a = by_fp["aaaaaaaaaaaa"]
    assert a["sample_size"] == 3
    assert a["sr_sample_size"] == 3
    assert a["first_attempt_measured"] == 3
    assert a["first_attempt_success_rate"] == round(2 / 3, 3)
    assert a["sr_skip_rate"] == 0.0
    assert a["sr_catch_rate"] == 0.0
    assert a["rejection_category_counts"] == {"correctness": 1}

    b = by_fp["bbbbbbbbbbbb"]
    assert b["sample_size"] == 3
    assert b["sr_sample_size"] == 2
    assert b["first_attempt_success_rate"] == 0.0
    assert b["sr_skip_rate"] == round(1 / 3, 3)
    assert b["sr_catch_rate"] is None
    assert b["rejection_category_counts"] == {"correctness": 3, "performance": 3}

    # Project mean = 2/6 successes = 0.333; A above, B below.
    assert body["project_first_attempt_success_rate"] == round(2 / 6, 3)
    assert a["delta_vs_project_mean"] is not None and a["delta_vs_project_mean"] > 0
    assert b["delta_vs_project_mean"] is not None and b["delta_vs_project_mean"] < 0


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_unknown_bucket_for_legacy_rows(
    client: AsyncClient, db_session
):
    """Tasks without a prompt_fingerprint fall into an `unknown` bucket."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint=None,
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert len(fps) == 1
    assert fps[0]["fingerprint"] == "unknown"
    assert fps[0]["sample_size"] == 3


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_falls_back_to_context_mirror(
    client: AsyncClient, db_session
):
    """Legacy rows use context.prompt_fingerprint when the column is null."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint=None,
                context={
                    "prompt_fingerprint": "legacyfp12345",
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert len(fps) == 1
    assert fps[0]["fingerprint"] == "legacyfp12345"
    assert fps[0]["sample_size"] == 3


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_excludes_non_code_change(
    client: AsyncClient, db_session
):
    """Research/plan task types are not counted toward fingerprint analytics."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # 3 research tasks (excluded) sharing a fingerprint
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                prompt_fingerprint="cccccccccccc",
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sample_size"] == 0
    assert body["fingerprints"] == []


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_filters_below_min_sample_size(
    client: AsyncClient, db_session
):
    """Fingerprint buckets with sample_size < min_sample_size are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    # Fingerprint A has 3 tasks; B has 1 task — only A should survive a min of 2.
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_valid_outcome(),
                },
            )
        )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            prompt_fingerprint="bbbbbbbbbbbb",
            context={
                "_first_attempt_succeeded": False,
                "_self_review_outcome": _sr_valid_outcome(),
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?min_sample_size=2"
    )
    assert response.status_code == 200
    fps = response.json()["fingerprints"]
    assert [f["fingerprint"] for f in fps] == ["aaaaaaaaaaaa"]
    # Project mean still includes the singleton — sample_size counts all 4.
    assert response.json()["sample_size"] == 4


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_sr_quality_fields_gate_on_sr_sample_size(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": False, "issue": "x"}
                        },
                        "blind_spots": [],
                    },
                    "_first_attempt_rejection_categories": ["correctness"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "security",
                                "severity": "HIGH",
                                "description": "found",
                            }
                        ],
                    },
                    "_first_attempt_rejection_categories": ["security"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                prompt_fingerprint="aaaaaaaaaaaa",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": False, "issue": "x"}
                        },
                        "blind_spots": [],
                    },
                    "_first_attempt_rejection_categories": ["correctness"],
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                prompt_fingerprint="bbbbbbbbbbbb",
                context={
                    "_self_review_outcome": _sr_skipped_envelope_unknown_verdict()
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics?min_sample_size=2"
    )
    assert response.status_code == 200
    by_fp = {fp["fingerprint"]: fp for fp in response.json()["fingerprints"]}

    a = by_fp["aaaaaaaaaaaa"]
    assert a["sample_size"] == 3
    assert a["sr_sample_size"] == 2
    assert a["sr_catch_sample_size"] == 2
    assert a["sr_catch_rate"] == 0.5
    assert a["reviewer_rescue_sample_size"] == 1
    assert a["reviewer_rescue_rate"] == 1.0
    assert a["sr_false_positive_rate"] == 0.0
    disagreements = {
        entry["category"]: (
            entry["reviewer_overrode_sr_count"],
            entry["sr_overrode_reviewer_count"],
        )
        for entry in a["sr_reviewer_disagreement_top_categories"]
    }
    assert disagreements == {"correctness": (0, 1), "security": (1, 0)}

    b = by_fp["bbbbbbbbbbbb"]
    assert b["sample_size"] == 3
    assert b["sr_sample_size"] == 1
    assert b["sr_catch_rate"] is None
    assert b["reviewer_rescue_rate"] is None
    assert b["sr_false_positive_rate"] is None
    assert b["sr_false_positive_rate_by_category"] == []


@pytest.mark.asyncio
async def test_prompt_fingerprint_analytics_requires_project_membership(
    client: AsyncClient, db_session
):
    """Non-member callers get 404 from _get_user_project_with_role."""
    project = make_project(user_id="some-other-owner")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Intentionally skip make_membership for the test user.

    response = await client.get(
        f"/api/admin/projects/{project.id}/prompt-fingerprint-analytics"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# SR reviewer-disagreement aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_basic(db_session):
    """Counts disagreements where reviewer flagged a category self-review passed."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 create tasks: reviewer flags "correctness", self-review passed it
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "missed edge",
                            }
                        ],
                    }
                },
            )
        )
    # 1 fix task: reviewer flags "security", self-review omitted the category
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "security",
                            "severity": "CRITICAL",
                            "description": "unvalidated input",
                        }
                    ],
                }
            },
        )
    )
    # 1 task where self-review already FAILED the category — should NOT count as disagreement
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "logic bug",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    by_key = {(r["category"], r["task_type"]): r for r in results}
    assert by_key[("correctness", "create")]["count"] == 3
    assert by_key[("security", "fix")]["count"] == 1
    # Already-failed category must not appear
    assert ("correctness", "fix") not in by_key
    # Examples must include task_id for frontend drill-through
    correctness_entry = by_key[("correctness", "create")]
    assert len(correctness_entry["examples"]) > 0
    example = correctness_entry["examples"][0]
    assert "task_id" in example
    assert example["task_id"]  # non-empty UUID string
    assert example["task_type"] == "create"


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_skipped_excluded(db_session):
    """Skipped/fallback SR envelopes are excluded from disagreement counts."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "unknown",
                    "check_results": {},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    assert results == []


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_no_reviewer_findings_excluded(db_session):
    """Tasks without reviewer_findings are not counted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                }
            },
        )
    )
    await db_session.commit()

    results = await aggregate_sr_reviewer_disagreements(
        db_session,
        project.id,
        days=30,
        limit=10,
    )
    assert results == []


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint(client: AsyncClient, db_session):
    """GET sr-reviewer-disagreements returns envelope with window_days and disagreements list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    # Legacy field preserved (= returned slice size).
    assert body["total_entries"] == 1
    # Additive true-total / returned-count fields.
    assert body["total_distinct_categories"] == 1
    assert body["returned_entries"] == 1
    assert body["disagreements"][0]["category"] == "correctness"
    assert body["disagreements"][0]["task_type"] == "create"
    assert body["disagreements"][0]["count"] == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint_not_truncated(
    client: AsyncClient, db_session
):
    """Single distinct category: true total equals returned-entry count (no truncation)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_distinct_categories"] == 1
    assert body["returned_entries"] == 1
    assert body["total_entries"] == 1
    assert len(body["disagreements"]) == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_endpoint_truncated(
    client: AsyncClient, db_session
):
    """With >=2 distinct categories and limit=1, true total exceeds returned slice."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two distinct (category, task_type) disagreements.
    for category in ("correctness", "security"):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {category: {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": category,
                                "severity": "HIGH",
                                "description": "x",
                            }
                        ],
                    }
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30&limit=1"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_distinct_categories"] == 2
    assert body["returned_entries"] == 1
    # Truncated list reflects returned_entries, not the true total.
    assert len(body["disagreements"]) == 1
    # Legacy field tracks the returned slice (back-compat), NOT the true total.
    assert body["total_entries"] == 1
    assert body["total_distinct_categories"] > body["returned_entries"]


@pytest.mark.asyncio
async def test_sr_reviewer_disagreements_fp_fields(client: AsyncClient, db_session):
    """sr-reviewer-disagreements returns false_positive_count, true_positive_count, false_positive_rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # miss: SR passed correctness, reviewer flagged it
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "x",
                        }
                    ],
                }
            },
        )
    )
    # fp: SR flagged correctness failed, reviewer didn't independently flag it
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                    "reviewer_findings": [],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    d = next(
        e
        for e in body["disagreements"]
        if e["category"] == "correctness" and e["task_type"] == "create"
    )
    assert d["count"] == 1
    assert d["false_positive_count"] == 1
    assert d["true_positive_count"] == 0
    assert d["false_positive_rate"] == 1.0


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_basic(client: AsyncClient, db_session):
    """GET sr-reviewer-disagreements/tasks returns task rows for disagreements."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "Logic flaw in edge case",
                        }
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["task_type"] == "create"
    assert item["reviewer_findings"][0]["category"] == "correctness"
    assert body["category_counts"]["correctness"] == 1


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_category_filter(
    client: AsyncClient, db_session
):
    """category filter limits results to tasks with that disagreement category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # task with correctness disagreement
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {"category": "correctness", "severity": "HIGH", "description": "x"}
                    ],
                }
            },
        )
    )
    # task with security disagreement only
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                    "reviewer_findings": [
                        {"category": "security", "severity": "CRITICAL", "description": "y"}
                    ],
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&category=correctness"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["reviewer_findings"][0]["category"] == "correctness"


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_empty_state(
    client: AsyncClient, db_session
):
    """Returns empty items list when no disagreements exist."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["category_counts"] == {}


@pytest.mark.asyncio
async def test_sr_reviewer_disagreement_tasks_pagination(
    client: AsyncClient, db_session
):
    """Pagination returns correct page slices."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                        "reviewer_findings": [
                            {
                                "category": "correctness",
                                "severity": "HIGH",
                                "description": "issue",
                            }
                        ],
                    }
                },
            )
        )
    await db_session.commit()

    resp_p0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&page=0&page_size=2"
    )
    assert resp_p0.status_code == 200
    body_p0 = resp_p0.json()
    assert body_p0["total"] == 3
    assert len(body_p0["items"]) == 2

    resp_p1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-reviewer-disagreements/tasks?days=30&page=1&page_size=2"
    )
    assert resp_p1.status_code == 200
    body_p1 = resp_p1.json()
    assert body_p1["total"] == 3
    assert len(body_p1["items"]) == 1


@pytest.mark.asyncio
async def test_digest_sr_trends_fp_fields_passthrough(client: AsyncClient, db_session):
    """Digest passes fp_count and false_positive_rate from sr_catch_rate_trends in design_doc."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "signal_last_computed_at": "2026-05-01T00:00:00",
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.7,
                    "miss_rate": 0.3,
                    "trend": "stable",
                    "fp_count": 2,
                    "tp_count": 3,
                    "false_positive_rate": 0.4,
                }
            },
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    body = response.json()
    trends = body.get("self_review_prediction_accuracy_trends")
    assert trends is not None
    correctness = trends["categories"]["correctness"]
    assert correctness["fp_count"] == 2
    assert correctness["tp_count"] == 3
    assert correctness["false_positive_rate"] == 0.4
    assert trends["signal_last_computed_at"] == "2026-05-01T00:00:00"


@pytest.mark.asyncio
async def test_digest_sr_trends_full_fidelity_passthrough(
    client: AsyncClient, db_session
):
    """Digest passes all canonical SR trend fields unchanged (full-fidelity contract)."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "bucket_mode": "cycle",
            "current_cycle_id": "cycle-1",
            "previous_cycle_id": "cycle-0",
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "trend": "improving",
                    "fp_count": 1,
                    "tp_count": 4,
                    "false_positive_rate": 0.2,
                }
            },
            "by_task_type": {"create": {"correctness": {"hit_count": 3}}},
            "by_scope": {
                "large": {
                    "correctness": {
                        "hit_rate": 0.4,
                        "miss_rate": 0.6,
                        "sample_count": 5,
                    }
                }
            },
            "by_severity": {
                "high": {
                    "current_miss_rate": 0.1,
                    "current_hit_rate": 0.9,
                    "trend": "stable",
                }
            },
            "sr_skip_rate": 0.05,
            "sr_skip_reason_distribution": {"legacy_unknown": 2},
            "by_direction": {"dir-abc": {"correctness": {"hit_count": 2}}},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None

    assert trends["bucket_mode"] == "cycle"
    assert trends["current_cycle_id"] == "cycle-1"
    assert trends["previous_cycle_id"] == "cycle-0"
    assert trends["sr_skip_rate"] == 0.05
    assert trends["sr_skip_reason_distribution"] == {"legacy_unknown": 2}
    assert "dir-abc" in trends["by_direction"]
    assert "execution_evidence_miss_rates" not in trends
    assert "edges_considered_stats" not in trends
    assert trends["by_severity"]["high"]["trend"] == "stable"
    assert trends["computed_at"] == "2026-05-01T00:00:00"
    assert trends["categories"]["correctness"]["fp_count"] == 1
    assert trends["by_scope"]["large"]["correctness"]["miss_rate"] == 0.6
    assert trends["by_scope"]["large"]["correctness"]["sample_count"] == 5


@pytest.mark.asyncio
async def test_digest_sr_trends_overall_verdict_passthrough(
    client: AsyncClient, db_session
):
    """Digest explicitly promotes overall_trend_verdict/significant/data_sufficient."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.72,
            "previous_overall_hit_rate": 0.65,
            "overall_trend_verdict": "improving",
            "overall_trend_significant": True,
            "overall_trend_data_sufficient": True,
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["overall_trend_verdict"] == "improving"
    assert trends["overall_trend_significant"] is True
    assert trends["overall_trend_data_sufficient"] is True

    # Stable/not-significant case
    project2 = make_project()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.70,
            "previous_overall_hit_rate": 0.71,
            "overall_trend_verdict": "stable",
            "overall_trend_significant": False,
            "overall_trend_data_sufficient": True,
        }
    }
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    response2 = await client.get(f"/api/admin/digest?project_id={project2.id}")
    assert response2.status_code == 200
    trends2 = response2.json().get("self_review_prediction_accuracy_trends")
    assert trends2 is not None
    assert trends2["overall_trend_verdict"] == "stable"
    assert trends2["overall_trend_significant"] is False

    # Absent (None) case — fields present but null
    project3 = make_project()
    project3.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
        }
    }
    db_session.add(project3)
    await db_session.commit()
    await db_session.refresh(project3)

    response3 = await client.get(f"/api/admin/digest?project_id={project3.id}")
    assert response3.status_code == 200
    trends3 = response3.json().get("self_review_prediction_accuracy_trends")
    assert trends3 is not None
    assert trends3["overall_trend_verdict"] is None
    assert trends3["overall_trend_significant"] is None
    assert trends3["overall_trend_data_sufficient"] is None


@pytest.mark.asyncio
async def test_digest_sr_trends_co_failure_pairs_passthrough(
    client: AsyncClient, db_session
):
    """Digest passes co_failure_pairs from sr_catch_rate_trends in design_doc."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "signal_last_computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "co_failure_pairs": [
                {
                    "cat_a": "correctness",
                    "cat_b": "test-coverage",
                    "co_fail_count": 5,
                    "p_b_given_a": 0.8333,
                    "base_rate_b": 0.4167,
                    "lift": 2.0,
                },
                {
                    "cat_a": "test-coverage",
                    "cat_b": "correctness",
                    "co_fail_count": 5,
                    "p_b_given_a": 1.0,
                    "base_rate_b": 0.5,
                    "lift": 2.0,
                },
            ],
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    pairs = trends.get("co_failure_pairs")
    assert isinstance(pairs, list)
    assert len(pairs) == 2
    first = pairs[0]
    assert first["cat_a"] == "correctness"
    assert first["cat_b"] == "test-coverage"
    assert first["co_fail_count"] == 5
    assert first["lift"] == 2.0


@pytest.mark.asyncio
async def test_digest_sr_trends_empty_optional_fields_present(
    client: AsyncClient, db_session
):
    """Digest returns sr trend keys with empty defaults when optional fields are absent."""
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "bucket_mode": "calendar",
            "current_cycle_id": None,
            "previous_cycle_id": None,
            "categories": {},
            "by_task_type": {},
            "by_severity": {},
            "sr_skip_rate": None,
            "sr_skip_reason_distribution": {},
            "by_direction": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None

    for key in (
        "bucket_mode",
        "current_cycle_id",
        "previous_cycle_id",
        "sr_skip_rate",
        "sr_skip_reason_distribution",
        "by_direction",
    ):
        assert key in trends, f"missing key: {key}"
    assert trends["sr_skip_rate"] is None
    assert trends["by_direction"] == {}
    assert "execution_evidence_miss_rates" not in trends
    assert "edges_considered_stats" not in trends


@pytest.mark.asyncio
async def test_serialize_sr_catch_rate_trends_is_stale(client: AsyncClient, db_session):
    """is_stale=True when computed_at is >7 days old; False when fresh; True when absent."""
    # Case 1: stale (8 days old)
    project = make_project()
    stale_ts = (datetime.utcnow() - timedelta(days=8)).isoformat()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": stale_ts,
            "categories": {"correctness": {"miss_rate": 0.3, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["is_stale"] is True

    # Case 2: fresh (1 day old)
    project2 = make_project()
    fresh_ts = (datetime.utcnow() - timedelta(days=1)).isoformat()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": fresh_ts,
            "categories": {"correctness": {"miss_rate": 0.2, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    response2 = await client.get(f"/api/admin/digest?project_id={project2.id}")
    assert response2.status_code == 200
    trends2 = response2.json().get("self_review_prediction_accuracy_trends")
    assert trends2 is not None
    assert trends2["is_stale"] is False

    # Case 3: computed_at absent → stale
    project3 = make_project()
    project3.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {"correctness": {"miss_rate": 0.1, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project3)
    await db_session.commit()
    await db_session.refresh(project3)

    response3 = await client.get(f"/api/admin/digest?project_id={project3.id}")
    assert response3.status_code == 200
    trends3 = response3.json().get("self_review_prediction_accuracy_trends")
    assert trends3 is not None
    assert trends3["is_stale"] is True


@pytest.mark.asyncio
async def test_serialize_sr_catch_rate_trends_handles_tz_aware_computed_at(
    client: AsyncClient, db_session
):
    """A tz-aware computed_at is normalized before the stale check runs."""
    project = make_project()
    computed_at = datetime.now(timezone.utc) - timedelta(days=1)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": computed_at.isoformat(),
            "categories": {"correctness": {"miss_rate": 0.2, "trend": "stable"}},
            "by_task_type": {},
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["is_stale"] is False


@pytest.mark.asyncio
async def test_digest_sr_trends_overall_sr_alone_passthrough(
    client: AsyncClient, db_session
):
    """Digest explicitly promotes overall_sr_alone_* fields from sr_catch_rate_trends."""
    # Case 1: all catches are reviewer-origin → combined high, author-only low
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
            "overall_hit_rate": 0.80,
            "overall_sr_alone_hit_rate": 0.10,
            "previous_overall_sr_alone_hit_rate": 0.12,
            "overall_sr_alone_trend_verdict": "stable",
            "overall_sr_alone_trend_significant": False,
            "overall_sr_alone_trend_data_sufficient": True,
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    trends = response.json().get("self_review_prediction_accuracy_trends")
    assert trends is not None
    assert trends["overall_hit_rate"] == 0.80
    assert trends["overall_sr_alone_hit_rate"] == 0.10, (
        "combined high but author-only should be low"
    )
    assert trends["overall_sr_alone_trend_verdict"] == "stable"
    assert trends["overall_sr_alone_trend_significant"] is False
    assert trends["overall_sr_alone_trend_data_sufficient"] is True

    # Case 2: data insufficient → fields present but None / stable / data_sufficient=False
    project2 = make_project()
    project2.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": "2026-05-01T00:00:00",
            "categories": {},
            "by_task_type": {},
        }
    }
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    response2 = await client.get(f"/api/admin/digest?project_id={project2.id}")
    assert response2.status_code == 200
    trends2 = response2.json().get("self_review_prediction_accuracy_trends")
    assert trends2 is not None
    # All five SR-alone fields must be present even when source lacks them
    for field in (
        "overall_sr_alone_hit_rate",
        "previous_overall_sr_alone_hit_rate",
        "overall_sr_alone_trend_verdict",
        "overall_sr_alone_trend_significant",
        "overall_sr_alone_trend_data_sufficient",
    ):
        assert field in trends2, f"missing promoted field: {field}"
    assert trends2["overall_sr_alone_hit_rate"] is None
    assert trends2["overall_sr_alone_trend_significant"] is None
    assert trends2["overall_sr_alone_trend_data_sufficient"] is None


# ---------------------------------------------------------------------------
# Per-direction SR breakdown endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direction_sr_breakdown_empty(client: AsyncClient, db_session):
    """No tasks → empty directions list with min_sample key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["directions"] == []
    assert body["min_sample"] == 3


@pytest.mark.asyncio
async def test_direction_sr_breakdown_basic(client: AsyncClient, db_session):
    """Directions with >=3 qualifying tasks appear with correct catch-rate stats."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    ctx_hit = {
        "direction_id": "d-aaa",
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {"check_results": {"correctness": {"passed": False}}},
    }
    ctx_miss = {
        "direction_id": "d-aaa",
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {"check_results": {"correctness": {"passed": True}}},
    }
    for ctx in [ctx_hit, ctx_hit, ctx_miss]:
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=ctx,
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["directions"]) == 1
    entry = body["directions"][0]
    assert entry["direction_id"] == "d-aaa"
    assert entry["sample_size"] == 3
    assert entry["worst_category"] == "correctness"
    assert entry["overall_catch_rate"] == round(2 / 3, 3)
    cat = entry["categories"]["correctness"]
    assert cat["sample_count"] == 3
    assert cat["hit_rate"] == round(2 / 3, 3)
    assert cat["miss_rate"] == round(1 / 3, 3)


@pytest.mark.asyncio
async def test_direction_sr_breakdown_excludes_insufficient_sample(
    client: AsyncClient, db_session
):
    """Directions with fewer than 3 qualifying tasks are omitted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-bbb",
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "check_results": {"correctness": {"passed": False}}
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    assert response.json()["directions"] == []


@pytest.mark.asyncio
async def test_direction_sr_breakdown_examples_finalized(
    client: AsyncClient, db_session
):
    """Miss examples flow through ``_finalize_examples`` (severity-sorted,
    deduped, capped). Old inline aggregation appended raw issues until cap=3
    and would have included the duplicate; the canonical path dedupes."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 qualifying tasks, all misses on "correctness". Each task has a
    # review_history with the same MAJOR description plus one CRITICAL
    # description. Old code would have produced a list with duplicates;
    # finalized output dedupes by cleaned text and surfaces CRITICAL first.
    rev_hist = [
        {
            "iteration": 0,
            "issues": [
                {"description": "off-by-one in the loop", "severity": "MAJOR"},
                {
                    "description": "race condition under concurrent calls",
                    "severity": "CRITICAL",
                },
            ],
        }
    ]
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-fin",
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "check_results": {"correctness": {"passed": True}}
                    },
                    "review_loop": {"review_history": rev_hist},
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    cat = response.json()["directions"][0]["categories"]["correctness"]
    examples = cat.get("examples") or []
    # Deduped: each unique description appears once even though 3 tasks
    # contributed it.
    descs = [ex["desc"] for ex in examples]
    assert len(descs) == len(set(descs))
    # CRITICAL is sorted before MAJOR.
    assert examples[0]["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_direction_sr_breakdown_categories_sorted_worst_first(
    client: AsyncClient, db_session
):
    """Category dict is ordered worst-first by miss_rate, and worst_category
    matches the first key — proving the endpoint pulls ordering from the
    canonical sort rather than inferring from raw iteration order."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 3 tasks: correctness hit rate 1.0 (0 miss), error-handling hit rate 0.0
    # (3 miss). error-handling should come first.
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "direction_id": "d-sort",
                    "_first_attempt_rejection_categories": [
                        "correctness",
                        "error-handling",
                    ],
                    "_self_review_outcome": {
                        "check_results": {
                            "correctness": {"passed": False},
                            "error-handling": {"passed": True},
                        }
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    entry = response.json()["directions"][0]
    cats = entry["categories"]
    first_key = next(iter(cats))
    assert first_key == "error-handling"
    assert entry["worst_category"] == "error-handling"
    assert cats["error-handling"]["miss_rate"] == 1.0
    assert cats["correctness"]["miss_rate"] == 0.0


@pytest.mark.asyncio
async def test_direction_sr_breakdown_uses_stored_snapshot(
    client: AsyncClient, db_session
):
    """When sr_catch_rate_trends.by_direction is stored on the project, the endpoint
    uses it as the primary source — not the narrow COMPLETED-only task query.

    Proof: project has a stored snapshot for direction 'd-stored' but zero
    qualifying tasks in the DB. The endpoint must return the stored entry.
    """
    stored_by_dir = {
        "d-lower": {
            "sample_count": 5,
            "miss_rate": 0.2,
            "overall_catch_rate": 0.8,
            "categories": {
                "style-conventions": {
                    "hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "sample_count": 5,
                }
            },
        },
        "d-stored": {
            "sample_count": 7,
            "miss_rate": 0.571,
            "overall_catch_rate": 0.429,
            "categories": {
                "scope-drift": {
                    "hit_rate": 0.8,
                    "miss_rate": 0.2,
                    "sample_count": 7,
                },
                "regression_risk": {
                    "hit_rate": 0.429,
                    "miss_rate": 0.571,
                    "sample_count": 7,
                },
            },
            "trend": "increasing",
            "previous_miss_rate": 0.4,
            "rate_history": [0.4, 0.571],
        },
    }
    project = make_project(
        directions=[{"id": "d-stored", "text": "Stored direction label"}],
        design_doc={"sr_catch_rate_trends": {"by_direction": stored_by_dir}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/projects/{project.id}/direction-sr-breakdown"
    )
    assert response.status_code == 200
    body = response.json()
    assert [entry["direction_id"] for entry in body["directions"]] == [
        "d-stored",
        "d-lower",
    ]
    assert len(body["directions"]) == 2
    entry = body["directions"][0]
    assert entry["direction_id"] == "d-stored"
    assert entry["direction_text"] == "Stored direction label"
    assert entry["sample_size"] == 7
    assert entry["overall_catch_rate"] == 0.429
    assert entry["worst_category"] == "regression_risk"
    assert list(entry["categories"]) == ["regression_risk", "scope-drift"]
    assert entry["trend"] == "increasing"
    assert entry["previous_miss_rate"] == 0.4
    assert entry["rate_history"] == [0.4, 0.571]


# ---------------------------------------------------------------------------
# SR missed retrospectives endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_all_iterations_miss(
    client: AsyncClient, db_session
):
    """Late-iteration categories in the paginated backlog use all-iterations truth."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Late iter backlog task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_all_iterations_rejection_categories": {
                    "correctness": 1,
                    "architecture": 1,
                },
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {"passed": False},
                        "architecture": {"passed": True},
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert "architecture" in row["missed_categories"]
    assert "correctness" in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_reviewer_origin_still_counts_as_miss(
    client: AsyncClient, db_session
):
    """Paginated backlog treats reviewer rescues as misses, not catches."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Reviewer rescue backlog",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_self_review_outcome": {
                    "verdict": "fail",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {
                            "passed": False,
                            "origin": "reviewer_recovery_after_incomplete_sr",
                        }
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert "correctness" in row["missed_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_total_not_capped_at_page_size(
    client: AsyncClient, db_session
):
    """Backlog total counts all matching tasks, not just the returned page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Backlog task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(205)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page_size=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 205
    assert len(body["items"]) == 3


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_task_types_returned(
    client: AsyncClient, db_session
):
    """task_types field lists all types in matched set, independent of task_type filter."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for ttype in ("create", "fix"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {ttype}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": ttype}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Without task_type filter: both types returned
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]

    # With task_type filter: task_types still lists all types, total is filtered
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?task_type=create"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_zero_based_pagination(
    client: AsyncClient, db_session
):
    """page parameter is 0-based: page=0 is the first page, page=1 skips the first batch."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Paginate task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(5)
        ]
    )
    await db_session.commit()

    r0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=0&page_size=3"
    )
    assert r0.status_code == 200
    body0 = r0.json()
    assert body0["total"] == 5
    assert len(body0["items"]) == 3

    r1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=1&page_size=3"
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["total"] == 5
    assert len(body1["items"]) == 2

    ids0 = {item["task_id"] for item in body0["items"]}
    ids1 = {item["task_id"] for item in body1["items"]}
    assert ids0.isdisjoint(ids1), "page=0 and page=1 must return non-overlapping task sets"


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_category_counts_stable_across_pages(
    client: AsyncClient, db_session
):
    """category_counts is identical on every page and matches actual per-category totals."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 12 tasks missed 'correctness', 8 tasks missed 'architecture', 5 missed both
    for i in range(12):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Correctness-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Architecture-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_rejection_categories": {"architecture": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"architecture": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Both-category task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1, "architecture": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": True},
                            "architecture": {"passed": True},
                        },
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Fetch page 0 and page 1 with small page_size to force multi-page
    r0 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=0&page_size=10"
    )
    assert r0.status_code == 200
    body0 = r0.json()

    r1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page=1&page_size=10"
    )
    assert r1.status_code == 200
    body1 = r1.json()

    # category_counts must be present on both pages
    assert "category_counts" in body0
    assert "category_counts" in body1

    # counts are identical across pages
    assert body0["category_counts"] == body1["category_counts"]

    # correctness: 12 solo + 5 both = 17; architecture: 8 solo + 5 both = 13
    assert body0["category_counts"]["correctness"] == 17
    assert body0["category_counts"]["architecture"] == 13

    # With task_type=create filter: only create tasks (12 correctness-only + 5 both)
    r_create = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?task_type=create&page_size=5"
    )
    assert r_create.status_code == 200
    body_create = r_create.json()
    assert body_create["category_counts"]["correctness"] == 17
    assert body_create["category_counts"]["architecture"] == 5


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_category_counts_invariant_to_category_filter(
    client: AsyncClient, db_session
):
    """category_counts reflects the full population regardless of the `category` filter.

    When a category chip is active, `items`/`total` must narrow to that
    category, but `category_counts` must stay byte-identical to the unfiltered
    response so the other chips keep showing their independent counts.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 12 correctness-only, 8 architecture-only, 5 both.
    for i in range(12):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Correctness-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(8):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Architecture-only task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_rejection_categories": {"architecture": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"architecture": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Both-category task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {
                        "correctness": 1,
                        "architecture": 1,
                    },
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {
                            "correctness": {"passed": True},
                            "architecture": {"passed": True},
                        },
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Unfiltered: full population. correctness 12+5=17, architecture 8+5=13.
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?page_size=200"
    )
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["category_counts"]["correctness"] == 17
    assert body_all["category_counts"]["architecture"] == 13
    # total = 12 + 8 + 5 distinct tasks = 25
    assert body_all["total"] == 25

    # Filtered to correctness: chip counts must be IDENTICAL to unfiltered.
    r_corr = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?category=correctness&page_size=200"
    )
    assert r_corr.status_code == 200
    body_corr = r_corr.json()
    assert body_corr["category_counts"] == body_all["category_counts"]
    # ...but total / items narrow to only tasks that missed correctness (17).
    assert body_corr["total"] == 17
    assert len(body_corr["items"]) == 17
    assert all(
        "correctness" in item["missed_categories"] for item in body_corr["items"]
    )

    # Filtered to architecture: chip counts still identical to unfiltered.
    r_arch = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?category=architecture&page_size=200"
    )
    assert r_arch.status_code == 200
    body_arch = r_arch.json()
    assert body_arch["category_counts"] == body_all["category_counts"]
    assert body_arch["total"] == 13
    assert len(body_arch["items"]) == 13
    assert all(
        "architecture" in item["missed_categories"] for item in body_arch["items"]
    )


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_drilldown_fields_present(
    client: AsyncClient, db_session
):
    """sr_check_results and reviewer_findings are included in each response item."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_desc = "y" * 450
    db_session.add(
        make_task(
            project_id=project.id,
            title="Drilldown task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {
                        "correctness": {
                            "passed": True,
                            "issue": "sr note",
                            "origin": "reviewer",
                        },
                        "performance": {"passed": False},
                    },
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": long_desc,
                        },
                        {
                            "category": "security",
                            "severity": "LOW",
                            "description": "ignored category",
                        },
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    # sr_check_results must be present and filtered to missed categories only
    assert "sr_check_results" in row, "sr_check_results missing from response item"
    assert set(row["sr_check_results"]) == {"correctness"}
    assert row["sr_check_results"]["correctness"]["origin"] == "reviewer"
    assert row["sr_check_results"]["correctness"]["issue"] == "sr note"
    # reviewer_findings must be present and filtered to missed categories, description truncated
    assert "reviewer_findings" in row, "reviewer_findings missing from response item"
    assert row["reviewer_findings"] == [
        {
            "category": "correctness",
            "severity": "HIGH",
            "description": long_desc[:400],
        }
    ]


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_scope_filter(
    client: AsyncClient, db_session
):
    """?scope=<value> filters items and rolls up total/category_counts honestly."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _ctx(scope: str, missed_cat: str = "correctness"):
        return {
            "scope": scope,
            "_first_attempt_rejection_categories": {missed_cat: 1},
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {missed_cat: {"passed": True}},
                "blind_spots": [],
            },
        }

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="Small task A",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("small", "correctness"),
            ),
            make_task(
                project_id=project.id,
                title="Small task B",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("small", "performance"),
            ),
            make_task(
                project_id=project.id,
                title="Large task A",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("large", "correctness"),
            ),
        ]
    )
    await db_session.commit()

    # Unfiltered baseline
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert r_all.status_code == 200
    body_all = r_all.json()
    assert body_all["total"] == 3
    assert body_all["category_counts"] == {"correctness": 2, "performance": 1}

    # Filter to scope=large — only the single large-scope task should match
    r_large = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?scope=large"
    )
    assert r_large.status_code == 200
    body_large = r_large.json()
    assert body_large["total"] == 1
    scopes = {item["scope"] for item in body_large["items"]}
    assert scopes == {"large"}
    # category_counts must reflect ONLY the scope-filtered subset
    assert body_large["category_counts"] == {"correctness": 1}
    assert "performance" not in body_large["category_counts"]

    # Filter to scope=small — both small-scope tasks, both categories
    r_small = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?scope=small"
    )
    assert r_small.status_code == 200
    body_small = r_small.json()
    assert body_small["total"] == 2
    assert body_small["category_counts"] == {"correctness": 1, "performance": 1}


@pytest.mark.asyncio
async def test_sr_missed_retrospectives_min_severity_filter(
    client: AsyncClient, db_session
):
    """?min_severity filters out tasks whose reviewer findings are all below the threshold.

    total and category_counts must reflect the severity-filtered subset.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _ctx(severity: str, missed_cat: str = "correctness"):
        return {
            "_first_attempt_rejection_categories": {missed_cat: 1},
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {missed_cat: {"passed": True}},
                "reviewer_findings": [
                    {"category": missed_cat, "severity": severity, "description": "x"}
                ],
                "blind_spots": [],
            },
        }

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="Critical miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("CRITICAL", "correctness"),
            ),
            make_task(
                project_id=project.id,
                title="High miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("HIGH", "performance"),
            ),
            make_task(
                project_id=project.id,
                title="Low miss",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context=_ctx("LOW", "correctness"),
            ),
        ]
    )
    await db_session.commit()

    # Unfiltered baseline: all three tasks
    r_all = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives"
    )
    assert r_all.status_code == 200
    assert r_all.json()["total"] == 3

    # min_severity=HIGH keeps CRITICAL + HIGH, drops the LOW-only task
    r_high = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?min_severity=HIGH"
    )
    assert r_high.status_code == 200
    body_high = r_high.json()
    assert body_high["total"] == 2
    titles = {item["title"] for item in body_high["items"]}
    assert titles == {"Critical miss", "High miss"}
    # category_counts must reflect ONLY the severity-filtered subset
    assert body_high["category_counts"] == {"correctness": 1, "performance": 1}

    # min_severity=CRITICAL keeps only the CRITICAL task
    r_crit = await client.get(
        f"/api/admin/projects/{project.id}/sr-missed-retrospectives?min_severity=CRITICAL"
    )
    assert r_crit.status_code == 200
    body_crit = r_crit.json()
    assert body_crit["total"] == 1
    assert body_crit["category_counts"] == {"correctness": 1}


# ---------------------------------------------------------------------------
# SR unhandled edges endpoint — task_types and pagination parity tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_unhandled_edges_task_types_returned(
    client: AsyncClient, db_session
):
    """task_types field lists all types in matched set, independent of task_type filter."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for ttype in ("create", "fix"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {ttype}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": ttype}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "edges_considered": [
                            {"case": "empty input", "handled": False}
                        ],
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    # Without task_type filter: both types returned
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]

    # With task_type filter: task_types still lists all types, total is filtered
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?task_type=create"
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["task_types"]) == ["create", "fix"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_unhandled_edges_total_not_capped_at_page_size(
    client: AsyncClient, db_session
):
    """Backlog total counts all matching tasks, not just the returned page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Edge task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "edges_considered": [
                            {"case": "null pointer", "handled": False}
                        ],
                        "blind_spots": [],
                    },
                },
            )
            for i in range(205)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?page_size=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 205
    assert len(body["items"]) == 3


@pytest.mark.asyncio
async def test_sr_unhandled_edges_blind_spot_counts(
    client: AsyncClient, db_session
):
    """blind_spot_counts aggregates themes with first-6-token normalisation,
    dedupes per task, drops disallowed themes, and remains anchored to the
    days+task_type universe when the blind_spot filter narrows rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task A: two unhandled edges, both blind-spots tokenize to the SAME
    # 6-token theme "no lock around state mutation that" — dedupe must count
    # this task as a single bump for that theme.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task A",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "race",
                            "handled": False,
                            "blind_spot": "No lock around state mutation that touches DB",
                        },
                        {
                            "case": "race-2",
                            "handled": False,
                            "blind_spot": "no LOCK around state mutation that runs concurrently",
                        },
                    ],
                },
            },
        )
    )
    # Task B: one unhandled edge with the SAME theme — second task bump.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task B",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "race",
                            "handled": False,
                            "blind_spot": "no lock around state mutation that breaks",
                        },
                    ],
                },
            },
        )
    )
    # Task C: distinct theme + a disallowed blind-spot that must be skipped +
    # an empty/whitespace blind-spot that must be skipped.
    db_session.add(
        make_task(
            project_id=project.id,
            title="task C",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_self_review_outcome": {
                    "edges_considered": [
                        {
                            "case": "timeout",
                            "handled": False,
                            "blind_spot": "Partial retry on network timeout boundary",
                        },
                        {
                            "case": "skip",
                            "handled": False,
                            "blind_spot": "did not run the full backend test suite",
                        },
                        {
                            "case": "blank",
                            "handled": False,
                            "blind_spot": "   ",
                        },
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
    )
    assert response.status_code == 200
    body = response.json()
    counts = body["blind_spot_counts"]
    # Dedupe: A contributes 1, B contributes 1 → 2 for the shared theme.
    assert counts["no lock around state mutation that"] == 2
    # Distinct theme from task C.
    assert counts["partial retry on network timeout boundary"] == 1
    # Disallowed and empty entries excluded — only the two themes above.
    assert set(counts.keys()) == {
        "no lock around state mutation that",
        "partial retry on network timeout boundary",
    }

    # Filtering by blind_spot narrows items + total but counts stay anchored
    # to the (days + task_type) universe so pills remain swap-able.
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?blind_spot=no+lock+around+state+mutation+that"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    titles = sorted(item["title"] for item in body["items"])
    assert titles == ["task A", "task B"]
    assert body["blind_spot_counts"] == counts

    # task_type filter narrows the counts universe.
    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives?task_type=fix"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blind_spot_counts"] == {
        "partial retry on network timeout boundary": 1,
    }


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_matches_exact_edge_case_label(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="case match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "missed null guard on cache write",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="case prefix match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "network guard timeout fallback",
                                "handled": False,
                                "blind_spot": "Missing RETRY guard around network timeout fallback",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="non match",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "different issue",
                                "handled": False,
                                "blind_spot": "unrelated serialization gap",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["case match"]
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }
    assert "blind_spot_counts" in body

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=network+guard+timeout+fallback"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["case prefix match"]
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=network+guard"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["edge_case_counts"] == {
        "different issue": 1,
        "network guard timeout fallback": 1,
        "retry path null pointer": 1,
    }


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_counts_stable_across_pages(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="create retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "Retry guard missing on write path cache",
                            },
                            {
                                "case": "timeout boundary",
                                "handled": False,
                                "blind_spot": "partial retry on timeout boundary",
                            },
                            {
                                "case": "serialization gap",
                                "handled": False,
                                "blind_spot": "missing JSON shape validation",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="fix retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on write path db",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="non retry task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "serialization gap",
                                "handled": False,
                                "blind_spot": "missing JSON shape validation",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="blind-spot sibling task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "queue fallback",
                                "handled": False,
                                "blind_spot": "retry guard missing on write path queue",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response_full = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=2&page=0"
    )
    assert response_full.status_code == 200
    body_full = response_full.json()
    create_item = next(
        item for item in body_full["items"] if item["title"] == "create retry task"
    )
    assert create_item["unhandled_edges"] == [
        {
            "case": "Retry path null pointer",
            "handled": False,
            "blind_spot": "Retry guard missing on write path cache",
        },
        {
            "case": "timeout boundary",
            "handled": False,
            "blind_spot": "partial retry on timeout boundary",
        },
        {
            "case": "serialization gap",
            "handled": False,
            "blind_spot": "missing JSON shape validation",
        },
    ]

    response_page_1 = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=1&page=0"
    )
    assert response_page_1.status_code == 200
    body_page_1 = response_page_1.json()
    assert body_page_1["total"] == 2
    assert len(body_page_1["items"]) == 1
    assert sorted(body_page_1["task_types"]) == ["create", "fix"]
    assert body_page_1["blind_spot_counts"] == {
        "retry guard missing on write path": 2,
    }
    assert body_page_1["edge_case_counts"] == {
        "retry path null pointer": 2,
        "serialization gap": 2,
        "queue fallback": 1,
        "timeout boundary": 1,
    }

    response_page_2 = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&page_size=1&page=1"
    )
    assert response_page_2.status_code == 200
    body_page_2 = response_page_2.json()
    assert body_page_2["total"] == 2
    assert len(body_page_2["items"]) == 1
    assert body_page_2["edge_case_counts"] == body_page_1["edge_case_counts"]
    assert body_page_2["blind_spot_counts"] == body_page_1["blind_spot_counts"]

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer&blind_spot=retry+guard+missing+on+write+path"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["edge_case_counts"] == {
        "retry path null pointer": 2,
        "queue fallback": 1,
    }
    assert body["blind_spot_counts"] == body_page_1["blind_spot_counts"]
    create_item = next(
        item for item in body["items"] if item["title"] == "create retry task"
    )
    assert create_item["unhandled_edges"] == [
        {
            "case": "Retry path null pointer",
            "handled": False,
            "blind_spot": "Retry guard missing on write path cache",
        },
        {
            "case": "timeout boundary",
            "handled": False,
            "blind_spot": "partial retry on timeout boundary",
        },
        {
            "case": "serialization gap",
            "handled": False,
            "blind_spot": "missing JSON shape validation",
        },
    ]


@pytest.mark.asyncio
async def test_sr_unhandled_edges_case_pattern_dedupes_duplicate_labels_per_task(
    client: AsyncClient, db_session
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title="duplicate label task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on cache write",
                            },
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on db write",
                            },
                        ],
                    },
                },
            ),
            make_task(
                project_id=project.id,
                title="single label task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "fix"}],
                context={
                    "_self_review_outcome": {
                        "edges_considered": [
                            {
                                "case": "Retry path null pointer",
                                "handled": False,
                                "blind_spot": "retry guard missing on queue write",
                            },
                        ],
                    },
                },
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-unhandled-edges-retrospectives"
        "?case_pattern=retry+path+null+pointer"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["edge_case_counts"] == {"retry path null pointer": 2}


# ---------------------------------------------------------------------------
# SR recent misses endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_recent_misses_empty(client: AsyncClient, db_session):
    """No tasks → endpoint returns empty list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["misses"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_sr_recent_misses_basic(client: AsyncClient, db_session):
    """Tasks where SR missed a human-caught category appear in the response."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task where SR passed "correctness" but human rejected for it → miss
    task = make_task(
        project_id=project.id,
        title="Add feature X",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "scope": "small",
            "_first_attempt_rejection_categories": ["correctness", "error-handling"],
            "_self_review_outcome": {
                "verdict": "pass",
                "fixes_applied": False,
                "check_results": {
                    "correctness": {"passed": True},
                    "error-handling": {"passed": False},
                },
                "blind_spots": [],
            },
        },
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert row["title"] == "Add feature X"
    assert row["scope"] == "small"
    assert row["task_type"] == "create"
    # correctness was missed (SR passed it, human flagged it)
    assert "correctness" in row["missed_categories"]
    # error-handling was caught (SR failed it, human flagged it)
    assert "error-handling" in row["caught_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_includes_drilldown_fields(
    client: AsyncClient, db_session
):
    """SR drilldown fields are filtered to missed categories and truncated."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_desc = "x" * 450
    db_session.add(
        make_task(
            project_id=project.id,
            title="Add feature Y",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness", "performance"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "check_results": {
                        "correctness": {
                            "passed": True,
                            "issue": "sr note",
                            "origin": "reviewer",
                        },
                        "performance": {"passed": False},
                    },
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": long_desc,
                        },
                        {
                            "category": "security",
                            "severity": "LOW",
                            "description": "ignored",
                        },
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    row = response.json()["misses"][0]
    assert set(row["sr_check_results"]) == {"correctness"}
    assert row["sr_check_results"]["correctness"]["origin"] == "reviewer"
    assert row["sr_check_results"]["correctness"]["issue"] == "sr note"
    assert row["reviewer_findings"] == [
        {
            "category": "correctness",
            "severity": "HIGH",
            "description": long_desc[:400],
        }
    ]


@pytest.mark.asyncio
async def test_sr_recent_misses_reviewer_origin_still_counts_as_miss(
    client: AsyncClient, db_session
):
    """Reviewer-stamped passed=False does not count as an author catch."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Reviewer rescue",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "fail",
                    "check_results": {
                        "correctness": {
                            "passed": False,
                            "issue": "reviewer found it",
                            "origin": "reviewer",
                        }
                    },
                    "blind_spots": [],
                    "reviewer_findings": [
                        {
                            "category": "correctness",
                            "severity": "HIGH",
                            "description": "reviewer rescue",
                        }
                    ],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert "correctness" in row["missed_categories"]
    assert "correctness" not in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_excludes_fully_caught(client: AsyncClient, db_session):
    """Tasks where SR caught all human-flagged categories are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # SR failed "correctness" AND human flagged "correctness" → no miss
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "fail",
                    "fixes_applied": True,
                    "check_results": {"correctness": {"passed": False}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["misses"] == []


@pytest.mark.asyncio
async def test_sr_recent_misses_excludes_tasks_without_sr_outcome(
    client: AsyncClient, db_session
):
    """Tasks missing _self_review_outcome or _first_attempt_rejection_categories are skipped."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No SR outcome
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_first_attempt_rejection_categories": ["correctness"]},
        )
    )
    # No rejection categories
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_self_review_outcome": _sr_valid_outcome()},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_sr_recent_misses_category_filter(client: AsyncClient, db_session):
    """The ?category= filter returns only rows that missed that specific category."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task 1: missed "correctness"
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task A",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": ["correctness"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"correctness": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    # Task 2: missed "security"
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task B",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": ["security"],
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?category=correctness"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["misses"][0]["title"] == "Task A"


@pytest.mark.asyncio
async def test_sr_recent_misses_limit_param(client: AsyncClient, db_session):
    """The ?limit= parameter caps the number of returned rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": ["correctness"],
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_project_isolation(client: AsyncClient, db_session):
    """Tasks from other projects are not included in the response."""
    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    miss_ctx = {
        "_first_attempt_rejection_categories": ["correctness"],
        "_self_review_outcome": {
            "verdict": "pass",
            "fixes_applied": False,
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [],
        },
    }
    db_session.add(
        make_task(
            project_id=project_a.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context=miss_ctx,
        )
    )
    db_session.add(
        make_task(
            project_id=project_b.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context=miss_ctx,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project_a.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_sr_recent_misses_all_iterations_miss(client: AsyncClient, db_session):
    """A category only in _all_iterations_rejection_categories (not first-attempt) is detected."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Late-iteration category "architecture" is absent from first-attempt but present
    # in _all_iterations_rejection_categories.  SR passed it → miss.
    db_session.add(
        make_task(
            project_id=project.id,
            title="Late iter task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_rejection_categories": {"correctness": 1},
                "_all_iterations_rejection_categories": {
                    "correctness": 1,
                    "architecture": 1,
                },
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {
                        "correctness": {"passed": False},
                        "architecture": {"passed": True},
                    },
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["misses"][0]
    assert "architecture" in row["missed_categories"]
    assert "correctness" in row["caught_categories"]


@pytest.mark.asyncio
async def test_sr_recent_misses_total_before_limit(client: AsyncClient, db_session):
    """total reflects all matching tasks, not just the capped page."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(7):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 7
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_total_not_capped_at_scan_limit(
    client: AsyncClient, db_session
):
    """total keeps counting beyond the old 2000-row scan cap."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                title=f"Task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={
                    "_first_attempt_rejection_categories": {"correctness": 1},
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [],
                    },
                },
            )
            for i in range(2005)
        ]
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-recent-misses?limit=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2005
    assert len(body["misses"]) == 3


@pytest.mark.asyncio
async def test_sr_recent_misses_fallback_first_attempt(client: AsyncClient, db_session):
    """When no _all_iterations and no review_history, _first_attempt_rejection_categories is used."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Old task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "fix"}],
            context={
                "_first_attempt_rejection_categories": {"security": 1},
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {"security": {"passed": True}},
                    "blind_spots": [],
                },
            },
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/sr-recent-misses")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert "security" in body["misses"][0]["missed_categories"]


# ── POST /regression-cases/{regression_class}/clear-manual-intervention ─────


# ── POST /regression-cases/{regression_class}/close ─────────────────────────


# ---------------------------------------------------------------------------
# Fresh-reviewer cycle-over-cycle trend (digest surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_trend_null_without_design_doc(
    client: AsyncClient, db_session
):
    """Project without fresh_reviewer_trend in design_doc → digest returns null trend."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    assert response.json()["fresh_reviewer_trend"] is None


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_trend_surfaces_from_design_doc(
    client: AsyncClient, db_session
):
    """When design_doc has a fresh_reviewer_trend entry the digest surfaces all expected keys."""
    stored_trend = {
        "computed_at": "2026-05-01T00:00:00",
        "bucket_mode": "cycle",
        "current_cycle_id": "cycle-a",
        "previous_cycle_id": "cycle-b",
        "current_sample_size": 8,
        "previous_sample_size": 6,
        "per_category_hit_rate": {
            "correctness": {
                "current": 0.75,
                "previous": 0.6,
                "delta": 0.15,
                "trend": "improving",
            },
        },
        "added_value": {
            "correctness": {"current": 3, "previous": 2, "trend": "stable"},
        },
        "approval_rate_lift": {
            "current": 0.12,
            "previous": 0.08,
            "delta": 0.04,
            "trend": "stable",
        },
    }
    project = make_project(design_doc={"fresh_reviewer_trend": stored_trend})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    frt = response.json()["fresh_reviewer_trend"]
    assert frt is not None
    assert frt["bucket_mode"] == "cycle"
    assert frt["current_cycle_id"] == "cycle-a"
    assert frt["previous_cycle_id"] == "cycle-b"
    assert frt["current_sample_size"] == 8
    assert frt["previous_sample_size"] == 6
    # Per-category hit rate improving classification is preserved
    assert frt["per_category_hit_rate"]["correctness"]["trend"] == "improving"
    assert frt["per_category_hit_rate"]["correctness"]["delta"] == 0.15
    # Added value and approval rate lift are surfaced
    assert frt["added_value"]["correctness"]["current"] == 3
    assert frt["approval_rate_lift"]["current"] == 0.12


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_trend_regressing_classification(
    client: AsyncClient, db_session
):
    """Regressing trend stored in design_doc is returned as-is by the digest."""
    stored_trend = {
        "computed_at": "2026-05-01T00:00:00",
        "bucket_mode": "calendar",
        "current_cycle_id": None,
        "previous_cycle_id": None,
        "current_sample_size": 7,
        "previous_sample_size": 5,
        "per_category_hit_rate": {
            "style-conventions": {
                "current": 0.4,
                "previous": 0.7,
                "delta": -0.3,
                "trend": "regressing",
            },
        },
        "added_value": {},
        "approval_rate_lift": {
            "current": 0.05,
            "previous": 0.15,
            "delta": -0.10,
            "trend": "regressing",
        },
    }
    project = make_project(design_doc={"fresh_reviewer_trend": stored_trend})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    frt = response.json()["fresh_reviewer_trend"]
    assert frt is not None
    assert frt["bucket_mode"] == "calendar"
    assert frt["per_category_hit_rate"]["style-conventions"]["trend"] == "regressing"
    assert frt["approval_rate_lift"]["trend"] == "regressing"
    # Mixed FR-on / FR-off reflected in asymmetric sample sizes
    assert frt["current_sample_size"] == 7
    assert frt["previous_sample_size"] == 5


@pytest.mark.asyncio
async def test_digest_fresh_reviewer_trend_null_when_below_minimum_sample(
    client: AsyncClient, db_session
):
    """compute_fresh_reviewer_trend returns None below _MIN_SR_SAMPLE and writes nothing
    to design_doc — digest should return null trend in that case."""
    # Simulate a project where the trend computation found insufficient sample and
    # therefore did NOT persist a fresh_reviewer_trend key.
    project = make_project(design_doc={"sr_catch_rate_trends": {}})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    # No fresh_reviewer_trend key → null
    assert response.json()["fresh_reviewer_trend"] is None


# ── GET /regression-cases ────────────────────────────────────────────────────


# ── GET /regression-cases/manual-intervention ─────────────────────────────────


# ── GET /regression-cases/recently-closed ─────────────────────────────────────


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/sr-alert-drilldown tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_alert_drilldown_returns_missed_tasks(client: AsyncClient, db_session):
    """Tasks where SR passed but human rejected on target category appear in drilldown."""
    project = make_project(
        design_doc={
            "sr_catch_rate_trends_alert": {
                "computed_at": datetime.utcnow().isoformat(),
                "categories": ["correctness"],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={
            "_self_review_outcome": {
                "verdict": "pass",
                "check_results": {"correctness": {"passed": True}},
            },
            "_first_attempt_rejection_categories": ["correctness"],
        },
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness", "alert_type": "trends"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["alert_type"] == "trends"
    assert data["categories"] == ["correctness"]
    item = data["items"][0]
    assert item["task_id"] == str(task.id)
    assert "correctness" in item["missed_categories"]


@pytest.mark.asyncio
async def test_sr_alert_drilldown_empty_when_no_alert(client: AsyncClient, db_session):
    """When no alert is active the window falls back to 14 days; no matching tasks → empty."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_sr_alert_drilldown_invalid_alert_type(client: AsyncClient, db_session):
    """alert_type values other than 'trends'/'floor' return 400."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/sr-alert-drilldown",
        params={"categories": "correctness", "alert_type": "invalid"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /api/admin/regression-dispatcher/health
# ---------------------------------------------------------------------------


# test: compute_mtti_p50_sec helper directly (unit tests in test_pipeline_regression_dispatcher.py)


# ---------------------------------------------------------------------------
# /api/admin/anomaly-tracker/snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anomaly_tracker_snapshot_empty(client: AsyncClient):
    """Empty persistent_anomalies returns empty projects list and null run_at."""
    import app.shared_state as _ss

    saved_dict = dict(_ss.persistent_anomalies)
    saved_run_at = _ss.last_anomaly_detection_run_at
    _ss.persistent_anomalies.clear()
    _ss.last_anomaly_detection_run_at = None
    try:
        response = await client.get("/api/admin/anomaly-tracker/snapshot")
        assert response.status_code == 200
        data = response.json()
        assert data["last_detection_run_at"] is None
        assert data["freshness_window_hours"] == 24
        assert data["total"] == 0
        assert data["projects"] == []
    finally:
        _ss.persistent_anomalies.clear()
        _ss.persistent_anomalies.update(saved_dict)
        _ss.last_anomaly_detection_run_at = saved_run_at


@pytest.mark.asyncio
async def test_anomaly_tracker_snapshot_fresh_and_stale(
    client: AsyncClient, db_session
):
    """Returns sorted entries, marks stuck=true for stale last_seen, echoes run_at."""
    import app.shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    proj_key = str(project.id)
    now = _utcnow()
    fresh_last = (now - timedelta(hours=2)).isoformat()
    stale_last = (now - timedelta(hours=72)).isoformat()
    first_seen = (now - timedelta(hours=96)).isoformat()
    run_at = now - timedelta(minutes=3)

    saved_dict = dict(_ss.persistent_anomalies)
    saved_run_at = _ss.last_anomaly_detection_run_at
    _ss.persistent_anomalies.clear()
    _ss.persistent_anomalies[proj_key] = {
        "FailureRateMomentum": {
            "first_seen": first_seen,
            "last_seen": fresh_last,
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "fresh anomaly",
            "affected_directions": ["dir-1"],
        },
        "CrossDirectionFingerprint_abc": {
            "first_seen": first_seen,
            "last_seen": stale_last,
            "occurrence_count": 2,
            "severity": "medium",
            "latest_details": "stale anomaly",
            "fingerprint": "abc",
        },
    }
    _ss.last_anomaly_detection_run_at = run_at
    try:
        response = await client.get(
            "/api/admin/anomaly-tracker/snapshot",
            params={"project_id": str(project.id), "freshness_hours": 48},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["last_detection_run_at"] == run_at.isoformat()
        assert data["freshness_window_hours"] == 48
        assert data["total"] == 2
        assert len(data["projects"]) == 1
        bucket = data["projects"][0]
        assert bucket["project_id"] == proj_key
        entries = bucket["anomalies"]
        # Sorted by last_seen desc — fresh first.
        assert entries[0]["key"] == "FailureRateMomentum"
        assert entries[0]["stuck"] is False
        assert entries[0]["affected_directions"] == ["dir-1"]
        assert entries[0]["last_dispatch_attempt_at"] is None
        assert entries[1]["key"] == "CrossDirectionFingerprint_abc"
        assert entries[1]["stuck"] is True
        assert entries[1]["fingerprint"] == "abc"
    finally:
        _ss.persistent_anomalies.clear()
        _ss.persistent_anomalies.update(saved_dict)
        _ss.last_anomaly_detection_run_at = saved_run_at


# ---------------------------------------------------------------------------
# self_review_effectiveness sr_fixed_count / sr_clean_count counts
# ---------------------------------------------------------------------------


def _sr_outcome(fixes_applied: bool) -> dict:
    return {
        "verdict": "pass",
        "fixes_applied": fixes_applied,
        "check_results": {},
        "blind_spots": [],
    }


@pytest.mark.asyncio
async def test_digest_self_review_effectiveness_includes_clean_and_fixed_counts(
    client: AsyncClient, db_session
):
    """sr_fixed_count and sr_clean_count are present and equal the integer counts of
    the fixed vs clean subsets of tasks_with_sr."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 fixed, 3 clean → sample_size=5
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=True),
                },
            )
        )
    for _ in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=False),
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    sre = response.json()["self_review_effectiveness"]
    assert sre is not None
    assert sre["sample_size"] == 5
    assert sre["sr_fixed_count"] == 2
    assert sre["sr_clean_count"] == 3
    # Existing fields are unchanged.
    assert "with_self_review_approval_rate" in sre
    assert "fixed_verdict_approval_rate" in sre
    assert "clean_verdict_approval_rate" in sre


@pytest.mark.asyncio
async def test_digest_self_review_effectiveness_below_threshold_is_none(
    client: AsyncClient, db_session
):
    """When tasks_with_sr is below the existing threshold (3), the entire
    self_review_effectiveness block remains None — the new count fields do not
    cause the block to render at smaller sample sizes."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 tasks with SR — below the threshold of 3.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=curr,
                status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
                payloads=[{"type": "fix"}],
                context={
                    "_first_attempt_succeeded": True,
                    "_self_review_outcome": _sr_outcome(fixes_applied=False),
                },
            )
        )
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["self_review_effectiveness"] is None


# ── _summarize_symptoms_by_layer + layer_summary integration ──────────────────


def _layer_entry(fingerprint: str, observed_at: datetime, sample: str = "") -> dict:
    return {
        "fingerprint": fingerprint,
        "observed_at": observed_at.isoformat(),
        "sample": sample,
    }


# ── GET /regression-cases/trend ──────────────────────────────────────────────


# ── GET /regression-cases/layer-defense-density ─────────────────────────────


# ── _compute_layer_coverage_by_class / _compute_symptom_layers_by_class ──────


def _make_regression_case(regression_class: str, fix_log=None, symptoms=None):
    """Lightweight stub — only the fields accessed by the coverage helpers."""
    import types

    case = types.SimpleNamespace(
        regression_class=regression_class,
        fingerprint_to_fix_log=fix_log or [],
        symptoms_by_layer=symptoms or {},
    )
    return case


# ── GET /regression-cases/cross-layer-compliance ─────────────────────────────


def _cross_layer_entry(layers_targeted=None, spawn_layers=None, days_ago=1):
    now = _utcnow()
    entry = {
        "fingerprint": "test_class",
        "fix_pr_number": 100,
        "merged_at": (now - timedelta(days=days_ago)).isoformat(),
        "spawn_was_cross_layer": True,
        "spawn_layers_enumerated": spawn_layers or ["CI_WORKFLOW", "AGENT_VM"],
    }
    if layers_targeted is not None:
        entry["layers_targeted"] = layers_targeted
    return entry


# ── recurrence_by_bucket in cross-layer-compliance ──────────────────────────


# ---------------------------------------------------------------------------
# GET /regression-cases/first-attempt-compliance-trend
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Viewport-debt coverage_summary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewport_debt_coverage_summary_in_response(
    client: AsyncClient, db_session
):
    """GET /viewport-debt returns coverage_summary with correct shape."""
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    membership = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(membership)
    await db_session.commit()

    fake_rows = [
        {
            "component": f"admin-ui/src/components/Comp{i}.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 20 - i,
            "has_viewport_test": (i % 5 == 0),
            "regression_case_count": 0,
            "source_url": None,
        }
        for i in range(20)
    ]
    top_rows = fake_rows[:15]
    # 4 rows have viewport tests (i=0,5,10,15); 16 do not
    untested_paths = [r["component"] for r in fake_rows if not r["has_viewport_test"]]
    expected_summary = {
        "total_components": 20,
        "untested_count": 16,
        "untested_components": untested_paths,
    }

    with patch(
        "app.viewport_debt._scan_viewport_debt_rows",
        return_value=(top_rows, [], expected_summary),
    ):
        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    assert "rows" in body
    assert "untested_rows" in body
    assert "scanned_at" in body
    assert "coverage_summary" in body
    cs = body["coverage_summary"]
    assert cs["total_components"] == 20
    assert cs["untested_count"] == 16
    assert len(cs["untested_components"]) == 16


@pytest.mark.asyncio
async def test_viewport_debt_coverage_summary_counts_full_set(
    client: AsyncClient, db_session
):
    """coverage_summary reflects pre-truncation totals; untested_components capped at 50."""
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    membership = make_membership(project.id, user_id="test-user-123", role="owner")
    db_session.add(membership)
    await db_session.commit()

    # 60 components all untested — verifies the 50-cap on untested_components
    fake_rows_all = [
        {
            "component": f"admin-ui/src/components/Big{i}.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 60 - i,
            "has_viewport_test": False,
            "regression_case_count": 0,
            "source_url": None,
        }
        for i in range(60)
    ]
    top_rows = fake_rows_all[:15]
    untested_paths = [r["component"] for r in fake_rows_all]
    expected_summary = {
        "total_components": 60,
        "untested_count": 60,
        "untested_components": untested_paths[:50],
    }

    with patch(
        "app.viewport_debt._scan_viewport_debt_rows",
        return_value=(top_rows, [], expected_summary),
    ):
        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    cs = body["coverage_summary"]
    assert cs["total_components"] == 60
    assert cs["untested_count"] == 60
    # top_rows are the truncated 15; rows in response should be 15
    assert len(body["rows"]) == 15
    # untested_components capped at 50
    assert len(cs["untested_components"]) == 50


# ---------------------------------------------------------------------------
# Acknowledge endpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /api/admin/regression-dispatcher/escalation-outcomes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _evaluate_sr_catch_rate_targets tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_fires_after_2_windows(db_session):
    """Alert fires when >=2 consecutive windows are below the configured target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.60,
                    "current_human_count": 15,
                    "rate_history": [0.62, 0.60],
                    "trend": "decreasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title == "SR catch-rate below target: correctness",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "0.80" in alerts[0].message or "80%" in alerts[0].message
    assert "n=15" in alerts[0].message


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_suppressed_on_low_findings(db_session):
    """Alert is NOT emitted when current_human_count is below the minimum finding floor."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "current_human_count": 4,  # below _SR_CATCH_RATE_MIN_FINDINGS=10
                    "rate_history": [0.52, 0.50],
                    "trend": "decreasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "SR catch-rate below target: correctness",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_suppressed_on_1_window(db_session):
    """Alert is NOT emitted when only 1 window is below target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"security": 0.75}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {
                    "current_hit_rate": 0.60,
                    "rate_history": [0.60],
                    "trend": "stable",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title == "SR catch-rate below target: security",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_resolves_on_recovery(db_session):
    """Existing alert is dismissed when category recovers above target."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.sr_catch_rate_targets = {"regression_risk": 0.70}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "regression_risk": {
                    "current_hit_rate": 0.85,
                    "rate_history": [0.85],
                    "trend": "increasing",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed an existing undismissed alert.
    existing_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="SR catch-rate below target: regression_risk",
        message="old alert",
    )
    db_session.add(existing_alert)
    await db_session.commit()
    await db_session.refresh(existing_alert)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    await db_session.refresh(existing_alert)
    assert existing_alert.dismissed_at is not None


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_no_fire_when_unset(db_session):
    """No alert fires when sr_catch_rate_targets is empty."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.30,
                    "rate_history": [0.30, 0.35],
                    "trend": "stable",
                }
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    count = len(
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert count == 0


@pytest.mark.asyncio
async def test_evaluate_sr_catch_rate_targets_prompt_version_changed_suppression(
    db_session,
):
    """Alert is suppressed when prompt_version_changed is True."""
    from app.routers.admin_digest import _evaluate_sr_catch_rate_targets
    from app.models import Alert

    project = make_project()
    project.sr_catch_rate_targets = {"correctness": 0.80}
    project.design_doc = {
        "sr_catch_rate_trends": {
            "prompt_version_changed": True,
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "rate_history": [0.50, 0.48],
                    "trend": "decreasing",
                }
            },
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_sr_catch_rate_targets(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# _evaluate_lone_checker_alert tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_fires_on_2_lone_checker_categories(
    db_session,
):
    """Alert fires when >=2 categories have time_to_miss_detect_lone_checker=True."""
    from app.routers.admin_digest import _evaluate_lone_checker_alert
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "regression_risk": {"time_to_miss_detect_lone_checker": True},
                "correctness": {"time_to_miss_detect_lone_checker": False},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_lone_checker_alert(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "lone-checker" in alerts[0].title.lower()
    assert "security" in alerts[0].message
    assert "regression_risk" in alerts[0].message


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_no_fire_on_1_category(db_session):
    """Alert does not fire when only one category is in lone-checker state."""
    from app.routers.admin_digest import _evaluate_lone_checker_alert
    from app.models import Alert

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "correctness": {"time_to_miss_detect_lone_checker": False},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _evaluate_lone_checker_alert(db_session, project.id)

    count = len(
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert count == 0


@pytest.mark.asyncio
async def test_evaluate_lone_checker_alert_dedup(db_session):
    """Alert is not duplicated if an open alert with the same title already exists."""
    from app.routers.admin_digest import (
        _evaluate_lone_checker_alert,
        _LONE_CHECKER_ALERT_TITLE,
    )
    from app.models import Alert, AlertSeverity

    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {"time_to_miss_detect_lone_checker": True},
                "regression_risk": {"time_to_miss_detect_lone_checker": True},
            }
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    existing = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title=_LONE_CHECKER_ALERT_TITLE,
        message="existing",
    )
    db_session.add(existing)
    await db_session.commit()

    await _evaluate_lone_checker_alert(db_session, project.id)

    all_alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(all_alerts) == 1


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/version-lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_lifecycle_no_versions(client: AsyncClient, db_session):
    """Project with no release versions returns null current_version and empty history."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()
    assert data["current_version"] is None
    assert data["recent_versions"] == []


@pytest.mark.asyncio
async def test_version_lifecycle_with_open_version(client: AsyncClient, db_session):
    """Open version fields are surfaced including qualification data."""
    from app.models import ReleaseVersion

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        qualification_attempts=2,
        last_qualification={
            "status": "dry_run",
            "confidence": 0.92,
            "threshold": 0.90,
            "sub_scores": {"audit": 0.95, "test_erosion": 0.88, "smoketest": 0.90},
            "ci_resolution": {
                "state": "green",
                "resolved_sha": "abc1234567",
                "detail": "candidate commit check-runs: green",
            },
            "blocker": None,
            "finalized_at": "2026-01-01T00:00:00",
        },
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert cv["version_number"] == 3
    assert cv["phase"] == "stabilize"
    assert cv["qualification_attempts"] == 2
    assert cv["merged_task_count"] == 0

    qual = cv["last_qualification"]
    assert qual["status"] == "dry_run"
    assert qual["confidence"] == 0.92
    assert qual["threshold"] == 0.90
    assert qual["sub_scores"]["audit"] == 0.95
    assert qual["ci_resolution"]["state"] == "green"
    assert qual["ci_resolution"]["resolved_sha"] == "abc1234567"
    assert qual["blocker"] is None

    assert data["recent_versions"] == []


@pytest.mark.asyncio
async def test_version_lifecycle_phase_reasoning_surfaced(
    client: AsyncClient, db_session
):
    """phase_reasoning is included in the current_version response when set."""
    from app.models import ReleaseVersion

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="stabilize",
        status="open",
        qualification_attempts=0,
        phase_reasoning="Moved to stabilize: 8 tasks merged and build confidence above threshold.",
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert (
        cv["phase_reasoning"]
        == "Moved to stabilize: 8 tasks merged and build confidence above threshold."
    )


@pytest.mark.asyncio
async def test_version_lifecycle_phase_reasoning_null_when_unset(
    client: AsyncClient, db_session
):
    """phase_reasoning is null in the response when no reasoning has been recorded."""
    from app.models import ReleaseVersion

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=6,
        phase="build",
        status="open",
        qualification_attempts=0,
    )
    db_session.add(version)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    cv = data["current_version"]
    assert cv is not None
    assert cv["phase_reasoning"] is None


@pytest.mark.asyncio
async def test_version_lifecycle_history(client: AsyncClient, db_session):
    """Closed versions appear in recent_versions, open version in current_version."""
    from app.models import ReleaseVersion

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        qualification_attempts=1,
        last_qualification={"status": "promoted", "confidence": 0.95},
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        qualification_attempts=0,
    )
    db_session.add(closed)
    db_session.add(open_v)
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    assert data["current_version"]["version_number"] == 2
    assert len(data["recent_versions"]) == 1
    assert data["recent_versions"][0]["version_number"] == 1
    assert data["recent_versions"][0]["status"] == "promoted"
    assert data["recent_versions"][0]["last_qualification"]["confidence"] == 0.95


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_fields_present(
    client: AsyncClient, db_session
):
    """promotion_enabled and promotion_readiness are always present in the response."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["promotion_enabled"], bool)
    pr = data["promotion_readiness"]
    assert pr is not None
    assert "ready" in pr
    assert "reason" in pr
    assert "verdicts_evaluated" in pr


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_enabled_true(
    client: AsyncClient, db_session
):
    """When promotion_enabled is True in runtime_config, the endpoint reflects it."""
    import app.shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    original = _ss.runtime_config.get("promotion_enabled", False)
    try:
        _ss.runtime_config["promotion_enabled"] = True
        response = await client.get(
            f"/api/admin/projects/{project.id}/version-lifecycle"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["promotion_enabled"] is True
    finally:
        _ss.runtime_config["promotion_enabled"] = original


@pytest.mark.asyncio
async def test_version_lifecycle_promotion_readiness_not_ready(
    client: AsyncClient, db_session
):
    """With no dry-run verdicts the readiness summary reports not-ready."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
    assert response.status_code == 200
    data = response.json()

    pr = data["promotion_readiness"]
    assert pr["ready"] is False
    assert pr["verdicts_evaluated"] == 0


# ---------------------------------------------------------------------------
# Memo-pathway exit count in digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_memo_pathway_exits_zero_without_project(client: AsyncClient):
    """Without project_id, memo_pathway_exits is 0."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    assert response.json()["memo_pathway_exits"] == 0


@pytest.mark.asyncio
async def test_digest_memo_pathway_exits_counts_completed_memo_tasks(
    client: AsyncClient, db_session
):
    """Digest surfaces completed memo tasks count via memo_pathway_exits."""
    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Two completed memo tasks
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "memo"}],
            )
        )
    # One completed non-memo task — should not count
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
        )
    )
    # One in-progress memo task — should not count
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "memo"}],
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["memo_pathway_exits"] == 2


@pytest.mark.asyncio
async def test_digest_latest_cycle_includes_memo_stats(client: AsyncClient, db_session):
    """When a completed cycle has memo counts in cycle_metadata, they appear in latest_cycle."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create a completed strategic cycle with memo counts
    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase1_completed(db_session, cycle.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session, cycle.id, memos_available=4, memos_cited=1
    )
    await db_session.commit()

    # Wire the cycle into project.vision._strategic_cycle_history so the
    # digest's legacy-JSONB path can find it.
    from sqlalchemy.orm.attributes import flag_modified

    await db_session.refresh(cycle)
    vision = dict(project.vision or {})
    vision["_strategic_cycle_history"] = [
        {
            "cycle_id": cycle.id,
            "started_at": cycle.phase1_started_at.isoformat()
            if cycle.phase1_started_at
            else None,
            "phase2_completed_at": cycle.phase2_completed_at.isoformat()
            if cycle.phase2_completed_at
            else None,
            "outcome": cycle.outcome,
            "directions_created": 0,
            "directions_retired": 0,
            "vision_updated": False,
        }
    ]
    project.vision = vision
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    lc = data.get("latest_cycle")
    assert lc is not None
    assert lc["memos_available"] == 4
    assert lc["memos_cited"] == 1


@pytest.mark.asyncio
async def test_digest_memo_ever_cited_false_when_no_citations(
    client: AsyncClient, db_session
):
    """memo_ever_cited.ever_cited is False when no cycle has memos_cited > 0."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await _sc.create(db_session, project_id=project.id)
    await db_session.commit()
    await _sc.mark_phase2_completed(
        db_session, cycle.id, memos_available=3, memos_cited=0
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    mec = data.get("memo_ever_cited")
    assert mec is not None
    assert mec["ever_cited"] is False
    assert mec["first_cycle_id"] is None


@pytest.mark.asyncio
async def test_digest_memo_ever_cited_true_with_oldest_cycle_cited(
    client: AsyncClient, db_session
):
    """memo_ever_cited.ever_cited=True even when citation is outside the 20-cycle display window."""
    from app import strategic_cycle as _sc

    project = make_project()
    db_session.add(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    await db_session.refresh(project)

    # Create 22 cycles; only the first one has a citation (outside recent 20-cycle window)
    first_cycle_id = None
    for i in range(22):
        c = await _sc.create(db_session, project_id=project.id)
        await db_session.commit()
        cited = 1 if i == 0 else 0
        await _sc.mark_phase2_completed(
            db_session, c.id, memos_available=1, memos_cited=cited
        )
        await db_session.commit()
        if i == 0:
            first_cycle_id = c.id

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    mec = data.get("memo_ever_cited")
    assert mec is not None
    assert mec["ever_cited"] is True
    assert mec["first_cycle_id"] == first_cycle_id


# ---------------------------------------------------------------------------
# /api/admin/projects/{project_id}/qualification-verdicts tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_qualification_verdicts_empty(client: AsyncClient, db_session):
    """Project with no QualificationVerdict rows returns an empty list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["verdicts"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_qualification_verdicts_returns_rows_newest_first(
    client: AsyncClient, db_session
):
    """Verdict rows are returned newest-first with all expected fields present."""
    from app.models import ReleaseVersion, QualificationVerdict

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=2,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    older = now - timedelta(hours=1)

    v1 = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc1234" + "0" * 33,
        confidence=0.88,
        sub_scores={"audit": 0.90, "test_erosion": 0.85, "smoketest": 0.89},
        blocker=None,
        would_promote=False,
        promotion_enabled=False,
        terminal_status="dry_run",
        threshold=0.90,
        started_at=older - timedelta(minutes=5),
        finalized_at=older,
        created_at=older,
    )
    v2 = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="def5678" + "0" * 33,
        confidence=0.93,
        sub_scores={"audit": 0.95, "test_erosion": 0.91, "smoketest": 0.92},
        blocker=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.90,
        started_at=now - timedelta(minutes=3),
        finalized_at=now,
        created_at=now,
    )
    db_session.add(v1)
    db_session.add(v2)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 2
    verdicts = data["verdicts"]
    assert len(verdicts) == 2

    # Newest first — v2 was created at `now`, v1 at `older`
    newest = verdicts[0]
    assert newest["terminal_status"] == "promoted"
    assert newest["would_promote"] is True
    assert newest["promotion_enabled"] is True
    assert newest["confidence"] == 0.93
    assert newest["sub_scores"]["audit"] == 0.95
    assert newest["blocker"] is None
    assert newest["threshold"] == 0.90
    assert newest["candidate_sha"] == "def5678" + "0" * 33
    assert newest["started_at"] is not None
    assert newest["finalized_at"] is not None
    assert newest["created_at"] is not None
    assert "id" in newest
    assert "release_version_id" in newest

    older_verdict = verdicts[1]
    assert older_verdict["terminal_status"] == "dry_run"
    assert older_verdict["would_promote"] is False


@pytest.mark.asyncio
async def test_list_qualification_verdicts_limit_param(client: AsyncClient, db_session):
    """The limit query parameter is respected and caps the result set."""
    from app.models import ReleaseVersion, QualificationVerdict

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=3,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    for i in range(3):
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                confidence=0.80 + i * 0.05,
                terminal_status="dry_run",
                would_promote=False,
                promotion_enabled=False,
                created_at=now - timedelta(hours=i),
            )
        )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/qualification-verdicts?limit=2"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["verdicts"]) == 2


@pytest.mark.asyncio
async def test_list_qualification_verdicts_only_own_project(
    client: AsyncClient, db_session
):
    """Verdicts from another project are not included in the response."""
    from app.models import ReleaseVersion, QualificationVerdict

    project_a = make_project()
    project_b = make_project()
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)
    db_session.add(make_membership(project_a.id))
    db_session.add(make_membership(project_b.id))

    ver_a = ReleaseVersion(
        project_id=project_a.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=1,
    )
    ver_b = ReleaseVersion(
        project_id=project_b.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=1,
    )
    db_session.add(ver_a)
    db_session.add(ver_b)
    await db_session.commit()
    await db_session.refresh(ver_a)
    await db_session.refresh(ver_b)

    now = _utcnow()
    db_session.add(
        QualificationVerdict(
            project_id=project_a.id,
            release_version_id=ver_a.id,
            confidence=0.88,
            terminal_status="dry_run",
            would_promote=False,
            promotion_enabled=False,
            created_at=now,
        )
    )
    db_session.add(
        QualificationVerdict(
            project_id=project_b.id,
            release_version_id=ver_b.id,
            confidence=0.75,
            terminal_status="dry_run",
            would_promote=False,
            promotion_enabled=False,
            created_at=now,
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project_a.id}/qualification-verdicts"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["verdicts"][0]["confidence"] == 0.88


@pytest.mark.asyncio
async def test_list_qualification_verdicts_requires_auth(
    unauthenticated_client: AsyncClient, db_session
):
    """Unauthenticated requests are rejected."""
    import uuid

    fake_id = uuid.uuid4()
    response = await unauthenticated_client.get(
        f"/api/admin/projects/{fake_id}/qualification-verdicts"
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Unit tests for compute_failure_grouping (pure function, no DB required)
# ---------------------------------------------------------------------------


def _make_failed_task(**kwargs):
    """Minimal failed Task object for compute_failure_grouping unit tests."""
    return make_task(status=TaskStatus.FAILED, **kwargs)


def test_compute_failure_grouping_empty():
    """Empty failed-task list returns zeroed-out structure."""
    now = _utcnow()
    result = compute_failure_grouping(
        failed_tasks=[],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert result["failure_groups"] == []
    assert result["cost_by_failure_class"] == {}
    assert result["total_failed_cost_usd"] == 0.0
    assert result["provision_waste_cost_usd"] == 0.0
    assert result["failure_stage_counts"] == {}
    assert result["analysis_overflow_summary"]["total_count"] == 0
    assert result["provision_waste_by_direction"] == []


def test_compute_failure_grouping_rate_limit_class():
    """Task with a rate-limit error message is placed in the RATE_LIMIT failure group."""
    now = _utcnow()
    task = _make_failed_task(
        title="Rate Limited Task",
        latest_status_message="API Error: Rate limit reached",
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={str(task.id): "API Error: Rate limit reached"},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    classes = {g["failure_class"] for g in result["failure_groups"]}
    assert "RATE_LIMIT" in classes
    rate_group = next(
        g for g in result["failure_groups"] if g["failure_class"] == "RATE_LIMIT"
    )
    assert rate_group["count"] == 1
    assert rate_group["tasks"][0]["title"] == "Rate Limited Task"
    assert rate_group["label"] == "Rate Limit"


def test_compute_failure_grouping_provision_failure_waste():
    """PROVISION_FAILURE tasks contribute to provision_waste_cost_usd."""
    now = _utcnow()
    prov_at = now - timedelta(hours=1)
    stop_at = now - timedelta(minutes=30)
    task = _make_failed_task(
        title="Quota Exceeded Task",
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=False,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert result["provision_waste_cost_usd"] > 0.0
    assert "PROVISION_FAILURE" in result["cost_by_failure_class"]
    assert result["provision_waste_cost_usd"] == pytest.approx(
        result["cost_by_failure_class"]["PROVISION_FAILURE"]
    )


def test_compute_failure_grouping_sorted_by_count_descending():
    """failure_groups are sorted by count descending."""
    now = _utcnow()
    tasks = [
        _make_failed_task(
            title=f"Code Error {i}",
            latest_status_message="TypeError: something broke",
        )
        for i in range(3)
    ] + [
        _make_failed_task(
            title="Rate Task",
            latest_status_message="API Error: Rate limit reached",
        )
    ]
    result = compute_failure_grouping(
        failed_tasks=tasks,
        error_log_map={str(t.id): t.latest_status_message for t in tasks},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    counts = [g["count"] for g in result["failure_groups"]]
    assert counts == sorted(counts, reverse=True)
    assert result["failure_groups"][0]["count"] >= 3


def test_compute_failure_grouping_provision_waste_by_direction():
    """provision_waste_by_direction groups PROVISION_FAILURE cost by direction_id."""
    import uuid

    now = _utcnow()
    dir_id = str(uuid.uuid4())
    prov_at = now - timedelta(hours=1)
    stop_at = now - timedelta(minutes=30)
    task = _make_failed_task(
        title="Provision Fail",
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=False,
        context={"direction_id": dir_id},
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={dir_id: "My Direction"},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    breakdown = result["provision_waste_by_direction"]
    assert len(breakdown) == 1
    assert breakdown[0]["direction_id"] == dir_id
    assert breakdown[0]["direction_text"] == "My Direction"
    assert breakdown[0]["provision_cost_usd"] > 0.0
    assert breakdown[0]["provision_task_count"] == 1


def test_compute_failure_grouping_analysis_overflow_summary():
    """Tasks with scope_gate_triggered='analysis_overflow' appear in analysis_overflow_summary."""
    now = _utcnow()
    prov_at = now - timedelta(minutes=20)
    stop_at = now - timedelta(minutes=10)
    task = _make_failed_task(
        title="Overflow Task",
        context={"scope_gate_triggered": "analysis_overflow", "scope": "medium"},
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=True,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    summary = result["analysis_overflow_summary"]
    assert summary["total_count"] == 1
    assert summary["by_scope"].get("medium") == 1
    assert summary["cost_usd"] > 0.0


def test_compute_failure_grouping_task_entry_fields():
    """Each task entry in failure_groups contains the expected keys."""
    now = _utcnow()
    task = _make_failed_task(
        title="Code Error Task",
        latest_status_message="TypeError: cannot read property",
        attempt_count=2,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={str(task.id): "TypeError: cannot read property"},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert len(result["failure_groups"]) == 1
    entry = result["failure_groups"][0]["tasks"][0]
    assert entry["task_id"] == str(task.id)
    assert entry["title"] == "Code Error Task"
    assert entry["attempt_count"] == 2
    assert entry["error_snippet"] == "TypeError: cannot read property"
    assert "taxonomy" in entry
    assert "timeout_subtype" in entry
    assert entry["timeout_subtype"] is None  # not AGENT_TIMEOUT


def test_failure_grouping_labels_cover_all_canonical_failure_classes():
    """Digest failure-group labels stay aligned with the canonical taxonomy."""
    assert set(ALL_FAILURE_CLASSES) <= set(_FAILURE_CLASS_LABELS)
    assert all(_FAILURE_CLASS_LABELS[cls] != cls for cls in ALL_FAILURE_CLASSES)


# ---------------------------------------------------------------------------
# Unit tests for compute_recurring_error_clusters (pure function, no DB required)
# ---------------------------------------------------------------------------


def test_compute_recurring_error_clusters_empty():
    """Empty failed-task list returns an empty cluster list."""
    assert compute_recurring_error_clusters(failed_tasks=[], error_log_map={}) == []


def test_compute_recurring_error_clusters_excludes_singletons():
    """Clusters with count < 2 are excluded."""
    tasks = [
        _make_failed_task(latest_status_message="TypeError: foo is undefined"),
        _make_failed_task(latest_status_message="Rate limit exceeded"),
        _make_failed_task(latest_status_message="SyntaxError: unexpected token"),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert result == []


def test_compute_recurring_error_clusters_groups_by_fingerprint():
    """Failures sharing a normalized fingerprint group into one cluster, aggregating directions/task_types."""
    tasks = [
        _make_failed_task(
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-a"},
            latest_status_message="Merge conflict on PR #1234",
        ),
        _make_failed_task(
            payloads=[{"type": "fix"}],
            context={"direction_id": "dir-a"},
            latest_status_message="Merge conflict on PR #5678",
        ),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 1
    cluster = result[0]
    assert cluster["count"] == 2
    assert len(cluster["fingerprint"]) == 12
    assert "Merge conflict" in cluster["error_snippet"]
    assert cluster["direction_ids"] == ["dir-a"]
    assert sorted(cluster["task_types"]) == ["create", "fix"]
    assert cluster["first_task_id"] == str(tasks[0].id)


def test_compute_recurring_error_clusters_error_log_map_overrides_status_message():
    """When error_log_map has an entry for the task id, it takes precedence over latest_status_message."""
    task_a = _make_failed_task(latest_status_message="stale")
    task_b = _make_failed_task(latest_status_message="stale")
    result = compute_recurring_error_clusters(
        failed_tasks=[task_a, task_b],
        error_log_map={
            str(task_a.id): "Merge conflict on PR #1",
            str(task_b.id): "Merge conflict on PR #2",
        },
    )
    assert len(result) == 1
    assert "Merge conflict" in result[0]["error_snippet"]


def test_compute_recurring_error_clusters_sorted_by_count_descending_and_capped():
    """Returned clusters are sorted by count descending and capped at 5."""
    tasks = []
    # Build 6 distinct clusters with counts 7,6,5,4,3,2 — all should make it past the >=2 gate,
    # but only the top 5 should be returned (sorted desc).
    for cluster_idx, count in enumerate([7, 6, 5, 4, 3, 2]):
        for _ in range(count):
            tasks.append(
                _make_failed_task(
                    latest_status_message=f"Cluster {cluster_idx} unique-marker error",
                )
            )
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 5
    counts = [c["count"] for c in result]
    assert counts == sorted(counts, reverse=True)
    assert counts == [7, 6, 5, 4, 3]


def test_compute_recurring_error_clusters_skips_tasks_with_no_message():
    """Tasks with neither error_log_map entry nor latest_status_message are skipped."""
    tasks = [
        _make_failed_task(latest_status_message=""),
        _make_failed_task(latest_status_message=""),
        _make_failed_task(latest_status_message="Merge conflict on PR #1"),
        _make_failed_task(latest_status_message="Merge conflict on PR #2"),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 1
    assert result[0]["count"] == 2


# ---------------------------------------------------------------------------
# Unit tests for compute_digest_trend_history (pure function, no DB required)
# ---------------------------------------------------------------------------


def test_compute_digest_trend_history_length():
    """Returns exactly 7 entries regardless of task list."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    assert len(result) == 7


def test_compute_digest_trend_history_entry_keys():
    """Each entry contains the expected keys."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    for entry in result:
        assert "period_end" in entry
        assert "success_rate" in entry
        assert "completed" in entry
        assert "failed" in entry
        assert "failed_by_class" in entry
        assert "failed_by_taxonomy" in entry
        assert "period_cost_usd" in entry
        assert "first_attempt_success_rate" in entry
        assert "first_attempt_sample_size" in entry


def test_compute_digest_trend_history_empty_tasks():
    """With no tasks all counts are 0 and rates are None."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    for entry in result:
        assert entry["completed"] == 0
        assert entry["failed"] == 0
        assert entry["success_rate"] is None
        assert entry["first_attempt_success_rate"] is None
        assert entry["first_attempt_sample_size"] == 0


def test_compute_digest_trend_history_completed_task_in_last_period():
    """A COMPLETED task in the most recent period appears in the last entry."""
    now = _utcnow()
    task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[task], hours=24, now=now)
    last = result[-1]
    assert last["completed"] == 1
    assert last["failed"] == 0
    assert last["success_rate"] == 1.0


def test_compute_digest_trend_history_failed_task_classified():
    """A FAILED task is reflected in failed_by_class for the correct period."""
    now = _utcnow()
    task = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        latest_status_message="API Error: Rate limit reached",
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[task], hours=24, now=now)
    last = result[-1]
    assert last["failed"] == 1
    assert last["success_rate"] == 0.0
    assert isinstance(last["failed_by_class"], dict)
    assert isinstance(last["failed_by_taxonomy"], dict)


def test_compute_digest_trend_history_oldest_first():
    """Entries are in ascending chronological order (period_end increases)."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    period_ends = [entry["period_end"] for entry in result]
    assert period_ends == sorted(period_ends)


def test_compute_digest_trend_history_first_attempt_rate():
    """first_attempt_success_rate reflects _first_attempt_succeeded context flag."""
    now = _utcnow()
    t_pass = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"_first_attempt_succeeded": True},
        created_at=now - timedelta(hours=1),
    )
    t_fail = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_first_attempt_succeeded": False},
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[t_pass, t_fail], hours=24, now=now)
    last = result[-1]
    assert last["first_attempt_sample_size"] == 2
    assert last["first_attempt_success_rate"] == 0.5


# ---------------------------------------------------------------------------
# Unit tests for compute_digest_cost_economics (pure function, no DB required)
# ---------------------------------------------------------------------------


def test_compute_digest_cost_economics_empty():
    """Empty tasks and directions return a zeroed-out cost structure."""
    now = _utcnow()
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=[],
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=12.5,
    )
    assert result["total_estimated_cost_usd"] == 0.0
    assert result["total_spot_cost_usd"] == 0.0
    assert result["total_on_demand_cost_usd"] == 0.0
    assert result["provisioning_overhead_usd"] == 0.0
    assert result["provisioning_overhead_pct"] is None
    assert result["daily_cost_usd"] == 0.0
    assert result["daily_cost_ceiling_usd"] == 12.5
    assert result["overall_product_ratio"] is None
    assert result["median_cost_per_merged_pr"] is None
    assert result["cost_per_merge_by_type"] == {}
    assert result["median_cpm_by_type"] is None
    assert result["pipeline_acceptance_rate"] is None
    assert result["pipeline_conversion_rate"] is None
    assert result["pipeline_autonomous_prs_merged"] == 0
    assert result["pipeline_assisted_prs_merged"] == 0
    assert result["pipeline_operator_action_count"] == 0
    assert result["overall_type_distribution"] == {}
    assert result["pipeline_filter_funnel"] == {}


def test_compute_digest_cost_economics_spot_on_demand_split():
    """Spot and on-demand task costs are split and totaled with known VM timing."""
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    spot_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),  # 1800s
    )
    od_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix"}],
        vm_spot=False,
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),  # 3600s
    )
    result = compute_digest_cost_economics(
        all_tasks=[spot_task, od_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    expected_spot = round(SPOT_RATE * 1800, 2)
    expected_od = round(ON_DEMAND_RATE * 3600, 2)
    assert result["total_spot_cost_usd"] == expected_spot
    assert result["total_on_demand_cost_usd"] == expected_od
    # Both tasks are undirected (no direction_id) and inside the window.
    assert result["total_estimated_cost_usd"] == round(
        SPOT_RATE * 1800 + ON_DEMAND_RATE * 3600, 2
    )


def test_compute_digest_cost_economics_provisioning_overhead():
    """Time between provision and agent claim counts as provisioning overhead."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    # Provisioned 1h before now, claimed (started) 50m before → 600s overhead,
    # stopped 30m before → 1800s total compute.
    task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=False,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_started_at=now - timedelta(minutes=50),
        vm_stopped_at=now - timedelta(minutes=30),
    )
    result = compute_digest_cost_economics(
        all_tasks=[task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # overhead_raw = ON_DEMAND_RATE * 600 = 0.0111…; rounds to 0.01.
    assert result["provisioning_overhead_usd"] == 0.01
    # pct = round(overhead_raw / rounded_total_compute, 3) = round(0.01117/0.03, 3).
    assert result["provisioning_overhead_pct"] == 0.372


def test_compute_digest_cost_economics_threshold_flag_in_place():
    """cost_per_merge_exceeds_threshold is stamped onto direction dicts in place."""
    now = _utcnow()
    dirs = [
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 1.0},
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 2.0},
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 100.0},
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # median([1, 2, 100]) = 2.0; threshold = 2.0 * 3 = 6.0.
    assert result["median_cost_per_merged_pr"] == 2.0
    # In-place mutation on the same dict objects.
    assert dirs[0]["cost_per_merge_exceeds_threshold"] is False
    assert dirs[1]["cost_per_merge_exceeds_threshold"] is False
    assert dirs[2]["cost_per_merge_exceeds_threshold"] is True


def test_compute_digest_cost_economics_cost_per_merge_by_type():
    """Per-task-type cost-per-merge counts merged tasks by payload type."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    merged = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )
    unmerged = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={},
    )
    result = compute_digest_cost_economics(
        all_tasks=[merged, unmerged],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    by_type = result["cost_per_merge_by_type"]
    assert "create" in by_type
    assert by_type["create"]["merged_count"] == 1
    assert by_type["create"]["cost_per_merge"] is not None
    assert by_type["create"]["total_cost_usd"] > 0.0


def test_compute_digest_cost_economics_pipeline_aggregates():
    """Pipeline-level rates and counts aggregate across all directions."""
    now = _utcnow()
    dirs = [
        {
            "estimated_cost_usd": 0.0,
            "prs_merged": 3,
            "prs_rejected": 1,
            "tasks_created": 10,
            "autonomous_prs_merged": 2,
            "assisted_prs_merged": 1,
            "operator_action_count": 4,
            "type_distribution": {"create": 5},
            "filter_funnel": {"a": 2},
        },
        {
            "estimated_cost_usd": 0.0,
            "prs_merged": 1,
            "prs_rejected": 1,
            "tasks_created": 5,
            "autonomous_prs_merged": 1,
            "assisted_prs_merged": 0,
            "operator_action_count": 1,
            "type_distribution": {"create": 2, "fix": 1},
            "filter_funnel": {"a": 1, "b": 3},
        },
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # merged=4, rejected=2 → 4/6 = 0.667; created=15 → 4/15 = 0.267.
    assert result["pipeline_acceptance_rate"] == 0.667
    assert result["pipeline_conversion_rate"] == 0.267
    assert result["pipeline_autonomous_prs_merged"] == 3
    assert result["pipeline_assisted_prs_merged"] == 1
    assert result["pipeline_operator_action_count"] == 5
    assert result["overall_type_distribution"] == {"create": 7, "fix": 1}
    assert result["pipeline_filter_funnel"] == {"a": 3, "b": 3}


def test_compute_scope_mismatch_summary_no_calibrated_tasks():
    """Returns None when no terminal task carries scope_calibrated_from."""
    completed = [make_task(status=TaskStatus.COMPLETED, context={})]
    failed = [make_task(status=TaskStatus.FAILED, context={"direction_id": "d-1"})]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is None


def test_compute_scope_mismatch_summary_empty_inputs():
    """Empty task lists yield None."""
    result = compute_scope_mismatch_summary(
        completed_tasks=[], failed_tasks=[], dir_id_to_text={}
    )
    assert result is None


def test_compute_scope_mismatch_summary_success_rate_and_total():
    """total_calibrated counts all calibrated tasks; success rate uses COMPLETED."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED, context={"scope_calibrated_from": "medium"}
        ),
        make_task(
            status=TaskStatus.COMPLETED, context={"scope_calibrated_from": "small"}
        ),
    ]
    failed = [
        make_task(status=TaskStatus.FAILED, context={"scope_calibrated_from": "large"}),
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is not None
    assert result["total_calibrated"] == 3
    assert result["calibrated_success_rate"] == round(2 / 3, 3)


def test_compute_scope_mismatch_summary_by_original_scope_breakdown():
    """by_original_scope buckets by the prefix before ':' and tallies outcomes."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "medium:reason-text"},
        ),
    ]
    failed = [
        make_task(
            status=TaskStatus.FAILED,
            context={"scope_calibrated_from": "medium:other-reason"},
        ),
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is not None
    assert result["by_original_scope"] == {
        "medium": {"total": 2, "completed": 1, "failed": 1}
    }


def test_compute_scope_mismatch_summary_blank_original_scope_is_unknown():
    """An empty scope_calibrated_from value is not calibrated; a bare ':' → 'unknown'."""
    # Empty string is falsy → excluded from calibrated set entirely.
    excluded = make_task(
        status=TaskStatus.COMPLETED, context={"scope_calibrated_from": ""}
    )
    # A value with no scope prefix before ':' falls back to "unknown".
    bare = make_task(
        status=TaskStatus.COMPLETED, context={"scope_calibrated_from": ":reason"}
    )
    result = compute_scope_mismatch_summary(
        completed_tasks=[excluded, bare], failed_tasks=[], dir_id_to_text={}
    )
    assert result is not None
    assert result["total_calibrated"] == 1
    assert "unknown" in result["by_original_scope"]


def test_compute_scope_mismatch_summary_top_directions_ranking():
    """top_directions ranks by calibration_rate, attaches text, and caps at 3."""
    completed = []
    failed = []
    # d-hi: 2/2 calibrated → rate 1.0
    for _ in range(2):
        completed.append(
            make_task(
                status=TaskStatus.COMPLETED,
                context={"scope_calibrated_from": "small", "direction_id": "d-hi"},
            )
        )
    # d-lo: 1/3 calibrated → rate ~0.333
    completed.append(
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "small", "direction_id": "d-lo"},
        )
    )
    for _ in range(2):
        failed.append(
            make_task(status=TaskStatus.FAILED, context={"direction_id": "d-lo"})
        )
    result = compute_scope_mismatch_summary(
        completed_tasks=completed,
        failed_tasks=failed,
        dir_id_to_text={"d-hi": "High dir", "d-lo": "Low dir"},
    )
    assert result is not None
    top = result["top_directions"]
    assert [d["direction_id"] for d in top] == ["d-hi", "d-lo"]
    assert top[0]["calibration_rate"] == 1.0
    assert top[0]["total_tasks"] == 2
    assert top[0]["direction_text"] == "High dir"
    assert top[1]["calibration_rate"] == round(1 / 3, 3)


def test_compute_scope_mismatch_summary_top_directions_capped_at_three():
    """Only the top 3 directions are returned even when more are calibrated."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "small", "direction_id": f"d-{i}"},
        )
        for i in range(5)
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=[], dir_id_to_text={}
    )
    assert result is not None
    assert len(result["top_directions"]) == 3


# ---------------------------------------------------------------------------
# Unit tests for compute_sr_prediction_accuracy (pure function, no DB required)
# ---------------------------------------------------------------------------


def _make_sr_task(check_results=None, rejection_categories=None, **kwargs):
    """Minimal terminal Task carrying _self_review_outcome.check_results and
    _first_attempt_rejection_categories for SR-prediction unit tests."""
    ctx = {
        "_self_review_outcome": {"check_results": check_results or {}},
        "_first_attempt_rejection_categories": rejection_categories or {},
    }
    ctx.update(kwargs.pop("context", {}))
    return make_task(status=TaskStatus.COMPLETED, context=ctx, **kwargs)


def test_compute_sr_prediction_accuracy_under_sample_size_returns_none():
    """Sample size <5 short-circuits to None regardless of content."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": False}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(4)
    ]
    assert compute_sr_prediction_accuracy(tasks) is None


def test_compute_sr_prediction_accuracy_perfect_match():
    """Every SR-failed category matches human rejection → hit rate 1.0, miss 0.0."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": False}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    assert result["prediction_hit_rate"] == 1.0
    assert result["miss_rate"] == 0.0
    assert result["false_alarm_rate"] == 0.0
    assert result["sample_size"] == 5
    # SR flagged AND human rejected => "self_flagged shipped unfixed"
    assert result["self_flagged_shipped_unfixed_task_count"] == 5
    assert result["self_flagged_shipped_unfixed_task_rate"] == 1.0


def test_compute_sr_prediction_accuracy_all_miss():
    """SR passed everything but humans rejected → miss_rate 1.0, hit_rate 0.0."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": True}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    assert result["prediction_hit_rate"] == 0.0
    assert result["miss_rate"] == 1.0
    # No SR failures recorded so false_alarm_rate is None (divide-by-zero guard).
    assert result["false_alarm_rate"] is None
    assert result["self_flagged_shipped_unfixed_task_count"] == 0
    assert result["self_flagged_shipped_unfixed_task_rate"] == 0.0


def test_compute_sr_prediction_accuracy_reviewer_origin_excluded():
    """Reviewer-stamped check_results entries do not count as author SR failures."""
    tasks = [
        _make_sr_task(
            check_results={
                "correctness": {
                    "passed": False,
                    "origin": "reviewer_recovery_after_incomplete_sr",
                }
            },
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    # The reviewer-origin failure should NOT count as an author catch.
    assert result["prediction_hit_rate"] == 0.0
    assert result["miss_rate"] == 1.0


# ---------------------------------------------------------------------------
# Unit tests for compute_review_quality_metrics
# ---------------------------------------------------------------------------


def test_compute_review_quality_metrics_empty_returns_nones():
    """Empty input → every metric is None / empty-ish per sample-size guards."""
    result = compute_review_quality_metrics([])
    assert result["self_review_effectiveness"] is None
    assert result["self_review_prediction_accuracy"] is None
    assert result["review_accuracy"] is None
    # _compute_fresh_reviewer_effectiveness always returns a dict; we just check shape.
    assert isinstance(result["fresh_reviewer_effectiveness"], dict)


def test_compute_review_quality_metrics_self_review_effectiveness_shape():
    """≥3 tasks with _self_review_outcome populates self_review_effectiveness."""
    with_sr_clean = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"fixes_applied": False},
                "_first_attempt_succeeded": True,
            },
        )
        for _ in range(3)
    ]
    result = compute_review_quality_metrics(with_sr_clean)
    eff = result["self_review_effectiveness"]
    assert eff is not None
    assert eff["sample_size"] == 3
    assert eff["sr_clean_count"] == 3
    assert eff["sr_fixed_count"] == 0
    assert eff["with_self_review_approval_rate"] == 1.0
    assert eff["clean_verdict_approval_rate"] == 1.0


def test_compute_review_quality_metrics_review_accuracy_shape():
    """≥5 review tasks with a verdict populates review_accuracy."""
    review_tasks = []
    for _ in range(5):
        review_tasks.append(
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review"}],
                context={
                    "blueprint_review_verdict": "APPROVE",
                    "user_caught_issues": False,
                    "user_overrode_review": False,
                    "review_dismissed": False,
                },
            )
        )
    result = compute_review_quality_metrics(review_tasks)
    acc = result["review_accuracy"]
    assert acc is not None
    assert acc["total_reviews"] == 5
    assert acc["total_approvals"] == 5
    assert acc["overrides"] == 0
    assert acc["precision"] == 1.0
    assert acc["recall"] == 1.0


def test_parallel_review_effectiveness_buckets_correctly():
    """Tasks with review_was_parallelized=True/False bucket into parallel/serial."""

    def _make_review(parallelized: bool, verdict: str = "APPROVE"):
        return make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": verdict,
                "review_was_parallelized": parallelized,
            },
        )

    parallel_tasks = [_make_review(True) for _ in range(3)]
    serial_tasks = [_make_review(False) for _ in range(3)]
    result = compute_review_quality_metrics(parallel_tasks + serial_tasks)
    pre = result["parallel_review_effectiveness"]
    assert pre is not None
    assert pre["sample_sizes"]["parallel"] == 3
    assert pre["sample_sizes"]["serial"] == 3
    assert pre["parallel"]["count"] == 3
    assert pre["serial"]["count"] == 3
    assert pre["parallel"]["approval_rate"] == 1.0
    assert pre["serial"]["approval_rate"] == 1.0


def test_parallel_review_effectiveness_none_when_insufficient_sample():
    """Returns None when either bucket has fewer than 3 tasks."""

    def _make_review(parallelized: bool):
        return make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "APPROVE",
                "review_was_parallelized": parallelized,
            },
        )

    # Only 2 parallel tasks — below threshold
    tasks = [_make_review(True) for _ in range(2)] + [
        _make_review(False) for _ in range(5)
    ]
    result = compute_review_quality_metrics(tasks)
    assert result["parallel_review_effectiveness"] is None


# ---------------------------------------------------------------------------
# /api/admin/projects/{id}/warm-reuse-funnel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_reuse_funnel_empty(client: AsyncClient, db_session):
    """Endpoint returns all-zero counters when no funnel events have been recorded."""
    import app.warm_backlog_reuse as wbr

    # Reset counters so prior test runs don't bleed over.
    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    response = await client.get(f"/api/admin/projects/{project.id}/warm-reuse-funnel")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_vms_with_eligible_backlog"] == 0
    assert data["teardown_defers_scheduled"] == 0
    assert data["next_role_polls_during_defer"] == 0
    assert data["warm_claims_succeeded"] == 0
    assert data["claim_skipped_total"] == 0
    assert data["claim_skipped_by_reason"] == {}
    assert data["window_hours"] == 24


@pytest.mark.asyncio
async def test_warm_reuse_funnel_counts_after_records(client: AsyncClient, db_session):
    """Counter functions increment correctly and are reflected in the endpoint."""
    import app.warm_backlog_reuse as wbr

    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    wbr.record_vm_with_eligible_backlog()
    wbr.record_vm_with_eligible_backlog()
    wbr.record_teardown_defer()
    wbr.record_next_role_poll_during_defer()
    wbr.record_warm_claim_succeeded()
    wbr.record_claim_skipped("no_eligible")
    wbr.record_claim_skipped("lost_claim_race")
    wbr.record_claim_skipped("no_eligible")

    response = await client.get(f"/api/admin/projects/{project.id}/warm-reuse-funnel")
    assert response.status_code == 200
    data = response.json()
    assert data["completed_vms_with_eligible_backlog"] == 2
    assert data["teardown_defers_scheduled"] == 1
    assert data["next_role_polls_during_defer"] == 1
    assert data["warm_claims_succeeded"] == 1
    assert data["claim_skipped_total"] == 3
    assert data["claim_skipped_by_reason"]["no_eligible"] == 2
    assert data["claim_skipped_by_reason"]["lost_claim_race"] == 1


@pytest.mark.asyncio
async def test_warm_reuse_funnel_requires_auth(unauthenticated_client: AsyncClient):
    """Unauthenticated requests are rejected."""
    import uuid

    fake_id = uuid.uuid4()
    response = await unauthenticated_client.get(
        f"/api/admin/projects/{fake_id}/warm-reuse-funnel"
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# compute_retry_analytics unit tests
# ---------------------------------------------------------------------------


class TestComputeRetryAnalytics:
    """Unit tests for the extracted compute_retry_analytics helper."""

    def _make_task(self, **kwargs):
        """Minimal Task-like object for unit tests (no DB needed)."""
        t = make_task(**kwargs)
        return t

    def test_no_retry_history(self):
        """Tasks with _attempt_number <= 1 (or absent) produce zero/None counts."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        failed = [
            make_task(status=TaskStatus.FAILED, context={}),
            make_task(status=TaskStatus.FAILED, context={"_attempt_number": 1}),
        ]
        completed = [
            make_task(status=TaskStatus.COMPLETED, context={}),
        ]
        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        assert result["retry_task_count"] == 0
        assert result["retry_success_count"] == 0
        assert result["retry_overhead_cost_usd"] == 0.0
        assert result["retry_recovery_cost_usd"] == 0.0
        assert result["retry_waste_by_direction"] == []
        assert result["avg_retries_before_failure"] is None
        assert result["retry_success_rate_by_class"] == {}
        assert result["retry_wasted_rate"] is None

    def test_retried_and_succeeded(self):
        """Completed tasks with _attempt_number > 1 count toward retry_success_count
        and retry_recovery_cost_usd; retry_success_rate_by_class is populated."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        completed = [
            make_task(
                status=TaskStatus.COMPLETED,
                context={
                    "_attempt_number": 2,
                    "_previous_failure_class": "CODE_ERROR",
                    "_prior_attempts_cost": 1.5,
                },
            ),
            make_task(
                status=TaskStatus.COMPLETED,
                context={
                    "_attempt_number": 3,
                    "_previous_failure_class": "CODE_ERROR",
                    "_prior_attempts_cost": 0.75,
                },
            ),
        ]
        result = compute_retry_analytics(
            failed_tasks=[],
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        assert result["retry_success_count"] == 2
        assert result["retry_recovery_cost_usd"] == round(1.5 + 0.75, 4)
        assert "CODE_ERROR" in result["retry_success_rate_by_class"]
        cls_entry = result["retry_success_rate_by_class"]["CODE_ERROR"]
        assert cls_entry["merged"] == 2
        assert cls_entry["failed"] == 0
        assert cls_entry["success_rate"] == 1.0
        # No failed retries → retry_wasted_rate denominator is 0+2=2, waste=0
        assert result["retry_wasted_rate"] == 0.0

    def test_retried_and_failed(self):
        """Failed tasks with _attempt_number > 1 contribute to retry_overhead_cost_usd
        and retry_waste_by_direction with top-5 truncation."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        # 6 directions — only top 5 by cost should appear
        failed = []
        dir_id_to_text = {}
        for i in range(6):
            did = f"dir-{i}"
            dir_id_to_text[did] = f"Direction {i}"
            duration_hours = i + 1  # dir-0=1h, dir-5=6h so dir-5 is most expensive
            t = make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2, "direction_id": did},
                vm_provisioned_at=now - timedelta(hours=duration_hours + 1),
                vm_stopped_at=now - timedelta(minutes=1),
                vm_spot=False,
            )
            failed.append(t)

        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=[],
            dir_id_to_text=dir_id_to_text,
            window_start=window_start,
        )
        assert result["retry_task_count"] == 6
        # Top-5 truncation: only 5 directions returned, sorted descending by cost
        assert len(result["retry_waste_by_direction"]) == 5
        costs = [e["retry_cost_usd"] for e in result["retry_waste_by_direction"]]
        assert costs == sorted(costs, reverse=True)
        # Overhead cost is positive
        assert result["retry_overhead_cost_usd"] > 0

    def test_retry_wasted_rate_denominator(self):
        """retry_wasted_rate = failed_retries / (failed_retries + succeeded_retries)."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        failed = [
            make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2},
            ),
        ]
        completed = [
            make_task(
                status=TaskStatus.COMPLETED,
                context={"_attempt_number": 2, "_previous_failure_class": "CODE_ERROR"},
            ),
            make_task(
                status=TaskStatus.COMPLETED,
                context={"_attempt_number": 2, "_previous_failure_class": "CODE_ERROR"},
            ),
        ]
        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        # 1 failed retry, 2 succeeded retries → wasted_rate = 1/3
        assert result["retry_task_count"] == 1
        assert result["retry_success_count"] == 2
        assert result["retry_wasted_rate"] == round(1 / 3, 4)


# ---------------------------------------------------------------------------
# compute_release_qualifier_infra_metrics tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_no_versions(db_session):
    """Empty DB returns zero-valued counters."""
    now = _utcnow()
    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=None,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 0
    assert result["release_infra_failure_total"] == 0
    assert result["audit_report_missing_count"] == 0
    assert result["audit_report_parseable_but_unscoreable_count"] == 0
    assert result["smoketest_report_missing_count"] == 0
    assert result["release_infra_failure_streak_max"] == 0


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_counts_by_error_category(db_session):
    """Rows with distinct infra_failure error codes increment the correct buckets."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    finalized_at = (now - timedelta(hours=1)).isoformat()

    error_cases = [
        "audit_report_missing",
        "audit_report_parseable_but_unscoreable",
        "smoketest_report_missing",
        "audit_task_failed_other",  # unknown bucket — counts in total only
    ]
    for i, error in enumerate(error_cases):
        v = ReleaseVersion(
            project_id=project.id,
            version_number=i + 1,
            phase="stabilize",
            last_qualification={
                "status": "infra_failure",
                "error": error,
                "consecutive_infra_errors": 1,
                "finalized_at": finalized_at,
            },
        )
        db_session.add(v)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=window_start,
    )
    assert result["qualifier_runs_total"] == 4
    assert result["release_infra_failure_total"] == 4
    assert result["audit_report_missing_count"] == 1
    assert result["audit_report_parseable_but_unscoreable_count"] == 1
    assert result["smoketest_report_missing_count"] == 1


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_streak_max(db_session):
    """release_infra_failure_streak_max reflects the highest consecutive_infra_errors."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    finalized_at = (now - timedelta(hours=1)).isoformat()

    for i, streak in enumerate([2, 4, 1]):
        v = ReleaseVersion(
            project_id=project.id,
            version_number=i + 1,
            phase="stabilize",
            last_qualification={
                "status": "infra_failure",
                "error": "audit_report_missing",
                "consecutive_infra_errors": streak,
                "finalized_at": finalized_at,
            },
        )
        db_session.add(v)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["release_infra_failure_streak_max"] == 4


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_excludes_outside_window(db_session):
    """Rows finalized before window_start are not counted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    old_finalized_at = (now - timedelta(hours=48)).isoformat()
    recent_finalized_at = (now - timedelta(hours=1)).isoformat()

    old_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "audit_report_missing",
            "consecutive_infra_errors": 3,
            "finalized_at": old_finalized_at,
        },
    )
    recent_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "smoketest_report_missing",
            "consecutive_infra_errors": 1,
            "finalized_at": recent_finalized_at,
        },
    )
    db_session.add(old_version)
    db_session.add(recent_version)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 1
    assert result["release_infra_failure_total"] == 1
    assert result["smoketest_report_missing_count"] == 1
    assert result["audit_report_missing_count"] == 0


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_qualifier_runs_total(db_session):
    """qualifier_runs_total counts all finalized quals, not just infra_failure ones."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    finalized_at = (now - timedelta(hours=1)).isoformat()

    # One infra_failure and one successful promotion
    infra_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "audit_report_missing",
            "consecutive_infra_errors": 1,
            "finalized_at": finalized_at,
        },
    )
    promoted_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="promote",
        last_qualification={
            "status": "promoted",
            "finalized_at": finalized_at,
        },
    )
    # Version with no qualification at all — should not count
    unqual_version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        last_qualification=None,
    )
    db_session.add(infra_version)
    db_session.add(promoted_version)
    db_session.add(unqual_version)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 2
    assert result["release_infra_failure_total"] == 1


class TestDigestMetricsModuleSeams:
    """Seam guards for the digest_metrics decomposition.

    ``compute_remediation_funnel`` and ``compute_direction_progress`` were the
    two largest analytics in ``app.digest_metrics``. They now live in cohesive
    sibling modules (``app.digest_remediation`` and
    ``app.digest_direction_progress`` respectively) and are re-exported from
    ``app.digest_metrics`` so existing import paths (and ``mock.patch``
    targets) keep working. These tests fail loudly if either the canonical
    home moves or the re-export is dropped.
    """

    def test_remediation_funnel_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_remediation as digest_remediation

        # Canonical home defines it as a real function.
        assert hasattr(digest_remediation, "compute_remediation_funnel")
        # digest_metrics re-export resolves to the same object.
        assert (
            digest_metrics.compute_remediation_funnel
            is digest_remediation.compute_remediation_funnel
        )

    def test_remediation_and_patterns_still_in_remediation_module(self):
        import app.digest_metrics as digest_metrics
        import app.digest_remediation as digest_remediation

        assert (
            digest_metrics.compute_remediation_and_patterns
            is digest_remediation.compute_remediation_and_patterns
        )

    def test_direction_progress_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_direction_progress as digest_direction_progress

        assert hasattr(digest_direction_progress, "compute_direction_progress")
        assert (
            digest_metrics.compute_direction_progress
            is digest_direction_progress.compute_direction_progress
        )

    def test_extracted_modules_do_not_import_digest_metrics_at_load_time(self):
        """The new sibling modules must not import ``digest_metrics`` at module
        load time — that would create a circular import (digest_metrics imports
        them). Cross-module needs are satisfied via function-local imports."""
        import ast
        import os

        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        for module_file in ("digest_remediation.py", "digest_direction_progress.py"):
            path = os.path.normpath(os.path.join(app_dir, module_file))
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in tree.body:  # top-level statements only
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app.digest_metrics", (
                        f"{module_file} imports app.digest_metrics at module "
                        "load time — this risks a circular import. Use a "
                        "function-local import instead."
                    )
