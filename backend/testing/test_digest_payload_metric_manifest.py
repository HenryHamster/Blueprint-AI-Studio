"""Guard test for the /api/admin/digest payload metric-governance manifest.

``_compute_digest`` (backend/app/routers/admin_digest.py) emits a dict of
top-level keys that drive the admin UI. ``DIGEST_PAYLOAD_METRIC_MANIFEST`` is
the durable, queryable record of what each of those keys means and who
consumes it (or why it was retired/pending).

These tests keep the manifest in sync with what the computation actually emits:

* Every top-level key returned by ``_compute_digest`` MUST appear in the
  manifest with a named consumer OR a retired/pending annotation — so a new
  metric cannot land without declaring who reads it.
* Every manifest key claiming an active ``consumer`` MUST still be returned by
  ``_compute_digest`` — so a stale consumer record can't outlive the field.
* Every retired/pending entry carries a non-empty reason and an ISO date.

The emitted-key set is derived by AST-parsing the source (the top-level keys
of the ``return { ... }`` dict literal inside ``_compute_digest``), so the
test is DB-free, deterministic, and standalone-smoke compatible.
"""

import ast
import datetime as _dt

from app.routers import admin_digest
from app.routers.admin_digest import DIGEST_PAYLOAD_METRIC_MANIFEST

_TARGET_FN = "_compute_digest"


def _emitted_payload_keys() -> set[str]:
    """AST-extract every string literal key from the ``return { ... }`` dict
    inside ``_compute_digest``.

    Collects keys from every ``ast.Return`` node whose value is an ``ast.Dict``
    so that an early-exit return with a non-dict value is silently skipped
    while the main payload return is fully covered.
    """
    source = open(admin_digest.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == _TARGET_FN
        ),
        None,
    )
    assert fn is not None, f"could not find {_TARGET_FN} in admin_digest.py"

    keys: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


def test_every_digest_payload_key_has_recorded_consumer_or_retirement():
    """Each key the computation emits must be declared in the manifest."""
    emitted = _emitted_payload_keys()
    assert emitted, (
        "AST extraction found no top-level string keys in the "
        f"{_TARGET_FN} return dict — extractor is broken"
    )
    undeclared = emitted - set(DIGEST_PAYLOAD_METRIC_MANIFEST)
    assert not undeclared, (
        "These /api/admin/digest payload keys are emitted by "
        f"{_TARGET_FN} but have no entry in DIGEST_PAYLOAD_METRIC_MANIFEST. "
        "Add each with a named consumer, or a pending/retired annotation: "
        f"{sorted(undeclared)}"
    )


def test_no_stale_consumer_manifest_entries():
    """A manifest key claiming an active consumer must still be emitted.

    Retired/pending entries are exempt — they document fields the computation
    no longer writes (retired) or writes without a consumer yet (pending).
    """
    emitted = _emitted_payload_keys()
    stale = [
        field
        for field, meta in DIGEST_PAYLOAD_METRIC_MANIFEST.items()
        if "consumer" in meta and field not in emitted
    ]
    assert not stale, (
        "These manifest keys claim an active consumer but are no longer "
        f"returned by {_TARGET_FN}. Mark them retired (with reason + date) "
        f"or remove the consumer record: {sorted(stale)}"
    )


def test_retired_and_pending_entries_have_reason_and_date():
    """Every non-consumer entry carries a non-empty reason and an ISO date."""
    for field, meta in DIGEST_PAYLOAD_METRIC_MANIFEST.items():
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
        # Raises ValueError if not a valid ISO date — fails the test loudly.
        _dt.date.fromisoformat(date)


def test_manifest_values_are_well_formed():
    """Each manifest value is a dict declaring exactly one disposition."""
    for field, meta in DIGEST_PAYLOAD_METRIC_MANIFEST.items():
        assert isinstance(meta, dict), f"{field}: manifest value must be a dict"
        has_consumer = "consumer" in meta
        has_status = "status" in meta
        assert has_consumer != has_status, (
            f"{field}: manifest entry must declare EXACTLY ONE of "
            f"'consumer' or 'status' (got consumer={has_consumer}, status={has_status})"
        )
