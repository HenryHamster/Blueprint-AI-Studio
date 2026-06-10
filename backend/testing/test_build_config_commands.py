"""Tests for build-config confirmed-command write-back and staleness tracking."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.claim_task_helpers import annotate_stale_build_config_dirs, is_commands_stale
from app.models import Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Unit tests: staleness helper
# ---------------------------------------------------------------------------


def test_is_commands_stale_absent():
    assert is_commands_stale(None) is True


def test_is_commands_stale_empty_string():
    assert is_commands_stale("") is True


def test_is_commands_stale_bad_format():
    assert is_commands_stale("not-a-date") is True


def test_is_commands_stale_old():
    old = (datetime.now(tz=timezone.utc) - timedelta(days=31)).isoformat()
    assert is_commands_stale(old) is True


def test_is_commands_stale_recent():
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=10)).isoformat()
    assert is_commands_stale(recent) is False


def test_is_commands_stale_exactly_30_days_not_stale():
    # 29 days old — not stale
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=29)).isoformat()
    assert is_commands_stale(ts) is False


def test_annotate_stale_build_config_dirs_empty():
    assert annotate_stale_build_config_dirs({}) == {}
    assert annotate_stale_build_config_dirs(None) is None


def test_annotate_stale_build_config_dirs_no_dirs():
    cfg = {"language": "python", "directories": []}
    result = annotate_stale_build_config_dirs(cfg)
    assert result["directories"] == []


def test_annotate_stale_build_config_dirs_stale_entry():
    old = (datetime.now(tz=timezone.utc) - timedelta(days=60)).isoformat()
    cfg = {
        "directories": [
            {"path": ".", "type": "python", "commands_confirmed_at": old},
        ]
    }
    result = annotate_stale_build_config_dirs(cfg)
    assert result["directories"][0]["commands_stale"] is True


def test_annotate_stale_build_config_dirs_fresh_entry():
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    cfg = {
        "directories": [
            {"path": ".", "type": "python", "commands_confirmed_at": recent},
        ]
    }
    result = annotate_stale_build_config_dirs(cfg)
    assert result["directories"][0]["commands_stale"] is False


def test_annotate_stale_does_not_mutate_original():
    cfg = {"directories": [{"path": ".", "type": "python"}]}
    result = annotate_stale_build_config_dirs(cfg)
    # original should not gain commands_stale
    assert "commands_stale" not in cfg["directories"][0]
    assert "commands_stale" in result["directories"][0]


# ---------------------------------------------------------------------------
# Schema round-trip: new fields on BuildConfigDirectoryEntry
# ---------------------------------------------------------------------------


def test_build_config_directory_entry_round_trip():
    from app.schemas import BuildConfigDirectoryEntry

    entry = BuildConfigDirectoryEntry(
        path=".",
        type="python",
        test_cmd="pytest",
        confirmed_test_cmd="pytest -x",
        confirmed_build_cmd=None,
        confirmed_install_cmd="pip install -e .",
        commands_confirmed_at="2026-01-01T00:00:00+00:00",
        confirming_task_id="abc-123",
    )
    dumped = entry.model_dump(exclude_none=True)
    assert dumped["confirmed_test_cmd"] == "pytest -x"
    assert "confirmed_build_cmd" not in dumped
    assert dumped["confirmed_install_cmd"] == "pip install -e ."
    assert dumped["commands_confirmed_at"] == "2026-01-01T00:00:00+00:00"
    assert dumped["confirming_task_id"] == "abc-123"


def test_build_config_directory_entry_defaults_none():
    from app.schemas import BuildConfigDirectoryEntry

    entry = BuildConfigDirectoryEntry(path="backend", type="python")
    assert entry.confirmed_test_cmd is None
    assert entry.confirmed_build_cmd is None
    assert entry.confirmed_install_cmd is None
    assert entry.commands_confirmed_at is None
    assert entry.confirming_task_id is None


# ---------------------------------------------------------------------------
# Integration tests: PATCH /api/agent/projects/{project_id}/build-config/commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_back_merges_confirmed_commands(client: AsyncClient, db_session):
    project = Project(
        name="WriteBack Test",
        user_id="test-user-123",
        build_config={
            "language": "python",
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-writeback-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-writeback-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [
                {
                    "path": ".",
                    "confirmed_test_cmd": "pytest -x --tb=short",
                    "confirmed_install_cmd": "pip install -e .",
                }
            ],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "." in data["updated_paths"]
    assert data["commands_confirmed_at"]

    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    assert dirs[0]["confirmed_test_cmd"] == "pytest -x --tb=short"
    assert dirs[0]["confirmed_install_cmd"] == "pip install -e ."
    # Original detected command must not be overwritten
    assert dirs[0]["test_cmd"] == "pytest"
    assert dirs[0]["confirming_task_id"] == str(task.id)
    assert dirs[0]["commands_confirmed_at"] == data["commands_confirmed_at"]


@pytest.mark.asyncio
async def test_write_back_sets_timestamp_and_task_id(client: AsyncClient, db_session):
    project = Project(
        name="Timestamp Test",
        user_id="test-user-123",
        build_config={
            "directories": [{"path": "backend", "type": "python"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="vm-ts-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-ts-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "backend", "confirmed_test_cmd": "pytest backend/"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    entry = project.build_config["directories"][0]
    assert entry["confirming_task_id"] == str(task.id)
    assert entry["commands_confirmed_at"] is not None
    # Timestamp must be parseable
    dt = datetime.fromisoformat(entry["commands_confirmed_at"].replace("Z", "+00:00"))
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_write_back_unknown_path_returns_404(client: AsyncClient, db_session):
    project = Project(
        name="Unknown Path Test",
        user_id="test-user-123",
        build_config={
            "directories": [{"path": ".", "type": "python"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-notfound-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-notfound-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "nonexistent/dir", "confirmed_test_cmd": "pytest"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_write_back_task_mismatch_returns_403(client: AsyncClient, db_session):
    project = Project(
        name="Mismatch Test",
        user_id="test-user-123",
        build_config={"directories": [{"path": ".", "type": "python"}]},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-mismatch-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    other_task_id = str(uuid.uuid4())
    token = create_agent_token(str(task.id), "vm-mismatch-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": other_task_id,  # mismatches token sub
            "entries": [{"path": ".", "confirmed_test_cmd": "pytest"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Integration test: claim returns commands_stale annotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_annotates_commands_stale(client: AsyncClient, db_session):
    old = (datetime.now(tz=timezone.utc) - timedelta(days=35)).isoformat()
    project = Project(
        name="Stale Annotation Test",
        user_id="test-user-123",
        build_config={
            "directories": [
                {
                    "path": ".",
                    "type": "python",
                    "confirmed_test_cmd": "pytest",
                    "commands_confirmed_at": old,
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-stale-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-stale-1")
    resp = await client.post(
        "/api/agent/claim",
        json={"task_id": str(task.id), "vm_instance_id": "vm-stale-1"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    dirs = data["project_metadata"]["build_config"]["directories"]
    assert dirs[0]["commands_stale"] is True


@pytest.mark.asyncio
async def test_claim_annotates_commands_not_stale(client: AsyncClient, db_session):
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    project = Project(
        name="Fresh Annotation Test",
        user_id="test-user-123",
        build_config={
            "directories": [
                {
                    "path": ".",
                    "type": "python",
                    "confirmed_test_cmd": "pytest",
                    "commands_confirmed_at": recent,
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-fresh-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-fresh-1")
    resp = await client.post(
        "/api/agent/claim",
        json={"task_id": str(task.id), "vm_instance_id": "vm-fresh-1"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    dirs = data["project_metadata"]["build_config"]["directories"]
    assert dirs[0]["commands_stale"] is False


# ---------------------------------------------------------------------------
# Integration test: admin GET endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_get_build_config_commands(client: AsyncClient, db_session):
    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    project = Project(
        name="Admin Read Test",
        user_id="test-user-123",
        build_config={
            "directories": [
                {
                    "path": ".",
                    "type": "python",
                    "test_cmd": "pytest",
                    "confirmed_test_cmd": "pytest -x",
                    "commands_confirmed_at": recent,
                    "confirming_task_id": "abc-123",
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/build-config/commands"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project_id"] == str(project.id)
    dirs = data["directories"]
    assert len(dirs) == 1
    d = dirs[0]
    assert d["detected_test_cmd"] == "pytest"
    assert d["confirmed_test_cmd"] == "pytest -x"
    assert d["commands_confirmed_at"] == recent
    assert d["confirming_task_id"] == "abc-123"
    assert d["commands_stale"] is False


@pytest.mark.asyncio
async def test_admin_get_build_config_commands_not_found(
    client: AsyncClient, db_session
):
    resp = await client.get(
        f"/api/admin/projects/{uuid.uuid4()}/build-config/commands"
    )
    assert resp.status_code == 404
