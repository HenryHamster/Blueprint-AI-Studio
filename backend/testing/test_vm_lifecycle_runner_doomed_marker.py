"""Unit tests for the runner pseudo-VM doomed-marker guards in
``app.vm_lifecycle``.

A persistent fleet runner's tasks carry ``vm_instance_id = 'runner:<id>'`` —
a logical name, not a GCE instance. The runner stays alive across task
completions, so the orchestrator-initiated-delete marker ("a VM delete is in
flight; don't warm-claim into it") must never be recorded for one: a stale
marker makes ``next_role`` refuse every warm-claim from the runner for the
30-minute TTL while dispatch keeps pinning tasks to it, and each pinned task
then heartbeat-reaps at ~380s and requeues to the same runner (production
2026-07-11, runner dd59526e: 10 tasks reaped per cycle, 287 refused claims
in 24h).

Covers:
  (a) ``fire_and_forget_vm_delete`` on a runner id records no marker and
      schedules no background delete;
  (b) ``_ensure_vm_deleted`` on a runner id pops a pre-existing marker
      (parity with the warm-pool guard) and still returns True;
  (c) regression guard: a real agent-VM id still records the marker
      synchronously.
"""

import time

import pytest

from app import vm_lifecycle
from app.vm_lifecycle import (
    _ensure_vm_deleted,
    _orchestrator_deleted_vms,
    fire_and_forget_vm_delete,
    is_orchestrator_initiated_delete,
)

RUNNER_VM_ID = "runner:dd59526e-78f1-44ad-913e-85c72cd41151"


@pytest.fixture(autouse=True)
def _clean_marker_state():
    _orchestrator_deleted_vms.clear()
    yield
    _orchestrator_deleted_vms.clear()


@pytest.mark.asyncio
async def test_fire_and_forget_never_marks_runner_vm_doomed(mocker):
    ensure = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )

    fire_and_forget_vm_delete(RUNNER_VM_ID, reason="agent_completion")

    doomed, reason = is_orchestrator_initiated_delete(RUNNER_VM_ID)
    assert not doomed
    assert reason is None
    ensure.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_vm_deleted_pops_stale_runner_marker():
    _orchestrator_deleted_vms[RUNNER_VM_ID] = (time.time(), "agent_completion")

    assert await _ensure_vm_deleted(RUNNER_VM_ID, reason="agent_completion") is True

    doomed, _ = is_orchestrator_initiated_delete(RUNNER_VM_ID)
    assert not doomed
    assert RUNNER_VM_ID not in _orchestrator_deleted_vms


@pytest.mark.asyncio
async def test_fire_and_forget_still_marks_real_agent_vm(mocker):
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    vm_id = "agent-vm-real-teardown"
    existing_deletes = set(vm_lifecycle._background_vm_deletes)

    fire_and_forget_vm_delete(vm_id, reason="agent_completion")

    doomed, reason = is_orchestrator_initiated_delete(vm_id)
    assert doomed
    assert reason == "agent_completion"
    # Drain only the task created by this call. The process-global set can
    # contain unrelated deletes scheduled by earlier suite tests, potentially
    # on a different event loop.
    new_deletes = set(vm_lifecycle._background_vm_deletes) - existing_deletes
    assert len(new_deletes) == 1
    for task in new_deletes:
        await task
