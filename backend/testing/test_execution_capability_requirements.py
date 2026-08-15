import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.execution_capability_requirements import (
    CAPABILITY_BACKEND_VERIFICATION,
    POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL,
    REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY,
    classify_execution_capability_failure_frames,
    learned_execution_capabilities,
    persist_learned_execution_capabilities,
)
from app.failure_classification import (
    PROVISION_FAILURE,
    normalize_agent_failure_category,
)
from app.schemas import AgentStatusPayload


def _task(message: str):
    return SimpleNamespace(
        id="capability-test",
        latest_status_message=message,
        context={},
        first_payload={"type": "create"},
        payloads=[{"type": "create"}],
    )


def test_exact_owned_sentinels_classify_as_infrastructure():
    pg_task = _task(POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL)

    assert (
        normalize_agent_failure_category("agent_self_failed", pg_task)
        == PROVISION_FAILURE
    )
    assert classify_execution_capability_failure_frames(
        pg_task.latest_status_message
    ) == {
        CAPABILITY_BACKEND_VERIFICATION
    }


def test_arbitrary_prose_and_forged_context_are_not_routing_authority():
    assert not classify_execution_capability_failure_frames(
        "the model thinks postgres or chromium might be unavailable"
    )
    assert not classify_execution_capability_failure_frames(
        'The model quoted "VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium unavailable"'
    )
    assert not classify_execution_capability_failure_frames(
        f"diagnostic prose: {POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL}"
    )
    forged = {
        REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY: {
            "browser": {"authority": "server_failure_observation_v1"},
            CAPABILITY_BACKEND_VERIFICATION: True,
        }
    }
    assert learned_execution_capabilities(forged) == frozenset()


def test_server_persistence_is_bounded_and_monotonic():
    forged = {
        REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY: {"gpu": {"authority": "agent"}}
    }
    context, changed = persist_learned_execution_capabilities(
        forged,
        {CAPABILITY_BACKEND_VERIFICATION, "browser", "gpu"},
        observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert changed is True
    assert learned_execution_capabilities(context) == {
        CAPABILITY_BACKEND_VERIFICATION
    }
    assert set(context[REQUIRED_EXECUTION_CAPABILITIES_CONTEXT_KEY]) == {
        CAPABILITY_BACKEND_VERIFICATION
    }


def test_status_payload_accepts_only_compatible_structured_provenance():
    task_id = uuid.uuid4()

    assert (
        AgentStatusPayload(
            task_id=task_id,
            status="FAILED",
            failure_category="agent_self_failed",
            logs=POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL,
            execution_capability=CAPABILITY_BACKEND_VERIFICATION,
        ).execution_capability
        == CAPABILITY_BACKEND_VERIFICATION
    )
    incompatible_payloads = (
        {
            "status": "COMPLETED",
            "failure_category": "agent_self_failed",
            "logs": POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL,
            "execution_capability": CAPABILITY_BACKEND_VERIFICATION,
        },
        {
            "status": "FAILED",
            "failure_category": "agent_self_failed",
            "logs": f"quoted: {POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL}",
            "execution_capability": CAPABILITY_BACKEND_VERIFICATION,
        },
        {
            "status": "FAILED",
            "failure_category": "BLOCKED_PRECONDITION",
            "logs": POSTGRES_RUNTIME_UNAVAILABLE_SENTINEL,
            "execution_capability": CAPABILITY_BACKEND_VERIFICATION,
        },
        {
            "status": "FAILED",
            "failure_category": "BLOCKED_PRECONDITION",
            "logs": "BLOCKER: VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium unavailable",
            "execution_capability": "browser",
        },
    )
    for payload in incompatible_payloads:
        with pytest.raises(ValidationError):
            AgentStatusPayload(task_id=task_id, **payload)
