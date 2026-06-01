"""Tests for project-configurable approval gate file lists.

Verifies that the approval gate can be configured per-project:
- Without config, all projects (including self-repo) use empty lists
- With config, projects can define their own sensitive file paths
- is_self_repo is informational metadata; config drives the gate
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.task_lifecycle import _get_approval_gate_file_lists
from app.models import Project
from testing.factories import make_membership


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_self_repo_no_config_empty():
    """Without approval_gate_config, self-repo projects return empty lists.

    Blueprint defaults are seeded into approval_gate_config by a migration
    for is_self_repo projects; code-level fallbacks have been removed.
    """
    project = Project(name="Blueprint Self", user_id="u1", is_self_repo=True)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_self_repo_seeded_config():
    """Self-repo project with seeded approval_gate_config returns Blueprint defaults."""
    project = Project(
        name="Blueprint Self",
        user_id="u1",
        is_self_repo=True,
        approval_gate_config={
            "infra_files": [
                "backend/app/main.py",
                "backend/app/compute.py",
                "backend/app/models.py",
                "backend/app/database.py",
                "backend/app/security.py",
                "backend/app/auth.py",
                "agent/src/index.ts",
                "CLAUDE.md",
                "backend/promotion_policy.yaml",
            ],
            "critical_files": [
                "backend/app/security.py",
                "backend/app/auth.py",
                "CLAUDE.md",
                "backend/promotion_policy.yaml",
            ],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert "backend/app/main.py" in infra_files
    assert "backend/app/models.py" in infra_files
    assert "backend/app/security.py" in infra_files
    assert "backend/app/auth.py" in infra_files
    assert "agent/src/index.ts" in infra_files
    assert "CLAUDE.md" in infra_files
    assert "backend/promotion_policy.yaml" in infra_files

    assert "backend/app/security.py" in critical_files
    assert "backend/app/auth.py" in critical_files
    assert "CLAUDE.md" in critical_files
    assert "backend/promotion_policy.yaml" in critical_files


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_non_self_repo_empty():
    """Without approval_gate_config, non-self-repo projects use empty lists."""
    project = Project(name="Other Project", user_id="u1", is_self_repo=False)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_custom_config():
    """With approval_gate_config, projects use their custom file lists."""
    project = Project(
        name="Custom Project",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={
            "infra_files": ["src/main.py", "src/config.py", "src/db.py"],
            "critical_files": ["src/config.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert infra_files == {"src/main.py", "src/config.py", "src/db.py"}
    assert critical_files == {"src/config.py"}


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_custom_config_overrides_self_repo():
    """With approval_gate_config, self-repo projects use config instead of defaults."""
    project = Project(
        name="Self with Custom Config",
        user_id="u1",
        is_self_repo=True,
        approval_gate_config={
            "infra_files": ["custom/file1.py"],
            "critical_files": ["custom/file1.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # Should use custom config, not Blueprint defaults
    assert infra_files == {"custom/file1.py"}
    assert critical_files == {"custom/file1.py"}
    assert "backend/app/main.py" not in infra_files


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_partial_config_infra_only():
    """Partial config (only infra_files): critical_files defaults to infra_files."""
    project = Project(
        name="Partial Config",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={
            "infra_files": ["src/main.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # critical_files inferred from infra_files so any infra touch requires approval
    assert infra_files == {"src/main.py"}
    assert critical_files == {"src/main.py"}


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_partial_config_critical_only():
    """Partial config (only critical_files): infra_files defaults to critical_files."""
    project = Project(
        name="Partial Config Critical Only",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={
            "critical_files": ["src/auth.py"],
        },
    )

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # infra_files inferred from critical_files
    assert critical_files == {"src/auth.py"}
    assert infra_files == {"src/auth.py"}


@pytest.mark.asyncio
async def test_get_approval_gate_file_lists_none_project():
    """Handles None project gracefully."""
    infra_files, critical_files = _get_approval_gate_file_lists(None)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest_asyncio.fixture()
async def approval_gate_db(mocker):
    """Isolated in-memory DB for approval gate integration tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.mark.asyncio
async def test_approval_gate_config_persisted_in_db(approval_gate_db):
    """approval_gate_config is properly persisted and retrieved from DB."""
    config = {
        "infra_files": ["src/main.py", "src/auth.py"],
        "critical_files": ["src/auth.py"],
    }
    project = Project(
        name="Persistent Config",
        user_id="u1",
        approval_gate_config=config,
    )
    approval_gate_db.add(project)
    await approval_gate_db.commit()
    await approval_gate_db.refresh(project)

    # Verify config is retrieved correctly
    assert project.approval_gate_config == config
    infra_files, critical_files = _get_approval_gate_file_lists(project)
    assert infra_files == {"src/main.py", "src/auth.py"}
    assert critical_files == {"src/auth.py"}


@pytest.mark.asyncio
async def test_approval_gate_empty_config_returns_empty(approval_gate_db):
    """Empty approval_gate_config dict returns empty sets for all projects.

    Blueprint defaults are seeded into approval_gate_config by a migration
    for is_self_repo projects; code-level fallbacks have been removed.
    """
    project = Project(
        name="Self Repo Empty Config",
        user_id="u1",
        is_self_repo=True,
        approval_gate_config={},
    )
    approval_gate_db.add(project)
    await approval_gate_db.commit()
    await approval_gate_db.refresh(project)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    assert len(infra_files) == 0
    assert len(critical_files) == 0


@pytest.mark.asyncio
async def test_partial_config_infra_only_sets_requires_approval_on_critical_touch(
    approval_gate_db,
):
    """Integration: partial config (infra_files only) still gates merge when an infra
    file is touched, because critical_files is inferred from infra_files.

    Simulates the _process_post_commit → review-merge check:
    1. Project config has only infra_files (no explicit critical_files).
    2. A PR touches a file in infra_files.
    3. Because critical_files == infra_files, touched_critical is non-empty.
    4. The merge gate would set requires_approval=True.
    """
    project = Project(
        name="Infra Only Config",
        user_id="u1",
        is_self_repo=False,
        approval_gate_config={"infra_files": ["src/main.py", "src/db.py"]},
    )
    approval_gate_db.add(project)
    await approval_gate_db.commit()
    await approval_gate_db.refresh(project)

    infra_files, critical_files = _get_approval_gate_file_lists(project)

    # With partial config, critical_files must equal infra_files
    assert critical_files == infra_files == {"src/main.py", "src/db.py"}

    # Simulate what _process_post_commit does: derive touched_infra and touched_critical
    pr_filenames = ["src/main.py", "src/utils.py"]  # one infra file touched
    touched_infra = [f for f in pr_filenames if f in infra_files]
    touched_critical = [f for f in touched_infra if f in critical_files]

    # Verify that the merge gate would fire (non-empty touched_critical → requires_approval=True)
    assert touched_infra == ["src/main.py"]
    assert touched_critical == ["src/main.py"], (
        "partial config must not silently disable the critical-file merge gate"
    )


# ---------------------------------------------------------------------------
# Operator write-path → agent-facing seam integration
#
# The HTTP endpoint's own GET/PATCH/validation contract is covered in
# test_admin_project_config.py. These tests assert the cross-seam property
# that is unique to this module: a config written through the owner-gated
# admin endpoint flows straight into _get_approval_gate_file_lists — the
# helper the claim-response seam uses to hand agents their gate lists — so
# the write takes effect immediately with no agent-side change.
# ---------------------------------------------------------------------------


@pytest.fixture
async def gate_project_owner(db_session):
    """Non-self project where test-user-123 is the owner."""
    project = Project(name="Gate Owner Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
async def gate_project_member(db_session):
    """Project where test-user-123 is only a member (not owner)."""
    project = Project(name="Gate Member Project", user_id="some-other-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, user_id="test-user-123", role="member"))
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_patch_approval_gate_config_takes_effect_in_claim_seam(
    client: AsyncClient, db_session, gate_project_owner
):
    """An owner PATCH is immediately visible to the agent-facing gate helper.

    Writing infra/critical lists through the admin endpoint must round-trip
    into _get_approval_gate_file_lists with no agent-side change, since the
    claim response reads the same column.
    """
    project = gate_project_owner
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={"infra_files": ["src/main.py", "src/db.py"], "critical_files": ["src/db.py"]},
    )
    assert resp.status_code == 200, resp.text

    db_session.expire(project)
    await db_session.refresh(project)
    infra_files, critical_files = _get_approval_gate_file_lists(project)
    assert infra_files == {"src/main.py", "src/db.py"}
    assert critical_files == {"src/db.py"}


@pytest.mark.asyncio
async def test_patch_approval_gate_config_owner_only_does_not_mutate_seam(
    client: AsyncClient, db_session, gate_project_member
):
    """A non-owner PATCH is rejected (403) and leaves the gate lists empty."""
    project = gate_project_member
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/approval-gate-config",
        json={"infra_files": ["src/secret.py"]},
    )
    assert resp.status_code == 403, resp.text

    db_session.expire(project)
    await db_session.refresh(project)
    infra_files, critical_files = _get_approval_gate_file_lists(project)
    assert infra_files == set()
    assert critical_files == set()
