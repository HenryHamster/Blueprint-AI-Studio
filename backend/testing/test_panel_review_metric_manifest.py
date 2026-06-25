"""Guard test for the panel-review gate effectiveness metric-governance manifest.

``compute_panel_review_effectiveness`` (backend/app/efficiency_snapshot.py) returns
a block of metric fields describing the durable panel-review gate effectiveness
measures (overturn rate, fail-open rate, coverage, cost).
``PANEL_REVIEW_METRIC_MANIFEST`` is the queryable, test-enforced record of what
each emitted field means, the named ground-truth source it is computed from, and
who consumes it (or a dated pending/retired reason).

Mirrors ``test_timeout_rate_metric_manifest.py``:

* Every top-level key the ``return {...}`` literal emits MUST appear in the
  manifest with a named ``consumer`` OR a retired/pending annotation — so a new
  field cannot land without declaring who reads it (the recurrence the manifest
  exists to stop).
* Every manifest key claiming an active ``consumer`` MUST still be emitted — so a
  stale consumer record cannot outlive the field it documents.
* Every active entry names a non-empty ``ground_truth`` source — the measure's
  whole point is that it reads PERSISTED ground truth, so the manifest documents
  that source per field.
* Every retired/pending entry carries a non-empty reason and an ISO date.

The emitted-field set is AST-extracted from the source (the string keys of the
``return {...}`` dict literal inside ``compute_panel_review_effectiveness``), so
the test is DB-free, deterministic, and standalone-smoke compatible.
"""

import ast
import datetime as _dt

from app import efficiency_snapshot
from app.digest_metrics import PANEL_REVIEW_METRIC_MANIFEST

_TARGET_FN = "compute_panel_review_effectiveness"


def _emitted_metric_fields() -> set[str]:
    """AST-extract the top-level string keys of the ``return {...}`` dict literal.

    Walks ``compute_panel_review_effectiveness`` for a ``Return`` whose value is a
    dict literal and collects every constant-string key. Robust to the function
    body changing around the return as long as the emitted shape is a dict literal.
    """
    source = open(efficiency_snapshot.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == _TARGET_FN
        ),
        None,
    )
    assert fn is not None, f"could not find {_TARGET_FN} in efficiency_snapshot.py"

    fields: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                fields.add(key.value)
    return fields


def test_every_emitted_field_has_a_recorded_consumer_or_retirement():
    """Each field the measure emits must be declared in the manifest."""
    emitted = _emitted_metric_fields()
    assert emitted, "AST extraction found no emitted return-dict keys — extractor is broken"
    undeclared = emitted - set(PANEL_REVIEW_METRIC_MANIFEST)
    assert not undeclared, (
        "These panel-review effectiveness measure fields are emitted by "
        f"{_TARGET_FN} but have no entry in PANEL_REVIEW_METRIC_MANIFEST. "
        "Add each with a named ground_truth + consumer, or a pending/retired "
        f"annotation: {sorted(undeclared)}"
    )


def test_no_stale_consumer_manifest_entries():
    """A manifest key claiming an active consumer must still be emitted."""
    emitted = _emitted_metric_fields()
    stale = [
        field
        for field, meta in PANEL_REVIEW_METRIC_MANIFEST.items()
        if "consumer" in meta and field not in emitted
    ]
    assert not stale, (
        "These manifest keys claim an active consumer but are no longer emitted "
        f"by {_TARGET_FN}. Mark them retired (reason + date) or remove the "
        f"consumer record: {sorted(stale)}"
    )


def test_active_entries_name_a_ground_truth_source():
    """Every active (consumer-bearing) field names a non-empty ground-truth source.

    The measure's contract is that it reads PERSISTED per-task ground truth, so
    each live field must document WHERE its value comes from.
    """
    for field, meta in PANEL_REVIEW_METRIC_MANIFEST.items():
        if "consumer" not in meta:
            continue
        assert meta.get("ground_truth", "").strip(), (
            f"{field}: active manifest entry must name a non-empty ground_truth source"
        )


def test_retired_and_pending_entries_have_reason_and_date():
    """Every non-consumer entry carries a non-empty reason and an ISO date."""
    for field, meta in PANEL_REVIEW_METRIC_MANIFEST.items():
        if "consumer" in meta:
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
        _dt.date.fromisoformat(date)  # raises (fails loudly) if not ISO


def test_manifest_values_are_well_formed():
    """Each manifest value is a dict declaring exactly one disposition."""
    for field, meta in PANEL_REVIEW_METRIC_MANIFEST.items():
        assert isinstance(meta, dict), f"{field}: manifest value must be a dict"
        has_consumer = "consumer" in meta
        has_status = "status" in meta
        assert has_consumer != has_status, (
            f"{field}: manifest entry must declare EXACTLY ONE of "
            f"'consumer' or 'status' (got consumer={has_consumer}, status={has_status})"
        )
