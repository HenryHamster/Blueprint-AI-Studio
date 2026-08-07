"""Tests for ``app.github_checks`` CI-log helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# include_failed_logs=False — skip the RED-sha failure fan-out
#
# On a RED sha get_check_runs fetches workflow runs + per-run jobs + per-failed
# -check job LOGS + annotations: 3-10+ extra calls against the SHARED 5000/hr
# install-token bucket, every poll. Callers that DISCARD failed_logs (the agent
# check-runs proxy) must not pay that.
# ---------------------------------------------------------------------------


def _red_check_runs_response():
    return _make_response(
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


@pytest.mark.asyncio
async def test_include_failed_logs_false_skips_failure_fanout_on_red_sha():
    """A RED sha with include_failed_logs=False costs exactly ONE GitHub call —
    the check-runs list — and no workflow-runs / jobs / logs / annotations."""
    request_mock = AsyncMock(return_value=_red_check_runs_response())

    with patch.object(github_checks, "_github_request", new=request_mock):
        result = await github_checks.get_check_runs(
            token="tok",
            owner="o",
            repo="r",
            ref="sha123abc",
            include_failed_logs=False,
        )

    assert request_mock.await_count == 1, (
        "the failure fan-out (workflow runs + jobs + logs + annotations) ran even "
        "though the caller discards failed_logs"
    )
    # Verdict is unchanged — only the (discarded) detail is skipped.
    assert result["status"] == "completed"
    assert result["conclusion"] == "failure"
    assert result["details"] == [
        {"name": "ci-build", "status": "completed", "conclusion": "failure"}
    ]
    assert result["failed_logs"] == ""
    assert result["failed_run_ids"] == []


@pytest.mark.asyncio
async def test_include_failed_logs_default_true_still_fans_out():
    """Existing callers (the fix loop) keep their behaviour: default True still
    fetches the failure detail."""
    call_iter = iter(
        [
            _red_check_runs_response(),
            _make_response(200, json_data={"workflow_runs": []}),  # workflow runs
            _make_response(200, text="FAILED testing/test_x.py::test_y"),  # job logs
        ]
    )
    request_mock = AsyncMock(side_effect=lambda *a, **kw: next(call_iter))

    with patch.object(github_checks, "_github_request", new=request_mock):
        result = await github_checks.get_check_runs(
            token="tok", owner="o", repo="r", ref="sha123abc"
        )

    assert request_mock.await_count > 1
    assert "testing/test_x.py::test_y" in result["failed_logs"]


@pytest.mark.asyncio
async def test_check_runs_cache_does_not_serve_logsfree_result_to_a_logs_caller():
    """include_failed_logs is part of the CACHE KEY.

    Otherwise the cheap logs-free entry written by the agent proxy would be
    served — within the 30s TTL — to auto_remediation / ci_resolution, which
    would then see failed_logs="" and drive the fix loop blind.
    """
    github_checks._check_runs_cache.clear()
    inner = AsyncMock(
        side_effect=lambda *a, **kw: {
            "status": "completed",
            "conclusion": "failure",
            "details": [],
            "failed_logs": "" if kw.get("include_failed_logs") is False else "REAL LOGS",
        }
    )

    with patch("app.github.get_check_runs", new=inner):
        logs_free = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha1", include_failed_logs=False
        )
        with_logs = await github_checks.get_check_runs_cached("tok", "o", "r", "sha1")
        # Second call on each variant is served from its own cache entry.
        logs_free_again = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha1", include_failed_logs=False
        )

    assert logs_free["failed_logs"] == ""
    assert with_logs["failed_logs"] == "REAL LOGS", (
        "a logs-free cache entry was served to a caller that needs failed_logs"
    )
    assert logs_free_again["failed_logs"] == ""
    # Two distinct variants fetched once each; the repeat hit the cache.
    assert inner.await_count == 2

    github_checks._check_runs_cache.clear()


@pytest.mark.asyncio
async def test_invalidate_check_runs_cache_drops_every_variant():
    """Invalidation must clear ALL variants for the sha — a key dimension added
    later must not leave a stale entry behind an invalidation."""
    github_checks._check_runs_cache.clear()
    inner = AsyncMock(
        return_value={
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
        }
    )

    with patch("app.github.get_check_runs", new=inner):
        await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha1", include_failed_logs=False
        )
        await github_checks.get_check_runs_cached("tok", "o", "r", "sha1")
        assert inner.await_count == 2
        assert len(github_checks._check_runs_cache) == 2

        github_checks.invalidate_check_runs_cache("o", "r", "sha1")
        assert github_checks._check_runs_cache == {}

        # Both variants re-fetch after invalidation.
        await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha1", include_failed_logs=False
        )
        await github_checks.get_check_runs_cached("tok", "o", "r", "sha1")
        assert inner.await_count == 4

    github_checks._check_runs_cache.clear()


# ---------------------------------------------------------------------------
# get_latest_inflight_workflow_run_for_branch — branch-level dispatch coalescing
#
# ci.yml is keyed to a branch-level cancel-in-progress concurrency group, so a
# fresh dispatch for a NEW integration HEAD cancels whatever full-suite run is
# already in flight.  The promotion CI gate uses this helper as its coalescing
# seam: before dispatching, it asks whether a run is already queued/in_progress
# on the branch and waits for that run to conclude instead of cancelling it.
# ---------------------------------------------------------------------------


def _run(
    run_id: int,
    *,
    status: str,
    conclusion=None,
    head_sha: str = "abc123",
    created_at: str | None = None,
):
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "html_url": f"https://gh/run/{run_id}",
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_inflight_run_picks_newest_queued_or_in_progress():
    """Only queued/in-progress runs qualify; the newest (by created_at) wins.
    Completed/cancelled runs are skipped so a stale run cannot be mistaken for
    an in-flight full-suite signal."""
    now = datetime.now(timezone.utc)
    recent = now.isoformat().replace("+00:00", "Z")
    older = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    response = _make_response(
        200,
        json_data={
            "workflow_runs": [
                _run(3, status="completed", conclusion="success", created_at=recent),
                _run(2, status="in_progress", created_at=older),
                _run(1, status="queued", created_at=recent),
            ]
        },
    )
    with patch.object(github_checks, "_github_request", new=AsyncMock(return_value=response)):
        result = await github_checks.get_latest_inflight_workflow_run_for_branch(
            "tok", "o", "r", "automain"
        )
    assert result is not None
    assert result["run_id"] == 1  # newest qualifying run (id 3 is completed)
    assert result["status"] == "queued"
    assert result["head_sha"] == "abc123"
    assert result["created_at"] == recent


@pytest.mark.asyncio
async def test_inflight_run_ignores_aged_runs():
    """A run created before ``max_age_seconds`` (a stuck/zombie run) must NOT
    block a fresh dispatch — the gate would otherwise coalesce onto a dead run
    forever and never dispatch."""
    aged = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace(
        "+00:00", "Z"
    )
    response = _make_response(
        200,
        json_data={"workflow_runs": [_run(7, status="in_progress", created_at=aged)]},
    )
    with patch.object(github_checks, "_github_request", new=AsyncMock(return_value=response)):
        result = await github_checks.get_latest_inflight_workflow_run_for_branch(
            "tok", "o", "r", "automain", max_age_seconds=60 * 60
        )
    assert result is None


@pytest.mark.asyncio
async def test_inflight_run_none_on_api_error():
    """A non-200 (e.g. 403/500) response degrades to None — the gate falls back
    to dispatching rather than blocking on a failed coalescing lookup."""
    with patch.object(
        github_checks, "_github_request", new=AsyncMock(return_value=_make_response(500))
    ):
        result = await github_checks.get_latest_inflight_workflow_run_for_branch(
            "tok", "o", "r", "automain"
        )
    assert result is None


@pytest.mark.asyncio
async def test_inflight_run_none_when_no_qualifying_run():
    """An empty run list or all-completed runs → None (nothing in flight to
    coalesce onto)."""
    for payload in ({"workflow_runs": []}, {"workflow_runs": [_run(9, status="completed", conclusion="failure")]}):
        with patch.object(
            github_checks,
            "_github_request",
            new=AsyncMock(return_value=_make_response(200, json_data=payload)),
        ):
            result = await github_checks.get_latest_inflight_workflow_run_for_branch(
                "tok", "o", "r", "automain"
            )
        assert result is None
