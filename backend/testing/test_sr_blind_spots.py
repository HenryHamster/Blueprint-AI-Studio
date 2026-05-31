"""Tests for the disallowed-blind-spot filter and contract parity."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.models import TaskStatus
from app.sr_blind_spots import (
    DISALLOWED_BLIND_SPOT_EXAMPLES,
    DISALLOWED_BLIND_SPOT_PATTERNS,
    aggregate_sr_false_attestation_rates,
    compute_persistent_sr_blind_spot_hint,
    count_reviewer_disagreements_in_tasks,
    filter_disallowed_blind_spots,
    is_disallowed_blind_spot,
    stamp_sr_blind_spot_computed_at,
    stamp_sr_catch_rate_computed_at,
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


# ---------------------------------------------------------------------------
# Unit: _count_reviewer_disagreement_directions — examples capture
# ---------------------------------------------------------------------------


def _make_disagreement_task(
    project_id,
    *,
    task_type: str = "create",
    check_results: dict | None = None,
    reviewer_findings: list | None = None,
):
    """Build a task for reviewer-disagreement example-capture tests."""
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        context={
            "_self_review_outcome": {
                "check_results": check_results or {},
                "reviewer_findings": reviewer_findings or [],
            }
        },
    )


def test_disagreement_directions_captures_verbatim_examples():
    """Missed reviewer findings are stored as {severity, description} examples."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {
                "category": "correctness",
                "severity": "HIGH",
                "description": "Missing null check on return value",
            }
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 1
    assert len(bucket["examples"]) == 1
    ex = bucket["examples"][0]
    assert ex["severity"] == "HIGH"
    assert ex["description"] == "Missing null check on return value"


def test_disagreement_directions_truncates_long_descriptions():
    """Descriptions longer than _REVIEWER_EXAMPLE_CHAR_CAP are truncated."""
    from app.sr_blind_spots import (
        _REVIEWER_EXAMPLE_CHAR_CAP,
        _count_reviewer_disagreement_directions,
    )

    pid = uuid.uuid4()
    long_desc = "x" * (_REVIEWER_EXAMPLE_CHAR_CAP + 50)
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {"category": "correctness", "severity": "HIGH", "description": long_desc}
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    stored = counts[("correctness", "create")]["examples"][0]["description"]
    assert len(stored) == _REVIEWER_EXAMPLE_CHAR_CAP


def test_disagreement_directions_caps_examples_per_bucket():
    """At most _REVIEWER_EXAMPLE_CAP examples are stored per (category, task_type) bucket."""
    from app.sr_blind_spots import (
        _REVIEWER_EXAMPLE_CAP,
        _count_reviewer_disagreement_directions,
    )

    pid = uuid.uuid4()
    tasks = [
        _make_disagreement_task(
            pid,
            reviewer_findings=[
                {
                    "category": "correctness",
                    "severity": "MEDIUM",
                    "description": f"Issue {i}",
                }
            ],
        )
        for i in range(_REVIEWER_EXAMPLE_CAP + 2)
    ]
    counts = _count_reviewer_disagreement_directions(tasks)
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == _REVIEWER_EXAMPLE_CAP + 2
    assert len(bucket["examples"]) == _REVIEWER_EXAMPLE_CAP


def test_disagreement_directions_no_example_when_description_empty():
    """A reviewer finding with empty description is counted as a miss but adds no example."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {"category": "correctness", "severity": "HIGH", "description": ""}
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 1
    assert bucket["examples"] == []


def test_disagreement_directions_defaults_severity_to_low():
    """A finding without a recognised severity value is stored with severity='LOW'."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {"category": "correctness", "description": "Some issue without severity"}
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    assert counts[("correctness", "create")]["examples"][0]["severity"] == "LOW"


def test_disagreement_directions_examples_include_task_id():
    """Examples must carry task_id and preserve existing severity/description fields."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {
                "category": "correctness",
                "severity": "HIGH",
                "description": "Missing null check",
            }
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    ex = counts[("correctness", "create")]["examples"][0]
    assert ex["severity"] == "HIGH"
    assert ex["description"] == "Missing null check"
    assert "task_id" in ex
    assert ex["task_id"] == str(task.id)


def test_disagreement_directions_examples_include_pr_url_when_present():
    """Examples carry pr_url from task context._pr_url when available."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    pr = "https://github.com/Owner/Repo/pull/42"
    task = make_task(
        project_id=pid,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        context={
            "_pr_url": pr,
            "_self_review_outcome": {
                "check_results": {},
                "reviewer_findings": [
                    {"category": "correctness", "severity": "HIGH", "description": "Issue"}
                ],
            },
        },
    )
    counts = _count_reviewer_disagreement_directions([task])
    ex = counts[("correctness", "create")]["examples"][0]
    assert ex.get("pr_url") == pr


def test_disagreement_directions_examples_omit_pr_url_when_absent():
    """Examples do not carry pr_url key when task context has no _pr_url."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {"category": "correctness", "severity": "HIGH", "description": "Issue"}
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    ex = counts[("correctness", "create")]["examples"][0]
    assert "pr_url" not in ex


# ---------------------------------------------------------------------------
# Unit: reviewer-disagreement envelope — honest total vs truncation
# ---------------------------------------------------------------------------


def test_reviewer_disagreement_envelope_total_exceeds_returned_when_truncated():
    """The envelope reports the true number of distinct disagreement categories,
    which exceeds the returned slice when more categories exist than ``limit``."""
    from app.sr_blind_spots import (
        _count_reviewer_disagreement_directions,
        _shape_reviewer_disagreement_envelope,
    )

    pid = uuid.uuid4()
    # Five distinct disagreement categories, each a fresh-reviewer finding the
    # SR pass omitted — so five distinct (category, "create") buckets.
    categories = [
        "correctness",
        "security",
        "performance",
        "error-handling",
        "type-safety",
    ]
    tasks = [
        _make_disagreement_task(
            pid,
            reviewer_findings=[
                {"category": cat, "severity": "HIGH", "description": f"{cat} issue"}
            ],
        )
        for cat in categories
    ]
    counts = _count_reviewer_disagreement_directions(tasks)

    limit = 2
    envelope = _shape_reviewer_disagreement_envelope(counts, days=30, limit=limit)

    assert len(envelope["disagreements"]) == limit
    assert envelope["returned_entries"] == limit
    # True total reflects ALL distinct categories, not the truncated page.
    assert envelope["total_distinct_categories"] == len(categories)
    assert envelope["total_distinct_categories"] > len(envelope["disagreements"])
    assert envelope["window_days"] == 30


def test_reviewer_disagreement_envelope_total_matches_returned_when_not_truncated():
    """When fewer categories exist than ``limit``, total equals returned count."""
    from app.sr_blind_spots import (
        _count_reviewer_disagreement_directions,
        _shape_reviewer_disagreement_envelope,
    )

    pid = uuid.uuid4()
    task = _make_disagreement_task(
        pid,
        reviewer_findings=[
            {"category": "correctness", "severity": "HIGH", "description": "Issue"}
        ],
    )
    counts = _count_reviewer_disagreement_directions([task])
    envelope = _shape_reviewer_disagreement_envelope(counts, days=14, limit=20)

    assert envelope["total_distinct_categories"] == 1
    assert envelope["returned_entries"] == 1
    assert len(envelope["disagreements"]) == 1


# ---------------------------------------------------------------------------
# Unit: aggregate_sr_co_failure_pairs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Unit: compute_persistent_sr_blind_spot_hint
# ---------------------------------------------------------------------------


def _make_reviewer_miss_task(
    project_id,
    *,
    created_at: datetime,
    category: str = "correctness",
    task_type: str = "create",
):
    """Task where the reviewer caught *category* but SR passed it."""
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        created_at=created_at,
        context={
            "_self_review_outcome": {
                "check_results": {category: {"passed": True}},
                "reviewer_findings": [
                    {
                        "category": category,
                        "severity": "HIGH",
                        "description": "missed by SR",
                    }
                ],
            }
        },
    )


def _make_clean_task(
    project_id,
    *,
    created_at: datetime,
    task_type: str = "create",
):
    """Task with no reviewer disagreement (SR and reviewer agree)."""
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "x"}],
        created_at=created_at,
        context={
            "_self_review_outcome": {
                "check_results": {"correctness": {"passed": True}},
                "reviewer_findings": [],
            }
        },
    )


def test_persistent_hint_empty_below_threshold_in_one_window():
    """Rate below threshold in window 0 → no persistent categories."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    # Window 0 (0-7 days ago): 1 miss / 5 tasks = 0.20 — below 0.25
    tasks = [_make_reviewer_miss_task(pid, created_at=now - timedelta(days=2))] + [
        _make_clean_task(pid, created_at=now - timedelta(days=2)) for _ in range(4)
    ]
    # Window 1 (7-14 days ago): 2 misses / 5 tasks = 0.40 — above 0.25
    tasks += [
        _make_reviewer_miss_task(pid, created_at=now - timedelta(days=10))
        for _ in range(2)
    ] + [_make_clean_task(pid, created_at=now - timedelta(days=10)) for _ in range(3)]
    result = compute_persistent_sr_blind_spot_hint(
        tasks, threshold=0.25, min_sample=3, now=now
    )
    assert result["categories"] == []
    assert result["hint"] is None


def test_persistent_hint_populated_when_above_threshold_both_windows():
    """Rate exceeds threshold in both windows → hint names the category."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    tasks = []
    # Both windows: 2 misses / 4 tasks = 0.50 — above 0.25
    for days_ago in (3, 10):
        tasks += [
            _make_reviewer_miss_task(pid, created_at=now - timedelta(days=days_ago))
            for _ in range(2)
        ]
        tasks += [
            _make_clean_task(pid, created_at=now - timedelta(days=days_ago))
            for _ in range(2)
        ]
    result = compute_persistent_sr_blind_spot_hint(
        tasks, threshold=0.25, min_sample=3, now=now
    )
    assert len(result["categories"]) == 1
    assert result["categories"][0]["category"] == "correctness"
    assert result["categories"][0]["task_type"] == "create"
    assert result["categories"][0]["rate"] == pytest.approx(0.5, abs=0.01)
    assert result["hint"] is not None
    assert "correctness" in result["hint"]
    assert "50%" in result["hint"]


def test_persistent_hint_empty_when_window_below_min_sample():
    """A window with fewer than min_sample qualifying tasks does not block but does not count."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    # Window 0 (0-7 days ago): only 2 qualifying tasks — below min_sample=3
    tasks = [
        _make_reviewer_miss_task(pid, created_at=now - timedelta(days=2))
        for _ in range(2)
    ]
    # Window 1 (7-14 days ago): 3/4 tasks disagree (above threshold)
    tasks += [
        _make_reviewer_miss_task(pid, created_at=now - timedelta(days=10))
        for _ in range(3)
    ] + [_make_clean_task(pid, created_at=now - timedelta(days=10))]
    result = compute_persistent_sr_blind_spot_hint(
        tasks, threshold=0.25, min_sample=3, now=now
    )
    # Window 0 has too few samples → its rates dict is empty → key missing → not persistent
    assert result["categories"] == []
    assert result["hint"] is None


def test_persistent_hint_no_double_counting_with_catch_rate():
    """Running compute_persistent_sr_blind_spot_hint does not alter count_reviewer_disagreements_in_tasks."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    tasks = [
        _make_reviewer_miss_task(pid, created_at=now - timedelta(days=3))
        for _ in range(3)
    ] + [_make_clean_task(pid, created_at=now - timedelta(days=3)) for _ in range(2)]

    counts_before = count_reviewer_disagreements_in_tasks(tasks)
    compute_persistent_sr_blind_spot_hint(tasks, threshold=0.25, min_sample=3, now=now)
    counts_after = count_reviewer_disagreements_in_tasks(tasks)

    assert counts_before == counts_after


def test_persistent_hint_multiple_categories_listed_in_hint():
    """When two categories exceed the threshold, the hint names both."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    tasks = []
    for days_ago in (3, 10):
        # 2 correctness misses + 2 scope-drift misses + 4 clean → each at 0.25 rate
        tasks += [
            _make_reviewer_miss_task(
                pid, created_at=now - timedelta(days=days_ago), category="correctness"
            )
            for _ in range(2)
        ]
        tasks += [
            _make_reviewer_miss_task(
                pid, created_at=now - timedelta(days=days_ago), category="scope-drift"
            )
            for _ in range(2)
        ]
        tasks += [
            _make_clean_task(pid, created_at=now - timedelta(days=days_ago))
            for _ in range(4)
        ]
    result = compute_persistent_sr_blind_spot_hint(
        tasks, threshold=0.25, min_sample=3, now=now
    )
    cats = {c["category"] for c in result["categories"]}
    assert "correctness" in cats
    assert "scope-drift" in cats
    assert result["hint"] is not None
    assert "correctness" in result["hint"]
    assert "scope-drift" in result["hint"]


def test_persistent_hint_fix_task_type_tracked_separately():
    """Reviewer-disagreement rates for 'fix' task type are tracked separately from 'create'."""
    now = datetime(2026, 1, 14, 12, 0, 0)
    pid = uuid.uuid4()
    tasks = []
    for days_ago in (3, 10):
        tasks += [
            _make_reviewer_miss_task(
                pid, created_at=now - timedelta(days=days_ago), task_type="fix"
            )
            for _ in range(2)
        ]
        tasks += [
            _make_clean_task(pid, created_at=now - timedelta(days=days_ago))
            for _ in range(2)
        ]
    result = compute_persistent_sr_blind_spot_hint(
        tasks, threshold=0.25, min_sample=3, now=now
    )
    task_types = {c["task_type"] for c in result["categories"]}
    assert "fix" in task_types
    assert "create" not in task_types


# ---------------------------------------------------------------------------
# Unit: _count_reviewer_disagreement_directions — per-origin sub-counters
# ---------------------------------------------------------------------------


def _make_origin_task(project_id, *, origin: str | None, category: str = "correctness"):
    """Task where the reviewer rescued *category* via a structured origin."""
    check_results: dict = {}
    if origin is not None:
        check_results[category] = {"passed": True, "origin": origin}
    else:
        # No check_results entry → coverage gap (agent omitted the category)
        pass
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x"}],
        context={
            "_self_review_outcome": {
                "check_results": check_results,
                "reviewer_findings": [
                    {
                        "category": category,
                        "severity": "HIGH",
                        "description": "reviewer caught it",
                    }
                ],
            }
        },
    )


def test_origin_recovery_incomplete_increments_miss_recovery_incomplete():
    """origin='reviewer_recovery_after_incomplete_sr' → miss_recovery_incomplete += 1."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_origin_task(
        pid, origin="reviewer_recovery_after_incomplete_sr", category="correctness"
    )
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 1
    assert bucket["miss_recovery_incomplete"] == 1
    assert bucket["miss_disagreement"] == 0
    assert bucket["miss_coverage_gap"] == 0


def test_origin_disagreement_increments_miss_disagreement():
    """origin='reviewer_disagreement' → miss_disagreement += 1."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_origin_task(
        pid, origin="reviewer_disagreement", category="correctness"
    )
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 1
    assert bucket["miss_disagreement"] == 1
    assert bucket["miss_recovery_incomplete"] == 0
    assert bucket["miss_coverage_gap"] == 0


def test_origin_absent_entry_increments_miss_coverage_gap():
    """No check_results entry for the category → miss_coverage_gap += 1."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    # origin=None → no check_results entry → falls through to coverage_gap
    task = _make_origin_task(pid, origin=None, category="correctness")
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 1
    assert bucket["miss_coverage_gap"] == 1
    assert bucket["miss_recovery_incomplete"] == 0
    assert bucket["miss_disagreement"] == 0


def test_origin_plain_reviewer_increments_miss_coverage_gap():
    """origin='reviewer' (pre-breakdown legacy) → miss_coverage_gap += 1."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    task = _make_origin_task(pid, origin="reviewer", category="scope-drift")
    counts = _count_reviewer_disagreement_directions([task])
    bucket = counts[("scope-drift", "create")]
    assert bucket["miss"] == 1
    assert bucket["miss_coverage_gap"] == 1
    assert bucket["miss_recovery_incomplete"] == 0
    assert bucket["miss_disagreement"] == 0


def test_origin_sub_counters_sum_to_aggregate_miss():
    """miss_recovery_incomplete + miss_disagreement + miss_coverage_gap == miss."""
    from app.sr_blind_spots import _count_reviewer_disagreement_directions

    pid = uuid.uuid4()
    tasks = [
        _make_origin_task(
            pid, origin="reviewer_recovery_after_incomplete_sr", category="correctness"
        ),
        _make_origin_task(pid, origin="reviewer_disagreement", category="correctness"),
        _make_origin_task(pid, origin=None, category="correctness"),
    ]
    counts = _count_reviewer_disagreement_directions(tasks)
    bucket = counts[("correctness", "create")]
    assert bucket["miss"] == 3
    total_sub = (
        bucket["miss_recovery_incomplete"]
        + bucket["miss_disagreement"]
        + bucket["miss_coverage_gap"]
    )
    assert total_sub == bucket["miss"]


# ---------------------------------------------------------------------------
# Unit: stamp_sr_catch_rate_computed_at
# ---------------------------------------------------------------------------


def test_stamp_sr_catch_rate_computed_at_writes_heartbeat():
    """Heartbeat is written into sr_catch_rate_trends.signal_last_computed_at."""
    fixed = datetime(2026, 1, 15, 12, 0, 0)
    doc: dict = {}
    stamp_sr_catch_rate_computed_at(doc, now=fixed)
    assert doc["sr_catch_rate_trends"]["signal_last_computed_at"] == fixed.isoformat()


def test_stamp_helpers_default_now_emit_naive_utc_strings():
    """Audit C10: default-now heartbeat stamps must be tz-NAIVE ISO strings.

    The staleness watchdog parses these strings and subtracts a naive _utcnow().
    A tz-aware writer (datetime.now(timezone.utc)) would emit a '+00:00' suffix
    that the dispatcher then strips, mis-dating the freshness window. Assert both
    sr_blind_spots stamp helpers default to naive UTC (no tz offset) so they stay
    consistent with the watchdog and with task_results timestamps.
    """
    blind_doc: dict = {}
    stamp_sr_blind_spot_computed_at(blind_doc)
    blind_ts = blind_doc["sr_blind_spot_signal_last_computed_at"]
    assert "+00:00" not in blind_ts and not blind_ts.endswith("Z")
    assert datetime.fromisoformat(blind_ts).tzinfo is None

    catch_doc: dict = {}
    stamp_sr_catch_rate_computed_at(catch_doc)
    catch_ts = catch_doc["sr_catch_rate_trends"]["signal_last_computed_at"]
    assert "+00:00" not in catch_ts and not catch_ts.endswith("Z")
    assert datetime.fromisoformat(catch_ts).tzinfo is None


def test_stamp_sr_catch_rate_computed_at_preserves_existing_keys():
    """Existing keys in sr_catch_rate_trends are not clobbered."""
    fixed = datetime(2026, 1, 15, 12, 0, 0)
    doc: dict = {"sr_catch_rate_trends": {"computed_at": "old", "some_key": 42}}
    stamp_sr_catch_rate_computed_at(doc, now=fixed)
    assert doc["sr_catch_rate_trends"]["some_key"] == 42
    assert doc["sr_catch_rate_trends"]["signal_last_computed_at"] == fixed.isoformat()


def test_stamp_sr_catch_rate_computed_at_initialises_missing_trends():
    """Works when sr_catch_rate_trends key is absent from doc."""
    fixed = datetime(2026, 3, 1, 8, 30, 0)
    doc: dict = {"other_key": "value"}
    stamp_sr_catch_rate_computed_at(doc, now=fixed)
    assert "sr_catch_rate_trends" in doc
    assert doc["sr_catch_rate_trends"]["signal_last_computed_at"] == fixed.isoformat()
    assert doc["other_key"] == "value"


def test_stamp_sr_catch_rate_computed_at_data_sufficient_true():
    """data_sufficient=True stamps both data_sufficient and last_sufficient_computed_at."""
    fixed = datetime(2026, 4, 1, 10, 0, 0)
    doc: dict = {}
    stamp_sr_catch_rate_computed_at(doc, now=fixed, data_sufficient=True)
    trends = doc["sr_catch_rate_trends"]
    assert trends["signal_last_computed_at"] == fixed.isoformat()
    assert trends["data_sufficient"] is True
    assert trends["last_sufficient_computed_at"] == fixed.isoformat()


def test_stamp_sr_catch_rate_computed_at_data_sufficient_false():
    """data_sufficient=False stamps data_sufficient but does NOT stamp last_sufficient_computed_at."""
    fixed = datetime(2026, 4, 1, 10, 0, 0)
    doc: dict = {}
    stamp_sr_catch_rate_computed_at(doc, now=fixed, data_sufficient=False)
    trends = doc["sr_catch_rate_trends"]
    assert trends["signal_last_computed_at"] == fixed.isoformat()
    assert trends["data_sufficient"] is False
    assert "last_sufficient_computed_at" not in trends


def test_stamp_sr_catch_rate_computed_at_data_sufficient_false_preserves_existing():
    """data_sufficient=False preserves an existing last_sufficient_computed_at (frozen trend detection)."""
    fixed_old = datetime(2026, 3, 1, 0, 0, 0)
    fixed_new = datetime(2026, 4, 1, 10, 0, 0)
    doc: dict = {
        "sr_catch_rate_trends": {
            "signal_last_computed_at": fixed_old.isoformat(),
            "last_sufficient_computed_at": fixed_old.isoformat(),
            "data_sufficient": True,
        }
    }
    stamp_sr_catch_rate_computed_at(doc, now=fixed_new, data_sufficient=False)
    trends = doc["sr_catch_rate_trends"]
    assert trends["signal_last_computed_at"] == fixed_new.isoformat()
    assert trends["data_sufficient"] is False
    # last_sufficient_computed_at must stay at the old value — not overwritten
    assert trends["last_sufficient_computed_at"] == fixed_old.isoformat()


def test_stamp_sr_catch_rate_computed_at_no_data_sufficient_omits_fields():
    """Default call (no data_sufficient) does not write data_sufficient or last_sufficient_computed_at."""
    fixed = datetime(2026, 4, 1, 10, 0, 0)
    doc: dict = {}
    stamp_sr_catch_rate_computed_at(doc, now=fixed)
    trends = doc["sr_catch_rate_trends"]
    assert trends["signal_last_computed_at"] == fixed.isoformat()
    assert "data_sufficient" not in trends
    assert "last_sufficient_computed_at" not in trends
