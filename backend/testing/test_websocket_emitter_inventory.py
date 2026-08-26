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


def _tree_drift(before: dict[str, str], app_dir: Path) -> list[str]:
    """Re-read ``app_dir`` and report the paths that changed since ``before``.

    Used only to attribute a mismatch. The disk-walk leg of the equivalence test
    below reads the tree a second time, so a writer touching the checkout mid-run
    (a concurrent ``git checkout``, an editor save, a parallel worktree sync) can
    make the two legs disagree for reasons that have nothing to do with the
    builder. Naming those paths turns a ~5000-line "the builder diverged" diff
    into a one-line, correct diagnosis.
    """
    after = read_app_sources(app_dir)
    changed = set(before) ^ set(after)
    changed |= {key for key in set(before) & set(after) if before[key] != after[key]}
    return sorted(changed)


def test_build_from_sources_matches_disk_walk():
    """The content-injectable builder must never diverge from the disk walk.

    This is the safety property that lets an at-ref evaluator trust
    ``build_from_sources`` (fed git-tree blobs) to produce the same inventory the
    disk-walking guard test produces from a checkout.

    Three distinct claims, asserted separately so a failure says which one broke:

    1. ``build_from_sources`` is order-independent -- decided against a SINGLE
       read, so the claim is settled by the builder alone and cannot be muddied
       by the tree changing between two reads.
    2. ``read_app_sources`` enumerates in the same order ``build_from_sources``
       sorts by, so read order and build order agree by construction.
    3. The disk walk agrees with the injectable path. This leg necessarily reads
       the tree a second time; if it disagrees, drift is ruled out explicitly
       before the builder is blamed.
    """
    sources = read_app_sources(_app_dir())
    baseline = build_from_sources(sources)
    assert baseline, "Expected at least one websocket emitter under backend/app."

    # (1) Reversed insertion order must not move a single record. The builder
    # sorts by the mapping key, which is unique per file, so that sort is total.
    shuffled = dict(reversed(list(sources.items())))
    assert build_from_sources(shuffled) == baseline, (
        "build_from_sources is order-dependent: reversing the mapping's insertion "
        "order changed the emitted records. Its sort key must be TOTAL, so that no "
        "tie is ever broken by insertion order -- otherwise an at-ref evaluator "
        "fed git-tree blobs computes a different inventory than this guard."
    )

    # (2) Read order == build order.
    assert list(sources) == sorted(sources), (
        "read_app_sources enumerated in a different order than build_from_sources "
        "sorts by. Sort the walk by the emitted source_path key, not by Path."
    )

    # (3) Disk walk == injectable path.
    disk = build_websocket_event_inventory(_app_dir())
    if disk != baseline:
        drift = _tree_drift(sources, _app_dir())
        assert not drift, (
            "backend/app changed on disk while this test ran, so the two reads saw "
            "different trees. This is NOT a builder divergence -- re-run against a "
            f"quiescent checkout. Paths that changed: {drift[:20]}\n"
            + format_offender_marker(drift)
        )
        assert disk == baseline, (
            "build_websocket_event_inventory diverged from build_from_sources over "
            "the same, verified-unchanged sources."
        )


_SYNTHETIC_EMITTER_SOURCE = """from app.broadcast import broadcast_to_project


async def handler(project_id):
    await broadcast_to_project(project_id, "task_updated", {"task_id": 1})
    await broadcast_to_project(project_id, "task_updated", {"task_id": 1})
"""


def test_source_path_sort_is_total_across_tree_shapes(tmp_path):
    """Pin the total ordering: enumeration and build both key off ``source_path``.

    ``build_from_sources`` sorts its string keys, which is a TOTAL order. The
    tempting way to walk the disk -- ``sorted(app_dir.rglob("*.py"))`` -- is a
    DIFFERENT order: on Python 3.11 (the CI interpreter) ``PurePath`` compares
    component lists, so ``app/routers/agent.py`` sorts before ``app/routers.py``
    while the string keys sort the other way round (``.`` < ``/``). Today's
    ``backend/app`` happens to contain no ``X/`` + ``X.py`` pair, which is the
    only reason the two orders have ever agreed; an unremarkable new module would
    split them.

    So this builds that adversarial shape explicitly and pins the emitted order.
    Narrowing the key -- sorting by ``Path``, by stem, by directory-then-file --
    fails here loudly instead of silently reordering the real inventory later.
    """
    app_dir = tmp_path / "backend" / "app"
    for relative in (
        "routers.py",
        "routers/agent.py",
        "sub.py",
        "sub/x.py",
        "__pycache__/ignored.py",
    ):
        target = app_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_SYNTHETIC_EMITTER_SOURCE, encoding="utf-8")

    expected_keys = [
        "backend/app/routers.py",
        "backend/app/routers/agent.py",
        "backend/app/sub.py",
        "backend/app/sub/x.py",
    ]

    sources = read_app_sources(app_dir)
    assert list(sources) == expected_keys, (
        "read_app_sources enumerated in the wrong order. It must order by the "
        "emitted source_path key (the same key build_from_sources sorts by), not "
        f"by Path. Got: {list(sources)}"
    )
    assert sorted(sources) == expected_keys
    assert "backend/app/__pycache__/ignored.py" not in sources

    records = build_from_sources(sources)
    assert [record["source_path"] for record in records] == [
        key for key in expected_keys for _ in range(2)
    ], "Emitted record order does not follow the total source_path order."
    assert [record["occurrence"] for record in records] == [1, 2] * len(expected_keys), (
        "Repeated identical call sites must be numbered per file, in file order."
    )

    # The two public entry points agree on this shape, in both insertion orders.
    assert build_websocket_event_inventory(app_dir) == records
    assert build_from_sources(dict(reversed(list(sources.items())))) == records


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


def test_disk_walk_mismatch_is_attributed_to_tree_drift(tmp_path):
    """A tree that changes between reads must be named as drift, not as divergence.

    The disk-walk equivalence check above reads ``backend/app`` twice. A writer
    touching the checkout in between (a concurrent ``git checkout``, an editor
    save, a parallel worktree sync) makes the two reads disagree for reasons that
    have nothing to do with the builder -- and because ``inventory_signature``
    drops ``lineno``, a pure line-shift slips past the snapshot gate while
    breaking full-record equality. That is exactly the confusing shape this
    attribution exists to name, so pin it.
    """
    app_dir = tmp_path / "backend" / "app"
    app_dir.mkdir(parents=True)
    target = app_dir / "dispatch.py"
    target.write_text(_SYNTHETIC_EMITTER_SOURCE, encoding="utf-8")

    before = read_app_sources(app_dir)
    baseline = build_from_sources(before)
    assert _tree_drift(before, app_dir) == [], "A quiescent tree must report no drift."

    # A pure line shift: same emitters, same signatures, different line numbers.
    target.write_text("# inserted\n" + _SYNTHETIC_EMITTER_SOURCE, encoding="utf-8")

    shifted = build_websocket_event_inventory(app_dir)
    assert shifted != baseline, "Expected the line shift to move the records."
    assert [inventory_signature(record) for record in shifted] == [
        inventory_signature(record) for record in baseline
    ], "A line shift must leave the signatures -- and so the snapshot gate -- intact."

    assert _tree_drift(before, app_dir) == ["backend/app/dispatch.py"], (
        "Tree drift must be attributed to the changed path, so a mid-run edit is "
        "not misreported as a builder divergence."
    )
