"""Integration tests for /api/admin/digest and /api/admin/activity endpoints."""
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import TaskStatus
from testing.factories import make_project, make_task, make_membership, _utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def unauthenticated_client(override_get_session):
    """Client with no auth override — useful for testing 401/403 responses."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
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

    entries = [c for c in data["recent_completions"] if c["title"] == "Scoped Completed Task"]
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
async def test_digest_aggregates_merge_retries_saved(client: AsyncClient, db_session):
    """total_merge_retries_saved sums _merge_retries_saved across completed+failed tasks;
    merge_early_exit_count counts tasks with a positive saved value."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = make_task(project_id=project.id, status=TaskStatus.FAILED, context={"_merge_retries_saved": 3})
    t2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED, context={"_merge_retries_saved": 2})
    t3 = make_task(project_id=project.id, status=TaskStatus.FAILED, context={"_merge_retries_saved": 0})
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
async def test_digest_failed_task_direction_attribution(client: AsyncClient, db_session):
    """Failed tasks with direction_id context expose direction_id/direction_text in needs_attention, recent_failures, and failure_groups."""
    project = make_project(
        directions=[
            {"id": "dir-f1", "text": "Harden reliability", "status": "active"}
        ]
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
        latest_status_message="quota exceeded for region us-central1",
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
        latest_status_message="quota exceeded for zone us-central1-a",
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
async def test_digest_recent_failures_includes_failure_class_and_scope(client: AsyncClient, db_session):
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
        directions=[
            {"id": "dir-rc", "text": "Reliability", "status": "active"}
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two failures with the same root cause but differing PR numbers — should
    # normalize to the same fingerprint.
    db_session.add(make_task(
        project_id=project.id,
        title="Fail A",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-rc"},
        latest_status_message="Merge conflict on PR #1234",
    ))
    db_session.add(make_task(
        project_id=project.id,
        title="Fail B",
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix"}],
        context={"direction_id": "dir-rc"},
        latest_status_message="Merge conflict on PR #5678",
    ))
    # Singleton — should be excluded from clusters.
    db_session.add(make_task(
        project_id=project.id,
        title="Fail C",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        latest_status_message="Unique failure: disk full",
    ))
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
async def test_digest_recurring_error_clusters_empty_when_all_unique(client: AsyncClient, db_session):
    """If every failed task has a distinct fingerprint, recurring_error_clusters is empty."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i, msg in enumerate([
        "TypeError: foo is undefined",
        "Rate limit exceeded for region us-west",
        "SyntaxError: unexpected token in parser",
    ]):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Unique {i}",
            status=TaskStatus.FAILED,
            latest_status_message=msg,
        ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["recurring_error_clusters"] == []


@pytest.mark.asyncio
async def test_digest_approval_task_direction_attribution(client: AsyncClient, db_session):
    """Approval-pending tasks with direction_id context expose direction attribution in needs_attention."""
    project = make_project(
        directions=[
            {"id": "dir-a1", "text": "Approval direction", "status": "active"}
        ]
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

    approval_items = [a for a in data["needs_attention"] if a["reason"] == "approval_required"]
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

    approval_items = [a for a in data["needs_attention"] if a["reason"] == "approval_required"]
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
    project = make_project(directions=[{"id": "d-1", "text": "Improve perf", "status": "active"}])
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
async def test_digest_active_tasks_includes_review_iteration(client: AsyncClient, db_session):
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
async def test_digest_includes_long_running_completed_task(client: AsyncClient, db_session):
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
async def test_activity_failed_includes_failure_class_and_taxonomy(client: AsyncClient, db_session):
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
async def test_digest_direction_progress_excludes_inactive(client: AsyncClient, db_session):
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
        db_session.add(make_task(
            project_id=project.id,
            title=f"Task for {did}",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": did},
        ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert "dir-active" in ids
    assert "dir-inactive" not in ids


@pytest.mark.asyncio
async def test_digest_direction_progress_includes_held_and_paused(client: AsyncClient, db_session):
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
        db_session.add(make_task(
            project_id=project.id,
            title=f"Task for {did}",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": did},
        ))
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
async def test_digest_direction_progress_excludes_no_activity(client: AsyncClient, db_session):
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
    db_session.add(make_task(
        project_id=project.id,
        title="Task for busy",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-busy"},
    ))
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
    window_start = _utcnow() - timedelta(hours=24)
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction with PRs",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 3},   # inside window
                    {"timestamp": old_ts, "prs_merged": 10},      # outside window
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    assert data["direction_progress"][0]["prs_merged"] == 3  # only the recent entry


@pytest.mark.asyncio
async def test_digest_direction_progress_review_quality(client: AsyncClient, db_session):
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
                    {"timestamp": recent_ts, "reviews_dismissed": 2, "reviews_overridden": 1, "user_issues_caught": 3},
                    {"timestamp": recent_ts2, "reviews_dismissed": 1, "reviews_overridden": 0, "user_issues_caught": 2},
                    {"timestamp": old_ts, "reviews_dismissed": 99, "reviews_overridden": 99, "user_issues_caught": 99},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-rq"},
    ))
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
async def test_digest_direction_progress_lifetime_metrics(client: AsyncClient, db_session):
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
                    {"timestamp": recent_ts, "prs_merged": 2, "prs_rejected": 1, "tasks_created": 5, "tasks_failed": 1},
                    {"timestamp": old_ts, "prs_merged": 1, "prs_rejected": 0, "tasks_created": 3, "tasks_failed": 2},
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-life"},
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["lifetime_cycles"] == 9  # 2 history entries + 7 aggregated
    assert dp["lifetime_prs_merged"] == 11  # 2 + 1 + 8
    assert dp["lifetime_prs_rejected"] == 5  # 1 + 0 + 4
    assert dp["lifetime_tasks_failed"] == 6  # 1 + 2 + 3
    assert dp["lifetime_conversion_rate"] == round(11 / 28, 3)  # 11 merged / (5 + 3 + 20) created


@pytest.mark.asyncio
async def test_digest_direction_progress_sorted_by_cycles(client: AsyncClient, db_session):
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
    db_session.add(make_task(
        project_id=project.id,
        title="Few task 1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-few"},
    ))
    for i in range(3):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Many task {i}",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-many"},
        ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert ids.index("dir-many") < ids.index("dir-few")


@pytest.mark.asyncio
async def test_digest_direction_progress_absent_without_project_id(client: AsyncClient, db_session):
    """direction_progress is empty when no project_id is supplied."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["direction_progress"] == []


@pytest.mark.asyncio
async def test_digest_direction_progress_taxonomy_and_failure_titles(client: AsyncClient, db_session):
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-tax"},
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-tax"

    # taxonomy counts summed across both in-window entries, old entry excluded
    assert dp["taxonomy_summary"] == {"scope_related": 3, "infra_related": 1, "code_defect": 3}

    # recent_failure_titles: last 3 titles from in-window failed_task_details
    titles = dp["recent_failure_titles"]
    assert titles is not None
    assert len(titles) == 3
    assert "old failed task" not in titles
    assert titles[-1] == "failed task D"


@pytest.mark.asyncio
async def test_digest_direction_progress_type_breakdown(client: AsyncClient, db_session):
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-tb"},
    ))
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
async def test_digest_direction_progress_decomposition_summary(client: AsyncClient, db_session):
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
                            {"plan_task_id": "p1", "subtask_count": 3, "subtask_merge_rate": 0.5, "cross_module_count": 1},
                            {"plan_task_id": "p2", "subtask_count": 5, "subtask_merge_rate": None, "cross_module_count": 0},
                        ],
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 1,
                        "decomposition_quality": [
                            {"plan_task_id": "p3", "subtask_count": 2, "subtask_merge_rate": 1.0, "cross_module_count": 3},
                        ],
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 0,
                        "decomposition_quality": [
                            {"plan_task_id": "p_old", "subtask_count": 99, "subtask_merge_rate": 0.0, "cross_module_count": 99},
                        ],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-dq"},
    ))
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
async def test_digest_direction_progress_taxonomy_absent_when_no_entries(client: AsyncClient, db_session):
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-plain"},
    ))
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-drop"},
    ))
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
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
    ))
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        ))

    # Current window: 3 completed + 1 failed = 0.75 success rate
    # delta = 0.75 - 0.25 = 0.50 > 0.05 → trend="improving" → "Good progress —"
    curr = _curr_ts()
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
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
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
    ))

    # Current window: 1 completed + 3 failed = 0.25 success rate
    # delta = 0.25 - 0.75 = -0.50 < -0.05 → trend="declining" → "Pipeline health declining —"
    curr = _curr_ts()
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
    ))
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        ))
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
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
    ))

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
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
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
    ))

    # Current window: 2 completed + 1 failed → forces standard narrative path
    # completed_delta = 2 - 1 = 1 > 0 → "(up from 1 last period)"
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
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
async def test_narrative_taxonomy_insight_dominant_category(client: AsyncClient, db_session):
    """When a taxonomy category dominates failures (>=50%), narrative appends an actionable hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Failed Task {i}",
            status=TaskStatus.FAILED,
        ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "infra_related"
    assert "Infrastructure issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_review_quality(client: AsyncClient, db_session):
    """CODE_ERROR failures with test/lint keywords → review_quality narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Lint Fail {i}",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_foo",
        ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failure_taxonomy_distribution"]["dominant_category"] == "review_quality"
    assert "Review quality issues dominate" in data["narrative_summary"]


@pytest.mark.asyncio
async def test_narrative_taxonomy_insight_scope_related(client: AsyncClient, db_session):
    """CODE_ERROR failures with expansion_ratio > 1.5 → scope_related narrative hint."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Scope Fail {i}",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed", "expansion_ratio": 2.5},
            latest_status_message="assertion error in handler",
        ))
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
        db_session.add(make_task(
            project_id=project.id,
            title=f"Defect {i}",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="null pointer exception in handler",
        ))
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
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        ))
    for _ in range(5):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        ))

    # Current window: 6 completed + 4 failed = 0.60 success rate → delta = 0.10 > 0.05
    curr = _curr_ts()
    for _ in range(6):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    for _ in range(4):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        ))
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
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        ))
    for _ in range(2):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
        ))

    # Current window: 7 completed + 3 failed = 0.70 success rate → delta = -0.10 < -0.05
    curr = _curr_ts()
    for _ in range(7):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.FAILED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        ))
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
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=prev, status_log=[{"t": prev.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=prev, status_log=[{"t": prev.isoformat(), "status": "FAILED"}],
    ))

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["trend"] == "stable"
    assert data["success_rate_delta"] == 0.0


@pytest.mark.asyncio
async def test_trend_none_when_no_previous_terminal_tasks(client: AsyncClient, db_session):
    """trend=None when the previous window has zero terminal tasks (can't compute prev_success_rate)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Current window only: 2 completed — no tasks in previous window
    curr = _curr_ts()
    for _ in range(2):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
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
    db_session.add(make_task(
        project_id=project.id,
        title="Rate Limited Task",
        status=TaskStatus.FAILED,
        latest_status_message="API Error: Rate limit reached",
        created_at=curr,
        status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "RATE_LIMIT" in failure_classes

    rate_limit_group = next(g for g in data["failure_groups"] if g["failure_class"] == "RATE_LIMIT")
    assert rate_limit_group["count"] == 1
    task_ids = [t["task_id"] for t in rate_limit_group["tasks"]]
    assert any("Rate Limited Task" in t.get("title", "") for t in rate_limit_group["tasks"])


@pytest.mark.asyncio
async def test_failure_group_provision_failure(client: AsyncClient, db_session):
    """Task with 'quota' in status message is classified into PROVISION_FAILURE failure group."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(make_task(
        project_id=project.id,
        title="Quota Exceeded Task",
        status=TaskStatus.FAILED,
        latest_status_message="quota exceeded for region us-central1",
        created_at=curr,
        status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "PROVISION_FAILURE" in failure_classes

    provision_group = next(g for g in data["failure_groups"] if g["failure_class"] == "PROVISION_FAILURE")
    assert provision_group["count"] == 1


@pytest.mark.asyncio
async def test_failure_group_code_error_default(client: AsyncClient, db_session):
    """Task with a generic error message is classified into CODE_ERROR failure group (default)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(make_task(
        project_id=project.id,
        title="Generic Error Task",
        status=TaskStatus.FAILED,
        latest_status_message="TypeError: cannot read property 'foo' of undefined",
        created_at=curr,
        status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    failure_classes = {g["failure_class"] for g in data["failure_groups"]}
    assert "CODE_ERROR" in failure_classes

    code_error_group = next(g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR")
    assert code_error_group["count"] == 1
    task_entry = code_error_group["tasks"][0]
    assert "taxonomy" in task_entry
    assert task_entry["taxonomy"] in {"scope_related", "review_quality", "code_defect", "infra_related"}


@pytest.mark.asyncio
async def test_failure_groups_sorted_by_count_descending(client: AsyncClient, db_session):
    """failure_groups are sorted by count descending (most frequent failure class first)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # 2 CODE_ERROR tasks (generic messages)
    for i in range(2):
        db_session.add(make_task(
            project_id=project.id,
            title=f"Code Error Task {i}",
            status=TaskStatus.FAILED,
            latest_status_message="SyntaxError: unexpected token",
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        ))
    # 1 RATE_LIMIT task
    db_session.add(make_task(
        project_id=project.id,
        title="Rate Limit Task",
        status=TaskStatus.FAILED,
        latest_status_message="API Error: Rate limit reached",
        created_at=curr,
        status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
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
        ("Cascade Task", "Cancelled: parent evaluation timeout", "cascade_cancellation"),
        ("Iter Task", "merge blocked, fix not spawned (iteration cap)", "iteration_limit"),
        ("Crash Task", "[FATAL:uncaughtException] boom", "process_crash"),
    ]
    for title, msg, _ in cases:
        db_session.add(make_task(
            project_id=project.id,
            title=title,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            created_at=curr,
            status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    timeout_group = next(g for g in data["failure_groups"] if g["failure_class"] == "AGENT_TIMEOUT")
    title_to_subtype = {t["title"]: t["timeout_subtype"] for t in timeout_group["tasks"]}
    for title, _, expected_subtype in cases:
        assert title_to_subtype[title] == expected_subtype, (title, title_to_subtype[title])

    rf_by_title = {f["title"]: f for f in data["recent_failures"]}
    for title, _, expected_subtype in cases:
        assert rf_by_title[title]["timeout_subtype"] == expected_subtype


@pytest.mark.asyncio
async def test_non_timeout_failures_have_null_timeout_subtype(client: AsyncClient, db_session):
    """Non-AGENT_TIMEOUT failures expose timeout_subtype=None."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    db_session.add(make_task(
        project_id=project.id,
        title="Code Error Task",
        status=TaskStatus.FAILED,
        latest_status_message="TypeError: something broke",
        created_at=curr,
        status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
    ))
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    code_group = next(g for g in data["failure_groups"] if g["failure_class"] == "CODE_ERROR")
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
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
    ))
    # One task awaiting approval
    db_session.add(make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
        created_at=curr,
    ))
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
async def test_narrative_all_clear_when_no_attention_needed(client: AsyncClient, db_session):
    """Narrative is a single 'All clear' sentence when only completions exist (no failures, no approvals)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    for _ in range(3):
        db_session.add(make_task(
            project_id=project.id, status=TaskStatus.COMPLETED,
            created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        ))
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
async def test_trend_history_includes_first_attempt_rate(client: AsyncClient, db_session):
    """trend_history entries include first_attempt_success_rate and first_attempt_sample_size."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add two completed tasks in the current window with _first_attempt_succeeded context
    curr = _curr_ts()
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        context={"_first_attempt_succeeded": True},
    ))
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
        context={"_first_attempt_succeeded": False},
    ))
    # A task without the metric — should not count toward sample
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.COMPLETED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "COMPLETED"}],
    ))
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
async def test_trend_history_includes_failed_by_taxonomy(client: AsyncClient, db_session):
    """trend_history entries include failed_by_taxonomy counts parallel to failed_by_class."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    curr = _curr_ts()
    # A review_quality failure (status message contains a keyword)
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        latest_status_message="pytest tests failed",
    ))
    # A scope_related failure (positive evidence: status-msg scope keyword)
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        latest_status_message="scope too large for task",
        context={"scope": "medium", "scope_calibrated_from": "small"},
    ))
    # A residual code_defect failure (no scope, no review keywords)
    db_session.add(make_task(
        project_id=project.id, status=TaskStatus.FAILED,
        created_at=curr, status_log=[{"t": curr.isoformat(), "status": "FAILED"}],
        latest_status_message="unexpected exception",
    ))
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
async def test_digest_scope_conversion_rates_aggregated(client: AsyncClient, db_session):
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
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
async def test_digest_scope_breakdown_preserves_failure_sub_counts(client: AsyncClient, db_session):
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
                                "created": 5, "merged": 1, "failed": 4,
                                "failed_infra": 1, "failed_code_error": 2, "failed_scope_overflow": 1,
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    sb = response.json()["direction_progress"][0]["scope_breakdown"]["small"]
    assert sb["failed_infra"] == 1
    assert sb["failed_code_error"] == 2
    assert sb["failed_scope_overflow"] == 1


@pytest.mark.asyncio
async def test_digest_scope_breakdown_includes_efficiency_predictors(client: AsyncClient, db_session):
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
                                "created": 3, "merged": 2, "failed": 1,
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
                                "created": 3, "merged": 2, "failed": 1,
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
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
async def test_digest_direction_progress_cost_by_scope_and_status(client: AsyncClient, db_session):
    """direction_progress entries include cost_by_scope and cost_by_status breakdowns."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: small/completed (on-demand, 3600s) and medium/failed (spot, 1800s)
    db_session.add(make_task(
        project_id=project.id,
        title="t1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1", "scope": "small"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    ))
    db_session.add(make_task(
        project_id=project.id,
        title="t2",
        status=TaskStatus.FAILED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1", "scope": "medium"},
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=True,
    ))
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
async def test_digest_direction_progress_cost_by_type(client: AsyncClient, db_session):
    """direction_progress entries include cost_by_type breakdown keyed by task type."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id,
        title="t1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1", "scope": "small"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    ))
    db_session.add(make_task(
        project_id=project.id,
        title="t2",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-1", "scope": "small"},
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=True,
    ))
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
    db_session.add(make_task(
        project_id=project.id,
        title="t1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1", "scope": "small"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    ))
    db_session.add(make_task(
        project_id=project.id,
        title="t2",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1", "scope": "medium"},
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=True,
    ))
    # Undirected spot task contributes to top-level split only
    db_session.add(make_task(
        project_id=project.id,
        title="t3",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={},
        vm_provisioned_at=now - timedelta(hours=3),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=True,
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    body = response.json()
    dp = body["direction_progress"][0]
    assert "spot_cost_usd" in dp
    assert "on_demand_cost_usd" in dp
    assert dp["spot_cost_usd"] > 0
    assert dp["on_demand_cost_usd"] > 0
    assert round(dp["spot_cost_usd"] + dp["on_demand_cost_usd"], 2) == dp["estimated_cost_usd"]

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

    db_session.add(make_task(
        project_id=project.id,
        title="timeout task",
        status=TaskStatus.FAILED,
        latest_status_message="",
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    ))
    db_session.add(make_task(
        project_id=project.id,
        title="external api task",
        status=TaskStatus.FAILED,
        latest_status_message="could not connect to server",
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=True,
    ))
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
async def test_digest_scope_conversion_rates_absent_without_project(client: AsyncClient, db_session):
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
    prev_ts = (_utcnow() - timedelta(hours=36)).isoformat()  # in prev 24h window (24–48h ago)

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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
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

    db_session.add(make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    ))
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
async def test_digest_pipeline_filter_funnel_sums_across_directions(client: AsyncClient, db_session):
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
        db_session.add(make_task(
            project_id=project.id,
            title=f"Research {did}",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": did},
        ))
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
    for i, (title, note) in enumerate([
        ("Task A", "Scope too broad; split into smaller PRs"),
        ("Task B", "scope too broad;   split into smaller prs"),
        ("Task C", "Missing tests for new logic"),
    ]):
        db_session.add(make_task(
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
        ))
    # Non-rejected failure should not count
    db_session.add(make_task(
        project_id=project.id,
        title="Unrelated failure",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-rej"},
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=10),
    ))
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

    db_session.add(make_task(
        project_id=project.id, title="c1", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-sm1", "scope": "small", "scope_calibrated_from": "medium"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="c2", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-sm1", "scope": "small", "scope_calibrated_from": "medium:cross_module"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="c3", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-sm1", "scope": "small"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="c4", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"direction_id": "dir-sm2", "scope": "medium", "scope_calibrated_from": "large"},
    ))
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

    db_session.add(make_task(
        project_id=project.id, title="nc", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}], context={"scope": "small"},
    ))
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
    db_session.add(make_task(
        project_id=project.id, title="r1", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
    ))
    db_session.add(make_task(
        project_id=project.id, title="r2", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
    ))
    # One task where retry changed class (not a repeat)
    db_session.add(make_task(
        project_id=project.id, title="r3", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_failure_class_history": ["PROVISION_FAILURE", "CODE_ERROR"]},
    ))
    # Single-entry history — should be ignored
    db_session.add(make_task(
        project_id=project.id, title="r4", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_failure_class_history": ["CODE_ERROR"]},
    ))
    # Completed task — should be ignored even with history
    db_session.add(make_task(
        project_id=project.id, title="r5", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"_failure_class_history": ["CODE_ERROR", "CODE_ERROR"]},
    ))
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

    db_session.add(make_task(
        project_id=project.id, title="c1", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "review_cap_exhausted", "review_cap_iterations": 16, "direction_id": "d-aaa"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="c2", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "review_cap_exhausted", "review_cap_iterations": 12, "direction_id": "d-aaa"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="c3", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "review_cap_exhausted", "review_cap_iterations": 8, "direction_id": "d-bbb"},
    ))
    # Unrelated FAILED task — should NOT be counted
    db_session.add(make_task(
        project_id=project.id, title="other", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "something_else", "direction_id": "d-ccc"},
    ))
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
async def test_digest_review_cap_exhaustion_summary_cost(client: AsyncClient, db_session):
    """review_cap_exhaustion_summary.total_cost_usd sums VM cost for cap-exhausted tasks."""
    from app.routers.admin_analytics import SPOT_RATE, ON_DEMAND_RATE
    from datetime import datetime, timezone
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.now(timezone.utc)
    # 1 hour spot task
    db_session.add(make_task(
        project_id=project.id, title="c1", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "review_cap_exhausted", "review_cap_iterations": 16, "direction_id": "d-aaa"},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=True,
    ))
    # 30 min on-demand task
    db_session.add(make_task(
        project_id=project.id, title="c2", status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_detail": "review_cap_exhausted", "review_cap_iterations": 12, "direction_id": "d-aaa"},
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=False,
    ))
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    summary = response.json()["review_cap_exhaustion_summary"]
    expected = SPOT_RATE * 3600 + ON_DEMAND_RATE * 1800
    assert summary["total_cost_usd"] == pytest.approx(round(expected, 4))
    assert summary["avg_cost_usd"] == pytest.approx(round(expected / 2, 4))


@pytest.mark.asyncio
async def test_digest_review_cap_exhaustion_summary_empty(client: AsyncClient, db_session):
    """review_cap_exhaustion_summary returns zero count and None avg when no cap exhaustion."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id, title="ok", status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    ))
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
async def test_failure_stage_distribution_aggregates_stages(client: AsyncClient, db_session):
    """failure_stage_distribution tallies _previous_failure_stage and falls back to _derive_failure_stage."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(make_task(
        project_id=project.id, title="ctx stage",
        status=TaskStatus.FAILED,
        context={"_previous_failure_stage": "test"},
    ))
    db_session.add(make_task(
        project_id=project.id, title="derived lint",
        status=TaskStatus.FAILED,
        latest_status_message="ruff lint error in handler",
    ))
    db_session.add(make_task(
        project_id=project.id, title="derived lint 2",
        status=TaskStatus.FAILED,
        latest_status_message="eslint failure",
    ))
    db_session.add(make_task(
        project_id=project.id, title="provision",
        status=TaskStatus.FAILED,
        context={"failure_category": "clone_failed"},
    ))
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
    db_session.add(make_task(
        project_id=project.id,
        title="retry on-demand",
        status=TaskStatus.FAILED,
        context={"_attempt_number": 2},
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),
        vm_spot=False,
    ))
    # Retry spot: attempt 3, 30m of VM time (in-window)
    db_session.add(make_task(
        project_id=project.id,
        title="retry spot",
        status=TaskStatus.FAILED,
        context={"_attempt_number": 3},
        vm_provisioned_at=now - timedelta(minutes=60),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=True,
    ))
    # First-attempt failure: must be excluded from retry counts
    db_session.add(make_task(
        project_id=project.id,
        title="first attempt",
        status=TaskStatus.FAILED,
        context={"_attempt_number": 1},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=False,
    ))
    # Retry with no VM timing: counts toward retry_task_count but not cost
    db_session.add(make_task(
        project_id=project.id,
        title="retry no-vm",
        status=TaskStatus.FAILED,
        context={"_attempt_number": 4},
    ))
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
async def test_digest_retry_recovery_cost_from_prior_attempts(client: AsyncClient, db_session):
    """retry_recovery_cost_usd sums _prior_attempts_cost across completed tasks
    with _attempt_number > 1 — the compute burned on failed attempts that
    eventually succeeded via retry."""
    now = _utcnow()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Successful retry with recorded prior-attempt cost
    db_session.add(make_task(
        project_id=project.id,
        title="succeeded on retry",
        status=TaskStatus.COMPLETED,
        context={"_attempt_number": 2, "_prior_attempts_cost": 0.50},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=False,
    ))
    # Successful retry with multi-attempt accumulated cost
    db_session.add(make_task(
        project_id=project.id,
        title="succeeded on 3rd",
        status=TaskStatus.COMPLETED,
        context={"_attempt_number": 3, "_prior_attempts_cost": 1.25},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=15),
        vm_spot=True,
    ))
    # First-attempt success — excluded (no prior cost)
    db_session.add(make_task(
        project_id=project.id,
        title="first attempt success",
        status=TaskStatus.COMPLETED,
        context={"_attempt_number": 1, "_prior_attempts_cost": 9.0},
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        vm_spot=False,
    ))
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
                    {"timestamp": recent_ts, "escalation_outcomes": {
                        "small_to_medium": {"attempted": 4, "merged": 2, "failed": 2},
                        "medium_to_large": {"attempted": 1, "merged": 0, "failed": 1},
                    }},
                    {"timestamp": old_ts, "escalation_outcomes": {
                        "small_to_medium": {"attempted": 99, "merged": 99, "failed": 0},
                    }},
                ],
            },
            {
                "id": "dir-e2",
                "text": "Dir 2",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts2, "escalation_outcomes": {
                        "small_to_medium": {"attempted": 2, "merged": 1, "failed": 1},
                        "small_to_large": {"attempted": 1, "merged": 1, "failed": 0},
                    }},
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
        "attempted": 6, "merged": 3, "failed": 3, "success_rate": 0.5,
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


@pytest.mark.asyncio
async def test_digest_cumulative_merged_by_trigger_class(client: AsyncClient, db_session):
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
        },
    )
    # In-window CI auto-remediation
    ci_in = make_task(
        project_id=project.id,
        title="ci in-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
        context={"auto_remediation": True, "trigger_failure_class": "LINT_ERROR"},
    )
    # Out-of-window CI auto-remediation
    ci_old = make_task(
        project_id=project.id,
        title="ci out-of-window",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(days=10),
        context={"auto_remediation": True, "trigger_failure_class": "LINT_ERROR"},
    )
    db_session.add_all([rem_in, rem_old, ci_in, ci_old])
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
    db_session.add_all([ci_merged, ci_failed])
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    ci = response.json()["remediation_funnel"]["ci_auto_remediation"]

    assert ci["spawned"] == 2
    assert ci["merged"] == 1
    assert ci["failed"] == 1
    assert ci["by_ci_failure_type"]["test"] == {"spawned": 1, "merged": 1, "failed": 0}
    assert ci["by_ci_failure_type"]["build"] == {"spawned": 1, "merged": 0, "failed": 1}


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
