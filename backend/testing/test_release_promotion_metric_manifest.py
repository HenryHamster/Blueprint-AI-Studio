"""Guard test for the promotion-gate readiness metric-governance manifest.

``compute_promotion_readiness`` (backend/app/release_readiness.py) returns a
block of metric fields describing the durable promotion-gate readiness verdict
(would_promote_rate, validated_precision, regime annotation).
``PROMOTION_READINESS_METRIC_MANIFEST`` is the queryable, test-enforced record
of what each emitted field means, the named ground-truth source it is computed
from, and who consumes it (or a dated pending/retired reason).

* Every top-level key emitted by ``compute_promotion_readiness`` MUST appear in
  the manifest with a named ``consumer`` OR a retired/pending annotation — so a
  new field cannot land without declaring who reads it.
* Every manifest key claiming an active ``consumer`` MUST still be emitted — so
  a stale consumer record cannot outlive the field it documents.
* Every active entry names a non-empty ``ground_truth`` source.
* Every retired/pending entry carries a non-empty reason and an ISO date.

Because ``compute_promotion_readiness`` builds its output via ``out = {...}``
rather than a single ``return {...}`` literal on every path, the emitted-field
set is extracted from the stable no-history shape-defining helper:
* ``_empty_readiness`` — the stable no-history return dict (covers all core
  keys including regime annotation added by ``_annotate_regime``).

The test is DB-free, deterministic, and standalone-smoke compatible.
"""

import ast
import datetime as _dt

from app import release_readiness
from app.release_readiness import PROMOTION_READINESS_METRIC_MANIFEST
from testing.fixgate_attribution import format_offender_marker

_CONDITIONAL_FIELDS = frozenset()


def _extract_return_dict_keys(source: str, fn_name: str) -> set[str]:
    """AST-extract top-level string keys of the ``return {...}`` dict literal.

    Walks ``fn_name`` for a ``Return`` whose value is a dict literal and
    collects every constant-string key.
    """
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == fn_name
        ),
        None,
    )
    assert fn is not None, f"could not find {fn_name} in release_readiness.py"

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


def _emitted_metric_fields() -> set[str]:
    """Return the fields from the stable shape-defining helper."""
    source = open(release_readiness.__file__, encoding="utf-8").read()
    core = _extract_return_dict_keys(source, "_empty_readiness")
    assert core, "AST extraction found no keys in _empty_readiness — extractor is broken"
    return core


def test_every_emitted_field_has_a_recorded_consumer_or_retirement():
    """Each field compute_promotion_readiness emits must be declared in the manifest."""
    emitted = _emitted_metric_fields()
    undeclared = emitted - set(PROMOTION_READINESS_METRIC_MANIFEST)
    assert not undeclared, (
        "These compute_promotion_readiness fields are emitted but have no entry in "
        "PROMOTION_READINESS_METRIC_MANIFEST. Add each with a named ground_truth + "
        f"consumer, or a pending/retired annotation: {sorted(undeclared)}" + "\n" + format_offender_marker(str(offender) for offender in undeclared)
    )


def test_no_stale_consumer_manifest_entries():
    """A manifest key claiming an active consumer must still be emitted."""
    emitted = _emitted_metric_fields() | _CONDITIONAL_FIELDS
    stale = [
        field
        for field, meta in PROMOTION_READINESS_METRIC_MANIFEST.items()
        if "consumer" in meta and field not in emitted
    ]
    assert not stale, (
        "These manifest keys claim an active consumer but are no longer emitted "
        "by compute_promotion_readiness. Mark them retired (reason + date) or "
        f"remove the consumer record: {sorted(stale)}" + "\n" + format_offender_marker(str(offender) for offender in stale)
    )


def test_active_entries_name_a_ground_truth_source():
    """Every active (consumer-bearing) field names a non-empty ground-truth source."""
    for field, meta in PROMOTION_READINESS_METRIC_MANIFEST.items():
        if "consumer" not in meta:
            continue
        assert meta.get("ground_truth", "").strip(), (
            f"{field}: active manifest entry must name a non-empty ground_truth source"
        )


def test_retired_and_pending_entries_have_reason_and_date():
    """Every non-consumer entry carries a non-empty reason and an ISO date."""
    for field, meta in PROMOTION_READINESS_METRIC_MANIFEST.items():
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
    for field, meta in PROMOTION_READINESS_METRIC_MANIFEST.items():
        assert isinstance(meta, dict), f"{field}: manifest value must be a dict"
        has_consumer = "consumer" in meta
        has_status = "status" in meta
        assert has_consumer != has_status, (
            f"{field}: manifest entry must declare EXACTLY ONE of "
            f"'consumer' or 'status' (got consumer={has_consumer}, status={has_status})"
        )
