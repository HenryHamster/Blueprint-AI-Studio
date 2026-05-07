"""
End-to-end lifecycle integration tests that run the real agent subprocess.

These tests validate the full contract between the agent (TypeScript/Node)
and the backend (FastAPI) by:
  1. Starting a real FastAPI server on a random port
  2. Spawning the agent via `npm start` with a mock claude CLI on PATH
  3. Asserting the agent claims, executes, and reports status correctly

Run directly (must NOT run inside Docker — the tests spawn the real agent subprocess
via `npm start`, which requires agent/node_modules on the host filesystem):

    pytest testing/test_lifecycle_integration.py -v -m integration

Prerequisites: cd agent && npm ci
"""

import asyncio
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid

import pytest

# ---------------------------------------------------------------------------
# Module-level skip guard: agent must have node_modules installed
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_AGENT_DIR = _REPO_ROOT / "agent"

if not (_AGENT_DIR / "node_modules").is_dir():
    pytest.skip(
        "agent/node_modules not installed — run `cd agent && npm ci`",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Environment bootstrap (must happen before any app imports)
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-key-for-pytest")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-ticket-secret")

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler  # noqa: E402


def _sqlite_visit_JSONB(self, type_, **kw):
    return "TEXT"


if not hasattr(_SQLiteTypeCompiler, "_orig_visit_JSONB"):
    _SQLiteTypeCompiler._orig_visit_JSONB = True
    _SQLiteTypeCompiler.visit_JSONB = _sqlite_visit_JSONB

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlmodel import SQLModel, select  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from app.auth import create_agent_token  # noqa: E402
from app.database import get_session  # noqa: E402
from app.models import (  # noqa: E402
    Project,
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mock_claude_dir():
    """Copy mock_claude.sh to a temp dir as 'claude' and put it on PATH."""
    tmpdir = tempfile.mkdtemp(prefix="mock_claude_")
    src = pathlib.Path(__file__).parent / "mock_claude.sh"
    dst = pathlib.Path(tmpdir) / "claude"
    shutil.copy2(src, dst)
    dst.chmod(0o755)
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def bare_git_repo():
    """
    Create a bare git repo and a working clone with an initial commit.
    Configure git URL rewriting so the agent's clone URL redirects to
    the local bare repo.

    Yields (bare_path, clone_path, gitconfig_path).  Pass gitconfig_path
    to start_agent() so the agent subprocess honours the URL rewriting via
    GIT_CONFIG_GLOBAL — this avoids touching the real global gitconfig and is
    safe if the test process is killed before teardown.
    """
    tmpdir = tempfile.mkdtemp(prefix="git_lifecycle_")
    bare_path = os.path.join(tmpdir, "test.git")
    clone_path = os.path.join(tmpdir, "work")

    # Create bare repo
    subprocess.run(
        ["git", "init", "--bare", bare_path], check=True, capture_output=True
    )

    # Create working clone, add initial commit, push
    subprocess.run(
        ["git", "clone", bare_path, clone_path], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    # Create an initial file so the repo has a commit
    with open(os.path.join(clone_path, "README.md"), "w") as f:
        f.write("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=clone_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "HEAD:main"],
        cwd=clone_path,
        check=True,
        capture_output=True,
    )

    # Write URL rewriting to a dedicated temp gitconfig instead of mutating the
    # real global gitconfig.  The agent subprocess receives GIT_CONFIG_GLOBAL
    # pointing at this file so git picks it up without affecting the host user.
    # The agent constructs exactly: https://x-access-token:test-token@github.com/test-owner/test-repo.git
    full_clone_url = (
        "https://x-access-token:test-token@github.com/test-owner/test-repo.git"
    )
    gitconfig_path = os.path.join(tmpdir, "gitconfig")
    with open(gitconfig_path, "w") as f:
        f.write(f'[url "{bare_path}"]\n')
        f.write(f"    insteadOf = {full_clone_url}\n")

    yield bare_path, clone_path, gitconfig_path

    shutil.rmtree(tmpdir, ignore_errors=True)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture()
def integration_server(bare_git_repo):
    """
    Start a real FastAPI server on a random port backed by file-based SQLite.
    Mocks GitHub token resolution and PR creation so no real GitHub access is needed.
    """
    import uvicorn
    import threading

    tmpdir = tempfile.mkdtemp(prefix="integ_db_")
    db_path = os.path.join(tmpdir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    # Create engine and tables synchronously
    from sqlalchemy import create_engine

    sync_url = f"sqlite:///{db_path}"
    sync_engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(sync_engine)
    sync_engine.dispose()

    # Async engine for the server
    server_engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False},
    )
    ServerSession = sessionmaker(
        server_engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.main import app  # noqa: E402

    async def _override_session():
        async with ServerSession() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    # Mock GitHub token resolution — return test values
    _gh_token_mock = AsyncMock(return_value=("test-token", "test-owner", "test-repo"))
    _broadcast_mock = AsyncMock()
    _create_pr_mock = AsyncMock(
        return_value={
            "html_url": "https://github.com/test-owner/test-repo/pull/1",
            "number": 1,
        }
    )

    patches = [
        patch("app.routers.agent.get_github_token_for_task", _gh_token_mock),
        patch("app.main.broadcast_to_project", _broadcast_mock),
        patch("app.routers.agent.create_pull_request", _create_pr_mock),
    ]
    for p in patches:
        p.start()

    # Reset rate limiter
    from app.main import limiter

    underlying = getattr(limiter, "_limiter", None)
    storage = getattr(underlying, "storage", None) if underlying else None
    if storage and hasattr(storage, "reset"):
        storage.reset()

    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)

    yield db_url, f"http://127.0.0.1:{port}"

    # Shutdown
    server.should_exit = True
    thread.join(timeout=5)
    for p in patches:
        p.stop()
    app.dependency_overrides.pop(get_session, None)

    # Clean up engine
    _loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        _loop.run_until_complete(server_engine.dispose())
    finally:
        _loop.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


async def create_test_task(
    db_url: str,
    *,
    github_owner: str = "test-owner",
    github_repo: str = "test-repo",
    payloads: list,
    status: TaskStatus = TaskStatus.QUEUED,
    vm_instance_id: str | None = None,
    title: str = "Integration test task",
) -> tuple[Task, Project]:
    """Create a Project and Task directly in the file-based SQLite DB."""
    engine = create_async_engine(db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    project = Project(
        name="Integration Test Project",
        user_id="test-user-123",
        github_owner=github_owner,
        github_repo=github_repo,
        autonomous_enabled=False,
        directions=[],
    )
    task = Task(
        project_id=project.id,
        title=title,
        description="Integration test",
        status=status,
        created_by="test-user",
        requires_approval=False,
        payloads=payloads,
        context={},
        status_log=[],
        vm_instance_id=vm_instance_id,
    )

    async with Session() as session:
        session.add(project)
        session.add(task)
        await session.commit()
        await session.refresh(project)
        await session.refresh(task)

    await engine.dispose()
    return task, project


async def wait_for_terminal_status(
    db_url: str,
    task_id: uuid.UUID,
    timeout_seconds: int = 120,
    poll_interval: float = 2.0,
) -> Task:
    """Poll the DB until the task reaches a terminal status."""
    engine = create_async_engine(db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    deadline = time.monotonic() + timeout_seconds
    last_status = None

    try:
        while time.monotonic() < deadline:
            async with Session() as session:
                result = await session.execute(select(Task).where(Task.id == task_id))
                task = result.scalars().first()
                if task:
                    last_status = task.status
                    if task.status in (
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    ):
                        # Also load artifacts eagerly
                        art_result = await session.execute(
                            select(Artifact).where(Artifact.task_id == task_id)
                        )
                        task._loaded_artifacts = list(art_result.scalars().all())
                        return task
            await asyncio.sleep(poll_interval)
    finally:
        await engine.dispose()

    raise TimeoutError(
        f"Task {task_id} did not reach terminal status within {timeout_seconds}s "
        f"(last status: {last_status})"
    )


def start_agent(
    task_id: uuid.UUID,
    vm_instance_id: str,
    server_url: str,
    mock_claude_dir: str,
    mock_mode: str = "write",
    gitconfig_path: str | None = None,
) -> tuple[subprocess.Popen, str, str]:
    """Spawn the agent subprocess with mock claude on PATH.

    Returns (process, agent_workspace_path, mode_file_path).
    The caller is responsible for cleaning up the workspace directory and
    calling cleanup_agent() (which removes the mode file) after the agent exits.
    """
    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    # Redirect the agent's workspace to an isolated temp directory so it does NOT
    # delete /tmp/workspace (the actual project source tree). The agent always does
    # `rm -rf WORKSPACE` before cloning; AGENT_WORKSPACE env var overrides the
    # hardcoded '/tmp/workspace' constant in agent/src/index.ts.
    agent_workspace = tempfile.mkdtemp(prefix="agent_ws_")

    # The agent builds a minimal env for the claude subprocess (only PATH,
    # HOME, LANG, TERM, and a few auth vars). MOCK_CLAUDE_MODE won't be
    # propagated, so we also write the mode to a per-task file the mock reads.
    # Using a per-task filename avoids races when tests run concurrently.
    mode_file = f"/tmp/mock_claude_mode_{task_id}"
    with open(mode_file, "w") as f:
        f.write(mock_mode)

    env = {
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": server_url,
        "AGENT_SECRET_TOKEN": agent_jwt,
        "MOCK_CLAUDE_MODE": mock_mode,
        "TASK_ID": str(task_id),
        # Override the agent's workspace path — keeps /tmp/workspace intact
        "AGENT_WORKSPACE": agent_workspace,
        # Minimal env for Node.js
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": f"{mock_claude_dir}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": "xterm-256color",
        # Prevent the agent from trying real Claude auth
        "ANTHROPIC_API_KEY": "sk-ant-fake-key-for-testing",
    }

    # Point git at the isolated gitconfig so URL rewriting works without touching
    # the host's real global gitconfig.
    if gitconfig_path:
        env["GIT_CONFIG_GLOBAL"] = gitconfig_path

    proc = subprocess.Popen(
        ["npm", "start"],
        cwd=str(_AGENT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc, agent_workspace, mode_file


def cleanup_agent(
    proc: subprocess.Popen, timeout: int = 10, mode_file: str | None = None
):
    """Gracefully stop the agent subprocess and clean up the per-task mode file."""
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait(timeout=5)
    # Drain output to avoid broken pipe
    proc.stdout.close()
    proc.stderr.close()
    # Clean up per-task mode file
    if mode_file:
        try:
            os.unlink(mode_file)
        except FileNotFoundError:
            pass


def get_bare_repo_branches(bare_path: str) -> list[str]:
    """List branches in the bare repo (excluding HEAD)."""
    result = subprocess.run(
        ["git", "branch"],
        cwd=bare_path,
        capture_output=True,
        text=True,
    )
    branches = []
    for line in result.stdout.strip().splitlines():
        branch = line.strip().lstrip("* ")
        if branch:
            branches.append(branch)
    return branches


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_task_lifecycle(integration_server, bare_git_repo, mock_claude_dir):
    """
    Full lifecycle: create task -> agent claims -> mock claude writes a file ->
    agent commits, pushes, creates PR -> status COMPLETED with PR artifact.
    """
    db_url, server_url = integration_server
    bare_path, _, gitconfig_path = bare_git_repo
    vm_id = f"integ-write-{uuid.uuid4().hex[:8]}"

    task, project = await create_test_task(
        db_url,
        payloads=[
            {"type": "create", "prompt": "Create a test file", "base_branch": "main"}
        ],
        status=TaskStatus.QUEUED,
        vm_instance_id=vm_id,
    )

    proc, agent_workspace, mode_file = start_agent(
        task.id,
        vm_id,
        server_url,
        mock_claude_dir,
        mock_mode="write",
        gitconfig_path=gitconfig_path,
    )
    try:
        completed_task = await wait_for_terminal_status(
            db_url, task.id, timeout_seconds=90
        )
        assert completed_task.status == TaskStatus.COMPLETED, (
            f"Expected COMPLETED but got {completed_task.status}. "
            f"Latest message: {completed_task.latest_status_message}"
        )

        # Check for PR artifact
        artifacts = getattr(completed_task, "_loaded_artifacts", [])
        pr_artifacts = [
            a for a in artifacts if a.artifact_type == ArtifactType.PULL_REQUEST
        ]
        assert len(pr_artifacts) > 0, (
            f"Expected a PULL_REQUEST artifact, got: {[a.artifact_type for a in artifacts]}"
        )

        # Verify the bare repo received a push (new branch besides main)
        branches = get_bare_repo_branches(bare_path)
        non_main = [b for b in branches if b != "main"]
        assert len(non_main) > 0, (
            f"Expected a new branch pushed to bare repo, only found: {branches}"
        )

    finally:
        cleanup_agent(proc, mode_file=mode_file)
        shutil.rmtree(agent_workspace, ignore_errors=True)


@pytest.mark.asyncio
async def test_readonly_task_lifecycle(
    integration_server, bare_git_repo, mock_claude_dir
):
    """
    Research task lifecycle: agent claims -> mock claude writes findings file ->
    agent reports COMPLETED with RESEARCH_FINDINGS artifact, no git push.
    """
    db_url, server_url = integration_server
    bare_path, _, gitconfig_path = bare_git_repo
    vm_id = f"integ-read-{uuid.uuid4().hex[:8]}"

    task, project = await create_test_task(
        db_url,
        payloads=[
            {
                "type": "research",
                "prompt": "Research the codebase",
                "base_branch": "main",
            }
        ],
        status=TaskStatus.QUEUED,
        vm_instance_id=vm_id,
    )

    proc, agent_workspace, mode_file = start_agent(
        task.id,
        vm_id,
        server_url,
        mock_claude_dir,
        mock_mode="readonly",
        gitconfig_path=gitconfig_path,
    )
    try:
        completed_task = await wait_for_terminal_status(
            db_url, task.id, timeout_seconds=90
        )
        assert completed_task.status == TaskStatus.COMPLETED, (
            f"Expected COMPLETED but got {completed_task.status}. "
            f"Latest message: {completed_task.latest_status_message}"
        )

        # Research tasks produce RESEARCH_FINDINGS artifacts
        artifacts = getattr(completed_task, "_loaded_artifacts", [])
        research_artifacts = [
            a for a in artifacts if a.artifact_type == ArtifactType.RESEARCH_FINDINGS
        ]
        assert len(research_artifacts) > 0, (
            f"Expected RESEARCH_FINDINGS artifact, got: {[a.artifact_type for a in artifacts]}"
        )

        # Verify no new branches were pushed (research is read-only)
        branches = get_bare_repo_branches(bare_path)
        non_main = [b for b in branches if b != "main"]
        assert len(non_main) == 0, (
            f"Research task should NOT push branches, but found: {non_main}"
        )

    finally:
        cleanup_agent(proc, mode_file=mode_file)
        shutil.rmtree(agent_workspace, ignore_errors=True)


@pytest.mark.asyncio
async def test_failure_lifecycle(integration_server, bare_git_repo, mock_claude_dir):
    """
    Failure lifecycle: mock claude exits with error -> agent reports FAILED
    with ERROR_LOG artifact.
    """
    db_url, server_url = integration_server
    bare_path, _, gitconfig_path = bare_git_repo
    vm_id = f"integ-fail-{uuid.uuid4().hex[:8]}"

    task, project = await create_test_task(
        db_url,
        payloads=[
            {"type": "create", "prompt": "This will fail", "base_branch": "main"}
        ],
        status=TaskStatus.QUEUED,
        vm_instance_id=vm_id,
    )

    proc, agent_workspace, mode_file = start_agent(
        task.id,
        vm_id,
        server_url,
        mock_claude_dir,
        mock_mode="fail",
        gitconfig_path=gitconfig_path,
    )
    try:
        completed_task = await wait_for_terminal_status(
            db_url, task.id, timeout_seconds=90
        )
        assert completed_task.status == TaskStatus.FAILED, (
            f"Expected FAILED but got {completed_task.status}. "
            f"Latest message: {completed_task.latest_status_message}"
        )

        # Error information should be preserved
        artifacts = getattr(completed_task, "_loaded_artifacts", [])
        error_artifacts = [
            a for a in artifacts if a.artifact_type == ArtifactType.ERROR_LOG
        ]
        assert len(error_artifacts) > 0, (
            f"Expected ERROR_LOG artifact, got: {[a.artifact_type for a in artifacts]}"
        )

    finally:
        cleanup_agent(proc, mode_file=mode_file)
        shutil.rmtree(agent_workspace, ignore_errors=True)
