"""Guard test for the direction-outcome metric-governance manifest.

``_update_direction_research_outcomes`` (backend/app/direction_outcomes.py)
writes a set of metric fields into each direction's ``research_history`` entry.
``RESEARCH_HISTORY_METRIC_MANIFEST`` is the durable, queryable record of what
each of those fields means and who consumes it (or why it was retired/pending).

These tests keep the manifest in sync with what the computation actually emits:

* Every emitted ``entry["<field>"]`` MUST appear in the manifest with a named
  consumer OR a retired/pending annotation — so a new metric cannot land
  without declaring who reads it (the recurrence this manifest exists to stop).
* Every manifest key claiming an active ``consumer`` MUST still be emitted — so
  a stale consumer record can't outlive the field it documents.
* Every retired/pending entry carries a non-empty reason and an ISO date — so
  a dropped/consumer-less metric leaves an auditable trail, not a vanished
  comment.

The emitted-field set is derived by AST-parsing the source (every Store-context
``entry["..."]`` subscript inside the function), so the test is DB-free,
deterministic, and standalone-smoke compatible.
"""

import ast
import datetime as _dt

from app import direction_outcomes
from app.direction_outcomes import RESEARCH_HISTORY_METRIC_MANIFEST

_TARGET_FN = "_update_direction_research_outcomes"


def _emitted_metric_fields() -> set[str]:
    """AST-extract every ``entry["<literal>"]`` write target inside the target fn.

    Only Store-context subscripts on the ``entry`` name with a string-constant
    key are collected, so reads like ``entry["acceptance_declared_merged"]``
    appearing inside a value expression are excluded.
    """
    source = open(direction_outcomes.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == _TARGET_FN
        ),
        None,
    )
    assert fn is not None, f"could not find {_TARGET_FN} in direction_outcomes.py"

    fields: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Subscript):
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id == "entry"):
            continue
        if not isinstance(node.ctx, ast.Store):
            continue
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            fields.add(key.value)
    return fields


def test_every_emitted_metric_has_a_recorded_consumer_or_retirement():
    """Each field the computation emits must be declared in the manifest."""
    emitted = _emitted_metric_fields()
    assert emitted, "AST extraction found no emitted entry[...] fields — extractor is broken"
    undeclared = emitted - set(RESEARCH_HISTORY_METRIC_MANIFEST)
    assert not undeclared, (
        "These research_history metric fields are emitted by "
        f"{_TARGET_FN} but have no entry in RESEARCH_HISTORY_METRIC_MANIFEST. "
        "Add each with a named consumer, or a pending/retired annotation: "
        f"{sorted(undeclared)}"
    )


def test_no_stale_consumer_manifest_entries():
    """A manifest key claiming an active consumer must still be emitted.

    Retired/pending entries are exempt — they intentionally document fields the
    computation no longer writes (retired) or writes without a consumer yet
    (pending).
    """
    emitted = _emitted_metric_fields()
    stale = [
        field
        for field, meta in RESEARCH_HISTORY_METRIC_MANIFEST.items()
        if "consumer" in meta and field not in emitted
    ]
    assert not stale, (
        "These manifest keys claim an active consumer but are no longer emitted "
        f"by {_TARGET_FN}. Mark them retired (with reason + date) or remove the "
        f"consumer record: {sorted(stale)}"
    )


def test_retired_and_pending_entries_have_reason_and_date():
    """Every non-consumer entry carries a non-empty reason and an ISO date."""
    for field, meta in RESEARCH_HISTORY_METRIC_MANIFEST.items():
        if "consumer" in meta:
            # Active consumer entries must NOT also carry a retirement status,
            # and must name a non-empty consumer.
            assert "status" not in meta, (
                f"{field}: an entry with a 'consumer' must not also carry 'status'"
            )
            assert meta["consumer"].strip(), f"{field}: consumer string is empty"
            continue
        status = meta.get("status")
        assert status in ("retired", "pending"), (
            f"{field}: non-consumer manifest entry must declare "
            f"status 'retired' or 'pending', got {status!r}"
        )
        assert meta.get("reason", "").strip(), f"{field}: {status} entry needs a reason"
        date = meta.get("date", "")
        # Raises ValueError if not a valid ISO date — fails the test loudly.
        _dt.date.fromisoformat(date)


def test_manifest_values_are_well_formed():
    """Each manifest value is a dict declaring exactly one disposition."""
    for field, meta in RESEARCH_HISTORY_METRIC_MANIFEST.items():
        assert isinstance(meta, dict), f"{field}: manifest value must be a dict"
        has_consumer = "consumer" in meta
        has_status = "status" in meta
        assert has_consumer != has_status, (
            f"{field}: manifest entry must declare EXACTLY ONE of "
            f"'consumer' or 'status' (got consumer={has_consumer}, status={has_status})"
        )


def test_no_pending_unread_conversion_metric():
    """conversion_rate must be retired, not pending.

    A pending status signals an unresolved question about whether the stored
    field should be the source of truth or be removed.  The field has been
    retired — all conversion surfaces derive directly from the canonical count
    fields (verified_prs_merged / tasks_created) and no production code reads
    the stored entry copy.  This test locks in that decision so the manifest
    cannot regress to pending.
    """
    meta = RESEARCH_HISTORY_METRIC_MANIFEST.get("conversion_rate")
    assert meta is not None, (
        "conversion_rate must remain in the manifest as a retired entry; "
        "remove this test only if the field name changes"
    )
    assert meta.get("status") == "retired", (
        f"conversion_rate manifest entry must be 'retired', got status={meta.get('status')!r}. "
        "All conversion surfaces recompute from count fields — the stored copy is dead weight."
    )
    assert "consumer" not in meta, (
        "conversion_rate must not claim an active consumer; "
        "no production code reads entry['conversion_rate'] from research_history"
    )
