"""Tests for backend/app/ci_resolution.py — the strict orchestrator-side
CI verdict resolver that replaces the lenient ``get_check_runs`` default on
the PR-merge hot path.

The lenient default treats a commit with zero check-runs as a passing build
so the orchestrator's CI-wait does not hang on no-CI repos.  That is wrong
for PR-driven CI configs where the ``pull_request`` event runs on GitHub's
synthetic merge-commit SHA rather than the branch HEAD: the branch HEAD then
carries zero check-runs while the real verdict lives on the PR head commit
reachable via ``commits/{sha}/pulls``.  The strict resolver inspects the
branch HEAD first, falls back to the PR head, and refuses to mark CI green
purely from the absence of a signal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import ci_resolution
from app.models import Project


def _proj(build_config=None):
    return Project(name="t", user_id="u", build_config=build_config)


# proceed_on_no_checks / requires_ci_checks — greenfield no-CI repo merge policy


def test_requires_ci_checks_defaults_true():
    assert ci_resolution.requires_ci_checks(_proj()) is True
    assert ci_resolution.requires_ci_checks(_proj({"other": 1})) is True


def test_requires_ci_checks_false_when_declared():
    assert ci_resolution.requires_ci_checks(_proj({"requires_ci_checks": False})) is False


def test_proceed_on_no_checks_ci_repo_non_staging_keeps_polling():
    # CI-gated repo: a definitive no_checks on a trunk base must NOT proceed —
    # for a CI repo a missing check means "not registered yet", so keep polling.
    assert ci_resolution.proceed_on_no_checks(_proj(), "main") is False


def test_proceed_on_no_checks_staging_base_proceeds():
    # Staging-branch rollups have CI path-filtered out by design → proceed.
    assert ci_resolution.proceed_on_no_checks(_proj(), "research/cycle-1") is True
    assert ci_resolution.proceed_on_no_checks(_proj(), "plan/cycle-1") is True


def test_proceed_on_no_checks_no_ci_project_proceeds_any_base():
    # The greenfield-repo fix: no-CI project proceeds on a definitive no_checks
    # for ANY base (e.g. a fresh repo with no GitHub Actions).
    p = _proj({"requires_ci_checks": False})
    assert ci_resolution.proceed_on_no_checks(p, "main") is True
    assert ci_resolution.proceed_on_no_checks(p, None) is True


def test_proceed_on_no_checks_none_project():
    assert ci_resolution.proceed_on_no_checks(None, "main") is False


@pytest.mark.asyncio
async def test_branch_head_has_checks_returns_branch_head_source(mocker):
    """Normal case: branch HEAD carries a completed green build → resolved on the
    branch HEAD itself, no PR-merge-SHA fallback needed."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [{"name": "ci"}],
            }
        ),
    )
    list_prs = mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_abc",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["source"] == "branch_head"
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == "branch_head_abc"
    assert result["conclusion"] == "success"
    # No PR-lookup fallback when the branch HEAD itself resolves to a
    # completed verdict — the resolver short-circuits at step 1.
    list_prs.assert_not_called()


@pytest.mark.asyncio
async def test_branch_head_bare_pr_event_merge_sha_resolves_green(mocker):
    """PR-driven CI shape: branch HEAD has 0 check-runs, but the PR-event merge
    SHA (returned via ``commits/{sha}/pulls``) carries a green build →
    ci_passed=True, source='merge_sha'."""
    pr_head_sha = "pr_event_merge_sha_001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {
            "status": "completed",
            "conclusion": "success",
            "details": [{"name": "ci"}],
        }

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["source"] == "merge_sha"
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == pr_head_sha


@pytest.mark.asyncio
async def test_branch_head_bare_no_pr_event_resolves_no_checks_not_green(mocker):
    """Branch HEAD has 0 check-runs AND no PR-event merge SHA exists →
    ci_passed=False.  This is the bug the strict mode fixes: the lenient
    default would mark this green and the orchestrator would attempt to merge
    against the absence of a CI signal.

    The state is ``no_checks`` (a *definitive* absence: we queried HEAD and
    found no PR-event SHA to fall back to), NOT ``pending`` — so the merge gate
    can distinguish "CI will never run here" (e.g. an internal staging-branch
    PR that CI path-filters out) from a transient resolution failure. It still
    refuses to pass: ci_passed is False on CI-gated bases."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "no_checks", "conclusion": None, "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    assert result["state"] == "no_checks"


@pytest.mark.asyncio
async def test_all_pr_heads_no_checks_returns_no_checks(mocker):
    """Branch HEAD bare and every reachable PR-event head also carries zero
    check-runs → ``no_checks`` (definitive). Distinguishes this from a head
    with in-progress checks (which must stay ``pending``)."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        # Every SHA — branch HEAD and the PR head — has no checks.
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_no_checks"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "no_checks"


@pytest.mark.asyncio
async def test_pr_head_in_progress_returns_pending_not_no_checks(mocker):
    """Branch HEAD bare but a PR-event head has checks IN PROGRESS → ``pending``
    (a real, not-yet-final signal). Must NOT be reported as ``no_checks`` —
    that would let the staging-branch merge bypass fire while CI is still
    running."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "in_progress", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_running"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_pr_head_fetch_error_returns_pending_not_no_checks(mocker):
    """Branch HEAD bare and the only PR-event head's check-runs fetch raises →
    ``pending`` (transient). A fetch error must not be mistaken for a confirmed
    absence of checks, or the staging-branch bypass could merge on a network
    blip."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        raise RuntimeError("network down")

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_err"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_branch_head_has_checks_red_returns_red(mocker):
    """Branch HEAD has a completed failing build → ci_passed=False, state=red."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "ci", "conclusion": "failure"}],
            }
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_red",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "branch_head"
    assert result["state"] == "red"


@pytest.mark.asyncio
async def test_branch_head_in_progress_returns_pending(mocker):
    """Branch HEAD has in-progress checks → ci_passed=False, state=pending.
    Stays on branch_head source — we have a real signal, just not final yet."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "in_progress",
                "conclusion": None,
                "details": [],
            }
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_pending",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "branch_head"
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_branch_head_fetch_failure_returns_unresolved(mocker):
    """Any exception from get_check_runs_cached at step 1 → unresolved (never
    raises into the orchestrator's cycle loop)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=RuntimeError("network down")),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_fail",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_empty_branch_sha_returns_unresolved(mocker):
    """Empty branch_sha short-circuits to unresolved without any API call."""
    cr = mocker.patch(
        "app.github.get_check_runs_cached", new=AsyncMock(return_value=None)
    )
    list_prs = mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    cr.assert_not_called()
    list_prs.assert_not_called()


@pytest.mark.asyncio
async def test_pr_head_red_beats_green_across_multiple_heads(mocker):
    """Branch HEAD bare; multiple associated PR heads with mixed verdicts —
    a red on any PR head must win over a green on another head regardless of
    the order GitHub returns them."""
    green_sha = "pr_head_green01"
    red_sha = "pr_head_red0001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_multi":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == green_sha:
            return {
                "status": "completed",
                "conclusion": "success",
                "details": [],
            }
        if sha == red_sha:
            return {
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            }
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    # Green head listed FIRST — the resolver must not short-circuit on it.
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 10,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": green_sha},
                },
                {
                    "number": 11,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": red_sha},
                },
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=None,
        branch_sha="bare_multi",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "red"
    assert result["source"] == "merge_sha"
    assert result["head_sha_resolved"] == red_sha


@pytest.mark.asyncio
async def test_explicit_pr_number_filters_associated_prs(mocker):
    """When pr_number is supplied, the resolver prefers that PR's head over
    other associated PRs — supports the orchestrator passing the parking
    task's PR number so we resolve through the PR actually being merged."""
    target_sha = "target_pr_head_aa"
    other_sha = "other_pr_head_bb"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_explicit":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == target_sha:
            return {
                "status": "completed",
                "conclusion": "success",
                "details": [],
            }
        if sha == other_sha:
            return {
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            }
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                # Unrelated PR red — would normally win the red-preferring
                # sweep, but the explicit pr_number=99 filter excludes it.
                {
                    "number": 100,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": other_sha},
                },
                {
                    "number": 99,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": target_sha},
                },
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=99,
        branch_sha="bare_explicit",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == target_sha
