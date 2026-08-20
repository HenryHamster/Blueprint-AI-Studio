"""Inventory guard for backend websocket emitter call sites.

This is intentionally a source-inventory test, not a payload behavior test.
Runtime payload contracts stay covered by the event-specific tests; this file
keeps new websocket emitters from appearing without review and classification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from regen_emitter_inventory import build_fixture
from regen_emitter_inventory import main as regen_main
from websocket_event_inventory import (
    build_from_sources,
    build_websocket_event_inventory,
    inventory_signature,
    read_app_sources,
)
from testing.fixgate_attribution import format_offender_marker

# These are source-inventory tests that AST-scan the entire backend/app tree
# (twice over, in the regen round-trip) — legitimately slow and prone to tripping
# the 30s global pytest-timeout on a loaded CI shard. Give the file headroom; this
# is a timeout accommodation for a correct-but-slow scan, not a weakened assertion.
pytestmark = pytest.mark.timeout(120)


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


def _app_dir() -> Path:
    return Path(__file__).parent.parent / "app"


def _load_snapshot() -> dict[str, Any]:
    assert SNAPSHOT_PATH.exists(), (
        "Missing websocket emitter inventory snapshot. Generate it from "
        "build_websocket_event_inventory(Path('backend/app')) after reviewing "
        "the emitter classifications."
    )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _current_inventory() -> list[dict[str, Any]]:
    return build_websocket_event_inventory(_app_dir())


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
        + "\n"
        + format_offender_marker(
            [*(f"missing:{item}" for item in missing),
             *(f"added:{item}" for item in added)]
        )
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
        assert not missing, f"Emitter entry is missing fields {missing}: {entry}" + "\n" + format_offender_marker(str(offender) for offender in missing)
        assert entry["classification"] in VALID_CLASSIFICATIONS, (
            f"Invalid websocket emitter classification: {entry}"
        )

    assert "workbench_v1_helper" in classifications
    assert "strategic_v1_helper" in classifications
    assert "message_v1_payload" in classifications
    assert "legacy_project_event" in classifications
    assert "legacy_review_required" in classifications


def test_build_from_sources_matches_disk_walk():
    """The content-injectable builder must never diverge from the disk walk.

    This is the safety property that lets an at-ref evaluator trust
    ``build_from_sources`` (fed git-tree blobs) to produce the same inventory the
    disk-walking guard test produces from a checkout. Read the sources
    independently (in arbitrary dict order) to prove the injectable path is
    byte-identical AND order-independent, not merely reusing the same read.
    """
    disk = build_websocket_event_inventory(_app_dir())

    # Independent read; reversed insertion order to exercise the internal sort.
    sources = read_app_sources(_app_dir())
    shuffled = dict(reversed(list(sources.items())))
    injectable = build_from_sources(shuffled)

    assert injectable == disk


def test_regen_round_trip_snapshot_matches_source(tmp_path):
    """Regenerating the fixture yields a snapshot that passes the gate test.

    Drives the actual CLI entry point (``--fix``) to a temp fixture, then reuses
    the snapshot-match comparison against it. Also asserts a second ``--fix`` is a
    byte-for-byte no-op (idempotent).
    """
    fixture_path = tmp_path / "websocket_event_inventory_v1.json"

    assert regen_main(["--fix", "--fixture", str(fixture_path)]) == 0
    assert fixture_path.exists()

    first = fixture_path.read_text(encoding="utf-8")
    assert regen_main(["--fix", "--fixture", str(fixture_path)]) == 0
    assert fixture_path.read_text(encoding="utf-8") == first, (
        "Second --fix changed the fixture; regeneration is not idempotent."
    )

    snapshot = json.loads(first)
    expected = [inventory_signature(r) for r in _snapshot_inventory(snapshot)]
    current = [inventory_signature(r) for r in _current_inventory()]
    assert [r for r in expected if r not in current] == []
    assert [r for r in current if r not in expected] == []

    # The plain (no-flag) check mode reports the freshly written fixture as clean.
    assert regen_main(["--fixture", str(fixture_path)]) == 0


def test_regen_preserves_lineno_for_unchanged_signature():
    """An unrelated edit that only shifts line numbers must not churn the fixture.

    ``build_fixture`` carries prior line numbers forward for any emitter whose
    stable signature is unchanged, so regeneration touches only genuinely
    new/changed call sites.
    """
    app_dir = str(_app_dir())
    baseline = build_fixture(app_dir, existing=None)
    assert baseline["emitters"], "Expected at least one emitter."

    # Pretend the prior fixture recorded a different line number for one emitter.
    stale = json.loads(json.dumps(baseline))
    stale["emitters"][0]["lineno"] = 987654

    rebuilt = build_fixture(app_dir, existing=stale)
    assert rebuilt["emitters"][0]["lineno"] == 987654, (
        "Line number was rewritten despite an unchanged signature (churn)."
    )
