"""Invariant test ensuring backend event_type emissions stay in sync with frontend classifications.

This test extracts all event_type values emitted by notification_dispatch.py and
validates them against a JSON snapshot. The snapshot serves as a single source of
truth for which event types exist, their classification (escalation/high_signal/routine),
and which backend function emits them.
"""

import json
import re
from pathlib import Path


def test_event_type_snapshot_exists():
    """Verify the snapshot file exists."""
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"
    assert snapshot_path.exists(), (
        "event_type_snapshot.json must exist in backend/testing/"
    )


def test_extract_event_types_from_source():
    """Extract all event_type="..." strings from notification_dispatch.py.

    This test validates that the snapshot is complete and accurate by:
    1. Extracting all event_type string literals from the source
    2. Comparing against the snapshot
    3. Failing with a clear message if any event_type is missing
    """
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"
    source_path = Path(__file__).parent.parent / "app" / "notification_dispatch.py"

    assert snapshot_path.exists(), "event_type_snapshot.json not found"
    assert source_path.exists(), "notification_dispatch.py not found"

    # Load the snapshot
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    snapshot_events = {et["name"] for et in snapshot["event_types"]}

    # Extract event_type="..." from source using regex
    with open(source_path) as f:
        source_code = f.read()

    # Find all event_type="..." patterns (both function args and dict keys)
    # Matches: event_type="value" and "event_type": "value"
    pattern = r'(?:event_type|"event_type")\s*[:=]\s*["\']([^"\']+)["\']'
    source_events = set(re.findall(pattern, source_code))

    # Validate no drift
    missing_in_snapshot = source_events - snapshot_events
    extra_in_snapshot = snapshot_events - source_events

    error_parts = []
    if missing_in_snapshot:
        error_parts.append(
            f"Event types emitted by backend but missing from snapshot: {sorted(missing_in_snapshot)}\n"
            f"  → Add these to backend/testing/event_type_snapshot.json with appropriate classification "
            f"('escalation', 'high_signal', or 'routine')"
        )

    if extra_in_snapshot:
        error_parts.append(
            f"Event types in snapshot but not emitted by backend: {sorted(extra_in_snapshot)}\n"
            f"  → These may have been removed. Delete from backend/testing/event_type_snapshot.json"
        )

    if error_parts:
        raise AssertionError("\n\n".join(error_parts))


def test_snapshot_has_required_fields():
    """Verify each event_type entry in snapshot has all required fields."""
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    required_fields = {"name", "classification", "source_fn"}
    valid_classifications = {"escalation", "high_signal", "routine"}

    for entry in snapshot["event_types"]:
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"Event type '{entry.get('name')}' missing fields: {missing}"
        )

        classification = entry.get("classification")
        assert classification in valid_classifications, (
            f"Event type '{entry['name']}' has invalid classification '{classification}'. "
            f"Must be one of: {valid_classifications}"
        )
