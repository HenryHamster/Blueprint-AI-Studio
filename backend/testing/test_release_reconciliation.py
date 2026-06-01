"""Tests for the out-of-band release-reconciliation helpers.

``backend/app/release_reconciliation.py`` owns the reconciliation paths that
catch a release version up when its promotion PR lands (or strands) outside the
normal qualification flow.  These tests verify the helpers for a SIMULATED
non-self project — both with a custom integration/promotion pair and with the
single-branch case where the integration branch IS the promotion target.

The single-branch guard mirrors ``release_promotion.promote()`` and the
back-merge guard (``hotfix_backmerge.reconcile_main_into_automain``, which skips
as ``lts_inactive``): when there is no separate release trunk, the reconciler
must be provably inert — it must not run GitHub I/O and must not record a
spurious promotion against a branch fleet work already lands on.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app import release_version as rv
from app.models import Project
from app.release_reconciliation import (
    _promotion_pending_timeout_seconds,
    _reconcile_if_pending_auto_merge_landed,
    _reconcile_if_promotion_failed_landed,
    _reconcile_if_promotion_pending_timed_out,
)
from app.time_utils import _utcnow


def _shared_session_factory(db_session):
    """Hand the reconciler the test's shared session (no-op __aexit__).

    The reconciler opens its own session via the injected factory; under the
    single-connection SQLite test harness the "dedicated" session must BE the
    test session so assertions can run afterwards.
    """

    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    return _factory


async def _make_project_and_version(
    db_session,
    *,
    integration_branch: str,
    promotion_target: str,
):
    """Create a project with the given branch pair plus an open v1."""
    project = Project(
        name="Reconcile-Test",
        user_id="reconcile-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


# ===========================================================================
# Single-branch guard — integration branch IS the promotion target.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_promotion_failed_skips_on_single_branch(db_session, mocker):
    """On a single-branch repo the promotion-failed reconciler is inert: it
    performs NO GitHub I/O and leaves the version untouched."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="main", promotion_target="main"
    )
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": "abcd" * 10,
        "error": "merge_blocked",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_promotion_failed_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_skips_on_single_branch(db_session, mocker):
    """On a single-branch repo the pending-auto-merge reconciler is inert: it
    performs NO GitHub I/O and leaves the version untouched."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request", new=AsyncMock(return_value={})
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="trunk"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/acme/widget/pull/42",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    get_pr.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


# ===========================================================================
# Custom integration/promotion pair — the multi-branch LTS model is active.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_promotion_failed_records_promotion_custom_branches(
    db_session, mocker
):
    """With a custom integration/promotion pair, a promotion_failed candidate
    that is in fact already on the promotion target reconciles to promoted —
    and the ancestry check runs against the configured promotion target."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    candidate_sha = "abcd" * 10
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": candidate_sha,
        "error": "merge_blocked",
        "confidence": 0.95,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_promotion_failed_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    # The ancestry check targeted the configured promotion target, not "main".
    assert is_ancestor.await_args.args[4] == "release"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_records_promotion_custom_branches(
    db_session, mocker
):
    """With a custom integration/promotion pair, a pending promotion PR whose
    candidate has landed on the promotion target reconciles to promoted."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    candidate_sha = "abcd" * 10
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "confidence": 0.95,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/acme/widget/pull/42",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    assert is_ancestor.await_args.args[4] == "release"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"


# ===========================================================================
# Terminal-PR sub-branches of _reconcile_if_pending_auto_merge_landed — the
# candidate is NOT yet on the promotion target, so the reconciler inspects the
# promotion PR's state and drives a closed-without-merge / CI-red PR to
# promotion_failed so the retry machinery re-applies.  Custom branch pair.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_pr_closed_without_merge(db_session, mocker):
    """A pending promotion PR closed without merging drives the version to
    promotion_failed (error=pr_closed_without_merge) so retry machinery applies."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    # Candidate has NOT landed on the promotion target — forces the PR-state path.
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=False)
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "confidence": 0.95,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/acme/widget/pull/42",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    # Ancestry was checked against the configured promotion target, not "main".
    assert is_ancestor.await_args.args[4] == "release"
    assert get_pr.await_args.args[3] == 42
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_closed_without_merge"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_pr_ci_red(db_session, mocker):
    """A pending promotion PR still open but with conclusively-failed CI drives
    the version to promotion_failed (error=pr_ci_red) so retry machinery applies."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=False)
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"sha": "deadbeef" * 5},
            }
        ),
    )
    # A completed build with a failure → _classify_check_runs → _CI_RED.
    check_runs = mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "failure"}),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "confidence": 0.95,
        "promotion_pending": {
            "pr_number": 43,
            "pr_url": "https://github.com/acme/widget/pull/43",
            "auto_merge_enabled": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_auto_merge_landed(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    assert is_ancestor.await_args.args[4] == "release"
    check_runs.assert_awaited()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "pr_ci_red"
    assert "promotion_pending" not in version.last_qualification


# ===========================================================================
# Pure-DB timeout backstop — never touches main, with or without a custom pair.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_promotion_pending_timeout_never_touches_main(db_session):
    """The promotion_pending wall-clock backstop is pure DB: a stranded pending
    sub-state is force-failed back to stabilize without any GitHub I/O, leaving
    the promotion target (main) untouched."""
    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "auto_merge_enabled": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    assert "promotion_pending" not in version.last_qualification
