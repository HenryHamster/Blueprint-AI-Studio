"""Tests for boot-account attribution on warm-reuse claims.

A warm/backlog-reuse claim runs the newly-claimed task on the LLM credentials
frozen into the reused VM's boot secret manifest, but bypasses normal
select_max_plan_account. Without attribution the boot account's active-task
count is undercounted and its concurrency / daily caps can be silently
overrun. These tests assert:

  - ``_vm_boot_account_identity`` recovers the boot account from the VM's prior
    role chain, and returns None when no prior task on the VM recorded one.
  - a backlog-reused task is stamped with the VM's boot account/provider/model
    (not left unattributed), so the orchestrator's reconciliation and the
    completion-release path can count and release it.
  - the boot account's in-memory ``active_tasks`` is reserved immediately on
    claim so the cap math cannot be bypassed by reuse.
"""

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.max_plan_pool import (
    _account_by_id,
    _max_plan_pools,
    _max_plan_pool_lock,
    get_task_llm_account_id,
    get_task_llm_model,
    get_task_llm_provider,
    release_max_plan_account,
)
from app.models import Project, Task, TaskStatus
from app.routers.agent_warm_continuity import _vm_boot_account_identity

BOOT_ACCOUNT_ID = "acct-boot-warm-001"


async def _make_project(db_session, name="Boot Attribution"):
    project = Project(name=name, user_id="test-user-attr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_task(db_session, project_id, **overrides):
    defaults = {
        "project_id": project_id,
        "title": "warm task",
        "payloads": [{"type": "create", "prompt": "do X", "base_branch": "automain"}],
        "status": TaskStatus.QUEUED,
        "context": {"scope": "small"},
    }
    defaults.update(overrides)
    task = Task(**defaults)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _boot_ctx():
    """Context shape a cold dispatch leaves on the VM's root task."""
    return {
        "llm_account_id": BOOT_ACCOUNT_ID,
        "max_plan_account_id": BOOT_ACCOUNT_ID,
        "llm_provider": "anthropic",
        "llm_auth_mode": "oauth",
        "llm_model": "claude-opus-4-8",
    }


class _PoolAccount:
    """Seed a single in-memory pool account and clean it up."""

    def __init__(self, account_id=BOOT_ACCOUNT_ID, active_tasks=0):
        self.account_id = account_id
        self.active_tasks = active_tasks

    async def __aenter__(self):
        self.account = {
            "id": self.account_id,
            "user_id": "test-user-attr",
            "name": "boot-account",
            "enabled": True,
            "oauth_token": "tok-boot",
            "active_tasks": self.active_tasks,
        }
        async with _max_plan_pool_lock:
            _max_plan_pools.setdefault("test-user-attr", []).append(self.account)
            _account_by_id[self.account_id] = self.account
        return self.account

    async def __aexit__(self, *exc):
        async with _max_plan_pool_lock:
            _account_by_id.pop(self.account_id, None)
            _max_plan_pools.pop("test-user-attr", None)
        return False


@pytest.mark.asyncio
async def test_vm_boot_account_identity_finds_latest(db_session):
    """Returns the account/provider/model recorded by a prior task on the VM."""
    project = await _make_project(db_session)
    vm_id = "agent-vm-attr-identity"
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.COMPLETED,
        vm_instance_id=vm_id,
        context=_boot_ctx(),
    )

    identity = await _vm_boot_account_identity(db_session, vm_id)
    assert identity is not None
    assert identity["account_id"] == BOOT_ACCOUNT_ID
    assert identity["provider"] == "anthropic"
    assert identity["auth_mode"] == "oauth"
    assert identity["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_vm_boot_account_identity_none_when_unattributed(db_session):
    """Returns None when no prior task on the VM recorded an account."""
    project = await _make_project(db_session, name="No Boot Account")
    vm_id = "agent-vm-attr-none"
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.COMPLETED,
        vm_instance_id=vm_id,
        context={"scope": "small"},  # no llm_account_id
    )
    assert await _vm_boot_account_identity(db_session, vm_id) is None
    # Missing VM id is also None (local/test VMs).
    assert await _vm_boot_account_identity(db_session, None) is None


@pytest.mark.asyncio
async def test_backlog_reuse_attributes_boot_account_and_reserves(
    client: AsyncClient, db_session, mocker
):
    """A backlog-reused task is attributed to the VM's boot account and that
    account's active-task count is reserved so caps can't be bypassed."""
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )
    from test_warm_backlog_reuse import _FlagGuard

    async with _PoolAccount(active_tasks=0) as account:
        with _FlagGuard(backlog=True):
            project = await _make_project(db_session, name="Backlog Attr")
            vm_id = "agent-vm-attr-backlog"
            # The cold-dispatched root task on this VM carries the boot account.
            prior = await _make_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
                context=_boot_ctx(),
            )
            # A fresh QUEUED backlog task — never cold-dispatched, no account.
            backlog = await _make_task(db_session, project.id)

            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["task_id"] == str(backlog.id)

            db_session.expire(backlog)
            await db_session.refresh(backlog)
            # (a) attributed to the VM's boot account, not left unattributed.
            assert get_task_llm_account_id(backlog) == BOOT_ACCOUNT_ID
            assert get_task_llm_provider(backlog) == "anthropic"
            assert get_task_llm_model(backlog) == "claude-opus-4-8"
            # (b) active-task count reflects the reused work.
            assert account["active_tasks"] == 1

            # Completion release path reads the stamped account and frees it.
            await release_max_plan_account(BOOT_ACCOUNT_ID)
            assert account["active_tasks"] == 0


@pytest.mark.asyncio
async def test_vm_boot_account_identity_skips_unattributed_returns_older(db_session):
    """When the most recent task on a VM has no account but an older one does,
    _vm_boot_account_identity returns the older attributed task's account.

    This is the exact regression path the SQL filter (has_account + .limit(1))
    is designed to cover: without the filter, .limit(1) would return the
    unattributed latest task and the function would return None, silently
    under-counting the boot account's active tasks in cap math.
    """
    project = await _make_project(db_session, name="Multi-Task Attribution")
    vm_id = "agent-vm-attr-multi"

    # Older task: has the boot account stamped (cold-dispatched root).
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.COMPLETED,
        vm_instance_id=vm_id,
        context=_boot_ctx(),
    )
    # Newer task: no account (e.g. a queued backlog task that was reused but
    # whose context was never attributed — precisely the scenario being guarded).
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.COMPLETED,
        vm_instance_id=vm_id,
        context={"scope": "small"},  # no llm_account_id
    )

    identity = await _vm_boot_account_identity(db_session, vm_id)
    assert identity is not None, (
        "_vm_boot_account_identity returned None even though an older task on "
        "this VM has an account — the SQL has_account filter likely regressed"
    )
    assert identity["account_id"] == BOOT_ACCOUNT_ID
    assert identity["provider"] == "anthropic"
