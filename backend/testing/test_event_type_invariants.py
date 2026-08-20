"""Invariant test ensuring backend event_type emissions stay in sync with frontend classifications.

This test extracts all event_type values emitted by notification_dispatch.py and
validates them against a JSON snapshot. The snapshot serves as a single source of
truth for which event types exist, their classification (escalation/high_signal/routine),
and which backend function emits them.
"""

import json
import re
from pathlib import Path

from testing.fixgate_attribution import format_offender_marker


def _snapshot_missing_field_offenders(entry: dict, missing: set[str]) -> list[str]:
    """Qualify a missing field with the snapshot entry that owns it."""

    name = entry.get("name", "<unnamed>")
    return [f"{name}:{field}" for field in sorted(missing)]


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
    app_dir = Path(__file__).parent.parent / "app"

    # Source files that emit event_type literals included in the snapshot
    source_paths = [
        app_dir / "notification_dispatch.py",
        app_dir / "capability_trial_verdict.py",
    ]

    assert snapshot_path.exists(), "event_type_snapshot.json not found"
    for sp in source_paths:
        assert sp.exists(), f"{sp.name} not found"

    # Load the snapshot
    with open(snapshot_path) as f:
        snapshot = json.load(f)
    snapshot_events = {et["name"] for et in snapshot["event_types"]}

    # Extract event_type="..." from all source files using regex.
    # Match keyword arguments, dict entries, and typed default parameters:
    #   event_type="value"
    #   "event_type": "value"
    #   event_type: str = "value"
    pattern = (
        r'(?:event_type|"event_type")\s*'
        r'(?:[:=]|:\s*[A-Za-z_][\w.\[\] |]*\s*=)\s*'
        r'["\']([^"\']+)["\']'
    )
    source_events: set[str] = set()
    for source_path in source_paths:
        with open(source_path) as f:
            source_code = f.read()
        source_events.update(re.findall(pattern, source_code))

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
        offenders = [
            *(f"missing-from-snapshot:{name}" for name in missing_in_snapshot),
            *(f"extra-in-snapshot:{name}" for name in extra_in_snapshot),
        ]
        raise AssertionError(
            "\n\n".join(error_parts)
            + "\n"
            + format_offender_marker(offenders)
        )


def test_snapshot_has_required_fields():
    """Verify each event_type entry in snapshot has all required fields."""
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    required_fields = {"name", "classification", "source_fn", "audience"}
    valid_classifications = {"escalation", "high_signal", "routine"}
    valid_audiences = {"operator-action-needed", "operator-fyi", "strategist-internal", "pipeline-log"}

    for entry in snapshot["event_types"]:
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"Event type '{entry.get('name')}' missing fields: {missing}\n"
            + format_offender_marker(_snapshot_missing_field_offenders(entry, missing))
        )

        classification = entry.get("classification")
        assert classification in valid_classifications, (
            f"Event type '{entry['name']}' has invalid classification '{classification}'. "
            f"Must be one of: {valid_classifications}"
            + "\n"
            + format_offender_marker(
                [f"{entry['name']}:invalid-classification:{classification}"]
            )
        )
        audience = entry.get("audience")
        assert audience in valid_audiences, (
            f"Event type '{entry['name']}' has invalid audience '{audience}'. "
            f"Must be one of: {valid_audiences}"
            + "\n"
            + format_offender_marker(
                [f"{entry['name']}:invalid-audience:{audience}"]
            )
        )


def test_snapshot_missing_field_identity_includes_entry_name():
    """Two entries missing the same field remain distinct attribution offenders."""

    missing = {"audience"}
    assert _snapshot_missing_field_offenders({"name": "old_event"}, missing) == [
        "old_event:audience"
    ]
    assert _snapshot_missing_field_offenders({"name": "new_event"}, missing) == [
        "new_event:audience"
    ]


def test_all_snapshot_entries_have_audience():
    """Every event type in the snapshot must carry an ``audience`` field.

    This is the fail-closed guard for the ledger side: any event type without
    ``audience`` would default to ``strategist-internal`` (suppressed for the
    operator).  A new ``post_*_message`` function whose event type is missing
    from the snapshot (or lacks ``audience``) fails CI — the same mechanism
    that keeps ``HIGH_SIGNAL_NOTE_EVENT_TYPES`` honest today.
    """
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"
    with open(snapshot_path) as f:
        snapshot = json.load(f)

    valid_audiences = {"operator-action-needed", "operator-fyi", "strategist-internal", "pipeline-log"}
    for entry in snapshot["event_types"]:
        audience = entry.get("audience")
        assert audience in valid_audiences, (
            f"Event type '{entry['name']}' has missing or invalid audience='{audience}'. "
            f"Every event type must declare an audience in {valid_audiences}."
            + "\n"
            + format_offender_marker(
                [f"{entry['name']}:invalid-audience:{audience}"]
            )
        )


def test_snapshot_audience_matches_runtime_registry():
    """Every snapshot entry's ``audience`` must match the live ``route_event`` registry.

    The snapshot is the ledger-side declaration; ``message_audience._EVENT_AUDIENCE_REGISTRY``
    is the runtime resolver. If they drift — e.g. someone edits the registry without updating
    the snapshot or vice-versa — an event type would resolve to a different audience at
    runtime than the one declared in the snapshot, silently leaking internal notices to
    the operator or suppressing operator-visible items.
    """
    import importlib

    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"
    with open(snapshot_path) as f:
        snapshot = json.load(f)

    ma = importlib.import_module("app.message_audience")

    mismatches: list[str] = []
    for entry in snapshot["event_types"]:
        name = entry["name"]
        expected_audience = entry["audience"]
        actual_audience = ma.route_event(name)
        if actual_audience != expected_audience:
            mismatches.append(
                f"  {name!r}: snapshot={expected_audience!r}, registry={actual_audience!r}"
            )

    assert not mismatches, (
        "Snapshot audience does not match runtime registry for:\n"
        + "\n".join(mismatches)
        + "\n\n→ Update either event_type_snapshot.json or message_audience._EVENT_AUDIENCE_REGISTRY"
        " so they agree."
        + "\n"
        + format_offender_marker(mismatches)
    )


def test_high_signal_note_set_matches_snapshot():
    """Cross-layer invariant: HIGH_SIGNAL_NOTE_EVENT_TYPES must equal the snapshot's
    high_signal NOTE entries — in both notification_dispatch and admin_notifications.

    Drift between these three sources is what caused the original bug (high-signal NOTEs
    silently evicted by routine flood because the guaranteed-query set was stale).
    """
    snapshot_path = Path(__file__).parent / "event_type_snapshot.json"

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    # Derive expected set from snapshot: classification=high_signal, message_type=NOTE
    expected: set[str] = {
        et["name"]
        for et in snapshot["event_types"]
        if et.get("classification") == "high_signal"
        and et.get("message_type") == "NOTE"
    }

    # Verify escalation types use BLOCKER (not NOTE) so they are already guaranteed
    # via message_type and do not need to appear in HIGH_SIGNAL_NOTE_EVENT_TYPES.
    escalation_notes = {
        et["name"]
        for et in snapshot["event_types"]
        if et.get("classification") == "escalation"
        and et.get("message_type") != "BLOCKER"
    }
    assert not escalation_notes, (
        f"Escalation event types must emit as BLOCKER, not NOTE: {sorted(escalation_notes)}\n"
        "  → Update snapshot message_type or the source function's message_type argument."
        + "\n"
        + format_offender_marker(escalation_notes)
    )

    # Import the backend constant from notification_dispatch (canonical source of truth)
    import importlib

    nd = importlib.import_module("app.notification_dispatch")
    nd_set: frozenset[str] = nd.HIGH_SIGNAL_NOTE_EVENT_TYPES

    # Import from admin_notifications (must mirror notification_dispatch)
    an = importlib.import_module("app.routers.admin_notifications")
    an_set: frozenset[str] = an.HIGH_SIGNAL_NOTE_EVENT_TYPES

    drift_parts = []
    if set(nd_set) != expected:
        missing = expected - set(nd_set)
        extra = set(nd_set) - expected
        drift_parts.append(
            "notification_dispatch.HIGH_SIGNAL_NOTE_EVENT_TYPES does not match snapshot:\n"
            + (
                f"  missing (in snapshot but not in set): {sorted(missing)}\n"
                if missing
                else ""
            )
            + (
                f"  extra (in set but not in snapshot): {sorted(extra)}\n"
                if extra
                else ""
            )
            + "  → Update HIGH_SIGNAL_NOTE_EVENT_TYPES in backend/app/notification_dispatch.py"
        )
    if set(an_set) != expected:
        missing = expected - set(an_set)
        extra = set(an_set) - expected
        drift_parts.append(
            "admin_notifications.HIGH_SIGNAL_NOTE_EVENT_TYPES does not match snapshot:\n"
            + (
                f"  missing (in snapshot but not in set): {sorted(missing)}\n"
                if missing
                else ""
            )
            + (
                f"  extra (in set but not in snapshot): {sorted(extra)}\n"
                if extra
                else ""
            )
            + "  → Update HIGH_SIGNAL_NOTE_EVENT_TYPES in backend/app/routers/admin_notifications.py"
        )

    if drift_parts:
        offenders = [
            *(
                f"notification_dispatch:missing:{name}"
                for name in expected - set(nd_set)
            ),
            *(
                f"notification_dispatch:extra:{name}"
                for name in set(nd_set) - expected
            ),
            *(
                f"admin_notifications:missing:{name}"
                for name in expected - set(an_set)
            ),
            *(
                f"admin_notifications:extra:{name}"
                for name in set(an_set) - expected
            ),
        ]
        raise AssertionError(
            "\n\n".join(drift_parts)
            + "\n"
            + format_offender_marker(offenders)
        )
