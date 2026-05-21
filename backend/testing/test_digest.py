"""Integration tests for /api/admin/digest and /api/admin/activity endpoints."""

from datetime import datetime, timedelta

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
    RegressionRemediationCase,
    RegressionCaseStatus,
)
from app.routers.admin_analytics import _derive_completed_at
from app.routers.admin_digest import (
    _compute_layer_coverage_by_class,
    _compute_symptom_layers_by_class,
    _stash_projected_task_fields,
    _summarize_symptoms_by_layer,
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
        "app.routers.admin_digest.dispatch_external_notification",
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

    # Create more tasks than the example cap for a single theme
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
                        "blind_spots": [f"did not run the full test suite variant {i}"],
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
    by_key = {(r["category"], r["task_type"]): r["count"] for r in results}
    assert by_key[("correctness", "create")] == 3
    assert by_key[("security", "fix")] == 1
    # Already-failed category must not appear
    assert ("correctness", "fix") not in by_key


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
    assert body["total_entries"] == 1
    assert body["disagreements"][0]["category"] == "correctness"
    assert body["disagreements"][0]["task_type"] == "create"
    assert body["disagreements"][0]["count"] == 1


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
async def test_pipeline_regressions_surface_signal_staleness(
    client: AsyncClient, db_session
):
    """Stale signal timestamps are surfaced as warning regressions."""
    from app.pipeline_regression_dispatcher import run_once

    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {
                "signal_last_computed_at": (
                    datetime.utcnow() - timedelta(days=3)
                ).isoformat(),
            },
            "fix_loop_divergence_signal_last_computed_at": (
                datetime.utcnow() - timedelta(days=8)
            ).isoformat(),
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await run_once(db_session, project.id)

    response = await client.get(
        f"/api/admin/pipeline-regressions?project_id={project.id}"
    )
    assert response.status_code == 200
    body = response.json()
    stale = {
        r["regression_class"]: r
        for r in body["regressions"]
        if r["regression_class"].startswith("signal_staleness:")
    }
    assert "signal_staleness:sr_catch_rate" in stale
    assert "signal_staleness:fix_loop_divergence" in stale
    assert "last computed" in stale["signal_staleness:sr_catch_rate"]["details"]


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
            "execution_evidence_miss_rates": {
                "test-coverage": {"miss_rate": 0.3, "miss_count": 3, "sample_count": 10}
            },
            "edges_considered_stats": {"total": 50, "with_sr": 40},
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
    assert "test-coverage" in trends["execution_evidence_miss_rates"]
    assert trends["edges_considered_stats"] == {"total": 50, "with_sr": 40}
    assert trends["by_severity"]["high"]["trend"] == "stable"
    assert trends["computed_at"] == "2026-05-01T00:00:00"
    assert trends["categories"]["correctness"]["fp_count"] == 1
    assert trends["by_scope"]["large"]["correctness"]["miss_rate"] == 0.6
    assert trends["by_scope"]["large"]["correctness"]["sample_count"] == 5


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
            "execution_evidence_miss_rates": {},
            "edges_considered_stats": {},
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
        "execution_evidence_miss_rates",
        "edges_considered_stats",
    ):
        assert key in trends, f"missing key: {key}"
    assert trends["sr_skip_rate"] is None
    assert trends["by_direction"] == {}
    assert trends["execution_evidence_miss_rates"] == {}


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


@pytest.mark.asyncio
async def test_clear_regression_manual_intervention_transitions_to_active(
    client: AsyncClient, db_session
):
    """POST clear-manual-intervention flips NEEDS_MANUAL_INTERVENTION→ACTIVE."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "sr_catch_rate_drop"
    flagged_at = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        attempts=3,
        needs_manual_intervention_at=flagged_at,
        signal_snapshot_at_spawn={"value": 0.42, "threshold": 0.6, "window": "24h"},
    )
    db_session.add(case)
    await db_session.commit()
    last_seen_before = case.last_seen_at

    response = await client.post(
        f"/api/admin/regression-cases/{regression_class}/clear-manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["regression_class"] == regression_class
    assert data["status"] == "ACTIVE"
    assert data["attempts"] == 3
    assert data["needs_manual_intervention_at"] is None
    assert "id" in data

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.needs_manual_intervention_at is None
    # Cooldown timestamps preserved so dispatcher does not immediately re-spawn.
    assert case.last_seen_at == last_seen_before
    # Snapshot preserved across the transition.
    assert case.signal_snapshot_at_spawn == {
        "value": 0.42,
        "threshold": 0.6,
        "window": "24h",
    }


@pytest.mark.asyncio
async def test_clear_regression_manual_intervention_404_when_missing(
    client: AsyncClient, db_session
):
    """POST clear-manual-intervention returns 404 when no case row exists."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/regression-cases/no_such_class/clear-manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_clear_regression_manual_intervention_404_when_not_flagged(
    client: AsyncClient, db_session
):
    """POST clear-manual-intervention returns 404 when case is ACTIVE (not flagged)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "fix_loop_divergence"
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        attempts=1,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/regression-cases/{regression_class}/clear-manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


# ── POST /regression-cases/{regression_class}/close ─────────────────────────


@pytest.mark.asyncio
async def test_close_regression_case_transitions_active_to_closed(
    client: AsyncClient, db_session
):
    """Operator-close transitions ACTIVE→CLOSED and stamps the close-loop log."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "sr_blind_spot:theme:did not run full backend pytest"
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        attempts=0,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        "/api/admin/regression-cases/close",
        params={
            "regression_class": regression_class,
            "project_id": str(project.id),
            "reason": "gated at source",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["regression_class"] == regression_class
    assert data["status"] == "CLOSED"
    assert data["prior_status"] == "ACTIVE"

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "operator_closed"
    log = case.fingerprint_to_fix_log or []
    assert len(log) == 1
    assert log[0]["operator_close_reason"] == "gated at source"
    assert log[0]["prior_status"] == "ACTIVE"
    assert log[0]["fingerprint"] == regression_class


@pytest.mark.asyncio
async def test_close_regression_case_idempotent_on_already_closed(
    client: AsyncClient, db_session
):
    """Closing an already-CLOSED case is a 200 no-op (no second log entry)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "sr_blind_spot:theme:did not run full docker pytest"
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.CLOSED,
        attempts=0,
        last_remediation_result="closed",
        fingerprint_to_fix_log=[
            {"fingerprint": regression_class, "recovered_at": "2026-05-08T00:00:00"},
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        "/api/admin/regression-cases/close",
        params={
            "regression_class": regression_class,
            "project_id": str(project.id),
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CLOSED"
    assert data["prior_status"] == "CLOSED"

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "closed"
    assert len(case.fingerprint_to_fix_log or []) == 1


@pytest.mark.asyncio
async def test_close_regression_case_404_when_missing(client: AsyncClient, db_session):
    """Returns 404 when no case row exists for (project_id, regression_class)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        "/api/admin/regression-cases/close",
        params={
            "regression_class": "no_such_class",
            "project_id": str(project.id),
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_close_regression_case_handles_slashes_and_quotes_in_class(
    client: AsyncClient, db_session
):
    """regression_class is a query parameter, so values with slashes / quotes
    don't fragment FastAPI's path routing. Concrete observation 2026-05-08:
    one ACTIVE case had class
    'sr_blind_spot:theme:agent/src/prompt.test.ts now expects ordering of \\'install'
    and a path-based endpoint 404'd because URL-decoded slashes split the path."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = (
        "sr_blind_spot:theme:agent/src/prompt.test.ts now expects ordering of 'install"
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        attempts=0,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        "/api/admin/regression-cases/close",
        params={
            "regression_class": regression_class,
            "project_id": str(project.id),
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["regression_class"] == regression_class
    assert data["status"] == "CLOSED"

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_close_regression_case_clears_durable_recovery_clock(
    client: AsyncClient, db_session
):
    """signal_healthy_since_at is cleared on close so a future re-escalation does
    not auto-close on its first tick from a stale value. signal_first_seen_at and
    signal_first_crossed_at are preserved so MTTD on a future spawn reflects the
    original detection latency."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "sr_blind_spot:theme:operator_close_marker_test"
    first_seen = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        attempts=0,
        signal_first_seen_at=first_seen,
        signal_first_crossed_at=first_seen,
        signal_healthy_since_at=_utcnow(),
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.post(
        "/api/admin/regression-cases/close",
        params={
            "regression_class": regression_class,
            "project_id": str(project.id),
        },
    )
    assert response.status_code == 200

    await db_session.refresh(case)
    assert case.signal_healthy_since_at is None
    # Detection-latency signals preserved.
    assert case.signal_first_seen_at == first_seen
    assert case.signal_first_crossed_at == first_seen


@pytest.mark.asyncio
async def test_regression_remediation_case_persists_close_loop_fields(db_session):
    """Model accepts and round-trips the close-loop tracking fields added in 0074."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="pipeline_success_rate",
        status=RegressionCaseStatus.MERGED,
        attempts=2,
        last_remediation_pr_number=4242,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={"value": 0.55, "threshold": 0.7},
        signal_snapshot_at_merge={"value": 0.68, "threshold": 0.7},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "abc123",
                "fix_pr_number": 4242,
                "closed_at": "2026-05-05T00:00:00Z",
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    assert case.attempts == 2
    assert case.last_remediation_pr_number == 4242
    assert case.last_remediation_result == "merged"
    assert case.signal_snapshot_at_spawn["value"] == 0.55
    assert case.signal_snapshot_at_merge["threshold"] == 0.7
    assert len(case.fingerprint_to_fix_log) == 1
    assert case.fingerprint_to_fix_log[0]["fix_pr_number"] == 4242


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


@pytest.mark.asyncio
async def test_list_regression_cases_empty(client: AsyncClient, db_session):
    """GET /regression-cases with no rows returns the expected envelope with empty cases list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert data["cases"] == []
    assert data["summary"] == {"by_regression_class": []}
    assert "status_filter" in data


@pytest.mark.asyncio
async def test_list_regression_cases_response_shape(client: AsyncClient, db_session):
    """GET /regression-cases pins the full response shape for each case."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        attempts=2,
        hit_count=3,
        sample_message="SR catch rate dropped below threshold",
        last_remediation_pr_number=1234,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={"value": 0.45, "threshold": 0.6},
        signal_snapshot_at_merge={"value": 0.62, "threshold": 0.6},
        fingerprint_to_fix_log=[{"fingerprint": "abc", "fix_pr_number": 1234}],
        needs_manual_intervention_at=None,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert len(data["cases"]) == 1

    c = data["cases"][0]
    # Pin every field the frontend consumes — contract drift test.
    assert "id" in c
    assert c["project_id"] == str(project.id)
    assert c["regression_class"] == "sr_catch_rate:correctness"
    assert c["status"] == "ACTIVE"
    assert c["attempts"] == 2
    assert c["hit_count"] == 3
    assert c["sample_message"] == "SR catch rate dropped below threshold"
    assert c["last_remediation_pr_number"] == 1234
    assert c["last_remediation_result"] == "merged"
    assert c["signal_snapshot_at_spawn"] == {"value": 0.45, "threshold": 0.6}
    assert c["signal_snapshot_at_merge"] == {"value": 0.62, "threshold": 0.6}
    assert c["recurrence_count"] == 1
    assert c["fingerprint_to_fix_log"] == [
        {"fingerprint": "abc", "fix_pr_number": 1234}
    ]
    assert c["needs_manual_intervention_at"] is None
    assert "first_seen_at" in c
    assert "last_seen_at" in c
    assert "last_remediation_task_id" in c
    assert "signal_first_crossed_at" in c


@pytest.mark.asyncio
async def test_list_regression_cases_summary_median_stats(
    client: AsyncClient, db_session
):
    """GET /regression-cases includes per-class MTTR summary from CLOSED rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 1, 9, 0, 0)
    cases = [
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate:correctness",
            status=RegressionCaseStatus.CLOSED,
            first_seen_at=first_seen,
            fingerprint_to_fix_log=[
                {
                    "merged_at": "2026-05-01T11:00:00Z",
                    "recovered_at": "2026-05-01T12:00:00Z",
                }
            ],
        ),
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate:latency",
            status=RegressionCaseStatus.CLOSED,
            first_seen_at=first_seen,
            fingerprint_to_fix_log=[
                {
                    "merged_at": "2026-05-01T10:00:00Z",
                    "recovered_at": "2026-05-01T13:00:00Z",
                }
            ],
        ),
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="fix_loop_divergence",
            status=RegressionCaseStatus.CLOSED,
            first_seen_at=first_seen,
            fingerprint_to_fix_log=[
                {
                    "merged_at": "2026-05-01T10:30:00Z",
                    "recovered_at": "2026-05-01T11:30:00Z",
                }
            ],
        ),
    ]
    db_session.add_all(cases)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    summary = response.json()["summary"]["by_regression_class"]
    assert summary == [
        {
            "regression_class": "fix_loop_divergence",
            "count": 1,
            "median_time_from_first_seen_to_merged_seconds": 5400.0,
            "median_time_from_merged_to_recovered_seconds": 3600.0,
            "median_time_to_detect_seconds": None,
        },
        {
            "regression_class": "sr_catch_rate:correctness",
            "count": 1,
            "median_time_from_first_seen_to_merged_seconds": 7200.0,
            "median_time_from_merged_to_recovered_seconds": 3600.0,
            "median_time_to_detect_seconds": None,
        },
        {
            "regression_class": "sr_catch_rate:latency",
            "count": 1,
            "median_time_from_first_seen_to_merged_seconds": 3600.0,
            "median_time_from_merged_to_recovered_seconds": 10800.0,
            "median_time_to_detect_seconds": None,
        },
    ]


@pytest.mark.asyncio
async def test_list_regression_cases_summary_uses_latest_merge_before_recovery(
    client: AsyncClient, db_session, mocker
):
    """Recovery is paired with the most recent merged_at in the real lifecycle log shape."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        first_seen_at=datetime(2026, 5, 1, 9, 0, 0),
        last_seen_at=datetime(2026, 5, 1, 13, 30, 0),
    )

    mocker.patch(
        "app.models._utcnow",
        side_effect=[
            datetime(2026, 5, 1, 10, 0, 0),
            datetime(2026, 5, 1, 12, 0, 0),
        ],
    )
    case.mark_merged(101, {"value": 0.61})
    case.re_arm(max_attempts=3)
    case.mark_merged(202, {"value": 0.67})
    case.mark_closed(post_merge_value=0.74, recovered_at="2026-05-01T13:00:00Z")

    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    summary = response.json()["summary"]["by_regression_class"]
    assert summary == [
        {
            "regression_class": "sr_catch_rate:correctness",
            "count": 1,
            "median_time_from_first_seen_to_merged_seconds": 10800.0,
            "median_time_from_merged_to_recovered_seconds": 3600.0,
            "median_time_to_detect_seconds": None,
        }
    ]

    closed_log = case.fingerprint_to_fix_log
    assert closed_log == [
        {
            "fingerprint": "sr_catch_rate:correctness",
            "fix_pr_number": 101,
            "merged_at": "2026-05-01T10:00:00",
        },
        {
            "fingerprint": "sr_catch_rate:correctness",
            "fix_pr_number": 202,
            "merged_at": "2026-05-01T12:00:00",
        },
        {
            "fingerprint": "sr_catch_rate:correctness",
            "recovered_at": "2026-05-01T13:00:00Z",
            "post_merge_value": 0.74,
        },
    ]


@pytest.mark.asyncio
async def test_list_regression_cases_summary_includes_time_to_detect(
    client: AsyncClient, db_session
):
    """Summary median_time_to_detect_seconds is computed when signal_first_crossed_at is set."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 1, 9, 0, 0)
    signal_crossed = datetime(
        2026, 5, 1, 8, 55, 0
    )  # 5 minutes before first_seen → 300s
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=first_seen,
        signal_first_crossed_at=signal_crossed,
        fingerprint_to_fix_log=[
            {
                "merged_at": "2026-05-01T10:00:00Z",
                "recovered_at": "2026-05-01T11:00:00Z",
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    summary = response.json()["summary"]["by_regression_class"]
    assert len(summary) == 1
    assert summary[0]["regression_class"] == "fix_loop_divergence"
    assert summary[0]["median_time_to_detect_seconds"] == 300.0


@pytest.mark.asyncio
async def test_list_regression_cases_default_filter_excludes_closed(
    client: AsyncClient, db_session
):
    """Default status filter includes ACTIVE/REMEDIATING/NEEDS_MANUAL_INTERVENTION; excludes CLOSED/MERGED."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
    )
    closed_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.CLOSED,
    )
    db_session.add_all([active_case, closed_case])
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["cases"][0]["regression_class"] == "sr_catch_rate:correctness"


@pytest.mark.asyncio
async def test_list_regression_cases_explicit_status_filter(
    client: AsyncClient, db_session
):
    """Explicit status= query param restricts results to only those statuses."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
    )
    manual_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        needs_manual_intervention_at=_utcnow(),
    )
    db_session.add_all([active_case, manual_case])
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id), "status": "NEEDS_MANUAL_INTERVENTION"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["cases"][0]["regression_class"] == "fix_loop_divergence"
    assert data["cases"][0]["status"] == "NEEDS_MANUAL_INTERVENTION"
    assert data["status_filter"] == ["NEEDS_MANUAL_INTERVENTION"]


@pytest.mark.asyncio
async def test_list_regression_cases_invalid_status_returns_400(
    client: AsyncClient, db_session
):
    """Invalid status value returns 400 with a descriptive error."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id), "status": "BOGUS"},
    )
    assert response.status_code == 400
    assert "Invalid status values" in response.json()["error"]["message"]


# ── GET /regression-cases/manual-intervention ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_regression_manual_intervention_response_shape(
    client: AsyncClient, db_session
):
    """GET /regression-cases/manual-intervention pins the response shape."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    flagged_at = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:architecture",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        attempts=3,
        needs_manual_intervention_at=flagged_at,
        signal_snapshot_at_spawn={"value": 0.3, "threshold": 0.6},
        signal_snapshot_at_merge={},
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert len(data["cases"]) == 1

    c = data["cases"][0]
    assert "id" in c
    assert c["project_id"] == str(project.id)
    assert c["regression_class"] == "sr_catch_rate:architecture"
    assert c["signal_name"] == "sr catch rate:architecture"
    assert c["status"] == "NEEDS_MANUAL_INTERVENTION"
    assert c["attempts"] == 3
    assert c["signal_snapshot_at_spawn"] == {"value": 0.3, "threshold": 0.6}
    assert c["signal_snapshot_at_merge"] == {}
    assert c["recurrence_count"] == 0
    assert c["needs_manual_intervention_at"] is not None
    assert "last_seen_at" in c
    assert "last_remediation_pr_number" in c
    assert c["investigate_spawned_at"] is None
    assert c["time_to_investigate_sec"] is None


@pytest.mark.asyncio
async def test_list_regression_manual_intervention_investigate_spawned_at_populated(
    client: AsyncClient, db_session
):
    """investigate_spawned_at and time_to_investigate_sec are returned when column is set."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 5, 10, 0, 0)
    investigate_spawned = datetime(2026, 5, 5, 10, 5, 0)  # 5 minutes later → 300s

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        first_seen_at=first_seen,
        investigate_spawned_at=investigate_spawned,
        needs_manual_intervention_at=_utcnow(),
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["investigate_spawned_at"] is not None
    assert c["time_to_investigate_sec"] == 300


# ── GET /regression-cases/recently-closed ─────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recently_closed_regression_cases_filters_to_closed_only(
    client: AsyncClient, db_session
):
    """GET /regression-cases/recently-closed only returns CLOSED cases."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    closed_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
    )
    active_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.ACTIVE,
    )
    manual_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="pipeline_success_rate",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        needs_manual_intervention_at=_utcnow(),
    )
    db_session.add_all([closed_case, active_case, manual_case])
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert len(data["cases"]) == 1
    assert data["cases"][0]["regression_class"] == "sr_catch_rate:correctness"


@pytest.mark.asyncio
async def test_list_recently_closed_regression_cases_scoped_by_project(
    client: AsyncClient, db_session
):
    """GET /regression-cases/recently-closed only returns cases for the requested project."""
    project_a = make_project()
    project_b = make_project()
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    case_a = RegressionRemediationCase(
        project_id=project_a.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
    )
    case_b = RegressionRemediationCase(
        project_id=project_b.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.CLOSED,
    )
    db_session.add_all([case_a, case_b])
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project_a.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["cases"][0]["regression_class"] == "sr_catch_rate:correctness"


@pytest.mark.asyncio
async def test_list_recently_closed_extracts_fix_pr_and_recovered_at_and_time_to_recover(
    client: AsyncClient, db_session
):
    """Multi-entry fingerprint_to_fix_log: latest fix_pr_number/recovered_at win, time_to_recover_sec is computed."""
    from datetime import timezone

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    recovered_at_str = "2026-05-05T13:30:00+00:00"  # 90 minutes later → 5400s

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=first_seen,
        fingerprint_to_fix_log=[
            {"fingerprint": "abc", "fix_pr_number": 1111},
            {
                "fingerprint": "def",
                "fix_pr_number": 2222,
                "recovered_at": recovered_at_str,
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    # Latest entry's fix_pr_number wins (the loop overwrites earlier values).
    assert c["fix_pr_number"] == 2222
    assert c["recovered_at"] == recovered_at_str
    assert c["recurrence_count"] == 2
    # 90 minutes = 5400 seconds.
    assert c["time_to_recover_sec"] == 5400
    assert c["regression_class"] == "sr_catch_rate:correctness"
    assert "id" in c
    assert "first_seen_at" in c


@pytest.mark.asyncio
async def test_list_recently_closed_happy_path_recurrence_count_is_one_cycle(
    client: AsyncClient, db_session
):
    """A fix entry + a separate recovery entry collapses to one cycle (not two)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {"fingerprint": "abc", "fix_pr_number": 555},
            {"fingerprint": "abc", "recovered_at": "2026-05-05T13:30:00+00:00"},
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["recurrence_count"] == 1


@pytest.mark.asyncio
async def test_list_recently_closed_two_cycles_when_re_armed(
    client: AsyncClient, db_session
):
    """Two fix→recovery cycles count as 2 recurrences."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {"fingerprint": "abc", "fix_pr_number": 100},
            {"fingerprint": "abc", "recovered_at": "2026-05-05T11:00:00+00:00"},
            {"fingerprint": "abc", "fix_pr_number": 200},
            {"fingerprint": "abc", "recovered_at": "2026-05-05T13:00:00+00:00"},
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["recurrence_count"] == 2


@pytest.mark.asyncio
async def test_list_recently_closed_time_to_recover_none_when_no_recovered_at(
    client: AsyncClient, db_session
):
    """time_to_recover_sec is None when fingerprint_to_fix_log entries lack recovered_at."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[{"fingerprint": "abc", "fix_pr_number": 999}],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    assert c["fix_pr_number"] == 999
    assert c["recovered_at"] is None
    assert c["time_to_recover_sec"] is None


@pytest.mark.asyncio
async def test_list_recently_closed_time_to_detect_computed_from_signal_first_crossed_at(
    client: AsyncClient, db_session
):
    """time_to_detect_sec is the gap between signal_first_crossed_at and first_seen_at."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 5, 10, 0, 0)
    signal_crossed = datetime(2026, 5, 5, 9, 50, 0)  # 10 minutes before → 600s

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=first_seen,
        signal_first_crossed_at=signal_crossed,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["time_to_detect_sec"] == 600
    assert c["signal_first_crossed_at"] is not None
    assert c["time_to_recover_sec"] is None  # no recovered_at


@pytest.mark.asyncio
async def test_list_recently_closed_investigate_spawned_at_populated(
    client: AsyncClient, db_session
):
    """investigate_spawned_at and time_to_investigate_sec are returned when column is set."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first_seen = datetime(2026, 5, 5, 10, 0, 0)
    investigate_spawned = datetime(2026, 5, 5, 10, 5, 0)  # 5 minutes later → 300s

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=first_seen,
        investigate_spawned_at=investigate_spawned,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["investigate_spawned_at"] is not None
    assert c["time_to_investigate_sec"] == 300


@pytest.mark.asyncio
async def test_list_recently_closed_merged_at_paired_with_recovery(
    client: AsyncClient, db_session
):
    """merged_at is returned, paired with the latest recovered_at in fingerprint_to_fix_log."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=datetime(2026, 5, 1, 9, 0, 0),
        fingerprint_to_fix_log=[
            {
                "fingerprint": "abc",
                "merged_at": "2026-05-01T10:00:00+00:00",
                "recovered_at": "2026-05-01T11:00:00+00:00",
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["merged_at"] == "2026-05-01T10:00:00+00:00"
    assert c["recovered_at"] == "2026-05-01T11:00:00+00:00"


@pytest.mark.asyncio
async def test_list_recently_closed_merged_at_null_when_no_merge_logged(
    client: AsyncClient, db_session
):
    """merged_at is null when fingerprint_to_fix_log has no merged_at entries."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=datetime(2026, 5, 1, 9, 0, 0),
        fingerprint_to_fix_log=[
            {"fingerprint": "abc", "recovered_at": "2026-05-01T11:00:00+00:00"},
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["merged_at"] is None
    assert c["recovered_at"] == "2026-05-01T11:00:00+00:00"


@pytest.mark.asyncio
async def test_list_recently_closed_investigate_spawned_at_null_for_legacy(
    client: AsyncClient, db_session
):
    """investigate_spawned_at and time_to_investigate_sec are null for legacy rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="pipeline_success_rate",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=datetime(2026, 4, 1, 0, 0, 0),
        investigate_spawned_at=None,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    c = response.json()["cases"][0]
    assert c["investigate_spawned_at"] is None
    assert c["time_to_investigate_sec"] is None


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


@pytest.mark.asyncio
async def test_regression_dispatcher_health_empty(client: AsyncClient, db_session):
    """Empty state: no cycle records and no cases returns zeroed aggregates."""
    import app.shared_state as _ss

    saved = list(_ss.dispatcher_cycle_log)
    _ss.dispatcher_cycle_log.clear()
    try:
        response = await client.get(
            "/api/admin/regression-dispatcher/health",
            params={"window_hours": 24},
        )
    finally:
        for rec in saved:
            _ss.dispatcher_cycle_log.append(rec)

    assert response.status_code == 200
    data = response.json()
    agg = data["aggregate"]
    assert agg["cycles"] == 0
    assert agg["spawns"] == 0
    assert agg["signals_tripped"] == 0
    assert agg["mttd_p50_sec"] is None
    assert agg["recoveries"] == {"marginal": 0, "confirmed": 0}
    assert data["per_class"] == []
    assert len(data["spawns_sparkline"]) == 24


@pytest.mark.asyncio
async def test_regression_dispatcher_health_with_cycle_and_case(
    client: AsyncClient, db_session
):
    """Cycle records aggregate correctly; open case appears in per_class.

    Cycle records are now persisted in the durable ``DispatcherCycleRecord``
    table (see migration 0087); the legacy in-memory
    ``shared_state.dispatcher_cycle_log`` is no longer the source of truth
    for this endpoint.
    """
    from app.models import DispatcherCycleRecord

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
    )
    db_session.add(case)
    now = _utcnow()
    db_session.add(
        DispatcherCycleRecord(
            project_id=project.id,
            cycle_started_at=now - timedelta(minutes=5),
            cycle_finished_at=now - timedelta(minutes=4),
            signals_evaluated=3,
            signals_tripped=1,
            spawns_emitted=1,
            rejections_by_reason={"cooldown": 0, "dedup": 1},
            mttd_p50_sec=120.0,
            marginal_recoveries_count=1,
            confirmed_recoveries_count=2,
        )
    )
    db_session.add(
        DispatcherCycleRecord(
            project_id=project.id,
            cycle_started_at=now - timedelta(minutes=3),
            cycle_finished_at=now - timedelta(minutes=2),
            signals_evaluated=2,
            signals_tripped=0,
            spawns_emitted=0,
            rejections_by_reason={"cooldown": 0, "dedup": 0},
            mttd_p50_sec=180.0,
            marginal_recoveries_count=0,
            confirmed_recoveries_count=1,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-dispatcher/health",
        params={"project_id": str(project.id), "window_hours": 24},
    )

    assert response.status_code == 200
    data = response.json()
    agg = data["aggregate"]
    assert agg["cycles"] == 2
    assert agg["signals_tripped"] == 1
    assert agg["spawns"] == 1
    assert agg["rejections"]["dedup"] == 1
    # mttd_p50_sec should be mean of 120.0 and 180.0
    assert agg["mttd_p50_sec"] == 150.0
    # recoveries should sum across both cycles
    assert agg["recoveries"]["marginal"] == 1
    assert agg["recoveries"]["confirmed"] == 3

    assert len(data["per_class"]) == 1
    cls = data["per_class"][0]
    assert cls["regression_class"] == "sr_catch_rate:correctness"
    assert cls["status"] == RegressionCaseStatus.ACTIVE


@pytest.mark.asyncio
async def test_regression_dispatcher_health_mttd_with_none_values(
    client: AsyncClient, db_session
):
    """mttd_p50_sec aggregates only non-None values; None cycles are skipped."""
    from app.models import DispatcherCycleRecord

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        DispatcherCycleRecord(
            project_id=project.id,
            cycle_started_at=now - timedelta(minutes=5),
            cycle_finished_at=now - timedelta(minutes=4),
            signals_evaluated=1,
            signals_tripped=0,
            spawns_emitted=0,
            rejections_by_reason={},
            mttd_p50_sec=100.0,
        )
    )
    db_session.add(
        DispatcherCycleRecord(
            project_id=project.id,
            cycle_started_at=now - timedelta(minutes=3),
            cycle_finished_at=now - timedelta(minutes=2),
            signals_evaluated=1,
            signals_tripped=0,
            spawns_emitted=0,
            rejections_by_reason={},
            mttd_p50_sec=None,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-dispatcher/health",
        params={"project_id": str(project.id), "window_hours": 24},
    )

    assert response.status_code == 200
    data = response.json()
    agg = data["aggregate"]
    assert agg["cycles"] == 2
    # mttd_p50_sec should be 100.0 (only non-None value)
    assert agg["mttd_p50_sec"] == 100.0
    assert agg["recoveries"] == {"marginal": 0, "confirmed": 0}


@pytest.mark.asyncio
async def test_regression_dispatcher_health_mtti_null_when_no_acked_cases(
    client: AsyncClient, db_session
):
    """mtti_p50_sec is None when no cases have operator_acknowledged_at set."""
    from app.models import DispatcherCycleRecord

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Case escalated but not yet acknowledged
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:mtti_test",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        needs_manual_intervention_at=now - timedelta(hours=1),
        operator_acknowledged_at=None,
    )
    db_session.add(case)
    db_session.add(
        DispatcherCycleRecord(
            project_id=project.id,
            cycle_started_at=now - timedelta(minutes=5),
            cycle_finished_at=now - timedelta(minutes=4),
            signals_evaluated=1,
            signals_tripped=1,
            spawns_emitted=1,
            rejections_by_reason={},
            mttd_p50_sec=60.0,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-dispatcher/health",
        params={"project_id": str(project.id), "window_hours": 24},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["aggregate"]["mtti_p50_sec"] is None


@pytest.mark.asyncio
async def test_regression_dispatcher_health_mtti_p50_with_acked_cases(
    client: AsyncClient, db_session
):
    """mtti_p50_sec is the median seconds from escalation to acknowledgement."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Case 1: 600s MTTI (10 minutes)
    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate:mtti_case1",
            status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            needs_manual_intervention_at=now - timedelta(hours=2),
            operator_acknowledged_at=now - timedelta(hours=2) + timedelta(seconds=600),
        )
    )
    # Case 2: 1200s MTTI (20 minutes)
    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate:mtti_case2",
            status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            needs_manual_intervention_at=now - timedelta(hours=1),
            operator_acknowledged_at=now - timedelta(hours=1) + timedelta(seconds=1200),
        )
    )
    # Case 3: not acknowledged — must be excluded from MTTI
    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate:mtti_unacked",
            status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            needs_manual_intervention_at=now - timedelta(minutes=30),
            operator_acknowledged_at=None,
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-dispatcher/health",
        params={"project_id": str(project.id), "window_hours": 24},
    )

    assert response.status_code == 200
    data = response.json()
    agg = data["aggregate"]
    # Median of [600, 1200] = 900
    assert agg["mtti_p50_sec"] == 900.0


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


@pytest.mark.asyncio
async def test_anomaly_tracker_snapshot_dispatch_lookup(
    client: AsyncClient, db_session
):
    """When a RegressionRemediationCase exists for (project, anomaly_key), its
    last_seen_at is echoed as last_dispatch_attempt_at."""
    import app.shared_state as _ss

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case_seen = _utcnow() - timedelta(minutes=10)
    case = RegressionRemediationCase(
        project_id=project.id,
        # Dispatcher prefixes anomaly cases with 'anomaly:' (pipeline_regression_dispatcher.py:1748);
        # the snapshot endpoint strips that prefix when keying case_index.
        regression_class="anomaly:FailureRateMomentum",
        status=RegressionCaseStatus.ACTIVE,
        last_seen_at=case_seen,
    )
    db_session.add(case)
    await db_session.commit()

    proj_key = str(project.id)
    saved_dict = dict(_ss.persistent_anomalies)
    _ss.persistent_anomalies.clear()
    _ss.persistent_anomalies[proj_key] = {
        "FailureRateMomentum": {
            "first_seen": _utcnow().isoformat(),
            "last_seen": _utcnow().isoformat(),
            "occurrence_count": 3,
            "severity": "high",
            "latest_details": "x",
        },
    }
    try:
        response = await client.get(
            "/api/admin/anomaly-tracker/snapshot",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        data = response.json()
        entry = data["projects"][0]["anomalies"][0]
        assert entry["last_dispatch_attempt_at"] == case_seen.isoformat()
    finally:
        _ss.persistent_anomalies.clear()
        _ss.persistent_anomalies.update(saved_dict)


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


def test_summarize_symptoms_by_layer_cross_layer_two_populated_layers():
    """Two layers with recent entries → cross_layer=true, both layers reported."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    symptoms = {
        "CI_WORKFLOW": [
            _layer_entry("fp1", now - timedelta(hours=1), "ci sample"),
            _layer_entry("fp1", now - timedelta(minutes=30), "ci sample updated"),
            _layer_entry("fp2", now - timedelta(minutes=10), "newer ci sample"),
        ],
        "AGENT_VM": [
            _layer_entry("fp3", now - timedelta(hours=2), "vm sample"),
        ],
    }
    summary = _summarize_symptoms_by_layer(symptoms, now=now)
    assert summary["cross_layer"] is True
    assert summary["layers_affected"] == ["AGENT_VM", "CI_WORKFLOW"]
    assert summary["window_hours"] == 48
    by_layer = {entry["layer"]: entry for entry in summary["layers"]}
    assert by_layer["CI_WORKFLOW"]["count"] == 3
    assert by_layer["CI_WORKFLOW"]["distinct_fingerprints"] == 2
    assert by_layer["CI_WORKFLOW"]["sample"] == "newer ci sample"
    assert by_layer["AGENT_VM"]["count"] == 1
    assert by_layer["AGENT_VM"]["distinct_fingerprints"] == 1
    # Layers ordered by last_observed_at descending → CI_WORKFLOW first.
    assert summary["layers"][0]["layer"] == "CI_WORKFLOW"


def test_summarize_symptoms_by_layer_single_layer_not_cross_layer():
    """One layer populated → cross_layer=false."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    symptoms = {
        "BASELINE_GATE": [
            _layer_entry("fp1", now - timedelta(hours=1), "lint baseline"),
        ],
    }
    summary = _summarize_symptoms_by_layer(symptoms, now=now)
    assert summary["cross_layer"] is False
    assert summary["layers_affected"] == ["BASELINE_GATE"]
    assert len(summary["layers"]) == 1


def test_summarize_symptoms_by_layer_excludes_entries_older_than_window():
    """Entries older than 48h are dropped; layer with only stale entries is omitted."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    symptoms = {
        "CI_WORKFLOW": [
            _layer_entry("fp_old", now - timedelta(hours=72), "stale"),
            _layer_entry("fp_new", now - timedelta(hours=1), "fresh"),
        ],
        "AGENT_VM": [
            _layer_entry("fp_old", now - timedelta(hours=72), "stale only"),
        ],
    }
    summary = _summarize_symptoms_by_layer(symptoms, now=now)
    assert summary["layers_affected"] == ["CI_WORKFLOW"]
    assert summary["cross_layer"] is False
    ci = summary["layers"][0]
    assert ci["count"] == 1  # only the fresh entry counted
    assert ci["sample"] == "fresh"


def test_summarize_symptoms_by_layer_empty_and_malformed_safe():
    """None, empty, and malformed payloads return an empty summary without raising."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    empty_shape = {
        "layers": [],
        "layers_affected": [],
        "cross_layer": False,
        "window_hours": 48,
        "layer_mismatch": False,
        "prior_fix_layers": [],
        "current_symptom_layers": [],
    }
    assert _summarize_symptoms_by_layer(None, now=now) == empty_shape
    assert _summarize_symptoms_by_layer({}, now=now) == empty_shape
    # Non-list entries / bad observed_at / non-dict items are skipped.
    bad = {
        "CI_WORKFLOW": "not-a-list",
        "AGENT_VM": [
            "not-a-dict",
            {"fingerprint": "fp", "observed_at": "totally-bogus", "sample": "x"},
            {"fingerprint": "fp2", "observed_at": None, "sample": "y"},
        ],
    }
    assert _summarize_symptoms_by_layer(bad, now=now) == empty_shape


def test_summarize_symptoms_by_layer_truncates_long_sample():
    """Sample is truncated to 200 chars."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    long_sample = "x" * 500
    symptoms = {
        "CI_WORKFLOW": [
            _layer_entry("fp1", now - timedelta(minutes=5), long_sample),
        ],
    }
    summary = _summarize_symptoms_by_layer(symptoms, now=now)
    assert len(summary["layers"][0]["sample"]) == 200


def test_summarize_symptoms_by_layer_handles_zulu_timestamp():
    """observed_at written as ...Z is parsed correctly."""
    now = datetime(2026, 5, 7, 12, 0, 0)
    symptoms = {
        "CI_WORKFLOW": [
            {
                "fingerprint": "fp1",
                "observed_at": "2026-05-07T11:30:00Z",
                "sample": "z",
            }
        ],
    }
    summary = _summarize_symptoms_by_layer(symptoms, now=now)
    assert summary["layers_affected"] == ["CI_WORKFLOW"]
    assert summary["layers"][0]["count"] == 1


@pytest.mark.asyncio
async def test_list_regression_cases_includes_layer_summary(
    client: AsyncClient, db_session
):
    """GET /regression-cases returns layer_summary on each case."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        symptoms_by_layer={
            "CI_WORKFLOW": [
                _layer_entry("fp1", now - timedelta(hours=1), "ci sample"),
            ],
            "AGENT_VM": [
                _layer_entry("fp2", now - timedelta(hours=2), "vm sample"),
            ],
        },
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    layer_summary = data["cases"][0]["layer_summary"]
    assert layer_summary["cross_layer"] is True
    assert sorted(layer_summary["layers_affected"]) == ["AGENT_VM", "CI_WORKFLOW"]
    assert layer_summary["window_hours"] == 48
    layer_names = {entry["layer"] for entry in layer_summary["layers"]}
    assert layer_names == {"CI_WORKFLOW", "AGENT_VM"}


@pytest.mark.asyncio
async def test_list_regression_manual_intervention_includes_layer_summary(
    client: AsyncClient, db_session
):
    """GET /regression-cases/manual-intervention returns layer_summary on each case."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:architecture",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        attempts=3,
        needs_manual_intervention_at=now,
        symptoms_by_layer={
            "REVIEWER_PASS": [
                _layer_entry("fp1", now - timedelta(hours=3), "reviewer sample"),
            ],
        },
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/manual-intervention",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    layer_summary = data["cases"][0]["layer_summary"]
    assert layer_summary["cross_layer"] is False
    assert layer_summary["layers_affected"] == ["REVIEWER_PASS"]


@pytest.mark.asyncio
async def test_list_regression_cases_includes_untargeted_layers(
    client: AsyncClient, db_session
):
    """GET /regression-cases includes untargeted_layers from the dispatcher helper."""
    from app.pipeline_regression_dispatcher import (
        _CANONICAL_RECURRENCE_LAYERS,
        compute_untargeted_layers,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fix_log = [
        {"fingerprint": "abc", "fix_pr_number": 1, "layers_targeted": ["AGENT_VM"]},
        {
            "fingerprint": "abc",
            "fix_pr_number": 2,
            "layers_targeted": ["REVIEWER_PASS"],
        },
    ]
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=fix_log,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    untargeted = data["cases"][0]["untargeted_layers"]
    # Helper-produced answer is the source of truth.
    expected = compute_untargeted_layers(fix_log)
    assert untargeted == expected
    # Sanity: AGENT_VM and REVIEWER_PASS were targeted; the other three remain
    # in canonical order.
    assert untargeted == [
        layer
        for layer in _CANONICAL_RECURRENCE_LAYERS
        if layer not in {"AGENT_VM", "REVIEWER_PASS"}
    ]


@pytest.mark.asyncio
async def test_list_recently_closed_includes_layer_summary(
    client: AsyncClient, db_session
):
    """GET /regression-cases/recently-closed returns layer_summary on each case."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        symptoms_by_layer={
            "AGENT_VERB": [
                _layer_entry("fp1", now - timedelta(hours=4), "verb sample"),
            ],
        },
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    layer_summary = data["cases"][0]["layer_summary"]
    assert layer_summary["layers_affected"] == ["AGENT_VERB"]
    assert layer_summary["cross_layer"] is False


@pytest.mark.asyncio
async def test_list_auto_recovered_includes_layer_summary(
    client: AsyncClient, db_session
):
    """GET /regression-cases/auto-recovered returns layer_summary on each case."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:scope-drift",
        status=RegressionCaseStatus.CLOSED,
        last_remediation_result="recovered_unattended",
        symptoms_by_layer={
            "DISPATCHER": [
                _layer_entry("fp1", now - timedelta(hours=2), "dispatcher sample"),
            ],
            "AGENT_VM": [
                _layer_entry("fp2", now - timedelta(hours=1), "vm sample"),
            ],
        },
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/auto-recovered",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    layer_summary = data["cases"][0]["layer_summary"]
    assert layer_summary["cross_layer"] is True
    assert sorted(layer_summary["layers_affected"]) == ["AGENT_VM", "DISPATCHER"]


@pytest.mark.asyncio
async def test_list_regression_cases_layer_summary_empty_for_default_case(
    client: AsyncClient, db_session
):
    """When symptoms_by_layer defaults to empty, layer_summary is the empty shape."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.ACTIVE,
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    layer_summary = response.json()["cases"][0]["layer_summary"]
    assert layer_summary == {
        "layers": [],
        "layers_affected": [],
        "cross_layer": False,
        "window_hours": 48,
        "layer_mismatch": False,
        "prior_fix_layers": [],
        "current_symptom_layers": [],
    }


@pytest.mark.asyncio
async def test_list_recently_closed_hold_days_history_no_prior_attempts(
    client: AsyncClient, db_session
):
    """When fingerprint_to_fix_log has no entries with both merged_at+recovered_at, hold_days_history is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {"fingerprint": "abc", "fix_pr_number": 1111},
            {"fingerprint": "def", "fix_pr_number": 2222},
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    assert c["hold_days_history"] == []
    assert c["is_fragile"] is False


@pytest.mark.asyncio
async def test_list_recently_closed_hold_days_history_with_short_hold(
    client: AsyncClient, db_session
):
    """When a prior attempt re-tripped within 48h, it appears in hold_days_history and is_fragile is True."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "abc",
                "fix_pr_number": 1111,
                "merged_at": "2026-05-05T10:00:00+00:00",
            },
            {
                "fingerprint": "abc",
                "recovered_at": "2026-05-05T16:30:00+00:00",  # 6.5 hours = 0.27 days
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    assert len(c["hold_days_history"]) == 1
    assert c["hold_days_history"][0] < 1.0  # 6.5 hours is less than 1 day
    assert c["is_fragile"] is True


@pytest.mark.asyncio
async def test_list_recently_closed_hold_days_history_with_long_hold(
    client: AsyncClient, db_session
):
    """When all prior attempts held >7 days, hold_days_history contains those durations and is_fragile is False."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "abc",
                "fix_pr_number": 1111,
                "merged_at": "2026-04-28T10:00:00+00:00",
            },
            {
                "fingerprint": "abc",
                "recovered_at": "2026-05-05T10:00:00+00:00",  # 7 days exactly
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    assert len(c["hold_days_history"]) == 1
    assert c["hold_days_history"][0] == 7.0
    assert c["is_fragile"] is False


@pytest.mark.asyncio
async def test_list_recently_closed_hold_days_history_mixed_holds_with_fragile(
    client: AsyncClient, db_session
):
    """When hold_days_history has mixed durations including a short hold, is_fragile is True."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "abc",
                "fix_pr_number": 1111,
                "merged_at": "2026-05-01T10:00:00+00:00",
            },
            {
                "fingerprint": "abc",
                "recovered_at": "2026-05-02T10:00:00+00:00",  # 1.0 day
            },
            {
                "fingerprint": "def",
                "fix_pr_number": 2222,
                "merged_at": "2026-05-02T11:00:00+00:00",
            },
            {
                "fingerprint": "def",
                "recovered_at": "2026-05-02T17:00:00+00:00",  # 0.25 days (6 hours) - fragile!
            },
            {
                "fingerprint": "ghi",
                "fix_pr_number": 3333,
                "merged_at": "2026-05-02T18:00:00+00:00",
            },
            {
                "fingerprint": "ghi",
                "recovered_at": "2026-05-10T18:00:00+00:00",  # 8.0 days
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/recently-closed",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1

    c = data["cases"][0]
    assert len(c["hold_days_history"]) == 3
    assert c["hold_days_history"] == [1.0, 0.25, 8.0]
    assert c["is_fragile"] is True  # Because 0.25 < 2.0


# ── GET /regression-cases/trend ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regression_case_trend_empty(client: AsyncClient, db_session):
    """Returns contiguous daily buckets with zeros when no cases exist."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        "/api/admin/regression-cases/trend",
        params={"project_id": str(project.id), "days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    assert len(body["daily"]) == 7
    assert all(b["opened"] == 0 and b["closed"] == 0 for b in body["daily"])
    assert body["by_class"] == {}


@pytest.mark.asyncio
async def test_regression_case_trend_counts_opened_and_closed(
    client: AsyncClient, db_session
):
    """Cases opened today appear in today's bucket; CLOSED cases appear in closed bucket."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # An ACTIVE case opened today (opened += 1).
    opened_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_stall",
        status=RegressionCaseStatus.ACTIVE,
        first_seen_at=now,
        updated_at=now,
    )
    # A CLOSED case (different class) updated today (closed += 1).
    closed_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence",
        status=RegressionCaseStatus.CLOSED,
        first_seen_at=now - timedelta(days=5),
        updated_at=now,
    )
    db_session.add(opened_case)
    db_session.add(closed_case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/trend",
        params={"project_id": str(project.id), "days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    today = now.strftime("%Y-%m-%d")
    today_bucket = next(b for b in body["daily"] if b["date"] == today)
    assert today_bucket["opened"] == 1
    assert today_bucket["closed"] == 1
    # Per-class breakdown present for both classes.
    assert "fix_loop_stall" in body["by_class"]
    assert "fix_loop_divergence" in body["by_class"]
    stall_today = next(
        b for b in body["by_class"]["fix_loop_stall"] if b["date"] == today
    )
    assert stall_today["opened"] == 1
    assert stall_today["closed"] == 0
    div_today = next(
        b for b in body["by_class"]["fix_loop_divergence"] if b["date"] == today
    )
    assert div_today["opened"] == 0
    assert div_today["closed"] == 1


# ── GET /regression-cases/layer-defense-density ─────────────────────────────


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_returns_canonical_layers_with_counts(
    client: AsyncClient, db_session
):
    """Endpoint returns all six canonical layers with correct counts and sample case ids."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case_a = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:a",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "fp1",
                "merged_at": (now - timedelta(days=2)).isoformat(),
                "layers_targeted": ["BASELINE_GATE"],
            },
            {
                "fingerprint": "fp2",
                "merged_at": (now - timedelta(days=4)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW", "REVIEWER_PASS"],
            },
        ],
    )
    case_b = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:b",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "fp3",
                "merged_at": (now - timedelta(days=1)).isoformat(),
                "layers_targeted": ["BASELINE_GATE"],
            },
        ],
    )
    db_session.add_all([case_a, case_b])
    await db_session.commit()
    await db_session.refresh(case_a)
    await db_session.refresh(case_b)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 30
    assert "generated_at" in body
    layers = body["layers"]
    assert [r["layer"] for r in layers] == [
        "CI_WORKFLOW",
        "AGENT_VM",
        "AGENT_VERB",
        "BASELINE_GATE",
        "REVIEWER_PASS",
        "VIEWPORT",
    ]
    by_layer = {r["layer"]: r for r in layers}
    assert by_layer["BASELINE_GATE"]["fix_count"] == 2
    assert by_layer["CI_WORKFLOW"]["fix_count"] == 1
    assert by_layer["REVIEWER_PASS"]["fix_count"] == 1
    assert by_layer["AGENT_VM"]["fix_count"] == 0
    assert by_layer["AGENT_VM"]["last_fix_at"] is None
    assert by_layer["AGENT_VM"]["sample_case_ids"] == []
    # case_b's most-recent BASELINE_GATE entry leads case_a's older one.
    assert by_layer["BASELINE_GATE"]["sample_case_ids"] == [
        str(case_b.id),
        str(case_a.id),
    ]
    assert by_layer["CI_WORKFLOW"]["sample_case_ids"] == [str(case_a.id)]


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_respects_days_param(
    client: AsyncClient, db_session
):
    """A days=7 window excludes entries older than 7 days."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:c",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "recent",
                "merged_at": (now - timedelta(days=2)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW"],
            },
            {
                "fingerprint": "old",
                "merged_at": (now - timedelta(days=20)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW"],
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id), "days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    by_layer = {r["layer"]: r for r in body["layers"]}
    assert by_layer["CI_WORKFLOW"]["fix_count"] == 1
    assert by_layer["CI_WORKFLOW"]["sample_case_ids"] == [str(case.id)]


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_no_cases_returns_zeroed_layers(
    client: AsyncClient, db_session
):
    """Project with no regression cases returns six zero-row layers."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    layers = body["layers"]
    assert len(layers) == 6
    for row in layers:
        assert row["fix_count"] == 0
        assert row["last_fix_at"] is None
        assert row["sample_case_ids"] == []


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_validates_days_param(
    client: AsyncClient, db_session
):
    """days param must be within the allowed range."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id), "days": 0},
    )
    assert response.status_code == 422

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id), "days": 1000},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_includes_fragility_stats(
    client: AsyncClient, db_session
):
    """Endpoint includes avg_hold_days, still_holding_count, held_gt_7d_count,
    retripped_within_48h_count for each layer row."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:frag",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "fp1",
                "merged_at": (now - timedelta(days=5)).isoformat(),
                "recovered_at": (now - timedelta(days=4, hours=23)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW"],
            },
            {
                "fingerprint": "fp2",
                "merged_at": (now - timedelta(days=10)).isoformat(),
                "layers_targeted": ["BASELINE_GATE"],
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    by_layer = {r["layer"]: r for r in body["layers"]}

    for row in body["layers"]:
        assert "avg_hold_days" in row
        assert "still_holding_count" in row
        assert "held_gt_7d_count" in row
        assert "retripped_within_48h_count" in row

    ci = by_layer["CI_WORKFLOW"]
    assert ci["fix_count"] == 1
    assert ci["avg_hold_days"] is not None
    assert ci["avg_hold_days"] < 2.0
    assert ci["retripped_within_48h_count"] == 1
    assert ci["still_holding_count"] == 0

    bg = by_layer["BASELINE_GATE"]
    assert bg["fix_count"] == 1
    assert bg["avg_hold_days"] is None
    assert bg["still_holding_count"] == 1
    assert bg["retripped_within_48h_count"] == 0

    av = by_layer["AGENT_VM"]
    assert av["fix_count"] == 0
    assert av["avg_hold_days"] is None
    assert av["still_holding_count"] == 0
    assert av["held_gt_7d_count"] == 0
    assert av["retripped_within_48h_count"] == 0


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_includes_single_vs_multi_cohort_stats(
    client: AsyncClient, db_session
):
    """Endpoint forwards single_layer_* / multi_layer_* cohort fields per row."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:cohort",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            # single-layer CI_WORKFLOW fix, recovered hold=2d
            {
                "fingerprint": "fp-single",
                "merged_at": (now - timedelta(days=10)).isoformat(),
                "recovered_at": (now - timedelta(days=8)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW"],
            },
            # multi-layer CI_WORKFLOW + BASELINE_GATE fix, recovered hold=10d
            {
                "fingerprint": "fp-multi",
                "merged_at": (now - timedelta(days=20)).isoformat(),
                "recovered_at": (now - timedelta(days=10)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW", "BASELINE_GATE"],
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    by_layer = {r["layer"]: r for r in body["layers"]}

    for row in body["layers"]:
        assert "single_layer_fix_count" in row
        assert "single_layer_avg_hold_days" in row
        assert "multi_layer_fix_count" in row
        assert "multi_layer_avg_hold_days" in row

    ci = by_layer["CI_WORKFLOW"]
    assert ci["fix_count"] == 2
    assert ci["single_layer_fix_count"] == 1
    assert ci["single_layer_avg_hold_days"] is not None
    assert abs(ci["single_layer_avg_hold_days"] - 2.0) < 0.1
    assert ci["multi_layer_fix_count"] == 1
    assert ci["multi_layer_avg_hold_days"] is not None
    assert abs(ci["multi_layer_avg_hold_days"] - 10.0) < 0.1

    bg = by_layer["BASELINE_GATE"]
    assert bg["fix_count"] == 1
    assert bg["single_layer_fix_count"] == 0
    assert bg["single_layer_avg_hold_days"] is None
    assert bg["multi_layer_fix_count"] == 1
    assert bg["multi_layer_avg_hold_days"] is not None
    assert abs(bg["multi_layer_avg_hold_days"] - 10.0) < 0.1

    av = by_layer["AGENT_VM"]
    assert av["single_layer_fix_count"] == 0
    assert av["single_layer_avg_hold_days"] is None
    assert av["multi_layer_fix_count"] == 0
    assert av["multi_layer_avg_hold_days"] is None


@pytest.mark.asyncio
async def test_layer_defense_density_endpoint_fragility_held_gt_7d(
    client: AsyncClient, db_session
):
    """held_gt_7d_count increments for still-holding entries older than 7 days."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="rc:held",
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "old",
                "merged_at": (now - timedelta(days=20)).isoformat(),
                "layers_targeted": ["REVIEWER_PASS"],
            },
            {
                "fingerprint": "recent",
                "merged_at": (now - timedelta(days=3)).isoformat(),
                "layers_targeted": ["REVIEWER_PASS"],
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    response = await client.get(
        "/api/admin/regression-cases/layer-defense-density",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    by_layer = {r["layer"]: r for r in response.json()["layers"]}
    rp = by_layer["REVIEWER_PASS"]
    assert rp["still_holding_count"] == 2
    assert rp["held_gt_7d_count"] == 1
    assert rp["avg_hold_days"] is None


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


def test_layer_coverage_by_class_basic():
    rows = [
        _make_regression_case(
            "baseline_drift",
            fix_log=[
                {"layers_targeted": ["AGENT_VM", "AGENT_VM", "CI_WORKFLOW"]},
                {"layers_targeted": ["AGENT_VM"]},
                {"layers_targeted": ["AGENT_VM"]},
            ],
        ),
    ]
    out = _compute_layer_coverage_by_class(rows)
    assert "baseline_drift" in out
    entry = out["baseline_drift"]
    assert entry["total_fixes_with_layers"] == 3
    combos = entry["combinations"]
    # [AGENT_VM] appears 2x, [AGENT_VM, CI_WORKFLOW] appears 1x; sorted by count desc
    assert combos[0] == {"layers": ["AGENT_VM"], "count": 2}
    assert combos[1] == {"layers": ["AGENT_VM", "CI_WORKFLOW"], "count": 1}


def test_layer_coverage_by_class_no_layer_bucket():
    rows = [
        _make_regression_case(
            "flaky_ci",
            fix_log=[
                {"layers_targeted": []},  # doc-only fix
                {"layers_targeted": ["CI_WORKFLOW"]},
            ],
        ),
    ]
    out = _compute_layer_coverage_by_class(rows)
    entry = out["flaky_ci"]
    assert entry["total_fixes_with_layers"] == 2
    no_layer = next(c for c in entry["combinations"] if c["layers"] == [])
    assert no_layer["count"] == 1


def test_layer_coverage_by_class_skips_unclassified():
    rows = [
        _make_regression_case(
            "flaky_ci",
            fix_log=[{"merged_at": "2024-01-01T00:00:00Z"}],  # no layers_targeted key
        ),
    ]
    out = _compute_layer_coverage_by_class(rows)
    assert "flaky_ci" not in out


def test_compute_symptom_layers_by_class_basic():
    rows = [
        _make_regression_case(
            "baseline_drift", symptoms={"CI_WORKFLOW": {}, "AGENT_VM": {}}
        ),
        _make_regression_case("baseline_drift", symptoms={"BASELINE_GATE": {}}),
        _make_regression_case("flaky_ci", symptoms={"CI_WORKFLOW": {}}),
    ]
    out = _compute_symptom_layers_by_class(rows)
    assert sorted(out["baseline_drift"]) == ["AGENT_VM", "BASELINE_GATE", "CI_WORKFLOW"]
    assert out["flaky_ci"] == ["CI_WORKFLOW"]


def test_compute_symptom_layers_by_class_empty_symptoms_skipped():
    rows = [
        _make_regression_case("no_symptoms", symptoms={}),
        _make_regression_case("no_symptoms_none", symptoms=None),
    ]
    out = _compute_symptom_layers_by_class(rows)
    assert out == {}


# ── GET /regression-cases/cross-layer-compliance ─────────────────────────────


def _make_cross_layer_case(project_id, regression_class, fix_log_entries):
    return RegressionRemediationCase(
        project_id=project_id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        fingerprint_to_fix_log=fix_log_entries,
    )


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


@pytest.mark.asyncio
async def test_cross_layer_compliance_compliant(client: AsyncClient, db_session):
    """Cross-layer spawn + fix touches >=2 layers => compliant."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=["CI_WORKFLOW", "AGENT_VM"])],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 1
    assert body["compliant"] == 1
    assert body["non_compliant"] == 0
    assert body["compliance_rate"] == 1.0
    assert body["awaiting_backfill_count"] == 0
    assert body["by_class"]["sr_catch_rate"]["compliant"] == 1


@pytest.mark.asyncio
async def test_cross_layer_compliance_non_compliant_one_layer(
    client: AsyncClient, db_session
):
    """Cross-layer spawn + fix touches only 1 layer => non-compliant."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=["CI_WORKFLOW"])],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 1
    assert body["compliant"] == 0
    assert body["non_compliant"] == 1
    assert body["compliance_rate"] == 0.0
    assert body["by_class"]["sr_catch_rate"]["non_compliant"] == 1


@pytest.mark.asyncio
async def test_cross_layer_compliance_awaiting_backfill_excluded(
    client: AsyncClient, db_session
):
    """Entry without layers_targeted key => excluded from numerator/denominator."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Entry has no layers_targeted key (awaiting backfill).
    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=None)],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 0
    assert body["awaiting_backfill_count"] == 1
    assert body["compliance_rate"] is None


@pytest.mark.asyncio
async def test_cross_layer_compliance_non_cross_layer_excluded(
    client: AsyncClient, db_session
):
    """Fix-log entry without spawn_was_cross_layer => not counted in any bucket."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    entry = {
        "fingerprint": "sr_catch_rate",
        "fix_pr_number": 200,
        "merged_at": now.isoformat(),
        "layers_targeted": ["CI_WORKFLOW", "AGENT_VM"],
        # no spawn_was_cross_layer field
    }
    case = _make_cross_layer_case(project.id, "sr_catch_rate", [entry])
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 0
    assert body["awaiting_backfill_count"] == 0
    assert body["compliance_rate"] is None


@pytest.mark.asyncio
async def test_cross_layer_compliance_doc_test_only_failure_mode(
    client: AsyncClient, db_session
):
    """Cross-layer spawn + zero canonical layers => doc_test_only failure mode."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=[])],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 1
    assert body["non_compliant"] == 1
    assert body["failure_modes"]["doc_test_only"] == 1


@pytest.mark.asyncio
async def test_cross_layer_compliance_most_recent_layer_only_failure_mode(
    client: AsyncClient, db_session
):
    """Cross-layer spawn + fix only touches last spawn layer => most_recent_layer_only."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # spawn_layers_enumerated has AGENT_VM last; fix only touches AGENT_VM.
    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [
            _cross_layer_entry(
                layers_targeted=["AGENT_VM"],
                spawn_layers=["CI_WORKFLOW", "AGENT_VM"],
            )
        ],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_classified"] == 1
    assert body["non_compliant"] == 1
    assert body["failure_modes"]["most_recent_layer_only"] == 1


# ── recurrence_by_bucket in cross-layer-compliance ──────────────────────────


@pytest.mark.asyncio
async def test_cross_layer_recurrence_no_retrip(client: AsyncClient, db_session):
    """Compliant fix with no subsequent fix → retripped_count=0, recurrence_rate=0."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=["CI_WORKFLOW", "AGENT_VM"], days_ago=5)],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    rb = body["recurrence_by_bucket"]
    assert rb["compliant"]["merged_count"] == 1
    assert rb["compliant"]["retripped_count"] == 0
    assert rb["compliant"]["recurrence_rate"] == 0.0
    assert rb["compliant"]["median_hold_days"] is None


@pytest.mark.asyncio
async def test_cross_layer_recurrence_retripped_within_window(
    client: AsyncClient, db_session
):
    """Compliant fix followed by another fix within 14 days → retripped_count=1."""

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    first_fix = {
        "fingerprint": "sr_catch_rate",
        "fix_pr_number": 101,
        "merged_at": (now - timedelta(days=10)).isoformat(),
        "spawn_was_cross_layer": True,
        "spawn_layers_enumerated": ["CI_WORKFLOW", "AGENT_VM"],
        "layers_targeted": ["CI_WORKFLOW", "AGENT_VM"],
    }
    # Re-trip fix 8 days after first fix
    second_fix = {
        "fingerprint": "sr_catch_rate",
        "fix_pr_number": 102,
        "merged_at": (now - timedelta(days=2)).isoformat(),
        "spawn_was_cross_layer": True,
        "spawn_layers_enumerated": ["CI_WORKFLOW", "AGENT_VM"],
        "layers_targeted": ["CI_WORKFLOW", "AGENT_VM"],
    }
    case = _make_cross_layer_case(project.id, "sr_catch_rate", [first_fix, second_fix])
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    rb = body["recurrence_by_bucket"]
    assert rb["compliant"]["merged_count"] == 2
    assert rb["compliant"]["retripped_count"] == 1
    assert rb["compliant"]["recurrence_rate"] == 0.5
    assert rb["compliant"]["median_hold_days"] is not None
    assert rb["compliant"]["median_hold_days"] > 0


@pytest.mark.asyncio
async def test_cross_layer_recurrence_doc_test_only_bucket(
    client: AsyncClient, db_session
):
    """Non-compliant doc_test_only fix tracked separately from compliant bucket."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=[], days_ago=3)],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    rb = body["recurrence_by_bucket"]
    assert rb["doc_test_only"]["merged_count"] == 1
    assert rb["doc_test_only"]["retripped_count"] == 0
    assert rb["compliant"]["merged_count"] == 0
    assert rb["compliant"]["recurrence_rate"] is None


@pytest.mark.asyncio
async def test_cross_layer_compliance_new_additive_fields(
    client: AsyncClient, db_session
):
    """New additive fields are returned without changing existing awaiting_backfill_count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # One entry that is awaiting backfill and older than 24h (over threshold)
    old_awaiting_entry = _cross_layer_entry(layers_targeted=None, days_ago=3)
    # One entry that is awaiting backfill but recent (within 24h threshold)
    recent_awaiting_entry = {
        "fingerprint": "test_class",
        "fix_pr_number": 101,
        "merged_at": (now - timedelta(hours=2)).isoformat(),
        "spawn_was_cross_layer": True,
        "spawn_layers_enumerated": ["CI_WORKFLOW", "AGENT_VM"],
        # no layers_targeted
    }
    case = _make_cross_layer_case(
        project.id, "sr_catch_rate", [old_awaiting_entry, recent_awaiting_entry]
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/cross-layer-compliance",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()

    # Existing field is unchanged
    assert body["awaiting_backfill_count"] == 2

    # New additive fields
    assert body["awaiting_backfill_over_threshold_count"] == 1
    assert body["awaiting_backfill_oldest_merged_at"] is not None
    # Oldest merged_at should reflect the 3-days-ago entry
    oldest_dt = datetime.fromisoformat(body["awaiting_backfill_oldest_merged_at"])
    expected_oldest = now - timedelta(days=3)
    assert abs((oldest_dt - expected_oldest).total_seconds()) < 5


# ---------------------------------------------------------------------------
# GET /regression-cases/first-attempt-compliance-trend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_attempt_compliance_trend_first_attempt_compliant(
    client: AsyncClient, db_session
):
    """First fix touches >=2 canonical layers => first_attempt_compliant incremented."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=["CI_WORKFLOW", "AGENT_VM"], days_ago=3)],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/first-attempt-compliance-trend",
        params={"project_id": str(project.id), "num_windows": 6, "window_days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert "windows" in body
    assert body["num_windows"] == 6
    assert body["window_days"] == 7
    total_fa = sum(w["first_attempt_compliant"] for w in body["windows"])
    total_fr = sum(w["followup_rescued"] for w in body["windows"])
    assert total_fa == 1
    assert total_fr == 0
    # Rate should be 1.0 for the window containing this case
    rates = [w["rate"] for w in body["windows"] if w["rate"] is not None]
    assert any(r == 1.0 for r in rates)


@pytest.mark.asyncio
async def test_first_attempt_compliance_trend_followup_rescued(
    client: AsyncClient, db_session
):
    """First fix has <2 layers but followup spawned+merged => followup_rescued."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fix_log = [
        # First fix: only 1 layer (non-compliant)
        {
            "fix_pr_number": 1,
            "merged_at": (now - timedelta(days=3)).isoformat(),
            "spawn_was_cross_layer": True,
            "spawn_layers_enumerated": ["CI_WORKFLOW", "AGENT_VM"],
            "layers_targeted": ["CI_WORKFLOW"],
        },
        # Compliance followup marker
        {
            "type": "compliance_followup_spawned",
            "spawned_at": (now - timedelta(days=2)).isoformat(),
        },
        # Subsequent fix after followup
        {
            "fix_pr_number": 2,
            "merged_at": (now - timedelta(days=1)).isoformat(),
            "layers_targeted": ["CI_WORKFLOW", "AGENT_VM"],
        },
    ]
    case = _make_cross_layer_case(project.id, "sr_catch_rate", fix_log)
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/first-attempt-compliance-trend",
        params={"project_id": str(project.id), "num_windows": 6, "window_days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    total_fa = sum(w["first_attempt_compliant"] for w in body["windows"])
    total_fr = sum(w["followup_rescued"] for w in body["windows"])
    assert total_fa == 0
    assert total_fr == 1


@pytest.mark.asyncio
async def test_first_attempt_compliance_trend_awaiting_backfill_excluded(
    client: AsyncClient, db_session
):
    """Entry without layers_targeted is awaiting_backfill and excluded from rate."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_case(
        project.id,
        "sr_catch_rate",
        [_cross_layer_entry(layers_targeted=None, days_ago=3)],
    )
    db_session.add(case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-cases/first-attempt-compliance-trend",
        params={"project_id": str(project.id), "num_windows": 6, "window_days": 7},
    )
    assert response.status_code == 200
    body = response.json()
    total_fa = sum(w["first_attempt_compliant"] for w in body["windows"])
    total_fr = sum(w["followup_rescued"] for w in body["windows"])
    total_ab = sum(w["awaiting_backfill"] for w in body["windows"])
    assert total_fa == 0
    assert total_fr == 0
    assert total_ab == 1
    # Rate is None when denominator is 0
    assert all(w["rate"] is None for w in body["windows"])


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
        "app.routers.admin_digest._scan_viewport_debt_rows",
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
        "app.routers.admin_digest._scan_viewport_debt_rows",
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


@pytest.mark.asyncio
async def test_viewport_debt_counts_assertion_type_regressions(
    client: AsyncClient, db_session
):
    """GET /viewport-debt counts regressions with new assertion_type format."""
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
            "component": "admin-ui/src/components/TaskCard.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 5,
            "has_viewport_test": True,
            "regression_case_count": 0,
            "source_url": None,
        },
        {
            "component": "admin-ui/src/components/MessagesPanel.tsx",
            "eslint_disable_count": 1,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 3,
            "has_viewport_test": True,
            "regression_case_count": 0,
            "source_url": None,
        },
    ]

    with patch(
        "app.routers.admin_digest._scan_viewport_debt_rows",
        return_value=(
            fake_rows,
            [],
            {"total_components": 2, "untested_count": 0, "untested_components": []},
        ),
    ):
        # Create regression cases in both old and new format
        case1 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard.tsx@375",  # old format
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        case2 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard:text_truncation@375",  # new format
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        case3 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:MessagesPanel:overflow@768",  # new format, different width
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        db_session.add(case1)
        db_session.add(case2)
        db_session.add(case3)
        await db_session.commit()

        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    rows = body["rows"]

    # Find TaskCard and MessagesPanel rows
    task_card_row = next((r for r in rows if "TaskCard" in r["component"]), None)
    messages_panel_row = next(
        (r for r in rows if "MessagesPanel" in r["component"]), None
    )

    assert task_card_row is not None, "TaskCard row not found"
    assert task_card_row["regression_case_count"] == 2  # old format + new format

    assert messages_panel_row is not None, "MessagesPanel row not found"
    assert messages_panel_row["regression_case_count"] == 1  # new format


@pytest.mark.asyncio
async def test_viewport_debt_returns_per_assertion_type_breakdown(
    client: AsyncClient, db_session
):
    """GET /viewport-debt includes regression_case_breakdown keyed by assertion type."""
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
            "component": "admin-ui/src/components/TaskCard.tsx",
            "eslint_disable_count": 0,
            "data_viewport_allow_small_target": 0,
            "data_viewport_allow_overflow": 0,
            "data_viewport_allow_truncation": 0,
            "total_opt_outs": 1,
            "has_viewport_test": True,
            "regression_case_count": 0,
            "source_url": None,
        },
    ]

    with patch(
        "app.routers.admin_digest._scan_viewport_debt_rows",
        return_value=(
            fake_rows,
            [],
            {"total_components": 1, "untested_count": 0, "untested_components": []},
        ),
    ):
        # Two overflow cases and one truncation case for TaskCard
        case1 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard:overflow@375",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        case2 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard:overflow@768",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        case3 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard:truncation@375",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        # Old-format case counts as "unknown" assertion type
        case4 = RegressionRemediationCase(
            project_id=project.id,
            regression_class="viewport_regression:TaskCard.tsx@375",
            status=RegressionCaseStatus.ACTIVE,
            first_seen_at=_utcnow(),
        )
        db_session.add_all([case1, case2, case3, case4])
        await db_session.commit()

        response = await client.get(f"/api/admin/projects/{project.id}/viewport-debt")

    assert response.status_code == 200
    body = response.json()
    row = next((r for r in body["rows"] if "TaskCard" in r["component"]), None)
    assert row is not None, "TaskCard row not found"
    assert row["regression_case_count"] == 4
    breakdown = row["regression_case_breakdown"]
    assert breakdown["overflow"] == 2
    assert breakdown["truncation"] == 1
    assert breakdown["unknown"] == 1


# ---------------------------------------------------------------------------
# Acknowledge endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_regression_case_stamps_timestamp(
    client: AsyncClient, db_session
):
    """POST /regression-cases/{case_id}/acknowledge stamps operator_acknowledged_at."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        needs_manual_intervention_at=_utcnow(),
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    assert case.operator_acknowledged_at is None

    response = await client.post(
        f"/api/admin/regression-cases/{case.id}/acknowledge",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(case.id)
    assert body["operator_acknowledged_at"] is not None

    await db_session.refresh(case)
    assert case.operator_acknowledged_at is not None


@pytest.mark.asyncio
async def test_acknowledge_regression_case_idempotent(client: AsyncClient, db_session):
    """POST /regression-cases/{case_id}/acknowledge is idempotent — second call preserves original timestamp."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_ack = _utcnow() - timedelta(hours=1)
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:architecture",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        needs_manual_intervention_at=_utcnow() - timedelta(hours=2),
        operator_acknowledged_at=original_ack,
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    response = await client.post(
        f"/api/admin/regression-cases/{case.id}/acknowledge",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    returned_at = datetime.fromisoformat(body["operator_acknowledged_at"])
    assert abs((returned_at - original_ack).total_seconds()) < 2


@pytest.mark.asyncio
async def test_acknowledge_regression_case_not_found(client: AsyncClient, db_session):
    """POST /regression-cases/{case_id}/acknowledge returns 404 for unknown case."""
    import uuid

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.post(
        f"/api/admin/regression-cases/{uuid.uuid4()}/acknowledge",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# /api/admin/regression-dispatcher/escalation-outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regression_escalation_outcomes_baseline_vs_compliance_followup(
    client: AsyncClient, db_session
):
    """Baseline cross-layer fix and compliance-followup fix bucket separately with independent prevention rates."""
    from app.pipeline_regression_dispatcher import (
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        _ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    baseline_merged = (now - timedelta(days=35)).isoformat()
    cf_original_merged = (now - timedelta(days=50)).isoformat()
    cf_followup_merged = (now - timedelta(days=35)).isoformat()

    # Baseline: compliant multi-layer fix, no compliance-followup marker → escalated_cross_layer bucket
    baseline_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        escalation_reason=ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        first_seen_at=now - timedelta(days=55),
        fingerprint_to_fix_log=[
            {
                "fix_pr_number": 1,
                "merged_at": baseline_merged,
                "layers_targeted": ["BACKEND", "FRONTEND"],
            },
        ],
    )
    db_session.add(baseline_case)

    # Compliance-followup: original non-compliant fix + spawned marker + followup fix → compliance_followup bucket
    cf_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:architecture",
        status=RegressionCaseStatus.ACTIVE,
        escalation_reason=ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        first_seen_at=now - timedelta(days=55),
        fingerprint_to_fix_log=[
            {
                "fix_pr_number": 2,
                "merged_at": cf_original_merged,
                "layers_targeted": ["BACKEND"],
            },
            {
                "type": "compliance_followup_spawned",
                "spawned_at": (now - timedelta(days=42)).isoformat(),
            },
            {
                "fix_pr_number": 3,
                "merged_at": cf_followup_merged,
                "layers_targeted": ["BACKEND", "FRONTEND"],
            },
        ],
    )
    db_session.add(cf_case)
    await db_session.commit()

    response = await client.get(
        "/api/admin/regression-dispatcher/escalation-outcomes",
        params={"project_id": str(project.id), "lookback_days": 60},
    )
    assert response.status_code == 200
    by = response.json()["by_reason"]

    # Baseline fix stays in cross_layer bucket with durable prevention
    b_cross = by[ESCALATION_REASON_ESCALATED_CROSS_LAYER]
    assert b_cross["merged_count"] == 1
    assert (
        b_cross["prevention_rate_30d"] == 1.0
    )  # no retrip within 30d of baseline merge

    # Compliance-followup fix lands in its own bucket with independent prevention rate
    b_cf = by[_ESCALATION_BUCKET_COMPLIANCE_FOLLOWUP]
    assert b_cf["merged_count"] == 1
    assert b_cf["prevention_rate_30d"] == 1.0  # cf fix held, no retrip within 30d


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
    assert qual["blocker"] is None

    assert data["recent_versions"] == []


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
