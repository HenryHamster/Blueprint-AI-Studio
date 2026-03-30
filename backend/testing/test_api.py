import pytest
from httpx import AsyncClient
from app.models import ProjectStatus

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
        "payloads": [{"command": "echo hello"}],
        "storage_uris": []
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

@pytest.mark.asyncio
async def test_list_projects_empty(client: AsyncClient):
    """
    Tests that a fresh database returns an empty list of projects.
    """
    response = await client.get("/api/projects")
    assert response.status_code == 200
    assert response.json() == {"projects": []}

@pytest.mark.asyncio
async def test_get_project_tasks(client: AsyncClient, mocker):
    """
    Tests retrieving tasks and artifacts for a specific project.
    """
    mocker.patch("app.main.provision_agent_vm", return_value=None)
    
    # 1. Create project
    payload = {
        "name": "Test Project Tasks",
        "payloads": [{"command": "echo hello"}],
        "storage_uris": []
    }
    create_resp = await client.post("/api/deploy", json=payload)
    project_id = create_resp.json()["project_id"]

    # 2. Get tasks
    resp = await client.get(f"/api/projects/{project_id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["payloads"] == [{"command": "echo hello"}]

@pytest.mark.asyncio
async def test_get_project_tasks_not_found(client: AsyncClient):
    """
    Tests that requesting tasks for a non-existent project returns 404.
    """
    import uuid
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/projects/{fake_id}/tasks")
    assert resp.status_code == 404
