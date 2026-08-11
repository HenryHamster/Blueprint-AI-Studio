"""Shared contract assertions for agent task responses.

Used by test_agent_tasks.py and test_agent_strategic_fetch.py to validate
the flat+children contract for GET /api/agent/projects/{project_id}/tasks
and /api/agent/projects/{project_id}/tasks/{task_id}.
"""

# blueprint-test-module: intentional-zero-tests -- imported contract assertion helpers; pytest tests live in test_agent_tasks.py and test_agent_strategic_fetch.py

def assert_task_item_contract(item: dict) -> None:
    """Assert that a single task item matches the flat+children contract.

    Each task item in the response must include:
    - Flat fields: id, parent_task_id, status, task_type, title, created_at, updated_at, latest_status_message
    - children array (always present, can be empty)
    - Optional outcome fields for terminal tasks: pr_url, failure_reason, completed_at
    - Optional blocking fields: blocked_reason, blocker_kind, park_reason
    """
    # Core required fields
    assert "id" in item, "task item missing required field: id"
    assert "parent_task_id" in item, "task item missing required field: parent_task_id"
    assert "status" in item, "task item missing required field: status"
    assert "task_type" in item, "task item missing required field: task_type"
    assert "title" in item, "task item missing required field: title"
    assert "created_at" in item, "task item missing required field: created_at"
    assert "updated_at" in item, "task item missing required field: updated_at"
    assert "latest_status_message" in item, "task item missing required field: latest_status_message"

    # Flat + children contract
    assert "children" in item, "task item missing required field: children"
    assert isinstance(item["children"], list), f"children must be a list, got {type(item['children'])}"


def assert_task_list_response_contract(body: dict) -> None:
    """Assert that a list response matches the flat+children contract.

    The response must include:
    - status: success
    - data: list of task items
    - count: number of items
    """
    assert body.get("status") == "success", f"expected status=success, got {body.get('status')}"
    assert "data" in body, "response missing required field: data"
    assert isinstance(body["data"], list), f"data must be a list, got {type(body['data'])}"
    assert "count" in body, "response missing required field: count"
    assert body["count"] == len(body["data"]), f"count {body['count']} does not match data length {len(body['data'])}"

    # Validate each item against the contract
    for item in body["data"]:
        assert_task_item_contract(item)
