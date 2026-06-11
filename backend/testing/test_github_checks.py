"""Tests for ``app.github_checks`` CI-log helpers."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import github_checks
from app.github_checks import _extract_pytest_selectors


def _make_response(status_code: int, json_data=None, text="", links=None):
    """Minimal httpx-like response mock."""
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data if json_data is not None else {}
    m.text = text
    m.links = links or {}
    return m


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


# ---------------------------------------------------------------------------
# Job-logs fetch: non-200 responses must emit a WARNING (Fix 2 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_logs_non200_emits_warning(caplog):
    """Regression: a non-200 (e.g. 403) response to the job-logs fetch must emit
    a WARNING including the HTTP status code so the failure mode is visible.

    Before the fix, any non-200 silently skipped (no log), making it impossible
    to tell from logs why 'Failing tests:' selectors never appeared in Blueprint
    CI comments (likely 403: GitHub App installation token lacks Actions read
    permission).
    """
    # check-runs list: one failed check-run with id=99
    check_runs_resp = _make_response(
        200,
        json_data={
            "check_runs": [
                {
                    "name": "ci-build",
                    "status": "completed",
                    "conclusion": "failure",
                    "id": 99,
                    "output": {},
                }
            ]
        },
    )
    # workflow-runs pre-fetch: 200 but empty (no job IDs to map)
    workflow_runs_resp = _make_response(200, json_data={"workflow_runs": []})
    # job-logs fetch: 403 (the common production failure mode)
    job_logs_403 = _make_response(403, text="Forbidden")
    # annotations fallback: 200 empty
    annotations_resp = _make_response(200, json_data=[])

    call_iter = iter([check_runs_resp, workflow_runs_resp, job_logs_403, annotations_resp])

    with patch.object(
        github_checks, "_github_request", new=AsyncMock(side_effect=lambda *a, **kw: next(call_iter))
    ):
        with caplog.at_level(logging.WARNING, logger="app.github_checks"):
            result = await github_checks.get_check_runs(
                token="tok", owner="o", repo="r", ref="sha123abc"
            )

    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "403" in msg and "job logs" in msg.lower() for msg in warning_messages
    ), f"Expected WARNING with '403' and 'job logs' in messages, got: {warning_messages}"
    # The result still has a non-empty failed_logs (falls back to check name)
    assert result["conclusion"] == "failure"
