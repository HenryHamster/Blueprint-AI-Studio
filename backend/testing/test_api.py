import json
from datetime import timezone
import uuid

import pytest
from httpx import AsyncClient

from app.models import (
    Artifact,
    ArtifactType,
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
)
from app.auth import create_agent_token


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_read_root(client: AsyncClient):
    """
    Tests the basic health check endpoint.
    """
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "online", "service": "Blueprint Orchestrator"}


@pytest.mark.asyncio
async def test_deploy_blueprint(client: AsyncClient, mocker):
    """
    Tests the POST /api/deploy endpoint.
    We mock the GCP Compute call so no real VM is spun up.
    """
    # Mock the background task so it doesn't actually try to provision a VM
    mocker.patch("app.main.provision_agent_vm", return_value=None)

    payload = {
        "name": "Test Project",
        "payloads": [{"type": "smoketest"}],
        "storage_uris": [],
    }

    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "project_id" in data

    # Verify it was saved in the isolated in-memory DB
    project_id = data["project_id"]
    get_response = await client.get(f"/api/projects/{project_id}")
    assert get_response.status_code == 200
    project_data = get_response.json()
    assert project_data["name"] == "Test Project"
    assert project_data["status"] == ProjectStatus.DEPLOYING


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_list_projects_empty(client: AsyncClient):
    """
    Tests that a fresh database returns an empty list of projects.
    """
    response = await client.get("/api/projects")
    assert response.status_code == 200
    data = response.json()
    assert data["projects"] == []
    assert "pagination" in data


@pytest.mark.asyncio
async def test_get_project_tasks(client: AsyncClient, mocker):
    """
    Tests retrieving tasks and artifacts for a specific project.
    """
    mocker.patch("app.main.provision_agent_vm", return_value=None)

    # 1. Create project
    payload = {
        "name": "Test Project Tasks",
        "payloads": [{"type": "smoketest"}],
        "storage_uris": [],
    }
    create_resp = await client.post("/api/deploy", json=payload)
    project_id = create_resp.json()["project_id"]

    # 2. Get tasks — deploy creates 1 parent task + 1 child task per payload
    resp = await client.get(f"/api/projects/{project_id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 2
    # The child task (has a parent_task_id) carries the validated payload
    child_tasks = [t for t in data["tasks"] if t["parent_task_id"] is not None]
    assert len(child_tasks) == 1
    assert child_tasks[0]["payloads"][0]["type"] == "smoketest"


@pytest.mark.asyncio
async def test_get_project_tasks_filter_by_direction_and_type(
    client: AsyncClient, db_session
):
    """Filter tasks by direction_id (JSONB context) and task_type (first payload)."""
    project = Project(
        name="Filter Test", user_id="test-user-123", status=ProjectStatus.DEPLOYING
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )

    t1 = Task(
        project_id=project.id,
        title="A",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "research"}],
        context={"direction_id": "d-1"},
    )
    t2 = Task(
        project_id=project.id,
        title="B",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        context={"direction_id": "d-1"},
    )
    t3 = Task(
        project_id=project.id,
        title="C",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "research"}],
        context={"direction_id": "d-2"},
    )
    t4 = Task(
        project_id=project.id,
        title="D",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        context={},
    )
    for t in (t1, t2, t3, t4):
        db_session.add(t)
    await db_session.commit()

    resp = await client.get(f"/api/projects/{project.id}/tasks?direction_id=d-1")
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["A", "B"]

    resp = await client.get(f"/api/projects/{project.id}/tasks?task_type=research")
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["A", "C"]

    resp = await client.get(
        f"/api/projects/{project.id}/tasks?direction_id=d-1&task_type=create"
    )
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["B"]


@pytest.mark.asyncio
async def test_get_project_tasks_hides_backend_managed_but_keeps_payloadless(
    client: AsyncClient, db_session
):
    project = Project(name="Task cleanliness", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    normal = Task(project_id=project.id, title="normal parent", payloads=[])
    shadow = Task(
        project_id=project.id,
        title="Resident operator turn (shadow)",
        payloads=[{"type": "strategic_resident_turn"}],
        archived=True,
    )
    db_session.add(normal)
    db_session.add(shadow)
    await db_session.commit()

    response = await client.get(f"/api/projects/{project.id}/tasks")
    assert response.status_code == 200
    # BACKEND_MANAGED_TASK_TYPES is now empty (M4 flipped strategic_resident_turn
    # to execution_owner=agent), so the default listing no longer hides the
    # archived resident-turn shadow row — it matches the include_backend_managed
    # view below. Compare as a set (PG created_at ties make ordering nondeterministic).
    assert {row["title"] for row in response.json()["tasks"]} == {
        "normal parent",
        "Resident operator turn (shadow)",
    }

    debug = await client.get(
        f"/api/projects/{project.id}/tasks?include_backend_managed=true"
    )
    assert {row["title"] for row in debug.json()["tasks"]} == {
        "normal parent",
        "Resident operator turn (shadow)",
    }


@pytest.mark.asyncio
async def test_get_project_tasks_includes_cost_fields(client: AsyncClient, db_session):
    """
    Completed tasks with VM timing data expose duration_seconds and
    estimated_cost_usd. Tasks with vm_provisioned_at but no stop time
    return None for duration_seconds and 0.0 for estimated_cost_usd
    (provisioning is evidence of cost; 0.0 = running but not yet accrued).
    Tasks with no VM evidence at all return None for both.
    """
    from datetime import datetime, timedelta, timezone

    project = Project(
        name="Cost Fields Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )

    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    stop = start + timedelta(seconds=3600)
    completed = Task(
        project_id=project.id,
        title="Completed",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_provisioned_at=start,
        vm_stopped_at=stop,
        vm_spot=False,
    )
    pending = Task(
        project_id=project.id,
        title="Pending",
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_provisioned_at=start,
        vm_stopped_at=None,
    )
    db_session.add_all([completed, pending])
    await db_session.commit()

    resp = await client.get(f"/api/projects/{project.id}/tasks")
    assert resp.status_code == 200
    by_title = {t["title"]: t for t in resp.json()["tasks"]}
    assert by_title["Completed"]["duration_seconds"] == pytest.approx(3600.0)
    assert by_title["Completed"]["estimated_cost_usd"] == pytest.approx(
        3600.0 * (0.067 / 3600)
    )
    assert by_title["Pending"]["duration_seconds"] is None
    # vm_provisioned_at is set → evidence of a VM, so 0.0 (not None); no stop time yet
    assert by_title["Pending"]["estimated_cost_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_get_project_tasks_not_found(client: AsyncClient):
    """
    Tests that requesting tasks for a non-existent project returns 404.
    """
    import uuid

    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/projects/{fake_id}/tasks")
    assert resp.status_code == 404


# --- Validation and Error Handling Tests ---


@pytest.mark.asyncio
async def test_deploy_missing_name_returns_422(client: AsyncClient):
    """
    POST /api/deploy without the required `name` field should return 422.
    """
    payload = {"payloads": [{"command": "echo hello"}]}
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deploy_null_name_returns_422(client: AsyncClient):
    """
    POST /api/deploy with `name` set to null should return 422 since name is required str.
    """
    payload = {"name": None, "payloads": [{"command": "echo hello"}]}
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_deploy_extra_fields_are_ignored(client: AsyncClient, mocker):
    """
    POST /api/deploy with extra unexpected fields — DeployPayload has no extra="forbid",
    so Pydantic v2 silently ignores unknown fields and the request should succeed.
    """
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    payload = {
        "name": "Test Extra Fields",
        "payloads": [{"type": "smoketest"}],
        "unexpected_field": "should be ignored",
        "another_extra": 42,
    }
    response = await client.post("/api/deploy", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert "project_id" in data


@pytest.mark.asyncio
async def test_get_project_different_user_returns_404(client: AsyncClient, db_session):
    """
    GET /api/projects/{id} where the project belongs to a different user_id should
    return 404 (not 403), because the app filters by user_id and treats it as not found.
    """
    other_project = Project(
        name="Other User's Project",
        user_id="other-user-456",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    response = await client.get(f"/api/projects/{other_project.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_project_tasks_different_user_returns_404(
    client: AsyncClient, db_session
):
    """
    GET /api/projects/{id}/tasks where the project belongs to a different user_id should
    return 404, since the app verifies project ownership before fetching tasks.
    """
    other_project = Project(
        name="Other User's Tasks Project",
        user_id="other-user-456",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    response = await client.get(f"/api/projects/{other_project.id}/tasks")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_projects_pagination_beyond_results(client: AsyncClient, db_session):
    """
    GET /api/projects?limit=5&offset=10 when the DB only has 3 projects for the current
    user should return an empty projects list with correct pagination metadata.
    """
    for i in range(3):
        project = Project(
            name=f"Pagination Project {i}",
            user_id="test-user-123",
            status=ProjectStatus.DEPLOYING,
        )
        db_session.add(project)
    await db_session.commit()

    response = await client.get("/api/projects?limit=5&offset=10")
    assert response.status_code == 200
    data = response.json()
    assert data["projects"] == []
    assert "pagination" in data
    assert data["pagination"]["limit"] == 5
    assert data["pagination"]["offset"] == 10
    assert data["pagination"]["count"] == 0


@pytest.mark.asyncio
async def test_list_projects_status_filter(client: AsyncClient, db_session):
    """
    GET /api/projects?status=COMPLETED should return only projects with COMPLETED status.
    """
    completed_project = Project(
        name="Completed Project",
        user_id="test-user-123",
        status=ProjectStatus.COMPLETED,
    )
    deploying_project = Project(
        name="Deploying Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    failed_project = Project(
        name="Failed Project", user_id="test-user-123", status=ProjectStatus.FAILED
    )
    db_session.add(completed_project)
    db_session.add(deploying_project)
    db_session.add(failed_project)
    await db_session.commit()
    for p in [completed_project, deploying_project, failed_project]:
        await db_session.refresh(p)
        db_session.add(
            ProjectMember(project_id=p.id, user_id="test-user-123", role="owner")
        )
    await db_session.commit()

    response = await client.get("/api/projects?status=COMPLETED")
    assert response.status_code == 200
    data = response.json()
    assert len(data["projects"]) == 1
    assert data["projects"][0]["name"] == "Completed Project"
    assert data["projects"][0]["status"] == ProjectStatus.COMPLETED


@pytest.mark.asyncio
async def test_ws_ticket_nonexistent_project_returns_404(client: AsyncClient):
    """
    POST /api/projects/{id}/ws-ticket on a non-existent project should return 404.
    """
    fake_id = str(uuid.uuid4())
    response = await client.post(f"/api/projects/{fake_id}/ws-ticket")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fix_task_receives_project_context(client: AsyncClient, db_session):
    """
    Claiming a fix-type task should include project_context in the response,
    the same as research/plan/create tasks.
    """
    project = Project(
        name="Fix Context Test Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "test-vm-fix-001"
    task = Task(
        project_id=project.id,
        title="Fix a bug",
        status=TaskStatus.QUEUED,
        vm_instance_id=vm_id,
        payloads=[{"type": "fix", "prompt": "Fix the null pointer exception"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), vm_id)
    response = await client.post(
        "/api/agent/claim",
        headers={"X-Agent-Token": token},
    )
    assert response.status_code == 200
    data = response.json()
    assert "project_context" in data
    assert "active_tasks" in data["project_context"]
    assert "sibling_tasks" in data["project_context"]
    assert "open_prs" in data["project_context"]


# --- Bulk Action Tests ---


async def _make_task(db_session, project, status, requires_approval=False):
    """Helper: create a task belonging to a project with test user as member."""
    task = Task(
        project_id=project.id,
        title="Test task",
        status=status,
        payloads=[{"type": "fix", "prompt": "do something"}],
        requires_approval=requires_approval,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_project_with_member(db_session):
    """Helper: create a project with test-user-123 as member."""
    project = Project(
        name="Bulk Test Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_admin_task_artifacts_expose_storage_uri(client: AsyncClient, db_session):
    """Storage-backed artifacts surface their gs:// URI additively in both the
    task-list and task-detail serializers, so operator surfaces can resolve a
    signed download. The field is always present (null for non-storage
    artifacts) and existing fields stay intact."""
    project = await _make_project_with_member(db_session)
    task = Task(
        project_id=project.id,
        title="screenshot task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.flush()
    storage_uri = f"gs://blueprint-agent-artifacts/tasks/{task.id}/plot.png"
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.SCREENSHOT,
            storage_uri=storage_uri,
        )
    )
    await db_session.commit()

    list_resp = await client.get("/api/admin/tasks", params={"limit": 100})
    assert list_resp.status_code == 200
    listed = next(t for t in list_resp.json()["tasks"] if t["id"] == str(task.id))
    list_arts = listed["artifacts"]
    assert len(list_arts) == 1
    assert list_arts[0]["storage_uri"] == storage_uri
    # Additive — existing fields remain.
    assert "artifact_type" in list_arts[0]

    detail_resp = await client.get(f"/api/admin/tasks/{task.id}")
    assert detail_resp.status_code == 200
    detail_arts = detail_resp.json()["artifacts"]
    assert detail_arts[0]["storage_uri"] == storage_uri


@pytest.mark.asyncio
async def test_admin_tasks_include_estimated_cost_usd(client: AsyncClient, db_session):
    """Both the list and detail admin task endpoints should expose estimated_cost_usd
    computed from VM provisioning timestamps and the spot/on-demand rate. Tasks with
    missing timing data must report None (never an implicit zero)."""
    from datetime import datetime, timedelta
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    project = await _make_project_with_member(db_session)

    started = datetime.now(timezone.utc).replace(tzinfo=None).replace(microsecond=0) - timedelta(hours=1)
    stopped = started + timedelta(seconds=3600)
    spot_task = Task(
        project_id=project.id,
        title="Completed spot task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        vm_provisioned_at=started,
        vm_stopped_at=stopped,
        vm_spot=True,
    )
    ondemand_task = Task(
        project_id=project.id,
        title="Completed on-demand task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "y"}],
        vm_provisioned_at=started,
        vm_stopped_at=stopped,
        vm_spot=False,
    )
    no_timing_task = Task(
        project_id=project.id,
        title="Task without VM timing",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "z"}],
    )
    db_session.add_all([spot_task, ondemand_task, no_timing_task])
    await db_session.commit()
    for t in (spot_task, ondemand_task, no_timing_task):
        await db_session.refresh(t)

    list_resp = await client.get("/api/admin/tasks", params={"limit": 100})
    assert list_resp.status_code == 200
    by_id = {t["id"]: t for t in list_resp.json()["tasks"]}
    assert by_id[str(spot_task.id)]["estimated_cost_usd"] == pytest.approx(
        3600 * SPOT_RATE
    )
    assert by_id[str(ondemand_task.id)]["estimated_cost_usd"] == pytest.approx(
        3600 * ON_DEMAND_RATE
    )
    assert by_id[str(no_timing_task.id)]["estimated_cost_usd"] is None

    detail_resp = await client.get(f"/api/admin/tasks/{spot_task.id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["estimated_cost_usd"] == pytest.approx(3600 * SPOT_RATE)

    detail_none = await client.get(f"/api/admin/tasks/{no_timing_task.id}")
    assert detail_none.status_code == 200
    assert detail_none.json()["estimated_cost_usd"] is None


@pytest.mark.asyncio
async def test_admin_tasks_expose_unverified_merge_reason(
    client: AsyncClient, db_session
):
    """Both the list and detail admin task endpoints expose a slim
    unverified_merge_reason mirrored from context.review_loop. A task whose merge
    proceeded UNVERIFIED returns the persisted reason key; a normal task returns
    None (so the drawer's Merge Test Gate section stays hidden)."""
    project = await _make_project_with_member(db_session)

    unverified_task = Task(
        project_id=project.id,
        title="UNVERIFIED merge task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        context={
            "review_loop": {
                "iteration": 2,
                "unverified_merge_reason": "no_detectable_test_command",
            }
        },
    )
    normal_task = Task(
        project_id=project.id,
        title="Normal merge task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "y"}],
        context={"review_loop": {"iteration": 1}},
    )
    db_session.add_all([unverified_task, normal_task])
    await db_session.commit()
    for t in (unverified_task, normal_task):
        await db_session.refresh(t)

    list_resp = await client.get("/api/admin/tasks", params={"limit": 100})
    assert list_resp.status_code == 200
    by_id = {t["id"]: t for t in list_resp.json()["tasks"]}
    assert (
        by_id[str(unverified_task.id)]["unverified_merge_reason"]
        == "no_detectable_test_command"
    )
    assert by_id[str(normal_task.id)]["unverified_merge_reason"] is None

    detail_resp = await client.get(f"/api/admin/tasks/{unverified_task.id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["unverified_merge_reason"] == "no_detectable_test_command"

    detail_normal = await client.get(f"/api/admin/tasks/{normal_task.id}")
    assert detail_normal.status_code == 200
    assert detail_normal.json()["unverified_merge_reason"] is None


@pytest.mark.asyncio
async def test_admin_tasks_expose_evaluation_summary(client: AsyncClient, db_session):
    """Both admin task endpoints expose normalized evaluation summaries for
    merge-gate, memo, self-audit, and audit-style task outputs."""
    project = await _make_project_with_member(db_session)

    verify_task = Task(
        project_id=project.id,
        title="Verify tests",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "verify_tests"}],
    )
    db_session.add(verify_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=verify_task.id,
            artifact_type=ArtifactType.MERGE_TEST_RESULT,
            logs=json.dumps(
                {
                    "detected": True,
                    "passed": True,
                    "tests": [
                        {"name": "pytest", "status": "pass", "message": "ok"},
                    ],
                    "output": "PASS all tests",
                }
            ),
        )
    )

    memo_task = Task(
        project_id=project.id,
        title="Memo task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "memo"}],
        plan_output={
            "memo": {
                "summary": "Cache strategy summary",
                "reasoning": "Long-form reasoning about the chosen cache strategy.",
                "citations": ["task:123", "direction:cache"],
                "tags": ["memo", "strategy"],
            }
        },
    )
    self_audit_task = Task(
        project_id=project.id,
        title="Self audit task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "self_audit"}],
        plan_output=[
            {
                "system_id": "auth",
                "score": 45,
                "recommended_mode": "audit",
                "top_issues": ["Missing tests"],
                "evidence": [],
                "components": {
                    "test_coverage": 20,
                    "friction_density": 40,
                    "claim_consistency": 50,
                    "failure_mix": 30,
                },
            },
            {
                "system_id": "billing",
                "score": 80,
                "recommended_mode": "ship",
                "top_issues": [],
                "evidence": [],
                "components": {
                    "test_coverage": 80,
                    "friction_density": 80,
                    "claim_consistency": 80,
                    "failure_mix": 80,
                },
            },
        ],
    )
    audit_task = Task(
        project_id=project.id,
        title="Security audit task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "security_audit"}],
        plan_output={
            "findings": [
                {
                    "severity": "high",
                    "title": "Missing auth check",
                    "description": "Endpoint is missing authorization.",
                },
                {
                    "severity": "low",
                    "title": "Verbose logging",
                    "description": "Logs are noisy.",
                },
            ]
        },
    )
    # perf shares the registry's findings builder with security_audit — it
    # flows through without a dedicated admin.py branch, proving registry-driven
    # dispatch handles new evaluator-backed types by registration alone.
    perf_task = Task(
        project_id=project.id,
        title="Perf audit task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "perf"}],
        plan_output={
            "findings": [
                {"severity": "low", "title": "Slow query", "description": "n+1"},
            ]
        },
    )
    db_session.add_all([memo_task, self_audit_task, audit_task, perf_task])
    await db_session.commit()
    for t in (verify_task, memo_task, self_audit_task, audit_task, perf_task):
        await db_session.refresh(t)

    list_resp = await client.get("/api/admin/tasks", params={"limit": 100})
    assert list_resp.status_code == 200
    by_id = {t["id"]: t for t in list_resp.json()["tasks"]}

    verify_summary = by_id[str(verify_task.id)]["evaluation_summary"]
    assert verify_summary["verdict"] == "pass"
    assert verify_summary["evidence"][0]["label"] == "Detected suite"
    assert verify_summary["highlights"][0]["label"] == "pytest"

    memo_summary = by_id[str(memo_task.id)]["evaluation_summary"]
    assert memo_summary["verdict"] == "memo"
    assert memo_summary["highlights"][0]["label"] == "Citations"
    assert memo_summary["evidence"][0]["value"] == "Cache strategy summary"

    self_audit_summary = by_id[str(self_audit_task.id)]["evaluation_summary"]
    assert self_audit_summary["verdict"] == "audit"
    assert self_audit_summary["evidence"][0]["label"] == "Systems"
    assert self_audit_summary["highlights"][0]["label"] == "auth"

    audit_summary = by_id[str(audit_task.id)]["evaluation_summary"]
    assert audit_summary["verdict"] == "actionable"
    assert audit_summary["evidence"][1]["value"] == "1"
    assert audit_summary["highlights"][0]["label"] == "Missing auth check"

    perf_summary = by_id[str(perf_task.id)]["evaluation_summary"]
    assert perf_summary["verdict"] == "clean"
    assert perf_summary["highlights"][0]["label"] == "Slow query"

    detail_resp = await client.get(f"/api/admin/tasks/{verify_task.id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["evaluation_summary"]["verdict"] == "pass"

    detail_memo = await client.get(f"/api/admin/tasks/{memo_task.id}")
    assert detail_memo.status_code == 200
    assert (
        detail_memo.json()["evaluation_summary"]["highlights"][0]["label"]
        == "Citations"
    )


@pytest.mark.asyncio
async def test_admin_tasks_filter_by_direction_and_type(
    client: AsyncClient, db_session
):
    """Admin /api/admin/tasks supports direction_id and task_type filters."""
    project = await _make_project_with_member(db_session)

    t1 = Task(
        project_id=project.id,
        title="A",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "research"}],
        context={"direction_id": "d-1"},
    )
    t2 = Task(
        project_id=project.id,
        title="B",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        context={"direction_id": "d-1"},
    )
    t3 = Task(
        project_id=project.id,
        title="C",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "research"}],
        context={"direction_id": "d-2"},
    )
    t4 = Task(
        project_id=project.id,
        title="D",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create"}],
        context={},
    )
    db_session.add_all([t1, t2, t3, t4])
    await db_session.commit()

    resp = await client.get(
        "/api/admin/tasks", params={"direction_id": "d-1", "limit": 100}
    )
    assert resp.status_code == 200
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["A", "B"]

    resp = await client.get(
        "/api/admin/tasks", params={"task_type": "research", "limit": 100}
    )
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["A", "C"]

    resp = await client.get(
        "/api/admin/tasks",
        params={"direction_id": "d-1", "task_type": "create", "limit": 100},
    )
    titles = sorted(t["title"] for t in resp.json()["tasks"])
    assert titles == ["B"]


@pytest.mark.asyncio
async def test_admin_tasks_filter_by_project_id(client: AsyncClient, db_session):
    """Admin /api/admin/tasks scopes results server-side when project_id is set."""
    project_a = await _make_project_with_member(db_session)
    project_b = await _make_project_with_member(db_session)

    db_session.add_all(
        [
            Task(
                project_id=project_a.id,
                title="A1",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "research"}],
            ),
            Task(
                project_id=project_a.id,
                title="A2",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create"}],
            ),
            Task(
                project_id=project_b.id,
                title="B1",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "research"}],
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        "/api/admin/tasks",
        params={"project_id": str(project_a.id), "limit": 100},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(t["title"] for t in body["tasks"]) == ["A1", "A2"]
    assert all(t["project_id"] == str(project_a.id) for t in body["tasks"])
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_bulk_action_invalid_action(client: AsyncClient):
    """POST /api/admin/tasks/bulk-action with unknown action returns 400."""
    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "delete", "task_ids": []},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bulk_action_retry_requeues_tasks(
    client: AsyncClient, db_session, mocker
):
    """Bulk retry sets status=QUEUED, assigns new vm_instance_id, and creates TaskHistory."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, TaskStatus.FAILED)
    task2 = await _make_task(db_session, project, TaskStatus.FAILED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "retry", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0
    assert all(r["status"] == "ok" for r in data["results"])

    # Verify task state via the admin GET endpoint (avoids SQLite session isolation)
    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    t1_data = get_resp.json()
    assert t1_data["status"] == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_bulk_action_approve_clears_approval_flag(
    client: AsyncClient, db_session
):
    """Bulk approve clears requires_approval and creates TaskHistory."""
    project = await _make_project_with_member(db_session)
    task1 = await _make_task(
        db_session, project, TaskStatus.QUEUED, requires_approval=True
    )
    task2 = await _make_task(
        db_session, project, TaskStatus.QUEUED, requires_approval=True
    )

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0

    # Verify via GET endpoint
    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    t1_data = get_resp.json()
    assert t1_data["requires_approval"] is False


@pytest.mark.asyncio
async def test_bulk_action_partial_errors(client: AsyncClient, db_session, mocker):
    """Invalid task IDs in the batch produce per-task errors without failing the whole request."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    valid_task = await _make_task(db_session, project, TaskStatus.FAILED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={
            "action": "retry",
            "task_ids": [str(valid_task.id), "not-a-uuid", str(uuid.uuid4())],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 1
    assert data["error_count"] == 2
    statuses = {r["task_id"]: r["status"] for r in data["results"]}
    assert statuses[str(valid_task.id)] == "ok"
    assert statuses["not-a-uuid"] == "error"


@pytest.mark.asyncio
async def test_bulk_action_cancel_sets_cancelled(
    client: AsyncClient, db_session, mocker
):
    """Bulk cancel sets status=CANCELLED and creates TaskHistory."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)
    mocker.patch("app.max_plan_pool.release_max_plan_account", return_value=None)

    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, TaskStatus.IN_PROGRESS)
    task2 = await _make_task(db_session, project, TaskStatus.QUEUED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 2
    assert data["error_count"] == 0

    get_resp = await client.get(f"/api/admin/tasks/{task1.id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_bulk_action_cancel_skips_terminal_tasks(
    client: AsyncClient, db_session, mocker
):
    """Bulk cancel returns error for already-terminal tasks."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", return_value=None)

    project = await _make_project_with_member(db_session)
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [str(task.id)]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success_count"] == 0
    assert data["error_count"] == 1
    assert "terminal" in data["results"][0]["error"]


@pytest.mark.asyncio
async def test_patch_clears_post_commit_pending(client: AsyncClient, db_session):
    """PATCH /api/admin/tasks/{id} with {"post_commit_pending": false} clears
    the column AND strips _post_commit_pending_meta from context, leaving an
    _post_commit_abandoned breadcrumb. Operator escape hatch for tasks the
    sweep can't recover from (row-lock contention preventing the bump).
    """
    project = await _make_project_with_member(db_session)
    task = Task(
        project_id=project.id,
        title="Stuck task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        post_commit_pending=True,
        post_commit_failed=False,
        context={
            "_post_commit_pending_meta": {
                "at": "2026-05-07T08:00:00",
                "reason": "post_commit_error",
                "attempts": 4,
                "payload": {"task_id": "x", "status": "COMPLETED", "logs": "{}"},
            },
            "pr_merged": True,  # unrelated context survives
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.patch(
        f"/api/admin/tasks/{task.id}",
        json={"post_commit_pending": False},
    )
    assert resp.status_code == 200, resp.text

    # Verify directly via db_session refresh — the GET admin endpoint
    # doesn't expose post_commit_pending/post_commit_failed in its response,
    # so we read the columns via the session.
    await db_session.refresh(task)
    assert task.post_commit_pending is False
    assert task.post_commit_failed is True
    ctx = task.context or {}
    assert "_post_commit_pending_meta" not in ctx, "pending meta should be stripped"
    abandoned = ctx.get("_post_commit_abandoned")
    assert abandoned is not None
    assert abandoned["by"] == "ADMIN_PATCH"
    assert abandoned["prior_attempts"] == 4
    assert abandoned["prior_reason"] == "post_commit_error"
    # Unrelated context survives
    assert ctx.get("pr_merged") is True
    # Status unchanged
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_get_project_tasks_banked_retry_cost(client: AsyncClient, db_session):
    """Retried tasks: banked retry VM spend must appear in estimated_cost_usd, not be
    silently dropped by reading only the current-attempt vm_provisioned_at window."""
    project = Project(
        name="BankedRetryProject",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    task = Task(
        project_id=project.id,
        title="Retried",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        vm_runtime_cost_banked_db=0.05,
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get(f"/api/projects/{project.id}/tasks")
    assert resp.status_code == 200
    by_title = {t["title"]: t for t in resp.json()["tasks"]}
    assert by_title["Retried"]["estimated_cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_admin_tasks_banked_retry_cost(client: AsyncClient, db_session):
    """Admin task list and detail must include banked retry VM spend in estimated_cost_usd;
    a task with both banked and current-attempt cost must expose the sum."""
    from datetime import datetime, timedelta
    from app.vm_cost_rates import get_vm_rate_per_second

    project = await _make_project_with_member(db_session)
    started = datetime.now(timezone.utc).replace(tzinfo=None).replace(microsecond=0) - timedelta(hours=1)
    stopped = started + timedelta(seconds=3600)

    banked_only = Task(
        project_id=project.id,
        title="BankedOnly",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        vm_runtime_cost_banked_db=0.07,
    )
    banked_plus_current = Task(
        project_id=project.id,
        title="BankedPlusCurrent",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "y"}],
        vm_runtime_cost_banked_db=0.03,
        vm_provisioned_at=started,
        vm_stopped_at=stopped,
        vm_spot=False,
    )
    db_session.add_all([banked_only, banked_plus_current])
    await db_session.commit()
    for t in (banked_only, banked_plus_current):
        await db_session.refresh(t)

    list_resp = await client.get("/api/admin/tasks", params={"limit": 100})
    assert list_resp.status_code == 200
    by_id = {t["id"]: t for t in list_resp.json()["tasks"]}
    assert by_id[str(banked_only.id)]["estimated_cost_usd"] == pytest.approx(0.07)
    assert by_id[str(banked_plus_current.id)]["estimated_cost_usd"] == pytest.approx(
        0.03 + 3600 * get_vm_rate_per_second(spot=False)
    )

    detail_resp = await client.get(f"/api/admin/tasks/{banked_only.id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["estimated_cost_usd"] == pytest.approx(0.07)


@pytest.mark.asyncio
async def test_patch_post_commit_pending_false_is_noop_when_already_false(
    client: AsyncClient, db_session
):
    """PATCHing post_commit_pending=False on a task where the column is
    already False is a no-op (no _post_commit_abandoned breadcrumb added)."""
    project = await _make_project_with_member(db_session)
    task = Task(
        project_id=project.id,
        title="Already clean task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        post_commit_pending=False,
        context={"some_key": "some_val"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.patch(
        f"/api/admin/tasks/{task.id}",
        json={"post_commit_pending": False},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert "_post_commit_abandoned" not in ctx, (
        "abandoned breadcrumb should NOT be added when column was already False"
    )
    assert ctx.get("some_key") == "some_val"


@pytest.mark.asyncio
async def test_admin_tasks_project_id_filter_returns_scoped_counts(
    client: AsyncClient, db_session
):
    """GET /api/admin/tasks?project_id=<id> returns scoped_active_count,
    scoped_evaluating_count, and scoped_queued_count reflecting only that
    project, while active_count remains the global fleet total."""
    # Project A — the scoped project
    project_a = Project(
        name="Project A",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        payloads=[{"type": "create"}],
    )
    db_session.add(project_a)
    await db_session.commit()
    await db_session.refresh(project_a)
    db_session.add(
        ProjectMember(project_id=project_a.id, user_id="test-user-123", role="owner")
    )

    # Project B — a second project owned by the same user
    project_b = Project(
        name="Project B",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        payloads=[{"type": "create"}],
    )
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_b)
    db_session.add(
        ProjectMember(project_id=project_b.id, user_id="test-user-123", role="owner")
    )

    # Tasks in project A
    task_a_active = Task(
        project_id=project_a.id,
        title="A active",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    task_a_evaluating = Task(
        project_id=project_a.id,
        title="A evaluating",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create"}],
    )
    # Task in project B (must NOT appear in scoped counts for project A)
    task_b_active = Task(
        project_id=project_b.id,
        title="B active",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    db_session.add_all([task_a_active, task_a_evaluating, task_b_active])
    await db_session.commit()

    resp = await client.get(
        "/api/admin/tasks",
        params={"project_id": str(project_a.id), "archived": "false"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Global counts include tasks from both member projects
    assert data["active_count"] >= 2, (
        "global active_count should include both member projects"
    )

    # Scoped counts are present and only reflect project A
    assert "scoped_active_count" in data
    assert "scoped_evaluating_count" in data
    assert "scoped_queued_count" in data
    assert data["scoped_active_count"] == 1
    assert data["scoped_evaluating_count"] == 1
    assert data["scoped_queued_count"] == 0


@pytest.mark.asyncio
async def test_admin_tasks_project_id_filter_hides_non_member_counts(
    client: AsyncClient, db_session
):
    """Non-members must not learn per-project counts through scoped fields."""
    project = Project(
        name="Private Project",
        user_id="other-user",
        status=ProjectStatus.DEPLOYING,
        payloads=[{"type": "create"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            Task(
                project_id=project.id,
                title="private active",
                status=TaskStatus.IN_PROGRESS,
                payloads=[{"type": "create"}],
            ),
            Task(
                project_id=project.id,
                title="private evaluating",
                status=TaskStatus.EVALUATING,
                payloads=[{"type": "create"}],
            ),
            Task(
                project_id=project.id,
                title="private queued",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create"}],
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/admin/tasks", params={"project_id": str(project.id)})
    assert resp.status_code == 200
    data = resp.json()

    assert data["tasks"] == []
    assert data["total"] == 0
    assert data["scoped_active_count"] == 0
    assert data["scoped_evaluating_count"] == 0
    assert data["scoped_queued_count"] == 0


@pytest.mark.asyncio
async def test_admin_tasks_no_project_id_omits_scoped_counts(
    client: AsyncClient, db_session
):
    """GET /api/admin/tasks without project_id must NOT include
    scoped_*_count fields — they are only present when a project is scoped."""
    project = await _make_project_with_member(db_session)
    db_session.add(
        Task(
            project_id=project.id,
            title="task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
        )
    )
    await db_session.commit()

    resp = await client.get("/api/admin/tasks", params={"archived": "false"})
    assert resp.status_code == 200
    data = resp.json()

    assert "active_count" in data
    assert "scoped_active_count" not in data
    assert "scoped_evaluating_count" not in data
    assert "scoped_queued_count" not in data


@pytest.mark.asyncio
async def test_task_counts_scoped_to_member_projects(client: AsyncClient, db_session):
    """Global counts only aggregate projects the caller is a member of.
    A non-member project's tasks must not appear in active_count or
    scoped counts, even when that project_id is passed as a filter."""
    # Caller's own project
    member_project = Project(
        name="Member Project",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        payloads=[{"type": "create"}],
    )
    db_session.add(member_project)
    await db_session.commit()
    await db_session.refresh(member_project)
    db_session.add(
        ProjectMember(
            project_id=member_project.id, user_id="test-user-123", role="owner"
        )
    )

    # A foreign project — a different user owns it, caller is NOT a member
    foreign_project = Project(
        name="Foreign Project",
        user_id="other-user-999",
        status=ProjectStatus.DEPLOYING,
        payloads=[{"type": "create"}],
    )
    db_session.add(foreign_project)
    await db_session.commit()
    await db_session.refresh(foreign_project)
    db_session.add(
        ProjectMember(
            project_id=foreign_project.id, user_id="other-user-999", role="owner"
        )
    )

    # One active task in the caller's project
    member_task = Task(
        project_id=member_project.id,
        title="member active",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    # One active task in the foreign project — must NOT appear in any counts
    foreign_task = Task(
        project_id=foreign_project.id,
        title="foreign active",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    db_session.add_all([member_task, foreign_task])
    await db_session.commit()

    # 1. Global counts without project_id: should only include member project
    resp = await client.get("/api/admin/tasks", params={"archived": "false"})
    assert resp.status_code == 200
    data = resp.json()
    # The member project has exactly 1 active task; foreign project must not contribute
    assert data["active_count"] >= 1, "member project tasks should be counted"
    # Verify foreign task is excluded by checking the task list excludes it
    task_ids = [t["id"] for t in data["tasks"]]
    assert str(foreign_task.id) not in task_ids, (
        "foreign task must not appear in task list"
    )

    # 2. Scoped counts for foreign project_id: non-member gets zero scoped counts
    resp2 = await client.get(
        "/api/admin/tasks",
        params={"project_id": str(foreign_project.id), "archived": "false"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["scoped_active_count"] == 0, (
        "non-member must see zero scoped_active_count"
    )
    assert data2["scoped_evaluating_count"] == 0
    assert data2["scoped_queued_count"] == 0


@pytest.mark.asyncio
async def test_project_task_context_filters_llm_runtime_keys(
    client: AsyncClient, db_session
):
    """User-facing task listing must not expose llm_* or max_plan_* context keys."""
    project = Project(
        name="ContextFilterTest",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )

    task = Task(
        project_id=project.id,
        title="RuntimeCtxTask",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "x"}],
        context={
            "direction_id": "d-abc",
            "llm_account_id": "acct-secret-uuid",
            "llm_provider": "anthropic",
            "llm_auth_mode": "oauth",
            "llm_model": "claude-sonnet-4-6",
            "llm_execution_mode": "max",
            "max_plan_account_id": "pool-acct-uuid",
        },
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get(f"/api/projects/{project.id}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    match = [t for t in tasks if t["title"] == "RuntimeCtxTask"]
    assert len(match) == 1
    ctx = match[0]["context"]

    # Safe key must pass through
    assert ctx.get("direction_id") == "d-abc"

    # Runtime account/provider keys must be filtered out
    for key in (
        "llm_account_id",
        "llm_provider",
        "llm_auth_mode",
        "llm_model",
        "llm_execution_mode",
        "max_plan_account_id",
    ):
        assert key not in ctx, (
            f"context key '{key}' must be filtered from user-facing response"
        )
