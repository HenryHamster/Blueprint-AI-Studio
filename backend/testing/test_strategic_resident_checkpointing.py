"""Focused tests for canonical Resident creation and v1 checkpointing."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, StrategicResident, StrategicResidentCheckpoint
from app.strategic_resident_checkpointing import (
    CHECKPOINT_MAX_BYTES,
    build_initial_strategic_checkpoint,
    initialize_strategic_resident,
)


@pytest_asyncio.fixture()
async def resident_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paused", "expected_policy"), [(False, "active"), (True, "paused")]
)
async def test_new_project_commits_with_one_resident_and_v1_checkpoint(
    resident_db, paused, expected_policy
):
    project = Project(
        name="New Resident Project",
        user_id="operator",
        paused=paused,
        problem_model={"purpose": "Own project outcomes", "_cache": "drop"},
        vision={
            "purpose": "Ship deliberately",
            "_resident_session": "drop",
            "_resident_strategic_session": {
                "_session_summary": "Legacy summary",
                "_decisions_reg": json.dumps(
                    {
                        "version": 1,
                        "decisions": [
                            {"id": "decision-1", "summary": "Ship deliberately"}
                        ],
                    }
                ),
                "_message_cursor": "opaque-cursor",
                "_exam_ledger": "drop raw operational history",
            },
        },
        directions=[
            {"id": "d-1", "text": "Build Resident", "status": "active"},
            {"id": "d-old", "text": "Done", "status": "completed"},
        ],
        strategic_memory_notes=[{"note": "Keep conversation calm"}],
        running_update="Resident control plane is initializing.",
    )
    resident_db.add(project)
    await resident_db.flush()

    first = await initialize_strategic_resident(
        resident_db, project.id, locked_project=project
    )
    second = await initialize_strategic_resident(
        resident_db, project.id, locked_project=project
    )
    assert second.project_id == first.project_id
    await resident_db.commit()

    stored = await resident_db.get(StrategicResident, project.id)
    assert stored is not None
    assert stored.background_policy == expected_policy
    assert stored.engine_owner == "legacy"
    assert stored.authority_generation == 1
    assert stored.current_checkpoint_id is not None
    checkpoint = await resident_db.get(
        StrategicResidentCheckpoint, stored.current_checkpoint_id
    )
    assert checkpoint is not None
    assert checkpoint.version == 1
    assert checkpoint.checkpoint["migration"]["source"] == "runtime_initialization"
    assert checkpoint.checkpoint["charter"]["purpose"] == "Own project outcomes"
    assert (
        checkpoint.checkpoint["working_strategy"]["running_update"]
        == "Resident control plane is initializing."
    )
    assert [
        item["id"] for item in checkpoint.checkpoint["working_strategy"]["directions"]
    ] == ["d-1"]
    assert (
        checkpoint.checkpoint["working_strategy"]["legacy_session_summary"]
        == "Legacy summary"
    )
    assert (
        checkpoint.checkpoint["working_strategy"]["legacy_decisions_register"][
            "decisions"
        ][0]["id"]
        == "decision-1"
    )
    assert checkpoint.checkpoint["cursors"]["legacy_message_cursor"] == "opaque-cursor"
    assert "_exam_ledger" not in json.dumps(checkpoint.checkpoint)
    assert len(checkpoint.content_hash) == 64
    assert len(checkpoint.checkpoint) > 0


def test_runtime_and_migration_use_the_same_v1_projection_shape():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0611_strategic_resident_control_plane.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0611_checkpoint_parity", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    project = {
        "problem_model": {"purpose": "Parity"},
        "vision": {
            "purpose": "Same shape",
            "_resident_strategic_session": {
                "_session_summary": "é" * 6_000,
                "_decisions_reg": json.dumps(
                    {
                        "version": 1,
                        "decisions": [
                            {"id": str(index), "summary": f"decision-{index}"}
                            for index in range(60)
                        ],
                    }
                ),
                "_message_cursor": "cursor-" + ("x" * 3_000),
                "_exam_ledger": "must-not-project",
            },
        },
        "directions": [{"id": "d-1", "text": "Parity", "status": "active"}],
        "strategic_memory_notes": [{"note": "Remember parity"}],
        "running_update": "One canonical shape.",
    }

    runtime_body, runtime_hash = build_initial_strategic_checkpoint(
        project, source="legacy_projection"
    )
    migration_body, migration_hash = migration._build_initial_checkpoint(project)

    assert runtime_body == migration_body
    assert runtime_hash == migration_hash
    assert len(migration._canonical_json(runtime_body)) <= CHECKPOINT_MAX_BYTES
    strategy = runtime_body["working_strategy"]
    assert len(strategy["legacy_session_summary"].encode("utf-8")) == 10_000
    assert len(strategy["legacy_decisions_register"]["decisions"]) == 50
    assert len(runtime_body["cursors"]["legacy_message_cursor"]) == 2_000
    assert "_exam_ledger" not in json.dumps(runtime_body)


def test_invalid_legacy_decisions_are_stamped_without_ambiguous_raw_text():
    body, _content_hash = build_initial_strategic_checkpoint(
        {
            "vision": {
                "_resident_strategic_session": {
                    "_decisions_reg": "{not-json",
                }
            }
        }
    )

    strategy = body["working_strategy"]
    assert strategy["legacy_decisions_register_malformed"] is True
    assert "legacy_decisions_register" not in strategy


def test_every_production_project_constructor_initializes_resident():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    expected = {
        app_dir / "admin.py": 2,
        app_dir / "routers" / "admin_onboard.py": 1,
        app_dir / "routers" / "projects.py": 1,
    }
    for path, constructor_count in expected.items():
        source = path.read_text(encoding="utf-8")
        assert source.count("project = Project(") == constructor_count
        assert source.count("await initialize_strategic_resident(") >= constructor_count

    onboard = expected.keys()
    onboard_path = next(path for path in onboard if path.name == "admin_onboard.py")
    source = onboard_path.read_text(encoding="utf-8")
    seeded = source.index("if seeded_message is not None:")
    savepoint = source.index("session.begin_nested()", seeded)
    coalesce = source.index("coalesce_shadow_resident_turn(", savepoint)
    commit = source.index("await session.commit()", coalesce)
    assert seeded < savepoint < coalesce < commit
