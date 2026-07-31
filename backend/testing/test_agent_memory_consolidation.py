"""Contract checks for the no-workspace memory worker seam."""

from __future__ import annotations

import uuid

from app.payload_schemas import validate_task_payload
from app.task_capabilities import (
    agent_capability_slice,
    get_task_capability,
    task_capability_contract,
)


def test_memory_consolidation_is_a_bounded_non_repo_capability():
    capability = get_task_capability("memory_consolidation")
    assert capability is not None
    assert capability.workspace_requirement == "none"
    assert capability.provider_configurable is False
    assert capability.allowed_providers == frozenset({"anthropic"})
    assert capability.requires_code is False
    assert capability.user_launchable is False
    published = task_capability_contract()["memory_consolidation"]
    assert published["workspace_requirement"] == "none"
    assert published["allowed_providers"] == ["anthropic"]
    claimed = agent_capability_slice("memory_consolidation")
    assert claimed is not None and claimed["workspace_requirement"] == "none"


def test_memory_consolidation_payload_has_required_job_and_hard_caps():
    job_id = uuid.uuid4()
    payload = validate_task_payload(
        {
            "type": "memory_consolidation",
            "job_id": str(job_id),
            "max_prompt_tokens": 100,
            "max_completion_tokens": 50,
            "max_total_tokens": 125,
            "max_input_bytes": 1024,
        }
    )
    assert payload["job_id"] == job_id
    assert payload["max_input_bytes"] == 1024
