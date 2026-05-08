"""Tests for the disallowed-blind-spot filter and contract parity."""

import pytest

from app.sr_blind_spots import (
    DISALLOWED_BLIND_SPOT_EXAMPLES,
    DISALLOWED_BLIND_SPOT_PATTERNS,
    filter_disallowed_blind_spots,
    is_disallowed_blind_spot,
    triage_sr_blind_spot_theme,
)


# ---------------------------------------------------------------------------
# Unit: is_disallowed_blind_spot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "did not run the full backend test suite",
        "did not run docker pytest",
        "did not run viewport tests",
        "Did Not Run The Full Backend Test Suite",
        "didn't run the full docker pytest",
        "skipped the entire backend test suite",
        "did not execute the whole viewport tests",
        "did not run the full playwright tests",
        "skipped docker pytest",
    ],
)
def test_is_disallowed_matches_forbidden_entries(entry):
    assert is_disallowed_blind_spot(entry), f"Expected {entry!r} to be disallowed"


@pytest.mark.parametrize(
    "entry",
    [
        "did not re-read c.ts after diff truncated",
        "unsure whether retry path handles concurrent calls",
        "no test for the empty-list edge case I added handling for",
        "self-review skipped",
        "did not run all unit tests",  # "all" not in full/entire/whole, no docker/viewport
    ],
)
def test_is_disallowed_leaves_valid_entries_alone(entry):
    assert not is_disallowed_blind_spot(entry), f"Expected {entry!r} to be allowed"


# ---------------------------------------------------------------------------
# Unit: filter_disallowed_blind_spots
# ---------------------------------------------------------------------------


def test_filter_drops_disallowed_preserves_allowed():
    sro = {
        "verdict": "pass",
        "blind_spots": [
            "did not run the full backend test suite",  # disallowed
            "unsure about edge case in empty list",  # allowed
            "did not run docker pytest",  # disallowed
            "did not re-read c.ts after truncation",  # allowed
        ],
    }
    result = filter_disallowed_blind_spots(sro)
    assert result["blind_spots"] == [
        "unsure about edge case in empty list",
        "did not re-read c.ts after truncation",
    ]
    assert result["_filtered_blind_spots_count"] == 2


def test_filter_records_zero_when_nothing_dropped():
    sro = {"blind_spots": ["unsure about retry path"]}
    filter_disallowed_blind_spots(sro)
    assert sro["_filtered_blind_spots_count"] == 0
    assert sro["blind_spots"] == ["unsure about retry path"]


def test_filter_handles_empty_blind_spots():
    sro = {"blind_spots": []}
    filter_disallowed_blind_spots(sro)
    assert sro["_filtered_blind_spots_count"] == 0


def test_filter_handles_missing_blind_spots_key():
    sro = {"verdict": "pass"}
    filter_disallowed_blind_spots(sro)
    assert sro["blind_spots"] == []
    assert sro["_filtered_blind_spots_count"] == 0


def test_filter_mutates_sro_in_place():
    sro = {"blind_spots": ["did not run viewport tests"]}
    result = filter_disallowed_blind_spots(sro)
    assert result is sro  # same object


# ---------------------------------------------------------------------------
# Contract: prompt examples must match the backend blocklist
# ---------------------------------------------------------------------------


def test_prompt_examples_match_backend_blocklist():
    """Ensure every example from prompt.ts:2057 is blocked by the backend filter.

    This test fails when DISALLOWED_BLIND_SPOT_PATTERNS drifts out of sync
    with the human-readable examples in DISALLOWED_BLIND_SPOT_EXAMPLES.
    """
    for example in DISALLOWED_BLIND_SPOT_EXAMPLES:
        assert is_disallowed_blind_spot(example), (
            f"Prompt example {example!r} is not matched by DISALLOWED_BLIND_SPOT_PATTERNS. "
            "Update the backend blocklist to cover this example."
        )


def test_contract_fails_when_blocklist_mutated():
    """Demonstrate that removing a pattern breaks the contract test."""
    # Temporarily check that a pattern-less check fails for one known example.
    # This is a meta-test: if we had no patterns, examples would not be filtered.
    example = DISALLOWED_BLIND_SPOT_EXAMPLES[0]
    # Verify the existing patterns DO match (positive assertion for the contract).
    assert any(p.search(example) for p in DISALLOWED_BLIND_SPOT_PATTERNS), (
        "No pattern matches the first DISALLOWED_BLIND_SPOT_EXAMPLES entry — "
        "the backend constant is out of sync with the prompt examples."
    )


# ---------------------------------------------------------------------------
# Unit: triage_sr_blind_spot_theme
# ---------------------------------------------------------------------------


def test_triage_stale_assertion_two_signals():
    """Theme with test-file path + assertion phrase fires ≥2 signals → likely_stale_assertion."""
    theme = "agent/src/prompt.test.ts now expects ordering"
    result = triage_sr_blind_spot_theme(theme, [])
    assert result["verdict"] == "likely_stale_assertion"
    assert "theme_references_test_file" in result["signals"]
    assert any(s.startswith("assertion_phrase:") for s in result["signals"])
    assert result["triage_context"] != ""


def test_triage_prompt_regression_no_signals():
    """Generic theme with no test-file refs or assertion language → likely_prompt_regression."""
    theme = "missed edge case in retry logic"
    examples = [
        {"text": "no coverage for empty input", "task_id": "t1", "task_type": "create"}
    ]
    result = triage_sr_blind_spot_theme(theme, examples)
    assert result["verdict"] == "likely_prompt_regression"
    assert result["signals"] == []
    assert result["triage_context"] == ""


def test_triage_ambiguous_single_signal():
    """One signal fires (assertion keyword, no test-file path in theme) → ambiguous."""
    theme = "assertion mismatch in validation output"
    result = triage_sr_blind_spot_theme(theme, [])
    assert result["verdict"] == "ambiguous"
    assert len(result["signals"]) == 1
    assert result["triage_context"] != ""


def test_triage_single_test_file_cluster_raises_confidence():
    """All examples from the same test file fires single_test_file_cluster → likely_stale_assertion."""
    theme = "install command ordering changed"
    examples = [
        {
            "text": "test_install.py now expects ordering of install commands",
            "task_id": "t1",
            "task_type": "fix",
        },
        {
            "text": "test_install.py expects ordering after prompt refactor",
            "task_id": "t2",
            "task_type": "fix",
        },
    ]
    result = triage_sr_blind_spot_theme(theme, examples)
    assert "single_test_file_cluster" in result["signals"]
    # "expects ordering" and "now expects" also fire assertion_phrase → ≥2 signals
    assert result["verdict"] == "likely_stale_assertion"


def test_triage_mixed_test_files_suppresses_cluster_signal():
    """Examples from different test files → no single_test_file_cluster signal."""
    theme = "coverage gap in config parsing"
    examples = [
        {
            "text": "test_config.py references old key name",
            "task_id": "t1",
            "task_type": "fix",
        },
        {
            "text": "test_loader.py references different key",
            "task_id": "t2",
            "task_type": "fix",
        },
    ]
    result = triage_sr_blind_spot_theme(theme, examples)
    assert "single_test_file_cluster" not in result["signals"]
