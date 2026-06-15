"""Unit tests for the reusable context size-bounding primitive.

These are pure-Python (no DB) and cover the behavior the review-loop write paths
rely on: feedback truncation, entry-count capping, no input mutation, and the
truncate-only mode a work-queue accumulation (e.g. research deferred_findings)
would adopt. The end-to-end review-loop spawn-path coverage lives in
test_review_pipeline.py::test_review_history_feedback_is_bounded.
"""

from app.context_bounds import (
    CONTEXT_SOFT_WARN_BYTES,
    DEFERRED_FINDING_MAX_CHARS,
    REVIEW_FEEDBACK_MAX_CHARS,
    REVIEW_HISTORY_MAX_ENTRIES,
    _TRUNCATION_MARKER,
    bound_history_entries,
    bound_review_history,
    context_size_bytes,
    warn_if_context_oversized,
)


# ── bound_history_entries: text truncation ────────────────────────────────────


def test_truncates_oversized_text_field_with_marker():
    huge = "x" * 1_000_000
    out = bound_history_entries(
        [{"feedback": huge}],
        text_fields=("feedback",),
        max_field_chars=100,
    )
    fb = out[0]["feedback"]
    # Truncated to exactly the cap (prefix + marker), and marked as cut.
    assert len(fb) == 100
    assert fb.endswith(_TRUNCATION_MARKER)
    assert fb.startswith("x")


def test_short_text_field_is_byte_identical():
    """Below-cap feedback must pass through untouched — this is the common case,
    so truncation is a no-op for realistic feedback and existing behavior holds."""
    short = "Looks good, fix the null check on line 42."
    out = bound_history_entries(
        [{"feedback": short}],
        text_fields=("feedback",),
        max_field_chars=REVIEW_FEEDBACK_MAX_CHARS,
    )
    assert out[0]["feedback"] == short
    assert _TRUNCATION_MARKER not in out[0]["feedback"]


def test_structured_fields_are_preserved_verbatim():
    """Only named text_fields are touched. issues / changed_files / finding_hash /
    verdict / iteration — the data every structured consumer reads — pass through
    unchanged, including when the text field on the same entry is truncated."""
    entry = {
        "iteration": 3,
        "feedback": "y" * 50_000,
        "verdict": "REQUEST_CHANGES",
        "issues": [{"description": "real bug", "severity": "CRITICAL"}],
        "changed_files": ["a.py", "b.py"],
        "finding_hash": "abc123",
    }
    out = bound_history_entries(
        [entry], text_fields=("feedback",), max_field_chars=1000
    )[0]
    assert out["iteration"] == 3
    assert out["verdict"] == "REQUEST_CHANGES"
    assert out["issues"] == [{"description": "real bug", "severity": "CRITICAL"}]
    assert out["changed_files"] == ["a.py", "b.py"]
    assert out["finding_hash"] == "abc123"
    # The text field WAS bounded though.
    assert len(out["feedback"]) == 1000


def test_non_string_text_field_is_not_coerced():
    """A text_field that is not a str (None, or accidentally a list) is left as-is
    rather than crashing or being stringified."""
    out = bound_history_entries(
        [{"feedback": None}, {"feedback": ["unexpected"]}],
        text_fields=("feedback",),
        max_field_chars=10,
    )
    assert out[0]["feedback"] is None
    assert out[1]["feedback"] == ["unexpected"]


def test_multiple_text_fields_each_truncated():
    out = bound_history_entries(
        [{"description": "d" * 500, "title": "t" * 500, "keep": "k" * 500}],
        text_fields=("description", "title"),
        max_field_chars=50,
    )[0]
    assert len(out["description"]) == 50
    assert len(out["title"]) == 50
    # A field not named in text_fields is never touched.
    assert out["keep"] == "k" * 500


# ── bound_history_entries: entry-count cap ────────────────────────────────────


def test_caps_entry_count_keeping_most_recent():
    entries = [{"iteration": i, "feedback": "f"} for i in range(100)]
    out = bound_history_entries(
        entries, text_fields=("feedback",), max_field_chars=10, max_entries=10
    )
    assert len(out) == 10
    # Keep-most-recent: iterations 90..99, in order.
    assert [e["iteration"] for e in out] == list(range(90, 100))


def test_max_entries_none_keeps_all():
    """Truncate-only mode (the deferred_findings / work-queue shape): no entry is
    dropped — dropping a queued work item would silently lose work — only the
    per-entry text is bounded."""
    entries = [{"description": "d" * 9000, "finding_hash": f"h{i}"} for i in range(40)]
    out = bound_history_entries(
        entries,
        text_fields=("description",),
        max_field_chars=DEFERRED_FINDING_MAX_CHARS,
        max_entries=None,
    )
    assert len(out) == 40  # none dropped
    assert all(len(e["description"]) == DEFERRED_FINDING_MAX_CHARS for e in out)
    assert [e["finding_hash"] for e in out] == [f"h{i}" for i in range(40)]


def test_max_entries_zero_keeps_none():
    out = bound_history_entries(
        [{"feedback": "f"}],
        text_fields=("feedback",),
        max_field_chars=10,
        max_entries=0,
    )
    assert out == []


# ── bound_history_entries: safety ─────────────────────────────────────────────


def test_does_not_mutate_input():
    original = [{"feedback": "z" * 9000, "issues": [{"a": 1}]}]
    snapshot_feedback = original[0]["feedback"]
    bound_history_entries(
        original, text_fields=("feedback",), max_field_chars=100, max_entries=1
    )
    # Original list and its dicts are untouched.
    assert original[0]["feedback"] == snapshot_feedback
    assert len(original[0]["feedback"]) == 9000
    assert original[0]["issues"] == [{"a": 1}]


def test_empty_and_none_safe():
    assert (
        bound_history_entries(None, text_fields=("feedback",), max_field_chars=10) == []
    )
    assert (
        bound_history_entries([], text_fields=("feedback",), max_field_chars=10) == []
    )


def test_non_dict_entries_pass_through():
    out = bound_history_entries(
        ["raw", 5, {"feedback": "q" * 100}],
        text_fields=("feedback",),
        max_field_chars=10,
    )
    assert out[0] == "raw"
    assert out[1] == 5
    assert len(out[2]["feedback"]) == 10


# ── bound_review_history wrapper ──────────────────────────────────────────────


def test_bound_review_history_applies_both_bounds():
    # More entries than the cap, each with an oversized feedback blob.
    history = [
        {"iteration": i, "feedback": "L" * (REVIEW_FEEDBACK_MAX_CHARS + 5000)}
        for i in range(REVIEW_HISTORY_MAX_ENTRIES + 25)
    ]
    out = bound_review_history(history)
    assert len(out) == REVIEW_HISTORY_MAX_ENTRIES
    assert all(len(e["feedback"]) <= REVIEW_FEEDBACK_MAX_CHARS for e in out)
    # Most-recent kept.
    assert out[-1]["iteration"] == REVIEW_HISTORY_MAX_ENTRIES + 24


def test_bound_review_history_noop_for_normal_history():
    """A realistic short chain is returned with identical feedback (a structural-
    equality copy), so no existing review-loop behavior changes."""
    history = [
        {"iteration": 0, "feedback": "first review", "verdict": "REQUEST_CHANGES"},
        {"iteration": 1, "feedback": "", "verdict": "APPROVE"},
    ]
    out = bound_review_history(history)
    assert out == history


# ── telemetry ─────────────────────────────────────────────────────────────────


def test_context_size_bytes_counts_serialized_size():
    assert context_size_bytes(None) == 0
    assert context_size_bytes({}) == 0
    ctx = {"k": "v" * 1000}
    assert context_size_bytes(ctx) >= 1000


def test_context_size_bytes_handles_non_json_native_values():
    """default=str keeps the size total over values json can't natively encode
    (a set here) instead of raising into a write path."""
    assert context_size_bytes({"weird": {1, 2, 3}}) > 0


def test_warn_if_context_oversized_returns_size_and_warns(caplog):
    small = {"a": "b"}
    assert warn_if_context_oversized(small, label="t") == context_size_bytes(small)

    big = {"blob": "x" * (CONTEXT_SOFT_WARN_BYTES + 1000)}
    import logging

    with caplog.at_level(logging.WARNING, logger="app.context_bounds"):
        size = warn_if_context_oversized(big, label="spawn fix PR#1")
    assert size > CONTEXT_SOFT_WARN_BYTES
    assert any("possible fat-row regression" in r.message for r in caplog.records)
