"""Tests for the pr_is_not_behind hit counter and admin diagnostics endpoint."""

import pytest

from app import shared_state
from app.github import pr_is_not_behind
from app.shared_state import (
    PR_IS_NOT_BEHIND_CALL_SITES,
    pr_is_not_behind_counts,
    record_pr_is_not_behind,
    snapshot_pr_is_not_behind_counts,
)


@pytest.fixture(autouse=True)
def _reset_counter():
    """Clear the in-process counter before and after each test."""
    pr_is_not_behind_counts.clear()
    yield
    pr_is_not_behind_counts.clear()


def test_record_pr_is_not_behind_increments_buckets():
    record_pr_is_not_behind("ci_completion", True)
    record_pr_is_not_behind("ci_completion", True)
    record_pr_is_not_behind("ci_completion", False)
    record_pr_is_not_behind("ci_completion", None)

    bucket = pr_is_not_behind_counts["ci_completion"]
    assert bucket == {"true": 2, "false": 1, "none": 1, "total": 4}


def test_record_pr_is_not_behind_ignores_empty_call_site():
    record_pr_is_not_behind("", True)
    record_pr_is_not_behind(None, True)  # type: ignore[arg-type]
    assert pr_is_not_behind_counts == {}


def test_snapshot_includes_canonical_zero_buckets_before_any_hits():
    snapshot = snapshot_pr_is_not_behind_counts()
    for site in PR_IS_NOT_BEHIND_CALL_SITES:
        assert snapshot[site] == {"true": 0, "false": 0, "none": 0, "total": 0}


def test_snapshot_is_deep_copy():
    record_pr_is_not_behind("ci_completion", True)
    snapshot = snapshot_pr_is_not_behind_counts()
    snapshot["ci_completion"]["true"] = 999
    # Mutating the snapshot must not affect live state.
    assert pr_is_not_behind_counts["ci_completion"]["true"] == 1


@pytest.mark.asyncio
async def test_pr_is_not_behind_records_true_on_clean_state():
    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        mergeable_state="clean",
        call_site="ci_completion",
    )
    assert result is True
    assert pr_is_not_behind_counts["ci_completion"] == {
        "true": 1,
        "false": 0,
        "none": 0,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_pr_is_not_behind_records_false_on_behind_state():
    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        mergeable_state="behind",
        call_site="rollup_pre_merge",
    )
    assert result is False
    assert pr_is_not_behind_counts["rollup_pre_merge"] == {
        "true": 0,
        "false": 1,
        "none": 0,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_pr_is_not_behind_records_none_when_no_signals():
    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        call_site="rollup_post_merge",
    )
    assert result is None
    assert pr_is_not_behind_counts["rollup_post_merge"] == {
        "true": 0,
        "false": 0,
        "none": 1,
        "total": 1,
    }


@pytest.mark.asyncio
async def test_pr_is_not_behind_no_call_site_does_not_record():
    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        mergeable_state="clean",
    )
    assert result is True
    assert pr_is_not_behind_counts == {}


@pytest.mark.asyncio
async def test_pr_is_not_behind_records_for_each_canonical_call_site():
    # Each canonical call site contributes one True (mergeable_state=clean) and
    # one False (mergeable_state=behind), and one None (no signals).
    for site in PR_IS_NOT_BEHIND_CALL_SITES:
        await pr_is_not_behind(
            "tok",
            "owner",
            "repo",
            mergeable_state="clean",
            call_site=site,
        )
        await pr_is_not_behind(
            "tok",
            "owner",
            "repo",
            mergeable_state="behind",
            call_site=site,
        )
        await pr_is_not_behind(
            "tok",
            "owner",
            "repo",
            call_site=site,
        )

    snapshot = snapshot_pr_is_not_behind_counts()
    for site in PR_IS_NOT_BEHIND_CALL_SITES:
        assert snapshot[site] == {
            "true": 1,
            "false": 1,
            "none": 1,
            "total": 3,
        }, f"unexpected bucket for {site}: {snapshot[site]}"
        assert (
            snapshot[site]["total"]
            == snapshot[site]["true"] + snapshot[site]["false"] + snapshot[site]["none"]
        )


@pytest.mark.asyncio
async def test_admin_endpoint_returns_snapshot_with_canonical_keys():
    """The /api/admin/pr-behind-detector-stats endpoint exposes the snapshot."""
    from app.routers.admin_health import get_pr_behind_detector_stats

    record_pr_is_not_behind("ci_completion", True)
    record_pr_is_not_behind("orchestrator_ci", False)
    record_pr_is_not_behind("orchestrator_ci", None)

    payload = await get_pr_behind_detector_stats(current_user={"role": "admin"})

    assert "pr_is_not_behind" in payload
    stats = payload["pr_is_not_behind"]

    # All four canonical keys present even when never hit.
    for site in PR_IS_NOT_BEHIND_CALL_SITES:
        assert site in stats

    assert stats["ci_completion"] == {"true": 1, "false": 0, "none": 0, "total": 1}
    assert stats["orchestrator_ci"] == {"true": 0, "false": 1, "none": 1, "total": 2}
    assert stats["rollup_pre_merge"] == {"true": 0, "false": 0, "none": 0, "total": 0}
    assert stats["rollup_post_merge"] == {"true": 0, "false": 0, "none": 0, "total": 0}


def test_shared_state_module_exports_helpers():
    # Sanity: the helpers live on shared_state and are importable.
    assert callable(shared_state.record_pr_is_not_behind)
    assert callable(shared_state.snapshot_pr_is_not_behind_counts)
    assert isinstance(shared_state.PR_IS_NOT_BEHIND_CALL_SITES, tuple)
    assert "ci_completion" in shared_state.PR_IS_NOT_BEHIND_CALL_SITES
    assert "rollup_pre_merge" in shared_state.PR_IS_NOT_BEHIND_CALL_SITES
    assert "rollup_post_merge" in shared_state.PR_IS_NOT_BEHIND_CALL_SITES
    assert "orchestrator_ci" in shared_state.PR_IS_NOT_BEHIND_CALL_SITES
