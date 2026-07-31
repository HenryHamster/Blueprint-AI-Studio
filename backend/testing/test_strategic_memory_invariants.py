from __future__ import annotations

import uuid

from app.strategic_memory_invariants import audit_ledger_records


def _clean_snapshot(project_id: uuid.UUID) -> dict[str, object]:
    source_id = uuid.uuid4()
    item_id = uuid.uuid4()
    return {
        "state": {"project_id": project_id, "ledger_revision": 4, "source_high_watermark": 1},
        "sources": [
            {"id": source_id, "project_id": project_id, "source_sequence": 1, "payload_state": "active"}
        ],
        "items": [{"id": item_id, "project_id": project_id, "current_version": 1, "current_status": "active"}],
        "versions": [{"item_id": item_id, "version": 1, "project_id": project_id}],
        "transitions": [
            {
                "id": uuid.uuid4(), "project_id": project_id, "item_id": item_id,
                "item_version": 1, "transition": "activate", "source_id": source_id,
                "recorded_source_sequence": 1, "target_item_id": None, "target_version": None,
            }
        ],
        "item_sources": [{"project_id": project_id, "item_id": item_id, "item_version": 1, "source_id": source_id, "span_index": 0}],
        "edges": [],
        "edge_sources": [],
        "barriers": [],
        "barrier_events": [],
        "projection": {
            "project_id": project_id,
            "ledger_revision": 4,
            "canonical_text": "",
            "canonical_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
    }


def _audit(snapshot: dict[str, object], project_id: uuid.UUID):
    return audit_ledger_records(project_id=project_id, **snapshot)


def test_clean_ledger_snapshot_has_no_violations() -> None:
    project_id = uuid.uuid4()
    report = _audit(_clean_snapshot(project_id), project_id)

    assert report.is_clean


def test_auditor_reports_head_and_provenance_without_content() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    items = snapshot["items"]
    assert isinstance(items, list)
    items[0]["current_version"] = 9
    report = _audit(snapshot, project_id)

    assert {violation.code for violation in report.violations} == {"item_head_missing_version"}
    assert all("canonical" not in ":".join(violation.record_ids) for violation in report.violations)


def test_auditor_reports_foreign_references_and_invalidated_provenance() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["payload_state"] = "deleted"
    snapshot["edges"] = [
        {
            "id": uuid.uuid4(), "project_id": uuid.uuid4(),
            "from_node_id": snapshot["items"][0]["id"], "from_version": 1,
            "to_node_id": uuid.uuid4(), "to_version": 1, "relation": "invented_relation",
        }
    ]
    report = _audit(snapshot, project_id)

    assert {violation.code for violation in report.violations} >= {
        "current_item_without_active_provenance",
        "foreign_project_edge",
        "edge_missing_endpoint_version",
        "invalid_edge_relation",
    }


def test_auditor_reports_transition_and_projection_fence_defects() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    transitions = snapshot["transitions"]
    assert isinstance(transitions, list)
    transitions[0]["source_id"] = uuid.uuid4()
    transitions[0]["transition"] = "not_a_transition"
    projection = snapshot["projection"]
    assert isinstance(projection, dict)
    projection["ledger_revision"] = 5
    projection["canonical_sha256"] = "bad"
    report = _audit(snapshot, project_id)

    assert {violation.code for violation in report.violations} >= {
        "transition_missing_source",
        "invalid_transition_type",
        "projection_ahead_of_ledger",
        "invalid_projection_hash",
    }


def test_auditor_enforces_supersede_target_semantics() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    transition = snapshot["transitions"][0]
    transition["transition"] = "supersede"
    report = _audit(snapshot, project_id)
    assert "supersede_missing_target" in {violation.code for violation in report.violations}

    transition["transition"] = "retract"
    transition["target_item_id"] = snapshot["items"][0]["id"]
    transition["target_version"] = 1
    report = _audit(snapshot, project_id)
    assert "nonsupersede_transition_has_target" in {violation.code for violation in report.violations}


def test_auditor_detects_self_and_cyclic_supersession() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    first_item = snapshot["items"][0]["id"]
    second_item = uuid.uuid4()
    source_id = snapshot["sources"][0]["id"]
    snapshot["items"].append({
        "id": second_item, "project_id": project_id, "current_version": 1, "current_status": "active",
    })
    snapshot["versions"].append({"item_id": second_item, "version": 1, "project_id": project_id})
    snapshot["item_sources"].append({
        "project_id": project_id, "item_id": second_item, "item_version": 1, "source_id": source_id, "span_index": 1,
    })
    snapshot["transitions"] = [
        {
            "id": uuid.uuid4(), "project_id": project_id, "item_id": first_item, "item_version": 1,
            "transition": "supersede", "source_id": source_id, "recorded_source_sequence": 1,
            "target_item_id": second_item, "target_version": 1,
        },
        {
            "id": uuid.uuid4(), "project_id": project_id, "item_id": second_item, "item_version": 1,
            "transition": "supersede", "source_id": source_id, "recorded_source_sequence": 1,
            "target_item_id": first_item, "target_version": 1,
        },
    ]
    report = _audit(snapshot, project_id)
    assert "cyclic_supersession" in {violation.code for violation in report.violations}

    snapshot["transitions"] = [
        {
            "id": uuid.uuid4(), "project_id": project_id, "item_id": first_item, "item_version": 1,
            "transition": "supersede", "source_id": source_id, "recorded_source_sequence": 1,
            "target_item_id": first_item, "target_version": 1,
        }
    ]
    report = _audit(snapshot, project_id)
    assert "supersede_self_target" in {violation.code for violation in report.violations}


def test_auditor_rejects_barrier_event_history_and_generation_regression() -> None:
    project_id = uuid.uuid4()
    snapshot = _clean_snapshot(project_id)
    source_id = snapshot["sources"][0]["id"]
    item_id = snapshot["items"][0]["id"]
    barrier_id = uuid.uuid4()
    snapshot["barriers"] = [{
        "id": barrier_id, "project_id": project_id, "correction_source_id": source_id,
        "candidate_item_id": item_id, "candidate_version": 1, "context_generation": 3,
    }]
    snapshot["barrier_events"] = [{
        "id": uuid.uuid4(), "project_id": project_id, "barrier_id": barrier_id,
        "event_type": "resolved", "source_id": source_id, "recorded_source_sequence": 2,
        "context_generation": 2, "created_at": 1,
    }]
    snapshot["state"]["context_authority_generation"] = 3

    report = _audit(snapshot, project_id)

    assert {violation.code for violation in report.violations} >= {
        "barrier_event_authority_generation_regression",
        "invalid_barrier_event_history",
    }
