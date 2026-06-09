"""Tests for ``app.github_checks`` CI-log helpers."""

from app.github_checks import _extract_pytest_selectors


class TestExtractPytestSelectors:
    """The ci_failure fix loop's ./check re-runs exactly the selectors that
    ``get_check_runs`` surfaces in ``failed_logs``. These must be recovered from
    the raw job log even when captured WARNING/ERROR output buries the pytest
    summary — otherwise the agent has nothing to verify and ships blind."""

    def test_selectors_survive_trailing_log_noise(self):
        # Real failing selectors appear in the short-summary block, THEN a
        # failure-path test floods the tail with captured FAILED/ERROR log
        # lines. error_lines[-30:] would be all noise; the selectors must still
        # be extracted because we scan the whole log.
        raw = "\n".join(
            [
                "=========================== short test summary info ===========================",
                "FAILED testing/test_pipeline_integration.py::test_review_approve_does_not_spawn_fix - AssertionError",
                "FAILED testing/test_review_pipeline.py::test_merge_success_caller_runs_one_shots_when_uncontended - x",
            ]
            + [
                f"2026-06-09T09:08:{i:02d}Z WARNING app.strategic_completion: task {i} FAILED (failure #1)"
                for i in range(40)
            ]
            + ["2026-06-09T09:09:00Z ERROR app.release_reconciliation: promotion deploy FAILED for v1"]
        )
        assert _extract_pytest_selectors(raw) == [
            "testing/test_pipeline_integration.py::test_review_approve_does_not_spawn_fix",
            "testing/test_review_pipeline.py::test_merge_success_caller_runs_one_shots_when_uncontended",
        ]

    def test_log_noise_without_selectors_yields_nothing(self):
        # Lines that say FAILED/error but carry no ``path::test`` selector
        # (captured app logs) must not be mistaken for failing tests.
        raw = (
            "[STRATEGIC] strategic_review task abc FAILED (failure #1)\n"
            "ERROR app.release_reconciliation: promotion deploy FAILED for v1\n"
            "some error happened here\n"
            "[CI] PR #4242 reopen attempts (10) hit cap — marking task FAILED"
        )
        assert _extract_pytest_selectors(raw) == []

    def test_dedupes_and_caps(self):
        raw = "\n".join(
            ["FAILED a.py::t"] * 5 + [f"FAILED b{i}.py::t" for i in range(100)]
        )
        sels = _extract_pytest_selectors(raw, cap=10)
        assert sels[0] == "a.py::t"
        assert len(sels) == 10
        assert len(set(sels)) == 10  # no duplicates

    def test_empty_log_is_safe(self):
        assert _extract_pytest_selectors("") == []
        assert _extract_pytest_selectors(None) == []
