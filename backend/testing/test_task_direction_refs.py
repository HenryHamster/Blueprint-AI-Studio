import uuid

from app.models import Task
from app.task_direction_refs import (
    first_task_direction_id,
    task_primary_direction_ids,
    task_related_direction_ids,
)


def test_primary_direction_prefers_payload_ownership_over_plan_references():
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "research", "direction_id": "d-payload"}],
        plan_output={"source_direction_id": "d-plan"},
    )

    assert first_task_direction_id(task) == "d-payload"
    assert task_primary_direction_ids(task) == ["d-payload"]
    assert task_related_direction_ids(task) == ["d-payload", "d-plan"]


def test_primary_direction_uses_current_direction_over_payload_direction_list():
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[
            {
                "type": "research",
                "current_direction_id": "d-current",
                "directions": [{"id": "d-related"}],
            }
        ],
    )

    assert task_primary_direction_ids(task) == ["d-current"]
    assert task_related_direction_ids(task) == ["d-current", "d-related"]


def test_context_source_and_target_direction_ids_are_related_not_primary():
    task = Task(
        project_id=uuid.uuid4(),
        context={
            "source_direction_id": "d-source",
            "target_direction_id": "d-target",
        },
    )

    assert first_task_direction_id(task) is None
    assert task_primary_direction_ids(task) == []
    assert task_related_direction_ids(task) == ["d-source", "d-target"]
