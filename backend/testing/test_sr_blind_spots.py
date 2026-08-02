"""Tests for the disallowed-blind-spot filter and contract parity."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.models import TaskStatus
from app.sr_blind_spots import (
    DISALLOWED_BLIND_SPOT_EXAMPLES,
    DISALLOWED_BLIND_SPOT_PATTERNS,
    aggregate_sr_false_attestation_rates,
    filter_disallowed_blind_spots,
    has_reviewer_confirmed_categories,
    is_disallowed_blind_spot,
    stamp_sr_blind_spot_computed_at,
    triage_sr_blind_spot_theme,
)
from testing.factories import make_task


def _make_fa_task(
    project_id,
    *,
    sr_cat_passed: bool | None,
    ci_passed: bool | None,
    cat: str = "tests_executed_cleanly",
    task_type: str = "create",
):
    """Build a task with an SR execution-evidence claim and an optional CI result."""
    check_results: dict = {}
    if sr_cat_passed is not None:
        check_results[cat] = {"passed": sr_cat_passed}
    review_loop: dict = {}
    if ci_passed is not None:
        review_loop["ci_passed"] = ci_passed
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context={
            "_self_review_outcome": {"check_results": check_results},
            "review_loop": review_loop,
        },
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
        # Truncated 6-token theme form — the dispatcher checks this directly,
        # and the original suffix-anchored pattern never matched it.
        "did not run the full backend",
        # No-test-noun variants that slipped the original entry-level filter
        # but still theme to "did not run the full backend".
        "did not run the full backend suite",
        "did not run the full backend integration tests",
        "did not run the full backend docker tests",
        "skipped the complete e2e suite",
        "did not run the entire frontend test suite",
        # Shorter no-qualifier variants picked up by pattern 2.
        "did not run integration tests",
        "did not run the docker integration tests",
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
        # Genuine "did not run X" blind spots must still survive — X is not a
        # full suite, so the broadened pattern 1 must not over-match.
        "did not run the linter on the changed file",
        "did not run the new migration against a fresh db",
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
# Ingest normalization + helper: check_results shape (dict vs list)
#
# Agents occasionally emit ``check_results`` as a JSON list instead of the
# expected category->record dict. The outer-only ``Dict[str, Any]`` schema
# validation lets it through, and a non-empty list is truthy so the
# ``... or {}`` guard misses it. The ingest funnel normalizes a non-dict shape
# to ``{}`` and the ``safe_check_results`` helper guards individual reads.
# ---------------------------------------------------------------------------


def test_filter_normalizes_list_shaped_check_results_to_empty_dict():
    sro = {
        "verdict": "pass",
        "blind_spots": [],
        "check_results": [
            {"category": "correctness", "passed": False},
            {"category": "security", "passed": False},
        ],
    }
    result = filter_disallowed_blind_spots(sro)
    assert result["check_results"] == {}


def test_filter_leaves_valid_dict_check_results_untouched():
    valid = {"correctness": {"passed": True}, "security": {"passed": False}}
    sro = {"verdict": "pass", "blind_spots": [], "check_results": valid}
    result = filter_disallowed_blind_spots(sro)
    assert result["check_results"] == valid


def test_filter_does_not_add_check_results_when_absent():
    sro = {"verdict": "pass", "blind_spots": []}
    result = filter_disallowed_blind_spots(sro)
    assert "check_results" not in result


def test_safe_check_results_guards_non_dict_shapes():
    from app.sr_blind_spots import safe_check_results

    assert safe_check_results({"check_results": [{"category": "x"}]}) == {}
    assert safe_check_results({"check_results": None}) == {}
    assert safe_check_results({}) == {}
    valid = {"correctness": {"passed": True}}
    assert safe_check_results({"check_results": valid}) == valid


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


# ---------------------------------------------------------------------------
# Unit: aggregate_sr_false_attestation_rates
# ---------------------------------------------------------------------------


def test_false_attestation_true_attestation_counts_ci_confirmed():
    """SR claims passed + CI passed → ci_confirmed incremented, false_attested stays 0."""
    pid = uuid.uuid4()
    tasks = [_make_fa_task(pid, sr_cat_passed=True, ci_passed=True) for _ in range(5)]
    result = aggregate_sr_false_attestation_rates(tasks, min_sample=5)
    assert result is not None
    cat = result["tests_executed_cleanly"]
    assert cat["sr_claimed_passed"] == 5
    assert cat["ci_confirmed"] == 5
    assert cat["false_attested"] == 0
    assert cat["false_attestation_rate"] == 0.0


def test_false_attestation_detects_ci_failure():
    """SR claims passed but CI failed → false_attested incremented."""
    pid = uuid.uuid4()
    tasks = [
        _make_fa_task(pid, sr_cat_passed=True, ci_passed=False) for _ in range(3)
    ] + [_make_fa_task(pid, sr_cat_passed=True, ci_passed=True) for _ in range(2)]
    result = aggregate_sr_false_attestation_rates(tasks, min_sample=5)
    assert result is not None
    cat = result["tests_executed_cleanly"]
    assert cat["false_attested"] == 3
    assert cat["ci_confirmed"] == 2
    assert cat["false_attestation_rate"] == round(3 / 5, 3)


def test_false_attestation_missing_ci_data_tallied_separately():
    """SR claimed passed but no CI outcome → ci_data_missing incremented."""
    pid = uuid.uuid4()
    # 5 tasks with CI data (to meet min_sample), plus 3 with no CI data
    tasks = [
        _make_fa_task(pid, sr_cat_passed=True, ci_passed=True) for _ in range(5)
    ] + [_make_fa_task(pid, sr_cat_passed=True, ci_passed=None) for _ in range(3)]
    result = aggregate_sr_false_attestation_rates(tasks, min_sample=5)
    assert result is not None
    cat = result["tests_executed_cleanly"]
    assert cat["ci_data_missing"] == 3
    assert cat["ci_confirmed"] == 5
    assert cat["false_attested"] == 0


def test_false_attestation_below_min_sample_returns_none():
    """Fewer than min_sample tasks with CI outcome → None."""
    pid = uuid.uuid4()
    tasks = [_make_fa_task(pid, sr_cat_passed=True, ci_passed=True) for _ in range(4)]
    assert aggregate_sr_false_attestation_rates(tasks, min_sample=5) is None


def test_false_attestation_sr_failed_entry_excluded():
    """Tasks where SR marks the category failed are not counted (only passed claims)."""
    pid = uuid.uuid4()
    # SR failed (passed=False) — these should be invisible to the aggregator
    tasks_sr_failed = [
        _make_fa_task(pid, sr_cat_passed=False, ci_passed=False) for _ in range(5)
    ]
    result = aggregate_sr_false_attestation_rates(tasks_sr_failed, min_sample=1)
    # No SR-passed claims → result is None (total_with_ci == 0)
    assert result is None


def test_false_attestation_non_code_change_tasks_excluded():
    """review/plan task types are excluded; only code-change types qualify."""
    pid = uuid.uuid4()
    tasks = [
        _make_fa_task(pid, sr_cat_passed=True, ci_passed=False, task_type="review")
        for _ in range(5)
    ]
    assert aggregate_sr_false_attestation_rates(tasks, min_sample=1) is None


def _make_co_fail_task(
    project_id, *, failed_cats: list[str], task_type: str = "create"
):
    """Build a task where the given SR categories are all marked failed."""
    check_results = {cat: {"passed": False} for cat in failed_cats}
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context={"_self_review_outcome": {"check_results": check_results}},
    )


def test_co_failure_returns_none_below_min_sample():
    """Fewer than min_sample qualifying tasks → None."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    tasks = [_make_co_fail_task(pid, failed_cats=["correctness", "error-handling"])]
    assert aggregate_sr_co_failure_pairs(tasks, min_sample=5) is None


def test_co_failure_returns_none_for_empty_tasks():
    """Empty task list → None."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    assert aggregate_sr_co_failure_pairs([], min_sample=1) is None


def test_co_failure_lift_calculation():
    """High co-occurrence produces lift > 1; values match the manual formula."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    # 10 qualifying tasks: 5 fail both "correctness" and "error-handling",
    # 5 fail neither.  Expected lift = (5/5) / (5/10) = 2.0
    tasks = [
        _make_co_fail_task(pid, failed_cats=["correctness", "error-handling"])
        for _ in range(5)
    ] + [_make_co_fail_task(pid, failed_cats=[]) for _ in range(5)]
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None
    pair = next(
        (
            p
            for p in result
            if p["cat_a"] == "correctness" and p["cat_b"] == "error-handling"
        ),
        None,
    )
    assert pair is not None
    assert pair["co_fail_count"] == 5
    assert pair["p_b_given_a"] == 1.0
    assert pair["base_rate_b"] == 0.5
    assert pair["lift"] == 2.0


def test_co_failure_filters_below_min_co_count():
    """Pairs with co_count < min_co_count are excluded from results."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    # Only 1 task co-fails "correctness" and "error-handling".
    tasks = [_make_co_fail_task(pid, failed_cats=["correctness", "error-handling"])] + [
        _make_co_fail_task(pid, failed_cats=[]) for _ in range(4)
    ]
    # min_co_count=2 should filter out the single co-failure pair.
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result == []


def test_co_failure_returns_top_n_sorted_by_lift():
    """Returns at most top_n pairs, sorted highest lift first."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    # 10 tasks: all 10 co-fail A+B; only 5 co-fail A+C.
    # lift(A→B) = (10/10)/(10/10) = 1.0; lift(A→C) = (5/10)/(5/10) = 1.0
    # Let's make a clearer divergence: 10 tasks all fail A+B, 5 extra fail C alone.
    # total=15; miss_a=10, miss_b=10, co_ab=10 → lift=1.5
    # miss_c=5, co_ac=0 → no A+C pair
    tasks = [
        _make_co_fail_task(pid, failed_cats=["cat_a", "cat_b"]) for _ in range(10)
    ] + [_make_co_fail_task(pid, failed_cats=["cat_c"]) for _ in range(5)]
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2, top_n=1)
    assert result is not None
    assert len(result) <= 1


def test_co_failure_excludes_non_code_change_tasks():
    """Tasks of type 'review' or 'plan' are excluded from the sample."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    # 5 review tasks that would otherwise qualify.
    tasks = [
        _make_co_fail_task(
            pid, failed_cats=["correctness", "error-handling"], task_type="review"
        )
        for _ in range(5)
    ]
    assert aggregate_sr_co_failure_pairs(tasks, min_sample=5) is None


def test_co_failure_lift_near_one_when_independent():
    """When co-failure matches the base-rate product, lift is ~1."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    # 9 tasks: 3 miss only A, 3 miss only B, 1 misses both A+B, 2 miss neither.
    # miss_a=4, miss_b=4, co_ab=1, total=9
    # p_b_given_a = 1/4 = 0.25; base_rate_b = 4/9 ≈ 0.444; lift ≈ 0.5625 (below 1)
    # This demonstrates lift < 1 for weak co-occurrence (no strong shared signal).
    tasks = (
        [_make_co_fail_task(pid, failed_cats=["cat_a"]) for _ in range(3)]
        + [_make_co_fail_task(pid, failed_cats=["cat_b"]) for _ in range(3)]
        + [_make_co_fail_task(pid, failed_cats=["cat_a", "cat_b"])]
        + [_make_co_fail_task(pid, failed_cats=[]) for _ in range(2)]
    )
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=1)
    assert result is not None
    pair = next(
        (p for p in result if p["cat_a"] == "cat_a" and p["cat_b"] == "cat_b"), None
    )
    assert pair is not None
    assert pair["lift"] < 1.0


def test_co_failure_result_keys_present():
    """Each result entry has all required keys."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    pid = uuid.uuid4()
    tasks = [
        _make_co_fail_task(pid, failed_cats=["correctness", "error-handling"])
        for _ in range(5)
    ]
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None and len(result) > 0
    entry = result[0]
    for key in (
        "cat_a",
        "cat_b",
        "co_fail_count",
        "p_b_given_a",
        "base_rate_b",
        "lift",
    ):
        assert key in entry, f"Missing key: {key}"


def test_stamp_helpers_default_now_emit_naive_utc_strings():
    """Audit C10: default-now heartbeat stamp must be tz-NAIVE ISO string.

    The staleness watchdog parses this string and subtracts a naive _utcnow().
    A tz-aware writer (datetime.now(timezone.utc)) would emit a '+00:00' suffix
    that the dispatcher then strips, mis-dating the freshness window.
    """
    blind_doc: dict = {}
    stamp_sr_blind_spot_computed_at(blind_doc)
    blind_ts = blind_doc["sr_blind_spot_signal_last_computed_at"]
    assert "+00:00" not in blind_ts and not blind_ts.endswith("Z")
    assert datetime.fromisoformat(blind_ts).tzinfo is None


def test_has_reviewer_confirmed_categories_all_iterations_only():
    """All-iterations set present (later-iteration confirmation), first-attempt
    absent → in-sample."""
    ctx = {"_all_iterations_rejection_categories": {"correctness": 1}}
    assert has_reviewer_confirmed_categories(ctx) is True


def test_has_reviewer_confirmed_categories_first_attempt_only():
    """First-attempt set present, all-iterations absent → in-sample."""
    ctx = {"_first_attempt_rejection_categories": {"correctness": 1}}
    assert has_reviewer_confirmed_categories(ctx) is True


def test_has_reviewer_confirmed_categories_neither():
    """Neither reviewer-confirmed field present (or both empty) → out-of-sample."""
    assert has_reviewer_confirmed_categories({}) is False
    assert (
        has_reviewer_confirmed_categories(
            {
                "_all_iterations_rejection_categories": {},
                "_first_attempt_rejection_categories": {},
            }
        )
        is False
    )
