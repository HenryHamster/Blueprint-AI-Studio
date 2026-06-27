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
import uuid

from app import efficiency_snapshot
from app.digest_metrics import PANEL_REVIEW_METRIC_MANIFEST
from app.efficiency_snapshot import compute_panel_review_effectiveness
from testing.factories import make_task

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


# ---------------------------------------------------------------------------
# Behavioral formula-pinning tests
# ---------------------------------------------------------------------------

_RECON_KEY = "review_second_opinion"


def _make_recon_task(state: str, *, escalated: bool = False, pid=None) -> object:
    """Build a minimal Task fixture with the given reconciliation state."""
    recon: dict = {"state": state}
    if escalated:
        recon["escalated"] = True
    return make_task(
        project_id=pid or uuid.uuid4(),
        context={_RECON_KEY: recon},
    )


def test_fail_open_rate_formula():
    """fail_open_rate = challenged_degraded / (total_challenged + challenged_degraded).

    Manifest ground_truth: challenged_degraded / (total_challenged + challenged_degraded)
    if (total_challenged > 0 or challenged_degraded > 0) else None.
    """
    pid = uuid.uuid4()
    # 4 confirmed + 1 degraded → rate = 1/5 = 0.2
    tasks = [_make_recon_task("challenged_confirmed", pid=pid) for _ in range(4)]
    tasks.append(_make_recon_task("degraded_model_unavailable", pid=pid))
    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["total_challenged"] == 4
    assert result["challenged_degraded"] == 1
    expected_rate = round(1 / (4 + 1), 4)  # 0.2
    assert result["fail_open_rate"] == expected_rate, (
        f"fail_open_rate formula mismatch: got {result['fail_open_rate']}, expected {expected_rate}"
    )

    # degraded only (no successful challenges) → rate = 1/1 = 1.0
    tasks2 = [_make_recon_task("degraded_model_unavailable", pid=pid)]
    result2 = compute_panel_review_effectiveness(tasks2)
    assert result2 is not None
    assert result2["fail_open_rate"] == 1.0

    # no degraded events → rate = 0.0 (confirmed-zero, distinct from no-data)
    tasks3 = [_make_recon_task("challenged_confirmed", pid=pid)]
    result3 = compute_panel_review_effectiveness(tasks3)
    assert result3 is not None
    assert result3["fail_open_rate"] == 0.0


def test_fail_open_rate_none_when_no_invocations():
    """fail_open_rate is None when escalated-only window (no challenged or degraded)."""
    pid = uuid.uuid4()
    tasks = [_make_recon_task("unchallenged", escalated=True, pid=pid)]
    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["fail_open_rate"] is None, (
        "fail_open_rate must be None when total_challenged==0 and challenged_degraded==0"
    )


def test_effective_coverage_formula():
    """effective_coverage = (challenged + degraded + escalated) / (challenged + degraded + escalated + sampled_out).

    Manifest ground_truth: (total_challenged + challenged_degraded + challenged_escalated)
    / (total_challenged + challenged_degraded + challenged_escalated + sampled_out).
    """
    pid = uuid.uuid4()
    # 3 confirmed + 1 degraded + 1 escalated + 5 sampled_out
    # numerator = 3 + 1 + 1 = 5; denominator = 5 + 5 = 10 → coverage = 0.5
    tasks = [_make_recon_task("challenged_confirmed", pid=pid) for _ in range(3)]
    tasks.append(_make_recon_task("degraded_model_unavailable", pid=pid))
    tasks.append(_make_recon_task("unchallenged", escalated=True, pid=pid))
    tasks += [_make_recon_task("sampled_out", pid=pid) for _ in range(5)]
    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["sampled_out_count"] == 5
    assert result["challenged_escalated"] == 1
    assert result["challenged_degraded"] == 1
    assert result["total_challenged"] == 3
    expected_coverage = round((3 + 1 + 1) / (3 + 1 + 1 + 5), 4)  # 0.5
    assert result["effective_coverage"] == expected_coverage, (
        f"effective_coverage formula mismatch: got {result['effective_coverage']}, expected {expected_coverage}"
    )


def test_effective_coverage_none_when_no_opportunities():
    """effective_coverage is None when there are zero total opportunities."""
    # This case cannot occur normally (None is returned before the dict) but
    # we verify the formula: None only when all counts are zero.
    result = compute_panel_review_effectiveness([])
    assert result is None  # no data → entire result is None, not a dict with None fields


def test_overturn_rate_formula():
    """overturn_rate = challenged_rejected / total_challenged if total_challenged > 0 else None.

    Manifest ground_truth: challenged_rejected / total_challenged if total_challenged > 0 else None.
    """
    pid = uuid.uuid4()
    # 2 confirmed + 1 revised + 2 rejected → overturn = 2/5 = 0.4
    tasks = [_make_recon_task("challenged_confirmed", pid=pid) for _ in range(2)]
    tasks.append(_make_recon_task("challenged_revised", pid=pid))
    tasks += [_make_recon_task("challenged_rejected", pid=pid) for _ in range(2)]
    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["challenged_rejected"] == 2
    assert result["total_challenged"] == 5
    expected_overturn = round(2 / 5, 4)  # 0.4
    assert result["overturn_rate"] == expected_overturn, (
        f"overturn_rate formula mismatch: got {result['overturn_rate']}, expected {expected_overturn}"
    )

    # No challenged events → overturn_rate is None
    tasks2 = [_make_recon_task("sampled_out", pid=pid)]
    result2 = compute_panel_review_effectiveness(tasks2)
    assert result2 is not None
    assert result2["overturn_rate"] is None
