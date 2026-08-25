"""Pre-dispatch terminal fail for a task whose payload can never run.

Production 2026-08-23 (Blueprint project 37d445e1): four
``guard_suppression_remediation`` tasks were spawned with a ``fix`` payload
carrying no ``branch``.  ``fix`` is exactly the payload type whose agent
handler (``handleFix`` in agent/src/handlers/review-fix.ts) validates
``payload.branch`` on its first line of real work and throws
``branch is required``.  Every one of them provisioned a VM, booted the agent,
claimed the task, died in ~2 seconds, and repeated three more times — 16 VM
provisions, four dead tasks, zero work possible.

The payload is deterministic in the persisted row, so the malformed-ness is
knowable BEFORE a VM is spent.  This filter is the pre-dispatch seam that
spends none.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.failure_classification import (
    CODE_ERROR,
    INFRA_CLASSES,
    MALFORMED_TASK_PAYLOAD,
)
from app.models import TaskStatus
from app.orchestrator_filters import filter_malformed_payloads
from testing.factories import make_task

BRANCHLESS_FIX = {
    "type": "fix",
    "prompt": "Remove the named inherited guard failure.",
}
RUNNABLE_FIX = {
    "type": "fix",
    "prompt": "Remove the named inherited guard failure.",
    "pr_number": 17786,
    "branch": "blueprint/fix-4fdd6113",
}


def _mock_session():
    session = MagicMock()
    session.commit = AsyncMock()
    return session


def _candidate(payload, project_id=None):
    return make_task(
        project_id=project_id or uuid.uuid4(),
        status=TaskStatus.QUEUED,
        payloads=[payload],
        title="fix: resolve recurring vitest:agent baseline failure",
    )


class TestFilterMalformedPayloads:
    @pytest.mark.asyncio
    async def test_branchless_fix_is_dropped_before_a_vm_is_spent(self):
        task = _candidate(BRANCHLESS_FIX)
        session = _mock_session()
        broadcast = AsyncMock()

        kept = await filter_malformed_payloads(
            [task.id], [task], session, broadcast
        )

        assert kept == [], "a task that can never run must not reach dispatch"
        assert task.status == TaskStatus.FAILED
        session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_failure_is_charged_to_the_platform_not_the_agent(self):
        task = _candidate(BRANCHLESS_FIX)

        await filter_malformed_payloads(
            [task.id], [task], _mock_session(), AsyncMock()
        )

        category = (task.context or {}).get("failure_category")
        assert category == MALFORMED_TASK_PAYLOAD
        assert category != CODE_ERROR
        # Terminal: the resurrection sweep flips only INFRA_CLASSES members back
        # to QUEUED, and re-queuing re-reads the identical malformed row.
        assert MALFORMED_TASK_PAYLOAD not in INFRA_CLASSES

    @pytest.mark.asyncio
    async def test_reason_names_the_field_that_is_missing(self):
        task = _candidate(BRANCHLESS_FIX)

        await filter_malformed_payloads(
            [task.id], [task], _mock_session(), AsyncMock()
        )

        msg = task.latest_status_message or ""
        assert "branch" in msg.lower(), msg
        assert "fix" in msg.lower(), msg

    @pytest.mark.asyncio
    async def test_no_retry_budget_is_consumed(self):
        task = _candidate(BRANCHLESS_FIX)
        task.attempt_count = 0

        await filter_malformed_payloads(
            [task.id], [task], _mock_session(), AsyncMock()
        )

        assert task.attempt_count == 0

    @pytest.mark.asyncio
    async def test_runnable_payload_passes_through_untouched(self):
        task = _candidate(RUNNABLE_FIX)
        session = _mock_session()

        kept = await filter_malformed_payloads(
            [task.id], [task], session, AsyncMock()
        )

        assert kept == [task.id]
        assert task.status == TaskStatus.QUEUED
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_typeless_payload_is_left_alone(self):
        """Only a payload that DECLARES a type and then violates that type's
        contract is in scope; a typeless row is a different question and must
        not be terminalized here."""
        task = _candidate({})
        session = _mock_session()

        kept = await filter_malformed_payloads(
            [task.id], [task], session, AsyncMock()
        )

        assert kept == [task.id]
        assert task.status == TaskStatus.QUEUED
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_the_malformed_candidate_is_dropped(self):
        pid = uuid.uuid4()
        bad = _candidate(BRANCHLESS_FIX, project_id=pid)
        good = _candidate(RUNNABLE_FIX, project_id=pid)

        kept = await filter_malformed_payloads(
            [bad.id, good.id], [bad, good], _mock_session(), AsyncMock()
        )

        assert kept == [good.id]
        assert bad.status == TaskStatus.FAILED
        assert good.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_empty_eligible_list_short_circuits(self):
        session = _mock_session()

        assert await filter_malformed_payloads([], [], session, AsyncMock()) == []
        session.commit.assert_not_awaited()
