"""Unit tests for the teardown serial-console forensics capture in
``app.vm_lifecycle``.

Covers ``_capture_and_persist_teardown_forensics`` and its wiring into
``_ensure_vm_deleted``:

  (a) a non-empty serial tail is persisted into ``task.context['teardown_forensics']``
      with all five keys, preserving pre-existing context keys;
  (b) an empty/None tail leaves ``task.context`` unmutated;
  (c) a capture that raises or times out NEVER blocks teardown — the VM delete
      still runs.
"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app import vm_lifecycle
from app.models import Project, Task, TaskStatus


@pytest_asyncio.fixture()
async def vm_db(mocker):
    """Isolated in-memory DB bound into app.vm_lifecycle.AsyncSessionLocal."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.vm_lifecycle.AsyncSessionLocal", Session)

    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_task(session, *, vm_id="vm-forensics", context=None):
    project = Project(
        name="forensics-proj",
        user_id="user1",
        integration_branch="main",
        promotion_target="main",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        context=context if context is not None else {},
    )
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@pytest.mark.asyncio
async def test_tail_captured_persists_all_keys_and_preserves_context(vm_db, mocker):
    t = await _make_task(vm_db, vm_id="vm-cap", context={"existing": "keep-me"})

    mocker.patch("app.compute.get_vm_zone", return_value="us-central1-f")
    mocker.patch(
        "app.compute._capture_serial_for_vm", return_value="BEACON_DEATH\nFATAL boom"
    )

    await vm_lifecycle._capture_and_persist_teardown_forensics("vm-cap", "reaper_orphan")

    await vm_db.refresh(t)
    forensics = t.context["teardown_forensics"]
    assert set(forensics.keys()) == {
        "reason",
        "captured_at",
        "vm_instance_id",
        "zone",
        "serial_tail",
    }
    assert forensics["reason"] == "reaper_orphan"
    assert forensics["vm_instance_id"] == "vm-cap"
    assert forensics["zone"] == "us-central1-f"
    assert forensics["serial_tail"] == "BEACON_DEATH\nFATAL boom"
    assert isinstance(forensics["captured_at"], str) and forensics["captured_at"]
    # Pre-existing context key must survive.
    assert t.context["existing"] == "keep-me"


@pytest.mark.asyncio
async def test_no_tail_leaves_context_unmutated(vm_db, mocker):
    t = await _make_task(vm_db, vm_id="vm-notail", context={"existing": "keep-me"})

    mocker.patch("app.compute.get_vm_zone", return_value="us-central1-f")
    mocker.patch("app.compute._capture_serial_for_vm", return_value=None)

    await vm_lifecycle._capture_and_persist_teardown_forensics("vm-notail", "teardown")

    await vm_db.refresh(t)
    # Contract: the no-tail path adds no forensics and preserves pre-existing
    # keys. We assert that directly rather than exact dict-equality, because the
    # Task before_insert listener (_stamp_task_provider_floor) legitimately adds
    # context['provides_capability'] to every new task — exact equality would be
    # brittle against that unrelated stamp.
    assert "teardown_forensics" not in t.context
    assert t.context["existing"] == "keep-me"


@pytest.mark.asyncio
async def test_empty_string_tail_leaves_context_unmutated(vm_db, mocker):
    t = await _make_task(vm_db, vm_id="vm-empty", context={})

    mocker.patch("app.compute.get_vm_zone", return_value="us-central1-f")
    mocker.patch("app.compute._capture_serial_for_vm", return_value="")

    await vm_lifecycle._capture_and_persist_teardown_forensics("vm-empty", "teardown")

    await vm_db.refresh(t)
    assert "teardown_forensics" not in t.context


@pytest.mark.asyncio
async def test_capture_exception_does_not_block_teardown(vm_db, mocker):
    """A capture that raises must not prevent the VM delete from running."""
    # The process-global inventory cache is unrelated to this contract and is
    # mutated by service tests. Pin it to a valid count so this test exercises
    # only the forensics-failure path.
    mocker.patch("app.vm_lifecycle._last_gcp_vm_count", 0)
    mocker.patch(
        "app.vm_lifecycle._capture_and_persist_teardown_forensics",
        side_effect=RuntimeError("serial RPC blew up"),
    )
    delete_mock = mocker.patch(
        "app.vm_lifecycle.delete_agent_vm",
        new_callable=mocker.AsyncMock,
        return_value=True,
    )
    # Skip the warm-pool DB guard.
    mocker.patch(
        "app.vm_lifecycle._vm_is_governed_pool_member",
        new_callable=mocker.AsyncMock,
        return_value=False,
    )

    result = await vm_lifecycle._ensure_vm_deleted("vm-boom", reason="terminal")

    assert result is True
    delete_mock.assert_awaited_once_with("vm-boom")


@pytest.mark.asyncio
async def test_capture_timeout_does_not_block_teardown(vm_db, mocker):
    """A capture that hangs past the timeout must not delay/block teardown."""

    async def _hang(*_a, **_k):
        # Block until cancelled by the teardown-forensics timeout — the point is
        # that the capture never returns on its own, so it must be event-driven
        # rather than a real multi-second sleep that CI would actually incur.
        await asyncio.Event().wait()

    mocker.patch(
        "app.vm_lifecycle._capture_and_persist_teardown_forensics", side_effect=_hang
    )
    mocker.patch("app.vm_lifecycle._TEARDOWN_FORENSICS_TIMEOUT", 0.01)
    delete_mock = mocker.patch(
        "app.vm_lifecycle.delete_agent_vm",
        new_callable=mocker.AsyncMock,
        return_value=True,
    )
    mocker.patch(
        "app.vm_lifecycle._vm_is_governed_pool_member",
        new_callable=mocker.AsyncMock,
        return_value=False,
    )

    result = await vm_lifecycle._ensure_vm_deleted("vm-hang", reason="terminal")

    assert result is True
    delete_mock.assert_awaited_once_with("vm-hang")
