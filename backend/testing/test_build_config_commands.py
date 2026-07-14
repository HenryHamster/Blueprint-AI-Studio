"""Tests for build-config confirmed-command write-back and staleness tracking."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.claim_task_helpers import annotate_stale_build_config_dirs, is_commands_stale
from app.models import Project, ProjectMember, Task, TaskStatus
from app.schemas import BuildConfig as BuildConfigSchema


@pytest.fixture(autouse=True)
def _clear_redetect_cooldowns():
    """Prevent module-level cooldown dicts from leaking between tests."""
    from app import build_config_detection

    build_config_detection._redetect_no_test_cooldown.clear()
    build_config_detection._redetect_empty_cooldown.clear()
    build_config_detection._redetect_stale_empty_cooldown.clear()
    yield
    build_config_detection._redetect_no_test_cooldown.clear()
    build_config_detection._redetect_empty_cooldown.clear()
    build_config_detection._redetect_stale_empty_cooldown.clear()


# ---------------------------------------------------------------------------
# Helper: simple project stub for ci_resolution unit tests
# ---------------------------------------------------------------------------


def _make_project(build_config):
    class _Stub:
        pass

    p = _Stub()
    p.build_config = build_config
    return p


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
    assert entry.confirmed_lint_cmd is None
    assert entry.confirmed_test_cmd is None
    assert entry.confirmed_build_cmd is None
    assert entry.confirmed_install_cmd is None
    assert entry.commands_confirmed_at is None
    assert entry.confirming_task_id is None


def test_build_config_directory_entry_has_confirmed_lint_cmd():
    """confirmed_lint_cmd is a first-class field with None default and round-trips correctly."""
    from app.schemas import BuildConfigDirectoryEntry

    entry = BuildConfigDirectoryEntry(
        path=".",
        type="python",
        lint_cmd="ruff check .",
        confirmed_lint_cmd="ruff check . --fix",
        commands_confirmed_at="2026-01-01T00:00:00+00:00",
    )
    dumped = entry.model_dump(exclude_none=True)
    assert dumped["confirmed_lint_cmd"] == "ruff check . --fix"
    assert dumped["lint_cmd"] == "ruff check ."
    # A None confirmed_lint_cmd is excluded from exclude_none dump
    entry2 = BuildConfigDirectoryEntry(path=".", type="python")
    dumped2 = entry2.model_dump(exclude_none=True)
    assert "confirmed_lint_cmd" not in dumped2


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
    # Top-level write-back marker stamps enough evidence for repo_bootstrap
    # completion to verify command-memory success without re-scanning directories.
    bc = project.build_config
    assert bc["last_command_writeback_task_id"] == str(task.id)
    assert "." in bc["last_command_writeback_paths"]
    assert bc["last_command_writeback_at"] == data["commands_confirmed_at"]


@pytest.mark.asyncio
async def test_write_back_marker_consumed_by_bootstrap_completion(
    client: AsyncClient, db_session
):
    """End-to-end: a write-back during a repo_bootstrap task is verified as
    confirmed command memory by _handle_repo_bootstrap_completion."""
    from app.task_completion import _handle_repo_bootstrap_completion

    project = Project(
        name="WriteBack Bootstrap E2E",
        user_id="test-user-123",
        build_config={
            "language": "python",
            "directories": [
                {"path": "backend", "type": "python", "test_cmd": "pytest"}
            ],
        },
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "repo_bootstrap"}],
        vm_instance_id="vm-writeback-bootstrap",
        status=TaskStatus.IN_PROGRESS,
        context={
            "repo_overview": {
                "tech_stack": "Python",
                "directory_layout": "backend/",
                "first_steps_summary": "pytest",
            },
            "command_memory": {"status": "success", "updated_paths": ["backend"]},
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-writeback-bootstrap")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "backend", "confirmed_test_cmd": "pytest -x"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    await _handle_repo_bootstrap_completion(task, "", db_session)
    await db_session.refresh(project)

    bc = project.build_config or {}
    assert bc.get("bootstrap_command_memory_status") == "confirmed"
    assert bc.get("bootstrap_command_memory_complete") is True


@pytest.mark.asyncio
async def test_write_back_blank_existing_path_does_not_stamp_command_memory_marker(
    client: AsyncClient, db_session
):
    """Blank confirmed commands on an existing path are ignored and do not
    unlock repo_bootstrap command-memory confirmation."""
    from app.task_completion import _handle_repo_bootstrap_completion

    project = Project(
        name="Blank WriteBack Test",
        user_id="test-user-123",
        build_config={
            "language": "python",
            "directories": [
                {"path": "backend", "type": "python", "test_cmd": "pytest"}
            ],
        },
        integration_branch="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "repo_bootstrap"}],
        vm_instance_id="vm-writeback-blank",
        status=TaskStatus.IN_PROGRESS,
        context={
            "repo_overview": {
                "tech_stack": "Python",
                "directory_layout": "backend/",
                "first_steps_summary": "pytest",
            },
            "command_memory": {"status": "success", "updated_paths": ["backend"]},
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-writeback-blank")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "backend", "confirmed_test_cmd": "   "}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated_paths"] == []

    await db_session.refresh(project)
    bc = project.build_config or {}
    assert bc.get("last_command_writeback_task_id") is None
    assert bc.get("last_command_writeback_paths") is None

    await _handle_repo_bootstrap_completion(task, "", db_session)
    await db_session.refresh(project)
    bc = project.build_config or {}

    assert bc.get("bootstrap_command_memory_status") == "writeback_failed"
    assert bc.get("bootstrap_command_memory_complete") is False


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
async def test_write_back_unknown_path_creates_new_entry(
    client: AsyncClient, db_session
):
    """An unknown directory path with a non-empty confirmed command creates a new entry."""
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
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "nonexistent/dir" in data["updated_paths"]

    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    # Original entry preserved, new entry appended
    assert len(dirs) == 2
    new_entry = next(d for d in dirs if d.get("path") == "nonexistent/dir")
    assert new_entry["confirmed_test_cmd"] == "pytest"
    assert new_entry["confirming_task_id"] == str(task.id)
    assert new_entry["commands_confirmed_at"] == data["commands_confirmed_at"]
    # Detected test_cmd must NOT be set on a freshly-created entry
    assert "test_cmd" not in new_entry


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

    resp = await client.get(f"/api/admin/projects/{project.id}/build-config/commands")
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
async def test_admin_get_build_config_commands_legacy_no_command_memory(
    client: AsyncClient, db_session
):
    """Legacy projects without command-memory keys surface command_memory: None."""
    project = Project(
        name="Legacy Command Memory",
        user_id="test-user-123",
        build_config={"directories": [{"path": ".", "type": "python"}]},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resp = await client.get(f"/api/admin/projects/{project.id}/build-config/commands")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "command_memory" in data
    assert data["command_memory"] is None


@pytest.mark.asyncio
async def test_admin_get_build_config_commands_surfaces_command_memory(
    client: AsyncClient, db_session
):
    """Persisted bootstrap_command_memory_* keys surface as a command_memory block."""
    project = Project(
        name="Command Memory Degraded",
        user_id="test-user-123",
        build_config={
            "directories": [{"path": ".", "type": "python"}],
            "bootstrap_command_memory_status": "writeback_failed",
            "bootstrap_command_memory_complete": False,
            "bootstrap_command_memory_detail": "reported success without write-back evidence",
            "bootstrap_command_memory_paths": ["backend"],
            "bootstrap_command_memory_updated_at": "2026-06-01T12:00:00+00:00",
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resp = await client.get(f"/api/admin/projects/{project.id}/build-config/commands")
    assert resp.status_code == 200, resp.text
    cm = resp.json()["command_memory"]
    assert cm is not None
    assert cm["status"] == "writeback_failed"
    assert cm["complete"] is False
    assert cm["detail"] == "reported success without write-back evidence"
    assert cm["paths"] == ["backend"]
    assert cm["updated_at"] == "2026-06-01T12:00:00+00:00"


@pytest.mark.asyncio
async def test_admin_get_build_config_commands_not_found(
    client: AsyncClient, db_session
):
    resp = await client.get(f"/api/admin/projects/{uuid.uuid4()}/build-config/commands")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Unit tests: ci_resolution confirmed-command precedence
# ---------------------------------------------------------------------------


def test_has_detectable_tests_fresh_confirmed_no_detected():
    """A fresh confirmed_test_cmd with no test_cmd still counts as detectable."""
    from app.ci_resolution import has_detectable_tests

    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    project = _make_project(
        {
            "directories": [
                {
                    "path": ".",
                    "confirmed_test_cmd": "pytest -x",
                    "commands_confirmed_at": recent,
                }
            ]
        }
    )
    assert has_detectable_tests(project) is True


def test_has_detectable_tests_stale_confirmed_no_detected():
    """A stale confirmed_test_cmd with no test_cmd is NOT detectable."""
    from app.ci_resolution import has_detectable_tests

    old = (datetime.now(tz=timezone.utc) - timedelta(days=35)).isoformat()
    project = _make_project(
        {
            "directories": [
                {
                    "path": ".",
                    "confirmed_test_cmd": "pytest -x",
                    "commands_confirmed_at": old,
                }
            ]
        }
    )
    assert has_detectable_tests(project) is False


def test_has_detectable_tests_stale_confirmed_with_detected():
    """A stale confirmed command but a present test_cmd still returns True (falls back to detected)."""
    from app.ci_resolution import has_detectable_tests

    old = (datetime.now(tz=timezone.utc) - timedelta(days=35)).isoformat()
    project = _make_project(
        {
            "directories": [
                {
                    "path": ".",
                    "test_cmd": "pytest",
                    "confirmed_test_cmd": "pytest --different",
                    "commands_confirmed_at": old,
                }
            ]
        }
    )
    assert has_detectable_tests(project) is True


def test_compute_merge_verdict_fresh_confirmed_cmd():
    """A fresh confirmed_test_cmd with no test_cmd → tests_gated with confirmed cmd."""
    from app.ci_resolution import compute_merge_verification_verdict

    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [
                {
                    "path": ".",
                    "confirmed_test_cmd": "pytest -x",
                    "commands_confirmed_at": recent,
                }
            ],
        }
    )
    result = compute_merge_verification_verdict(project)
    assert result["state"] == "tests_gated"
    assert len(result["test_commands"]) == 1
    assert result["test_commands"][0]["test_cmd"] == "pytest -x"


def test_compute_merge_verdict_fresh_confirmed_overrides_detected():
    """A fresh confirmed_test_cmd takes precedence over a different detected test_cmd."""
    from app.ci_resolution import compute_merge_verification_verdict

    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [
                {
                    "path": ".",
                    "test_cmd": "pytest-old",
                    "confirmed_test_cmd": "pytest -x --tb=short",
                    "commands_confirmed_at": recent,
                }
            ],
        }
    )
    result = compute_merge_verification_verdict(project)
    assert result["state"] == "tests_gated"
    assert result["test_commands"][0]["test_cmd"] == "pytest -x --tb=short"


def test_compute_merge_verdict_stale_confirmed_uses_detected():
    """A stale confirmed_test_cmd falls back to the detected test_cmd."""
    from app.ci_resolution import compute_merge_verification_verdict

    old = (datetime.now(tz=timezone.utc) - timedelta(days=35)).isoformat()
    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [
                {
                    "path": ".",
                    "test_cmd": "pytest",
                    "confirmed_test_cmd": "pytest --stale",
                    "commands_confirmed_at": old,
                }
            ],
        }
    )
    result = compute_merge_verification_verdict(project)
    assert result["state"] == "tests_gated"
    assert result["test_commands"][0]["test_cmd"] == "pytest"


def test_empty_detection_confirmed_command_flips_verdict_to_tests_gated():
    """Headline gate-flip: project with NO detected directories but a fresh confirmed
    command gets verdict=tests_gated, and has_detectable_tests returns True."""
    from app.ci_resolution import (
        compute_merge_verification_verdict,
        has_detectable_tests,
    )

    recent = (datetime.now(tz=timezone.utc) - timedelta(days=2)).isoformat()
    # Simulate a project whose initial detection found no directories at all
    # but a repo_bootstrap agent later confirmed a working test command via
    # the write-back endpoint (which now creates a new entry).
    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [
                {
                    "path": ".",
                    "confirmed_test_cmd": "pytest tests/",
                    "commands_confirmed_at": recent,
                    "confirming_task_id": "bootstrap-task-abc",
                }
            ],
        }
    )
    assert has_detectable_tests(project) is True
    result = compute_merge_verification_verdict(project)
    assert result["state"] == "tests_gated"
    assert result["test_commands"][0]["test_cmd"] == "pytest tests/"
    assert result["test_commands"][0]["path"] == "."


@pytest.mark.asyncio
async def test_write_back_confirmed_lint_cmd(client: AsyncClient, db_session):
    """confirmed_lint_cmd is persisted and stamps commands_confirmed_at."""
    project = Project(
        name="Lint WriteBack Test",
        user_id="test-user-123",
        build_config={
            "language": "python",
            "directories": [
                {"path": ".", "type": "python", "lint_cmd": "ruff check ."}
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-lint-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-lint-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": ".", "confirmed_lint_cmd": "ruff check . --fix"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "." in data["updated_paths"]
    assert data["commands_confirmed_at"]

    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    assert dirs[0]["confirmed_lint_cmd"] == "ruff check . --fix"
    # Original detected lint_cmd must not be overwritten
    assert dirs[0]["lint_cmd"] == "ruff check ."
    assert dirs[0]["confirming_task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_write_back_unknown_path_lint_only_creates_entry(
    client: AsyncClient, db_session
):
    """An unknown path with only confirmed_lint_cmd (no test/build/install) creates a new entry."""
    project = Project(
        name="Lint Unknown Path Test",
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
        vm_instance_id="vm-lint-new-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-lint-new-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "frontend", "confirmed_lint_cmd": "eslint src/"}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "frontend" in data["updated_paths"]

    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    assert len(dirs) == 2
    new_entry = next(d for d in dirs if d.get("path") == "frontend")
    assert new_entry["confirmed_lint_cmd"] == "eslint src/"
    assert new_entry["confirming_task_id"] == str(task.id)
    # Detected lint_cmd must NOT be set on a freshly-created entry
    assert "lint_cmd" not in new_entry


@pytest.mark.asyncio
async def test_write_back_empty_confirmed_cmd_unknown_path_skipped(
    client: AsyncClient, db_session
):
    """An unknown path with ALL empty confirmed commands is silently skipped (no new entry)."""
    project = Project(
        name="Empty Confirmed Cmd Test",
        user_id="test-user-123",
        build_config={
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-empty-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-empty-1")
    resp = await client.patch(
        f"/api/agent/projects/{project.id}/build-config/commands",
        json={
            "task_id": str(task.id),
            "entries": [{"path": "new/dir", "confirmed_test_cmd": None}],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # No path was updated since confirmed cmd was empty
    assert "new/dir" not in data["updated_paths"]

    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    # No new directory was created
    assert len(dirs) == 1


@pytest.mark.asyncio
async def test_patch_project_build_config_preserves_bootstrap_command_memory(
    client: AsyncClient, db_session
):
    """PATCH /projects/{id} must preserve durable bootstrap command-memory fields."""
    project = Project(
        name="Bootstrap Command Memory Patch",
        user_id="test-user-123",
        build_config={
            "language": "python",
            "bootstrap_command_memory_status": "confirmed",
            "bootstrap_command_memory_complete": True,
            "bootstrap_command_memory_detail": "all commands persisted",
            "bootstrap_command_memory_paths": ["backend"],
            "bootstrap_command_memory_updated_at": "2026-06-01T12:00:00+00:00",
        },
    )
    db_session.add(project)
    await db_session.commit()
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    await db_session.refresh(project)

    resp = await client.patch(
        f"/api/admin/projects/{project.id}",
        json={"build_config": {"language": "typescript"}},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    bc = project.build_config or {}
    assert bc["language"] == "typescript"
    assert bc["bootstrap_command_memory_status"] == "confirmed"
    assert bc["bootstrap_command_memory_complete"] is True
    assert bc["bootstrap_command_memory_detail"] == "all commands persisted"
    assert bc["bootstrap_command_memory_paths"] == ["backend"]
    assert bc["bootstrap_command_memory_updated_at"] == "2026-06-01T12:00:00+00:00"


def test_redetection_preserves_confirmed_lint_cmd():
    """Re-running detection after a prior confirmation preserves confirmed_lint_cmd."""
    from app.build_config_detection import apply_operator_build_config_overrides

    recent = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    existing = {
        "language": "python",
        "directories": [
            {
                "path": ".",
                "type": "python",
                "lint_cmd": "ruff check .",
                "confirmed_lint_cmd": "ruff check . --fix",
                "confirmed_test_cmd": "pytest -x",
                "commands_confirmed_at": recent,
                "confirming_task_id": "task-abc",
            }
        ],
    }
    detected = {
        "language": "python",
        "directories": [
            {"path": ".", "type": "python", "lint_cmd": "ruff check ."},
        ],
    }
    result = apply_operator_build_config_overrides(detected, existing)
    dirs = result["directories"]
    assert len(dirs) == 1
    assert dirs[0]["confirmed_lint_cmd"] == "ruff check . --fix"
    assert dirs[0]["confirmed_test_cmd"] == "pytest -x"
    assert dirs[0]["commands_confirmed_at"] == recent
    assert dirs[0]["confirming_task_id"] == "task-abc"


def test_build_config_schema_round_trips_bootstrap_command_memory():
    """BuildConfig validation must preserve bootstrap command-memory markers."""
    cfg = BuildConfigSchema(
        language="python",
        bootstrap_command_memory_status="confirmed",
        bootstrap_command_memory_complete=True,
        bootstrap_command_memory_detail="all commands persisted",
        bootstrap_command_memory_paths=["backend"],
        bootstrap_command_memory_updated_at="2026-06-01T12:00:00+00:00",
    )

    dumped = cfg.model_dump(exclude_none=True)

    assert dumped["bootstrap_command_memory_status"] == "confirmed"
    assert dumped["bootstrap_command_memory_complete"] is True
    assert dumped["bootstrap_command_memory_detail"] == "all commands persisted"
    assert dumped["bootstrap_command_memory_paths"] == ["backend"]
    assert dumped["bootstrap_command_memory_updated_at"] == "2026-06-01T12:00:00+00:00"


def test_redetection_preserves_bootstrap_command_memory():
    """Re-running detection preserves durable bootstrap command-memory fields."""
    from app.build_config_detection import apply_operator_build_config_overrides

    existing = {
        "language": "python",
        "bootstrap_command_memory_status": "writeback_failed",
        "bootstrap_command_memory_complete": False,
        "bootstrap_command_memory_detail": "reported success without write-back evidence",
        "bootstrap_command_memory_paths": ["backend"],
        "bootstrap_command_memory_updated_at": "2026-06-01T12:00:00+00:00",
        "directories": [{"path": ".", "type": "python"}],
    }
    detected = {
        "language": "python",
        "directories": [{"path": ".", "type": "python"}],
    }

    result = apply_operator_build_config_overrides(detected, existing)

    assert result["bootstrap_command_memory_status"] == "writeback_failed"
    assert result["bootstrap_command_memory_complete"] is False
    assert (
        result["bootstrap_command_memory_detail"]
        == "reported success without write-back evidence"
    )
    assert result["bootstrap_command_memory_paths"] == ["backend"]
    assert result["bootstrap_command_memory_updated_at"] == "2026-06-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Unit tests: maybe_redetect_missing_test_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_gate_redetects_before_unverified_fall_open():
    """Acceptance proof: re-detection fires for a non-empty, detection-complete
    project with no test_cmd, returns True when detection finds a test command,
    and after return has_detectable_tests is True (enabling verify_tests gate).
    The fall-open reason is no_detectable_test_command only when re-detection
    still finds nothing."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_redetect_missing_test_command
    from app.ci_resolution import has_detectable_tests

    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python"}],
        }
    )
    project.github_owner = "external-owner"
    project.github_repo = "external-repo"
    project.id = "proj-redetect-proof"

    session = AsyncMock()
    detected = {
        "directories": [{"path": ".", "type": "python", "test_cmd": "pytest tests/"}]
    }

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected,
        ),
        patch("sqlalchemy.orm.attributes.flag_modified"),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_redetect_missing_test_command(project, session)

    assert result is True
    # Gate can now run tests — has_detectable_tests uses updated project.build_config
    assert has_detectable_tests(project) is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_redetect_missing_test_command_skips_when_repo_empty():
    """Re-detection is skipped when repo_is_empty=True (other path handles it)."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_redetect_missing_test_command

    project = _make_project(
        {
            "requires_ci_checks": False,
            "repo_is_empty": True,
            "directories": [],
        }
    )
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-empty"

    session = AsyncMock()

    with patch(
        "app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock
    ) as mock_detect:
        result = await maybe_redetect_missing_test_command(project, session)

    assert result is False
    mock_detect.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_redetect_missing_test_command_skips_when_has_tests():
    """Re-detection is skipped when the project already has a detectable test command."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_redetect_missing_test_command

    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python", "test_cmd": "pytest"}],
        }
    )
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-has-tests"

    session = AsyncMock()

    with patch(
        "app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock
    ) as mock_detect:
        result = await maybe_redetect_missing_test_command(project, session)

    assert result is False
    mock_detect.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_redetect_missing_test_command_returns_false_when_detection_finds_no_tests():
    """Re-detection returns False and leaves config unchanged when detection still finds no tests.
    The gate fall-open reason (no_detectable_test_command) is truthful in this case."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_redetect_missing_test_command

    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python"}],
        }
    )
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-no-tests"
    original_dirs = list(project.build_config["directories"])

    session = AsyncMock()
    detected_no_tests = {"directories": [{"path": ".", "type": "python"}]}

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected_no_tests,
        ),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_redetect_missing_test_command(project, session)

    assert result is False
    # Config unchanged — directories still have no test command
    assert project.build_config["directories"] == original_dirs
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_redetect_missing_test_command_honors_cooldown():
    """Second call within the 6h cooldown window skips _detect_build_config entirely."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_redetect_missing_test_command

    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python"}],
        }
    )
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-cooldown-test"

    session = AsyncMock()
    detected = {
        "directories": [{"path": ".", "type": "python", "test_cmd": "pytest tests/"}]
    }

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected,
        ) as mock_detect,
        patch("sqlalchemy.orm.attributes.flag_modified"),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        # First call stamps the cooldown
        result1 = await maybe_redetect_missing_test_command(project, session)
        assert result1 is True
        assert mock_detect.call_count == 1

        # Second call within the cooldown window must return False without
        # invoking _detect_build_config again.
        result2 = await maybe_redetect_missing_test_command(project, session)
        assert result2 is False
        assert mock_detect.call_count == 1, (
            "_detect_build_config must NOT be called on the second call within cooldown"
        )


@pytest.mark.asyncio
async def test_maybe_redetect_missing_test_command_skips_when_quota_reserve_active():
    """When the install-token quota is near exhaustion, re-detection is skipped."""
    import time
    from unittest.mock import AsyncMock, patch

    from app import shared_state
    from app.build_config_detection import maybe_redetect_missing_test_command
    from app.github_repo_snapshot import GROUNDING_QUOTA_RESERVE

    project = _make_project(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python"}],
        }
    )
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-quota-test"

    session = AsyncMock()

    saved_quota = dict(shared_state.github_quota)
    shared_state.github_quota.update(
        {
            "remaining": GROUNDING_QUOTA_RESERVE - 1,
            "limit": 5000,
            "reset_at": int(time.time()) + 1800,
        }
    )
    try:
        with patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
        ) as mock_detect:
            result = await maybe_redetect_missing_test_command(project, session)
    finally:
        shared_state.github_quota.update(saved_quota)

    assert result is False
    mock_detect.assert_not_awaited()


# ---------------------------------------------------------------------------
# maybe_correct_stale_repo_empty_signal — the inverse self-heal that unwedges a
# stale repo_is_empty=False on a genuinely empty/near-empty repo.
# ---------------------------------------------------------------------------


def _stale_empty_project():
    project = _make_project({"repo_is_empty": False, "source_file_count": 12})
    project.github_owner = "external-owner"
    project.github_repo = "external-repo"
    project.id = "proj-stale-empty"
    return project


@pytest.mark.asyncio
async def test_stale_empty_signal_flips_on_authoritative_empty():
    """A stale repo_is_empty=False flips to True when fresh detection
    authoritatively confirms the repo is empty (repo_is_empty True + a real
    source_file_count)."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()
    detected = {
        "language": None,
        "directories": [],
        "env_vars": {},
        "repo_is_empty": True,
        "source_file_count": 0,
        "has_ci": False,
    }

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected,
        ),
        patch("sqlalchemy.orm.attributes.flag_modified"),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is True
    assert project.build_config["repo_is_empty"] is True
    assert project.build_config["source_file_count"] == 0
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_empty_signal_does_not_flip_when_repo_has_content():
    """FAIL-LOUD: a repo that genuinely HAS content (detection returns
    repo_is_empty=False) leaves the stored signal untouched, so a real clone
    shortfall keeps failing loud rather than being masked as greenfield."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()
    detected_non_empty = {
        "language": "python",
        "directories": [{"path": "./", "type": "python"}],
        "repo_is_empty": False,
        "source_file_count": 12,
    }

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected_non_empty,
        ),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    assert project.build_config["repo_is_empty"] is False
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_empty_signal_does_not_flip_on_indeterminate_read():
    """FAIL-LOUD: an indeterminate/partial read (source_file_count None) is NOT
    an authoritative empty verdict, so the signal is not corrected."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()
    # listing_complete=False path returns (None, None) (undetermined); but even
    # a defensive repo_is_empty=True paired with an unknown count must not flip.
    detected_indeterminate = {
        "repo_is_empty": True,
        "source_file_count": None,
    }

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected_indeterminate,
        ),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    assert project.build_config["repo_is_empty"] is False
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_empty_signal_does_not_flip_on_transient_failure():
    """FAIL-LOUD: a transient GitHub failure (detector returns {}) leaves the
    stored signal untouched."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    assert project.build_config["repo_is_empty"] is False
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_empty_signal_noop_when_already_true():
    """No-op (no GitHub call) when the stored signal is already True — nothing
    to correct."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _make_project({"repo_is_empty": True, "source_file_count": 0})
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-already-true"

    session = AsyncMock()

    with patch(
        "app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock
    ) as mock_detect:
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    mock_detect.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_empty_signal_noop_when_signal_absent():
    """No-op when repo_is_empty is absent — the guard fires only on an explicit
    False (a repo whose emptiness was never determined must not be flipped)."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _make_project({"directories": [{"path": ".", "type": "python"}]})
    project.github_owner = "owner"
    project.github_repo = "repo"
    project.id = "proj-absent-signal"

    session = AsyncMock()

    with patch(
        "app.routers.admin_onboard._detect_build_config", new_callable=AsyncMock
    ) as mock_detect:
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    mock_detect.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_empty_signal_honors_cooldown():
    """A second call within the cooldown window skips _detect_build_config."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()
    detected = {"repo_is_empty": True, "source_file_count": 0}

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            return_value=detected,
        ) as mock_detect,
        patch("sqlalchemy.orm.attributes.flag_modified"),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        first = await maybe_correct_stale_repo_empty_signal(project, session)
        # Reset the signal so the second call would otherwise be eligible.
        project.build_config = {"repo_is_empty": False, "source_file_count": 12}
        second = await maybe_correct_stale_repo_empty_signal(project, session)

    assert first is True
    assert second is False
    assert mock_detect.await_count == 1


@pytest.mark.asyncio
async def test_stale_empty_signal_swallows_detector_exception():
    """Best-effort: an exception in the detector never propagates; the session
    is rolled back and the stored signal is left unchanged."""
    from unittest.mock import AsyncMock, patch

    from app.build_config_detection import maybe_correct_stale_repo_empty_signal

    project = _stale_empty_project()
    session = AsyncMock()

    with (
        patch(
            "app.github.get_repo_installation_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch(
            "app.routers.admin_onboard._detect_build_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "app.shared_state.get_project_integration_branch",
            return_value="main",
        ),
    ):
        result = await maybe_correct_stale_repo_empty_signal(project, session)

    assert result is False
    assert project.build_config["repo_is_empty"] is False
    session.rollback.assert_awaited_once()
