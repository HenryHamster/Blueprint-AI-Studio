"""Focused tests for receipt-bound Workday replay projections."""

from app.strategic_workday_mutation_rebind import workday_operation_replay_response


def test_completion_debt_replay_projects_the_exact_debt_id():
    receipt = {
        "op": "dispose_completion_debt",
        "entity_kind": "completion_debt",
        "entity_id": "00000000-0000-0000-0000-000000000123",
        "effect": "updated",
    }

    assert workday_operation_replay_response(receipt) == {
        "status": "ok",
        "op": "dispose_completion_debt",
        "idempotent_replay": True,
        "debt_id": "00000000-0000-0000-0000-000000000123",
    }


def test_qualification_intent_replay_projects_exact_intent_id():
    receipt = {
        "op": "request_qualification",
        "entity_kind": "release_qualification_intent",
        "entity_id": "00000000-0000-0000-0000-000000000456",
        "effect": "created",
    }

    assert workday_operation_replay_response(receipt) == {
        "status": "ok",
        "op": "request_qualification",
        "idempotent_replay": True,
        "qualification_intent_id": "00000000-0000-0000-0000-000000000456",
    }
