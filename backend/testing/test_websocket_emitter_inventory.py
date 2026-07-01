"""Inventory guard for backend websocket emitter call sites.

This is intentionally a source-inventory test, not a payload behavior test.
Runtime payload contracts stay covered by the event-specific tests; this file
keeps new websocket emitters from appearing without review and classification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from websocket_event_inventory import (
    build_websocket_event_inventory,
    inventory_signature,
)


SNAPSHOT_PATH = (
    Path(__file__).parent / "__fixtures__" / "websocket_event_inventory_v1.json"
)
VALID_CLASSIFICATIONS = {
    "legacy_global_event",
    "legacy_project_event",
    "legacy_review_required",
    "message_v1_payload",
    "raw_websocket_control",
    "strategic_v1_emitter",
    "strategic_v1_helper",
    "transport",
    "workbench_v1_emitter",
    "workbench_v1_helper",
}


def _load_snapshot() -> dict[str, Any]:
    assert SNAPSHOT_PATH.exists(), (
        "Missing websocket emitter inventory snapshot. Generate it from "
        "build_websocket_event_inventory(Path('backend/app')) after reviewing "
        "the emitter classifications."
    )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _current_inventory() -> list[dict[str, Any]]:
    app_dir = Path(__file__).parent.parent / "app"
    return build_websocket_event_inventory(app_dir)


def _snapshot_inventory(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    emitters = snapshot.get("emitters")
    assert isinstance(emitters, list), "Snapshot must contain an emitters list."
    return emitters


def test_websocket_emitter_inventory_snapshot_matches_source():
    snapshot = _load_snapshot()
    expected = [inventory_signature(record) for record in _snapshot_inventory(snapshot)]
    current = [inventory_signature(record) for record in _current_inventory()]

    missing = [record for record in expected if record not in current]
    added = [record for record in current if record not in expected]

    assert not missing and not added, (
        "Backend websocket emitter inventory changed. Review every added or "
        "removed call site, then update backend/testing/__fixtures__/"
        "websocket_event_inventory_v1.json.\n\n"
        f"Missing from source: {json.dumps(missing[:8], indent=2, sort_keys=True)}\n\n"
        f"Added in source: {json.dumps(added[:8], indent=2, sort_keys=True)}"
    )


def test_websocket_emitter_inventory_entries_are_classified():
    snapshot = _load_snapshot()
    emitters = _snapshot_inventory(snapshot)
    assert emitters, "Snapshot must include at least one websocket emitter."

    required = {
        "source_path",
        "scope",
        "lineno",
        "callee",
        "canonical_callee",
        "event",
        "event_is_literal",
        "event_expression",
        "payload_arg_kind",
        "workbench_resource",
        "workbench_action",
        "classification",
        "notes",
        "occurrence",
    }
    classifications = {entry.get("classification") for entry in emitters}
    for entry in emitters:
        missing = required - set(entry)
        assert not missing, f"Emitter entry is missing fields {missing}: {entry}"
        assert entry["classification"] in VALID_CLASSIFICATIONS, (
            f"Invalid websocket emitter classification: {entry}"
        )

    assert "workbench_v1_helper" in classifications
    assert "strategic_v1_helper" in classifications
    assert "message_v1_payload" in classifications
    assert "legacy_project_event" in classifications
    assert "legacy_review_required" in classifications
